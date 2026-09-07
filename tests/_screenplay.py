"""A JSON-serialisable mock screenplay shared by the CLI tests and the real-Blender end-to-end test.

Scripts are real bpy so the same screenplay drives the fake runner (which ignores them) and real Blender.
Placeholders such as "{finding_id}" and "{finding_ids}" are filled by the mock from the request metadata
Alloy attaches, so the screenplay never guesses ids."""
from __future__ import annotations

import json
from pathlib import Path

from builder.schemas import OBSERVATION_SECTIONS

PARTS = ["p_base", "p_post", "p_housing", "p_bracket", "p_lens", "p_bolt", "p_bolt.1", "p_bolt.2", "p_bolt.3"]


def _statement(text, status="observed"):
    return {"statement": text, "status": status, "evidence_refs": ["ref"]}


def observation(agent):
    return {**{s: [_statement(f"{agent}: {s}")] for s in OBSERVATION_SECTIONS}, "questions_for_user": []}


BRIEF = {"intended_asset": "Fixture lamp", "authoritative_references": ["front", "side", "three_quarter"],
         "modeling_scope": "head first", "deliverables": ["editable parts"], "scale_and_coordinates": "provisional, Z up",
         "symmetry_and_pose_assumptions": [], "fidelity_priorities": ["silhouette", "depth", "attachment"],
         "observed_vs_inferred_construction": [_statement("housing is a box on a post")],
         "missing_evidence": ["no rear view"], "unresolved_decisions": []}

PLAN = {"parts": [{"part_id": p, "name": p, "parent": None, "interpretation": "fixture part", "evidence_status": "observed",
                   "confidence": "medium", "questions": []} for p in PARTS],
        "relations": [{"from_part": "p_bracket", "to_part": "p_housing", "type": "attached_to"},
                      {"from_part": "p_post", "to_part": "p_base", "type": "attached_to"}],
        "dependencies": [], "interfaces": ["bracket mounts on housing top face"], "hypotheses": []}

FLOATING = {"part_id": "p_bracket", "region": "mount", "view": "side", "observed_mismatch": "bracket floats above the housing",
            "severity": "high", "confidence": "high", "evidence_refs": ["render:side"],
            "proposed_correction": "lower the bracket until it sits on the housing top face",
            "expected_improvement": "no gap between bracket and housing in the side view"}


def task_result(script, part, assessment):
    return {"operations": [{"intent": f"edit {part}", "target_part_ids": [part], "expected_outcome": "matches the reference",
                            "declared_effects": {"creates": [], "modifies": [part], "deletes": []}, "script": script}],
            "self_assessment": assessment, "questions_for_user": []}


def coverage():
    return [{"part_id": p, "view": v, "instances_inspected": "all"} for p in PARTS for v in ("front", "side", "three_quarter")]


def screenplay(*, housing_script="ALLOY.get('p_housing').scale.y = 0.6\n",
               bracket_script="ALLOY.get('p_bracket').location.z -= 0.15\n"):
    probes = {
        "probe": [{"shape": "triangle", "color": "red", "number": 7}],
        "write_probe": [{"attempted_path": "{attempted_path}", "outcome": "attempted_and_refused", "write_succeeded": False, "error_text": "denied"}],
        "session_probe": [{"nonce": "{nonce}"}, {"nonce": "{nonce}"}],
        # the mock states what it scripts: a cancelled process whose tree kill was confirmed (R-22 is proven live
        # by the process-layer tests and the real adapters, never by this screenplay)
        "cancel_probe": [{"__outcome__": "cancelled", "__kill_confirmed__": True}],
    }
    return {
        "A": {**probes,
              "intake_observation": [observation("A")], "brief_draft": [BRIEF], "construction_plan": [PLAN],
              "build_task": [task_result(housing_script, "p_housing", "housing depth restored to the side-view proportion")],
              "verification": [{"finding_id": "{finding_id}", "verdict": "improved", "evidence_refs": ["render:side"],
                                "rationale": "scripted"}]},
        "B": {**probes,
              "intake_observation": [observation("B")],
              "review_task": [{"findings": [FLOATING], "coverage": coverage()}],
              "review_reconcile": [{"findings_confirmed": "{finding_ids}", "findings_withdrawn": [], "notes": "confirmed"}],
              "correction_task": [task_result(bracket_script, "p_bracket", "bracket lowered onto the housing")]},
    }


def write_screenplay(path: Path, **kw) -> Path:
    path.write_text(json.dumps(screenplay(**kw), ensure_ascii=False, indent=1), encoding="utf-8")
    return path
