"""Sessions. Local, SSH and WinRM sessions are programs running in a PTY; RDP sessions are
remote desktops drawn by guacd (see guac.py) and have no process or transcript here. Window
sessions are a window of another program on this desktop (a ScreenConnect control window, a VM
console), read only through screenshots (see xwindow.py).

Sessions reaching the same machine share a `device` key, so the model can be told that an
RDP view and a WinRM shell are the same computer. Opening a session links it automatically
to open sessions that reach the same address the same way (link_key: the address and the jump
hosts on the way, and for a loopback address the port); the technician can link or unlink by
hand."""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..hostenv import host_env
from .ptyproc import PtyProcess
from .ssh import jump_route

BACKLOG_BYTES = 512 * 1024
CLOSING_READS = 16                       # reads of 64 KB taken from a PTY whose program has exited
VIEWER_BEHIND_BYTES = 8 * 1024 * 1024    # output a viewer may fall behind by before it is skipped


class OutputQueue:
    """One viewer's output still to be sent, bounded in bytes. A viewer that falls further
    behind (a tab in the background while `yes` or a busy tcpdump runs) has what waits dropped
    for a line saying so, rather than the app's memory growing without end; the transcript
    keeps all of it. None (the session ended) is never dropped: nothing comes after it."""

    def __init__(self, limit: int = VIEWER_BEHIND_BYTES):
        self._q: asyncio.Queue = asyncio.Queue()
        self._bytes, self._limit = 0, limit

    def put_nowait(self, data: bytes | None) -> None:
        if data is not None and self._bytes + len(data) > self._limit:
            skipped = self._bytes + len(data)
            while not self._q.empty():          # in place: the sender is waiting on this queue
                self._q.get_nowait()
            data = (f"\r\n\x1b[0;33m[{skipped:,} bytes of output skipped here: this view fell behind. The "
                    "transcript has all of it.]\x1b[0m\r\n").encode()
            self._bytes = 0
        if data is not None:
            self._bytes += len(data)
        self._q.put_nowait(data)

    async def get(self) -> bytes | None:
        data = await self._q.get()
        if data is not None:
            self._bytes -= len(data)
        return data

_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"   # OSC
    r"|\x1b\[[0-?]*[ -/]*[@-~]"            # CSI
    r"|\x1b[PX^_][^\x1b]*\x1b\\"           # DCS/SOS/PM/APC
    r"|\x1b[()][A-Za-z0-9]"                # charset
    # other 2-byte: ESC 7/8 (save/restore cursor), ESC = >, ESC c, ESC M... Not the introducers
    # above (ESC [ ] P X ^ _): taken as 2-byte, the start of an OSC title a read cut off went
    # unnoticed, and the title text landed in the transcript
    r"|\x1b[0-OQ-WYZ\\`-~]"
)
# what starts an OSC, DCS, SOS, PM or APC string
_INTRODUCER = re.compile(r"\x1b[\]PX^_]")
# the start of a control or charset sequence and no more: a read cut it and nothing followed
_CUT_SEQUENCE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*|[()])?")


def _open_string(text: str) -> int:
    """Where a string (OSC, DCS, SOS, PM or APC) begins that BEL or ESC \\ has not ended yet, its
    end being in the next read; -1 when there is none. That is the first introducer after the
    last terminator, found in one pass. Until Nov 2026 a pattern looked for it, which tried
    every introducer in turn to the end of the text: output of nothing but ESC ] kept the whole
    app busy for 24 seconds per megabyte."""
    ended = max(text.rfind("\x07"), text.rfind("\x1b\\")) + 1
    found = _INTRODUCER.search(text, ended)
    return found.start() if found else -1


