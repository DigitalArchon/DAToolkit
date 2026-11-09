"""Who and where a computer is: what a web search must never carry unasked.

A search leaves in the clear (through NanoGPT to the search provider), and redaction
(safety/redact.py) takes only secrets out of a query: an e-mail address, a MAC address or a private
IP address went through (`nginx 502 10.20.30.40 jsmith@acme-corp.com`). A query that names one, or
this computer's own names (the login and real name, the host name, the home folder), is shown to
the technician first whatever the search mode, and a research agent's is refused. The query is read
as the search provider reads it: without the characters no one can see (hidden.visible), and with
compatibility forms (fullwidth digits) as their plain ones.

After DA Private Coder's identity.py (Nov 2026), which took it from DA Vibe Manager's
safety/outside.py; plain words go through ("opnsense", "openvpn"), or no search would be possible."""

from __future__ import annotations

import ipaddress
import os
import pwd
import re
import socket
import unicodedata

from .hidden import visible

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MAC = re.compile(r"\b[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}\b", re.I)
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# logins too common to be told apart from a word in a query
COMMON_NAMES = {"user", "admin", "root", "guest", "home", "test", "linux", "ubuntu", "mint", "debian", "fedora",
                "desktop", "laptop", "computer", "pc", "localhost"}


def names() -> list[str]:
    """This computer's names: login, real name, host name, home folder."""
    out = []
    try:
        pw = pwd.getpwuid(os.getuid())
        out += [pw.pw_name, pw.pw_dir]
        out += [p for p in re.split(r"[\s,]+", pw.pw_gecos or "") if len(p) >= 3]
    except KeyError:
        pass
    try:
        out += [socket.gethostname()]
    except OSError:
        pass
    return [x for x in dict.fromkeys(out) if x and len(x) >= 3 and x.lower() not in COMMON_NAMES]


def _private_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return not ip.is_global


def check(query: str, known: list[str] | None = None) -> list[str]:
    """The parts of `query` that say who or where a computer is (empty: none does). `known`: the names
    to look for, this computer's by default."""
    found: list[str] = []
    shown = visible(query)
    for text in dict.fromkeys((shown, unicodedata.normalize("NFKC", shown))):
        low = text.lower()
        for name in names() if known is None else known:
            if re.search(rf"(?<![\w]){re.escape(name.lower())}(?![\w])", low):
                found.append(name)
        found += _EMAIL.findall(text) + _MAC.findall(text)
        found += [ip for ip in _IPV4.findall(text) if _private_ip(ip)]
    return list(dict.fromkeys(found))
