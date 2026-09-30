"""Entry point: start the local server and open the desktop window."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import signal
import socket
import threading
import time
from pathlib import Path

import uvicorn

from . import config
from .engine import Engine
from .server.app import create_app, runtime_dir


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class JsApi:
    """Exposed to the page as window.pywebview.api. WebKitGTK's async clipboard API is
    unreliable, so terminal copy/paste goes through GTK on the main thread instead. Exports
    are saved through the native Save dialog. (Underscore attributes are not exposed.)"""

    _window = None

    def save_file(self, name: str, data_b64: str):
        """Ask where to save `name` and write the bytes there; returns the path, or None if
        the technician cancelled."""
        import webview

        name = os.path.basename(str(name)) or "export"
        chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=os.path.expanduser("~"),
                                                 save_filename=name)
        if not chosen:
            return None
        path = chosen if isinstance(chosen, str) else chosen[0]
        Path(path).write_bytes(base64.b64decode(data_b64))
        return path

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
    ap.add_argument("--companion", action="store_true",
                    help="also serve the read-mostly phone view on the LAN (binds all interfaces; prints its URL)")
    args = ap.parse_args(argv)

    token = secrets.token_urlsafe(32)
    companion_token = secrets.token_urlsafe(24) if args.companion else None
    port = args.port or _free_port()
    cfg = config.load()
    app = create_app(token, lambda emit: Engine(cfg, emit, runtime_dir()), companion_token)
    bind = "0.0.0.0" if args.companion else "127.0.0.1"
    server = uvicorn.Server(uvicorn.Config(app, host=bind, port=port, log_level="warning",
                                           ws_max_size=16 * 1024 * 1024))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise SystemExit("Server failed to start.")
        time.sleep(0.05)

    url = f"http://127.0.0.1:{port}/?t={token}"
    if companion_token:
        print(f"Companion view (phone, same Wi-Fi): http://{_lan_ip()}:{port}/companion?t={companion_token}\n"
              "It shows the chat and queue and can mark items done; it cannot reach a terminal. "
              "The main URL is also reachable on the LAN while --companion is on: keep it private.")
    try:
        if args.browser:
            print(f"DAToolkit running. Open this URL (keep it private - it grants terminal access):\n{url}")
            thread.join()
        else:
            import webview

            api = JsApi()
            window = webview.create_window("DAToolkit", url, width=1500, height=950, min_size=(900, 600),
                                           js_api=api)
            api._window = window

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
