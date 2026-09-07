"""Limits and consumption accounting (spec R-25, R-85, R-86, R-87).

Measured and estimated costs are separate. Unknown consumption is counted as unknown, never as zero,
and never satisfies a cap. In-flight work counts at dispatch. Correction attempts per finding are
tracked apart from transport retries. Zero means unlimited.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .config import DEFAULT_LIMITS
from .providers.base import Usage
from .records import UNKNOWN, Estimated, Measured, quantity_to_json


class LimitTracker:
    def __init__(self, limits: dict[str, Any] | None = None, *, cost_enforced_by_provider: dict[str, bool] | None = None,
                 now: Callable[[], float] = time.monotonic):
        self.limits: dict[str, Any] = dict(DEFAULT_LIMITS)
        self.limits.update(limits or {})
        self.cost_enforced_by_provider = dict(cost_enforced_by_provider or {})
        self._now = now
        self.started = now()
        self.requests_inflight = 0
        self.requests_completed = 0
        self.renders_inflight = 0
        self.renders_completed = 0
        self.cost_measured_total: float | None = None
        self.cost_estimated_total: float | None = None
        self.unknown_cost_invocations = 0
        self.transport_retries = 0
        self._correction_attempts: dict[str, int] = {}
        self.per_agent: dict[str, dict[str, int]] = {}
        self.max_invocation_cost: dict[str, float] = {}     # agent id -> largest single-invocation cost reported (measured or estimated)
        # spend outside the modeling requests (preflight probes) that still counts toward the monetary cap
        self.external_costs: dict[str, dict[str, float | int]] = {}

    def restore(self, snapshot: dict[str, Any] | None) -> None:
        """Continue counting from a persisted ``status()`` snapshot (restart, or a second process)."""
        if not snapshot:
            return
        self.requests_completed = int((snapshot.get("requests") or {}).get("completed") or 0)
        self.renders_completed = int((snapshot.get("renders") or {}).get("completed") or 0)
        cost = snapshot.get("cost") or {}
        measured, estimated = cost.get("measured") or {}, cost.get("estimated") or {}
        self.cost_measured_total = float(measured["value"]) if measured.get("kind") == "measured" else None
        self.cost_estimated_total = float(estimated["value"]) if estimated.get("kind") == "estimated" else None
        self.unknown_cost_invocations = int(cost.get("unknown_invocations") or 0)
        self.transport_retries = int(snapshot.get("transport_retries") or 0)
        self._correction_attempts = {k: int(v) for k, v in (snapshot.get("correction_attempts") or {}).items()}
        self.per_agent = {k: dict(v) for k, v in (snapshot.get("per_agent") or {}).items()}
        self.max_invocation_cost = {k: float(v) for k, v in (snapshot.get("max_invocation_cost") or {}).items()}
        self.external_costs = {k: {"measured": float(v.get("measured") or 0.0), "estimated": float(v.get("estimated") or 0.0),
                                   "unknown_invocations": int(v.get("unknown_invocations") or 0)}
                               for k, v in (snapshot.get("external") or {}).items()}
        self.started = self._now() - float(snapshot.get("elapsed_s") or 0.0)

    # --- accounting ---------------------------------------------------------------

    def note_dispatch(self, kind: str = "request", agent_id: str | None = None) -> None:
        if kind == "render":
            self.renders_inflight += 1
        else:
            self.requests_inflight += 1
            if agent_id:
                self.per_agent.setdefault(agent_id, {"requests": 0, "retries": 0})["requests"] += 1

    def note_complete(self, kind: str = "request", agent_id: str | None = None, usage: Usage | None = None) -> None:
        if kind == "render":
            self.renders_inflight = max(0, self.renders_inflight - 1)
            self.renders_completed += 1
            return
        self.requests_inflight = max(0, self.requests_inflight - 1)
        self.requests_completed += 1
        cost = usage.cost_usd if usage is not None else UNKNOWN
        if isinstance(cost, Measured):
            self.cost_measured_total = round((self.cost_measured_total or 0.0) + float(cost.value), 6)
        elif isinstance(cost, Estimated):
            self.cost_estimated_total = round((self.cost_estimated_total or 0.0) + float(cost.value), 6)
        else:
            self.unknown_cost_invocations += 1
        if isinstance(cost, (Measured, Estimated)) and agent_id:
            self.max_invocation_cost[agent_id] = max(self.max_invocation_cost.get(agent_id, 0.0), float(cost.value))

    def note_external_cost(self, source: str, usage: Usage | None) -> None:
        """Spend that is not a modeling request (a preflight probe) but is real money: it enters the measured or
        estimated total so the cap covers it; unknown stays unknown, never zero (R-25, R-85)."""
        cost = usage.cost_usd if usage is not None else UNKNOWN
        entry = self.external_costs.setdefault(source, {"measured": 0.0, "estimated": 0.0, "unknown_invocations": 0})
        if isinstance(cost, Measured):
            entry["measured"] = round(float(entry["measured"]) + float(cost.value), 6)
            self.cost_measured_total = round((self.cost_measured_total or 0.0) + float(cost.value), 6)
        elif isinstance(cost, Estimated):
            entry["estimated"] = round(float(entry["estimated"]) + float(cost.value), 6)
            self.cost_estimated_total = round((self.cost_estimated_total or 0.0) + float(cost.value), 6)
        else:
            entry["unknown_invocations"] = int(entry["unknown_invocations"]) + 1
            self.unknown_cost_invocations += 1

    def add_external(self, source: str, *, measured: float = 0.0, estimated: float = 0.0, unknown_invocations: int = 0) -> None:
        """Seed spend recorded by an earlier tracker (a preflight report from another process)."""
        entry = self.external_costs.setdefault(source, {"measured": 0.0, "estimated": 0.0, "unknown_invocations": 0})
        if measured:
            entry["measured"] = round(float(entry["measured"]) + float(measured), 6)
            self.cost_measured_total = round((self.cost_measured_total or 0.0) + float(measured), 6)
        if estimated:
            entry["estimated"] = round(float(entry["estimated"]) + float(estimated), 6)
            self.cost_estimated_total = round((self.cost_estimated_total or 0.0) + float(estimated), 6)
        if unknown_invocations:
            entry["unknown_invocations"] = int(entry["unknown_invocations"]) + int(unknown_invocations)
            self.unknown_cost_invocations += int(unknown_invocations)

    def note_transport_retry(self, agent_id: str | None = None) -> None:
        self.transport_retries += 1
        if agent_id:
            self.per_agent.setdefault(agent_id, {"requests": 0, "retries": 0})["retries"] += 1

    def note_correction_attempt(self, finding_id: str) -> int:
        self._correction_attempts[finding_id] = self._correction_attempts.get(finding_id, 0) + 1
        return self._correction_attempts[finding_id]

    def correction_attempts(self, finding_id: str) -> int:
        return self._correction_attempts.get(finding_id, 0)

    def correction_allowed(self, finding_id: str) -> bool:
        cap = int(self.limits.get("attempts_per_finding") or 0)
        return cap == 0 or self.correction_attempts(finding_id) < cap

    # --- decisions ------------------------------------------------------------------

    def elapsed_s(self) -> float:
        return self._now() - self.started

    def can_dispatch(self, kind: str = "request", agent_id: str | None = None) -> tuple[bool, str | None, str]:
        minutes = float(self.limits.get("wall_clock_minutes") or 0)
        if minutes > 0 and self.elapsed_s() > minutes * 60:
            return False, "time_limit", f"wall clock limit of {minutes:g} min reached ({self.elapsed_s() / 60:.1f} min elapsed)"
        max_requests = int(self.limits.get("max_requests") or 0)
        total_requests = self.requests_inflight + self.requests_completed
        if kind != "render" and max_requests > 0 and total_requests >= max_requests:
            return False, "budget_limit", (f"request limit {max_requests} reached ({self.requests_completed} completed, "
                                           f"{self.requests_inflight} in flight)")
        max_renders = int(self.limits.get("max_renders") or 0)
        if kind == "render" and max_renders > 0 and self.renders_inflight + self.renders_completed >= max_renders:
            return False, "budget_limit", f"render limit {max_renders} reached"
        cap = float(self.limits.get("max_cost_usd") or 0)
        if cap > 0 and self.cost_measured_total is not None and self.cost_measured_total >= cap:
            return False, "budget_limit", f"measured cost {self.cost_measured_total:.2f} USD reached the cap of {cap:.2f} USD"
        if cap > 0 and self.cost_estimated_total is not None and self.cost_estimated_total >= cap:
            return False, "budget_limit", (f"estimated cost {self.cost_estimated_total:.2f} USD (provider estimate, not a "
                                           f"measurement) reached the cap of {cap:.2f} USD")
        if kind != "render" and cap > 0 and agent_id and agent_id in self.max_invocation_cost:
            remaining = self.remaining_budget_usd() or 0.0
            worst = self.max_invocation_cost[agent_id]
            if remaining < worst:
                return False, "budget_limit", (f"remaining budget {remaining:.2f} USD is below the largest cost one invocation "
                                               f"of this agent has reported ({worst:.2f} USD); a cap applied by the provider "
                                               "only stops a call after the spend, so nothing more is dispatched")
        return True, None, ""

    def remaining_budget_usd(self) -> float | None:
        """What a per-invocation provider cap may still spend: the cap minus measured and estimated totals.
        None when no cap is configured. Unknown consumption is not subtracted (it cannot be), and is reported."""
        cap = float(self.limits.get("max_cost_usd") or 0)
        if cap <= 0:
            return None
        spent = (self.cost_measured_total or 0.0) + (self.cost_estimated_total or 0.0)
        return max(0.0, round(cap - spent, 6))

    def cost_enforceable(self) -> tuple[bool, str]:
        cap = float(self.limits.get("max_cost_usd") or 0)
        if cap <= 0:
            return False, "no monetary cap configured"
        flags = list(self.cost_enforced_by_provider.values())
        if not flags or not all(flags):
            return False, ("not enforceable: at least one provider reports no cost and enforces no per-invocation cap; "
                           f"{self.unknown_cost_invocations} invocation(s) so far reported unknown cost")
        note = "provider-enforced per-invocation cap plus Alloy's measured total"
        if self.unknown_cost_invocations:
            note += f"; {self.unknown_cost_invocations} invocation(s) reported unknown cost and are not in the total"
        return True, note

    def status(self) -> dict[str, Any]:
        enforceable, note = self.cost_enforceable()
        limits: dict[str, Any] = {}
        for name, value in self.limits.items():
            entry: dict[str, Any] = {"value": value, "unlimited": not value}
            if name == "max_cost_usd":
                entry["enforceable"] = enforceable
                entry["note"] = note
            else:
                entry["enforceable"] = True
            limits[name] = entry
        return {
            "elapsed_s": round(self.elapsed_s(), 3),
            "requests": {"inflight": self.requests_inflight, "completed": self.requests_completed},
            "renders": {"inflight": self.renders_inflight, "completed": self.renders_completed},
            "cost": {
                "measured": quantity_to_json(Measured(self.cost_measured_total, "provider"))
                if self.cost_measured_total is not None else quantity_to_json(UNKNOWN),
                "estimated": quantity_to_json(Estimated(self.cost_estimated_total, "tracker"))
                if self.cost_estimated_total is not None else quantity_to_json(UNKNOWN),
                "unknown_invocations": self.unknown_cost_invocations,
            },
            "limits": limits,
            "transport_retries": self.transport_retries,
            "correction_attempts": dict(self._correction_attempts),
            "max_invocation_cost": dict(self.max_invocation_cost),
            "findings_at_attempt_limit": [f for f in self._correction_attempts if not self.correction_allowed(f)],
            "per_agent": dict(self.per_agent),
            "external": {k: dict(v) for k, v in self.external_costs.items()},
        }
