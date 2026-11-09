"""The phone companion's HTTPS listener, its certificate, and the phones paired with it.

The companion is served on its own port (Settings → General, fixed so a firewall can open
just that one) over TLS with a self-signed certificate that is made once per install and
kept, so its fingerprint stays the same. Pairing takes two scans so that nothing secret
crosses the network before the technician has checked that fingerprint:

1. the "trust" code opens /pair, which holds no secret. The phone's browser warns about the
   certificate; the technician accepts it, opens the certificate details and compares its
   SHA-256 fingerprint with the one this app shows;
2. the "connect" code carries a pairing code, good once and for five minutes. The browser has
   already accepted this exact certificate, so a different one (someone in the middle) brings
   the warning back. The phone's page claims the code; the desktop then asks "Allow this
   phone?", showing four digits the phone shows too, so a claim made from somewhere else with
   a photographed code is told apart. Allowed, the phone is given a token of its own, once;
   only its hash is kept. Each phone can be disconnected by itself.

Android Chrome shows the fingerprint only after proceeding past the warning, which is why
nothing that pairs is part of the first code. Until Nov 2026 the second code carried one token
every phone shared, and scanning it was all it took (SealedLore's phone/pairing.py is the model
for what replaced it)."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import errno
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
import ssl
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import segno
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config import private_dir
from .engine import UserError

CERT_FILE = "companion-cert.pem"
KEY_FILE = "companion-key.pem"
# Apple's limit for server certificates; a new one is made when it runs out (the fingerprint changes)
CERT_DAYS = 825
RENEW_BEFORE = dt.timedelta(days=7)
# a few phones use a handful of connections each; more is a flood, answered with 503 so it
# can't grow without bound on the event loop the GUI shares
MAX_CONNECTIONS = 64
# the phone sends nothing over its WebSocket (it only listens)
WS_MAX_SIZE = 64 * 1024


# ---------------------------------------------------------------- pairing (SealedLore's phone/pairing.py)

CODE_SECONDS = 5 * 60  # a pairing code lives this long
CLAIM_SECONDS = 5 * 60  # a claim waits this long for the desktop's answer
CODE_FAILURES = 5  # wrong codes before the code is spent anyway
CLAIMS_PER_MINUTE = 10  # all claims together, right or wrong
NAME_LENGTH = 40


class PairingRefused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class Claim:
    id: str
    name: str
    check: str  # the four digits both screens show
    made_at: float
    state: str = "waiting"  # waiting, approved, refused, expired
    token: str = ""  # given once, on approval


@dataclass
class Device:
    """A phone the technician allowed: its token's hash, never the token."""
    id: str
    name: str
    token_hash: str
    paired_at: str


def new_code() -> str:
    """128 bits, as 26 lower-case base32 letters and digits."""
    return base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def device_for(token: str | None, devices: Iterable[tuple[str, str]]) -> str | None:
    """The device whose token this is, from (device id, token hash) pairs. Every hash is
    compared, in constant time."""
    if not token:
        return None
    digest = token_hash(token)
    found = None
    for device_id, stored in devices:
        if hmac.compare_digest(digest, stored):
            found = device_id
    return found


def clean_name(name: object) -> str:
    """A phone's name as the desktop shows it: printable, one line, short."""
    text = "".join(" " if ch.isspace() else ch for ch in str(name or "") if ch.isprintable() or ch.isspace())
    return " ".join(text.split())[:NAME_LENGTH] or "A phone"


