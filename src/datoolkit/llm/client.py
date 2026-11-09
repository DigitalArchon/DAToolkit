"""OpenAI-compatible streaming client (NanoGPT, local servers, any compatible endpoint)."""

from __future__ import annotations

import codecs
import hashlib
import ipaddress
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import urlparse

from openai import APIError, AsyncOpenAI, DefaultAsyncHttpxClient
from openai.types.chat import ChatCompletionChunk

from . import netguard

# standard: plaintext to a cloud provider. tee: runs in an enclave, but the prompt passes the
# provider's gateway in the clear. e2ee: sealed on this machine to an attested enclave's key
# (NanoGPT Private Mode, llm/private_mode.py). local: your own hardware.
TIERS = ("standard", "tee", "e2ee", "local")

# Case sensitivity -> model tiers it permits
SENSITIVITY_TIERS = {
    "open": {"standard", "tee", "e2ee", "local"},
    "confidential": {"e2ee", "local"},
    "sovereign": {"local"},
}


def is_private_mode(model_id: str) -> bool:
    """NanoGPT Private Mode: only ever sent sealed (llm/private_mode.py). However it's written
    ("Private/…", a space before it): when in doubt, sealed."""
    return str(model_id or "").strip().lower().startswith("private/")


def is_local_url(base_url: str) -> bool:
    """Whether a base URL counts as the Local tier (allowed for Confidential and Sovereign).
    UNDER REVIEW: this trusts the LAN. Plain http to a private IP, or a `.local` name that DNS
    could point anywhere, qualifies, so client data and the key may cross the LAN unencrypted
    (never the internet: llm/netguard.py refuses plain http to anything else, and a `.lan` name
    is reached over https only). Accepted for now (an Ollama server on a trusted LAN); see
    MANUAL.md, Model tiers."""
    if base_url.startswith("training://"):
        return True  # scripted training provider: nothing leaves the machine
    host = (urlparse(base_url).hostname or "").lower()
    if host in ("localhost", "") or host.endswith(".local") or host.endswith(".lan"):
        return True
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    # the networks netguard allows plain http to, not Python's is_private: that also counts
    # public addresses (6to4 2002::/16, Teredo 2001::/32) and reserved ranges as private
    return netguard.is_local(host)


def detect_tier(model_id: str, base_url: str, overrides: dict[str, str] | None = None) -> str:
    # e2ee is never taken from an override: only a private/ model is sealed (Engine.save_provider)
    if overrides and model_id in overrides and overrides[model_id] in ("standard", "tee", "local"):
        return overrides[model_id]
    if is_local_url(base_url):
        return "local"
    if is_private_mode(model_id):
        return "e2ee"
    parts = model_id.strip().lower().split("/")
    if "tee" in parts or parts[0] == "phala" or model_id.lower().endswith(("-tee", ":tee")):
        return "tee"
    return "standard"


# The fields of a chat completions message. A message in the conversation may carry more for
# another API (an Anthropic turn's own content blocks, "native"; the state it was answered
# under, "state_before": llm/anthropic_api.py), which an OpenAI-compatible provider never sees.
OPENAI_MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")
# The reply sent for a tool call the conversation never answered (a case saved before the
# engine answered every call): a request with an unanswered call is refused.
NO_RESULT = "(no result: the turn ended before this tool call was answered)"


def openai_messages(messages: list[dict]) -> list[dict]:
    """`messages` as chat completions takes them: anything another API keeps on them left out,
    and every tool call answered before the next message (as anthropic_api.render does)."""
    out: list[dict] = []
    pending: list[str] = []          # tool call ids of the last assistant message not yet answered
    for m in messages:
        role = m.get("role")
        if role == "tool":
            if m.get("tool_call_id") in pending:
                pending.remove(m.get("tool_call_id"))
        elif pending:
            out += [{"role": "tool", "tool_call_id": i, "content": NO_RESULT} for i in pending]
            pending = []
        out.append(m if all(k in OPENAI_MESSAGE_KEYS for k in m) else {k: v for k, v in m.items() if k in OPENAI_MESSAGE_KEYS})
        if role == "assistant":
            pending = [c.get("id") for c in m.get("tool_calls") or []]
    return out + [{"role": "tool", "tool_call_id": i, "content": NO_RESULT} for i in pending]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text as streamed

    def parsed(self) -> dict:
        """The arguments, a JSON object. ValueError for anything else (a string or a list is
        valid JSON too): each caller answers the model that its call was malformed."""
        value = json.loads(self.arguments or "{}")
        if not isinstance(value, dict):
            raise ValueError("the arguments must be a JSON object")
        return value


