"""NanoGPT Private Mode protocol, against a fake relay and a fake attestation verifier."""

import hashlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from datoolkit.llm.client import LLMClient, detect_tier
from datoolkit.llm.private_mode import (
    Attestation, Enclave, ModelRetired, PrivateModeClient, PrivateModeError, Verified, _NonEmptyBodyTransport,
    offered, offers_private_mode, relay_url, sealing_transport)
from datoolkit.llm.prompts import PROPOSE_TOOL

BASE = "https://nano-gpt.com/api/v1"
RELAY = "https://nano-gpt.com/api/v1/private/tinfoil"
KEY = "ab" * 32
SCOPE = "c" * 64


def verified(key=KEY):
    return Verified(key, "enclave.tinfoil.example", Attestation(
        enclave="enclave.tinfoil.example", hardware="AMD SEV-SNP", measurement="m", release_digest="d" * 64,
        release_tag="v1.2.3", latest_release="", latest_checked=True,
        hpke_key_sha256=hashlib.sha256(key.encode()).hexdigest(), verified_at=0))


def chunk(delta, finish=None):
    return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


TOOL_REPLY = (chunk({"role": "assistant", "content": "Checking."})
              + chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {
                  "name": "propose_commands",
                  "arguments": json.dumps({"items": [{"session_id": "local", "command": "df -h",
                                                      "purpose": "disk", "risk": "read_only"}]})}}]})
              + chunk({}, "tool_calls") + "data: [DONE]\n\n")


LISTED = ["private/gemma4-31b:thinking", "private/glm-5-3", "private/kimi-k3"]   # as the relay lists them (sorted)


class Relay:
    def __init__(self, replies=None, private_header="tinfoil", listed=LISTED, preflight_status=200):
        self.replies = list(replies or [(200, TOOL_REPLY)])
        self.private_header = private_header
        self.listed = listed                    # the relay's public list of Private Mode models
        self.preflight_status = preflight_status
        self.preflights, self.requests, self.keys, self.verifies, self.lists = [], [], [], 0, 0

    def verifier(self, relay, secret):
        assert relay == RELAY and len(secret) == 64
        self.verifies += 1
        return verified()

    def preflight(self, request):
        if request.url.path.endswith("/private/tinfoil/models"):
            assert request.method == "GET" and "authorization" not in request.headers   # public: no key sent
            self.lists += 1
            if self.listed is None:
                return httpx.Response(503, text="down")
            return httpx.Response(200, json={"data": [{"id": m} for m in self.listed] + [{"id": "TEE/glm-5.3"}]})
        assert request.url.path.endswith("/private/tinfoil/preflight")
        self.preflights.append((json.loads(request.content), request.headers["authorization"]))
        if self.preflight_status >= 400:
            return httpx.Response(self.preflight_status, json={"error": {"message": "nope"}})
        return httpx.Response(200, json={"cacheScope": SCOPE, "upstreamModel": "glm-5-3"})

    def transport(self, key):
        self.keys.append(key)

        def handle(request):
            assert request.url.path.endswith("/private/tinfoil/v1/chat/completions")
            self.requests.append((json.loads(request.content), dict(request.headers)))
            status, body = self.replies.pop(0)
            headers = {"content-type": "text/event-stream"}
            if self.private_header:
                headers["x-nanogpt-private-mode"] = self.private_header
            return httpx.Response(status, text=body, headers=headers)
        return httpx.MockTransport(handle)

    def client(self, enclave=None):
        enclave = enclave or Enclave(RELAY, verifier=self.verifier)
        return PrivateModeClient(BASE, "sk-test", enclave, transport_factory=self.transport,
                                 plain_http=httpx.AsyncClient(transport=httpx.MockTransport(self.preflight)))


async def collect(client, model="private/glm-5-3"):
    events = []
    async for kind, val in client.stream(model, [{"role": "user", "content": "disk full?"}], [PROPOSE_TOOL]):
        events.append((kind, val))
    return events


def test_detection():
    assert offers_private_mode(BASE) and offers_private_mode("https://nano-gpt.com/api/v1")
    assert not offers_private_mode("https://api.openai.com/v1")
    assert relay_url(BASE) == RELAY
    assert detect_tier("private/glm-5-3", BASE) == "e2ee"
    assert detect_tier("TEE/glm-5.3", BASE) == "tee"


