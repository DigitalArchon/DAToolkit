"""The phone companion's TLS listener, certificate and pairing (companion.py)."""

import base64
import json
import os
import socket
from io import BytesIO

import httpx
import pytest
from fastapi import FastAPI

from datoolkit import companion
from datoolkit.engine import UserError
from datoolkit.server.app import create_app

from test_engine import env, sse, wait_turn  # noqa: F401


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_certificate_is_made_once_and_kept(tmp_path):
    cert, key = companion.ensure_cert(tmp_path, "192.168.1.20")
    first = companion.fingerprint(cert.read_bytes())
    assert len(first.split(":")) == 32 and first == first.upper()
    assert os.stat(key).st_mode & 0o777 == 0o600                    # the private key is the owner's alone
    companion.ensure_cert(tmp_path, "10.0.0.5")                     # a new address doesn't change it
    assert companion.fingerprint(cert.read_bytes()) == first
    companion.ensure_cert(tmp_path, "10.0.0.5", renew=True)
    assert companion.fingerprint(cert.read_bytes()) != first


def test_nearly_expired_certificate_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(companion, "CERT_DAYS", 3)                  # inside RENEW_BEFORE
    cert, _ = companion.ensure_cert(tmp_path, "192.168.1.20")
    first = companion.fingerprint(cert.read_bytes())
    monkeypatch.setattr(companion, "CERT_DAYS", 825)
    companion.ensure_cert(tmp_path, "192.168.1.20")
    assert companion.fingerprint(cert.read_bytes()) != first