@dataclass
class TurnResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict | None = None
    id: str | None = None  # the completion's id: a TEE model's reply signature is looked up by it
    # A TEE model's stream (llm/tee.py check_reply): the receipt its headers named (its record is
    # looked up by that), the route nano-gpt took, and the sha256 of every byte it carried
    receipt_id: str | None = None
    tee_route: str | None = None
    body_hash: str | None = None
    # Anthropic's Messages API (llm/anthropic_api.py): the turn's own content blocks, kept so they
    # go back unchanged (thinking signatures, search results); the state rendered before it as a
    # turn-scoped system message; its prefix fingerprint; the searches Anthropic ran, the sources
    # it cited, the model that answered when another stepped in, and why a reply was declined
    native: list | None = None
    state_sent: str = ""
    prefix_sha: str = ""
    searches: list = field(default_factory=list)
    citations: list = field(default_factory=list)
    served_by: str = ""
    refusal: dict | None = None
    # the reply's text after Anthropic's last server-side search, when it searched: what it wrote
    # before ("I'll look that up") was done by the search, not left as a promise
    text_after_search: str | None = None


class LLMClient:
    def __init__(self, base_url: str, api_key: str | None, http_client=None):
        # Local servers often need no key, but the SDK insists on a value. The SDK's own HTTP
        # client follows redirects, which would send the request again to wherever a reply
        # points, plain http and other hosts included: netguard checked only the base URL.
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed", max_retries=1, timeout=300,
                                   http_client=http_client or DefaultAsyncHttpxClient(follow_redirects=False))

    async def list_models(self) -> list[str]:
        page = await self._client.models.list()
        return sorted(m.id for m in page.data)

    async def stream(
        self, model: str, messages: list[dict], tools: list[dict] | None = None, params: dict | None = None,
        *, receipts: bool = False,
    ) -> AsyncIterator[tuple[str, object]]:
        """Yield ("text"|"reasoning", str) deltas and ("tool", name) as each tool call starts,
        then ("done", TurnResult). `receipts`: a TEE model's reply, whose signed record names the
        hash of the exact bytes its stream carried (llm/tee.py check_reply): every byte is hashed,
        to the end, and the receipt's headers kept on the TurnResult."""
        if is_private_mode(model):
            raise RuntimeError(f"{model} is a Private Mode model and is only ever sent sealed; nothing was sent")
        kwargs: dict = {"model": model, "messages": openai_messages(messages), "stream": True,
                        "stream_options": {"include_usage": True}}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        kwargs.update(_split_params(params))
        acc = StreamAccumulator()
        if receipts:
            async for event in self._stream_as_received(kwargs, acc):
                yield event
            yield "done", acc.finish()
            return
        stream = await self._client.chat.completions.create(**kwargs)
        try:
            async for chunk in stream:
                for event in acc.feed(chunk):
                    yield event
        finally:
            await stream.close()     # stopped or cancelled: drop the connection, so the provider stops generating
        yield "done", acc.finish()

    async def _stream_as_received(self, kwargs: dict, acc: StreamAccumulator) -> AsyncIterator[tuple[str, str]]:
        """The stream read line by line as it came, every byte (decoded of any transfer encoding)
        hashed to the end, `[DONE]` and after: the SDK's own reader stops at `[DONE]`. Chunks are
        built as the SDK builds them, and an error the stream carries raises as the SDK raises it.
        From SealedLore's providers/openai_compat.py (Nov 2026)."""
        async with self._client.chat.completions.with_streaming_response.create(**kwargs) as response:
            body = hashlib.sha256()
            acc.result.receipt_id = response.headers.get("x-receipt-id") or response.headers.get("x-tee-signature-id")
            acc.result.tee_route = response.headers.get("x-tee-verification-route")
            done = False
            async for line in _lines(response, body):
                if done or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    done = True      # read on (nothing after it is taken), so the hash covers every byte
                    continue
                chunk = json.loads(data)
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    error = chunk["error"]
                    message = error.get("message") if isinstance(error, dict) else None
                    raise APIError(message=message if isinstance(message, str) and message
                                   else "An error occurred during streaming",
                                   request=response.http_response.request, body=error)
                for event in acc.feed(ChatCompletionChunk.construct(**chunk)):
                    yield event
            acc.result.body_hash = "sha256:" + body.hexdigest()

    async def complete(self, model: str, messages: list[dict], params: dict | None = None) -> str:
        if is_private_mode(model):
            raise RuntimeError(f"{model} is a Private Mode model and is only ever sent sealed; nothing was sent")
        resp = await self._client.chat.completions.create(model=model, messages=openai_messages(messages),
                                                          **_split_params(params))
        return resp.choices[0].message.content or ""


