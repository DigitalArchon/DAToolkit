"""Shared setup for the model evaluation: a real Engine in this process, a throwaway data
directory per run, stub sessions with no terminal behind them, and a spend ledger.

Nothing here starts the app server or opens a port; the API key comes from the keyring entry
the app itself uses (provider "NanoGPT")."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = Path(os.environ.get("EVAL_RUNS") or HERE / "runs")    # one runs folder (and spend ledger) for every checkout
SCENARIOS = HERE / "scenarios"
LEDGER = RUNS / "ledger.jsonl"
PRICES = RUNS / "prices.json"
BUDGET = float(os.environ.get("EVAL_BUDGET_USD", "30"))
PROVIDER = "NanoGPT"
BASE_URL = "https://nano-gpt.com/api/v1"


def use_data_dir(path: Path) -> None:
    """Point the app's config/data dirs at `path` (before any case is created)."""
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state"),
                     ("XDG_CACHE_HOME", "cache")):
        (path / sub).mkdir(parents=True, exist_ok=True)
        os.environ[var] = str(path / sub)


@dataclass
class StubSession:
    """Looks like a session to the engine's roster; there is no process behind it."""
    id: str
    name: str
    kind: str
    target: str = ""
    shell: str = ""
    os_hint: str = ""
    host_name: str = ""
    exited: bool = False
    device: str = ""
    link: str = "auto"

    def roster(self) -> dict:
        return {"id": self.id, "name": self.name, "kind": self.kind, "target": self.target, "shell": self.shell,
                "os_hint": self.os_hint, "exited": self.exited, "host_name": self.host_name,
                "device": self.device, "link": self.link}


SHELLS = {"ssh": "remote shell/CLI", "winrm": "PowerShell (remoting, single-line)", "local": "bash"}


def add_sessions(engine, sessions: list[dict], log: bool) -> None:
    for i, s in enumerate(sessions, 1):
        stub = StubSession(id=s["id"], name=s.get("name", s["id"]), kind=s["kind"], target=s.get("target", ""),
                           shell=s.get("shell", SHELLS.get(s["kind"], "")), os_hint=s.get("os_hint", ""),
                           host_name=s.get("name", s["id"]), device=f"dev{i}")
        engine.sessions.sessions[stub.id] = stub
        if log:
            engine.log("session_opened", **stub.roster())


def slug(model: str) -> str:
    """anthropic/claude-opus-5.5 -> claude-opus-5.5"""
    return model.split("/")[-1].replace(":", "-")


def load_scenario(name: str) -> dict:
    return tomllib.loads((SCENARIOS / f"{name}.toml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- spend


def prices() -> dict:
    """model id -> pricing, from NanoGPT's public model list (cached for the session)."""
    if PRICES.exists() and time.time() - PRICES.stat().st_mtime < 86400:
        return json.loads(PRICES.read_text())
    import httpx
    data = httpx.get(f"{BASE_URL}/models", params={"detailed": "true"}, timeout=60).json()["data"]
    out = {m["id"]: m.get("pricing") or {} for m in data}
    RUNS.mkdir(parents=True, exist_ok=True)
    PRICES.write_text(json.dumps(out))
    return out


def cost_of(model: str, usage: dict | None) -> float:
    """What NanoGPT charged (it reports `cost` in the usage), else an estimate from list prices."""
    if not usage:
        return 0.0
    if isinstance(usage.get("cost"), (int, float)):
        return float(usage["cost"])
    p = prices().get(model.split(":")[0]) or prices().get(model) or {}
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") or 0
    prompt = (usage.get("prompt_tokens") or 0) - cached
    return (prompt * p.get("prompt", 3) + (usage.get("completion_tokens") or 0) * p.get("completion", 15)
            + cached * p.get("cacheReadInputPer1kTokens", 0) * 1000) / 1e6


def spent() -> float:
    if not LEDGER.exists():
        return 0.0
    return sum(json.loads(line)["cost"] for line in LEDGER.read_text().splitlines() if line.strip())


def record_spend(model: str, cost: float, what: str) -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps({"ts": time.time(), "model": model, "cost": round(cost, 6), "what": what}) + "\n")


def check_budget() -> None:
    total = spent()
    if total >= BUDGET:
        raise SystemExit(f"Budget reached: US${total:.2f} of US${BUDGET:.2f} spent. Raise EVAL_BUDGET_USD to go on.")


# ---------------------------------------------------------------- engine


def make_engine(events: list[dict]):
    """An Engine with a NanoGPT provider and the evaluation's settings. Call after use_data_dir."""
    from datoolkit.config import Config, Provider, Settings
    from datoolkit.engine import Engine

    settings = Settings(search_mode="ask", auto_review="off", prompt_cache="1h",
                        generation={"temperature": 0.3, "reasoning_effort": "low"})
    cfg = Config(providers=[Provider(PROVIDER, BASE_URL)], settings=settings)
    holder: dict = {}

    def emit(ev: dict) -> None:
        events.append(ev)
        eng = holder.get("engine")
        # web search and research wait for the technician; the evaluation declines them, so every
        # model works from the same evidence, and counts the requests
        if ev.get("type") == "search" and ev.get("search", {}).get("status") == "awaiting":
            asyncio.get_running_loop().call_soon(eng.answer_search, ev["search"]["id"], False)
        if ev.get("type") == "research" and ev.get("research", {}).get("status") == "awaiting":
            asyncio.get_running_loop().call_soon(eng.answer_research, ev["research"]["id"], False)

    rt = Path(os.environ["XDG_STATE_HOME"]) / "rt"
    rt.mkdir(parents=True, exist_ok=True)
    engine = Engine(cfg, emit, rt, save_config=lambda c: None)
    holder["engine"] = engine
    return engine


async def run_turn(engine, model: str, what: str, **send) -> tuple[dict, float]:
    """Send one technician message and wait for the AI's turn to end. Returns (turn_end event, seconds)."""
    check_budget()
    case_dir = engine.case.dir
    req_log = case_dir / "requests.jsonl"
    offset = req_log.stat().st_size if req_log.exists() else 0
    t0 = time.monotonic()
    engine.send(**send)
    await engine._turn
    elapsed = time.monotonic() - t0
    rounds = []
    if req_log.exists():
        with req_log.open(encoding="utf-8") as f:
            f.seek(offset)
            for line in f:
                rec = json.loads(line)
                if rec.get("purpose") != "chat":
                    continue
                rounds.append(rec["response"])
    total = sum(cost_of(model, r.get("usage")) for r in rounds)
    if total:
        record_spend(model, total, what)
    return {"rounds": rounds}, elapsed
