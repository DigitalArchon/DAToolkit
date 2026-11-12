"""The queue's own rules: a call is queued whole or not at all, ratings are made by the rules
in force, a rehearsal is rated like any command, and nothing proposed can hold the app up."""

import json

import pytest

from datoolkit.queue import Queue

from test_engine import env, sse, tool_call_stream, wait_turn  # noqa: F401


@pytest.mark.parametrize("bad", ["rm -rf /", None, 7, ["ls"], {"command": ["ls", "-la"]}, {"command": None},
                                 {"command": "ls", "purpose": {"why": "x"}}, {"command": "ls", "session_id": ["a"]}])
def test_a_call_with_an_item_that_cant_be_read_queues_none_of_it(bad):
    q = Queue()
    q.add("c0", [{"command": "uptime", "risk": "read_only"}])
    with pytest.raises((TypeError, ValueError), match="item 3"):
        q.add("c1", [{"command": "df -h", "risk": "read_only"}, {"command": "free -m", "risk": "read_only"}, bad])
    assert [p.command for p in q.items] == ["uptime"]
    (p,) = q.add("c2", [{"command": "who", "risk": "read_only"}])           # and no number was used up
    assert p.num == 2


def test_an_item_without_a_command_is_still_left_out_quietly():
    q = Queue()
    added = q.add("c1", [{"command": "df -h"}, {"purpose": "nothing to run"}, {"command": "  "}, {"command": "free -m", "risk": None}])
    assert [(p.num, p.command, p.model_risk) for p in added] == [(1, "df -h", "modifying"), (2, "free -m", "modifying")]


def test_a_rehearsal_of_an_item_that_is_gone_is_refused_before_anything_is_added():
    q = Queue()
    with pytest.raises(KeyError):
        q.add("dryrun", [{"command": "ls -ld /srv", "risk": "read_only"}], insert_before=9, dry_run_of=9)
    assert q.items == [] and q.add("c", [{"command": "uptime"}])[0].num == 1


async def test_the_ai_is_told_its_call_was_invalid_and_nothing_of_it_is_queued(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("x", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    bad = {"items": [{"session_id": sid, "command": "df -h", "purpose": "space", "risk": "read_only"}, "free -m"]}
    good = {"items": [{"session_id": sid, "command": "df -h", "purpose": "space", "risk": "read_only"},
                      {"session_id": sid, "command": "free -m", "purpose": "memory", "risk": "read_only"}]}
    fake.responses += [tool_call_stream(bad), tool_call_stream(good), sse(({"role": "assistant", "content": "Queued."}, "stop"))]
    engine.send("check the box")
    await wait_turn(engine)
    assert "Invalid propose_commands arguments (item 2 is not an object)" in fake.requests[1]["messages"][-1]["content"]
    # the second call's two items, once each, and both in the chat entry and the log
    assert [(p.num, p.command) for p in engine.queue.items] == [(1, "df -h"), (2, "free -m")]
    assert engine.chat[-1]["proposals"] == [1, 2]
    logged = [json.loads(line) for line in (engine.case.dir / "events.jsonl").read_text().splitlines()]
    assert [e["num"] for e in logged if e["event"] == "proposal"] == [1, 2]
