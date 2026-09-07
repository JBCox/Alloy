"""Agent output schemas (JSON Schema subset) and a dependency-free validator (spec R-5, R-24, R-28, R-29, R-73).

The same schema dicts are handed to providers that accept a schema natively (claude ``--json-schema``,
codex ``--output-schema``) and used locally to validate every output, native or extracted.
"""
from __future__ import annotations

from typing import Any

SEVERITIES = ["critical", "high", "medium", "low"]
# R-17 write probe: what the agent did about the requested write. The first two prove enforcement (a refused
# attempt, or no tool that could write at all); could_not_attempt proves nothing (for example the packet was unreadable).
WRITE_PROBE_OUTCOMES = ["written", "attempted_and_refused", "no_write_tool_available", "could_not_attempt"]
CONFIDENCES = ["high", "medium", "low"]
EVIDENCE_STATUS = ["observed", "inferred", "uncertain"]


def _obj(properties: dict[str, Any], required: list[str] | None = None, *, additional: bool = True) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    if not additional:
        schema["additionalProperties"] = False
    return schema


def _arr(items: dict[str, Any], min_items: int = 0) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": items}
    if min_items:
        schema["minItems"] = min_items
    return schema


STRING = {"type": "string"}
STRINGS = _arr(STRING)
QUESTION = _obj({"question": STRING, "why_it_changes_construction": STRING}, ["question", "why_it_changes_construction"])
STATEMENT = _obj({"statement": STRING, "status": {"type": "string", "enum": EVIDENCE_STATUS}, "evidence_refs": STRINGS},
                 ["statement", "status"])

OBSERVATION_SECTIONS = [
    "silhouette_and_proportions", "masses_and_depth", "pieces_and_construction", "armour_vs_frame",
    "overlaps_gaps_joints_attachments", "material_differences", "camera_and_pose_ambiguities",
    "contradictions_occlusion_missing_views_uncertainty",
]

OPERATION_REQUEST = _obj({
    "intent": STRING, "target_part_ids": STRINGS, "expected_outcome": STRING,
    "declared_effects": _obj({"creates": STRINGS, "modifies": STRINGS, "deletes": STRINGS}, ["creates", "modifies", "deletes"]),
    "script": STRING, "rationale": STRING,
}, ["intent", "target_part_ids", "expected_outcome", "declared_effects", "script"])

FINDING = _obj({
    "part_id": STRING, "region": STRING, "view": STRING, "observed_mismatch": STRING,
    "severity": {"type": "string", "enum": SEVERITIES}, "confidence": {"type": "string", "enum": CONFIDENCES},
    "evidence_refs": _arr(STRING, 1), "proposed_correction": STRING, "expected_improvement": STRING,
    "alternative_hypotheses": STRINGS, "kind": {"type": "string", "enum": ["defect", "uncertain_interpretation"]},
    "rationale": STRING,
}, ["part_id", "observed_mismatch", "severity", "confidence", "evidence_refs", "proposed_correction", "expected_improvement"])

# instances_inspected is text ("all", "3 of 4", ...): a single type keeps the schema expressible in every
# provider's structured-output dialect (OpenAI strict mode, observed live with codex 0.153.1).
COVERAGE = _obj({"part_id": STRING, "view": STRING, "instances_inspected": STRING,
                 "sampling_strategy": STRING}, ["part_id", "view", "instances_inspected"])
# R-35 approximate dimensions and ratios with uncertainty: named entries, never a free-form object (same reason).
DIMENSION = _obj({"name": STRING, "value": STRING, "unit": STRING, "status": {"type": "string", "enum": EVIDENCE_STATUS},
                  "note": STRING}, ["name", "value", "status"])

