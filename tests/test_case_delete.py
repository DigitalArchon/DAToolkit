"""Deleting cases from disk."""

import pytest

from datoolkit.case import Case
from datoolkit.config import data_dir

from test_engine import env  # noqa: F401


def test_delete_removes_only_case_directories(tmp_path):
    c = Case.create("old job", "open", root=tmp_path)
    (c.dir / "transcript-local1.log").write_text("x")
    other = tmp_path / "not-a-case"
    other.mkdir()
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    Case.delete(c.id, root=tmp_path)
    assert not c.dir.exists()
    for bad in ("not-a-case", "..", "../outside", "a/b", ""):
        with pytest.raises((FileNotFoundError, ValueError)):
            Case.delete(bad, root=tmp_path)
    assert other.exists() and outside.exists()


def test_delete_skips_a_symlinked_case(tmp_path):
    real = Case.create("real", "open", root=tmp_path / "elsewhere")
    base = tmp_path / "cases"
    base.mkdir()
    (base / "link").symlink_to(real.dir)
    with pytest.raises(FileNotFoundError):
        Case.delete("link", root=base)
    assert real.dir.exists()


async def test_engine_deletes_cases_but_not_the_open_one(env):  # noqa: F811
    engine, _, _ = env
    engine.new_case("first", "open")
    first = engine.case.id
    engine.new_case("second", "open")
    engine.new_case("third", "open")
    ids = [c["id"] for c in engine.list_cases()]
    assert len(ids) == 3 and (data_dir() / "cases" / first).exists()
    engine._similar = [{"id": first, "name": "first", "runbook": "RB"}]
    r = engine.delete_cases([first, engine.case.id, "nope"])
    assert r["deleted"] == [first] and len(r["errors"]) == 2 and "open case" in r["errors"][0]
    assert first not in [c["id"] for c in engine.list_cases()] and engine.case.dir.exists()
    assert engine._similar == [] and engine._runbooks == ""
