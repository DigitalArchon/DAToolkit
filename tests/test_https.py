"""https, or nothing (llm/netguard.py).

A plain http base URL used to be saved and used: the API key and the case went out
unencrypted. Now such an address is refused by Settings, and again at each place a request
leaves from, so a config.toml edited by hand sends nothing either. Plain http stays allowed
for this machine and its local network, which is how a model server of one's own is reached.
"""

import asyncio
import json

import httpx
import pytest

from datoolkit import creds
from datoolkit.config import Config, Provider
from datoolkit.engine import Engine, UserError
from datoolkit.llm import netguard
from datoolkit.llm.anthropic_api import AnthropicClient
from datoolkit.llm.client import LLMClient
from datoolkit.llm.secure_http import ProviderError

from test_engine import env  # noqa: F401


def test_https_is_accepted_anywhere():
    for url in ("https://nano-gpt.com/api/v1", "https://api.example.com:8443/v1", "HTTPS://Example.com/x",
                "https://192.0.2.7/v1", "  https://nano-gpt.com/api  "):
        assert netguard.problem(url) is None, url


def test_plain_http_is_accepted_only_on_this_machine_and_its_network():
    for url in ("http://localhost:11434/v1", "http://LOCALHOST/v1", "http://127.0.0.1:8080/v1",
                "http://127.8.9.10/v1", "http://[::1]:1234/v1", "http://10.0.0.5/v1", "http://172.16.0.1/v1",
                "http://172.31.255.254/v1", "http://192.168.1.20:5000/v1", "http://169.254.10.10/v1",
                "http://[fe80::1]/v1", "http://[fd12:3456:789a::1]/v1", "http://[fc00::1]/v1",
                "http://ollama.local:11434/v1", "http://box.localhost/v1"):
        assert netguard.problem(url) is None, url


def test_plain_http_to_anywhere_else_is_refused_with_the_reason():
    for url in ("http://api.example.com/v1", "http://nano-gpt.com/api/v1", "http://8.8.8.8/v1",
                "http://172.15.0.1/v1", "http://172.32.0.1/v1", "http://192.169.0.1/v1", "http://11.0.0.1/v1",
                "http://169.255.0.1/v1", "http://[2001:db8::1]/v1", "http://[fec0::1]/v1",
                # ranges Python calls "private" that are not the local network
                "http://192.0.2.1/v1", "http://198.18.0.1/v1", "http://100.64.0.1/v1",
                # names that only look local (a .lan name is reached over https only)
                "http://localhost.example.com/v1", "http://local/v1", "http://10.0.0.5.example.com/v1",
                "http://mylocal/v1", "http://ollama.lan:11434/v1"):
        found = netguard.problem(url)
        assert found and "not https" in found and "https://" in found, url


def test_an_ipv4_address_dressed_as_ipv6_is_judged_as_what_it_is():
    assert netguard.problem("http://[::ffff:192.168.1.5]/v1") is None
    assert netguard.problem("http://[::ffff:8.8.8.8]/v1") is not None


def test_an_escape_in_the_host_is_not_a_way_round():
    """An address cut at its "%" read as local, though httpx asks DNS for the name whole: sent
    over plain http, with the key and the case, to a name anyone can register under evil.com.
    (DA Assistant's and SealedLore's tests of the same rule.)"""
    import httpx

    from datoolkit.config import Host
    from datoolkit.llm.client import is_local_url
    from datoolkit.sessions import winrm

    assert httpx.URL("http://127.0.0.1%2e.evil.com/v1").host == "127.0.0.1%2e.evil.com"   # what DNS is asked
    for url in ("http://127.0.0.1%2eevil.com/v1", "http://127.0.0.1%2e.evil.com/v1",
                "http://192.168.1.10%2Eattacker.example:11434/v1", "http://10.0.0.1%2Fx.evil.com/v1",
                "http://127.0.0.1%25.evil.com/", "http://192.168.1.1%.attacker.example/v1",
                "http://localhost%2eevil.com/", "http://evil%2ecom.local/", "http://printer%2f.local/v1"):
        assert netguard.problem(url) is not None, url
        assert not is_local_url(url), url
    # A zone is the one "%" an address may have, and https is judged by its scheme.
    assert netguard.problem("http://[fe80::1%25eth0]:11434/v1") is None
    assert netguard.is_local("fe80::1%eth0") and netguard.is_local("studio.local.") and netguard.is_local("dev.localhost")
    assert netguard.problem("https://127.0.0.1%2eexample.com/v1") is None
    # The Local tier and WinRM's plain HTTP are judged the same way.
    assert is_local_url("http://[fe80::1%25eth0]:11434/v1") and is_local_url("https://ollama.lan/v1")
    assert not is_local_url("https://box%2f.lan/v1")
    assert winrm.problem(Host("dc", "winrm", "10.0.0.5%2e.evil.com", auth="ntlm", winrm_ssl=False)) is not None
    assert winrm.problem(Host("dc", "winrm", "10.0.0.5", auth="ntlm", winrm_ssl=False)) is None


