"""Work that outlives a case switch stays with the case it was for: write-ups, second opinions,
cancelled compactions and reply-signature checks are saved and logged in their own case, never
in the one open when they finish. And what was checked before an attestation is checked again
after it: the case may have been switched, or its sensitivity raised, while that took seconds."""

import asyncio
import json
import threading

import httpx
import pytest

from datoolkit.engine import UserError
from datoolkit.llm.client import LLMClient

from test_compact import _three_exchanges
from test_engine import env, sse, tool_call_stream, wait_turn  # noqa: F401
from test_tee_engine import settled, tee_factory


def events_of(case) -> list[dict]:
    path = case.dir / "events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def requests_of(case) -> str:
    path = case.dir / "requests.jsonl"
    return path.read_text(encoding="utf-8") if path.exists() else ""


async def drained(engine):
    for _ in range(300):
        if not engine._background:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background work did not finish")


def gated_complete(engine, reply: str, sent: list | None = None):
    """engine._complete held until the returned event is set."""
    gate = asyncio.Event()

    async def complete(client, prov, model, messages):
        if sent is not None:
            sent.append((model, messages))
        await gate.wait()
        return reply
    engine._complete = complete
    return gate


async def test_a_write_up_that_finishes_after_a_case_switch_is_saved_in_its_own_case(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("Case A", "open", notes="client X")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    fake.responses.append(sse(({"role": "assistant", "content": "Disk on A is full."}, "stop")))
    engine.send("Server A disk full")
    await wait_turn(engine)
    case_a = engine.case
    gate = gated_complete(engine, "RUNBOOK OF A")
    task = asyncio.create_task(engine.distill_runbook())
    await asyncio.sleep(0.05)
    engine.new_case("Case B", "open")          # the technician closed the dialog and moved on
    case_b = engine.case
    gate.set()
    result = await task
    assert (case_a.dir / "runbook.md").read_text(encoding="utf-8") == "RUNBOOK OF A\n"
    assert result["path"] == str(case_a.dir / "runbook.md")
    assert not (case_b.dir / "runbook.md").exists()
    assert "Server A disk full" in requests_of(case_a) and not requests_of(case_b)
    assert "runbook" in [e["event"] for e in events_of(case_a)]
    assert "runbook" not in [e["event"] for e in events_of(case_b)]


async def test_a_second_opinion_that_finishes_after_a_case_switch_is_logged_in_its_own_case(env):  # noqa: F811
    engine, fake, _ = env
    engine.cfg.settings.review_model = "Fake|anthropic/claude-opus-5.5"
    engine.cfg.settings.auto_review = "disruptive"
    engine.new_case("Case A", "open", notes="Site X: core switch 10.9.9.1")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    case_a = engine.case
    gate = gated_complete(engine, "SUMMARY: reboots\nDATA: none\nVERDICT: proceed with care")
    fake.responses.append(tool_call_stream({"items": [
        {"session_id": sid, "command": "sudo reboot", "purpose": "apply", "risk": "disruptive", "rollback": "n/a"}]}))
    engine.send("apply the fix")
    await wait_turn(engine)
    assert engine.queue.get(1).review["status"] == "checking"
    engine.new_case("Case B", "open")
    case_b = engine.case
    gate.set()
    await drained(engine)
    assert "10.9.9.1" in requests_of(case_a) and "10.9.9.1" not in requests_of(case_b)
    assert "second_opinion" in [e["event"] for e in events_of(case_a)]
    assert "second_opinion" not in [e["event"] for e in events_of(case_b)]
    assert not engine.queue.items                  # B's queue got nothing of A's


async def test_a_compaction_cancelled_by_a_case_switch_is_logged_in_its_own_case(env, monkeypatch):  # noqa: F811
    engine, fake, _ = env
    await _three_exchanges(engine, fake)
    case_a = engine.case
    started = asyncio.Event()

    class Hanging(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (b'data: {"id":"c","object":"chat.completion.chunk","created":0,"model":"m","choices":'
                   b'[{"index":0,"delta":{"content":"Problem"},"finish_reason":null}]}\n\n')
            started.set()
            await asyncio.sleep(3600)
            yield b""

    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, stream=Hanging(), headers={"content-type": "text/event-stream"})))
    monkeypatch.setattr(engine, "_client", lambda prov, model="": LLMClient(prov.base_url, "sk-test", http))
    task = asyncio.create_task(engine.compact_preview(1))
    await asyncio.wait_for(started.wait(), 5)
    engine.new_case("Other client's case", "open")     # compact_cancel(), then the switch
    case_b = engine.case
    with pytest.raises(UserError):
        await task
    assert not requests_of(case_b)
    assert '"cancelled by the technician"' in requests_of(case_a)
    assert "compact_cancelled" in [e["event"] for e in events_of(case_a)]
    assert "compact_cancelled" not in [e["event"] for e in events_of(case_b)]