class Pairing:
    """The pairing code's state: at most one live code, and the claims made with it. Clock
    injected for tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._code: str | None = None
        self._code_made = 0.0
        self._failures = 0
        self._claims: dict[str, Claim] = {}
        self._recent: deque[float] = deque()

    # --- the desktop's side ------------------------------------------------

    def open(self) -> str:
        """A new code; any earlier one is spent."""
        self._code = new_code()
        self._code_made = self._clock()
        self._failures = 0
        return self._code

    def close(self) -> None:
        """The code is spent and unanswered claims expire."""
        self._code = None
        for claim in self._claims.values():
            if claim.state == "waiting":
                claim.state = "expired"

    @property
    def code(self) -> str | None:
        self._expire()
        return self._code

    def waiting(self) -> list[Claim]:
        self._expire()
        return [c for c in self._claims.values() if c.state == "waiting"]

    def answer(self, claim_id: str, allow: bool) -> str | None:
        """The desktop's answer. Allowed, the new token (to keep its hash); refused, or a claim
        that's gone, None."""
        self._expire()
        claim = self._claims.get(claim_id)
        if claim is None or claim.state != "waiting":
            return None
        if not allow:
            claim.state = "refused"
            return None
        claim.state = "approved"
        claim.token = new_token()
        return claim.token

    # --- the phone's side --------------------------------------------------

    def claim(self, code: object, name: object) -> Claim:
        self._expire()
        now = self._clock()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= CLAIMS_PER_MINUTE:
            raise PairingRefused(429, "Too many tries. Wait a minute and scan the code again.")
        self._recent.append(now)
        if self._code is None:
            raise PairingRefused(410, "Pairing isn't open on the computer. Show the connect code there and scan it again.")
        # As bytes: compare_digest refuses a str with anything beyond ASCII in it (TypeError, a 500,
        # and the try uncounted); surrogatepass, so a lone surrogate is a wrong try too
        if not isinstance(code, str) or not hmac.compare_digest(
                code.strip().lower().encode("utf-8", "surrogatepass"), self._code.encode("ascii")):
            self._failures += 1
            if self._failures >= CODE_FAILURES:
                self._code = None
            raise PairingRefused(403, "That code isn't the one on the computer's screen.")
        self._code = None  # spent, whatever the desktop answers
        claim = Claim(id=secrets.token_urlsafe(16), name=clean_name(name), check=f"{secrets.randbelow(10000):04d}",
                      made_at=now)
        self._claims[claim.id] = claim
        return claim

    def poll(self, claim_id: str) -> tuple[str, str]:
        """(state, token): the token only once, on the first poll after approval; the claim is
        forgotten once it is answered and told."""
        self._expire()
        claim = self._claims.get(claim_id)
        if claim is None:
            return "expired", ""
        if claim.state == "waiting":
            return "waiting", ""
        del self._claims[claim_id]
        return claim.state, claim.token

    def _expire(self) -> None:
        now = self._clock()
        if self._code is not None and now - self._code_made > CODE_SECONDS:
            self._code = None
        for claim_id, claim in list(self._claims.items()):
            if now - claim.made_at > CLAIM_SECONDS:
                if claim.state == "waiting":
                    claim.state = "expired"
                # Long answered and never collected: nothing waits for it.
                if now - claim.made_at > 2 * CLAIM_SECONDS:
                    del self._claims[claim_id]


