"""Persistent configuration. Never holds secrets - those live in the OS keyring (see creds.py)."""

from __future__ import annotations

import os
import stat
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


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def private_dir(path: Path) -> Path:
    """Make `path`, readable by this user alone: every directory from DA Toolkit's own root (the
    config or data directory) down to it is created or tightened to 0700 (Path.mkdir applies
    its mode to the last directory only, so parents made on the way would take the umask's,
    and the names under them, cases and hosts, are the technician's clients'). Directories
    above that root (~/.config, ~/.local/share) are left as they are; a path outside both is
    made private from itself down."""
    path = Path(path)
    root = next((r for r in (config_dir(), data_dir()) if _within(path, r)), path)
    root.parent.mkdir(parents=True, exist_ok=True)
    below = path.relative_to(root).parts
    for d in [root] + [root.joinpath(*below[:i + 1]) for i in range(len(below))]:
        d.mkdir(mode=0o700, exist_ok=True)
        if stat.S_IMODE(d.stat().st_mode) != 0o700:
            os.chmod(d, 0o700)
    return path


def tighten_dirs() -> None:
    """At start: make the config and data directories, and the directories in them (cases,
    baselines, research, tools, recipes), private; earlier versions made them with the
    umask's permissions. Symbolic links are left alone: what they point to isn't ours."""
    for d in (config_dir(), data_dir()):
        if d.is_dir() and not d.is_symlink():
            private_dir(d)
            for child in d.iterdir():
                if child.is_dir() and not child.is_symlink():
                    private_dir(child)


@dataclass
class Provider:
    name: str
    base_url: str
    default_model: str = ""
    # model id -> "standard" | "tee" | "local"; overrides automatic detection
    tier_overrides: dict[str, str] = field(default_factory=dict)
    # model id -> "yes" | "no": can it read images (overrides what the provider reports)
    vision_overrides: dict[str, str] = field(default_factory=dict)
    # model id -> context window in tokens (for providers that don't report it, e.g. local models)
    context_overrides: dict[str, int] = field(default_factory=dict)


@dataclass
class Host:
    name: str
    kind: str  # "ssh" | "winrm" | "rdp"
    host: str
    port: int | None = None
    user: str = ""
    # ssh: "agent" | "key" | "password"; winrm: "ntlm" | "kerberos" | "negotiate" | "basic"; rdp: "password"
    auth: str = "agent"
    key_file: str = ""
    jump: str = ""
    ssh_options: list[str] = field(default_factory=list)
    # winrm: HTTPS (5986) or plain HTTP (5985; only on the local network, see sessions/winrm.py);
    # the certificate checked against the system CA store, or else by DA Toolkit (pinned on first
    # use); Basic auth over plain HTTP only with the technician's say-so (the password is in the clear)
    winrm_ssl: bool = True
    winrm_cert_validation: bool = True
    winrm_plain_basic: bool = False
    os_hint: str = ""
    # rdp: guacd's "security" (any | nla | nla-ext | tls | rdp) and keyboard layout on the server
    rdp_security: str = "any"
    rdp_layout: str = "en-us-qwerty"


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
    # second opinions asked automatically when a command is queued (needs review_model):
    # "off", "disruptive", or "flagged" (anything not plain read-only: modifying, disruptive or sensitive)
    auto_review: str = "off"
    # AI web searches through NanoGPT: "ask" (approve each), "auto" (Open cases only), "off"
    search_mode: str = "ask"
    search_provider: str = "perplexity"
    # the research agent's "links" searches: the best pages fast, which it then reads itself
    search_links_provider: str = "kagi"
    # name of the NanoGPT provider whose key pays for searches; empty = the first NanoGPT one
    search_via: str = ""
    # "provider|model" of the research agent (research tool); empty = Claude Sonnet 5.5 on the
    # search provider. It sees only the AI's brief, and is gated like web search
    research_model: str = ""
    # Claude prompt caching through NanoGPT for the chat model: "off", "5m" or "1h" (cache
    # lifetime; 1h outlasts the minutes a technician spends running commands between turns)
    prompt_cache: str = "1h"
    # generation parameters sent with every request; a missing key means "the model's default"
    generation: dict = field(default_factory=lambda: {"temperature": 0.3, "reasoning_effort": "low"})
    # "provider|model" that describes images for a chat model without vision; empty = none
    vision_model: str = ""
    # extra system-prompt instructions for particular chat models: [{"match": "glm", "text": "..."}],
    # matched as a piece of the model id; built-in notes (llm/model_prompts.py) unless turned off
    model_prompts: list = field(default_factory=list)
    builtin_model_prompts: bool = True
    # the phone companion's HTTPS port, fixed so a firewall can open just this one
    companion_port: int = 48443
    # "window" (the app window, needs WebKitGTK) or "browser" (the default web browser); next launch
    ui_mode: str = "window"
    # pane sizes in pixels and which panes are collapsed (see Engine.save_layout). Kept here, not
    # in the page's storage: the app window's storage is private and the browser's origin (a
    # random port) changes every launch
    layout: dict = field(default_factory=dict)


@dataclass
class Config:
    providers: list[Provider] = field(default_factory=list)
    hosts: list[Host] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    active_provider: str = ""
    active_model: str = ""
    recent_models: list[str] = field(default_factory=list)
    # clients (customers) a case can belong to (Settings → Clients). Runbooks from earlier cases
    # reach the AI only within one client's cases: see Engine._find_similar
    clients: list[str] = field(default_factory=list)

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
    private_dir(path.parent)
    tmp = path.with_suffix(".tmp")
    with tmp.open("wb") as f:
        tomli_w.dump(cfg.to_dict(), f)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
