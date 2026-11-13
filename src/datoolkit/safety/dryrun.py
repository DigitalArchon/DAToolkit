"""Rehearsal variants of state-changing commands.

Where a tool has a dry-run mode, or there is something to look at before the change, the
technician is offered it as a separate queue item before the real command. This is a lookup,
not an analysis: unknown commands get None.

A rehearsal must change nothing, and it is rated like any other command (Queue._classify): until
Nov 2026 it was labelled read-only for being a rehearsal, and some were not (`-WhatIf` on the end
of a line after a comment ran the real command). So each rule here is one of three kinds:

- new text that only reads, taking nothing from the command but a name that has been checked
  (`crontab -l`, `sc.exe queryex Spooler`): marked `reader`, the one kind the queue takes at its word;
- the command's own text made into a reader (`ls -ld` of what `rm` names, `find` without -delete,
  `apt -s`, `rsync -n`): offered only if the allow-list (risk.reads_only) confirms the result;
- the command with a flag that asks the tool itself not to act (-WhatIf, --check, --dry-run=server):
  nothing here can confirm the tool obeys, so it is offered only where the flag can't be lost or
  overridden, and it keeps the rating of the command it rehearses.

Most rules keep the command's own text and change a flag, which rehearses only the first command of
a line: in `mv a b; mv c d` or `git clean -fdx && make` the rest would run for real. So those are
offered only for a single simple command; for a chained line, only the rehearsals that keep none of
its text (or check what they take from it) are.
"""

from __future__ import annotations

import re

from .risk import _quoted, reads_only

_I = re.IGNORECASE
_MAX = 4000                  # a command longer than this gets no rehearsal: none of these rules is for a script
# shell syntax that runs (or feeds) another command, or writes a file, outside quotes
_CHAINED = re.compile(r"[;&|\n`<>]|\$\(")
_SUDO = r"(\s*(?:sudo\s+)?)"


class Rehearsal(tuple):
    """(command, description) of a rehearsal. `reader` is set for the ones made of new text that only
    reads: the rest are for the risk rules to rate."""

    reader: bool

    def __new__(cls, command: str, description: str, reader: bool = False):
        self = super().__new__(cls, (command, description))
        self.reader = reader
        return self


def _simple(command: str) -> bool:
    """One command, nothing chained, piped or redirected (quoted and escaped text aside)."""
    q = _quoted(command)
    return q is not None and not _CHAINED.search(re.sub(r"\\.", "  ", q[0]))


def _word(w: str) -> str | None:
    """A name taken from the command for a rehearsal, or None if it carries shell syntax."""
    return None if not w or w.startswith("-") or re.search(r"[^\w.@:+-]", w) else w


def _reader(text: str) -> str | None:
    """`text` if the allow-list confirms that it only reads."""
    return text if reads_only(text) else None


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


def _sc_deps(name: str) -> str | None:
    """The service's state and what depends on it. sc.exe by its full name (`sc` alone is Set-Content
    to Windows PowerShell), one command a line: cmd.exe and PowerShell 5.1 share no separator."""
    quoted = re.fullmatch(r'"([\w .-]+)"', name)
    name = f'"{quoted.group(1)}"' if quoted else _word(name)
    return f"sc.exe queryex {name}\nsc.exe enumdepend {name}" if name else None


def _crontab(m: re.Match, c: str) -> str | None:
    """List the crontab that -r would remove: the same user's (-u), as the same user (sudo)."""
    user = re.search(r"\s-u\s*(\S+)", c)
    if user and not _word(user.group(1)):
        return None
    return m.group(1).lstrip() + "crontab -l" + (f" -u {user.group(1)}" if user else "")


def _fw_backup(m: re.Match) -> str:
    """Save the rules before they are changed, as root if the change is made as root, to a file of a
    new name each time. Writing that file is a change itself, a small one: these are rated modifying."""
    sudo, tool = "sudo " if m.group(1).strip() else "", m.group(2)
    if tool in ("iptables", "ip6tables"):
        return f'f=$(mktemp /tmp/{tool}.XXXXXX) && {sudo}{tool}-save > "$f" && echo "saved to $f" && {sudo}{tool} -S | head -50'
    return f'f=$(mktemp /tmp/nft.XXXXXX) && {sudo}nft list ruleset > "$f" && echo "saved to $f" && wc -l "$f"'


