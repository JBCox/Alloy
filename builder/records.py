"""Record types, quantities, and validation (spec R-2, R-25).

A record is ``kind`` + stable ``id`` + ``version`` + optional ``state`` + a JSON ``data`` dict.
``KIND_SPECS`` declares, per kind, the id prefix, the required data keys, and the allowed
states with the initial state. The store refuses records that fail ``validate_record``.

Quantities: ``Measured`` and ``Estimated`` carry a value and its source; ``UNKNOWN`` refuses
arithmetic so an unreported cost or token count can never silently become zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .ids import new_id, utc_now


# --- quantities ---------------------------------------------------------------

class _Unknown:
    """Singleton for values a provider or tool did not report."""

    is_known = False
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNKNOWN"

    def _refuse(self, *_args: Any) -> Any:
        raise TypeError("UNKNOWN quantity cannot be used in arithmetic; treat it as unreported, never as zero")

    __add__ = __radd__ = __sub__ = __rsub__ = __mul__ = __rmul__ = _refuse
    __truediv__ = __rtruediv__ = __lt__ = __le__ = __gt__ = __ge__ = _refuse

    def __eq__(self, other: object) -> bool:
        return other is self

    def __hash__(self) -> int:
        return id(self)


UNKNOWN = _Unknown()


@dataclass(frozen=True)
class Measured:
    value: float | int
    source: str = ""
    is_known = True


@dataclass(frozen=True)
class Estimated:
    value: float | int
    source: str = ""
    is_known = True


Quantity = Measured | Estimated | _Unknown


def quantity_to_json(q: Quantity) -> dict[str, Any]:
    if q is UNKNOWN:
        return {"kind": "unknown"}
    if isinstance(q, Measured):
        return {"kind": "measured", "value": q.value, "source": q.source}
    if isinstance(q, Estimated):
        return {"kind": "estimated", "value": q.value, "source": q.source}
    raise TypeError(f"not a quantity: {q!r}")


def quantity_from_json(d: dict[str, Any] | None) -> Quantity:
    if not d or d.get("kind") == "unknown":
        return UNKNOWN
    if d["kind"] == "measured":
        return Measured(d["value"], d.get("source", ""))
    if d["kind"] == "estimated":
        return Estimated(d["value"], d.get("source", ""))
    raise ValueError(f"unknown quantity kind: {d.get('kind')!r}")


# --- state enumerations (spec R-4) ---------------------------------------------

class ExecutionState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    WAITING_FOR_PROVIDER = "waiting_for_provider"
    RENDERING = "rendering"
    PAUSED = "paused"
    RECOVERING = "recovering"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewState(StrEnum):
    UNREVIEWED = "unreviewed"
    FINDINGS_OPEN = "findings_open"
    CHANGES_REQUIRED = "changes_required"
    READY_FOR_USER_REVIEW = "ready_for_user_review"


class AcceptanceState(StrEnum):
    UNACCEPTED = "unaccepted"
    ACCEPTED_AT_REVISION = "accepted_at_revision"
    SUPERSEDED = "superseded"


class StopReason(StrEnum):
    USER_PAUSE = "user_pause"
    USER_CANCEL = "user_cancel"
    MISSING_EVIDENCE = "missing_evidence"
    STALLED = "stalled"
    ATTEMPT_LIMIT = "attempt_limit"
    BUDGET_LIMIT = "budget_limit"
    TIME_LIMIT = "time_limit"
    EXECUTION_FAILURE = "execution_failure"
    READY_FOR_USER_REVIEW = "ready_for_user_review"


class TaskState(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    AWAITING_VALIDATION = "awaiting_validation"
    DONE = "done"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class OperationState(StrEnum):
    CREATED = "created"
    STAGED = "staged"
    RUNNING = "running"
    VALIDATING = "validating"
    PROMOTING = "promoting"
    COMMITTED = "committed"
    REJECTED = "rejected"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class FindingState(StrEnum):
    OPEN = "open"
    CORRECTION_PLANNED = "correction_planned"
    CORRECTING = "correcting"
    VERIFY_PENDING = "verify_pending"
    CLOSED = "closed"
    WAIVED = "waived"
    EVIDENCE_GAP = "evidence_gap"
    REASSESS = "reassess"


def _values(enum_cls: type[StrEnum]) -> tuple[str, ...]:
    return tuple(m.value for m in enum_cls)


# --- kind specifications -------------------------------------------------------

@dataclass(frozen=True)
class KindSpec:
    prefix: str
    required: tuple[str, ...] = ()
    states: tuple[str, ...] | None = None
    initial: str | None = None


KIND_SPECS: dict[str, KindSpec] = {
    "project": KindSpec("prj", ("name", "asset_name")),
    "run": KindSpec("run", ("project_id", "attended"), _values(ExecutionState), "idle"),
    "agent": KindSpec("ag", ("label", "provider")),
    "provider_session": KindSpec("ps", ("agent_id", "session_uuid", "kind")),
    "invocation": KindSpec("inv", ("agent_id", "purpose"), ("pending", "running", "completed", "failed"), "pending"),
    "reference": KindSpec("ref", ("file", "sha256", "width", "height", "kind")),
    "reference_region": KindSpec("reg", ("reference_id", "name", "bbox")),
    "brief": KindSpec("brief", ("text",)),
    "observation": KindSpec("obs", ("agent_id", "subject"), ("valid", "invalidated"), "valid"),
    "part": KindSpec("p", ("name",), ("planned", "blocked_out", "detailed", "integrated"), "planned"),
    "component": KindSpec("cmp", ("name", "part_ids"), _values(ReviewState), "unreviewed"),
    "relation": KindSpec("rel", ("from_part", "to_part", "type")),
    "dependency": KindSpec("dep", ("from_id", "to_part", "pinned_version")),
    "task": KindSpec("t", ("kind", "part_ids", "expected_outcome"), _values(TaskState), "pending"),
    "ownership": KindSpec("own", ("resource", "holder", "token", "base_revision_id"), ("held", "released"), "held"),
    "handoff": KindSpec("ho", ("resource", "from_holder", "to_holder", "revision_id")),
    "operation": KindSpec("op", ("task_id", "kind", "expected_base_revision_id", "intent", "expected_outcome"),
                          _values(OperationState), "created"),
    "revision": KindSpec("rev", ("file", "sha256", "parent_revision_id", "created_by_op_id")),
    "checkpoint": KindSpec("ck", ("revision_id", "name", "files")),
    "camera": KindSpec("cam", ("name", "params")),
    "view": KindSpec("view", ("name", "camera_id", "mode")),
    "render": KindSpec("rnd", ("view_id", "revision_id", "file", "manifest"), ("pending", "ok", "failed"), "pending"),
    "finding": KindSpec("f", ("part_id", "observed_mismatch", "severity", "confidence"), _values(FindingState), "open"),
    "correction_attempt": KindSpec("ca", ("finding_id", "task_id"),
                                   ("in_progress", "improved", "unchanged", "regressed", "uncertain"), "in_progress"),
    "acceptance": KindSpec("acc", ("component_id",), _values(AcceptanceState), "unaccepted"),
    "feedback": KindSpec("fb", ("text",), ("received", "applied"), "received"),
    "waiver": KindSpec("wv", ("finding_id", "rationale", "user")),
    "preflight_report": KindSpec("pf", ("agent_id",)),
    "coverage": KindSpec("cov", ("part_id", "view_id", "revision_id", "inspected_by")),
    "packet": KindSpec("pk", ("kind", "agent_id", "files")),
}


class RecordValidationError(ValueError):
    pass


@dataclass
class Record:
    kind: str
    id: str
    data: dict[str, Any] = field(default_factory=dict)
    state: str | None = None
    parent_id: str | None = None
    version: int = 0
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def new(cls, kind: str, data: dict[str, Any] | None = None, *, state: str | None = None,
            parent_id: str | None = None, id: str | None = None) -> "Record":
        spec = KIND_SPECS.get(kind)
        if spec is None:
            raise RecordValidationError(f"unknown record kind {kind!r}")
        if state is None and spec.states:
            state = spec.initial
        return cls(kind=kind, id=id or new_id(spec.prefix), data=dict(data or {}), state=state, parent_id=parent_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "id": self.id, "data": self.data, "state": self.state,
            "parent_id": self.parent_id, "version": self.version,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Record":
        return cls(kind=d["kind"], id=d["id"], data=dict(d.get("data") or {}), state=d.get("state"),
                   parent_id=d.get("parent_id"), version=int(d.get("version", 0)),
                   created_at=d.get("created_at", ""), updated_at=d.get("updated_at", ""))

    def copy(self) -> "Record":
        return Record.from_json(_deep_copy(self.to_json()))


def _deep_copy(obj: Any) -> Any:
    import copy

    return copy.deepcopy(obj)


def validate_record(rec: Record) -> None:
    """Raise ``RecordValidationError`` listing every problem; silent on success."""
    problems: list[str] = []
    spec = KIND_SPECS.get(rec.kind)
    if spec is None:
        raise RecordValidationError(f"unknown record kind {rec.kind!r}")
    if not rec.id or not rec.id.startswith(spec.prefix + "_"):
        problems.append(f"id {rec.id!r} must start with {spec.prefix + '_'!r}")
    if not isinstance(rec.data, dict):
        problems.append("data must be a dict")
    else:
        missing = [k for k in spec.required if k not in rec.data]
        if missing:
            problems.append(f"missing required keys: {', '.join(missing)}")
    if spec.states:
        if rec.state not in spec.states:
            problems.append(f"state {rec.state!r} not in {spec.states}")
    elif rec.state is not None:
        problems.append(f"kind {rec.kind!r} has no states but state={rec.state!r}")
    if problems:
        raise RecordValidationError(f"{rec.kind} {rec.id}: " + "; ".join(problems))
