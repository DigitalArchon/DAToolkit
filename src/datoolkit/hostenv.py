"""The environment for programs DAToolkit starts on the technician's behalf (their shell, ssh,
the file manager). When DAToolkit runs from an AppImage, the AppImage runtime adds variables
describing its own mount; they mean nothing to those programs, and another AppImage started
from a DAToolkit terminal would misread them, so they are left out."""

from __future__ import annotations

import os

APPIMAGE_VARS = ("APPIMAGE", "APPDIR", "ARGV0", "OWD", "APPIMAGE_EXTRACT_AND_RUN")
# variables DAToolkit changed for itself, with their values before (None: they weren't set)
ORIGINAL: dict[str, str | None] = {}


def set_for_self(name: str, value: str) -> None:
    """Set an environment variable for DAToolkit's own process only: host_env() gives children
    the value it had before."""
    ORIGINAL.setdefault(name, os.environ.get(name))
    os.environ[name] = value


def host_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in APPIMAGE_VARS}
    for name, value in ORIGINAL.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    env.update(extra)
    return env
