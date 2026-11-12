"""Local HTTP/WebSocket server for the GUI, bound to 127.0.0.1 and gated by a per-launch token,
and the phone companion's app, served over TLS on its own port (see companion.py)."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import shutil
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers

from .. import config
from ..companion import CompanionServer, PairingRefused
from ..hostenv import host_env
from ..llm import providers as providers_mod
from ..engine import Engine, UserError
from ..sessions import guac

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
COMPANION_FILES = {"style.css", "companion.js", "icon.svg"}
# a photo is at most 6 MB (8 MB as base64, see Engine.clean_images) plus its description
MAX_COMPANION_BODY = 10 * 1024 * 1024
# a phone's claim of a pairing code (the code and its name), which needs no token
MAX_PAIR_BODY = 4 * 1024
PAIR_PATH = "/api/companion/pair"
# the window's largest request: a message or its draft with four such images, and its text
MAX_WINDOW_BODY = 48 * 1024 * 1024
# a file saved through the app window's Save dialog (/api/desktop/save): a case's full export
MAX_SAVED_FILE = 1024 * 1024 * 1024
# A browser is opened with a one-time code, never the token (app.browser_opener): this long to be used
LAUNCH_SECONDS = 120
LAUNCH_PATH = "/api/launch"
MAX_LAUNCH_BODY = 1024


def peer_uid(peer: tuple | None, server: tuple | None) -> int | None:
    """The user whose socket at `peer` is connected to `server` on this computer (a loopback TCP
    connection), as the kernel's table of TCP sockets has it; None where that can't be said.
    (DA Vibe Manager's server/app.py, Nov 2026.)"""
    if not peer or not server or not all(isinstance(at[1], int) for at in (peer, server)):
        return None

    def written(host: str, port: int) -> str:
        try:
            raw = socket.inet_aton(host)
        except OSError:
            return ""
        return f"{(raw[::-1] if sys.byteorder == 'little' else raw).hex().upper()}:{port:04X}"
    local, remote = written(peer[0], peer[1]), written(server[0], server[1])
    try:
        with open("/proc/net/tcp", encoding="ascii") as f:
            lines = f.read().splitlines()[1:]
    except OSError:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) > 7 and fields[1] == local and fields[2] == remote:
            return int(fields[7])
    return None


class Launches:
    """The one-time codes the browser is opened with (http://127.0.0.1:PORT/#c=CODE), each exchanged once
    for the window's token (POST /api/launch). A browser's command line shows every user of this computer
    what it was opened with, and the token in it gave another user the app for as long as it ran (Nov 2026:
    it was ?t=TOKEN). A code is good once, for LAUNCH_SECONDS, and only to a process of this user's: one
    taken first by anyone else would be refused. (DA Vibe Manager's, 41f957d, Nov 2026.)"""

    def __init__(self) -> None:
        self._codes: dict[str, float] = {}

    def new(self) -> str:
        code = secrets.token_urlsafe(24)
        now = time.monotonic()
        self._codes = {c: at for c, at in self._codes.items() if now - at < LAUNCH_SECONDS}
        self._codes[code] = now
        return code

    def take(self, code: str, peer: tuple | None = None, server: tuple | None = None) -> bool:
        """Whether `code` is one given out, not used yet, still current, and asked for by this user."""
        found = next((c for c in self._codes if hmac.compare_digest(c.encode(), str(code).encode("utf-8", "replace"))), None)
        if found is None:
            return False
        uid = peer_uid(peer, server)
        if uid is not None and uid != os.getuid():
            return False                    # another user's process: refused, and the code stands for this one's
        at = self._codes.pop(found)
        return time.monotonic() - at < LAUNCH_SECONDS


