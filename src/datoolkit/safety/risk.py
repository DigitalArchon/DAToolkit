"""Local risk classifier.

The model labels each proposed command with a risk level, but a model's self-assessment
is not trustworthy (it can be wrong, or steered by prompt injection in command output).
These rules can only *raise* the level the model gave, never lower it.
"""

from __future__ import annotations

import re

LEVELS = ("read_only", "modifying", "disruptive")

_I = re.IGNORECASE

# Position where a new command starts: line start, after a separator, or after sudo.
CMD = r"(?:^|[;&|(]\s*|\bsudo\s+(?:-\S+\s+)*)"

# Each entry: (pattern, reason). Patterns are matched against the whole command text.
DISRUPTIVE: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+", _I), "recursive/forced delete"),
    (re.compile(r"\b(mkfs(\.\w+)?|wipefs|fdisk|sfdisk|parted|sgdisk|gdisk|blkdiscard)\b"), "disk/partition tool"),
    (re.compile(r"\bdd\b.*\bof=", _I), "dd writing to a target"),
    (re.compile(r">\s*/dev/(sd|nvme|vd|hd|mmcblk)"), "write to block device"),
    (re.compile(CMD + r"(shutdown|reboot|poweroff|halt)\b", re.M), "shutdown/reboot"),
    (re.compile(CMD + r"(telinit|init)\s+[06]\b", re.M), "runlevel change"),
    (re.compile(r"\bsystemctl\s+(\S+\s+)*(stop|restart|disable|mask|kill|isolate|reboot|poweroff|halt)\b"), "service stop/restart"),
    (re.compile(r"\bservice\s+\S+\s+(stop|restart)\b"), "service stop/restart"),
    (re.compile(r"\b(kill\s+-9|killall|pkill)\b"), "process kill"),
    (re.compile(r"\b(iptables|ip6tables)\s+(-[a-z]*\s+)*-(F|X|P)\b"), "firewall flush/policy"),
    (re.compile(r"\bnft\s+(flush|delete)\b"), "firewall flush"),
    (re.compile(r"\bufw\s+(disable|reset)\b"), "firewall disable"),
    (re.compile(r"\b(chmod|chown)\s+-R\b.*\s/(\s|$)"), "recursive permission change on /"),
    (re.compile(r"\b(userdel|deluser)\b"), "user deletion"),
    (re.compile(r"\bcrontab\s+-r\b"), "crontab removal"),
    (re.compile(r"\bdrop\s+(database|table|schema)\b", _I), "SQL drop"),
    (re.compile(r"\btruncate\s+table\b", _I), "SQL truncate"),
    (re.compile(r"\bip\s+link\s+set\s+\S+\s+down\b"), "interface down"),
    (re.compile(r"\bdocker\s+(system\s+prune|volume\s+(rm|prune)|rm\s+-f)\b"), "docker destructive"),
    (re.compile(r"\bkubectl\s+delete\b"), "kubernetes delete"),
    # code fetched from the network or decoded at runtime and executed: unreviewable
    (re.compile(r"\b(curl|wget|fetch|Invoke-WebRequest|iwr)\b[^|;]*\|\s*(sudo\s+)?(ba|z|k|da|)sh\b", _I), "pipe to shell"),
    (re.compile(r"\b(ba|z|k|da|)sh\s+(-c\s+)?[\"']?\s*\$\((curl|wget)\b", _I), "shell runs downloaded script"),
    (re.compile(r"\b(ba|z|k|da|)sh\s+<\s*\((curl|wget)\b", _I), "shell runs downloaded script"),
    (re.compile(r"\bbase64\s+(-d|--decode)\b[^|;]*\|\s*(sudo\s+)?(ba|z|k|da|)sh\b", _I), "decoded payload piped to shell"),
    (re.compile(r"\bshred\b"), "secure erase"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:"), "fork bomb"),
    (re.compile(r"\bhistory\s+-c\b"), "history clear"),
    (re.compile(r"\b(nc|ncat|netcat)\b.*\s-e\s", _I), "reverse shell"),
    (re.compile(r"\bchmod\s+(-R\s+)?[0-7]*777\b"), "world-writable permissions"),
    # PowerShell / Windows
    (re.compile(r"\b(Invoke-Expression|iex)\b", _I), "Invoke-Expression"),
    (re.compile(r"(?<!\S)-(EncodedCommand|enc|ec)\s+\S+", _I), "encoded PowerShell"),
    (re.compile(r"\b(Invoke-WebRequest|iwr|curl|wget|Invoke-RestMethod|irm)\b.*\|\s*(iex|Invoke-Expression)\b", _I), "download and execute"),
    (re.compile(r"\bDownloadString\s*\(", _I), "download and execute"),
    (re.compile(r"\bRemove-Item\b.*-Recurse\b", _I), "recursive delete"),
    (re.compile(r"\b(Format-Volume|Clear-Disk|Initialize-Disk|Remove-Partition)\b", _I), "disk operation"),
    (re.compile(r"\b(Stop-Computer|Restart-Computer)\b", _I), "shutdown/reboot"),
    (re.compile(r"\b(Stop-Service|Restart-Service|Stop-Process)\b", _I), "service/process stop"),
    (re.compile(r"\b(Disable-NetAdapter|Remove-NetIPAddress|Remove-NetRoute)\b", _I), "network change"),
    (re.compile(r"\bRemove-AD\w+\b", _I), "Active Directory removal"),
    (re.compile(r"\b(Clear-EventLog|wevtutil\s+cl)\b", _I), "event log clear"),
    (re.compile(r"\bnet\s+stop\b", _I), "service stop"),
    (re.compile(r"\b(bcdedit|diskpart)\b", _I), "boot/disk configuration"),
    (re.compile(r"\breg\s+delete\b", _I), "registry delete"),
    (re.compile(r"\btaskkill\b", _I), "process kill"),
    # Network devices (Cisco IOS / NX-OS / Junos / MikroTik / FortiOS)
    (re.compile(r"^\s*reload\b", _I | re.M), "device reload"),
    (re.compile(r"\b(write\s+erase|erase\s+(startup|nvram|flash))", _I), "config erase"),
    (re.compile(r"\bformat\s+(flash|disk|bootflash|usb)\S*", _I), "storage format"),
    (re.compile(r"\bdelete\s+/force\b", _I), "forced delete"),
    (re.compile(r"\brequest\s+system\s+(reboot|halt|power-off|zeroize)\b", _I), "device reboot/zeroize"),
    (re.compile(r"/system\s+(reboot|shutdown|reset-configuration)\b", _I), "device reboot/reset"),
    (re.compile(r"\bexecute\s+(reboot|shutdown|factoryreset|formatlogdisk)\b", _I), "device reboot/reset"),
]

