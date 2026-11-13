"""The command review queue. Proposals only ever become terminal input through a user action."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field, fields

from .safety import hidden, risk
from .safety.dryrun import dry_run
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
    rehearsal_reads: bool = False        # ...and is new text that only reads (safety/dryrun.py's `reader`)
    sensitive: list[str] = field(default_factory=list)  # local rules: may expose secrets or private data
    review: dict = field(default_factory=dict)          # second opinion on this exact command (see Engine)
    hidden: list[str] = field(default_factory=list)     # invisible characters taken out of the command

    @property
    def edited(self) -> bool:
        return self.command != self.original_command

    @property
    def unconfirmed(self) -> bool:
        """The AI called it read-only and nothing in the local rules says otherwise, but they
        don't know it as a command that only reads (risk.UNKNOWN): it is shown as modifying, and
        what is said of it is that, not that a change lacks its rollback."""
        return self.model_risk == "read_only" and self.risk_reasons == [risk.UNKNOWN]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["edited"] = self.edited
        d["unconfirmed"] = self.unconfirmed
        return d


def _text(raw: dict, key: str, i: int) -> str:
    """A text field of proposed item number `i`: missing or null is empty, a number is its
    digits, and a list or an object is an error (as text it would be Python's own notation)."""
    value = raw.get(key)
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise TypeError(f"item {i}: {key} must be a string")
    return str(value)


class Queue:
    def __init__(self) -> None:
        self.items: list[Proposal] = []
        self._next = 1
        self._rehearsals: dict[int, tuple] = {}      # item number -> (its command, dry_run() of that command)

    def add(self, call_id: str, raw_items: list[dict], known_sessions: set[str] | None = None,
            fallback_session: str = "", session_kinds: dict[str, str] | None = None,
            insert_before: int | None = None, dry_run_of: int | None = None) -> list[Proposal]:
        """Queue proposals. An unknown or missing session id falls back to `fallback_session`
        (the only open session, when there is exactly one). `session_kinds` (id -> kind) lets
        the blast-radius rule see which session a command would cut. `dry_run_of` marks the
        items as rehearsals of that item; it is never read from `raw_items`, which may come
        from the model. A rehearsal is rated like any command; only one that is the reader
        safety/dryrun.py itself offers for that item is taken as read-only at its word."""
        # The whole call is read before any of it is queued: an item that can't be read (not an
        # object, a command that isn't text) raises here, with nothing added. Until Nov 2026 the
        # items before it were queued already and the error then hid them from the chat and the
        # log, and the AI, told its call was invalid, sent them again.
        if insert_before is not None:
            self.get(insert_before)
        offer = dry_run(self.get(dry_run_of).command) if dry_run_of is not None else None
        built, num = [], self._next
        for i, raw in enumerate(raw_items, 1):
            if not isinstance(raw, dict):
                raise TypeError(f"item {i} is not an object")
            if not isinstance(raw.get("command", ""), str):
                raise TypeError(f"item {i}: command must be a string")
            command, removed = hidden.clean(raw.get("command", ""))
            command = command.strip()
            if not command:
                continue
            model_risk = _text(raw, "risk", i) or "modifying"
            session_id = _text(raw, "session_id", i)
            if fallback_session and known_sessions is not None and session_id not in known_sessions:
                session_id = fallback_session
            p = Proposal(
                num=num,
                call_id=call_id,
                session_id=session_id,
                command=command,
                purpose=_text(raw, "purpose", i),
                model_risk=model_risk,
                risk="modifying",
                original_command=command,
                rollback=_text(raw, "rollback", i),
                group=_text(raw, "group", i),
                recipe=_text(raw, "recipe", i),
                recipe_key=_text(raw, "recipe_key", i),
                dry_run_of=dry_run_of,
                rehearsal_reads=bool(offer and offer.reader and offer[0].strip() == command),
                hidden=removed,
            )
            self._classify(p, session_kinds)
            num += 1
            built.append(p)
        self._next = num
        if insert_before is not None:
            at = self.items.index(self.get(insert_before))
            self.items[at:at] = built
        else:
            self.items.extend(built)
        return built

    @staticmethod
    def _classify(p: Proposal, session_kinds: dict[str, str] | None) -> None:
        # a WinRM console is PowerShell and nothing else: there a statement may begin with a variable
        kind = (session_kinds or {}).get(p.session_id, "local")
        p.risk, p.risk_reasons = risk.effective(p.model_risk, p.command, "powershell" if kind == "winrm" else "")
        if p.dry_run_of is not None and p.rehearsal_reads and p.risk != "disruptive" and p.command == p.original_command:
            # A rehearsal is rated by the rules above like any command: `Remove-Item -WhatIf` stays
            # modifying, `kubectl delete --dry-run=server` disruptive, and `apt -s` or `ls -ld` come
            # out read-only because the allow-list confirms them. Until Nov 2026 every rehearsal was
            # forced to read-only, and some ran the real command. The one kind still taken at its
            # word is new text that only reads (`crontab -l`, `sc.exe queryex`), which the rules
            # confirm anyway: never below disruptive, and only as made, not once it is edited.
            p.risk, p.risk_reasons = "read_only", []
        if p.recipe and p.unconfirmed and p.command == p.original_command:
            # A recipe's step is its author's text (a built-in recipe's, or one the technician
            # wrote), not the AI's: the AI can only name a recipe, and the engine takes the recipe
            # fields out of anything the AI proposes itself. Where the rules have nothing against a
            # step but don't know it as a reader (a loop over the users' crontabs, a port probe
            # through bash), the recipe's own label stands. A rule that raises it still does, and
            # so does an edit: then it is judged like any other command.
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
        # the same text again (Run saves the box first) is no edit: it would drop the review
        changed = command is not None and command.strip() and (command.strip() != p.command or removed)
        if changed and p.status == "pending":
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

    def rehearsal(self, p: Proposal) -> tuple[str, str]:
        """(command, description) the Dry run button would queue before `p`. Raises ValueError
        saying why none is offered."""
        if p.status != "pending":
            raise ValueError("Only pending items can be rehearsed.")
        if p.dry_run_of is not None:
            raise ValueError("This item is already a dry run.")
        if p.risk == "read_only":
            raise ValueError("This command is read-only already.")
        if any(q.dry_run_of == p.num and q.status == "pending" for q in self.items):
            raise ValueError(f"A dry run of #{p.num} is already queued.")
        # worked out once for each command an item has had: to_list() asks for every item's on every
        # queue event, and until Nov 2026 that ran the dry-run rules over the whole queue each time
        known = self._rehearsals.get(p.num)
        if known is None or known[0] != p.command:
            known = self._rehearsals[p.num] = (p.command, dry_run(p.command))
        dr = known[1]
        if not dr:
            raise ValueError("No dry-run form is known for this command.")
        return dr

    def _offer(self, p: Proposal) -> dict | None:
        try:
            cmd, desc = self.rehearsal(p)
        except ValueError:
            return None
        return {"command": cmd, "description": desc}

    def to_list(self) -> list[dict]:
        """Every item, with `dry_run` set to the rehearsal on offer for it (or None)."""
        return [p.to_dict() | {"dry_run": self._offer(p)} for p in self.items]

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
                # What is still to be run is rated by the rules in force now, not by those of the day it
                # was saved (until Nov 2026 a resumed case kept its old ratings): from the model's own
                # label up, as when it was queued. The review of this very command stands, and so does
                # what was found about the session it would cut: the sessions aren't open again yet.
                review, cut = ({} if removed else p.review), p.cuts_session
                cls._classify(p, None)
                p.review = review
                if cut:
                    p.cuts_session, p.risk = cut, "disruptive"
                    p.risk_reasons = p.risk_reasons + [f"cuts this session: {cut}"]
            q.items.append(p)
        q._next = max((p.num for p in q.items), default=0) + 1
        return q
