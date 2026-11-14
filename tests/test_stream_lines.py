"""A provider's stream is read a line at a time, each piece split once, and no line past
MAX_LINE_CHARS is held (llm/client.py read_lines; capped_lines under the SDKs' own readers): the
line so far was joined to every piece and split again, whole, and nothing bounded it. From
SealedLore's providers/streaming.read_lines and its tests."""

import gzip
import json
import time

import httpx
import httpx2
import pytest

from datoolkit.llm import client as client_mod
from datoolkit.llm.anthropic_api import AnthropicClient
from datoolkit.llm.client import LLMClient, capped_lines, read_lines
from datoolkit.llm.secure_http import ProviderError


@pytest.fixture(autouse=True)
def no_proxies(monkeypatch):
    """The SDKs' clients route through no proxy here: their transport is the test's."""
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


async def pieces_of(data: bytes, size: int):
    for i in range(0, len(data), size):
        yield data[i:i + size]


class Pieces:
    """A body served a piece at a time, counting how many were taken."""

    def __init__(self, pieces) -> None:
        self._pieces = iter(pieces)
        self.taken = 0

    async def __aiter__(self):
        for piece in self._pieces:
            self.taken += 1
            yield piece


@pytest.mark.parametrize("size", [1, 2, 3, 7, 1000])
async def test_lines_read_in_any_pieces_are_the_lines_of_an_event_stream(size):
    text = "data: one\r\ndata: two\rdata: thrée\n\ndata: four\r\n\r\nlast"
    lines = [line async for line in read_lines(pieces_of(text.encode(), size))]
    assert lines == ["data: one", "data: two", "data: thrée", "", "data: four", "", "last"]


async def test_only_an_event_streams_line_ends_end_a_line():
    """U+2028, U+2029 and U+0085 are left unescaped in a JSON string: a line is not ended there."""
    text = 'data: {"t": "a\u2028b\u2029c\u0085d\x0be"}\n'
    assert [line async for line in read_lines(pieces_of(text.encode(), 4))] == [text[:-1]]


async def test_a_long_line_costs_its_length_not_its_length_squared():
    """Every piece was added to the line so far and the whole of it split again: a megabyte-long
    line in small pieces took tens of seconds."""
    data = b"data: " + b"x" * (1024 * 1024) + b"\n"
    started = time.monotonic()
    lines = [line async for line in read_lines(pieces_of(data, 32))]
    assert [len(line) for line in lines] == [len(data) - 1]
    assert time.monotonic() - started < 5.0


def openai_client(body) -> LLMClient:
    """An LLMClient whose own HTTP client (the one it builds) answers with `body`."""
    client = LLMClient("https://x.test/v1", "k")
    http = client._client._client
    http._transport = httpx2.MockTransport(lambda request: httpx2.Response(
        200, content=body, headers={"content-type": "text/event-stream"}))
    return client


async def test_a_stream_line_that_never_ends_is_refused_before_it_fills_memory(monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_LINE_CHARS", 10_000)
    body = Pieces([b"data: " + b"x" * 1000] + [b"x" * 1000] * 10_000)
    with pytest.raises(ProviderError, match="x.test/v1/chat/completions sent a line of more than"):
        async for _ in openai_client(body).stream("m", [{"role": "user", "content": "hi"}]):
            pass
    assert body.taken < 20, "read no further than the limit"


async def test_a_stream_still_carries_its_reply():
    stream = (b'data: {"id": "c1", "choices": [{"delta": {"content": "Hel"}}]}\r\n\r\n'
              b'data: {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n')
    events = [e async for e in openai_client(Pieces([stream[i:i + 5] for i in range(0, len(stream), 5)]))
              .stream("m", [{"role": "user", "content": "hi"}])]
    assert "".join(v for k, v in events if k == "text") == "Hello"
    assert (events[-1][1].id, events[-1][1].finish_reason) == ("c1", "stop")


def test_the_sdks_own_clients_read_through_the_cap():
    openai_http = LLMClient("https://x.test/v1", "k")._client._client
    anthropic_http = AnthropicClient("https://api.anthropic.com", "k")._client._client
    for http in (openai_http, anthropic_http):
        assert capped_lines in http.event_hooks["response"]


async def test_what_an_sdk_reads_itself_is_capped_decoded_too(monkeypatch):
    """The Messages API's event stream is read by Anthropic's SDK, which keeps a line until it ends;
    a reply not streamed is read whole by either SDK. Counted as decoded: a few compressed
    kilobytes can be a line of megabytes."""
    monkeypatch.setattr(client_mod, "MAX_LINE_CHARS", 10_000)
    endless = Pieces([b"event: message_start\ndata: " + b"x" * 1000] + [b"x" * 1000] * 10_000)
    client = AnthropicClient("https://api.anthropic.com", "k")
    client._client._client._transport = httpx2.MockTransport(lambda request: httpx2.Response(
        200, content=endless, headers={"content-type": "text/event-stream"}))
    with pytest.raises(ProviderError, match="api.anthropic.com/v1/messages sent a line of more than"):
        async for _ in client.stream("claude-opus-5-5", [{"role": "user", "content": "hi"}]):
            pass
    assert endless.taken < 20, "read no further than the limit"
    # a body of one compressed line, read whole (aread): refused by what it decodes to
    bomb = gzip.compress(json.dumps({"data": ["x" * 30_000]}).encode())
    assert len(bomb) < 1000

    async def handler(request):
        return httpx.Response(200, content=Pieces([bomb]), headers={"content-encoding": "gzip"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), event_hooks={"response": [capped_lines]}) as h:
        with pytest.raises(ProviderError, match="a line of more than"):
            await h.get("https://x.test/v1/models")
    # lines that end come through whole, however many
    many = b"data: x\n" * 5000

    async def lines(request):
        return httpx.Response(200, content=Pieces([many[i:i + 999] for i in range(0, len(many), 999)]))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lines), event_hooks={"response": [capped_lines]}) as h:
        assert (await h.get("https://x.test/")).content == many


async def test_anthropics_stream_still_carries_its_reply():
    from test_anthropic import _sse

    reply = _sse([{"type": "text", "text": "Hello"}])
    client = AnthropicClient("https://api.anthropic.com", "k")
    client._client._client._transport = httpx2.MockTransport(lambda request: httpx2.Response(
        200, content=Pieces([reply[i:i + 7] for i in range(0, len(reply), 7)]),
        headers={"content-type": "text/event-stream"}))
    events = [e async for e in client.stream("claude-opus-5-5", [{"role": "user", "content": "hi"}])]
    assert "".join(v for k, v in events if k == "text") == "Hello"
