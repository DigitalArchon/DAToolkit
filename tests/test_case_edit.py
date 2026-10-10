"""Renaming cases, changing their notes and raising their sensitivity."""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from datoolkit.case import Case
from datoolkit.engine import UserError
from datoolkit.server.app import create_app

from test_engine import env  # noqa: F401


def test_edit_writes_the_meta_and_logs_it(tmp_path):
    c = Case.create("TKT-1 printer", "confidential", "old notes", root=tmp_path)
    c.edit("TKT-1 printer offline at Acme", "new notes")
    again = Case.load(c.id, root=tmp_path)
    assert (again.name, again.notes, again.sensitivity, again.started) == \
        ("TKT-1 printer offline at Acme", "new notes", "confidential", c.started)
    assert again.id == c.id                              # the directory keeps its original id
    last = json.loads((c.dir / "events.jsonl").read_text().splitlines()[-1])
    assert last["event"] == "case_edited" and last["old_name"] == "TKT-1 printer" and last["name"] == again.name
    lines = len((c.dir / "events.jsonl").read_text().splitlines())
    again.edit(again.name, again.notes)                  # no change, nothing logged
    assert len((c.dir / "events.jsonl").read_text().splitlines()) == lines


def test_a_case_from_before_case_json_gets_one_on_edit(tmp_path):
    c = Case.create("legacy", "open", root=tmp_path)
    (c.dir / "case.json").unlink()                       # as written before case.json existed
    Case.load(c.id, root=tmp_path).edit("legacy, renamed", "")
    assert json.loads((c.dir / "case.json").read_text())["name"] == "legacy, renamed"
    assert [x["name"] for x in Case.list_all(root=tmp_path)] == ["legacy, renamed"]


def test_load_takes_only_case_directories(tmp_path):
    base = tmp_path / "cases"
    base.mkdir()
    outside = Case.create("outside", "open", root=tmp_path / "elsewhere")
    (base / "link").symlink_to(outside.dir)
    for bad in ("../elsewhere/" + outside.id, "link", "..", "", "nope"):
        with pytest.raises((FileNotFoundError, ValueError)):
            Case.load(bad, root=base)


async def test_engine_renames_the_open_case(env):  # noqa: F811
    engine, _, events = env
    engine.new_case("first name", "open", "keep these notes")
    events.clear()
    r = engine.edit_case(engine.case.id, "  better name  ")
    assert r["name"] == "better name" and r["notes"] == "keep these notes"     # notes left out: unchanged
    assert engine.snapshot()["case"]["name"] == "better name"
    assert any(e["type"] == "state" and e["state"]["case"]["name"] == "better name" for e in events)
    assert "better name" in engine._system_prompt() and "first name" not in engine._system_prompt()
    engine.edit_case(engine.case.id, "better name", "")
    assert engine.case.notes == "" and Case.load(engine.case.id).notes == ""


async def test_engine_renames_a_case_on_disk_without_touching_the_open_one(env):  # noqa: F811
    engine, _, _ = env
    engine.new_case("old job", "open", "n")
    old = engine.case.id
    engine.new_case("current job", "open")
    engine._similar = [{"id": old, "name": "old job", "started": "2026-01-01", "runbook": "Restart the spooler."}]
    engine._runbooks = "### old job"
    engine.edit_case(old, "Acme spooler", "spooler notes")
    assert {c["id"]: (c["name"], c["notes"]) for c in engine.list_cases()}[old] == ("Acme spooler", "spooler notes")
    assert engine.case.name == "current job"
    assert engine._similar[0]["name"] == "Acme spooler" and "Acme spooler" in engine._runbooks


async def test_engine_refuses_an_empty_name_or_a_bad_id(env):  # noqa: F811
    engine, _, _ = env
    engine.new_case("job", "open")
    with pytest.raises(UserError, match="needs a name"):
        engine.edit_case(engine.case.id, "   ")
    for bad in ("nope", "../x", ""):
        with pytest.raises(UserError, match="Could not edit"):
            engine.edit_case(bad, "x")
    assert engine.case.name == "job"


async def test_edit_route(env):  # noqa: F811
    engine, _, _ = env
    engine.new_case("job", "open", "notes")
    app: FastAPI = create_app("main", lambda emit: engine)
    app.state.engine = engine
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        m = {"x-token": "main"}
        r = await c.post("/api/case/edit", json={"id": engine.case.id, "name": "renamed"}, headers=m)
        assert r.status_code == 200 and r.json()["name"] == "renamed" and engine.case.notes == "notes"
        r = await c.post("/api/case/edit", json={"id": engine.case.id, "name": ""}, headers=m)
        assert r.status_code == 400 and engine.case.name == "renamed"
        assert (await c.post("/api/case/edit", json={"id": engine.case.id, "name": "x"})).status_code == 403


