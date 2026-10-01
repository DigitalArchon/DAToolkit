"""The phone companion's TLS listener, certificate and pairing (companion.py)."""

import os
import socket

import httpx
import pytest
from fastapi import FastAPI

from datoolkit import companion
from datoolkit.engine import UserError
from datoolkit.server.app import create_app

from test_engine import env  # noqa: F401


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
                assert (await phone.get("/api/state", headers={"x-token": "main"})).status_code == 404
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
