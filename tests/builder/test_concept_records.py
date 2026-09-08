"""Addendum A records and machinery: reference canon fields (R-94, R-95), the new record kinds (R-96, R-101, R-104),
the canon state machine (design Section 14; intake guard), the concept schemas in strict-form-compatible shape
(design 12b item 15), the not-applicable cost quantity (R-105), and the concept config keys (D9, D11, D12)."""
from __future__ import annotations

import json

import pytest

from builder.config import BuilderConfig
from builder.records import (
    KIND_SPECS,
    NOT_APPLICABLE,
    UNKNOWN,
    CanonState,
    Record,
    RecordValidationError,
    quantity_from_json,
    quantity_to_json,
)
from builder.schemas import SCHEMAS
from builder.state import CANON, IllegalTransition, transition
from builder.store import Store


@pytest.fixture
def store(workdir):
    s = Store(workdir / "builder.sqlite3")
    s.open()
    yield s
    s.close()


def _plan(store, **data):
    return store.upsert(Record.new("concept_plan", {"needed_views": ["front", "side"], "approval_mode": "each", **data}),
                        actor="engine", event="concept_plan.created")


# --- record kinds ---------------------------------------------------------------------

def test_new_record_kinds_exist_with_states_and_prefixes():
    assert KIND_SPECS["generation"].prefix == "gen"
    assert set(KIND_SPECS["generation"].states) == {"open", "imported", "checked", "approved", "rejected", "abandoned", "failed"}
    assert KIND_SPECS["generation"].initial == "open"
    assert KIND_SPECS["canon_description"].prefix == "canon" and KIND_SPECS["canon_description"].states is None
    assert set(KIND_SPECS["evidence_conflict"].states) == {"open", "resolved_by_user", "resolved_by_regeneration"}
    assert set(KIND_SPECS["study_request"].states) == {"requested", "generated", "checked", "approved", "rejected", "withdrawn"}
    assert set(KIND_SPECS["concept_plan"].states) == {s.value for s in CanonState}
    assert KIND_SPECS["concept_plan"].initial == "no_canon"


def test_generation_record_requires_its_manifest_keys():
    rec = Record.new("generation", {"target": {"kind": "view", "view": "front"}})
    with pytest.raises(RecordValidationError) as exc:
        from builder.records import validate_record

        validate_record(rec)
    assert "prompt" in str(exc.value) and "attachments" in str(exc.value)


def test_canon_states_enumerated():
    assert [s.value for s in CanonState] == ["no_canon", "anchor_pending", "anchor_approved", "turnaround_pending",
                                             "turnaround_approved", "complete"]


# --- quantities -----------------------------------------------------------------------

def test_not_applicable_cost_is_neither_zero_nor_unknown():
    assert NOT_APPLICABLE is not UNKNOWN and NOT_APPLICABLE.is_known is False
    with pytest.raises(TypeError):
        NOT_APPLICABLE + 1
    j = quantity_to_json(NOT_APPLICABLE)
    assert j["kind"] == "not_applicable" and "manual" in j["reason"]
    assert quantity_from_json(j) is NOT_APPLICABLE
    assert quantity_from_json({"kind": "unknown"}) is UNKNOWN


# --- canon machine --------------------------------------------------------------------

def test_canon_machine_states_and_text_start():
    assert set(CANON.states) == {"no_canon", "anchor_pending", "anchor_approved", "turnaround_pending",
                                 "turnaround_approved", "complete"}


def test_canon_from_text_flow_is_guarded(store):
    plan = _plan(store)
    plan = transition(store, plan, "anchor_pending", actor="engine", reason="candidates requested",
                      context={"anchor_requests_open": True})
    with pytest.raises(IllegalTransition):
        transition(store, plan, "anchor_approved", actor="engine", reason="no anchor yet", context={"anchor_approved": False})
    plan = transition(store, plan, "anchor_approved", actor="user", reason="picked", context={"anchor_approved": True})
    with pytest.raises(IllegalTransition):
        transition(store, plan, "turnaround_pending", actor="engine", reason="no canon description",
                   context={"canon_description": False})
    plan = transition(store, plan, "turnaround_pending", actor="engine", reason="views requested",
                      context={"canon_description": True})
    with pytest.raises(IllegalTransition):
        transition(store, plan, "turnaround_approved", actor="engine", reason="partial", context={"coverage_met": False})
    with pytest.raises(IllegalTransition):   # open conflicts block completion (R-95)
        transition(store, plan, "turnaround_approved", actor="engine", reason="conflict",
                   context={"coverage_met": True, "open_conflicts": 1})
    plan = transition(store, plan, "turnaround_approved", actor="engine", reason="all views approved",
                      context={"coverage_met": True, "open_conflicts": 0})
    plan = transition(store, plan, "complete", actor="engine", reason="hand-off", context={"open_conflicts": 0})
    assert plan.state == "complete"
    entries = store.journal()
    assert [e.to_state for e in entries if e.event == "concept_plan.transition"][-1] == "complete"


def test_canon_from_images_skips_anchor_pending(store):
    plan = _plan(store)
    plan = transition(store, plan, "anchor_approved", actor="engine", reason="seed is the anchor",
                      context={"anchor_approved": True})
    assert plan.state == "anchor_approved"


