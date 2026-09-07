"""The state machines (spec R-4; addendum A canon) with guarded, journaled transitions.

* execution   (record kind ``run``,          field ``state``)
* review      (record kind ``component``,    field ``state``)
* acceptance  (record kind ``acceptance``,   field ``state``)
* stop reason (record kind ``run``,          field ``data['stop_reason']``)
* canon       (record kind ``concept_plan``, field ``state``)  the fifth machine, concept stage (design Section 14)

Rules enforced here, not by prompts (R-1, R-5): only ``engine`` and ``user`` actors may transition;
an illegal transition raises ``IllegalTransition`` and writes nothing; guards read facts the engine
computed from records (``context``), never agent text.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .ids import utc_now
from .records import AcceptanceState, CanonState, ExecutionState, Record, ReviewState, StopReason
from .store import Store, actor_kind_of

Guard = Callable[[Record, dict[str, Any], dict[str, Any]], str | None]


class IllegalTransition(Exception):
    pass


@dataclass
class Machine:
    name: str
    record_kind: str
    states: tuple[str, ...]
    transitions: dict[tuple[str, str], Guard | None] = field(default_factory=dict)

    def allow(self, src: str, dst: str, guard: Guard | None = None) -> None:
        self.transitions[(src, dst)] = guard


def _ctx_true(key: str, message: str) -> Guard:
    def guard(_rec: Record, ctx: dict[str, Any], _inputs: dict[str, Any]) -> str | None:
        return None if ctx.get(key) is True else message
    return guard


def _ctx_false(key: str, message: str) -> Guard:
    def guard(_rec: Record, ctx: dict[str, Any], _inputs: dict[str, Any]) -> str | None:
        return message if ctx.get(key) is True else None
    return guard


def _all(*guards: Guard) -> Guard:
    def guard(rec: Record, ctx: dict[str, Any], inputs: dict[str, Any]) -> str | None:
        for g in guards:
            msg = g(rec, ctx, inputs)
            if msg:
                return msg
        return None
    return guard


# --- execution ------------------------------------------------------------------

EXECUTION = Machine("execution", "run", tuple(s.value for s in ExecutionState))
_ACTIVE = ("running", "waiting_for_provider", "rendering", "waiting_for_user", "recovering")
_not_limited = _ctx_false("limit_reached", "a limit is reached; raise the limit before resuming")
EXECUTION.allow("idle", "running", _ctx_true("preflight_ok", "preflight has not passed"))
EXECUTION.allow("running", "waiting_for_provider")
EXECUTION.allow("waiting_for_provider", "running")
EXECUTION.allow("running", "rendering")
EXECUTION.allow("rendering", "running")
EXECUTION.allow("running", "waiting_for_user")
EXECUTION.allow("waiting_for_user", "running", _not_limited)
for _s in _ACTIVE:
    EXECUTION.allow(_s, "paused")
EXECUTION.allow("paused", "running", _not_limited)
for _s in ("idle", "paused", *_ACTIVE):
    EXECUTION.allow(_s, "cancelled", _ctx_true("inflight_reconciled", "in-flight operations are not reconciled"))
    EXECUTION.allow(_s, "failed")
for _s in ("paused", *_ACTIVE):
    if _s != "recovering":
        EXECUTION.allow(_s, "recovering")
EXECUTION.allow("recovering", "running")
EXECUTION.allow("recovering", "paused")

STOP_REASONS: tuple[str, ...] = tuple(s.value for s in StopReason)
_STOP_STATES = {"paused", "cancelled", "failed", "waiting_for_user"}
_REASONS_FOR_STATE = {
    "cancelled": {"user_cancel"},
    "failed": {"execution_failure"},
    "waiting_for_user": {"ready_for_user_review", "missing_evidence"},
    "paused": set(STOP_REASONS),
}
_CLEARS_REASON = {"running"}

# --- review ---------------------------------------------------------------------

REVIEW = Machine("review", "component", tuple(s.value for s in ReviewState))


def _has_open_findings(_rec: Record, ctx: dict[str, Any], _inputs: dict[str, Any]) -> str | None:
    return None if int(ctx.get("open_findings", 0)) >= 1 else "no open findings were recorded"


def _ready_guard(_rec: Record, ctx: dict[str, Any], _inputs: dict[str, Any]) -> str | None:
    if ctx.get("last_output_malformed"):
        return "the last agent output was malformed; a parse failure never advances review (R-5)"
    if int(ctx.get("open_findings", 1)) != 0:
        return "open findings remain"
    if ctx.get("renders_fresh") is not True:
        return "renders are stale, missing, or mismatched (R-64)"
    if ctx.get("coverage_met") is not True:
        return "review coverage is incomplete (R-72)"
    if int(ctx.get("pending_ops", 1)) != 0:
        return "operations are still pending"
    return None


REVIEW.allow("unreviewed", "findings_open", _has_open_findings)
REVIEW.allow("unreviewed", "ready_for_user_review", _ready_guard)
REVIEW.allow("findings_open", "changes_required")
REVIEW.allow("findings_open", "ready_for_user_review", _ready_guard)
REVIEW.allow("changes_required", "findings_open", _has_open_findings)
REVIEW.allow("changes_required", "ready_for_user_review", _ready_guard)
for _s in ("findings_open", "changes_required", "ready_for_user_review"):
    REVIEW.allow(_s, "unreviewed")

# --- acceptance -----------------------------------------------------------------

ACCEPTANCE = Machine("acceptance", "acceptance", tuple(s.value for s in AcceptanceState))


def _accept_guard(_rec: Record, ctx: dict[str, Any], inputs: dict[str, Any]) -> str | None:
    if ctx.get("_actor_kind") != "user":
        return "only the user accepts a component (R-75)"
    for key in ("revision_id", "evidence_versions", "dependency_versions"):
        if key not in inputs:
            return f"acceptance must bind {key} (R-6)"
    return None


def _supersede_guard(_rec: Record, _ctx: dict[str, Any], inputs: dict[str, Any]) -> str | None:
    return None if inputs.get("supersede_reason") else "supersede_reason is required"


ACCEPTANCE.allow("unaccepted", "accepted_at_revision", _accept_guard)
ACCEPTANCE.allow("accepted_at_revision", "superseded", _supersede_guard)
_ACCEPTANCE_COPIES = {
    "accepted_at_revision": ("revision_id", "evidence_versions", "dependency_versions"),
    "superseded": ("supersede_reason", "superseded_by"),
}

# --- canon (concept stage, design Section 14; addendum R-95, R-99 to R-103) --------------

CANON = Machine("canon", "concept_plan", tuple(s.value for s in CanonState))
_no_open_conflicts = _ctx_false("_has_open_conflicts", "an evidence conflict is open; resolve it or regenerate (R-95)")


def _conflicts_guard(rec: Record, ctx: dict[str, Any], inputs: dict[str, Any]) -> str | None:
    ctx["_has_open_conflicts"] = int(ctx.get("open_conflicts", 0) or 0) > 0
    return _no_open_conflicts(rec, ctx, inputs)


def _proceed_guard(rec: Record, ctx: dict[str, Any], inputs: dict[str, Any]) -> str | None:
    """Completing from a partial turnaround is the owner's explicit decision, recorded (R-103)."""
    if ctx.get("_actor_kind") != "user":
        return "only the user proceeds with a partial reference set (R-103)"
    if inputs.get("proceed") is not True:
        return "an explicit proceed=True is required to accept a partial set (R-103)"
    return _conflicts_guard(rec, ctx, inputs)


