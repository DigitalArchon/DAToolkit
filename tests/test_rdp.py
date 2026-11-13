"""RDP sessions through guacd: protocol, handshake, certificate pinning (trust on first use),
device linking, session-cut rules, and the browser <-> guacd relay."""

import asyncio
import json
import re
import datetime
import socket
import ssl
import struct
import threading
import warnings

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from starlette.testclient import TestClient

from datoolkit import creds
from datoolkit.config import Host
from datoolkit.engine import UserError
from datoolkit.llm import prompts
from datoolkit.safety.risk import session_impact
from datoolkit.server.app import create_app
from datoolkit.sessions import guac, rdpcert
from datoolkit.sessions.manager import RdpSession

from test_engine import env  # noqa: F401


# ---------------------------------------------------------------- protocol

def test_encode_and_incremental_parse():
    msg = (guac.encode("args", "VERSION_1_3_0", "hostname", "pässwörd;,.") + guac.encode("sync", 42)
           + guac.encode("", "ping", "1"))
    p, out = guac.Parser(), []
    for i in range(0, len(msg), 4):
        out += p.feed(msg[i:i + 4])
    assert out == [["args", "VERSION_1_3_0", "hostname", "pässwörd;,."], ["sync", "42"], ["", "ping", "1"]]
    assert guac.is_internal(guac.encode("", "ping", "1")) and not guac.is_internal(guac.encode("sync", 1))
    with pytest.raises(guac.GuacError):
        guac.Parser().feed("x.abc;")


@pytest.mark.parametrize("bad", [
    "0.,-4.", "4.sync,0.,-4.",            # a negative length moved the parser backwards: it never came out of these
    "-1.;", "1.a,-1.b;", " 3.abc;", "+3.abc;", "1_0.abcdefghij;", "\uff13.abc;", "3 .abc;", ".abc;", "0x3.abc;",
    "99999999999999999999.x", "1048577.x", "12345678", "9" * 5000,      # more than is ever taken, or no end to the digits
    "abc",                                                             # and not a length at all, dot or no dot
])
def test_the_parser_takes_only_plain_lengths(bad):
    import signal

    def stuck(*_):
        raise AssertionError(f"still parsing {bad!r}")
    old = signal.signal(signal.SIGALRM, stuck)
    signal.alarm(5)
    try:
        with pytest.raises(guac.GuacError, match="element"):
            guac.Parser().feed(bad)
        watch = guac.ConnectWatch()
        with pytest.raises(guac.GuacError):
            watch.feed(guac.encode("sync", "1") + bad)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_the_parser_waits_for_what_is_still_to_come():
    p = guac.Parser()
    assert p.feed("") == [] and p.feed("4") == [] and p.feed(".sy") == [] and p.feed("nc,10") == []
    assert p.feed("48576." + "x" * guac.MAX_ELEMENT) == []            # the longest element taken, 1048576 characters
    assert p.feed(";0.;") == [["sync", "x" * guac.MAX_ELEMENT], [""]]
    assert guac.Parser().feed(guac.encode("x", "a\U0001F600b")) == [["x", "a\U0001F600b"]]


# What guacd sends after `ready`, as recorded against Windows 11 (Nov 2026). guacd 1.3 starts
# with the joining user's display (no sync) and sends its first sync once RDP is up; 1.6 sends a
# sync straight away, ending a frame of only the pointer, before RDP connects.
_JOIN_13 = (guac.encode("size", "0", "1024", "768") + guac.encode("size", "-1", "11", "16")
            + guac.encode("img", "1", "14", "-1", "image/png", "0", "0") + guac.encode("blob", "1", "iVBORw0K")
            + guac.encode("end", "1") + guac.encode("cursor", "0", "0", "-1", "0", "0", "11", "16"))
_JOIN_16 = guac.encode("cursor", "0", "0", "-1", "0", "0", "0", "0") + guac.encode("mouse", "0", "0", "0") + guac.encode("sync", "100")
_DESKTOP = (guac.encode("size", "0", "1024", "768") + guac.encode("rect", "0", "0", "0", "1024", "768")
            + guac.encode("cfill", "14", "0", "0", "0", "0", "255") + guac.encode("sync", "200"))
GUACD_13_OK = _JOIN_13 + guac.encode("sync", "200") + guac.encode("copy", "-2", "0", "0", "41", "39", "14", "0", "5", "5")
GUACD_13_BADPASS = _JOIN_13 + guac.encode("error", "Authentication failure (invalid credentials?)", "769")
GUACD_16_OK = _JOIN_16 + _DESKTOP
GUACD_16_BADPASS = _JOIN_16 + guac.encode("error", "Authentication failure (invalid credentials?)", "769")
GUACD_16_NLA_EXT = _JOIN_16 + guac.encode("error", "Security negotiation failed (wrong security type?)", "769")


class FakeGuacd:
    """A guacd that offers a few args, records the handshake and what arrives afterwards."""

    def __init__(self, reply_ready=True, args=("VERSION_1_3_0", "hostname", "password", "ignore-cert"),
                 after=guac.encode("sync", "7")):
        self.args = list(args)
        self.after = after               # what it sends after `ready` (see GUACD_13_OK and the rest)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received = ""
        self.reply_ready = reply_ready
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        conn, _ = self.sock.accept()
        self.sock.close()                # one connection is all it takes
        conn.settimeout(5)
        parser, got = guac.Parser(), []
        try:
            while not any(i[0] == "connect" for i in got):
                data = conn.recv(65536)
                if not data:
                    return
                got += parser.feed(data.decode())
                if got and got[0][0] == "select" and len(got) == 1:
                    conn.sendall(guac.encode("args", *self.args).encode())
            self.handshake = got
            if not self.reply_ready:
                conn.sendall(guac.encode("error", "Aborted. See logs.", "519").encode())
                return
            if isinstance(self.after, bytes):            # bytes that aren't the protocol, a moment into the session
                conn.sendall(guac.encode("ready", "$abc").encode())
                threading.Event().wait(0.3)
                conn.sendall(self.after)
            else:
                conn.sendall((guac.encode("ready", "$abc") + self.after).encode())
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                self.received += data.decode()
        except OSError:
            return
        finally:
            conn.close()


async def test_handshake_answers_args_from_params():
    g = FakeGuacd()
    r, w = await asyncio.open_connection("127.0.0.1", g.port)
    cid, names, leftover, _ = await guac.handshake(r, w, "rdp", {"hostname": "dc01", "password": "p;w", "ignore-cert": "true"},
                                                   1024, 768, 96, "Australia/Brisbane")
    w.close()
    assert cid == "$abc" and names == ["VERSION_1_3_0", "hostname", "password", "ignore-cert"]
    ops = [i[0] for i in g.handshake]
    assert ops == ["select", "size", "audio", "video", "image", "timezone", "connect"]
    assert g.handshake[-1] == ["connect", "VERSION_1_3_0", "dc01", "p;w", "true"]
    assert g.handshake[1] == ["size", "1024", "768", "96"]
    assert leftover == guac.encode("sync", "7")          # nothing guacd sent after ready is lost


async def test_handshake_hands_the_pin_to_a_guacd_that_checks_it():
    """guacd 1.6 offers cert-fingerprints: it gets the pin and ignore-cert false; 1.3 doesn't,
    and ignore-cert stays true (DA Toolkit's own probe checked the certificate)."""
    params = {"hostname": "dc01", "password": "pw", "ignore-cert": "true", "cert-fingerprints": "sha256:ab:cd"}
    g = FakeGuacd(args=("VERSION_1_5_0", "hostname", "password", "ignore-cert", "cert-fingerprints", "cert-tofu"))
    r, w = await asyncio.open_connection("127.0.0.1", g.port)
    _, names, _, _ = await guac.handshake(r, w, "rdp", params, 800, 600)
    w.close()
    assert g.handshake[-1] == ["connect", "VERSION_1_5_0", "dc01", "pw", "false", "sha256:ab:cd", ""]
    assert guac.enforces_pin(names, params)
    g = FakeGuacd()
    r, w = await asyncio.open_connection("127.0.0.1", g.port)
    _, names, _, _ = await guac.handshake(r, w, "rdp", params, 800, 600)
    w.close()
    assert g.handshake[-1] == ["connect", "VERSION_1_3_0", "dc01", "pw", "true"]
    assert not guac.enforces_pin(names, params)
    assert guac.fingerprint_param("AB:CD:EF") == "sha256:ab:cd:ef"


async def test_handshake_error_from_guacd():
    g = FakeGuacd(reply_ready=False)
    r, w = await asyncio.open_connection("127.0.0.1", g.port)
    with pytest.raises(guac.GuacError, match="Aborted"):
        await guac.handshake(r, w, "rdp", {}, 800, 600)
    w.close()


async def _answering(*replies: bytes):
    """A server that answers what it is sent with `replies`, one for each time it is written to."""
    async def handle(reader, writer):
        try:
            for reply in replies:
                await reader.read(65536)
                writer.write(reply)
                await writer.drain()
            await reader.read(65536)
        except OSError:
            pass
        writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_an_answer_that_isnt_utf8_is_not_guacd(monkeypatch):
    server, port = await _answering(b"4.args,\xff\xfe\xfd;")
    async with server:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        with pytest.raises(guac.GuacError, match="isn't speaking the Guacamole protocol"):
            await guac.handshake(r, w, "rdp", {}, 800, 600)
        w.close()
        monkeypatch.setattr(guac, "GUACD", ("127.0.0.1", port))
        assert await guac.probe(2) is False