class TokenGate:
    """ASGI middleware in front of an app whose /api/ routes need its token. FastAPI reads and
    parses a JSON body before it runs dependencies (where the token is checked), and nothing
    else limits its size, so without this anyone who could reach the port (any local user, for
    the window's; anyone on the LAN, for the companion's) could make the app buffer and parse
    any amount without the token, stalling the event loop the GUI and terminals share. An /api/
    request is refused here on its headers alone: no valid token, no stated length, or a body
    over its limit. The connection is closed rather than read to the end. WebSockets check
    their tokens themselves."""

    def __init__(self, app, check: Callable[[str | None], bool], limit: Callable[[str], int],
                 open_paths: tuple[str, ...] = ()):
        self.app = app
        self.check = check
        self.limit = limit
        self.open_paths = open_paths     # paths that need no token (a phone's pairing), size still held

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/"):
            refusal = self._refusal(scope["path"], Headers(scope=scope))
            if refusal:
                status, why = refusal
                await JSONResponse({"detail": why}, status, headers={"connection": "close"})(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _refusal(self, path: str, headers: Headers) -> tuple[int, str] | None:
        needs_token = not (self.open_paths and path.startswith(self.open_paths))
        if needs_token and not self.check(headers.get("x-token")):
            return 403, "bad token"
        if "transfer-encoding" in headers:   # chunked: no length to check before reading
            return 411, "a Content-Length is required"
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            return 400, "bad Content-Length"
        if length > self.limit(path):
            return 413, "request too large"
        return None


class CompanionGate(TokenGate):
    """The gate in front of the phone companion's app: a paired phone's token, and
    MAX_COMPANION_BODY; a claim of a pairing code needs none, and is held to MAX_PAIR_BODY."""

    def __init__(self, app, companion: CompanionServer):
        super().__init__(app, companion.check,
                         lambda path: MAX_PAIR_BODY if path.startswith(PAIR_PATH) else MAX_COMPANION_BODY,
                         open_paths=(PAIR_PATH,))
        self.companion = companion


# On every response of the phone's (errors and refusals included): nothing of it is kept, sniffed,
# sent on as a referrer or framed by another page, and its pages load nothing from anywhere else.
# The pages' own meta policies stay; a header's is the only place frame-ancestors is honoured.
# (SealedLore's phone/http.py, Nov 2026.)
COMPANION_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                                "img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-src 'none'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), interest-cohort=()",
}


# On every response of the window's own app. Its page carries its Content-Security-Policy in a meta
# element, which is the one place frame-ancestors is not honoured: the header says it, so no other
# page can put this one in a frame, and nothing served here is sniffed into another type or named
# in a referrer.
WINDOW_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


