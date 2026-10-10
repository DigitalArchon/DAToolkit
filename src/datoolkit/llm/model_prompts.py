"""Extra instructions for particular chat models, added to the end of the system prompt.

Some models follow DAToolkit's working rules less reliably than others in measured ways (see
tools/model-eval). Each built-in note answers a habit seen in those runs; the technician can
turn them off and add their own, matched by a piece of the model id."""

from __future__ import annotations

import re

# (pattern on the model id, note). Matched case-insensitively anywhere in the id, so a note for
# "glm" covers z-ai/glm-5.3, TEE/glm-5.3 and private/glm-5-3 alike.
BUILTIN: list[tuple[str, str]] = []

MAX_RULES = 30
MAX_MATCH = 100
MAX_TEXT = 4000


def builtin_for(model: str) -> list[str]:
    return [note for pattern, note in BUILTIN if re.search(pattern, model, re.I)]


def user_for(model: str, rules: list[dict]) -> list[str]:
    m = model.lower()
    return [r["text"].strip() for r in rules or []
            if r.get("match", "").strip() and r["match"].strip().lower() in m and r.get("text", "").strip()]


def notes_for(model: str, rules: list[dict], builtin: bool = True) -> str:
    """The notes that apply to this model: built-in ones (unless turned off), then the technician's."""
    if not model:
        return ""
    notes = (builtin_for(model) if builtin else []) + user_for(model, rules)
    return "\n\n".join(notes)


def validate(rules) -> list[dict]:
    if not isinstance(rules, list):
        raise ValueError("Model instructions must be a list.")
    if len(rules) > MAX_RULES:
        raise ValueError(f"At most {MAX_RULES} model instructions.")
    out = []
    for r in rules:
        if not isinstance(r, dict):
            raise ValueError("Each model instruction needs a model match and a text.")
        match, text = str(r.get("match", "")).strip(), str(r.get("text", "")).strip()
        if not match and not text:
            continue
        if not match or not text:
            raise ValueError("Each model instruction needs both a model match and a text.")
        if len(match) > MAX_MATCH or len(text) > MAX_TEXT:
            raise ValueError(f"A model match is at most {MAX_MATCH} characters and a text at most {MAX_TEXT}.")
        out.append({"match": match, "text": text})
    return out
