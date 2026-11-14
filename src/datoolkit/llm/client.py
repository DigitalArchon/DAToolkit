"""OpenAI-compatible streaming client (NanoGPT, local servers, any compatible endpoint)."""

from __future__ import annotations

import codecs
import contextvars
import hashlib
import ipaddress
import json
import re
import time
import uuid
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx
from openai import APIError, APIStatusError, AsyncOpenAI, DefaultAsyncHttpxClient, Timeout
from openai.types.chat import ChatCompletionChunk

from . import netguard
from .secure_http import ACCEPT_ENCODING, MAX_BODY_BYTES, MAX_ERROR_BYTES, ProviderError, encoded, loads, too_much

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


# How long a model server gets to accept the connection, beside the minutes a reply may take to
# come (300 s between its pieces here, 600 s on the Messages API). The two were one number until
# Nov 2026, so a local model server that was switched off showed "thinking" for ten to twenty
# minutes (two attempts) before the turn failed.
CONNECT_TIMEOUT = 10

# Read from a provider no further than this without a line's end. A stream's line (one event) is a
# few kilobytes, an image sent inline a few megabytes: a longer one is no reply, and reading on would
# hold memory for as long as the provider sent (SealedLore's providers/streaming.MAX_LINE_CHARS).
MAX_LINE_CHARS = 32 * 1024 * 1024
_NEWLINE = re.compile(r"\r\n|\r|\n")


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
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        # "http://[::1", half typed or edited by hand in config.toml: not an address at all, so
        # not a local one. It raised here, wherever a provider's tier or kind was asked for;
        # netguard refuses the address itself, with the reason, before anything is sent to it.
        return False
    if host in ("localhost", ""):
        return True
    if host.endswith((".local", ".lan")):
        return netguard.is_dns_name(host)          # "%2e" and the like would reach DNS as another name
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    # the networks netguard allows plain http to, not Python's is_private: that also counts
    # public addresses (6to4 2002::/16, Teredo 2001::/32) and reserved ranges as private
    return netguard.is_local(host)


def local_http_options(base_url: str) -> dict:
    """Arguments for the HTTP client (httpx's, or an SDK's) that will talk to `base_url`.

    A local address stays local. For one of this machine or its network (is_local_url: what the
    Local tier counts) the client is built with `trust_env=False`, so a proxy named in the
    environment (HTTP_PROXY, HTTPS_PROXY, ALL_PROXY) is never handed the request. httpx exempts
    no address of its own accord: until Nov 2026 a Sovereign case on http://localhost:11434 went,
    key and conversation, to whatever proxy the desktop session had set. Certificates are still
    read as the environment names them (SSL_CERT_FILE, SSL_CERT_DIR: the AppImage points the first
    at the system's bundle so that a company's own CA stands), which `trust_env=False` alone
    would stop. And every scheme is mounted on the client's own transport (None), because
    Anthropic's SDK builds its default client's proxies from the environment itself, whatever
    `trust_env` says, and lets only a mount given here stand over them.

    Any other address gets nothing here, and keeps the environment's proxy as before."""
    if not is_local_url(base_url):
        return {}
    return {"trust_env": False, "verify": httpx.create_ssl_context(),
            "mounts": {"all://": None, "http://": None, "https://": None}}


