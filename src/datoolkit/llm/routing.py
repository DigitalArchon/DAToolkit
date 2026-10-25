"""Which host serves a model: a route, per model, on NanoGPT and OpenRouter.

Ported from SealedLore (engine/routing.py, providers/model_hosts.py). NanoGPT runs most open
models (GLM, DeepSeek, Kimi, Qwen) on several upstream hosts and picks one; on the
subscription, usually the cheapest at FP8 precision or better. A route asks for another:

- a priority, sent as `provider.sort`: "latency" (the fastest first word), "speed" (fastest
  overall), "price" (cheapest);
- a host, sent as a preference (`provider.order`), never a pin: a host that is down falls
  back to another;
- "FP8 or better" (`provider.min_quantization`), ticked by default: hosts that run the model
  at lower precision, or don't say, are left out (FP4 hosts measured worse in SealedLore).

Any NanoGPT route but its own routing is billed pay-as-you-go, even for a model the
subscription includes. OpenRouter takes the same three priorities (`provider.sort`, its
"throughput" for "speed") and the precision floor (`provider.quantizations`).

Routes never apply to private/ (end-to-end encrypted) or TEE models: their protection is the
enclave they were attested on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

PRIORITIES = ("latency", "speed", "price", "host")
LABELS = {"": "Provider's own routing", "latency": "Fastest first word", "speed": "Fastest overall",
          "price": "Cheapest", "host": "A specific host"}
OWN = {"nanogpt": "NanoGPT's own routing (subscription)", "openrouter": "OpenRouter's own routing"}
ROUTABLE_KINDS = ("nanogpt", "openrouter")
_OPENROUTER_SORT = {"latency": "latency", "speed": "throughput", "price": "price"}
_FP8_UP = ["fp8", "fp16", "bf16", "fp32"]

# Bits per weight, for the "FP8 or better" floor; a host whose precision isn't known is left out
BITS = {"int4": 4, "fp4": 4, "nvfp4": 4, "fp6": 6, "int8": 8, "fp8": 8, "fp16": 16, "bf16": 16, "fp32": 32}
PRIVACY = {"zdr": "No retention", "no_training": "Kept, not trained on", "logs_training": "Kept and trained on"}
# a routed reply billed this far from the preferred host's price came from another host
PRICE_TOLERANCE = 0.2


def routable(kind: str, model: str, tier: str) -> bool:
    return kind in ROUTABLE_KINDS and bool(model) and not model.startswith("private/") and tier == "standard"


def validate(route: dict | None, kind: str) -> dict | None:
    """A route as stored, or None for the provider's own routing. Raises ValueError."""
    if not route or not route.get("priority"):
        return None
    priority = str(route["priority"])
    if priority not in PRIORITIES or (priority == "host" and kind != "nanogpt"):
        raise ValueError(f"Unknown route {priority!r}.")
    out: dict = {"priority": priority, "fp8": bool(route.get("fp8", True))}
    if priority == "host":
        host = str(route.get("host") or "").strip()
        if not host:
            raise ValueError("Choose the host.")
        out["host"] = host
        if route.get("host_name"):
            out["host_name"] = str(route["host_name"])[:80]
        price = route.get("price")
        if isinstance(price, dict):
            out["price"] = {k: float(price[k]) for k in ("input", "output", "cache_read")
                            if isinstance(price.get(k), (int, float))}
    return out


def body(kind: str, route: dict | None) -> dict:
    """The request field for `route`: none for the provider's own routing."""
    if not route or kind not in ROUTABLE_KINDS:
        return {}
    provider: dict[str, Any] = {}
    if kind == "nanogpt":
        if route["priority"] == "host":
            provider["order"] = [route["host"]]
        else:
            provider["sort"] = route["priority"]
        if route.get("fp8"):
            provider["min_quantization"] = "fp8"
    else:
        sort = _OPENROUTER_SORT.get(route["priority"])
        if not sort:
            return {}
        provider["sort"] = sort
        if route.get("fp8"):
            provider["quantizations"] = list(_FP8_UP)
    return {"provider": provider}


def label(kind: str, route: dict | None) -> str:
    """ "Fastest first word · FP8+", for a badge."""
    if not route:
        return OWN.get(kind, LABELS[""])
    what = route.get("host_name") or route.get("host") if route["priority"] == "host" else LABELS[route["priority"]]
    return f"{what} · {'FP8+' if route.get('fp8') else 'any precision'}"


# ---------------------------------------------------------------- NanoGPT's hosts for a model

@dataclass
class Host:
    id: str
    name: str
    available: bool = True
    quantization: str | None = None
    privacy: str | None = None
    tokens_per_second: float | None = None
    first_token_ms: float | None = None
    input_price: float | None = None       # USD per million tokens
    output_price: float | None = None
    cache_read_price: float | None = None
    caching: bool | None = None

    @property
    def fp8_or_better(self) -> bool:
        return BITS.get((self.quantization or "").lower(), 0) >= 8

    def to_dict(self) -> dict:
        return {**asdict(self), "fp8_or_better": self.fp8_or_better,
                "privacy_label": PRIVACY.get(self.privacy or "", "Unknown")}


