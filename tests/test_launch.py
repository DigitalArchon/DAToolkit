"""How DAToolkit starts: app window or browser, the fallback, Quit, and a clean environment for
the programs it starts (it may run from an AppImage)."""

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from datoolkit import app as app_mod
from datoolkit.engine import UserError
from datoolkit.hostenv import host_env
from datoolkit.server.app import create_app

from test_engine import env  # noqa: F401


def test_no_display_means_no_window(monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert "no graphical display" in app_mod.window_problem()


def test_missing_webkit_says_what_to_install(monkeypatch):
    import gi

    monkeypatch.setenv("DISPLAY", ":99")

    def refuse(namespace, version):
        if namespace == "WebKit2":
            raise ValueError("Namespace WebKit2 not available")
    monkeypatch.setattr(gi, "require_version", refuse)
    problem = app_mod.window_problem()
    assert "WebKitGTK 4.1" in problem and "gir1.2-webkit2-4.1" in problem and "webkit2gtk4.1" in problem


async def test_open_in_setting_is_validated(env):  # noqa: F811
    engine, _, _ = env
    with pytest.raises(UserError):
        engine.save_settings({"ui_mode": "tab"})
    engine.save_settings({"ui_mode": "browser"})
    assert engine.cfg.settings.ui_mode == "browser"
    engine.ui_notice = "Running in your web browser until then."
    assert engine.snapshot()["ui_notice"] == "Running in your web browser until then."


async def test_quit_needs_the_token_and_only_exists_when_it_can_quit(env, tmp_path):  # noqa: F811
    engine, _, _ = env
    quit_called = asyncio.Event()
    app: FastAPI = create_app("main", lambda emit: engine, companion_dir=tmp_path, on_quit=quit_called.set)
    app.state.engine = engine
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/api/quit", headers={"x-token": "nope"})).status_code == 403
        assert (await c.post("/api/quit", headers={"x-token": "main"})).status_code == 200
        await asyncio.wait_for(quit_called.wait(), 2)
    plain: FastAPI = create_app("main", lambda emit: engine, companion_dir=tmp_path)
    plain.state.engine = engine
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=plain), base_url="http://t") as c:
        assert (await c.post("/api/quit", headers={"x-token": "main"})).status_code == 404


def test_appimage_variables_stay_out_of_child_programs(monkeypatch):
    monkeypatch.setenv("APPDIR", "/tmp/.mount_DAToolkit")
    monkeypatch.setenv("APPIMAGE", "/home/x/DAToolkit.AppImage")
    monkeypatch.setenv("HOME_TEST_KEEP", "1")
    env = host_env(TERM="xterm-256color")
    assert "APPDIR" not in env and "APPIMAGE" not in env
    assert env["HOME_TEST_KEEP"] == "1" and env["TERM"] == "xterm-256color"


async def test_a_terminal_session_doesnt_see_the_appimage(env, monkeypatch):  # noqa: F811
    engine, _, _ = env
    monkeypatch.setenv("APPDIR", "/tmp/.mount_DAToolkit")
    engine.new_case("env", "open")
    sess = engine.sessions.spawn("probe", ["/bin/sh", "-c", 'echo "appdir=[${APPDIR:-}] term=$TERM"; sleep 0.2'], {},
                                 name="probe", kind="local")
    for _ in range(100):
        if b"appdir=" in sess.backlog:
            break
        await asyncio.sleep(0.02)
    assert b"appdir=[] term=xterm-256color" in bytes(sess.backlog)
    engine.sessions.close_all()


def test_a_variable_changed_for_datoolkit_itself_reaches_children_unchanged(monkeypatch):
    from datoolkit import hostenv

    monkeypatch.setattr(hostenv, "ORIGINAL", {})
    monkeypatch.delenv("GI_TYPELIB_PATH", raising=False)
    monkeypatch.setenv("XDG_DATA_DIRS", "/usr/share")
    hostenv.set_for_self("GI_TYPELIB_PATH", "/appimage/typelibs/xlib-2.0")
    hostenv.set_for_self("XDG_DATA_DIRS", "/appimage/share:/usr/share")
    env = host_env()
    assert "GI_TYPELIB_PATH" not in env and env["XDG_DATA_DIRS"] == "/usr/share"


