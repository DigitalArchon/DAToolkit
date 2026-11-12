"""A secret in a queued command, or in a baseline's text, never goes to a model: the technician
may type a password into a command's box, and the queue keeps it for the terminal alone."""

import asyncio
import json

from datoolkit.llm import prompts

from test_engine import env, wait_turn  # noqa: F401
from test_research import say
from test_revise_search import setup

MYSQL = "mysql -u root -pS3cretPW99 -e 'show processlist'"
SSHPASS = "sshpass -p Backup!Pass7 ssh backup@nas df -h"


def _queue(engine, sid):
    engine.queue.add("c", [{"session_id": sid, "command": "mysql -u root -p<PASSWORD> -e 'show processlist'",
                            "risk": "read_only", "purpose": "what is running"},
                           {"session_id": sid, "command": "sshpass -p <PASSWORD> ssh backup@nas df -h", "risk": "read_only"}])
    engine.update_item(1, command=MYSQL)                 # the technician fills the password in
    engine.update_item(2, command=SSHPASS)


async def test_a_password_typed_into_a_command_reaches_no_model_with_its_results(env):  # noqa: F811
    engine, fake, _ = env
    sid = setup(engine)
    _queue(engine, sid)
    engine.update_item(1, status="ran")
    fake.responses.append(say("Nothing unusual."))
    engine.send("", results=[{"num": 1, "text": "Id  User  Host\n1   root  localhost"}])
    await wait_turn(engine)
    request = json.dumps(fake.requests[-1])
    assert "S3cretPW99" not in request and "Backup!Pass7" not in request       # header, original, queue state
    user = [m for m in fake.requests[-1]["messages"] if m["role"] == "user"][-1]["content"]
    assert f"#1 on session `{sid}` RAN: `mysql -u root -p[REDACTED] -e 'show processlist'`" in user
    assert "(edited by technician; originally `mysql -u root -p[REDACTED] -e 'show processlist'`)" in user
    assert "sshpass -p [REDACTED] ssh backup@nas df -h" in fake.requests[-1]["messages"][0]["content"]
    # the technician's own view, the queue and the audit log's record of the edit are as typed
    assert engine.queue.get(1).command == MYSQL and engine.queue.get(2).command == SSHPASS
    assert [e for e in engine.chat if e["kind"] == "user"][-1]["results"][0]["command"] == MYSQL
    events = (engine.case.dir / "events.jsonl").read_text()
    edited = [json.loads(line) for line in events.splitlines() if '"proposal_edited"' in line]
    assert edited[0]["command"] == MYSQL
    sent = [json.loads(line) for line in events.splitlines() if '"sent_to_ai"' in line][-1]
    assert "S3cretPW99" not in sent["content"]


def test_the_queue_state_masks_secrets_and_still_cuts_long_commands():
    text = prompts.queue_text([{"num": 1, "session_id": "s", "status": "pending", "command": SSHPASS},
                               {"num": 2, "session_id": "s", "status": "ran", "command": "echo " + "x" * 5_000_000}])
    assert "sshpass -p [REDACTED] ssh backup@nas df -h" in text and "Backup!Pass7" not in text
    assert "`echo " + "x" * 195 + "…`" in text


async def test_the_reviewer_is_sent_the_command_without_its_password(env):  # noqa: F811
    engine, fake, _ = env
    sid = setup(engine)
    engine.cfg.settings.auto_review = "flagged"
    engine.cfg.settings.review_model = "Fake|anthropic/claude-opus-5.5"
    engine.queue.add("c", [{"session_id": sid, "command": "mysql -u root -p<PASSWORD> -e 'show processlist'",
                            "risk": "read_only", "purpose": "what is running", "rollback": "mysql -pOldPw123 -e 'x'"}])
    fake.completions.append("SUMMARY: lists queries\nDATA: none\nVERDICT: proceed")
    before = len(fake.requests)
    engine.update_item(1, command=MYSQL)                 # typing the password in starts the automatic review
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(fake.requests) > before and not engine._background:
            break
    sent = fake.requests[-1]["messages"][-1]["content"]
    assert "Command (#1, labelled" in sent and "mysql -u root -p[REDACTED] -e 'show processlist'" in sent
    assert "S3cretPW99" not in json.dumps(fake.requests[before:]) and "OldPw123" not in sent
    assert engine.queue.get(1).review["level"] == "ok"


async def test_a_baseline_diff_is_redacted_before_it_is_compared_and_counts_what_it_masked(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    sid = setup(engine)
    added = engine.queue_recipe("baseline-linux", sid)
    for p in added:
        engine.update_item(p.num, status="ran")
    first, second = added[0].num, added[1].num
    old = {first: "machine backup.example.com login svc password OldSecret1\n[remote]\npass = b2xkc2VjcmV0\nhostname sw1\n"
                  "password OldEnable9\nusers: 3",
           second: "token: abcdef123456\nmode: fast"}
    new = {first: "machine backup.example.com login svc password NewSecret2\n[remote]\npass = bmV3c2VjcmV0\nhostname sw1\n"
                  "password NewEnable7\nusers: 4",
           second: "token: zyxwvu654321\nmode: fast"}
    texts = [old]
    monkeypatch.setattr(engine, "capture", lambda num: {"text": texts[0].get(num, "same"), "source": "transcript"})
    engine.save_baseline(sid)
    texts[0] = new
    d = engine.diff_baseline(sid)
    for secret in ("OldSecret1", "NewSecret2", "b2xkc2VjcmV0", "bmV3c2VjcmV0", "OldEnable9", "NewEnable7", "abcdef123456",
                   "zyxwvu654321"):
        assert secret not in d["text"]
    assert "-users: 3" in d["text"] and "+users: 4" in d["text"]
    assert d["redactions"] == d["text"].count("[REDACTED]") > 0
    # a section whose only change is a secret is still a lead, its value unseen
    assert added[1].recipe_key in d["changed"] and "differs only in values that were redacted" in d["text"]
