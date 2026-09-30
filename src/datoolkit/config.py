"""Persistent configuration. Never holds secrets - those live in the OS keyring (see creds.py)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import tomli_w

NANOGPT_BASE_URL = "https://nano-gpt.com/api/v1"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "datoolkit"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "datoolkit"


@dataclass
class Provider:
    name: str
    base_url: str
    default_model: str = ""
    # model id -> "standard" | "tee" | "local"; overrides automatic detection
    tier_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class Host:
    name: str
    kind: str  # "ssh" | "winrm"
    host: str
    port: int | None = None
    user: str = ""
    # ssh: "agent" | "key" | "password"; winrm: "ntlm" | "kerberos" | "negotiate" | "basic"
    auth: str = "agent"
    key_file: str = ""
    jump: str = ""
    ssh_options: list[str] = field(default_factory=list)
    winrm_ssl: bool = True
    winrm_cert_validation: bool = True
    os_hint: str = ""


@dataclass
class Settings:
    capture_max_lines: int = 300
    capture_max_chars: int = 24000
    scrollback: int = 10000
    font_size: int = 13
    # warn in the top bar when the last request's prompt used more tokens than this
    context_warn_tokens: int = 100000
    # "provider|model" used for second opinions on disruptive commands; empty = the active model
    review_model: str = ""
    # AI web searches through NanoGPT: "ask" (approve each), "auto" (Open cases only), "off"
    search_mode: str = "ask"
    search_provider: str = "kagi"
    # name of the NanoGPT provider whose key pays for searches; empty = the first NanoGPT one
    search_via: str = ""


@dataclass
class Config:
    providers: list[Provider] = field(default_factory=list)
    hosts: list[Host] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    active_provider: str = ""
    active_model: str = ""
    recent_models: list[str] = field(default_factory=list)

    def provider(self, name: str) -> Provider | None:
        return next((p for p in self.providers if p.name == name), None)

    def host(self, name: str) -> Host | None:
        return next((h for h in self.hosts if h.name == name), None)

    def to_dict(self) -> dict:
        return _strip_none(asdict(self))


def _strip_none(obj):
    # TOML has no null
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(v) for v in obj]
    return obj


def _build(cls, data: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def from_dict(data: dict) -> Config:
    cfg = _build(Config, {k: v for k, v in data.items() if k not in ("providers", "hosts", "settings")})
    cfg.providers = [_build(Provider, p) for p in data.get("providers", [])]
    cfg.hosts = [_build(Host, h) for h in data.get("hosts", [])]
    cfg.settings = _build(Settings, data.get("settings", {}))
    return cfg


def load(path: Path | None = None) -> Config:
    path = path or config_dir() / "config.toml"
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        return from_dict(tomllib.load(f))


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or config_dir() / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as f:
        tomli_w.dump(cfg.to_dict(), f)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