@dataclass
class ModelHosts:
    model: str
    supported: bool = False
    auto_tokens_per_second: float | None = None
    auto_first_token_ms: float | None = None
    hosts: list[Host] = field(default_factory=list)
    official: str | None = None       # the model maker's own host, when NanoGPT lists one

    def to_dict(self) -> dict:
        return {"model": self.model, "supported": self.supported, "auto_tokens_per_second": self.auto_tokens_per_second,
                "auto_first_token_ms": self.auto_first_token_ms, "official": self.official,
                "hosts": [h.to_dict() for h in self.hosts]}


def _number(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _per_million(pricing: Any, key: str) -> float | None:
    per_1k = _number(pricing.get(key)) if isinstance(pricing, dict) else None
    return per_1k * 1000 if per_1k is not None else None


def parse_hosts(model: str, data: dict) -> ModelHosts:
    hosts = []
    for e in data.get("providers") or []:
        if not isinstance(e, dict) or not isinstance(e.get("provider"), str):
            continue
        privacy = e.get("privacy") if isinstance(e.get("privacy"), dict) else {}
        hosts.append(Host(
            id=e["provider"], name=str(e.get("displayName") or e["provider"]), available=e.get("available") is not False,
            quantization=e.get("quantization") if isinstance(e.get("quantization"), str) else None,
            privacy=privacy.get("classification") if isinstance(privacy.get("classification"), str) else None,
            tokens_per_second=_number(e.get("tps")), first_token_ms=_number(e.get("ttftMs")),
            input_price=_per_million(e.get("pricing"), "inputPer1kTokens"),
            output_price=_per_million(e.get("pricing"), "outputPer1kTokens"),
            cache_read_price=_per_million(e.get("pricing"), "cacheReadInputPer1kTokens"),
            caching=e.get("supportsPromptCaching") if isinstance(e.get("supportsPromptCaching"), bool) else None))
    baseline = data.get("officialBaseline")
    official = baseline.get("provider") if isinstance(baseline, dict) else None
    return ModelHosts(model=model, supported=data.get("supportsProviderSelection") is True,
                      auto_tokens_per_second=_number(data.get("autoTps")), auto_first_token_ms=_number(data.get("autoTtftMs")),
                      hosts=hosts, official=official if isinstance(official, str) else None)


def hosts_url(base_url: str, model: str) -> str:
    """https://nano-gpt.com/api/v1 -> https://nano-gpt.com/api/models/<model>/providers"""
    p = urlsplit(base_url)
    return f"{p.scheme}://{p.netloc}/api/models/{quote(model, safe='')}/providers"


async def fetch_hosts(base_url: str, model: str, http: httpx.AsyncClient | None = None) -> ModelHosts:
    """NanoGPT's hosts for `model`, with its own figures for each. Needs no key, so none is sent."""
    client = http or httpx.AsyncClient(timeout=30, follow_redirects=False)
    try:
        r = await client.get(hosts_url(base_url, model), headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    finally:
        if http is None:
            await client.aclose()
    if not isinstance(data, dict):
        raise ValueError(f"the hosts listing for {model} wasn't an object")
    return parse_hosts(model, data)


# ---------------------------------------------------------------- was the route followed?

def check(kind: str, route: dict | None, usage: dict | None) -> dict | None:
    """What one routed reply's bill says about its route: {"met": True | False | None, "why"}.
    NanoGPT names no host in a reply, so the cost is the evidence: nothing billed means it
    served the call on its own routing (a host that was down, say); a bill far from the
    chosen host's price means another host. None when no route was asked for."""
    if not route or kind != "nanogpt":
        return None
    cost = (usage or {}).get("cost")
    if not isinstance(cost, (int, float)):
        return {"met": None, "why": "NanoGPT reported no cost for this reply, so whether the route held is unknown."}
    if cost == 0:
        return {"met": False, "why": ("NanoGPT billed nothing, so it served this reply on its own subscription routing "
                                      "and not the route (the host may be down or not serve this model now, or keep "
                                      "prompts while your account requires hosts that keep nothing).")}
    price = route.get("price") if route.get("priority") == "host" else None
    prompt = int((usage or {}).get("prompt_tokens") or 0)
    written = int((usage or {}).get("completion_tokens") or 0)
    if price and price.get("input") is not None and price.get("output") is not None and (prompt or written):
        cached = int(((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        cached = min(cached, prompt)
        cache_price = price.get("cache_read", price["input"])
        expected = ((prompt - cached) * price["input"] + cached * cache_price + written * price["output"]) / 1e6
        if expected > 0 and abs(cost - expected) > PRICE_TOLERANCE * expected:
            return {"met": False, "why": (f"Billed ${cost:.6f} where the chosen host would bill about ${expected:.6f}: "
                                          "another host served it (the chosen one may be busy or down).")}
        return {"met": True, "why": f"Billed ${cost:.6f}, at the chosen host's price."}
    return {"met": True, "why": f"Billed ${cost:.6f} as a routed call."}
