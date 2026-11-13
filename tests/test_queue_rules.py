"""The queue's own rules: a call is queued whole or not at all, ratings are made by the rules
in force, a rehearsal is rated like any command, and nothing proposed can hold the app up."""

import base64
import json
import random
import time

import pytest

from datoolkit import queue as queue_mod
from datoolkit.queue import Queue
from datoolkit.safety import hidden

from test_engine import env, sse, tool_call_stream, wait_turn  # noqa: F401


@pytest.mark.parametrize("bad", ["rm -rf /", None, 7, ["ls"], {"command": ["ls", "-la"]}, {"command": None},
                                 {"command": "ls", "purpose": {"why": "x"}}, {"command": "ls", "session_id": ["a"]}])
def test_a_call_with_an_item_that_cant_be_read_queues_none_of_it(bad):
    q = Queue()
    q.add("c0", [{"command": "uptime", "risk": "read_only"}])
    with pytest.raises((TypeError, ValueError), match="item 3"):
        q.add("c1", [{"command": "df -h", "risk": "read_only"}, {"command": "free -m", "risk": "read_only"}, bad])
    assert [p.command for p in q.items] == ["uptime"]
    (p,) = q.add("c2", [{"command": "who", "risk": "read_only"}])           # and no number was used up
    assert p.num == 2


def test_an_item_without_a_command_is_still_left_out_quietly():
    q = Queue()
    added = q.add("c1", [{"command": "df -h"}, {"purpose": "nothing to run"}, {"command": "  "}, {"command": "free -m", "risk": None}])
    assert [(p.num, p.command, p.model_risk) for p in added] == [(1, "df -h", "modifying"), (2, "free -m", "modifying")]


def test_a_rehearsal_of_an_item_that_is_gone_is_refused_before_anything_is_added():
    q = Queue()
    with pytest.raises(KeyError):
        q.add("dryrun", [{"command": "ls -ld /srv", "risk": "read_only"}], insert_before=9, dry_run_of=9)
    assert q.items == [] and q.add("c", [{"command": "uptime"}])[0].num == 1


async def test_the_ai_is_told_its_call_was_invalid_and_nothing_of_it_is_queued(env):  # noqa: F811
    engine, fake, _ = env
    engine.new_case("x", "open")
    engine.select_model("Fake", "anthropic/claude-opus-5.5")
    engine.open_session("local")
    sid = engine.sessions.roster()[0]["id"]
    bad = {"items": [{"session_id": sid, "command": "df -h", "purpose": "space", "risk": "read_only"}, "free -m"]}
    good = {"items": [{"session_id": sid, "command": "df -h", "purpose": "space", "risk": "read_only"},
                      {"session_id": sid, "command": "free -m", "purpose": "memory", "risk": "read_only"}]}
    fake.responses += [tool_call_stream(bad), tool_call_stream(good), sse(({"role": "assistant", "content": "Queued."}, "stop"))]
    engine.send("check the box")
    await wait_turn(engine)
    assert "Invalid propose_commands arguments (item 2 is a string, not an object)" in fake.requests[1]["messages"][-1]["content"]
    # the second call's two items, once each, and both in the chat entry and the log
    assert [(p.num, p.command) for p in engine.queue.items] == [(1, "df -h"), (2, "free -m")]
    assert engine.chat[-1]["proposals"] == [1, 2]
    logged = [json.loads(line) for line in (engine.case.dir / "events.jsonl").read_text().splitlines()]
    assert [e["num"] for e in logged if e["event"] == "proposal"] == [1, 2]


# ---- nothing proposed may hold the app up: the rules run on the event loop that drives every terminal

