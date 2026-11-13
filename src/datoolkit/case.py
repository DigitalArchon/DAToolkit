"""A diagnostic case: sensitivity level, audit log and exports."""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import data_dir, private_dir, write_atomic

SENSITIVITIES = ("open", "confidential", "sovereign")
# What the copy of state.json as it was before the last save is called (Case.load_backup).
BACKUP_SUFFIX = ".bak"


def utf8(text: str) -> bytes:
    """`text` as UTF-8. A surrogate has no UTF-8 form: a pair (a reply streamed in pieces can
    split one) is joined, a lone one (half an emoji pasted) becomes U+FFFD. Raising instead
    lost the write, and with the text still in the case, every later save of it."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace").encode("utf-8")


def clean_text(text: str) -> str:
    """`text` with its surrogates mended as utf8() does."""
    return utf8(text).decode("utf-8")


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
    # the client (customer) the case is for, one of the config's client list; "" for none
    client: str = ""
    # feed the AI runbooks from this client's earlier cases (of the same or a lower sensitivity)
    history: bool = False

    @classmethod
    def create(cls, name: str, sensitivity: str, notes: str = "", root: Path | None = None,
               client: str = "", history: bool = False) -> "Case":
        if sensitivity not in SENSITIVITIES:
            raise ValueError(f"Unknown sensitivity {sensitivity}")
        now = datetime.now()
        base = root or data_dir() / "cases"
        case_id = stem = f"{now:%Y%m%d-%H%M%S}-{_slug(name)}"
        n = 1
        while (base / case_id).exists():     # the same name in the same second: never another case's folder
            n += 1
            case_id = f"{stem}-{n}"
        d = private_dir(base / case_id)
        case = cls(name=name, sensitivity=sensitivity, notes=notes, id=case_id, dir=d,
                   started=now.isoformat(timespec="seconds"), client=client, history=bool(history))
        case.log("case_started", name=name, sensitivity=sensitivity, notes=notes, client=client, history=bool(history))
        case.write_meta()
        return case

    @classmethod
    def load(cls, case_id: str, root: Path | None = None) -> "Case":
        """Reopen a case directory written by create()."""
        d = _case_dir(case_id, root)
        meta = _read_meta(d)
        return cls(name=meta["name"], sensitivity=meta["sensitivity"], notes=meta.get("notes", ""),
                   id=case_id, dir=d, started=meta.get("started", ""), client=meta.get("client", ""),
                   history=bool(meta.get("history", False)))

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
                       "client": meta.get("client", ""), "history": bool(meta.get("history", False)),
                       "resumable": state.exists(), "messages": 0}
            if state.exists():
                try:
                    summary["messages"] = len(json.loads(state.read_text(encoding="utf-8")).get("chat", []))
                except (OSError, ValueError):
                    pass
            out.append(summary)
        out.sort(key=lambda c: c["id"], reverse=True)
        return out

    @staticmethod
    def delete(case_id: str, root: Path | None = None) -> None:
        """Remove a case directory and everything in it."""
        shutil.rmtree(_case_dir(case_id, root))

    def edit(self, name: str, notes: str, sensitivity: str | None = None, client: str | None = None,
             history: bool | None = None) -> None:
        """Rename the case, replace its notes, raise its sensitivity, and change its client and
        whether that client's history is used (None: unchanged). Sensitivity only goes up: what
        was sent under the old level has been sent, and a lower level would allow more models to
        see the conversation so far. The directory keeps its original id."""
        sensitivity = sensitivity or self.sensitivity
        if sensitivity not in SENSITIVITIES:
            raise ValueError(f"Unknown sensitivity {sensitivity}")
        if SENSITIVITIES.index(sensitivity) < SENSITIVITIES.index(self.sensitivity):
            raise ValueError(f"A case's sensitivity can only be raised, not lowered from {self.sensitivity}")
        client = self.client if client is None else client
        history = self.history if history is None else bool(history)
        if (name, notes, sensitivity, client, history) == (self.name, self.notes, self.sensitivity, self.client, self.history):
            return
        self.log("case_edited", name=name, notes=notes, sensitivity=sensitivity, client=client, history=history,
                 old_name=self.name, old_notes=self.notes, old_sensitivity=self.sensitivity, old_client=self.client,
                 old_history=self.history)
        self.name, self.notes, self.sensitivity, self.client, self.history = name, notes, sensitivity, client, history
        self.write_meta()

    def set_client(self, client: str) -> None:
        """The client was renamed or deleted in the config: follow it."""
        if client == self.client:
            return
        self.log("client_changed", client=client, old_client=self.client)
        self.client = client
        self.write_meta()

    def write_meta(self) -> None:
        _write_json(self.dir / "case.json", {"name": self.name, "sensitivity": self.sensitivity,
                                             "notes": self.notes, "started": self.started,
                                             "client": self.client, "history": self.history})

    def save_state(self, conv: list[dict], chat: list[dict], queue: list[dict],
                   hypotheses: list[dict] | None = None, compactions: list[dict] | None = None,
                   last_usage: dict | None = None, similar: list[dict] | None = None) -> None:
        """Persist everything needed to resume the case later. `similar`: the past cases whose
        runbooks are in its AI's context (id, score, matched terms; search.reload_hits). The
        state.json before this one is kept as state.json.bak (load_backup)."""
        _write_state(self.dir / "state.json", {"version": 2, "conv": conv, "chat": chat, "queue": queue,
                                               "hypotheses": hypotheses or [], "compactions": compactions or [],
                                               "last_usage": last_usage, "similar": similar or []})

    def load_state(self) -> dict | None:
        """The saved state, None when there is none. Raises ValueError when state.json isn't one."""
        path = self.dir / "state.json"
        if not path.exists():
            return None
        return _read_state(path)

    def has_backup(self) -> bool:
        return (self.dir / f"state.json{BACKUP_SUFFIX}").exists()

    def load_backup(self) -> dict | None:
        """The state as it was saved one time before the last (state.json.bak), for when
        state.json can't be read; None when there is none. Raises ValueError like load_state."""
        path = self.dir / f"state.json{BACKUP_SUFFIX}"
        if not path.exists():
            return None
        return _read_state(path)

    def set_state_aside(self) -> str:
        """Move a state.json that can't be read out of the way (state.json.damaged-<time>), so
        the next save doesn't keep it as the backup in place of the one that can. Returns its
        new name, "" when there was none to move."""
        path = self.dir / "state.json"
        kept = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            path.replace(kept)
        except OSError:
            return ""
        return kept.name

    def save_draft(self, text: str, images: list[str]) -> None:
        """The chat box's message not sent yet and the photos attached to it (draft.json).
        Saves of one case land in the order they were asked for: the page saves as the
        technician types, each save on a thread of its own, and one with photos takes long
        enough to be overtaken by the next. Until Nov 2026 the older one could finish last and
        put back photos just removed, and both wrote one draft.tmp. Each save takes a number as
        it begins, and one that finds a later number already written does nothing."""
        seq = next(_draft_seq)
        path = self.dir / "draft.json"
        data = _json_bytes({"text": text, "images": images}) if text.strip() or images else None
        with _drafts_guard:
            order = _drafts.setdefault(str(self.dir), _DraftOrder())
        with order.lock:
            if seq < order.landed:
                return
            if data is None:
                path.unlink(missing_ok=True)
            else:
                write_atomic(path, data, sync_dir=False)
            order.landed = seq

    def load_draft(self) -> dict:
        try:
            data = json.loads((self.dir / "draft.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"text": "", "images": []}
        images = data.get("images") if isinstance(data.get("images"), list) else []
        return {"text": str(data.get("text") or ""), "images": [i for i in images if isinstance(i, str)]}

    def patch_chat_entry(self, index: int, expect: dict, **fields) -> bool:
        """Set `fields` on one chat entry of the saved state, if it still holds `expect`: a
        result that arrived after the case was closed. Returns whether it did."""
        state = self.load_state()
        chat = (state or {}).get("chat")
        if not isinstance(chat, list) or not 0 <= index < len(chat) or not isinstance(chat[index], dict):
            return False
        if any(chat[index].get(k) != v for k, v in expect.items()):
            return False
        chat[index].update(fields)
        _write_state(self.dir / "state.json", state)
        return True

    def log(self, event: str, **data) -> None:
        rec = {"ts": time.time(), "event": event, **data}
        line = utf8(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        with (self.dir / "events.jsonl").open("ab") as f:
            f.write(line)

    def transcript_path(self, session_id: str) -> Path:
        """Where a session's terminal transcript is written. Named by the whole id: cut to 40
        characters, as names once were, two sessions to a host with a long name (web01-..., and
        its -2) shared one file, and each one's output read as the other's."""
        return self.dir / f"term-{re.sub(r'[^A-Za-z0-9._-]+', '-', session_id).strip('-') or 'session'}.log"

    def transcript_file(self, session_id: str) -> Path:
        """The transcript to read: transcript_path, or the cut name a case from before has."""
        path = self.transcript_path(session_id)
        old = self.dir / f"term-{_slug(session_id)}.log"
        return old if not path.exists() and old.exists() else path

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "sensitivity": self.sensitivity,
                "notes": self.notes, "dir": str(self.dir), "started": self.started,
                "client": self.client, "history": self.history}

    def export_markdown(self, chat: list[dict], queue: list[dict], hypotheses: list[dict] | None = None) -> Path:
        out = [f"# {self.name}", "",
               f"- Case id: `{self.id}`", f"- Started: {self.started}",
               f"- Sensitivity: {self.sensitivity}"] + ([f"- Client: {self.client}"] if self.client else []) + [""]
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
                    out += [f"**#{r['num']} {r['status'].upper()}** on `{r['session_id']}`: {inline_code(r['command'])}", ""]
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
                if entry.get("citations"):     # Anthropic's search: the sources go with the text
                    out += ["Sources: " + " · ".join(f"[{c.get('title') or c['url']}]({c['url']})" for c in entry["citations"]), ""]
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
            out.append(f"| {q['num']} | {q['session_id']} | {inline_code(_cell(q['command']))} | {q['risk']} | {q['status']} "
                       f"| {_cell(q.get('rollback'))} | {_cell(q.get('note'))} |")
        path = self.dir / "transcript.md"
        path.write_bytes(utf8("\n".join(out) + "\n"))
        return path


def _json_bytes(data: dict) -> bytes:
    return utf8(json.dumps(data, ensure_ascii=False, default=str))


def _write_json(path: Path, data: dict) -> None:
    """`data` as the JSON file `path`, whole and on the disk or not at all (config.write_atomic)."""
    write_atomic(path, _json_bytes(data))


def _write_state(path: Path, data: dict) -> None:
    """state.json, with the one it replaces kept beside it as state.json.bak: a state.json that
    can't be read made its case unopenable, and the conversation in it was lost."""
    raw = _json_bytes(data)
    _keep_previous(path)
    write_atomic(path, raw, sync_dir=False)


def _keep_previous(path: Path) -> None:
    """The file as it is now, kept as <name>.bak: a second name for the same data (a hard
    link), so nothing is copied and at no moment is either name missing; copied where the file
    system has no hard links. One that isn't a whole JSON object (cut short, emptied) is not
    kept: the backup that is there stays, the last state that could be read."""
    try:
        with path.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(0)
            first = f.read(1)
            f.seek(max(0, size - 64))
            last = f.read().rstrip()[-1:]
    except OSError:
        return
    if first != b"{" or last != b"}":
        return
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    tmp = bak.with_name(f".{bak.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.unlink(missing_ok=True)
        try:
            os.link(path, tmp)
        except OSError:
            shutil.copyfile(path, tmp)
        os.replace(tmp, bak)
    except OSError:
        tmp.unlink(missing_ok=True)      # no backup this time: the save itself goes on


def _read_state(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} doesn't hold a case's state")
    return data


class _DraftOrder:
    """One case's draft saves: the lock they are written under, and the number of the latest
    one written (Case.save_draft)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.landed = 0


_draft_seq = itertools.count(1)
_drafts: dict[str, _DraftOrder] = {}       # case directory -> its draft saves
_drafts_guard = threading.Lock()


def _case_dir(case_id: str, root: Path | None = None) -> Path:
    """The directory of a case. Only a direct child of the cases directory that is a case (has
    case.json or a case_started event) counts: no paths, no symlinks."""
    base = (root or data_dir() / "cases").resolve()
    if not re.fullmatch(r"[\w.-]+", case_id) or case_id in (".", ".."):
        raise ValueError(f"Bad case id {case_id!r}")
    d = base / case_id
    if d.is_symlink() or not d.is_dir() or d.resolve().parent != base or _read_meta(d) is None:
        raise FileNotFoundError(f"No case {case_id}")
    return d


def _read_meta(d: Path) -> dict | None:
    """case.json, or the case_started event of a case written before case.json existed, with
    its name and sensitivity always there (one damaged file mustn't break the case list): a
    sensitivity that can't be read is taken as the strictest."""
    meta = _read_meta_raw(d)
    if meta is None:
        return None
    meta = dict(meta)
    meta["name"] = str(meta.get("name") or d.name)
    if meta.get("sensitivity") not in SENSITIVITIES:
        meta["sensitivity"] = SENSITIVITIES[-1]
    return meta


def _read_meta_raw(d: Path) -> dict | None:
    """case.json; failing that (none, or damaged), the case_started event that began the audit log."""
    try:
        data = json.loads((d / "case.json").read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    try:
        with (d / "events.jsonl").open(encoding="utf-8") as f:
            first = json.loads(f.readline() or "{}")
    except (OSError, ValueError):
        return None
    if not isinstance(first, dict) or first.get("event") != "case_started":
        return None
    started = datetime.fromtimestamp(first["ts"]).isoformat(timespec="seconds") if isinstance(first.get("ts"), (int, float)) else ""
    return {"name": first.get("name", d.name), "sensitivity": first.get("sensitivity", "open"),
            "notes": first.get("notes", ""), "started": started, "client": first.get("client", ""),
            "history": bool(first.get("history", False))}


def inline_code(text: str) -> str:
    """`text` as inline code, with a delimiter longer than any backtick run inside: PowerShell
    escapes with a backtick, and `"`n"` ended a one-backtick span early."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{'`' * (longest + 1)}{pad}{text}{pad}{'`' * (longest + 1)}"


def _cell(text: str) -> str:
    """Text for a Markdown table cell: a | or a line break would end the cell or the row."""
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def fence(text: str, lang: str = "") -> str:
    """Code fence longer than any backtick run inside the text."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"
