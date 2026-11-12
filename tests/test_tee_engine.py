"""TEE models in the engine: attested before anything is sent, re-attested when old, replies
signature-checked, wherever a reply comes from: the chat, the vision helper, write-ups, second
opinions, compaction and the research agent. The verifier itself is tested in test_tee.py."""

import asyncio
import json
import threading

import httpx
import pytest

from datoolkit.engine import UserError
from datoolkit.llm.dcap import since_boot
from datoolkit.llm.tee import NRAS_URL, TeeClient

from test_engine import env, sse, wait_turn  # noqa: F401
from test_models import _png
from test_research import DOC, FakeWeb, agent_turns, nano, research_call, say
from test_tee import (
    GPUS_LOOSE,
    OWN_ENCLAVE_UNCHECKED,
    RECEIPT_KEYS_LOOSE,
    RELAYED,
    TINFOIL,
    UP_TO_DATE,
    InstanceEvidence,
    receipt_server,
    redpill_asked,
    tee_server,
)


def tee_factory(calls: list | None = None, *, server=tee_server, **options):
    """TeeClients on test_tee's fake nano-gpt (`server`: tee_server, or receipt_server for one
    with RedPill's keys and receipts), Intel's and NVIDIA's checks stood in for."""
    def make(base, key, model):
        if calls is not None:
            calls.append(model)
        return TeeClient(base, key, model, client=server(**options),
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


async def test_a_tee_model_is_attested_and_its_replies_checked_to_their_bytes(env):  # noqa: F811
    engine, fake, _ = env
    reply = sse(({"role": "assistant", "content": "Let's look."}, "stop"))
    seen: list = []
    engine._tee_factory = tee_factory(server=receipt_server, tied="statement", stream=reply.encode(), seen=seen)
    engine.new_case("t", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    assert engine.attestation == {"status": "checking", "model": "TEE/glm-5.3", "kind": "tee"}   # "TEE attesting…"
    att = await settled(engine)
    # RedPill's own form, which binds its receipt keys: what is attested is RedPill's gateway, said
    # so, and what that leaves to its word (SealedLore 9af15c1)
    assert (att["status"], att["kind"], att["level"], att["gpus"]) == ("verified", "tee", "partial", 8)
    assert att["shortfalls"] == [OWN_ENCLAVE_UNCHECKED, GPUS_LOOSE] and att["signing_address"]
    assert att["summary"].startswith("partially verified: the model's own enclave unchecked")
    assert (att["attested"], att["headline"]) == ("gateway", "TEE gateway attested")
    assert att["what"].startswith("Attested: RedPill's TEE gateway") and "gateway's word" in att["what"]
    assert "the quote binds its receipt keys" in att["detail"]
    fake.stream_headers = {"X-Receipt-Id": "rcpt-1", "X-TEE-Verification-Route": "r1"}
    fake.responses.append(reply)
    engine.send("VPN is down")
    await wait_turn(engine)
    entry = engine.chat[-1]
    assert fake.requests[-1]["model"] == "TEE/glm-5.3"
    assert entry["tee_attested"] == att["summary"] and entry["tee_headline"] == "TEE gateway attested"
    assert await signature_of(entry) == "verified"
    lookup = [r for r in seen if "/tee/signature/" in str(r.url)][-1]
    assert "/tee/signature/rcpt-1" in str(lookup.url) and lookup.headers["X-TEE-Verification-Route"] == "r1"


async def test_a_reply_other_than_the_one_signed_fails_and_one_not_comparable_is_signed(env):  # noqa: F811
    engine, fake, _ = env
    engine._tee_factory = tee_factory(server=receipt_server, stream=b"data: [DONE]\n\n")
    engine.new_case("b", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    att = await settled(engine)
    assert att["level"] == "partial" and att["shortfalls"] == [RECEIPT_KEYS_LOOSE, GPUS_LOOSE]
    assert (att["attested"], att["headline"]) == ("enclave", "TEE attested")
    fake.responses.append(sse(({"role": "assistant", "content": "Let's look."}, "stop")))
    engine.send("VPN is down")
    await wait_turn(engine)
    assert await signature_of(engine.chat[-1]) == "failed"
    engine.select_model("Fake", "TEE/gemma-4-31b-it")       # nano-gpt rebuilds its streams
    await settled(engine)
    fake.responses.append(sse(({"role": "assistant", "content": "Again."}, "stop")))
    engine.send("and now?")
    await wait_turn(engine)
    assert await signature_of(engine.chat[-1]) == "signed"


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
    engine._tee[("Fake", "TEE/glm-5.3")] = (client, since_boot() - 3600, shown)
    fake.responses.append(sse(({"role": "assistant", "content": "two"}, "stop")))
    engine.send("y")
    await wait_turn(engine)
    assert calls == ["TEE/glm-5.3"] * 2 and engine.chat[-1]["text"] == "two"


async def test_a_tee_attestation_is_made_again_after_a_sleep(env, suspend):  # noqa: F811
    """Aged by time.monotonic, which stops while the machine is suspended: after a night's sleep
    the attestation made the evening before counted as minutes old."""
    engine, fake, _ = env
    calls = []
    engine._tee_factory = tee_factory(calls)
    engine.new_case("z", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    await settled(engine)
    fake.responses.append(sse(({"role": "assistant", "content": "one"}, "stop")))
    engine.send("x")
    await wait_turn(engine)
    assert calls == ["TEE/glm-5.3"]
    suspend(1)
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


async def test_a_model_attested_through_the_gateway_alone_has_its_replies_checked(env):  # noqa: F811
    """Live, TEE/deepseek-v3.2 (Nov 10 2026): its own evidence is relayed for a nonce not ours, and
    was refused; RedPill's gateway, attested for ours, stands in. Its replies are signed by the
    gateway's keys, and there is no signing address: a record that can't be read leaves a reply
    unchecked, never unsigned."""
    engine, fake, _ = env
    reply = sse(({"role": "assistant", "content": "Let's look."}, "stop"))
    relayed = {"aci": "bound", "plain": lambda nonce: (200, RELAYED)}
    engine._tee_factory = tee_factory(server=receipt_server, stream=reply.encode(), **relayed)
    engine.new_case("g", "open")
    engine.select_model("Fake", "TEE/deepseek-v3.2")
    att = await settled(engine)
    assert (att["status"], att["level"]) == ("verified", "partial")
    assert att["shortfalls"] == ["no GPU evidence", OWN_ENCLAVE_UNCHECKED]
    assert att["summary"].startswith("partially verified: no GPU evidence")
    assert att["signing_address"] is None and len(att["signers"]) == 1
    fake.stream_headers = {"X-Receipt-Id": "rcpt-1"}
    fake.responses.append(reply)
    engine.send("VPN is down")
    await wait_turn(engine)
    assert await signature_of(engine.chat[-1]) == "verified"
    engine._tee_factory = tee_factory(server=receipt_server, not_ready=1, **relayed)   # no record kept
    engine.select_model("Fake", "TEE/deepseek-v3.2")
    await settled(engine)
    fake.stream_headers = {}
    fake.responses.append(sse(({"role": "assistant", "content": "Again."}, "stop")))
    engine.send("and now?")
    await wait_turn(engine)
    assert await signature_of(engine.chat[-1]) == "unchecked"


async def test_through_the_gateway_alone_a_reply_whose_record_has_no_receipt_is_not_verified(env):  # noqa: F811
    """Its own enclave is the gateway's word in each receipt: a record without one showed the
    reply verified on the gateway's signature alone. It is unchecked ("⚠ reply not verified"),
    never unsigned, which would say every instance that could answer was attested."""
    engine, fake, _ = env
    reply = sse(({"role": "assistant", "content": "Let's look."}, "stop"))
    engine._tee_factory = tee_factory(server=receipt_server, stream=reply.encode(), with_receipt=False,
                                      aci="bound", plain=lambda nonce: (200, RELAYED))
    engine.new_case("g", "open")
    engine.select_model("Fake", "TEE/deepseek-v3.2")
    assert (await settled(engine))["status"] == "verified"
    for headers in ({"X-Receipt-Id": "rcpt-1"}, {}):
        fake.stream_headers = headers
        fake.responses.append(reply)
        engine.send("VPN is down")
        await wait_turn(engine)
        assert await signature_of(engine.chat[-1]) == "unchecked", headers


async def test_a_chutes_model_shows_its_own_replies_unsigned_and_checks_those_redpill_answers(env):  # noqa: F811
    """Chutes answers first and signs nothing; a reply RedPill's gateway answers instead names its
    receipt (nano-gpt, Nov 10 2026), and is checked against the gateway's attested keys."""
    engine, fake, _ = env
    evidence = InstanceEvidence(1)
    reply = sse(({"role": "assistant", "content": "Hi"}, "stop"))
    seen: list = []
    engine._tee_factory = tee_factory(server=receipt_server, aci="bound", stream=reply.encode(), seen=seen,
                                      plain=lambda nonce: (200, evidence.body(nonce)))
    engine.new_case("k", "open")
    engine.select_model("Fake", "TEE/kimi-k2.6")
    att = await settled(engine)
    assert (att["status"], att["level"], att["instances"]) == ("verified", "partial", 1)
    assert att["signing_address"] is None and att["signers"]
    fake.stream_headers = {"X-TEE-Verification-Route": "chutes-r"}          # Chutes answered
    fake.responses.append(reply)
    engine.send("x")
    await wait_turn(engine)
    assert await signature_of(engine.chat[-1]) == "unsigned"
    assert not [r for r in seen if "/tee/signature/" in str(r.url)], "nothing to look up"
    fake.stream_headers = {"X-Receipt-Id": "rcpt-1", "X-TEE-Verification-Route": "redpill-r"}   # RedPill did
    fake.responses.append(reply)
    engine.send("y")
    await wait_turn(engine)
    assert await signature_of(engine.chat[-1]) == "verified"
    lookup = [r for r in seen if "/tee/signature/" in str(r.url)][-1]
    assert "/tee/signature/rcpt-1" in str(lookup.url) and lookup.headers["X-TEE-Verification-Route"] == "redpill-r"


async def test_a_tee_vision_helper_is_attested_before_an_image_goes_to_it_and_its_description_checked(env):  # noqa: F811
    engine, fake, _ = env
    engine.cfg.providers[0].vision_overrides = {"text/model": "no", "TEE/kimi-k3": "yes"}
    described = sse(({"role": "assistant", "content": "A console menu."}, "stop"))
    engine._tee_factory = tee_factory(reply=described.encode())
    engine.new_case("h", "open")
    engine.select_model("Fake", "text/model")
    engine.save_settings({"vision_model": "Fake|TEE/kimi-k3"})
    att = await settled(engine, "helper_attestation")
    assert att["status"] == "verified" and att["kind"] == "tee"
    fake.responses += [described, sse(({"role": "assistant", "content": "ok"}, "stop"))]
    engine.send("look", images=[_png()])
    await wait_turn(engine)
    asked = next(r for r in fake.requests if r["model"] == "TEE/kimi-k3")
    assert asked["stream"] is True                       # so its bytes can be compared with the signed record
    note = engine.chat[-2]["image_notes"]["img-1.png"]
    assert note["model"] == "TEE/kimi-k3" and note["tee_attested"].startswith("partially verified: receipt keys")
    assert note["tee_headline"] == "TEE attested"
    assert note["text"] == "A console menu." and note["tee_signature"] == "verified"
    kept = json.loads((engine.case.dir / "image-descriptions.json").read_text(encoding="utf-8"))
    assert kept["img-1.png"]["tee_signature"] == "verified"
    assert engine.chat[-1]["text"] == "ok"


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


# ---------------------------------------------------------------- replies outside the chat

def _toasts(events) -> list[str]:
    return [e["text"] for e in events if e["type"] == "toast"]


def _checks_logged(engine) -> list[dict]:
    path = engine.case.dir / "events.jsonl"
    return [e for e in map(json.loads, path.read_text(encoding="utf-8").splitlines())
            if e["event"] == "tee_reply_signature"]


async def test_a_tee_write_up_is_streamed_checked_to_its_bytes_and_marked(env):  # noqa: F811
    """A write-up was asked for with a plain completion: its reply was never checked, and a reply
    that isn't streamed has no bytes the signed record was ever seen to name."""
    engine, fake, events = env
    reply = sse(({"role": "assistant", "content": "Ticket: VPN restored."}, "stop"))
    seen: list = []
    engine._tee_factory = tee_factory(server=receipt_server, tied="statement", stream=reply.encode(), seen=seen)
    engine.new_case("w", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    att = await settled(engine)
    engine.conv = [{"role": "user", "content": "VPN down"}, {"role": "assistant", "content": "Restarted it."}]
    fake.stream_headers = {"X-Receipt-Id": "rcpt-w", "X-TEE-Verification-Route": "r2"}
    fake.responses.append(reply)
    out = await engine.ticket_summary()
    assert fake.requests[-1]["model"] == "TEE/glm-5.3" and fake.requests[-1]["stream"] is True
    assert out["text"] == "Ticket: VPN restored."
    assert out["tee_attested"] == att["summary"] and out["tee_signature"] == "verified"
    assert out["tee_headline"] == "TEE gateway attested"
    lookup = [r for r in seen if "/tee/signature/" in str(r.url)][-1]
    assert "/tee/signature/rcpt-w" in str(lookup.url) and lookup.headers["X-TEE-Verification-Route"] == "r2"
    logged = _checks_logged(engine)[-1]
    assert (logged["purpose"], logged["result"], logged["replies"]) == ("ticket summary", "verified", 1)
    assert not _toasts(events)


async def test_after_a_reply_fails_its_check_the_model_is_attested_again_before_its_next_request(env):  # noqa: F811
    engine, fake, events = env
    calls: list = []
    engine._tee_factory = tee_factory(calls, server=receipt_server, stream=b"bytes this app never received")
    engine.new_case("f", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    await settled(engine)
    engine.conv = [{"role": "user", "content": "VPN down"}, {"role": "assistant", "content": "Restarted it."}]
    fake.responses.append(sse(({"role": "assistant", "content": "An update."}, "stop")))
    out = await engine.client_update()
    assert out["tee_signature"] == "failed"
    assert any("client update didn't match the record its attested enclave signed" in t and "attested again" in t
               for t in _toasts(events))
    assert calls == ["TEE/glm-5.3"]
    fake.responses.append(sse(({"role": "assistant", "content": "A runbook."}, "stop")))
    await engine.distill_runbook()
    assert calls == ["TEE/glm-5.3"] * 2                  # attested again before the runbook was asked for
    engine._tee_factory = tee_factory(calls, server=receipt_server, stream=sse(
        ({"role": "assistant", "content": "A summary."}, "stop")).encode())
    fake.responses.append(sse(({"role": "assistant", "content": "A summary."}, "stop")))
    assert (await engine.ticket_summary())["tee_signature"] == "verified"    # after the runbook failed too
    assert calls == ["TEE/glm-5.3"] * 3
    fake.responses.append(sse(({"role": "assistant", "content": "A summary."}, "stop")))
    await engine.ticket_summary()
    assert calls == ["TEE/glm-5.3"] * 3                  # a good reply: the attestation stands


async def test_a_turns_failed_reply_is_waited_for_and_has_the_model_attested_again_first(env):  # noqa: F811
    """The turn's replies are checked once it ends, in the background: a request made while that
    check runs waits for it, or the next request would go out under the attestation it doubts."""
    engine, fake, events = env
    calls: list = []
    engine._tee_factory = tee_factory(calls, server=receipt_server, stream=b"bytes this app never received")
    engine.new_case("t", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    await settled(engine)
    client = engine._tee[("Fake", "TEE/glm-5.3")][0]
    gate, real = threading.Event(), client.check_reply
    client.check_reply = lambda rid, **seen: gate.wait(5) and real(rid, **seen)
    fake.responses.append(sse(({"role": "assistant", "content": "Let's look."}, "stop")))
    engine.send("VPN is down")
    await wait_turn(engine)
    assert engine.chat[-1]["tee_signature"] == "checking"
    fake.responses.append(sse(({"role": "assistant", "content": "A summary."}, "stop")))
    task = asyncio.create_task(engine.ticket_summary())
    await asyncio.sleep(0.1)
    assert not task.done() and len(fake.requests) == 1   # held until the turn's reply is checked
    gate.set()
    await task
    assert await signature_of(engine.chat[-1]) == "failed"
    assert calls == ["TEE/glm-5.3"] * 2 and len(fake.requests) == 2
    assert any("TEE/glm-5.3's reply didn't match" in t for t in _toasts(events))


async def test_a_tee_second_opinion_is_checked_and_its_mark_kept_with_the_review(env):  # noqa: F811
    engine, fake, _ = env
    reply = sse(({"role": "assistant", "content": "SUMMARY: shows the load\nDATA: none\nVERDICT: proceed"}, "stop"))
    engine._tee_factory = tee_factory(server=receipt_server, stream=reply.encode())
    engine.new_case("v", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.cfg.settings.review_model = "Fake|TEE/glm-5.3"
    num = engine.queue.add("call_1", [{"session_id": "s1", "command": "uptime", "purpose": "load"}],
                           fallback_session="s1")[0].num
    fake.stream_headers = {"X-Receipt-Id": "rcpt-v"}
    fake.responses.append(reply)
    out = await engine.review_command(num)
    assert fake.requests[-1]["model"] == "TEE/glm-5.3" and fake.requests[-1]["stream"] is True
    assert out["verdict"] == "proceed" and out["tee_signature"] == "verified"
    assert out["tee_attested"].startswith("partially verified: receipt keys")
    review = engine.queue.get(num).review
    assert review["status"] == "done" and review["tee_signature"] == "verified" and review["tee_attested"]
    assert _checks_logged(engine)[-1]["purpose"] == f"second opinion on #{num}"


async def test_a_tee_compaction_summary_is_checked_before_it_is_shown(env):  # noqa: F811
    engine, fake, events = env
    summary = sse(({"role": "assistant", "content": "Problem: VPN down. Restarted it."}, "stop"))
    engine._tee_factory = tee_factory(server=receipt_server, stream=summary.encode())
    engine.new_case("k", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    await settled(engine)
    engine.conv = [m for text in ("VPN down", "still down", "up now") for m in (
        {"role": "user", "content": text}, {"role": "assistant", "content": f"noted: {text}"})]
    fake.stream_headers = {"X-Receipt-Id": "rcpt-k"}
    fake.responses.append(summary)
    r = await engine.compact_preview(1)
    assert fake.requests[-1]["model"] == "TEE/glm-5.3"
    assert r["summary"] == "Problem: VPN down. Restarted it." and r["tee_signature"] == "verified" and r["tee_attested"]
    engine._tee_factory = tee_factory(server=receipt_server, stream=b"another summary's bytes")
    engine.select_model("Fake", "TEE/glm-5.3")          # attested anew, against a record of other bytes
    await settled(engine)
    fake.responses.append(summary)
    r = await engine.compact_preview(1)
    assert r["tee_signature"] == "failed" and ("Fake", "TEE/glm-5.3") in engine._tee_doubted
    assert any("summary for compaction didn't match" in t for t in _toasts(events))


def _research_env(fixture, **server):
    """The engine of `env`, its research agent on a TEE model and searching without asking."""
    engine, fake, events = fixture
    nano(engine)
    web = FakeWeb()
    engine._search_http = httpx.AsyncClient(transport=httpx.MockTransport(web.handler))
    engine.cfg.settings.research_model = "Fake|TEE/glm-5.3"
    engine.cfg.settings.search_mode = "auto"
    seen: list = []
    engine._tee_factory = tee_factory(server=receipt_server, seen=seen, **server)
    engine.new_case("r", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    return engine, fake, events, seen


async def test_every_reply_of_a_tee_research_agent_is_checked_and_the_run_marked(env):  # noqa: F811
    turns = agent_turns()
    streams = {f"rcpt-{i}": turn.encode() for i, turn in enumerate(turns)}
    engine, fake, events, seen = _research_env(env, streams=streams)
    fake.responses += [research_call()] + turns + [say("Per the v21 docs: Network > DHCP > Relay.")]
    fake.receipt_ids = ["chat-1", *streams, "chat-2"]           # one per stream, in the order they're sent
    engine.send("need a dhcp relay on the sophos")
    await wait_turn(engine)
    assert [r["model"] for r in fake.requests[1:4]] == ["TEE/glm-5.3"] * 3 and all(r["stream"] for r in fake.requests)
    rec = engine.chat[-1]["research"][0]
    assert rec["status"] == "done" and rec["tee_signature"] == "verified" and rec["tee_attested"]
    looked_up = sorted(str(r.url).rsplit("/", 1)[-1].split("?")[0] for r in seen if "/tee/signature/" in str(r.url))
    assert looked_up == sorted(streams)                        # each of the agent's replies, and only those
    assert _checks_logged(engine)[-1] | {"replies": 3} == _checks_logged(engine)[-1]
    assert engine._research_cache().find(rec["brief"])          # kept for later cases


async def test_a_tee_research_run_with_a_reply_that_fails_is_marked_so_and_not_kept_for_later_cases(env):  # noqa: F811
    turns = agent_turns()
    streams = {f"rcpt-{i}": turn.encode() for i, turn in enumerate(turns)}
    streams["rcpt-1"] = b"what the enclave signed instead"
    engine, fake, events, _ = _research_env(env, streams=streams)
    fake.responses += [research_call()] + turns + [say("Per the v21 docs: Network > DHCP > Relay.")]
    fake.receipt_ids = ["chat-1", *streams, "chat-2"]
    engine.send("need a dhcp relay on the sophos")
    await wait_turn(engine)
    rec = engine.chat[-1]["research"][0]
    assert rec["status"] == "done" and rec["tee_signature"] == "failed"
    assert any("TEE/glm-5.3's research report didn't match" in t for t in _toasts(events))
    assert not engine._research_cache().find(rec["brief"])
    assert ("Fake", "TEE/glm-5.3") in engine._tee_doubted


async def test_a_tee_page_check_is_checked_and_the_page_marked(env):  # noqa: F811
    check = say(json.dumps({"clean": True, "summary": "Sophos docs", "passages": []}))
    engine, fake, _, _ = _research_env(env, stream=check.encode())
    prov = engine.cfg.provider("Fake")
    rec = {"id": "p1", "task": "page", "url": DOC, "steps": [], "status": "running"}
    fake.stream_headers = {"X-Receipt-Id": "rcpt-p"}
    fake.responses.append(check)
    text = await engine._run_page(rec, DOC, prov, "TEE/glm-5.3", "tee")
    assert "found nothing aimed at an AI" in text
    assert rec["status"] == "done" and rec["tee_signature"] == "verified" and rec["tee_attested"]


# ---------------------------------------------------------------- who ran a reply (Settings → Model)

def _logged(engine, event) -> list[dict]:
    path = engine.case.dir / "events.jsonl"
    return [e for e in map(json.loads, path.read_text(encoding="utf-8").splitlines()) if e["event"] == event]


@pytest.fixture
def no_records_kept(monkeypatch):
    from datoolkit.llm import tee

    monkeypatch.setattr(tee, "_records", {})


async def test_a_tee_replys_provider_is_named_only_when_settings_ask(env, no_records_kept):  # noqa: F811
    """Off by default: asking RedPill goes to api.redpill.ai directly, so nothing asks it unless the
    technician chose to (SealedLore 44e145d). What went there is in the case's log and timeline."""
    engine, fake, _ = env
    reply = sse(({"role": "assistant", "content": "Let's look."}, "stop"))
    seen: list = []
    engine._tee_factory = tee_factory(server=receipt_server, aci="bound", session=TINFOIL, stream=reply.encode(),
                                      seen=seen)
    engine.new_case("n", "open")
    engine.select_model("Fake", "TEE/gpt-oss-120b")
    await settled(engine)
    assert engine.cfg.settings.tee_name_upstreams is False
    fake.stream_headers = {"X-Receipt-Id": "rcpt-1"}
    fake.responses.append(reply)
    engine.send("VPN is down")
    await wait_turn(engine)
    assert await signature_of(engine.chat[-1]) == "verified"
    assert "tee_upstream" not in engine.chat[-1] and redpill_asked(seen) == []

    engine.save_settings({"tee_name_upstreams": "true"})        # only a real yes turns it on
    assert engine.cfg.settings.tee_name_upstreams is False
    engine.save_settings({"tee_name_upstreams": True})
    assert engine.snapshot()["config"]["settings"]["tee_name_upstreams"] is True
    fake.responses.append(reply)
    engine.send("and now?")
    await wait_turn(engine)
    entry = engine.chat[-1]
    assert await signature_of(entry) == "verified"
    upstream = entry["tee_upstream"]
    assert (upstream["name"], upstream["label"], upstream["scope"]) == ("tinfoil", "Tinfoil", "router")
    assert "RedPill's word, fetched from api.redpill.ai" in upstream["words"]
    asked = redpill_asked(seen)
    assert len(asked) == 1 and "authorization" not in {k.lower() for k in asked[0].headers}
    logged = _logged(engine, "redpill_session_record")
    assert [(e["host"], e["session"], e["model"], e["provider"]) for e in logged] == [
        ("api.redpill.ai", upstream["session"], "TEE/gpt-oss-120b", "tinfoil")]
    assert _logged(engine, "tee_reply_signature")[-1]["provider"] == "tinfoil"
    assert any(t["kind"] == "redpill_session_record" and "Asked api.redpill.ai who ran a TEE/gpt-oss-120b reply" in t["text"]
               and t["text"].endswith(": tinfoil") for t in engine.timeline()["events"])
    # The same session's next reply: named again, nothing more asked of RedPill or logged as sent.
    fake.responses.append(reply)
    engine.send("more")
    await wait_turn(engine)
    assert await signature_of(engine.chat[-1]) == "verified" and engine.chat[-1]["tee_upstream"]["name"] == "tinfoil"
    assert len(redpill_asked(seen)) == 1 and len(_logged(engine, "redpill_session_record")) == 1


async def test_a_tee_write_up_names_its_provider_too_and_a_failed_one_names_nobody(env, no_records_kept):  # noqa: F811
    engine, fake, _ = env
    reply = sse(({"role": "assistant", "content": "Ticket: VPN restored."}, "stop"))
    engine._tee_factory = tee_factory(server=receipt_server, aci="bound", session=TINFOIL, stream=reply.encode())
    engine.new_case("w", "open")
    engine.select_model("Fake", "TEE/gpt-oss-120b")
    await settled(engine)
    engine.save_settings({"tee_name_upstreams": True})
    engine.conv = [{"role": "user", "content": "VPN down"}, {"role": "assistant", "content": "Restarted it."}]
    fake.stream_headers = {"X-Receipt-Id": "rcpt-w"}
    fake.responses.append(reply)
    out = await engine.ticket_summary()
    assert out["tee_signature"] == "verified" and out["tee_upstream"]["label"] == "Tinfoil"
    # a reply that doesn't match its record names nobody: the name would rest on nothing
    engine._tee_factory = tee_factory(server=receipt_server, aci="bound", session=TINFOIL, stream=b"other bytes")
    engine.select_model("Fake", "TEE/gpt-oss-120b")
    await settled(engine)
    fake.responses.append(reply)
    out = await engine.client_update()
    assert out["tee_signature"] == "failed" and "tee_upstream" not in out


async def test_a_model_chutes_serves_has_no_provider_looked_up(env, no_records_kept):  # noqa: F811
    """Each Chutes instance has a session record of its own, megabytes each: never fetched."""
    engine, fake, _ = env
    evidence = InstanceEvidence(1)
    reply = sse(({"role": "assistant", "content": "Hi"}, "stop"))
    seen: list = []
    engine._tee_factory = tee_factory(server=receipt_server, aci="bound", session=TINFOIL, stream=reply.encode(),
                                      seen=seen, plain=lambda nonce: (200, evidence.body(nonce)))
    engine.new_case("k", "open")
    engine.select_model("Fake", "TEE/kimi-k2.6")
    await settled(engine)
    engine.save_settings({"tee_name_upstreams": True})
    fake.stream_headers = {"X-Receipt-Id": "rcpt-1"}                 # RedPill answered in Chutes' place
    fake.responses.append(reply)
    engine.send("x")
    await wait_turn(engine)
    entry = engine.chat[-1]
    assert await signature_of(entry) == "verified"
    assert entry["tee_upstream"]["name"] == "" and "Chutes serves" in entry["tee_upstream"]["note"]
    assert redpill_asked(seen) == [] and _logged(engine, "redpill_session_record") == []


def test_one_reply_unnamed_or_two_providers_leave_a_turn_unnamed():
    from datoolkit.engine import Engine
    from datoolkit.llm.tee import TeeUpstream

    phala, tinfoil = TeeUpstream(name="phala-244"), TeeUpstream(name="tinfoil")
    unread = TeeUpstream(note="api.redpill.ai answered HTTP 503")
    assert Engine._tee_upstream_of([("verified", phala), ("signed", phala)])["label"] == "Phala"
    assert "different providers" in Engine._tee_upstream_of([("verified", phala), ("verified", tinfoil)])["note"]
    assert Engine._tee_upstream_of([("verified", phala), ("verified", unread)])["note"] == unread.note
    assert Engine._tee_upstream_of([("verified", phala), ("unchecked", None)]) is None
    assert Engine._tee_upstream_of([("verified", None)]) is None           # not looked up: nothing said


# ---------------------------------------------------------------- the guard: refused, or being attested

def switchable(calls: list | None = None):
    """tee_factory's makers whose nano-gpt binds our nonce while `state["ok"]`, and whose attestation
    waits for `state["gate"]` (a threading.Event) when one is set."""
    state = {"ok": True, "gate": None}
    plain = tee_factory(calls, server=lambda **_: tee_server(nonce_bound=state["ok"]))

    def make(base, key, model):
        client = plain(base, key, model)
        attest, gate = client.attest, state["gate"]
        client.attest = lambda: (gate.wait(5) if gate else True) and attest()
        return client
    return make, state


async def test_a_refused_recheck_leaves_no_attestation_in_force(env):  # noqa: F811
    """Re-check refused ("⚠ TEE attestation refused: nothing is sent to it until attestation
    succeeds"), yet a write-up, a turn or a second opinion went out within the quarter hour under
    the attestation before it."""
    engine, fake, _ = env
    engine._tee_factory, state = switchable()
    engine.new_case("r", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    assert (await settled(engine))["status"] == "verified"
    state["ok"] = False
    assert (await engine.attest_now())["status"] == "failed"
    engine.conv = [{"role": "user", "content": "VPN down"}, {"role": "assistant", "content": "Restarted it."}]
    sent = len(fake.requests)
    with pytest.raises(UserError, match="nothing was sent"):
        await engine.ticket_summary()
    engine.send("VPN is down")
    await wait_turn(engine)
    assert "nothing was sent" in engine._last_turn_error
    engine.cfg.settings.review_model = "Fake|TEE/glm-5.3"
    num = engine.queue.add("call_1", [{"session_id": "s1", "command": "uptime", "purpose": "load"}],
                           fallback_session="s1")[0].num
    with pytest.raises(UserError, match="nothing was sent"):
        await engine.review_command(num)
    assert len(fake.requests) == sent
    # attested again, and holding: sent again
    state["ok"] = True
    fake.responses.append(sse(({"role": "assistant", "content": "Ticket."}, "stop")))
    assert (await engine.ticket_summary())["text"] == "Ticket."


async def test_a_model_chosen_again_and_refused_gets_nothing_under_its_earlier_attestation(env):  # noqa: F811
    engine, fake, _ = env
    engine._tee_factory, state = switchable()
    engine.new_case("a", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    await settled(engine)
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    state["ok"] = False
    engine.select_model("Fake", "TEE/glm-5.3")                 # within the quarter hour, refused this time
    engine.conv = [{"role": "user", "content": "VPN down"}, {"role": "assistant", "content": "Restarted it."}]
    sent = len(fake.requests)
    with pytest.raises(UserError, match="nothing was sent"):  # asked at once: waits for the attestation
        await engine.client_update()
    assert engine.attestation["status"] == "failed" and len(fake.requests) == sent


async def test_a_request_made_while_its_model_is_attested_again_waits_for_what_that_finds(env):  # noqa: F811
    engine, fake, _ = env
    calls: list = []
    engine._tee_factory, state = switchable(calls)
    engine.new_case("w", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    await settled(engine)
    engine.conv = [{"role": "user", "content": "VPN down"}, {"role": "assistant", "content": "Restarted it."}]
    for holds in (False, True):
        gate = state["gate"] = threading.Event()
        state["ok"] = holds
        recheck = asyncio.create_task(engine.attest_now())
        await asyncio.sleep(0.05)
        sent = len(fake.requests)
        fake.responses.append(sse(({"role": "assistant", "content": "Ticket."}, "stop")))
        task = asyncio.create_task(engine.ticket_summary())
        await asyncio.sleep(0.1)
        assert not task.done() and len(fake.requests) == sent, "held while the model is being attested"
        gate.set()
        assert (await recheck)["status"] == ("verified" if holds else "failed")
        if holds:
            assert (await task)["text"] == "Ticket." and len(fake.requests) == sent + 1
        else:
            with pytest.raises(UserError, match="nothing was sent"):
                await task
            assert len(fake.requests) == sent
            fake.responses.clear()
        state["gate"] = None


async def test_an_attestation_cancelled_by_another_model_chosen_leaves_nothing_waiting_on_it(env):  # noqa: F811
    """Choosing another TEE model cancels the attestation under way, begun or not: a request waiting
    for it then attests the model itself, and one cancelled before it began ends all the same, or
    the next request to that model would wait for ever."""
    engine, fake, _ = env
    calls: list = []
    engine._tee_factory, state = switchable(calls)
    engine.new_case("c", "open")
    engine.cfg.settings.review_model = "Fake|TEE/glm-5.3"
    num = engine.queue.add("call_1", [{"session_id": "s1", "command": "uptime", "purpose": "load"}],
                           fallback_session="s1")[0].num
    verdict = sse(({"role": "assistant", "content": "SUMMARY: load\nDATA: none\nVERDICT: proceed"}, "stop"))
    # begun, and waited for, then cancelled
    state["gate"] = threading.Event()
    engine.select_model("Fake", "TEE/glm-5.3")
    fake.responses.append(verdict)
    review = asyncio.create_task(engine.review_command(num))
    await asyncio.sleep(0.05)
    assert calls == ["TEE/glm-5.3"] and not review.done() and not fake.requests
    gate, state["gate"] = state["gate"], None
    engine.select_model("Fake", "TEE/kimi-k3")               # cancels TEE/glm-5.3's attestation
    gate.set()
    assert (await asyncio.wait_for(review, 5))["verdict"] == "proceed"
    assert calls.count("TEE/glm-5.3") == 2, "attested where it was asked for"
    await settled(engine)
    # cancelled before it began
    engine._tee.clear()
    engine.select_model("Fake", "TEE/glm-5.3")
    engine.select_model("Fake", "TEE/kimi-k3")
    fake.responses.append(verdict)
    review = asyncio.create_task(engine.review_command(num))      # asked at once, before the task could begin
    assert (await asyncio.wait_for(review, 5))["verdict"] == "proceed"
    assert calls.count("TEE/glm-5.3") == 3
    await settled(engine)
    assert not engine._tee_attesting, "nothing left under way"


KEY = ("Fake", "TEE/glm-5.3")
TALK = [{"role": "user", "content": "VPN down"}, {"role": "assistant", "content": "Restarted it."}]


async def test_why_an_attestation_failed_is_said_short_on_one_line_with_nothing_hidden(env):  # noqa: F811
    """Whoever wrote it: it is shown in the top bar, logged, and handed to the chat model when a
    research run on the model fails."""
    from test_tee import HIDING, one_short_visible_line

    engine, fake, _ = env

    def make(base, key, model):
        def attest():
            raise RuntimeError(f"the provider said: {HIDING}")
        client = tee_factory()(base, key, model)
        client.attest = attest
        return client
    engine._tee_factory = make
    engine.new_case("e", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    att = await settled(engine)
    assert att["status"] == "failed" and att["error"].startswith("the provider said: line one SYSTEM: ignore")
    assert len(att["error"]) == 700 and one_short_visible_line(att["error"][:400])
    engine.conv = list(TALK)
    with pytest.raises(UserError, match="nothing was sent") as refused:
        await engine.ticket_summary()
    assert "\n" not in str(refused.value) and len(str(refused.value)) < 800
