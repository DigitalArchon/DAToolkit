"""TEE models in the engine: attested before anything is sent, re-attested when old, replies
signature-checked. The verifier itself is tested in test_tee.py."""

import asyncio
import time

import httpx
import pytest

from datoolkit.engine import UserError
from datoolkit.llm.tee import NRAS_URL, TeeClient

from test_engine import env, sse, wait_turn  # noqa: F401
from test_models import _png
from test_tee import UP_TO_DATE, InstanceEvidence, tee_server


def tee_factory(calls: list | None = None, **server):
    """TeeClients on test_tee's fake nano-gpt, Intel's and NVIDIA's checks stood in for."""
    def make(base, key, model):
        if calls is not None:
            calls.append(model)
        return TeeClient(base, key, model, client=tee_server(**server),
                         intel=lambda raw, s: UP_TO_DATE, nvidia=lambda a, n, s: 8)
    return make


async def settled(engine, slot="attestation"):
    for _ in range(200):
        if (getattr(engine, slot) or {}).get("status") != "checking":
            return getattr(engine, slot)
        await asyncio.sleep(0.01)
    raise AssertionError("attestation did not finish")


async def signature_of(entry):
    for _ in range(200):
        if entry.get("tee_signature") != "checking":
            return entry.get("tee_signature")
        await asyncio.sleep(0.01)
    raise AssertionError("signature check did not finish")