async def test_sealed_request_protocol():
    relay = Relay()
    client = relay.client()
    events = await collect(client)
    result = events[-1][1]
    assert events[0] == ("text", "Checking.")
    assert result.tool_calls[0].name == "propose_commands"
    assert json.loads(result.tool_calls[0].arguments)["items"][0]["command"] == "df -h"

    assert relay.verifies == 1 and relay.keys == [KEY]           # sealed to the attested key
    pre, auth = relay.preflights[0]
    assert pre["model"] == "private/glm-5-3" and pre["requestBodyBytes"] > 0 and auth == "Bearer sk-test"
    body, headers = relay.requests[0]
    assert body["model"] == "glm-5-3"                            # upstream name from preflight
    assert body["tools"][0]["function"]["name"] == "propose_commands" and body["tool_choice"] == "auto"
    assert body["user_cache_secret"] == client.enclave.cache_secret  # our own secret, inside the seal
    assert body["chat_template_kwargs"] == {"thinking": True}
    assert body["stream_options"] == {"include_usage": True}
    assert headers["x-nanogpt-private-cache-scope"] == hashlib.sha256(SCOPE.encode()).hexdigest()
    assert headers["x-tinfoil-enclave-url"] == "https://enclave.tinfoil.example"
    assert headers["x-nanogpt-private-model"] == "private/glm-5-3"


async def test_refuses_reply_not_marked_private():
    relay = Relay(private_header=None)
    with pytest.raises(PrivateModeError, match="didn't come back through Private Mode"):
        await collect(relay.client())


async def test_refuses_truncated_stream():
    relay = Relay(replies=[(200, chunk({"role": "assistant", "content": "partial"}))])
    with pytest.raises(PrivateModeError, match="ended before it was complete"):
        await collect(relay.client())


async def test_reattests_and_retries_once_on_missing_seal():
    relay = Relay(replies=[(400, '{"error":"missing_encrypted_body_header"}'), (200, TOOL_REPLY)])
    await collect(relay.client())
    assert relay.verifies == 2 and len(relay.requests) == 2


async def test_failed_attestation_sends_nothing():
    relay = Relay()

    def bad_verifier(relay_, secret):
        raise RuntimeError("measurement mismatch")

    with pytest.raises(PrivateModeError, match="couldn't be attested.*nothing was sent"):
        await collect(relay.client(Enclave(RELAY, verifier=bad_verifier)))
    assert relay.preflights == [] and relay.requests == []


async def test_attestation_cached_then_expires():
    relay = Relay(replies=[(200, TOOL_REPLY)] * 3)
    now = [1000.0]
    enclave = Enclave(RELAY, verifier=relay.verifier, clock=lambda: now[0])
    client = relay.client(enclave)
    await collect(client)
    await collect(client)
    assert relay.verifies == 1
    now[0] += 301
    await collect(client)
    assert relay.verifies == 2


async def test_plain_client_refuses_private_models():
    with pytest.raises(RuntimeError, match="only ever sent sealed"):
        async for _ in LLMClient(BASE, "sk").stream("private/glm-5-3", []):
            pass
    with pytest.raises(PrivateModeError):
        await collect(Relay().client(), model="TEE/glm-5.3")


async def test_real_ehbp_seals_body_and_guard_blocks_unsealed():
    priv = X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    seen = {}

    class Inner(_NonEmptyBodyTransport):
        async def handle_async_request(self, request):
            from ehbp.protocol import ENCAPSULATED_KEY_HEADER
            if ENCAPSULATED_KEY_HEADER not in request.headers:
                raise PrivateModeError("refusing to send an unsealed request to the enclave")
            seen["body"] = await request.aread()
            seen["headers"] = request.headers
            return httpx.Response(500, text="stop here")

    from ehbp import AsyncEHBPTransport
    transport = AsyncEHBPTransport.from_public_key_hex(pub, inner=Inner())
    async with httpx.AsyncClient(transport=transport) as c:
        await c.post(RELAY + "/v1/chat/completions", json={"messages": [{"role": "user", "content": "SECRET-PROMPT"}]})
    assert b"SECRET-PROMPT" not in seen["body"] and len(seen["body"]) > 20
    assert "ehbp-encapsulated-key" in seen["headers"]

    # the production transport's inner layer refuses anything that isn't sealed
    guard = _NonEmptyBodyTransport()
    with pytest.raises(PrivateModeError, match="unsealed"):
        await guard.handle_async_request(httpx.Request("POST", RELAY + "/v1/chat/completions", content=b"{}"))
    assert isinstance(sealing_transport(pub), AsyncEHBPTransport)


