"""Build argv for the system OpenSSH client."""

from __future__ import annotations

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


def target_label(host: Host) -> str:
    label = f"{host.user}@{host.host}" if host.user else host.host
    return f"{label}:{host.port}" if host.port else label
