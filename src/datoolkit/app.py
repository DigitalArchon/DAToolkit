"""Entry point: start the local server and open the desktop window."""

from __future__ import annotations

import argparse
import secrets
import signal
import socket
import threading
import time

import uvicorn

from . import config
from .engine import Engine
from .server.app import create_app, runtime_dir


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class JsApi:
    """Exposed to the page as window.pywebview.api. WebKitGTK's async clipboard API is
    unreliable, so terminal copy/paste goes through GTK on the main thread instead."""

    @staticmethod
    def _on_main(fn, timeout: float = 5.0):
        from gi.repository import GLib

        result: dict = {}
        done = threading.Event()

        def run():
            try:
                result["value"] = fn()
            finally:
                done.set()
            return False

        GLib.idle_add(run)
        done.wait(timeout)
        return result.get("value")

    def clipboard_get(self) -> str:
        from gi.repository import Gdk, Gtk

        return self._on_main(lambda: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).wait_for_text()) or ""

    def clipboard_set(self, text: str) -> None:
        from gi.repository import Gdk, Gtk

        def set_text():
            cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            cb.set_text(text, -1)
            cb.store()

        self._on_main(set_text)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="datoolkit", description="Gated AI diagnostic console")
    ap.add_argument("--browser", action="store_true",
                    help="don't open a window; print a URL to open in a local browser instead")
    ap.add_argument("--port", type=int, default=0, help="port to listen on (default: random)")
    args = ap.parse_args(argv)

    token = secrets.token_urlsafe(32)
    port = args.port or _free_port()
    cfg = config.load()
    app = create_app(token, lambda emit: Engine(cfg, emit, runtime_dir()))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           ws_max_size=16 * 1024 * 1024))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise SystemExit("Server failed to start.")
        time.sleep(0.05)

    url = f"http://127.0.0.1:{port}/?t={token}"
    try:
        if args.browser:
            print(f"DAToolkit running. Open this URL (keep it private - it grants terminal access):\n{url}")
            thread.join()
        else:
            import webview

            window = webview.create_window("DAToolkit", url, width=1500, height=950, min_size=(900, 600),
                                           js_api=JsApi())

            def on_started():
                from gi.repository import GLib

                def close(*_):
                    window.destroy()
                    return False

                for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, close)

            webview.start(on_started, gui="gtk", private_mode=True)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
