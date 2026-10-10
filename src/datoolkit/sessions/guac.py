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
GUACD_IMAGE = "docker.io/guacamole/guacd:1.6.0"
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


def _stopped_container(which) -> str | None:
    """The runtime holding a guacd container from an earlier install_help, if there is one."""
    for runtime in ("podman", "docker"):
        if which(runtime):
            try:
                if subprocess.run([runtime, "container", "inspect", "guacd"], capture_output=True,
                                  timeout=5, env=host_env()).returncode == 0:
                    return runtime
            except (OSError, subprocess.SubprocessError):
                pass
    return None


def install_help(release: dict[str, str] | None = None, which=_which, installed: bool | None = None,
                 container=_stopped_container) -> tuple[str, list[str]]:
    """What to tell a technician whose guacd isn't answering: a sentence and the commands to
    run, for this distribution. guacd must listen on 127.0.0.1:4822 (its default)."""
    if installed is None:
        installed = any(os.path.exists(p) for p in GUACD_BINARIES)
    if installed:
        return ("guacd is installed but isn't running on 127.0.0.1:4822. Start it, and have it "
                "start at boot:", ["sudo systemctl enable --now guacd"])
    runtime = container(which)
    if runtime:
        return "guacd's container isn't running. Start it:", [f"{runtime} start guacd"]
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
    """Run upstream's guacd image, published on loopback only (guacd's link is unencrypted)."""
    run = f"run -d --name guacd --restart unless-stopped -p 127.0.0.1:4822:4822 {GUACD_IMAGE}"
    text = f"{why} Run Apache Guacamole's own guacd container, reachable only from this machine:"
    if which("podman") or not which("docker"):
        # rootless podman restarts --restart containers only through this user service
        cmds = [f"podman {run}", "systemctl --user enable podman-restart.service"]
        return text, ([install_podman] if install_podman and not which("podman") else []) + cmds
    return text, [f"docker {run}"]


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
