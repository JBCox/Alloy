"""R-25 unknown usage is never zero, R-85 explicit limits with enforceable vs estimated labels,
R-86 in-flight accounted and nothing dispatched after a limit, R-87 correction attempts separate from retries."""
from __future__ import annotations

import pytest

from builder.limits import LimitTracker
from builder.providers.base import Usage
from builder.records import UNKNOWN, Estimated, Measured


def test_unknown_cost_never_counts_as_zero_and_cap_is_labeled_unenforced():
    t = LimitTracker({"max_cost_usd": 5.0}, cost_enforced_by_provider={"ag_A": False})
    t.note_dispatch("request", agent_id="ag_A")
    t.note_complete("request", agent_id="ag_A", usage=Usage())
    s = t.status()
    assert s["cost"]["measured"] == {"kind": "unknown"}
    assert s["cost"]["unknown_invocations"] == 1
    assert s["limits"]["max_cost_usd"]["enforceable"] is False
    assert "unknown" in s["limits"]["max_cost_usd"]["note"]
    ok, reason, _ = t.can_dispatch()
    assert ok and reason is None


def test_measured_cost_reaching_cap_stops_with_budget_limit():
    t = LimitTracker({"max_cost_usd": 1.0}, cost_enforced_by_provider={"ag_A": True})
    t.note_dispatch("request", agent_id="ag_A")
    t.note_complete("request", agent_id="ag_A", usage=Usage(cost_usd=Measured(0.6, "claude")))
    assert t.can_dispatch()[0]
    t.note_dispatch("request", agent_id="ag_A")
    t.note_complete("request", agent_id="ag_A", usage=Usage(cost_usd=Measured(0.5, "claude")))
    ok, reason, msg = t.can_dispatch()
    assert not ok and reason == "budget_limit" and "1.1" in msg
    assert t.status()["cost"]["measured"] == {"kind": "measured", "value": 1.1, "source": "provider"}
    assert t.status()["limits"]["max_cost_usd"]["enforceable"] is True


def test_remaining_budget_below_the_agents_largest_call_stops_dispatch():
    """Learned live: a provider-side cap stops a call only after the spend (claude spent 2.07 USD against a 0.06 USD
    cap). Alloy must not dispatch when the remaining budget cannot cover what one call of that agent has cost."""
    t = LimitTracker({"max_cost_usd": 5.0}, cost_enforced_by_provider={"ag_A": True})
    t.note_dispatch("request", agent_id="ag_A")
    t.note_complete("request", agent_id="ag_A", usage=Usage(cost_usd=Estimated(2.26, "claude")))
    t.note_dispatch("request", agent_id="ag_A")
    t.note_complete("request", agent_id="ag_A", usage=Usage(cost_usd=Estimated(2.67, "claude")))
    assert t.remaining_budget_usd() == 0.07
    ok, reason, msg = t.can_dispatch("request", agent_id="ag_A")
    assert not ok and reason == "budget_limit" and "2.67" in msg and "after the spend" in msg
    assert t.can_dispatch("render")[0]                       # renders are not money
    ok, _, _ = t.can_dispatch("request", agent_id="ag_B")     # an agent with no reported cost is not held by A's history
    assert ok
    assert t.status()["max_invocation_cost"] == {"ag_A": 2.67}
    t2 = LimitTracker({"max_cost_usd": 5.0})
    t2.restore(t.status())
    assert not t2.can_dispatch("request", agent_id="ag_A")[0]


def test_estimated_and_measured_costs_are_kept_separate():
    t = LimitTracker({}, cost_enforced_by_provider={})
    t.note_dispatch("request", agent_id="ag_A")
    t.note_complete("request", agent_id="ag_A", usage=Usage(cost_usd=Estimated(0.2, "heuristic")))
    s = t.status()["cost"]
    assert s["measured"] == {"kind": "unknown"} and s["estimated"] == {"kind": "estimated", "value": 0.2, "source": "tracker"}


