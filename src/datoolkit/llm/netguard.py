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

And it is judged whole, as httpx will send it: `http://127.0.0.1%2e.evil.com/v1` is not
127.0.0.1 but a name, one DNS is asked for whole (a name under evil.com), so it is not local.
Only an IPv6 address has a "%" of its own, its zone ("fe80::1%eth0", %25 in a URL), and a
.local or .localhost name must be a plain DNS name for the same reason. (From SealedLore's
models/config.py and DA Assistant's netguard.py, Nov 2026.)
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

LOCAL_NAMES = ("localhost",)
LOCAL_SUFFIXES = (".localhost", ".local")
LOCAL_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "fe80::/10", "fc00::/7"))
# a name a resolver reads as written: "%", "/" and the like in a name could reach DNS as another
_DNS_NAME = re.compile(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*")
# what no address has in it, and Python's parser and httpx's may not agree on (DA Assistant's)
UNSAFE = re.compile(r"[\\\s\x00-\x1f\x7f]")
# Everything of a text up to its last "@" before a query or a fragment, the scheme and "//"
# aside: where an address that can't be read may have a user name and password.
_UP_TO_AT = re.compile(r"^((?:[^:/?#]+:)?//)?[^?#]*@")
# An address anywhere in a text (an error quoting one), to the next space or quote
_ADDRESS_IN_TEXT = re.compile(r"[a-z][a-z0-9+.-]*://[^\s'\"<>]+", re.I)


class InsecureURL(ValueError):
    pass


def is_dns_name(host: str | None) -> bool:
    """Whether `host` is a plain DNS name, which a resolver reads as written."""
    return bool(_DNS_NAME.fullmatch((host or "").strip().lower().rstrip(".")))


def is_local(host: str | None) -> bool:
    """Whether `host`, as written, is on this machine or its own network. Read whole: an IPv6
    zone ("fe80::1%eth0") is part of its address, and anything else after a "%"
    ("127.0.0.1%2e.evil.com") makes it a name, which DNS would resolve wherever it likes. It was
    cut at the "%", so that name passed for 127.0.0.1."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in LOCAL_NAMES or host.endswith(LOCAL_SUFFIXES):
        return is_dns_name(host)
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return any(address.version == network.version and address in network for network in LOCAL_NETWORKS)


def problem(url: str | None) -> str | None:
    """Why `url` may not be used, or None when it may. The address is named as shown() has it,
    without a password written into it: these are shown in Settings and the chat, and logged."""
    text = (url or "").strip()
    if not text:
        return "No address given."
    if UNSAFE.search(text):
        return (f"Not a web address: {shown(text)!r}. An address has no spaces, backslashes or control "
                f"characters in it.")
    try:
        parsed = urlparse(text)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        parsed.port                 # raises on a port no address can have
    except ValueError:              # "http://[fd00::5", half typed
        return f"Not a web address: {shown(text)!r}."
    if scheme not in ("http", "https") or not host:
        return f"Not a web address: {shown(text)!r}. It must start with https://"
    if scheme == "https" or is_local(host):
        return None
    return (f"{shown(text)} is not https, so the API key and everything sent with it would cross the "
            f"network unencrypted. Use an https:// address. Plain http is accepted only for this "
            f"machine and its local network (localhost, 10.x, 172.16-31.x, 192.168.x, .local).")


def shown(url) -> str:
    """`url` as it may be shown or logged: without the user name and password an address may
    carry (https://alice:secret@host/v1). For any text at all, and never raising: what is said of
    an address that can't be used names it too. One that can't be read as an address (half
    typed, without a scheme, with a port no address can have) says nothing certain of where its
    host begins, so all it has up to its last "@" goes. (DA Assistant's netguard.shown.)"""
    text = str(url or "")
    try:
        parsed = urlparse(text)
        parsed.port                 # raises on a port that is not a number
    except ValueError:
        parsed = None
    if parsed is None or not parsed.netloc:
        return _UP_TO_AT.sub(r"\1…@", text, count=1)
    if "@" not in parsed.netloc:
        return text
    return parsed._replace(netloc="…@" + parsed.netloc.rsplit("@", 1)[1]).geturl()


def without_password(url) -> str:
    """`url` with the user name and password written into it taken out, as shown() does, but
    leaving an address where they were: what config.toml keeps of an address whose password is
    in the keyring. Text with neither is returned as it is. (DA Assistant's.)"""
    text = str(url or "")
    if "@" not in text:
        return text
    try:
        parsed = urlparse(text)
        parsed.port                 # raises on a port that is not a number
    except ValueError:
        parsed = None
    if parsed is None or not parsed.netloc:
        return _UP_TO_AT.sub(r"\1", text, count=1)
    if "@" not in parsed.netloc:
        return text
    return parsed._replace(netloc=parsed.netloc.rsplit("@", 1)[1]).geturl()


def scrubbed(text) -> str:
    """`text` with every address in it as shown() has it, without a user name and password: for
    an error that quotes an address it was given (httpx's say "for url '...'", password and all)."""
    return _ADDRESS_IN_TEXT.sub(lambda found: shown(found.group(0)), str(text or ""))


def key_problem(key, name: str = "the provider") -> str | None:
    """Why `key` can't be sent as an API key, or None. A key is plain ASCII with no spaces in it;
    one pasted with a line break, or a zero-width space from a web page, was refused by the HTTP
    library in words that quoted it whole (h11: "Illegal header value b'Bearer sk-…'"), and those
    were shown in the chat and logged. Says what is wrong and nothing of the key.
    (DA Assistant's netguard.key_problem, Nov 2026.)"""
    if key and (not key.isascii() or UNSAFE.search(key) or " " in key):
        return (f"The API key for {name} has a space, a line break or another character no API key has "
                f"in it, picked up where it was copied from, perhaps. Enter it again in Settings → Providers.")
    return None


def check(url: str | None) -> str:
    """Raise InsecureURL unless `url` may be used. Returns it stripped."""
    found = problem(url)
    if found:
        raise InsecureURL(found)
    return (url or "").strip()