def _sed_diff(m: re.Match, c: str) -> str | None:
    """The edit as a diff: sed without -i, compared with the one file it would have changed."""
    plain = re.sub(r"\s(?:-([nErsuz]*)i\S*|--in-place(?:=\S*)?)(?=\s)", lambda x: f" -{x.group(1)}" if x.group(1) else " ", c, count=1)
    return _reader(plain + " | diff - " + _last_arg(c))


def _find_only(m: re.Match, c: str) -> str | None:
    rest = m.group(1)
    if re.search(r"\s-(?:delete|exec|execdir|ok|okdir|fprint0?|fprintf|fls)\b", rest):
        return None                          # another action is left that would still run
    return _reader(rest)


# Cmdlets known to honour -WhatIf (they declare SupportsShouldProcess): PowerShell's own for items, content,
# services, processes and the computer, and the modules for networking, shares, DNS, DHCP, scheduled tasks,
# local accounts, Active Directory, Hyper-V, storage and BitLocker. A cmdlet not named here may ignore the
# switch and act (the DISM ones do), and a script named like a cmdlet (stop-all.sh) certainly would.
_WHATIF = re.compile(
    r"(?:Remove|Set|Clear|New|Copy|Move|Rename)-Item(?:Property)?|(?:Set|Add|Clear)-Content"
    r"|(?:Stop|Start|Restart|Suspend|Resume|Set|New|Remove)-Service|Stop-Process|(?:Restart|Stop|Rename|Add|Remove)-Computer"
    r"|Set-(?:Date|TimeZone|ExecutionPolicy|Acl)|(?:Clear|Remove|Limit)-EventLog|Clear-RecycleBin|(?:Expand|Compress)-Archive"
    r"|(?:Install|Uninstall)-(?:WindowsFeature|Package|Module)|Update-Module|Remove-AppxPackage"
    r"|(?:New|Set|Remove|Enable|Disable|Restart|Rename)-Net[A-Za-z0-9]+|(?:New|Set|Remove|Grant|Revoke|Block|Unblock)-Smb[A-Za-z]+"
    r"|(?:Add|Set|Remove|Clear)-Dns(?:Client|Server)[A-Za-z0-9]+|(?:Add|Set|Remove)-DhcpServer[A-Za-z0-9]+"
    r"|(?:Register|Unregister|Enable|Disable|Start|Stop)-ScheduledTask|(?:Add|Remove|Set)-Printer[A-Za-z]*|(?:Set|Add|Remove)-MpPreference"
    r"|(?:New|Set|Remove|Enable|Disable|Rename)-LocalUser|(?:New|Set|Remove|Rename)-LocalGroup|(?:Add|Remove)-LocalGroupMember"
    r"|(?:New|Set|Remove|Move|Rename|Restore|Add)-AD[A-Za-z]+|(?:Enable|Disable|Unlock)-ADAccount"
    r"|(?:Start|Stop|Restart|Remove|Suspend|Resume|Save|Checkpoint|Set|New)-VM[A-Za-z]*|Restore-VMSnapshot|(?:Dismount|Mount)-VHD"
    r"|Format-Volume|(?:Clear|Initialize|Set)-Disk|(?:New|Remove|Resize|Set)-Partition|(?:Enable|Disable)-BitLocker"
    r"|(?:Enable|Disable)-PSRemoting|Invoke-CimMethod|Invoke-WmiMethod|Remove-CimInstance|Set-CimInstance", _I)
# what -WhatIf does not reach, or what could take it away: a comment, another command or a subexpression
# (which run as written), a script block, a redirect, a line continued, PowerShell's stop-parsing token
_UNREHEARSED = re.compile(r"[#;&\n`<>(){}]|--%")


def _whatif(command: str) -> str | None:
    """`Cmdlet -WhatIf args`, the switch straight after the cmdlet's name: at the end of the line it can
    fall into a comment or after the stop-parsing token. Only the last command of a pipeline is rehearsed,
    and what feeds it runs as written, so each earlier stage must be a known reader
    (`Get-ChildItem *.tmp | Remove-Item`)."""
    q = _quoted(command)
    if q is None or '\\"' in command or _UNREHEARSED.search(q[0]) or re.search(r"(?<!\S)-WhatIf\b", q[0], _I):
        return None
    cut = q[0].rfind("|") + 1                # quotes are blanked in q[0], at the same positions
    m = re.match(r"\s*([A-Za-z]+-[A-Za-z][A-Za-z0-9]*)(?=\s|$)", command[cut:])
    if not m or not _WHATIF.fullmatch(m.group(1)):
        return None
    if cut and not all(reads_only(stage) for stage in _stages(command, q[0])[:-1]):
        return None
    return command[:cut + m.end()] + " -WhatIf" + command[cut + m.end():]


