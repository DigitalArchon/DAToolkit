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


def test_guacd_install_help_per_distribution():
    def which_of(*present):
        return lambda name: f"/usr/bin/{name}" if name in present else None

    def help_for(release, *present):
        return guac.install_help(release, which_of(*present), installed=False)
    pull = f"podman pull {guac.GUACD_IMAGE}"
    assert "@sha256:" in guac.GUACD_IMAGE                       # pinned
    mint = {"ID": "linuxmint", "ID_LIKE": "ubuntu debian", "NAME": "Linux Mint"}
    assert help_for(mint)[1] == ["sudo apt install guacd"]
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
    assert rt.calls[-1] == ["podman", "run", "-d", "--rm", "--name", guac.CONTAINER,
                            "-p", "127.0.0.1:4822:4822", guac.GUACD_IMAGE]
    c.stop()
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
