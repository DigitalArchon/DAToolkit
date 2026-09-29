"""Build argv for the system OpenSSH client."""

from __future__ import annotations

from ..config import Host


def ssh_argv(host: Host) -> list[str]:
    argv = ["ssh", "-o", "ServerAliveInterval=30"]
    if host.port:
        argv += ["-p", str(host.port)]
    if host.user:
        argv += ["-l", host.user]
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
    argv.append(host.host)
    return argv


def target_label(host: Host) -> str:
    label = f"{host.user}@{host.host}" if host.user else host.host
    return f"{label}:{host.port}" if host.port else label
