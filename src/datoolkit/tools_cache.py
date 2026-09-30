"""Portable tool cache: vetted CLI binaries the technician can copy to a host.

The cache is a directory of files plus tools.toml with a SHA-256 for each. Nothing here
runs anything: "transfer" produces a command (scp for SSH hosts, a PowerShell
base64-chunk copy for WinRM) that goes through the queue like any other proposal.
"""

from __future__ import annotations

import base64
import hashlib
import shlex
import shutil
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w

from .config import Host, data_dir


def cache_dir() -> Path:
    return data_dir() / "tools"


@dataclass
class Tool:
    name: str
    file: str            # file name inside the cache directory
    sha256: str
    os: str = "windows"  # "windows" | "linux" | "any"
    notes: str = ""      # what it does, where it came from, licence
    run: str = ""        # how to run it once on the host, e.g. "WizTree64.exe /export=C:\\wiztree.csv"
    size: int = 0
    tags: list[str] = field(default_factory=list)


def _manifest_path(root: Path) -> Path:
    return root / "tools.toml"


def load(root: Path | None = None) -> list[Tool]:
    root = root or cache_dir()
    path = _manifest_path(root)
    if not path.exists():
        return []
    with path.open("rb") as f:
        data = tomllib.load(f)
    return [Tool(**{k: v for k, v in t.items() if k in Tool.__dataclass_fields__}) for t in data.get("tool", [])]


def save(tools: list[Tool], root: Path | None = None) -> None:
    root = root or cache_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _manifest_path(root).open("wb") as f:
        tomli_w.dump({"tool": [asdict(t) for t in tools]}, f)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def add(source: Path, name: str, os_: str, notes: str = "", run: str = "", root: Path | None = None) -> Tool:
    """Copy a file into the cache and record its hash."""
    root = root or cache_dir()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    source = Path(source).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"{source} is not a file")
    dest = root / source.name
    if dest != source:
        shutil.copy2(source, dest)
    tool = Tool(name=name.strip() or source.stem, file=source.name, sha256=sha256_of(dest), os=os_,
                notes=notes.strip(), run=run.strip(), size=dest.stat().st_size)
    tools = [t for t in load(root) if t.name != tool.name]
    tools.append(tool)
    save(tools, root)
    return tool


def remove(name: str, root: Path | None = None) -> None:
    root = root or cache_dir()
    tools = load(root)
    keep = [t for t in tools if t.name != name]
    for t in tools:
        if t.name == name and not any(k.file == t.file for k in keep):
            (root / t.file).unlink(missing_ok=True)
    save(keep, root)


def verify(root: Path | None = None) -> list[dict]:
    """Each tool with whether its file is present and its hash still matches."""
    root = root or cache_dir()
    out = []
    for t in load(root):
        path = root / t.file
        status = "missing" if not path.exists() else ("ok" if sha256_of(path) == t.sha256 else "HASH MISMATCH")
        out.append({**asdict(t), "status": status, "path": str(path)})
    return out


def transfer_command(tool: Tool, host: Host | None, kind: str, root: Path | None = None) -> dict:
    """A command the technician runs to copy the tool to a host.

    ssh: an scp command for a *local* session (uses the tech's ssh config, keys and agent).
    winrm: a PowerShell command for the *remote* session that decodes an embedded base64
    payload; fine for small tools, and the command shows the hash so it can be checked."""
    root = root or cache_dir()
    path = root / tool.file
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing from the tool cache")
    if sha256_of(path) != tool.sha256:
        raise ValueError(f"{tool.file} no longer matches its recorded hash; refusing to transfer")
    if kind == "ssh" and host:
        target = f"{host.user}@{host.host}" if host.user else host.host
        port = ["-P", str(host.port)] if host.port else []
        jump = ["-J", host.jump] if host.jump else []
        argv = ["scp"] + port + jump + [str(path), f"{target}:~/{tool.file}"]
        check = f"ssh {shlex.quote(target)} sha256sum ~/{shlex.quote(tool.file)}"
        return {"session": "local", "command": " ".join(shlex.quote(a) for a in argv),
                "purpose": f"Copy {tool.name} to {target} (then verify: {check})", "risk": "modifying",
                "verify": check, "expected_sha256": tool.sha256}
    if kind == "winrm":
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("WinRM inline transfer is limited to 4 MB; use a share or scp for larger tools")
        b64 = base64.b64encode(path.read_bytes()).decode()
        dest = f"$env:TEMP\\{tool.file}"
        cmd = (f"[IO.File]::WriteAllBytes(\"{dest}\", [Convert]::FromBase64String('{b64}')); "
               f"(Get-FileHash \"{dest}\" -Algorithm SHA256).Hash.ToLower() -eq '{tool.sha256}'")
        return {"session": "remote", "command": cmd,
                "purpose": f"Write {tool.name} to %TEMP% and confirm its SHA-256 is {tool.sha256[:12]}…", "risk": "modifying",
                "expected_sha256": tool.sha256}
    raise ValueError("Transfer needs an SSH host (scp from a local session) or a WinRM session")