def test_guacd_sending_something_else_mid_session_ends_the_connection_with_a_reason(tmp_path, monkeypatch):
    e = _relay(tmp_path, monkeypatch, FakeGuacd(after=guac.encode("sync", "7").encode() + b"\xff\xfe not the protocol"),
               "RDP via guacd ended")
    assert _rdp_events(e) == [("rdp_connecting", {}), ("rdp_connect_failed", {
        "error": "what answered on guacd's port isn't speaking the Guacamole protocol (it sent bytes that aren't UTF-8)"})]


# ---------------------------------------------------------------- certificate probe

def _self_signed(tmp_path, cn="dc01.corp.example"):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365)).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / f"{cn}.pem", tmp_path / f"{cn}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path, rdpcert.fingerprint(cert.public_bytes(serialization.Encoding.DER))


async def fake_rdp_server(mode, cert=None):
    """mode: 'tls' (select TLS and do a handshake), 'legacy' (select standard RDP security),
    'refuse' (RDP_NEG_FAILURE)."""
    async def handle(reader, writer):
        head = await reader.readexactly(4)
        await reader.readexactly(struct.unpack(">H", head[2:4])[0] - 4)
        if mode in ("refuse", "ssl-not-allowed"):       # HYBRID_REQUIRED_BY_SERVER, SSL_NOT_ALLOWED_BY_SERVER
            neg = struct.pack("<BBHI", 0x03, 0, 8, 5 if mode == "refuse" else 2)
        else:
            neg = struct.pack("<BBHI", 0x02, 0, 8, 1 if mode == "tls" else 0)
        cc = bytes([6 + len(neg), 0xD0, 0, 0, 0x12, 0x34, 0]) + neg
        writer.write(struct.pack(">BBH", 3, 0, 4 + len(cc)) + cc)
        await writer.drain()
        if mode == "tls":
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*cert)
            try:
                await writer.start_tls(ctx)
                await reader.read(1)
            except (ssl.SSLError, ConnectionError):
                pass
        writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def test_fetch_reads_tls_certificate(tmp_path):
    c, k, fp = _self_signed(tmp_path)
    server, port = await fake_rdp_server("tls", (c, k))
    async with server:
        got = await rdpcert.fetch("127.0.0.1", port, "bob")
    assert got["tls"] and got["sha256"] == fp and "dc01.corp.example" in got["subject"] and got["self_signed"]


async def test_fetch_legacy_and_refused():
    server, port = await fake_rdp_server("legacy")
    async with server:
        assert (await rdpcert.fetch("127.0.0.1", port))["tls"] is False
    server, port = await fake_rdp_server("refuse")
    async with server:
        with pytest.raises(rdpcert.CertError, match="refused TLS"):
            await rdpcert.fetch("127.0.0.1", port)
    with pytest.raises(rdpcert.CertError, match="cannot reach"):
        await rdpcert.fetch("127.0.0.1", 1)
    # a server set to standard RDP security says TLS isn't allowed: legacy, not an error
    server, port = await fake_rdp_server("ssl-not-allowed")
    async with server:
        assert (await rdpcert.fetch("127.0.0.1", port))["tls"] is False


@pytest.mark.parametrize("answer", [
    b"\x03\x00\x00\x02",                  # a packet shorter than its own header: a negative read, unhandled
    b"\x03\x00\x00\x00", b"\x03\x00\x00\x04", b"\x03\x00\x00\x05\xd0",
])
async def test_fetch_takes_a_malformed_answer_for_what_it_is(answer):
    server, port = await _answering(answer)
    async with server:
        with pytest.raises(rdpcert.CertError, match="did not answer like an RDP server|refused the RDP connection|failed"):
            await rdpcert.fetch("127.0.0.1", port)


@pytest.mark.parametrize("fetch", [rdpcert.fetch, rdpcert.fetch_tls])
async def test_a_connection_that_fails_while_the_certificate_is_read_is_a_certificate_error(monkeypatch, fetch):
    class Reader:
        async def readexactly(self, n):
            raise OSError(113, "No route to host")           # the network went, between connecting and reading

    class Writer:
        def write(self, data):
            pass

        async def drain(self):
            pass

        async def start_tls(self, ctx):
            raise OSError(113, "No route to host")

        def close(self):
            pass

    async def connected(host, port):
        return Reader(), Writer()
    monkeypatch.setattr(asyncio, "open_connection", connected)
    with pytest.raises(rdpcert.CertError, match="10.250.0.9:3389 failed: .*No route to host"):
        await fetch("10.250.0.9", 3389)


async def test_a_host_set_to_legacy_security_is_warned_about_not_pinned(rdp_env, tmp_path):
    engine, events = rdp_env[0], rdp_env[1]
    c, k, fp = _self_signed(tmp_path)
    server, port = await fake_rdp_server("tls", (c, k))
    engine.cfg.hosts.append(Host("old", "rdp", "127.0.0.1", port=port, user="bob", auth="password", rdp_security="rdp"))
    creds.set_secret("host", "old", "pw")
    async with server:
        opener = asyncio.create_task(engine.open_rdp("old"))
        await answer_prompts(engine, ["yes"])
        r = await opener
    asked = [e["prompt"]["text"] for e in events if e["type"] == "prompt"]
    assert len(asked) == 1 and "set to legacy RDP security" in asked[0] and fp not in asked[0]
    assert engine.pins.get("127.0.0.1", port) is None                     # a certificate it doesn't use isn't pinned
    assert "cert-fingerprints" not in engine.rdp_params(r["id"]) and engine.rdp_params(r["id"])["security"] == "rdp"


# ---------------------------------------------------------------- engine: trust on first use

@pytest.fixture
def rdp_env(env, monkeypatch, tmp_path):  # noqa: F811
    engine, fake, events = env

    async def reachable():
        return True
    monkeypatch.setattr(engine, "_guacd_reachable", reachable)
    engine.new_case("rdp", "open")
    return engine, events, tmp_path


async def answer_prompts(engine, answers):
    """Answer credential/confirmation prompts in order as they appear."""
    for ans in answers:
        for _ in range(500):
            if engine._prompts:
                break
            await asyncio.sleep(0.01)
        pid, (_, info) = next(iter(engine._prompts.items()))
        engine.answer_prompt(pid, ans)
        await asyncio.sleep(0.02)
    return True


async def test_tofu_pins_then_trusts_then_blocks_a_change(rdp_env):
    engine, events, tmp_path = rdp_env
    c1, k1, fp1 = _self_signed(tmp_path, "dc01")
    server, port = await fake_rdp_server("tls", (c1, k1))
    engine.cfg.hosts.append(Host("dc01", "rdp", "127.0.0.1", port=port, user="CORP\\bob", auth="password"))
    async with server:
        # first use: certificate prompt, then password prompt
        opener = asyncio.create_task(engine.open_rdp("dc01"))
        await answer_prompts(engine, ["yes", "s3cret"])
        roster = await opener
        prompts_seen = [e["prompt"]["text"] for e in events if e["type"] == "prompt"]
        assert fp1 in prompts_seen[0] and "(yes/no)" in prompts_seen[0]
        assert roster["kind"] == "rdp" and roster["cert"]["sha256"] == fp1
        params = engine.rdp_params(roster["id"])
        assert params["username"] == "bob" and params["domain"] == "CORP" and params["password"] == "s3cret"
        assert params["ignore-cert"] == "true"
        assert params["cert-fingerprints"] == "sha256:" + fp1.lower()     # enforced by a guacd that can
        assert params["security"] == "tls"                               # what the server chose: no downgrade
        assert params["resize-method"] == "display-update"     # Fit to window resizes live where the server can
        assert engine.pins.get("127.0.0.1", port)["sha256"] == fp1
        assert engine.snapshot()["config"]["hosts"][-1]["pinned"] == fp1
        # second time: pinned, and the password comes from the keyring when saved
        creds.set_secret("host", "dc01", "s3cret")
        n = len([e for e in events if e["type"] == "prompt"])
        r2 = await engine.open_rdp("dc01")
        assert len([e for e in events if e["type"] == "prompt"]) == n and r2["id"] == "dc01-2"
    # the server's certificate changes: refused, whatever the technician would say
    c2, k2, fp2 = _self_signed(tmp_path, "evil")
    server, port2 = await fake_rdp_server("tls", (c2, k2))
    engine.cfg.hosts[-1].port = port2
    engine.pins.pin("127.0.0.1", port2, {"sha256": fp1, "subject": "", "issuer": "", "not_after": ""})
    async with server:
        with pytest.raises(UserError, match="CHANGED"):
            await engine.open_rdp("dc01")
        assert "rdp_cert_mismatch" in (engine.case.dir / "events.jsonl").read_text()
        assert engine.forget_pin("dc01") is True
        opener = asyncio.create_task(engine.open_rdp("dc01"))
        await answer_prompts(engine, ["no"])                 # technician declines the new certificate
        with pytest.raises(UserError, match="not trusted"):
            await opener


