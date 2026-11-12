"""Anthropic's Messages API (llm/anthropic_api.py) against a fake endpoint, through the Engine:
the request shape, the append-only history (state as turn-scoped system messages, thinking
blocks sent back unchanged, and left out once what came before them changed), Anthropic's own
web search, refusals, paused turns, and Anthropic-compatible endpoints."""

import asyncio
import json

import anthropic
import httpx2
import pytest

from datoolkit import creds
from datoolkit.config import Config, Provider
from datoolkit.engine import Engine, UserError
from datoolkit.llm import anthropic_api, prompts

OFFICIAL = "https://api.anthropic.com"
COMPAT = "https://api.moonshot.example/anthropic"
OPUS = "claude-opus-5-5"


def _caps(vision=True, efforts=("low", "medium", "high", "xhigh", "max")):
    effort = {"supported": True, **{e: {"supported": e in efforts} for e in ("low", "medium", "high", "xhigh", "max")}}
    return {"image_input": {"supported": vision}, "thinking": {"supported": True, "types": {"adaptive": {"supported": True}}},
            "effort": effort}


def _sse(blocks: list[dict], stop="end_turn", model=OPUS, usage=None, stop_details=None) -> bytes:
    """A streamed Messages API reply made of `blocks` (text, thinking, tool_use, server_tool_use,
    web_search_tool_result)."""
    out = []

    def ev(kind, data):
        out.append(f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n")
    ev("message_start", {"message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
                                     "stop_reason": None, "usage": {"input_tokens": 100, "output_tokens": 0}}})
    for i, b in enumerate(blocks):
        t = b["type"]
        if t == "text":
            ev("content_block_start", {"index": i, "content_block": {"type": "text", "text": ""}})
            ev("content_block_delta", {"index": i, "delta": {"type": "text_delta", "text": b["text"]}})
            for c in b.get("citations", []):
                ev("content_block_delta", {"index": i, "delta": {"type": "citations_delta", "citation": c}})
        elif t == "thinking":
            ev("content_block_start", {"index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            ev("content_block_delta", {"index": i, "delta": {"type": "thinking_delta", "thinking": b["thinking"]}})
            ev("content_block_delta", {"index": i, "delta": {"type": "signature_delta", "signature": b["signature"]}})
        elif t in ("tool_use", "server_tool_use"):
            ev("content_block_start", {"index": i, "content_block": {"type": t, "id": b["id"], "name": b["name"], "input": {}}})
            ev("content_block_delta", {"index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
        else:
            ev("content_block_start", {"index": i, "content_block": b})
        ev("content_block_stop", {"index": i})
    ev("message_delta", {"delta": {"stop_reason": stop, **({"stop_details": stop_details} if stop_details else {})},
                         "usage": usage or {"output_tokens": 20, "cache_read_input_tokens": 50}})
    ev("message_stop", {})
    return "".join(out).encode()


class FakeAnthropic:
    def __init__(self):
        self.replies: list[bytes] = []
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.models = [{"id": OPUS, "type": "model", "display_name": "Claude Opus 5.5", "created_at": "2026-09-01T00:00:00Z",
                        "max_input_tokens": 1_000_000, "max_tokens": 128_000, "capabilities": _caps()},
                       {"id": "claude-haiku-4-5", "type": "model", "display_name": "Claude Haiku 4.5",
                        "created_at": "2025-10-01T00:00:00Z", "max_input_tokens": 200_000, "max_tokens": 64_000,
                        "capabilities": {"image_input": {"supported": True}, "thinking": {"supported": False},
                                         "effort": {"supported": False}}}]
        self.list_fails = False

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/v1/models"):
            if self.list_fails:
                return httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": "no"}})
            return httpx2.Response(200, json={"data": self.models, "has_more": False, "first_id": None, "last_id": None})
        self.requests.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=self.replies.pop(0))


@pytest.fixture
async def aenv(tmp_path):
    fake = FakeAnthropic()
    cfg = Config(providers=[Provider("Anthropic", OFFICIAL), Provider("Compat", COMPAT, api="anthropic")])
    engine = Engine(cfg, lambda e: None, tmp_path / "rt", save_config=lambda c: None)
    engine._anthropic_http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(fake.handler))
    creds.set_secret("provider", "Anthropic", "sk-ant")
    creds.set_secret("provider", "Compat", "sk-compat")
    await engine.start()
    yield engine, fake
    await engine.stop()


async def wait_turn(engine):
    for _ in range(300):
        if not engine.busy:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("turn did not finish")


def propose(sid, command="df -h", tid="toolu_1"):
    return {"type": "tool_use", "id": tid, "name": "propose_commands",
            "input": {"items": [{"session_id": sid, "command": command, "purpose": "disk", "risk": "read_only"}]}}


async def start(engine, sensitivity="open"):
    await engine.list_models("Anthropic")
    engine.new_case("disk", sensitivity)
    engine.select_model("Anthropic", OPUS)
    engine.open_session("local")
    return engine.sessions.roster()[0]["id"]


# ---------------------------------------------------------------- models and request shape

async def test_models_and_capabilities_come_from_anthropics_list(aenv):
    engine, fake = aenv
    models = {m["id"]: m for m in await engine.list_models("Anthropic")}
    assert models[OPUS]["vision"] is True and models[OPUS]["reasoning"] is True and models[OPUS]["context"] == 1_000_000
    assert models[OPUS]["tier"] == "standard" and models[OPUS]["routable"] is False
    assert engine.cfg.provider("Anthropic").default_model == OPUS
    # a retired model stays in Anthropic's list as a record, but every request naming it fails
    fake.models.append({"id": "claude-2-1", "type": "model", "display_name": "Claude 2.1", "created_at": "2023-11-21T00:00:00Z",
                        "lifecycle": "retired", "retires_at": "2025-07-21T00:00:00Z"})
    assert "claude-2-1" not in {m["id"] for m in await engine.list_models("Anthropic", refresh=True)}
    view = engine.snapshot()["config"]["providers"][0]
    assert view["kind"] == "anthropic" and view["api_effective"] == "anthropic"


async def test_a_turn_is_sent_append_only_and_thinking_goes_back_unchanged(aenv):
    engine, fake = aenv
    sid = await start(engine)
    fake.replies.append(_sse([{"type": "thinking", "thinking": "Disk first.", "signature": "SIG1"},
                              {"type": "text", "text": "Checking disk space."}, propose(sid)], stop="tool_use"))
    engine.send("server is slow")
    await wait_turn(engine)
    entry = engine.chat[-1]
    assert entry["text"] == "Checking disk space." and entry["reasoning"] == "Disk first." and entry["proposals"] == [1]

    first = fake.requests[0]
    assert first["model"] == OPUS and first["thinking"]["type"] == "adaptive"
    assert first["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert first["output_config"] == {"effort": engine.cfg.settings.generation["reasoning_effort"]}
    assert "temperature" not in first
    assert first["fallbacks"] == "default"
    assert first["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert prompts.STATE_HEADER not in first["system"][0]["text"]
    state = first["messages"][-1]
    assert state["role"] == "system" and state["clear_at"] == "next_user_message"
    assert state["content"].startswith(prompts.STATE_HEADER)
    assert first["messages"][-2]["content"][-1]["cache_control"]["ttl"] == "1h"     # the conversation is cached
    betas = fake.headers[0]["anthropic-beta"].split(",")
    assert {anthropic_api.THINKING_BETA, anthropic_api.CLEAR_AT_BETA, anthropic_api.FALLBACK_BETA} <= set(betas)
    names = [t.get("name") for t in first["tools"]]
    assert "propose_commands" in names and "web_search" not in names    # no NanoGPT: no search tool at all

    stored = next(m for m in engine.conv if m["role"] == "assistant")
    assert stored["native"]["content"][0] == {"type": "thinking", "thinking": "Disk first.", "signature": "SIG1"}
    assert stored["state_before"] == state["content"]

    engine.queue.update(1, status="ran")
    fake.replies.append(_sse([{"type": "text", "text": "The disk is fine."}]))
    engine.send("", results=[{"num": 1, "text": "/dev/sda1 40% used"}])
    await wait_turn(engine)
    second = fake.requests[1]
    # everything the first request sent is sent again, word for word, before what is new
    strip = anthropic_api._strip
    assert strip(second["messages"][:len(first["messages"])]) == strip(first["messages"])
    assert second["messages"][len(first["messages"]) - 1] == state            # the old state, verbatim
    assert second["messages"][len(first["messages"])]["content"][0]["signature"] == "SIG1"
    results = second["messages"][len(first["messages"]) + 1]["content"]
    assert results[0]["type"] == "tool_result" and results[0]["tool_use_id"] == "toolu_1"
    assert second["messages"][-1]["role"] == "system" and second["messages"][-1]["content"] != state["content"]


async def test_thinking_is_left_out_once_what_came_before_it_changed(aenv):
    engine, fake = aenv
    await start(engine)
    fake.replies.append(_sse([{"type": "thinking", "thinking": "Hmm.", "signature": "SIG1"},
                              {"type": "text", "text": "Hello."}]))
    engine.send("hi")
    await wait_turn(engine)
    fake.replies.append(_sse([{"type": "text", "text": "Still here."}]))
    engine.send("again")
    await wait_turn(engine)
    sent = [b for m in fake.requests[1]["messages"] if m["role"] == "assistant" for b in m["content"]]
    assert {"type": "thinking", "thinking": "Hmm.", "signature": "SIG1"} in sent

    engine.edit_case(engine.case.id, engine.case.name, notes="Site is in Perth.")   # the system prompt changes
    fake.replies.append(_sse([{"type": "text", "text": "Noted."}]))
    engine.send("one more")
    await wait_turn(engine)
    sent = [b for m in fake.requests[2]["messages"] if m["role"] == "assistant" for b in m["content"]]
    assert not any(b["type"] == "thinking" for b in sent)
    assert any(b.get("text") == "Hello." for b in sent)


async def test_native_search_runs_on_anthropics_side_and_is_shown(aenv):
    engine, fake = aenv
    engine.cfg.settings.search_engine = "native"
    await start(engine)
    assert engine.search_status()["native"] is True and engine.search_status()["research"] is False
    fake.replies.append(_sse([
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "opnsense 25.1 release notes"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
            {"type": "web_search_result", "url": "https://docs.opnsense.org/releases.html", "title": "Releases",
             "encrypted_content": "ENC", "page_age": "2026-01-29"}]},
        {"type": "text", "text": "25.1 changed the firewall UI.", "citations": [
            {"type": "web_search_result_location", "url": "https://docs.opnsense.org/releases.html", "title": "Releases",
             "encrypted_index": "EI", "cited_text": "..."}]}],
        usage={"output_tokens": 30, "server_tool_use": {"web_search_requests": 1}}))
    engine.send("what changed in opnsense 25.1?")
    await wait_turn(engine)
    tools = fake.requests[0]["tools"]
    assert {"type": "web_search_20260209", "name": "web_search", "max_uses": 4} in tools
    assert not any(t.get("name") == "research" for t in tools)
    assert "Anthropic runs each of your searches" in fake.requests[0]["system"][0]["text"]
    entry = engine.chat[-1]
    assert entry["searches"][0]["query"] == "opnsense 25.1 release notes" and entry["searches"][0]["status"] == "done"
    assert entry["searches"][0]["results"][0]["url"] == "https://docs.opnsense.org/releases.html"
    assert entry["citations"] == [{"url": "https://docs.opnsense.org/releases.html", "title": "Releases"}]
    assert engine.last_usage["web_search_requests"] == 1
    # the search's blocks go back as Anthropic sent them
    stored = next(m for m in engine.conv if m["role"] == "assistant")["native"]["content"]
    assert stored[1]["content"][0]["encrypted_content"] == "ENC"


async def test_native_search_is_never_used_for_a_confidential_case(aenv):
    engine, fake = aenv
    engine.cfg.settings.search_engine = "native"
    await start(engine)
    engine.edit_case(engine.case.id, engine.case.name, sensitivity="confidential")
    st = engine.search_status()
    assert st["mode"] == "off" and not st.get("native")


async def test_native_search_needs_anthropics_api_not_only_its_address(aenv):
    engine, fake = aenv
    engine.cfg.settings.search_engine = "native"
    await start(engine)
    assert engine.search_status().get("native") is True
    engine.cfg.providers[0].api = "openai"         # api.anthropic.com through its OpenAI-compatible API
    st = engine.search_status()
    assert not st.get("native") and engine._chat_tools()[1] == {}       # no native_search for chat completions


async def test_no_other_model_is_chosen_while_a_turn_runs(aenv):
    engine, fake = aenv
    await start(engine)
    model = engine.cfg.active_model
    engine._turn = asyncio.get_running_loop().create_future()          # a turn still running
    try:
        with pytest.raises(UserError, match="Wait for the AI"):
            engine.select_model("Compat", "claude-sonnet-5-5")
        assert engine.cfg.active_model == model
    finally:
        engine._turn.cancel()
        engine._turn = None


async def test_a_call_cut_off_by_the_context_window_is_not_queued(aenv):
    engine, fake = aenv
    sid = await start(engine)
    fake.replies.append(_sse([{"type": "text", "text": "Checking."}, propose(sid, "df -h")],
                             stop="model_context_window_exceeded"))
    engine.send("disk full")
    await wait_turn(engine)
    assert not engine.queue.items                 # its input may be missing fields the parser let go


async def test_a_whole_call_before_a_cut_off_block_is_asked_for_again_not_queued(aenv):
    # Until Nov 2026 a call that was whole before the reply was cut off was queued. No call of a
    # reply cut off at the output limit is acted on now (the calls after it never came): the model
    # is told so and asked again, and the technician sees that the reply was cut off.
    engine, fake = aenv
    sid = await start(engine)
    fake.replies.append(_sse([{"type": "text", "text": "Checking."}, propose(sid, "df -h"),
                              {"type": "text", "text": "And after that I would"}], stop="max_tokens"))
    fake.replies.append(_sse([{"type": "text", "text": "Again, shorter."}, propose(sid, "df -h", tid="toolu_2")]))
    engine.send("disk full")
    await wait_turn(engine)
    assert [p.command for p in engine.queue.items] == ["df -h"]          # once, from the reply that was whole
    told = [b for m in fake.requests[1]["messages"] if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_result" and b.get("tool_use_id") == "toolu_1"]
    assert "cut off at the output limit" in json.dumps(told) and "Nothing of it was queued" in json.dumps(told)
    assert engine.chat[-1] == {"kind": "note", "text": "The reply was cut off at the output limit."}
    assert engine.chat[-2]["proposals"] == [1] and engine.chat[-2]["cut_off"] == ["length"]


async def test_a_declined_request_runs_no_tools(aenv):
    engine, fake = aenv
    sid = await start(engine)
    fake.replies.append(_sse([{"type": "text", "text": "I"}, propose(sid)], stop="refusal",
                             stop_details={"type": "refusal", "category": "cyber", "explanation": "Policy."}))
    engine.send("scan the network")
    await wait_turn(engine)
    entry = engine.chat[-1]
    assert entry["refusal"]["category"] == "cyber" and not entry["proposals"]
    assert not engine.queue.items and len(fake.requests) == 1


async def test_a_paused_search_turn_is_resumed(aenv):
    engine, fake = aenv
    engine.cfg.settings.search_engine = "native"
    await start(engine)
    fake.replies.append(_sse([{"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "q"}}],
                             stop="pause_turn"))
    fake.replies.append(_sse([{"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": []},
                              {"type": "text", "text": "Nothing found."}]))
    engine.send("look it up")
    await wait_turn(engine)
    assert len(fake.requests) == 2
    resumed = fake.requests[1]["messages"][-1]
    assert resumed["role"] == "assistant" and resumed["content"][0]["type"] == "server_tool_use"
    stored = next(m for m in engine.conv if m["role"] == "assistant")["native"]["content"]
    assert [b["type"] for b in stored] == ["server_tool_use", "web_search_tool_result", "text"]
    assert engine.chat[-1]["text"] == "Nothing found."


async def test_a_compatible_endpoint_gets_the_plain_request(aenv):
    engine, fake = aenv
    fake.list_fails = True
    engine.cfg.provider("Compat").default_model = "kimi-k3"
    assert [m["id"] for m in await engine.list_models("Compat")] == ["kimi-k3"]
    engine.new_case("c", "open")
    engine.select_model("Compat", "kimi-k3")
    fake.replies.append(_sse([{"type": "text", "text": "Hi."}], model="kimi-k3"))
    engine.send("hello")
    await wait_turn(engine)
    assert fake.requests, engine.chat[-1]
    body = fake.requests[0]
    assert "thinking" not in body and "output_config" not in body and "fallbacks" not in body
    assert body["temperature"] == 0.3 and body["max_tokens"] == anthropic_api.COMPATIBLE_MAX_TOKENS
    assert "anthropic-beta" not in fake.headers[0]
    assert fake.headers[0]["x-api-key"] == "sk-compat"
    assert all(m["role"] != "system" for m in body["messages"])          # the state goes as user text
    assert body["messages"][-1]["content"][-1]["text"].startswith(prompts.STATE_HEADER)


# ---------------------------------------------------------------- rendering

def test_render_pairs_tool_results_and_fills_the_gaps():
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": [{"type": "text", "text": "look"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
            {"role": "assistant", "content": "ok", "tool_calls": [
                {"id": "call:1", "type": "function", "function": {"name": "a", "arguments": "{\"x\": 1}"}},
                {"id": "call_2", "type": "function", "function": {"name": "b", "arguments": "not json"}}]},
            {"role": "tool", "tool_call_id": "call:1", "content": "done"},
            {"role": "user", "content": "and?"},
            {"role": "tool", "tool_call_id": "gone", "content": "orphan"},
            {"role": "assistant", "content": ""},
            {"role": "system", "content": "STATE", "_state": True}]
    system, out, state, _ = anthropic_api.render(msgs, "claude-haiku-4-5", True, [])
    assert system == "SYS" and state == ""
    assert out[0]["content"][1]["type"] == "image" and out[0]["content"][1]["source"]["data"] == "AAAA"
    assert out[1]["content"][1] == {"type": "tool_use", "id": "call_1", "name": "a", "input": {"x": 1}}
    assert out[1]["content"][2]["input"] == {}
    results = out[2]["content"]
    assert [b.get("tool_use_id") for b in results[:2]] == ["call_1", "call_2"]     # results first, the missing one filled
    assert results[1]["content"] == anthropic_api.NO_RESULT
    assert results[2]["text"] == "and?" and "orphan" in results[3]["text"]
    assert out[3] == {"role": "assistant", "content": [{"type": "text", "text": anthropic_api.NO_MESSAGE}]}
    assert out[4]["content"][-1]["text"] == "STATE"                    # Haiku 4.5: state as a user message


def test_a_fallback_reply_goes_back_without_the_declined_models_internals():
    blocks = [{"type": "thinking", "thinking": "x", "signature": "s"}, {"type": "text", "text": "Part"},
              {"type": "tool_use", "id": "t", "name": "n", "input": {}},
              {"type": "fallback", "from": {"model": OPUS}, "to": {"model": "claude-opus-4-8"}},
              {"type": "text", "text": "rest"}]
    assert [b["type"] for b in anthropic_api.after_fallback(blocks)] == ["text", "fallback", "text"]


def test_model_families():
    assert anthropic_api.state_mode(OPUS, True) == "clear_at"
    assert anthropic_api.state_mode(OPUS, False) == "user"
    assert anthropic_api.state_mode("claude-sonnet-5", True) == "user"
    assert anthropic_api.web_search_tool("claude-haiku-5-5")["type"] == "web_search_20250305"
    assert anthropic_api.web_search_tool("anthropic/claude-sonnet-4.6")["type"] == "web_search_20260209"


async def test_overrides_are_checked_when_a_provider_is_saved(aenv):
    engine, _ = aenv
    base = {"name": "N", "base_url": "https://nano-gpt.com/api/v1"}
    with pytest.raises(UserError, match="standard, tee or local"):
        engine.save_provider({**base, "tier_overrides": {"zai/glm-5.3": "e2ee"}})
    engine.save_provider({**base, "context_overrides": {"m": "1M", "n": "32k", "o": ""}, "api": "openai",
                          "routes": {"zai/glm-5.3": {"priority": "latency", "fp8": True}}})
    prov = engine.cfg.provider("N")
    assert prov.context_overrides == {"m": 1_000_000, "n": 32_000} and prov.api == ""
    assert prov.routes == {"zai/glm-5.3": {"priority": "latency", "fp8": True}}
    with pytest.raises(UserError, match="Route"):
        engine.save_provider({**base, "routes": {"x": {"priority": "fastest"}}})


def test_the_appimage_lock_carries_the_sdk_in_range():
    import re
    from pathlib import Path
    lock = (Path(__file__).resolve().parents[1] / "packaging" / "appimage" / "requirements.lock").read_text()
    version = tuple(int(x) for x in re.search(r"^anthropic==([\d.]+)", lock, re.M).group(1).split("."))
    assert (1, 13) <= version < (2,)
    assert re.search(r"^docstring-parser==", lock, re.M)


def test_only_append_only_models_are_opted_into_the_history_check():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"},
            {"role": "system", "content": prompts.STATE_HEADER + "\n\nstate", "_state": True}]
    client = anthropic_api.AnthropicClient(OFFICIAL, "k")
    new, extra_new, _ = client.request(OPUS, msgs, params={"reasoning_effort": "low"})
    old, extra_old, _ = client.request("claude-opus-4-6", msgs, params={"reasoning_effort": "low"})
    assert new["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    assert anthropic_api.THINKING_BETA in extra_new["extra_headers"]["anthropic-beta"]
    assert old["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert anthropic_api.THINKING_BETA not in extra_old["extra_headers"].get("anthropic-beta", "")
    assert old["messages"][-1]["role"] == "user"          # its state is a trailing user message


def test_stored_turn_scoped_states_carry_their_beta_even_when_this_requests_state_isnt_one():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello", "state_before": "old state"},
            {"role": "user", "content": "go on"},
            {"role": "system", "content": "state", "_state": True},
            {"role": "user", "content": "now summarise"}]          # compaction: the state isn't last
    kwargs, extra, info = anthropic_api.AnthropicClient(OFFICIAL, "k").request(OPUS, msgs)
    assert info["state_sent"] == "" and kwargs["messages"][1]["clear_at"] == "next_user_message"
    assert anthropic_api.CLEAR_AT_BETA in extra["extra_headers"]["anthropic-beta"]


async def test_saying_it_will_look_something_up_before_a_native_search_is_no_promise(aenv):
    engine, fake = aenv
    engine.cfg.settings.search_engine = "native"
    await start(engine)
    fake.replies.append(_sse([
        {"type": "text", "text": "I'll look up the current release."},
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "opnsense release"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": []},
        {"type": "thinking", "thinking": "Found it.", "signature": "S"},       # live, Haiku 5.5 thinks here
        {"type": "text", "text": "The newest release is 26.7.7."}]))
    engine.send("latest opnsense?")
    await wait_turn(engine)
    assert len(fake.requests) == 1                     # no "you promised commands" nudge
    assert engine.chat[-1]["text"] == "I'll look up the current release.\n\nThe newest release is 26.7.7."


# ---------------------------------------------------------------- the newest models, and ones not yet named

@pytest.mark.parametrize("model", ["claude-fable-5-2", "claude-mythos-5-2", "claude-opus-9"])
async def test_a_model_newer_than_the_tables_gets_the_append_only_request(aenv, model):
    """Claude Fable 5.2 and Mythos 5.2 refuse an edited history for every account, and a model
    this module has never heard of is taken for one as strict: the state as a turn-scoped system
    message kept in place, the history check set to drop, and no sampling settings. None of
    the three is in the fake's model list, so nothing is known of them but the id."""
    engine, fake = aenv
    sid = await start(engine)
    engine.select_model("Anthropic", model)
    assert engine.cfg.settings.generation["temperature"] == 0.3            # a setting these models refuse
    fake.replies.append(_sse([{"type": "thinking", "thinking": "Disk first.", "signature": "SIG1"},
                              {"type": "text", "text": "Checking disk space."}, propose(sid)], stop="tool_use", model=model))
    engine.send("server is slow")
    await wait_turn(engine)
    assert engine.chat[-1]["proposals"] == [1], engine.chat[-1]
    engine.queue.update(1, status="ran")
    fake.replies.append(_sse([{"type": "text", "text": "The disk is fine."}], model=model))
    engine.send("", results=[{"num": 1, "text": "/dev/sda1 40% used"}])
    await wait_turn(engine)
    first, second = fake.requests
    for body, headers in zip((first, second), fake.headers):
        assert body["model"] == model
        assert body["thinking"] == {"type": "adaptive", "display": "summarized",
                                    "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
        assert "temperature" not in body and "top_p" not in body
        betas = set(headers["anthropic-beta"].split(","))
        assert {anthropic_api.THINKING_BETA, anthropic_api.CLEAR_AT_BETA} <= betas
        assert not any(b.get("text", "").startswith(prompts.STATE_HEADER)        # never as text in a user turn
                       for m in body["messages"] if m["role"] == "user" for b in m["content"])
    state = first["messages"][-1]
    assert state["role"] == "system" and state["clear_at"] == "next_user_message"
    assert state["content"].startswith(prompts.STATE_HEADER)
    # the first user turn, byte for byte, cache breakpoints aside (they move with the conversation)
    strip = anthropic_api._strip
    assert json.dumps(strip(second["messages"][0]), sort_keys=True) == json.dumps(strip(first["messages"][0]), sort_keys=True)
    assert second["system"][0]["text"] == first["system"][0]["text"] and second["tools"] == first["tools"]
    # ... and so is everything else the first request sent: its state stays where it was, and
    # the thinking block goes back as it came
    assert strip(second["messages"][:len(first["messages"])]) == strip(first["messages"])
    assert second["messages"][len(first["messages"]) - 1] == state
    assert second["messages"][len(first["messages"])]["content"][0] == {"type": "thinking", "thinking": "Disk first.",
                                                                        "signature": "SIG1"}
    assert second["messages"][-1]["role"] == "system" and second["messages"][-1]["clear_at"] == "next_user_message"
    assert second["messages"][-1]["content"] != state["content"]
    # the refusal fallback only where the reference lists it: not for a model it doesn't name
    assert (first.get("fallbacks") == "default") == (model != "claude-opus-9")


def test_the_tables_name_the_52_models_and_an_unnamed_model_is_a_new_one():
    mode, has, tables = anthropic_api.state_mode, anthropic_api._model_is, anthropic_api
    for new in ("claude-fable-5-2", "claude-mythos-5-2", "anthropic/claude-fable-5.2"):
        assert mode(new, True) == "clear_at"
        for table in (tables.CLEAR_AT_MODELS, tables.NO_SAMPLING_MODELS, tables.DYNAMIC_SEARCH_MODELS,
                      tables.FALLBACK_MODELS, tables.ADAPTIVE_MODELS):
            assert has(new, *table), (new, table)
    # as the reference lists them (Nov 2026): dynamic search on every Fable and Mythos, not Haiku 5.5;
    # the refusal fallback not on Haiku 5.5 (it has none) or Mythos 5 (it declines nothing)
    assert tables.web_search_tool("claude-mythos-5-1")["type"] == "web_search_20260209"
    assert tables.web_search_tool("claude-haiku-5-5")["type"] == "web_search_20250305"
    assert has("claude-mythos-5-1", *tables.FALLBACK_MODELS) and has("claude-fable-5", *tables.FALLBACK_MODELS)
    assert not has("claude-haiku-5-5", *tables.FALLBACK_MODELS) and not has("claude-mythos-5", *tables.FALLBACK_MODELS)
    assert not has("claude-sonnet-5", *tables.CLEAR_AT_MODELS) and has("claude-haiku-5-5", *tables.CLEAR_AT_MODELS)
    # a model no table names: the newest shape on Anthropic's endpoint, the plain one anywhere else
    for unknown in ("claude-opus-9", "claude-fable-6-20270301", "claude-haiku-6-5", "anthropic/claude-sonnet-7.1"):
        assert tables._unnamed(unknown) and mode(unknown, True) == "clear_at" and mode(unknown, False) == "user"
        assert tables._adaptive(unknown, None) and tables._efforts(unknown, None) == ["low", "medium", "high", "xhigh", "max"]
        assert tables.web_search_tool(unknown)["type"] == "web_search_20250305"
    # the models named as older keep the older shape, however their id is written
    for old in ("claude-sonnet-5", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
                "claude-opus-4-5-20251101", "claude-sonnet-4-5", "claude-opus-4-1-20250805", "claude-sonnet-4-20250514",
                "claude-3-7-sonnet-20250219", "claude-3-5-haiku-latest", "claude-3-opus-20240229", "claude-2.1",
                "claude-instant-1.2", "claude-mythos-preview"):
        assert not tables._unnamed(old) and mode(old, True) == "user", old
    assert not tables._unnamed("kimi-k3") and not tables._unnamed("moonshot/kimi-claude-like")


def test_an_unnamed_model_keeps_its_sampling_settings_only_off_anthropics_endpoint():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"},
            {"role": "system", "content": prompts.STATE_HEADER + "\n\nstate", "_state": True}]
    params = {"temperature": 0.3, "top_p": 0.9, "reasoning_effort": "medium"}
    kwargs, extra, _ = anthropic_api.AnthropicClient(OFFICIAL, "k").request("claude-opus-9", msgs, params=params)
    assert "temperature" not in extra["extra_body"] and "top_p" not in extra["extra_body"]
    assert extra["extra_body"]["output_config"] == {"effort": "medium"} and "fallbacks" not in extra["extra_body"]
    # what the model list said of it is believed: one listed as not thinking gets no thinking, and still no sampling
    listed = anthropic_api.AnthropicClient(OFFICIAL, "k", caps={"claude-opus-9": {"adaptive": False, "efforts": []}})
    kwargs, extra, _ = listed.request("claude-opus-9", msgs, params=params)
    assert "thinking" not in kwargs and "temperature" not in extra["extra_body"]
    assert kwargs["messages"][-1]["clear_at"] == "next_user_message"
    kwargs, extra, _ = anthropic_api.AnthropicClient(COMPAT, "k").request("claude-opus-9", msgs, params=params)
    assert extra["extra_body"]["temperature"] == 0.3 and "thinking" not in kwargs and not extra["extra_headers"]
    assert kwargs["messages"][-1]["role"] == "user"
    # a model named as older, on Anthropic's endpoint: as before
    kwargs, extra, _ = anthropic_api.AnthropicClient(OFFICIAL, "k").request("claude-haiku-4-5", msgs, params=params)
    assert extra["extra_body"]["temperature"] == 0.3 and kwargs["messages"][-1]["role"] == "user"


async def test_a_case_that_changes_models_sends_every_turn_back_as_it_was(aenv):
    """Fable 5.1 -> Fable 5.2 -> Opus 5.5 -> Sonnet 5 (no turn-scoped messages) -> Fable 5.2. Every
    block goes back unchanged whoever wrote it (the API drops what the model asked can't read:
    no earlier model reads Fable 5.2's thinking), and what an append-only model is sent never
    differs from what the append-only model before it was sent."""
    engine, fake = aenv
    await start(engine)
    strip = anthropic_api._strip

    async def turn(model, text, signature):
        engine.select_model("Anthropic", model)
        fake.replies.append(_sse([{"type": "thinking", "thinking": "t", "signature": signature},
                                  {"type": "text", "text": f"Answer from {model}."}], model=model))
        engine.send(text)
        await wait_turn(engine)
        assert engine.chat[-1]["text"] == f"Answer from {model}.", engine.chat[-1]
        return fake.requests[-1]

    def signatures(body):
        return [b["signature"] for m in body["messages"] if m["role"] == "assistant" for b in m["content"]
                if b["type"] == "thinking"]

    r1 = await turn("claude-fable-5-1", "one", "SIG-51")
    r2 = await turn("claude-fable-5-2", "two", "SIG-52")
    r3 = await turn(OPUS, "three", "SIG-OPUS")
    assert signatures(r2) == ["SIG-51"] and signatures(r3) == ["SIG-51", "SIG-52"]       # Fable 5.2's too: the API drops it
    for earlier, later in ((r1, r2), (r2, r3)):
        assert later["system"] == earlier["system"] and later["tools"] == earlier["tools"]
        assert strip(later["messages"][:len(earlier["messages"])]) == strip(earlier["messages"])
        assert later["thinking"]["block_binding"] == {"prefix_mismatch_behavior": "drop_block"}
    # Sonnet 5 takes no system message in the conversation: the states leave it, the newest goes
    # as user text, and it isn't opted into a history check its own rendering would fail
    r4 = await turn("claude-sonnet-5", "four", "SIG-SONNET")
    assert all(m["role"] != "system" for m in r4["messages"]) and "block_binding" not in r4["thinking"]
    assert r4["messages"][-1]["content"][-1]["text"].startswith(prompts.STATE_HEADER)
    assert signatures(r4) == ["SIG-51", "SIG-52", "SIG-OPUS"]
    # back on Fable 5.2: the turns it and the others answered are as they were sent then
    r5 = await turn("claude-fable-5-2", "five", "SIG-52b")
    assert strip(r5["messages"][:len(r3["messages"])]) == strip(r3["messages"])
    assert r5["messages"][-1]["clear_at"] == "next_user_message"
    assert signatures(r5)[:3] == ["SIG-51", "SIG-52", "SIG-OPUS"]
    # Sonnet 5's turn was answered under a state sent as user text, which is not in this rendering:
    # what came before its thinking has changed, so that block is left out (text and all else stays)
    assert "SIG-SONNET" not in signatures(r5)
    assert any(b.get("text") == "Answer from claude-sonnet-5." for m in r5["messages"] if m["role"] == "assistant"
               for b in m["content"])


def test_another_vendors_thinking_is_not_sent_to_anthropic():
    """A turn a model of an Anthropic-compatible endpoint wrote carries that vendor's signature;
    Anthropic refuses one it can't read, whatever the history check is set to."""
    def conv(wrote):
        return [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "Hello.", "native": {"api": "anthropic", "model": wrote, "prefix_sha": "", "content": [
                    {"type": "thinking", "thinking": "hm", "signature": "THEIRS"}, {"type": "text", "text": "Hello."}]}},
                {"role": "user", "content": "and?"}]
    _, out, _, _ = anthropic_api.render(conv("kimi-k3"), "claude-fable-5-2", True, [])
    assert out[1]["content"] == [{"type": "text", "text": "Hello."}]
    _, out, _, _ = anthropic_api.render(conv("claude-opus-5-5"), "claude-fable-5-2", True, [])
    assert out[1]["content"][0]["signature"] == "THEIRS"
    _, out, _, _ = anthropic_api.render(conv("kimi-k3"), "kimi-k3", False, [])            # its own endpoint: as it came
    assert out[1]["content"][0]["signature"] == "THEIRS"


# ---------------------------------------------------------------- the history fingerprint

def _whole_sha(system, tools, msgs) -> str:
    """The fingerprint as it was computed until Nov 2026, the whole history written out at once:
    what the turns of every stored case hold."""
    import hashlib

    strip = anthropic_api._strip
    tools = sorted(strip(tools), key=lambda t: t.get("name", ""))
    data = json.dumps([system, tools, strip(msgs)], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8", "surrogatepass")).hexdigest()


def _exchange(i: int, kb: int = 0, sha: str = "") -> list[dict]:
    """One round of a tool loop as the engine stores it: the assistant's turn with Anthropic's own
    blocks, the tool's answer, the technician's results."""
    blocks = [{"type": "thinking", "thinking": "", "signature": f"SIG{i}"}, {"type": "text", "text": "Next."},
              {"type": "tool_use", "id": f"toolu_{i}", "name": "propose_commands", "input": {"items": [{"command": "journalctl -n 200"}]}}]
    return [{"role": "assistant", "content": "Next.", "state_before": f"{prompts.STATE_HEADER}\n\nstate {i}",
             "native": {"api": "anthropic", "model": OPUS, "content": blocks, "prefix_sha": sha},
             "tool_calls": [{"id": f"toolu_{i}", "type": "function", "function": {"name": "propose_commands", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"toolu_{i}", "content": "Queued."},
            {"role": "user", "content": f"[Results returned by the technician] {i} half an emoji \ud83d\n" + "x" * 1024 * kb}]


def test_the_running_fingerprint_is_the_one_stored_cases_hold():
    """Each turn's fingerprint is now read from a hash kept running through the rendering. It
    must be the value the whole-history hash gave, or every stored case would lose its thinking
    blocks on its next request."""
    tools = [{"name": "propose_commands", "description": "d", "input_schema": {"type": "object"}},
             {"name": "ask_technician", "description": "d", "input_schema": {"type": "object"}, "cache_control": {"type": "ephemeral"}}]
    conv = [{"role": "system", "content": "SYSTEM"}, {"role": "system", "content": "more of it"},
            {"role": "user", "content": [{"type": "text", "text": "look"},
                                         {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]

    def state(i):
        return [{"role": "system", "content": f"{prompts.STATE_HEADER}\n\nstate {i}", "_state": True}]

    for i in range(6):
        system, msgs, sent, sha = anthropic_api.render(conv + state(i), OPUS, True, tools, "1h")
        assert sha == _whole_sha(system, tools, msgs) and sent.endswith(f"state {i}")
        conv += _exchange(i, sha=sha)                    # the turn that answered records it, as the engine does
    _, msgs, _, sha = anthropic_api.render(conv + state(6), OPUS, True, tools, "1h")
    signatures = [b["signature"] for m in msgs if m["role"] == "assistant" for b in m["content"] if b["type"] == "thinking"]
    assert signatures == [f"SIG{i}" for i in range(6)]                       # every stored fingerprint still matches
    # ... and one edited message in the middle is still found: thinking from there on is left out
    edited = [dict(m) for m in conv]
    at = [i for i, m in enumerate(edited) if m["role"] == "user"][3]
    edited[at]["content"] = "edited"
    _, msgs, _, _ = anthropic_api.render(edited + state(6), OPUS, True, tools, "1h")
    signatures = [b["signature"] for m in msgs if m["role"] == "assistant" for b in m["content"] if b["type"] == "thinking"]
    assert signatures == ["SIG0", "SIG1", "SIG2"]


def test_the_fingerprint_of_a_long_case_costs_one_pass_not_one_per_turn():
    """The history so far was written out and hashed again at every turn of it: 1.8 s per request,
    on the event loop, at 300 exchanges of 8 KB of output each."""
    import time

    conv = [{"role": "system", "content": "S" * 20000}, {"role": "user", "content": "start"}]
    for i in range(300):
        conv += _exchange(i, kb=8, sha="recorded-under-another-history")
    conv.append({"role": "system", "content": prompts.STATE_HEADER + "\n\nstate", "_state": True})
    tools = [{"name": "propose_commands", "description": "d", "input_schema": {"type": "object"}}]
    took = []
    for _ in range(3):
        started = time.perf_counter()
        _, msgs, _, sha = anthropic_api.render(conv, OPUS, True, tools, "1h")
        took.append(time.perf_counter() - started)
    assert len(msgs) == 902 and len(sha) == 64
    assert min(took) < 0.3, took