def test_what_is_not_a_web_address_at_all_is_refused():
    for url in ("", "   ", None, "nano-gpt.com/api", "ftp://example.com/x", "file:///etc/passwd", "http://",
                "https://", "javascript:alert(1)"):
        assert netguard.problem(url) is not None, url
    with pytest.raises(netguard.InsecureURL):
        netguard.check("http://api.example.com/v1")
    assert netguard.check(" https://api.example.com/v1 ") == "https://api.example.com/v1"


def test_an_address_no_parser_can_read_is_nobodys_and_not_local():
    """Half typed, or edited by hand in config.toml: asking for such a provider's tier or kind
    raised ValueError ("Invalid IPv6 URL"), wherever the provider list is shown."""
    from datoolkit.llm import websearch
    from datoolkit.llm.client import detect_tier, is_local_url, local_http_options
    from datoolkit.llm.providers import describe, is_official_anthropic, kind_of

    for url in ("http://[::1", "https://[fd00::5", "https://[not-an-address]/v1"):
        assert netguard.problem(url) is not None, url                    # refused, with the reason
        assert is_local_url(url) is False, url
        assert detect_tier("llama3", url) == "standard", url             # never the Local tier
        assert kind_of(url) == "openai-compatible" and kind_of(url, "anthropic") == "anthropic-compatible", url
        assert describe(url)["kind"] == "openai-compatible" and not is_official_anthropic(url), url
        assert websearch.is_nanogpt(url) is False and local_http_options(url) == {}, url
    assert is_local_url("http://[::1]:11434/v1") and kind_of("http://[::1]:11434/v1") == "local"


def engine_with(tmp_path, *providers):
    engine = Engine(Config(providers=list(providers)), lambda e: None, tmp_path / "rt", save_config=lambda c: None)
    for p in providers:
        creds.set_secret("provider", p.name, "sk-test")
    return engine


async def test_settings_will_not_save_a_plain_http_address_off_the_local_network(tmp_path):
    engine = engine_with(tmp_path)
    with pytest.raises(UserError, match="not saved.*not https"):
        engine.save_provider({"name": "Elsewhere", "base_url": "http://api.example.com/v1"})
    assert engine.cfg.providers == []
    for url in ("https://api.example.com/v1", "http://localhost:11434/v1", "http://10.0.0.5:8000/v1",
                "http://ollama.local:11434/v1", "training://disk-full"):
        engine.save_provider({"name": url, "base_url": url})
    assert [p.base_url for p in engine.cfg.providers] == [
        "https://api.example.com/v1", "http://localhost:11434/v1", "http://10.0.0.5:8000/v1",
        "http://ollama.local:11434/v1", "training://disk-full"]


async def test_a_config_edited_by_hand_to_hold_one_sends_nothing(tmp_path):
    """Checked again where each request leaves from: the chat, the model list, a TEE model's
    attestation, web search and research."""
    engine = engine_with(tmp_path, Provider("Elsewhere", "http://api.example.com/v1"),
                         Provider("NanoGPT", "http://nano-gpt.com/api/v1"))
    sent = []

    class Never:                       # stands in for the HTTP clients: records any attempt to use one
        def __getattr__(self, name):
            return lambda *a, **k: sent.append(name)

    engine._search_http = engine._caps_http = Never()
    await engine.start()
    try:
        with pytest.raises(UserError, match="Could not list models.*not https"):
            await engine.list_models("Elsewhere")
        engine.new_case("hand-edited", "open")
        engine.cfg.active_provider, engine.cfg.active_model = "Elsewhere", "any/model"
        engine.send("hello")
        for _ in range(200):
            if not engine.busy:
                break
            await asyncio.sleep(0.01)
        assert engine.chat[-1]["kind"] == "note" and "not sent" in engine.chat[-1]["text"]
        assert "not https" in engine.chat[-1]["text"] and engine.can_retry

        engine.cfg.active_model = "TEE/glm-5.3"
        att = await engine.attest_now()
        assert att["status"] == "failed" and "not https" in att["error"]

        with pytest.raises(UserError, match="not https"):
            await engine._run_search("anything")
        with pytest.raises(UserError, match="not https"):
            engine._research_tools({})
    finally:
        await engine.stop()
    assert sent == [], "no request left the machine"