def reading_options(base_url: str) -> dict:
    """`local_http_options`, and what every client reading a provider's answers takes (httpx's, or
    an SDK's, which is httpx2's): only gzip and deflate asked for (secure_http.ACCEPT_ENCODING; httpx
    asks for br too whenever brotli is installed, whose output per piece has no bound), and its
    bodies read only so far (`capped_lines`)."""
    return {"headers": {"Accept-Encoding": ACCEPT_ENCODING}, "event_hooks": {"response": [capped_lines]},
            **local_http_options(base_url)}


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
    every tool call answered before the next message (as anthropic_api.render does), and no
    tool message that answers no call."""
    out: list[dict] = []
    pending: list[str] = []          # tool call ids of the last assistant message not yet answered
    for m in messages:
        role = m.get("role")
        if role == "tool":
            if m.get("tool_call_id") not in pending:
                # An answer to no call of the assistant message before it (a case saved mid-turn
                # by an earlier version, its answers added after a later message): a provider
                # refuses the whole request for one, so it is left out. The call itself was
                # answered in its place, with NO_RESULT.
                continue
            pending.remove(m.get("tool_call_id"))
        elif pending:
            out += [{"role": "tool", "tool_call_id": i, "content": NO_RESULT} for i in pending]
            pending = []
        out.append(m if all(k in OPENAI_MESSAGE_KEYS for k in m) else {k: v for k, v in m.items() if k in OPENAI_MESSAGE_KEYS})
        if role == "assistant":
            pending = [c.get("id") for c in m.get("tool_calls") or []]
    return out + [{"role": "tool", "tool_call_id": i, "content": NO_RESULT} for i in pending]


def redirected(err: Exception) -> ProviderError | None:
    """A redirect an endpoint answered a chat request with (a 308 from www to the bare host, say),
    as the error to raise, or None for any other error. The clients follow none (a redirect
    re-sends the prompt, and the key, to wherever it points), so it is said, and where to, for the
    address to be put right in Settings if the endpoint has moved. The SDKs said "Error code: 308"
    and no more. For either SDK's APIStatusError. (SealedLore e947938, Nov 11 2026.)"""
    response = getattr(err, "response", None)
    status = getattr(response, "status_code", None)
    if not (isinstance(status, int) and 300 <= status < 400):
        return None
    location = response.headers.get("location")
    target = location
    if location:
        try:
            target = str(response.request.url.join(location))
        except Exception:  # noqa: BLE001 - an address that can't be read is named as it came
            pass
    where = f" to {netguard.shown(target)[:200]}" if target else ""
    return ProviderError(
        f"{netguard.shown(str(response.request.url))} redirected (HTTP {status}){where}. Nothing was sent "
        "there: if the endpoint has moved, put its new address in Settings → AI providers.", status_code=status)


def no_reply(response, what: str = "stream") -> ProviderError:
    """An answer that carried no reply at all (a web page where the API was expected, a sign-in
    page on a local network, a JSON body with no choices), as the error to raise: an empty reply
    would say nothing of why, and a turn ended with nothing shown at all. A body with SSE `data:`
    lines is a stream whatever its content type says; only one with none comes here.
    (SealedLore e947938, Nov 11 2026.)"""
    kind = response.headers.get("content-type", "").split(";")[0].strip() or "no content type"
    return ProviderError(
        f"{netguard.shown(str(response.request.url))} answered HTTP {response.status_code} ({kind[:80]}) with "
        f"no reply in it, not the {what} a chat endpoint sends. Check the provider's address in Settings → AI "
        "providers: a web page answers like this where the API was expected (an address without its /v1, say).",
        status_code=response.status_code)


def cut_off(response) -> ProviderError:
    """A stream that ended without the provider saying the reply was done (no finish reason and
    no `[DONE]`; on the Messages API no stop reason), as the error to raise: the connection
    closed mid-answer, at a proxy's time limit or a server that crashed. Until Nov 2026 what had
    arrived was taken for the reply, a command cut short in the middle and a half-written tool
    call included. A reply the provider itself ended early (finish reason "length",
    "content_filter") is not this: that one is whole as far as it goes, and says why it stopped."""
    return ProviderError(
        "the reply stopped before it was finished: the connection closed mid-answer, and "
        f"{netguard.shown(str(response.request.url))} never said the reply was complete. What arrived of it is "
        "not a whole reply, so no command or other call in it was taken; Retry asks again.",
        status_code=response.status_code)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text as streamed

    def parsed(self) -> dict:
        """The arguments, a JSON object. ValueError for anything else (a string or a list is
        valid JSON too): each caller answers the model that its call was malformed."""
        value = loads(self.arguments or "{}")
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
    # looked up by that), the route nano-gpt took, the sha256 of every byte it carried, and when its
    # request went (time.time(), just before it was sent): its receipt must have been served after
    # that, or it was another request's handed back
    receipt_id: str | None = None
    tee_route: str | None = None
    body_hash: str | None = None
    sent_at: float | None = None
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


