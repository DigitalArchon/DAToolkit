"""Per-turn checks of how well a model follows DAToolkit's working rules. Each flag is a
behaviour the technician would notice; none of them judges the diagnosis itself."""

from __future__ import annotations

import json
import re

from datoolkit.safety.risk import LEVELS, classify

TOOL_NAMES = {"propose_commands", "update_hypotheses", "run_recipe", "ask_technician", "revise_queue", "web_search",
              "research"}

# The engine's promise and stale-reference checks (model-prompts branch), copied so every run,
# before and after that branch, is flagged the same way.
_PROMISE = re.compile(
    r"\b(I need you to|please|could you|can you|now|next)\b[^.?!\n]{0,40}\b(run|execute|try|check|paste)\b[^.?!\n]{0,40}"
    r"\b(commands?|checks?|these|the following|few more|a few|some more|the next)\b"
    r"|\b(I['’]?ve|I have|I['’]?ll|I will|let me|I['’]?m going to|I['’]?d like to|I want to|we['’]?ll|we will|we need to)\s+(now\s+|just\s+|also\s+|then\s+|first\s+)?"
    r"(queue|queued|propose|proposed|add|added|give you|send you|prepare|line up|make|apply|enable|check|look|start by)\b"
    r"|\b(I['’]?ll|I will|let me|I['’]?m going to|I need to|now I need to)\b[^.?!\n]{0,40}\b(pull|grab|gather|collect|"
    r"fetch|see|ask for|check|confirm|verify|inspect|queue)\b"
    r"|\blet['’]?s\s+(now\s+|just\s+|also\s+|then\s+|first\s+)?(get|gather|grab|redo|rerun|run|try|test|verify|confirm|"
    r"check|queue|start|inspect|pull|look at|update the hypothesis board and queue)\b"
    r"|^\s*(queue|run|try|execute|paste)\s+(these|this|the following|them|both)\b"
    r"|\bthe (fix|next step|change) (is|would be|will be) (to|one|a single|simple|this)\b|\bthe (fix|next step)\s*[:—–]"
    r"|\b(here (are|is)|below (are|is))\b[^.?!\n]{0,30}\b(commands?|checks?|steps?)\b", re.I | re.M)
_ALREADY_QUEUED = re.compile(r"#\d+|\b(in|from) (the|your) queue\b|\balready queued\b|\bpending\b|\bqueued (above|earlier)\b",
                             re.I)


# an offer that waits on the technician is not a promise: "If you'd like that, I'll propose a drop
# rule" (Qwen 3.8 Max), "Once you've swapped the cable, I'll queue the checks"
_CONDITIONAL = re.compile(r"^\s*(if|once|when|after|as soon as)\b|\bif (you|that|this|it|they)\b", re.I)


def promises_commands(text: str) -> bool:
    """True when the end of the message announces commands or a change that should have been
    queued. Each sentence of the last few lines is judged on its own."""
    lines = [line for line in re.sub(r"[*_`]+", "", text).strip().splitlines() if line.strip()][-4:]
    if lines and lines[-1].rstrip().endswith(":"):        # "Run these:" with nothing after it
        return True
    for line in lines:
        for sentence in re.split(r"(?<=[.?!])\s+", line.strip()):
            if _PROMISE.search(sentence) and not _ALREADY_QUEUED.search(sentence) and not _CONDITIONAL.search(sentence):
                return True
    return False


_RUN_REF = re.compile(r"\b(run|re-?run|try|execute|send)\b[^.?!\n]{0,40}?((?:#\d+[\s,/&and–-]*)+)", re.I)


def stale_run_refs(text: str, pending: set[int]) -> list[int]:
    """Items the message asks the technician to run that are not pending: skipped, withdrawn
    or already run (live: GLM 5.3 asked for #10 and #11 after they had been skipped)."""
    out = []
    for m in _RUN_REF.finditer(text):
        for n in re.findall(r"#(\d+)", m.group(2)):
            if int(n) not in pending and int(n) not in out:
                out.append(int(n))
    return out


# words that start a shell/CLI command, to tell a command written in prose from a mere name
CMD_WORDS = re.compile(
    r"^\s*(sudo\s+)?(ls|cat|grep|awk|sed|df|du|ip|ss|ping|ethtool|journalctl|systemctl|ffprobe|ffmpeg|mount|findmnt|"
    r"iperf3?|dmesg|lsblk|smartctl|top|free|uptime|nload|ifstat|sar|iostat|docker|tail|head|find|w32tm|Get-\w+|"
    r"Set-\w+|Test-\w+|Restart-\w+|nltest|klist|dcdiag|repadmin|net|Resolve-DnsName|/[a-z]+)\b", re.I)

# a model's native tool-call syntax leaking into the visible message (DeepSeek's DSML, Kimi's
# sections, Qwen/Hermes <tool_call>, Mistral [TOOL_CALLS], <function=...>)
TOOL_MARKUP = re.compile(r"<｜DSML｜|<\|tool_calls?_section_begin\|>|<\|tool_call_begin\|>|</?tool_call>|\[TOOL_CALLS\]|"
                         r"<function=|<\|?(begin|end)_of_tool|<invoke name=", re.I)

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
    if TOOL_MARKUP.search(text):
        f["tool_markup_in_text"] = TOOL_MARKUP.search(text).group(0)
    if not proposals and not questions and not entry.get("withdrawn"):
        if promises_commands(text):
            f["promise_without_call"] = " / ".join(text.strip().splitlines()[-2:])[-200:]
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
    pending = {p["num"] for t in history[-1:] for p in t.get("pending_after", [])} | {p["num"] for p in proposals}
    stale = stale_run_refs(text, pending) if history else []
    if stale and not proposals:
        f["stale_reference"] = stale
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
