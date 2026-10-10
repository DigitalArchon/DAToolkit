"""Combine every run and probe under runs/ into results/summary.json and results/summary.md.

  report.py [--tag base]   # probes with this tag (scenario runs are grouped by run-id suffix)
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import flags as flags_mod  # noqa: E402

RESULTS = common.HERE / "results"
TURN_FLAGS = ["no_message", "no_message_nudge", "promise_without_call", "promise_nudge", "commands_in_prose_only",
              "bad_tool_calls", "risk_under_labelled", "missing_rollback", "shell_rule_breaches", "unknown_session",
              "reproposed_pending", "repeated_question", "error_or_empty", "tool_markup_in_text", "stale_reference"]


def _variant(run_id: str) -> str:
    """'glm-5.3--media-stutter--r1' -> 'base'; '...--r1-prompt' -> 'prompt'."""
    last = run_id.rsplit("--", 1)[-1]
    return last.split("-", 1)[1] if "-" in last else "base"


def _sessions(meta: dict) -> dict:
    return {s["id"]: s for s in (meta.get("sessions") or common.load_scenario(meta["scenario"])["sessions"])}


def collect():
    runs, probes = [], []
    for rd in sorted(common.RUNS.iterdir()):
        if not (rd / "meta.json").exists():
            continue
        meta = json.loads((rd / "meta.json").read_text())
        if meta.get("scenario") == "probe":
            if (rd / "probe.json").exists():
                probes.append(json.loads((rd / "probe.json").read_text()))
            continue
        turns = [json.loads(p.read_text()) for p in sorted(rd.glob("turn-*.json"), key=lambda p: int(p.stem.split("-")[1]))]
        for i, t in enumerate(turns):      # flag again with today's checks, so every run is measured alike
            kept = {k: v for k, v in t["flags"].items() if k in ("no_message_nudge", "promise_nudge", "rounds", "bad_tool_calls",
                                                                   "search_requests", "research_requests")}
            entry = {"text": t["message"], "questions": [{"question": q} for q in t["questions"]], "withdrawn": t["withdrawn"],
                     "error": t["error"]}
            fresh = flags_mod.turn_flags(entry, [], t["proposals"], _sessions(meta), turns[:i], [])
            t["flags"] = {**fresh, **kept}
        verdict = json.loads((rd / "verdict.json").read_text()) if (rd / "verdict.json").exists() else None
        runs.append({"run_id": rd.name, "model": meta["model"], "scenario": meta["scenario"], "variant": _variant(rd.name),
                     "turns": turns, "verdict": verdict})
    return runs, probes


def summarise(runs, probes, tag):
    models: dict[tuple[str, str], dict] = defaultdict(lambda: {"runs": [], "turns": []})
    for r in runs:
        if r["scenario"] == "toolpaths":
            continue
        m = models[(r["model"], r["variant"])]
        m["runs"].append(r)
        m["turns"] += r["turns"]
    out = {}
    for (model, variant), m in models.items():
        turns = m["turns"]
        n = len(turns) or 1
        verdicts = [r["verdict"] for r in m["runs"] if r["verdict"]]
        cache_read = sum((rd.get("usage") or {}).get("cache_read_input_tokens") or
                         ((rd.get("usage") or {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0
                         for t in turns for rd in t["rounds"])
        prompt = sum((rd.get("usage") or {}).get("prompt_tokens") or 0 for t in turns for rd in t["rounds"])
        out[f"{model}|{variant}"] = {
            "model": model, "variant": variant, "runs": len(m["runs"]), "with_verdict": len(verdicts),
            "solved": sum(1 for v in verdicts if v.get("solved")),
            "root_cause": sum(1 for v in verdicts if v.get("root_cause_found")),
            "quality": round(statistics.mean(v["quality"] for v in verdicts if v.get("quality")), 2) if verdicts else None,
            "turns_per_run": round(len(turns) / max(len(m["runs"]), 1), 1),
            "flag_rate": {f: round(sum(1 for t in turns if f in t["flags"]) / n, 3) for f in TURN_FLAGS},
            "cost": round(sum(t["cost"] for t in turns), 3),
            "median_seconds": statistics.median([t["seconds"] for t in turns]) if turns else None,
            "cache_read_share": round(cache_read / prompt, 3) if prompt else 0,
            "oddities": [f"{r['run_id']}: {o}" for r in m["runs"] if r["verdict"] for o in r["verdict"].get("oddities", [])],
            "scenarios": {r["run_id"]: {"solved": (r["verdict"] or {}).get("solved"), "quality": (r["verdict"] or {}).get("quality"),
                                        "turns": len(r["turns"])} for r in m["runs"]},
        }
    probe_out: dict = defaultdict(lambda: defaultdict(dict))
    for p in probes:
        if p.get("tag", "base") != tag and tag != "*":
            continue
        probe_out[f"{p['model']}|{p.get('tag', 'base')}"][p["size"]][p["probe"]] = {
            "score": p["scores"]["score"], "failed": [k for k, v in p["scores"].items() if v is False],
            "prompt_tokens": p["prompt_tokens"], "error": p["error"]}
    return out, {k: dict(v) for k, v in probe_out.items()}


def markdown(models, probes, total) -> str:
    lines = ["# DAToolkit model evaluation", "", f"Spend recorded in the ledger: US${total:.2f}", "",
             "## Scenario runs", "",
             "| Model | Variant | Runs | Solved | Root cause | Quality | Turns/run | No msg | Nudged | Promise w/o call | Prose cmds | Bad tool | Under-label | No rollback | Shell rule | Cost | Med s |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, m in sorted(models.items()):
        f = m["flag_rate"]
        lines.append(f"| {m['model']} | {m['variant']} | {m['runs']} | {m['solved']}/{m['with_verdict']} | {m['root_cause']}/{m['with_verdict']} "
                     f"| {m['quality']} | {m['turns_per_run']} | {f['no_message']:.0%} | {f['no_message_nudge']:.0%} | "
                     f"{f['promise_without_call']:.0%} | {f['commands_in_prose_only']:.0%} | {f['bad_tool_calls']:.0%} | "
                     f"{f['risk_under_labelled']:.0%} | {f['missing_rollback']:.0%} | {f['shell_rule_breaches']:.0%} | "
                     f"${m['cost']:.2f} | {m['median_seconds']} |")
    lines += ["", "Flag columns are the share of turns with that flag.", "", "## Context probes", "",
              "| Model | Size | follow-up | change | recall | Failed checks |", "|---|---|---|---|---|---|"]
    for k, sizes in sorted(probes.items()):
        for size in sorted(sizes):
            row = sizes[size]
            fails = "; ".join(f"{n}: {', '.join(v['failed'])}" for n, v in row.items() if v["failed"] or v["error"])
            toks = max(v["prompt_tokens"] for v in row.values())
            lines.append(f"| {k} | {size // 1000}k (~{toks // 1000}k sent) | "
                         + " | ".join(f"{row[n]['score']:.2f}" if n in row else "-" for n in ("followup", "change", "recall"))
                         + f" | {fails} |")
    lines += ["", "## Oddities noted by the host agents", ""]
    for k, m in sorted(models.items()):
        if m["oddities"]:
            lines.append(f"**{m['model']} ({m['variant']})**")
            lines += [f"- {o}" for o in m["oddities"]]
            lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="*")
    args = ap.parse_args()
    runs, probes = collect()
    models, probe_sum = summarise(runs, probes, args.tag)
    RESULTS.mkdir(exist_ok=True)
    total = common.spent()
    (RESULTS / "summary.json").write_text(json.dumps({"models": models, "probes": probe_sum, "spent": total}, indent=1))
    (RESULTS / "summary.md").write_text(markdown(models, probe_sum, total))
    print((RESULTS / "summary.md").read_text())


if __name__ == "__main__":
    main()
