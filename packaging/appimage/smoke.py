"""Smoke test for an extracted AppImage, run by the AppImage's own Python inside a distro
container with a virtual display (see packaging/test-appimage.sh).

    smoke.py <AppDir> <out-dir> <name> fallback   WebKitGTK absent: opens in the browser, says why
    smoke.py <AppDir> <out-dir> <name> full       WebKitGTK present: app window, a live terminal
                                                  session, and a screenshot of the page"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request

APPDIR, OUT, NAME, MODE = sys.argv[1:5]
ENV = {**os.environ, "XDG_CONFIG_HOME": "/tmp/smoke/config", "XDG_DATA_HOME": "/tmp/smoke/data"}


def start(*args: str) -> tuple[subprocess.Popen, str, str]:
    """Run the app, return (process, base URL, token) once it prints its URL."""
    proc = subprocess.Popen([f"{APPDIR}/AppRun", "--port", "8790", *args], env=ENV, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for line in proc.stdout:
        print(f"  | {line.rstrip()}")
        if line.startswith("http://"):
            return proc, "http://127.0.0.1:8790", line.strip().split("t=", 1)[1]
    raise SystemExit("the app exited before printing its URL")


def api(base: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(base + path, method=method, headers={"X-Token": token, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(req, timeout=10))


def check(ok: bool, what: str) -> None:
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        raise SystemExit(1)


if MODE == "fallback":
    # webbrowser honours $BROWSER: "echo" stands in for opening a browser
    proc, base, token = start()
    st = api(base, token, "GET", "/api/state")
    check("WebKitGTK" in st["ui_notice"] and "Running in your web browser" in st["ui_notice"],
          f"{NAME}: without WebKitGTK it opens in the browser and says why")
    api(base, token, "POST", "/api/quit")
    proc.wait(10)
    check(proc.returncode is not None, f"{NAME}: Quit ends the process")
    sys.exit(0)

# --- full: the app window first
win = subprocess.Popen([f"{APPDIR}/AppRun"], env=ENV, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(8)
alive = win.poll() is None
win.terminate()
log = win.communicate(timeout=10)[0]
ok = alive and "Traceback" not in log and "Running in your web browser" not in log
check(ok, f"{NAME}: the app window starts and stays up" + ("" if ok else "\n" + "\n".join(f"  | {x}" for x in log.splitlines()[-15:])))

# a terminal session, through the server, as the GUI drives it
proc, base, token = start("--no-open")
api(base, token, "POST", "/api/case", {"name": f"smoke {NAME}", "sensitivity": "open"})
sid = api(base, token, "POST", "/api/sessions", {"kind": "local"})["id"]


async def shell() -> str:
    import websockets

    async with websockets.connect(f"ws://127.0.0.1:8790/ws/term/{sid}?t={token}") as ws:
        await ws.send(json.dumps({"type": "input", "data": "echo AB$((6*7))CD; tty\r"}))
        seen = b""
        while b"AB42CD" not in seen:
            seen += await asyncio.wait_for(ws.recv(), 10)
        await asyncio.sleep(0.5)
        try:
            seen += await asyncio.wait_for(ws.recv(), 1)
        except asyncio.TimeoutError:
            pass
        return seen.decode(errors="replace")

out = asyncio.run(shell())
check("AB42CD" in out and "/dev/pts/" in out, f"{NAME}: a local terminal session runs commands on a real PTY")

# the page itself, rendered by the host's WebKitGTK through the bundled PyGObject
sys.path.insert(0, f"{APPDIR}/usr/python/lib/python3.12/site-packages")
from datoolkit.app import _bundled_girepository  # noqa: E402

_bundled_girepository()
import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

shot = f"{OUT}/{NAME}.png"
window = Gtk.OffscreenWindow()
view = WebKit2.WebView()
view.set_size_request(1300, 850)
window.add(view)
window.show_all()


def snap() -> bool:
    view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE, WebKit2.SnapshotOptions.NONE, None,
                      lambda v, r: (v.get_snapshot_finish(r).write_to_png(shot), Gtk.main_quit()))
    return False


view.connect("load-changed", lambda v, e: e == WebKit2.LoadEvent.FINISHED and GLib.timeout_add(2500, snap))
view.load_uri(f"{base}/?t={token}")
GLib.timeout_add(30000, Gtk.main_quit)
Gtk.main()
check(os.path.exists(shot), f"{NAME}: the page renders ({shot})")
api(base, token, "POST", "/api/quit")
proc.wait(10)
