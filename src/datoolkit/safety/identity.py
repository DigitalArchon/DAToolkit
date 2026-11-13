"""Who and where a computer is: what a web search must never carry unasked.

A search leaves in the clear (through NanoGPT to the search provider), and redaction
(safety/redact.py) takes only secrets out of a query: an e-mail address, a MAC address or a private
IP address went through (`nginx 502 10.20.30.40 jsmith@acme-corp.com`). A query that names one, or
a UNC path's server, or one of the names it is given (this computer's own: the login and real name,
the host name, the home folder; a case's: its client, each session's host, domain and user), is
shown to the technician first whatever the search mode, and a research agent's is refused. The
query is read as the search provider reads it: without the characters no one can see
(hidden.visible), and with compatibility forms (fullwidth digits) as their plain ones.

A name is looked for as it may be written, not only as it was given: a host by its short name when
its full one is known (dc01 for dc01.acme-corp.local) and the reverse, a name with _ - . or a space
between its parts or none (ACME-WS01, acme_ws01, acme ws01), an account by its user and its domain
(ACME\\bob, bob@acme.local). Until Nov 2026 only the exact name was, IPv6 addresses weren't looked
for at all, nor a MAC address as Cisco and HP write it, nor an IPv4 address with leading zeros.

After DA Private Coder's identity.py (Nov 2026), which took it from DA Vibe Manager's
safety/outside.py; plain words go through ("opnsense", "openvpn"), or no search would be possible."""

from __future__ import annotations

import ipaddress
import os
import pwd
import re
import socket
import unicodedata
from functools import lru_cache

from .hidden import visible

_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]++@[\w-]+(?:\.[\w-]+)+")
# a MAC address as most write it, as Cisco does (001a.2b3c.4d5e) and as HP does (001a2b-3c4d5e); the
# last two only with a letter in them: twelve digits in groups are a serial number as often
_MAC = re.compile(r"\b[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}\b"
                  r"|\b(?=[0-9.-]{0,13}[a-f])(?:[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}|[0-9a-f]{6}-[0-9a-f]{6})\b", re.I)
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
# what may be an IPv6 address: ipaddress says whether it is one
_IPV6 = re.compile(r"(?<![\w:.])(?:[0-9a-f]{0,4}+:){2,7}+[0-9a-f.]{0,15}+(?![\w:])", re.I)
_UNC = re.compile(r"(?<![\\\w])\\\\([\w.-]++)(?=\\[^\\\s])")
# logins, real-name words and host names too common to be told apart from a word in a query: alone,
# none of them names anyone, and a search for "windows server backup service" must not wait for a yes
COMMON_NAMES = frozenset((
    "user", "users", "admin", "administrator", "root", "guest", "home", "test", "linux", "ubuntu", "mint", "debian",
    "fedora", "desktop", "laptop", "computer", "pc", "localhost", "support", "tech", "technician", "office", "service",
    "services", "server", "servers", "client", "clients", "windows", "backup", "backups", "helpdesk", "help", "info",
    "mail", "email", "sales", "owner", "manager", "operator", "staff", "student", "teacher", "reception", "public",
    "private", "shared", "share", "default", "system", "local", "domain", "work", "workstation", "host", "hostname",
    "network", "internet", "printer", "scanner", "kiosk", "demo", "temp", "family", "digital", "data", "database",
    "web", "www", "ftp", "smtp", "imap", "vpn", "dns", "dhcp", "nas", "gateway", "router", "firewall", "switch", "proxy",
    "cloud", "remote", "portal", "intranet", "corp", "company", "lan", "wan", "internal", "dev", "prod", "production",
    "staging", "app", "api", "main", "master", "node", "new", "old", "the", "and", "com", "net", "org", "servername",
    "computername", "username", "fileserver", "example", "security", "account", "accounts", "error", "login", "name"))
# a UNC path's server that is none: a placeholder, a device path (\\.\pipe, \\?\C:), WSL's
_NO_SERVER = COMMON_NAMES | {".", "?", "wsl$", "wsl.localhost", "tsclient", "127.0.0.1"}
_PARTS = re.compile(r"[\s_./\\-]+")


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
    return [x for x in dict.fromkeys(out) if _identifying(x)]


def known_names(*given: object, own: bool = True) -> list[str]:
    """The names to look for in what leaves, from what a case holds: its client, each open session's
    host, domain and user, each as it is stored (dc01.acme.local, 10.0.0.5:22, ACME\\bob, bob@acme.local,
    None, ""). What isn't text, or is empty, is passed over, and a name comes once; `own` adds this
    computer's names (names()), which every search is checked against. check() reads each name in the
    forms it may be written."""
    out = list(names()) if own else []
    for name in given:
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return list(dict.fromkeys(out))