async def test_a_tee_model_is_attested_and_its_replies_signature_checked(env):  # noqa: F811
    engine, fake, _ = env
    engine._tee_factory = tee_factory()
    engine.new_case("t", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    assert engine.attestation["status"] == "checking"
    att = await settled(engine)
    assert (att["status"], att["kind"], att["level"], att["gpus"]) == ("verified", "tee", "full", 8)
    assert att["summary"].startswith("Intel TDX quote verified") and att["signing_address"]
    fake.responses.append(sse(({"role": "assistant", "content": "Let's look."}, "stop")))
    engine.send("VPN is down")
    await wait_turn(engine)
    entry = engine.chat[-1]
    assert fake.requests[-1]["model"] == "TEE/glm-5.3"
    assert entry["tee_attested"] == att["summary"]
    assert await signature_of(entry) == "signed"


async def test_nothing_is_sent_to_a_tee_model_whose_attestation_fails(env):  # noqa: F811
    engine, fake, _ = env
    engine._tee_factory = tee_factory(nonce_bound=False, echo_nonce=True)   # a nonce merely echoed back
    engine.new_case("r", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    att = await settled(engine)
    assert att["status"] == "failed" and "didn't bind" in att["error"]
    sent = len(fake.requests)
    engine.send("VPN is down")
    await wait_turn(engine)
    assert len(fake.requests) == sent
    assert "nothing was sent" in engine._last_turn_error and engine.can_retry


async def test_a_reply_signed_by_another_key_is_marked_failed(env):  # noqa: F811
    engine, fake, _ = env
    engine._tee_factory = tee_factory(signer=1)
    engine.new_case("s", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    await settled(engine)
    fake.responses.append(sse(({"role": "assistant", "content": "Hi"}, "stop")))
    engine.send("x")
    await wait_turn(engine)
    assert await signature_of(engine.chat[-1]) == "failed"


async def test_an_old_attestation_is_made_again_before_sending(env):  # noqa: F811
    engine, fake, _ = env
    calls = []
    engine._tee_factory = tee_factory(calls)
    engine.new_case("o", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    await settled(engine)
    fake.responses.append(sse(({"role": "assistant", "content": "one"}, "stop")))
    engine.send("x")
    await wait_turn(engine)
    assert calls == ["TEE/glm-5.3"]                      # fresh: reused
    client, _when, shown = engine._tee[("Fake", "TEE/glm-5.3")]
    engine._tee[("Fake", "TEE/glm-5.3")] = (client, time.monotonic() - 3600, shown)
    fake.responses.append(sse(({"role": "assistant", "content": "two"}, "stop")))
    engine.send("y")
    await wait_turn(engine)
    assert calls == ["TEE/glm-5.3"] * 2 and engine.chat[-1]["text"] == "two"


async def test_a_provider_that_signs_no_replies_shows_them_unsigned(env):  # noqa: F811
    engine, fake, _ = env
    evidence = InstanceEvidence(1)

    def handler(request):
        if "/tee/attestation" in str(request.url):
            return httpx.Response(200, json=evidence.body(request.url.params["nonce"]))
        if str(request.url).startswith(NRAS_URL):
            return httpx.Response(200, json=[["JWT", "a.b.c"]])      # judged by the stand-in below
        return httpx.Response(400, json={"error": "TEE signature is not available"})

    engine._tee_factory = lambda base, key, model: TeeClient(
        base, key, model, client=httpx.Client(transport=httpx.MockTransport(handler)),
        intel=lambda raw, s: UP_TO_DATE, nvidia=lambda a, n, s: 8)
    engine.new_case("c", "open")
    engine.select_model("Fake", "TEE/kimi-k3")
    att = await settled(engine)
    assert att["status"] == "verified", att
    assert att["instances"] == 1 and att["signing_address"] is None
    fake.responses.append(sse(({"role": "assistant", "content": "Hi"}, "stop")))
    engine.send("x")
    await wait_turn(engine)
    assert await signature_of(engine.chat[-1]) == "unsigned"


async def test_a_tee_vision_helper_is_attested_before_an_image_goes_to_it(env):  # noqa: F811
    engine, fake, _ = env
    engine.cfg.providers[0].vision_overrides = {"text/model": "no", "TEE/kimi-k3": "yes"}
    engine._tee_factory = tee_factory()
    engine.new_case("h", "open")
    engine.select_model("Fake", "text/model")
    engine.save_settings({"vision_model": "Fake|TEE/kimi-k3"})
    att = await settled(engine, "helper_attestation")
    assert att["status"] == "verified" and att["kind"] == "tee"
    fake.completions.append("A console menu.")
    fake.responses.append(sse(({"role": "assistant", "content": "ok"}, "stop")))
    engine.send("look", images=[_png()])
    await wait_turn(engine)
    note = engine.chat[-2]["image_notes"]["img-1.png"]
    assert note["model"] == "TEE/kimi-k3" and note["tee_attested"].startswith("Intel TDX quote verified")


async def test_no_image_goes_to_a_tee_helper_that_is_refused(env):  # noqa: F811
    engine, fake, _ = env
    engine.cfg.providers[0].vision_overrides = {"text/model": "no", "TEE/kimi-k3": "yes"}
    engine._tee_factory = tee_factory(attestation_status=400)
    engine.new_case("n", "open")
    engine.select_model("Fake", "text/model")
    engine.save_settings({"vision_model": "Fake|TEE/kimi-k3"})
    assert (await settled(engine, "helper_attestation"))["status"] == "failed"
    sent = len(fake.requests)
    engine.send("look", images=[_png()])
    await wait_turn(engine)
    assert len(fake.requests) == sent                    # neither the helper nor the chat model was asked
    assert "could not be attested" in engine._last_turn_error and engine.can_retry


async def test_the_reviewer_on_a_refused_tee_model_sends_nothing(env):  # noqa: F811
    engine, fake, _ = env
    engine._tee_factory = tee_factory(attestation_status=400)
    engine.new_case("v", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.cfg.settings.review_model = "Fake|TEE/glm-5.3"
    num = engine.queue.add("call_1", [{"session_id": "s1", "command": "uptime", "purpose": "load"}],
                           fallback_session="s1")[0].num
    sent = len(fake.requests)
    with pytest.raises(UserError, match="nothing was sent"):
        await engine.review_command(num)
    assert len(fake.requests) == sent
