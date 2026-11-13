"""Watch items: run a read-only command repeatedly for a bounded time, and collapse the
captured output to the iterations that changed."""

from __future__ import annotations

import re

from .risk import _quoted

STAMP = "=== WATCH "   # printed by the wrapper before every iteration
_STAMP_RE = re.compile(r"^=== WATCH (\d+)/(\d+) (\S+) ===\s*$", re.M)


def bounds(interval, count) -> tuple[int, int]:
    """(interval, count) as a watch runs them: at least a second apart, 1 to 720 times."""
    return max(1, int(interval)), max(1, min(int(count), 720))


def _embeddable(command: str) -> str:
    """The command ready to sit in the middle of a one-line loop (the WinRM console runs a line
    at a time): a trailing comment would comment out the loop's end, leaving the shell waiting
    for it, and a trailing `;` doubled up is a syntax error."""
    text = command.strip()
    q = _quoted(text)
    if q:
        m = re.search(r"(?:^|\s)#", q[0])            # a comment starts a word, outside quotes
        if m:
            text = text[:m.start()].rstrip()
    while text.endswith(";") and not text.endswith("\\;"):       # (find's `-exec ... \;` ends in one that belongs to it)
        text = text[:-1].rstrip()
    return text


def wrap(command: str, interval: int, count: int, shell: str) -> str:
    """Wrap a single-line command in a bounded loop for the session's shell family."""
    interval, count = bounds(interval, count)
    command = _embeddable(command)
    if not command:
        raise ValueError("There is no command to watch.")
    if shell == "powershell":
        return (f"1..{count} | ForEach-Object {{ Write-Output ('{STAMP}' + $_ + '/{count} ' + (Get-Date -Format HH:mm:ss) + ' ==='); "
                f"{command}; if ($_ -lt {count}) {{ Start-Sleep {interval} }} }}")
    sep = " " if command.endswith("&") and not command.endswith("&&") else "; "    # `cmd &` ends a command itself
    return (f"for i in $(seq 1 {count}); do printf '{STAMP}%s/{count} %s ===\\n' \"$i\" \"$(date +%H:%M:%S)\"; "
            f"{command}{sep}[ \"$i\" -lt {count} ] && sleep {interval}; done")


def collapse(text: str) -> tuple[str, int]:
    """Drop iterations whose output equals the previous one. Returns (text, dropped_count)."""
    stamps = list(_STAMP_RE.finditer(text))
    if len(stamps) < 2:
        return text, 0
    head = text[:stamps[0].start()]
    blocks = []
    for i, m in enumerate(stamps):
        end = stamps[i + 1].start() if i + 1 < len(stamps) else len(text)
        blocks.append((m.group(0).strip(), text[m.end():end].strip("\n")))
    out = [head.rstrip("\n")] if head.strip() else []
    prev = None
    dropped = 0
    run_last = None   # stamp of the last iteration dropped in the current unchanged run
    for stamp, body in blocks:
        if body == prev:
            dropped += 1
            run_last = stamp
            continue
        if run_last is not None:
            out.append(f"[... unchanged through {run_last} ...]")
            run_last = None
        out.append(stamp)
        if body:
            out.append(body)
        prev = body
    if run_last is not None:
        out.append(f"[... unchanged through {run_last} ...]")
    return "\n".join(out), dropped