_BLOB = base64.b64encode(random.Random(2).randbytes(3_000_000)).decode()             # 4,000,000 characters
_JSON = json.dumps({f"key{i}": f"value{i}" for i in range(1500)}, separators=(",", ":"))
SLOW_SHAPES = {
    # a tool sent over WinRM: one command holding the file as base64 (tools_cache.transfer_command)
    "a 4 MB transfer": ("[IO.File]::WriteAllBytes(\"$env:TEMP\\tool.exe\", [Convert]::FromBase64String('" + _BLOB + "')); "
                        "(Get-FileHash \"$env:TEMP\\tool.exe\" -Algorithm SHA256).Hash.ToLower() -eq '" + "ab" * 32 + "'"),
    "20,000 semicolons": ";" * 20000,
    "20,000 line breaks": "echo a" + "\n" * 20000 + "echo b",
    "a 30 KB quoted JSON argument": "ssh host \"echo '" + _JSON.replace('"', '\\"') + "' > /etc/app.json\"",
    "a 30 KB here-document": "cat > /etc/app/config.json <<'EOF'\n" + _JSON + "\nEOF",
    "50,000 pipes": "|" * 50000, "20,000 quotes": "'" * 20000, "dd, 5,000 times": "dd " * 5000, "a function's start, 4,000 times": "x(){ " * 4000,
    "grep, 4,000 times": "grep " * 4000, "kubectl get secret, 1,000 times": "kubectl get secret " * 1000,
    "taskkill, 2,000 times": "taskkill a " * 2000, "400 KB of hex": random.Random(3).randbytes(200_000).hex(),
    "200 KB of words": "lorem ipsum dolor " * 11000,
    # shapes that one pattern or another read again from every repeat
    "a variable's start, 6,400 times": 'key="' * 6400, "32,000 slashes": "/" * 32000, "20,000 brackets": "[" * 20000,
    "sudo, 3,200 times": "sudo -u a " * 3200, "30 KB of SQL": "psql -c '" + "a" * 30000 + "'", "a-, 16,000 times": "a-" * 16000,
    "a.b, 10,000 times": "a.b" * 10000, "5,000 pipes in quotes": "echo '" + "| b c " * 5000 + "'", "print, 5,000 times": "awk '" + "print " * 5000 + "'",
}


@pytest.mark.parametrize("shape", list(SLOW_SHAPES))
@pytest.mark.parametrize("kind", ["ssh", "window"])
def test_no_command_takes_the_queue_long(shape, kind):
    command = SLOW_SHAPES[shape]
    q = Queue()
    started = time.monotonic()
    q.add("t", [{"session_id": "s", "command": command, "risk": "read_only"}], session_kinds={"s": kind})
    added = time.monotonic() - started
    started = time.monotonic()
    for _ in range(3):
        q.to_list()
    listed = time.monotonic() - started
    assert added < 0.8 and listed < 0.4, (shape, added, listed)                  # each took seconds; a few took minutes


def test_a_transfer_is_rated_for_what_it_is_not_for_its_payload():
    (p,) = Queue().add("tool", [{"session_id": "s", "command": SLOW_SHAPES["a 4 MB transfer"], "risk": "modifying"}], session_kinds={"s": "winrm"})
    # random base64 holds `/iex+` and `rg` ... `pass` somewhere: neither is a word of this command
    assert (p.risk, p.risk_reasons, p.sensitive, p.cuts_session) == ("modifying", [], [], "")
    assert len(p.command) > 4_000_000                                              # the command itself is kept whole


def test_hidden_characters_are_found_in_a_long_command_without_reading_each_character():
    started = time.monotonic()
    text, notes = hidden.clean(_BLOB + "\u202e\u200b" + _BLOB[:1000] + " x\r\n")
    assert time.monotonic() - started < 0.5
    assert text == _BLOB + _BLOB[:1000] + " x\n" and len(notes) == 2


def test_the_rehearsal_on_offer_is_worked_out_once_for_each_command(monkeypatch):
    calls = []
    real = queue_mod.dry_run
    monkeypatch.setattr(queue_mod, "dry_run", lambda command: calls.append(command) or real(command))
    q = Queue()
    q.add("c", [{"command": "rm -rf /srv/x", "risk": "disruptive"}, {"command": "df -h", "risk": "read_only"},
                {"command": "sudo apt install htop", "risk": "modifying"}])
    for _ in range(5):
        offers = [d["dry_run"] for d in q.to_list()]
    assert offers[0]["command"] == "ls -ld /srv/x" and offers[1] is None and offers[2]["command"] == "sudo apt install -s htop"
    assert calls == ["rm -rf /srv/x", "sudo apt install htop"]                    # not five times each
    q.update(1, command="rm -rf /srv/y")
    assert q.to_list()[0]["dry_run"]["command"] == "ls -ld /srv/y" and q.to_list() and calls[2:] == ["rm -rf /srv/y"]


# ---- a resumed case: what is still pending is rated by the rules in force now

def _saved(num, command, **fields):
    return {"num": num, "call_id": "c", "session_id": "s", "command": command, "purpose": "", "model_risk": "read_only", "risk": "read_only",
            "risk_reasons": [], "original_command": command, "status": "pending", **fields}