def _identifying(name: str) -> bool:
    """Whether a name tells one computer, person or organisation from another: three characters or more,
    not a number, and not made of common words alone ("support", "office-pc", "Tech Support", /home/user)."""
    words = [w for w in _PARTS.split(name.lower()) if w]
    return len(name) >= 3 and not name.isdigit() and any(w not in COMMON_NAMES and not w.isdigit() for w in words)


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """`text` as an IP address, an IPv4 one with leading zeros too (010.000.000.001), which ipaddress
    refuses and a search provider reads."""
    if ":" not in text:
        octets = text.split(".")
        if len(octets) == 4 and all(o.isascii() and o.isdigit() and len(o) <= 3 for o in octets):
            text = ".".join(str(int(o)) for o in octets)
    try:
        return ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return None


def _private_ip(text: str) -> bool:
    ip = _address(text)
    return ip is not None and not ip.is_global and not ip.is_unspecified


@lru_cache(maxsize=512)
def _forms(name: str) -> tuple[tuple[str, re.Pattern], ...]:
    """What to look for, for one name given, each with the pattern that finds it: the name itself first,
    then the parts that name its owner alone. \\\\server\\share: the server; DOMAIN\\user and
    user@domain: each of the two; a host or domain name with dots: its first label too (dc01,
    acme-corp), unless that is a common word (mail.acme.com). A session's address is read without its
    scheme and port (ssh://dc01.acme.local:22)."""
    name = unicodedata.normalize("NFKC", visible(name)).strip().strip("\"'")
    name = re.sub(r"^[a-z][a-z0-9+.-]*://", "", name, flags=re.I)
    if name.startswith("\\\\"):
        name = name[2:].split("\\")[0]
    if (bracketed := re.fullmatch(r"\[([0-9A-Fa-f:.%\w]+)\](?::\d+)?", name)):
        name = bracketed[1]
    elif name.count(":") == 1 and re.search(r":\d{1,5}$", name):
        name = name.rsplit(":", 1)[0]
    forms = [name]
    if "\\" in name and ":" not in name and not name.startswith(("/", "~")):
        forms += name.split("\\", 1)
    elif "@" in name:
        forms += name.rsplit("@", 1)
    for form in forms[:]:
        labels = form.split(".")
        if len(labels) > 1 and "/" not in form and "\\" not in form and _address(form) is None:
            forms.append(labels[0])
    found = []
    for form in dict.fromkeys(forms):
        if _identifying(form):
            # with _ - . or a space between its parts, or nothing; not inside a longer word or number
            # (an underscore before or after is no part of it: backup_jsmith.zip)
            parts = [re.escape(p) for p in re.split(r"[\s_-]+", form.lower()) if p]
            found.append((form, re.compile(r"(?<![^\W_])" + r"[\s_.\-]{0,3}".join(parts) + r"(?![^\W_])")))
    return tuple(found)


def check(query: str, known: list[str] | None = None) -> list[str]:
    """The parts of `query` that say who or where a computer is (empty: none does). `known`: the names
    to look for, this computer's by default (known_names() builds the list for a case); one that is empty,
    shorter than three characters or a common word names no one and is passed over. A name found is
    told as it is known (ACME-WS01 for "acme_ws01"), or by the part of it the query holds (dc01 for
    dc01.acme-corp.local, jsmith for ACME\\jsmith)."""
    found: list[str] = []
    parts: set[str] = set()
    shown = visible(query)
    given = names() if known is None else [n for n in dict.fromkeys(known) if isinstance(n, str) and n.strip()]
    for text in dict.fromkeys((shown, unicodedata.normalize("NFKC", shown))):
        low = text.lower()
        for name in given:
            forms = [form for form, pattern in _forms(name) if pattern.search(low)]
            if forms and forms[0] == _forms(name)[0][0]:
                found.append(forms[0])                       # the name whole
            else:
                found += forms                               # or the parts of it that are there
                parts.update(forms)
        found += _EMAIL.findall(text) + _MAC.findall(text)
        found += [ip for ip in _IPV4.findall(text) + _IPV6.findall(text) if _private_ip(ip)]
        found += ["\\\\" + server for server in _UNC.findall(text) if server.lower() not in _NO_SERVER]
    # a part isn't told beside a name that holds it: not "acme-corp" beside "acme-corp.local"
    return [n for n in dict.fromkeys(found)
            if not (n in parts and any(n != other and n.lower() in other.lower() for other in found))]
