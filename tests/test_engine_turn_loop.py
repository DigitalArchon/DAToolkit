"""The turn loop's own state: what a round is sent, what a turn that overflowed or produced nothing
leaves behind, and a conversation with a call left open."""

import json

import httpx

from datoolkit import creds
from datoolkit.engine import MAX_TOOL_ROUNDS, PROMISE_NUDGE
from datoolkit.llm.client import LLMClient

from test_engine import env, sse, wait_turn  # noqa: F401
from test_features import multi_tool_stream
from test_research import say
from test_revise_search import setup
from test_turn_recovery import unanswered

HYP = ("update_hypotheses", {"items": [{"id": "dns", "text": "DNS is slow", "confidence": 0.6, "status": "open"}]})
OVERFLOW = {"error": {"message": "This request is too long for the model's context window.", "code": "context_length_exceeded"}}
USAGE = {"prompt_tokens": 300, "completion_tokens": 5, "total_tokens": 305}


def with_usage(stream: str) -> str:
    chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": [], "usage": USAGE}
    return stream.replace("data: [DONE]", f"data: {json.dumps(chunk)}\n\ndata: [DONE]")


def answering(engine, fake, monkeypatch, first):
    """The fake API behind `first(body) -> httpx.Response | None`, which answers a request itself when
    it returns a response."""
    def handler(request):
        if not request.url.path.endswith("/models"):
            answer = first(json.loads(request.content))
            if answer is not None:
                return answer
        return fake.handler(request)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(engine, "_client", lambda prov, model="": LLMClient(
        prov.base_url, creds.get_secret("provider", prov.name), http))


def orphans(messages: list[dict]) -> list[str]:
    """Tool messages that answer no call of the assistant message before them, which a provider refuses."""
    out, open_ids = [], set()
    for m in messages:
        if m["role"] == "assistant":
            open_ids = {c["id"] for c in m.get("tool_calls") or []}
        elif m["role"] == "tool":
            if m["tool_call_id"] not in open_ids:
                out.append(m["tool_call_id"])
        else:
            open_ids = set()
    return out


# ---------------------------------------------------------------- words only is for one round

async def test_the_round_after_the_words_only_round_may_call_tools_again(env):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    fake.responses += [
        multi_tool_stream([HYP], text=""),             # round 1: the board only, no words: carries on
        multi_tool_stream([HYP], text=""),             # round 2: the same: asked for its message, words only
        say("DNS looks likely. Now I need you to run a few more commands to confirm it."),    # round 3: a promise
        multi_tool_stream([("propose_commands", {"items": [{"command": "dig example.com", "risk": "read_only",
                                                            "purpose": "resolver"}]})], text="Queued."),
    ]
    engine.send("names resolve slowly")
    await wait_turn(engine)
    first, second, words, promised = fake.requests
    assert [r["tool_choice"] for r in (first, second, words)] == ["auto", "auto", "none"]
    assert promised["messages"][-1]["content"] == PROMISE_NUDGE          # asked to queue the commands,
    assert promised["tool_choice"] == "auto"                             # and allowed to
    assert engine.chat[-1]["proposals"] == [1] and engine.queue.get(1).command == "dig example.com"


# ---------------------------------------------------------------- overflow

async def _two_exchanges(engine, fake):
    for text in ("web01 is slow", "load average is 12"):
        fake.responses.append(with_usage(say("ok")))
        engine.send(text)
        await wait_turn(engine)


async def test_an_overflow_in_a_later_round_is_remembered_and_compaction_sends_only_the_part(env, monkeypatch):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    # too long once the conversation holds the third exchange's tool reply: the turn's second round
    answering(engine, fake, monkeypatch, lambda body: httpx.Response(400, json=OVERFLOW) if any(
        m["role"] == "tool" for m in body["messages"]) and len(body["messages"]) > 6 else None)
    await _two_exchanges(engine, fake)
    assert engine._overflowed is False
    fake.responses.append(with_usage(multi_tool_stream([("propose_commands", {"items": "x"})], text="Let me queue it.")))
    engine.send("and now?")
    await wait_turn(engine)
    assert engine.chat[-1].get("context") is True and engine.chat[-1]["retry"]
    assert engine._overflowed is True                    # though the turn's first round went through and reported usage
    fake.responses.append(say("Summary."))
    r = await engine.compact_preview(0)
    assert r["whole"] is False and r["summary"] == "Summary."
    sent = [m["content"] for m in fake.requests[-1]["messages"] if m["role"] == "user"]
    assert sent[0] == "web01 is slow" and len(sent) == 2 and "load average is 12" not in sent   # the part, and the instruction
    # a turn that fits again clears it
    engine.compact_apply(r["summary"])
    fake.responses.append(with_usage(say("Carrying on.")))
    engine.retry()
    await wait_turn(engine)
    assert engine._overflowed is False and engine.chat[-1]["text"] == "Carrying on."


