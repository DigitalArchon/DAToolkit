"""What a model can do: vision, reasoning (and which efforts), tool calls, limits.

NanoGPT reports this in its detailed model list (GET /models?detailed=true). Private Mode
models (private/kimi-k3) are not in that list, so they are matched to their public
counterpart by name (moonshotai/kimi-k3, TEE/kimi-k3). Other OpenAI-compatible providers
report nothing, so their capabilities are unknown unless the technician sets an override."""

from __future__ import annotations

import re

import httpx

EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def _norm(model_id: str) -> str:
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


def lookup(caps: dict[str, dict], model_id: str) -> dict | None:
    """Capabilities of `model_id`; a private/ model borrows those of its public twin."""
    if model_id in caps:
        return caps[model_id]
    n = _norm(model_id)
    twins = [mid for mid in caps if _norm(mid) == n]
    twins.sort(key=lambda mid: (not mid.startswith("TEE/"), mid))     # the enclave build first
    return caps[twins[0]] if twins else None
