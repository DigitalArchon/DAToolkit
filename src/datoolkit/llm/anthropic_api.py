"""Anthropic's Messages API: the official endpoint, and others that copy it.

The engine keeps the conversation in OpenAI's chat format (llm/client.py); this module turns it
into a Messages API request and the reply back into the same stream of events and TurnResult,
so the rest of the app doesn't care which API a provider speaks.

On Anthropic's own endpoint (is_official_anthropic) the request also uses what only it has:
adaptive thinking with its reasoning shown, effort, prompt caching on the system prompt and the
conversation, server-side web search (the native search, Engine.search_status), and the
refusal fallback. An Anthropic-compatible endpoint gets the plain request: messages, tools,
the system prompt, sampling settings.

**The conversation stays append-only.** A thinking block's signature binds it to the model and
to everything sent before it; on the newer models (Claude Opus 5.5, Sonnet 5.5, Fable 5.1 ...)
an edit to that history invalidates every later block. So:

- each assistant turn keeps the content blocks Anthropic sent ("native" on the message) and
  they go back unchanged: thinking with its signature, search results, citations;
- the case's state (sessions, queue, hypotheses), which changes every request, goes after the
  conversation as a turn-scoped system message (clear_at "next_user_message") that is stored
  with the turn that answered it ("state_before") and sent again, verbatim, every time after:
  once a later user message exists it renders nothing and costs nothing, but nothing before a
  thinking block ever changes. Models without those messages get the state as a trailing user
  message, rebuilt each request, as through NanoGPT; they don't check the history;
- a thinking block whose history did change since it was written (a compaction, a removed
  exchange, an edited system prompt) is left out, as the API's own check would: each turn
  records a fingerprint of what came before it (prefix_sha), compared on every request. And the
  request asks the API to drop, not refuse, any block that still fails its check
  (prefix_mismatch_behavior "drop_block").
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import AsyncIterator

import anthropic

from .capabilities import EFFORTS
from .client import ToolCall, TurnResult, is_private_mode
from .params import nearest_effort
from .providers import is_official_anthropic

THINKING_BETA = "thinking-binding-controls-2026-08-01"
CLEAR_AT_BETA = "mid-conversation-system-clear-at-2026-08-21"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MAX_TOKENS = 64000
COMPATIBLE_MAX_TOKENS = 16000
MAX_CONTINUATIONS = 5        # pause_turn: a server-side search loop that paused, resumed this often at most
NO_MESSAGE = "(no message)"
NO_RESULT = "(no result: the turn ended before this tool call was answered)"
NATIVE_SEARCH_MAX = 4        # Anthropic's searches per request (Engine.MAX_SEARCHES_PER_TURN for the gated one)

_THINKING_TYPES = ("thinking", "redacted_thinking")


def _model_is(model: str, *names: str) -> bool:
    """Whether `model` (an id, perhaps behind a proxy's "anthropic/" prefix or with a date) is one
    of `names` ("opus-5-5", "sonnet-5"...)."""
    bare = model.split("/")[-1].lower().replace(".", "-")
    return any(re.fullmatch(rf"claude-{re.escape(n)}(-\d{{8}})?", bare) for n in names)


# Mid-conversation system messages, and their turn-scoped (clear_at) form
CLEAR_AT_MODELS = ("opus-4-8", "opus-5", "opus-5-5", "fable-5", "fable-5-1", "mythos-5", "mythos-5-1", "sonnet-5-5",
                   "haiku-5-5")
# temperature / top_p are refused (or only their defaults accepted)
NO_SAMPLING_MODELS = ("opus-4-7", "opus-4-8", "opus-5", "opus-5-5", "fable-5", "fable-5-1", "mythos-5", "mythos-5-1",
                      "sonnet-5", "sonnet-5-5", "haiku-5-5")
# web search with dynamic filtering; other Claude models take the basic version
DYNAMIC_SEARCH_MODELS = ("opus-4-6", "opus-4-7", "opus-4-8", "opus-5", "opus-5-5", "sonnet-4-6", "sonnet-5", "sonnet-5-5",
                         "fable-5", "fable-5-1")
# the server-side refusal fallback, routed by the refusal's category ("default")
FALLBACK_MODELS = ("opus-5", "opus-5-5", "fable-5-1", "sonnet-5-5")
# adaptive thinking, when the model list didn't say
ADAPTIVE_MODELS = ("opus-4-6", "opus-4-7", "opus-4-8", "opus-5", "opus-5-5", "sonnet-4-6", "sonnet-5", "sonnet-5-5",
                   "haiku-5-5", "fable-5", "fable-5-1", "mythos-5", "mythos-5-1")


def is_claude(model: str) -> bool:
    return "claude" in model.lower()


def state_mode(model: str, official: bool) -> str:
    """How the case's state goes with a request: "clear_at" (a turn-scoped system message, kept
    in the history) or "user" (a trailing user message, rebuilt each time)."""
    return "clear_at" if official and _model_is(model, *CLEAR_AT_MODELS) else "user"


def web_search_tool(model: str, max_uses: int = NATIVE_SEARCH_MAX) -> dict:
    kind = "web_search_20260209" if _model_is(model, *DYNAMIC_SEARCH_MODELS) else "web_search_20250305"
    return {"type": kind, "name": "web_search", "max_uses": max_uses}


def parse_caps(model) -> dict:
    """A model object from GET /v1/models, as capabilities.parse shapes NanoGPT's."""
    caps = getattr(model, "capabilities", None) or {}
    if not isinstance(caps, dict):
        caps = getattr(caps, "model_dump", lambda: {})()

    def ok(*path) -> bool:
        node = caps
        for p in path:
            node = node.get(p) if isinstance(node, dict) else None
        return bool(isinstance(node, dict) and node.get("supported"))
    efforts = [e for e in EFFORTS if ok("effort", e)]
    return {"vision": ok("image_input"), "reasoning": ok("thinking"), "adaptive": ok("thinking", "types", "adaptive"),
            "efforts": efforts if ok("effort") else [], "tools": True,
            "context": getattr(model, "max_input_tokens", None), "max_output": getattr(model, "max_tokens", None)}