async def test_legacy_rdp_asks_every_time_and_guacd_missing(rdp_env, monkeypatch):
    engine, events, _ = rdp_env
    server, port = await fake_rdp_server("legacy")
    engine.cfg.hosts.append(Host("old", "rdp", "127.0.0.1", port=port, user="bob", auth="password"))
    creds.set_secret("host", "old", "pw")
    async with server:
        opener = asyncio.create_task(engine.open_rdp("old"))
        await answer_prompts(engine, ["yes"])
        assert (await opener)["cert"]["tls"] is False
        legacy = engine.rdp_params("old")
        assert legacy["security"] == "rdp" and "cert-fingerprints" not in legacy
        assert "no TLS certificate" in [e for e in events if e["type"] == "prompt"][-1]["prompt"]["text"]

    async def down():
        return False
    monkeypatch.setattr(engine, "_guacd_reachable", down)
    monkeypatch.setattr(guac, "install_help", lambda: ("guacd isn't installed.", ["sudo apt install guacd"]))
    with pytest.raises(UserError, match="isn't installed") as err:
        await engine.open_rdp("old")
    assert err.value.commands == ["sudo apt install guacd"]


_PIN = {"sha256": "AA:BB", "subject": "CN=a", "issuer": "CN=a", "not_after": "2030-01-01"}


def test_the_pin_file_is_replaced_whole_and_private(tmp_path, monkeypatch):
    import os
    import stat

    store = rdpcert.PinStore(tmp_path / "cfg" / "rdp_pins.json")
    assert store.get("dc01", 3389) is None and store.trusted("dc01", 3389) is None and store.forget("dc01", 3389) is False
    store.pin("DC01", 3389, _PIN)
    store.pin("fs01", 5986, {**_PIN, "sha256": "CC:DD"})
    assert store.trusted("dc01", 3389)["sha256"] == "AA:BB" and store.get("FS01", 5986)["sha256"] == "CC:DD"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    # every change goes through a file of its own and a rename, forgetting too (it wrote in place)
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: replaced.append((str(a), str(b))) or real(a, b))
    assert store.forget("dc01", 3389) is True and store.get("dc01", 3389) is None
    assert len(replaced) == 1 and replaced[0][1] == str(store.path) and replaced[0][0] != str(store.path)
    # a write that fails half way leaves the pins as they were, and no file of its own behind
    def no_space(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "fsync", no_space)
    with pytest.raises(OSError):
        store.pin("dc02", 3389, _PIN)
    with pytest.raises(OSError):
        store.forget("fs01", 5986)
    assert json.loads(store.path.read_text()).keys() == {"fs01:5986"}
    assert [f.name for f in store.path.parent.iterdir()] == ["rdp_pins.json"]


@pytest.mark.parametrize("damage", ["", '{"dc01:3389": {"sha2', "[]", '{"dc01:3389": "AA:BB"}', b"\xff\xfe\x00"])
def test_a_pin_file_that_cant_be_read_is_set_aside_not_taken_for_no_pins(tmp_path, damage):
    store = rdpcert.PinStore(tmp_path / "rdp_pins.json")
    store.pin("dc01", 3389, _PIN)
    store.pin("fs01", 5986, _PIN)
    store.path.write_bytes(damage if isinstance(damage, bytes) else damage.encode())
    assert store.get("dc01", 3389) is None                      # what Settings shows: no pin, and nothing changed
    assert store.path.read_bytes() == (damage if isinstance(damage, bytes) else damage.encode())
    with pytest.raises(rdpcert.PinsUnreadable, match=r"couldn't be read .* set aside as rdp_pins\.json\.unreadable-\d{8}-\d{6}"):
        store.pin("dc02", 3389, _PIN)                           # never written over with the one new pin
    aside = [f for f in tmp_path.iterdir() if ".unreadable-" in f.name]
    assert len(aside) == 1 and aside[0].read_bytes() == store.path.with_name(aside[0].name).read_bytes() and not store.path.exists()
    assert aside[0].read_bytes() == (damage if isinstance(damage, bytes) else damage.encode())
    # said once: from here on there are no pins, and new ones are kept
    assert store.trusted("dc01", 3389) is None
    store.pin("dc02", 3389, _PIN)
    assert store.trusted("dc02", 3389)["sha256"] == "AA:BB"


async def test_an_unreadable_pin_file_is_reported_before_anything_is_trusted(rdp_env, monkeypatch):
    engine, events, tmp_path = rdp_env
    engine.cfg.hosts.append(Host("dc05", "rdp", "10.250.0.5", user="bob", auth="password"))
    creds.set_secret("host", "dc05", "pw")

    async def fetch(host, port=3389, user=""):
        return dict(GOOD_CERT)
    monkeypatch.setattr(rdpcert, "fetch", fetch)
    engine.pins.pin("10.250.0.5", 3389, {**GOOD_CERT, "sha256": "the pin that was"})
    engine.pins.path.write_text("")                             # cut to nothing by a crash or a full disk
    assert engine.snapshot()["config"]["hosts"][-1]["pinned"] == ""
    with pytest.raises(UserError, match="couldn't be read .* set aside .* Nothing was sent to 10.250.0.5:3389"):
        await engine.open_rdp("dc05")
    assert not [e for e in events if e["type"] == "prompt"] and not engine.sessions.sessions
    assert "pins_unreadable" in (engine.case.dir / "events.jsonl").read_text()
    # the technician, told, connects again: a first connection, asked about
    opener = asyncio.create_task(engine.open_rdp("dc05"))
    await answer_prompts(engine, ["yes"])
    await opener
    assert "First connection to 10.250.0.5:3389" in [e for e in events if e["type"] == "prompt"][0]["prompt"]["text"]
    assert engine.pins.trusted("10.250.0.5", 3389)["sha256"] == "AA:BB"
    # removing a pin from a file that can't be read says so too
    engine.pins.path.write_text("{")
    with pytest.raises(UserError, match="set aside .* There is no pin left to remove"):
        engine.forget_pin("dc05")
    kept = sorted(f.read_text() for f in engine.pins.path.parent.iterdir() if ".unreadable-" in f.name)
    assert kept == ["", "{"]                                    # each kept, the second not over the first


async def _prompts(engine, n, secs=5.0):
    for _ in range(int(secs * 100)):
        if len(engine._prompts) >= n:
            return list(engine._prompts)
        await asyncio.sleep(0.01)
    raise AssertionError(f"fewer than {n} prompts are up")


async def test_two_opens_of_one_rdp_host_are_two_sessions(rdp_env, monkeypatch):
    """Open clicked twice, both waiting at the password prompt: both were given the id `dc03`,
    and the second to finish replaced the first."""
    engine = rdp_env[0]
    engine.cfg.hosts.append(Host("dc03", "rdp", "10.250.0.3", user="bob", auth="password"))

    async def fetch(host, port=3389, user=""):
        await asyncio.sleep(0.02)                         # the probe of the server's certificate
        return dict(GOOD_CERT)
    monkeypatch.setattr(rdpcert, "fetch", fetch)
    engine.pins.pin("10.250.0.3", 3389, GOOD_CERT)
    first, second = asyncio.create_task(engine.open_rdp("dc03")), asyncio.create_task(engine.open_rdp("dc03"))
    for pid in await _prompts(engine, 2):
        engine.answer_prompt(pid, "pw-" + engine._prompts[pid][1]["session_id"])
    a, b = await first, await second
    assert {a["id"], b["id"]} == {"dc03", "dc03-2"} and set(engine.sessions.sessions) == {"dc03", "dc03-2"}
    assert {sid: engine.rdp_params(sid)["password"] for sid in ("dc03", "dc03-2")} == {"dc03": "pw-dc03", "dc03-2": "pw-dc03-2"}
    assert not engine.sessions._reserved


async def test_a_session_opened_while_an_rdp_host_waits_doesnt_get_its_id(rdp_env, monkeypatch):
    """An RDP open at its password prompt, and a terminal session taking the same id meanwhile:
    the RDP session then replaced it, and its program ran on, logged in, with no tab."""
    engine = rdp_env[0]
    engine.cfg.hosts.append(Host("gw", "rdp", "10.250.0.4", user="bob", auth="password"))

    async def fetch(host, port=3389, user=""):
        return dict(GOOD_CERT)
    monkeypatch.setattr(rdpcert, "fetch", fetch)
    engine.pins.pin("10.250.0.4", 3389, GOOD_CERT)
    opener = asyncio.create_task(engine.open_rdp("gw"))
    (pid,) = await _prompts(engine, 1)
    pty = engine.sessions.spawn(engine.sessions.unique_id("GW"), ["/bin/cat"], {}, name="GW", kind="ssh")
    assert pty.id == "gw-2"
    engine.answer_prompt(pid, "pw")
    assert (await opener)["id"] == "gw"
    assert engine.sessions.sessions["gw-2"] is pty and isinstance(engine.sessions.sessions["gw"], RdpSession)
    # an open that comes to nothing gives its id back
    opener = asyncio.create_task(engine.open_rdp("gw"))
    (pid,) = await _prompts(engine, 1)
    assert engine.sessions._reserved == {"gw-3"}
    engine.answer_prompt(pid, None)
    with pytest.raises(UserError, match="No password given"):
        await opener
    assert not engine.sessions._reserved and engine.sessions.unique_id("gw") == "gw-3"
    engine.sessions.close_all()
    await engine.sessions.wait_closed()
    assert pty.proc.popen.poll() is not None


