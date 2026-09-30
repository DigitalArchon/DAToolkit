"""Conversational flow: questions with quick replies, hypothesis moves shown inline, one-click
skips, and the stacking fix that keeps terminal selections under dialogs."""

import re
from pathlib import Path

from datoolkit.config import Provider
from datoolkit.engine import hypothesis_changes
from datoolkit.llm import prompts

from test_engine import env, sse, wait_turn  # noqa: F401
from test_features import multi_tool_stream

WEB = Path(__file__).resolve().parents[1] / "src" / "datoolkit" / "web"


def test_prompt_asks_for_conversation_and_offers_questions():
    names = [t["function"]["name"] for t in prompts.TOOLS]
    assert "ask_technician" in names
    assert "write your message to the technician FIRST, before any tool call" in prompts.SYSTEM_PROMPT
    assert "skips a command" in prompts.SYSTEM_PROMPT


async def test_ask_technician_and_hypothesis_moves_land_on_the_message(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("q", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    fake.responses.append(multi_tool_stream([
        ("update_hypotheses", {"items": [{"id": "dns", "text": "DNS", "confidence": 0.5, "status": "open"},
                                         {"id": "disk", "text": "Disk", "confidence": 0.3, "status": "open"}]}),
        ("ask_technician", {"questions": [{"question": "When did it start?", "options": ["Today", "This week", ""]},
                                          "Anything changed recently?", {"question": ""}]}),
        ("propose_commands", {"items": [{"session_id": sid, "command": "df -h", "purpose": "disk", "risk": "read_only"}]}),
    ], text="Sounds like DNS or disk. A couple of questions while this runs."))
    engine.send("site is slow")
    await wait_turn(engine)

    entry = engine.chat[-1]
    assert entry["kind"] == "assistant" and entry["text"].startswith("Sounds like")
    assert entry["questions"] == [{"question": "When did it start?", "options": ["Today", "This week"]},
                                  {"question": "Anything changed recently?", "options": []}]
    assert [c["kind"] for c in entry["hyp_changes"]] == ["new", "new"]
    assert entry["proposals"] == [1]
    replies = [m["content"] for m in engine.conv if m["role"] == "tool"]
    assert any("Questions shown" in r for r in replies)
    assert len(fake.requests) == 1          # a question is not an error: no extra round trip

    # the answer comes back as an ordinary message; the board move is reported on the next reply
    fake.responses.append(multi_tool_stream([
        ("update_hypotheses", {"items": [{"id": "dns", "text": "DNS", "confidence": 0.8, "status": "supported"}]}),
    ], text="Today fits the DNS change."))
    engine.send("Q: When did it start? — Today")
    await wait_turn(engine)
    moves = {c["id"]: c["kind"] for c in engine.chat[-1]["hyp_changes"]}
    assert moves == {"dns": "supported", "disk": "dropped"}
    assert "**Question:** When did it start? (Today / This week)" in Path(engine.export_markdown()).read_text()


async def test_bad_questions_are_answered_not_retried(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("q", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    fake.responses.append(multi_tool_stream([("ask_technician", {"questions": "nope"})], text="Hmm."))
    engine.send("hi")
    await wait_turn(engine)
    assert len(fake.requests) == 1
    assert "Invalid ask_technician" in engine.conv[-1]["content"]
    assert "questions" not in engine.chat[-1]


def test_hypothesis_changes_thresholds():
    old = {"a": {"id": "a", "confidence": 0.5, "status": "open"}, "b": {"id": "b", "confidence": 0.5, "status": "open"},
           "c": {"id": "c", "confidence": 0.5, "status": "open"}}
    new = [{"id": "a", "text": "A", "confidence": 0.52, "status": "open"},      # below threshold
           {"id": "b", "text": "B", "confidence": 0.3, "status": "open"},
           {"id": "c", "text": "C", "confidence": 0.0, "status": "ruled_out"}]
    assert [(c["id"], c["kind"]) for c in hypothesis_changes(old, new)] == [("b", "down"), ("c", "ruled_out")]


async def test_skip_without_reason_tells_the_ai_generically(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("s", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    engine.queue.add("c", [{"session_id": sid, "command": "cat /etc/shadow", "purpose": "x", "risk": "read_only"},
                           {"session_id": sid, "command": "uptime", "purpose": "y", "risk": "read_only"}])
    engine.update_item(1, status="skipped", note="")                # force skip: one click, no note
    engine.update_item(2, status="skipped", note="already checked")  # skip with a reason
    fake.responses.append(sse(({"role": "assistant", "content": "Understood."}, "stop")))
    engine.send("", results=[{"num": 1}, {"num": 2}])
    await wait_turn(engine)
    content = fake.requests[0]["messages"][-1]["content"]
    first, second = content.split("#2 on session")
    assert "SKIPPED" in first and "chose not to run this and gave no reason" in first
    assert "already checked" in second and "gave no reason" not in second


async def test_training_scenario_asks_a_question(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    monkeypatch.delattr(engine, "_client")
    engine.cfg.providers.append(Provider("Training", "training://disk-full"))
    engine.new_case("train", "sovereign")
    engine.select_model("Training", "training/disk-full")
    engine.open_session("local")
    engine.send("disk is full")
    await wait_turn(engine)
    assert engine.chat[-1]["questions"][0]["options"] == ["Yes", "No", "Not sure"]


def test_dialogs_stack_above_the_terminal():
    css = (WEB / "style.css").read_text()
    assert re.search(r"#modal-root\s*\{[^}]*z-index:\s*\d+", css)
    assert re.search(r"#terms\s*\{[^}]*isolation:\s*isolate", css)


def test_force_skip_is_one_click_in_queue_and_chat():
    js = (WEB / "app.js").read_text()
    assert 'status: "skipped", note: ""' in js
    assert js.count('btn("Force skip"') == 2       # queue row and the card in the chat


async def test_tool_call_start_is_reported_while_arguments_stream(env):  # noqa: F811
    """Arguments stream without visible text; the UI needs to know the model is still working."""
    engine, fake, events = env
    engine.new_case("t", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    fake.responses.append(multi_tool_stream([("ask_technician", {"questions": ["When?"]}),
                                             ("update_hypotheses", {"items": []})], text="Let me ask."))
    engine.send("hi")
    await wait_turn(engine)
    tools = [e["name"] for e in events if e["type"] == "delta" and e["kind"] == "tool"]
    assert tools == ["ask_technician", "update_hypotheses"]
    assert engine.chat[-1]["questions"] == [{"question": "When?", "options": []}]


def test_prompt_knows_about_appliance_menus():
    assert "console menu" in prompts.SYSTEM_PROMPT and "Never send shell commands to a menu" in prompts.SYSTEM_PROMPT


def test_ui_has_turn_status_and_terminal_screenshot():
    js, html = (WEB / "app.js").read_text(), (WEB / "index.html").read_text()
    assert "function terminalScreenshot" in js and 'id="tab-shot-btn"' in html and 'id="shot-btn"' in html
    assert "AI finished" in js and "still working" in js
    assert 'role="status"' in html


async def test_roster_never_reads_like_a_prompt_and_says_what_was_seen(env):  # noqa: F811
    """A target written user@host ("root@192.168.1.1") was taken by the model for a shell
    prompt it could see. The roster now names the login user separately, says it is
    connection details only, and states per session whether any output has been sent."""
    engine, fake, _ = env
    engine.new_case("r", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.sessions.spawn("router1", ["/bin/cat"], {}, name="Router1", kind="ssh", target="root@192.168.1.1",
                          shell="remote shell/CLI", address="192.168.1.1")
    text = engine._system_prompt()
    roster = text.split("Open sessions.")[1].split("\n\n")[0]
    assert "root@" not in roster and "192.168.1.1, logging in as user `root`" in roster
    assert "not screen contents" in roster and "NOT been sent any output from this session yet" in roster
    engine.queue.add("c", [{"session_id": "router1", "command": "uname -a", "risk": "read_only"}])
    engine.update_item(1, status="ran")
    fake.responses.append(sse(({"role": "assistant", "content": "ok"}, "stop")))
    engine.send("", results=[{"num": 1, "text": "FreeBSD 14.1"}])
    await wait_turn(engine)
    assert "Output from it has been sent to you 1 time(s)" in engine._system_prompt()
    engine.sessions.close_all()
