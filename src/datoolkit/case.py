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
        case.write_meta()
        return case

    @classmethod
    def load(cls, case_id: str, root: Path | None = None) -> "Case":
        """Reopen a case directory written by create()."""
        d = (root or data_dir() / "cases") / case_id
        meta = _read_meta(d)
        if meta is None:
            raise FileNotFoundError(f"No case {case_id}")
        return cls(name=meta["name"], sensitivity=meta["sensitivity"], notes=meta.get("notes", ""),
                   id=case_id, dir=d, started=meta.get("started", ""))

    @staticmethod
    def list_all(root: Path | None = None) -> list[dict]:
        """Summaries of every case on disk, newest first."""
        base = root or data_dir() / "cases"
        out = []
        if not base.is_dir():
            return out
        for d in base.iterdir():
            if not d.is_dir():
                continue
            meta = _read_meta(d)
            if meta is None:
                continue
            state = d / "state.json"
            summary = {"id": d.name, "name": meta["name"], "sensitivity": meta["sensitivity"],
                       "notes": meta.get("notes", ""), "started": meta.get("started", ""),
                       "resumable": state.exists(), "messages": 0}
            if state.exists():
                try:
                    summary["messages"] = len(json.loads(state.read_text(encoding="utf-8")).get("chat", []))
                except (OSError, ValueError):
                    pass
            out.append(summary)
        out.sort(key=lambda c: c["id"], reverse=True)
        return out

    def write_meta(self) -> None:
        _write_json(self.dir / "case.json", {"name": self.name, "sensitivity": self.sensitivity,
                                             "notes": self.notes, "started": self.started})

    def save_state(self, conv: list[dict], chat: list[dict], queue: list[dict],
                   hypotheses: list[dict] | None = None) -> None:
        """Persist everything needed to resume the case later."""
        _write_json(self.dir / "state.json", {"version": 2, "conv": conv, "chat": chat, "queue": queue,
                                              "hypotheses": hypotheses or []})

    def load_state(self) -> dict | None:
        path = self.dir / "state.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def log(self, event: str, **data) -> None:
        rec = {"ts": time.time(), "event": event, **data}
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def transcript_path(self, session_id: str) -> Path:
        return self.dir / f"term-{_slug(session_id)}.log"

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "sensitivity": self.sensitivity,
                "notes": self.notes, "dir": str(self.dir), "started": self.started}

    def export_markdown(self, chat: list[dict], queue: list[dict], hypotheses: list[dict] | None = None) -> Path:
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
                for img in entry.get("images", []):
                    out += [f"![photo]({img})", ""]
            elif kind == "assistant":
                out += [f"### AI ({entry.get('model', '')}, {entry.get('tier', '')})", "", entry.get("text", ""), ""]
                for q in entry.get("questions", []):
                    opts = f" ({' / '.join(q['options'])})" if q.get("options") else ""
                    out += [f"> **Question:** {q['question']}{opts}", ""]
                if entry.get("proposals"):
                    out += ["Proposed: " + ", ".join(f"#{n}" for n in entry["proposals"]), ""]
            elif kind == "note":
                out += [f"_{entry.get('text', '')}_", ""]
        if hypotheses:
            out += ["## Hypotheses", ""]
            for hyp in hypotheses:
                mark = f" ({hyp['tech_mark']} by technician)" if hyp.get("tech_mark") else ""
                out.append(f"- **{hyp.get('id')}** {hyp.get('text')} — {hyp.get('confidence', 0):.2f}, {hyp.get('status')}{mark}")
            out.append("")
        out += ["## Command queue", "", "| # | Session | Command | Risk | Status | Rollback | Note |", "|---|---|---|---|---|---|---|"]
        for q in queue:
            cmd = q["command"].replace("|", "\\|").replace("\n", " ")
            rb = (q.get("rollback") or "").replace("|", "\\|").replace("\n", " ")
            out.append(f"| {q['num']} | {q['session_id']} | `{cmd}` | {q['risk']} | {q['status']} | {rb} | {q['note']} |")
        path = self.dir / "transcript.md"
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        return path


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def _read_meta(d: Path) -> dict | None:
    """case.json, or the case_started event of a case written before case.json existed."""
    meta = d / "case.json"
    try:
        if meta.exists():
            return json.loads(meta.read_text(encoding="utf-8"))
        events = d / "events.jsonl"
        if events.exists():
            with events.open(encoding="utf-8") as f:
                first = json.loads(f.readline() or "{}")
            if first.get("event") == "case_started":
                started = datetime.fromtimestamp(first["ts"]).isoformat(timespec="seconds") if "ts" in first else ""
                return {"name": first.get("name", d.name), "sensitivity": first.get("sensitivity", "open"),
                        "notes": first.get("notes", ""), "started": started}
    except (OSError, ValueError):
        pass
    return None


def fence(text: str, lang: str = "") -> str:
    """Code fence longer than any backtick run inside the text."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"
