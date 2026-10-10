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
import os
import shutil
import subprocess
import uuid as uuid_mod
from pathlib import Path

from ..hostenv import host_env

INTERNAL = ""          # opcode of tunnel-internal instructions (uuid, ping)
HANDSHAKE_TIMEOUT = 20
# Always this machine, on guacd's default port. The link to guacd is plain, unauthenticated
# TCP carrying the password and the whole session, so it is never made over a network.
GUACD = ("127.0.0.1", 4822)
# Upstream's 1.6.0 image, pinned to its multi-architecture manifest list
GUACD_IMAGE = ("docker.io/guacamole/guacd:1.6.0"
               "@sha256:8974eaa9ba32f713daf311e7cc8cd7e4cdfba1edea39eed75524e78ef4b08f4f")
CONTAINER = "datoolkit-guacd"     # the container DA Toolkit runs while an RDP session is open
CONTAINER_START_WAIT = 20         # seconds for guacd to answer once the container runs
GUACD_BINARIES = ("/usr/sbin/guacd", "/usr/bin/guacd", "/usr/local/sbin/guacd")


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


class GuacdContainer:
    """guacd in upstream's container, where the distribution doesn't package it. DA Toolkit
    runs it only while an RDP session is open: start() when one opens and guacd isn't
    answering, stop() when the last one closes and when the app quits. It is published on
    loopback only, since guacd's link is unencrypted. Never pulls: the technician downloads
    the pinned image once (install_help says how)."""

    def __init__(self, which=_which, run=_run):
        self._which, self._run = which, run
        self.runtime: str | None = None      # set while a container we run is up

    def usable(self) -> str | None:
        """The runtime that can start it now (installed, image present), or None."""
        runtime = container_runtime(self._which)
        return runtime if runtime and image_present(runtime, self._run) else None

    def adopt(self) -> None:
        """guacd is already answering: if it's our container, left by a run that didn't end
        cleanly, take it over so it's stopped like one we started."""
        if self.runtime:
            return
        runtime = container_runtime(self._which)
        if not runtime:
            return
        try:
            r = self._run([runtime, "container", "inspect", "--format", "{{.State.Running}}", CONTAINER], 15)
        except (OSError, subprocess.SubprocessError):
            return
        if r.returncode == 0 and r.stdout.strip() == "true":
            self.runtime = runtime

    def start(self) -> None:
        """Run the container (removing a stopped leftover first). Raises GuacError."""
        runtime = self.usable()
        if not runtime:
            raise GuacError("no container runtime with the guacd image")
        try:
            self._run([runtime, "rm", "-f", CONTAINER], 30)
            r = self._run([runtime, "run", "-d", "--rm", "--name", CONTAINER,
                           "-p", f"{GUACD[0]}:{GUACD[1]}:4822", GUACD_IMAGE], 60)
        except (OSError, subprocess.SubprocessError) as e:
            raise GuacError(str(e)) from e
        if r.returncode != 0:
            lines = (r.stderr or r.stdout).strip().splitlines()
            raise GuacError(lines[-1] if lines else f"{runtime} run failed")
        self.runtime = runtime

    def stop(self) -> None:
        if not self.runtime:
            return
        runtime, self.runtime = self.runtime, None
        try:   # guacd keeps no state: kill it straight away (--rm removes it)
            self._run([runtime, "rm", "-f"] + (["-t", "0"] if runtime == "podman" else []) + [CONTAINER], 30)
        except (OSError, subprocess.SubprocessError):
            pass


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


def install_help(release: dict[str, str] | None = None, which=_which,
                 installed: bool | None = None) -> tuple[str, list[str]]:
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
        return "guacd isn't installed. Install it (it starts as a service):", ["sudo apt install guacd"]
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


async def connect(timeout: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.wait_for(asyncio.open_connection(*GUACD), timeout)


def encode(*elements) -> str:
    return ",".join(f"{len(s)}.{s}" for s in (str(e) for e in elements)) + ";"


class Parser:
    """Incremental parser: feed text, get complete instructions as lists of strings."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> list[list[str]]:
        self._buf += text
        buf, out, start = self._buf, [], 0
        while True:
            elems, i, complete = [], start, False
            while True:
                dot = buf.find(".", i)
                if dot == -1:
                    break                          # length not complete yet
                try:
                    n = int(buf[i:dot])
                except ValueError as e:
                    raise GuacError(f"bad element length {buf[i:dot][:20]!r}") from e
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


async def _read_instruction(reader: asyncio.StreamReader, parser: Parser, decoder, pending: list) -> list[str]:
    while not pending:
        data = await reader.read(65536)
        if not data:
            raise GuacError("guacd closed the connection during the handshake")
        pending.extend(parser.feed(decoder.decode(data)))
    return pending.pop(0)


async def handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, protocol: str,
                    params: dict[str, str], width: int, height: int, dpi: int = 96,
                    timezone: str = "") -> tuple[str, list[str], str, object]:
    """Select the protocol, answer `args` from `params`, and wait for `ready`. Returns
    (connection id, the parameter names guacd asked for, text that arrived after `ready` and
    must be passed on, the UTF-8 decoder holding any split character)."""
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
    values = []
    for name in names:
        if name.startswith("VERSION_"):
            values.append(name)            # speak whatever protocol version guacd offers
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
