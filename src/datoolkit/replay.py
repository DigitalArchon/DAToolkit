"""Rules replay: run today's risk, redaction and injection rules over past cases' event logs
and report what would now be classified differently. `datoolkit-replay [case-dir ...]`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import data_dir
from .safety import risk
from .safety.inject import suspicious
from .safety.redact import redact
from .safety.sensitive import sensitive


def replay_case(case_dir: Path) -> dict:
    events = case_dir / "events.jsonl"
    out = {"case": case_dir.name, "proposals": 0, "risk_changes": [], "session_cuts": [], "sensitive_flags": [],
           "redaction_changes": [], "injection_flags": []}
    if not events.exists():
        return out
    kinds: dict[str, str] = {}
    for line in events.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ev = e.get("event")
        if ev in ("session_opened", "session_carried_over"):
            kinds[e.get("id", "")] = e.get("kind", "local")
        elif ev == "proposal":
            out["proposals"] += 1
            level, reasons = risk.effective(e.get("model_risk", "modifying"), e.get("command", ""))
            if level != e.get("risk"):
                out["risk_changes"].append({"num": e.get("num"), "command": e.get("command"), "was": e.get("risk"),
                                            "now": level, "reasons": reasons})
            cut = risk.session_impact(e.get("command", ""), kinds.get(e.get("session_id", ""), "local"))
            if cut and not e.get("cuts_session"):
                out["session_cuts"].append({"num": e.get("num"), "command": e.get("command"), "reason": cut})
            flags = sensitive(e.get("command", ""))
            if flags and not e.get("sensitive"):
                out["sensitive_flags"].append({"num": e.get("num"), "command": e.get("command"), "reasons": flags})
        elif ev == "sent_to_ai" and e.get("content"):
            content = e["content"]
            _, n = redact(content)
            if n:
                out["redaction_changes"].append({"ts": e.get("ts"), "new_redactions": n})
            flags = suspicious(content)
            if flags:
                out["injection_flags"].append({"ts": e.get("ts"), "reasons": flags})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="datoolkit-replay",
                                 description="Re-run current safety rules over past cases and report differences")
    ap.add_argument("cases", nargs="*", type=Path, help="case directories (default: all)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    dirs = args.cases or sorted(p for p in (data_dir() / "cases").glob("*") if p.is_dir())
    reports = [replay_case(d) for d in dirs]
    if args.json:
        json.dump(reports, sys.stdout, indent=1)
        return 0
    total = sum(r["proposals"] for r in reports)
    changed = [r for r in reports if r["risk_changes"] or r["session_cuts"] or r["sensitive_flags"]
               or r["redaction_changes"] or r["injection_flags"]]
    print(f"{len(reports)} case(s), {total} proposal(s); {len(changed)} case(s) would differ under current rules.")
    for r in changed:
        print(f"\n== {r['case']}")
        for c in r["risk_changes"]:
            print(f"  risk   #{c['num']}: {c['was']} -> {c['now']}  {c['command']}  ({', '.join(c['reasons'])})")
        for c in r["session_cuts"]:
            print(f"  cuts   #{c['num']}: {c['reason']}  {c['command']}")
        for c in r["sensitive_flags"]:
            print(f"  sens   #{c['num']}: {c['command']}  ({', '.join(c['reasons'])})")
        for c in r["redaction_changes"]:
            print(f"  redact: a message sent to the AI would now get {c['new_redactions']} more redaction(s)")
        for c in r["injection_flags"]:
            print(f"  inject: a message sent to the AI would now be flagged ({', '.join(c['reasons'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