def _efforts(model: str, caps: dict | None) -> list[str]:
    if caps is not None and "efforts" in caps:
        return caps["efforts"]
    if _model_is(model, "opus-4-5"):
        return ["low", "medium", "high"]
    if _model_is(model, "opus-4-6", "sonnet-4-6"):
        return ["low", "medium", "high", "max"]
    if _model_is(model, *ADAPTIVE_MODELS):
        return ["low", "medium", "high", "xhigh", "max"]
    return []


def _adaptive(model: str, caps: dict | None) -> bool:
    if caps is not None and "adaptive" in caps:
        return bool(caps["adaptive"])
    return _model_is(model, *ADAPTIVE_MODELS)


def _tool_id(raw: str) -> str:
    """Tool ids as the API takes them (letters, digits, _ and -): one from another provider may
    have others. The same id gives the same result every time, so the history doesn't change."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", raw or "") or "call"


def _image(url: str) -> dict | None:
    m = re.match(r"data:(image/[a-z]+);base64,(.+)$", url or "", re.S)
    if not m:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}


def _user_blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    out = []
    for part in content or []:
        if part.get("type") == "text" and str(part.get("text", "")).strip():
            out.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            img = _image((part.get("image_url") or {}).get("url", ""))
            if img:
                out.append(img)
    return out


def _plain_assistant(m: dict) -> list[dict]:
    blocks = []
    if isinstance(m.get("content"), str) and m["content"].strip():
        blocks.append({"type": "text", "text": m["content"]})
    for c in m.get("tool_calls") or []:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        blocks.append({"type": "tool_use", "id": _tool_id(c.get("id", "")), "name": fn.get("name", ""),
                       "input": args if isinstance(args, dict) else {}})
    return blocks


def _visible(blocks: list[dict]) -> bool:
    return any(b.get("type") not in _THINKING_TYPES for b in blocks)


def _strip(obj):
    """A message or block as the history check sees it: without thinking blocks (they aren't part
    of the prefix) or cache breakpoints (which may move freely)."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_strip(x) for x in obj if not (isinstance(x, dict) and x.get("type") in _THINKING_TYPES)]
    return obj


