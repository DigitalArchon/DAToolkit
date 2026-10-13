"""WinRM hosts: how one may be connected to, and the console's settings.

The rule for model providers (llm/netguard.py) applies here too: plain HTTP only to this
machine and its local network, never further. Basic authentication over plain HTTP sends the
password in the clear, so it needs the technician's say-so on the host (and the local network)
as well; NTLM and Kerberos encrypt the exchange themselves. Over HTTPS the server's
certificate is checked against the system's CA store by pypsrp, or, when the technician turns
that off (a self-signed listener), by DA Toolkit itself: trust on first use and pinned, as for
RDP (engine.open_winrm). Nothing ever connects with the certificate unchecked."""

from __future__ import annotations

import json

from ..config import Host
from ..llm import netguard


def problem(host: Host) -> str | None:
    """Why this WinRM host may not be connected to as configured, or None."""
    if host.winrm_ssl:
        return None
    if not netguard.is_local(host.host):
        return (f"WinRM over plain HTTP is accepted only for this machine and its local network (localhost, "
                f"10.x, 172.16-31.x, 192.168.x, .local), and {host.host} is not on it. Use HTTPS (port 5986).")
    if host.auth == "basic" and not host.winrm_plain_basic:
        return ("Basic authentication over plain HTTP sends the password across the network in the clear. "
                "Tick \"Allow Basic over plain HTTP\" on the host if that is acceptable on this network, or "
                "use HTTPS, or NTLM/Kerberos, which encrypt the exchange themselves.")
    return None


def pins(host: Host) -> bool:
    """Whether DA Toolkit checks this host's certificate itself (trust on first use, pinned)."""
    return bool(host.winrm_ssl) and not host.winrm_cert_validation


def port(host: Host) -> int:
    return host.port or (5986 if host.winrm_ssl else 5985)


def console_config(host: Host) -> str:
    """DATOOLKIT_PSRP for sessions/psrp_console.py: connection settings, no secrets. A pinned
    certificate was checked by the engine before the console starts, so pypsrp's own check is
    off then; otherwise it checks against the system CA store."""
    return json.dumps({"host": host.host, "port": host.port, "user": host.user, "auth": host.auth,
                       "ssl": host.winrm_ssl, "cert_validation": host.winrm_cert_validation})
