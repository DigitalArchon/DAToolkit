"""Compaction: the chat model summarises the older part of its context; the technician applies or undoes it."""

import json

import pytest

from datoolkit.engine import UserError
from datoolkit.export import full_transcript
from datoolkit.llm import prompts

from test_engine import env, wait_turn  # noqa: F401
from test_research import nano, say


async def _three_exchanges(engine, fake, provider="Fake"):
    engine.new_case("long", "open")
    engine.select_model(provider, "anthropic/claude-opus-5.5")
    for i, text in enumerate(["web01 is slow", "load average is 12", "it's the backup job"]):
        fake.responses.append(say(f"reply {i}"))
        engine.send(text)
        await wait_turn(engine)
    return list(engine.conv)


async def test_compaction_summarises_the_older_exchanges_and_keeps_the_rest(env):  # noqa: F811
    engine, fake, _ = env
    old = await _three_exchanges(engine, fake)
    view = engine.context_view()
    assert len(view["groups"]) == 3 and view["model"] == "anthropic/claude-opus-5.5"
    fake.responses.append(say("Problem: web01 slow. Findings: load 12."))
    r = await engine.compact_preview(1)
    req = fake.requests[-1]
    assert req["tools"]                                     # sent like a chat request, so its cache applies
    instruction = req["messages"][-1]["content"]
    assert "Changes made" in instruction and 'begins "it\'s the backup job"' in instruction
    assert [m["content"] for m in req["messages"][1:-1] if m["role"] == "user"] == [
        "web01 is slow", "load average is 12", "it's the backup job"]
    assert r["summary"] == "Problem: web01 slow. Findings: load 12." and r["exchanges"] == 2 and r["kept"] == 1
    assert engine.conv == old                               # nothing changes before apply

    engine.compact_apply(r["summary"] + "\nEdited by the technician.")
    assert engine.conv[0]["role"] == "user" and engine.conv[0]["content"].startswith("[DAToolkit: earlier exchanges")
    assert engine.conv[0]["content"].endswith("Edited by the technician.")
    assert engine.conv[1] == {"role": "assistant", "content": prompts.COMPACT_ACK}
    assert engine.conv[2:] == old[4:]                       # the last exchange, word for word
    assert engine.context_view()["groups"][0]["compacted"] is True
    assert "Compacted 2 exchange(s)" in engine.chat[-1]["text"]
    state = json.loads((engine.case.dir / "state.json").read_text())
    assert state["compactions"][0]["file"] == "context-before-compact-1.json" and state["conv"] == engine.conv
    backup = json.loads((engine.case.dir / "context-before-compact-1.json").read_text())
    assert backup["conv"] == old and backup["replaced_messages"] == 4

    fake.responses.append(say("Carrying on."))
    engine.send("what next?")
    await wait_turn(engine)
    sent = fake.requests[-1]["messages"]
    assert sent[1] == engine.conv[0] and sent[1:] == engine.conv[:-1]      # system + all but the new reply
    requests = [json.loads(line) for line in (engine.case.dir / "requests.jsonl").read_text().splitlines()]
    assert requests[-2]["purpose"] == "compact" and requests[-1]["conv_index"] == 0   # logged whole again
    md = full_transcript(engine.case.to_dict(), requests, engine.conv, engine.chat, "sys")
    assert "· compact ·" in md and "The conversation was trimmed or rebuilt" in md


async def test_undo_puts_the_exchanges_back_and_keeps_what_came_after(env):  # noqa: F811
    engine, fake, _ = env
    old = await _three_exchanges(engine, fake)
    fake.responses.append(say("Summary."))
    await engine.compact_preview(0)
    engine.compact_apply("Summary.")
    assert engine.snapshot()["can_undo_compaction"]
    fake.responses.append(say("ok"))
    engine.send("one more")
    await wait_turn(engine)
    engine.compact_undo()
    assert engine.conv[:len(old)] == old and engine.conv[-2:] == [
        {"role": "user", "content": "one more"}, {"role": "assistant", "content": "ok"}]
    assert not engine.compactions and not engine.snapshot()["can_undo_compaction"]
    with pytest.raises(UserError, match="no compaction to undo"):
        engine.compact_undo()


async def test_undo_is_refused_once_the_summary_is_gone(env):  # noqa: F811
    engine, fake, _ = env
    await _three_exchanges(engine, fake)
    fake.responses.append(say("Summary."))
    await engine.compact_preview(1)
    engine.compact_apply("Summary.")
    engine.drop_context([0])
    assert not engine.snapshot()["can_undo_compaction"]


async def test_apply_is_refused_when_the_conversation_changed(env):  # noqa: F811
    engine, fake, _ = env
    await _three_exchanges(engine, fake)
    fake.responses.append(say("Summary."))
    await engine.compact_preview(0)
    fake.responses.append(say("ok"))
    engine.send("meanwhile")
    await wait_turn(engine)
    with pytest.raises(UserError, match="changed since the summary"):
        engine.compact_apply("Summary.")
    with pytest.raises(UserError, match="latest one always stays"):
        await engine.compact_preview(3)
    with pytest.raises(UserError, match="Write a summary first"):
        engine.compact_apply("Summary.")


async def test_after_an_overflow_only_the_part_to_compact_is_sent(env):  # noqa: F811
    engine, fake, _ = env
    await _three_exchanges(engine, fake)
    fake.overflow = "This model's maximum context length is 32768 tokens. However, you requested 40000 tokens."
    engine.send("and now?")
    await wait_turn(engine)
    assert engine._overflowed and "Compact or remove" in engine.chat[-1]["text"]
    fake.overflow = None
    fake.responses.append(say("Summary."))
    r = await engine.compact_preview(1)
    users = [m["content"] for m in fake.requests[-1]["messages"] if m["role"] == "user"]
    assert users[:2] == ["web01 is slow", "load average is 12"] and "it's the backup job" not in users
    assert "the conversation above" in users[-1] and not r["whole"]
    # a part that still doesn't fit says what to do
    fake.overflow = "prompt is too long: 40000 tokens > 32768 maximum"
    with pytest.raises(UserError, match="Compact fewer exchanges"):
        await engine.compact_preview(0)


async def test_with_claude_caching_the_instruction_follows_the_cached_conversation(env):  # noqa: F811
    engine, fake, _ = env
    nano(engine)
    await _three_exchanges(engine, fake, provider="NanoGPT")
    chat_req = fake.requests[-1]
    fake.responses.append(say("Summary."))
    await engine.compact_preview(1)
    req = fake.requests[-1]
    assert req["prompt_caching"] == chat_req["prompt_caching"] | {"cut_after_message_index": len(req["messages"]) - 2}
    assert req["messages"][:-1] == chat_req["messages"][:-1] + [{"role": "assistant", "content": "reply 2"}]
    last = req["messages"][-1]["content"]
    assert last.startswith(prompts.STATE_HEADER) and "compacting your context" in last
    assert engine.context_view()["cache"] == "1h"


def test_ui_offers_compaction_with_a_review_step_and_undo():
    from pathlib import Path
    js = (Path(__file__).parent.parent / "src/datoolkit/web/app.js").read_text()
    for needle in ('"/api/context/compact"', '"/api/context/compact/apply"', '"/api/context/compact/undo"',
                   "can_undo_compaction", "writes the new, shorter context to the cache again", "exchangeLabel"):
        assert needle in js, needle
