"""Entry point: start the local server and open the desktop window."""

from __future__ import annotations

import argparse
import os
import secrets
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn

from . import config
from .engine import Engine
from .hostenv import set_for_self
from .server.app import create_app, runtime_dir


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Desktop:
    """What the page can't do itself inside the app window. WebKitGTK's async clipboard API is
    unreliable, so terminal copy/paste goes through GTK on the main thread, and exports are saved
    through the native Save dialog. The page reaches these through the local server
    (/api/desktop/..., main token only): pywebview's own JS bridge builds its functions with
    `new Function`, which the page's Content-Security-Policy rightly refuses."""

    def __init__(self, window=None):
        self._window = window

    def save_file(self, name: str, data: bytes):
        """Ask where to save `name` and write the bytes there; returns the path, or None if
        the technician cancelled. Blocks until the dialog closes: call it off the event loop."""
        import webview

        name = os.path.basename(str(name)) or "export"
        chosen = self._window.create_file_dialog(webview.FileDialog.SAVE, directory=os.path.expanduser("~"),
                                                 save_filename=name)
        if not chosen:
            return None
        path = chosen if isinstance(chosen, str) else chosen[0]
        Path(path).write_bytes(data)
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


WEBKIT_INSTALL = ("Ubuntu, Mint, Debian: sudo apt install gir1.2-webkit2-4.1 · Fedora: sudo dnf install webkit2gtk4.1 · "
                  "Arch, CachyOS: sudo pacman -S webkit2gtk-4.1")


HOST_TYPELIB_DIRS = ("/usr/lib64/girepository-1.0", "/usr/lib/x86_64-linux-gnu/girepository-1.0",
                     "/usr/lib/girepository-1.0")


def _bundled_girepository() -> None:
    """Fill gaps in the host's GObject introspection, from copies bundled in the AppImage (outside
    it there are none, and this does nothing). The host's own always win:
    - PyGObject is built against girepository-2.0, which Fedora and Arch ship inside GLib but
      Debian and Ubuntu package separately (libgirepository-2.0-0) and don't always install. If
      the host has none, the bundled copy is loaded under the same soname, so PyGObject's import
      finds it already loaded.
    - GTK's typelibs refer to base ones (xlib, cairo, freetype, ...) that Arch ships separately
      (gobject-introspection-runtime). Only those the host lacks are added to the search path,
      for this process only (child programs get the original, see hostenv)."""
    import ctypes

    fallback = Path(sys.executable).resolve().parents[2] / "lib/girepository-fallback"
    if not fallback.is_dir():
        return
    try:
        ctypes.CDLL("libgirepository-2.0.so.0")
    except OSError:
        ctypes.CDLL(str(fallback / "libgirepository-2.0.so.0"), mode=ctypes.RTLD_GLOBAL)
    missing = [str(d) for d in sorted((fallback / "typelibs").glob("*"))
               if not any(os.path.exists(f"{h}/{d.name}.typelib") for h in HOST_TYPELIB_DIRS)]
    if missing:
        current = os.environ.get("GI_TYPELIB_PATH")
        set_for_self("GI_TYPELIB_PATH", ":".join(missing + ([current] if current else [])))


def window_problem() -> str | None:
    """Why the app window can't open here (no WebKitGTK, no display), or None if it can."""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "There is no graphical display."
    try:
        _bundled_girepository()
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
        from gi.repository import Gtk, WebKit2  # noqa: F401
    except Exception as e:  # noqa: BLE001 - a missing typelib, an old GLib, a broken install: all mean "no window"
        return f"The app window needs WebKitGTK 4.1, which isn't installed ({e}). Install it ({WEBKIT_INSTALL})"
    return None


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="datoolkit", description="Gated AI diagnostic console")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--browser", action="store_true",
                      help="open in the default web browser instead of the app window")
    mode.add_argument("--window", action="store_true",
                      help="open the app window even if Settings say to use the browser")
    ap.add_argument("--no-open", action="store_true",
                    help="with the browser: only print the URL (for opening it yourself)")
    ap.add_argument("--port", type=int, default=0, help="port to listen on (default: random)")
    args = ap.parse_args(argv)

    token = secrets.token_urlsafe(32)
    port = args.port or _free_port()
    cfg = config.load()
    use_browser = args.browser or args.no_open or (cfg.settings.ui_mode == "browser" and not args.window)
    notice = ""
    if not use_browser:
        problem = window_problem()
        if problem:
            use_browser, notice = True, f"{problem}. Running in your web browser until then."
            print(notice, file=sys.stderr)
    desktop = None if use_browser else Desktop()

    def make_engine(emit):
        engine = Engine(cfg, emit, runtime_dir())
        engine.ui_notice = notice
        return engine

    app = create_app(token, make_engine, desktop, on_quit=lambda: setattr(server, "should_exit", True))
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
        if use_browser:
            print(f"DAToolkit running. Open this URL (keep it private - it grants terminal access):\n{url}",
                  flush=True)
            if not args.no_open:
                import webbrowser

                webbrowser.open(url)
            while thread.is_alive():  # until Quit in the page, or Ctrl+C
                thread.join(0.5)
        else:
            import webview
            from gi.repository import GLib

            # the window's class, which desktops match to datoolkit.desktop (and its icon); under
            # `python -m datoolkit` it would otherwise be "__main__.py"
            GLib.set_prgname("datoolkit")
            GLib.set_application_name("DAToolkit")
            desktop._window = webview.create_window("DAToolkit", url + "&desktop=1", width=1500, height=950,
                                                    min_size=(900, 600))
            window = desktop._window

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
