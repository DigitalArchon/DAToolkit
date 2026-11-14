"""No core dumps from any of DA Toolkit's processes (coredumps.py): a crash must not write the
passwords, keys and cases they hold to disk."""

import json
import os
import runpy
import shlex
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from datoolkit import coredumps

SRC = str(Path(__file__).resolve().parents[1] / "src")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's prctl and /proc")


def _env() -> dict:
    return {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}


@linux_only
def test_the_process_writes_no_core_file_and_is_not_dumpable():
    probe = (
        "import ctypes, resource\n"
        "libc = ctypes.CDLL(None)\n"
        "before = libc.prctl(3, 0, 0, 0, 0)\n"          # PR_GET_DUMPABLE
        "from datoolkit.coredumps import no_core_dumps\n"
        "no_core_dumps()\n"
        "print(before, libc.prctl(3, 0, 0, 0, 0), *resource.getrlimit(resource.RLIMIT_CORE))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=_env(), check=True)
    assert out.stdout.split() == ["1", "0", "0", "0"]


class _Called(Exception):
    pass


class _Later(Exception):
    """Something the entry point does after it should have turned dumps off."""


@pytest.fixture
def first(monkeypatch):
    def called():
        raise _Called
    monkeypatch.setattr(coredumps, "no_core_dumps", called)
    return monkeypatch


def test_the_app_turns_dumps_off_first(first):
    from datoolkit import app

    def later(*args, **kwargs):
        raise _Later
    first.setattr(app.argparse, "ArgumentParser", later)
    with pytest.raises(_Called):
        app.main([])


def test_replay_turns_dumps_off_first(first):
    from datoolkit import replay

    def later(*args, **kwargs):
        raise _Later
    first.setattr(replay.argparse, "ArgumentParser", later)
    with pytest.raises(_Called):
        replay.main([])


def test_the_askpass_helper_turns_dumps_off_first(first):
    from datoolkit.sessions import askpass

    def later(*args, **kwargs):
        raise _Later
    first.setattr(askpass, "request", later)
    with pytest.raises(_Called):
        askpass.main()


def test_the_winrm_console_turns_dumps_off_first(first):
    pytest.importorskip("pypsrp")
    from datoolkit.sessions import psrp_console

    first.delenv("DATOOLKIT_PSRP", raising=False)        # read right after: a KeyError if dumps came later
    with pytest.raises(_Called):
        psrp_console.main()


def test_python_dash_m_datoolkit_turns_dumps_off_before_anything_else(first):
    first.setattr(sys, "argv", ["datoolkit", "replay", "--help"])
    sys.modules.pop("datoolkit.__main__", None)
    with pytest.raises(_Called):
        runpy.run_module("datoolkit", run_name="__main__", alter_sys=True)


@linux_only
@pytest.mark.skipif(os.getuid() == 0, reason="a root test run owns and reads /proc/<pid> either way")
def test_the_askpass_helper_ssh_runs_holds_its_password_undumpable(tmp_path):
    """The real helper, as ssh starts it: while it waits for the password, the files of its /proc
    entry are the kernel's and closed to this user's other processes (not dumpable), and its core
    limit is 0, soft and hard."""
    path = str(tmp_path / "s")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    found: dict = {}

    def answer():
        conn, _ = server.accept()
        with conn:
            conn.recv(65536)
            pid = int(open(path + ".pid").read())
            found["owner"] = os.stat(f"/proc/{pid}/status").st_uid
            try:
                open(f"/proc/{pid}/environ", "rb").close()
                found["environ"] = "readable"
            except PermissionError:
                found["environ"] = "refused"
            found["limits"] = next(line for line in open(f"/proc/{pid}/limits") if line.startswith("Max core"))
            conn.sendall((json.dumps({"ok": True, "answer": "hunter2"}) + "\n").encode())
    helper = threading.Thread(target=answer, daemon=True)
    helper.start()
    # the pid is written by a wrapper before it execs the helper, so the pid is the helper's
    wrapper = (f"echo $$ > {shlex.quote(path)}.pid; "
               f"exec {shlex.quote(sys.executable)} -P -m datoolkit.sessions.askpass 'Password:'")
    out = subprocess.run(["/bin/sh", "-c", wrapper], capture_output=True, text=True, timeout=30,
                         env={**_env(), "DATOOLKIT_SOCK": path, "DATOOLKIT_TOKEN": "t"})
    helper.join(10)
    server.close()
    assert out.stdout == "hunter2\n", out.stderr
    assert found["owner"] == 0, "not dumpable: the kernel owns the files of its /proc entry"
    assert found["environ"] == "refused", "nor may this user's other processes read it"
    assert found["limits"].split()[4:6] == ["0", "0"], found["limits"]


def test_the_window_asks_gtk_for_its_own_dialogs_where_the_portal_would_refuse_it(monkeypatch):
    """xdg-desktop-portal refuses every request of a process that isn't dumpable, so where the
    desktop asks GTK for the portal's dialogs (GTK_USE_PORTAL=1), the window's Attach photo
    dialog would never open: GTK's own is asked for, for the app alone (app.own_dialogs)."""
    from datoolkit import app, hostenv

    monkeypatch.setattr(hostenv, "ORIGINAL", {})
    monkeypatch.setenv("GTK_USE_PORTAL", "1")
    app.own_dialogs()
    assert os.environ["GTK_USE_PORTAL"] == "0"
    assert hostenv.host_env()["GTK_USE_PORTAL"] == "1", "a shell, ssh or the file manager: as the desktop asks"
    hostenv.put_back("GTK_USE_PORTAL")
    assert os.environ["GTK_USE_PORTAL"] == "1" and not hostenv.ORIGINAL
    for unasked in (None, "0", ""):
        if unasked is None:
            monkeypatch.delenv("GTK_USE_PORTAL", raising=False)
        else:
            monkeypatch.setenv("GTK_USE_PORTAL", unasked)
        app.own_dialogs()
        assert os.environ.get("GTK_USE_PORTAL") == unasked and not hostenv.ORIGINAL


def test_gtk_is_told_before_it_starts_and_a_browser_opened_instead_isnt(monkeypatch):
    import webbrowser

    import uvicorn

    from datoolkit import app, hostenv

    monkeypatch.setattr(hostenv, "ORIGINAL", {})
    monkeypatch.setenv("GTK_USE_PORTAL", "1")
    seen = {}

    def no_window():
        seen["as_gtk_starts"] = os.environ.get("GTK_USE_PORTAL")
        return "There is no graphical display."
    monkeypatch.setattr(app, "window_problem", no_window)
    monkeypatch.setattr(webbrowser, "open", lambda url: seen.setdefault("browser", os.environ.get("GTK_USE_PORTAL")))

    class Server:                      # starts, and is done at once
        def __init__(self, config):
            self.started, self.should_exit = False, False

        def run(self):
            self.started = True
    monkeypatch.setattr(uvicorn, "Server", Server)
    app.main(["--window", "--port", "48579"])
    assert seen == {"as_gtk_starts": "0", "browser": "1"}
