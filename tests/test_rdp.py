"""RDP sessions through guacd: protocol, handshake, certificate pinning (trust on first use),
device linking, session-cut rules, and the browser <-> guacd relay."""

import asyncio
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


class FakeGuacd:
    """A guacd that offers a few args, records the handshake and what arrives afterwards."""

    def __init__(self, reply_ready=True):
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
        conn.settimeout(5)
        parser, got = guac.Parser(), []
        try:
            while not any(i[0] == "connect" for i in got):
                data = conn.recv(65536)
                if not data:
                    return
                got += parser.feed(data.decode())
                if got and got[0][0] == "select" and len(got) == 1:
                    conn.sendall(guac.encode("args", "VERSION_1_3_0", "hostname", "password", "ignore-cert").encode())
            self.handshake = got
            if not self.reply_ready:
                conn.sendall(guac.encode("error", "Aborted. See logs.", "519").encode())
                return
            conn.sendall((guac.encode("ready", "$abc") + guac.encode("sync", "7")).encode())
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
        if mode == "refuse":
            neg = struct.pack("<BBHI", 0x03, 0, 8, 5)
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
        assert engine.forget_rdp_pin("dc01") is True
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
        assert "no TLS certificate" in [e for e in events if e["type"] == "prompt"][-1]["prompt"]["text"]

    async def down():
        return False
    monkeypatch.setattr(engine, "_guacd_reachable", down)
    with pytest.raises(UserError, match="apt install guacd"):
        await engine.open_rdp("old")


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
    g = FakeGuacd()
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
            first = ws.receive_text()
            ins = guac.Parser().feed(first)
            assert ins[0][0] == "" and len(ins[0]) == 2 and ins[1] == ["sync", "7"]   # tunnel uuid, then guacd's data
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
        assert "rdp_connected" in (e.case.dir / "events.jsonl").read_text()


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
