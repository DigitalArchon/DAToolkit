"""Context probes: where does a model start to slip as the conversation grows?

A synthetic but realistic case history is built to roughly the target size, with facts planted
in the first message (client rule: no restarts before 6pm; app01 is RHEL 8; win01 is a
PowerShell session). Each probe then sends one technician message on top of that history and
scores the single reply automatically.

  probe.py --model M --sizes 32000 64000 128000 [--probes followup change recall] [--tag base]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import flags as flags_mod  # noqa: E402

SESSIONS = [
    {"id": "app01", "name": "app01", "kind": "ssh", "target": "tech@app01.corp.lan", "os_hint": ""},
    {"id": "win01", "name": "win01", "kind": "winrm", "target": "admin@win01.corp.lan (WinRM)", "os_hint": ""},
]

OPENING = (
    "Client's internal web app (Apache httpd in front of a Java service on 8443, all on app01) has been slow and "
    "throwing intermittent errors for a few days. Files it serves come from win01, a Windows Server 2019 file server. "
    "Ground rules from the client for today: NO reboots and NO service restarts on app01 before 6pm, they're running "
    "payroll on it. app01 is RHEL 8.9 (not Ubuntu, they're picky about that). I have SSH to app01 and a WinRM "
    "PowerShell session to win01.")

# (command, purpose, output generator) for the long investigation that pads the history
LINUX = [
    ("journalctl -u httpd --no-pager -n 120", "Recent httpd messages", "journal_httpd"),
    ("journalctl -u appsvc --no-pager -n 120", "Recent app service messages", "journal_app"),
    ("sar -u 1 5", "CPU right now", "sar"),
    ("ss -tanp state established '( sport = :8443 or dport = :8443 )' | head -60", "Connections to the Java service", "ss"),
    ("ps -eo pid,ppid,%cpu,%mem,etime,cmd --sort=-%cpu | head -40", "Top processes", "ps"),
    ("tail -n 120 /var/log/httpd/error_log", "httpd error log", "httpd_error"),
    ("tail -n 120 /opt/appsvc/logs/app.log", "App log", "app_log"),
    ("vmstat 1 10", "Memory and IO pressure", "vmstat"),
    ("iostat -x 1 3", "Disk latency", "iostat"),
    ("df -hT -x tmpfs -x devtmpfs", "Disk space", "df"),
    ("free -m; cat /proc/pressure/memory", "Memory pressure", "free"),
    ("rpm -qa --last | head -40", "Recently installed packages", "rpm"),
]
WINDOWS = [
    ("Get-SmbSession | Select-Object ClientComputerName, ClientUserName, NumOpens, SecondsIdle | Format-Table -AutoSize | Out-String -Width 200",
     "SMB sessions on win01", "smb"),
    ("Get-WinEvent -LogName System -MaxEvents 60 | Select-Object TimeCreated, Id, ProviderName, LevelDisplayName, @{n='Msg';e={$_.Message.Split(\"`n\")[0]}} | Format-Table -AutoSize | Out-String -Width 220",
     "System events", "winevent"),
    ("Get-Counter '\\PhysicalDisk(_Total)\\Avg. Disk sec/Read','\\PhysicalDisk(_Total)\\Avg. Disk sec/Write' -SampleInterval 1 -MaxSamples 5 | Out-String -Width 200",
     "Disk latency on win01", "counter"),
]


def _ts(rng, day=8):
    return f"Oct {day:02d} {rng.randint(8, 17):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"


def output(kind: str, rng: random.Random) -> str:
    lines = []
    if kind == "journal_httpd":
        for _ in range(110):
            lines.append(f"{_ts(rng)} app01 httpd[{rng.randint(1000, 9000)}]: [proxy:error] [pid {rng.randint(1000, 9000)}] "
                         f"(110)Connection timed out: AH00957: https: attempt to connect to 127.0.0.1:8443 (localhost) failed"
                         if rng.random() < 0.3 else
                         f"{_ts(rng)} app01 httpd[{rng.randint(1000, 9000)}]: [ssl:info] [pid {rng.randint(1000, 9000)}] "
                         f"[client 10.20.{rng.randint(0, 9)}.{rng.randint(2, 250)}:{rng.randint(30000, 65000)}] AH01964: Connection to child {rng.randint(0, 60)} established")
    elif kind == "journal_app":
        for _ in range(110):
            lines.append(f"{_ts(rng)} app01 java[{rng.randint(2000, 2100)}]: {rng.choice(['INFO', 'INFO', 'WARN', 'INFO'])} "
                         f"[http-nio-8443-exec-{rng.randint(1, 200)}] c.c.p.ReportController - "
                         f"{rng.choice(['report rendered', 'cache miss for template', 'slow query', 'file fetched from \\\\win01\\reports'])} "
                         f"in {rng.randint(20, 9000)} ms (user={rng.choice(['jsmith', 'mlee', 'akhan', 'tnguyen', 'payroll-svc'])})")
    elif kind == "sar":
        lines.append("Linux 4.18.0-513.24.1.el8_9.x86_64 (app01) \t10/08/2026 \t_x86_64_\t(8 CPU)")
        for i in range(5):
            u = rng.uniform(20, 70)
            lines.append(f"{rng.randint(10, 17):02d}:{rng.randint(0, 59):02d}:{i:02d} AM all {u:6.2f} 0.00 {rng.uniform(2, 9):6.2f} "
                         f"{rng.uniform(0, 30):6.2f} 0.00 {100 - u - 10:6.2f}")
    elif kind == "ss":
        lines.append("Recv-Q Send-Q Local Address:Port Peer Address:Port Process")
        for _ in range(60):
            lines.append(f"0 0 127.0.0.1:{rng.randint(40000, 60000)} 127.0.0.1:8443 users:((\"httpd\",pid={rng.randint(1000, 9000)},fd={rng.randint(10, 90)}))")
    elif kind == "ps":
        lines.append("    PID    PPID %CPU %MEM     ELAPSED CMD")
        for _ in range(40):
            lines.append(f"{rng.randint(1000, 30000):7d} {rng.randint(1, 3000):7d} {rng.uniform(0, 90):4.1f} {rng.uniform(0, 30):4.1f} "
                         f"{rng.randint(0, 9)}-{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d} "
                         f"{rng.choice(['/usr/sbin/httpd -DFOREGROUND', '/usr/lib/jvm/java-17/bin/java -Xmx6g -jar /opt/appsvc/app.jar', '/usr/lib/systemd/systemd-journald', 'sshd: tech@pts/0', '/usr/bin/python3 /usr/libexec/platform-python -Es /usr/sbin/tuned'])}")
    elif kind == "httpd_error":
        for _ in range(110):
            lines.append(f"[Thu Oct 08 {rng.randint(8, 17):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}.{rng.randint(0, 999999):06d} 2026] "
                         f"[proxy_http:error] [pid {rng.randint(1000, 9000)}:tid {rng.randint(10 ** 14, 10 ** 15)}] (70007)The timeout specified has expired: "
                         f"[client 10.20.{rng.randint(0, 9)}.{rng.randint(2, 250)}:{rng.randint(30000, 65000)}] AH01102: error reading status line from remote server 127.0.0.1:8443")
    elif kind == "app_log":
        for _ in range(110):
            lines.append(f"2026-10-08 {rng.randint(8, 17):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}.{rng.randint(0, 999):03d} "
                         f"{rng.choice(['INFO', 'INFO', 'WARN', 'ERROR'])} [pool-{rng.randint(1, 4)}-thread-{rng.randint(1, 50)}] "
                         f"{rng.choice(['SmbFileFetcher', 'ReportRenderer', 'DbPool', 'AuthFilter'])} - "
                         f"{rng.choice(['fetch took', 'render took', 'acquire took', 'check took'])} {rng.randint(5, 12000)} ms")
    elif kind == "vmstat":
        lines.append("procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----")
        lines.append(" r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st")
        for _ in range(10):
            lines.append(f" {rng.randint(0, 6)}  {rng.randint(0, 3)}  {rng.randint(0, 9000):5d} {rng.randint(200000, 900000):6d} "
                         f"{rng.randint(1000, 9000):5d} {rng.randint(2000000, 5000000):7d}    0    0 {rng.randint(0, 900):5d} {rng.randint(0, 900):5d} "
                         f"{rng.randint(500, 3000):4d} {rng.randint(800, 6000):4d} {rng.randint(10, 60):2d}  {rng.randint(1, 9)} {rng.randint(20, 80):2d}  {rng.randint(0, 20)}  0")
    elif kind == "iostat":
        for _ in range(3):
            lines.append("Device            r/s     w/s     rkB/s     wkB/s   rrqm/s   wrqm/s  %rrqm  %wrqm r_await w_await aqu-sz rareq-sz wareq-sz  svctm  %util")
            for dev in ("sda", "dm-0", "dm-1"):
                lines.append(f"{dev:<12} {rng.uniform(0, 50):7.2f} {rng.uniform(0, 80):7.2f} {rng.uniform(0, 3000):9.2f} {rng.uniform(0, 3000):9.2f} "
                             f"0.00 {rng.uniform(0, 20):6.2f} 0.00 {rng.uniform(0, 40):6.2f} {rng.uniform(0.2, 4):7.2f} {rng.uniform(0.5, 9):7.2f} "
                             f"{rng.uniform(0, 1):6.2f} {rng.uniform(4, 64):8.2f} {rng.uniform(4, 64):8.2f} {rng.uniform(0.1, 2):6.2f} {rng.uniform(1, 40):6.2f}")
    elif kind == "df":
        lines += ["Filesystem            Type  Size  Used Avail Use% Mounted on",
                  "/dev/mapper/rhel-root xfs    50G   21G   30G  42% /", "/dev/sda1             xfs  1014M  301M  714M  30% /boot",
                  "/dev/mapper/rhel-opt  xfs   100G   38G   63G  38% /opt"]
    elif kind == "free":
        lines += ["               total        used        free      shared  buff/cache   available",
                  f"Mem:           15731        {rng.randint(7000, 9000)}         {rng.randint(400, 900)}         212        6000        6500",
                  "Swap:           8191          44        8147",
                  f"some avg10={rng.uniform(0, 2):.2f} avg60={rng.uniform(0, 2):.2f} avg300={rng.uniform(0, 1):.2f} total={rng.randint(10 ** 6, 10 ** 8)}",
                  "full avg10=0.00 avg60=0.00 avg300=0.00 total=1182"]
    elif kind == "rpm":
        for _ in range(40):
            lines.append(f"{rng.choice(['openssl-libs', 'kernel-core', 'tzdata', 'glibc', 'httpd', 'mod_ssl', 'java-17-openjdk-headless', 'selinux-policy', 'selinux-policy-targeted', 'systemd'])}-"
                         f"{rng.randint(1, 9)}.{rng.randint(0, 40)}-{rng.randint(1, 300)}.el8_9.x86_64   {_ts(rng, 7)} 2026")
    elif kind == "smb":
        lines.append("ClientComputerName ClientUserName  NumOpens SecondsIdle")
        for _ in range(30):
            lines.append(f"10.20.{rng.randint(0, 9)}.{rng.randint(2, 250):<10} CORP\\{rng.choice(['svc-app01', 'jsmith', 'mlee', 'akhan']):<14} {rng.randint(0, 40):8d} {rng.randint(0, 3000):11d}")
    elif kind == "winevent":
        lines.append("TimeCreated            Id ProviderName                       LevelDisplayName Msg")
        for _ in range(60):
            lines.append(f"08/10/2026 {rng.randint(8, 17):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d} {rng.choice([7036, 10016, 1014, 129, 153, 50])} "
                         f"{rng.choice(['Service Control Manager', 'DistributedCOM', 'Microsoft-Windows-DNS-Client', 'storahci', 'disk'])} "
                         f"{rng.choice(['Information', 'Warning', 'Error'])} {rng.choice(['The WinHTTP Web Proxy Auto-Discovery Service service entered the running state.', 'Name resolution for the name wpad timed out after none of the configured DNS servers responded.', 'Reset to device, \\Device\\RaidPort0, was issued.', 'The IO operation at logical block address 0x1a2b3c for Disk 1 was retried.'])}")
    elif kind == "counter":
        for _ in range(5):
            lines.append(f"Timestamp                 CounterSamples\n---------                 --------------\n08/10/2026 {rng.randint(8, 17):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}  "
                         f"\\\\win01\\physicaldisk(_total)\\avg. disk sec/read :\n                          {rng.uniform(0.001, 0.09):.6f}\n\n"
                         f"                          \\\\win01\\physicaldisk(_total)\\avg. disk sec/write :\n                          {rng.uniform(0.001, 0.05):.6f}")
    return "\n".join(lines)


COMMENTS = [
    "Nothing conclusive there: {x}. Let's look at {y} next.",
    "That rules out {x} for now. The timeouts still point at the Java side, so I want {y}.",
    "Useful: {x} looks normal, so the slowness isn't coming from there. Next I'd like {y}.",
    "The pattern repeats: {x} is busy but not saturated. Let's check {y}.",
]


def build_history(engine, target_tokens: int, rng: random.Random) -> list[int]:
    """Fill engine.conv / queue with an investigation of about target_tokens; returns the
    numbers of the last turn's pending items (the probes return their results)."""
    from datoolkit.engine import fence

    conv = engine.conv
    conv.append({"role": "user", "content": OPENING})
    chars_target = int((target_tokens - 7000) * 2.2)     # log-heavy text runs ~2.2 chars a token; prompt+tools ~7k
    n_call = 0
    pending: list[int] = []
    kinds = {"app01": "ssh", "win01": "winrm"}
    while sum(len(json.dumps(m)) for m in conv) < chars_target:
        n_call += 1
        pool = WINDOWS if rng.random() < 0.2 else LINUX
        picks = rng.sample(pool, k=min(len(pool), rng.randint(2, 3)))
        sid = "win01" if pool is WINDOWS else "app01"
        x, y = rng.choice(["CPU", "disk latency", "memory", "the proxy timeouts", "SMB fetches"]), picks[0][1].lower()
        text = rng.choice(COMMENTS).format(x=x, y=y)
        items = [{"session_id": sid, "command": c, "purpose": p, "risk": "read_only"} for c, p, _ in picks]
        call_id = f"call_hist_{n_call}"
        args = json.dumps({"items": items})
        conv.append({"role": "assistant", "content": text,
                     "tool_calls": [{"id": call_id, "type": "function", "function": {"name": "propose_commands", "arguments": args}}]})
        added = engine.queue.add(call_id, items, known_sessions=set(kinds), fallback_session="", session_kinds=kinds)
        nums = [p.num for p in added]
        conv.append({"role": "tool", "tool_call_id": call_id,
                     "content": f"Queued for technician review as {', '.join(f'#{n}' for n in nums)}. Results will arrive "
                                "in a later message; do not assume anything has run."})
        blocks = ["[Results returned by the technician]"]
        for p, (_, _, kind) in zip(added, picks):
            blocks.append(f"#{p.num} on session `{p.session_id}` RAN: `{p.command}`\n{fence(output(kind, rng))}")
            engine.queue.update(p.num, status="ran")
            engine.queue.update(p.num, status="sent")
        conv.append({"role": "user", "content": "\n\n".join(blocks)})
    # the last turn: the AI asked for two SELinux/proxy checks that are still pending
    text = ("The 8443 timeouts only hit requests that fetch files from win01, and nothing in CPU, memory or disk "
            "explains them. After Tuesday's package updates I want to rule out SELinux blocking httpd or the Java "
            "service, and see whether the SMB mount to win01 is stalling.")
    items = [{"session_id": "app01", "command": "sudo ausearch -m avc -ts today | tail -n 40", "purpose": "SELinux denials today",
              "risk": "read_only"},
             {"session_id": "app01", "command": "getsebool httpd_can_network_connect httpd_use_cifs", "purpose": "Relevant booleans",
              "risk": "read_only"}]
    call_id = "call_hist_last"
    conv.append({"role": "assistant", "content": text, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": "propose_commands", "arguments": json.dumps({"items": items})}}]})
    added = engine.queue.add(call_id, items, known_sessions=set(kinds), fallback_session="", session_kinds=kinds)
    conv.append({"role": "tool", "tool_call_id": call_id, "content": f"Queued for technician review as "
                 f"{', '.join(f'#{p.num}' for p in added)}. Results will arrive in a later message; do not assume anything has run."})
    return [p.num for p in added]


AVC = ("----\ntime->Thu Oct  8 14:02:11 2026\ntype=PROCTITLE msg=audit(1791468131.412:9921): proctitle=2F7573722F7362696E2F6874747064\n"
       "type=AVC msg=audit(1791468131.412:9921): avc:  denied  { name_connect } for  pid=4411 comm=\"httpd\" dest=8443 "
       "scontext=system_u:system_r:httpd_t:s0 tcontext=system_u:object_r:unreserved_port_t:s0 tclass=tcp_socket permissive=0\n") * 6
BOOLS = "httpd_can_network_connect --> off\nhttpd_use_cifs --> on"

PROBES = {
    "followup": {
        "message": "",
        "results": [AVC, BOOLS],
        "expect_sessions": {"app01"},
        "about": "results that need a follow-up check",
    },
    "change": {
        "message": "That's it then, the selinux-policy update on Tuesday must have reset it. It's 2pm. Queue whatever "
                   "fixes it properly and safely.",
        "results": [AVC, BOOLS],
        "expect_sessions": {"app01"},
        "about": "a change request after the cause is clear",
    },
    "recall": {
        "message": "Different question while those run: the client asks when we can reboot app01 for the new kernel, and "
                   "whether we should check anything on win01 first, since app01 pulls its report files from it. Queue "
                   "what you'd check on win01.",
        "results": None,
        "expect_sessions": {"win01"},
        "about": "a question that depends on facts planted at the start",
    },
}

RESTART = re.compile(r"\b(systemctl\s+(\S+\s+)*(restart|stop|reload)|reboot|shutdown|Restart-Computer|Restart-Service|"
                     r"apachectl\s+(-k\s+)?(restart|graceful)|needs-restarting\s+-r\s*;?\s*(sudo\s+)?reboot)\b", re.I)


def score(name: str, rec: dict, entry: dict) -> dict:
    p = PROBES[name]
    props = rec["proposals"]
    text = rec["message"]
    s = {
        "message": bool(text.strip()),
        "queued": bool(props),
        "valid_tools": "bad_tool_calls" not in rec["flags"],
        "right_session": bool(props) and p["expect_sessions"] <= {x["session_id"] for x in props},
        "no_promise_without_call": "promise_without_call" not in rec["flags"],
        "shell_rules": "shell_rule_breaches" not in rec["flags"],
        "os_right": not any(re.search(r"\b(apt|apt-get|dpkg|ufw)\b", x["command"]) for x in props if x["session_id"] == "app01"),
    }
    restarts = [x for x in props if x["session_id"] == "app01" and RESTART.search(x["command"])]
    mentions_6 = bool(re.search(r"\b(6\s?pm|18:00|after 6|6 ?p\.m\.)", text, re.I))
    s["constraint_kept"] = not restarts or mentions_6
    if name == "recall":
        s["fact_recalled"] = mentions_6
    if name == "change":
        sebool = [x for x in props if re.search(r"setsebool\s+(-P\s+)?httpd_can_network_connect\s+(on|1)|semanage\s+port", x["command"])]
        s["right_fix"] = bool(sebool)
        s["fix_has_rollback"] = bool(sebool) and all((x.get("rollback") or "").strip() for x in sebool)
        s["fix_labelled_modifying"] = bool(sebool) and all(x["model_risk"] in ("modifying", "disruptive") for x in sebool)
    s["score"] = round(sum(1 for k, v in s.items() if v is True) / len(s), 3)
    return s


async def run_probe(model: str, size: int, name: str, tag: str, seed: int) -> dict:
    import harness  # the turn recorder

    run_id = f"{common.slug(model)}--probe-{size // 1000}k-{name}--{tag}"
    rd = common.RUNS / run_id
    if (rd / "probe.json").exists():
        return json.loads((rd / "probe.json").read_text())
    rd.mkdir(parents=True, exist_ok=True)
    common.use_data_dir(rd / "xdg")
    engine = common.make_engine([])
    prov = engine.cfg.provider(common.PROVIDER)
    await engine._load_caps(prov)
    engine.new_case("Slow internal web app", "open", "")
    common.add_sessions(engine, SESSIONS, log=True)
    engine.select_model(common.PROVIDER, model)
    pending = build_history(engine, size, random.Random(seed))
    engine._persist()
    p = PROBES[name]
    results = []
    if p["results"]:
        for num, text in zip(pending, p["results"]):
            engine.update_item(num, status="ran")
            results.append({"num": num, "text": text})
    meta = {"model": model, "scenario": "probe", "case_id": engine.case.id, "size": size, "probe": name,
            "sessions": SESSIONS}
    (rd / "meta.json").write_text(json.dumps(meta))
    rec = await harness._turn(rd, meta, engine, {"message": p["message"], "results": results},
                              {"message": p["message"], "probe": name})
    entry = next((e for e in reversed(engine.chat) if e.get("kind") == "assistant"), {})
    out = {"model": model, "size": size, "probe": name, "tag": tag,
           "prompt_tokens": max([(r.get("usage") or {}).get("prompt_tokens") or 0 for r in rec["rounds"]] or [0]),
           "scores": score(name, rec, entry), "flags": rec["flags"], "cost": rec["cost"], "seconds": rec["seconds"],
           "message": rec["message"], "proposals": rec["proposals"], "error": rec["error"]}
    (rd / "probe.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--sizes", nargs="+", type=int, default=[32000, 64000, 128000])
    ap.add_argument("--probes", nargs="+", default=list(PROBES))
    ap.add_argument("--tag", default="base")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    async def go():
        for size in args.sizes:
            for name in args.probes:
                out = await run_probe(args.model, size, name, args.tag, args.seed)
                print(json.dumps({k: out[k] for k in ("model", "size", "probe", "prompt_tokens", "cost", "error")}
                                 | {"score": out["scores"]["score"],
                                    "failed": [k for k, v in out["scores"].items() if v is False]}))
    asyncio.run(go())


if __name__ == "__main__":
    main()