async def test_a_pinned_host_that_stops_offering_tls_is_refused(rdp_env, tmp_path):
    engine, events, _ = rdp_env
    server, port = await fake_rdp_server("legacy")
    engine.cfg.hosts.append(Host("dc02", "rdp", "127.0.0.1", port=port, user="bob", auth="password"))
    creds.set_secret("host", "dc02", "pw")
    engine.pins.pin("127.0.0.1", port, {"sha256": "AA:BB", "subject": "CN=dc02", "issuer": "CN=dc02", "not_after": "2030-01-01"})
    pinned_on = engine.pins.get("127.0.0.1", port)["pinned"]
    async with server:
        # the answer "no TLS here" is unencrypted: with a pin on file it is no question to say yes to
        with pytest.raises(UserError) as err:
            await engine.open_rdp("dc02")
        text = str(err.value)
        assert f"pinned on {pinned_on}" in text and "NO TLS" in text and "AA:BB" in text
        assert "intercepting" in text and "remove the pin under Settings → Hosts" in text
        assert not [e for e in events if e["type"] == "prompt"] and not engine.sessions.sessions
        logged = [json.loads(line) for line in (engine.case.dir / "events.jsonl").read_text().splitlines()]
        assert [(e["pinned"], e["got"]) for e in logged if e["event"] == "rdp_cert_mismatch"] == [("AA:BB", "")]
        assert not [e for e in logged if e["event"] == "rdp_no_tls"]
        # the technician's own choice of legacy security for the host is asked about as before
        engine.cfg.hosts[-1].rdp_security = "rdp"
        opener = asyncio.create_task(engine.open_rdp("dc02"))
        await answer_prompts(engine, ["yes"])
        assert (await opener)["cert"]["tls"] is False
        # and with the pin removed, the server's own legacy answer is a question again
        engine.cfg.hosts[-1].rdp_security = "any"
        assert engine.forget_pin("dc02") is True
        opener = asyncio.create_task(engine.open_rdp("dc02"))
        await answer_prompts(engine, ["no"])
        with pytest.raises(UserError, match="no TLS"):
            await opener


def _certificate_named(cn: str) -> dict:
    key = ec.generate_private_key(ec.SECP256R1())
    with warnings.catch_warnings():                    # longer than a common name may be
        warnings.simplefilter("ignore", UserWarning)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn, _validate=False)])
    now = datetime.datetime.now(datetime.timezone.utc)
    der = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
           .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=30))
           .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.DER))
    return {"tls": True, "protocol": "nla", "sha256": rdpcert.fingerprint(der), "sha1": rdpcert.thumbprint(der),
            **rdpcert.describe(der)}


@pytest.mark.parametrize("kind", ["rdp", "winrm"])
async def test_a_certificates_own_text_cant_draw_a_fingerprint_line(rdp_env, kind):
    engine, events, _ = rdp_env
    forged = "3F:9A:" + ":".join(["11"] * 30)            # the fingerprint someone wants the technician to read
    cert = _certificate_named("SRV01.corp.example\nIssuer: CN=SRV01.corp.example (self-signed)\r\nValid until: "
                              f"2027-05-01\nSHA-256: {forged}\u2028\x1b[2J\u202e" + "\n" * 40 + "(details)")
    assert not any(ch in cert["subject"] + cert["issuer"] for ch in "\n\r\x1b\u2028\u202e")
    opener = asyncio.create_task(engine._trust_cert("s", "10.99.0.3", 3389, cert, kind))
    await answer_prompts(engine, ["no"])
    with pytest.raises(UserError, match="not trusted"):
        await opener
    lines = [e for e in events if e["type"] == "prompt"][-1]["prompt"]["text"].splitlines()
    # the real fingerprints directly under the first line, above anything the certificate says
    assert lines[1] == f"SHA-256: {cert['sha256']}" and lines[2].endswith(cert["sha1"])
    assert lines[3].startswith("Subject: ") and forged in lines[3] and lines[4].startswith("Issuer: ")
    assert [n for n, line in enumerate(lines) if line.startswith("SHA-256:")] == [1] and len(lines) < 10
    # and a name of any length is cut
    long = rdpcert.describe.__globals__["_one_line"]("x" * 5000)
    assert len(long) == rdpcert.NAME_LIMIT and long.endswith("…")
    assert len(_certificate_named("a" * 60 + " " + "b" * 60 + " " + "c" * 60 + " " + "d" * 60)["subject"]) == rdpcert.NAME_LIMIT


def test_guacd_install_help_per_distribution():
    def which_of(*present):
        return lambda name: f"/usr/bin/{name}" if name in present else None

    from types import SimpleNamespace

    def apt(candidate):      # what `apt-cache policy guacd` prints: Ubuntu 24.04's, or 26.04's nothing
        out = f"guacd:\n  Installed: (none)\n  Candidate: {candidate}\n" if candidate else ""
        return lambda argv, timeout=30: SimpleNamespace(stdout=out, returncode=0)

    def help_for(release, *present, run=apt("1.3.0-1.3ubuntu1")):
        return guac.install_help(release, which_of(*present), installed=False, run=run)
    pull = f"podman pull {guac.GUACD_IMAGE}"
    assert "@sha256:" in guac.GUACD_IMAGE                       # pinned
    mint = {"ID": "linuxmint", "ID_LIKE": "ubuntu debian", "NAME": "Linux Mint"}
    assert help_for(mint)[1] == ["sudo apt install guacd"]
    # Ubuntu 26.04 has no guacd (seen on the lab's 26.04.1, Nov 2026): the container, as on Debian
    noble_next = {"ID": "ubuntu", "ID_LIKE": "debian", "NAME": "Ubuntu", "PRETTY_NAME": "Ubuntu 26.04.1 LTS"}
    text, cmds = help_for(noble_next, run=apt(""))
    assert "Ubuntu 26.04.1 LTS doesn't package it" in text and cmds == ["sudo apt install podman", pull]
    assert help_for(noble_next, "podman", run=apt("(none)"))[1] == [pull]
    assert guac.apt_has("guacd", apt("1.3.0")) is True and guac.apt_has("guacd", apt("(none)")) is False

    def no_apt(argv, timeout=30):
        raise FileNotFoundError(argv[0])
    assert guac.apt_has("guacd", no_apt) is None
    assert help_for(mint, run=no_apt)[1] == ["sudo apt install guacd"]     # can't ask: as before
    text, cmds = help_for({"ID": "fedora", "NAME": "Fedora Linux"})
    assert cmds == ["sudo dnf install guacd libguac-client-rdp", "sudo systemctl enable --now guacd"]
    assert "EPEL" not in text
    assert "EPEL" in help_for({"ID": "rocky", "ID_LIKE": "rhel centos fedora"})[0]
    # Arch and its derivatives: no apt, and the AUR package doesn't build as is, so the container,
    # which DA Toolkit runs only while an RDP session is open: just download it once
    cachy = {"ID": "cachyos", "ID_LIKE": "arch", "NAME": "CachyOS Linux"}
    text, cmds = help_for(cachy)
    assert "AUR" in text and "only from this machine" in text and "only while an RDP session is open" in text
    assert cmds == ["sudo pacman -S --needed podman", pull]
    assert help_for(cachy, "podman")[1] == [pull]
    assert help_for(cachy, "docker")[1] == [pull.replace("podman", "docker", 1)]
    assert not any("restart" in c for c in help_for(cachy, "podman")[1])   # never left running
    # Debian 13 ships no guacd either
    text, cmds = help_for({"ID": "debian", "NAME": "Debian GNU/Linux"})
    assert "Debian GNU/Linux doesn't package it" in text and cmds == ["sudo apt install podman", pull]
    assert help_for({"ID": "gentoo"})[1] == [pull]
    # installed but stopped: start it, whatever the distribution
    assert guac.install_help(cachy, which_of(), installed=True)[1] == ["sudo systemctl enable --now guacd"]


@pytest.fixture(autouse=True)
def own_lock_dir(tmp_path, monkeypatch):
    """The guacd container's locks in the test's own folder: never the real runtime directory,
    where a DA Toolkit that is running holds them."""
    (tmp_path / "run").mkdir()
    monkeypatch.setattr(guac, "lock_dir", lambda: tmp_path / "run")


class FakeRuntime:
    """podman as GuacdContainer sees it: records commands, answers like the real one."""

    def __init__(self, image=True, run_error=""):
        self.calls, self.image, self.run_error, self.running = [], image, run_error, False

    def which(self, name):
        return "/usr/bin/podman" if name == "podman" else None

    def run(self, argv, timeout=30):
        self.calls.append(argv)
        ok = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})
        fail = type("R", (), {"returncode": 125, "stdout": "", "stderr": "Error: something\nError: " + self.run_error})
        if argv[1:3] == ["image", "exists"]:
            return ok if self.image else fail
        if argv[1] == "run":
            if self.run_error:
                return fail
            self.running = True
        if argv[1] == "rm":
            self.running = False
        if argv[1:3] == ["container", "inspect"]:
            return type("R", (), {"returncode": 0 if self.running else 125, "stdout": "true\n" if self.running else "",
                                  "stderr": ""})
        return ok


