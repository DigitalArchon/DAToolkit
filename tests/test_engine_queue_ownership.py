"""The AI changes only what it queued itself: recipe steps, tool-transfer checks, rollbacks and dry
runs the technician queued are not its to withdraw or reorder, and its own items can't pose as a
recipe's."""

from datoolkit import tools_cache
from datoolkit.llm import prompts

from test_engine import env, wait_turn  # noqa: F401
from test_features import multi_tool_stream
from test_revise_search import setup


def _own(sid, command="df -h"):
    return ("propose_commands", {"items": [{"session_id": sid, "command": command, "purpose": "x", "risk": "read_only"}]})


async def test_the_ai_cannot_withdraw_what_the_technician_queued(env, tmp_path, monkeypatch):  # noqa: F811
    engine, fake, _ = env
    sid = setup(engine)
    steps = [p.num for p in engine.queue_recipe("baseline-linux", sid)]                # the technician's recipe
    engine.queue.add("call_m", [{"session_id": sid, "command": "systemctl restart nginx", "risk": "disruptive",
                                 "rollback": "systemctl stop nginx", "purpose": "restart"},
                                {"session_id": sid, "command": "apt-get install -y htop", "risk": "modifying"}])
    restart, install = steps[-1] + 1, steps[-1] + 2
    engine.update_item(restart, status="ran")
    rollback = engine.queue_rollbacks([restart])[0]["num"]
    dry = engine.dry_run_item(install)["num"]
    monkeypatch.setattr(tools_cache, "cache_dir", lambda: tmp_path / "tools")
    src = tmp_path / "probe.sh"
    src.write_text("#!/bin/sh\n")
    engine.add_tool(str(src), "probe", "linux", run="sh ~/probe.sh")
    engine.cfg.hosts[0].auth = "agent"
    sess = engine.sessions.sessions[sid]
    kind, host_name = sess.kind, sess.host_name
    local = engine.open_session("local")["id"]
    sess.kind, sess.host_name = "ssh", "web01"
    tool = engine.transfer_tool("probe", sid)["nums"]
    sess.kind, sess.host_name = kind, host_name
    theirs = [steps[0], rollback, dry, *tool]
    fake.responses += [
        multi_tool_stream([("revise_queue", {"withdraw": [{"num": n, "reason": "not needed"} for n in [*theirs, install]]})],
                          text="Tidying the queue."),
    ]
    engine.send("go on")
    await wait_turn(engine)
    assert all(engine.queue.get(n).status == "pending" for n in theirs)
    assert engine.queue.get(install).status == "withdrawn"                             # its own item
    reply = [m["content"] for m in engine.conv if m["role"] == "tool"][-1]
    assert f"Withdrew #{install}." in reply
    for n, what in ((steps[0], "a recipe's step"), (rollback, "a rollback"), (dry, "a dry run"), (tool[0], "a tool transfer")):
        assert f"#{n} was queued by the technician ({what}), not by you" in reply
    assert [w["num"] for w in engine.chat[-1]["withdrawn"]] == [install]
    assert local in engine.sessions.sessions


async def test_the_ai_reorders_only_its_own_items(env):  # noqa: F811
    engine, fake, _ = env
    sid = setup(engine)
    first = engine.queue_recipe("baseline-linux", sid)[0].num                          # #1…: the technician's
    fake.responses += [multi_tool_stream([_own(sid, "uptime"), _own(sid, "free -m")], text="Two checks.")]
    engine.send("go")
    await wait_turn(engine)
    a, b = engine.chat[-1]["proposals"]
    before = [p.num for p in engine.queue.items]
    fake.responses += [multi_tool_stream([("revise_queue", {"order": [b, first, a]})], text="Reordering.")]
    engine.send("reorder")
    await wait_turn(engine)
    after = [p.num for p in engine.queue.items]
    assert after.index(first) == before.index(first)                                   # the recipe's step stayed put
    assert after.index(b) < after.index(a)                                             # its own two swapped
    reply = [m["content"] for m in engine.conv if m["role"] == "tool"][-1]
    assert f"#{first} was queued by the technician, not by you, and keeps its place" in reply
    assert f"Pending items now run in the order #{b}, #{a}." in reply


async def test_a_recipe_the_ai_queued_is_its_own_to_withdraw(env):  # noqa: F811
    engine, fake, _ = env
    sid = setup(engine)
    fake.responses += [multi_tool_stream([("run_recipe", {"recipe_id": "baseline-linux", "session_id": sid})], text="Baseline.")]
    engine.send("go")
    await wait_turn(engine)
    num = engine.chat[-1]["proposals"][0]
    fake.responses += [multi_tool_stream([("revise_queue", {"withdraw": [{"num": num, "reason": "wrong OS"}]})], text="Sorry.")]
    engine.send("that is a Windows host")
    await wait_turn(engine)
    assert engine.queue.get(num).status == "withdrawn"


async def test_the_ais_own_items_cannot_pose_as_a_recipes_steps(env, monkeypatch):  # noqa: F811
    engine, fake, _ = env
    sid = setup(engine)
    item = {"session_id": sid, "command": "echo all-good", "purpose": "x", "risk": "read_only",
            "recipe": "baseline-linux", "recipe_key": "services"}
    fake.responses += [multi_tool_stream([("propose_commands", {"items": [item]})], text="Queued.")]
    engine.send("go")
    await wait_turn(engine)
    p = engine.queue.get(1)
    assert (p.command, p.recipe, p.recipe_key) == ("echo all-good", "", "")
    engine.update_item(1, status="ran")
    monkeypatch.setattr(engine, "capture", lambda num: {"text": "all-good", "source": "transcript"})
    assert engine._baseline_items(sid) == {}                                           # no section of a baseline


def test_the_queue_state_says_which_items_are_the_technicians():
    text = prompts.queue_text([{"num": 1, "session_id": "s", "status": "pending", "command": "uname -a", "call_id": "recipe"},
                               {"num": 2, "session_id": "s", "status": "pending", "command": "df -h", "call_id": "call_9"},
                               {"num": 3, "session_id": "s", "status": "ran", "command": "id", "call_id": "rollback"}])
    lines = text.split("\n")
    assert "#1" in lines[2] and "queued by the technician, a recipe's step: not yours to withdraw or reorder" in lines[2]
    assert "#2" in lines[3] and "technician" not in lines[3]
    assert "queued by the technician" not in text.split("Run by the technician")[1]     # said of pending items only
    assert "that you queued" in prompts.REVISE_TOOL["function"]["description"]
