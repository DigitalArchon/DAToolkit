"""Guacamole protocol: the handshake with guacd and the relay between guacd and the browser.

guacd (Apache Guacamole's proxy daemon; install_help() says how to get it) makes the RDP
connection with FreeRDP and streams the desktop as Guacamole instructions. The server does the
handshake itself so connection parameters, including the password, never reach the browser;
after `ready` it relays instructions both ways. The browser side is guacamole-common-js's
WebSocketTunnel, which expects the tunnel UUID as its first instruction and has its internal
pings echoed back rather than forwarded.

Wire format: each element is LENGTH.VALUE, where LENGTH counts Unicode code points;
elements are separated by commas and the instruction ends with a semicolon."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import os
import re
import pwd
import shutil
import subprocess
import tempfile
import uuid as uuid_mod
from pathlib import Path

from ..hostenv import host_env

INTERNAL = ""          # opcode of tunnel-internal instructions (uuid, ping)
# The longest element taken (in characters): far above anything guacd sends (it cuts its own
# instructions into pieces of a few kilobytes), and a bound on what a stream that only claims a
# length can make the parser wait for and keep
MAX_ELEMENT = 1 << 20
_LENGTH_DIGITS = len(str(MAX_ELEMENT))
UNAUTHORIZED = "769"   # guacd's status code for a connection the server didn't authorize
HANDSHAKE_TIMEOUT = 20
# Always this machine, on guacd's default port. The link to guacd is plain, unauthenticated
# TCP carrying the password and the whole session, so it is never made over a network.
GUACD = ("127.0.0.1", 4822)
# Upstream's 1.6.0 image, pinned to its multi-architecture manifest list
GUACD_IMAGE = ("docker.io/guacamole/guacd:1.6.0"
               "@sha256:8974eaa9ba32f713daf311e7cc8cd7e4cdfba1edea39eed75524e78ef4b08f4f")
CONTAINER = "datoolkit-guacd"     # the container DA Toolkit runs while an RDP session is open
CONTAINER_START_WAIT = 20         # seconds for guacd to answer once the container runs
# Two lock files beside the instances' runtime directories, for the DA Toolkits one user has
# running (GuacdContainer): every one with remote desktops through the container holds the first
# shared, and starting, taking over and removing the container happen one at a time under the second
USERS_LOCK, TURN_LOCK = "datoolkit-guacd.lock", "datoolkit-guacd.turn.lock"
GUACD_BINARIES = ("/usr/sbin/guacd", "/usr/bin/guacd", "/usr/local/sbin/guacd")
# The kernel's tables of TCP sockets, each with its owner's uid: who guacd's port belongs to
PROC_TABLES = ("/proc/net/tcp", "/proc/net/tcp6")
LOGIN_DEFS = "/etc/login.defs"
LISTEN = "0A"
# Local addresses, as the tables write them, where a connection to 127.0.0.1 can land: 127.0.0.1,
# 0.0.0.0, :: (taking IPv4 too) and ::ffff:127.0.0.1
_TAKES_LOOPBACK = frozenset({"0100007F", "00000000", "0" * 32, "0000000000000000FFFF00000100007F"})


class GuacError(Exception):
    pass


def os_release(path: str = "/etc/os-release") -> dict[str, str]:
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k] = v.strip().strip('"')
    except OSError:
        pass
    return out


def _which(name: str) -> str | None:
    # the AppImage's own PATH entries come first; the helpers we look for are the system's
    return shutil.which(name, path=os.environ.get("PATH", "") + ":/usr/bin:/usr/sbin:/bin")


def _run(argv: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=host_env())


def container_runtime(which=_which) -> str | None:
    return next((r for r in ("podman", "docker") if which(r)), None)


def image_present(runtime: str, run=_run) -> bool:
    argv = ([runtime, "image", "exists", GUACD_IMAGE] if runtime == "podman"
            else [runtime, "image", "inspect", GUACD_IMAGE])
    try:
        return run(argv, 15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def lock_dir() -> Path:
    """Where the locks are kept: the user's runtime directory, which every DA Toolkit of theirs sees."""
    return Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())


