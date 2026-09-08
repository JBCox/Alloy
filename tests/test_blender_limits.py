"""R-88 attempt-limit reassessment and R-85/R-89 editable limits on the real fixture (layer B): two corrections that
do not verify on real renders move the finding to reassessment and stop the run with ``attempt_limit``; raising the
limit and resuming runs the materially different approach, which verifies on new renders."""
from __future__ import annotations

import pytest

from builder.blender.runner import BlenderRunner
from builder.config import BuilderConfig
from builder.engine import Engine
from builder.fixture import create_fixture
from builder.providers.mock import ScriptedAdapter

from _screenplay import screenplay

pytestmark = pytest.mark.blender


@pytest.fixture
def runner(blender_exe, workdir):
    return BlenderRunner(blender_exe, logs_dir=workdir / "logs")


def test_two_unverified_corrections_reassess_then_a_raised_limit_lets_the_new_approach_verify(runner, workdir):
    project, info = create_fixture(workdir / "fixture", runner)
    try:
        play = screenplay()
        tiny = {"operations": [{"intent": "nudge bracket", "target_part_ids": ["p_bracket"], "expected_outcome": "closer",
                                "declared_effects": {"creates": [], "modifies": ["p_bracket"], "deletes": []},
                                "script": "ALLOY.get('p_bracket').location.z -= 0.005\n"}],
                "self_assessment": "nudged", "questions_for_user": []}
        # B corrects twice without success; A (the reassessor, never the author) is reassigned the third, materially
        # different correction, and B verifies it on new renders
        proper = play["B"]["correction_task"][0]
        play["B"]["correction_task"] = [tiny, tiny]
        play["A"]["correction_task"] = [proper]
        play["A"]["verification"] = [{"finding_id": "{finding_id}", "verdict": "unchanged", "evidence_refs": ["render:side"], "rationale": "gap remains"},
                                     {"finding_id": "{finding_id}", "verdict": "unchanged", "evidence_refs": ["render:side"], "rationale": "gap remains"}]
        play["B"]["verification"] = [{"finding_id": "{finding_id}", "verdict": "improved", "evidence_refs": ["render:side"], "rationale": "seated"}]
        play["A"]["reassessment"] = [{"cause": "geometry", "materially_different_approach": "move the bracket onto the housing top face in one step",
                                      "evidence_gap": False}]
        cfg = BuilderConfig.from_dict({"builder": {"limits": {"attempts_per_finding": 2, "stall_steps": 12}}})
        cfg.agents["A"].provider = cfg.agents["B"].provider = "mock"
        adapters = {label: ScriptedAdapter(label, play[label]) for label in ("A", "B")}
        eng = Engine(project, cfg, adapters=adapters, runner=runner)
        eng.preflight(live=True)
        eng.start()
        status = eng.run_until_stop(max_steps=80)
        store = project.store
        assert status["stop_reason"] == "attempt_limit", status["notes"]
        f = store.list("finding")[0]
        assert f.state == "reassess" and f.data["reassessed"] and f.data["reassessment"]["cause"] == "geometry"
        attempts = store.list("correction_attempt")
        assert [a.state for a in attempts] == ["unchanged", "unchanged"]
        for a in attempts:      # every attempt kept its before and after renders as real files (R-88 preserve evidence)
            for rid in a.data["before_render_ids"] + a.data["after_render_ids"]:
                r = store.require("render", rid)
                assert r.state == "ok" and store.verify_file(r.data["file"])[0]
        assert status["consumption"]["steps_without_progress"] < 12
        # the measured gap is still open after two nudges (evidence, not agreement)
        rev = eng.operations.latest_revision()
        meas = runner.measure(rev.data["file"], {"bboxes": ["p_housing", "p_bracket"], "distances": [["p_housing", "p_bracket"]],
                                                 "ratios": [], "overlaps": {"ids": ["p_housing", "p_bracket"], "intended_contact": []}},
                              op_id="meas_after_two")
        assert meas.ok and meas.data["distances"][0]["bbox_gap"] > 0.1

        # raise the limit (R-85, R-89) and resume: the reassigned seat runs the materially different approach
        applied = eng.apply_limits({"attempts_per_finding": 3}, actor="user:test")
        assert applied == {"attempts_per_finding": 3}
        eng.resume(user="user:test")
        status = eng.run_until_stop(max_steps=80)
        assert status["stop_reason"] == "ready_for_user_review", status["notes"]
        f = store.get("finding", f.id)
        assert f.state == "closed" and len(f.data["attempts"]) == 3
        third = store.require("correction_attempt", f.data["attempts"][-1])
        assert third.data["agent_id"] == "ag_A" and third.data["verifier_agent_id"] == "ag_B"
        rev = eng.operations.latest_revision()
        meas = runner.measure(rev.data["file"], {"bboxes": ["p_housing", "p_bracket"], "distances": [["p_housing", "p_bracket"]],
                                                 "ratios": [], "overlaps": {"ids": ["p_housing", "p_bracket"], "intended_contact": [["p_housing", "p_bracket"]]}},
                              op_id="meas_final")
        assert meas.ok and meas.data["distances"][0]["bbox_gap"] < 1e-4
        assert any(e.event == "run.limits_changed" for e in store.journal())
        assert store.verify_rebuild()
    finally:
        project.close()
