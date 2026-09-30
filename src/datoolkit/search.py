"""Memory across cases: search past cases and their runbooks. Plain term scoring over the
files in each case directory; no index, no embeddings, nothing leaves the machine."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .case import Case
from .config import data_dir

_WORD = re.compile(r"[a-z0-9][a-z0-9._-]{2,}")
_STOP = {"the", "and", "for", "with", "that", "this", "from", "have", "has", "not", "are", "was", "were", "but",
         "can", "cant", "its", "into", "when", "what", "how", "why", "all", "any", "our", "you", "your", "they",
         "them", "there", "here", "also", "then", "than", "been", "will", "would", "could", "should", "just",
         "about", "after", "before", "does", "did", "done", "get", "got", "still", "again", "please", "help"}


def terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _case_text(d: Path) -> tuple[str, str]:
    """(searchable text, runbook text) for a case directory."""
    parts, runbook = [], ""
    for name in ("case.json", "ticket-summary.md", "runbook.md"):
        p = d / name
        if p.exists():
            try:
                t = p.read_text(encoding="utf-8")
            except OSError:
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


def search(query: str, root: Path | None = None, limit: int = 5, exclude_id: str = "") -> list[dict]:
    """Past cases ranked by term overlap with the query. Cases with a runbook rank higher."""
    q = terms(query)
    if not q:
        return []
    base = root or data_dir() / "cases"
    hits = []
    for c in Case.list_all(base):
        if c["id"] == exclude_id:
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
