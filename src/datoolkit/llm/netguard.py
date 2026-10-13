"""HTTPS, or nothing.

Every request to a model provider carries the API key, and most carry the case: the
conversation, command output, client data. So a base URL is used only if it is https, or if
it never leaves the local network, which is how a model server on this machine or the one
next to it is reached, and where there is usually no certificate to be had:

    localhost, *.localhost, *.local
    127.0.0.0/8, ::1
    10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16
    169.254.0.0/16, fe80::/10            (link-local)
    fc00::/7                             (unique local)

Anything else over plain http is refused, with the reason, before a byte is sent: not
rewritten to https, which might be another server altogether, and not allowed with a
warning. Certificates are always verified; nothing here or anywhere else switches that off.

A name is judged as written. It is not looked up to see where it points: an address that
resolves to this network today can resolve elsewhere tomorrow. Settings refuses such a URL,
and the engine checks again where each request leaves from (Engine._secure), so a config
file edited by hand sends nothing either. Ported from DA Assistant's netguard.py.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

LOCAL_NAMES = ("localhost",)
LOCAL_SUFFIXES = (".localhost", ".local")
LOCAL_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fe80::/10", "fc00::/7"))


class InsecureURL(ValueError):
    pass


def is_local(host: str | None) -> bool:
    """Whether `host`, as written, is on this machine or its own network."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in LOCAL_NAMES or host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return any(address.version == network.version and address in network for network in LOCAL_NETWORKS)


def problem(url: str | None) -> str | None:
    """Why `url` may not be used, or None when it may."""
    text = (url or "").strip()
    if not text:
        return "No address given."
    parsed = urlparse(text)
    scheme = parsed.scheme.lower()
    try:
        host = parsed.hostname
    except ValueError:
        host = None
    if scheme not in ("http", "https") or not host:
        return f"Not a web address: {text!r}. It must start with https://"
    if scheme == "https" or is_local(host):
        return None
    return (f"{text} is not https, so the API key and everything sent with it would cross the "
            f"network unencrypted. Use an https:// address. Plain http is accepted only for this "
            f"machine and its local network (localhost, 10.x, 172.16-31.x, 192.168.x, .local).")


def check(url: str | None) -> str:
    """Raise InsecureURL unless `url` may be used. Returns it stripped."""
    found = problem(url)
    if found:
        raise InsecureURL(found)
    return (url or "").strip()