MODIFYING: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(apt|apt-get|yum|dnf|zypper|pacman|snap|flatpak)\s+(\S+\s+)*(install|remove|purge|upgrade|dist-upgrade|update|autoremove|-S|-R)\b"), "package change"),
    (re.compile(r"\bpip3?\s+(install|uninstall)\b"), "package change"),
    (re.compile(r"\bsystemctl\s+(\S+\s+)*(start|enable|reload|daemon-reload|edit|set-property)\b"), "service change"),
    (re.compile(r"\bsed\s+(-[a-z]*\s+)*-i"), "in-place file edit"),
    (re.compile(r"\btee\b"), "file write"),
    (re.compile(r"(?<![0-9&>=-])>{1,2}(?!&)\s*(?!/dev/null\b)[^\s|&;]"), "output redirect to file"),
    (re.compile(CMD + r"(mv|cp|rm|rmdir|chmod|chown|chgrp|mkdir|touch|ln|install|truncate)\b"), "filesystem change"),
    (re.compile(r"\b(useradd|usermod|groupadd|groupmod|passwd|chpasswd|visudo)\b"), "account change"),
    (re.compile(r"\bgit\s+(commit|push|reset|checkout|merge|rebase|pull|clean)\b"), "repository change"),
    (re.compile(r"\bdocker\s+(rm|rmi|stop|restart|kill|run|start|pull|compose\s+(up|down|restart))\b"), "container change"),
    (re.compile(r"\bkubectl\s+(apply|create|scale|rollout|patch|edit|label|annotate|cordon|drain)\b"), "kubernetes change"),
    (re.compile(r"\b(ip\s+(addr|route|link)\s+(add|del|change|replace|set|flush)|ifdown|ifup|nmcli\s+(con|connection|dev|device)\s+(up|down|modify|delete|add))\b"), "network change"),
    (re.compile(r"\b(iptables|ip6tables|nft|ufw|firewall-cmd)\b"), "firewall"),
    (re.compile(r"\b(mount|umount|swapoff|swapon)\b"), "mount change"),
    (re.compile(r"\bsysctl\s+-w\b"), "kernel parameter change"),
    (re.compile(CMD + r"eval\b", re.M), "eval"),
    (re.compile(r"\b(python[23]?|perl|ruby|node|php)\s+-[ce]\s", _I), "inline interpreter code"),
    (re.compile(CMD + r"(su|sudo\s+-i|sudo\s+su)\b(?!\S)", re.M), "switch user"),
    (re.compile(r"\bkill\b"), "signal process"),
    # PowerShell / Windows
    (re.compile(r"\b(Set|New|Add|Remove|Enable|Disable|Start|Install|Uninstall|Update|Rename|Move|Copy|Clear|Reset|Register|Unregister|Grant|Revoke|Restore)-[A-Za-z]+\b", _I), "state-changing cmdlet"),
    (re.compile(r"\breg\s+(add|import|load|unload|restore)\b", _I), "registry change"),
    (re.compile(r"\bnetsh\b.*\b(set|add|delete|reset)\b", _I), "network change"),
    (re.compile(r"\bipconfig\s+/(release|renew|flushdns|registerdns)\b", _I), "network change"),
    (re.compile(r"\b(sc(\.exe)?\s+(config|stop|start|delete|create)|net\s+(start|user|localgroup|share))\b", _I), "service/account change"),
    (re.compile(r"\b(gpupdate|DISM|sfc\s+/scannow|chkdsk\s+.*/[fr])\b", _I), "system repair"),
    # Network devices
    (re.compile(r"^\s*(conf(igure)?(\s+t(erminal)?)?|config\s+system)\b", _I | re.M), "configuration mode"),
    (re.compile(r"\b(write\s+mem(ory)?|copy\s+run(ning-config)?\s+start(up-config)?|commit)\b", _I), "config save/commit"),
    (re.compile(r"^\s*clear\s+", _I | re.M), "clear counters/sessions"),
    (re.compile(r"^\s*/\S+.*\s(set|add|remove|disable|enable)\b", _I | re.M), "RouterOS change"),
    (re.compile(r"^\s*(no\s+)?shutdown\s*$", _I | re.M), "interface shutdown"),
]


def classify(command: str) -> tuple[str, list[str]]:
    """Return (level, reasons) for the command according to local rules."""
    reasons = [why for pat, why in DISRUPTIVE if pat.search(command)]
    if reasons:
        return "disruptive", reasons
    reasons = [why for pat, why in MODIFYING if pat.search(command)]
    if reasons:
        return "modifying", reasons
    return "read_only", []


def effective(model_level: str, command: str) -> tuple[str, list[str]]:
    """Combine the model's label with local rules; the higher level wins."""
    local, reasons = classify(command)
    model_level = model_level if model_level in LEVELS else "modifying"
    level = max(model_level, local, key=LEVELS.index)
    return level, reasons
