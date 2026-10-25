"""What a model can do: vision, reasoning (and which efforts), tool calls, limits.

NanoGPT reports this in its detailed model list (GET /models?detailed=true). Private Mode
models (private/kimi-k3) are not in that list, so they are matched to their public
counterpart by name (moonshotai/kimi-k3, TEE/kimi-k3). OpenRouter's model list and Anthropic's
(llm/anthropic_api.py) say much the same. Other providers report nothing, so their
capabilities are unknown unless the technician sets an override."""

from __future__ import annotations

import re

import httpx

EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def norm_id(model_id: str) -> str:
    """A model id without its provider prefix, dots and underscores as dashes: NanoGPT names the
    same model glm-5.3 in one list and glm-5-3 in another. A :thinking suffix stays: that is
    another model."""
    return re.sub(r"[._]", "-", model_id.split("/")[-1].lower())


def parse(items: list[dict]) -> dict[str, dict]:
    out = {}
    for m in items:
        caps = m.get("capabilities") or {}
        out[m["id"]] = {
            "vision": bool(caps.get("vision")),
            "reasoning": bool(caps.get("reasoning")),
            "efforts": [e for e in (m.get("reasoning_efforts") or []) if e in EFFORTS],
            "tools": bool(caps.get("tool_calling")),
            "context": m.get("context_length"),
            "max_output": m.get("max_output_tokens"),
        }
    return out


async def fetch_nanogpt(base_url: str, api_key: str | None, http: httpx.AsyncClient | None = None) -> dict[str, dict]:
    client = http or httpx.AsyncClient(timeout=30)
    try:
        r = await client.get(base_url.rstrip("/") + "/models", params={"detailed": "true"},
                             headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
        r.raise_for_status()
        return parse(r.json().get("data", []))
    finally:
        if http is None:
            await client.aclose()


def parse_openrouter(items: list[dict]) -> dict[str, dict]:
    """OpenRouter's model list (GET /models): modalities, parameters and limits per model."""
    out = {}
    for m in items:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        arch = m.get("architecture") or {}
        params = m.get("supported_parameters") or []
        top = m.get("top_provider") or {}
        out[m["id"]] = {
            "vision": "image" in (arch.get("input_modalities") or []),
            "reasoning": "reasoning" in params or "include_reasoning" in params,
            "efforts": [],
            "tools": "tools" in params,
            "context": m.get("context_length") or top.get("context_length"),
            "max_output": top.get("max_completion_tokens"),
        }
    return out


async def fetch_openrouter(base_url: str, api_key: str | None, http: httpx.AsyncClient | None = None) -> dict[str, dict]:
    client = http or httpx.AsyncClient(timeout=30)
    try:
        r = await client.get(base_url.rstrip("/") + "/models",
                             headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
        r.raise_for_status()
        return parse_openrouter(r.json().get("data", []))
    finally:
        if http is None:
            await client.aclose()


def lookup(caps: dict[str, dict], model_id: str) -> dict | None:
    """Capabilities of `model_id`; a private/ model borrows those of its public twin."""
    if model_id in caps:
        return caps[model_id]
    n = norm_id(model_id)
    twins = [mid for mid in caps if norm_id(mid) == n]
    twins.sort(key=lambda mid: (not mid.startswith("TEE/"), mid))     # the enclave build first
    return caps[twins[0]] if twins else None


# How providers say a request didn't fit (OpenAI/vLLM, Anthropic, Gemini, llama.cpp, generic).
_OVERFLOW = re.compile(r"context[_ ]length[_ ]exceeded|maximum context length|context (?:window|size|length) "
                       r"(?:exceeded|is exceeded|limit)|exceeds? (?:the )?(?:available |model'?s? )?context|prompt is too long|"
                       r"too many (?:input )?tokens|input token count .* exceeds|reduce the length of (?:the )?(?:messages|prompt)",
                       re.I)
_LIMIT = [re.compile(p, re.I) for p in (
    r"maximum context length is (\d+)",                 # OpenAI, vLLM
    r"\d+ tokens > (\d+) maximum",                      # Anthropic
    r"maximum number of tokens allowed \((\d+)\)",      # Gemini
    r"context (?:window|size|length)(?: limit)? (?:of|is) (\d+)",
    r"n_ctx(?:_slot)?\s*[=:]\s*(\d+)",                  # llama.cpp
)]


def overflow(err: Exception | str) -> tuple[bool, int | None]:
    """Whether a provider error says the request was too long for the model's context window,
    and the window size when the message names it."""
    text = str(err)
    if not _OVERFLOW.search(text):
        return False, None
    for pat in _LIMIT:
        m = pat.search(text)
        if m and int(m.group(1)) >= 1024:
            return True, int(m.group(1))
    return True, None
