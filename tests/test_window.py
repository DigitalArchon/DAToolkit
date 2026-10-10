"""Window sessions: listing and capturing X11 windows (against a private Xvfb display, so
nothing appears on the real desktop), the engine and server around them, the session-cut rules
and what the model is told."""

import base64
import io
import os
import shutil
import subprocess
import sys
import time

import pytest
from PIL import Image
from starlette.testclient import TestClient

from datoolkit.engine import UserError
from datoolkit.llm import prompts
from datoolkit.safety.risk import session_impact
from datoolkit.server.app import create_app
from datoolkit.sessions import xwindow

from test_engine import env  # noqa: F401

COLOUR = (0x20, 0x50, 0xC0)

# Two windows: a "ScreenConnect" one filled with COLOUR, and a plain one the test can hide
# (withdraw) by writing "hide" to stdin. Prints "ready" once both are mapped.
TK_APP = r"""
import sys, threading, tkinter as tk
root = tk.Tk(className="ScreenConnect.WindowsClient")
root.title("ScreenConnect - TESTPC")
root.geometry("320x200+10+10")
root.configure(background="#2050c0")
other = tk.Toplevel(root, class_="Notes")
other.title("Notes")
other.geometry("200x100+400+10")
def ready():
    print("ready", flush=True)
def listen():
    for line in sys.stdin:
        if line.strip() == "hide":
            root.after(0, other.withdraw)
threading.Thread(target=listen, daemon=True).start()
root.after(300, ready)
root.mainloop()
"""


def _free_display() -> int:
    for n in range(73, 200):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
            return n
    pytest.skip("no free X display number")


