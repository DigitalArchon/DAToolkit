"""The model clients on the wire (llm/client.py, llm/anthropic_api.py, and the smaller clients
built from a provider's address): what reaches a proxy the environment names, and what doesn't.

A stub proxy and a stub provider listen on loopback; nothing here leaves the machine."""

import asyncio
import json

import pytest

from datoolkit.llm import capabilities, research, routing, websearch
from datoolkit.llm.anthropic_api import AnthropicClient
from datoolkit.llm.client import LLMClient, local_http_options

from test_engine import env, wait_turn  # noqa: F401

USER = [{"role": "user", "content": "SOVEREIGN CASE: the database password is hunter2"}]
CHUNK = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
         "choices": [{"index": 0, "delta": {"content": "fine"}, "finish_reason": "stop"}]}


def anthropic_sse(text="fine", stop="end_turn") -> bytes:
    def ev(kind, **data):
        return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"
    return (ev("message_start", message={"id": "m1", "type": "message", "role": "assistant", "model": "m", "content": [],
                                         "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 0}})
            + ev("content_block_start", index=0, content_block={"type": "text", "text": ""})
            + ev("content_block_delta", index=0, delta={"type": "text_delta", "text": text})
            + ev("content_block_stop", index=0)
            + ev("message_delta", delta={"stop_reason": stop}, usage={"output_tokens": 1})
            + ev("message_stop")).encode()


class Stub:
    """An HTTP server on loopback that keeps what it was sent. As a provider it answers every
    path with what `answers` holds for it; as a proxy it answers 502 to everything."""

    def __init__(self, answers: dict | None = None):
        self.answers = answers
        self.seen: list[bytes] = []
        self.server = None

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()

    def paths(self) -> list[str]:
        return [raw.split(b" ")[1].decode() for raw in self.seen]

    async def _handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                           if line.lower().startswith(b"content-length:")), 0)
            body = await reader.readexactly(length) if length else b""
            self.seen.append(head + body)
            path = head.split(b" ")[1].decode().split("?")[0]
            found = (self.answers or {}).get(path) if self.answers is not None else None
            if found is None:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            else:
                kind, content = found
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {kind}\r\nContent-Length: {len(content)}\r\n"
                             "Connection: close\r\n\r\n".encode() + content)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def proxy(monkeypatch):
    """A proxy on loopback that every proxy variable names, and nothing exempted from it."""
    async with Stub() as stub:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{stub.port}")
            monkeypatch.setenv(name.lower(), f"http://127.0.0.1:{stub.port}")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        yield stub


PROVIDER = {"/v1/chat/completions": ("text/event-stream", f"data: {json.dumps(CHUNK)}\n\ndata: [DONE]\n\n".encode()),
            "/v1/messages": ("text/event-stream", anthropic_sse()),
            "/v1/models": ("application/json", json.dumps({"object": "list", "data": [{"id": "m", "object": "model"}],
                                                           "has_more": False, "first_id": None, "last_id": None}).encode()),
            "/api/web": ("application/json", b'{"data": []}'),
            "/api/scrape-urls": ("application/json", b'{"results": []}'),
            "/api/models/m/providers": ("application/json", b'{"providers": []}')}


async def test_a_local_provider_is_reached_directly_whatever_proxy_the_environment_names(proxy):
    """Until Nov 2026 a "Local" provider's requests went to HTTP_PROXY / ALL_PROXY like any other:
    the key and a Sovereign case's conversation, off the machine."""
    async with Stub(PROVIDER) as provider:
        base = f"http://127.0.0.1:{provider.port}"
        for client in (LLMClient(base + "/v1", "sk-local-SECRET"), AnthropicClient(base, "sk-local-SECRET")):
            events = [e async for e in client.stream("m", USER)]
            assert events[-1][0] == "done" and events[-1][1].content == "fine"
            assert await client.list_models() == ["m"]
        # the model list with its capabilities: the real functions (conftest keeps the engine off them)
        for name in ("fetch_nanogpt", "fetch_openrouter"):
            real = getattr(capabilities, name).__defaults__[-1]
            assert list(await real(base + "/v1", "sk-local-SECRET")) == ["m"]
        await websearch.web_search(base + "/api/v1", "sk-local-SECRET", "a query")
        await research.scrape(base + "/api/v1", "sk-local-SECRET", ["https://docs.example.com/a"])
        await routing.fetch_hosts(base + "/api/v1", "m")
        assert proxy.seen == [], "nothing for a local address reaches the proxy"
        assert set(provider.paths()) >= {"/v1/chat/completions", "/v1/messages", "/v1/models", "/v1/models?detailed=true",
                                         "/api/web", "/api/scrape-urls", "/api/models/m/providers"}
        assert all(b"hunter2" not in raw for raw in proxy.seen)


