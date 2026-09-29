"""OpenAI-compatible streaming client (NanoGPT, local servers, any compatible endpoint)."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import urlparse

from openai import AsyncOpenAI

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
    """NanoGPT Private Mode: only ever sent sealed (llm/private_mode.py)."""
    return model_id.startswith("private/")


def is_local_url(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    if host in ("localhost", "") or host.endswith(".local") or host.endswith(".lan"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


def detect_tier(model_id: str, base_url: str, overrides: dict[str, str] | None = None) -> str:
    if overrides and model_id in overrides and overrides[model_id] in TIERS:
        return overrides[model_id]
    if is_local_url(base_url):
        return "local"
    if is_private_mode(model_id):
        return "e2ee"
    parts = model_id.lower().split("/")
    if "tee" in parts or parts[0] == "phala" or model_id.lower().endswith(("-tee", ":tee")):
        return "tee"
    return "standard"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text as streamed

    def parsed(self) -> dict:
        return json.loads(self.arguments or "{}")


@dataclass
class TurnResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict | None = None


class LLMClient:
    def __init__(self, base_url: str, api_key: str | None, http_client=None):
        # Local servers often need no key, but the SDK insists on a value.
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed", max_retries=1, timeout=300,
                                   http_client=http_client)

    async def list_models(self) -> list[str]:
        page = await self._client.models.list()
        return sorted(m.id for m in page.data)

    async def stream(
        self, model: str, messages: list[dict], tools: list[dict] | None = None
    ) -> AsyncIterator[tuple[str, object]]:
        """Yield ("text"|"reasoning", str) deltas, then ("done", TurnResult)."""
        if is_private_mode(model):
            raise RuntimeError(f"{model} is a Private Mode model and is only ever sent sealed; nothing was sent")
        kwargs: dict = {"model": model, "messages": messages, "stream": True,
                        "stream_options": {"include_usage": True}}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        acc = StreamAccumulator()
        stream = await self._client.chat.completions.create(**kwargs)
        async for chunk in stream:
            for event in acc.feed(chunk):
                yield event
        yield "done", acc.finish()

    async def complete(self, model: str, messages: list[dict]) -> str:
        if is_private_mode(model):
            raise RuntimeError(f"{model} is a Private Mode model and is only ever sent sealed; nothing was sent")
        resp = await self._client.chat.completions.create(model=model, messages=messages)
        return resp.choices[0].message.content or ""


class StreamAccumulator:
    """Turns chat.completion.chunk objects into deltas and a final TurnResult."""

    def __init__(self) -> None:
        self.result = TurnResult()
        self._calls: dict[int, ToolCall] = {}

    def feed(self, chunk) -> list[tuple[str, str]]:
        events: list[tuple[str, str]] = []
        if chunk.usage:
            self.result.usage = chunk.usage.model_dump()
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
            call = self._calls.setdefault(tc.index, ToolCall(id="", name="", arguments=""))
            if tc.id:
                call.id = tc.id
            if tc.function:
                if tc.function.name:
                    call.name += tc.function.name
                if tc.function.arguments:
                    call.arguments += tc.function.arguments
        return events

    def finish(self) -> TurnResult:
        self.result.tool_calls = [self._calls[i] for i in sorted(self._calls)]
        for i, call in enumerate(self.result.tool_calls):
            if not call.id:
                call.id = f"call_{i}"
        return self.result
