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
from datoolkit.hostenv import browsers_get_the_original_environment as REAL_BROWSERS

SRC = str(Path(__file__).resolve().parents[1] / "src")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux's prctl and /proc")


def _env() -> dict:
    return {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}


@linux_only
def test_the_process_writes_no_core_file_and_is_not_dumpable():
    """As the standard has it (Nov 14 2026): not dumpable, the soft core limit 1 (the kernel aborts a
    core at exactly 1, which systemd-coredump's pipe doesn't at 0), the hard limit as it was, and an
    empty coredump_filter, written before the process stops being dumpable (its /proc files are
    root's after)."""
    probe = (
        "import ctypes, resource\n"
        "libc = ctypes.CDLL(None)\n"
        "before = libc.prctl(3, 0, 0, 0, 0)\n"          # PR_GET_DUMPABLE
        "hard = resource.getrlimit(resource.RLIMIT_CORE)[1]\n"
        "from datoolkit.coredumps import no_core_dumps\n"
        "no_core_dumps()\n"
        "soft, after = resource.getrlimit(resource.RLIMIT_CORE)\n"
        "print(before, libc.prctl(3, 0, 0, 0, 0), soft, after == hard,\n"
        "      open('/proc/self/coredump_filter').read().strip())\n"
    )
    out = subprocess.run(["prlimit", "--core=0:unlimited", sys.executable, "-c", probe], capture_output=True,
                         text=True, env=_env(), check=True)
    assert out.stdout.split() == ["1", "0", "1", "True", "00000000"]


@linux_only
def test_a_hard_limit_of_nothing_is_left_as_it_is():
    """Where the core's hard limit is 0 already, the soft can't be 1: it is left at 0, and the rest done."""
    probe = (
        "import ctypes, resource\n"
        "from datoolkit.coredumps import no_core_dumps\n"
        "no_core_dumps()\n"
        "print(ctypes.CDLL(None).prctl(3, 0, 0, 0, 0), *resource.getrlimit(resource.RLIMIT_CORE))\n"
    )
    out = subprocess.run(["prlimit", "--core=0:0", sys.executable, "-c", probe], capture_output=True,
                         text=True, env=_env(), check=True)
    assert out.stdout.split() == ["0", "0", "0"]


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
    limit's soft limit is 1 (the hard one left as it was)."""
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
    assert found["limits"].split()[4] == "1", found["limits"]


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
    monkeypatch.setattr(webbrowser, "open", lambda url, *a: seen.setdefault("browser", os.environ.get("GTK_USE_PORTAL")))

    class Server:                      # starts, and is done at once
        def __init__(self, config):
            self.started, self.should_exit = False, False

        def run(self):
            self.started = True
    monkeypatch.setattr(uvicorn, "Server", Server)
    app.main(["--window", "--port", "48579"])
    assert seen == {"as_gtk_starts": "0", "browser": "1"}


def test_the_browser_the_app_starts_gets_the_desktops_portal_choice(monkeypatch):
    """The app sets GTK_USE_PORTAL=0 for itself alone (the portal refuses an undumpable process);
    a browser webbrowser starts, for the app or for a link clicked in the window (pywebview opens it
    with webbrowser.open), gets what the desktop asked for, and the app keeps its own after."""
    import webbrowser

    from datoolkit import app, hostenv

    monkeypatch.setattr(hostenv, "ORIGINAL", {})
    monkeypatch.setattr(hostenv, "_REAL_OPEN", None)
    monkeypatch.setenv("GTK_USE_PORTAL", "1")
    seen = []
    monkeypatch.setattr(webbrowser, "open", lambda url, new=0, autoraise=True: seen.append(
        (url, os.environ.get("GTK_USE_PORTAL"))) or True)
    app.own_dialogs()
    REAL_BROWSERS()                     # the real one: the test run's is a stub (conftest)
    assert webbrowser.open is hostenv.open_url
    webbrowser.open("https://example.test/", 2, True)       # as pywebview opens a link
    app.browser_opener(9, type("L", (), {"new": lambda self: "code"})())()
    assert seen == [("https://example.test/", "1"), ("http://127.0.0.1:9/#c=code", "1")]
    assert os.environ["GTK_USE_PORTAL"] == "0"
    hostenv.put_back("GTK_USE_PORTAL")


def test_the_app_puts_its_browsers_on_the_original_environment_at_start(monkeypatch):
    from datoolkit import app, hostenv

    calls = []
    monkeypatch.setattr(hostenv, "browsers_get_the_original_environment", lambda: calls.append(1))

    def later(*args, **kwargs):
        raise _Later
    monkeypatch.setattr(app.argparse, "ArgumentParser", later)
    with pytest.raises(_Later):
        app.main([])
    assert calls == [1]