async def test_engine_routes_private_model_sealed_and_logs_attestation(tmp_path, monkeypatch):
    import asyncio

    from datoolkit import creds
    from datoolkit.config import Config, Provider
    from datoolkit.engine import Engine

    relay = Relay()
    cfg = Config(providers=[Provider("NanoGPT", BASE)])
    engine = Engine(cfg, lambda e: None, tmp_path / "rt", save_config=lambda c: None)
    creds.set_secret("provider", "NanoGPT", "sk-test")
    enclave = Enclave(RELAY, verifier=relay.verifier)
    real_client = engine._client

    def client(prov, model=""):
        return relay.client(enclave) if model.startswith("private/") else real_client(prov, model)

    monkeypatch.setattr(engine, "_client", client)
    await engine.start()
    try:
        engine.new_case("client X", "confidential")
        engine.select_model("NanoGPT", "private/glm-5-3")
        engine.send("fs01 is slow")
        for _ in range(200):
            if not engine.busy and engine.attestation.get("status") == "verified":
                break
            await asyncio.sleep(0.01)
        assert engine.attestation["status"] == "verified"
        assert engine.chat[-1]["tier"] == "e2ee" and "runs tinfoilsh" in engine.chat[-1]["sealed"]
        assert [p.command for p in engine.queue.items] == ["df -h"]
        log = (engine.case.dir / "events.jsonl").read_text()
        assert "enclave_attested" in log and KEY not in log  # key fingerprint only, never the key
    finally:
        await engine.stop()


def test_a_model_off_the_relays_list_is_known_to_be_retired():
    assert offered("private/glm-5-3", LISTED) is True
    assert offered("private/glm-5.3", LISTED) is True, "NanoGPT's dotted alias"
    assert offered("private/glm-5-3-flash", LISTED) is False
    assert offered("private/gemma4-31b", LISTED) is False, ":thinking is its own model"
    assert offered("private/glm-5-3-flash", []) is None, "a list that couldn't be read"


async def test_nothing_is_sent_to_a_retired_model():
    relay = Relay()
    client = relay.client()
    with pytest.raises(ModelRetired, match="no longer offered.*choose another end-to-end encrypted model") as caught:
        await collect(client, model="private/glm-5-3-flash")
    assert caught.value.model == "private/glm-5-3-flash"
    assert caught.value.offered == LISTED                          # what to carry on with (private/ only)
    assert relay.lists == 1 and relay.verifies == 0 and relay.preflights == [] and relay.requests == []
    with pytest.raises(ModelRetired):
        await client.attest("private/glm-5-3-flash")               # not attested either
    assert relay.verifies == 0
    assert await client.attest("private/glm-5-3") == verified().attestation
    assert relay.lists == 1, "the list is kept, like the attestation"


async def test_the_private_list_is_read_again_once_it_is_old():
    relay = Relay(replies=[(200, TOOL_REPLY)] * 4)
    now = [1000.0]
    client = relay.client(Enclave(RELAY, verifier=relay.verifier, clock=lambda: now[0]))
    await collect(client)
    await collect(client)
    assert relay.lists == 1
    now[0] += 301
    await collect(client)
    assert relay.lists == 2
    relay.listed = ["private/kimi-k3"]                              # retired meanwhile: found at the next read
    await collect(client)
    now[0] += 301
    with pytest.raises(ModelRetired):
        await collect(client)