def test_guacd_container_runs_pinned_on_loopback_and_stops():
    rt = FakeRuntime()
    c = guac.GuacdContainer(rt.which, rt.run)
    assert c.usable() == "podman"
    c.start()
    assert c.runtime == "podman"
    assert rt.calls[-2] == ["podman", "rm", "-f", guac.CONTAINER]          # a stopped leftover goes first
    # never downloaded by the app, and given nothing: it reads what hostile servers send
    assert rt.calls[-1] == ["podman", "run", "-d", "--rm", "--pull=never", "--cap-drop=ALL",
                            "--security-opt", "no-new-privileges", "--name", guac.CONTAINER,
                            "-p", "127.0.0.1:4822:4822", guac.GUACD_IMAGE]
    assert c.stop() is True
    assert rt.calls[-1] == ["podman", "rm", "-f", "-t", "0", guac.CONTAINER] and c.runtime is None
    n = len(rt.calls)
    c.stop()                                                               # not ours any more: nothing to do
    assert len(rt.calls) == n
    # left running by a run that didn't end cleanly: taken over, so it's stopped later
    rt.running = True
    c.adopt()
    assert c.runtime == "podman"
    assert guac.GuacdContainer(FakeRuntime(image=False).which, FakeRuntime(image=False).run).usable() is None
    bad = FakeRuntime(run_error="port 4822 already in use")
    with pytest.raises(guac.GuacError, match="already in use"):
        guac.GuacdContainer(bad.which, bad.run).start()


def _removals(rt) -> int:
    return len([c for c in rt.calls if c[1] == "rm" and "-t" in c])


def test_one_da_toolkit_doesnt_remove_the_guacd_another_is_using(tmp_path):
    """Two running DA Toolkits (two tickets), one container: the second took over the first
    one's and removed it when its own last RDP session closed, the first one's desktops with it."""
    rt = FakeRuntime()                                    # the one podman both instances talk to
    a, b = guac.GuacdContainer(rt.which, rt.run), guac.GuacdContainer(rt.which, rt.run)
    a.start()
    b.adopt()                                             # guacd answers: B's first RDP session shares it
    assert a.runtime == b.runtime == "podman" and len([c for c in rt.calls if c[1] == "run"]) == 1
    assert b.stop() is False and rt.running and b.runtime is None      # B's last session closed: A's stay up
    b.start()                                             # B opens another: no second container, no removal
    assert rt.running and b.runtime == "podman" and len([c for c in rt.calls if c[1] == "run"]) == 1
    assert a.stop() is False and rt.running               # and the other way round
    assert b.stop() is True and not rt.running and _removals(rt) == 1   # the last one out removes it
    assert a._using is None and b._using is None


def test_a_da_toolkit_that_crashed_doesnt_keep_the_guacd_container_alive(tmp_path):
    import os

    rt = FakeRuntime()
    a, b = guac.GuacdContainer(rt.which, rt.run), guac.GuacdContainer(rt.which, rt.run)
    a.start()
    b.adopt()
    os.close(a._using)                                    # A is killed: the kernel drops its lock with it
    assert b.stop() is True and not rt.running
    # and its container, left running, is taken over by the next instance and removed after use
    rt.running = True
    c = guac.GuacdContainer(rt.which, rt.run)
    c.adopt()
    assert c.runtime == "podman" and c.stop() is True and not rt.running


def test_without_a_lock_file_an_instance_acts_as_the_only_one(tmp_path):
    rt = FakeRuntime()
    c = guac.GuacdContainer(rt.which, rt.run, lock_dir=tmp_path / "no such folder")
    c.start()
    assert rt.running and c._using is None
    assert c.stop() is True and not rt.running


async def test_closing_the_last_rdp_session_leaves_the_container_to_another_da_toolkit(container_env):
    engine, _, rt = container_env
    other = guac.GuacdContainer(rt.which, rt.run)         # a second DA Toolkit, with a desktop of its own open
    server, port = await fake_rdp_server("legacy")
    engine.cfg.hosts.append(Host("old", "rdp", "127.0.0.1", port=port, user="bob", auth="password"))
    creds.set_secret("host", "old", "pw")
    async with server:
        opener = asyncio.create_task(engine.open_rdp("old"))
        await answer_prompts(engine, ["yes"])
        a = await opener
        other.adopt()
        engine.close_session(a["id"])
        await _settle(engine)
        assert rt.running and engine.guacd.runtime is None and _removals(rt) == 0
        log = (engine.case.dir / "events.jsonl").read_text()
        assert '"action": "left"' in log and '"action": "stop"' not in log
        # the other one closes its own: gone; and this one starts it again when it needs it
        assert other.stop() is True and not rt.running
        opener = asyncio.create_task(engine.open_rdp("old"))
        await answer_prompts(engine, ["yes"])
        await opener
        assert rt.running and engine.guacd.runtime == "podman"


@pytest.fixture
def container_env(env, monkeypatch):  # noqa: F811
    """An engine whose guacd is our container: not answering until started."""
    engine, _, events = env
    rt = FakeRuntime()
    engine.guacd = guac.GuacdContainer(rt.which, rt.run)

    async def reachable():
        return rt.running

    async def probe(timeout=3):
        return rt.running
    monkeypatch.setattr(engine, "_guacd_reachable", reachable)
    monkeypatch.setattr(guac, "probe", probe)
    engine.new_case("rdp", "open")
    return engine, events, rt


async def _settle(engine):
    for _ in range(50):
        if not engine._background:
            return
        await asyncio.sleep(0.02)


async def test_guacd_container_lives_only_while_rdp_is_open(container_env):
    engine, events, rt = container_env
    server, port = await fake_rdp_server("legacy")
    engine.cfg.hosts.append(Host("old", "rdp", "127.0.0.1", port=port, user="bob", auth="password"))
    creds.set_secret("host", "old", "pw")
    async with server:
        opener = asyncio.create_task(engine.open_rdp("old"))
        await answer_prompts(engine, ["yes"])
        a = await opener
        assert rt.running and engine.guacd.runtime == "podman"
        assert any(e["type"] == "toast" and "Starting guacd" in e["text"] for e in events)
        opener = asyncio.create_task(engine.open_rdp("old"))
        await answer_prompts(engine, ["yes"])
        b = await opener
        assert sum(1 for c in rt.calls if c[1] == "run") == 1          # one container for both
        # a session that fails to open doesn't stop it while another needs it ...
        opener = asyncio.create_task(engine.open_rdp("old"))
        await answer_prompts(engine, ["no"])
        with pytest.raises(UserError):
            await opener
        await _settle(engine)
        assert rt.running
    engine.close_session(a["id"])
    await _settle(engine)
    assert rt.running                                                   # b still open
    engine.close_session(b["id"])
    await _settle(engine)
    assert not rt.running and engine.guacd.runtime is None
    log = (engine.case.dir / "events.jsonl").read_text()
    assert '"action": "start"' in log and '"action": "stop"' in log


async def test_guacd_container_stays_for_an_rdp_session_still_opening(container_env):
    # host B's first-use prompt is up (it can be for minutes) when session A closes: the
    # container must stay, or B registers with no guacd behind its tunnel
    engine, _, rt = container_env
    server, port = await fake_rdp_server("legacy")
    engine.cfg.hosts.append(Host("old", "rdp", "127.0.0.1", port=port, user="bob", auth="password"))
    creds.set_secret("host", "old", "pw")
    async with server:
        opener = asyncio.create_task(engine.open_rdp("old"))
        await answer_prompts(engine, ["yes"])
        a = await opener
        events_before = len([c for c in rt.calls if c[1] == "stop"])
        opener = asyncio.create_task(engine.open_rdp("old"))              # B: its prompt waits ...
        for _ in range(100):
            if engine._prompts:
                break
            await asyncio.sleep(0.01)
        engine.close_session(a["id"])                                        # ... while A closes
        await _settle(engine)
        assert rt.running and len([c for c in rt.calls if c[1] == "stop"]) == events_before
        await answer_prompts(engine, ["yes"])
        b = await opener
        assert rt.running
    engine.close_session(b["id"])
    await _settle(engine)
    assert not rt.running


async def test_guacd_container_stops_when_rdp_fails_to_open_and_on_quit(container_env):
    engine, _, rt = container_env
    server, port = await fake_rdp_server("legacy")
    engine.cfg.hosts.append(Host("old", "rdp", "127.0.0.1", port=port, user="bob", auth="password"))
    async with server:
        opener = asyncio.create_task(engine.open_rdp("old"))
        await answer_prompts(engine, ["no"])               # legacy security declined: no session
        with pytest.raises(UserError):
            await opener
        await _settle(engine)
        assert not rt.running                              # started for nothing, so stopped again
        creds.set_secret("host", "old", "pw")
        opener = asyncio.create_task(engine.open_rdp("old"))
        await answer_prompts(engine, ["yes"])
        await opener
        assert rt.running
    await engine.stop()                                    # the app quits with the session open
    assert not rt.running


async def test_guacd_container_failures_are_explained(container_env, monkeypatch):
    engine, _, rt = container_env
    engine.cfg.hosts.append(Host("old", "rdp", "127.0.0.1", port=3389, user="bob", auth="password"))
    rt.run_error = "rootlessport listen tcp 127.0.0.1:4822: bind: address already in use"
    with pytest.raises(UserError, match="Couldn't start guacd's container: .*address already in use"):
        await engine.open_rdp("old")
    rt.run_error = ""

    async def never(timeout=3):
        return False
    monkeypatch.setattr(guac, "probe", never)
    monkeypatch.setattr(guac, "CONTAINER_START_WAIT", 0.5)
    with pytest.raises(UserError, match="didn't answer"):
        await engine.open_rdp("old")
    assert not rt.running                                  # not left behind
    rt.image = False                                       # no image: the download instructions
    monkeypatch.setattr(guac, "install_help", lambda: ("guacd isn't installed.", ["podman pull x"]))
    with pytest.raises(UserError, match="isn't installed"):
        await engine.open_rdp("old")