def test_proceed_with_partial_set_is_a_user_action_and_is_recorded(store):
    plan = _plan(store)
    plan = transition(store, plan, "anchor_pending", actor="engine", reason="x", context={"anchor_requests_open": True})
    plan = transition(store, plan, "anchor_approved", actor="user", reason="x", context={"anchor_approved": True})
    plan = transition(store, plan, "turnaround_pending", actor="engine", reason="x", context={"canon_description": True})
    with pytest.raises(IllegalTransition):     # the engine never proceeds on a partial set by itself (R-103)
        transition(store, plan, "complete", actor="engine", reason="partial", inputs={"proceed": True, "missing": ["side"]},
                   context={"open_conflicts": 0})
    with pytest.raises(IllegalTransition):     # nor the user without saying so explicitly
        transition(store, plan, "complete", actor="user:josh", reason="partial", context={"open_conflicts": 0})
    plan = transition(store, plan, "complete", actor="user:josh", reason="partial set accepted",
                      inputs={"proceed": True, "missing": ["side"]}, context={"open_conflicts": 0})
    assert plan.state == "complete" and plan.data["proceeded_partial"]["missing"] == ["side"]
    assert plan.data["proceeded_partial"]["by"] == "user:josh"


def test_anchor_rejection_reopens_anchor_pending(store):
    plan = _plan(store)
    plan = transition(store, plan, "anchor_pending", actor="engine", reason="x", context={"anchor_requests_open": True})
    plan = transition(store, plan, "anchor_approved", actor="user", reason="x", context={"anchor_approved": True})
    plan = transition(store, plan, "anchor_pending", actor="user", reason="anchor rejected by the owner")
    assert plan.state == "anchor_pending"


def test_agent_text_never_moves_canon(store):
    plan = _plan(store)
    with pytest.raises(IllegalTransition):
        transition(store, plan, "anchor_pending", actor="agent:A", reason="looks great, approved",
                   context={"anchor_requests_open": True})


# --- schemas --------------------------------------------------------------------------

def test_concept_schemas_exist_and_are_strict_form_expressible():
    from builder.providers.cli_common import strict_schema

    for name in ("art_director_prompt", "canon_description", "consistency_verdict", "anchor_pick"):
        assert name in SCHEMAS, name
        strict = strict_schema(SCHEMAS[name])

        def walk(s, path):
            if isinstance(s, dict):
                t = s.get("type")
                if "properties" in s:
                    assert s.get("additionalProperties") is False, path
                    assert set(s["required"]) == set(s["properties"]), path
                    for k, v in s["properties"].items():
                        walk(v, f"{path}.{k}")
                if isinstance(t, list):
                    assert len(t) == 2 and "null" in t, path    # single type, nullable only for optional keys
                if isinstance(s.get("items"), dict):
                    walk(s["items"], f"{path}[]")
                if t == "object":
                    assert "properties" in s, f"{path}: free-form objects are not expressible in strict mode"

        walk(strict, name)
        assert json.dumps(SCHEMAS[name]) == json.dumps(SCHEMAS[name])
    ad = SCHEMAS["art_director_prompt"]["required"]
    for key in ("subject", "silhouette", "construction", "materials", "palette", "style", "framing", "prompt"):
        assert key in ad, key   # R-100
    cd = SCHEMAS["canon_description"]["required"]
    for key in ("silhouette_and_proportions", "main_masses", "parts_and_construction", "materials_and_colors",
                "distinguishing_details", "unknowns"):
        assert key in cd, key   # R-101
    verdict = SCHEMAS["consistency_verdict"]
    assert verdict["properties"]["verdict"]["enum"] == ["consistent", "inconsistent", "uncertain"]
    item = verdict["properties"]["inconsistencies"]["items"]["required"]
    assert {"part", "region", "what_differs"} <= set(item)   # R-102


# --- config ---------------------------------------------------------------------------

def test_concept_config_defaults_and_lenient_parse():
    cfg = BuilderConfig.from_dict({})
    assert cfg.concept["approval"] == "each" and cfg.concept["anchor_candidates"] == 4
    assert cfg.concept["views"] == ["front", "side", "rear", "top", "underside", "three-quarter"]
    assert cfg.concept["max_images"] > 0 and cfg.concept["max_regenerations_per_view"] > 0
    assert cfg.image_generation == {"seat": "manual", "vendor": "chatgpt", "model": ""}
    cfg = BuilderConfig.from_dict({"builder": {"concept": {"approval": "anchor_only", "views": ["front", "side"], "max_images": 6,
                                                            "bogus": 1},
                                               "image_generation": {"seat": "manual", "vendor": "gemini", "model": "as shown"}}})
    assert cfg.concept["approval"] == "anchor_only" and cfg.concept["views"] == ["front", "side"] and cfg.concept["max_images"] == 6
    assert cfg.image_generation["vendor"] == "gemini" and cfg.image_generation["model"] == "as shown"
    assert any("bogus" in w for w in cfg.warnings)
    bad = BuilderConfig.from_dict({"builder": {"concept": {"approval": "whatever"}, "image_generation": {"seat": "api"}}})
    assert bad.concept["approval"] == "each" and any("approval" in w for w in bad.warnings)
    assert bad.image_generation["seat"] == "manual" and any("api" in w for w in bad.warnings)
    assert "concept" in cfg.to_dict() and "image_generation" in cfg.to_dict()
