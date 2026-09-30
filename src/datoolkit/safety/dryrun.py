"""Rehearsal variants of state-changing commands.

Where a tool has a dry-run mode, the technician is offered it as a separate read-only queue
item before the real command. This is a lookup, not an analysis: unknown commands get None.
"""

from __future__ import annotations

import re

_I = re.IGNORECASE

# (pattern, function producing the dry-run command, description)
_RULES: list[tuple[re.Pattern, object, str]] = [
    (re.compile(r"^(\s*(?:sudo\s+)?rsync\s)(?!.*\s-n\b)(?!.*--dry-run)"), lambda m, c: c.replace(m.group(1), m.group(1) + "-n -v ", 1), "rsync dry run"),
    (re.compile(r"^(\s*(?:sudo\s+)?(apt|apt-get)\s+(\S+\s+)*(install|remove|purge|upgrade|dist-upgrade|autoremove)\b)(?!.*\s-s\b)(?!.*--simulate)"),
     lambda m, c: c.replace(m.group(1), m.group(1) + " -s", 1), "apt simulation"),
    (re.compile(r"^(\s*(?:sudo\s+)?(dnf|yum)\s+(install|remove|upgrade|update)\b)"), lambda m, c: c.replace(m.group(1), m.group(1) + " --assumeno", 1), "dnf/yum with --assumeno"),
    (re.compile(r"^(\s*(?:sudo\s+)?pacman\s+-S)"), lambda m, c: c.replace(m.group(1), m.group(1) + "p", 1), "pacman print-only"),
    (re.compile(r"^(\s*(?:sudo\s+)?pip3?\s+install\b)(?!.*--dry-run)"), lambda m, c: c.replace(m.group(1), m.group(1) + " --dry-run", 1), "pip dry run"),
    (re.compile(r"^(\s*terraform\s+)apply\b"), lambda m, c: re.sub(r"\bapply\b", "plan", c, count=1), "terraform plan"),
    (re.compile(r"^(\s*kubectl\s+(apply|delete|create|patch|replace)\b)(?!.*--dry-run)"), lambda m, c: c + " --dry-run=server", "kubectl server-side dry run"),
    (re.compile(r"^(\s*(?:sudo\s+)?ansible-playbook\b)(?!.*--check)"), lambda m, c: c + " --check --diff", "ansible check mode"),
    (re.compile(r"^(\s*(?:sudo\s+)?sed\s+(-\S+\s+)*-i\S*\s)"), lambda m, c: re.sub(r"\s-i\S*(?=\s)", " ", c, count=1) + " | diff - " + _last_arg(c), "show the edit as a diff"),
    (re.compile(r"^(\s*(?:sudo\s+)?rm\s+(-\S+\s+)*)(\S.*)$"), lambda m, c: "ls -ld " + m.group(3), "list what would be deleted"),
    (re.compile(r"^(\s*(?:sudo\s+)?find\b.*)(\s-delete\b|\s-exec\s+rm\b.*)$"), lambda m, c: m.group(1), "find without -delete"),
    (re.compile(r"^(\s*(?:sudo\s+)?(iptables|ip6tables|nft|ufw|firewall-cmd)\b)"), lambda m, c: _fw_backup(m.group(2)), "save the current firewall first"),
    (re.compile(r"^(\s*(?:sudo\s+)?(systemctl|service)\s)"), lambda m, c: "systemctl list-dependencies --reverse " + _svc(c) + " --no-pager", "what depends on this service"),
    (re.compile(r"^(\s*(?:sudo\s+)?(cp|mv)\s+(-\S+\s+)*)(.+)$"), lambda m, c: "ls -ld " + m.group(4), "check source and destination"),
    (re.compile(r"^(\s*(?:sudo\s+)?(chmod|chown|chgrp)\s+(-R\s+)?\S+\s+)(.+)$"), lambda m, c: "ls -ld " + m.group(4), "current permissions"),
    (re.compile(r"^(\s*(?:sudo\s+)?crontab\s+-r\b)"), lambda m, c: "crontab -l", "list the crontab first"),
    (re.compile(r"^(\s*(?:sudo\s+)?(umount|swapoff)\s)"), lambda m, c: "lsof +f -- " + _last_arg(c) + " | head -20", "who is using it"),
    (re.compile(r"^(\s*(?:sudo\s+)?docker\s+(rm|rmi|stop|kill|restart)\b)"), lambda m, c: "docker ps -a --filter name=" + _last_arg(c), "which container matches"),
    (re.compile(r"^(\s*git\s+(clean|reset|checkout|push|rebase|merge)\b)"), lambda m, c: c + (" -n" if " clean" in c else "") if " clean" in c else "git status --short && git log --oneline -5", "check the tree first"),
    (re.compile(r"^(\s*(?:sudo\s+)?(write\s+mem|copy\s+run))", _I), lambda m, c: "show archive config differences", "show pending config differences"),
    # PowerShell: every state-changing cmdlet with -WhatIf support
    (re.compile(r"\b(Remove|Set|New|Stop|Restart|Start|Disable|Enable|Rename|Move|Copy|Clear|Uninstall|Install|Reset|Add|Update)-[A-Za-z]+\b(?!.*-WhatIf)", _I),
     lambda m, c: c + " -WhatIf", "PowerShell -WhatIf"),
    (re.compile(r"\breg\s+(delete|add)\b", _I), lambda m, c: re.sub(r"\breg\s+(delete|add)\b", "reg query", c, count=1, flags=_I), "query the key first"),
    (re.compile(r"\bnet\s+stop\s+(\S+)", _I), lambda m, c: f"sc queryex {m.group(1)} && sc enumdepend {m.group(1)}", "service state and dependents"),
]


def _last_arg(c: str) -> str:
    parts = c.strip().split()
    return parts[-1] if parts else "."


def _svc(c: str) -> str:
    words = [w for w in c.split() if not w.startswith("-") and w not in ("sudo", "systemctl", "service")]
    return words[-1] if words else ""


def _fw_backup(tool: str) -> str:
    return {"iptables": "iptables-save > /tmp/iptables.before && iptables -S | head -50",
            "ip6tables": "ip6tables-save > /tmp/ip6tables.before && ip6tables -S | head -50",
            "nft": "nft list ruleset > /tmp/nft.before && wc -l /tmp/nft.before",
            "ufw": "ufw status verbose",
            "firewall-cmd": "firewall-cmd --list-all"}[tool]


def dry_run(command: str) -> tuple[str, str] | None:
    """(rehearsal command, description) for a state-changing command, or None."""
    for pat, fn, desc in _RULES:
        m = pat.search(command)
        if m:
            try:
                out = fn(m, command)
            except Exception:  # noqa: BLE001 - a rule that can't parse the command offers nothing
                continue
            if out and out.strip() != command.strip():
                return out, desc
    return None