def only_what_it_was_given(sdk_client) -> None:
    """An SDK's client (OpenAI's or Anthropic's), made to send only what this app gave it.

    Both SDKs read their vendor's environment variables whenever a client is built, whatever
    address it is for: OPENAI_ORG_ID and OPENAI_PROJECT_ID become headers of every request, and
    OPENAI_CUSTOM_HEADERS and ANTHROPIC_CUSTOM_HEADERS add any header at all, an Authorization
    among them. Those were set for OpenAI's or Anthropic's own service, by another tool or in the
    technician's shell. A provider here is an address and a key from Settings, so none of it goes
    with a request to NanoGPT, a local server or an Anthropic-compatible endpoint, nor to
    Anthropic itself. (The key and the address are always passed, so the variables for those
    were never read.) The SDKs take no argument that says so: what they read is cleared from the
    client as it is built, by a subclass, so that a copy the SDK makes of the client
    (`with_options`), which is built the same way, is cleared too. No default header is passed
    by this app, so none is lost. tests/test_llm_clients.py reads the headers a request then has."""
    for name in ("organization", "project"):
        if hasattr(sdk_client, name):
            setattr(sdk_client, name, None)
    sdk_client._custom_headers = {}
    if hasattr(sdk_client, "_ambient_authorizations"):      # the Authorization values OPENAI_CUSTOM_HEADERS held
        sdk_client._ambient_authorizations = frozenset()


class _OpenAI(AsyncOpenAI):
    """OpenAI's client, sending only what it was given here (only_what_it_was_given)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        only_what_it_was_given(self)


# Servers (by their address) that refused `stream_options` in this run of the app: they are not
# sent it again. It asks for the token counts at the end of a stream, which most servers send
# and a few older or stricter ones answer with HTTP 400 ("unknown field", "extra inputs are not
# permitted"). The engine's learning of refused settings (Engine._learn_unsupported) can't leave
# it out: that works on the settings the engine chose, and this one is the client's own.
_NO_STREAM_OPTIONS: set[str] = set()


def refuses_stream_options(err: Exception) -> bool:
    """Whether a server's refusal of a chat request is about `stream_options`."""
    return getattr(err, "status_code", None) in (400, 422) and "stream_options" in str(err).lower()


def not_json(response) -> ProviderError:
    """A line of a stream that is not JSON with more of the stream after it (a line the stream
    ended in the middle of is `cut_off`), as the error to raise: what it held of the reply is
    lost, so the reply is not shown as if it were whole. It raised a bare JSONDecodeError."""
    return ProviderError(
        f"{netguard.shown(str(response.request.url))} sent a line in its stream that is not JSON, so part of the "
        "reply could not be read and none of it was taken; Retry asks again.", status_code=response.status_code)