class GuacdContainer:
    """guacd in upstream's container, where the distribution doesn't package it. DA Toolkit
    runs it only while an RDP session is open: start() when one opens and guacd isn't
    answering, stop() when the last one closes and when the app quits. It is published on
    loopback only, since guacd's link is unencrypted. Never pulls: the technician downloads
    the pinned image once (install_help says how), and the run itself says --pull=never.

    guacd reads what RDP servers send, hostile ones included, so the container gets nothing it
    has no use for: every capability dropped and no way to gain privileges (upstream's image
    runs guacd as an ordinary user on an unprivileged port).

    There is one container, of one name, for every DA Toolkit the user has running (a technician
    with two tickets has a reason to start two). Until Nov 2026 the second took over the first
    one's container and removed it when its own last RDP session closed, with the first one's
    desktops still on it. Now each instance holds a lock, shared, for as long as it uses the
    container, and removes the container only when it can have that lock to itself, that is,
    when no other instance is using it. The kernel drops a lock with the process that held it,
    so an instance that crashed holds nothing, and its container is still found and taken over
    by the next. Where no lock file can be had, an instance acts as if it were the only one."""

    def __init__(self, which=_which, run=_run, lock_dir: Path | None = None):
        self._which, self._run = which, run
        self._lock_dir = lock_dir
        self.runtime: str | None = None      # set while a container we run, or share, is up
        self._using: int | None = None       # the shared lock, held while `runtime` is set

    def usable(self) -> str | None:
        """The runtime that can start it now (installed, image present), or None."""
        runtime = container_runtime(self._which)
        return runtime if runtime and image_present(runtime, self._run) else None

    def _lock_file(self, name: str) -> int | None:
        """A descriptor of our own on a lock file, or None when there is none to be had."""
        try:
            fd = os.open((self._lock_dir or lock_dir()) / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            return None
        if os.fstat(fd).st_uid != os.getuid():     # someone else's file, in a shared /tmp
            os.close(fd)
            return None
        return fd

    @contextlib.contextmanager
    def _turn(self):
        """One instance at a time looks at the container and starts, takes over or removes it."""
        fd = self._lock_file(TURN_LOCK)
        try:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            if fd is not None:
                os.close(fd)

    def _use(self, runtime: str) -> None:
        """The container is ours to use from here on (within _turn)."""
        if self._using is None:
            self._using = self._lock_file(USERS_LOCK)
            if self._using is not None:
                fcntl.flock(self._using, fcntl.LOCK_SH)
        self.runtime = runtime

    def _last_user(self) -> bool:
        """Stop using the container (within _turn). True when no other instance is using it."""
        fd, self._using = self._using, None
        if fd is None:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def _running(self, runtime: str) -> bool:
        try:
            r = self._run([runtime, "container", "inspect", "--format", "{{.State.Running}}", CONTAINER], 15)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0 and r.stdout.strip() == "true"

    def adopt(self) -> None:
        """guacd is already answering: if it's our container, another instance's or one left by
        a run that didn't end cleanly, share it, so it's stopped like one we started once
        nobody uses it."""
        if self.runtime:
            return
        runtime = container_runtime(self._which)
        if not runtime:
            return
        with self._turn():
            if self._running(runtime):
                self._use(runtime)

    def start(self) -> None:
        """Run the container (removing a stopped leftover first). Raises GuacError."""
        runtime = self.usable()
        if not runtime:
            raise GuacError("no container runtime with the guacd image")
        with self._turn():
            if self._running(runtime):       # another instance started it since we looked: shared
                self._use(runtime)
                return
            try:
                self._run([runtime, "rm", "-f", CONTAINER], 30)
                r = self._run([runtime, "run", "-d", "--rm", "--pull=never", "--cap-drop=ALL",
                               "--security-opt", "no-new-privileges", "--name", CONTAINER,
                               "-p", f"{GUACD[0]}:{GUACD[1]}:4822", GUACD_IMAGE], 60)
            except (OSError, subprocess.SubprocessError) as e:
                raise GuacError(str(e)) from e
            if r.returncode != 0:
                lines = (r.stderr or r.stdout).strip().splitlines()
                raise GuacError(lines[-1] if lines else f"{runtime} run failed")
            self._use(runtime)

    def stop(self) -> bool:
        """We need the container no longer. It is removed, and True returned, unless another
        instance is using it: then it is left to that one."""
        if not self.runtime:
            return False
        runtime, self.runtime = self.runtime, None
        with self._turn():
            if not self._last_user():
                return False
            try:   # guacd keeps no state: kill it straight away (--rm removes it)
                self._run([runtime, "rm", "-f"] + (["-t", "0"] if runtime == "podman" else []) + [CONTAINER], 30)
            except (OSError, subprocess.SubprocessError):
                pass
            return True


async def probe(timeout: float = 3) -> bool:
    """Whether guacd answers the protocol, not just the port: rootless podman's port
    forwarder accepts connections before guacd inside the container is listening."""
    try:
        reader, writer = await connect(timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.write(encode("select", "rdp").encode())
        await writer.drain()
        ins = await asyncio.wait_for(_read_instruction(reader, Parser(), codecs.getincrementaldecoder("utf-8")(), []),
                                     timeout)
        return ins[0] == "args"
    except (OSError, asyncio.TimeoutError, GuacError):
        return False
    finally:
        writer.close()


def apt_has(package: str, run=_run) -> bool | None:
    """Whether apt can install `package` here (a candidate version), or None without apt."""
    try:
        r = run(["apt-cache", "policy", package], 15)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"Candidate:\s*(\S+)", r.stdout or "")
    return bool(m) and m.group(1) != "(none)"


def install_help(release: dict[str, str] | None = None, which=_which,
                 installed: bool | None = None, run=_run) -> tuple[str, list[str]]:
    """What to tell a technician whose guacd isn't answering: a sentence and the commands to
    run, for this distribution. guacd must listen on 127.0.0.1:4822 (its default)."""
    if installed is None:
        installed = any(os.path.exists(p) for p in GUACD_BINARIES)
    if installed:
        return ("guacd is installed but isn't running on 127.0.0.1:4822. Start it, and have it "
                "start at boot:", ["sudo systemctl enable --now guacd"])
    release = os_release() if release is None else release
    ids = {release.get("ID", "")} | set(release.get("ID_LIKE", "").split())
    if ids & {"ubuntu", "linuxmint"}:
        # Ubuntu 26.04 dropped guacd with FreeRDP 2 (it has only FreeRDP 3, which guacd 1.6.0
        # doesn't build against); 24.04 and Mint 22 have 1.3. Asked of apt, not guessed from the version.
        if apt_has("guacd", run) is not False:
            return "guacd isn't installed. Install it (it starts as a service):", ["sudo apt install guacd"]
        return _container_help(f"guacd isn't installed, and {release.get('PRETTY_NAME') or release.get('NAME') or 'this release'} "
                               "doesn't package it.", which, "sudo apt install podman")
    if ids & {"fedora", "rhel", "centos"}:
        epel = " (on RHEL and its rebuilds, enable EPEL first)" if "fedora" not in release.get("ID", "") else ""
        return (f"guacd isn't installed. Install it with its RDP plugin, then start it{epel}:",
                ["sudo dnf install guacd libguac-client-rdp", "sudo systemctl enable --now guacd"])
    if "arch" in ids:
        # guacamole-server is only in the AUR, and 1.6.0 doesn't build there: current glibc
        # trips its -Werror, and its RDP plugin doesn't compile against Arch's FreeRDP 3
        return _container_help("guacd isn't in the Arch or CachyOS repositories, and the AUR's "
                               "guacamole-server doesn't build as is.", which,
                               "sudo pacman -S --needed podman")
    if "debian" in ids:
        # Debian 12 and 13 ship no guacd
        return _container_help(f"guacd isn't installed, and {release.get('NAME') or 'Debian'} doesn't "
                               "package it.", which, "sudo apt install podman")
    return _container_help("guacd isn't installed, or isn't packaged for this distribution "
                           "(install podman or Docker first).", which, "")


def _container_help(why: str, which, install_podman: str) -> tuple[str, list[str]]:
    """Download upstream's guacd image once; DA Toolkit runs it while an RDP session is open."""
    text = (f"{why} DA Toolkit can run Apache Guacamole's own guacd container for you, only while an "
            "RDP session is open and reachable only from this machine. Download it once, then open "
            "the RDP host again:")
    runtime = container_runtime(which)
    if runtime == "docker":
        return text, [f"docker pull {GUACD_IMAGE}"]
    return text, ([install_podman] if install_podman and not runtime else []) + [f"podman pull {GUACD_IMAGE}"]


def _sockets(tables=None):
    """(local address, local port, remote address, remote port, state, owner's uid) of each TCP
    socket in the kernel's tables."""
    for table in tables or PROC_TABLES:
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            try:
                (la, lp), (ra, rp) = (x.split(":") for x in f[1:3])
                yield la.upper(), int(lp, 16), ra.upper(), int(rp, 16), f[3].upper(), int(f[7])
            except (ValueError, IndexError):
                continue


def _uid_min(path: str | None = None) -> int:
    """The first ordinary user's id (UID_MIN in /etc/login.defs; 1000 where it doesn't say)."""
    try:
        for line in Path(path or LOGIN_DEFS).read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "UID_MIN":
                return int(parts[1])
    except (OSError, ValueError):
        pass
    return 1000


def _owner_problem(uid: int, port: int) -> str | None:
    """Why guacd's port, held by `uid`, isn't one to hand a connection to; None when it is: this
    user's (guacd run by hand, or rootless podman's port forwarder, which runs as the user), or a
    system account's (root's, as Docker's forwarder is, or a service's: the guacd package's).
    None of those can another user of this computer run as."""
    if uid == os.getuid() or uid < _uid_min():
        return None
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        name = f"user id {uid}"
    return (f"What listens on 127.0.0.1:{port} is {name}'s, another user of this computer, not guacd as you or "
            f"the system run it, so nothing was sent to it: no RDP host, user name or password. Stop it, or run "
            f"guacd yourself or as a system service.")


def check_listener(port: int | None = None, tables=None) -> None:
    """Raise GuacError when a socket listening where a connection to guacd would land belongs to
    another user of this computer. guacd's link is plain, unauthenticated TCP carrying the RDP
    host, user and password: anyone else who took 127.0.0.1:4822 first would be handed them."""
    port = GUACD[1] if port is None else port
    for local, lport, _remote, _rport, state, uid in _sockets(tables):
        if state == LISTEN and lport == port and local in _TAKES_LOOPBACK:
            problem = _owner_problem(uid, port)
            if problem:
                raise GuacError(problem)


def check_peer(local_port: int, port: int | None = None, tables=None) -> None:
    """Once connected, the socket that accepted the connection from `local_port` (its owner is the
    listener's) is held to the same: one that took the port after check_listener isn't trusted."""
    port = GUACD[1] if port is None else port
    for local, lport, remote, rport, _state, uid in _sockets(tables):
        if lport == port and rport == local_port and local in _TAKES_LOOPBACK and remote in _TAKES_LOOPBACK:
            problem = _owner_problem(uid, port)
            if problem:
                raise GuacError(problem)
            return


async def connect(timeout: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """A connection to guacd, made only when what listens there is this user's or the system's
    (check_listener, check_peer): never handed to another user's. Raises GuacError then."""
    check_listener()
    reader, writer = await asyncio.wait_for(asyncio.open_connection(*GUACD), timeout)
    try:
        check_peer(writer.get_extra_info("sockname")[1])
    except GuacError:
        writer.close()
        raise
    return reader, writer


def encode(*elements) -> str:
    return ",".join(f"{len(s)}.{s}" for s in (str(e) for e in elements)) + ";"


def login_refused(error: str, code: str) -> bool:
    """Whether guacd's `error` says the server refused the user name and password. Status 769
    alone doesn't: guacd gives it to "Authentication failure (invalid credentials?)" and to
    "Security negotiation failed (wrong security type?)" alike (seen against Windows 11, Nov
    2026), and in the second the password was never tried."""
    return code == UNAUTHORIZED and "authentication" in error.lower()


def fingerprint_param(sha256: str) -> str:
    """A pinned certificate as guacd's `cert-fingerprints` (1.6+) takes it: FreeRDP's
    `hash:fingerprint` form, the fingerprint as colon-separated lower-case hex."""
    return "sha256:" + sha256.lower()


def enforces_pin(names: list[str], params: dict[str, str]) -> bool:
    """Whether this guacd checks the certificate against the pin on the connection itself:
    it offers `cert-fingerprints` (1.6 and later) and there is a pin to give it."""
    return "cert-fingerprints" in names and bool(params.get("cert-fingerprints"))


class Parser:
    """Incremental parser: feed text, get complete instructions as lists of strings.

    A length is one to seven ASCII digits and nothing else. int() also took a sign, spaces,
    underscores and other scripts' digits: "-4" moved the parser backwards, and `0.,-4.` kept it
    going round for ever, on the event loop, on a few bytes from whatever answered on guacd's
    port; a length of a billion made it keep everything that followed. Until Nov 2026."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[list[str]]:
        self._buf += text
        buf, out, start = self._buf, [], 0
        while True:
            elems, i, complete = [], start, False
            while True:
                dot = buf.find(".", i, i + _LENGTH_DIGITS + 1)
                # the length's digits, as far as they have arrived: all of them when the dot has
                length = buf[i:dot] if dot != -1 else buf[i:i + _LENGTH_DIGITS + 1]
                if dot == -1 and not length:
                    break                          # nothing of the next element yet
                if not (length.isascii() and length.isdigit()) or len(length) > _LENGTH_DIGITS:
                    raise GuacError(f"bad element length {length[:20]!r}")
                if dot == -1:
                    break                          # length not complete yet
                n = int(length)
                if n > MAX_ELEMENT:
                    raise GuacError(f"element of {n} characters: more than is taken")
                end = dot + 1 + n
                if end >= len(buf):
                    break                          # value or terminator not here yet
                elems.append(buf[dot + 1:end])
                i = end + 1
                if buf[end] == ";":
                    complete = True
                    break
                if buf[end] != ",":
                    raise GuacError(f"bad element terminator {buf[end]!r}")
            if not complete:
                break
            out.append(elems)
            start = i
        self._buf = buf[start:]
        return out


class ConnectWatch:
    """Reads what guacd sends after `ready`, without changing it, for what the audit log needs:
    when the remote desktop is really connected, and why guacd gave up.

    `ready` only says guacd took the connection parameters. The RDP connection is made after
    it and can still fail (a wrong password, a security mode the server refuses), which guacd
    reports with an `error` instruction. Connected is the first `sync` ending a frame that holds
    something besides the pointer: guacd 1.3 sends no `sync` until the RDP connection is up,
    while 1.6 sends one at once, before it connects, ending a frame of only `cursor` and `mouse`
    (both seen against Windows 11, Nov 2026)."""

    # what a frame may hold that doesn't show a connected desktop
    _NOT_DRAWING = frozenset({"cursor", "mouse", "nop", "sync", "ack", "argv", "required", "msg", "error",
                              "disconnect", "log", "name"})

    def __init__(self) -> None:
        self._parser = Parser()
        self._drawn = False
        self.connected = False
        self.error = ""          # guacd's reason, from its first `error` instruction
        self.code = ""           # its Guacamole status code (769: unauthorized, 519: upstream refused, ...)

    def feed(self, text: str) -> list[str]:
        """The events this text completes: "connected" and "error", once each. Raises GuacError
        on text that isn't the Guacamole protocol."""
        events = []
        for ins in self._parser.feed(text):
            op = ins[0] if ins else ""
            if op == "error":
                if not self.error:
                    self.error, self.code = (ins[1] if len(ins) > 1 else "error"), (ins[2] if len(ins) > 2 else "")
                    events.append("error")
            elif op == "sync":
                if self._drawn and not self.connected:
                    self.connected = True
                    events.append("connected")
                self._drawn = False
            elif op not in self._NOT_DRAWING:
                self._drawn = True
        return events


def decode(decoder, data: bytes) -> str:
    """What guacd sent, as text. The protocol is UTF-8; anything else isn't guacd, or isn't
    well, and is a GuacError like other things that aren't the protocol, not a decoding error
    nobody catches."""
    try:
        return decoder.decode(data)
    except UnicodeDecodeError as e:
        raise GuacError("what answered on guacd's port isn't speaking the Guacamole protocol (it sent bytes that "
                        "aren't UTF-8)") from e


async def _read_instruction(reader: asyncio.StreamReader, parser: Parser, decoder, pending: list) -> list[str]:
    while not pending:
        data = await reader.read(65536)
        if not data:
            raise GuacError("guacd closed the connection during the handshake")
        pending.extend(parser.feed(decode(decoder, data)))
    return pending.pop(0)


async def handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, protocol: str,
                    params: dict[str, str], width: int, height: int, dpi: int = 96,
                    timezone: str = "", before_connect=None) -> tuple[str, list[str], str, object]:
    """Select the protocol, answer `args` from `params`, and wait for `ready`. Returns
    (connection id, the parameter names guacd asked for, text that arrived after `ready` and
    must be passed on, the UTF-8 decoder holding any split character). `before_connect` is
    awaited with the names guacd asked for, once they are known and before anything of
    `params` (the password among it) is sent: what it raises ends the handshake there."""
    parser, decoder, pending = Parser(), codecs.getincrementaldecoder("utf-8")(), []

    async def expect(opcode: str) -> list[str]:
        ins = await asyncio.wait_for(_read_instruction(reader, parser, decoder, pending), HANDSHAKE_TIMEOUT)
        if ins[0] == "error":
            raise GuacError(f"guacd: {ins[1] if len(ins) > 1 else 'error'}")
        if ins[0] != opcode:
            raise GuacError(f"expected {opcode} from guacd, got {ins[0]}")
        return ins[1:]

    writer.write(encode("select", protocol).encode())
    await writer.drain()
    names = await expect("args")
    if before_connect:
        await before_connect(names)
    pinned = enforces_pin(names, params)
    values = []
    for name in names:
        if name.startswith("VERSION_"):
            values.append(name)            # speak whatever protocol version guacd offers
        elif name == "ignore-cert" and pinned:
            # guacd 1.6+: FreeRDP checks the certificate against the pin on this very
            # connection, so the probe that read it (rdpcert.fetch) can't be told apart from it
            values.append("false")
        else:
            values.append(params.get(name, ""))
    writer.write((encode("size", width, height, dpi) + encode("audio") + encode("video")
                  + encode("image", "image/png", "image/jpeg", "image/webp")
                  + (encode("timezone", timezone) if timezone else "")
                  + encode("connect", *values)).encode())
    await writer.drain()
    ready = await expect("ready")
    leftover = "".join(encode(*ins) for ins in pending) + parser._buf
    return (ready[0] if ready else ""), names, leftover, decoder


def tunnel_uuid() -> str:
    return encode(INTERNAL, str(uuid_mod.uuid4()))


def is_internal(message: str) -> bool:
    return message.startswith("0.,")