def _sha(system: str, tools: list[dict], msgs: list[dict]) -> str:
    tools = sorted(_strip(tools), key=lambda t: t.get("name", ""))     # bound as a name-sorted set
    data = json.dumps([system, tools, _strip(msgs)], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def native_blocks(m: dict) -> list[dict] | None:
    native = m.get("native")
    if isinstance(native, dict) and native.get("api") == "anthropic" and isinstance(native.get("content"), list):
        return native["content"]
    return None


def after_fallback(blocks: list[dict]) -> list[dict]:
    """A reply in which another model took over (the refusal fallback) goes back without what the
    declined model left before the last switch point: its thinking, its tool calls, a search
    with no result, and anything else that isn't text or a paired search."""
    last = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    if last < 0:
        return blocks
    answered = {b.get("tool_use_id") for b in blocks if b.get("type", "").endswith("_tool_result")}
    kept = []
    for b in blocks[:last]:
        t = b.get("type")
        if t == "text" or (t == "server_tool_use" and b.get("id") in answered) or t.endswith("_tool_result"):
            kept.append(b)
    return kept + blocks[last:]


def render(messages: list[dict], model: str, official: bool, tools: list[dict], cache_ttl: str = ""):
    """(system, messages, state sent as a turn-scoped message or "", prefix fingerprint) for a
    Messages API request. `messages` are the engine's: a system prompt first, then the
    conversation, perhaps ending with the case's state (a message marked "_state")."""
    mode = state_mode(model, official)
    system_parts: list[str] = []
    out: list[dict] = []
    pending: list[str] = []           # tool_use ids of the last assistant turn not yet answered
    asked: set[str] = set()           # every tool_use id of the last assistant turn
    state_sent = ""

    def user() -> dict:
        if not out or out[-1]["role"] != "user":
            out.append({"role": "user", "_results": [], "_rest": []})
        return out[-1]

    def answer_pending() -> None:
        if pending:
            u = user()
            u["_results"] += [{"type": "tool_result", "tool_use_id": i, "content": NO_RESULT} for i in pending]
            pending.clear()

    sys_text = lambda: "\n\n".join(system_parts)                     # noqa: E731
    for i, m in enumerate(messages):
        role = m.get("role")
        content = m.get("content")
        if role == "system" and m.get("_state"):
            last = i == len(messages) - 1
            answer_pending()
            if mode == "clear_at" and last and out and out[-1]["role"] == "user":
                state_sent = str(content)
                out.append({"role": "system", "content": state_sent, "clear_at": "next_user_message"})
            else:
                user()["_rest"].append({"type": "text", "text": str(content)})
        elif role == "system":
            if not out:
                system_parts.append(str(content or ""))
            else:
                user()["_rest"].append({"type": "text", "text": str(content or "")})
        elif role == "tool":
            tid = _tool_id(m.get("tool_call_id", ""))
            text = content if isinstance(content, str) else json.dumps(content)
            if tid in asked:
                user()["_results"].append({"type": "tool_result", "tool_use_id": tid, "content": text or "(empty)"})
                if tid in pending:
                    pending.remove(tid)
            else:                # its call is no longer in the history (compacted or removed): as text
                user()["_rest"].append({"type": "text", "text": f"[Result of an earlier tool call]\n{text}"})
        elif role == "user":
            blocks = _user_blocks(content)
            user()["_rest"] += blocks or [{"type": "text", "text": "(empty message)"}]
        elif role == "assistant":
            answer_pending()
            if not out:
                out.append({"role": "user", "_results": [], "_rest": [{"type": "text", "text": "(conversation start)"}]})
            if mode == "clear_at" and m.get("state_before") and out[-1]["role"] == "user":
                out.append({"role": "system", "content": m["state_before"], "clear_at": "next_user_message"})
            native = native_blocks(m)
            blocks = [dict(b) for b in native] if native is not None else _plain_assistant(m)
            if native is not None and mode == "clear_at" and (m.get("native") or {}).get("prefix_sha"):
                if m["native"]["prefix_sha"] != _sha(sys_text(), tools, _finished(out)):
                    blocks = [b for b in blocks if b.get("type") not in _THINKING_TYPES]
            if not _visible(blocks):
                blocks.append({"type": "text", "text": NO_MESSAGE})
            out.append({"role": "assistant", "content": blocks})
            pending = [b["id"] for b in blocks if b.get("type") == "tool_use"]
            asked = set(pending)
    answer_pending()
    msgs = _finished(out)
    if cache_ttl and msgs:
        _breakpoint(msgs, cache_ttl)
    return sys_text(), msgs, state_sent, _sha(sys_text(), tools, msgs)


def _finished(out: list[dict]) -> list[dict]:
    """User turns with their tool results first, as the API requires."""
    done = []
    for m in out:
        if m["role"] == "user":
            done.append({"role": "user", "content": m["_results"] + m["_rest"]})
        else:
            done.append(m)
    return done


def _breakpoint(msgs: list[dict], ttl: str) -> None:
    """Cache up to the last block of the conversation: the newest user turn's, before any state
    sent in it (a turn-scoped state message takes no breakpoint)."""
    for m in reversed(msgs):
        if m["role"] != "user" or not m["content"]:
            continue
        blocks = m["content"]
        last = len(blocks) - 1
        if m is msgs[-1] and len(blocks) > 1 and blocks[-1].get("type") == "text" and _is_state(blocks[-1]):
            last -= 1
        blocks[last] = {**blocks[last], "cache_control": {"type": "ephemeral", "ttl": ttl}}
        return


_STATE_MARK = "[DAToolkit: the current state"


def _is_state(block: dict) -> bool:
    return str(block.get("text", "")).startswith(_STATE_MARK)


def _tools(tools: list[dict] | None) -> list[dict]:
    out = []
    for t in tools or []:
        if t.get("type") == "function":
            fn = t["function"]
            out.append({"name": fn["name"], "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
        else:
            out.append(t)               # an Anthropic tool already (the native web search)
    return out


_STOPS = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length",
          "model_context_window_exceeded": "length", "refusal": "refusal", "pause_turn": "stop"}


def _dump(block) -> dict:
    return block.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(block, "model_dump") else dict(block)


def _usage(u, total: dict) -> dict:
    u = _dump(u) if u is not None else {}
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        total[key] = total.get(key, 0) + int(u.get(key) or 0)
    searches = ((u.get("server_tool_use") or {}).get("web_search_requests")) or 0
    total["web_search_requests"] = total.get("web_search_requests", 0) + int(searches)
    return total


def openai_usage(total: dict) -> dict:
    """Anthropic's usage in the shape the app reads (prompt_tokens counts the cached parts too)."""
    prompt = total.get("input_tokens", 0) + total.get("cache_read_input_tokens", 0) + total.get("cache_creation_input_tokens", 0)
    out = {"prompt_tokens": prompt, "completion_tokens": total.get("output_tokens", 0),
           "total_tokens": prompt + total.get("output_tokens", 0),
           "cache_read_input_tokens": total.get("cache_read_input_tokens", 0),
           "cache_creation_input_tokens": total.get("cache_creation_input_tokens", 0)}
    if total.get("web_search_requests"):
        out["web_search_requests"] = total["web_search_requests"]
    return out


def _search_results(block: dict) -> tuple[list[dict], str]:
    content = block.get("content")
    if isinstance(content, dict):          # an error is an object, results a list
        return [], str(content.get("error_code") or "search failed")
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "date": r.get("page_age") or ""}
            for r in content or [] if isinstance(r, dict)], ""


