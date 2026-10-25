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
    assert first["output_config"] == {"effort": "low"} and "temperature" not in first
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
