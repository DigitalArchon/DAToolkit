"""Two DA Toolkits running at once (a technician with two tickets has a reason to start a second):
neither writes over what the other saved. Each Engine here is one of them: its own Config read
from the one config.toml, and its own descriptors on the cases."""

import pytest

from datoolkit import config as config_mod
from datoolkit import creds
from datoolkit.case import Case
from datoolkit.config import Config, Host, Provider
from datoolkit.engine import Engine, UserError

LOCAL = "http://localhost:11434/v1"


@pytest.fixture
async def two(tmp_path):
    config_mod.save(Config(providers=[Provider("Mine", LOCAL)], hosts=[Host("old", "ssh", "10.0.0.1")], clients=["Acme"],
                           active_provider="Mine", active_model="m1"))
    events = {"a": [], "b": []}
    a = Engine(config_mod.load(), events["a"].append, tmp_path / "a")
    b = Engine(config_mod.load(), events["b"].append, tmp_path / "b")
    yield a, b, events
    await a.stop()
    await b.stop()


async def test_a_pane_dragged_in_one_does_not_undo_what_the_other_saved(two):
    a, b, events = two
    a.add_client("Bolt")
    a.save_host({"name": "web01", "kind": "ssh", "host": "10.99.0.1"})
    b.save_layout({"chat_w": 500})                       # silently: it runs on every drag
    on_disk = config_mod.load()
    assert [h.name for h in on_disk.hosts] == ["old", "web01"] and on_disk.clients == ["Acme", "Bolt"]
    assert on_disk.settings.layout == {} and b.cfg.settings.layout == {"chat_w": 500}     # held here, not written
    assert not [e for e in events["b"] if e["type"] == "toast"]


async def test_settings_changed_in_the_one_that_is_behind_are_refused_whole(two):
    a, b, _ = two
    a.save_settings({"font_size": 15})
    said = "Settings were changed by another DA Toolkit that is running. Close this one and start it again"
    before = (b.cfg.to_dict(), dict(creds.keyring.get_keyring().store))
    for change in (lambda: b.save_settings({"font_size": 20}),
                   lambda: b.save_provider({"name": "Other", "base_url": "https://llm.example/v1"}, "sk-other"),
                   lambda: b.delete_provider("Mine"),
                   lambda: b.save_host({"name": "dc01", "kind": "ssh", "host": "10.0.0.9", "auth": "password"}, "hunter2"),
                   lambda: b.delete_host("old"),
                   lambda: b.add_client("Bolt"),
                   lambda: b.rename_client("Acme", "Acme Pty Ltd"),
                   lambda: b.delete_client("Acme")):
        with pytest.raises(UserError, match=said):
            change()
    assert (b.cfg.to_dict(), dict(creds.keyring.get_keyring().store)) == before      # nothing changed here, no key kept
    assert config_mod.load().settings.font_size == 15
    a.save_settings({"font_size": 16})                   # the one that saved last goes on saving
    assert config_mod.load().settings.font_size == 16


async def test_a_model_chosen_in_the_one_behind_is_used_but_not_written(two):
    a, b, events = two
    a.add_client("Bolt")
    b.select_model("Mine", "m2")
    b.select_model("Mine", "m3")
    assert b.cfg.active_model == "m3" and config_mod.load().active_model == "m1"
    told = [e["text"] for e in events["b"] if e["type"] == "toast"]
    assert len(told) == 1 and "until this one is closed but not remembered" in told[0]      # said once


async def test_a_case_is_open_in_one_at_a_time(two):
    a, b, _ = two
    a.new_case("shared ticket", "open", client="Acme")
    shared = a.case.id
    with pytest.raises(UserError, match="open in another DA Toolkit; it would overwrite that one's conversation"):
        b.open_case(shared)
    assert b.case is None
    a.chat.append({"kind": "user", "text": "written in the first"})
    a._persist()
    # nor deleted, nor edited, nor its client renamed under the one that has it open
    assert b.delete_cases([shared]) == {"deleted": [], "errors": [
        f"{shared}: it is open in another DA Toolkit, which is still writing into it; close it there first"]}
    with pytest.raises(UserError, match="open in another DA Toolkit, which would write its own name"):
        b.edit_case(shared, "renamed in the second", sensitivity="sovereign")
    for change in (lambda: b.rename_client("Acme", "Acme Pty Ltd"), lambda: b.delete_client("Acme")):
        with pytest.raises(UserError, match="shared ticket is open in another DA Toolkit"):
            change()
    assert b.cfg.clients == ["Acme"] and config_mod.load().clients == ["Acme"]
    found = Case.load(shared)
    assert (found.name, found.sensitivity, found.client) == ("shared ticket", "open", "Acme")
    assert [e["text"] for e in found.load_state()["chat"]] == ["written in the first"]

    b.new_case("the second's own", "open")
    with pytest.raises(UserError, match="open in another DA Toolkit"):
        a.open_case(b.case.id)                           # a new case is held from the start
    assert a.case.id == shared
    a.open_case(shared)                                  # its own open case, opened again: still its own
    with pytest.raises(UserError, match="open in another DA Toolkit"):
        b.open_case(shared)

    a.new_case("something else", "open")                 # switching lets the case go
    b.open_case(shared)
    assert [e["text"] for e in b.chat][0] == "written in the first"
    with pytest.raises(UserError, match="open in another DA Toolkit"):
        a.open_case(shared)
    await b.stop()                                       # and so does stopping
    a.open_case(shared)
    assert a.case.id == shared


async def test_a_case_that_cant_be_opened_is_not_left_held(two):
    a, b, _ = two
    a.new_case("broken", "open")
    broken = a.case
    a.new_case("current", "open")
    (broken.dir / "state.json").write_text("{")
    (broken.dir / "state.json.bak").unlink(missing_ok=True)
    with pytest.raises(UserError, match="Could not open"):
        a.open_case(broken.id)
    assert b.delete_cases([broken.id])["deleted"] == [broken.id]     # not held by the one that failed to open it
    with pytest.raises(UserError, match="open in another DA Toolkit"):
        b.open_case(a.case.id)                                       # which still holds its own