async def test_a_preflight_refusal_for_a_retired_model_says_so():
    """Every path's backstop: a model retired since the list was read."""
    relay = Relay(preflight_status=400)
    client = relay.client()
    with pytest.raises(PrivateModeError, match=r"refused the Private Mode request \(HTTP 400\)") as plain:
        await collect(client)                                       # still listed: NanoGPT's own refusal
    assert not isinstance(plain.value, ModelRetired)
    relay.listed = ["private/kimi-k3"]                              # gone, but the kept list still has it
    with pytest.raises(ModelRetired) as caught:
        await collect(client)
    assert caught.value.status_code == 400 and caught.value.offered == ["private/kimi-k3"]
    assert relay.requests == [], "nothing sealed was sent"
    # The list can't be read: nothing is claimed about the model.
    down = Relay(preflight_status=400, listed=None)
    with pytest.raises(PrivateModeError) as unknown:
        await collect(down.client())
    assert not isinstance(unknown.value, ModelRetired) and down.lists == 2


async def test_engine_says_a_private_model_is_retired_and_carries_on_with_another(tmp_path, monkeypatch):
    import asyncio

    from datoolkit import creds
    from datoolkit.config import Config, Provider
    from datoolkit.engine import Engine

    relay = Relay(replies=[(200, TOOL_REPLY)] * 2)
    cfg = Config(providers=[Provider("NanoGPT", BASE)])
    engine = Engine(cfg, lambda e: None, tmp_path / "rt", save_config=lambda c: None)
    creds.set_secret("provider", "NanoGPT", "sk-test")
    enclave = Enclave(RELAY, verifier=relay.verifier)
    real_client = engine._client
    monkeypatch.setattr(engine, "_client",
                        lambda prov, model="": relay.client(enclave) if model.startswith("private/") else real_client(prov, model))
    await engine.start()
    try:
        engine.new_case("client X", "confidential")
        engine.select_model("NanoGPT", "private/glm-5-3-flash")
        for _ in range(200):
            if engine.attestation.get("status") != "checking":
                break
            await asyncio.sleep(0.01)
        assert engine.attestation["status"] == "retired" and engine.attestation["offered"] == LISTED
        assert "no longer offered" in engine.attestation["error"] and relay.verifies == 0
        assert (await engine.attest_now())["status"] == "retired"

        engine.send("fs01 is slow")
        for _ in range(200):
            if not engine.busy:
                break
            await asyncio.sleep(0.01)
        assert relay.preflights == [] and relay.requests == [], "nothing was sent"
        note = engine.chat[-1]
        assert note["kind"] == "note" and note["retry"] and "not sent" in note["text"] and "no longer offered" in note["text"]
        assert note["retired"] == {"model": "private/glm-5-3-flash", "offered": LISTED}
        assert engine.can_retry
        log = (engine.case.dir / "events.jsonl").read_text()
        assert "private_model_retired" in log

        engine.select_model("NanoGPT", "private/glm-5-3")          # "Ask again with it"
        engine.retry()
        for _ in range(300):
            if not engine.busy and engine.attestation.get("status") == "verified":
                break
            await asyncio.sleep(0.01)
        assert engine.attestation["status"] == "verified"
        assert engine.chat[-1]["kind"] == "assistant" and engine.chat[-1]["tier"] == "e2ee"
        assert [p["model"] for p, _ in relay.preflights] == ["private/glm-5-3"]
        assert relay.lists == 1, "one read of the list served the check, the turn and the retry"
    finally:
        await engine.stop()


ENCRYPTION_STACK = ("tinfoil", "tinfoil-ehbp", "pyhpke")


def test_the_encryption_stack_is_pinned_exactly_and_alike_everywhere():
    """A source install (pip install -e .) reads pyproject.toml, not the AppImage lock, and
    tinfoil accepts any tinfoil-ehbp>=0.3.0: unpinned, a source install took 0.4.0 the day it
    came out (Oct 6 2026), a new request framing never run against the enclave here. The lock
    is made from pyproject.toml, so the pin there is what holds a relock to the same version."""
    import re
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    deps = tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]
    lock = (root / "packaging/appimage/requirements.lock").read_text()
    for name in ENCRYPTION_STACK:
        locked = re.search(rf"^{re.escape(name)}==(\S+) \\$", lock, re.MULTILINE)
        assert locked, f"{name} is not in the AppImage lock"
        pin = f"{name}=={locked.group(1)}"
        assert pin in deps, f"pyproject.toml must pin {pin} exactly"
        block = re.split(r"(?m)^(?=\S)", lock[locked.end():], 1)[0]     # its hashes and "via" lines
        assert "datoolkit (./pyproject.toml)" in block, f"the lock must record {name} as pyproject.toml's own pin"
