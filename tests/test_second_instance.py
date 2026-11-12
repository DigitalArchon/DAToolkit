"""Starting DA Toolkit beside one that is already running, and launcher options that don't go together."""

import os

import pytest

from datoolkit import app as app_mod


def test_a_second_datoolkit_is_told_of_the_first(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "_instance_lock", None)
    assert app_mod.other_instance(tmp_path) is None                       # the first: it holds the lock
    first = app_mod._instance_lock
    try:
        said = app_mod.other_instance(tmp_path)                           # a second, beside it
        assert "Another DA Toolkit is already running" in said and f"process {os.getpid()}" in said
        assert app_mod._instance_lock is first                            # the first keeps what it holds
    finally:
        first.close()
    monkeypatch.setattr(app_mod, "_instance_lock", None)
    assert app_mod.other_instance(tmp_path) is None                       # the first has gone: no notice
    app_mod._instance_lock.close()
    assert (tmp_path.stat().st_mode & 0o777) == 0o700


def test_no_open_and_window_together_are_refused(capsys):
    """--no-open won over --window without a word, and the window never opened."""
    with pytest.raises(SystemExit) as stop:
        app_mod.main(["--window", "--no-open"])
    assert stop.value.code == 2 and "--no-open goes with the browser" in capsys.readouterr().err