class SecurityHeaders:
    """ASGI middleware: `headers` set on every HTTP response, whoever made it."""

    def __init__(self, app, headers: dict[str, str]):
        self.app = app
        self.headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        names = {name for name, _ in self.headers}

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in names]
                message = {**message, "headers": kept + self.headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def window_limit(path: str) -> int:
    """The largest body the window's app takes at `path`."""
    if path == LAUNCH_PATH:
        return MAX_LAUNCH_BODY
    return MAX_SAVED_FILE if path == "/api/desktop/save" else MAX_WINDOW_BODY


def create_app(token: str, make_engine: Callable[[Callable[[dict], None]], Engine],
               desktop=None, companion_dir: Path | None = None,
               on_quit: Callable[[], None] | None = None, launches: Launches | None = None) -> FastAPI:
    """The GUI's app, for 127.0.0.1 only. It also builds the phone companion's app, which
    app.state.companion (companion.CompanionServer) serves on its own port over TLS when the
    technician starts it: that one can see chat, queue and hypotheses, mark items
    done/skipped and send a photo with a description, and nothing else. It never reaches a
    terminal.
    `desktop` (app.Desktop, only in the app window) serves the clipboard and the Save dialog.
    `companion_dir` holds the companion's certificate (default: the config directory).
    `on_quit` ends the process (POST /api/quit): in the browser, closing the tab doesn't.
    `launches`: the one-time codes a browser is opened with, exchanged for the token (POST /api/launch)."""
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
    comp.add_middleware(SecurityHeaders, headers=COMPANION_HEADERS)       # outermost: on the gate's refusals too
    app.state.companion = companion

    def check(t: str | None) -> bool:
        # as bytes: compare_digest raises on a str with anything but ASCII in it (a 500, not a 403)
        return t is not None and hmac.compare_digest(t.encode("utf-8", "surrogateescape"), token.encode())

    def auth(request: Request) -> Engine:
        if not check(request.headers.get("x-token")):
            raise HTTPException(403, "bad token")
        return request.app.state.engine

    # the token, and the size, judged on the headers before FastAPI reads a body (TokenGate);
    # auth above still stands behind it on every route. The one request without the token is the
    # browser's exchange of its one-time code for it (Launches), held to MAX_LAUNCH_BODY.
    app.add_middleware(TokenGate, check=check, limit=window_limit, open_paths=(LAUNCH_PATH,))
    app.add_middleware(SecurityHeaders, headers=WINDOW_HEADERS)           # outermost: on the gate's refusals too

    def auth_companion(request: Request) -> Engine:
        if not companion.check(request.headers.get("x-token")):
            raise HTTPException(403, "bad token")
        return app.state.engine

    def companion_view(e: Engine) -> dict:
        snap = e.snapshot()
        vision = e.vision_status()
        # of the case, its name and sensitivity, which is all the phone's page shows: not its notes,
        # its client or the folder it is kept in
        case = {k: snap["case"].get(k) for k in ("name", "sensitivity")} if snap["case"] else None
        return {"case": case, "chat": snap["chat"][-30:], "queue": snap["queue"], "busy": snap["busy"],
                "photos": {"ok": vision["mode"] != "none", "why": vision.get("why", "")},
                "hypotheses": snap["hypotheses"], "sessions": [{k: s[k] for k in ("id", "kind", "target", "exited")}
                                                               for s in snap["sessions"]]}

    async def user_error(_: Request, exc: UserError):
        return JSONResponse({"error": str(exc), **({"commands": exc.commands} if exc.commands else {})},
                            status_code=400)

    async def key_error(_: Request, exc: KeyError):
        # an unknown queue number, session id or prompt id is a stale client, not a crash
        return JSONResponse({"error": f"Not found: {exc.args[0] if exc.args else exc}"}, status_code=400)

    async def bad_value(_: Request, exc: Exception):
        # a field of the wrong type or form in a request ("delta": "up", a port of "22a")
        return JSONResponse({"error": f"Invalid request: {exc}"}, status_code=400)

    def obj(body: dict, key: str) -> dict:
        """The object a request carries at `key` (a provider, a host): anything else is refused."""
        value = body.get(key, {})
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be an object")
        return value

    for a in (app, comp):
        a.add_exception_handler(UserError, user_error)
        a.add_exception_handler(KeyError, key_error)
        a.add_exception_handler(ValueError, bad_value)
        a.add_exception_handler(TypeError, bad_value)

    @app.post(LAUNCH_PATH)
    async def launch(request: Request):
        """The token, once, for the one-time code a browser was opened with (Launches)."""
        try:
            code = str((await request.json()).get("code") or "")
        except (ValueError, AttributeError):
            code = ""
        client = request.client
        if launches is None or not launches.take(code, (client.host, client.port) if client else None,
                                                 request.scope.get("server")):
            return JSONResponse({"error": "This link has been used already, or is too old. Start DA Toolkit "
                                          "again: it opens a new one."}, status_code=403)
        return {"token": token}

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

    @comp.post(PAIR_PATH)
    async def companion_claim(request: Request):
        """A phone claims the pairing code its connect QR carried (no token yet): the desktop is
        asked to allow it, showing the four digits this answers with."""
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            raise HTTPException(400, "the body isn't JSON") from None
        body = body if isinstance(body, dict) else {}
        try:
            claim = companion.pairing.claim(body.get("code"), body.get("name"))
        except PairingRefused as refused:
            return JSONResponse({"error": refused.reason}, status_code=refused.status)
        emit({"type": "companion_claim", "claim": {"id": claim.id, "name": claim.name, "check": claim.check}})
        return {"claim": claim.id, "check": claim.check}

    @comp.get(PAIR_PATH + "/{claim_id}")
    async def companion_claim_state(claim_id: str):
        """The desktop's answer to a claim, once: the token with "approved", the first time."""
        state, token = companion.pairing.poll(claim_id)
        if state in ("approved", "refused", "expired"):
            emit({"type": "companion", "phones": len(companion.sockets)})
        return {"state": state, **({"token": token} if token else {})}

    @comp.get("/api/companion/state")
    async def companion_state(e: Engine = Depends(auth_companion)):
        return companion_view(e)

    @comp.post("/api/companion/queue/{num}")
    async def companion_mark(num: int, body: dict, e: Engine = Depends(auth_companion)):
        status = body.get("status")
        if status not in ("ran", "skipped", "pending"):
            raise HTTPException(400, "companion can only mark items ran, skipped or pending")
        if status == "pending" and e.queue.get(num).status == "withdrawn":
            # "pending" from the phone undoes its own ran or skipped; an item the AI took back is
            # restored at the computer, where the command is in front of the technician
            raise UserError(f"#{num} was withdrawn by the AI. Restore it on the computer if it should run after all.")
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
        cleaned = await asyncio.to_thread(e.clean_images, [image])   # CPU-bound: not on the event loop
        e.send(str(body.get("message", ""))[:4000], images=cleaned, via="phone")
        return {"ok": True}

    @comp.post("/api/companion/hypotheses/{hid:path}")
    async def companion_mark_h(hid: str, body: dict, e: Engine = Depends(auth_companion)):
        e.mark_hypothesis(hid, str(body.get("mark", "")))
        return {"ok": True}

    @comp.websocket("/ws/events")
    async def comp_events(ws: WebSocket):
        device = companion.device_of(ws.query_params.get("t"))
        if device is None:
            await ws.close(code=4403)
            return
        await ws.accept()
        companion.sockets[ws] = device
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
        except (WebSocketDisconnect, RuntimeError, KeyError):     # KeyError: a frame that isn't text
            pass
        finally:
            listeners.discard(q)
            companion.sockets.pop(ws, None)
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
        elif action == "token":         # a new pairing code for the connect QR
            await companion.rotate()
        elif action == "certificate":
            await companion.renew_cert(companion_port())
        else:
            raise HTTPException(404)
        e.log("companion", action=action, port=companion.port if companion.running else None)
        emit({"type": "companion", "phones": len(companion.sockets)})
        return companion.info(companion_port())

    @app.post("/api/phone/claims/{claim_id}")
    async def phone_claim(claim_id: str, body: dict, e: Engine = Depends(auth)):
        """The technician's answer to a phone asking to pair: allowed, it gets a token of its own."""
        allow = body.get("allow") is True
        device = companion.answer(claim_id, allow)
        e.log("companion_pairing", allowed=device is not None, name=device.name if device else None)
        emit({"type": "companion", "phones": len(companion.sockets)})
        return companion.info(companion_port())

    @app.delete("/api/phone/devices/{device_id}")
    async def phone_forget(device_id: str, e: Engine = Depends(auth)):
        """Disconnect one paired phone: its token stops working, and it would have to pair again."""
        name = companion.devices[device_id].name if device_id in companion.devices else None
        await companion.revoke(device_id)
        e.log("companion_disconnected", name=name)
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
        e.save_provider(obj(body, "provider"), body.get("api_key") or None, body.get("original_name"))
        return {"ok": True}

    @app.delete("/api/providers/{name:path}")
    async def delete_provider(name: str, e: Engine = Depends(auth)):
        e.delete_provider(name)
        return {"ok": True}

    @app.get("/api/models")
    async def models(provider: str, refresh: bool = False, e: Engine = Depends(auth)):
        return {"models": await e.list_models(provider, refresh)}

    @app.post("/api/route")
    async def set_route(body: dict, e: Engine = Depends(auth)):
        e.set_route(str(body.get("provider", "")), str(body.get("model", "")), body.get("route") or None)
        return {"ok": True}

    @app.get("/api/route/hosts")
    async def route_hosts(provider: str, model: str, refresh: bool = False, e: Engine = Depends(auth)):
        return await e.model_hosts(provider, model, refresh)

    @app.get("/api/provider-presets")
    async def provider_presets(e: Engine = Depends(auth)):
        return {"presets": [{"kind": k, "label": label, "name": n, "base_url": u, "api": a}
                            for k, label, n, u, a in providers_mod.PRESETS]}

    @app.post("/api/model")
    async def select_model(body: dict, e: Engine = Depends(auth)):
        e.select_model(body["provider"], body["model"])
        return {"ok": True}

    @app.post("/api/attest")
    async def attest(request: Request, e: Engine = Depends(auth)):
        body = await request.json() if int(request.headers.get("content-length") or 0) else {}
        return await e.attest_now(str(body.get("slot", "chat")) if isinstance(body, dict) else "chat")

    @app.post("/api/hosts")
    async def save_host(body: dict, e: Engine = Depends(auth)):
        e.save_host(obj(body, "host"), body.get("password") or None, body.get("original_name"))
        return {"ok": True}

    @app.delete("/api/hosts/{name:path}")
    async def delete_host(name: str, e: Engine = Depends(auth)):
        e.delete_host(name)
        return {"ok": True}

    @app.post("/api/hosts/{name:path}/forget-pin")
    async def forget_pin(name: str, e: Engine = Depends(auth)):
        return {"removed": e.forget_pin(name)}

    @app.post("/api/hosts/{name:path}/forget-password")
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
        e.new_case(str(body.get("name") or ""), str(body.get("sensitivity") or "open"), str(body.get("notes") or ""),
                   client=str(body.get("client") or ""), history=bool(body.get("history")))
        return {"ok": True}

    @app.post("/api/clients")
    async def add_client(body: dict, e: Engine = Depends(auth)):
        e.add_client(str(body.get("name", "")))
        return {"ok": True, "clients": e.cfg.clients}

    @app.post("/api/clients/{name:path}")
    async def rename_client(name: str, body: dict, e: Engine = Depends(auth)):
        return {"ok": True, "cases": e.rename_client(name, str(body.get("name", ""))), "clients": e.cfg.clients}

    @app.delete("/api/clients/{name:path}")
    async def delete_client(name: str, e: Engine = Depends(auth)):
        return {"ok": True, "cases": e.delete_client(name), "clients": e.cfg.clients}

    @app.get("/api/cases")
    async def list_cases(e: Engine = Depends(auth)):
        return {"cases": e.list_cases()}

    @app.post("/api/cases/delete")
    async def delete_cases(body: dict, e: Engine = Depends(auth)):
        return e.delete_cases([str(i) for i in body.get("ids", [])])

    @app.post("/api/case/edit")
    async def edit_case(body: dict, e: Engine = Depends(auth)):
        notes = body.get("notes")
        return e.edit_case(str(body.get("id", "")), str(body.get("name", "")), None if notes is None else str(notes),
                           str(body.get("sensitivity") or "") or None,
                           client=None if "client" not in body else str(body.get("client") or ""),
                           history=None if "history" not in body else bool(body.get("history")))

    @app.post("/api/case/open")
    async def open_case(body: dict, e: Engine = Depends(auth)):
        e.open_case(str(body.get("id", "")))
        return {"ok": True}

    @app.post("/api/sessions")
    async def open_session(body: dict, e: Engine = Depends(auth)):
        if body.get("kind") == "rdp":
            return await e.open_rdp(body.get("host", ""))
        if body.get("kind") == "winrm":
            return await e.open_winrm(body.get("host", ""))
        if body.get("kind") == "window":
            return await e.open_window(body.get("xid", 0), str(body.get("name", "")), str(body.get("os_hint", "")))
        return e.open_session(body.get("kind", "local"), body.get("host", ""))

    @app.get("/api/windows")
    async def list_windows(e: Engine = Depends(auth)):
        return await e.list_windows()

    @app.get("/api/sessions/{sid}/shot")
    async def window_shot(sid: str, preview: bool = False, e: Engine = Depends(auth)):
        return await e.window_shot(sid, preview)

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
        e.set_session_hint(sid, str(body.get("os_hint") or ""))
        return {"ok": True}

    @app.post("/api/queue/{num}")
    async def update_item(num: int, body: dict, e: Engine = Depends(auth)):
        fields = {k: body[k] for k in ("command", "session_id", "status", "note") if k in body}
        return {"ok": True, "item": e.update_item(num, **fields)}

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

    @app.get("/api/draft")
    async def load_draft(case_id: str, e: Engine = Depends(auth)):
        return await asyncio.to_thread(e.load_draft, case_id)

    @app.post("/api/draft")
    async def save_draft(body: dict, e: Engine = Depends(auth)):
        await asyncio.to_thread(e.save_draft, str(body.get("case_id") or ""), body.get("text", ""), body.get("images"))
        return {"ok": True}

    @app.post("/api/send")
    async def send(body: dict, e: Engine = Depends(auth)):
        images = body.get("images") or []
        if images:                              # decoded and re-encoded off the event loop
            images = await asyncio.to_thread(e.clean_images, images)
        e.send(body.get("message", ""), body.get("results"), body.get("snippets"), images)
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
    @app.post("/api/hypotheses/{hid:path}/mark")
    async def mark_hypothesis(hid: str, body: dict, e: Engine = Depends(auth)):
        e.mark_hypothesis(hid, str(body.get("mark", "")))
        return {"ok": True}

    # baselines
    @app.get("/api/baselines")
    async def baselines(session_id: str = "", e: Engine = Depends(auth)):
        return {"baselines": e.list_baselines(session_id)}

    @app.post("/api/baselines/delete")
    async def baseline_delete(body: dict, e: Engine = Depends(auth)):
        e.delete_baseline(str(body.get("path", "")))
        return {"ok": True}

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

    @app.delete("/api/tools/{name:path}")
    async def remove_tool(name: str, e: Engine = Depends(auth)):
        e.remove_tool(name)
        return {"ok": True}

    @app.post("/api/tools/{name:path}/transfer")
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
        except (WebSocketDisconnect, RuntimeError, KeyError):     # KeyError: a frame that isn't text
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
        # what guacd sends is watched (never changed) for when the desktop is really connected
        # and why it wasn't: `ready` comes before the RDP connection is even tried
        watch: guac.ConnectWatch | None = guac.ConnectWatch()
        connected, failure, refused, pin = False, "", "", None

        async def recheck(names: list[str]) -> None:
            # A guacd that can't hold the connection to the pin itself (1.3 takes no
            # `cert-fingerprints`) connects with ignore-cert, so the certificate is read again
            # here, for every connection the page makes and before guacd is given the password:
            # this one may come hours after the session was opened (Reconnect, a fit-to-window
            # reconnect, a reload). The probe and guacd's connection are still two TLS sessions.
            nonlocal refused
            if sess.cert.get("tls") and not guac.enforces_pin(names, params):
                try:
                    await engine.rdp_recheck(sess)
                except UserError as e:
                    refused = str(e)
                    raise

        try:
            try:
                try:
                    reader, writer = await guac.connect(5)
                except OSError:
                    # our container stopped since this session opened: started again, if that
                    # is how guacd is provided here
                    if not await engine._ensure_guacd():
                        raise
                    reader, writer = await guac.connect(5)
                _, names, leftover, decoder = await guac.handshake(reader, writer, "rdp", params, width, height, dpi,
                                                                   _local_timezone(), before_connect=recheck)
            except (OSError, asyncio.TimeoutError, guac.GuacError, UserError) as e:
                failure = str(e) or type(e).__name__
                await ws.send_text(guac.tunnel_uuid() + guac.encode(
                    "error", refused or f"Could not start RDP via guacd: {failure}", "519"))
                return
            pin = guac.enforces_pin(names, params) if params.get("cert-fingerprints") else None
            engine.rdp_connecting(sess)

            def watched(text: str) -> None:
                nonlocal watch, connected
                if watch is None:
                    return
                try:
                    if "connected" in watch.feed(text):
                        connected = True
                        engine.rdp_connected(sess, pin)
                except guac.GuacError:     # not the protocol after all: stop watching, keep relaying
                    watch = None

            watched(leftover)
            await ws.send_text(guac.tunnel_uuid() + leftover)

            async def down():            # guacd -> browser
                while data := await reader.read(65536):
                    text = decoder.decode(data)
                    if text:
                        watched(text)
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
            current = sess.relay is asyncio.current_task()
            if current:
                sess.relay = None
            engine.rdp_ended(sess, connected, error=failure or (watch.error if watch else ""),
                             code="" if failure or not watch else watch.code, current=current)
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):     # the page went first (closed or reconnected)
                pass

    @app.websocket("/ws/term/{sid}")
    async def ws_term(ws: WebSocket, sid: str):
        """One viewer of a terminal session: its output out, input and resizes in. A message
        that isn't one of ours (not JSON, not an object, a size no terminal has) is passed over;
        until Nov 2026 each of those ended the socket with an unhandled exception, and the tab
        with it."""
        engine: Engine = app.state.engine
        if not check(ws.query_params.get("t")) or sid not in engine.sessions.sessions:
            await ws.close(code=4403)
            return
        if getattr(engine.sessions.sessions[sid], "proc", None) is None:
            await ws.close(code=4404)        # a remote desktop or a window: no terminal to attach to
            return
        await ws.accept()
        backlog, q = engine.sessions.subscribe(sid)

        async def pump_out():
            try:
                if backlog:
                    await ws.send_bytes(backlog)
                while True:
                    data = await q.get()
                    if data is None:
                        await ws.close()
                        return
                    await ws.send_bytes(data)
            except (WebSocketDisconnect, RuntimeError):     # the page went first
                pass

        def size(value) -> int:          # columns or rows a terminal may have: 1 to 1000
            return value if type(value) is int and 1 <= value <= 1000 else 0

        out_task = asyncio.create_task(pump_out())
        try:
            while True:
                try:
                    text = await ws.receive_text()
                except KeyError:             # a binary frame
                    continue
                if sid not in engine.sessions.sessions:
                    break
                try:
                    msg = json.loads(text)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("type") == "input" and isinstance(msg.get("data"), str):
                    # half a surrogate pair (a string cut in the middle of a character) has no
                    # encoding: left out, the rest typed
                    engine.sessions.write(sid, msg["data"].encode("utf-8", "ignore"))
                elif msg.get("type") == "resize" and size(msg.get("cols")) and size(msg.get("rows")):
                    engine.sessions.resize(sid, msg["cols"], msg["rows"])
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
        try:
            ours = d.stat().st_uid == os.getuid() and not d.is_symlink()
        except OSError:           # gone meanwhile
            continue
        if pid.isdigit() and not Path(f"/proc/{pid}").exists() and ours:
            shutil.rmtree(d, ignore_errors=True)
    return base / f"datoolkit-{os.getpid()}"