async def test_compaction_that_overflows_whole_is_tried_again_with_the_part_alone(env, monkeypatch):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    answering(engine, fake, monkeypatch, lambda body: httpx.Response(400, json=OVERFLOW) if len(body["messages"]) > 5 else None)
    await _two_exchanges(engine, fake)                   # system + 4: fits; a third exchange, or the instruction, doesn't
    assert engine._overflowed is False
    asked = len(fake.requests)
    fake.responses.append(say("Summary of the first."))
    r = await engine.compact_preview(0)
    assert r["summary"] == "Summary of the first." and r["whole"] is False and engine._overflowed is True
    assert len(fake.requests) == asked + 1               # the whole was refused before the fake API; the part went through
    assert [m["content"] for m in fake.requests[-1]["messages"] if m["role"] == "user"][0] == "web01 is slow"
    logged = [json.loads(line) for line in (engine.case.dir / "requests.jsonl").read_text().splitlines()]
    assert [("error" in r["response"]) for r in logged if r["purpose"] == "compact"] == [True, False]


# ---------------------------------------------------------------- a call with one bad item

async def test_one_malformed_item_queues_nothing_and_the_model_is_told_which(env):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    good = {"command": "df -h", "risk": "read_only", "purpose": "disk"}
    fake.responses += [multi_tool_stream([("propose_commands", {"items": [good, "uptime"]})], text="Checking disk."),
                       multi_tool_stream([("propose_commands", {"items": [good, {"command": "uptime", "risk": "read_only",
                                                                                 "purpose": "load"}]})], text="Again.")]
    engine.send("disk is full")
    await wait_turn(engine)
    replies = [m["content"] for m in engine.conv if m["role"] == "tool"]
    assert "item 2 is a string, not an object" in replies[0] and "Nothing was queued" in replies[0]
    assert [(p.num, p.command) for p in engine.queue.items] == [(1, "df -h"), (2, "uptime")]      # once each
    assert engine.chat[-1]["proposals"] == [1, 2]
    logged = [json.loads(line) for line in (engine.case.dir / "events.jsonl").read_text().splitlines()]
    assert [e["num"] for e in logged if e["event"] == "proposal"] == [1, 2]


async def test_a_call_that_fails_while_queueing_leaves_nothing_half_done(env, monkeypatch):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    real = engine.queue.add

    def fails_late(call_id, items, **kw):                # as a queue that checks each item as it adds it
        real(call_id, items[:1], **kw)
        raise ValueError("item 2 has no command")
    monkeypatch.setattr(engine.queue, "add", fails_late)
    fake.responses += [multi_tool_stream([("propose_commands", {"items": [{"command": "df -h"}, {"purpose": "x"}]})],
                                         text="Checking."), say("Sorry.")]
    engine.send("disk is full")
    await wait_turn(engine)
    assert engine.queue.items == [] and engine.chat[-1]["proposals"] == []
    reply = [m["content"] for m in engine.conv if m["role"] == "tool"][0]
    assert "item 2 has no command" in reply and "Nothing was queued" in reply


# ---------------------------------------------------------------- a call left open

