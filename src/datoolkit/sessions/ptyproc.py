"""A program running in a pseudo-terminal, started without forking the Python process.

ptyprocess (and the stdlib pty module) fork the interpreter itself, which is unsafe once
uvicorn and the executor have threads running: a lock held by another thread at fork time
stays held forever in the child. Here the child is started by subprocess, whose fork/exec
happens in C with no Python code in between, and util-linux `setsid --ctty` makes the new
session leader take the PTY as its controlling terminal (so Ctrl-C, job control and
SIGWINCH reach it) before it execs the program."""

from __future__ import annotations

import fcntl
import os
import signal
import struct
import subprocess
import termios


class PtyProcess:
    def __init__(self, popen: subprocess.Popen, fd: int):
        self.popen = popen
        self.fd = fd  # the master side: read the program's output, write its input
        self.pid = popen.pid

    @classmethod
    def spawn(cls, argv: list[str], env: dict[str, str], cwd: str,
              dimensions: tuple[int, int] = (24, 80)) -> "PtyProcess":
        master, slave = os.openpty()
        try:
            _set_size(slave, *dimensions)
            popen = subprocess.Popen(["setsid", "--ctty", *argv], stdin=slave, stdout=slave, stderr=slave,
                                     env=env, cwd=cwd, close_fds=True)
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        return cls(popen, master)

    @property
    def exitstatus(self) -> int | None:
        """The exit code, or None while running or when a signal ended it."""
        rc = self.popen.returncode
        return rc if rc is None or rc >= 0 else None

    def isalive(self) -> bool:
        """Reaps the child if it has exited. After the PTY reports EOF the child is usually
        gone or about to be, so this waits briefly for it."""
        try:
            self.popen.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return True
        return False

    def setwinsize(self, rows: int, cols: int) -> None:
        _set_size(self.fd, rows, cols)

    def terminate(self) -> None:
        """Hang up the session like closing a terminal window, then kill whatever ignores it."""
        for sig, grace in ((signal.SIGHUP, 0.5), (signal.SIGKILL, 1)):
            if self.popen.poll() is not None:
                break
            try:
                os.killpg(self.pid, sig)  # the child is a session and process group leader
            except ProcessLookupError:
                self.popen.send_signal(sig)  # closed so soon that setsid hasn't run yet
            try:
                self.popen.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        self.close()

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def _set_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
