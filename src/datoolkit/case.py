"""A diagnostic case: sensitivity level, audit log and exports."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import data_dir

SENSITIVITIES = ("open", "confidential", "sovereign")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:40] or "case"


@dataclass
class Case:
    name: str
    sensitivity: str
    notes: str = ""
    id: str = ""
    dir: Path = field(default_factory=Path)
    started: str = ""

    @classmethod
    def create(cls, name: str, sensitivity: str, notes: str = "", root: Path | None = None) -> "Case":
        if sensitivity not in SENSITIVITIES:
            raise ValueError(f"Unknown sensitivity {sensitivity}")
        now = datetime.now()
        case_id = f"{now:%Y%m%d-%H%M%S}-{_slug(name)}"
        d = (root or data_dir() / "cases") / case_id
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        case = cls(name=name, sensitivity=sensitivity, notes=notes, id=case_id, dir=d,
                   started=now.isoformat(timespec="seconds"))
        case.log("case_started", name=name, sensitivity=sensitivity, notes=notes)
        return case

    def log(self, event: str, **data) -> None:
        rec = {"ts": time.time(), "event": event, **data}
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def transcript_path(self, session_id: str) -> Path:
        return self.dir / f"term-{_slug(session_id)}.log"

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "sensitivity": self.sensitivity,
                "notes": self.notes, "dir": str(self.dir), "started": self.started}

    def export_markdown(self, chat: list[dict], queue: list[dict]) -> Path:
        out = [f"# {self.name}", "",
               f"- Case id: `{self.id}`", f"- Started: {self.started}",
               f"- Sensitivity: {self.sensitivity}", ""]
        if self.notes:
            out += ["## Notes", "", self.notes, ""]
        out += ["## Conversation", ""]
        for entry in chat:
            kind = entry.get("kind")
            if kind == "user":
                out += ["### Technician", ""]
                if entry.get("text"):
                    out += [entry["text"], ""]
                for r in entry.get("results", []):
                    out += [f"**#{r['num']} {r['status'].upper()}** on `{r['session_id']}`: `{r['command']}`", ""]
                    if r.get("note"):
                        out += [f"> Note: {r['note']}", ""]
                    if r.get("text"):
                        out += [fence(r["text"]), ""]
                for s in entry.get("snippets", []):
                    out += [f"Terminal excerpt from `{s['session_id']}`:", "", fence(s["text"]), ""]
            elif kind == "assistant":
                out += [f"### AI ({entry.get('model', '')}, {entry.get('tier', '')})", "", entry.get("text", ""), ""]
                if entry.get("proposals"):
                    out += ["Proposed: " + ", ".join(f"#{n}" for n in entry["proposals"]), ""]
            elif kind == "note":
                out += [f"_{entry.get('text', '')}_", ""]
        out += ["## Command queue", "", "| # | Session | Command | Risk | Status | Note |", "|---|---|---|---|---|---|"]
        for q in queue:
            cmd = q["command"].replace("|", "\\|").replace("\n", " ")
            out.append(f"| {q['num']} | {q['session_id']} | `{cmd}` | {q['risk']} | {q['status']} | {q['note']} |")
        path = self.dir / "transcript.md"
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return path


def fence(text: str, lang: str = "") -> str:
    """Code fence longer than any backtick run inside the text."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"