def _stages(command: str, blanked: str) -> list[str]:
    out, start = [], 0
    for m in re.finditer(r"\|", blanked):
        out.append(command[start:m.start()])
        start = m.end()
    return out + [command[start:]]


def _kubectl(m: re.Match, c: str) -> str | None:
    if re.search(r"\s--(?:dry-run|raw)\b", c):
        return None                          # --raw sends the request as it is: no dry run reaches it
    return c[:m.end(1)] + " --dry-run=server" + c[m.end(1):]


def _reg_query(m: re.Match, c: str) -> str | None:
    """The key (and the value named with /v) as they are now."""
    value = re.search(r'\s(/v\s+(?:"[^"]*"|\S+)|/ve)(?=\s|$)', c, _I)
    return _reader(f"reg query {m.group(2)}" + (f" {value.group(1)}" if value else ""))


# (pattern, function producing the dry-run command, description, whether it is new text that only reads)
_RULES: list[tuple[re.Pattern, object, str, bool]] = [
    (re.compile(r"^(\s*(?:sudo\s+)?rsync\s)(?!.*\s(?:-n\b|--dry-run|--no-|--daemon|--server))"),
     lambda m, c: c.replace(m.group(1), m.group(1) + "-n -v ", 1), "rsync dry run", False),
    (re.compile(r"^(\s*(?:sudo\s+)?(apt|apt-get)\s+(\S+\s+)*?(install|remove|purge|upgrade|dist-upgrade|full-upgrade|autoremove)\b)(?!.*\s(?:-s\b|--simulate|-o|-c))"),
     lambda m, c: _reader(c.replace(m.group(1), m.group(1) + " -s", 1)), "apt simulation", False),
    # dnf asks before it acts and takes "no" from --assumeno; a -y on the line would answer first
    (re.compile(r"^(\s*(?:sudo\s+)?(dnf|yum)\s+(install|remove|upgrade|update)\b)"),
     lambda m, c: re.sub(r"\s(?:-y|--assumeyes)(?=\s|$)", "", c.replace(m.group(1), m.group(1) + " --assumeno", 1)), "dnf/yum with --assumeno", False),
    # -Sp prints what -S would fetch; with y or c it would still refresh the databases or empty the cache
    (re.compile(r"^(\s*(?:sudo\s+)?pacman\s+-S)(?![a-zA-Z]*[ycp])"), lambda m, c: _reader(c.replace(m.group(1), m.group(1) + "p", 1).replace("sudo ", "", 1)),
     "pacman print-only", False),
    (re.compile(r"^(\s*(?:sudo\s+)?pip3?\s+install\b)(?!.*--dry-run)"), lambda m, c: c.replace(m.group(1), m.group(1) + " --dry-run", 1), "pip dry run", False),
    (re.compile(r"^(\s*(?:terraform|tofu)\s+)apply\b(?!.*\s-out)"),
     lambda m, c: re.sub(r"\s-auto-approve\b", "", re.sub(r"\bapply\b", "plan", c, count=1)), "terraform plan", False),
    # the flags go straight after the verb: on the end they could follow `--` and be passed on
    (re.compile(r"^(\s*kubectl(?:\s+(?:-n|--namespace|--context|--kubeconfig)(?:=|\s+)\S+)*\s+(apply|delete|create|patch|replace)\b)"),
     _kubectl, "kubectl server-side dry run", False),
    (re.compile(r"^(\s*(?:sudo\s+)?ansible-playbook\b)(?!.*--check)"),
     lambda m, c: c.replace(m.group(1), m.group(1) + " --check --diff", 1), "ansible check mode", False),
    (re.compile(r"^(\s*(?:sudo\s+)?sed\s+(-\S+\s+)*(?:-[a-zA-Z]*i\S*|--in-place\S*)\s)"), _sed_diff, "show the edit as a diff", False),
    (re.compile(r"^" + _SUDO + r"rm\s+((?:-\S+\s+)*)(\S.*)$"), lambda m, c: _reader(m.group(1).lstrip() + "ls -ld " + m.group(3)),
     "list what would be deleted", False),
    (re.compile(r"^(\s*(?:sudo\s+)?find\b.*?)(?:\s-delete\b|\s-exec\s+rm\b.*)\s*$"), _find_only, "find without -delete", False),
    (re.compile(r"^" + _SUDO + r"(iptables|ip6tables|nft)\b"), lambda m, c: _fw_backup(m), "save the current firewall first", False),
    (re.compile(r"^" + _SUDO + r"ufw\b"), lambda m, c: m.group(1).lstrip() + "ufw status verbose", "the firewall as it is now", True),
    (re.compile(r"^" + _SUDO + r"firewall-cmd\b"), lambda m, c: m.group(1).lstrip() + "firewall-cmd --list-all", "the firewall as it is now", True),
    (re.compile(r"^(\s*(?:sudo\s+)?(systemctl|service)\s)"), lambda m, c: _svc_deps(c), "what depends on this service", True),
    (re.compile(r"^" + _SUDO + r"(cp|mv)\s+(-\S+\s+)*(.+)$"), lambda m, c: _reader("ls -ld " + _no_flags(m.group(4))), "check source and destination", False),
    (re.compile(r"^" + _SUDO + r"(chmod|chown|chgrp)\s+(-R\s+)?\S+\s+(.+)$"), lambda m, c: _reader("ls -ld " + m.group(4)), "current permissions", False),
    (re.compile(r"^" + _SUDO + r"crontab\s+(?:-u\s*\S+\s+)?-[a-z]*r[a-z]*\b"), _crontab, "list the crontab first", True),
    (re.compile(r"^(\s*(?:sudo\s+)?(umount|swapoff)\s)"), lambda m, c: _reader("lsof +f -- " + _last_arg(c) + " | head -20"), "who is using it", False),
    (re.compile(r"^(\s*(?:sudo\s+)?docker\s+(rm|rmi|stop|kill|restart)\b)"),
     lambda m, c: "docker ps -a --filter name=" + _word(_last_arg(c)) if _word(_last_arg(c)) else None, "which container matches", True),
    (re.compile(r"^(\s*git\s+clean\b)(?!.*\s-[a-zA-Z]*[ni])"), lambda m, c: _reader(c.replace(m.group(1), m.group(1) + " -n", 1)), "git clean dry run", False),
    (re.compile(r"^(\s*git\s+(reset|checkout|push|rebase|merge)\b)"), lambda m, c: "git status --short && git log --oneline -5",
     "check the tree first", True),
    (re.compile(r"^(\s*(?:sudo\s+)?(write\s+mem|copy\s+run))", _I), lambda m, c: "show archive config differences", "show pending config differences", True),
    # PowerShell: the cmdlets known to support -WhatIf
    (re.compile(r"^(?=[^\n]*[A-Za-z]+-[A-Za-z])"), lambda m, c: _whatif(c), "PowerShell -WhatIf", False),
    (re.compile(r"^(\s*reg(?:\.exe)?\s+(?:delete|add)\s+)(\"[^\"]+\"|\S+)", _I), _reg_query, "query the key first", False),
    (re.compile(r"^\s*net1?(?:\.exe)?\s+stop\s+(\"[^\"]+\"|\S+)", _I), lambda m, c: _sc_deps(m.group(1)), "service state and dependents", True),
]

# rehearsals that keep none of the command's text, or check the one word they take from it:
# offered for a chained line too (`net stop svc && net start svc`)
_WHOLE_LINE = {"the firewall as it is now", "what depends on this service", "list the crontab first",
               "check the tree first", "show pending config differences", "service state and dependents"}


def dry_run(command: str) -> Rehearsal | None:
    """(rehearsal command, description) for a state-changing command, or None. The result's `reader`
    says whether it is new text that only reads."""
    if len(command) > _MAX:
        return None
    simple = _simple(command)
    for pat, fn, desc, reader in _RULES:
        if not simple and desc not in _WHOLE_LINE and desc != "PowerShell -WhatIf":
            continue                         # (-WhatIf reads a pipeline itself, and refuses any other chain)
        m = pat.search(command)
        if m:
            try:
                out = fn(m, command)
            except Exception:  # noqa: BLE001 - a rule that can't parse the command offers nothing
                continue
            if out and out.strip() != command.strip():
                return Rehearsal(out, desc, reader)
    return None
