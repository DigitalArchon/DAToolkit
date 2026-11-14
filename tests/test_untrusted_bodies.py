"""What is read from outside is read only so far, as every DA app reads it (the cross-app standard,
Nov 2026): only gzip and deflate asked for and any other coding refused unread, an error's body read
to 64 KB, a whole body to 32 MB (both counted decoded), and JSON nested no deeper than 200 levels.
llm/secure_http.py (make_client, loads), llm/client.py (capped_lines, reading_options) and the
clients built with them."""

import gzip
import json
import unittest.mock

import httpx
import httpx2
import pytest

from datoolkit.llm import client as client_mod
from datoolkit.llm import private_mode, secure_http, tee
from datoolkit.llm.anthropic_api import AnthropicClient
from datoolkit.llm.client import LLMClient, capped_lines, reading_options
from datoolkit.llm.secure_http import MAX_NESTING, ProviderError, TooDeep, loads, make_client, nests_deeper

from test_private_mode import BASE, RELAY, Relay


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def pieces(data: bytes, size: int = 4096):
    """A body as a connection hands it over, a piece at a time (bytes given whole are read at once)."""
    return iter([data[i:i + size] for i in range(0, len(data), size)])


async def apieces(data: bytes, size: int = 4096):
    for i in range(0, len(data), size):
        yield data[i:i + size]


@pytest.fixture(autouse=True)
def no_proxies(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_json_from_outside_is_refused_past_two_hundred_levels():
    assert loads(nested(MAX_NESTING)) is not None
    assert not nests_deeper(json.loads(nested(MAX_NESTING)))
    with pytest.raises(TooDeep):
        loads(nested(MAX_NESTING + 1))
    with pytest.raises(ValueError):
        loads('{"a": ' * 300 + "1" + "}" * 300)


def test_json_too_deep_for_the_parser_is_a_refusal_not_a_recursion_error():
    """Python's own parser gives up near a thousand levels with a RecursionError, which a caller
    looking for a ValueError let through."""
    with pytest.raises(TooDeep):
        loads(nested(100_000))


# ------------------------------------------------------------------- the sync client (TEE, Intel, NVIDIA)

def sync_client(handler) -> httpx.Client:
    client = make_client("https://x.test")
    client._transport = httpx.MockTransport(handler)
    return client


def test_the_attestation_client_asks_for_gzip_and_deflate_alone():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={})
    sync_client(handler).get("https://x.test/")
    assert seen["accept-encoding"] == "gzip, deflate"


def test_the_attestation_client_refuses_a_coding_it_did_not_ask_for():
    with pytest.raises(ProviderError, match="compressed as 'br'"):
        sync_client(lambda r: httpx.Response(200, content=b"x", headers={"content-encoding": "br"})).get(
            "https://x.test/")


def test_the_attestation_client_reads_an_error_to_64_kb_and_no_further():
    big = b"e" * (secure_http.MAX_ERROR_BYTES * 10)
    response = sync_client(lambda r: httpx.Response(500, content=pieces(big))).get("https://x.test/")
    assert len(response.content) == secure_http.MAX_ERROR_BYTES


def test_a_redirects_own_body_is_read_to_64_kb_followed_or_not():
    """httpx reads a redirect it follows whole before following it: read only so far, as an error's."""
    sent = []

    def handler(request):
        if request.url.path == "/moved":
            body = b"r" * (secure_http.MAX_ERROR_BYTES * 16)
            sent.append(0)

            def counted():
                for piece in pieces(body):
                    sent[-1] += len(piece)
                    yield piece
            return httpx.Response(302, headers={"location": "/here"}, content=counted())
        return httpx.Response(200, content=b"fine")
    client = sync_client(handler)
    followed = client.get("https://x.test/moved", follow_redirects=True)
    assert followed.text == "fine" and len(followed.history[0].content) == secure_http.MAX_ERROR_BYTES
    assert sent[0] <= secure_http.MAX_ERROR_BYTES + 4096, "the rest unread"
    assert len(client.get("https://x.test/moved").content) == secure_http.MAX_ERROR_BYTES


async def test_a_redirects_body_through_the_sdks_hook_is_read_to_64_kb():
    body = await read_through_hook(httpx.Response(
        307, headers={"location": "https://x.test/there"}, content=apieces(b"r" * (secure_http.MAX_ERROR_BYTES * 4))))
    assert len(body) == secure_http.MAX_ERROR_BYTES


def test_the_attestation_client_refuses_a_body_past_32_mb_counted_decoded(monkeypatch):
    monkeypatch.setattr(secure_http, "MAX_BODY_BYTES", 100_000)
    packed = gzip.compress(b"0" * 1_000_000)
    assert len(packed) < 100_000
    with pytest.raises(ProviderError, match="sent more than"):
        sync_client(lambda r: httpx.Response(200, content=pieces(packed), headers={"content-encoding": "gzip"})).get(
            "https://x.test/")


def test_a_session_record_nested_past_reading_is_a_note_never_a_crash():
    """RedPill's record of a session, nested far past what can be read, says it couldn't be read."""
    deep = nested(50_000).encode()
    client = sync_client(lambda r: httpx.Response(200, content=deep))
    with pytest.raises(ProviderError, match="couldn't be read"):
        tee._session_record(client, "ab" * 32)


