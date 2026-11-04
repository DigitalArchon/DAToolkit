"""What goes back to the AI, and the queue states that decide it: an item is marked run only
while it is pending (a dialog or a phone screen can be older than the item), an emptied note is
no note, a payload in a command doesn't fill the context, a baseline diff is redacted like any
output shown for sending, and removing the newest exchange leaves nothing to retry."""

import httpx
import pytest
from fastapi import FastAPI

from datoolkit.engine import UserError
from datoolkit.server import app as server_app

from test_engine import env, sse, wait_turn  # noqa: F401
from test_revise_search import setup


def _queued(engine, sid, *commands):
    return [p.num for p in engine.queue.add("c", [{"session_id": sid, "command": c, "risk": "read_only"}
                                                  for c in commands])]


async def test_an_item_is_marked_run_only_while_it_is_pending(env):  # noqa: F811
    engine, _, _ = env
    sid = setup(engine)
    a, b = _queued(engine, sid, "uptime", "df -h")
    engine.queue.update(a, status="withdrawn", note="Withdrawn by the AI: wrong host")
    with pytest.raises(UserError, match="withdrawn by the AI now"):
        engine.update_item(a, status="ran")               # "Run it" in a dialog opened before the withdrawal
    assert engine.queue.get(a).status == "withdrawn"
    engine.update_item(a, status="pending", note="")       # Restore
    engine.update_item(a, status="ran")
    engine.update_item(a, status="pending")                # Reset
    engine.queue.update(b, status="sent")
    for status in ("pending", "ran", "skipped"):
        with pytest.raises(UserError, match="sent to the AI now"):
            engine.update_item(b, status=status)
    for status in ("sent", "withdrawn"):
        with pytest.raises(UserError, match="by hand"):
            engine.update_item(a, status=status)


async def test_a_phone_showing_an_old_queue_cannot_undo_a_sent_item(env, tmp_path):  # noqa: F811
    engine, _, _ = env
    sid = setup(engine)
    (num,) = _queued(engine, sid, "uptime")
    engine.queue.update(num, status="sent")
    app: FastAPI = server_app.create_app("main", lambda emit: engine, companion_dir=tmp_path)
    app.state.engine = engine
    app.state.companion.token = "phone"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.state.companion.app), base_url="http://t") as c:
        r = await c.post(f"/api/companion/queue/{num}", json={"status": "pending"}, headers={"x-token": "phone"})
    assert r.status_code == 400 and engine.queue.get(num).status == "sent"


async def test_a_note_emptied_in_send_results_is_not_sent(env):  # noqa: F811
    engine, fake, _ = env
    sid = setup(engine)
    (num,) = _queued(engine, sid, "systemctl status billing")
    engine.update_item(num, status="skipped", note="not on prod: client ACME's billing server")
    fake.responses.append(sse(({"role": "assistant", "content": "Understood."}, "stop")))
    engine.send("", results=[{"num": num, "text": "", "note": ""}])
    await wait_turn(engine)
    sent = fake.requests[-1]["messages"][-1]["content"]
    assert "ACME" not in sent and "gave no reason" in sent


async def test_a_command_carrying_a_payload_is_shortened_in_what_the_ai_is_told(env):  # noqa: F811
    engine, fake, _ = env
    sid = setup(engine)
    payload = "A" * 400_000
    command = f"[IO.File]::WriteAllBytes(\"$env:TEMP\\\\x.exe\", [Convert]::FromBase64String('{payload}')); 'hash-check'"
    (num,) = _queued(engine, sid, command)
    engine.update_item(num, status="ran")
    fake.responses.append(sse(({"role": "assistant", "content": "Copied."}, "stop")))
    engine.send("", results=[{"num": num, "text": "True"}])
    await wait_turn(engine)
    sent = fake.requests[-1]["messages"][-1]["content"]
    assert len(sent) < 5000 and "characters left out" in sent
    assert "WriteAllBytes" in sent and "'hash-check'" in sent         # how it starts and ends stays


async def test_a_baseline_diff_is_redacted_before_it_is_shown_for_sending(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    engine.new_case("b", "open")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    added = engine.queue_recipe("baseline-linux", sid)
    outputs = {p.num: f"{p.command}\nline-{p.recipe_key}\n$ " for p in added}
    monkeypatch.setattr(engine, "capture", lambda num: {"text": outputs[num], "source": "transcript"})
    for p in added:
        engine.update_item(p.num, status="ran")
    engine.save_baseline(sid)
    cron = next(p for p in added if p.recipe_key == "cron")
    outputs[cron.num] = f"{cron.command}\n0 2 * * * PGPASSWORD=Hunter2Secret pg_dump -h db app\n$ "
    diff = engine.diff_baseline(sid)
    assert "Hunter2Secret" not in diff["text"] and diff["redactions"] >= 1


async def test_removing_the_newest_exchange_leaves_nothing_to_retry(env):  # noqa: F811
    engine, fake, _ = env
    setup(engine)
    for text in ("first", "second"):
        fake.responses.append(sse(({"role": "assistant", "content": f"answer to {text}"}, "stop")))
        engine.send(text)
        await wait_turn(engine)
    assert not engine.can_retry
    engine.drop_context([len(engine.context_view()["groups"]) - 1])
    assert not engine.can_retry
    assert engine.conv[-1]["content"].startswith("[An earlier exchange was removed")
    with pytest.raises(UserError, match="no longer in the AI's context"):
        engine.drop_context([7])


async def test_half_an_emoji_doesnt_stop_the_case_being_saved(env):  # noqa: F811
    # JSON from the page can carry a lone surrogate (an emoji cut in half), and a reply streamed
    # in pieces can split a surrogate pair; UTF-8 has no form for either, and the failed write
    # used to fail every later save of the case too
    import json

    engine, fake, _ = env
    setup(engine)
    fake.responses.append(sse(({"role": "assistant", "content": "Disk \ud83d"}, None),
                              ({"content": "\ude00 is full."}, "stop")))
    engine.send(json.loads('"disk full \\ud83d"'))
    await wait_turn(engine)
    engine.add_client("Acme")
    engine._persist()
    saved = json.loads((engine.case.dir / "state.json").read_text(encoding="utf-8"))
    assert saved["chat"][0]["text"] == "disk full �"
    assert "Disk \U0001F600 is full." in json.dumps(saved["chat"], ensure_ascii=False)
    assert "�" in (engine.case.dir / "events.jsonl").read_text(encoding="utf-8")
    name, data = engine.export_full(False)
    assert data[:2] == b"PK"
