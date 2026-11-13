"""A case's files on disk: written whole, the state before the last kept as a backup, draft saves
in the order they were asked for."""

import json
import threading

import pytest

from datoolkit import case as case_mod
from datoolkit.case import Case
from datoolkit.engine import UserError

from test_engine import env  # noqa: F401


def _save(case, *texts):
    case.save_state([], [{"kind": "user", "text": t} for t in texts], [])


# ---------------------------------------------------------------- state.json and its backup

def test_each_save_keeps_the_state_before_it(tmp_path):
    c = Case.create("job", "open", root=tmp_path)
    _save(c, "one")
    assert c.load_backup() is None                               # nothing came before the first save
    _save(c, "one", "two")
    _save(c, "one", "two", "three")
    assert [e["text"] for e in c.load_state()["chat"]] == ["one", "two", "three"]
    assert [e["text"] for e in c.load_backup()["chat"]] == ["one", "two"]
    assert sorted(p.name for p in c.dir.iterdir()) == ["case.json", "events.jsonl", "state.json", "state.json.bak"]


def test_a_state_cut_short_never_becomes_the_backup(tmp_path):
    c = Case.create("job", "open", root=tmp_path)
    _save(c, "one")
    _save(c, "one", "two")
    whole = (c.dir / "state.json").read_bytes()
    (c.dir / "state.json").write_bytes(whole[:len(whole) // 2])          # what a crash left
    with pytest.raises(ValueError):
        c.load_state()
    _save(c, "one", "two", "three")                                      # the case was open: its next save
    assert [e["text"] for e in c.load_backup()["chat"]] == ["one"]       # still the last state that could be read
    (c.dir / "state.json").write_bytes(b"")
    _save(c, "four")
    assert [e["text"] for e in c.load_backup()["chat"]] == ["one"]


async def test_a_case_whose_state_cant_be_read_opens_from_its_backup(env):  # noqa: F811
    engine, _, events = env
    engine.new_case("damaged", "open")
    damaged = engine.case
    engine.chat.append({"kind": "user", "text": "the printer is offline"})
    engine._persist()
    engine.queue.add("c", [{"session_id": "s", "command": "lpstat -t", "risk": "read_only"}])
    engine._persist()
    whole = (damaged.dir / "state.json").read_bytes()
    engine.new_case("other", "open")
    for wreck in (whole[:len(whole) // 2], b"", None):                   # cut short, emptied, gone
        if wreck is None:
            (damaged.dir / "state.json").unlink()
        else:
            (damaged.dir / "state.json").write_bytes(wreck)
        events.clear()
        engine.open_case(damaged.id)
        assert engine.case.id == damaged.id and engine.chat[0]["text"] == "the printer is offline"
        said = [e["text"] for e in engine.chat if e["kind"] == "note" and "couldn't be read" in e["text"]][-1]
        assert "opened from the copy kept of the save before it" in said
        assert any(e["type"] == "toast" and e["text"] == said for e in events)
        assert json.loads((damaged.dir / "state.json").read_text())["chat"][0]["text"] == "the printer is offline"
        kept = [p for p in damaged.dir.glob("state.json.damaged-*")]
        if wreck is not None:                                            # the unreadable file is kept, and named
            assert len(kept) == 1 and kept[0].read_bytes() == wreck and kept[0].name in said
            kept[0].unlink()
        engine.new_case("other", "open")


async def test_without_a_backup_the_case_is_still_refused_and_nothing_is_written(env):  # noqa: F811
    engine, _, _ = env
    engine.new_case("broken", "open")
    broken = engine.case
    (broken.dir / "state.json").write_text("{")
    engine.new_case("current", "open")
    with pytest.raises(UserError, match="Could not open"):
        engine.open_case(broken.id)
    assert engine.case.name == "current" and (broken.dir / "state.json").read_text() == "{"


# ---------------------------------------------------------------- drafts

def test_an_older_draft_save_never_lands_after_a_newer_one(tmp_path, monkeypatch):
    """The page saves the draft as the technician types, each save on its own thread: one with
    four photos is still being written when the next, with the photos removed, has finished."""
    c = Case.create("draft", "open", root=tmp_path)
    photo = "data:image/png;base64," + "A" * 1000
    newer_landed, older_serialised = threading.Event(), threading.Event()
    real = case_mod._json_bytes

    def slow(data):
        out = real(data)
        if data.get("images"):                      # the save with photos: slow enough to be overtaken
            older_serialised.set()
            assert newer_landed.wait(5)
        return out

    monkeypatch.setattr(case_mod, "_json_bytes", slow)
    errors = []

    def save(text, images):
        try:
            c.save_draft(text, images)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    older = threading.Thread(target=save, args=("message with four photos", [photo] * 4))
    older.start()
    assert older_serialised.wait(5)
    newer = threading.Thread(target=save, args=("message, photos removed", []))
    newer.start()
    newer.join(5)
    newer_landed.set()
    older.join(5)
    assert errors == [] and c.load_draft() == {"text": "message, photos removed", "images": []}
    assert [p.name for p in c.dir.iterdir() if "draft" in p.name] == ["draft.json"]     # and no temp file of either


def test_many_draft_saves_at_once_leave_the_last_one_asked_for(tmp_path):
    c = Case.create("draft", "open", root=tmp_path)
    again = Case.load(c.id, root=tmp_path)           # the page saves by id: another handle on the same case
    threads = [threading.Thread(target=(c if n % 2 else again).save_draft, args=(f"draft {n}", ["x" * (4000 - n * 100)]))
               for n in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    c.save_draft("the last", [])
    assert c.load_draft()["text"] == "the last"
    c.save_draft("", [])
    assert not (c.dir / "draft.json").exists() and not list(c.dir.glob(".draft*"))