async def test_any_other_address_keeps_the_environments_proxy(proxy):
    for client in (LLMClient("https://models.example.invalid/v1", "sk-x"),
                   AnthropicClient("https://models.example.invalid", "sk-x")):
        client._client = client._client.with_options(max_retries=0)
        with pytest.raises(Exception):                                    # noqa: B017 - the stub proxy answers 502
            [e async for e in client.stream("m", USER)]
    assert len(proxy.seen) == 2 and all(raw.startswith(b"CONNECT models.example.invalid:443") for raw in proxy.seen)


def test_which_addresses_stay_off_the_proxy():
    for local in ("http://localhost:11434/v1", "http://127.0.0.1:8080/v1", "http://[::1]:1234/v1", "http://10.0.0.5/v1",
                  "http://172.16.0.1/v1", "http://192.168.1.50:11434/v1", "http://169.254.10.10/v1", "http://[fe80::1]/v1",
                  "http://[fd12:3456:789a::1]/v1", "http://ollama.local:11434/v1", "https://ollama.local/v1",
                  "https://models.office.lan/v1", "https://192.168.1.50/v1"):
        options = local_http_options(local)
        assert options["trust_env"] is False, local
        for client in (LLMClient(local, "k"), AnthropicClient(local, "k")):
            assert client._client._client.trust_env is False, local
    for other in ("https://nano-gpt.com/api/v1", "https://api.anthropic.com", "https://192.0.2.7/v1",
                  "https://127.0.0.1.evil.example/v1", "https://lan/v1", "https://[2001:db8::1]/v1"):
        assert local_http_options(other) == {}, other
        for client in (LLMClient(other, "k"), AnthropicClient(other, "k")):
            assert client._client._client.trust_env is True, other


def test_a_local_https_server_is_still_checked_against_the_environments_certificates(tmp_path, monkeypatch):
    """`trust_env=False` alone would also stop SSL_CERT_FILE being read, which the AppImage sets
    to the system's bundle so that a company's own CA stands for a server on its network."""
    import certifi

    bundle = tmp_path / "company-ca.pem"
    bundle.write_text(open(certifi.where()).read().split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))
    context = local_http_options("https://models.office.lan/v1")["verify"]
    assert len(context.get_ca_certs()) == 1                              # the one certificate of that file
    assert context.verify_mode.name == "CERT_REQUIRED" and context.check_hostname


# ---------------------------------------------------------------- the SDKs' own environment variables

def mocked(make, handler):
    """`make(http_client)`'s client, answered by `handler` (an httpx2.MockTransport)."""
    import httpx2
    from openai import DefaultAsyncHttpxClient

    client = make(DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler), follow_redirects=False))
    client._client = client._client.with_options(max_retries=0)
    return client


