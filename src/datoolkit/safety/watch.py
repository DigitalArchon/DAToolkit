"""Watch items: run a read-only command repeatedly for a bounded time, and collapse the
captured output to the iterations that changed."""

from __future__ import annotations

import re

STAMP = "=== WATCH "   # printed by the wrapper before every iteration
_STAMP_RE = re.compile(r"^=== WATCH (\d+)/(\d+) (\S+) ===\s*$", re.M)


def wrap(command: str, interval: int, count: int, shell: str) -> str:
    """Wrap a single-line command in a bounded loop for the session's shell family."""
    interval, count = max(1, int(interval)), max(1, min(int(count), 720))
    if shell == "powershell":
        return (f"1..{count} | ForEach-Object {{ Write-Output ('{STAMP}' + $_ + '/{count} ' + (Get-Date -Format HH:mm:ss) + ' ==='); "
                f"{command}; if ($_ -lt {count}) {{ Start-Sleep {interval} }} }}")
    return (f"for i in $(seq 1 {count}); do printf '{STAMP}%s/{count} %s ===\\n' \"$i\" \"$(date +%H:%M:%S)\"; "
            f"{command}; [ \"$i\" -lt {count} ] && sleep {interval}; done")


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