def test_request_and_render_counts_include_inflight_and_block_new_dispatch():
    t = LimitTracker({"max_requests": 2, "max_renders": 1})
    t.note_dispatch("request", agent_id="ag_A")
    assert t.can_dispatch()[0]
    t.note_dispatch("request", agent_id="ag_B")           # second is in flight, not complete
    ok, reason, _ = t.can_dispatch()
    assert not ok and reason == "budget_limit"
    assert t.status()["requests"]["inflight"] == 2 and t.status()["requests"]["completed"] == 0
    t2 = LimitTracker({"max_renders": 1})
    t2.note_dispatch("render")
    assert t2.can_dispatch("render")[1] == "budget_limit"
    assert t2.can_dispatch("request")[0]  # a render cap does not block a request


def test_wall_clock_limit(monkeypatch):
    clock = {"t": 1000.0}
    t = LimitTracker({"wall_clock_minutes": 1}, now=lambda: clock["t"])
    assert t.can_dispatch()[0]
    clock["t"] += 61
    ok, reason, _ = t.can_dispatch()
    assert not ok and reason == "time_limit"


def test_correction_attempts_are_separate_from_transport_retries():
    t = LimitTracker({"attempts_per_finding": 2})
    assert t.correction_attempts("f_1") == 0
    t.note_correction_attempt("f_1")
    t.note_transport_retry("ag_A")
    t.note_transport_retry("ag_A")
    assert t.correction_attempts("f_1") == 1 and t.status()["transport_retries"] == 2
    assert t.correction_allowed("f_1")
    t.note_correction_attempt("f_1")
    assert not t.correction_allowed("f_1")
    assert t.status()["findings_at_attempt_limit"] == ["f_1"]


def test_zero_limits_mean_unlimited_and_status_lists_them():
    t = LimitTracker({"max_requests": 0, "wall_clock_minutes": 0})
    for _ in range(50):
        t.note_dispatch("request", agent_id="ag_A")
    assert t.can_dispatch()[0]
    assert t.status()["limits"]["max_requests"]["value"] == 0 and t.status()["limits"]["max_requests"]["unlimited"]


# --- R-85 stalls, R-88 no endless oscillation, R-89 editable limits ------------------------------------------------

def test_steps_without_progress_reach_the_stall_limit_and_restore_across_processes():
    t = LimitTracker({"stall_steps": 3})
    assert t.note_step(("a", 1)) is False          # first observation is progress by definition
    assert t.note_step(("a", 1)) is False          # 1 step without progress
    assert t.note_step(("a", 1)) is False          # 2
    assert t.status()["steps_without_progress"] == 2
    assert t.note_step(("a", 2)) is False          # progress resets the counter
    assert t.status()["steps_without_progress"] == 0
    for _ in range(2):
        assert t.note_step(("a", 2)) is False
    assert t.note_step(("a", 2)) is True           # 3 consecutive steps without progress: stalled
    ok, reason, msg = t.can_dispatch()
    assert not ok and reason == "stalled" and "progress" in msg
    t2 = LimitTracker({"stall_steps": 3})
    t2.restore(t.status())
    assert t2.status()["steps_without_progress"] == 3 and not t2.can_dispatch()[0]
    t3 = LimitTracker({"stall_steps": 0})          # zero means no stall limit
    for _ in range(50):
        assert t3.note_step(("same",)) is False
    assert t3.can_dispatch()[0]


def test_limits_are_editable_with_validation_and_the_status_reflects_the_change():
    t = LimitTracker({"max_requests": 2})
    t.note_dispatch("request", agent_id="ag_A")
    t.note_dispatch("request", agent_id="ag_A")
    assert t.can_dispatch()[1] == "budget_limit"
    applied = t.apply_limits({"max_requests": 10, "max_cost_usd": "12.5", "stall_steps": 20})
    assert applied == {"max_requests": 10, "max_cost_usd": 12.5, "stall_steps": 20}
    assert t.can_dispatch()[0]
    assert t.status()["limits"]["max_requests"]["value"] == 10 and t.status()["limits"]["max_cost_usd"]["value"] == 12.5
    with pytest.raises(ValueError, match="unknown limit"):
        t.apply_limits({"max_tokens": 5})
    with pytest.raises(ValueError, match="negative"):
        t.apply_limits({"max_renders": -1})
    with pytest.raises(ValueError, match="number"):
        t.apply_limits({"wall_clock_minutes": "soon"})
    assert t.limits["max_requests"] == 10     # a rejected batch changes nothing