async def test_the_sdks_environment_adds_no_header_to_a_provider(monkeypatch):
    """OPENAI_ORG_ID, OPENAI_PROJECT_ID, OPENAI_CUSTOM_HEADERS and ANTHROPIC_CUSTOM_HEADERS are for
    OpenAI's and Anthropic's own services: whatever another tool or the shell set there went, as
    headers, to every provider of that API. A provider is sent its own key and nothing else."""
    import httpx2

    for name, value in {"OPENAI_ORG_ID": "org-ENV", "OPENAI_PROJECT_ID": "proj-ENV", "OPENAI_API_KEY": "ENV-OPENAI-KEY",
                        "OPENAI_ADMIN_KEY": "ENV-ADMIN-KEY", "OPENAI_BASE_URL": "https://env-base.invalid/v1",
                        "OPENAI_CUSTOM_HEADERS": "X-Env: 1\nAuthorization: Bearer ENV-HEADER-KEY\nOpenAI-Beta: env",
                        "ANTHROPIC_CUSTOM_HEADERS": "X-Env: 1\nx-api-key: ENV-HEADER-KEY\nanthropic-beta: env-beta",
                        "ANTHROPIC_API_KEY": "ENV-ANTHROPIC-KEY", "ANTHROPIC_AUTH_TOKEN": "ENV-OAUTH-TOKEN",
                        "ANTHROPIC_BASE_URL": "https://env-base.invalid"}.items():
        monkeypatch.setenv(name, value)
    got: list[tuple[str, dict]] = []

    def handler(request):
        got.append((str(request.url), {k.lower(): v for k, v in request.headers.items()}))
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"object": "list", "data": [], "has_more": False, "first_id": None, "last_id": None})
        if request.url.path.endswith("/messages"):
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=anthropic_sse())
        if not json.loads(request.content).get("stream"):
            return httpx2.Response(200, json={"id": "c", "object": "chat.completion", "created": 0, "model": "m", "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "fine"}}]})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=f"data: {json.dumps(CHUNK)}\n\ndata: [DONE]\n\n".encode())

    made = [lambda http: LLMClient("https://models.example/api/v1", "PROVIDER-KEY", http_client=http),
            lambda http: LLMClient("http://localhost:11434/v1", None, http_client=http),
            lambda http: AnthropicClient("https://compatible.example/anthropic", "PROVIDER-KEY", http_client=http),
            lambda http: AnthropicClient("https://api.anthropic.com", "PROVIDER-KEY", http_client=http),
            lambda http: AnthropicClient("http://localhost:8080", None, http_client=http)]
    for make in made:
        client = mocked(make, handler)
        await client.list_models()
        [e async for e in client.stream("m", USER)]
        assert await client.complete("m", USER) == "fine"
    assert len(got) == 3 * len(made)
    for url, headers in got:
        assert "env-base" not in url, url
        text = json.dumps(headers)
        assert "ENV" not in text and "env" not in text.replace("x-stainless", ""), (url, headers)
        for name in ("openai-organization", "openai-project", "x-env", "openai-beta"):
            assert name not in headers, (url, name)
        if "anthropic-version" in headers:                 # the Messages API: its key header, and no Authorization
            assert headers["x-api-key"] in ("PROVIDER-KEY", "not-needed") and "authorization" not in headers, url
            assert "env-beta" not in headers.get("anthropic-beta", "")
        else:
            assert headers["authorization"] in ("Bearer PROVIDER-KEY", "Bearer not-needed"), url


# ---------------------------------------------------------------- a server that isn't there

def test_connecting_has_a_timeout_of_its_own_beside_the_long_wait_for_a_reply():
    from datoolkit.llm.client import CONNECT_TIMEOUT

    assert CONNECT_TIMEOUT <= 15
    for client, read in ((LLMClient("http://192.168.1.50:11434/v1", None), 300),
                         (AnthropicClient("http://192.168.1.50:8080", None), 600),
                         (AnthropicClient("https://api.anthropic.com", "k"), 600)):
        timeout = client._client.timeout
        assert timeout.connect == CONNECT_TIMEOUT and timeout.read == read, client


@pytest.mark.parametrize("make", [lambda base: LLMClient(base + "/v1", None), lambda base: AnthropicClient(base, None)])
async def test_a_model_server_that_never_answers_the_connection_fails_soon(make, monkeypatch):
    """A machine that is switched off takes no connection and refuses none. Here: a listening
    socket that accepts nothing, its queue filled, so the next connection waits for ever."""
    import socket

    from datoolkit.llm import anthropic_api
    from datoolkit.llm import client as client_module

    monkeypatch.setattr(client_module, "CONNECT_TIMEOUT", 0.3)
    monkeypatch.setattr(anthropic_api, "CONNECT_TIMEOUT", 0.3)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(0)
    port = listener.getsockname()[1]
    held = []
    try:
        for _ in range(4):                       # more than the queue holds: these are never accepted
            s = socket.socket()
            s.setblocking(False)
            s.connect_ex(("127.0.0.1", port))
            held.append(s)
        client = make(f"http://127.0.0.1:{port}")
        client._client = client._client.with_options(max_retries=0)
        with pytest.raises(Exception, match="(?i)timed out|timeout"):
            await asyncio.wait_for(client.complete("m", USER), 5)       # 300 s and more without the connect timeout
    finally:
        for s in held + [listener]:
            s.close()


# ---------------------------------------------------------------- a reply that was cut off

def chunk(delta=None, finish=None, **more) -> dict:
    return {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
            "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **more}


def stream_of(*events, done=True) -> bytes:
    """An SSE body of chat completion chunks (dicts) or raw data lines (str), ended by [DONE] or not."""
    out = "".join("data: " + (e if isinstance(e, str) else json.dumps(e)) + "\n\n" for e in events)
    return (out + ("data: [DONE]\n\n" if done else "")).encode()


def answered_with(make, body: bytes):
    """`make(http_client)`'s client, every request answered with the stream `body`."""
    import httpx2

    return mocked(make, lambda request: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body))


def chat_stream(body: bytes, tools=None):
    """The events of an OpenAI-compatible stream `body`, as LLMClient yields them."""
    client = answered_with(lambda http: LLMClient("https://models.example/v1", "k", http_client=http), body)
    return client.stream("m", USER, tools=tools)


PROPOSE = [{"type": "function", "function": {"name": "propose_commands", "parameters": {"type": "object"}}}]
HALF_CALL = {"tool_calls": [{"index": 0, "id": "t1", "function": {
    "name": "propose_commands", "arguments": '{"items": [{"command": "systemctl restart ngin'}}]}


@pytest.mark.parametrize("events", [
    [chunk({"content": "Run this: rm -rf /var/lib/app/ca"})],                     # "...cache", cut in the middle of a path
    [chunk({"content": "I'll queue it."}), chunk(HALF_CALL)],
    [chunk({"role": "assistant"})]])
async def test_a_stream_that_ends_without_saying_it_is_done_is_an_error_not_a_reply(events):
    """No finish reason and no [DONE]: the connection closed mid-answer. Until Nov 2026 what had
    arrived was the reply: a command cut short, a tool call with half its arguments."""
    from datoolkit.llm.secure_http import ProviderError

    seen = []
    with pytest.raises(ProviderError, match="stopped before it was finished: the connection closed mid-answer") as err:
        async for event in chat_stream(stream_of(*events, done=False), PROPOSE):
            seen.append(event)
    assert not any(kind == "done" for kind, _ in seen), "no result is handed over"
    assert "models.example" in str(err.value) and "Retry" in str(err.value)


@pytest.mark.parametrize("finish", ["length", "content_filter", "stop"])
@pytest.mark.parametrize("done", [True, False])
async def test_a_reply_the_provider_ended_itself_reaches_the_caller_with_its_reason(finish, done):
    """Cut by the output limit or a filter, the reply is whole as far as it goes and says why it
    stopped: the engine marks it. Said by the finish reason, with or without a [DONE] after it."""
    body = stream_of(chunk({"content": "The fix is to set PermitRootLogin to"}, finish), done=done)
    events = [e async for e in chat_stream(body)]
    result = events[-1][1]
    assert events[-1][0] == "done" and result.finish_reason == finish
    assert result.content == "The fix is to set PermitRootLogin to"


async def test_a_turn_whose_reply_was_cut_off_queues_nothing_and_offers_retry(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("c", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    args = json.dumps({"items": [{"session_id": sid, "command": "df -h", "purpose": "space", "risk": "read_only"},
                                 {"session_id": sid, "command": "sudo systemctl restart rsyslog", "purpose": "x"}]})
    fake.responses.append(stream_of(
        chunk({"role": "assistant", "content": "Two checks, then the fix."}),
        chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                               "function": {"name": "propose_commands", "arguments": args[:len(args) - 30]}}]}),
        done=False).decode())
    engine.send("disk is full")
    await wait_turn(engine)
    assert not engine.queue.items
    note = engine.chat[-1]
    assert note["kind"] == "note" and note["retry"] is True and engine.can_retry
    assert note["text"].startswith("AI request ProviderError: the reply stopped before it was finished: "
                                   "the connection closed mid-answer")
    assert engine.chat[-2]["text"] == "Two checks, then the fix."           # what arrived stays shown, above the note