def test_os_release_parsing(tmp_path):
    f = tmp_path / "os-release"
    f.write_text('NAME="CachyOS Linux"\nID=cachyos\nID_LIKE=arch\n# comment\n')
    assert guac.os_release(str(f)) == {"NAME": "CachyOS Linux", "ID": "cachyos", "ID_LIKE": "arch"}
    assert guac.os_release(str(tmp_path / "missing")) == {}


def test_user_error_commands_reach_the_page(tmp_path, monkeypatch):
    holder = {}

    def make(emit):
        from datoolkit.config import Config
        from datoolkit.engine import Engine
        holder["e"] = Engine(Config(), emit, tmp_path / "rt", save_config=lambda c: None)
        return holder["e"]

    async def down():
        return False
    monkeypatch.setattr(guac, "install_help", lambda: ("guacd isn't installed.", ["a", "b"]))
    with TestClient(create_app("tok", make)) as client:
        e = holder["e"]
        e.new_case("guacd", "open")
        e.cfg.hosts.append(Host(name="ts9", kind="rdp", host="ts9.lan", user="bob"))
        monkeypatch.setattr(e, "_guacd_reachable", down)
        r = client.post("/api/sessions", json={"kind": "rdp", "host": "ts9"}, headers={"X-Token": "tok"})
        assert r.status_code == 400 and r.json() == {"error": "guacd isn't installed.", "commands": ["a", "b"]}
        r = client.post("/api/sessions", json={"kind": "rdp", "host": "nope"}, headers={"X-Token": "tok"})
        assert r.json() == {"error": "Unknown RDP host nope"}


def test_host_validation_for_rdp(env):  # noqa: F811
    engine, _, _ = env
    engine.save_host({"name": "ts1", "kind": "rdp", "host": "ts1.lan", "user": "bob", "rdp_security": "nla",
                      "rdp_layout": "en-gb-qwerty"})
    h = engine.cfg.host("ts1")
    assert (h.kind, h.rdp_security, h.rdp_layout) == ("rdp", "nla", "en-gb-qwerty")
    with pytest.raises(UserError):
        engine.save_host({"name": "x", "kind": "rdp", "host": "x", "rdp_security": "yolo"})
    with pytest.raises(UserError, match="open_rdp"):
        engine.new_case("c", "open") or engine.open_session("rdp", "ts1")


# ---------------------------------------------------------------- device linking

def _rdp(engine, sid, address):
    return engine.sessions.add(RdpSession(id=sid, name=sid, params={}, target=f"{address}:3389", address=address,
                                          os_hint="Windows Server 2022"))


async def test_auto_link_manual_link_and_unlink(env):  # noqa: F811
    engine, _, _ = env
    engine.new_case("link", "open")
    engine.sessions.spawn("dc01", ["/bin/cat"], {}, name="dc01", kind="winrm", target="dc01.corp", address="dc01.corp")
    _rdp(engine, "dc01-rdp", "DC01.corp")                        # same address, different case: linked
    _rdp(engine, "fs01-rdp", "10.0.0.5")
    ids = {s["id"]: s["device"] for s in engine.sessions.roster()}
    assert ids["dc01"] == ids["dc01-rdp"] != ids["fs01-rdp"]
    text = prompts.session_roster(engine.sessions.roster())
    assert "the SAME machine" in text and "send commands to `dc01`" in text and "`dc01-rdp` is what the technician sees" in text
    assert "TYPED into whatever window has focus" in text

    engine.link_session("dc01-rdp", None)                        # unlink: stays unlinked
    assert engine.sessions.sessions["dc01-rdp"].link == "unlinked"
    assert len({s["device"] for s in engine.sessions.roster()}) == 3
    engine.sessions.spawn("dc01-b", ["/bin/cat"], {}, name="dc01-b", kind="ssh", target="dc01.corp", address="dc01.corp")
    devs = {s["id"]: s["device"] for s in engine.sessions.roster()}
    assert devs["dc01-b"] == devs["dc01"] != devs["dc01-rdp"]   # a new session auto-links to the linked one only
    engine.link_session("fs01-rdp", "dc01")                     # manual: hostname vs IP
    assert engine.sessions.sessions["fs01-rdp"].device == devs["dc01"]
    assert engine.sessions.sessions["fs01-rdp"].link == "manual"
    with pytest.raises(UserError):
        engine.link_session("nope", None)
    assert "session_unlinked" in (engine.case.dir / "events.jsonl").read_text()
    engine.sessions.close_all()


def test_rdp_session_cut_rules():
    assert "Remote Desktop Services" in session_impact("Restart-Service TermService -Force", "rdp")
    assert session_impact("logoff", "rdp") and session_impact("Disable-NetAdapter -Name Ethernet", "rdp")
    assert session_impact("Restart-Service WinRM", "rdp") is None      # doesn't carry a desktop
    assert session_impact("Get-EventLog System -Newest 20", "rdp") is None


# ---------------------------------------------------------------- the relay

def test_ws_relay_handshakes_pings_and_forwards(tmp_path, monkeypatch):
    g = FakeGuacd(after=GUACD_13_OK)
    monkeypatch.setattr(guac, "GUACD", ("127.0.0.1", g.port))
    token = "tok"
    holder = {}

    def make(emit):
        from datoolkit.config import Config
        from datoolkit.engine import Engine
        e = Engine(Config(), emit, tmp_path / "rt", save_config=lambda c: None)
        holder["e"] = e
        return e

    app = create_app(token, make)
    with TestClient(app) as client:
        e = holder["e"]
        e.new_case("relay", "open")
        e.sessions.add(RdpSession(id="dc01", name="dc01", params={"hostname": "dc01", "password": "pw", "ignore-cert": "true"}))
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/rdp/dc01?t=wrong", subprotocols=["guacamole"]) as ws:
                ws.receive_text()
        with client.websocket_connect("/ws/rdp/dc01?t=tok&width=1000&height=700", subprotocols=["guacamole"]) as ws:
            got = ws.receive_text()
            while not got.endswith(guac.encode("copy", "-2", "0", "0", "41", "39", "14", "0", "5", "5")):
                got += ws.receive_text()
            ins = guac.Parser().feed(got)
            assert ins[0][0] == "" and len(ins[0]) == 2                  # the tunnel's uuid first
            assert "".join(guac.encode(*i) for i in ins[1:]) == GUACD_13_OK     # then guacd's data, untouched
            ping = guac.encode("", "ping", "123")
            ws.send_text(ping)
            assert ws.receive_text() == ping                     # answered here, not sent to guacd
            ws.send_text(guac.encode("key", "65", "1"))
            ws.send_text(guac.encode("size", "1200", "640"))     # Fit to window: passed on for display-update
            for _ in range(100):
                if "size" in g.received:
                    break
                threading.Event().wait(0.02)
        assert g.handshake[-1] == ["connect", "VERSION_1_3_0", "dc01", "pw", "true"]
        assert g.handshake[1] == ["size", "1000", "700", "96"]
        assert g.received == guac.encode("key", "65", "1") + guac.encode("size", "1200", "640")
        for _ in range(100):
            if "rdp_disconnected" in (e.case.dir / "events.jsonl").read_text():
                break
            threading.Event().wait(0.02)
        assert _rdp_events(e) == [("rdp_connecting", {}), ("rdp_connected", {"pin_enforced": None}), ("rdp_disconnected", {})]


def _rdp_events(e) -> list:
    out = []
    for line in (e.case.dir / "events.jsonl").read_text().splitlines():
        ev = json.loads(line)
        if ev.get("event", "").startswith("rdp_"):
            out.append((ev["event"], {k: v for k, v in ev.items() if k not in ("event", "ts", "session_id")}))
    return out


def _relay(tmp_path, monkeypatch, g, until):
    """Open an RDP session's tunnel against the fake guacd `g`, read until `until` is in what
    arrives, close the page's side; returns the engine."""
    monkeypatch.setattr(guac, "GUACD", ("127.0.0.1", g.port))
    holder = {}

    def make(emit):
        from datoolkit.config import Config
        from datoolkit.engine import Engine
        holder["e"] = Engine(Config(), emit, tmp_path / "rt", save_config=lambda c: None)
        return holder["e"]

    with TestClient(create_app("tok", make)) as client:
        e = holder["e"]
        e.new_case("relay", "open")
        e.sessions.add(RdpSession(id="dc01", name="dc01", params={"hostname": "dc01", "password": "pw", "ignore-cert": "true"}))
        with client.websocket_connect("/ws/rdp/dc01?t=tok", subprotocols=["guacamole"]) as ws:
            got = ""
            while until not in got:
                got += ws.receive_text()
        for _ in range(100):
            if any(k in ("rdp_disconnected", "rdp_connect_failed") for k, _ in _rdp_events(e)):
                break
            threading.Event().wait(0.02)
    return e


