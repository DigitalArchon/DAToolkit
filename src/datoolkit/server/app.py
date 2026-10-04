"""Local HTTP/WebSocket server for the GUI, bound to 127.0.0.1 and gated by a per-launch token,
and the phone companion's app, served over TLS on its own port (see companion.py)."""

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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers

from .. import config
from ..companion import CompanionServer
from ..hostenv import host_env
from ..engine import Engine, UserError
from ..sessions import guac

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
COMPANION_FILES = {"style.css", "companion.js", "icon.svg"}
# a photo is at most 6 MB (8 MB as base64, see Engine._save_images) plus its description
MAX_COMPANION_BODY = 10 * 1024 * 1024


class CompanionGate:
    """ASGI middleware in front of the companion's app. FastAPI reads and parses a JSON body
    before it runs dependencies (where the token is checked), and nothing else limits its
    size, so without this anyone on the LAN could make the app buffer and parse any amount
    without the token, stalling the event loop the GUI and terminals share. An /api/ request
    is refused here on its headers alone: no valid token, no stated length, or a body over
    MAX_COMPANION_BODY. The connection is closed rather than read to the end."""

    def __init__(self, app, companion: CompanionServer):
        self.app = app
        self.companion = companion

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/"):
            refusal = self._refusal(Headers(scope=scope))
            if refusal:
                status, why = refusal
                await JSONResponse({"detail": why}, status, headers={"connection": "close"})(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _refusal(self, headers: Headers) -> tuple[int, str] | None:
        if not self.companion.check(headers.get("x-token")):
            return 403, "bad token"
        if "transfer-encoding" in headers:   # chunked: no length to check before reading
            return 411, "a Content-Length is required"
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            return 400, "bad Content-Length"
        if length > MAX_COMPANION_BODY:
            return 413, "request too large"
        return None


def create_app(token: str, make_engine: Callable[[Callable[[dict], None]], Engine],
               desktop=None, companion_dir: Path | None = None,
               on_quit: Callable[[], None] | None = None) -> FastAPI:
    """The GUI's app, for 127.0.0.1 only. It also builds the phone companion's app, which
    app.state.companion (companion.CompanionServer) serves on its own port over TLS when the
    technician starts it: that one can see chat, queue and hypotheses, mark items
    done/skipped and send a photo with a description, and nothing else. It never reaches a
    terminal.
    `desktop` (app.Desktop, only in the app window) serves the clipboard and the Save dialog.
    `companion_dir` holds the companion's certificate (default: the config directory).
    `on_quit` ends the process (POST /api/quit): in the browser, closing the tab doesn't."""
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
            await app.state.companion.stop()
            await app.state.engine.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    comp = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    companion = CompanionServer(comp, companion_dir or config.config_dir())
    comp.add_middleware(CompanionGate, companion=companion)
    app.state.companion = companion

    def check(t: str | None) -> bool:
        return t is not None and hmac.compare_digest(t, token)

    def auth(request: Request) -> Engine:
        if not check(request.headers.get("x-token")):
            raise HTTPException(403, "bad token")
        return request.app.state.engine

    def auth_companion(request: Request) -> Engine:
        if not companion.check(request.headers.get("x-token")):
            raise HTTPException(403, "bad token")
        return app.state.engine

    def companion_view(e: Engine) -> dict:
        snap = e.snapshot()
        vision = e.vision_status()
        return {"case": snap["case"], "chat": snap["chat"][-30:], "queue": snap["queue"], "busy": snap["busy"],
                "photos": {"ok": vision["mode"] != "none", "why": vision.get("why", "")},
                "hypotheses": snap["hypotheses"], "sessions": [{k: s[k] for k in ("id", "kind", "target", "exited")}
                                                               for s in snap["sessions"]]}

    async def user_error(_: Request, exc: UserError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    async def key_error(_: Request, exc: KeyError):
        # an unknown queue number, session id or prompt id is a stale client, not a crash
        return JSONResponse({"error": f"Not found: {exc.args[0] if exc.args else exc}"}, status_code=400)

    for a in (app, comp):
        a.add_exception_handler(UserError, user_error)
        a.add_exception_handler(KeyError, key_error)

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ phone companion (its own port, TLS)

    @comp.get("/")
    async def comp_root():
        return RedirectResponse("/pair")

    @comp.get("/pair")
    async def comp_pair():
        return FileResponse(WEB_DIR / "pair.html", headers={"Cache-Control": "no-store"})

    @comp.get("/companion")
    async def comp_page():
        return FileResponse(WEB_DIR / "companion.html", headers={"Cache-Control": "no-store"})

    @comp.get("/static/{name}")
    async def comp_static(name: str):
        if name not in COMPANION_FILES:  # the phone gets its page and nothing else of the GUI
            raise HTTPException(404)
        return FileResponse(WEB_DIR / name)

    @comp.get("/api/companion/state")
    async def companion_state(e: Engine = Depends(auth_companion)):
        return companion_view(e)

    @comp.post("/api/companion/queue/{num}")
    async def companion_mark(num: int, body: dict, e: Engine = Depends(auth_companion)):
        status = body.get("status")
        if status not in ("ran", "skipped", "pending"):
            raise HTTPException(400, "companion can only mark items ran, skipped or pending")
        e.update_item(num, status=status, note=str(body.get("note", ""))[:200])
        return {"ok": True}

    @comp.post("/api/companion/photo")
    async def companion_photo(body: dict, e: Engine = Depends(auth_companion)):
        """A photo with the technician's description, sent to the AI like one attached on the
        desktop (same case gates, cleaned by the server). Only a photo: the phone sends no
        results, snippets or bare text."""
        image = body.get("image")
        if not isinstance(image, str) or not image:
            raise HTTPException(400, "a photo is required")
        e.send(str(body.get("message", ""))[:4000], images=[image], via="phone")
        return {"ok": True}

    @comp.post("/api/companion/hypotheses/{hid}")
    async def companion_mark_h(hid: str, body: dict, e: Engine = Depends(auth_companion)):
        e.mark_hypothesis(hid, str(body.get("mark", "")))
        return {"ok": True}

    @comp.websocket("/ws/events")
    async def comp_events(ws: WebSocket):
        if not companion.check(ws.query_params.get("t")):
            await ws.close(code=4403)
            return
        await ws.accept()
        companion.sockets.add(ws)
        q: asyncio.Queue = asyncio.Queue()
        listeners.add(q)
        try:
            await ws.send_text(json.dumps({"type": "state", "state": companion_view(app.state.engine)}))
            await _changed_phones()
            receiver = asyncio.create_task(ws.receive_text())
            while True:
                getter = asyncio.create_task(q.get())
                done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    getter.cancel()
                    receiver.result()  # raises on disconnect
                    receiver = asyncio.create_task(ws.receive_text())
                    continue
                # the phone never gets config, prompts or credential dialogs: just a refresh cue
                if getter.result().get("type") in ("state", "queue", "chat", "turn_end", "hypotheses", "turn_start", "sessions"):
                    await ws.send_text(json.dumps({"type": "state", "state": companion_view(app.state.engine)}))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            listeners.discard(q)
            companion.sockets.discard(ws)
            await _changed_phones()

    async def _changed_phones() -> None:
        emit({"type": "companion", "phones": len(companion.sockets)})

    # control from the GUI (main token)

    def companion_port() -> int:
        return app.state.engine.cfg.settings.companion_port

    @app.get("/api/phone")
    async def phone_info(e: Engine = Depends(auth)):
        return companion.info(companion_port())

    @app.post("/api/phone/{action}")
    async def phone_action(action: str, e: Engine = Depends(auth)):
        if action == "start":
            await companion.start(companion_port())
        elif action == "stop":
            await companion.stop()
        elif action == "token":
            await companion.rotate()
        elif action == "certificate":
            await companion.renew_cert(companion_port())
        else:
            raise HTTPException(404)
        e.log("companion", action=action, port=companion.port if companion.running else None)
        emit({"type": "companion", "phones": len(companion.sockets)})
        return companion.info(companion_port())

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ------------------------------------------------------------ REST

    if desktop is not None:
        @app.get("/api/desktop/clipboard")
        async def desktop_clip_get(e: Engine = Depends(auth)):
            return {"text": await asyncio.to_thread(desktop.clipboard_get)}

        @app.post("/api/desktop/clipboard")
        async def desktop_clip_set(body: dict, e: Engine = Depends(auth)):
            await asyncio.to_thread(desktop.clipboard_set, str(body.get("text", "")))
            return {"ok": True}

        @app.post("/api/desktop/save")
        async def desktop_save(name: str, request: Request, e: Engine = Depends(auth)):
            return {"path": await asyncio.to_thread(desktop.save_file, name, await request.body())}

    if on_quit is not None:
        @app.post("/api/quit")
        async def quit_app(e: Engine = Depends(auth)):
            e.log("quit")
            asyncio.get_running_loop().call_later(0.3, on_quit)  # let this reply reach the page first
            return {"ok": True}

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
    async def attest(request: Request, e: Engine = Depends(auth)):
        body = await request.json() if int(request.headers.get("content-length") or 0) else {}
        return await e.attest_now(str(body.get("slot", "chat")))

    @app.post("/api/hosts")
    async def save_host(body: dict, e: Engine = Depends(auth)):
        e.save_host(body.get("host", {}), body.get("password") or None, body.get("original_name"))
        return {"ok": True}

    @app.delete("/api/hosts/{name}")
    async def delete_host(name: str, e: Engine = Depends(auth)):
        e.delete_host(name)
        return {"ok": True}

    @app.post("/api/hosts/{name}/forget-pin")
    async def forget_pin(name: str, e: Engine = Depends(auth)):
        return {"removed": e.forget_rdp_pin(name)}

    @app.post("/api/hosts/{name}/forget-password")
    async def forget_password(name: str, e: Engine = Depends(auth)):
        e.forget_host_password(name)
        return {"ok": True}

    @app.post("/api/settings")
    async def save_settings(body: dict, e: Engine = Depends(auth)):
        e.save_settings(body)
        if "companion_port" in body and companion.running and companion.port != companion_port():
            await companion.start(companion_port())  # phones pair again on the new port
        return {"ok": True}

    @app.post("/api/layout")
    async def save_layout(body: dict, e: Engine = Depends(auth)):
        e.save_layout(body)
        return {"ok": True}

    @app.post("/api/case")
    async def new_case(body: dict, e: Engine = Depends(auth)):
        e.new_case(body.get("name", ""), body.get("sensitivity", "open"), body.get("notes", ""))
        return {"ok": True}

    @app.get("/api/cases")
    async def list_cases(e: Engine = Depends(auth)):
        return {"cases": e.list_cases()}

    @app.post("/api/cases/delete")
    async def delete_cases(body: dict, e: Engine = Depends(auth)):
        return e.delete_cases([str(i) for i in body.get("ids", [])])

    @app.post("/api/case/open")
    async def open_case(body: dict, e: Engine = Depends(auth)):
        e.open_case(str(body.get("id", "")))
        return {"ok": True}

    @app.post("/api/sessions")
    async def open_session(body: dict, e: Engine = Depends(auth)):
        if body.get("kind") == "rdp":
            return await e.open_rdp(body.get("host", ""))
        return e.open_session(body.get("kind", "local"), body.get("host", ""))

    @app.post("/api/sessions/{sid}/link")
    async def link_session(sid: str, body: dict, e: Engine = Depends(auth)):
        e.link_session(sid, body.get("to") or None)
        return {"ok": True}

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

    @app.get("/api/queue/{num}/capture")
    async def capture(num: int, e: Engine = Depends(auth)):
        return e.capture(num)

    @app.post("/api/queue/{num}/move")
    async def move_item(num: int, body: dict, e: Engine = Depends(auth)):
        e.move_item(num, int(body.get("delta", 0)))
        return {"ok": True}

    @app.post("/api/preview")
    async def preview(body: dict, e: Engine = Depends(auth)):
        return {"items": e.preview([str(t) for t in body.get("texts", [])], body.get("nums"))}

    @app.post("/api/send")
    async def send(body: dict, e: Engine = Depends(auth)):
        e.send(body.get("message", ""), body.get("results"), body.get("snippets"), body.get("images"))
        return {"ok": True}

    @app.post("/api/web-search/{sid}")
    async def answer_search(sid: str, body: dict, e: Engine = Depends(auth)):
        e.answer_search(sid, bool(body.get("approve")), body.get("query"))
        return {"ok": True}

    @app.post("/api/research/{rid}")
    async def answer_research(rid: str, body: dict, e: Engine = Depends(auth)):
        e.answer_research(rid, bool(body.get("approve")), body.get("text"))
        return {"ok": True}

    @app.post("/api/web-search-test")
    async def test_search(body: dict, e: Engine = Depends(auth)):
        return await e.test_search(str(body.get("query", "")))

    # queue helpers
    @app.post("/api/queue/{num}/dry-run")
    async def dry_run(num: int, e: Engine = Depends(auth)):
        return e.dry_run_item(num)

    @app.post("/api/queue/{num}/watch")
    async def watch(num: int, body: dict, e: Engine = Depends(auth)):
        return e.watch_item(num, int(body.get("interval", 10)), int(body.get("count", 30)))

    @app.post("/api/queue/{num}/review")
    async def review(num: int, e: Engine = Depends(auth)):
        return await e.review_command(num)

    @app.get("/api/queue/group/{group}")
    async def group_items(group: str, e: Engine = Depends(auth)):
        return {"nums": e.run_group(group)}

    @app.get("/api/rollback")
    async def rollback_list(e: Engine = Depends(auth)):
        return {"items": e.rollback_candidates()}

    @app.post("/api/rollback/queue")
    async def rollback_queue(body: dict, e: Engine = Depends(auth)):
        return {"items": e.queue_rollbacks([int(n) for n in body.get("nums", [])])}

    # recipes
    @app.get("/api/recipes")
    async def list_recipes(e: Engine = Depends(auth)):
        return {"recipes": e.list_recipes()}

    @app.post("/api/recipes/{rid}/queue")
    async def queue_recipe(rid: str, body: dict, e: Engine = Depends(auth)):
        added = e.queue_recipe(rid, str(body.get("session_id", "")), bool(body.get("include_install")))
        return {"nums": [p.num for p in added]}

    # hypotheses
    @app.post("/api/hypotheses/{hid}/mark")
    async def mark_hypothesis(hid: str, body: dict, e: Engine = Depends(auth)):
        e.mark_hypothesis(hid, str(body.get("mark", "")))
        return {"ok": True}

    # baselines
    @app.get("/api/baselines")
    async def baselines(e: Engine = Depends(auth)):
        return {"baselines": e.list_baselines()}

    @app.post("/api/baselines/save")
    async def baseline_save(body: dict, e: Engine = Depends(auth)):
        return e.save_baseline(str(body.get("session_id", "")))

    @app.post("/api/baselines/diff")
    async def baseline_diff(body: dict, e: Engine = Depends(auth)):
        return e.diff_baseline(str(body.get("session_id", "")), str(body.get("baseline", "")))

    # tool cache
    @app.get("/api/tools")
    async def tools(e: Engine = Depends(auth)):
        return {"tools": e.list_tools()}

    @app.post("/api/tools")
    async def add_tool(body: dict, e: Engine = Depends(auth)):
        return e.add_tool(str(body.get("path", "")), str(body.get("name", "")), str(body.get("os", "windows")),
                          str(body.get("notes", "")), str(body.get("run", "")))

    @app.delete("/api/tools/{name}")
    async def remove_tool(name: str, e: Engine = Depends(auth)):
        e.remove_tool(name)
        return {"ok": True}

    @app.post("/api/tools/{name}/transfer")
    async def transfer_tool(name: str, body: dict, e: Engine = Depends(auth)):
        return e.transfer_tool(name, str(body.get("session_id", "")))

    # context, timeline, search, files
    @app.get("/api/context")
    async def context(e: Engine = Depends(auth)):
        return e.context_view()

    @app.post("/api/context/drop")
    async def context_drop(body: dict, e: Engine = Depends(auth)):
        e.drop_context([int(g) for g in body.get("groups", [])])
        return {"ok": True}

    @app.post("/api/context/compact")
    async def context_compact(body: dict, e: Engine = Depends(auth)):
        return await e.compact_preview(int(body.get("upto", -1)))

    @app.post("/api/context/compact/cancel")
    async def context_compact_cancel(e: Engine = Depends(auth)):
        return {"cancelled": e.compact_cancel()}

    @app.post("/api/context/compact/apply")
    async def context_compact_apply(body: dict, e: Engine = Depends(auth)):
        e.compact_apply(str(body.get("summary", "")))
        return {"ok": True}

    @app.post("/api/context/compact/undo")
    async def context_compact_undo(e: Engine = Depends(auth)):
        e.compact_undo()
        return {"ok": True}

    @app.get("/api/case/timeline")
    async def timeline(e: Engine = Depends(auth)):
        return e.timeline()

    @app.get("/api/case/transcript")
    async def transcript(sid: str, upto: int | None = None, e: Engine = Depends(auth)):
        return {"text": e.transcript_text(sid, upto)}

    @app.get("/api/case/file/{name}")
    async def case_file(name: str, e: Engine = Depends(auth)):
        return FileResponse(e.case_file(name), headers={"Cache-Control": "no-store"})

    @app.get("/api/search")
    async def search_cases(q: str, e: Engine = Depends(auth)):
        from ..search import search

        hits = search(q, exclude_id=e.case.id if e.case else "", limit=10)
        return {"cases": [{k: v for k, v in h.items() if k != "runbook"} | {"runbook_preview": h["runbook"][:600]} for h in hits]}

    @app.get("/api/training/scenarios")
    async def training_scenarios(e: Engine = Depends(auth)):
        from ..llm.training import scenarios

        return {"scenarios": scenarios()}

    @app.post("/api/retry")
    async def retry(e: Engine = Depends(auth)):
        e.retry()
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
        return e.export_markdown()

    @app.get("/api/export/full")
    async def export_full(terminals: bool = False, e: Engine = Depends(auth)):
        name, data = e.export_full(terminals)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @app.post("/api/export/summary")
    async def export_summary(e: Engine = Depends(auth)):
        return await e.ticket_summary()

    @app.post("/api/export/client")
    async def export_client(e: Engine = Depends(auth)):
        return await e.client_update()

    @app.post("/api/export/runbook")
    async def export_runbook(e: Engine = Depends(auth)):
        return await e.distill_runbook()

    @app.post("/api/open-folder")
    async def open_folder(e: Engine = Depends(auth)):
        if e.case:
            await asyncio.create_subprocess_exec("xdg-open", str(e.case.dir), env=host_env())
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

    @app.websocket("/ws/rdp/{sid}")
    async def ws_rdp(ws: WebSocket, sid: str):
        """Browser <-> guacd relay for one RDP session. The server does the handshake, so the
        connection parameters (password included) never reach the page. One viewer at a time:
        a new connection replaces the previous one."""
        engine: Engine = app.state.engine
        q = ws.query_params
        if not check(q.get("t")):
            await ws.close(code=4403)
            return
        try:
            params = engine.rdp_params(sid)
        except KeyError:
            await ws.close(code=4404)
            return
        await ws.accept(subprotocol="guacamole")
        sess = engine.sessions.sessions[sid]
        if sess.relay and sess.relay is not asyncio.current_task():
            sess.relay.cancel()
        sess.relay = asyncio.current_task()
        clamp = lambda v, lo, hi, d: max(lo, min(hi, int(v))) if str(v).isdigit() else d  # noqa: E731
        width, height = clamp(q.get("width"), 200, 8192, 1280), clamp(q.get("height"), 200, 8192, 800)
        dpi = clamp(q.get("dpi"), 48, 480, 96)
        writer = None
        try:
            try:
                reader, writer = await guac.connect(5)
                _, _, leftover, decoder = await guac.handshake(reader, writer, "rdp", params, width, height, dpi,
                                                               _local_timezone())
            except (OSError, asyncio.TimeoutError, guac.GuacError) as e:
                msg = str(e) or type(e).__name__
                engine.rdp_state(sid, False, msg)
                await ws.send_text(guac.tunnel_uuid() + guac.encode("error", f"Could not start RDP via guacd: {msg}", "519"))
                await ws.close()
                return
            engine.rdp_state(sid, True)
            await ws.send_text(guac.tunnel_uuid() + leftover)

            async def down():            # guacd -> browser
                while data := await reader.read(65536):
                    text = decoder.decode(data)
                    if text:
                        await ws.send_text(text)

            async def up():              # browser -> guacd; tunnel pings are answered here
                while True:
                    msg = await ws.receive_text()
                    if guac.is_internal(msg):
                        await ws.send_text(msg)
                        continue
                    writer.write(msg.encode())
                    await writer.drain()

            tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
        except (WebSocketDisconnect, RuntimeError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if writer:
                writer.close()
            if sess.relay is asyncio.current_task():
                sess.relay = None
                engine.rdp_state(sid, False)
            try:
                await ws.close()
            except RuntimeError:
                pass

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


def _local_timezone() -> str:
    try:
        return Path("/etc/timezone").read_text().strip()
    except OSError:
        link = os.path.realpath("/etc/localtime")
        return link.split("zoneinfo/", 1)[1] if "zoneinfo/" in link else ""


def runtime_dir() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    # sweep leftovers from instances that were killed before they could clean up
    for d in base.glob("datoolkit-*"):
        pid = d.name.removeprefix("datoolkit-")
        if pid.isdigit() and not Path(f"/proc/{pid}").exists() and d.owner() == os.environ.get("USER", d.owner()):
            shutil.rmtree(d, ignore_errors=True)
    return base / f"datoolkit-{os.getpid()}"
