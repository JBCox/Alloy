"""R-4 explicit, guarded, separate state machines; R-1 agent text never changes state; R-5 parse failures never advance."""
from __future__ import annotations

import pytest

from builder.records import Record
from builder.state import (
    ACCEPTANCE,
    EXECUTION,
    REVIEW,
    STOP_REASONS,
    IllegalTransition,
    transition,
)
from builder.store import Store


@pytest.fixture
def store(workdir):
    s = Store(workdir / "builder.sqlite3")
    s.open()
    yield s
    s.close()


def _run(store):
    return store.upsert(Record.new("run", {"project_id": "prj", "attended": True}), actor="engine", event="run.created")


def _component(store):
    return store.upsert(Record.new("component", {"name": "Head", "part_ids": ["p_1"]}), actor="engine", event="component.created")


def test_machines_enumerate_spec_states():
    assert set(EXECUTION.states) == {"idle", "running", "waiting_for_user", "waiting_for_provider", "rendering",
                                     "paused", "recovering", "failed", "cancelled"}
    assert set(REVIEW.states) == {"unreviewed", "findings_open", "changes_required", "ready_for_user_review"}
    assert set(ACCEPTANCE.states) == {"unaccepted", "accepted_at_revision", "superseded"}
    assert set(STOP_REASONS) == {"user_pause", "user_cancel", "missing_evidence", "stalled", "attempt_limit",
                                 "budget_limit", "time_limit", "execution_failure", "ready_for_user_review"}


def test_legal_execution_transition_is_journaled(store):
    run = _run(store)
    run = transition(store, run, "running", actor="engine", reason="start", context={"preflight_ok": True})
    assert run.state == "running"
    entries = store.journal()
    assert entries[-1].from_state == "idle" and entries[-1].to_state == "running"
    assert entries[-1].event == "run.transition"


def test_illegal_transition_raises_and_writes_nothing(store):
    run = _run(store)
    before = len(store.journal())
    with pytest.raises(IllegalTransition):
        transition(store, run, "rendering", actor="engine", reason="nope")
    assert len(store.journal()) == before
    assert store.get("run", run.id).state == "idle"


def test_start_requires_preflight_guard(store):
    run = _run(store)
    with pytest.raises(IllegalTransition):
        transition(store, run, "running", actor="engine", reason="start", context={"preflight_ok": False})


def test_stop_sets_reason_and_resume_clears_it(store):
    run = _run(store)
    run = transition(store, run, "running", actor="engine", reason="start", context={"preflight_ok": True})
    run = transition(store, run, "paused", actor="user", reason="pause", stop_reason="user_pause")
    assert run.data["stop_reason"] == "user_pause"
    with pytest.raises(IllegalTransition):
        transition(store, run, "paused", actor="user", reason="pause", stop_reason="because")
    run = transition(store, run, "running", actor="user", reason="resume", context={"limit_reached": False})
    assert run.data["stop_reason"] is None


def test_stop_requires_a_reason(store):
    run = _run(store)
    run = transition(store, run, "running", actor="engine", reason="start", context={"preflight_ok": True})
    with pytest.raises(IllegalTransition):
        transition(store, run, "paused", actor="user", reason="pause")


def test_resume_blocked_while_limit_reached(store):
    run = _run(store)
    run = transition(store, run, "running", actor="engine", reason="start", context={"preflight_ok": True})
    run = transition(store, run, "paused", actor="engine", reason="budget", stop_reason="budget_limit")
    with pytest.raises(IllegalTransition):
        transition(store, run, "running", actor="user", reason="resume", context={"limit_reached": True})


def test_agents_can_never_transition_state(store):
    run = _run(store)
    with pytest.raises(IllegalTransition):
        transition(store, run, "running", actor="agent:A", reason="I am done, looks good, we agree",
                   context={"preflight_ok": True})
    comp = _component(store)
    with pytest.raises(IllegalTransition):
        transition(store, comp, "ready_for_user_review", actor="agent:B", reason="done",
                   context={"open_findings": 0, "renders_fresh": True, "coverage_met": True, "pending_ops": 0})


def test_review_ready_requires_fresh_renders_coverage_and_no_findings(store):
    comp = _component(store)
    base = {"open_findings": 0, "renders_fresh": True, "coverage_met": True, "pending_ops": 0}
    for broken in ({"open_findings": 1}, {"renders_fresh": False}, {"coverage_met": False}, {"pending_ops": 1}):
        with pytest.raises(IllegalTransition):
            transition(store, comp, "ready_for_user_review", actor="engine", reason="review", context={**base, **broken})
    comp = transition(store, comp, "ready_for_user_review", actor="engine", reason="review", context=base)
    assert comp.state == "ready_for_user_review"


def test_review_findings_flow(store):
    comp = _component(store)
    comp = transition(store, comp, "findings_open", actor="engine", reason="B reported", context={"open_findings": 2})
    comp = transition(store, comp, "changes_required", actor="engine", reason="correct")
    comp = transition(store, comp, "findings_open", actor="engine", reason="one closed", context={"open_findings": 1})
    comp = transition(store, comp, "unreviewed", actor="engine", reason="upstream change invalidated")
    assert comp.state == "unreviewed"


def test_parse_failure_is_not_a_transition(store):
    comp = _component(store)
    with pytest.raises(IllegalTransition):
        transition(store, comp, "ready_for_user_review", actor="engine", reason="malformed output",
                   context={"open_findings": 0, "renders_fresh": True, "coverage_met": True, "pending_ops": 0,
                            "last_output_malformed": True})


def test_acceptance_binds_revision_and_supersedes_visibly(store):
    acc = store.upsert(Record.new("acceptance", {"component_id": "cmp_1"}), actor="engine", event="acceptance.created")
    assert acc.state == "unaccepted"
    with pytest.raises(IllegalTransition):
        transition(store, acc, "accepted_at_revision", actor="user", reason="accept")  # no binding
    acc = transition(store, acc, "accepted_at_revision", actor="user", reason="accept",
                     inputs={"revision_id": "rev_3", "evidence_versions": {"ref_1": 1}, "dependency_versions": {"p_2": 4}})
    assert acc.data["revision_id"] == "rev_3"
    with pytest.raises(IllegalTransition):
        transition(store, acc, "accepted_at_revision", actor="agent:A", reason="accept",
                   inputs={"revision_id": "rev_4", "evidence_versions": {}, "dependency_versions": {}})
    acc = transition(store, acc, "superseded", actor="engine", reason="rev_4 changed the component",
                     inputs={"supersede_reason": "revision_changed"})
    assert acc.state == "superseded" and acc.data["supersede_reason"] == "revision_changed"
    assert store.history("acceptance", acc.id)[-1].state == "accepted_at_revision"
    with pytest.raises(IllegalTransition):
        transition(store, acc, "unaccepted", actor="user", reason="no way back")


def test_nothing_is_accepted_or_ready_by_default(store):
    comp = _component(store)
    acc = store.upsert(Record.new("acceptance", {"component_id": comp.id}), actor="engine", event="acceptance.created")
    assert comp.state == "unreviewed" and acc.state == "unaccepted"