def _saved_mid_turn():
    """A conversation as state.json held it while a search waited for approval: one call of two answered."""
    calls = [{"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}
             for cid, name in (("c0", "update_hypotheses"), ("c1", "web_search"))]
    return [{"role": "user", "content": "vpn drops"},
            {"role": "assistant", "content": "Let me look.", "tool_calls": calls},
            {"role": "tool", "tool_call_id": "c0", "content": "Hypothesis board updated (1 items)."}]


async def test_a_call_left_open_is_answered_in_its_place_not_after_the_next_message(env, monkeypatch):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    engine.conv = _saved_mid_turn()
    engine._req_conv_len = len(engine.conv)
    failed = []
    answering(engine, fake, monkeypatch, lambda body: failed.append(body) or httpx.Response(
        400, json={"error": {"message": "upstream down"}}) if not failed else None)
    engine.send("are you there?")
    await wait_turn(engine)
    assert "upstream down" in engine._last_turn_error
    assert [(m["role"], m.get("tool_call_id", "")) for m in engine.conv] == [
        ("user", ""), ("assistant", ""), ("tool", "c0"), ("tool", "c1"), ("user", "")]
    fake.responses.append(say("Yes."))
    engine.send("hello?")
    await wait_turn(engine)
    sent = fake.requests[-1]["messages"][1:]
    assert orphans(sent) == [] and unanswered(sent) == [] and engine.chat[-1]["text"] == "Yes."


async def test_a_conversation_already_saved_with_a_misplaced_reply_is_put_right(env):  # noqa: F811
    # as 0.1.0b28 left it: the missing reply appended after the technician's next message
    engine, fake, _ = env
    setup(engine)
    engine.conv = _saved_mid_turn() + [{"role": "user", "content": "are you there?"},
                                       {"role": "tool", "tool_call_id": "c1", "content": "Not done: an error."}]
    engine._req_conv_len = len(engine.conv)
    fake.responses.append(say("Yes."))
    engine.send("hello?")
    await wait_turn(engine)
    sent = fake.requests[-1]["messages"][1:]
    assert [(m["role"], m.get("tool_call_id", "")) for m in sent] == [
        ("user", ""), ("assistant", ""), ("tool", "c0"), ("tool", "c1"), ("user", ""), ("user", "")]
    assert sent[3]["content"] == "Not done: an error." and orphans(sent) == []
    logged = [json.loads(line) for line in (engine.case.dir / "requests.jsonl").read_text().splitlines()]
    assert logged[-1]["conv_index"] == 0                 # rearranged, so logged whole


# ---------------------------------------------------------------- a turn with nothing to show

async def test_a_turn_out_of_rounds_with_nothing_to_show_says_so_and_can_be_retried(env):  # noqa: F811
    engine, fake, events = env
    setup(engine)
    # a model stuck writing an invalid call, with no words
    fake.responses += [multi_tool_stream([("propose_commands", {"items": "df -h"})], text="") for _ in range(MAX_TOOL_ROUNDS)]
    engine.send("disk is full")
    await wait_turn(engine)
    assert len(fake.requests) == MAX_TOOL_ROUNDS
    note = engine.chat[-1]
    assert note["kind"] == "note" and note["retry"] and "nothing to show" in note["text"]
    assert f"{MAX_TOOL_ROUNDS} times" in note["text"]
    end = [e for e in events if e["type"] == "turn_end"][-1]
    assert end["error"] and end["can_retry"] and engine.can_retry
    assert unanswered(engine.conv) == []
    fake.responses.append(say("The disk is full of logs."))
    engine.retry()
    await wait_turn(engine)
    assert engine.chat[-1]["text"] == "The disk is full of logs." and engine._last_turn_error is None


async def test_an_empty_reply_says_so_and_can_be_retried(env):  # noqa: F811
    engine, fake, events = env
    setup(engine)
    fake.responses.append(sse(({"role": "assistant", "content": ""}, "stop")))
    engine.send("web01 is slow")
    await wait_turn(engine)
    assert [e["kind"] for e in engine.chat] == ["user", "note"]
    assert "empty reply" in engine.chat[-1]["text"] and engine.chat[-1]["retry"] and engine.can_retry
    fake.responses.append(say("Check the load."))
    engine.retry()
    await wait_turn(engine)
    assert engine.chat[-1]["text"] == "Check the load."
    assert [m["role"] for m in engine.conv] == ["user", "assistant"]      # the empty reply is gone


async def test_a_turn_out_of_rounds_with_something_to_show_says_it_stopped_there(env):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    fake.responses += [multi_tool_stream([("propose_commands", {"items": "df -h"})], text="Trying.")
                       for _ in range(MAX_TOOL_ROUNDS)]
    engine.send("disk is full")
    await wait_turn(engine)
    said, note = engine.chat[-2:]
    assert said["kind"] == "assistant" and said["text"].startswith("Trying.")
    assert note["kind"] == "note" and f"limit of {MAX_TOOL_ROUNDS} rounds" in note["text"] and "retry" not in note
    assert engine._last_turn_error is None