SCHEMAS: dict[str, dict[str, Any]] = {
    "probe_report": _obj({"shape": {"type": "string", "enum": ["triangle", "square", "circle"]}, "color": STRING,
                          "number": {"type": "integer"}}, ["shape", "color", "number"]),
    "write_probe_report": _obj({"attempted_path": STRING,
                                "outcome": {"type": "string", "enum": WRITE_PROBE_OUTCOMES},
                                "write_succeeded": {"type": "boolean"}, "error_text": STRING},
                               ["attempted_path", "outcome", "write_succeeded"]),
    "session_probe_report": _obj({"nonce": STRING}, ["nonce"]),
    "observation_report": _obj({**{s: _arr(STATEMENT) for s in OBSERVATION_SECTIONS},
                                "questions_for_user": _arr(QUESTION), "rationale": STRING},
                               OBSERVATION_SECTIONS + ["questions_for_user"]),
    "brief_draft": _obj({
        "intended_asset": STRING, "authoritative_references": STRINGS, "modeling_scope": STRING, "deliverables": STRINGS,
        "scale_and_coordinates": STRING, "symmetry_and_pose_assumptions": STRINGS, "fidelity_priorities": STRINGS,
        "observed_vs_inferred_construction": _arr(STATEMENT), "missing_evidence": STRINGS,
        "unresolved_decisions": _arr(QUESTION), "rationale": STRING,
    }, ["intended_asset", "authoritative_references", "modeling_scope", "deliverables", "scale_and_coordinates",
        "symmetry_and_pose_assumptions", "fidelity_priorities", "observed_vs_inferred_construction", "missing_evidence",
        "unresolved_decisions"]),
    "construction_plan": _obj({
        "parts": _arr(_obj({"part_id": STRING, "name": STRING, "parent": {"type": ["string", "null"]},
                            "interpretation": STRING, "dimensions": _arr(DIMENSION),
                            "evidence_status": {"type": "string", "enum": ["observed", "inferred", "mixed"]},
                            "confidence": {"type": "string", "enum": CONFIDENCES}, "questions": STRINGS},
                           ["part_id", "name", "interpretation", "evidence_status", "confidence"])),
        "relations": _arr(_obj({"from_part": STRING, "to_part": STRING, "type": STRING}, ["from_part", "to_part", "type"])),
        "dependencies": _arr(_obj({"from_id": STRING, "to_part": STRING}, ["from_id", "to_part"])),
        "interfaces": STRINGS,
        "hypotheses": _arr(_obj({"about": STRING, "chosen": STRING, "alternatives": STRINGS}, ["about", "chosen"])),
        "rationale": STRING,
    }, ["parts", "relations", "dependencies", "interfaces", "hypotheses"]),
    "operation_request": OPERATION_REQUEST,
    "task_result": _obj({
        "operations": _arr(OPERATION_REQUEST), "self_assessment": STRING, "questions_for_user": _arr(QUESTION),
        "no_edit_warranted": _obj({"reason": STRING, "evidence": STRINGS}, ["reason", "evidence"]),
        "rationale": STRING,
    }, ["operations", "self_assessment", "questions_for_user"]),
    "findings_report": _obj({"findings": _arr(FINDING), "coverage": _arr(COVERAGE), "rationale": STRING},
                            ["findings", "coverage"]),
    "reconciliation": _obj({
        "findings_confirmed": STRINGS,
        "findings_withdrawn": _arr(_obj({"finding_id": STRING, "reason": STRING}, ["finding_id", "reason"])),
        "notes": STRING,
    }, ["findings_confirmed", "findings_withdrawn"]),
    "verification_report": _obj({
        "finding_id": STRING, "verdict": {"type": "string", "enum": ["improved", "unchanged", "regressed", "uncertain"]},
        "evidence_refs": _arr(STRING, 1), "rationale": STRING,
    }, ["finding_id", "verdict", "evidence_refs"]),
    "reassessment": _obj({
        "cause": {"type": "string", "enum": ["geometry", "camera", "material", "lighting", "evidence", "unknown"]},
        "materially_different_approach": STRING, "evidence_gap": {"type": "boolean"}, "rationale": STRING,
    }, ["cause", "materially_different_approach", "evidence_gap"]),
}


def validate_against(schema: dict[str, Any], value: Any, path: str = "$") -> list[str]:
    """Return a list of problems (empty when valid). Supports the subset used by SCHEMAS."""
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
        return errors
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_is_type(value, t) for t in allowed):
            errors.append(f"{path}: expected {' or '.join(allowed)}, got {type(value).__name__}")
            return errors
    if isinstance(value, dict) and (types == "object" or types is None or "properties" in schema):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing required key {key!r}")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                errors.extend(validate_against(sub, value[key], f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append(f"{path}: unexpected key {key!r}")
    if isinstance(value, list) and (types == "array" or "items" in schema):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: expected at least {schema['minItems']} items, got {len(value)}")
        items = schema.get("items")
        if items:
            for i, item in enumerate(value):
                errors.extend(validate_against(items, item, f"{path}[{i}]"))
    if isinstance(value, str) and "minLength" in schema and len(value) < schema["minLength"]:
        errors.append(f"{path}: shorter than {schema['minLength']}")
    return errors


def _is_type(value: Any, t: str) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    return False
