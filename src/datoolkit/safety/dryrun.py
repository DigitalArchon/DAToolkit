"""Rehearsal variants of state-changing commands.

Where a tool has a dry-run mode, the technician is offered it as a separate read-only queue
item before the real command. This is a lookup, not an analysis: unknown commands get None.

A rehearsal is labelled read-only, so it must be one by construction. Most rules keep the
command's own text and change a flag, which rehearses only the first command of a line: in
`mv a b; mv c d` or `git clean -fdx && make` the rest would run for real. So those are offered
only for a single simple command; for a chained line, only the rehearsals that keep none of its
text (or check what they take from it) are.
"""

from __future__ import annotations

import re

from .risk import _quoted, classify

_I = re.IGNORECASE
# shell syntax that runs (or feeds) another command, or writes a file, outside quotes
_CHAINED = re.compile(r"[;&|\n`<>]|\$\(")

# (pattern, function producing the dry-run command, description)
_RULES: list[tuple[re.Pattern, object, str]] = [
    (re.compile(r"^(\s*(?:sudo\s+)?rsync\s)(?!.*\s-n\b)(?!.*--dry-run)"), lambda m, c: c.replace(m.group(1), m.group(1) + "-n -v ", 1), "rsync dry run"),
    (re.compile(r"^(\s*(?:sudo\s+)?(apt|apt-get)\s+(\S+\s+)*(install|remove|purge|upgrade|dist-upgrade|autoremove)\b)(?!.*\s-s\b)(?!.*--simulate)"),
     lambda m, c: c.replace(m.group(1), m.group(1) + " -s", 1), "apt simulation"),
    (re.compile(r"^(\s*(?:sudo\s+)?(dnf|yum)\s+(install|remove|upgrade|update)\b)"), lambda m, c: c.replace(m.group(1), m.group(1) + " --assumeno", 1), "dnf/yum with --assumeno"),
    (re.compile(r"^(\s*(?:sudo\s+)?pacman\s+-S)"), lambda m, c: c.replace(m.group(1), m.group(1) + "p", 1), "pacman print-only"),
    (re.compile(r"^(\s*(?:sudo\s+)?pip3?\s+install\b)(?!.*--dry-run)"), lambda m, c: c.replace(m.group(1), m.group(1) + " --dry-run", 1), "pip dry run"),
    (re.compile(r"^(\s*terraform\s+)apply\b"), lambda m, c: re.sub(r"\bapply\b", "plan", c, count=1), "terraform plan"),
    # the flags go straight after the verb: on the end they could follow `--` and be passed on
    (re.compile(r"^(\s*kubectl\s+(apply|delete|create|patch|replace)\b)(?!.*--dry-run)"),
     lambda m, c: c.replace(m.group(1), m.group(1) + " --dry-run=server", 1), "kubectl server-side dry run"),
    (re.compile(r"^(\s*(?:sudo\s+)?ansible-playbook\b)(?!.*--check)"),
     lambda m, c: c.replace(m.group(1), m.group(1) + " --check --diff", 1), "ansible check mode"),
    (re.compile(r"^(\s*(?:sudo\s+)?sed\s+(-\S+\s+)*-i\S*\s)"), lambda m, c: re.sub(r"\s-i\S*(?=\s)", " ", c, count=1) + " | diff - " + _last_arg(c), "show the edit as a diff"),
    (re.compile(r"^(\s*(?:sudo\s+)?rm\s+(-\S+\s+)*)(\S.*)$"), lambda m, c: "ls -ld " + m.group(3), "list what would be deleted"),
    (re.compile(r"^(\s*(?:sudo\s+)?find\b.*)(\s-delete\b|\s-exec\s+rm\b.*)$"), lambda m, c: m.group(1), "find without -delete"),
    (re.compile(r"^(\s*(?:sudo\s+)?(iptables|ip6tables|nft|ufw|firewall-cmd)\b)"), lambda m, c: _fw_backup(m.group(2)), "save the current firewall first"),
    (re.compile(r"^(\s*(?:sudo\s+)?(systemctl|service)\s)"), lambda m, c: _svc_deps(c), "what depends on this service"),
    (re.compile(r"^(\s*(?:sudo\s+)?(cp|mv)\s+(-\S+\s+)*)(.+)$"), lambda m, c: "ls -ld " + _no_flags(m.group(4)), "check source and destination"),
    (re.compile(r"^(\s*(?:sudo\s+)?(chmod|chown|chgrp)\s+(-R\s+)?\S+\s+)(.+)$"), lambda m, c: "ls -ld " + m.group(4), "current permissions"),
    (re.compile(r"^(\s*(?:sudo\s+)?crontab\s+-r\b)"), lambda m, c: "crontab -l", "list the crontab first"),
    (re.compile(r"^(\s*(?:sudo\s+)?(umount|swapoff)\s)"), lambda m, c: "lsof +f -- " + _last_arg(c) + " | head -20", "who is using it"),
    (re.compile(r"^(\s*(?:sudo\s+)?docker\s+(rm|rmi|stop|kill|restart)\b)"), lambda m, c: "docker ps -a --filter name=" + _last_arg(c), "which container matches"),
    (re.compile(r"^(\s*git\s+clean\b)"), lambda m, c: c.replace(m.group(1), m.group(1) + " -n", 1), "git clean dry run"),
    (re.compile(r"^(\s*git\s+(reset|checkout|push|rebase|merge)\b)"), lambda m, c: "git status --short && git log --oneline -5",
     "check the tree first"),
    (re.compile(r"^(\s*(?:sudo\s+)?(write\s+mem|copy\s+run))", _I), lambda m, c: "show archive config differences", "show pending config differences"),
    # PowerShell: every state-changing cmdlet with -WhatIf support
    (re.compile(r"\b(Remove|Set|New|Stop|Restart|Start|Disable|Enable|Rename|Move|Copy|Clear|Uninstall|Install|Reset|Add|Update)-[A-Za-z]+\b(?!.*-WhatIf)", _I),
     lambda m, c: _whatif(m, c), "PowerShell -WhatIf"),
    (re.compile(r"\breg\s+(delete|add)\b", _I), lambda m, c: re.sub(r"\breg\s+(delete|add)\b", "reg query", c, count=1, flags=_I), "query the key first"),
    (re.compile(r"\bnet\s+stop\s+(\S+)", _I), lambda m, c: _sc_deps(m.group(1)), "service state and dependents"),
]

