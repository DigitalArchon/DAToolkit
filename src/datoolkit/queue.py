"""The command review queue. Proposals only ever become terminal input through a user action."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .safety import risk

# pending -> ran | inserted | skipped -> sent
STATUSES = ("pending", "ran", "inserted", "skipped", "sent")


@dataclass
class Proposal:
    num: int
    call_id: str
    session_id: str
    command: str
    purpose: str
    model_risk: str
    risk: str
    risk_reasons: list[str] = field(default_factory=list)
    original_command: str = ""
    status: str = "pending"
    note: str = ""

    @property
    def edited(self) -> bool:
        return self.command != self.original_command

    def to_dict(self) -> dict:
        d = asdict(self)
        d["edited"] = self.edited
        return d


class Queue:
    def __init__(self) -> None:
        self.items: list[Proposal] = []
        self._next = 1

    def add(self, call_id: str, raw_items: list[dict], known_sessions: set[str] | None = None,
            fallback_session: str = "") -> list[Proposal]:
        """Queue proposals. An unknown or missing session id falls back to `fallback_session`
        (the only open session, when there is exactly one)."""
        added = []
        for raw in raw_items:
            command = str(raw.get("command", "")).strip()
            if not command:
                continue
            model_risk = str(raw.get("risk", "modifying"))
            level, reasons = risk.effective(model_risk, command)
            session_id = str(raw.get("session_id") or "")
            if fallback_session and known_sessions is not None and session_id not in known_sessions:
                session_id = fallback_session
            p = Proposal(
                num=self._next,
                call_id=call_id,
                session_id=session_id,
                command=command,
                purpose=str(raw.get("purpose", "")),
                model_risk=model_risk,
                risk=level,
                risk_reasons=reasons,
                original_command=command,
            )
            self._next += 1
            self.items.append(p)
            added.append(p)
        return added

    def get(self, num: int) -> Proposal:
        for p in self.items:
            if p.num == num:
                return p
        raise KeyError(f"No queue item #{num}")

    def update(self, num: int, *, command: str | None = None, session_id: str | None = None,
               status: str | None = None, note: str | None = None) -> Proposal:
        p = self.get(num)
        if command is not None and command.strip() and p.status == "pending":
            p.command = command.strip()
            p.risk, p.risk_reasons = risk.effective(p.model_risk, p.command)
        if session_id is not None and p.status == "pending":
            p.session_id = session_id
        if status is not None:
            if status not in STATUSES:
                raise ValueError(f"Bad status {status}")
            p.status = status
        if note is not None:
            p.note = note
        return p

    def move(self, num: int, delta: int) -> None:
        i = next(i for i, p in enumerate(self.items) if p.num == num)
        j = max(0, min(len(self.items) - 1, i + delta))
        self.items.insert(j, self.items.pop(i))

    def to_list(self) -> list[dict]:
        return [p.to_dict() for p in self.items]
