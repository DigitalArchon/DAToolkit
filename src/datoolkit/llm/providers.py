"""What kind of service a provider is, and which API it speaks.

A provider is an address and a key. Most speak OpenAI's chat completions API (NanoGPT,
OpenRouter, WaveSpeed, local servers); Anthropic's own endpoint speaks the Messages API, as
do some others that copy it ("Anthropic-compatible"). The API is worked out from the address
unless the technician chooses it (Provider.api), and the kind only labels the provider and
decides what it offers: NanoGPT searches, Private Mode and routes; OpenRouter routes;
Anthropic native web search, thinking and its prompt caching."""

from __future__ import annotations

from urllib.parse import urlsplit

from .client import is_local_url

APIS = ("openai", "anthropic")

ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# (kind, label, name, base URL, api) offered when adding a provider
PRESETS = [
    ("nanogpt", "NanoGPT", "NanoGPT", "https://nano-gpt.com/api/v1", "openai"),
    ("anthropic", "Anthropic", "Anthropic", ANTHROPIC_BASE_URL, "anthropic"),
    ("openrouter", "OpenRouter", "OpenRouter", "https://openrouter.ai/api/v1", "openai"),
    ("wavespeed", "WaveSpeed", "WaveSpeed", "https://llm.wavespeed.ai/v1", "openai"),
    ("local", "Local (Ollama / LM Studio / vLLM)", "Local", "http://localhost:11434/v1", "openai"),
]

KIND_LABELS = {"nanogpt": "NanoGPT", "anthropic": "Anthropic", "openrouter": "OpenRouter", "wavespeed": "WaveSpeed",
               "local": "Local", "training": "Training", "openai-compatible": "OpenAI-compatible",
               "anthropic-compatible": "Anthropic-compatible"}


def _host(base_url: str) -> str:
    try:
        return (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return ""


def _on(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def is_official_anthropic(base_url: str) -> bool:
    """Anthropic's own API: the one with native web search, adaptive thinking and the betas."""
    return _on(_host(base_url), "anthropic.com")


def api_of(base_url: str, api: str = "") -> str:
    """The API a provider speaks: its own choice, else Anthropic's for api.anthropic.com."""
    if api in APIS:
        return api
    return "anthropic" if is_official_anthropic(base_url) else "openai"


def kind_of(base_url: str, api: str = "") -> str:
    if base_url.startswith("training://"):
        return "training"
    host = _host(base_url)
    if _on(host, "nano-gpt.com"):
        return "nanogpt"
    if is_official_anthropic(base_url):
        return "anthropic"
    if _on(host, "openrouter.ai"):
        return "openrouter"
    if _on(host, "wavespeed.ai"):
        return "wavespeed"
    if api_of(base_url, api) == "anthropic":
        return "anthropic-compatible"
    if is_local_url(base_url):
        return "local"
    return "openai-compatible"


def describe(base_url: str, api: str = "") -> dict:
    """For the UI: the kind, its label, and the API spoken."""
    kind = kind_of(base_url, api)
    return {"kind": kind, "kind_label": KIND_LABELS[kind], "api": api_of(base_url, api)}