@pytest.mark.parametrize("stream, events", [
    # a wrong password: guacd 1.3 and 1.6 both said ready, and the RDP connection then failed
    (GUACD_13_BADPASS, [("rdp_connecting", {}), ("rdp_connect_failed", {"error": "Authentication failure (invalid credentials?)",
                                                                          "code": "769"})]),
    (GUACD_16_BADPASS, [("rdp_connecting", {}), ("rdp_connect_failed", {"error": "Authentication failure (invalid credentials?)",
                                                                          "code": "769"})]),
    # guacd 1.6's first sync comes before RDP connects: the desktop only with the next one
    (GUACD_16_OK, [("rdp_connecting", {}), ("rdp_connected", {"pin_enforced": None}), ("rdp_disconnected", {})]),
])
def test_the_audit_log_says_whether_the_desktop_connected(tmp_path, monkeypatch, stream, events):
    """Found against Windows 11 (Nov 2026): rdp_connected was logged once guacd had taken the
    parameters, before the RDP connection was tried, so a wrong password read as connected."""
    last = guac.Parser().feed(stream)[-1]
    e = _relay(tmp_path, monkeypatch, FakeGuacd(after=stream), guac.encode(*last))
    assert _rdp_events(e) == events
    tl = [x["text"] for x in e.timeline()["events"] if x["kind"].startswith("rdp_")]
    assert tl[-1] == (f"Remote desktop dc01 did not connect: {events[-1][1]['error']}" if events[-1][0] == "rdp_connect_failed"
                      else "Remote desktop dc01 disconnected")


def test_guacd_refusing_the_handshake_is_a_failed_connection(tmp_path, monkeypatch):
    e = _relay(tmp_path, monkeypatch, FakeGuacd(reply_ready=False), "Could not start RDP via guacd")
    assert _rdp_events(e) == [("rdp_connect_failed", {"error": "guacd: Aborted. See logs."})]


@pytest.mark.parametrize("stream, from_keyring, kept, told", [
    (GUACD_13_BADPASS, True, None, True),       # the saved password, refused: said to be that, with the way out
    (GUACD_16_BADPASS, False, None, False),     # one just typed and ticked "save", refused: not saved
    (GUACD_13_OK, False, "typed-pw", False),    # accepted: saved now
    (GUACD_16_NLA_EXT, True, None, False),      # the same status for a security mode the server refuses: no word of the password
    (GUACD_16_NLA_EXT, False, None, False),     # nor was a typed one tried: neither saved nor forgotten
])
def test_an_rdp_password_is_saved_when_accepted_and_a_saved_one_refused_is_named(tmp_path, monkeypatch, stream, from_keyring,
                                                                                kept, told):
    g = FakeGuacd(after=stream)
    monkeypatch.setattr(guac, "GUACD", ("127.0.0.1", g.port))
    holder, emitted = {}, []

    def make(emit):
        from datoolkit.config import Config
        from datoolkit.engine import Engine
        holder["e"] = Engine(Config(), lambda event: (emitted.append(event), emit(event)), tmp_path / "rt",
                             save_config=lambda c: None)
        return holder["e"]

    last = guac.encode(*guac.Parser().feed(stream)[-1])
    with TestClient(create_app("tok", make)) as client:
        e = holder["e"]
        e.new_case("relay", "open")
        e.sessions.add(RdpSession(id="dc01", name="dc01", host_name="dc01", keyring_password=from_keyring,
                                  params={"hostname": "dc01", "password": "typed-pw", "ignore-cert": "true"}))
        if not from_keyring:
            e.sessions.unsaved["dc01"] = ("dc01", "typed-pw")       # as ask_user leaves one ticked "save"
        with client.websocket_connect("/ws/rdp/dc01?t=tok", subprotocols=["guacamole"]) as ws:
            got = ""
            while last not in got:
                got += ws.receive_text()
        for _ in range(100):
            if any(k in ("rdp_disconnected", "rdp_connect_failed") for k, _ in _rdp_events(e)):
                break
            threading.Event().wait(0.02)
        assert creds.get_secret("host", "dc01") == kept
        # held for a later connection of the session only where the password was never tried
        assert e.sessions.unsaved == ({"dc01": ("dc01", "typed-pw")} if stream == GUACD_16_NLA_EXT and not from_keyring else {})
        toasts = [x["text"] for x in emitted if x["type"] == "toast"]
        assert (["The password saved for dc01 was used, and the server refused it. To replace it: Settings → Hosts → dc01 → "
                 "Forget stored password, then open the session again and type the new one."] if told else []) == toasts
        assert "typed-pw" not in (e.case.dir / "events.jsonl").read_text()


class FakeGuacdForMany(FakeGuacd):
    """One that takes any number of connections and records each one's `connect`."""

    def __init__(self, args):
        self.connects = []
        super().__init__(args=args)

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.one, args=(conn,), daemon=True).start()

    def one(self, conn):
        conn.settimeout(5)
        parser, got = guac.Parser(), []
        try:
            while not any(i[0] == "connect" for i in got):
                data = conn.recv(65536)
                if not data:
                    return
                got += parser.feed(data.decode())
                if got and got[0][0] == "select" and len(got) == 1:
                    conn.sendall(guac.encode("args", *self.args).encode())
            self.connects.append(got[-1])
            conn.sendall((guac.encode("ready", "$abc") + GUACD_13_OK).encode())
            while conn.recv(65536):
                pass
        except OSError:
            return
        finally:
            conn.close()


def _reconnecting(tmp_path, monkeypatch, args, certs):
    """Open a pinned RDP host through a fake guacd offering `args`, then let the page connect once
    for each of `certs` (what the server presents by then). Returns (engine, guacd, the number of
    times the certificate was read after the opening, what each connection first showed)."""
    g = FakeGuacdForMany(args)
    monkeypatch.setattr(guac, "GUACD", ("127.0.0.1", g.port))
    presented, probes, holder, shown = [GOOD_CERT], [], {}, []

    async def fetch(host, port=3389, user=""):
        probes.append((host, port))
        return dict(presented[0])
    monkeypatch.setattr(rdpcert, "fetch", fetch)

    def make(emit):
        from datoolkit.config import Config
        from datoolkit.engine import Engine
        holder["e"] = Engine(Config(hosts=[Host("dc09", "rdp", "10.250.0.9", user="bob", auth="password")]), emit,
                             tmp_path / "rt", save_config=lambda c: None)
        return holder["e"]

    with TestClient(create_app("tok", make)) as client:
        e = holder["e"]
        e.new_case("reconnect", "open")
        e.pins.pin("10.250.0.9", 3389, GOOD_CERT)
        creds.set_secret("host", "dc09", "SAVED-PW")
        sid = client.post("/api/sessions", json={"kind": "rdp", "host": "dc09"}, headers={"x-token": "tok"}).json()["id"]
        assert len(probes) == 1 and not g.connects                # the opening read it once; nothing sent yet
        probes.clear()
        for cert in certs:                                        # the page connects; then Reconnect, or a reload
            presented[0] = cert
            with client.websocket_connect(f"/ws/rdp/{sid}?t=tok", subprotocols=["guacamole"]) as ws:
                got = ws.receive_text()
                while "error" not in got and "sync" not in got:
                    got += ws.receive_text()
                shown.append(guac.Parser().feed(got)[1])
        for _ in range(100):
            if len([k for k, _ in _rdp_events(e) if k in ("rdp_disconnected", "rdp_connect_failed")]) == len(certs):
                break
            threading.Event().wait(0.02)
        return e, g, len(probes), shown


GOOD_CERT = {"tls": True, "protocol": "nla", "sha256": "AA:BB", "sha1": "CC", "subject": "CN=srv", "issuer": "CN=srv",
             "not_after": "2030-01-01", "self_signed": True}
_ARGS_13 = ("VERSION_1_3_0", "hostname", "password", "security", "ignore-cert")


def test_every_connection_through_a_guacd_that_cant_pin_reads_the_certificate_again(tmp_path, monkeypatch):
    """guacd 1.3 connects with ignore-cert: the certificate was read once, when the session was
    opened, and every later connection of the page (Reconnect, a reload) sent the password with
    no check at all."""
    e, g, probes, shown = _reconnecting(tmp_path, monkeypatch, _ARGS_13, [GOOD_CERT, GOOD_CERT, GOOD_CERT])
    assert probes == 3 and len(g.connects) == 3
    assert all(c == ["connect", "VERSION_1_3_0", "10.250.0.9", "SAVED-PW", "nla", "true"] for c in g.connects)
    assert all(first[0] != "error" for first in shown)


@pytest.mark.parametrize("now, says", [
    ({**GOOD_CERT, "sha256": "EE:FF"}, "it now presents another (SHA-256: EE:FF)"),
    ({"tls": False, "protocol": "rdp", "sha256": "", "subject": "", "issuer": "", "not_after": "", "self_signed": False},
     "it now offers NO TLS at all"),
])
def test_a_reconnect_to_a_changed_certificate_is_refused_before_the_password_is_sent(tmp_path, monkeypatch, now, says):
    e, g, probes, shown = _reconnecting(tmp_path, monkeypatch, _ARGS_13, [GOOD_CERT, now, GOOD_CERT])
    assert probes == 3 and len(g.connects) == 2                   # the second never got as far as `connect`
    assert shown[0][0] != "error" and shown[2][0] != "error"
    op, text, code = shown[1]                                     # in the RDP tab, as other failures are
    assert op == "error" and code == "519" and text.startswith("Not connected: 10.250.0.9:3389 presented")
    assert "AA:BB" in text and says in text and "intercepting" in text and "remove the pin" in text
    events = _rdp_events(e)
    assert [k for k, _ in events] == ["rdp_connecting", "rdp_connected", "rdp_disconnected", "rdp_cert_mismatch",
                                      "rdp_connect_failed", "rdp_connecting", "rdp_connected", "rdp_disconnected"]
    mismatch = dict(events)["rdp_cert_mismatch"]
    assert (mismatch["pinned"], mismatch["got"], mismatch["tls"]) == ("AA:BB", now["sha256"], now["tls"])


