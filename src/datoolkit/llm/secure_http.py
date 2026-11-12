"""The httpx client the TEE verifier uses (llm/tee.py, dcap.py, nras.py).

Ported from SealedLore (providers/http.py), where the verifier comes from:

- **A local server stays local.** An endpoint on this machine or its network (netguard.is_local:
  loopback only, until Nov 2026) is built with `trust_env=False`, so proxy variables never route
  it elsewhere; anything else keeps the environment's proxies. Either keeps the environment's
  certificate settings. The chat clients do the same (llm/client.py local_http_options).
- **A redirect never downgrades.** The response hook refuses a redirect whose target isn't
  https (or http on loopback), whether or not the client follows redirects, and one from a
  server out on the internet into this computer or its network.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from . import netguard

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def is_loopback(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in LOOPBACK_HOSTS


def is_secure(url: str) -> bool:
    """https anywhere, or http on this machine."""
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and (parsed.hostname or "").lower() in LOOPBACK_HOSTS


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


def refuse_insecure_redirect(response: httpx.Response) -> None:
    """A response hook: a redirect to anywhere the app wouldn't send to directly, or from a
    server out on the internet into this computer or its network (an attestation endpoint
    must not reach the router's admin page)."""
    if not response.is_redirect:
        return
    location = response.headers.get("location")
    if location is None:
        return
    # httpx runs the hooks before it builds the next request, so `Location` is read here as
    # written, and a relative one ("/v2/x") is resolved the way the client will resolve it:
    # read raw, it had no scheme, so every relative redirect was refused, and one into this
    # network over https wasn't. (From SealedLore's providers/http.py, Nov 2026.)
    try:
        target = str(response.request.url.join(location))
    except httpx.InvalidURL:
        target = location
    source = str(response.request.url)
    if not is_secure(target):
        raise ProviderError(
            f"{response.request.url.host} redirected to {netguard.shown(target)[:120]!r}; refusing to "
            "send over an insecure connection"
        )
    if netguard.is_local(_host(target)) and not netguard.is_local(_host(source)):
        raise ProviderError(
            f"{response.request.url.host} redirected to {netguard.shown(target)[:120]!r}, a server on this "
            "computer or your local network; refused"
        )


def make_client(base_url: str, **kwargs: Any) -> httpx.Client:
    """An httpx client for `base_url`: no proxies for an endpoint on this machine or its network,
    and the redirect guard. `kwargs` go to `httpx.Client` as they are."""
    local = netguard.is_local(_host(base_url))
    if kwargs.setdefault("trust_env", not local) is False and local:
        # certificates still as the environment names them, which trust_env=False alone would stop
        kwargs.setdefault("verify", httpx.create_ssl_context())
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["response"] = [*hooks.get("response", ()), refuse_insecure_redirect]
    return httpx.Client(event_hooks=hooks, **kwargs)