@pytest.fixture
def xdisplay(monkeypatch):
    if not shutil.which("Xvfb"):
        pytest.skip("Xvfb is not installed")
    try:
        import tkinter  # noqa: F401
    except ImportError:
        pytest.skip("tkinter is not available")
    n = _free_display()
    xvfb = subprocess.Popen(["Xvfb", f":{n}", "-screen", "0", "800x600x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                break
            time.sleep(0.05)
        else:
            pytest.skip("Xvfb didn't start")
        monkeypatch.setenv("DISPLAY", f":{n}")
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        yield f":{n}"
    finally:
        xvfb.terminate()
        xvfb.wait(5)


@pytest.fixture
def tkwin(xdisplay):
    p = subprocess.Popen([sys.executable, "-c", TK_APP], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         text=True, env={**os.environ, "DISPLAY": xdisplay})
    try:
        if p.stdout.readline().strip() != "ready":
            pytest.skip("Tk couldn't open a window")
        # the X ids of the windows carrying the titles (Tk's own ids are of inner windows)
        ids = {w.title: w.xid for w in xwindow.list_windows()}
        yield p, ids["ScreenConnect - TESTPC"], ids["Notes"]
    finally:
        p.kill()
        p.wait(5)


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- xwindow

def test_lists_windows_remote_tools_first(tkwin):
    _, main, other = tkwin
    wins = xwindow.list_windows()
    ids = [w.xid for w in wins]
    assert main in ids and other in ids
    first = wins[0]
    assert first.xid == main and first.remote_tool and first.visible
    assert first.title == "ScreenConnect - TESTPC" and first.wm_class.lower() == "screenconnect.windowsclient"
    assert (first.width, first.height) == (320, 200)
    assert next(w for w in wins if w.xid == other).remote_tool is False


def test_captures_the_window_pixels(tkwin):
    _, main, _ = tkwin
    png, notes = xwindow.capture(main)
    img = Image.open(io.BytesIO(png))
    assert img.size == (320, 200) and img.mode == "RGB"
    assert img.getpixel((160, 100)) == COLOUR            # byte order right: not swapped to (C0, 50, 20)
    assert notes["title"] == "ScreenConnect - TESTPC" and notes["composited"] is False


def test_hidden_and_closed_windows_are_refused(tkwin):
    p, main, other = tkwin
    p.stdin.write("hide\n")
    p.stdin.flush()
    assert _wait(lambda: not xwindow.window_info(other).visible)
    with pytest.raises(xwindow.WindowError, match="minimised"):
        xwindow.capture(other)
    p.kill()
    p.wait(5)
    assert _wait(lambda: xwindow.window_info(main) is None)
    with pytest.raises(xwindow.WindowError, match="closed"):
        xwindow.capture(main)


def test_unavailable_without_x(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert "Wayland" in xwindow.available()
    with pytest.raises(xwindow.WindowError, match="Wayland"):
        xwindow.list_windows()
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert "X11" in xwindow.available()


# ---------------------------------------------------------------- engine and server

async def test_open_window_session_and_screenshot(env, tkwin):  # noqa: F811
    engine, _, _ = env
    p, main, _ = tkwin
    with pytest.raises(UserError, match="Start a case"):
        await engine.open_window(main)
    engine.new_case("sc", "open")
    listed = await engine.list_windows()
    assert listed["available"] and listed["windows"][0]["xid"] == main and not listed["windows"][0]["open"]

    sess = await engine.open_window(main, "TESTPC", "Windows 11")
    assert sess["kind"] == "window" and sess["id"] == "testpc" and sess["os_hint"] == "Windows 11"
    assert sess["target"].lower() == 'screenconnect.windowsclient window "screenconnect - testpc"'
    assert (await engine.list_windows())["windows"][0]["open"]
    with pytest.raises(UserError, match="already open as session testpc"):
        await engine.open_window(main)

    shot = await engine.window_shot("testpc")
    img = Image.open(io.BytesIO(base64.b64decode(shot["image"].split(",", 1)[1])))
    assert img.getpixel((5, 5)) == COLOUR
    await engine.window_shot("testpc", preview=True)
    events = (engine.case.dir / "events.jsonl").read_text()
    assert events.count('"window_screenshot"') == 1           # previews aren't logged

    p.kill()
    p.wait(5)
    assert _wait(lambda: xwindow.window_info(main) is None)
    with pytest.raises(UserError, match="closed"):
        await engine.window_shot("testpc")
    assert engine.sessions.sessions["testpc"].exited
    with pytest.raises(UserError, match="has closed"):
        await engine.window_shot("testpc")
    engine.close_session("testpc")
    assert "testpc" not in engine.sessions.sessions


async def test_window_items_get_window_cut_rules(env, tkwin):  # noqa: F811
    engine, _, _ = env
    _, main, _ = tkwin
    engine.new_case("cuts", "open")
    await engine.open_window(main, "pc1")
    added = engine.queue.add("c1", [
        {"session_id": "pc1", "command": 'Restart-Service "ScreenConnect Client (1234)"', "purpose": "[PowerShell, as admin] x",
         "risk": "disruptive"},
        {"session_id": "pc1", "command": "Get-Service Spooler", "purpose": "[PowerShell] y", "risk": "read_only"}],
        session_kinds=engine._session_kinds())
    assert "remote-support agent" in added[0].cuts_session and not added[1].cuts_session


def test_server_routes_without_x(tmp_path, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    holder = {}

    def make(emit):
        from datoolkit.config import Config
        from datoolkit.engine import Engine
        holder["e"] = Engine(Config(), emit, tmp_path / "rt", save_config=lambda c: None)
        return holder["e"]

    with TestClient(create_app("tok", make)) as client:
        hdr = {"X-Token": "tok"}
        r = client.get("/api/windows", headers=hdr).json()
        assert r["available"] is False and "X11" in r["why"] and r["windows"] == []
        assert client.get("/api/windows").status_code == 403
        holder["e"].new_case("nox", "open")
        r = client.post("/api/sessions", json={"kind": "window", "xid": 123}, headers=hdr)
        assert r.status_code == 400 and "X11" in r.json()["error"]
        r = client.post("/api/sessions", json={"kind": "window", "xid": None}, headers=hdr)
        assert r.status_code == 400 and r.json()["error"] == "Pick a window from the list."
        r = client.get("/api/sessions/nope/shot", headers=hdr)
        assert r.status_code == 400 and "not a window session" in r.json()["error"]


# ---------------------------------------------------------------- rules and prompts

def test_window_session_cut_rules():
    assert "remote-support agent" in session_impact('Stop-Service -Name "ScreenConnect Client (abc)"', "window")
    assert "remote-support agent" in session_impact("net stop TeamViewer", "window")
    assert "kills" in session_impact("taskkill /F /IM AnyDesk.exe", "window")
    assert session_impact("logoff", "window") and session_impact("ipconfig /release", "window")
    assert session_impact("Restart-Service WinRM", "window") is None       # not what carries this session
    assert session_impact("Get-Service | Select-Object -First 20", "window") is None
    assert session_impact("Stop-Service Spooler", "window") is None


def test_model_is_told_to_paste_and_read_screenshots():
    win = {"id": "pc1", "kind": "window", "target": 'ScreenConnect window "PC1"', "os_hint": "Windows 11",
           "device": "dev1", "shell": "x"}
    line = prompts.session_roster([win])
    assert "PASTES your commands" in line and "PowerShell or cmd" in line and "shell: x" not in line
    ssh = {"id": "pc1-ssh", "kind": "ssh", "target": "admin@pc1", "device": "dev1", "shell": "remote shell/CLI"}
    grouped = prompts.session_roster([win, ssh])
    assert "send commands to `pc1-ssh`" in grouped and "`pc1` is what the technician sees" in grouped
    assert "[PowerShell, as admin]" in prompts.SYSTEM_PROMPT and "| clip" in prompts.SYSTEM_PROMPT
