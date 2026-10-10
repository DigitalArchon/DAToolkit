"""Sessions. Local, SSH and WinRM sessions are programs running in a PTY; RDP sessions are
remote desktops drawn by guacd (see guac.py) and have no process or transcript here. Window
sessions are a window of another program on this desktop (a ScreenConnect control window, a VM
console), read only through screenshots (see xwindow.py).

Sessions reaching the same machine share a `device` key, so the model can be told that an
RDP view and a WinRM shell are the same computer. Opening a session links it automatically
to open sessions with the same address; the technician can link or unlink by hand."""

from __future__ import annotations

import asyncio
import codecs
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..hostenv import host_env
from .ptyproc import PtyProcess

BACKLOG_BYTES = 512 * 1024

_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC
    r"|\x1b\[[0-?]*[ -/]*[@-~]"            # CSI
    r"|\x1b[PX^_][^\x1b]*\x1b\\"           # DCS/SOS/PM/APC
    r"|\x1b[()][A-Za-z0-9]"                # charset
    r"|\x1b[@-Z\\-_=>]"                    # other 2-byte
)


class TranscriptWriter:
    """Appends an ANSI-stripped copy of terminal output to a log file."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        self._file = None
        self._times = None   # sidecar: "<unix time> <byte offset>" per write, for timeline replay

    def open(self, path: Path | None) -> None:
        self.close()
        if path is not None:
            self._file = path.open("a", encoding="utf-8")
            self._times = path.with_suffix(path.suffix + ".times").open("a", encoding="utf-8")

    def write(self, data: bytes) -> None:
        if self._file is None:
            return
        text = self._pending + self._decoder.decode(data)
        # hold back an escape sequence split across reads
        esc = text.rfind("\x1b")
        if esc != -1 and len(text) - esc < 64 and not _ANSI.match(text, esc):
            text, self._pending = text[:esc], text[esc:]
        else:
            self._pending = ""
        text = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "")
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
        if not text:
            return
        self._file.write(text)
        self._file.flush()
        if self._times:
            self._times.write(f"{time.time():.3f} {self._file.tell()}\n")
            self._times.flush()

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None
        if self._times:
            self._times.close()
            self._times = None


@dataclass
class Session:
    id: str
    name: str
    kind: str  # "local" | "ssh" | "winrm"
    proc: PtyProcess
    target: str = ""
    shell: str = ""
    os_hint: str = ""
    host_name: str = ""
    exited: bool = False
    exit_status: int | None = None
    backlog: bytearray = field(default_factory=bytearray)
    subscribers: set = field(default_factory=set)
    transcript: TranscriptWriter = field(default_factory=TranscriptWriter)
    used_stored_password: bool = False
    address: str = ""          # host address used for automatic device linking
    device: str = ""           # sessions with the same key reach the same machine
    link: str = "auto"         # "auto" | "manual" | "unlinked" (never auto-linked again)

    def roster(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "target": self.target,
                "shell": self.shell, "os_hint": self.os_hint, "exited": self.exited,
                "host_name": self.host_name, "device": self.device, "link": self.link}


@dataclass
class RdpSession:
    """A remote desktop. `params` are guacd connection parameters, password included; they
    stay in this process and are only ever sent to guacd."""
    id: str
    name: str
    params: dict
    kind: str = "rdp"
    target: str = ""
    shell: str = "remote desktop (keystrokes go to the focused window)"
    os_hint: str = ""
    host_name: str = ""
    exited: bool = False
    address: str = ""
    device: str = ""
    link: str = "auto"
    cert: dict = field(default_factory=dict)
    connected: bool = False
    relay: object = None        # the task relaying the current browser connection

    def roster(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "target": self.target,
                "shell": self.shell, "os_hint": self.os_hint, "exited": self.exited,
                "host_name": self.host_name, "device": self.device, "link": self.link,
                "connected": self.connected,
                "cert": {k: self.cert.get(k) for k in ("tls", "sha256", "subject") if k in self.cert}}


@dataclass
class WindowSession:
    """A window of another program on this desktop, found by its X window id. DA Toolkit only
    takes screenshots of it; commands for it are pasted there by the technician."""
    id: str
    name: str
    xid: int
    wm_class: str = ""
    title: str = ""
    kind: str = "window"
    target: str = ""
    shell: str = "a window on the technician's screen (commands are pasted there by the technician)"
    os_hint: str = ""
    host_name: str = ""
    exited: bool = False
    address: str = ""
    device: str = ""
    link: str = "auto"

    def roster(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "target": self.target,
                "shell": self.shell, "os_hint": self.os_hint, "exited": self.exited,
                "host_name": self.host_name, "device": self.device, "link": self.link,
                "xid": self.xid, "wm_class": self.wm_class, "title": self.title}


class SessionManager:
    def __init__(self, on_change: Callable[[], None]):
        self.sessions: dict[str, Session | RdpSession | WindowSession] = {}
        self._on_change = on_change
        self._transcript_dir: Callable[[str], Path | None] = lambda sid: None
        self._devices = 0

    def set_transcript_paths(self, path_for: Callable[[str], Path | None]) -> None:
        self._transcript_dir = path_for
        for s in self.sessions.values():
            if isinstance(s, Session):
                s.transcript.open(path_for(s.id))

    def unique_id(self, base: str) -> str:
        base = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-") or "session"
        if base not in self.sessions:
            return base
        n = 2
        while f"{base}-{n}" in self.sessions:
            n += 1
        return f"{base}-{n}"

    def spawn(self, sid: str, argv: list[str], env: dict[str, str], **meta) -> Session:
        full_env = host_env(TERM="xterm-256color", COLORTERM="truecolor", **env)
        proc = PtyProcess.spawn(argv, env=full_env, cwd=os.path.expanduser("~"), dimensions=(30, 100))
        sess = Session(id=sid, proc=proc, **meta)
        sess.transcript.open(self._transcript_dir(sid))
        self._auto_link(sess)
        self.sessions[sid] = sess
        asyncio.get_running_loop().add_reader(proc.fd, self._on_readable, sess)
        self._on_change()
        return sess

    def add(self, sess: RdpSession | WindowSession) -> RdpSession | WindowSession:
        self._auto_link(sess)
        self.sessions[sess.id] = sess
        self._on_change()
        return sess

    # ---------------------------------------------------------------- device links

    def _new_device(self) -> str:
        self._devices += 1
        return f"dev{self._devices}"

    def _auto_link(self, sess) -> None:
        addr = sess.address.strip().lower()
        for other in self.sessions.values():
            if (addr and not other.exited and other.link != "unlinked"
                    and other.address.strip().lower() == addr):
                sess.device = other.device
                return
        sess.device = self._new_device()

    def link(self, sid: str, to: str | None) -> None:
        """Link `sid` to the device of `to`, or unlink it (to=None) for good."""
        sess = self.sessions[sid]
        if to:
            if to == sid:
                return
            sess.device, sess.link = self.sessions[to].device, "manual"
        else:
            sess.device, sess.link = self._new_device(), "unlinked"
        self._on_change()

    def device_of(self, sid: str) -> list:
        """Every open session on the same device as `sid`, itself included."""
        dev = self.sessions[sid].device
        return [s for s in self.sessions.values() if s.device == dev and not s.exited]

    def _on_readable(self, sess: Session) -> None:
        try:
            data = os.read(sess.proc.fd, 65536)
        except OSError:
            data = b""
        if not data:
            self._mark_exited(sess)
            return
        self._publish(sess, data)

    def _publish(self, sess: Session, data: bytes) -> None:
        sess.backlog.extend(data)
        if len(sess.backlog) > BACKLOG_BYTES:
            del sess.backlog[: len(sess.backlog) - BACKLOG_BYTES]
        sess.transcript.write(data)
        for q in list(sess.subscribers):
            q.put_nowait(data)

    def _mark_exited(self, sess: Session) -> None:
        loop = asyncio.get_running_loop()
        loop.remove_reader(sess.proc.fd)
        sess.exited = True
        try:
            sess.proc.isalive()  # reaps the child
            sess.exit_status = sess.proc.exitstatus
        except Exception:  # noqa: BLE001
            pass
        sess.proc.close()
        self._publish(sess, f"\r\n\x1b[2m[session ended, exit status {sess.exit_status}]\x1b[0m\r\n".encode())
        sess.transcript.close()
        self._on_change()

    def mark_window_closed(self, sess: WindowSession) -> None:
        """The window has gone: the session stays, marked closed, like an exited shell."""
        if not sess.exited:
            sess.exited = True
            self._on_change()

    def subscribe(self, sid: str) -> tuple[bytes, asyncio.Queue]:
        sess = self.sessions[sid]
        q: asyncio.Queue = asyncio.Queue()
        sess.subscribers.add(q)
        return bytes(sess.backlog), q

    def unsubscribe(self, sid: str, q: asyncio.Queue) -> None:
        if sid in self.sessions:
            self.sessions[sid].subscribers.discard(q)

    def write(self, sid: str, data: bytes) -> None:
        sess = self.sessions[sid]
        if not sess.exited:
            os.write(sess.proc.fd, data)

    def resize(self, sid: str, cols: int, rows: int) -> None:
        sess = self.sessions[sid]
        if not sess.exited and cols > 0 and rows > 0:
            sess.proc.setwinsize(rows, cols)

    def close(self, sid: str) -> None:
        sess = self.sessions.pop(sid, None)
        if sess is None:
            return
        if isinstance(sess, (RdpSession, WindowSession)):
            sess.exited = True
            if isinstance(sess, RdpSession) and sess.relay:
                sess.relay.cancel()
            self._on_change()
            return
        if not sess.exited:
            try:
                asyncio.get_running_loop().remove_reader(sess.proc.fd)
            except RuntimeError:
                pass
            sess.proc.terminate()
        sess.transcript.close()
        for q in list(sess.subscribers):
            q.put_nowait(None)
        self._on_change()

    def close_all(self) -> None:
        for sid in list(self.sessions):
            self.close(sid)

    def roster(self) -> list[dict]:
        return [s.roster() for s in self.sessions.values()]