def test_the_window_takes_the_name_of_the_installed_desktop_entry(tmp_path, monkeypatch):
    from datoolkit.app import desktop_id
    img = tmp_path / "Apps" / "DA Toolkit.AppImage"
    img.parent.mkdir()
    img.write_bytes(b"")
    (tmp_path / "link.AppImage").symlink_to(img)
    home, sys_dir = tmp_path / "home", tmp_path / "sys"
    (home / "applications" / "kde").mkdir(parents=True)
    (sys_dir / "applications").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    monkeypatch.setenv("XDG_DATA_DIRS", str(sys_dir))
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert desktop_id() == "datoolkit"                         # not an AppImage
    monkeypatch.setenv("APPIMAGE", str(img))
    (home / "applications" / "other.desktop").write_text("[Desktop Entry]\nExec=/usr/bin/other %U\n")
    assert desktop_id() == "datoolkit"                         # AppImage, but not integrated
    # AppImageLauncher style: its own file name, the AppImage path quoted, reached through a link
    (home / "applications" / "appimagekit_5c1e-DA_Toolkit.desktop").write_text(
        f'[Desktop Entry]\nName=DA Toolkit\nExec="{tmp_path / "link.AppImage"}" %U\n')
    assert desktop_id() == "appimagekit_5c1e-DA_Toolkit"
    (home / "applications" / "appimagekit_5c1e-DA_Toolkit.desktop").unlink()
    (home / "applications" / "kde" / "datoolkit.desktop").write_text(f"[Desktop Entry]\nTryExec={img}\nExec=x\n")
    assert desktop_id() == "kde-datoolkit"                     # a subdirectory joins the id with "-"


def test_the_window_icon_is_one_gtk_can_load(tmp_path, monkeypatch):
    from datoolkit.app import window_icon
    monkeypatch.delenv("APPDIR", raising=False)
    assert window_icon().endswith("icon.svg")
    monkeypatch.setenv("APPDIR", str(tmp_path))
    (tmp_path / "datoolkit.png").write_bytes(b"not a png")    # unreadable: falls back
    assert window_icon().endswith("icon.svg")


async def test_bad_requests_get_a_400_and_a_refused_settings_save_changes_nothing(env, tmp_path):  # noqa: F811
    engine, _, _ = env
    app: FastAPI = create_app("main", lambda emit: engine, companion_dir=tmp_path)
    app.state.engine = engine
    app.state.companion.token = "phone"
    m = {"x-token": "main"}
    engine.new_case("c", "open")
    engine.queue.add("c", [{"session_id": "s", "command": "uptime", "risk": "read_only"}])
    font, scroll = engine.cfg.settings.font_size, engine.cfg.settings.scrollback
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/api/case", json={"name": None}, headers=m)).status_code == 200    # an untitled case
        engine.queue.add("c", [{"session_id": "s", "command": "uptime", "risk": "read_only"}])
        for path, body in (("/api/case", {"name": "x", "sensitivity": "secret"}),
                           ("/api/queue/1/move", {"delta": "up"}), ("/api/settings", {"font_size": "big"}),
                           ("/api/hosts", {"host": {"name": "h", "kind": "ssh", "host": "h.lan", "port": "22a"}}),
                           ("/api/settings", {"font_size": 22, "scrollback": 50000, "companion_port": 443})):
            r = await c.post(path, json=body, headers=m)
            assert r.status_code == 400 and r.json()["error"], (path, body, r.status_code)
        assert (engine.cfg.settings.font_size, engine.cfg.settings.scrollback) == (font, scroll)
        # a token with characters outside ASCII is a wrong token, on both ports, not a crash
        assert (await c.get("/api/state", headers={"x-token": "tök".encode("latin-1")})).status_code == 403
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.state.companion.app), base_url="http://t") as c:
        assert (await c.get("/api/companion/state", headers={"x-token": "tök".encode("latin-1")})).status_code == 403


def test_the_browser_is_handed_a_private_page_not_the_address_with_the_token(tmp_path, monkeypatch, capsys):
    """webbrowser.open ran xdg-open, and the browser, with the address and its token on their
    command lines, which every user of this computer can read (/proc/<pid>/cmdline). The browser
    gets a page's path now, a file only this user can read, which moves on to the address with
    the token in its fragment (Jupyter's redirect file); it goes when DA Toolkit ends."""
    import os
    import re
    import stat
    import webbrowser
    from urllib.parse import unquote, urlparse

    import uvicorn

    class Server:                      # starts, and is done at once
        def __init__(self, config):
            self.started, self.should_exit = False, False

        def run(self):
            self.started = True

    seen = {}

    def open_(url):
        path = Path(unquote(urlparse(url).path))
        seen.update(url=url, page=path.read_text(), mode=stat.S_IMODE(os.stat(path).st_mode),
                    folder=stat.S_IMODE(os.stat(path.parent).st_mode), path=path)
        return True

    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(webbrowser, "open", open_)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    app_mod.main(["--browser", "--port", "48577"])
    token = re.search(r"#t=([\w-]+)", seen["page"]).group(1)
    assert len(token) > 30 and token not in seen["url"] and seen["url"].startswith(f"file://{tmp_path}/")
    assert f'location.replace("http://127.0.0.1:48577/#t={token}")' in seen["page"]
    assert (seen["mode"], seen["folder"]) == (0o600, 0o700)
    assert not seen["path"].exists() and not seen["path"].parent.exists()      # gone when it ended
    assert f"http://127.0.0.1:48577/#t={token}" in capsys.readouterr().out     # to open by hand