CANON.allow("no_canon", "anchor_pending", _ctx_true("anchor_requests_open", "no anchor candidate request is open"))
CANON.allow("no_canon", "anchor_approved", _ctx_true("anchor_approved", "no approved anchor exists"))
CANON.allow("anchor_pending", "anchor_approved", _ctx_true("anchor_approved", "no approved anchor exists"))
CANON.allow("anchor_approved", "anchor_pending")            # the anchor was rejected or superseded
CANON.allow("turnaround_pending", "anchor_pending")
CANON.allow("anchor_approved", "turnaround_pending", _ctx_true("canon_description", "no canon description is recorded (R-101)"))
CANON.allow("anchor_approved", "turnaround_approved",
            _all(_ctx_true("coverage_met", "the needed reference set is not covered by approved references (R-103)"),
                 _conflicts_guard))
CANON.allow("turnaround_pending", "turnaround_approved",
            _all(_ctx_true("coverage_met", "the needed reference set is not covered by approved references (R-103)"),
                 _conflicts_guard))
CANON.allow("turnaround_approved", "turnaround_pending")    # a view was rejected or superseded after approval
CANON.allow("turnaround_pending", "complete", _proceed_guard)
CANON.allow("turnaround_approved", "complete", _conflicts_guard)
CANON.allow("complete", "turnaround_pending")               # reopened by a rejection after hand-off (recorded)

