"""Training provider: a scripted fake model for learning the Run / Skip / Send loop.

A scenario is a list of turns; each turn has what the "AI" says and what it proposes. The
provider advances one turn per technician message, regardless of content, so a new
technician can practise the workflow with no API cost and no data leaving the machine.
Its tier is "local". Base URL: training://<scenario-id> (see training/scenarios)."""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path
from typing import AsyncIterator

from .client import ToolCall, TurnResult

SCHEME = "training://"
SCENARIO_DIR = Path(__file__).resolve().parents[1] / "training" / "scenarios"


def is_training_url(base_url: str) -> bool:
    return base_url.startswith(SCHEME)


def scenarios(extra_dir: Path | None = None) -> list[dict]:
    out = []
    dirs = [SCENARIO_DIR] + ([extra_dir] if extra_dir else [])
    for d in dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.toml")):
            try:
                with path.open("rb") as f:
                    data = tomllib.load(f)
                out.append({"id": path.stem, "title": data.get("title", path.stem), "turns": len(data.get("turn", [])),
                            "path": str(path)})
            except (OSError, ValueError):
                continue
    return out


def load_scenario(scenario_id: str, extra_dir: Path | None = None) -> dict:
    for d in [SCENARIO_DIR] + ([extra_dir] if extra_dir else []):
        path = d / f"{scenario_id}.toml"
        if path.exists():
            with path.open("rb") as f:
                return tomllib.load(f)
    raise FileNotFoundError(f"No training scenario {scenario_id}")


class TrainingClient:
    """Same interface as LLMClient. The turn index is the number of technician messages in
    the conversation, so resuming a case continues the scenario."""

    def __init__(self, base_url: str, extra_dir: Path | None = None):
        self.scenario_id = base_url.removeprefix(SCHEME).strip("/") or "disk-full"
        self.extra_dir = extra_dir
        self._scenario = None

    @property
    def scenario(self) -> dict:
        if self._scenario is None:
            self._scenario = load_scenario(self.scenario_id, self.extra_dir)
        return self._scenario

    async def list_models(self) -> list[str]:
        return [f"training/{s['id']}" for s in scenarios(self.extra_dir)]

    async def attest(self):  # pragma: no cover - never called: training is not private mode
        raise RuntimeError("training provider has no enclave")

    async def stream(self, model: str, messages: list[dict], tools: list[dict] | None = None
                     ) -> AsyncIterator[tuple[str, object]]:
        n_user = sum(1 for m in messages if m.get("role") == "user")
        turns = self.scenario.get("turn", [])
        turn = turns[min(n_user, len(turns)) - 1] if turns else {}
        finished = n_user > len(turns)
        text = (self.scenario.get("finished", "Scenario complete. Start a new case to run it again.")
                if finished else turn.get("say", ""))
        sessions = _session_ids(messages)
        for word in text.split(" "):
            await asyncio.sleep(0.01)
            yield "text", word + " "
        result = TurnResult(content=text, finish_reason="stop")
        items = [] if finished else turn.get("propose", [])
        if items and tools:
            payload = {"items": [{"session_id": sessions[0] if sessions else "local", "command": it["command"],
                                  "purpose": it.get("purpose", ""), "risk": it.get("risk", "read_only"),
                                  "rollback": it.get("rollback", "")} for it in items]}
            result.tool_calls.append(ToolCall(id=f"train_{n_user}", name="propose_commands", arguments=json.dumps(payload)))
            result.finish_reason = "tool_calls"
        hyps = turn.get("hypotheses") if not finished else None
        if hyps and tools and any(t["function"]["name"] == "update_hypotheses" for t in tools):
            result.tool_calls.append(ToolCall(id=f"trainh_{n_user}", name="update_hypotheses",
                                              arguments=json.dumps({"items": hyps})))
        result.usage = {"prompt_tokens": 500 * n_user, "completion_tokens": len(text) // 4, "total_tokens": 500 * n_user + len(text) // 4}
        yield "done", result

    async def complete(self, model: str, messages: list[dict]) -> str:
        return self.scenario.get("summary", "Training scenario: no summary available.")


def _session_ids(messages: list[dict]) -> list[str]:
    """Session ids from the roster in the system prompt (the real model reads the same text)."""
    import re

    for m in messages:
        if m.get("role") == "system":
            ids = re.findall(r"- id `([^`]+)`: (?!.*CLOSED)", m.get("content", ""))
            return ids
    return []