# rehearsals that keep none of the command's text, or check the one word they take from it:
# offered for a chained line too (`net stop svc && net start svc`)
_WHOLE_LINE = {"save the current firewall first", "what depends on this service", "list the crontab first",
               "check the tree first", "show pending config differences", "PowerShell -WhatIf",
               "service state and dependents"}


def _simple(command: str) -> bool:
    """One command, nothing chained, piped or redirected (quoted and escaped text aside)."""
    q = _quoted(command)
    return q is not None and not _CHAINED.search(re.sub(r"\\.", "  ", q[0]))


def _word(w: str) -> str | None:
    """A name taken from the command for a rehearsal, or None if it carries shell syntax."""
    return None if re.search(r"[^\w.@:+-]", w) else w


def _sc_deps(name: str) -> str | None:
    name = _word(name)
    return f"sc queryex {name} && sc enumdepend {name}" if name else None


def _last_arg(c: str) -> str:
    parts = c.strip().split()
    return parts[-1] if parts else "."


def _no_flags(args: str) -> str:
    return " ".join(w for w in args.split() if not w.startswith("-"))


def _svc_deps(c: str) -> str | None:
    """Reverse dependencies of the unit a systemctl/service command acts on. `service NAME
    ACTION` names it first, `systemctl VERB UNIT` second; `systemctl daemon-reload`, `reboot`
    and the like name none, so nothing is offered."""
    words = [w for w in c.split() if not w.startswith("-") and w != "sudo"]
    if words and words[0] == "service":
        unit = words[1] if len(words) > 2 else ""
    else:
        unit = words[2] if len(words) > 2 else ""
    user = " --user" if "--user" in c.split() else ""
    unit = _word(unit)
    return f"systemctl{user} list-dependencies --reverse {unit} --no-pager" if unit else None


def _whatif(m: re.Match, c: str) -> str | None:
    """-WhatIf goes on the end of the line, so only when nothing follows the cmdlet: after a
    pipe, `;` or `&&` it would land on a different command. And whatever comes before it runs
    as written, so only read-only pipeline stages may (`Get-ChildItem *.tmp | Remove-Item`)."""
    if re.search(r"[;|\n]|&&", c[m.end():]):
        return None
    before = c[:m.start()]
    if before.strip():
        if re.search(r"[;&\n`]|\$\(", before) or not before.rstrip().endswith("|"):
            return None
        if any(classify(stage)[0] != "read_only" for stage in before.split("|") if stage.strip()):
            return None
    return c + " -WhatIf"


def _fw_backup(tool: str) -> str:
    return {"iptables": "iptables-save > /tmp/iptables.before && iptables -S | head -50",
            "ip6tables": "ip6tables-save > /tmp/ip6tables.before && ip6tables -S | head -50",
            "nft": "nft list ruleset > /tmp/nft.before && wc -l /tmp/nft.before",
            "ufw": "ufw status verbose",
            "firewall-cmd": "firewall-cmd --list-all"}[tool]


def dry_run(command: str) -> tuple[str, str] | None:
    """(rehearsal command, description) for a state-changing command, or None."""
    simple = _simple(command)
    for pat, fn, desc in _RULES:
        if not simple and desc not in _WHOLE_LINE:
            continue
        m = pat.search(command)
        if m:
            try:
                out = fn(m, command)
            except Exception:  # noqa: BLE001 - a rule that can't parse the command offers nothing
                continue
            if out and out.strip() != command.strip():
                return out, desc
    return None
