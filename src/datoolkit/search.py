"""Memory across cases: search past cases and their runbooks. Plain term scoring over the
files in each case directory; no index, no embeddings, nothing leaves the machine."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .case import SENSITIVITIES, Case
from .config import data_dir

_WORD = re.compile(r"[a-z0-9][a-z0-9._-]{2,}")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "have", "has", "not", "are", "was", "were", "but",
         "can", "cant", "its", "into", "when", "what", "how", "why", "all", "any", "our", "you", "your", "they",
         "them", "there", "here", "also", "then", "than", "been", "will", "would", "could", "should", "just",
         "about", "after", "before", "does", "did", "done", "get", "got", "still", "again", "please", "help"}


def terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _runbook(d: Path) -> str:
    """A case's runbook, "" when it has none."""
    try:
        return (d / "runbook.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""


def _case_text(d: Path) -> tuple[str, str]:
    """(searchable text, runbook text) for a case directory. The one place a search reads a
    case's state.json: the list of cases it works from no longer does (Case.list_all), where
    until Nov 2026 every state.json was parsed twice for each search, photos and all."""
    parts, runbook = [], ""
    try:
        # what the case says of itself, not the file as text: its keys ("messages", "history")
        # would be words every case has
        meta = json.loads((d / "case.json").read_text(encoding="utf-8"))
        parts += [str(meta.get(key) or "") for key in ("name", "notes", "client")]
    except (OSError, ValueError, AttributeError):
        pass
    for name in ("ticket-summary.md", "runbook.md"):
        p = d / name
        if p.exists():
            try:
                t = p.read_text(encoding="utf-8")
            except (OSError, ValueError):
                continue
            parts.append(t)
            if name == "runbook.md":
                runbook = t
    state = d / "state.json"
    if state.exists():
        try:
            for e in json.loads(state.read_text(encoding="utf-8")).get("chat", []):
                if e.get("kind") in ("user", "assistant"):
                    parts.append(e.get("text", ""))
                    for r in e.get("results", []):
                        parts.append(r.get("command", ""))
        except (OSError, ValueError):
            pass
    return "\n".join(parts), runbook


def search(query: str, root: Path | None = None, limit: int = 5, exclude_id: str = "",
           client: str | None = None, max_sensitivity: str | None = None) -> list[dict]:
    """Past cases ranked by term overlap with the query. Cases with a runbook rank higher.
    `client` keeps to that client's cases (the empty string: cases of no client); `max_sensitivity`
    to cases of that sensitivity or a lower one. Both are None for the technician's own search,
    which stays on this machine; what goes to the AI is always narrowed by both."""
    q = terms(query)
    if not q:
        return []
    base = root or data_dir() / "cases"
    allowed = SENSITIVITIES.index(max_sensitivity) if max_sensitivity in SENSITIVITIES else None
    hits = []
    for c in Case.list_all(base):
        if c["id"] == exclude_id:
            continue
        if client is not None and c.get("client", "") != client:
            continue
        if allowed is not None and (c["sensitivity"] not in SENSITIVITIES or SENSITIVITIES.index(c["sensitivity"]) > allowed):
            continue
        text, runbook = _case_text(base / c["id"])
        words = terms(text)
        common = q & words
        if not common:
            continue
        score = len(common) / len(q) + (0.5 if runbook else 0) + (0.2 if terms(c["name"]) & q else 0)
        hits.append({**c, "score": round(score, 2), "matched": sorted(common)[:8], "has_runbook": bool(runbook),
                     "runbook": runbook[:4000]})
    hits.sort(key=lambda h: (-h["score"], h["id"]), reverse=False)
    return hits[:limit]


def reload_hits(saved: list, client: str, max_sensitivity: str, root: Path | None = None) -> list[dict]:
    """The hits a case found (as saved with it: id, score, matched terms) in search()'s form,
    for the case opened again: each read afresh, and kept only while it still exists, is the
    same client's and no more sensitive than `max_sensitivity`, as search() would have it."""
    base = root or data_dir() / "cases"
    allowed = SENSITIVITIES.index(max_sensitivity) if max_sensitivity in SENSITIVITIES else -1
    cases = {c["id"]: c for c in Case.list_all(base)}
    hits = []
    for s in saved if isinstance(saved, list) else []:
        c = cases.get(str(s.get("id", ""))) if isinstance(s, dict) else None
        if (not c or c.get("client", "") != client or c["sensitivity"] not in SENSITIVITIES
                or SENSITIVITIES.index(c["sensitivity"]) > allowed):
            continue
        runbook = _runbook(base / c["id"])       # the runbook alone: no need to read its conversation
        matched = s.get("matched") if isinstance(s.get("matched"), list) else []
        hits.append({**c, "score": s.get("score", 0), "matched": [str(m) for m in matched][:8], "has_runbook": bool(runbook),
                     "runbook": runbook[:4000]})
    return hits


def runbook_context(hits: list[dict], max_chars: int = 6000) -> str:
    """Runbooks from search hits, for the system prompt."""
    out, used = [], 0
    for h in hits:
        if not h.get("runbook"):
            continue
        block = f"### {h['name']} ({h['started'][:10]})\n{h['runbook'].strip()}"
        if used + len(block) > max_chars:
            break
        out.append(block)
        used += len(block)
    return "\n\n".join(out)