class TranscriptWriter:
    """Appends an ANSI-stripped copy of terminal output to a log file."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        self._file = None
        self._times = None   # sidecar: "<unix time> <byte offset>" per write, for timeline replay

    def open(self, path: Path | None) -> None:
        """Write to `path` from here on (another case's folder), or nowhere. What is held back
        goes with the output that follows it."""
        self._close_files()
        if path is not None:
            self._file = path.open("a", encoding="utf-8")
            try:
                self._times = path.with_suffix(path.suffix + ".times").open("a", encoding="utf-8")
            except OSError:
                self._close_files()
                raise

    def write(self, data: bytes) -> None:
        if self._file is None:
            return
        text = self._pending + self._decoder.decode(data)
        # hold back an escape sequence split across reads: a string (window titles run long)
        # until its end arrives, up to 4 KB; any other up to 64 characters
        open_string = _open_string(text)
        esc = text.rfind("\x1b")
        if open_string != -1 and len(text) - open_string < 4096:
            text, self._pending = text[:open_string], text[open_string:]
        elif open_string == -1 and esc != -1 and len(text) - esc < 64 and not _ANSI.match(text, esc):
            text, self._pending = text[:esc], text[esc:]
        else:
            self._pending = ""
        self._put(text)

    def _put(self, text: str) -> None:
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
        """The session has ended: what was being held back is written, since nothing will come
        to complete it. A stray introducer (a title cut off by a dropped connection, binary
        output) held back up to 4 KB of what followed, the error that ended the session among
        it, and that text was dropped here until Nov 2026. Only the introducer itself, or the
        start of a sequence with nothing after it, is left out."""
        if self._file:
            held, self._pending = self._pending + self._decoder.decode(b"", final=True), ""
            self._decoder.reset()
            if _INTRODUCER.match(held):
                held = held[2:]
            try:
                if not _CUT_SEQUENCE.fullmatch(held):
                    self._put(held)
            except OSError:              # a full disk: the session still closes
                pass
        self._close_files()

    def give_up(self) -> None:
        """The file can't be written: nothing more is tried for it."""
        self._pending = ""
        self._close_files()

    def _close_files(self) -> None:
        """Never raises: closing a file writes what it still holds, which a full disk refuses
        again, and a session must close all the same."""
        for f in (self._file, self._times):
            if f:
                try:
                    f.close()
                except OSError:
                    pass
        self._file = self._times = None


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
    pending: bytearray = field(default_factory=bytearray)   # input the PTY hasn't taken yet
    subscribers: set = field(default_factory=set)
    transcript: TranscriptWriter = field(default_factory=TranscriptWriter)
    transcript_error: str = ""      # why the transcript was switched off (it couldn't be written)
    child_fd: int = -1              # a pidfd of the program: readable once it has exited
    used_stored_password: bool = False
    address: str = ""          # host address used for automatic device linking,
    jump: str = ""             # with the jump hosts it is reached through (Host.jump)
    port: int = 0              # and its port (see link_key)
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
    jump: str = ""
    port: int = 0
    device: str = ""
    link: str = "auto"
    cert: dict = field(default_factory=dict)
    connected: bool = False
    pin_enforced: bool | None = None   # guacd checked the pin on the connection itself (1.6+)
    relay: object = None        # the task relaying the current browser connection

    def roster(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "target": self.target,
                "shell": self.shell, "os_hint": self.os_hint, "exited": self.exited,
                "host_name": self.host_name, "device": self.device, "link": self.link,
                "connected": self.connected, "pin_enforced": self.pin_enforced,
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
    jump: str = ""
    port: int = 0
    device: str = ""
    link: str = "auto"

    def roster(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "target": self.target,
                "shell": self.shell, "os_hint": self.os_hint, "exited": self.exited,
                "host_name": self.host_name, "device": self.device, "link": self.link,
                "xid": self.xid, "wm_class": self.wm_class, "title": self.title}


def _is_loopback(address: str) -> bool:
    if address == "localhost" or address.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return (getattr(ip, "ipv4_mapped", None) or ip).is_loopback


def link_key(sess) -> tuple | None:
    """What automatic linking goes by: two sessions are the same device only when they reach the
    same machine, and an address alone doesn't say that. 192.168.1.1 behind one client's jump
    host and 192.168.1.1 behind another's are two routers, so the jump hosts on the way are part
    of the key. A loopback address names whatever listens on that port, usually a port forwarded
    from somewhere else (a host saved as localhost:3390), so there the port is part of it too,
    and such a host is never the technician's own shell: only local shells link to each other.
    None for a session with no address (a window), which links to nothing by itself. Until Nov
    2026 the bare address was the key, and the model was told that such sessions were one machine
    and to send one's commands to the other."""
    address = sess.address.strip().lower().rstrip(".").strip("[]")
    if not address:
        return None
    if sess.kind == "local":
        return ("local",)
    route = jump_route(sess.jump)
    if _is_loopback(address):
        return ("loopback", route, sess.port)
    return (address, route)


class SessionManager:
    def __init__(self, on_change: Callable[[], None]):
        self.sessions: dict[str, Session | RdpSession | WindowSession] = {}
        self._on_change = on_change
        self._transcript_dir: Callable[[str], Path | None] = lambda sid: None
        self._devices = 0
        self._tasks: set[asyncio.Task] = set()     # reaping and terminating, off the event loop
        self._reserved: set[str] = set()           # ids of sessions still opening (see reserve)

    def set_transcript_paths(self, path_for: Callable[[str], Path | None]) -> None:
        self._transcript_dir = path_for
        for s in self.sessions.values():
            # not one that has exited: it writes nothing more, and its file would be made in the
            # new case's folder, empty, and kept open for as long as its tab stays
            if isinstance(s, Session) and not s.exited:
                self._open_transcript(s)

    def unique_id(self, base: str) -> str:
        base = re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-") or "session"
        taken = self.sessions.keys() | self._reserved
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    def reserve(self, base: str) -> str:
        """An id for a session that has things to wait for before it can be registered (a
        certificate to read, a question to the technician), kept out of unique_id's answers
        until then. Without this, two opens of one host waiting side by side were given the same
        id and the second replaced the first; and a session registered under the id meanwhile
        was replaced, its program still running and logged in, with no tab to close it from.
        The caller registers the session (spawn, add) or gives the id back (release)."""
        sid = self.unique_id(base)
        self._reserved.add(sid)
        return sid

    def release(self, sid: str) -> None:
        self._reserved.discard(sid)

    def _register(self, sess) -> None:
        if sess.id in self.sessions:         # never in place of another: that one would be orphaned
            raise ValueError(f"session id {sess.id!r} is already in use")
        self._auto_link(sess)
        self.sessions[sess.id] = sess
        self._reserved.discard(sess.id)

    def spawn(self, sid: str, argv: list[str], env: dict[str, str], **meta) -> Session:
        if sid in self.sessions:
            raise ValueError(f"session id {sid!r} is already in use")
        full_env = host_env(TERM="xterm-256color", COLORTERM="truecolor", **env)
        proc = PtyProcess.spawn(argv, env=full_env, cwd=os.path.expanduser("~"), dimensions=(30, 100))
        try:
            # Never block the event loop on the PTY: a paste the program isn't reading waits in
            # `pending` (see write) instead of stalling every session, the GUI and the companion.
            os.set_blocking(proc.fd, False)
            sess = Session(id=sid, proc=proc, **meta)
            self._open_transcript(sess)
            self._register(sess)
            asyncio.get_running_loop().add_reader(proc.fd, self._on_readable, sess)
        except BaseException:
            # the program is running already: not left behind, logged in, with no session to it
            self.sessions.pop(sid, None)
            proc.terminate()
            raise
        self._watch_child(sess)
        self._on_change()
        return sess

    def add(self, sess: RdpSession | WindowSession) -> RdpSession | WindowSession:
        self._register(sess)
        self._on_change()
        return sess

    # ---------------------------------------------------------------- transcripts

    def _open_transcript(self, sess: Session) -> None:
        try:
            sess.transcript.open(self._transcript_dir(sess.id))
            sess.transcript_error = ""
        except OSError as e:
            self._transcript_off(sess, e)

    def _transcript_off(self, sess: Session, why: OSError) -> None:
        """The transcript can't be opened or written (a full disk, usually). The terminal must
        go on working: it may be the shell that frees the space. So the transcript is switched
        off for this session, and the technician is told once, in the terminal itself. Until Nov
        2026 the error stopped the output on its way to every viewer, and closing the session
        raised it again."""
        sess.transcript.give_up()
        sess.transcript_error = why.strerror or str(why)
        self._show(sess, (f"\r\n\x1b[0;33m[DA Toolkit can't write this session's transcript ({sess.transcript_error}): "
                          "it is switched off for this session. The terminal works as before; output from here on is "
                          "not in the case's transcript, so copy what the AI should see from the terminal.]"
                          "\x1b[0m\r\n").encode())

    # ---------------------------------------------------------------- device links

    def _new_device(self) -> str:
        self._devices += 1
        return f"dev{self._devices}"

    def _auto_link(self, sess) -> None:
        key = link_key(sess)
        for other in self.sessions.values():
            if key and not other.exited and other.link != "unlinked" and link_key(other) == key:
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

    def _task(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_closed(self, timeout: float = 3) -> None:
        """Wait (briefly) for the background reaping and termination of closed sessions."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    def _on_readable(self, sess: Session) -> None:
        try:
            data = os.read(sess.proc.fd, 65536)
        except BlockingIOError:
            return                       # woken for nothing: not the end of the session
        except OSError:
            data = b""
        if not data:
            self._mark_exited(sess)
            return
        self._publish(sess, data)

    def _publish(self, sess: Session, data: bytes) -> None:
        self._show(sess, data)
        try:
            sess.transcript.write(data)
        except OSError as e:
            self._transcript_off(sess, e)

    def _show(self, sess: Session, data: bytes) -> None:
        """To the terminal's viewers and its backlog, which need no file."""
        sess.backlog.extend(data)
        if len(sess.backlog) > BACKLOG_BYTES:
            del sess.backlog[: len(sess.backlog) - BACKLOG_BYTES]
        for q in list(sess.subscribers):
            q.put_nowait(data)

    def _watch_child(self, sess: Session) -> None:
        """Have the session end when its program exits, not only when the PTY reports EOF: EOF
        comes when the last process holding the terminal has gone, and a background job the
        shell left (`sleep 600 &`, then `exit`) holds it. The session stayed "open" until the
        job finished, and Run typed into a terminal nobody read. A pidfd becomes readable when
        the program has exited; a kernel without them (before 5.3) leaves EOF as the only sign."""
        try:
            sess.child_fd = os.pidfd_open(sess.proc.pid)
        except (AttributeError, OSError):
            return
        asyncio.get_running_loop().add_reader(sess.child_fd, self._on_child_exit, sess)

    def _unwatch_child(self, sess: Session) -> None:
        if sess.child_fd >= 0:
            try:
                asyncio.get_running_loop().remove_reader(sess.child_fd)
            except RuntimeError:         # no event loop any more
                pass
            os.close(sess.child_fd)
            sess.child_fd = -1

    def _on_child_exit(self, sess: Session) -> None:
        self._unwatch_child(sess)
        if sess.exited:
            return
        for _ in range(CLOSING_READS):       # what it printed last is still in the PTY
            try:
                data = os.read(sess.proc.fd, 65536)
            except OSError:                  # nothing more to read, or the PTY has gone too
                break
            if not data:
                break
            self._publish(sess, data)
        self._mark_exited(sess)

    def _mark_exited(self, sess: Session) -> None:
        """The PTY reported EOF, or the program exited. The child is reaped on a worker thread
        (it may take a moment to go), and the closing line is written once its exit status is
        known."""
        loop = asyncio.get_running_loop()
        loop.remove_reader(sess.proc.fd)
        self._unwatch_child(sess)
        self._drop_pending(sess)
        sess.exited = True
        sess.proc.close()
        self._on_change()
        self._task(self._reap(sess))

    async def _reap(self, sess: Session) -> None:
        try:
            sess.exit_status = await asyncio.to_thread(sess.proc.reap, 1.0)
        except Exception:  # noqa: BLE001 - already reaped, or gone some other way
            pass
        self._publish(sess, f"\r\n\x1b[2m[session ended, exit status {sess.exit_status}]\x1b[0m\r\n".encode())
        sess.transcript.close()

    def mark_window_closed(self, sess: WindowSession) -> None:
        """The window has gone: the session stays, marked closed, like an exited shell."""
        if not sess.exited:
            sess.exited = True
            self._on_change()

    def subscribe(self, sid: str) -> tuple[bytes, OutputQueue]:
        sess = self.sessions[sid]
        q = OutputQueue()
        sess.subscribers.add(q)
        return bytes(sess.backlog), q

    def unsubscribe(self, sid: str, q: OutputQueue) -> None:
        if sid in self.sessions:
            self.sessions[sid].subscribers.discard(q)

    def write(self, sid: str, data: bytes) -> None:
        """Input for the program. What the PTY doesn't take at once (its input buffer is a few
        KB, and a program that isn't reading never empties it) waits in `pending` and goes in
        as the PTY accepts it, so a long paste never stalls the event loop."""
        sess = self.sessions[sid]
        if sess.exited:
            return
        if sess.pending:
            sess.pending += data
            return
        try:
            n = os.write(sess.proc.fd, data)
        except BlockingIOError:
            n = 0
        if n < len(data):
            sess.pending += data[n:]
            asyncio.get_running_loop().add_writer(sess.proc.fd, self._on_writable, sess)

    def _on_writable(self, sess: Session) -> None:
        try:
            n = os.write(sess.proc.fd, sess.pending)
        except BlockingIOError:
            return
        except OSError:                  # the PTY is gone: the reader sees EOF and ends the session
            n = len(sess.pending)
        del sess.pending[:n]
        if not sess.pending:
            asyncio.get_running_loop().remove_writer(sess.proc.fd)

    def _drop_pending(self, sess: Session) -> None:
        if sess.pending:
            sess.pending.clear()
            try:
                asyncio.get_running_loop().remove_writer(sess.proc.fd)
            except (RuntimeError, OSError):
                pass

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
        self._unwatch_child(sess)
        if not sess.exited:
            try:
                asyncio.get_running_loop().remove_reader(sess.proc.fd)
                self._drop_pending(sess)
                # SIGHUP, a moment's grace, then SIGKILL: waited for on a worker thread (see
                # wait_closed), never on the event loop
                self._task(asyncio.to_thread(sess.proc.terminate))
            except RuntimeError:         # no event loop (scripts, some tests): wait here
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