async def _tee_turn_held(engine, fake):
    """A TEE turn in case A whose reply-signature check waits for the returned event."""
    engine._tee_factory = tee_factory()
    engine.new_case("Case A", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    await settled(engine)
    client = engine._tee[("Fake", "TEE/glm-5.3")][0]
    gate, real = threading.Event(), client.verify_reply
    client.verify_reply = lambda rid: gate.wait(5) and real(rid)
    fake.responses.append(sse(({"role": "assistant", "content": "Let's look."}, "stop")))
    engine.send("VPN is down")
    await wait_turn(engine)
    entry = engine.chat[-1]
    assert entry["tee_signature"] == "checking"
    return gate, engine.case, engine.chat.index(entry)


async def test_a_signature_check_that_ends_after_a_case_switch_is_saved_in_its_case(env):  # noqa: F811
    engine, fake, _ = env
    gate, case_a, index = await _tee_turn_held(engine, fake)
    engine.new_case("Case B", "open")
    case_b = engine.case
    gate.set()
    await drained(engine)
    saved = json.loads((case_a.dir / "state.json").read_text(encoding="utf-8"))
    assert saved["chat"][index]["tee_signature"] == "signed"
    assert "tee_reply_signature" in [e["event"] for e in events_of(case_a)]
    assert "tee_reply_signature" not in [e["event"] for e in events_of(case_b)]
    assert not engine.chat


async def test_a_signature_check_reaches_its_case_when_it_was_opened_again(env):  # noqa: F811
    engine, fake, _ = env
    gate, case_a, index = await _tee_turn_held(engine, fake)
    engine.new_case("Case B", "open")
    engine.open_case(case_a.id)                   # a new Case object, its chat loaded from disk
    assert engine.chat[index]["tee_signature"] == "checking"
    gate.set()
    await drained(engine)
    assert engine.chat[index]["tee_signature"] == "signed"
    saved = json.loads((case_a.dir / "state.json").read_text(encoding="utf-8"))
    assert saved["chat"][index]["tee_signature"] == "signed"


def _attesting(engine):
    """engine._tee_guard held until the returned event is set, as a TEE attestation takes seconds."""
    gate = asyncio.Event()

    async def guard(prov, model, slot):
        await gate.wait()
    engine._tee_guard = guard
    return gate


async def test_a_write_up_is_not_sent_once_the_case_was_raised_during_attestation(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("A", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    engine.conv = [{"role": "user", "content": "client secret stuff"}, {"role": "assistant", "content": "ok"}]
    sent = []
    gated_complete(engine, "Summary.", sent).set()
    gate = _attesting(engine)
    task = asyncio.create_task(engine.ticket_summary())
    await asyncio.sleep(0.02)
    engine.edit_case(engine.case.id, "A", sensitivity="confidential")   # allowed: no turn is running
    gate.set()
    with pytest.raises(UserError, match="CONFIDENTIAL"):
        await task
    assert not sent


async def test_a_review_is_not_sent_once_the_case_was_raised_during_attestation(env):  # noqa: F811
    engine, fake, _ = env
    engine.cfg.settings.review_model = "Fake|TEE/glm-5.3"
    engine.new_case("A", "open", notes="Client X core switch")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    engine.queue.add("c", [{"session_id": sid, "command": "sudo reboot", "purpose": "x", "risk": "disruptive"}])
    sent = []
    gated_complete(engine, "SUMMARY: x\nDATA: none\nVERDICT: proceed", sent).set()
    gate = _attesting(engine)
    task = asyncio.create_task(engine.review_command(1))
    await asyncio.sleep(0.02)
    engine.edit_case(engine.case.id, "A", sensitivity="confidential")
    gate.set()
    with pytest.raises(UserError, match="CONFIDENTIAL"):
        await task
    assert not sent


async def test_a_write_up_is_not_sent_once_another_case_was_opened_during_attestation(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("A", "open")
    engine.select_model("Fake", "TEE/glm-5.3")
    engine.conv = [{"role": "user", "content": "about A"}, {"role": "assistant", "content": "ok"}]
    sent = []
    gated_complete(engine, "Summary.", sent).set()
    gate = _attesting(engine)
    task = asyncio.create_task(engine.client_update())
    await asyncio.sleep(0.02)
    engine.new_case("B", "open")
    gate.set()
    with pytest.raises(UserError, match="case changed"):
        await task
    assert not sent
