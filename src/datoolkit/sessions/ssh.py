"""Build argv for the system OpenSSH client."""

from __future__ import annotations

import functools
import re
import subprocess

from ..config import Host


def ssh_argv(host: Host) -> list[str]:
    argv = ["ssh", "-o", "ServerAliveInterval=30"]
    if host.port:
        argv += ["-p", str(host.port)]
    if host.user:
        argv += ["-l", host.user]
    argv += ssh_options(host)
    argv += ["--", host.host]          # the address is never read as an option (Engine.save_host refuses those too)
    return argv


def ssh_options(host: Host) -> list[str]:
    """What the session's ssh gets besides its port and user, for the other OpenSSH tools to the
    same host (scp takes the port as -P and the user in its target): the key, how to log in,
    the jump host and the host's own -o options."""
    argv = []
    if host.auth == "key" and host.key_file:
        argv += ["-i", host.key_file, "-o", "IdentitiesOnly=yes"]
    elif host.auth == "password":
        argv += ["-o", "PreferredAuthentications=keyboard-interactive,password",
                 "-o", "PubkeyAuthentication=no"]
    if host.jump:
        argv += ["-J", host.jump]
    for opt in host.ssh_options:
        opt = opt.strip()
        if opt:
            argv += ["-o", opt]
    return argv


def resolved_hostname(host: Host) -> str:
    """The address ssh connects to for this host after ~/.ssh/config (an alias's HostName),
    lower-cased, or "" when ssh can't say: `ssh -G` prints the settings it would use and
    connects to nothing. A password prompt names this address, not the alias."""
    argv = (["ssh", "-G"] + (["-p", str(host.port)] if host.port else []) + (["-l", host.user] if host.user else [])
            + ssh_options(host) + ["--", host.host])
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if key == "hostname":
            return value.strip().lower().rstrip(".")
    return ""


def jump_hosts(jump: str) -> set[str]:
    """The addresses of a host's jump hosts ("me@bastion:2222,[fd00::1]:22"), lower-cased, as a
    credential prompt of theirs would name them."""
    out = set()
    for hop in jump.split(","):
        hop = hop.strip().removeprefix("ssh://").rsplit("@", 1)[-1]
        if hop.startswith("["):
            hop = hop[1:].split("]", 1)[0]
        elif hop.count(":") == 1:
            hop = hop.split(":", 1)[0]
        if hop:
            out.add(hop.lower().rstrip("."))
    return out


@functools.lru_cache(maxsize=1)
def marks_server_text() -> bool:
    """Whether this computer's ssh says which server wrote a keyboard-interactive prompt:
    OpenSSH 8.4 and later put "(user@host) " before the server's text. An older one hands the
    text over bare, so a jump host could write a prompt that reads as ssh's own for the target.
    True where ssh can't be run at all: then nothing prompts."""
    try:
        done = subprocess.run(["ssh", "-V"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True
    version = re.search(r"OpenSSH_(\d+)\.(\d+)", done.stderr + done.stdout)
    return bool(version) and (int(version.group(1)), int(version.group(2))) >= (8, 4)


def target_label(host: Host) -> str:
    label = f"{host.user}@{host.host}" if host.user else host.host
    return f"{label}:{host.port}" if host.port else label