@pytest.mark.parametrize("make", [lambda: LLMClient("https://llm.example/v1", "sk-x"),
                                  lambda: AnthropicClient("https://api.example/v1", "sk-x")])
async def test_a_model_endpoint_redirecting_elsewhere_is_not_followed(make):
    # The SDKs' own HTTP clients follow redirects: an https endpoint answering 307 to a plain
    # http address got the whole request again, case and key (x-api-key) included.
    httpx2 = pytest.importorskip("httpx2")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx2.Response(307, headers={"location": "http://elsewhere.example/collect"})

    client = make()
    client._client._client._transport = httpx2.MockTransport(handler)    # the client's default one
    client._client._client._mounts = {}
    with pytest.raises(Exception, match="307"):
        await client.complete("m", [{"role": "user", "content": "the case"}])
    assert seen and all(url.startswith("https://") for url in seen)


# --- a redirect, or an answer with no stream in it (SealedLore e947938, Nov 11 2026) ---------------
# The SDKs said "Error code: 308" of a redirect; a web page answering 200 where the API was expected
# ended a turn with nothing at all (the OpenAI SDK read it as a stream with no events), an Anthropic
# turn with an empty AssertionError, and a write-up with "'str' object has no attribute 'choices'".

CLIENTS = [lambda base: LLMClient(base, "sk-x"), lambda base: AnthropicClient(base, "sk-x")]


def answering(make, handler, base="https://llm.example/v1"):
    """`make`'s client with its own default HTTP client (which follows no redirect), answered by
    `handler`."""
    httpx2 = pytest.importorskip("httpx2")
    client = make(base)
    client._client._client._transport = httpx2.MockTransport(handler)
    client._client._client._mounts = {}
    return client


async def streamed_by(client) -> list:
    return [e async for e in client.stream("m", [{"role": "user", "content": "the case"}])]


@pytest.mark.parametrize("make", CLIENTS)
@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_a_redirect_is_said_with_where_to_and_never_taken_for_an_empty_reply(make, status):
    httpx2 = pytest.importorskip("httpx2")
    seen: list = []

    def handler(request):
        seen.append(request)
        return httpx2.Response(status, headers={"location": "https://apex.example/v1/chat/x"})

    for call in (streamed_by, lambda c: c.complete("m", [{"role": "user", "content": "the case"}])):
        seen.clear()
        with pytest.raises(ProviderError) as info:
            await call(answering(make, handler))
        said = str(info.value)
        assert f"redirected (HTTP {status}) to https://apex.example/v1/chat/x" in said
        assert "Nothing was sent there" in said and "Settings → AI providers" in said
        assert info.value.status_code == status
        assert len(seen) == 1, "nothing was followed, nor retried"


@pytest.mark.parametrize("make", CLIENTS)
async def test_a_relative_redirect_names_the_whole_address_without_a_password(make):
    httpx2 = pytest.importorskip("httpx2")
    client = answering(make, lambda r: httpx2.Response(308, headers={"location": "/v2/chat"}),
                       base="https://bob:hunter2@llm.example/v1")
    with pytest.raises(ProviderError) as info:
        await streamed_by(client)
    assert "redirected (HTTP 308) to https://…@llm.example/v2/chat" in str(info.value)
    assert "hunter2" not in str(info.value) and "bob" not in str(info.value)


