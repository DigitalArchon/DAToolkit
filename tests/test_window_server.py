"""The window's own server and the phone's: the headers on the window's answers, what the phone is
given of a case and may do to the queue, requests of the wrong shape, and where the companion listens."""

import socket

import httpx
import pytest
from fastapi import FastAPI

from datoolkit import companion
from datoolkit.server.app import WINDOW_HEADERS, create_app

from test_engine import env  # noqa: F401


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_every_answer_of_the_windows_carries_its_headers(env, tmp_path):  # noqa: F811
    """The page's Content-Security-Policy is a meta element, where frame-ancestors isn't honoured, and
    nothing said the answers weren't to be framed, sniffed or named in a referrer."""
    engine, _, _ = env
    app: FastAPI = create_app("main", lambda emit: engine, companion_dir=tmp_path)
    app.state.engine = engine
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for path, headers in (("/", {}), ("/static/app.js", {}), ("/api/state", {"x-token": "main"}),
                              ("/api/state", {}),                 # refused by the gate
                              ("/nowhere", {})):
            r = await c.get(path, headers=headers)
            for name, value in WINDOW_HEADERS.items():
                assert r.headers.get(name) == value, (path, name)
        assert (await c.get("/", headers={})).headers["cache-control"] == "no-store"      # the page's own, kept
    assert WINDOW_HEADERS["Content-Security-Policy"] == "frame-ancestors 'none'" and WINDOW_HEADERS["X-Frame-Options"] == "DENY"


async def test_the_phone_gets_a_cases_name_and_sensitivity_and_nothing_else_of_it(env, tmp_path):  # noqa: F811
    engine, _, _ = env
    engine.add_client("Acme Dental")
    engine.new_case("fs01 slow", "open", "the domain admin is svc-backup", client="Acme Dental")
    app: FastAPI = create_app("main", lambda emit: engine, companion_dir=tmp_path)
    app.state.engine = engine
    app.state.companion.add_device("test phone", "phone")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.state.companion.app), base_url="http://t") as c:
        view = (await c.get("/api/companion/state", headers={"x-token": "phone"})).json()
    assert view["case"] == {"name": "fs01 slow", "sensitivity": "open"}
    assert "svc-backup" not in str(view) and "Acme Dental" not in str(view) and str(engine.case.dir) not in str(view)


async def test_the_phone_cant_bring_back_what_the_ai_withdrew(env, tmp_path):  # noqa: F811
    """Its "pending" undoes its own ran or skipped; from "withdrawn" it put a command back in the queue
    that the AI had taken out, from a screen that shows less of it than the computer does."""
    engine, _, _ = env
    engine.new_case("c", "open")
    engine.queue.add("c", [{"session_id": "s", "command": "uptime", "risk": "read_only"},
                           {"session_id": "s", "command": "df -h", "risk": "read_only"}])
    engine.queue.update(1, status="withdrawn")
    app: FastAPI = create_app("main", lambda emit: engine, companion_dir=tmp_path)
    app.state.engine = engine
    app.state.companion.add_device("test phone", "phone")
    ph = {"x-token": "phone"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.state.companion.app), base_url="http://t") as c:
        r = await c.post("/api/companion/queue/1", json={"status": "pending"}, headers=ph)
        assert r.status_code == 400 and "withdrawn by the AI" in r.json()["error"]
        assert engine.queue.get(1).status == "withdrawn"
        assert (await c.post("/api/companion/queue/2", json={"status": "skipped"}, headers=ph)).status_code == 200
        assert (await c.post("/api/companion/queue/2", json={"status": "pending"}, headers=ph)).status_code == 200
        assert engine.queue.get(2).status == "pending"
        assert (await c.post("/api/companion/queue/99", json={"status": "pending"}, headers=ph)).status_code == 400


async def test_a_provider_host_or_hint_of_the_wrong_shape_is_refused_not_a_crash(env, tmp_path):  # noqa: F811
    engine, _, _ = env
    engine.new_case("c", "open")
    sid = engine.open_session("local")["id"]
    app: FastAPI = create_app("main", lambda emit: engine, companion_dir=tmp_path)
    app.state.engine = engine
    m = {"x-token": "main"}
    hosts, providers = len(engine.cfg.hosts), len(engine.cfg.providers)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for path, body in (("/api/providers", {"provider": "x"}), ("/api/providers", {"provider": ["a"]}),
                           ("/api/hosts", {"host": []}), ("/api/hosts", {"host": "web01"})):
            r = await c.post(path, json=body, headers=m)
            assert r.status_code == 400 and r.json()["error"], (path, body)
        r = await c.post(f"/api/sessions/{sid}/hint", json={"os_hint": ["Windows"]}, headers=m)
        assert r.status_code == 200
        assert isinstance(engine.sessions.sessions[sid].os_hint, str)
    assert (len(engine.cfg.hosts), len(engine.cfg.providers)) == (hosts, providers)


def test_the_companion_listens_on_the_address_its_codes_carry():
    assert companion.listen_address("192.168.1.20") == "192.168.1.20"
    # no route, so lan_ip gave a host name: every interface, as before
    assert companion.listen_address("mintdev") == "0.0.0.0"
    assert companion.listen_address("") == "0.0.0.0"


async def test_the_companion_isnt_reachable_on_another_of_this_computers_addresses(env, tmp_path, monkeypatch):  # noqa: F811
    """It listened on every interface: on a client's VPN, a container bridge or a public address, its
    pairing page and its certificate (which names this computer) were there to be had."""
    engine, _, _ = env
    monkeypatch.setattr(companion, "lan_ip", lambda: "127.0.0.1")
    app: FastAPI = create_app("main", lambda emit: engine, companion_dir=tmp_path)
    app.state.engine = engine
    port = _free_port()
    await app.state.companion.start(port)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        with pytest.raises(OSError):                 # another address of this same computer
            socket.create_connection(("127.0.0.2", port), timeout=2).close()
    finally:
        await app.state.companion.stop()
