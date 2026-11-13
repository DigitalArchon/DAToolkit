"""Baselines and kept research reports follow the client and sensitivity of the case they came
from: neither is filed by more than a host's name or a brief's words, and both end up in what a
case sends to its model."""

import json
from pathlib import Path

import pytest

from datoolkit.case import Case
from datoolkit.engine import UserError

from test_engine import env, wait_turn  # noqa: F401
from test_research import agent_turns, research_call, rs, say, wait_for  # noqa: F401


def _case(engine, monkeypatch, name, sensitivity="open", client=""):
    """A case with the baseline recipe run on a local session; returns (session id, the items)."""
    if client and client not in engine.cfg.clients:
        engine.add_client(client)
    engine.new_case(name, sensitivity, client=client)
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    added = engine.queue_recipe("baseline-linux", sid)
    for p in added:
        engine.update_item(p.num, status="ran")
    monkeypatch.setattr(engine, "capture", lambda num: {"text": f"secret-of-{name}", "source": "transcript"})
    return sid


async def test_a_baseline_from_a_more_sensitive_case_is_refused(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    sid = _case(engine, monkeypatch, "vault", "sovereign")
    saved = engine.save_baseline(sid)["path"]
    stored = json.loads(Path(saved).read_text())
    assert (stored["sensitivity"], stored["client"]) == ("sovereign", "")
    assert engine.diff_baseline(sid)["sensitivity"] == "sovereign"            # its own case may
    sid = _case(engine, monkeypatch, "helpdesk", "open")
    listed = engine.list_baselines(sid)
    assert (listed[0]["sensitivity"], listed[0]["case_name"]) == ("sovereign", "vault") and "Sovereign" in listed[0]["refused"]
    for path in (saved, ""):                                                  # chosen, or the host's newest
        with pytest.raises(UserError, match="the case vault, which is Sovereign; this case is Open"):
            engine.diff_baseline(sid, path)


async def test_a_baseline_follows_its_case_when_that_was_raised_since(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    sid = _case(engine, monkeypatch, "first", "open")
    saved = engine.save_baseline(sid)["path"]
    first = engine.case.id
    engine.edit_case(first, "first", sensitivity="confidential")              # raised after the baseline was saved
    sid = _case(engine, monkeypatch, "second", "open")
    with pytest.raises(UserError, match="Confidential"):
        engine.diff_baseline(sid, saved)
    # the case gone, what was saved with the baseline decides: Open, as it was then
    Case.delete(first)
    d = engine.diff_baseline(sid, saved)
    assert (d["sensitivity"], d["case_name"], d["client"]) == ("open", "", "")


async def test_a_baseline_of_another_clients_machine_is_refused(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    sid = _case(engine, monkeypatch, "acme router", client="Acme Dental")
    saved = engine.save_baseline(sid)["path"]
    sid = _case(engine, monkeypatch, "other router", client="Other Corp")
    assert "Acme Dental" in engine.list_baselines(sid)[0]["refused"]
    with pytest.raises(UserError, match="for the client Acme Dental; this case is for Other Corp"):
        engine.diff_baseline(sid, saved)
    sid = _case(engine, monkeypatch, "acme again", client="Acme Dental")
    d = engine.diff_baseline(sid, saved)
    assert (d["client"], d["case_name"], d["sensitivity"]) == ("Acme Dental", "acme router", "open")
    sid = _case(engine, monkeypatch, "no client")                             # only two clients that differ are refused
    assert engine.diff_baseline(sid, saved)["client"] == "Acme Dental"


async def test_a_baseline_of_unknown_origin_is_usable_only_in_a_sovereign_case(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    sid = _case(engine, monkeypatch, "old", "open")
    saved = Path(engine.save_baseline(sid)["path"])
    data = json.loads(saved.read_text())
    saved.write_text(json.dumps({k: v for k, v in data.items() if k not in ("sensitivity", "client")}))   # as saved before
    gone = engine.case.id
    sid = _case(engine, monkeypatch, "today", "open")             # the case it came from is closed, then deleted
    Case.delete(gone)
    assert engine.list_baselines(sid)[0]["sensitivity"] is None
    with pytest.raises(UserError, match="only in a Sovereign case"):
        engine.diff_baseline(sid, str(saved))
    sid = _case(engine, monkeypatch, "sealed", "sovereign")
    assert engine.diff_baseline(sid, str(saved))["sensitivity"] is None


async def test_the_default_baseline_is_the_newest_this_case_may_use(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    sid = _case(engine, monkeypatch, "mine", "open")
    mine = Path(engine.save_baseline(sid)["path"])
    mine = mine.rename(mine.with_name("20200101-000000.json"))
    sid = _case(engine, monkeypatch, "vault", "sovereign")
    engine.save_baseline(sid)                                                 # newer, and out of an Open case's reach
    sid = _case(engine, monkeypatch, "mine again", "open")
    d = engine.diff_baseline(sid)
    assert d["baseline"] == str(mine) and "secret-of-vault" not in d["text"]


async def test_a_kept_report_is_not_handed_to_another_clients_case(rs):  # noqa: F811
    engine, fake, _, web = rs
    engine.cfg.settings.search_mode = "auto"
    for name in ("Acme Dental", "Other Corp"):
        engine.add_client(name)
    engine.new_case("acme", "open", client="Acme Dental")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    fake.responses += [research_call()] + agent_turns() + [say("Per the docs.")]
    engine.send("need a dhcp relay on the sophos")
    await wait_turn(engine)
    assert engine.chat[-1]["research"][0]["status"] == "done"
    searched = len(web.calls)

    engine.new_case("other", "open", client="Other Corp")
    fake.responses += [research_call()] + agent_turns() + [say("Per the docs.")]
    engine.send("same question, another client")
    await wait_turn(engine)
    assert engine.chat[-1]["research"][0]["status"] == "done" and len(web.calls) > searched    # researched again

    searched = len(web.calls)
    engine.new_case("acme again", "open", client="Acme Dental")
    fake.responses += [research_call(), say("Cached says so.")]
    engine.send("same question, the same client")
    await wait_turn(engine)
    assert engine.chat[-1]["research"][0]["status"] == "cached" and len(web.calls) == searched


async def test_a_kept_report_waits_for_the_technicians_approval_where_research_does(rs):  # noqa: F811
    engine, fake, _, web = rs
    engine.cfg.settings.search_mode = "auto"
    engine.new_case("one", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    fake.responses += [research_call()] + agent_turns() + [say("Per the docs.")]
    engine.send("need a dhcp relay on the sophos")
    await wait_turn(engine)
    searched, asked = len(web.calls), len(fake.requests)

    engine.cfg.settings.search_mode = "ask"
    engine.new_case("two", "open")
    fake.responses += [research_call(), say("Cached says so.")]
    engine.send("same again")
    await wait_for(lambda: engine._research_reqs)                             # asked, though a report is kept
    assert len(fake.requests) == asked + 1
    assert engine.snapshot()["research_requests"][0]["status"] == "awaiting"
    engine.answer_research(next(iter(engine._research_reqs)), True)
    await wait_turn(engine)
    assert engine.chat[-1]["research"][0]["status"] == "cached" and len(web.calls) == searched

    engine.new_case("three", "open")
    fake.responses += [research_call(), say("Without it, then.")]
    engine.send("same again")
    await wait_for(lambda: engine._research_reqs)
    engine.answer_research(next(iter(engine._research_reqs)), False)
    await wait_turn(engine)
    reply = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"][-1]["content"]
    assert engine.chat[-1]["research"][0]["status"] == "declined" and "Cached research report" not in reply
