"""A turn that ends early (Stop, or a tool call whose handling failed) leaves a conversation every
provider accepts: each tool call has its answer, and no round's words are recorded twice."""

import asyncio
import json

import httpx
import pytest

from datoolkit import creds
from datoolkit.llm.client import NO_RESULT, LLMClient, ToolCall, openai_messages

from test_engine import env, sse, wait_turn  # noqa: F401
from test_features import multi_tool_stream
from test_revise_search import search, search_call, setup, wait_for  # noqa: F401


def unanswered(messages: list[dict]) -> list[str]:
    """Tool call ids left without an answer before the next message, which chat completions refuses."""
    missing = []
    for i, m in enumerate(messages):
        later = messages[i + 1:]
        until = next((k for k, n in enumerate(later) if n.get("role") != "tool"), len(later))
        missing += [c["id"] for c in m.get("tool_calls") or []
                    if not any(n.get("tool_call_id") == c["id"] for n in later[:until])]
    return missing


def strict(fake):
    """The fake API, refusing an unanswered tool call as OpenAI's chat completions does."""
    def handler(request):
        if not request.url.path.endswith("/models"):
            missing = unanswered(json.loads(request.content)["messages"])
            if missing:
                return httpx.Response(400, json={"error": {"message": f"tool_call_ids without a response: {missing}"}})
        return fake.handler(request)
    return handler


def strict_client(engine, fake, monkeypatch):
    http = httpx.AsyncClient(transport=httpx.MockTransport(strict(fake)))
    monkeypatch.setattr(engine, "_client", lambda prov, model="": LLMClient(
        prov.base_url, creds.get_secret("provider", prov.name), http))


async def test_stop_while_a_search_awaits_approval_leaves_a_valid_conversation(search, monkeypatch):  # noqa: F811
    engine, fake, _, _ = search
    strict_client(engine, fake, monkeypatch)
    setup(engine)
    fake.responses.append(search_call(text="Let me check advisories."))
    engine.send("vpn drops")
    await wait_for(lambda: engine._search_reqs)
    engine.stop_turn()
    await wait_turn(engine)
    assert unanswered(engine.conv) == []
    assert sum("Let me check advisories." in (m.get("content") or "") for m in engine.conv) == 1
    fake.responses.append(sse(({"role": "assistant", "content": "Then check the certificate."}, "stop")))
    engine.send("forget the search; check the certificate")
    await wait_turn(engine)
    assert engine._last_turn_error is None
    assert engine.chat[-1]["text"] == "Then check the certificate."


async def test_a_failed_tool_call_is_answered_so_the_next_message_goes_through(search, monkeypatch):  # noqa: F811
    engine, fake, _, _ = search
    strict_client(engine, fake, monkeypatch)
    setup(engine)

    async def broken(call, entry, turn):
        raise RuntimeError("search backend exploded")
    monkeypatch.setattr(engine, "_web_search", broken)
    fake.responses.append(search_call())
    engine.send("vpn drops")
    await wait_turn(engine)
    assert "search backend exploded" in engine._last_turn_error
    assert unanswered(engine.conv) == []
    fake.responses.append(sse(({"role": "assistant", "content": "The logs will show it."}, "stop")))
    engine.send("never mind the search")
    await wait_turn(engine)
    assert engine._last_turn_error is None


async def test_stop_mid_reply_records_only_that_rounds_words(env, monkeypatch):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    started = asyncio.Event()

    class Hanging(httpx.AsyncByteStream):
        async def __aiter__(self):
            chunk = {"id": "c2", "object": "chat.completion.chunk", "created": 0, "model": "m",
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Second round, partly"},
                                  "finish_reason": None}]}
            yield f"data: {json.dumps(chunk)}\n\n".encode()
            started.set()
            await asyncio.sleep(3600)
            yield b""

    # round one: a malformed call, which the model is asked to make again
    first = multi_tool_stream([("propose_commands", {"items": "df -h"})], text="First round.")
    rounds = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return fake.handler(request)
        rounds.append(request)
        if len(rounds) == 1:
            return httpx.Response(200, text=first, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, stream=Hanging(), headers={"content-type": "text/event-stream"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(engine, "_client", lambda prov, model="": LLMClient(prov.base_url, "sk-test", http))
    engine.send("disk full")
    await asyncio.wait_for(started.wait(), 5)
    engine.stop_turn()
    await wait_turn(engine)
    said = [m.get("content") for m in engine.conv if m["role"] == "assistant"]
    assert said == ["First round.", "Second round, partly\n[response stopped by technician]"]
    assert unanswered(engine.conv) == []


@pytest.mark.parametrize("name,args", [
    ("revise_queue", ["#1"]), ("revise_queue", {"order": 3, "withdraw": 2}), ("run_recipe", ["disk-linux"]),
    ("web_search", ["opnsense"]), ("research", "opnsense"), ("propose_commands", "df -h"),
    ("ask_technician", [1]), ("update_hypotheses", "dns")])
async def test_tool_arguments_of_the_wrong_shape_get_an_answer_not_a_crash(env, name, args):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    fake.responses += [multi_tool_stream([(name, args)], text="Working on it."),
                       sse(({"role": "assistant", "content": "Done."}, "stop"))]
    engine.send("go")
    await wait_turn(engine)
    assert engine._last_turn_error is None
    assert unanswered(engine.conv) == []
    reply = next(m["content"] for m in engine.conv if m["role"] == "tool")
    assert "nvalid" in reply or "Nothing changed" in reply or "failed" in reply


def test_arguments_must_be_a_json_object():
    assert ToolCall("c", "web_search", '{"query": "x"}').parsed() == {"query": "x"}
    assert ToolCall("c", "web_search", "").parsed() == {}
    for raw in ('"x"', "[1]", "3", "null"):
        with pytest.raises(ValueError):
            ToolCall("c", "web_search", raw).parsed()


def test_a_saved_conversation_with_an_unanswered_call_is_repaired_for_chat_completions():
    call = lambda i: {"id": i, "type": "function", "function": {"name": "web_search", "arguments": "{}"}}  # noqa: E731
    conv = [{"role": "user", "content": "vpn drops"},
            {"role": "assistant", "content": "Let me check.", "tool_calls": [call("c0"), call("c1")]},
            {"role": "tool", "tool_call_id": "c1", "content": "results"},
            {"role": "assistant", "content": "Let me check.\n[response stopped by technician]"},
            {"role": "user", "content": "check the cert"},
            {"role": "assistant", "content": None, "tool_calls": [call("c2")], "native": {"api": "anthropic"}}]
    out = openai_messages(conv)
    assert unanswered(out) == []
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool", "assistant", "user", "assistant", "tool"]
    assert out[3] == {"role": "tool", "tool_call_id": "c0", "content": NO_RESULT}
    assert "native" not in out[6]
    assert openai_messages(conv[:1]) == conv[:1]
