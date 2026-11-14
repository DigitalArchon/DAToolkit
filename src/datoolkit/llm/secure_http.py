"""The httpx client the TEE verifier uses (llm/tee.py, dcap.py, nras.py).

Ported from SealedLore (providers/http.py), where the verifier comes from:

- **A local server stays local.** An endpoint on this machine or its network (netguard.is_local:
  loopback only, until Nov 2026) is built with `trust_env=False`, so proxy variables never route
  it elsewhere; anything else keeps the environment's proxies. Either keeps the environment's
  certificate settings. The chat clients do the same (llm/client.py local_http_options).
- **A redirect never downgrades.** The response hook refuses a redirect whose target isn't
  https (or http on loopback), whether or not the client follows redirects, and one from a
  server out on the internet into this computer or its network.
- **A body is read only so far** (`capped_body`; the standard every DA app keeps, Nov 2026):
  only gzip and deflate are asked for (`ACCEPT_ENCODING`), a body sent another way is refused
  unread, an error's is read to MAX_ERROR_BYTES and no further, and any other to MAX_BODY_BYTES,
  counted as decoded. JSON from outside is read by `loads`, nested no deeper than MAX_NESTING.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx

from . import netguard

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

# What a client reading a body from outside asks for: gzip and deflate, whose output per piece read
# is bounded (about a thousand times the piece at most), never br or zstd, whose isn't (httpx asks for
# br whenever brotli is installed, and it is). A body sent any other way is refused unread (`encoded`).
ACCEPT_ENCODING = "gzip, deflate"
_ASKED_ENCODINGS = {"gzip", "x-gzip", "deflate", "identity", ""}
# A whole body (a reply not streamed, a model list, an attestation) read no further than this, as
# decoded; an error's no further than this, the rest left unread: an error is said in a line.
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
# JSON from outside nested no deeper than this (SealedLore's storage.atomic.MAX_NESTING): Python's
# parser itself gives up near a thousand levels with a RecursionError, and what reads the result
# (a canonical form, a copy, a model's validation) recurses too.
MAX_NESTING = 200


class TooDeep(ValueError):
    """JSON from outside nested deeper than MAX_NESTING levels: refused, as JSON that can't be read is."""


def nests_deeper(data: Any, limit: int = MAX_NESTING) -> bool:
    """Whether `data` (as json.loads made it) nests past `limit` levels. Walked, never recursed
    into (SealedLore's storage.atomic.nests_deeper)."""
    stack: list[tuple[Any, int]] = [(data, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return True
        children = item.values() if isinstance(item, dict) else item if isinstance(item, list) else ()
        stack.extend((child, depth + 1) for child in children if isinstance(child, dict | list))
    return False


def loads(text: str | bytes | bytearray) -> Any:
    """JSON from outside, as json.loads reads it, refused (TooDeep, a ValueError) when nested past
    MAX_NESTING levels, or so far that the parser itself gave up (a RecursionError, which until
    Nov 2026 could reach where only a ValueError was looked for)."""
    try:
        data = json.loads(text)
    except RecursionError:
        raise TooDeep(f"JSON nested far deeper than {MAX_NESTING} levels") from None
    if nests_deeper(data):
        raise TooDeep(f"JSON nested deeper than {MAX_NESTING} levels")
    return data


def encoded(response) -> str | None:
    """The content coding a response was sent in that wasn't asked for (`ACCEPT_ENCODING`), or None:
    for an httpx or httpx2 response, before its body is read."""
    for coding in str(response.headers.get("content-encoding", "")).lower().split(","):
        if coding.strip() not in _ASKED_ENCODINGS:
            return coding.strip()
    return None


def too_much(response, limit: int = MAX_BODY_BYTES) -> ProviderError:
    """A body past `limit` (MAX_BODY_BYTES), as the error to raise: nothing more of it is read."""
    return ProviderError(f"{netguard.shown(str(response.request.url))} sent more than {limit // (1024 * 1024)} MB, "
                         "which isn't a reply; it was not read further.", status_code=response.status_code)


def capped_body(response: httpx.Response) -> None:
    """A response hook for a client reading bodies from outside (make_client's): a body sent in a
    coding not asked for is refused before a byte of it is read; an error's body is read to
    MAX_ERROR_BYTES and no further; any other to MAX_BODY_BYTES, counted as decoded (a few
    compressed megabytes can be gigabytes), else ProviderError. A stream read a piece at a time
    (`client.stream`) is held to the same."""
    coding = encoded(response)
    if coding:
        raise ProviderError(f"{netguard.shown(str(response.request.url))} sent its answer compressed as "
                            f"{coding[:20]!r}, which wasn't asked for; it was not read.",
                            status_code=response.status_code)
    read = response.iter_bytes
    limit = MAX_ERROR_BYTES if response.status_code >= 400 else MAX_BODY_BYTES

    def iter_bytes(chunk_size: int | None = None):
        total = 0
        pieces = read(chunk_size)
        try:
            for chunk in pieces:
                if total + len(chunk) > limit:
                    if limit == MAX_ERROR_BYTES:
                        yield chunk[:limit - total]     # an error's first words, the rest left unread
                        response.close()
                        return
                    raise too_much(response, limit)
                total += len(chunk)
                yield chunk
        finally:
            pieces.close()
    response.iter_bytes = iter_bytes      # read (read, json, text, iter_bytes) through it


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
    the redirect guard, and bodies read only so far (`capped_body`). `kwargs` go to `httpx.Client`
    as they are."""
    local = netguard.is_local(_host(base_url))
    if kwargs.setdefault("trust_env", not local) is False and local:
        # certificates still as the environment names them, which trust_env=False alone would stop
        kwargs.setdefault("verify", httpx.create_ssl_context())
    hooks = dict(kwargs.pop("event_hooks", None) or {})
    hooks["response"] = [*hooks.get("response", ()), refuse_insecure_redirect, capped_body]
    headers = httpx.Headers(kwargs.pop("headers", None))
    headers.setdefault("Accept-Encoding", ACCEPT_ENCODING)
    return httpx.Client(event_hooks=hooks, headers=headers, **kwargs)
