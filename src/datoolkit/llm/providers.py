"""What kind of service a provider is, and which API it speaks.

A provider is an address and a key. Most speak OpenAI's chat completions API (NanoGPT,
OpenRouter, WaveSpeed, Chutes, local servers); Anthropic's own endpoint speaks the Messages API, as
do some others that copy it ("Anthropic-compatible", Chutes' claude.chutes.ai among them); Tinfoil's
own API speaks Tinfoil's protocol, every request sealed end to end (llm/tinfoil_direct.py). The API
is worked out from the address unless the technician chooses it (Provider.api), and a sealed host
is spoken in its protocol whatever was chosen (llm/privacy.py chat_api: when in doubt, sealed).
The kind labels the provider and decides what it offers: NanoGPT searches, Private Mode and
routes; OpenRouter routes; Anthropic native web search, thinking and its prompt caching; Tinfoil
and Chutes their public model lists, read without the key."""

from __future__ import annotations

from urllib.parse import urlsplit

from .client import is_local_url
from .privacy import CHUTES_BASE_URL, TINFOIL_BASE_URL, chat_api, is_tinfoil, on_chutes

APIS = ("openai", "anthropic", "tinfoil")

ANTHROPIC_BASE_URL = "https://api.anthropic.com"

# (kind, label, name, base URL, api) offered when adding a provider
PRESETS = [
    ("nanogpt", "NanoGPT", "NanoGPT", "https://nano-gpt.com/api/v1", "openai"),
    ("anthropic", "Anthropic", "Anthropic", ANTHROPIC_BASE_URL, "anthropic"),
    ("tinfoil", "Tinfoil", "Tinfoil", TINFOIL_BASE_URL, ""),
    ("chutes", "Chutes", "Chutes", CHUTES_BASE_URL, ""),
    ("openrouter", "OpenRouter", "OpenRouter", "https://openrouter.ai/api/v1", "openai"),
    ("wavespeed", "WaveSpeed", "WaveSpeed", "https://llm.wavespeed.ai/v1", "openai"),
    ("local", "Local (Ollama / LM Studio / vLLM)", "Local", "http://localhost:11434/v1", "openai"),
]

KIND_LABELS = {"nanogpt": "NanoGPT", "anthropic": "Anthropic", "tinfoil": "Tinfoil", "chutes": "Chutes", "openrouter": "OpenRouter", "wavespeed": "WaveSpeed",
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
    """The API a provider speaks: Tinfoil's for Tinfoil's host whatever was chosen, else its own
    choice, else by the address: Anthropic's for api.anthropic.com and Chutes' claude.chutes.ai,
    OpenAI's for anything else (privacy.chat_api)."""
    return chat_api(base_url, api if api in APIS else "")


def kind_of(base_url: str, api: str = "") -> str:
    if base_url.startswith("training://"):
        return "training"
    host = _host(base_url)
    if _on(host, "nano-gpt.com"):
        return "nanogpt"
    if is_tinfoil(base_url, api):
        return "tinfoil"
    if on_chutes(base_url):
        return "chutes"
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