class AnthropicClient:
    """The same interface as LLMClient (list_models, stream, complete) over the Messages API."""

    def __init__(self, base_url: str, api_key: str | None, caps: dict[str, dict] | None = None, http_client=None):
        self.official = is_official_anthropic(base_url)
        # the SDK adds /v1/messages itself
        base = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
        self._client = anthropic.AsyncAnthropic(api_key=api_key or "not-needed", base_url=base, max_retries=1,
                                                timeout=600, http_client=http_client)
        self._caps = caps or {}

    async def list_models(self) -> list[str]:
        return (await self.list_models_detailed())[0]

    async def list_models_detailed(self) -> tuple[list[str], dict[str, dict]]:
        ids, caps = [], {}
        async for m in self._client.models.list(limit=1000):
            ids.append(m.id)
            if self.official:
                caps[m.id] = parse_caps(m)
        return sorted(ids), caps

    def request(self, model: str, messages: list[dict], tools: list[dict] | None = None,
                params: dict | None = None) -> tuple[dict, dict, dict]:
        """(the SDK's arguments, extra body, info: state sent and prefix fingerprint)."""
        params = dict(params or {})
        caps = self._caps.get(model)
        official = self.official
        a_tools = _tools(tools)
        if params.get("native_search") and official and is_claude(model):
            a_tools.append(web_search_tool(model, int(params["native_search"])))
        ttl = params.get("cache_ttl") if (official or is_claude(model)) else ""
        system, msgs, state_sent, prefix_sha = render(messages, model, official, a_tools, ttl or "")
        # Anthropic's models write up to 64k and more; a compatible endpoint's may stop far short
        default = min(DEFAULT_MAX_TOKENS, (caps or {}).get("max_output") or DEFAULT_MAX_TOKENS) if official else COMPATIBLE_MAX_TOKENS
        kwargs: dict = {"model": model, "messages": msgs, "max_tokens": int(params.get("max_tokens") or default)}
        if system:
            block = {"type": "text", "text": system}
            if ttl:
                block["cache_control"] = {"type": "ephemeral", "ttl": ttl}
            kwargs["system"] = [block]
        if a_tools:
            kwargs["tools"] = a_tools
            kwargs["tool_choice"] = {"type": "none" if params.get("tool_choice") == "none" else "auto"}
        extra: dict = {}
        betas: set[str] = set()
        thinking = official and _adaptive(model, caps)
        if thinking:
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized",
                                  "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
            betas.add(THINKING_BETA)
        effort = params.get("reasoning_effort")
        levels = _efforts(model, caps) if official else []
        if effort and levels:
            extra["output_config"] = {"effort": nearest_effort("low" if effort in ("none", "minimal") else effort, levels)}
        if not thinking and not _model_is(model, *NO_SAMPLING_MODELS):
            for key in ("temperature", "top_p"):     # no longer arguments of the SDK's stream(): sent as they are
                if params.get(key) is not None:
                    extra[key] = params[key]
        if official and _model_is(model, *FALLBACK_MODELS):
            extra["fallbacks"] = "default"
            betas.add(FALLBACK_BETA)
        if state_sent:
            betas.add(CLEAR_AT_BETA)
        headers = {"anthropic-beta": ",".join(sorted(betas))} if betas else {}
        return kwargs, {"extra_body": extra, "extra_headers": headers}, {"state_sent": state_sent, "prefix_sha": prefix_sha}

    async def stream(self, model: str, messages: list[dict], tools: list[dict] | None = None,
                     params: dict | None = None) -> AsyncIterator[tuple[str, object]]:
        """Yield ("text"|"reasoning", str), ("tool", name) as a tool call starts, ("search", record)
        as Anthropic's own search runs and returns, then ("done", TurnResult)."""
        if is_private_mode(model):
            raise RuntimeError(f"{model} is a Private Mode model and is only ever sent sealed; nothing was sent")
        kwargs, extra, info = self.request(model, messages, tools, params)
        result = TurnResult(state_sent=info["state_sent"], prefix_sha=info["prefix_sha"])
        blocks_all: list[dict] = []
        searches: dict[str, dict] = {}
        usage: dict = {}
        stop = None
        msg = None
        last_kind = ""
        for _ in range(MAX_CONTINUATIONS + 1):
            async with self._client.messages.stream(**kwargs, **extra) as s:
                async for ev in s:
                    if ev.type == "content_block_start":
                        b = ev.content_block
                        if b.type in ("tool_use", "server_tool_use"):
                            yield "tool", b.name
                        if b.type == "text" and last_kind and last_kind != "text" and result.content:
                            result.content += "\n\n"
                            yield "text", "\n\n"
                        last_kind = b.type
                    elif ev.type == "content_block_delta":
                        d = ev.delta
                        if d.type == "text_delta":
                            result.content += d.text
                            yield "text", d.text
                        elif d.type == "thinking_delta" and d.thinking:
                            result.reasoning += d.thinking
                            yield "reasoning", d.thinking
                    elif ev.type == "content_block_stop":
                        snap = s.current_message_snapshot.content
                        if ev.index >= len(snap):
                            continue
                        b = _dump(snap[ev.index])
                        if b.get("type") == "server_tool_use" and b.get("name") == "web_search":
                            rec = searches.setdefault(b["id"], {"id": b["id"], "results": []})
                            rec.update(query=str((b.get("input") or {}).get("query", "")), status="running")
                            yield "search", dict(rec)
                        elif b.get("type") == "web_search_tool_result":
                            rec = searches.setdefault(b.get("tool_use_id", ""), {"id": b.get("tool_use_id", ""),
                                                                                 "query": "", "results": []})
                            found, error = _search_results(b)
                            rec.update(results=found, status="failed" if error else "done",
                                       **({"error": error} if error else {}))
                            yield "search", dict(rec)
                msg = await s.get_final_message()
            blocks = [_dump(b) for b in msg.content]
            blocks_all += blocks
            _usage(msg.usage, usage)
            stop = msg.stop_reason
            if stop != "pause_turn":
                break
            # a server-side search loop paused: send the turn back as it is, and the API carries on
            if kwargs["messages"][-1]["role"] == "assistant":
                kwargs["messages"] = kwargs["messages"][:-1]
            kwargs["messages"] = kwargs["messages"] + [{"role": "assistant", "content": list(blocks_all)}]
        native = after_fallback(blocks_all)
        result.native = native
        result.id = msg.id if msg else None
        result.finish_reason = _STOPS.get(stop or "", stop)
        result.usage = openai_usage(usage)
        result.searches = list(searches.values())
        seen = set()
        for b in native:
            for c in b.get("citations") or [] if b.get("type") == "text" else []:
                if c.get("url") and c["url"] not in seen:
                    seen.add(c["url"])
                    result.citations.append({"url": c["url"], "title": c.get("title") or c["url"]})
        if msg is not None and msg.model and msg.model != model:
            result.served_by = msg.model
        if stop == "refusal":
            details = getattr(msg, "stop_details", None)
            details = _dump(details) if details is not None else {}
            result.refusal = {"category": details.get("category"), "explanation": details.get("explanation") or ""}
            return_calls: list[dict] = []        # never run the tools of a declined turn
        else:
            return_calls = [b for b in native if b.get("type") == "tool_use"]
            if stop == "max_tokens" and return_calls:
                return_calls = return_calls[:-1]  # the last one was cut off mid-input
        result.tool_calls = [ToolCall(id=b["id"], name=b["name"], arguments=json.dumps(b.get("input") or {}))
                             for b in return_calls]
        yield "done", result

    async def complete(self, model: str, messages: list[dict], params: dict | None = None) -> str:
        text = ""
        async for kind, val in self.stream(model, messages, params=params):
            if kind == "text":
                text += val
        return text