async def _lines(response, body) -> AsyncIterator[str]:
    """The stream's lines, each byte also fed to `body` (a hash) as it arrives, decoded of any
    transfer encoding (SealedLore's providers/openai_compat._lines)."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    async for chunk in response.iter_bytes():
        body.update(chunk)
        pending += decoder.decode(chunk)
        *complete, pending = re.split(r"\r\n|\r|\n", pending)
        for line in complete:
            yield line
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending


# provider extensions sent in the request body, whatever the SDK version knows about:
# reasoning_effort, NanoGPT's prompt_caching (Claude cache boundary and lifetime), and the
# route NanoGPT or OpenRouter is asked to follow (llm/routing.py)
_EXTRA_BODY = ("reasoning_effort", "prompt_caching", "provider")


def _split_params(params: dict | None) -> dict:
    """Standard sampling fields as SDK arguments; provider extensions in the extra body."""
    params = dict(params or {})
    extra = {k: params.pop(k) for k in _EXTRA_BODY if params.get(k)}
    for k in _EXTRA_BODY:
        params.pop(k, None)
    if extra:
        params["extra_body"] = extra
    return params


class StreamAccumulator:
    """Turns chat.completion.chunk objects into deltas and a final TurnResult."""

    def __init__(self) -> None:
        self.result = TurnResult()
        self._calls: list[ToolCall] = []           # in the order they began
        self._at: dict[int, ToolCall] = {}         # stream index -> the call it is filling now
        self._pricing: dict | None = None

    def feed(self, chunk) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        if getattr(chunk, "id", None) and not self.result.id:
            self.result.id = chunk.id
        if chunk.usage:
            self.result.usage = chunk.usage.model_dump()
        # NanoGPT's own record of the charge: its usage.cost comes and goes between replies
        # (as SealedLore found), and a routed call's bill is the evidence that the route held
        pricing = (getattr(chunk, "model_extra", None) or {}).get("x_nanogpt_pricing")
        if isinstance(pricing, dict):
            self._pricing = pricing
        if not chunk.choices:
            return events
        choice = chunk.choices[0]
        if choice.finish_reason:
            self.result.finish_reason = choice.finish_reason
        delta = choice.delta
        if delta is None:
            return events
        extra = delta.model_extra or {}
        reasoning = extra.get("reasoning_content") or extra.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            self.result.reasoning += reasoning
            events.append(("reasoning", reasoning))
        if delta.content:
            self.result.content += delta.content
            events.append(("text", delta.content))
        for tc in delta.tool_calls or []:
            call = self._call_for(tc)
            if tc.id:
                call.id = tc.id
            if tc.function:
                if tc.function.name:
                    if not call.name:
                        # arguments stream silently; this tells the UI the model is still working
                        events.append(("tool", tc.function.name))
                    if tc.function.name != call.name:   # some routes send the whole name with every piece
                        call.name += tc.function.name
                if tc.function.arguments:
                    call.arguments += tc.function.arguments
        return events

    def _call_for(self, tc) -> ToolCall:
        """The call a delta continues. A new id at an index that already holds a named call is a
        new call (some routes number every call 0), and a delta with no index continues the last
        call unless it brings an id of its own."""
        index = tc.index if isinstance(tc.index, int) else None
        current = self._at.get(index) if index is not None else (self._calls[-1] if self._calls else None)
        if current is None or (tc.id and current.id and tc.id != current.id and current.name):
            current = ToolCall(id="", name="", arguments="")
            self._calls.append(current)
            if index is not None:
                self._at[index] = current
        return current

    def finish(self) -> TurnResult:
        cost = (self._pricing or {}).get("cost", (self._pricing or {}).get("amount"))
        if self.result.usage is not None and not isinstance(self.result.usage.get("cost"), (int, float)) \
                and isinstance(cost, (int, float)):
            self.result.usage["cost"] = cost
        self.result.tool_calls = list(self._calls)
        for call in self.result.tool_calls:
            if not call.id:          # unique across rounds and turns, as the history needs them
                call.id = f"call_{uuid.uuid4().hex[:16]}"
        return self.result