class LLMClient:
    def __init__(self, base_url: str, api_key: str | None, http_client=None):
        # Local servers often need no key, but the SDK insists on a value. The SDK's own HTTP
        # client follows redirects, which would send the request again to wherever a reply
        # points, plain http and other hosts included: netguard checked only the base URL.
        # And a local server is reached directly, whatever proxy the environment names.
        self._client = _OpenAI(base_url=base_url, api_key=api_key or "not-needed", max_retries=1,
                               timeout=Timeout(300, connect=CONNECT_TIMEOUT),
                               http_client=http_client or DefaultAsyncHttpxClient(
                                   follow_redirects=False, **reading_options(base_url)))

    async def list_models(self) -> list[str]:
        page = await self._client.models.list()
        return sorted(m.id for m in page.data)

    async def stream(
        self, model: str, messages: list[dict], tools: list[dict] | None = None, params: dict | None = None,
        *, receipts: bool = False,
    ) -> AsyncIterator[tuple[str, object]]:
        """Yield ("text"|"reasoning", str) deltas and ("tool", name) as each tool call starts,
        then ("done", TurnResult). `receipts`: a TEE model's reply, whose signed record names the
        hash of the exact bytes its stream carried (llm/tee.py check_reply): the hash of every byte,
        to the end, and the receipt's headers are kept on the TurnResult.

        A redirect, or an answer with not one SSE data line in it (a web page), raises, saying
        where the endpoint redirected or what it answered (`redirected`, `no_reply`): a web page
        ended the turn with nothing shown at all, and a redirect said only "Error code: 308". So
        does a stream that ends with neither a finish reason nor `[DONE]` (`cut_off`): the
        connection closed mid-answer, and what arrived is no reply."""
        if is_private_mode(model):
            raise RuntimeError(f"{model} is a Private Mode model and is only ever sent sealed; nothing was sent")
        kwargs: dict = {"model": model, "messages": openai_messages(messages), "stream": True,
                        "stream_options": {"include_usage": True}}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        kwargs.update(_split_params(params))
        server = str(self._client.base_url)
        while True:
            if server in _NO_STREAM_OPTIONS:
                kwargs.pop("stream_options", None)
            acc = StreamAccumulator()
            began = False
            try:
                # closed with this one: stopped or cancelled, the connection is dropped at once
                async with aclosing(self._stream_as_received(kwargs, acc, receipts)) as events:
                    async for event in events:
                        began = True
                        yield event
                break
            except APIStatusError as e:
                # a server that refuses stream_options is asked once more without it, and not
                # sent it again; any other refusal is the caller's to read
                if began or "stream_options" not in kwargs or not refuses_stream_options(e):
                    raise
                _NO_STREAM_OPTIONS.add(server)
        yield "done", acc.finish()

    async def _stream_as_received(self, kwargs: dict, acc: StreamAccumulator,
                                  receipts: bool = True) -> AsyncIterator[tuple[str, str]]:
        """The stream read line by line as it came, every byte (decoded of any transfer encoding)
        hashed to the end, `[DONE]` and after: the SDK's own reader stops at `[DONE]`, and can't
        tell a stream that said nothing from a body that wasn't a stream at all. Chunks are built
        as the SDK builds them, and an error the stream carries raises as the SDK raises it. Every
        stream is read so; `receipts` keeps the receipt's headers and the hash on the TurnResult.
        Stopped or cancelled, the connection is dropped, so the provider stops generating. From
        SealedLore's providers/openai_compat.py (Nov 2026).

        With `receipts`, when the request went is kept too (`TurnResult.sent_at`): a receipt the
        gateway says it served before then isn't this request's (llm/tee.py `_receipt_holds`)."""
        try:
            sent_at = time.time()
            async with self._client.chat.completions.with_streaming_response.create(**kwargs) as response:
                body = hashlib.sha256()
                if receipts:
                    acc.result.sent_at = sent_at
                    acc.result.receipt_id = (response.headers.get("x-receipt-id")
                                             or response.headers.get("x-tee-signature-id"))
                    acc.result.tee_route = response.headers.get("x-tee-verification-route")
                done = saw_data = unread = False
                async for line in read_lines(response.iter_bytes(), body, response.http_response.request.url):
                    if done or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data:
                        continue         # an empty data line: some servers keep the connection alive with them
                    saw_data = True
                    if unread:
                        raise not_json(response.http_response)
                    if data == "[DONE]":
                        done = True      # read on (nothing after it is taken), so the hash covers every byte
                        continue
                    try:
                        chunk = loads(data)
                    except ValueError:
                        # if nothing follows it, the stream ended in the middle of this line (cut_off
                        # below); if more does, a line of the reply is lost (not_json above)
                        unread = True
                        continue
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
                if not saw_data:
                    # not one event: whatever came wasn't a reply, and an empty one would say nothing of why
                    raise no_reply(response.http_response)
                if unread or (not done and acc.result.finish_reason is None):
                    # it ended saying neither why nor that it had, or in the middle of a line: the
                    # connection closed mid-answer
                    raise cut_off(response.http_response)
                if receipts:
                    acc.result.body_hash = "sha256:" + body.hexdigest()
        except APIStatusError as e:              # only the request's own answer raises it
            redirect = redirected(e)
            if redirect is None:
                raise
            raise redirect from e

    async def complete(self, model: str, messages: list[dict], params: dict | None = None) -> str:
        if is_private_mode(model):
            raise RuntimeError(f"{model} is a Private Mode model and is only ever sent sealed; nothing was sent")
        whole = WHOLE_BODY.set(True)        # a reply not streamed: read no further than MAX_BODY_BYTES
        try:
            raw = await self._client.chat.completions.with_raw_response.create(
                model=model, messages=openai_messages(messages), **_split_params(params))
        except APIStatusError as e:
            redirect = redirected(e)
            if redirect is None:
                raise
            raise redirect from e
        finally:
            WHOLE_BODY.reset(whole)
        resp = raw.parse()
        # a body that isn't a chat completion (a web page, which the SDK hands back as text) said
        # "'str' object has no attribute 'choices'"
        choices = None if isinstance(resp, str) else getattr(resp, "choices", None)
        if not choices or getattr(choices[0], "message", None) is None:
            raise no_reply(raw.http_response, "reply")
        return choices[0].message.content or ""


