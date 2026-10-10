"""Extra instructions for particular chat models, added to the end of the system prompt.

Some models follow DAToolkit's working rules less reliably than others in measured ways (see
tools/model-eval). Each built-in note answers a habit seen in those runs; the technician can
turn them off and add their own, matched by a piece of the model id."""

from __future__ import annotations

import re

# (pattern on the model id, note). Matched case-insensitively anywhere in the id, so a note for
# "glm" covers z-ai/glm-5.3, TEE/glm-5.3 and private/glm-5-3 alike.
_QUEUE_RULE = (
    "The technician can only run what is in their queue. Whenever your message says you will check something, "
    "says what you want them to run, or describes a fix (\"I'll queue a few checks\", \"Let's get a baseline\", "
    "\"The fix is to...\"), make the propose_commands call in that same turn with exactly those commands. Never "
    "say you have queued something unless you made the call in this turn, and never end a turn with commands "
    "described only in words. If there is nothing to queue yet, say what you are waiting for instead.")

BUILTIN: list[tuple[str, str]] = [
    # GLM 5.3 (evaluation 2026-10-10): "Let's get a baseline first with some read-only checks" with no call;
    # asked for #10 and #11 after they had been skipped
    (r"glm", _QUEUE_RULE + " Only ask the technician to run item numbers that are pending in the queue (see the "
             "current state); anything skipped or already run must be queued again."),
    # DeepSeek V4 Pro: an empty \"I'll queue a few checks\" in a third of its turns; queued a command with a
    # placeholder path; sidestepped \"when can we reboot?\" and the ground rule that answered it
    (r"deepseek", _QUEUE_RULE + " Answer every question the technician asks directly in your message before "
                  "moving on, and keep applying ground rules they set earlier in the case (times, systems not to "
                  "touch) when they bear on the answer."),
    # Kimi K2.7 Code: 9 turns in 6 runs announced checks or a fix with nothing queued; once it made the
    # update_hypotheses call and stopped before propose_commands (Kimi K3 had none of this)
    (r"kimi-k2", _QUEUE_RULE + " When a turn needs a hypothesis update and commands, make both calls in the "
                 "same reply."),
    # Qwen 3.7 Plus and Qwen 3.8 27B: "Let me start by checking..." and "The fix is to turn on X." with no
    # call (Qwen 3.8 Max didn't need this)
    (r"qwen(?!.*max)", _QUEUE_RULE),
]

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
