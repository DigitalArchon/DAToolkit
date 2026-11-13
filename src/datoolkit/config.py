"""Persistent configuration. Never holds secrets - those live in the OS keyring (see creds.py).

So is a password written into a provider's address (https://user:pass@host/v1, a model server
behind one): config.toml has the address without it, and the keyring the address as typed
(ADDRESS_SECRET). Until Nov 2026 the whole address went into the file, password and all."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import tomli_w

from . import creds
from .llm import netguard, params

NANOGPT_BASE_URL = "https://nano-gpt.com/api/v1"
# A provider's address written with a user name and password: config.toml has it without them
# (netguard.without_password), and the keyring has it as typed, under this kind and the
# provider's name, as its API key is kept. Put back together when the config is read, and only
# while the file's address is that one less its password, so a password goes to no address but
# the one it was typed for, whatever the file is edited to. (DA Assistant's config.py, 729a415.)
ADDRESS_SECRET = "base_url_with_password"


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


def write_atomic(path: Path, data: bytes, sync_dir: bool = True) -> None:
    """`data` as the file `path`, whole or not at all. It is written to a temporary file of its
    own in the same directory (two writers never share one, and it is 0600 from the start),
    flushed to the disk, and only then renamed over `path`. Until Nov 2026 nothing was flushed:
    a crash soon after a save could leave the new name on an empty file. With `sync_dir` the
    directory is flushed after, so the rename outlasts a power cut too; a file saved many times
    a minute goes without (a case's state.json): if its rename is lost, the file before it is
    there, whole."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    if not sync_dir:
        return
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass                       # a file system that can't flush a directory: the file itself is on the disk
    finally:
        os.close(dir_fd)


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
    # which API it speaks: "openai" (chat completions) or "anthropic" (Messages API); "" = from
    # the address (api.anthropic.com speaks Anthropic's, everything else OpenAI's): llm/providers.py
    api: str = ""
    # model id -> route, which of the provider's hosts serves it (NanoGPT, OpenRouter):
    # {"priority": "latency" | "speed" | "price" | "host", "fp8": bool, "host": "..."}; llm/routing.py
    routes: dict[str, dict] = field(default_factory=dict)


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
    # rdp: guacd's "security" (any | nla | tls | rdp) and keyboard layout on the server; "nla-ext"
    # from an earlier version is refused when opened (engine._NLA_EXT)
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
    # who searches for the chat model: "nanogpt" (the web_search tool, gated as above) or "native"
    # (Anthropic's own web search, when the chat model is on Anthropic's API; Open cases only,
    # since it can't ask before each query: Engine.search_status)
    search_engine: str = "nanogpt"
    # name of the NanoGPT provider whose key pays for searches; empty = the first NanoGPT one
    search_via: str = ""
    # "provider|model" of the research agent (research tool); empty = Claude Sonnet 5.5 on the
    # search provider. It sees only the AI's brief, and is gated like web search
    research_model: str = ""
    # Claude prompt caching through NanoGPT for the chat model: "off", "5m" or "1h" (cache
    # lifetime; 1h outlasts the minutes a technician spends running commands between turns)
    prompt_cache: str = "1h"
    # generation parameters sent with every request; a missing key means "the model's default"
    generation: dict = field(default_factory=lambda: dict(params.DEFAULTS))
    # 2 since the default effort moved to medium (Oct 2026): a config without it still holds the
    # old defaults if untouched, which move to the new ones once (from_dict); a choice made after
    # that stays, the same values included
    generation_version: int = 2
    # "provider|model" that describes images for a chat model without vision; empty = none
    vision_model: str = ""
    # After a TEE reply whose receipt holds, name the provider that ran its model, from RedPill's
    # public record of its gateway's session (llm/tee.py served_by). Off by default: it connects to
    # api.redpill.ai directly, not through NanoGPT, so RedPill sees this computer's address (the
    # author, Nov 11 2026: port it off by default, with a warning of what it does)
    tee_name_upstreams: bool = False
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

    def __post_init__(self):
        # What each config file held when this Config was last read from it or written to it
        # (path -> the sha256 of its bytes, None for no file): save() writes over nothing else
        # (ChangedElsewhere). Not a field: it is never written, and two Configs read from one
        # file are two DA Toolkits' views of it, each with its own.
        self._on_disk: dict[str, str | None] = {}

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
    settings = data.get("settings", {})
    cfg.settings = _build(Settings, settings)
    if "generation_version" not in settings and cfg.settings.generation in params.OLD_DEFAULTS:
        cfg.settings.generation = dict(params.DEFAULTS)     # untouched old defaults: the new ones, once
    return cfg


class ChangedElsewhere(Exception):
    """config.toml is no longer what this DA Toolkit last read or wrote: another one that is
    running has saved settings since. Nothing was written."""


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _on_disk(path: Path) -> bytes | None:
    """The file's bytes, None when there is no file."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@contextmanager
def _one_writer(path: Path):
    """One DA Toolkit at a time between comparing the file with what was read and writing over
    it: a lock (flock) on a file beside it, which the system drops if the holder dies."""
    fd = None
    try:
        fd = os.open(path.with_name(f".{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        pass                            # no lock to be had here (a read-only directory, a file system without them)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)


def changed_elsewhere(cfg: Config) -> bool:
    """Whether a file `cfg` was read from or written to has been changed by someone else since
    (another DA Toolkit that is running, or an editor): a save would be refused."""
    try:
        return any(_digest(_on_disk(Path(p))) != seen for p, seen in cfg._on_disk.items())
    except OSError:
        return False                    # can't be read now: the save itself will say why


def _used_before(path: Path) -> bool:
    """Whether DA Toolkit has been used here, and so had settings: there are cases, or pinned
    certificates, or what an interrupted save of `path` left beside it. (The keyring can't be
    asked what it holds without a name to ask for.)"""
    try:
        if any((data_dir() / "cases").iterdir()):
            return True
    except OSError:
        pass
    return (config_dir() / "rdp_pins.json").exists() or any(path.parent.glob(f".{path.name}.*.tmp"))


def load(path: Path | None = None) -> Config:
    path = path or config_dir() / "config.toml"
    raw = _on_disk(path)
    if raw is None:
        cfg = Config()
        cfg._on_disk[str(path)] = None
        return cfg
    if not raw.strip() and _used_before(path):
        # DA Toolkit never writes an empty file: one beside cases is what a crash or a full disk
        # left of the settings, not a first start. Until Nov 2026 it was read as "no settings",
        # and the app started without a word, its providers and hosts gone; load_or_set_aside
        # now keeps it and says so.
        raise ValueError("it is empty, though DA Toolkit has been used on this computer")
    cfg = from_dict(tomllib.loads(raw.decode("utf-8")))
    cfg._on_disk[str(path)] = _digest(raw)
    if _with_passwords(cfg):
        try:
            save(cfg, path)             # the keyring has what the file had: out of the file
        except (OSError, ChangedElsewhere):
            pass                        # read-only, or just saved by another DA Toolkit: it goes at the next save
    return cfg


def _stored_address(name: str) -> str:
    try:
        return creds.get_secret(ADDRESS_SECRET, name) or ""
    except Exception:  # noqa: BLE001 - locked or unavailable: the file's address is what there is
        return ""


def _with_passwords(cfg: Config) -> bool:
    """Each provider's address as it was typed: the file's, with the password the keyring has for
    it. One the file has a password in (an older version wrote it, or it was edited in by hand)
    is moved to the keyring, and out of the file once it is there; where the keyring can't take
    it, it stays in the file and is used, as before. True if one moved."""
    moved = False
    for prov in cfg.providers:
        written = prov.base_url or ""
        if not written:
            continue
        stored = _stored_address(prov.name)
        if netguard.without_password(written) != written:
            if stored != written:
                try:
                    creds.set_secret(ADDRESS_SECRET, prov.name, written)
                    stored = written
                except Exception:  # noqa: BLE001 - no keyring to take it: it stays in the file
                    pass
            moved = moved or stored == written
        elif stored and netguard.without_password(stored) == written:
            prov.base_url = stored
    return moved


def load_or_set_aside(path: Path | None = None) -> tuple[Config, str]:
    """The config, or, when it can't be read, the defaults and why: the file is kept beside
    itself (config.toml.broken-<time>) and never overwritten, and the app still starts, with a
    banner, rather than ending in a traceback with no window."""
    path = path or config_dir() / "config.toml"
    try:
        return load(path), ""
    except Exception as e:  # noqa: BLE001 - a hand-edited file can be wrong in any way at all
        kept = path.with_name(f"{path.name}.broken-{time.strftime('%Y%m%d-%H%M%S')}")
        cfg = Config()
        try:
            path.replace(kept)
        except OSError:
            return cfg, f"{path} couldn't be read ({e}). DA Toolkit started with default settings."
        cfg._on_disk[str(path)] = None      # set aside: the next save writes a new file, and over no one else's
        return cfg, (f"{path.name} couldn't be read ({e}), so DA Toolkit started with default settings. The file "
                          f"is kept as {kept}: fix it, quit, and put it back as {path.name}.")


def _without_passwords(data: dict) -> dict:
    """The config as written: each provider's address without the password the keyring holds
    for it. One the keyring doesn't hold (no keyring could take it when an older version's file
    was read) stays as it was, as an API key would: dropping it would lose it."""
    for prov in data.get("providers", []):
        full = prov.get("base_url") or ""
        bare = netguard.without_password(full)
        if bare != full and _stored_address(prov.get("name", "")) == full:
            prov["base_url"] = bare
    return data


def save(cfg: Config, path: Path | None = None) -> None:
    """Write the config, unless the file is no longer what `cfg` was read from or last written
    as: then another DA Toolkit that is running has saved since, this one's settings are older
    than the file's, and writing them would undo that one's (a host saved there was gone when a
    pane was dragged here). ChangedElsewhere is raised and nothing is written. A Config that
    was never read from `path` (a first save to it) is written as before."""
    path = path or config_dir() / "config.toml"
    private_dir(path.parent)
    data = tomli_w.dumps(_without_passwords(cfg.to_dict())).encode("utf-8")
    key = str(path)
    with _one_writer(path):
        if key in cfg._on_disk and _digest(_on_disk(path)) != cfg._on_disk[key]:
            raise ChangedElsewhere(f"{path.name} was changed by another DA Toolkit that is running")
        write_atomic(path, data)
        cfg._on_disk[key] = _digest(data)