def lan_ip() -> str:
    """The address other devices on the LAN reach this machine at (the default route's)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


def fingerprint(cert_pem: bytes) -> str:
    """SHA-256 of the DER certificate as colon-separated hex pairs, as browsers show it."""
    der = x509.load_pem_x509_certificate(cert_pem).public_bytes(serialization.Encoding.DER)
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def _make_cert(cert_path: Path, key_path: Path, ip: str) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"DA Toolkit companion ({host})")])
    alt: list[x509.GeneralName] = [x509.DNSName(host)]
    try:
        alt.append(x509.IPAddress(ipaddress.ip_address(ip)))
    except ValueError:
        pass
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=CERT_DAYS))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    private_dir(cert_path.parent)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key_pem)
    os.chmod(key_path, 0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def ensure_cert(directory: Path, ip: str, renew: bool = False) -> tuple[Path, Path]:
    """The install's companion certificate and key, made on first use, when nearly expired,
    or when `renew` is asked for."""
    cert_path, key_path = directory / CERT_FILE, directory / KEY_FILE
    if not renew and cert_path.exists() and key_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            if cert.not_valid_after_utc - dt.datetime.now(dt.timezone.utc) > RENEW_BEFORE:
                return cert_path, key_path
        except ValueError:
            pass  # unreadable: make a new one
    _make_cert(cert_path, key_path, ip)
    return cert_path, key_path


def _qr(url: str) -> str:
    return segno.make(url, error="m").svg_data_uri(scale=6, border=2, dark="#000", light="#fff")


class CompanionServer:
    """Runs the companion app (server.app.create_app builds it) on the event loop it is started
    from, on 0.0.0.0:`port` over TLS. The phones paired with it are forgotten when it stops."""

    def __init__(self, app, directory: Path):
        self.app = app
        self.directory = directory
        self.port: int | None = None
        self.ip = ""
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self.devices: dict[str, Device] = {}      # the phones allowed: their tokens' hashes
        self.pairing = Pairing()
        self.sockets: dict = {}  # open companion WebSockets -> the device each belongs to

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def device_of(self, token: str | None) -> str | None:
        """The paired phone whose token this is, or None (compared by hash, in constant time)."""
        return device_for(token, ((d.id, d.token_hash) for d in self.devices.values()))

    def check(self, token: str | None) -> bool:
        return self.device_of(token) is not None

    def add_device(self, name: str, token: str | None = None) -> str:
        """A phone allowed: kept by its token's hash. Returns the token, for that phone alone
        (`token`: one chosen for it, as tests do)."""
        token = token or new_token()
        self._keep(name, token)
        return token

    def _keep(self, name: str, token: str) -> Device:
        device = Device(id=secrets.token_urlsafe(9), name=clean_name(name), token_hash=token_hash(token),
                        paired_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        self.devices[device.id] = device
        return device

    def open_code(self) -> str:
        """A new pairing code for the connect QR; the one before it is spent."""
        return self.pairing.open()

    def answer(self, claim_id: str, allow: bool) -> Device | None:
        """The technician's answer to a phone's claim. Allowed, the phone is kept (its token goes
        to it once, at its next poll); refused or gone, None."""
        claim = next((c for c in self.pairing.waiting() if c.id == claim_id), None)
        token = self.pairing.answer(claim_id, allow) if claim else None
        return self._keep(claim.name, token) if token else None

    async def revoke(self, device_id: str) -> None:
        """Disconnect one phone: its token stops working and its pages are closed."""
        self.devices.pop(device_id, None)
        await self._close_sockets(device_id)

    async def start(self, port: int) -> None:
        if self.running:
            await self.stop()
        self.ip = lan_ip()
        cert, key = ensure_cert(self.directory, self.ip)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError as e:
            sock.close()
            if e.errno == errno.EADDRINUSE:
                raise UserError(f"Port {port} is already in use, by another program or another DA Toolkit "
                                "with its phone companion running. Close that, or choose another port in "
                                "Settings → General.") from e
            raise UserError(f"Can't listen on port {port}: {e.strerror}. Choose another port in "
                            "Settings → General.") from e
        sock.listen(16)
        sock.setblocking(False)
        self.devices.clear()
        self.pairing = Pairing()
        self.port = port
        config = uvicorn.Config(self.app, log_level="warning", lifespan="off", ssl_certfile=str(cert),
                                ssl_keyfile=str(key), timeout_graceful_shutdown=2,
                                limit_concurrency=MAX_CONNECTIONS, ws_max_size=WS_MAX_SIZE)
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        for _ in range(100):
            if self._server.started or self._task.done():
                break
            await asyncio.sleep(0.02)
        if not self._server.started:
            await self.stop()
            raise UserError("The companion server didn't start.")

    async def stop(self) -> None:
        self.devices.clear()
        self.pairing.close()
        await self._close_sockets()
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, 5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - stopping must not fail
                self._task.cancel()
        self._server = self._task = None

    async def rotate(self) -> None:
        """A new pairing code (the one shown before is spent); phones paired already stay."""
        if self.running:
            self.open_code()

    async def renew_cert(self, port: int) -> None:
        ensure_cert(self.directory, lan_ip(), renew=True)
        if self.running:
            await self.start(port)

    async def _close_sockets(self, device_id: str | None = None) -> None:
        """Close the open pages of one phone, or of every phone."""
        for ws, owner in list(self.sockets.items()):
            if device_id is not None and owner != device_id:
                continue
            try:
                await ws.close(code=4403)
            except Exception:  # noqa: BLE001 - already gone
                pass
            self.sockets.pop(ws, None)

    def info(self, port: int) -> dict:
        """What the pairing dialog shows. The fingerprint is shown even while stopped, so it can
        be written down or compared at leisure."""
        cert_path = self.directory / CERT_FILE
        out: dict = {"running": self.running, "port": self.port if self.running else port,
                     "phones": len(self.sockets), "fingerprint": None, "expires": None}
        if cert_path.exists():
            pem = cert_path.read_bytes()
            out["fingerprint"] = fingerprint(pem)
            out["expires"] = x509.load_pem_x509_certificate(pem).not_valid_after_utc.date().isoformat()
        if self.running:
            base = f"https://{self.ip}:{self.port}"
            connected = set(self.sockets.values())
            out.update(ip=self.ip, pair_url=f"{base}/pair", pair_qr=_qr(f"{base}/pair"),
                       devices=[{"id": d.id, "name": d.name, "paired_at": d.paired_at, "connected": d.id in connected}
                                for d in self.devices.values()],
                       claims=[{"id": c.id, "name": c.name, "check": c.check} for c in self.pairing.waiting()])
            code = self.pairing.code
            if code:      # the connect code: good once, for CODE_SECONDS
                out.update(connect_url=f"{base}/companion#pair={code}", connect_qr=_qr(f"{base}/companion#pair={code}"))
        return out


def client_context(cert_path: Path) -> ssl.SSLContext:
    """A TLS context that trusts exactly this certificate (tests and diagnostics)."""
    ctx = ssl.create_default_context(cafile=str(cert_path))
    ctx.check_hostname = False
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # a self-signed leaf is its own trust anchor
    return ctx
