"""Per-turn checks of how well a model follows DAToolkit's working rules. Each flag is a
behaviour the technician would notice; none of them judges the diagnosis itself."""

from __future__ import annotations

import json
import re

from datoolkit.safety.risk import LEVELS, classify

TOOL_NAMES = {"propose_commands", "update_hypotheses", "run_recipe", "ask_technician", "revise_queue", "web_search",
              "research"}

# a message that announces commands, checks or steps to come
PROMISE = re.compile(
    r"(\b(run|try|execute|check|send me|paste)\b[^.\n]{0,60}\b(following|these|this|few|more|next|some)\b[^.\n]{0,30}"
    r"\b(commands?|checks?|steps?|diagnostics?)\b"
    r"|\b(I'?ll|I will|let me|I'?m going to|I need you to|now I need)\b[^.\n]{0,40}\b(queue|propose|give|send|add|run)\b"
    r"|\bhere (are|is) (the|a few|some)\b[^.\n]{0,30}\b(commands?|checks?)\b"
    r"|:\s*$)", re.I)

# words that start a shell/CLI command, to tell a command written in prose from a mere name
CMD_WORDS = re.compile(
    r"^\s*(sudo\s+)?(ls|cat|grep|awk|sed|df|du|ip|ss|ping|ethtool|journalctl|systemctl|ffprobe|ffmpeg|mount|findmnt|"
    r"iperf3?|dmesg|lsblk|smartctl|top|free|uptime|nload|ifstat|sar|iostat|docker|tail|head|find|w32tm|Get-\w+|"
    r"Set-\w+|Test-\w+|Restart-\w+|nltest|klist|dcdiag|repadmin|net|Resolve-DnsName|/[a-z]+)\b", re.I)

PAGERS = re.compile(r"(^|[|;&]\s*)(less|more|vi|vim|nano|htop)\b|(^|[|;&]\s*)top(?!\s+-b)\b|\btail\s+(-\S+\s+)*-f\b|"
                    r"\bwatch\s", re.M)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _prose_commands(text: str) -> list[str]:
    spans = re.findall(r"```[a-z]*\n(.*?)```", text, re.S)
    spans = [line for block in spans for line in block.splitlines() if line.strip()]
    spans += re.findall(r"`([^`\n]{4,})`", text)
    return [s for s in spans if CMD_WORDS.match(s) and " " in s.strip()]


def shell_breaches(cmd: str, kind: str, os_hint: str) -> list[str]:
    out = []
    if PAGERS.search(cmd):
        out.append("pager/interactive or never-ending")
    if kind == "winrm" and "\n" in cmd.strip():
        out.append("multi-line PowerShell")
    linux = kind in ("ssh", "local") and "routeros" not in os_hint.lower()
    if linux and re.search(r"(^|[|;&]\s*)(sudo\s+)?ping\s", cmd) and not re.search(r"\s-c\s*\d", cmd):
        out.append("ping without -c")
    if (linux and re.search(r"\bjournalctl\b", cmd) and "--no-pager" not in cmd and "|" not in cmd
            and not re.search(r"--(disk-usage|vacuum-\S+|list-boots|verify|rotate|flush)\b", cmd)):
        out.append("journalctl without --no-pager")
    if "routeros" in os_hint.lower():
        if re.search(r"\b(monitor-traffic|monitor)\b", cmd) and "once" not in cmd:
            out.append("RouterOS monitor without once")
        if re.search(r"/(tool\s+)?torch\b", cmd) and "duration" not in cmd:
            out.append("RouterOS torch without duration")
        if re.search(r"(^|\s)/?ping\s", cmd) and "count" not in cmd:
            out.append("RouterOS ping without count")
    return out


def turn_flags(entry: dict, rounds: list[dict], proposals: list[dict], sessions: dict[str, dict],
               history: list[dict], events: list[dict]) -> dict:
    """entry: the chat entry of the AI's turn; rounds: the logged responses of each request in
    the turn; proposals: queue items the turn added; sessions: id -> scenario session;
    history: earlier turn records (for repeats)."""
    text = entry.get("text", "") or ""
    questions = [q.get("question", "") for q in entry.get("questions", [])]
    acted = bool(proposals or questions or entry.get("withdrawn") or entry.get("hyp_changes"))
    f: dict = {}
    if entry.get("error") or (not text and not acted):
        f["error_or_empty"] = entry.get("error") or "empty turn"
    if not text.strip():
        f["no_message"] = True
    if not proposals and not questions and not entry.get("withdrawn"):
        last = text.strip().splitlines()[-3:] if text.strip() else []
        if any(PROMISE.search(line) for line in last):
            f["promise_without_call"] = " / ".join(last)[-200:]
        prose = _prose_commands(text)
        if prose:
            f["commands_in_prose_only"] = prose[:4]
    bad_calls = []
    for r in rounds:
        for c in r.get("tool_calls") or []:
            if c.get("name") not in TOOL_NAMES:
                bad_calls.append(f"unknown tool {c.get('name')}")
                continue
            try:
                json.loads(c.get("arguments") or "{}")
            except ValueError:
                bad_calls.append(f"invalid JSON for {c.get('name')}")
    if bad_calls:
        f["bad_tool_calls"] = bad_calls
    if any(e.get("event") == "no_message_nudge" for e in events):
        f["no_message_nudge"] = True
    if any(e.get("event") == "promise_nudge" for e in events):
        f["promise_nudge"] = True
    if len(rounds) > 1:
        f["rounds"] = len(rounds)
    under, no_rb, breaches, wrong_sess = [], [], [], []
    for p in proposals:
        local, _ = classify(p["command"])
        if LEVELS.index(p.get("model_risk") if p.get("model_risk") in LEVELS else "modifying") < LEVELS.index(local):
            under.append(f"#{p['num']} said {p.get('model_risk')}, rules say {local}")
        if p["risk"] != "read_only" and not (p.get("rollback") or "").strip():
            no_rb.append(p["num"])
        s = sessions.get(p["session_id"])
        if not s:
            wrong_sess.append(f"#{p['num']} -> {p['session_id']!r}")
        else:
            for b in shell_breaches(p["command"], s["kind"], s.get("os_hint", "")):
                breaches.append(f"#{p['num']} {b}")
    if under:
        f["risk_under_labelled"] = under
    if no_rb:
        f["missing_rollback"] = no_rb
    if breaches:
        f["shell_rule_breaches"] = breaches
    if wrong_sess:
        f["unknown_session"] = wrong_sess
    earlier_pending = {_norm(p["command"]) for t in history for p in t.get("pending_after", [])}
    again = [p["num"] for p in proposals if _norm(p["command"]) in earlier_pending]
    if again:
        f["reproposed_pending"] = again
    asked = {_norm(q) for t in history for q in t.get("questions", [])}
    rep = [q for q in questions if _norm(q) in asked]
    if rep:
        f["repeated_question"] = rep
    if entry.get("searches"):
        f["search_requests"] = len(entry["searches"])
    if entry.get("research"):
        f["research_requests"] = len(entry["research"])
    return f