@pytest.mark.parametrize("make", CLIENTS)
@pytest.mark.parametrize(("body", "kind"), [
    (b"<html><body>Sign in to the Wi-Fi</body></html>", "text/html"),
    (b"", "text/event-stream"),
    (b'{"id": "x", "choices": []}', "application/json"),
])
async def test_an_answer_with_no_stream_in_it_is_said_never_an_empty_reply(make, body, kind):
    httpx2 = pytest.importorskip("httpx2")
    client = answering(make, lambda r: httpx2.Response(200, content=body, headers={"content-type": kind}))
    with pytest.raises(ProviderError) as info:
        await streamed_by(client)
    said = str(info.value)
    assert f"answered HTTP 200 ({kind}) with no reply in it" in said and "Settings → AI providers" in said


@pytest.mark.parametrize(("body", "kind"), [
    (b"<html><body>Sign in to the Wi-Fi</body></html>", "text/html"),
    (b'{"id": "x", "choices": []}', "application/json"),
    (b'{"error": "upstream unavailable"}', "application/json"),
])
async def test_a_completion_with_no_reply_in_it_is_said(body, kind):
    """A write-up, a second opinion or an image's description, asked for without a stream."""
    httpx2 = pytest.importorskip("httpx2")
    client = answering(CLIENTS[0], lambda r: httpx2.Response(200, content=body, headers={"content-type": kind}))
    with pytest.raises(ProviderError, match=rf"answered HTTP 200 \({kind}\) with no reply in it, not the reply"):
        await client.complete("m", [{"role": "user", "content": "the case"}])
    done = {"id": "c", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "fine"}}]}
    ok = answering(CLIENTS[0], lambda r: httpx2.Response(200, json=done))
    assert await ok.complete("m", [{"role": "user", "content": "the case"}]) == "fine"


async def test_a_stream_is_read_whatever_its_content_type_says():
    """Real servers label their streams every way (llama.cpp, proxies): a body with `data:` lines is
    the reply. One that says nothing but [DONE] is an empty reply, as the endpoint sent it."""
    httpx2 = pytest.importorskip("httpx2")
    chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
             "choices": [{"index": 0, "delta": {"content": "fine"}, "finish_reason": "stop"}]}
    stream = f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode()
    for kind in ("text/html", "application/json", "text/plain", None):
        client = answering(CLIENTS[0], lambda r, kind=kind: httpx2.Response(
            200, content=stream, headers={"content-type": kind} if kind else {}))
        events = await streamed_by(client)
        assert "".join(v for k, v in events if k == "text") == "fine", kind
        assert events[-1][1].finish_reason == "stop" and events[-1][1].body_hash is None, "no hash unless asked"
    done = answering(CLIENTS[0], lambda r: httpx2.Response(200, content=b"data: [DONE]\n\n",
                                                          headers={"content-type": "text/event-stream"}))
    events = await streamed_by(done)
    assert events[-1][0] == "done" and events[-1][1].content == ""


