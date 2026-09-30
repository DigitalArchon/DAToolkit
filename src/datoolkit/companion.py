"""The phone companion's HTTPS listener, its certificate and its pairing codes.

The companion is served on its own port (Settings → General, fixed so a firewall can open
just that one) over TLS with a self-signed certificate that is made once per install and
kept, so its fingerprint stays the same. Pairing takes two scans so that nothing secret
crosses the network before the technician has checked that fingerprint:

1. the "trust" code opens /pair, which holds no secret. The phone's browser warns about the
   certificate; the technician accepts it, opens the certificate details and compares its
   SHA-256 fingerprint with the one this app shows;
2. the "connect" code carries the token. The browser has already accepted this exact
   certificate, so a different one (someone in the middle) brings the warning back.

Android Chrome shows the fingerprint only after proceeding past the warning, which is why
the token is never part of the first code."""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
import ssl
from pathlib import Path

import segno
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .engine import UserError

CERT_FILE = "companion-cert.pem"
KEY_FILE = "companion-key.pem"
# Apple's limit for server certificates; a new one is made when it runs out (the fingerprint changes)
CERT_DAYS = 825
RENEW_BEFORE = dt.timedelta(days=7)


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
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"DAToolkit companion ({host})")])
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
    cert_path.parent.mkdir(parents=True, exist_ok=True)
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
    from, on 0.0.0.0:`port` over TLS. The token changes on every start and on rotate()."""

    def __init__(self, app, directory: Path):
        self.app = app
        self.directory = directory
        self.token: str | None = None
        self.port: int | None = None
        self.ip = ""
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None
        self.sockets: set = set()  # open companion WebSockets, closed when the token changes

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def check(self, token: str | None) -> bool:
        return bool(self.token) and token is not None and hmac.compare_digest(token, self.token)

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
            raise UserError(f"Can't listen on port {port}: {e.strerror}. Choose another port in "
                            "Settings → General.") from e
        sock.listen(16)
        sock.setblocking(False)
        self.token, self.port = secrets.token_urlsafe(24), port
        config = uvicorn.Config(self.app, log_level="warning", lifespan="off", ssl_certfile=str(cert),
                                ssl_keyfile=str(key), timeout_graceful_shutdown=2)
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
        self.token = None
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
        """A new token: phones paired with the old one are disconnected and must scan again."""
        if self.running:
            self.token = secrets.token_urlsafe(24)
            await self._close_sockets()

    async def renew_cert(self, port: int) -> None:
        ensure_cert(self.directory, lan_ip(), renew=True)
        if self.running:
            await self.start(port)

    async def _close_sockets(self) -> None:
        for ws in list(self.sockets):
            try:
                await ws.close(code=4403)
            except Exception:  # noqa: BLE001 - already gone
                pass
        self.sockets.clear()

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
            out.update(ip=self.ip, pair_url=f"{base}/pair", pair_qr=_qr(f"{base}/pair"),
                       connect_url=f"{base}/companion#t={self.token}",
                       connect_qr=_qr(f"{base}/companion#t={self.token}"))
        return out


def client_context(cert_path: Path) -> ssl.SSLContext:
    """A TLS context that trusts exactly this certificate (tests and diagnostics)."""
    ctx = ssl.create_default_context(cafile=str(cert_path))
    ctx.check_hostname = False
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # a self-signed leaf is its own trust anchor
    return ctx
