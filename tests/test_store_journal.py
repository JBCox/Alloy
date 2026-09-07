"""R-2 versioned validated records, R-3 durable journal sufficient to rebuild state, D5 SQLite store."""
from __future__ import annotations

import sqlite3

import pytest

from builder.ids import canonical_json, hash_json, new_id, sha256_bytes, utc_now
from builder.records import (
    UNKNOWN,
    Estimated,
    Measured,
    Record,
    RecordValidationError,
    quantity_from_json,
    quantity_to_json,
    validate_record,
)
from builder.store import Store


# --- ids -------------------------------------------------------------------

def test_new_id_has_prefix_and_is_unique_and_sortable():
    a = new_id("op")
    b = new_id("op")
    assert a.startswith("op_") and b.startswith("op_")
    assert a != b
    assert len(a) == len(b)


def test_canonical_json_is_key_sorted_and_stable():
    assert canonical_json({"b": 1, "a": [1, {"z": 0, "y": None}]}) == '{"a":[1,{"y":null,"z":0}],"b":1}'
    assert hash_json({"b": 1, "a": 2}) == hash_json({"a": 2, "b": 1})
    assert sha256_bytes(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_utc_now_is_iso8601_utc():
    ts = utc_now()
    assert ts.endswith("Z") and "T" in ts


# --- quantities (R-25, R-85: unknown is never zero) --------------------------

def test_unknown_quantity_refuses_arithmetic_and_round_trips():
    with pytest.raises(TypeError):
        UNKNOWN + 1  # noqa: B018
    with pytest.raises(TypeError):
        1 + UNKNOWN  # noqa: B018
    assert quantity_from_json(quantity_to_json(UNKNOWN)) is UNKNOWN
    m = Measured(3.5, source="provider")
    e = Estimated(2, source="heuristic")
    assert quantity_from_json(quantity_to_json(m)) == m
    assert quantity_from_json(quantity_to_json(e)) == e
    assert quantity_to_json(m)["kind"] == "measured"
    assert quantity_to_json(e)["kind"] == "estimated"
    assert UNKNOWN.is_known is False and m.is_known is True


# --- records ----------------------------------------------------------------

def test_record_new_assigns_prefixed_id_and_initial_state():
    rec = Record.new("run", {"project_id": "prj_x", "attended": True})
    assert rec.id.startswith("run_")
    assert rec.state == "idle"
    assert rec.version == 0


def test_validate_record_reports_missing_keys_and_bad_state():
    rec = Record.new("finding", {"part_id": "p_1"})
    with pytest.raises(RecordValidationError) as ei:
        validate_record(rec)
    msg = str(ei.value)
    assert "observed_mismatch" in msg and "severity" in msg and "confidence" in msg
    rec2 = Record.new("run", {"project_id": "prj", "attended": True})
    rec2.state = "flying"
    with pytest.raises(RecordValidationError):
        validate_record(rec2)
    with pytest.raises(RecordValidationError):
        validate_record(Record(kind="nonsense", id="x_1", data={}))


# --- store ------------------------------------------------------------------

@pytest.fixture
def store(workdir):
    s = Store(workdir / "builder.sqlite3")
    s.open()
    yield s
    s.close()


def test_store_is_wal_and_has_schema_version(store):
    assert store.meta_get("schema_version") == "1"
    assert store.journal_mode() == "wal"


def test_upsert_versions_records_and_keeps_history(store):
    rec = Record.new("run", {"project_id": "prj", "attended": True})
    saved = store.upsert(rec, actor="engine", event="run.created")
    assert saved.version == 1
    saved.data["attended"] = False
    saved2 = store.upsert(saved, actor="engine", event="run.updated")
    assert saved2.version == 2
    hist = store.history("run", rec.id)
    assert [h.version for h in hist] == [1]
    assert hist[0].data["attended"] is True
    assert store.get("run", rec.id).data["attended"] is False


def test_upsert_refuses_invalid_record_and_writes_nothing(store):
    bad = Record.new("finding", {"part_id": "p_1"})
    with pytest.raises(RecordValidationError):
        store.upsert(bad, actor="engine", event="finding.created")
    assert store.get("finding", bad.id) is None
    assert store.journal() == []


def test_journal_entries_carry_actor_states_inputs_evidence_and_snapshot(store):
    rec = Record.new("run", {"project_id": "prj", "attended": True})
    store.upsert(rec, actor="engine", event="run.created", inputs={"cli": "new"}, evidence=["ref_1"])
    entries = store.journal()
    assert len(entries) == 1
    e = entries[0]
    assert e.actor == "engine" and e.event == "run.created"
    assert e.record_kind == "run" and e.record_id == rec.id
    assert e.from_state is None and e.to_state == "idle"
    assert e.inputs == {"cli": "new"} and e.evidence == ["ref_1"]
    assert e.after["data"]["project_id"] == "prj"
    assert e.ts.endswith("Z")


def test_journal_is_append_only(store):
    rec = Record.new("run", {"project_id": "prj", "attended": True})
    store.upsert(rec, actor="engine", event="run.created")
    with pytest.raises(sqlite3.DatabaseError):
        store.raw_execute("UPDATE journal SET actor='x'")
    with pytest.raises(sqlite3.DatabaseError):
        store.raw_execute("DELETE FROM journal")
    assert store.journal()[0].actor == "engine"


def test_rebuild_from_journal_reproduces_records(store):
    a = store.upsert(Record.new("run", {"project_id": "prj", "attended": True}), actor="engine", event="run.created")
    b = store.upsert(Record.new("part", {"name": "housing"}), actor="engine", event="part.created")
    a.data["attended"] = False
    store.upsert(a, actor="user", event="run.updated")
    b.data["name"] = "housing v2"
    store.upsert(b, actor="agent:A", event="part.updated")
    rebuilt = store.rebuild_from_journal()
    current = {(r.kind, r.id): r for r in store.list_all()}
    assert set(rebuilt) == set(current)
    for key, rec in current.items():
        assert rebuilt[key].data == rec.data
        assert rebuilt[key].version == rec.version
        assert rebuilt[key].state == rec.state
    assert store.verify_rebuild() is True


def test_list_filters_by_state_and_parent(store):
    p = store.upsert(Record.new("part", {"name": "root"}), actor="engine", event="part.created")
    c1 = Record.new("part", {"name": "child1"}, parent_id=p.id)
    c2 = Record.new("part", {"name": "child2"}, parent_id=p.id)
    store.upsert(c1, actor="engine", event="part.created")
    store.upsert(c2, actor="engine", event="part.created")
    assert {r.id for r in store.list("part", parent_id=p.id)} == {c1.id, c2.id}
    f = Record.new("finding", {"part_id": p.id, "observed_mismatch": "gap", "severity": "high", "confidence": "medium"})
    store.upsert(f, actor="engine", event="finding.created")
    assert [r.id for r in store.list("finding", state="open")] == [f.id]
    assert store.list("finding", state="closed") == []


def test_file_registry_detects_hash_mismatch(store, workdir):
    path = workdir / "rev ü.blend"
    path.write_bytes(b"abc")
    store.register_file(path, kind="revision", record_id="rev_1")
    ok, expected, actual = store.verify_file(path)
    assert ok and expected == actual
    path.write_bytes(b"abcd")
    ok, expected, actual = store.verify_file(path)
    assert not ok and expected != actual
    ok, expected, actual = store.verify_file(workdir / "missing.blend")
    assert not ok and actual is None


def test_control_requests_are_consumed_once(store):
    store.push_control("pause", {"by": "cli"})
    store.push_control("feedback", {"text": "make it wider"})
    first = store.pop_controls()
    assert [c.kind for c in first] == ["pause", "feedback"]
    assert first[1].payload == {"text": "make it wider"}
    assert store.pop_controls() == []


def test_append_journal_event_without_record(store):
    seq = store.append_journal(actor="engine", event="op.promoting", op_id="op_1",
                               inputs={"rev_id": "rev_9", "sha256": "abc"})
    e = store.journal()[-1]
    assert e.seq == seq and e.op_id == "op_1" and e.record_kind is None
    assert e.inputs["sha256"] == "abc"


def test_store_reopen_persists(workdir):
    path = workdir / "builder.sqlite3"
    s = Store(path)
    s.open()
    rec = s.upsert(Record.new("run", {"project_id": "prj", "attended": True}), actor="engine", event="run.created")
    s.close()
    s2 = Store(path)
    s2.open()
    assert s2.get("run", rec.id).data["project_id"] == "prj"
    assert len(s2.journal()) == 1
    s2.close()