async def test_a_turn_answered_by_a_web_page_says_so(env):  # noqa: F811
    """It ended with the technician's message and nothing after it: no reply, no error, no Retry."""
    engine, fake, _ = env

    def page(request):
        if request.url.path.endswith("/models"):
            return fake.handler(request)
        return httpx.Response(200, text="<html><body>Sign in</body></html>", headers={"content-type": "text/html"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(page))
    engine._client = lambda prov, model="": LLMClient(prov.base_url, "sk-test", http)
    engine.new_case("p", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.send("VPN is down")
    for _ in range(200):
        if not engine.busy:
            break
        await asyncio.sleep(0.01)
    note = engine.chat[-1]
    assert note["kind"] == "note" and note.get("retry") and engine.can_retry
    assert "answered HTTP 200 (text/html) with no reply in it" in note["text"]
    assert "https://fake.example/api/v1/chat/completions" in note["text"]


# --- the attestation client's redirects (llm/secure_http.py) ---------------------------------
# From SealedLore's tests of the same hook (providers/http.py, b829e91).


def _redirecting(handler, base="https://api.test/v1"):
    from datoolkit.llm.secure_http import make_client

    return make_client(base, transport=httpx.MockTransport(handler), follow_redirects=True)


def test_a_redirect_off_https_is_refused_before_anything_is_sent_again():
    from datoolkit.llm.secure_http import ProviderError

    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        if len(seen) == 1:
            return httpx.Response(307, headers={"location": "http://elsewhere.test/collect"})
        return httpx.Response(200, json={})

    with _redirecting(handler) as client, pytest.raises(ProviderError, match="insecure"):
        client.get("https://api.test/v1/tee/attestation")
    assert seen == ["https://api.test/v1/tee/attestation"]


def test_a_redirect_that_stays_on_https_is_followed_and_a_relative_one_too():
    """httpx runs response hooks before it resolves `Location`: read raw, a relative one
    ("/v2/x") had no scheme and every one was refused."""
    for location, then in (("https://api.test/v2/x", "https://api.test/v2/x"), ("/v2/x", "https://api.test/v2/x")):
        seen: list[str] = []

        def handler(request, location=location, seen=seen):
            seen.append(str(request.url))
            return httpx.Response(308, headers={"location": location}) if len(seen) == 1 else httpx.Response(200, json={})

        with _redirecting(handler) as client:
            assert client.get("https://api.test/v1/x").status_code == 200
        assert seen == ["https://api.test/v1/x", then], location


@pytest.mark.parametrize("location", ["https://192.168.1.1/admin", "http://192.168.1.1/admin",
                                      "http://localhost:8080/x", "https://[::1]:8443/x"])
def test_a_server_on_the_internet_cannot_redirect_into_the_local_network(location):
    from datoolkit.llm.secure_http import ProviderError

    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": location})

    with _redirecting(handler) as client, pytest.raises(ProviderError, match="(local network|insecure)"):
        client.get("https://api.test/v1/x")
    assert len(seen) == 1, "the redirect was not followed"


def test_a_server_on_this_computer_may_redirect_on_it():
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "http://localhost:9000/v1/x"}) if len(seen) == 1 \
            else httpx.Response(200, json={})

    with _redirecting(handler, "http://127.0.0.1:8080/v1") as client:
        assert client.get("http://127.0.0.1:8080/v1/x").status_code == 200
    assert seen[-1] == "http://localhost:9000/v1/x"


# --- an API key no HTTP header can carry (netguard.key_problem) ------------------------------
# From DA Assistant's tests of the same check: h11 refused such a key in words that quoted it
# whole ("Illegal header value b'Bearer sk-…'"), and those were shown in the chat and logged.

BAD_KEYS = ("sk-live-ABCDEF\nGHIJKL", "sk-live-ABCDEF\u200bGHIJKL", "sk-live ABCDEF", "sk-live-ABCDEF\r\nX-Evil: 1")


def test_a_key_no_header_can_carry_is_refused_without_quoting_it():
    import h11

    with pytest.raises(h11.LocalProtocolError, match="ABCDEF"):         # why: the library's own words
        h11.Request(method="GET", target="/", headers=[("Host", "x"), ("Authorization", f"Bearer {BAD_KEYS[0]}")])
    for key in BAD_KEYS:
        found = netguard.key_problem(key, "NanoGPT")
        assert "Enter it again" in found and "ABCDEF" not in found, repr(key)
    assert netguard.key_problem("sk-live-ABCDEF_GHIJKL-0123") is None
    assert netguard.key_problem("") is None and netguard.key_problem(None) is None


async def test_settings_refuse_such_a_key_and_nothing_is_built_with_one_stored_before(tmp_path):
    engine = engine_with(tmp_path, Provider("NanoGPT", "https://nano-gpt.com/api/v1"))
    for key in BAD_KEYS:
        with pytest.raises(UserError, match="Enter it again") as refused:
            engine.save_provider({"name": "NanoGPT", "base_url": "https://nano-gpt.com/api/v1"}, key, "NanoGPT")
        assert "ABCDEF" not in str(refused.value)
    assert creds.get_secret("provider", "NanoGPT") == "sk-test"           # left as it was
    # one an older version stored: no client is made with it, nor a TEE attestation, nor a search
    creds.set_secret("provider", "NanoGPT", BAD_KEYS[0])
    prov = engine.cfg.provider("NanoGPT")
    for make in (lambda: engine._client(prov, "z-ai/glm-5.3"), lambda: engine._client(prov, "private/glm-5-3")):
        with pytest.raises(UserError, match="Enter it again") as refused:
            make()
        assert "ABCDEF" not in str(refused.value)
    with pytest.raises(UserError, match="Enter it again"):
        await engine._attest_tee(prov, "TEE/glm-5.3")
    with pytest.raises(UserError, match="Enter it again"):
        await engine._run_search("anything")
