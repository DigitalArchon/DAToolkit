"""No core dumps: a native crash must not write the process's memory to disk.

DA Toolkit holds what a technician gives it in memory: the passwords and keys of SSH, WinRM and
RDP sessions (answered through the askpass bridge, held by the WinRM console's own process), the
AI providers' keys, and every case's conversation. A core file of any of its processes would put
them on disk, readable by whoever reads the crash. So each entry point (the app, `python -m
datoolkit`, datoolkit-replay, the askpass helper ssh runs, the WinRM console) calls
`no_core_dumps` first:

- `RLIMIT_CORE` 0 stops a core file;
- not dumpable (`PR_SET_DUMPABLE` 0) stops the system's collector, which reads the flag rather
  than the limit (systemd-coredump, apport), and, as a side effect, another process attaching a
  debugger. A program the process starts is dumpable again after its exec (ssh, the shells).

Linux only, as the app is, and any failure is ignored: the app runs as before where the calls
aren't there. DA Toolkit keeps no faulthandler crash log of its own, so nothing else changes.
From SealedLore (gui/app.py `_no_core_dumps`, Nov 2026).
"""

from __future__ import annotations

import sys

PR_SET_DUMPABLE = 4


def no_core_dumps() -> None:
    """No core file and not dumpable, for this process (Linux; failures ignored)."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ImportError, ValueError, OSError):
        pass
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass
