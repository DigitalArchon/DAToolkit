"""WinRM hosts: plain HTTP only on the local network, Basic over it only with the technician's
say-so, and the certificate checked by DA Toolkit (pinned) when the system CA store isn't used."""

import asyncio
import ssl

import pytest

from datoolkit.config import Host
from datoolkit.engine import UserError
from datoolkit.sessions import rdpcert, winrm

from test_engine import env  # noqa: F401
from test_rdp import _self_signed, answer_prompts


def test_plain_http_rules():
    assert winrm.problem(Host("a", "winrm", "dc01.corp.example", auth="ntlm")) is None          # HTTPS
    assert "HTTPS" in winrm.problem(Host("a", "winrm", "dc01.corp.example", auth="ntlm", winrm_ssl=False))
    assert winrm.problem(Host("a", "winrm", "192.168.1.14", auth="ntlm", winrm_ssl=False)) is None
    assert winrm.problem(Host("a", "winrm", "dc01.local", auth="kerberos", winrm_ssl=False)) is None
    assert "clear" in winrm.problem(Host("a", "winrm", "192.168.1.14", auth="basic", winrm_ssl=False))
    assert winrm.problem(Host("a", "winrm", "192.168.1.14", auth="basic", winrm_ssl=False, winrm_plain_basic=True)) is None
    assert "HTTPS" in winrm.problem(Host("a", "winrm", "203.0.113.5", auth="basic", winrm_ssl=False, winrm_plain_basic=True))
    assert winrm.port(Host("a", "winrm", "h")) == 5986 and winrm.port(Host("a", "winrm", "h", winrm_ssl=False)) == 5985
    assert winrm.pins(Host("a", "winrm", "h", winrm_cert_validation=False))
    assert not winrm.pins(Host("a", "winrm", "h")) and not winrm.pins(Host("a", "winrm", "h", winrm_ssl=False, winrm_cert_validation=False))


async def test_save_host_refuses_what_the_rules_refuse(env):  # noqa: F811
    engine, _, _ = env
    base = {"name": "dc", "kind": "winrm", "host": "203.0.113.5", "user": "admin", "auth": "ntlm"}
    with pytest.raises(UserError, match="Host not saved.*HTTPS"):
        engine.save_host({**base, "winrm_ssl": False})
    with pytest.raises(UserError, match="clear"):
        engine.save_host({**base, "host": "192.168.1.14", "winrm_ssl": False, "auth": "basic"})
    engine.save_host({**base, "host": "192.168.1.14", "winrm_ssl": False, "auth": "basic", "winrm_plain_basic": True})
    assert engine.cfg.host("dc").winrm_plain_basic is True
    # a config edited by hand is checked again where the session opens
    engine.cfg.host("dc").host = "203.0.113.5"
    engine.new_case("c", "open")
    with pytest.raises(UserError, match="HTTPS"):
        engine.open_session("winrm", "dc")
    with pytest.raises(UserError, match="HTTPS"):
        await engine.open_winrm("dc")


async def tls_server(cert, key):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)

    async def handle(reader, writer):
        try:
            await reader.read(1)
        finally:
            writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1]


async def test_fetch_tls_reads_the_listener_certificate(tmp_path):
    c, k, fp = _self_signed(tmp_path, "win10.lan")
    server, port = await tls_server(c, k)
    async with server:
        got = await rdpcert.fetch_tls("127.0.0.1", port)
    assert got["tls"] and got["sha256"] == fp and "win10.lan" in got["subject"] and got["self_signed"]
    with pytest.raises(rdpcert.CertError, match="cannot reach"):
        await rdpcert.fetch_tls("127.0.0.1", 1)


async def test_pinned_winrm_host_is_checked_before_the_console_starts(env, tmp_path, monkeypatch):  # noqa: F811
    engine, _, events = env
    engine.new_case("c", "open")
    c1, k1, fp1 = _self_signed(tmp_path, "win10")
    server, port = await tls_server(c1, k1)
    engine.cfg.hosts.append(Host("win10", "winrm", "127.0.0.1", port=port, user="admin", auth="ntlm",
                                 winrm_cert_validation=False))
    opened = []
    monkeypatch.setattr(engine, "open_session", lambda kind, host_name="", _cert_checked=False:
                        opened.append((kind, host_name, _cert_checked)) or {"id": host_name, "kind": kind})
    async with server:
        # not pinned yet: the technician is shown the fingerprint and accepts it
        opener = asyncio.create_task(engine.open_winrm("win10"))
        await answer_prompts(engine, ["yes"])
        assert (await opener)["id"] == "win10"
        text = [e for e in events if e["type"] == "prompt"][-1]["prompt"]["text"]
        assert fp1 in text and "winrm/config/listener" in text
        assert opened == [("winrm", "win10", True)]
        assert engine.pins.get("127.0.0.1", port)["sha256"] == fp1
        assert engine.snapshot()["config"]["hosts"][-1]["pinned"] == fp1
        assert "winrm_cert_pinned" in (engine.case.dir / "events.jsonl").read_text()
        # pinned: no prompt the second time
        n = len([e for e in events if e["type"] == "prompt"])
        await engine.open_winrm("win10")
        assert len([e for e in events if e["type"] == "prompt"]) == n
    # the listener's certificate changes: refused
    c2, k2, _ = _self_signed(tmp_path, "evil")
    server, port2 = await tls_server(c2, k2)
    engine.cfg.hosts[-1].port = port2
    engine.pins.pin("127.0.0.1", port2, {"sha256": fp1, "subject": "", "issuer": "", "not_after": ""})
    async with server:
        with pytest.raises(UserError, match="CHANGED"):
            await engine.open_winrm("win10")
        assert "winrm_cert_mismatch" in (engine.case.dir / "events.jsonl").read_text()
        assert engine.forget_pin("win10") is True
        assert engine.snapshot()["config"]["hosts"][-1]["pinned"] == ""
    # a host checked by the CA store, or over HTTP, has no pin to forget
    assert engine.forget_pin("web01") is False


async def test_pinned_host_never_opens_unchecked(env):  # noqa: F811
    engine, _, _ = env
    engine.new_case("c", "open")
    engine.cfg.hosts.append(Host("win10", "winrm", "192.168.1.14", user="admin", auth="ntlm", winrm_cert_validation=False))
    with pytest.raises(UserError, match="checked before every connection"):
        engine.open_session("winrm", "win10")