def too_long(url) -> ProviderError:
    """A provider sent a line of more than MAX_LINE_CHARS with no end."""
    return ProviderError(f"{netguard.shown(str(url))} sent a line of more than {MAX_LINE_CHARS // (1024 * 1024)} MB "
                         "with no end, which isn't a reply; it was not read further.")


async def read_lines(chunks, body=None, url="the provider") -> AsyncIterator[str]:
    """The stream's lines (ended by CRLF, CR or LF, as an event stream's are, and nothing else),
    each byte also fed to `body` (a hash) as it arrives, decoded of any transfer encoding
    (SealedLore's providers/openai_compat._lines). Each piece is split once, so a long line costs
    its length and no more: the line so far was joined to each piece that came and split again,
    whole, which took time growing with the square of its length; and a line past MAX_LINE_CHARS
    is a ProviderError, nothing more read (SealedLore's streaming.read_lines). `chunks`: the
    response's bytes as they arrive; `url`: where they came from, for the error."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    line: list[str] = []            # the line not yet ended, in the pieces it came in
    length = 0
    held_cr = False                 # a CR at a piece's end may be the first half of a CRLF

    def ended(text: str, final: bool) -> list[str]:
        nonlocal length, held_cr
        if held_cr:
            text = "\r" + text
        held_cr = not final and text.endswith("\r")
        if held_cr:
            text = text[:-1]
        out = []
        parts = _NEWLINE.split(text)
        for index, part in enumerate(parts):
            length += len(part)
            if length > MAX_LINE_CHARS:
                raise too_long(url)
            line.append(part)
            if index < len(parts) - 1:
                out.append("".join(line))
                line.clear()
                length = 0
        return out

    async for chunk in chunks:
        if body is not None:
            body.update(chunk)
        for done in ended(decoder.decode(chunk), final=False):
            yield done
    for done in ended(decoder.decode(b"", final=True), final=True):
        yield done
    if line and length:
        yield "".join(line)


# Set while a reply that isn't streamed is asked for (LLMClient.complete, and the like): its body
# is read no further than MAX_BODY_BYTES whatever its content type says (`capped_lines`).
WHOLE_BODY: contextvars.ContextVar[bool] = contextvars.ContextVar("whole_body", default=False)


async def capped_lines(response: httpx.Response) -> None:
    """A response hook for every client reading a provider's answers (OpenAI's and Anthropic's SDKs',
    and the app's own: `reading_options`): whatever is read of it (the Messages API's event stream, a
    reply not streamed, a model list) is read no further than MAX_LINE_CHARS without a line's end, as
    the streams this app reads are (`read_lines`). Counted in the bytes as decoded of any content
    encoding, which is what the SDK holds: a few compressed megabytes can be a line of gigabytes. The
    SDK's own readers keep a line until it ends, joining each piece to what came before.

    And as every DA app reads an answer (Nov 2026): one compressed in a way that wasn't asked for
    (br, zstd: secure_http.ACCEPT_ENCODING) is refused unread; an error's body is read to
    MAX_ERROR_BYTES and no further, the rest left unread; and a body that isn't an event stream (or
    one asked for whole: WHOLE_BODY) to MAX_BODY_BYTES in all. A refusal raises out of the SDK's
    reading as it is."""
    coding = encoded(response)
    if coding:
        raise ProviderError(f"{netguard.shown(str(response.request.url))} sent its answer compressed as "
                            f"{coding[:20]!r}, which wasn't asked for; it was not read.",
                            status_code=response.status_code)
    read = response.aiter_bytes
    error = response.status_code >= 400
    stream = (not WHOLE_BODY.get()
              and str(response.headers.get("content-type", "")).lower().startswith("text/event-stream"))
    limit = MAX_ERROR_BYTES if error else None if stream else MAX_BODY_BYTES

    async def aiter_bytes(chunk_size: int | None = None) -> AsyncIterator[bytes]:
        run = total = 0
        pieces = read(chunk_size)
        try:
            async for chunk in pieces:
                if limit is not None and total + len(chunk) > limit:
                    if error:
                        yield chunk[:limit - total]     # an error's first words, the rest left unread
                        await response.aclose()
                        return
                    raise too_much(response, limit)
                total += len(chunk)
                end = max(chunk.rfind(b"\n"), chunk.rfind(b"\r"))
                run = len(chunk) - end - 1 if end >= 0 else run + len(chunk)
                if run > MAX_LINE_CHARS:
                    raise too_long(response.request.url)
                yield chunk
        finally:
            await pieces.aclose()
    response.aiter_bytes = aiter_bytes     # read (aread, aiter_lines, the SDKs' streams) through it


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


def _text_of(content) -> str:
    """A delta's text. A string as a rule; some servers send the content as a list of parts
    (`[{"type": "text", "text": "..."}]`, as a request's content may be), which raised a
    TypeError when it was added to the reply."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part["text"] for part in content
                       if isinstance(part, dict) and part.get("type", "text") == "text" and isinstance(part.get("text"), str))
    return ""


