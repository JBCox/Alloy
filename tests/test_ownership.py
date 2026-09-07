"""R-41 single owner enforced at the execution boundary, R-44 handoff, R-45 stale tokens rejected."""
from __future__ import annotations

import pytest

from builder.ownership import OwnershipError, OwnershipManager
from builder.store import Store


@pytest.fixture
def store(workdir):
    s = Store(workdir / "b.sqlite3").open()
    yield s
    s.close()


@pytest.fixture
def owners(store):
    return OwnershipManager(store)


def test_acquire_free_resource_and_refuse_second_holder(owners):
    own = owners.acquire("assembly", "agent:A", "rev_1", actor="engine", reason="build task t_1")
    assert own.state == "held" and own.data["holder"] == "agent:A" and len(own.data["token"]) >= 16
    with pytest.raises(OwnershipError):
        owners.acquire("assembly", "agent:B", "rev_1", actor="engine", reason="build task t_2")
    same = owners.acquire("assembly", "agent:A", "rev_1", actor="engine", reason="again")
    assert same.id == own.id  # idempotent for the current holder
    assert owners.current("assembly").id == own.id


def test_release_frees_resource(owners):
    own = owners.acquire("assembly", "agent:A", "rev_1", actor="engine", reason="t")
    released = owners.release(own.data["token"], actor="engine", reason="done")
    assert released.state == "released" and released.data["release_reason"] == "done"
    assert owners.current("assembly") is None
    own2 = owners.acquire("assembly", "agent:B", "rev_1", actor="engine", reason="t2")
    assert own2.data["holder"] == "agent:B"


def test_check_rejects_unknown_released_wrong_resource_and_base_mismatch(owners):
    own = owners.acquire("assembly", "agent:A", "rev_1", actor="engine", reason="t")
    tok = own.data["token"]
    assert owners.check(tok, "assembly", "rev_1").id == own.id
    with pytest.raises(OwnershipError, match="unknown"):
        owners.check("nope", "assembly", "rev_1")
    with pytest.raises(OwnershipError, match="resource"):
        owners.check(tok, "component:cmp_1", "rev_1")
    with pytest.raises(OwnershipError, match="base revision"):
        owners.check(tok, "assembly", "rev_2")
    owners.release(tok, actor="engine", reason="x")
    with pytest.raises(OwnershipError, match="released"):
        owners.check(tok, "assembly", "rev_1")


def test_handoff_records_context_and_invalidates_old_token(owners, store):
    own = owners.acquire("assembly", "agent:A", "rev_1", actor="engine", reason="t")
    old_tok = own.data["token"]
    handoff, new_own = owners.handoff(old_tok, "agent:B", revision_id="rev_3", changed_parts=["p_1"],
                                      assumptions=["front view is orthographic-ish"], findings=["f_1"],
                                      pending_issues=["cap seam"], actor="engine", reason="B corrects f_1")
    assert handoff.kind == "handoff" and handoff.data["from_holder"] == "agent:A" and handoff.data["to_holder"] == "agent:B"
    assert handoff.data["revision_id"] == "rev_3" and handoff.data["changed_parts"] == ["p_1"]
    assert new_own.state == "held" and new_own.data["holder"] == "agent:B" and new_own.data["base_revision_id"] == "rev_3"
    assert new_own.data["token"] != old_tok
    with pytest.raises(OwnershipError):
        owners.check(old_tok, "assembly", "rev_3")
    assert owners.check(new_own.data["token"], "assembly", "rev_3").id == new_own.id
    events = [e.event for e in store.journal()]
    assert "ownership.released" in events and "handoff.created" in events and "ownership.acquired" in events


def test_advance_base_revision_for_current_holder(owners):
    own = owners.acquire("assembly", "agent:A", "rev_1", actor="engine", reason="t")
    tok = own.data["token"]
    owners.advance(tok, "rev_2", actor="engine", reason="op committed")
    assert owners.check(tok, "assembly", "rev_2").data["base_revision_id"] == "rev_2"
    with pytest.raises(OwnershipError):
        owners.check(tok, "assembly", "rev_1")