MACHINES: dict[str, Machine] = {m.record_kind: m for m in (EXECUTION, REVIEW, ACCEPTANCE, CANON)}


def machine_for(record: Record) -> Machine:
    try:
        return MACHINES[record.kind]
    except KeyError:
        raise IllegalTransition(f"record kind {record.kind!r} has no state machine") from None


def transition(store: Store, record: Record, to_state: str, *, actor: str, reason: str,
               context: dict[str, Any] | None = None, inputs: dict[str, Any] | None = None,
               evidence: list[Any] | None = None, stop_reason: str | None = None,
               run_id: str | None = None) -> Record:
    """Apply a guarded transition and journal it. Raises ``IllegalTransition`` without writing."""
    machine = machine_for(record)
    actor_kind = actor_kind_of(actor)
    if actor_kind not in ("engine", "user"):
        raise IllegalTransition(f"{actor!r} cannot transition state: only the engine and the user may (R-1)")
    ctx = dict(context or {})
    ctx["_actor_kind"] = actor_kind
    inp = dict(inputs or {})
    src = record.state
    if to_state not in machine.states:
        raise IllegalTransition(f"{machine.name}: unknown state {to_state!r}")
    if (src, to_state) not in machine.transitions:
        raise IllegalTransition(f"{machine.name}: {src} -> {to_state} is not allowed")
    guard = machine.transitions[(src, to_state)]
    if guard is not None:
        problem = guard(record, ctx, inp)
        if problem:
            raise IllegalTransition(f"{machine.name}: {src} -> {to_state} refused: {problem}")

    journal_inputs: dict[str, Any] = {"reason": reason, **inp}
    if machine is EXECUTION:
        if to_state in _STOP_STATES:
            if stop_reason not in STOP_REASONS:
                raise IllegalTransition(f"execution: stopping into {to_state!r} requires a stop reason from {STOP_REASONS}")
            if stop_reason not in _REASONS_FOR_STATE[to_state]:
                raise IllegalTransition(f"execution: stop reason {stop_reason!r} does not fit state {to_state!r}")
            record.data["stop_reason"] = stop_reason
            journal_inputs["stop_reason"] = stop_reason
        elif to_state in _CLEARS_REASON:
            record.data["stop_reason"] = None
    elif machine is ACCEPTANCE:
        for key in _ACCEPTANCE_COPIES.get(to_state, ()):
            if key in inp:
                record.data[key] = inp[key]
        if to_state == "accepted_at_revision":
            record.data["accepted_by"] = actor
            record.data["accepted_at"] = utc_now()
    elif machine is CANON and to_state == "complete" and inp.get("proceed") is True:
        record.data["proceeded_partial"] = {"by": actor, "at": utc_now(), "missing": list(inp.get("missing") or []),
                                            "reason": reason}
    record.state = to_state
    return store.upsert(record, actor=actor, event=f"{record.kind}.transition", inputs=journal_inputs,
                        evidence=evidence, outcome=to_state, run_id=run_id)
