"""Engine hooks for the concept stage: the image seat in preflight (R-109), the concept summary in status, and the
study request an agent may raise during reassessment (R-104)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from builder.concept import ConceptStage  # noqa: E402

from test_engine import _adapters, _config, _engine, _runner, project, verification  # noqa: E402,F401


def test_preflight_reports_the_image_seat_tiers(project):
    eng = _engine(project, _adapters(), cfg=_config(concept={"import_dir": str(project.path("imports"))},
                                                    image_generation={"vendor": "gemini"}))
    report = eng.preflight(live=True)
    seat = report["I"]
    assert seat["tiers"] == {"declared": "manual", "local": "not_confirmed", "live": "not_applicable"}
    assert seat["declared"]["vendor"] == "gemini" and "declared by the owner" in seat["live"]["evidence"]
    assert "both LLM seats" in seat["live"]["evidence"] and seat["ok"] is True
    assert seat["local"]["import_dir"] == str(project.path("imports"))
    stored = [r for r in project.store.list("preflight_report") if r.data.get("seat") == "I"]
    assert stored and stored[-1].data["tiers"]["live"] == "not_applicable"
    # the image seat never blocks a modeling run: an unconfirmed flow is reported, not a blocker
    eng.start()


def test_status_carries_the_concept_summary_with_the_mode_shown(project):
    eng = _engine(project, _adapters())
    status = eng.status()
    assert status["concept"]["canon_state"] == "no_canon" and status["concept"]["mode"] == "each"
    assert "owner approves every generated image" in status["concept"]["mode_text"]
    assert status["concept"]["seat"]["seat"] == "manual"


def test_reassessment_may_request_a_study_that_becomes_a_generation_request(project, workdir):
    play = {"A": {"art_director_prompt": [{"subject": "lamp", "silhouette": "s", "construction": "c", "materials": "m", "palette": "p",
                                           "style": "st", "framing": "f", "prompt": "STUDY PROMPT", "avoid": [], "attachments": []}]}}
    adapters = _adapters(
        a_extra={"verification": [verification("unchanged"), verification("unchanged")],
                 "reassessment": [{"cause": "evidence", "materially_different_approach": "need a close look at the mount",
                                   "evidence_gap": True,
                                   "study_requests": [{"part_id": "p_bracket", "view": "side", "region": "mount",
                                                       "purpose": "how the bracket meets the housing",
                                                       "draft_prompt": "close-up of the bracket mount"}]}],
                 **play["A"]},
        b_extra={"correction_task": [_correction(), _correction()]})
    eng = _engine(project, adapters)
    eng.preflight(live=True)
    eng.start()
    status = eng.run_until_stop(max_steps=80)
    assert status["stop_reason"] == "missing_evidence"
    studies = project.store.list("study_request")
    assert len(studies) == 1 and studies[0].data["part_id"] == "p_bracket" and studies[0].data["requested_by"] == "agent:A"
    # without a concept stage (owner-supplied references only) the study is recorded and the stop note says how to generate it
    assert studies[0].state == "requested" and studies[0].data.get("generation_id") is None
    assert "study" in status["notes"][-1]["note"] and "concept" in status["notes"][-1]["note"]
    assert any("p_bracket" in str(u) for u in status["uncertainties"])


def _correction():
    return {"operations": [{"intent": "nudge", "target_part_ids": ["p_bracket"], "expected_outcome": "x",
                            "declared_effects": {"creates": [], "modifies": ["p_bracket"], "deletes": []},
                            "script": "ALLOY.get('p_bracket').location.z -= 0.01"}],
            "self_assessment": "moved", "questions_for_user": []}
