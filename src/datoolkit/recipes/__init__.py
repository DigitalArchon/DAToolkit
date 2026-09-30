"""Recipe library: named, versioned diagnostic procedures per OS family.

A recipe is a list of commands with purposes and risk labels, plus an optional install step
for the tool it needs. Recipes are queued like any proposal; the technician still reviews,
runs and chooses what to send. Built-in recipes live here; technicians add their own as
TOML files under ~/.config/datoolkit/recipes/ (same fields).

The `baseline` recipes are special: their outputs can be saved per host and diffed later
(see engine.save_baseline / diff_baseline).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..config import config_dir

OS_FAMILIES = ("linux", "windows", "any")


@dataclass
class Step:
    command: str
    purpose: str
    risk: str = "read_only"
    key: str = ""          # stable id for baseline diffs; defaults to the command


@dataclass
class Recipe:
    id: str
    name: str
    os: str
    steps: list[Step]
    description: str = ""
    tags: list[str] = field(default_factory=list)
    install: str = ""      # modifying command that installs the tool the recipe needs
    install_check: str = ""  # read-only command that tells whether the tool is present
    baseline: bool = False  # outputs are worth snapshotting and diffing
    source: str = "builtin"

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "os": self.os, "description": self.description,
                "tags": self.tags, "install": self.install, "install_check": self.install_check,
                "baseline": self.baseline, "source": self.source,
                "steps": [{"command": s.command, "purpose": s.purpose, "risk": s.risk, "key": s.key or s.command}
                          for s in self.steps]}


def _r(id_, name, os_, steps, **kw) -> Recipe:
    return Recipe(id_, name, os_, [Step(*s) if isinstance(s, tuple) else s for s in steps], **kw)


BUILTIN: list[Recipe] = [
    # ------------------------------------------------------------- disk space
    _r("disk-usage-linux", "Where did the disk go", "linux", [
        ("df -hT -x tmpfs -x devtmpfs -x squashfs", "Filesystem usage by mount"),
        ("sudo du -xh --max-depth=2 / 2>/dev/null | sort -h | tail -40", "Largest directories two levels down (one filesystem)"),
        ("sudo find / -xdev -type f -size +500M -printf '%s\\t%p\\n' 2>/dev/null | sort -n | tail -20 | awk '{printf \"%.1fG\\t%s\\n\", $1/1073741824, $2}'", "Files over 500 MB"),
        ("sudo lsof +L1 2>/dev/null | awk 'NR==1 || $7 > 104857600' | head -20", "Deleted-but-open files still holding space"),
        ("journalctl --disk-usage; sudo du -sh /var/log /var/cache/apt /var/lib/docker /var/lib/snapd 2>/dev/null", "Usual suspects"),
    ], description="The WinDirStat question, answered with du. ncdu gives an interactive view if installed.",
       tags=["disk", "storage"], install="sudo apt install -y ncdu", install_check="command -v ncdu"),
    _r("disk-usage-windows", "Where did the disk go", "windows", [
        ("Get-PSDrive -PSProvider FileSystem | Select-Object Name, @{n='UsedGB';e={[math]::Round($_.Used/1GB,1)}}, @{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}} | Format-Table -AutoSize | Out-String -Width 200", "Drive usage"),
        ("Get-ChildItem C:\\ -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object { $s = (Get-ChildItem $_.FullName -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum; [pscustomobject]@{GB=[math]::Round($s/1GB,2); Folder=$_.FullName} } | Sort-Object GB -Descending | Select-Object -First 25 | Format-Table -AutoSize | Out-String -Width 200", "Top-level folder sizes on C: (slow on big disks)"),
        ("Get-ChildItem C:\\Users -Directory -ErrorAction SilentlyContinue | ForEach-Object { $s = (Get-ChildItem $_.FullName -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum; [pscustomobject]@{GB=[math]::Round($s/1GB,2); Profile=$_.Name} } | Sort-Object GB -Descending | Format-Table -AutoSize | Out-String -Width 200", "Profile sizes"),
        ("Get-ChildItem C:\\ -Recurse -Force -File -ErrorAction SilentlyContinue | Where-Object Length -gt 500MB | Sort-Object Length -Descending | Select-Object -First 20 @{n='GB';e={[math]::Round($_.Length/1GB,2)}}, FullName | Format-Table -AutoSize | Out-String -Width 200", "Files over 500 MB"),
        ("Get-ChildItem 'C:\\Windows\\SoftwareDistribution\\Download','C:\\Windows\\Temp',\"$env:TEMP\",'C:\\ProgramData\\Microsoft\\Windows\\WER' -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum | Select-Object @{n='CleanableGB';e={[math]::Round($_.Sum/1GB,2)}}", "Cleanable caches"),
        ("vssadmin list shadowstorage", "Shadow copy storage"),
    ], description="The WinDirStat question in PowerShell. WizTree (portable, CLI export) is faster on large disks; see the tool cache.",
       tags=["disk", "storage"], install="winget install --id AntibodySoftware.WizTree -e --accept-source-agreements --accept-package-agreements",
       install_check="Get-Command WizTree64 -ErrorAction SilentlyContinue | Select-Object Source"),
    # ------------------------------------------------------------- LAN discovery
    _r("lan-discovery-linux", "Who is on the LAN", "linux", [
        ("ip -br addr; ip route show", "Our addresses and routes"),
        ("ip neigh show | sort", "Neighbours already seen (ARP/ND)"),
        ("command -v nmap >/dev/null && sudo nmap -sn --max-retries 1 $(ip -4 route show default | awk '{print $3}' | head -1)/24 | grep -E 'Nmap scan report|MAC' || echo 'nmap not installed'", "Ping sweep of the default gateway's /24 (needs nmap)"),
        ("command -v avahi-browse >/dev/null && timeout 5 avahi-browse -art 2>/dev/null | grep -E '^=' | awk -F';' '{print $4, $5, $7, $8}' | sort -u | head -40 || true", "mDNS services"),
        ("cat /etc/resolv.conf 2>/dev/null; resolvectl status 2>/dev/null | grep -E 'DNS (Servers|Domain)' | head", "DNS in use"),
    ], description="The Advanced IP Scanner question. Run it in a session on the far side of a VPN to see that side.",
       tags=["network", "lan"], install="sudo apt install -y nmap", install_check="command -v nmap"),
    _r("lan-discovery-windows", "Who is on the LAN", "windows", [
        ("Get-NetIPConfiguration | Select-Object InterfaceAlias, IPv4Address, IPv4DefaultGateway, DNSServer | Format-List | Out-String -Width 200", "Our addresses, gateway and DNS"),
        ("Get-NetNeighbor -AddressFamily IPv4 | Where-Object State -ne Unreachable | Sort-Object IPAddress | Format-Table IPAddress, LinkLayerAddress, State -AutoSize | Out-String -Width 200", "Neighbours already seen"),
        ("$gw = (Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Select-Object -First 1).NextHop; $net = $gw -replace '\\.\\d+$',''; 1..254 | ForEach-Object -Parallel { if (Test-Connection -ComputerName \"$using:net.$_\" -Count 1 -Quiet -TimeoutSeconds 1) { \"$using:net.$_\" } } -ThrottleLimit 64 2>$null | Sort-Object { [version]$_ }", "Ping sweep of the gateway's /24 (PowerShell 7; on 5.1 use the next step)"),
        ("$gw = (Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Select-Object -First 1).NextHop; $net = $gw -replace '\\.\\d+$',''; 1..254 | ForEach-Object { $ip=\"$net.$_\"; if ((New-Object Net.NetworkInformation.Ping).Send($ip, 300).Status -eq 'Success') { $ip } }", "Ping sweep, PowerShell 5.1 (slower)"),
        ("Get-NetNeighbor -AddressFamily IPv4 -State Reachable,Stale | ForEach-Object { try { $n=[Net.Dns]::GetHostEntry($_.IPAddress).HostName } catch { $n='' }; [pscustomobject]@{IP=$_.IPAddress; MAC=$_.LinkLayerAddress; Name=$n} } | Format-Table -AutoSize | Out-String -Width 200", "Reverse-resolve the neighbours found"),
    ], description="The Advanced IP Scanner question in PowerShell. nmap via winget adds MAC vendor lookup.",
       tags=["network", "lan"], install="winget install --id Insecure.Nmap -e --accept-source-agreements --accept-package-agreements",
       install_check="Get-Command nmap -ErrorAction SilentlyContinue | Select-Object Source"),
    # ------------------------------------------------------------- path & latency
    _r("path-latency-linux", "Path and latency", "linux", [
        ("ping -c 5 -W 2 1.1.1.1; ping -c 5 -W 2 $(ip -4 route show default | awk '{print $3}' | head -1)", "Latency to the gateway and the internet"),
        ("command -v mtr >/dev/null && sudo mtr -rwzc 20 1.1.1.1 || traceroute -n -w 2 -q 1 1.1.1.1", "Per-hop loss and latency (mtr, or traceroute)"),
        ("ping -c 3 -M do -s 1472 1.1.1.1 2>&1 | tail -3; ping -c 3 -M do -s 1372 1.1.1.1 2>&1 | tail -3", "Path MTU: 1500 vs 1400 without fragmentation"),
        ("getent hosts example.com; time (dig +short example.com >/dev/null)", "DNS resolution and how long it takes"),
    ], description="Where the latency or loss is. Add iperf3 for throughput between two hosts.",
       tags=["network", "vpn"], install="sudo apt install -y mtr-tiny iperf3", install_check="command -v mtr; command -v iperf3"),
    _r("path-latency-windows", "Path and latency", "windows", [
        ("Test-Connection 1.1.1.1 -Count 5 | Format-Table Address, Latency, Status -AutoSize | Out-String -Width 200", "Latency to the internet"),
        ("Test-NetConnection 1.1.1.1 -TraceRoute -WarningAction SilentlyContinue | Out-String -Width 200", "Route to the internet"),
        ("pathping -n -q 5 -w 500 1.1.1.1", "Per-hop loss (takes a few minutes)"),
        ("ping -f -l 1472 -n 2 1.1.1.1; ping -f -l 1372 -n 2 1.1.1.1", "Path MTU: 1500 vs 1400 without fragmentation"),
        ("Measure-Command { Resolve-DnsName example.com -ErrorAction SilentlyContinue } | Select-Object TotalMilliseconds; Resolve-DnsName example.com | Select-Object Name, IPAddress | Out-String -Width 200", "DNS resolution and how long it takes"),
    ], tags=["network", "vpn"]),
    # ------------------------------------------------------------- disk health
    _r("disk-health-linux", "Disk health", "linux", [
        ("lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL", "Block devices"),
        ("for d in $(lsblk -dno NAME,TYPE | awk '$2==\"disk\"{print $1}'); do echo \"== /dev/$d\"; sudo smartctl -H -A /dev/$d 2>/dev/null | grep -Ei 'overall|Reallocated|Pending|Uncorrect|Wear|Percentage_Used|Temperature|Power_On' ; done", "SMART health and the attributes that matter (needs smartmontools)"),
        ("sudo dmesg -T 2>/dev/null | grep -Ei 'i/o error|ata[0-9]|nvme|reset|remount.*read-only' | tail -30", "Kernel I/O errors"),
        ("cat /proc/mdstat 2>/dev/null; sudo zpool status 2>/dev/null | head -40; sudo btrfs device stats / 2>/dev/null", "RAID / ZFS / btrfs status"),
    ], tags=["disk", "hardware"], install="sudo apt install -y smartmontools", install_check="command -v smartctl"),
    _r("disk-health-windows", "Disk health", "windows", [
        ("Get-PhysicalDisk | Select-Object FriendlyName, MediaType, Size, HealthStatus, OperationalStatus | Format-Table -AutoSize | Out-String -Width 200", "Physical disk health"),
        ("Get-PhysicalDisk | Get-StorageReliabilityCounter | Select-Object DeviceId, Temperature, ReadErrorsTotal, WriteErrorsTotal, Wear, PowerOnHours | Format-Table -AutoSize | Out-String -Width 200", "Reliability counters"),
        ("Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='disk','Ntfs','storahci','stornvme','volmgr'; Level=1,2,3} -MaxEvents 30 -ErrorAction SilentlyContinue | Select-Object TimeCreated, ProviderName, Id, @{n='Msg';e={$_.Message.Split(\"`n\")[0]}} | Format-Table -AutoSize | Out-String -Width 200", "Recent disk-related events"),
        ("Get-Volume | Select-Object DriveLetter, FileSystemLabel, FileSystem, HealthStatus, @{n='FreeGB';e={[math]::Round($_.SizeRemaining/1GB,1)}}, @{n='SizeGB';e={[math]::Round($_.Size/1GB,1)}} | Format-Table -AutoSize | Out-String -Width 200", "Volumes"),
    ], tags=["disk", "hardware"]),
    # ------------------------------------------------------------- DNS & reachability
    _r("dns-reach-linux", "DNS and reachability", "linux", [
        ("resolvectl status 2>/dev/null | head -30 || cat /etc/resolv.conf", "Resolver configuration"),
        ("for h in example.com $(hostname -d 2>/dev/null); do [ -n \"$h\" ] && dig +short +time=2 $h @$(awk '/^nameserver/{print $2; exit}' /etc/resolv.conf) | head -3; done", "Resolve via the configured nameserver"),
        ("ss -tlnp | head -30", "Listening TCP ports"),
        ("for t in 1.1.1.1:53 example.com:443; do timeout 3 bash -c \"</dev/tcp/${t%:*}/${t#*:}\" && echo \"$t open\" || echo \"$t closed/filtered\"; done", "TCP reachability"),
        ("ss -s; ss -tn state established | wc -l", "Socket summary"),
    ], tags=["network", "dns"]),
    _r("dns-reach-windows", "DNS and reachability", "windows", [
        ("Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object ServerAddresses | Format-Table InterfaceAlias, ServerAddresses -AutoSize | Out-String -Width 200", "Resolver configuration"),
        ("Resolve-DnsName example.com -Type A | Select-Object Name, IPAddress; Resolve-DnsName $env:USERDNSDOMAIN -ErrorAction SilentlyContinue | Select-Object -First 3 Name, IPAddress | Out-String -Width 200", "Resolve public and domain names"),
        ("Test-NetConnection example.com -Port 443 -InformationLevel Quiet; Test-NetConnection 1.1.1.1 -Port 53 -InformationLevel Quiet", "TCP reachability"),
        ("Get-NetTCPConnection -State Listen | Sort-Object LocalPort | Select-Object LocalAddress, LocalPort, OwningProcess, @{n='Proc';e={(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).Name}} | Format-Table -AutoSize | Out-String -Width 200", "Listening TCP ports"),
        ("Get-NetTCPConnection -State Established | Measure-Object | Select-Object Count", "Established connections"),
    ], tags=["network", "dns"]),
    # ------------------------------------------------------------- baseline snapshots
    _r("baseline-linux", "Baseline snapshot", "linux", [
        Step("hostnamectl 2>/dev/null || uname -a", "Identity", key="identity"),
        Step("systemctl list-units --type=service --state=running --no-pager --no-legend | awk '{print $1}' | sort", "Running services", key="services"),
        Step("systemctl list-unit-files --type=service --state=enabled --no-pager --no-legend | awk '{print $1}' | sort", "Enabled services", key="enabled"),
        Step("ss -tulnp 2>/dev/null | awk 'NR>1{print $1, $5, $7}' | sort", "Listening ports", key="ports"),
        Step("ip -br addr | sort; ip route show | sort", "Addresses and routes", key="network"),
        Step("df -hT -x tmpfs -x devtmpfs -x squashfs | sort", "Filesystems", key="disks"),
        Step("(dpkg-query -W -f='${Package} ${Version}\\n' 2>/dev/null || rpm -qa 2>/dev/null) | sort", "Installed packages", key="packages"),
        Step("lsmod | awk 'NR>1{print $1}' | sort", "Kernel modules", key="modules"),
        Step("cat /etc/resolv.conf 2>/dev/null | grep -v '^#'; cat /etc/hosts | grep -v '^#'", "Resolver and hosts file", key="resolver"),
        Step("(sudo iptables -S 2>/dev/null; sudo nft list ruleset 2>/dev/null) | head -200", "Firewall rules", key="firewall"),
        Step("for u in $(cut -d: -f1 /etc/passwd); do sudo crontab -l -u $u 2>/dev/null | sed \"s/^/$u: /\"; done; ls /etc/cron.d 2>/dev/null", "Cron jobs", key="cron"),
        Step("getent passwd | awk -F: '$3>=1000{print $1}' | sort; getent group sudo wheel adm 2>/dev/null", "Users and admin groups", key="users"),
    ], description="Snapshot of what a healthy host looks like. Save it; diff it when the host misbehaves.",
       tags=["baseline"], baseline=True),
    _r("baseline-windows", "Baseline snapshot", "windows", [
        Step("Get-ComputerInfo | Select-Object CsName, OsName, OsVersion, OsLastBootUpTime | Format-List | Out-String -Width 200", "Identity", key="identity"),
        Step("Get-Service | Where-Object Status -eq Running | Select-Object -ExpandProperty Name | Sort-Object", "Running services", key="services"),
        Step("Get-Service | Where-Object StartType -eq Automatic | Select-Object -ExpandProperty Name | Sort-Object", "Automatic services", key="enabled"),
        Step("Get-NetTCPConnection -State Listen | Select-Object LocalAddress, LocalPort, @{n='Proc';e={(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).Name}} | Sort-Object LocalPort | Format-Table -AutoSize | Out-String -Width 200", "Listening ports", key="ports"),
        Step("Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias, IPAddress, PrefixLength | Sort-Object InterfaceAlias | Format-Table -AutoSize | Out-String -Width 200; Get-NetRoute -AddressFamily IPv4 | Where-Object NextHop -ne '0.0.0.0' | Select-Object DestinationPrefix, NextHop, InterfaceAlias | Format-Table -AutoSize | Out-String -Width 200", "Addresses and routes", key="network"),
        Step("Get-Volume | Where-Object DriveLetter | Select-Object DriveLetter, FileSystem, HealthStatus, @{n='SizeGB';e={[math]::Round($_.Size/1GB)}} | Sort-Object DriveLetter | Format-Table -AutoSize | Out-String -Width 200", "Volumes", key="disks"),
        Step("Get-Package -ProviderName Programs, msi -ErrorAction SilentlyContinue | Select-Object Name, Version | Sort-Object Name | Format-Table -AutoSize | Out-String -Width 200", "Installed programs", key="packages"),
        Step("Get-WindowsDriver -Online -ErrorAction SilentlyContinue | Select-Object ProviderName, ClassName, Version | Sort-Object ProviderName, ClassName | Format-Table -AutoSize | Out-String -Width 200", "Third-party drivers", key="drivers"),
        Step("Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object ServerAddresses | Format-Table InterfaceAlias, ServerAddresses -AutoSize | Out-String -Width 200; Get-Content $env:SystemRoot\\System32\\drivers\\etc\\hosts | Where-Object { $_ -notmatch '^#' -and $_.Trim() }", "Resolver and hosts file", key="resolver"),
        Step("Get-NetFirewallProfile | Select-Object Name, Enabled, DefaultInboundAction | Format-Table -AutoSize | Out-String -Width 200; Get-NetFirewallRule -Enabled True -Direction Inbound -Action Allow | Select-Object -ExpandProperty DisplayName | Sort-Object", "Firewall", key="firewall"),
        Step("Get-ScheduledTask | Where-Object { $_.State -ne 'Disabled' -and $_.TaskPath -notlike '\\Microsoft\\*' } | Select-Object TaskName, TaskPath, State | Sort-Object TaskPath, TaskName | Format-Table -AutoSize | Out-String -Width 200", "Scheduled tasks (non-Microsoft)", key="tasks"),
        Step("Get-LocalUser | Where-Object Enabled | Select-Object -ExpandProperty Name | Sort-Object; Get-LocalGroupMember Administrators | Select-Object -ExpandProperty Name | Sort-Object", "Users and administrators", key="users"),
        Step("Get-CimInstance Win32_StartupCommand | Select-Object Name, Command, Location | Sort-Object Name | Format-Table -AutoSize | Out-String -Width 200", "Startup items", key="startup"),
    ], description="Snapshot of what a healthy host looks like. Save it; diff it when the host misbehaves.",
       tags=["baseline"], baseline=True),
    # ------------------------------------------------------------- watch helpers
    _r("watch-links-linux", "Watch links and errors", "linux", [
        ("ip -s -br link | grep -v '^lo'; dmesg -T 2>/dev/null | grep -iE 'link (is )?(up|down)|carrier' | tail -5", "Link state, counters and recent flaps (use Watch on this item)"),
    ], description="Queue it, then press Watch on the item to sample it every few seconds and see only changes.", tags=["network", "watch"]),
    _r("watch-memory-linux", "Watch memory and top processes", "linux", [
        ("free -m | head -2; ps -eo pid,rss,comm --sort=-rss | head -6", "Memory and the biggest processes (use Watch on this item)"),
    ], tags=["memory", "watch"]),
    _r("watch-memory-windows", "Watch memory and top processes", "windows", [
        ("Get-CimInstance Win32_OperatingSystem | Select-Object @{n='FreeMB';e={[math]::Round($_.FreePhysicalMemory/1KB)}}, @{n='TotalMB';e={[math]::Round($_.TotalVisibleMemorySize/1KB)}} | Out-String -Width 120; Get-Process | Sort-Object WS -Descending | Select-Object -First 5 Name, @{n='WSMB';e={[math]::Round($_.WS/1MB)}} | Format-Table -AutoSize | Out-String -Width 120", "Memory and the biggest processes (use Watch on this item)"),
    ], tags=["memory", "watch"]),
]


def user_recipes_dir() -> Path:
    return config_dir() / "recipes"


def _from_toml(path: Path) -> list[Recipe]:
    with path.open("rb") as f:
        data = tomllib.load(f)
    out = []
    for r in data.get("recipe", []):
        steps = [Step(str(s.get("command", "")), str(s.get("purpose", "")), str(s.get("risk", "read_only")), str(s.get("key", "")))
                 for s in r.get("steps", []) if s.get("command")]
        if not r.get("id") or not steps:
            continue
        out.append(Recipe(id=str(r["id"]), name=str(r.get("name", r["id"])), os=str(r.get("os", "any")),
                          steps=steps, description=str(r.get("description", "")), tags=list(r.get("tags", [])),
                          install=str(r.get("install", "")), install_check=str(r.get("install_check", "")),
                          baseline=bool(r.get("baseline", False)), source=path.name))
    return out


def load_all(user_dir: Path | None = None) -> list[Recipe]:
    """Built-in recipes plus the technician's own; a user recipe with the same id wins."""
    by_id = {r.id: r for r in BUILTIN}
    d = user_dir or user_recipes_dir()
    if d.is_dir():
        for path in sorted(d.glob("*.toml")):
            try:
                for r in _from_toml(path):
                    by_id[r.id] = r
            except (OSError, ValueError, TypeError):
                continue
    return list(by_id.values())


def get(recipe_id: str, user_dir: Path | None = None) -> Recipe | None:
    return next((r for r in load_all(user_dir) if r.id == recipe_id), None)


def os_family(session: dict) -> str:
    """Best guess of a session's OS family from its roster entry."""
    text = f"{session.get('kind', '')} {session.get('shell', '')} {session.get('os_hint', '')}".lower()
    if session.get("kind") == "winrm" or "windows" in text or "powershell" in text:
        return "windows"
    return "linux"


def roster_text(recipes: list[Recipe]) -> str:
    """Compact list for the system prompt."""
    return "\n".join(f"- {r.id} ({r.os}): {r.name}" + (f" - {r.description}" if r.description else "") for r in recipes)