async def test_pairing_over_tls(env, tmp_path, monkeypatch):  # noqa: F811
    engine, _, events = env
    engine.new_case("phone", "open")
    monkeypatch.setattr(companion, "lan_ip", lambda: "127.0.0.1")
    app: FastAPI = create_app("main", lambda emit: engine, companion_dir=tmp_path)
    app.state.engine = engine
    engine.cfg.settings.companion_port = port = _free_port()
    main = {"x-token": "main"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as gui:
        stopped = (await gui.get("/api/phone", headers=main)).json()
        assert not stopped["running"] and "connect_url" not in stopped and stopped["port"] == port
        assert (await gui.post("/api/phone/start", headers={"x-token": "nope"})).status_code == 403
        info = (await gui.post("/api/phone/start", headers=main)).json()
        try:
            assert info["running"] and info["pair_url"] == f"https://127.0.0.1:{port}/pair"
            assert "#t=" not in info["pair_url"] and info["pair_qr"].startswith("data:image/svg+xml")
            token = info["connect_url"].split("#t=", 1)[1]
            assert info["connect_url"] == f"https://127.0.0.1:{port}/companion#t={token}"
            cert = tmp_path / companion.CERT_FILE
            assert info["fingerprint"] == companion.fingerprint(cert.read_bytes())

            # a phone that trusts exactly this certificate
            async with httpx.AsyncClient(verify=companion.client_context(cert), base_url=f"https://127.0.0.1:{port}") as phone:
                assert (await phone.get("/pair")).status_code == 200
                r = await phone.get("/api/companion/state", headers={"x-token": token})
                assert r.status_code == 200 and r.json()["case"]["name"] == "phone"
                # the GUI's API isn't there, even with the phone's token; the main token means nothing
                assert (await phone.get("/api/state", headers={"x-token": token})).status_code == 404
                assert (await phone.get("/api/state", headers={"x-token": "main"})).status_code == 403
                # a new code cuts off the old one
                await gui.post("/api/phone/token", headers=main)
                assert (await phone.get("/api/companion/state", headers={"x-token": token})).status_code == 403
            # plain HTTP isn't served on the companion port
            with pytest.raises(httpx.HTTPError):
                async with httpx.AsyncClient() as plain:
                    await plain.get(f"http://127.0.0.1:{port}/pair", timeout=2)
            # the port is fixed: the same one after a restart
            await gui.post("/api/phone/stop", headers=main)
            assert (await gui.post("/api/phone/start", headers=main)).json()["port"] == port
        finally:
            await app.state.companion.stop()
    assert not app.state.companion.running


async def test_busy_port_is_a_clear_error(env, tmp_path):  # noqa: F811
    engine, _, _ = env
    server = companion.CompanionServer(FastAPI(), tmp_path)
    with socket.socket() as taken:
        taken.bind(("0.0.0.0", 0))
        taken.listen(1)
        with pytest.raises(UserError, match="already in use"):
            await server.start(taken.getsockname()[1])
    assert not server.running and server.token is None


async def test_port_setting_is_validated(env):  # noqa: F811
    engine, _, _ = env
    for bad in (80, 70000, "x"):
        with pytest.raises(UserError):
            engine.save_settings({"companion_port": bad})
    engine.save_settings({"companion_port": 50443})
    assert engine.cfg.settings.companion_port == 50443


async def test_phone_sends_a_photo_with_its_description(env, tmp_path):  # noqa: F811
    from PIL import Image

    engine, fake, _ = env
    engine.new_case("photo", "open")
    app: FastAPI = create_app("main", lambda emit: engine, companion_dir=tmp_path)
    app.state.engine = engine
    app.state.companion.token = "phone"
    buf = BytesIO()
    Image.new("RGB", (40, 30), "white").save(buf, "JPEG")
    photo = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    ph = {"x-token": "phone"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.state.companion.app), base_url="http://t") as c:
        view = (await c.get("/api/companion/state", headers=ph)).json()
        assert view["photos"] == {"ok": False, "why": "Choose a model first."}
        r = await c.post("/api/companion/photo", json={"message": "x", "image": photo}, headers=ph)
        assert r.status_code == 400                                    # the same gates as the desktop
        engine.select_model("Fake", "anthropic/claude-opus-5.5")
        assert (await c.get("/api/companion/state", headers=ph)).json()["photos"]["ok"]
        assert (await c.post("/api/companion/photo", json={"image": photo}, headers={"x-token": "main"})).status_code == 403
        assert (await c.post("/api/companion/photo", json={"message": "just text"}, headers=ph)).status_code == 400
        fake.responses.append(sse(({"role": "assistant", "content": "That port is err-disabled."}, "stop")))
        r = await c.post("/api/companion/photo", json={"message": "show interfaces on the core switch", "image": photo}, headers=ph)
        assert r.status_code == 200
        await wait_turn(engine)
    sent = fake.requests[0]["messages"][-1]["content"]
    assert "from the technician's phone" in sent[0]["text"] and "show interfaces on the core switch" in sent[0]["text"]
    assert sent[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    entry = engine.chat[0]
    assert entry["via"] == "phone" and entry["text"] == "show interfaces on the core switch" and entry["images"] == ["img-1.jpg"]
    logged = [json.loads(x) for x in (engine.case.dir / "events.jsonl").read_text().splitlines()]
    assert any(e.get("event") == "sent_to_ai" and e.get("via") == "phone" for e in logged)


async def _gate_call(token, headers):
    """Run CompanionGate on one POST; returns (status, response headers, app reached, body read)."""
    from datoolkit.server.app import CompanionGate

    seen = {"app": False, "read": False, "start": None}

    async def inner(scope, receive, send):
        seen["app"] = True

    async def receive():
        seen["read"] = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            seen["start"] = msg

    srv = companion.CompanionServer(FastAPI(), None)
    srv.token = token
    scope = {"type": "http", "method": "POST", "path": "/api/companion/photo", "query_string": b"",
             "headers": [(k.encode(), v.encode()) for k, v in headers.items()]}
    await CompanionGate(inner, srv)(scope, receive, send)
    start = seen["start"]
    return (start["status"] if start else None, dict(start["headers"]) if start else {}, seen["app"], seen["read"])


async def test_gate_refuses_on_headers_before_any_body_is_read():
    from datoolkit.server.app import MAX_COMPANION_BODY

    big = str(MAX_COMPANION_BODY + 1)
    for headers, status in (({"content-length": big}, 403),                                  # no token
                            ({"x-token": "wrong", "content-length": "10"}, 403),
                            ({"x-token": "phone", "content-length": big}, 413),
                            ({"x-token": "phone", "transfer-encoding": "chunked"}, 411),
                            ({"x-token": "phone", "content-length": "lots"}, 400)):
        got, resp_headers, reached, read = await _gate_call("phone", headers)
        assert (got, reached, read) == (status, False, False), headers
        assert resp_headers[b"connection"] == b"close"                 # not kept open for the rest
    assert MAX_COMPANION_BODY > 8 * 1024 * 1024 + 4000                 # a 6 MB photo as base64 still fits
    assert (await _gate_call("phone", {"x-token": "phone", "content-length": "100"}))[2]
    assert (await _gate_call(None, {"x-token": "", "content-length": "1"}))[0] == 403    # stopped: no token at all


async def test_unauthenticated_upload_is_cut_off_over_tls(env, tmp_path, monkeypatch):  # noqa: F811
    """Someone on the LAN without the token announcing a huge body gets 403 at once and the
    connection closes; the server never buffers it."""
    import asyncio

    engine, _, _ = env
    monkeypatch.setattr(companion, "lan_ip", lambda: "127.0.0.1")
    app: FastAPI = create_app("main", lambda emit: engine, companion_dir=tmp_path)
    app.state.engine = engine
    engine.cfg.settings.companion_port = port = _free_port()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as gui:
        await gui.post("/api/phone/start", headers={"x-token": "main"})
    try:
        ctx = companion.client_context(tmp_path / companion.CERT_FILE)
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
        writer.write(b"POST /api/companion/photo HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                     b"Content-Length: 2000000000\r\n\r\n" + b"[" * 65536)
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(), 5)               # read() to EOF: the server hangs up
        assert reply.startswith(b"HTTP/1.1 403")
        writer.close()
        # and the real phone path still works
        async with httpx.AsyncClient(verify=ctx, base_url=f"https://127.0.0.1:{port}") as phone:
            r = await phone.get("/api/companion/state", headers={"x-token": app.state.companion.token})
            assert r.status_code == 200
    finally:
        await app.state.companion.stop()