# the same on the Messages API

def anthropic_events(*events) -> bytes:
    def ev(kind, **data):
        return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"
    start = ev("message_start", message={"id": "m1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                                         "content": [], "stop_reason": None, "usage": {"input_tokens": 5, "output_tokens": 0}})
    return (start + "".join(ev(kind, **data) for kind, data in events)).encode()


TEXT_BLOCK = [("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
              ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "Two checks, then the fix."}}),
              ("content_block_stop", {"index": 0})]
WHOLE_INPUT = {"items": [{"session_id": "s1", "command": "df -h", "purpose": "space", "risk": "read_only"}]}
HALF_INPUT = ('{"items": [{"session_id": "s1", "command": "df -h", "purpose": "space", "risk": "read_only"}, '
              '{"session_id": "s1", "command": "sudo systemctl restart rsyslog", "purpose": "Release the fi')


def tool_block(index, tool_id, partial_json, closed=True):
    return [("content_block_start", {"index": index, "content_block": {"type": "tool_use", "id": tool_id,
                                                                       "name": "propose_commands", "input": {}}}),
            ("content_block_delta", {"index": index, "delta": {"type": "input_json_delta", "partial_json": partial_json}}),
            *([("content_block_stop", {"index": index})] if closed else [])]


def stopped(reason):
    return [("message_delta", {"delta": {"stop_reason": reason}, "usage": {"output_tokens": 9}}), ("message_stop", {})]