# ------------------------------------------------------------------- the async clients (SDKs, the app's)

async def read_through_hook(response: httpx.Response) -> bytes:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response),
                                 event_hooks={"response": [capped_lines]}) as client:
        return (await client.get("https://x.test/")).content


async def test_an_error_body_is_read_to_64_kb_and_no_further():
    body = await read_through_hook(httpx.Response(400, content=apieces(b"e" * (secure_http.MAX_ERROR_BYTES * 4))))
    assert len(body) == secure_http.MAX_ERROR_BYTES


async def test_a_whole_body_past_32_mb_is_refused(monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_BODY_BYTES", 50_000)
    with pytest.raises(ProviderError, match="sent more than"):
        await read_through_hook(httpx.Response(200, content=apieces(b"line\n" * 20_000),
                                               headers={"content-type": "application/json"}))


async def test_an_event_stream_is_held_to_its_lines_not_to_its_length(monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_BODY_BYTES", 50_000)
    body = await read_through_hook(httpx.Response(200, content=apieces(b"data: x\n\n" * 20_000),
                                                  headers={"content-type": "text/event-stream"}))
    assert len(body) == 9 * 20_000


async def test_a_reply_asked_for_whole_is_held_to_32_mb_whatever_its_content_type(monkeypatch):
    """LLMClient.complete asks for a reply not streamed: one that calls itself an event stream is
    read no further than a whole body is."""
    monkeypatch.setattr(client_mod, "MAX_BODY_BYTES", 50_000)
    client = LLMClient("https://x.test/v1", "k")
    client._client._client._transport = httpx2.MockTransport(lambda request: httpx2.Response(
        200, content=apieces(b"data: x\n\n" * 20_000), headers={"content-type": "text/event-stream"}))
    with pytest.raises(ProviderError, match="sent more than"):
        await client.complete("m", [{"role": "user", "content": "hi"}])


async def test_an_answer_in_a_coding_not_asked_for_is_refused_unread():
    with pytest.raises(ProviderError, match="compressed as 'zstd'"):
        await read_through_hook(httpx.Response(200, content=b"x", headers={"content-encoding": "zstd"}))


def sdk_headers(sdk_client) -> dict:
    return dict(sdk_client._client._client.headers)


def test_every_client_reading_a_provider_asks_for_gzip_and_deflate_alone():
    """httpx (and httpx2, the SDKs') ask for br too whenever brotli is installed, and it is."""
    assert sdk_headers(LLMClient("https://x.test/v1", "k"))["accept-encoding"] == "gzip, deflate"
    assert sdk_headers(AnthropicClient("https://x.test", "k"))["accept-encoding"] == "gzip, deflate"
    options = reading_options("https://x.test")
    assert options["headers"]["Accept-Encoding"] == "gzip, deflate"
    assert capped_lines in options["event_hooks"]["response"]


# ------------------------------------------------------------------- Private Mode

async def test_private_modes_requests_ask_for_gzip_and_deflate_alone():
    relay = Relay()
    seen = []
    factory = relay.transport

    def watching(key, proxy=None):
        inner = factory(key, proxy)

        def handle(request):
            seen.append(request.headers.get("accept-encoding"))
            return inner.handle_request(request)
        return httpx.MockTransport(handle)
    client = private_mode.PrivateModeClient(BASE, "sk-test", private_mode.Enclave(RELAY, verifier=relay.verifier),
                                            transport_factory=watching)
    client._plain = None
    plain_seen = []

    async def plain(request):
        plain_seen.append(request.headers.get("accept-encoding"))
        return relay.preflight(request)
    original = httpx.AsyncClient

    def built(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(plain))
        return original(**kwargs)
    with unittest.mock.patch.object(private_mode.httpx, "AsyncClient", built):
        [event async for event in client.stream("private/glm-5-3", [{"role": "user", "content": "x"}])]
    assert seen == ["gzip, deflate"]
    assert plain_seen and set(plain_seen) == {"gzip, deflate"}


async def test_a_preflight_answer_nested_past_reading_is_refused_in_words():
    relay = Relay()
    deep = nested(50_000)
    client = relay.client()
    client._plain = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: relay.preflight(r) if r.url.path.endswith("/models") else httpx.Response(200, text=deep)))
    with pytest.raises(private_mode.PrivateModeError, match="wasn't JSON"):
        [event async for event in client.stream("private/glm-5-3", [{"role": "user", "content": "x"}])]


async def test_the_relays_error_body_is_read_to_64_kb_and_no_further():
    relay = Relay()
    client = relay.client()
    client._transport_factory = lambda key, proxy=None: httpx.MockTransport(lambda request: httpx.Response(
        500, content=apieces(b"x" * (secure_http.MAX_ERROR_BYTES * 3))))
    with pytest.raises(private_mode.PrivateModeError) as raised:
        [event async for event in client.stream("private/glm-5-3", [{"role": "user", "content": "x"}])]
    assert len(raised.value.body) == secure_http.MAX_ERROR_BYTES