# ---------------------------------------------------------------- sensitivity: raised, never lowered

def test_sensitivity_only_goes_up(tmp_path):
    c = Case.create("job", "open", root=tmp_path)
    c.edit("job", "", "confidential")
    assert Case.load(c.id, root=tmp_path).sensitivity == "confidential"
    last = json.loads((c.dir / "events.jsonl").read_text().splitlines()[-1])
    assert (last["old_sensitivity"], last["sensitivity"]) == ("open", "confidential")
    for lower in ("open", "nonsense"):
        with pytest.raises(ValueError):
            c.edit("job", "", lower)
    c.edit("renamed", "", None)                          # no level given: unchanged
    assert Case.load(c.id, root=tmp_path).sensitivity == "confidential"


async def test_raising_the_open_case_drops_a_model_it_no_longer_allows(env):  # noqa: F811
    engine, _, events = env
    await engine.list_models("Fake")
    engine.new_case("job", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    events.clear()
    r = engine.edit_case(engine.case.id, "job", sensitivity="confidential")
    assert r["sensitivity"] == "confidential" and Case.load(engine.case.id).sensitivity == "confidential"
    assert engine.cfg.active_model == ""                 # Standard tier isn't allowed for Confidential
    assert any(e["type"] == "toast" and "choose another" in e["text"] for e in events)
    assert engine.chat[-1]["kind"] == "note" and "OPEN to CONFIDENTIAL" in engine.chat[-1]["text"]
    assert engine.case.load_state()["chat"][-1] == engine.chat[-1]
    assert not {m["id"]: m["allowed"] for m in await engine.list_models("Fake")}["anthropic/claude-opus-5.5"]
    engine.select_model("Fake", "private/glm-5-3")
    engine.edit_case(engine.case.id, "job", sensitivity="sovereign")
    assert engine.cfg.active_model == ""                 # nor E2EE for Sovereign
    with pytest.raises(UserError, match="only be raised"):
        engine.edit_case(engine.case.id, "job", sensitivity="confidential")
    assert engine.case.sensitivity == "sovereign"


async def test_raising_waits_for_the_ai_but_a_rename_does_not(env):  # noqa: F811
    engine, _, _ = env
    engine.new_case("job", "open")
    engine._turn = asyncio.get_running_loop().create_future()      # a turn in progress
    try:
        with pytest.raises(UserError, match="Wait for the AI"):
            engine.edit_case(engine.case.id, "job", sensitivity="confidential")
        engine.edit_case(engine.case.id, "renamed", sensitivity="open")
        assert (engine.case.name, engine.case.sensitivity) == ("renamed", "open")
    finally:
        engine._turn.cancel()
        engine._turn = None


async def test_raising_a_case_on_disk(env):  # noqa: F811
    engine, _, _ = env
    engine.new_case("old", "open")
    old = engine.case.id
    engine.new_case("current", "open")
    engine.edit_case(old, "old", sensitivity="sovereign")
    assert {c["id"]: c["sensitivity"] for c in engine.list_cases()}[old] == "sovereign"
    assert engine.case.sensitivity == "open"


# ---------------------------------------------------------------- setting up before any case

async def test_setup_works_without_a_case(env):  # noqa: F811
    """The start-a-case dialog can be closed: settings, providers, models and hosts must work
    with no case, and nothing about a case is saved until one is started."""
    engine, _, _ = env
    app: FastAPI = create_app("main", lambda emit: engine)
    app.state.engine = engine
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        m = {"x-token": "main"}
        assert (await c.get("/api/state", headers=m)).json()["case"] is None
        r = await c.post("/api/providers", json={"provider": {"name": "Local", "base_url": "http://127.0.0.1:11434/v1"}}, headers=m)
        assert r.status_code == 200
        r = await c.post("/api/hosts", json={"host": {"name": "fs01", "kind": "ssh", "host": "fs01.lan", "user": "bob"}}, headers=m)
        assert r.status_code == 200 and engine.cfg.host("fs01")
        assert (await c.post("/api/settings", json={"font_size": 15}, headers=m)).status_code == 200
        assert (await c.post("/api/model", json={"provider": "Fake", "model": "anthropic/claude-opus-5.5"}, headers=m)).status_code == 200
        # what needs a case says so (the page offers to start one on this exact message)
        for path, body in (("/api/send", {"message": "hi"}), ("/api/sessions", {"kind": "local"})):
            r = await c.post(path, json=body, headers=m)
            assert r.status_code == 400 and r.json()["error"] == "Start a case first."
        assert (await c.get("/api/cases", headers=m)).json()["cases"] == []
    assert engine.case is None