def messages_stream(body: bytes):
    """The events of a Messages API stream `body`, as AnthropicClient yields them."""
    client = answered_with(lambda http: AnthropicClient("https://api.anthropic.com", "k", http_client=http), body)
    return client.stream("claude-opus-5-5", [{"role": "system", "content": "s"}, *USER], PROPOSE)


@pytest.mark.parametrize("events", [
    TEXT_BLOCK + tool_block(1, "toolu_1", HALF_INPUT, closed=False),          # in the middle of a tool call
    TEXT_BLOCK[:2],                                                            # in the middle of the text
    TEXT_BLOCK + tool_block(1, "toolu_1", json.dumps(WHOLE_INPUT))])           # between blocks: still no stop reason
async def test_a_messages_stream_that_ends_without_a_stop_reason_is_an_error_not_a_reply(events):
    """The SDK hands back what had arrived as the message, a half-written call's input patched
    into valid JSON: its second command had lost its purpose, risk and rollback, and was queued."""
    from datoolkit.llm.secure_http import ProviderError

    seen = []
    with pytest.raises(ProviderError, match="stopped before it was finished: the connection closed mid-answer"):
        async for event in messages_stream(anthropic_events(*events)):
            seen.append(event)
    assert ("text", "Two checks, then the fix.") in seen and not any(kind == "done" for kind, _ in seen)


