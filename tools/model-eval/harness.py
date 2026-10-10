"""Drive one evaluation run, turn by turn: the host agent plays the technician.

  harness.py start  --model M --scenario S --run-id ID     # opening message, prints the AI's turn
  harness.py reply  --run-id ID --file reply.json          # technician's next message, prints the next turn
  harness.py show   --run-id ID                            # print the last turn again
  harness.py verdict --run-id ID --file verdict.json       # the host agent's verdict, when the run ends

reply.json: {"message": "...", "results": [{"num": 1, "text": "output"}],
             "skipped": [{"num": 2, "note": "why (optional)"}]}
verdict.json: {"solved": true, "root_cause_found": true, "notes": "..."}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import flags as flags_mod  # noqa: E402


def run_dir(run_id: str) -> Path:
    return common.RUNS / run_id


def _turns(rd: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(rd.glob("turn-*.json"), key=lambda p: int(p.stem.split("-")[1]))]


def _events_since(case_dir: Path, offset: int) -> list[dict]:
    path = case_dir / "events.jsonl"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        f.seek(offset)
        return [json.loads(line) for line in f if line.strip()]


def _size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


async def _turn(rd: Path, meta: dict, engine, send: dict, sent_view: dict) -> dict:
    sessions = {s["id"]: s for s in (meta.get("sessions") or common.load_scenario(meta["scenario"])["sessions"])}
    history = _turns(rd)
    ev_offset = _size(engine.case.dir / "events.jsonl")
    before = {p.num for p in engine.queue.items}
    chat_len = len(engine.chat)
    out, elapsed = await common.run_turn(engine, meta["model"], f"{rd.name} turn {len(history) + 1}", **send)
    entry = next((e for e in engine.chat[chat_len:] if e.get("kind") == "assistant"), {})
    added = [p.to_dict() for p in engine.queue.items if p.num not in before]
    rounds = out["rounds"]
    events = _events_since(engine.case.dir, ev_offset)
    rec = {
        "turn": len(history) + 1,
        "model": meta["model"], "scenario": meta["scenario"], "run_id": rd.name,
        "sent": sent_view,
        "message": entry.get("text", ""),
        "reasoning_chars": len(entry.get("reasoning", "") or ""),
        "questions": [q.get("question", "") for q in entry.get("questions", [])],
        "question_options": [q.get("options", []) for q in entry.get("questions", [])],
        "proposals": [{k: p.get(k) for k in ("num", "session_id", "command", "purpose", "model_risk", "risk",
                                              "risk_reasons", "rollback", "cuts_session", "sensitive", "hidden",
                                              "group")} for p in added],
        "withdrawn": entry.get("withdrawn", []),
        "hypotheses": [{k: h.get(k) for k in ("id", "text", "confidence", "status")} for h in engine.hypotheses],
        "error": engine._last_turn_error,
        "rounds": [{"finish_reason": r.get("finish_reason"), "content_chars": len(r.get("content") or ""),
                    "tool_calls": [c.get("name") for c in r.get("tool_calls") or []], "usage": r.get("usage")}
                   for r in rounds],
        "cost": round(sum(common.cost_of(meta["model"], r.get("usage")) for r in rounds), 5),
        "seconds": round(elapsed, 1),
        "pending_after": [{"num": p.num, "session_id": p.session_id, "command": p.command}
                          for p in engine.queue.items if p.status == "pending"],
    }
    rec["flags"] = flags_mod.turn_flags({**entry, "error": engine._last_turn_error}, rounds, rec["proposals"],
                                        sessions, history, events)
    (rd / f"turn-{rec['turn']}.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False))
    return rec


def show(rec: dict) -> None:
    print(f"=== {rec['run_id']} turn {rec['turn']} ({rec['seconds']}s, ${rec['cost']:.4f}) ===")
    if rec["error"]:
        print(f"[ERROR] {rec['error']}")
    print("AI MESSAGE:\n" + (rec["message"] or "(no message)"))
    for q, opts in zip(rec["questions"], rec["question_options"]):
        print(f"QUESTION: {q}" + (f"  options: {opts}" if opts else ""))
    if rec["withdrawn"]:
        print(f"WITHDRAWN: {rec['withdrawn']}")
    for p in rec["proposals"]:
        print(f"NEW #{p['num']} [{p['session_id']}] risk={p['risk']} (model said {p['model_risk']}): {p['command']}")
        if p.get("purpose"):
            print(f"     purpose: {p['purpose']}")
        if p.get("rollback"):
            print(f"     rollback: {p['rollback']}")
    if rec["pending_after"]:
        print("PENDING IN QUEUE: " + ", ".join(f"#{p['num']}" for p in rec["pending_after"]))
    if rec["flags"]:
        print("FLAGS: " + json.dumps(rec["flags"], ensure_ascii=False))
    print(f"(spent so far, all runs: ${common.spent():.2f} of ${common.BUDGET:.2f})")


async def start(args) -> None:
    rd = run_dir(args.run_id)
    if rd.exists():
        raise SystemExit(f"{rd} exists; pick another --run-id")
    rd.mkdir(parents=True)
    scen = common.load_scenario(args.scenario)
    common.use_data_dir(rd / "xdg")
    engine = common.make_engine([])
    prov = engine.cfg.provider(common.PROVIDER)
    await engine._load_caps(prov)
    engine.new_case(scen["title"], "open", scen.get("case_notes", ""))
    common.add_sessions(engine, scen["sessions"], log=True)
    engine.select_model(common.PROVIDER, args.model)
    meta = {"model": args.model, "scenario": args.scenario, "case_id": engine.case.id}
    (rd / "meta.json").write_text(json.dumps(meta, indent=1))
    rec = await _turn(rd, meta, engine, {"message": scen["opening"]}, {"message": scen["opening"]})
    show(rec)


async def reply(args) -> None:
    rd = run_dir(args.run_id)
    meta = json.loads((rd / "meta.json").read_text())
    if (rd / "verdict.json").exists():
        raise SystemExit("This run has a verdict already.")
    data = json.loads(Path(args.file).read_text())
    scen = common.load_scenario(meta["scenario"])
    common.use_data_dir(rd / "xdg")
    engine = common.make_engine([])
    prov = engine.cfg.provider(common.PROVIDER)
    await engine._load_caps(prov)
    common.add_sessions(engine, scen["sessions"], log=False)
    engine.open_case(meta["case_id"])
    engine.select_model(common.PROVIDER, meta["model"])
    results = []
    for r in data.get("results", []):
        engine.update_item(int(r["num"]), status="ran")
        results.append({"num": int(r["num"]), "text": str(r.get("text", ""))})
    for s in data.get("skipped", []):
        engine.update_item(int(s["num"]), status="skipped", note=str(s.get("note", "")))
        results.append({"num": int(s["num"]), "text": ""})
    send = {"message": data.get("message", ""), "results": results}
    rec = await _turn(rd, meta, engine, send, data)
    show(rec)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--model", required=True)
    s.add_argument("--scenario", required=True)
    s.add_argument("--run-id", required=True)
    r = sub.add_parser("reply")
    r.add_argument("--run-id", required=True)
    r.add_argument("--file", required=True)
    w = sub.add_parser("show")
    w.add_argument("--run-id", required=True)
    v = sub.add_parser("verdict")
    v.add_argument("--run-id", required=True)
    v.add_argument("--file", required=True)
    args = ap.parse_args()
    if args.cmd == "start":
        asyncio.run(start(args))
    elif args.cmd == "reply":
        asyncio.run(reply(args))
    elif args.cmd == "show":
        turns = _turns(run_dir(args.run_id))
        show(turns[-1])
    else:
        verdict = json.loads(Path(args.file).read_text())
        verdict["turns"] = len(_turns(run_dir(args.run_id)))
        (run_dir(args.run_id) / "verdict.json").write_text(json.dumps(verdict, indent=1))
        print("verdict saved")


if __name__ == "__main__":
    main()