def _whole_json(text: str) -> bool:
    try:
        loads(text)
    except ValueError:
        return False
    return True


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
        # Chunks are built without validation (construct), so a field may hold anything a server
        # cared to send. What isn't of the shape a chunk has is passed over, not tripped on: a
        # usage that is a string, a choice or a tool call that is null, raised an AttributeError
        # that ended the turn, after the text had streamed.
        usage = getattr(chunk, "usage", None)
        if hasattr(usage, "model_dump"):
            self.result.usage = usage.model_dump()
        elif isinstance(usage, dict):
            self.result.usage = dict(usage)
        # NanoGPT's own record of the charge: its usage.cost comes and goes between replies
        # (as SealedLore found), and a routed call's bill is the evidence that the route held
        pricing = (getattr(chunk, "model_extra", None) or {}).get("x_nanogpt_pricing")
        if isinstance(pricing, dict):
            self._pricing = pricing
        choices = getattr(chunk, "choices", None)
        choice = choices[0] if isinstance(choices, list) and choices else None
        if choice is None:
            return events
        if isinstance(getattr(choice, "finish_reason", None), str) and choice.finish_reason:
            self.result.finish_reason = choice.finish_reason
        delta = getattr(choice, "delta", None)
        if delta is None:
            return events
        extra = getattr(delta, "model_extra", None) or {}
        reasoning = extra.get("reasoning_content") or extra.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            self.result.reasoning += reasoning
            events.append(("reasoning", reasoning))
        text = _text_of(getattr(delta, "content", None))
        if text:
            self.result.content += text
            events.append(("text", text))
        tool_calls = getattr(delta, "tool_calls", None)
        for tc in tool_calls if isinstance(tool_calls, list) else []:
            if not hasattr(tc, "function"):
                continue
            call = self._call_for(tc)
            if tc.id and not call.id:    # the id a call began with stays its id, whatever later pieces bring
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
        """The call a delta continues, or a new one it begins. A delta is read against the call at
        its index, or the last call when it has no index. Some routes number every call 0, so a
        delta begins another call when it brings an id of its own, a name, and the call there has
        all its arguments. Other routes give one call a new id with every piece (and some repeat the
        name each time): a piece that brings no name, or arrives while the arguments so far are
        not yet whole JSON, belongs to the call being written. Until Nov 2026 any new id began a
        call, so such a call came apart into one with half its arguments and one with no name."""
        index = tc.index if isinstance(getattr(tc, "index", None), int) else None
        current = self._at.get(index) if index is not None else (self._calls[-1] if self._calls else None)
        another = (current is not None and tc.id and current.id and tc.id != current.id and current.name
                   and getattr(tc.function, "name", None) and _whole_json(current.arguments))
        if current is None or another:
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
