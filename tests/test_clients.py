"""Clients: cases belong to one, the list is kept in the config, and renaming or deleting a
client follows through to its cases."""

import json

import httpx
import pytest
from fastapi import FastAPI

from datoolkit.case import Case
from datoolkit.engine import UserError
from datoolkit.server.app import create_app

from test_engine import env  # noqa: F401


def test_case_keeps_its_client_and_history(tmp_path):
    c = Case.create("TKT-1", "open", client="Acme", history=True, root=tmp_path)
    again = Case.load(c.id, root=tmp_path)
    assert (again.client, again.history) == ("Acme", True)
    assert Case.list_all(root=tmp_path)[0]["client"] == "Acme" and Case.list_all(root=tmp_path)[0]["history"] is True
    first = json.loads((c.dir / "events.jsonl").read_text().splitlines()[0])
    assert first["client"] == "Acme" and first["history"] is True
    c.edit("TKT-1", "", client="", history=False)
    assert Case.load(c.id, root=tmp_path).client == "" and not Case.load(c.id, root=tmp_path).history
    last = json.loads((c.dir / "events.jsonl").read_text().splitlines()[-1])
    assert last["event"] == "case_edited" and last["old_client"] == "Acme" and last["client"] == ""
    c.edit("TKT-1", "", client=None, history=None)                 # None: unchanged, nothing logged
    assert len((c.dir / "events.jsonl").read_text().splitlines()) == 2
    c.set_client("Acme Pty Ltd")
    assert Case.load(c.id, root=tmp_path).client == "Acme Pty Ltd"
    assert "- Client: Acme Pty Ltd" in c.export_markdown([], []).read_text()
    old = Case.create("old", "open", root=tmp_path)                 # a case from before clients existed
    (old.dir / "case.json").write_text(json.dumps({"name": "old", "sensitivity": "open", "notes": "", "started": ""}))
    assert Case.load(old.id, root=tmp_path).client == "" and Case.load(old.id, root=tmp_path).history is False


async def test_engine_keeps_the_client_list(env):  # noqa: F811
    engine, _, events = env
    engine.add_client("  Acme   Pty Ltd ")
    assert engine.cfg.clients == ["Acme Pty Ltd"]
    with pytest.raises(UserError, match="already"):
        engine.add_client("Acme Pty Ltd")
    with pytest.raises(UserError, match="name"):
        engine.add_client("   ")
    engine.add_client("Beta")
    engine.new_case("a1", "open", client="Acme Pty Ltd", history=True)
    a1 = engine.case.id
    engine.new_case("b1", "open", client="Beta")
    engine.new_case("a2", "confidential", client="Acme Pty Ltd")
    assert engine.snapshot()["case"]["client"] == "Acme Pty Ltd"
    assert engine.snapshot()["config"]["clients"] == ["Acme Pty Ltd", "Beta"]
    # rename: the cases follow, the open one included
    assert engine.rename_client("Acme Pty Ltd", "Acme") == 2
    assert engine.cfg.clients == ["Acme", "Beta"] and engine.case.client == "Acme"
    assert {c["id"]: c["client"] for c in engine.list_cases()}[a1] == "Acme"
    with pytest.raises(UserError, match="already"):
        engine.rename_client("Beta", "Acme")
    with pytest.raises(UserError, match="Unknown"):
        engine.rename_client("Nobody", "x")
    # delete: the cases stay, with no client; the open case's runbooks go
    engine._similar, engine._runbooks = [{"id": a1, "name": "a1", "runbook": "RB"}], "### a1\nRB"
    assert engine.delete_client("Acme") == 2
    assert engine.cfg.clients == ["Beta"] and engine.case.client == "" and engine._runbooks == ""
    assert {c["id"]: c["client"] for c in engine.list_cases()}[a1] == ""
    assert any(e["type"] == "similar" and e["cases"] == [] for e in events)
    # a case can only be given a client that is in the list
    with pytest.raises(UserError, match="client list"):
        engine.edit_case(engine.case.id, "a2", client="Acme")
    r = engine.edit_case(engine.case.id, "a2", client="Beta", history=True)
    assert (r["client"], r["history"]) == ("Beta", True) and Case.load(engine.case.id).client == "Beta"


async def test_client_routes(env):  # noqa: F811
    engine, _, _ = env
    app: FastAPI = create_app("main", lambda emit: engine)
    app.state.engine = engine
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        m = {"x-token": "main"}
        r = await c.post("/api/clients", json={"name": "Acme"}, headers=m)
        assert r.status_code == 200 and r.json()["clients"] == ["Acme"]
        assert (await c.post("/api/clients", json={"name": "Acme"}, headers=m)).status_code == 400
        r = await c.post("/api/case", json={"name": "job", "sensitivity": "open", "client": "Acme", "history": True}, headers=m)
        assert r.status_code == 200 and engine.case.client == "Acme" and engine.case.history is True
        r = await c.post("/api/case/edit", json={"id": engine.case.id, "name": "job", "history": False}, headers=m)
        assert r.status_code == 200 and r.json()["client"] == "Acme" and r.json()["history"] is False
        r = await c.post("/api/clients/Acme", json={"name": "Acme Pty"}, headers=m)
        assert r.status_code == 200 and r.json()["cases"] == 1 and engine.case.client == "Acme Pty"
        r = await c.delete("/api/clients/Acme%20Pty", headers=m)
        assert r.status_code == 200 and r.json()["cases"] == 1 and engine.cfg.clients == [] and engine.case.client == ""
        assert (await c.post("/api/clients", json={"name": "x"})).status_code == 403
