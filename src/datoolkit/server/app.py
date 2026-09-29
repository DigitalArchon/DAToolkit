"""Local HTTP/WebSocket server for the GUI. Bound to 127.0.0.1 and gated by a per-launch token."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..engine import Engine, UserError

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def create_app(token: str, make_engine: Callable[[Callable[[dict], None]], Engine]) -> FastAPI:
    listeners: set[asyncio.Queue] = set()

    def emit(event: dict) -> None:
        for q in list(listeners):
            q.put_nowait(event)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = make_engine(emit)
        await app.state.engine.start()
        try:
            yield
        finally:
            await app.state.engine.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def check(t: str | None) -> bool:
        return t is not None and hmac.compare_digest(t, token)

    def auth(request: Request) -> Engine:
        if not check(request.headers.get("x-token")):
            raise HTTPException(403, "bad token")
        return request.app.state.engine

    @app.exception_handler(UserError)
    async def user_error(_: Request, exc: UserError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ------------------------------------------------------------ REST

    @app.get("/api/state")
    async def state(e: Engine = Depends(auth)):
        return e.snapshot()

    @app.post("/api/providers")
    async def save_provider(body: dict, e: Engine = Depends(auth)):
        e.save_provider(body.get("provider", {}), body.get("api_key") or None, body.get("original_name"))
        return {"ok": True}

    @app.delete("/api/providers/{name}")
    async def delete_provider(name: str, e: Engine = Depends(auth)):
        e.delete_provider(name)
        return {"ok": True}

    @app.get("/api/models")
    async def models(provider: str, refresh: bool = False, e: Engine = Depends(auth)):
        return {"models": await e.list_models(provider, refresh)}

    @app.post("/api/model")
    async def select_model(body: dict, e: Engine = Depends(auth)):
        e.select_model(body["provider"], body["model"])
        return {"ok": True}

    @app.post("/api/attest")
    async def attest(e: Engine = Depends(auth)):
        return await e.attest_now()

    @app.post("/api/hosts")
    async def save_host(body: dict, e: Engine = Depends(auth)):
        e.save_host(body.get("host", {}), body.get("password") or None, body.get("original_name"))
        return {"ok": True}

    @app.delete("/api/hosts/{name}")
    async def delete_host(name: str, e: Engine = Depends(auth)):
        e.delete_host(name)
        return {"ok": True}

    @app.post("/api/hosts/{name}/forget-password")
    async def forget_password(name: str, e: Engine = Depends(auth)):
        e.forget_host_password(name)
        return {"ok": True}

    @app.post("/api/settings")
    async def save_settings(body: dict, e: Engine = Depends(auth)):
        e.save_settings(body)
        return {"ok": True}

    @app.post("/api/case")
    async def new_case(body: dict, e: Engine = Depends(auth)):
        e.new_case(body.get("name", ""), body.get("sensitivity", "open"), body.get("notes", ""))
        return {"ok": True}

    @app.post("/api/sessions")
    async def open_session(body: dict, e: Engine = Depends(auth)):
        return e.open_session(body.get("kind", "local"), body.get("host", ""))

    @app.delete("/api/sessions/{sid}")
    async def close_session(sid: str, e: Engine = Depends(auth)):
        e.close_session(sid)
        return {"ok": True}

    @app.post("/api/sessions/{sid}/hint")
    async def session_hint(sid: str, body: dict, e: Engine = Depends(auth)):
        e.set_session_hint(sid, body.get("os_hint", ""))
        return {"ok": True}

    @app.post("/api/queue/{num}")
    async def update_item(num: int, body: dict, e: Engine = Depends(auth)):
        fields = {k: body[k] for k in ("command", "session_id", "status", "note") if k in body}
        e.update_item(num, **fields)
        return {"ok": True}

    @app.post("/api/queue/{num}/move")
    async def move_item(num: int, body: dict, e: Engine = Depends(auth)):
        e.move_item(num, int(body.get("delta", 0)))
        return {"ok": True}

    @app.post("/api/preview")
    async def preview(body: dict, e: Engine = Depends(auth)):
        return {"items": e.preview([str(t) for t in body.get("texts", [])])}

    @app.post("/api/send")
    async def send(body: dict, e: Engine = Depends(auth)):
        e.send(body.get("message", ""), body.get("results"), body.get("snippets"))
        return {"ok": True}

    @app.post("/api/stop")
    async def stop(e: Engine = Depends(auth)):
        e.stop_turn()
        return {"ok": True}

    @app.post("/api/prompts/{pid}")
    async def answer_prompt(pid: str, body: dict, e: Engine = Depends(auth)):
        e.answer_prompt(pid, body.get("answer"), bool(body.get("save")))
        return {"ok": True}

    @app.post("/api/export/markdown")
    async def export_md(e: Engine = Depends(auth)):
        return {"path": e.export_markdown()}

    @app.post("/api/export/summary")
    async def export_summary(e: Engine = Depends(auth)):
        return await e.ticket_summary()

    @app.post("/api/open-folder")
    async def open_folder(e: Engine = Depends(auth)):
        if e.case:
            await asyncio.create_subprocess_exec("xdg-open", str(e.case.dir))
        return {"ok": True}

    # ------------------------------------------------------------ WebSockets

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket):
        if not check(ws.query_params.get("t")):
            await ws.close(code=4403)
            return
        await ws.accept()
        q: asyncio.Queue = asyncio.Queue()
        listeners.add(q)
        try:
            await ws.send_text(json.dumps({"type": "state", "state": app.state.engine.snapshot()}))
            receiver = asyncio.create_task(ws.receive_text())
            while True:
                getter = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    getter.cancel()
                    receiver.result()  # raises on disconnect
                    receiver = asyncio.create_task(ws.receive_text())
                    continue
                await ws.send_text(json.dumps(getter.result(), default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            listeners.discard(q)

    @app.websocket("/ws/term/{sid}")
    async def ws_term(ws: WebSocket, sid: str):
        engine: Engine = app.state.engine
        if not check(ws.query_params.get("t")) or sid not in engine.sessions.sessions:
            await ws.close(code=4403)
            return
        await ws.accept()
        backlog, q = engine.sessions.subscribe(sid)

        async def pump_out():
            if backlog:
                await ws.send_bytes(backlog)
            while True:
                data = await q.get()
                if data is None:
                    await ws.close()
                    return
                await ws.send_bytes(data)

        out_task = asyncio.create_task(pump_out())
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if sid not in engine.sessions.sessions:
                    break
                if msg.get("type") == "input":
                    engine.sessions.write(sid, msg["data"].encode())
                elif msg.get("type") == "resize":
                    engine.sessions.resize(sid, int(msg["cols"]), int(msg["rows"]))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            out_task.cancel()
            engine.sessions.unsubscribe(sid, q)

    return app


def runtime_dir() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    # sweep leftovers from instances that were killed before they could clean up
    for d in base.glob("datoolkit-*"):
        pid = d.name.removeprefix("datoolkit-")
        if pid.isdigit() and not Path(f"/proc/{pid}").exists() and d.owner() == os.environ.get("USER", d.owner()):
            shutil.rmtree(d, ignore_errors=True)
    return base / f"datoolkit-{os.getpid()}"
