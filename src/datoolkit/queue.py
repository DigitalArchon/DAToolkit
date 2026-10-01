"""The command review queue. Proposals only ever become terminal input through a user action."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields

from .safety import hidden, risk
from .safety.sensitive import sensitive

# pending -> ran | inserted | skipped -> sent; pending -> withdrawn (the AI took it back)
STATUSES = ("pending", "ran", "inserted", "skipped", "sent", "withdrawn")


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
    ran_at: float | None = None          # time.time() when Run/Insert was clicked
    capture_start: int | None = None     # byte offset into the session transcript at that moment
    rollback: str = ""                   # how to undo it (required from the model for changes)
    group: str = ""                      # paired probes: items with the same group run together
    recipe: str = ""                     # recipe id this step came from
    recipe_key: str = ""                 # stable step key (baseline diffs)
    watch: bool = False                  # command was wrapped in a bounded watch loop
    cuts_session: str = ""               # local rule: this would cut the session it runs in
    dry_run_of: int | None = None        # this item rehearses another item
    sensitive: list[str] = field(default_factory=list)  # local rules: may expose secrets or private data
    review: dict = field(default_factory=dict)          # second opinion on this exact command (see Engine)
    hidden: list[str] = field(default_factory=list)     # invisible characters taken out of the command

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
            fallback_session: str = "", session_kinds: dict[str, str] | None = None,
            insert_before: int | None = None) -> list[Proposal]:
        """Queue proposals. An unknown or missing session id falls back to `fallback_session`
        (the only open session, when there is exactly one). `session_kinds` (id -> kind) lets
        the blast-radius rule see which session a command would cut."""
        added = []
        for raw in raw_items:
            command, removed = hidden.clean(str(raw.get("command", "")))
            command = command.strip()
            if not command:
                continue
            model_risk = str(raw.get("risk", "modifying"))
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
                risk="modifying",
                original_command=command,
                rollback=str(raw.get("rollback", "") or ""),
                group=str(raw.get("group", "") or ""),
                recipe=str(raw.get("recipe", "") or ""),
                recipe_key=str(raw.get("recipe_key", "") or ""),
                dry_run_of=raw.get("dry_run_of"),
                hidden=removed,
            )
            self._classify(p, session_kinds)
            self._next += 1
            if insert_before is not None:
                self.items.insert(self.items.index(self.get(insert_before)), p)
            else:
                self.items.append(p)
            added.append(p)
        return added

    @staticmethod
    def _classify(p: Proposal, session_kinds: dict[str, str] | None) -> None:
        p.risk, p.risk_reasons = risk.effective(p.model_risk, p.command)
        if p.dry_run_of is not None and p.risk != "disruptive":
            # a rehearsal (apt -s, rsync -n, -WhatIf, plan) trips the same local rules as the
            # real command; it is read-only by construction, but never below disruptive
            p.risk, p.risk_reasons = "read_only", []
        p.sensitive = sensitive(p.command)
        p.review = {}                    # a review was of the old command or target
        cut = risk.session_impact(p.command, (session_kinds or {}).get(p.session_id, "local"))
        p.cuts_session = cut or ""
        if cut:
            p.risk = "disruptive"
            if cut not in p.risk_reasons:
                p.risk_reasons = p.risk_reasons + [f"cuts this session: {cut}"]

    def get(self, num: int) -> Proposal:
        for p in self.items:
            if p.num == num:
                return p
        raise KeyError(f"No queue item #{num}")

    def update(self, num: int, *, command: str | None = None, session_id: str | None = None,
               status: str | None = None, note: str | None = None,
               session_kinds: dict[str, str] | None = None, watch: bool | None = None) -> Proposal:
        p = self.get(num)
        command, removed = hidden.clean(command) if command is not None else (None, [])
        if command is not None and command.strip() and p.status == "pending":
            p.command = command.strip()
            if removed:              # otherwise keep the note about what the AI's text carried
                p.hidden = removed
            if watch is not None:
                p.watch = watch
            self._classify(p, session_kinds)
        if session_id is not None and p.status == "pending":
            p.session_id = session_id
            self._classify(p, session_kinds)
        if status is not None:
            if status not in STATUSES:
                raise ValueError(f"Bad status {status}")
            if status in ("ran", "inserted") and p.status == "pending":
                p.ran_at = time.time()
            elif status == "pending":
                p.ran_at = p.capture_start = None
            p.status = status
        if note is not None:
            p.note = note
        return p

    def reorder(self, nums: list[int]) -> list[int]:
        """Put the listed pending items in the given order, in the slots those items occupy
        now; everything else stays where it is. Returns the numbers actually reordered."""
        chosen = []
        for n in nums:
            try:
                p = self.get(int(n))
            except (KeyError, ValueError, TypeError):
                continue
            if p.status == "pending" and p not in chosen:
                chosen.append(p)
        slots = sorted(self.items.index(p) for p in chosen)
        for slot, p in zip(slots, chosen):
            self.items[slot] = p
        return [p.num for p in chosen]

    def move(self, num: int, delta: int) -> None:
        i = self.items.index(self.get(num))
        j = max(0, min(len(self.items) - 1, i + delta))
        self.items.insert(j, self.items.pop(i))

    def to_list(self) -> list[dict]:
        return [p.to_dict() for p in self.items]

    @classmethod
    def from_list(cls, items: list[dict]) -> "Queue":
        """Rebuild a queue saved with to_list() (case resume)."""
        q = cls()
        known = {f.name for f in fields(Proposal)}
        for d in items:
            p = Proposal(**{k: v for k, v in d.items() if k in known})
            if p.review.get("status") == "checking":
                p.review = {}            # the app closed while it was being reviewed
            if p.status == "pending":    # saved before commands were cleaned on the way in
                command, removed = hidden.clean(p.command)
                if removed:
                    p.command, p.original_command, p.hidden = command.strip(), command.strip(), removed
                    cls._classify(p, None)
            q.items.append(p)
        q._next = max((p.num for p in q.items), default=0) + 1
        return q