def test_pending_items_of_a_resumed_case_are_rated_again():
    review = {"status": "done", "verdict": "ok", "notes": "fine"}
    q = Queue.from_list([
        _saved(1, "Stop-VM dc01 -TurnOff -Force"),                                  # the rules of the day it was saved had no word for it
        _saved(2, "fdisk -l", risk="disruptive", risk_reasons=["disk/partition tool"]),   # nor knew a listing from a change
        _saved(3, "df -h", model_risk="modifying", risk="modifying"),               # never below what the model said
        _saved(4, "frobnicate --all", original_command="ls", note="ask first", review=review),
        _saved(5, "sudo ufw status", risk="disruptive", cuts_session="may block SSH", risk_reasons=["cuts this session: may block SSH"]),
        _saved(6, "Stop-VM dc02 -Force", status="ran", ran_at=1.0),                 # what has run keeps the rating it ran under
        _saved(7, "ls -ld /srv", dry_run_of=1),                                    # a rehearsal from before they were rated
        _saved(8, "Remove-Item C:\\x -WhatIf", dry_run_of=1),
    ])
    by = {p.num: p for p in q.items}
    assert by[1].risk == "disruptive" and by[1].risk_reasons
    assert (by[2].risk, by[2].risk_reasons) == ("read_only", [])
    assert by[3].risk == "modifying"
    assert by[4].risk == "modifying" and by[4].edited and by[4].note == "ask first" and by[4].review == review
    assert by[5].risk == "disruptive" and by[5].cuts_session == "may block SSH" and "cuts this session: may block SSH" in by[5].risk_reasons
    assert (by[6].risk, by[6].risk_reasons) == ("read_only", [])
    assert by[7].risk == "read_only" and by[8].risk == "modifying"
    assert q.add("c2", [{"command": "uptime"}])[0].num == 9


def test_a_review_goes_when_the_command_it_was_of_had_hidden_characters():
    q = Queue.from_list([_saved(1, "uptime" + chr(0x202E), review={"status": "done", "verdict": "ok"})])
    assert q.items[0].command == "uptime" and q.items[0].review == {} and q.items[0].hidden


def test_a_recipes_step_keeps_its_authors_read_only_label_and_the_ais_same_text_doesnt():
    """A recipe is written by its author and only named by the AI: where the rules have nothing
    against a step but don't know it as a reader, the recipe's label stands; the same text proposed by
    the AI, or the step once edited, is judged like any other command."""
    from datoolkit import recipes
    from datoolkit.safety import risk

    q = Queue()
    flipped = 0
    for r in recipes.load_all():
        for st in r.steps:
            if st.risk != "read_only":
                continue
            raw = {"session_id": "s", "command": st.command, "risk": st.risk}
            as_recipe = q.add("recipe", [{**raw, "recipe": r.id, "recipe_key": getattr(st, "key", "")}])[0]
            level, why = risk.effective("read_only", st.command)
            if why == [risk.UNKNOWN]:
                flipped += 1
                assert as_recipe.risk == "read_only" and not as_recipe.unconfirmed, (r.id, st.command)
                from_ai = q.add("call_1", [raw])[0]
                assert from_ai.risk == "modifying" and from_ai.unconfirmed and from_ai.to_dict()["unconfirmed"]
                edited = q.update(as_recipe.num, command=st.command + " ")          # the same text: no edit
                assert edited.risk == "read_only"
                edited = q.update(as_recipe.num, command=st.command + "; true")
                assert edited.risk != "read_only"
            else:
                assert as_recipe.risk == level, (r.id, st.command)          # a rule that raises it still does
    assert flipped                                                         # the built-in recipes hold such steps


def test_a_winrm_session_is_powershells_alone_and_any_other_may_be_a_posix_shell():
    """The queue tells the rules which kind of session an item is for: a statement beginning with a
    variable keeps the AI's read-only in a WinRM console, and nowhere else."""
    q = Queue()
    raw = [{"session_id": "w", "command": "$s = Get-Service W32Time; $s.Status", "risk": "read_only"},
           {"session_id": "s", "command": "$s = Get-Service W32Time; $s.Status", "risk": "read_only"},
           {"session_id": "s", "command": "sudo $x", "risk": "read_only"},
           {"session_id": "w", "command": "sudo $x", "risk": "read_only"}]
    kinds = {"w": "winrm", "s": "ssh"}
    added = q.add("call_1", raw, session_kinds=kinds)
    assert [p.risk for p in added] == ["read_only", "modifying", "modifying", "modifying"]
    assert [p.unconfirmed for p in added] == [False, True, True, True]
    moved = q.update(added[0].num, session_id="s", session_kinds=kinds)        # the same text, for a session that may be bash
    assert moved.risk == "modifying"
