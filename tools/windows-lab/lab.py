"""Harness for driving a throwaway DA Toolkit instance against the lab's Windows 11 VM.

The instance has its own XDG dirs and ports (4859x), no AI providers, and test host names
(LabW11-*) that match nothing in the real keyring. Prompts are answered over /ws/events,
terminals driven over /ws/term as the page does (bracketed paste, then Enter)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path("/home/user/Documents/DAToolkit")
# the Python that runs the instance: the repo's venv, or another (a venv with the kerberos extra)
PY = os.environ.get("WINLAB_PYTHON") or str(REPO / ".venv/bin/python")
# instances, screenshots, browser profiles and reports: outside the repo
SCRATCH = Path(os.environ.get("WINLAB_WORK", "~/.cache/datoolkit-winlab")).expanduser()
(SCRATCH / "win").mkdir(parents=True, exist_ok=True)
WIN = "192.168.1.214"


# the lab's secrets (never in this repo): ~/.config/testlab, or a copy elsewhere (WINLAB_CREDS)
CREDS = Path(os.environ.get("WINLAB_CREDS") or "~/.config/testlab").expanduser()


def vm_creds() -> tuple[str, str]:
    vals = {}
    for line in (CREDS / "vm-credentials").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            vals[k.strip()] = shlex.split(v)[0] if v.strip() else ""
    return vals["VM_USER"], vals["VM_PASS"]


USER, PASS = vm_creds()
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[()][0-9A-B]|\x1b[=>]|\r")


def plain(b: bytes | str) -> str:
    s = b.decode("utf-8", "replace") if isinstance(b, bytes) else b
    return ANSI.sub("", s)


class App:
    def __init__(self, name: str, port: int = 48591, extra_env: dict | None = None):
        self.root = SCRATCH / "inst" / name
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.events: list[dict] = []
        self.prompts: list[dict] = []
        self.rules: list[tuple[str, str | None]] = []    # (regex on the prompt text, answer); None = cancel
        self.proc: subprocess.Popen | None = None
        self.token = ""
        self.extra_env = extra_env or {}

    def env(self) -> dict:
        return {**os.environ, "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_DATA_HOME": str(self.root / "data"),
                "XDG_STATE_HOME": str(self.root / "state"), "XDG_CACHE_HOME": str(self.root / "cache"), **self.extra_env}

    def start(self, fresh: bool = True) -> None:
        if fresh and self.root.exists():
            shutil.rmtree(self.root)
        for d in ("config", "data", "state", "cache"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self.log = open(self.root / "app.log", "a")
        # WINLAB_APP: an AppImage's AppRun (extracted), run as a technician would, instead of a Python
        app = [os.environ["WINLAB_APP"]] if os.environ.get("WINLAB_APP") else [PY, "-m", "datoolkit"]
        self.proc = subprocess.Popen(app + ["--no-open", "--port", str(self.port)], cwd=REPO if REPO.is_dir() else Path.home(),
                                     env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in self.proc.stdout:
            self.log.write(line)
            if "#t=" in line:
                self.token = line.strip().split("#t=", 1)[1]
                break
        else:
            raise SystemExit("app exited before printing its URL:\n" + (self.root / "app.log").read_text())
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        for line in self.proc.stdout:
            self.log.write(line)
            self.log.flush()

    def api(self, method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
        req = urllib.request.Request(self.base + path, method=method,
                                     headers={"X-Token": self.token, "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                return json.loads(data) if data else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors="replace")
            try:
                detail = json.loads(text).get("detail", text)
            except ValueError:
                detail = text
            return {"_error": e.code, "detail": detail}

    async def aapi(self, method: str, path: str, body: dict | None = None, timeout: float = 120) -> dict:
        return await asyncio.to_thread(self.api, method, path, body, timeout)

    def quit(self) -> None:
        if not self.proc:
            return
        try:
            self.api("POST", "/api/quit", timeout=5)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(5)
        self.proc = None

    # ------------------------------------------------------------------ events and prompts
    async def listen(self) -> None:
        import websockets

        async with websockets.connect(f"ws://127.0.0.1:{self.port}/ws/events?t={self.token}", max_size=None) as ws:
            async for raw in ws:
                ev = json.loads(raw)
                self.events.append(ev)
                if ev.get("type") == "prompt":
                    p = ev["prompt"]
                    self.prompts.append(p)
                    answer = "__unanswered__"
                    for rx, ans in self.rules:
                        if re.search(rx, p["text"], re.S | re.I):
                            answer = ans
                            break
                    if answer != "__unanswered__":
                        await self.aapi("POST", f"/api/prompts/{p['id']}", {"answer": answer, "save": False})

    def toasts(self) -> list[str]:
        return [e.get("text", "") for e in self.events if e.get("type") == "toast"]


class Term:
    def __init__(self, app: App, sid: str):
        self.app, self.sid = app, sid
        self.buf = b""
        self.ws = None

    async def __aenter__(self):
        import websockets

        self.ws = await websockets.connect(f"ws://127.0.0.1:{self.app.port}/ws/term/{self.sid}?t={self.app.token}",
                                           max_size=None)
        self.reader = asyncio.create_task(self._read())
        await self.ws.send(json.dumps({"type": "resize", "cols": 160, "rows": 45}))
        return self

    async def __aexit__(self, *exc):
        self.reader.cancel()
        await self.ws.close()

    async def _read(self):
        try:
            async for data in self.ws:
                self.buf += data if isinstance(data, bytes) else data.encode()
        except Exception:  # noqa: BLE001
            pass

    def text(self, since: int = 0) -> str:
        return plain(self.buf[since:])

    async def wait(self, rx: str, since: int = 0, timeout: float = 60) -> str | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            t = self.text(since)
            m = re.search(rx, t, re.S)
            if m:
                return t
            if self.reader.done():
                break
            await asyncio.sleep(0.1)
        return None

    async def send(self, data: str) -> None:
        await self.ws.send(json.dumps({"type": "input", "data": data}))

    async def paste(self, cmd: str, enter: bool = True) -> int:
        """As xterm.js's paste: bracketed when the program turned that on, \\n as \\r."""
        mark = len(self.buf)
        text = cmd.replace("\r\n", "\r").replace("\n", "\r")
        if b"\x1b[?2004h" in self.buf and self.buf.rfind(b"\x1b[?2004h") > self.buf.rfind(b"\x1b[?2004l"):
            text = "\x1b[200~" + text + "\x1b[201~"
        await self.send(text)
        if enter:
            await self.send("\r")
        return mark
