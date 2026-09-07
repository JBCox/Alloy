"""R-5 schema validation with one bounded repair round, R-24 native or extracted JSON, R-73 finding fields."""
from __future__ import annotations

import pytest

from builder.providers.structured import ParseOutcome, extract_json, parse_structured, run_with_repair
from builder.schemas import SCHEMAS, validate_against


def test_finding_schema_requires_separate_severity_and_confidence():
    schema = SCHEMAS["findings_report"]
    good = {"findings": [{"part_id": "p_1", "region": "top", "view": "front", "observed_mismatch": "gap under bracket",
                          "severity": "high", "confidence": "medium", "evidence_refs": ["rnd_1"],
                          "proposed_correction": "lower bracket", "expected_improvement": "no visible gap in side view"}],
            "coverage": [{"part_id": "p_1", "view": "front", "instances_inspected": "all"}]}
    assert validate_against(schema, good) == []
    bad = {"findings": [{"part_id": "p_1", "observed_mismatch": "gap", "severity": "urgent"}], "coverage": []}
    errors = validate_against(schema, bad)
    joined = "\n".join(errors)
    assert "confidence" in joined and "severity" in joined and "evidence_refs" in joined


def test_task_result_schema_accepts_no_edit_warranted_and_operations():
    schema = SCHEMAS["task_result"]
    ops = {"operations": [{"intent": "lift cap", "target_part_ids": ["p_cap"], "expected_outcome": "cap higher",
                           "declared_effects": {"creates": [], "modifies": ["p_cap"], "deletes": []},
                           "script": "ALLOY.get('p_cap').location.z += 0.1"}],
           "self_assessment": "cap now aligns with reference", "questions_for_user": []}
    assert validate_against(schema, ops) == []
    none = {"operations": [], "no_edit_warranted": {"reason": "matches reference within evidence", "evidence": ["rnd_2"]},
            "self_assessment": "", "questions_for_user": []}
    assert validate_against(schema, none) == []
    assert validate_against(schema, {"operations": [{"intent": "x"}]}) != []


def test_extract_json_from_fenced_text_and_reject_junk():
    obj, err = extract_json('Here you go:\n```json\n{"a": 1, "b": [1, 2]}\n```\nthanks')
    assert obj == {"a": 1, "b": [1, 2]} and err == ""
    obj, err = extract_json('prose {"nested": {"x": "}"}} trailing')
    assert obj == {"nested": {"x": "}"}}
    obj, err = extract_json("no json here at all")
    assert obj is None and err


def test_parse_structured_prefers_native_and_reports_schema_errors():
    schema = SCHEMAS["probe_report"]
    out = parse_structured(raw_text="ignored", native={"shape": "triangle", "color": "red", "number": 7}, schema=schema)
    assert out.ok and out.source == "native"
    out = parse_structured(raw_text='{"shape": "triangle", "color": "red", "number": 7}', native=None, schema=schema)
    assert out.ok and out.source == "extracted"
    out = parse_structured(raw_text='{"shape": "triangle"}', native=None, schema=schema)
    assert not out.ok and any("number" in e for e in out.errors)
    out = parse_structured(raw_text="", native=None, schema=schema)
    assert not out.ok and out.source == "none"


def test_run_with_repair_allows_exactly_one_repair_round():
    schema = SCHEMAS["probe_report"]
    calls = []

    def invoke(repair_prompt):
        calls.append(repair_prompt)
        if repair_prompt is None:
            return None, '{"shape": "triangle"}'
        return {"shape": "triangle", "color": "red", "number": 7}, ""

    outcome, rounds = run_with_repair(invoke, schema, "probe_report")
    assert outcome.ok and rounds == 1
    assert calls[0] is None and "number" in calls[1]

    def always_bad(repair_prompt):
        calls.append(repair_prompt)
        return None, "nope"

    calls.clear()
    outcome, rounds = run_with_repair(always_bad, schema, "probe_report")
    assert not outcome.ok and rounds == 1 and len(calls) == 2
    assert outcome.value is None  # a failed repair never yields a value that could be mistaken for approval


def test_parse_outcome_is_never_truthy_when_invalid():
    o = ParseOutcome(ok=False, value=None, errors=["x"], source="none")
    assert not o.ok and o.value is None
