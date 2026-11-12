"""RDP server certificates: fetch one and pin it (trust on first use).

guacd 1.3 (Ubuntu/Mint's package) can only ignore certificate errors, so DAToolkit checks
the certificate itself before every connection: it starts an RDP connection far enough to
complete the TLS handshake (X.224 Connection Request with an RDP Negotiation Request for TLS
or CredSSP), reads the server certificate, and compares its SHA-256 fingerprint with the pin
saved the first time the technician accepted it. guacd then connects with ignore-cert. Every
connection means each one guacd makes: when the session is opened, and again whenever the page
connects to it (the first time, Reconnect, a fit-to-window reconnect, a reload). Until Nov 2026
only the opening was checked, and a Reconnect hours later sent the password unchecked.

Limit: the check and guacd's connection are separate TLS sessions, so an attacker able to
tell them apart could still sit in the middle of the second one. It stops the common case
(a changed or spoofed certificate) and says so plainly. guacd 1.6 and later check the pin on
their own connection, and need none of this."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import ssl
import struct
import unicodedata
import warnings
from datetime import datetime
from pathlib import Path

from cryptography import x509

from ..config import config_dir, private_dir

TIMEOUT = 10
# seconds the whole of a certificate may take to read again before a reconnect: guacd, which is
# waiting for its handshake to go on meanwhile, gives up after 15
RECHECK_TIMEOUT = 12
PROTOCOL_TLS, PROTOCOL_HYBRID = 0x1, 0x2
NAME_LIMIT = 200          # characters of a certificate's subject or issuer that are shown
# Unicode categories never shown from a certificate: controls, formatting marks, line and
# paragraph separators, surrogates, private use and unassigned
_NOT_SHOWN = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs", "Co", "Cn"})


class CertError(Exception):
    pass


def _connection_request(user: str = "") -> bytes:
    cookie = f"Cookie: mstshash={user[:9]}\r\n".encode() if user else b""
    neg = struct.pack("<BBHI", 0x01, 0x00, 8, PROTOCOL_TLS | PROTOCOL_HYBRID)
    x224 = bytes([6 + len(cookie) + len(neg), 0xE0, 0, 0, 0, 0, 0]) + cookie + neg
    return struct.pack(">BBH", 3, 0, 4 + len(x224)) + x224


def fingerprint(der: bytes) -> str:
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def thumbprint(der: bytes) -> str:
    """The certificate's SHA-1 as Windows shows it (certlm.msc, the Cert: drive, `winrm
    enumerate`), for the technician to compare by eye. Only ever shown: the pin is the SHA-256."""
    return hashlib.sha1(der, usedforsecurity=False).hexdigest().upper()


def _one_line(text: str, limit: int = NAME_LIMIT) -> str:
    """A name out of a certificate as it may be shown and stored: one line, with no control or
    formatting characters (line breaks, escape sequences, the marks that reorder text), cut to
    `limit`. Whoever made the certificate chose every character of it."""
    text = "".join(ch if unicodedata.category(ch) not in _NOT_SHOWN else " " for ch in text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def describe(der: bytes) -> dict:
    """What a certificate says of itself, for the technician to read beside its fingerprint.
    Subject and issuer are the server's own words: cleaned and cut here, so that no prompt, list
    or log gets them with a line break (one could draw a second "SHA-256:" line above the real
    one, as the prompt was laid out until Nov 2026)."""
    try:
        with warnings.catch_warnings():          # a name longer than the standard allows is still read
            warnings.simplefilter("ignore", UserWarning)
            c = x509.load_der_x509_certificate(der)
            return {"subject": _one_line(c.subject.rfc4514_string()), "issuer": _one_line(c.issuer.rfc4514_string()),
                    "not_after": c.not_valid_after_utc.date().isoformat(),
                    "self_signed": c.subject == c.issuer}
    except ValueError:
        return {"subject": "?", "issuer": "?", "not_after": "?", "self_signed": False}


async def fetch(host: str, port: int = 3389, user: str = "") -> dict:
    """{"tls": bool, "protocol": "nla" | "tls" | "rdp", "sha256": str, "subject", "issuer",
    "not_after", "self_signed"}. `protocol` is the security the server chose when offered TLS
    and CredSSP (guacd's names); tls False means it only offers legacy RDP security, so there
    is no certificate."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TIMEOUT)
    except (OSError, asyncio.TimeoutError) as e:
        raise CertError(f"cannot reach {host}:{port}: {e or 'timed out'}") from e
    try:
        writer.write(_connection_request(user))
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(4), TIMEOUT)
        if head[0] != 3:
            raise CertError(f"{host}:{port} did not answer like an RDP server")
        body = await asyncio.wait_for(reader.readexactly(struct.unpack(">H", head[2:4])[0] - 4), TIMEOUT)
        if len(body) < 7 or body[1] & 0xF0 != 0xD0:
            raise CertError(f"{host}:{port} refused the RDP connection request")
        neg = body[7:]
        legacy = {"tls": False, "protocol": "rdp", "sha256": "", "subject": "", "issuer": "", "not_after": "",
                  "self_signed": False}
        if len(neg) >= 8 and neg[0] == 0x03:
            code = struct.unpack("<I", neg[4:8])[0]
            if code in (2, 3):   # SSL_NOT_ALLOWED_BY_SERVER, SSL_CERT_NOT_ON_SERVER: legacy RDP security only
                return legacy
            raise CertError(f"{host}:{port} refused TLS negotiation (RDP failure code {code})")
        selected = struct.unpack("<I", neg[4:8])[0] if len(neg) >= 8 and neg[0] == 0x02 else 0
        if not selected & (PROTOCOL_TLS | PROTOCOL_HYBRID):
            return legacy
        protocol = "nla" if selected & PROTOCOL_HYBRID else "tls"
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE          # we verify by pin, not by CA
        with warnings.catch_warnings():          # old Windows servers only speak TLS 1.0
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")   # old Windows servers
        except ssl.SSLError:
            pass
        await asyncio.wait_for(writer.start_tls(ctx), TIMEOUT)
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        if not der:
            raise CertError(f"{host}:{port} sent no certificate")
        return {"tls": True, "protocol": protocol, "sha256": fingerprint(der), "sha1": thumbprint(der), **describe(der)}
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ssl.SSLError, ConnectionError) as e:
        raise CertError(f"TLS handshake with {host}:{port} failed: {e or type(e).__name__}") from e
    finally:
        writer.close()


async def fetch_tls(host: str, port: int) -> dict:
    """The certificate of a plain TLS server (a WinRM HTTPS listener on 5986), in the shape
    fetch() returns, verified by pin like an RDP server's rather than by CA."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TIMEOUT)
    except (OSError, asyncio.TimeoutError) as e:
        raise CertError(f"cannot reach {host}:{port}: {e or 'timed out'}") from e
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE          # verified by pin, not by CA
        await asyncio.wait_for(writer.start_tls(ctx), TIMEOUT)
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        if not der:
            raise CertError(f"{host}:{port} sent no certificate")
        return {"tls": True, "protocol": "tls", "sha256": fingerprint(der), "sha1": thumbprint(der), **describe(der)}
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ssl.SSLError, ConnectionError) as e:
        raise CertError(f"TLS handshake with {host}:{port} failed: {e or type(e).__name__}") from e
    finally:
        writer.close()


class PinStore:
    """host:port -> pinned certificate (RDP servers and WinRM listeners), in
    ~/.config/datoolkit/rdp_pins.json (0600)."""

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "rdp_pins.json"

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def get(self, host: str, port: int) -> dict | None:
        return self._load().get(f"{host.lower()}:{port}")

    def pin(self, host: str, port: int, cert: dict) -> None:
        data = self._load()
        data[f"{host.lower()}:{port}"] = {k: cert[k] for k in ("sha256", "subject", "issuer", "not_after")} | {
            "pinned": datetime.now().isoformat(timespec="seconds")}
        private_dir(self.path.parent)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def forget(self, host: str, port: int) -> bool:
        data = self._load()
        if data.pop(f"{host.lower()}:{port}", None) is None:
            return False
        self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        return True
