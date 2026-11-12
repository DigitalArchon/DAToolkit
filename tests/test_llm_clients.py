"""The model clients on the wire (llm/client.py, llm/anthropic_api.py, and the smaller clients
built from a provider's address): what reaches a proxy the environment names, and what doesn't.

A stub proxy and a stub provider listen on loopback; nothing here leaves the machine."""

import asyncio
import json

import pytest

from datoolkit.llm import capabilities, research, routing, websearch
from datoolkit.llm.anthropic_api import AnthropicClient
from datoolkit.llm.client import LLMClient, local_http_options

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