def test_a_guacd_that_checks_the_pin_itself_needs_no_second_reading(tmp_path, monkeypatch):
    e, g, probes, shown = _reconnecting(tmp_path, monkeypatch, (*_ARGS_13, "cert-fingerprints"), [GOOD_CERT, GOOD_CERT])
    assert probes == 0 and len(g.connects) == 2
    # FreeRDP holds the connection itself to the pin: ignore-cert off, the fingerprint handed over
    assert g.connects[0][4:] == ["nla", "false", "sha256:aa:bb"]


async def test_a_certificate_that_cant_be_read_again_refuses_the_reconnect(rdp_env, monkeypatch):
    engine = rdp_env[0]
    sess = RdpSession(id="dc09", name="dc09", params={"hostname": "10.250.0.9", "port": "3389", "username": "bob"},
                      cert=dict(GOOD_CERT))

    async def unreachable(host, port=3389, user=""):
        raise rdpcert.CertError(f"cannot reach {host}:{port}: timed out")
    monkeypatch.setattr(rdpcert, "fetch", unreachable)
    with pytest.raises(UserError, match="couldn't be read again.*cannot reach 10.250.0.9:3389"):
        await engine.rdp_recheck(sess)


@pytest.mark.parametrize("stream, connected_at", [
    (GUACD_13_OK, "sync"), (GUACD_16_OK, "sync"), (GUACD_13_BADPASS, None), (GUACD_16_BADPASS, None),
])
def test_connect_watch_in_any_chunks(stream, connected_at):
    """Fed one character at a time, as guacd's stream can be split anywhere."""
    w = guac.ConnectWatch()
    events = [ev for ch in stream for ev in w.feed(ch)]
    assert w.connected == (connected_at is not None)
    assert events == (["connected"] if connected_at else ["error"])
    assert (w.error, w.code) == (("", "") if connected_at else ("Authentication failure (invalid credentials?)", "769"))
    whole = guac.ConnectWatch()
    assert whole.feed(stream) == events


def test_desktop_resizes_only_when_asked_and_can_be_maximized():
    """Resizing panes or the window must not resize the remote (a size change every drag is
    disruptive); the desktop is drawn 1:1 and scrolls, Fit to window resizes it in one click
    (reconnecting when the server ignores display-update), and Maximize hides the other panes."""
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "src" / "datoolkit" / "web"
    js, css = (web / "app.js").read_text(), (web / "style.css").read_text()
    assert js.count(".sendSize(") == 1
    fit = js[js.index("function rdpFitToWindow"):]
    fit = fit[:fit.index("\n}\n")]
    assert ".sendSize(" in fit and "rdpConnect(r)" in fit and "fitByReconnect" in fit
    assert "d.scale(1)" in js and 'btn("Fit to window"' in js
    assert "function rdpMaximize" in js and '"rdp-max"' in js
    assert re.search(r"\.rdp-view \{[^}]*overflow: auto", css)
    assert re.search(r"body\.rdp-max #chat-pane[^{]*\{ display: none", css.replace("\n", " "))
    assert re.search(r"\.rdp-status \{[^}]*width:", css)      # status text can't wrap the bar
    # Guacamole's canvases sit at z-index -1: without its own stacking context the view's black
    # background covers the 1:1 (untransformed) desktop
    assert re.search(r"\.rdp-view \{[^}]*isolation: isolate", css)
    # the remote pointer is the local pointer, not a drawn copy left behind at its last position
    assert "display.showCursor(false)" in js and "mouse.setCursor(canvas, x, y)" in js


# ---------------------------------------------------------------- whose guacd it is

HEAD = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n")


def _table(tmp_path, name, *rows):
    """A /proc/net/tcp-shaped table: rows of (local, remote, state, uid), addresses as written there."""
    lines = [f"   {i}: {local} {remote} {state} 00000000:00000000 00:00000000 00000000 {uid:5d}        0 {4000 + i} 1"
             for i, (local, remote, state, uid) in enumerate(rows)]
    path = tmp_path / name
    path.write_text(HEAD + "\n".join(lines) + "\n")
    return str(path)


@pytest.fixture
def as_user(tmp_path, monkeypatch):
    """This process runs as uid 1000, ordinary users starting at 1000."""
    defs = tmp_path / "login.defs"
    defs.write_text("# shadow\nUID_MIN\t\t\t 1000\nUID_MAX\t\t\t60000\n")
    monkeypatch.setattr(guac, "LOGIN_DEFS", str(defs))
    monkeypatch.setattr(guac.os, "getuid", lambda: 1000)


def test_guacds_port_held_by_another_user_is_refused_and_ours_or_the_systems_is_not(tmp_path, as_user):
    """DA Toolkit took whatever answered on 127.0.0.1:4822 for guacd and handed it the RDP host,
    user name and password, over plain TCP: another user of this computer who took the port first
    was given them."""
    port = 4822
    listen = lambda addr, uid: (f"{addr}:{port:04X}", "00000000:0000", "0A", uid)            # noqa: E731
    for rows in ([listen("0100007F", 1001)],                                    # 127.0.0.1, another user's
                 [listen("00000000", 1001)],                                    # every address
                 [listen("0100007F", 1000), listen("00000000", 1001)]):         # ours, and another's too
        with pytest.raises(guac.GuacError, match="another user of this computer.*nothing was sent"):
            guac.check_listener(port, [_table(tmp_path, "tcp", *rows)])
    six = _table(tmp_path, "tcp6", (f"{'0' * 32}:{port:04X}", f"{'0' * 32}:0000", "0A", 1001))      # [::], dual stack
    with pytest.raises(guac.GuacError, match="user id 1001|another user"):
        guac.check_listener(port, [six])
    # this user's (guacd by hand, rootless podman's forwarder), root's (Docker's forwarder), a
    # service's (the guacd package's own user), or a port no connection to 127.0.0.1 reaches
    for rows in ([listen("0100007F", 1000)], [listen("0100007F", 0)], [listen("00000000", 115)],
                 [listen("0200007F", 1001)], [(f"0100007F:{port + 1:04X}", "00000000:0000", "0A", 1001)],
                 [(f"0100007F:{port:04X}", "0100007F:C350", "01", 1001)]):     # not listening: judged by check_peer
        guac.check_listener(port, [_table(tmp_path, "tcp", *rows)])
    # once connected: the socket that took our connection, from port 50000 (0xC350), is the listener's
    took = _table(tmp_path, "tcp", (f"0100007F:{port:04X}", "0100007F:C350", "01", 1001),
                  ("0100007F:C350", f"0100007F:{port:04X}", "01", 1000))
    with pytest.raises(guac.GuacError, match="another user"):
        guac.check_peer(50000, port, [took])
    guac.check_peer(50001, port, [took])                                        # not ours: nothing said


async def test_the_socket_that_took_a_connection_is_found_in_the_real_tables(monkeypatch):
    async def serve(reader, writer):
        await reader.read(1)
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(guac, "GUACD", ("127.0.0.1", port))
    async with server:
        reader, writer = await guac.connect(3)                                 # ours: connected
        local = writer.get_extra_info("sockname")[1]
        mine = guac.os.getuid()
        monkeypatch.setattr(guac.os, "getuid", lambda: mine + 1)               # now it's "another user's"
        monkeypatch.setattr(guac, "_uid_min", lambda path=None: 0)
        with pytest.raises(guac.GuacError, match="another user"):
            guac.check_peer(local)
        writer.close()


async def test_an_rdp_host_isnt_opened_through_another_users_guacd(env, tmp_path, monkeypatch, as_user):  # noqa: F811
    engine, _, _ = env
    engine.new_case("rdp", "open")
    engine.cfg.hosts.append(Host("dc01", "rdp", "dc01.corp", user="bob", auth="password"))
    creds.set_secret("host", "dc01", "hunter2")
    monkeypatch.setattr(guac, "PROC_TABLES", (_table(tmp_path, "tcp", ("0100007F:12D6", "00000000:0000", "0A", 1001)),))
    connected = []
    monkeypatch.setattr(guac.asyncio, "open_connection", lambda *a, **k: connected.append(a))
    with pytest.raises(UserError, match="127.0.0.1:4822 is user id 1001's|another user of this computer"):
        await engine.open_rdp("dc01")
    assert connected == [] and not engine.sessions.sessions


async def test_extended_nla_is_refused_with_the_reason(env):  # noqa: F811
    """No guacd DA Toolkit can use speaks Extended NLA (all are built on FreeRDP 2), so it is no
    longer offered; a host saved with it by an earlier version says why when opened, before
    anything connects."""
    engine, _, _ = env
    with pytest.raises(UserError, match="Host not saved. Extended NLA can't connect.*choose NLA"):
        engine.save_host({"name": "w11", "kind": "rdp", "host": "192.168.1.214", "rdp_security": "nla-ext"})
    engine.save_host({"name": "w11", "kind": "rdp", "host": "192.168.1.214", "rdp_security": "nla"})
    engine.cfg.host("w11").rdp_security = "nla-ext"            # as an earlier version saved it
    engine.new_case("c", "open")
    with pytest.raises(UserError, match="w11 is set to Extended NLA.*FreeRDP 2.*Settings → Hosts"):
        await engine._open_rdp(engine.cfg.host("w11"))
    assert not engine.sessions.sessions
