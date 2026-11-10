"""Headless Firefox over WebDriver BiDi: just enough to load the app and run its JS."""

import asyncio
import base64
import itertools
import json
import shutil
import subprocess
import time
from pathlib import Path


class Firefox:
    def __init__(self, profile: Path, port: int = 48600):
        self.profile, self.port = profile, port
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.ctx = None

    async def __aenter__(self):
        import websockets

        if self.profile.exists():
            shutil.rmtree(self.profile)
        self.profile.mkdir(parents=True)
        self.proc = subprocess.Popen(["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=3G", "-p", "MemorySwapMax=0",
                                      "firefox", "--headless", "--no-remote", "--profile", str(self.profile),
                                      "--remote-debugging-port", str(self.port)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                self.ws = await websockets.connect(f"ws://127.0.0.1:{self.port}/session", max_size=None)
                break
            except OSError:
                await asyncio.sleep(0.3)
        else:
            raise RuntimeError("Firefox's BiDi port never opened")
        self.reader = asyncio.create_task(self._read())
        await self.cmd("session.new", {"capabilities": {}})
        self.ctx = (await self.cmd("browsingContext.create", {"type": "tab"}))["context"]
        return self

    async def __aexit__(self, *exc):
        try:
            await asyncio.wait_for(self.cmd("session.end", {}), 5)
        except Exception:  # noqa: BLE001
            pass
        self.reader.cancel()
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        subprocess.run(["pkill", "-f", "--", f"--profile {self.profile}"], check=False)

    async def _read(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            fut = self.pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                if msg.get("type") == "error":
                    fut.set_exception(RuntimeError(f"{msg.get('error')}: {msg.get('message')}"))
                else:
                    fut.set_result(msg.get("result"))

    async def cmd(self, method, params, timeout=120):
        i = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[i] = fut
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params}))
        return await asyncio.wait_for(fut, timeout)

    async def viewport(self, w, h):
        await self.cmd("browsingContext.setViewport", {"context": self.ctx, "viewport": {"width": w, "height": h}})

    async def goto(self, url):
        await self.cmd("browsingContext.navigate", {"context": self.ctx, "url": url, "wait": "complete"})

    async def js(self, expr: str, timeout=120):
        """Evaluate an async function body; its return value goes through JSON.stringify."""
        src = f"(async () => {{ const __r = await (async () => {{ {expr} }})(); return JSON.stringify(__r === undefined ? null : __r); }})()"
        res = await self.cmd("script.evaluate", {"expression": src, "target": {"context": self.ctx},
                                                 "awaitPromise": True, "resultOwnership": "none"}, timeout)
        if res.get("type") == "exception":
            raise RuntimeError(res["exceptionDetails"].get("text"))
        v = res["result"]
        return json.loads(v["value"]) if v.get("type") == "string" else None

    async def shot(self, path: Path):
        res = await self.cmd("browsingContext.captureScreenshot", {"context": self.ctx})
        path.write_bytes(base64.b64decode(res["data"]))

    async def wait_js(self, expr, timeout=30, every=0.3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            v = await self.js(expr)
            if v:
                return v
            await asyncio.sleep(every)
        return None