@pytest.mark.parametrize("reason", ["tool_use", "end_turn", "max_tokens"])
async def test_a_tool_call_still_being_written_is_never_returned_whatever_ended_the_stream(reason):
    """A stream may say it is done with a tool call's block never closed (a gateway that drops
    events, a limit reached as the input was written). The call before it is whole and stays."""
    body = anthropic_events(*TEXT_BLOCK, *tool_block(1, "toolu_1", json.dumps(WHOLE_INPUT)),
                            *tool_block(2, "toolu_2", HALF_INPUT, closed=False),
                            *stopped(reason))
    result = [e async for e in messages_stream(body)][-1][1]
    assert [(c.id, json.loads(c.arguments)) for c in result.tool_calls] == [("toolu_1", WHOLE_INPUT)]
    assert result.finish_reason == {"tool_use": "tool_calls", "end_turn": "stop", "max_tokens": "length"}[reason]


async def test_a_messages_reply_cut_by_the_output_limit_reaches_the_caller_as_length():
    result = [e async for e in messages_stream(anthropic_events(*TEXT_BLOCK, *stopped("max_tokens")))][-1][1]
    assert result.finish_reason == "length" and result.content == "Two checks, then the fix." and not result.tool_calls


async def test_a_stream_that_ends_in_the_middle_of_a_line_was_cut_off():
    from datoolkit.llm.secure_http import ProviderError

    whole = stream_of(chunk({"content": "The fix is"}), chunk({"content": " to restart it."}, "stop"))
    for body in (whole[:whole.index(b"restart")],                                       # mid-JSON, nothing after it
                 anthropic_events(*TEXT_BLOCK, *stopped("end_turn"))[:-60]):            # the Messages API likewise
        stream = chat_stream(body) if body.startswith(b"data:") else messages_stream(body)
        with pytest.raises(ProviderError, match="stopped before it was finished"):
            [e async for e in stream]


# ---------------------------------------------------------------- streams as servers really send them

async def chat_result(*events, done=True):
    return [e async for e in chat_stream(stream_of(*events, done=done), PROPOSE)][-1][1]


async def test_content_sent_as_a_list_of_parts_is_read():
    """Some servers send a delta's content as a request's may be written: a list of parts. It
    raised "can only concatenate str (not "list") to str"."""
    result = await chat_result(chunk({"content": [{"type": "text", "text": "Hello"}, {"type": "text", "text": ", world"}]}),
                               chunk({"content": [{"text": "."}, {"type": "image_url", "image_url": {"url": "x"}}, None, 7]}),
                               chunk({"content": " Done."}, "stop"))
    assert result.content == "Hello, world. Done." and result.finish_reason == "stop"


async def test_a_call_whose_id_changes_or_is_repeated_stays_one_call():
    args = '{"items": [{"command": "df -h"}]}'

    def piece(call_id, name, part, index=0):
        return chunk({"tool_calls": [{"index": index, "id": call_id, "function": {"name": name, "arguments": part}}]})

    # a new id with every piece (it came apart: one call with half its arguments, one with no name)
    result = await chat_result(piece("x1", "propose_commands", args[:9]), piece("x2", None, args[9:20]),
                               piece("x3", None, args[20:]), chunk({}, "tool_calls"))
    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [("x1", "propose_commands", args)]
    # ... and the whole name with it every time
    result = await chat_result(piece("x1", "propose_commands", args[:9]), piece("x2", "propose_commands", args[9:20]),
                               piece("x3", "propose_commands", args[20:]), chunk({}, "tool_calls"))
    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [("x1", "propose_commands", args)]
    # the same id on every piece, with an index and without one
    for index in (0, None):
        result = await chat_result(piece("same", "propose_commands", args[:9], index), piece("same", None, args[9:], index),
                                   chunk({}, "tool_calls"))
        assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [("same", "propose_commands", args)]
    # two calls a route numbered 0 both, each whole and with its own id and name: still two
    result = await chat_result(piece("a", "update_hypotheses", '{"items":[]}'), piece("b", "propose_commands", args),
                               chunk({}, "tool_calls"))
    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [("a", "update_hypotheses", '{"items":[]}'),
                                                                        ("b", "propose_commands", args)]


