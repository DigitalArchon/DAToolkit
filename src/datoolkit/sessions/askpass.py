"""Credential bridge between session programs and the app.

`ssh` (via SSH_ASKPASS) and the WinRM console run this module as a helper. It forwards the
prompt over a private Unix socket; the app answers from the keyring or asks the technician.
Secrets never appear in argv, the environment, or the terminal.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import shlex
import socket
import sys
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

# (session_id, prompt, kind) -> answer or None to refuse
Handler = Callable[[str, str, str], Awaitable[str | None]]


class AskpassBridge:
    def __init__(self, runtime_dir: Path, handler: Handler):
        self.runtime_dir = runtime_dir
        self.handler = handler
        self.token = secrets.token_hex(24)
        self.sock_path = runtime_dir / "askpass.sock"
        self.script_path = runtime_dir / "askpass"
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if len(str(self.sock_path)) > 100:  # AF_UNIX paths are limited to ~108 bytes
            self.runtime_dir = Path(tempfile.mkdtemp(prefix="datoolkit-"))
            self.sock_path = self.runtime_dir / "askpass.sock"
            self.script_path = self.runtime_dir / "askpass"
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.runtime_dir, 0o700)
        src_root = Path(__file__).resolve().parents[2]
        self.script_path.write_text(
            "#!/bin/sh\n"
            f"PYTHONPATH={shlex.quote(str(src_root))}${{PYTHONPATH:+:$PYTHONPATH}} "
            f"exec {shlex.quote(sys.executable)} -m datoolkit.sessions.askpass \"$@\"\n"
        )
        os.chmod(self.script_path, 0o700)
        if self.sock_path.exists():
            self.sock_path.unlink()
        self._server = await asyncio.start_unix_server(self._serve, path=str(self.sock_path))
        os.chmod(self.sock_path, 0o600)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for p in (self.sock_path, self.script_path):
            p.unlink(missing_ok=True)
        try:
            self.runtime_dir.rmdir()
        except OSError:
            pass

    def env_for(self, session_id: str) -> dict[str, str]:
        return {
            "SSH_ASKPASS": str(self.script_path),
            "SSH_ASKPASS_REQUIRE": "force",
            "DATOOLKIT_SOCK": str(self.sock_path),
            "DATOOLKIT_TOKEN": self.token,
            "DATOOLKIT_SESSION": session_id,
        }

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            req = json.loads(line)
            if not hmac.compare_digest(str(req.get("token", "")), self.token):
                reply = {"ok": False}
            else:
                answer = await self.handler(str(req.get("session", "")), str(req.get("prompt", "")),
                                            str(req.get("kind", "askpass")))
                reply = {"ok": answer is not None, "answer": answer or ""}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except Exception:  # noqa: BLE001 - never let a bad client take down the app
            pass
        finally:
            writer.close()


def request(prompt: str, kind: str = "askpass") -> str | None:
    """Client side: ask the app for an answer. Returns None if refused."""
    path, token = os.environ.get("DATOOLKIT_SOCK"), os.environ.get("DATOOLKIT_TOKEN")
    if not path or not token:
        return None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(path)
        msg = {"token": token, "session": os.environ.get("DATOOLKIT_SESSION", ""), "prompt": prompt, "kind": kind}
        s.sendall((json.dumps(msg) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    reply = json.loads(buf or b"{}")
    return reply.get("answer") if reply.get("ok") else None


def main() -> None:
    answer = request(" ".join(sys.argv[1:]))
    if answer is None:
        sys.exit(1)
    sys.stdout.write(answer + "\n")


if __name__ == "__main__":
    main()
