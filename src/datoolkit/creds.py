"""Secret storage in the OS keyring (Secret Service on Linux)."""

from __future__ import annotations

import keyring
from keyring.errors import PasswordDeleteError

SERVICE = "datoolkit"


def backend_error() -> str | None:
    """Return a human-readable problem if no usable keyring backend exists."""
    try:
        backend = keyring.get_keyring()
    except Exception as e:  # noqa: BLE001 - any backend failure is reportable
        return f"Keyring unavailable: {e}"
    if getattr(backend, "priority", 0) < 1:
        return (
            f"No secure keyring backend available ({type(backend).__name__}). "
            "Install and unlock a Secret Service provider such as gnome-keyring or KWallet."
        )
    return None


def _user(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def set_secret(kind: str, name: str, value: str) -> None:
    keyring.set_password(SERVICE, _user(kind, name), value)


def get_secret(kind: str, name: str) -> str | None:
    return keyring.get_password(SERVICE, _user(kind, name))


def delete_secret(kind: str, name: str) -> None:
    try:
        keyring.delete_password(SERVICE, _user(kind, name))
    except PasswordDeleteError:
        pass


def has_secret(kind: str, name: str) -> bool:
    return get_secret(kind, name) is not None