async def test_what_is_not_shaped_like_a_chunk_is_passed_over():
    """Each of these ended the turn with an AttributeError, some after the text had streamed."""
    for odd in ({"id": "c1", "choices": [], "usage": "n/a"}, {"id": "c1", "choices": [None]}, {"id": "c1", "choices": None},
                chunk({"tool_calls": [None]}), chunk({"tool_calls": {"index": 0}}), chunk({"tool_calls": "none"}),
                {"id": "c1", "choices": [{"index": 0, "delta": None, "finish_reason": None}]}, ""):
        result = await chat_result(chunk({"content": "h"}), odd, chunk({"content": "i"}, "stop"))
        assert result.content == "hi" and result.finish_reason == "stop" and not result.tool_calls, odd
    usage = {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}
    result = await chat_result(chunk({"content": "hi"}, "stop"), {"id": "c1", "choices": [], "usage": usage})
    assert result.usage["total_tokens"] == 4


async def test_a_line_that_is_not_json_in_the_middle_of_a_stream_is_said():
    from datoolkit.llm.secure_http import ProviderError

    with pytest.raises(ProviderError, match="sent a line in its stream that is not JSON"):
        await chat_result(chunk({"content": "Hello"}), "{oops", chunk({}, "stop"))


async def test_a_server_that_refuses_stream_options_is_asked_once_more_without_it():
    """The client asks for the token counts at the end of a stream (stream_options). A server
    that answers 400 to it could never be used: the engine can leave out a setting it chose
    (Engine._learn_unsupported), but not one the client adds itself."""
    import httpx2

    from datoolkit.llm import client as client_module

    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if "stream_options" in body:
            return httpx2.Response(400, json={"error": {"message": "Unrecognized request argument supplied: stream_options",
                                                        "type": "invalid_request_error"}})
        if body.get("temperature") is not None:
            return httpx2.Response(400, json={"error": {"message": "this model does not support temperature"}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=stream_of(chunk({"content": "fine"}, "stop")))

    def make():
        return mocked(lambda http: LLMClient("https://strict.example/v1", "k", http_client=http), handler)

    client_module._NO_STREAM_OPTIONS.discard("https://strict.example/v1/")
    events = [e async for e in make().stream("m", USER)]
    assert events[-1][1].content == "fine" and ["stream_options" in body for body in sent] == [True, False]
    events = [e async for e in make().stream("m", USER)]            # another client for the same server: not sent it again
    assert events[-1][1].content == "fine" and ["stream_options" in body for body in sent] == [True, False, False]
    # any other refusal is the caller's to read, as before (the engine learns it)
    with pytest.raises(Exception, match="does not support temperature"):
        [e async for e in make().stream("m", USER, params={"temperature": 0.3})]
    assert len(sent) == 4
    # and a server that takes it is sent it every time
    other = mocked(lambda http: LLMClient("https://models.example/v1", "k", http_client=http),
                   lambda request: sent.append(json.loads(request.content)) or httpx2.Response(
                       200, headers={"content-type": "text/event-stream"}, content=stream_of(chunk({"content": "ok"}, "stop"))))
    [e async for e in other.stream("m", USER)]
    assert sent[-1]["stream_options"] == {"include_usage": True}


async def test_a_turn_on_a_server_that_refuses_stream_options_is_answered(env):  # noqa: F811
    from datoolkit.llm import client as client_module

    engine, fake, _ = env
    client_module._NO_STREAM_OPTIONS.clear()
    engine.new_case("c", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    fake.refuse["anthropic/claude-opus-5.5"] = "stream_options"
    fake.responses.append(stream_of(chunk({"role": "assistant", "content": "Hello."}, "stop")).decode())
    try:
        engine.send("hi")
        await wait_turn(engine)
        assert engine.chat[-1]["kind"] == "assistant" and engine.chat[-1]["text"] == "Hello.", engine.chat[-1]
        assert ["stream_options" in body for body in fake.requests] == [True, False]
        assert not any(e["kind"] == "note" for e in engine.chat)        # nothing for the technician to do about it
    finally:
        client_module._NO_STREAM_OPTIONS.clear()
