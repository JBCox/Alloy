"""The vertical slice with mock adapters (layer D): R-7/R-9 both agents build, R-11 independent observation and
withheld self-assessment, R-56 loop, R-58 no-edit result, R-74 closure only on verified new renders, R-76
attended gate, R-44 handoff, R-46 checkpoint restore, R-75 accept/reopen, R-5 malformed output never advances,
R-85/R-86 limits, R-88 two failed corrections."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from builder.config import BuilderConfig
from builder.engine import Engine
from builder.operations import Operations
from builder.ownership import OwnershipManager
from builder.project import Project
from builder.providers.mock import ScriptedAdapter
from builder.records import Record
from builder.references import References

from _fake_runner import FakeRunner

PARTS = ["p_base", "p_post", "p_housing", "p_bracket", "p_lens", "p_bolt"]


def _statement(text, status="observed"):
    return {"statement": text, "status": status, "evidence_refs": ["ref"]}


def observation(agent):
    from builder.schemas import OBSERVATION_SECTIONS

    return {**{s: [_statement(f"{agent}: {s}")] for s in OBSERVATION_SECTIONS}, "questions_for_user": []}


BRIEF = {"intended_asset": "Lamp", "authoritative_references": ["front", "side"], "modeling_scope": "head only first",
         "deliverables": ["editable parts"], "scale_and_coordinates": "provisional, Z up", "symmetry_and_pose_assumptions": [],
         "fidelity_priorities": ["silhouette", "depth"], "observed_vs_inferred_construction": [_statement("housing is a box")],
         "missing_evidence": ["no rear view"], "unresolved_decisions": []}

PLAN = {"parts": [{"part_id": p, "name": p, "parent": None, "interpretation": "x", "evidence_status": "observed",
                   "confidence": "medium", "questions": []} for p in PARTS],
        "relations": [{"from_part": "p_bracket", "to_part": "p_housing", "type": "attached_to"}],
        "dependencies": [], "interfaces": ["bracket mounts on housing top"], "hypotheses": []}


def build_result(script, part="p_housing", assessment="housing deepened to match side view"):
    return {"operations": [{"intent": f"edit {part}", "target_part_ids": [part], "expected_outcome": "matches side view",
                            "declared_effects": {"creates": [], "modifies": [part], "deletes": []}, "script": script}],
            "self_assessment": assessment, "questions_for_user": []}


def findings(*findings_list):
    return {"findings": list(findings_list),
            "coverage": [{"part_id": p, "view": v, "instances_inspected": "all"}
                         for p in PARTS for v in ("front", "side", "three_quarter")]}


FLOATING = {"part_id": "p_bracket", "region": "mount", "view": "side", "observed_mismatch": "bracket floats above housing",
            "severity": "high", "confidence": "high", "evidence_refs": ["render:side"],
            "proposed_correction": "lower bracket onto housing", "expected_improvement": "no gap visible in side view"}


def verification(verdict):
    # "{finding_id}" is filled by the mock from the request metadata Alloy attaches (mocks never guess ids)
    return {"finding_id": "{finding_id}", "verdict": verdict, "evidence_refs": ["render:side"], "rationale": "scripted"}


def confirm_all(req):
    return {"findings_confirmed": list(req.extra.get("finding_ids", [])), "findings_withdrawn": [], "notes": "confirmed"}


@pytest.fixture
def project(workdir):
    prj = Project.create(workdir / "wf", name="Lamp", asset_name="Lamp", extra={"first_component": "Head"})
    refs = References(prj)
    for label in ("front", "side"):
        p = workdir / f"{label}.png"
        Image.new("RGB", (120, 90), (90, 90, 90)).save(p)
        refs.add(p, labels=[label], kind="target")
    seed = workdir / "seed.blend"
    seed.write_bytes(b"BLENDER-fake-seed")
    ops = Operations(prj, None, OwnershipManager(prj.store))
    ops.register_revision(seed, parent_revision_id=None, created_by_op_id=None, actor="engine",
                          identity_map=[{"alloy_id": p, "type": "OBJECT"} for p in PARTS], note="fixture revision 0")
    for p in PARTS:
        prj.store.upsert(Record.new("part", {"name": p, "blender_ids": [p]}, id=p), actor="engine", event="part.created")
    yield prj
    prj.close()


def _config(**over):
    cfg = BuilderConfig.from_dict({"builder": {"limits": {"attempts_per_finding": 2}, **over}})
    cfg.agents["A"].provider = "mock"
    cfg.agents["B"].provider = "mock"
    return cfg


def _runner():
    r = FakeRunner()
    r.identity_map = [{"alloy_id": p, "type": "OBJECT"} for p in PARTS]
    return r


def _adapters(a_extra=None, b_extra=None):
    probes = {
        "probe": [{"shape": "triangle", "color": "red", "number": 7}],
        "write_probe": [{"attempted_path": "scratch/x.txt", "outcome": "attempted_and_refused", "write_succeeded": False, "error_text": "denied"}],
        # two invocations: the first creates the session (nonce in the prompt), the second resumes it
        "session_probe": [{"nonce": "{nonce}"}, {"nonce": "{nonce}"}],
        "cancel_probe": [{"__outcome__": "cancelled", "__kill_confirmed__": True}],
    }
    a = ScriptedAdapter("A", {
        **probes,
        "intake_observation": [observation("A")],
        "brief_draft": [BRIEF],
        "construction_plan": [PLAN],
        "build_task": [build_result("ALLOY.get('p_housing').scale.y = 1.0")],
        "verification": [verification("improved")],
        **(a_extra or {}),
    })
    b = ScriptedAdapter("B", {
        **probes,
        "intake_observation": [observation("B")],
        "review_task": [findings(FLOATING)],
        "review_reconcile": [confirm_all],
        "correction_task": [build_result("ALLOY.get('p_bracket').location.z -= 0.15", part="p_bracket",
                                         assessment="bracket lowered onto housing")],
        **(b_extra or {}),
    })
    return {"A": a, "B": b}


def _engine(project, adapters, runner=None, cfg=None):
    return Engine(project, cfg or _config(), adapters=adapters, runner=runner or _runner())


def test_preflight_reports_three_tiers_per_capability_and_blender(project):
    eng = _engine(project, _adapters())
    report = eng.preflight(live=True)
    for label in ("A", "B"):
        caps = report[label]["capabilities"]
        assert caps["image_reading"]["live"] == "verified" and caps["read_only_enforcement"]["live"] == "verified"
        assert caps["session_resume"]["live"] == "verified"
        assert caps["cancellation"]["live"] == "verified" and "confirmed" in caps["cancellation"]["evidence"]
        assert caps["model_settings_applied"]["live"] == "verified" and "effective" in caps["model_settings_applied"]["evidence"]
        assert caps["structured_output"]["declared"] == "yes" and caps["structured_output"]["local"] == "ok"
        assert report[label]["ok"] is True and report[label]["cache_key"]
    assert report["blender"]["blender_version"] == "fake-1.0"
    assert project.store.list("preflight_report")


def test_preflight_blocks_start_when_image_probe_fails(project):
    adapters = _adapters(b_extra={"probe": [{"shape": "square", "color": "red", "number": 7}]})
    eng = _engine(project, adapters)
    report = eng.preflight(live=True)
    assert report["B"]["ok"] is False and "image_reading" in report["B"]["blockers"][0]
    with pytest.raises(RuntimeError, match="preflight"):
        eng.start()


def test_full_slice_a_builds_b_finds_and_corrects_with_handoff_and_gate(project):
    adapters = _adapters()
    runner = _runner()
    eng = _engine(project, adapters, runner)
    eng.preflight(live=True)
    run = eng.start()
    assert run.state == "running"
    status = eng.run_until_stop(max_steps=60)
    store = project.store

    # attended gate: ready for user review is not accepted (R-76)
    assert status["execution"] == "waiting_for_user" and status["stop_reason"] == "ready_for_user_review"
    comp = status["components"][0]
    assert comp["review_state"] == "ready_for_user_review" and comp["acceptance_state"] == "unaccepted"

    # independent intake: neither intake packet included the other agent's observation (R-11)
    a_intake = next(i for i in adapters["A"].invocations if i.purpose == "intake_observation")
    b_intake = next(i for i in adapters["B"].invocations if i.purpose == "intake_observation")
    for req in (a_intake, b_intake):
        manifest = json.loads((Path(req.packet_dir) / "packet.json").read_text(encoding="utf-8"))
        assert manifest["included_records"].get("observations") in (None, [])
    assert [o.data["agent_id"] for o in store.list("observation")] and len(store.list("observation")) == 2

    # review packet withheld the builder's self-assessment until initial findings were recorded (R-11)
    review_req = next(i for i in adapters["B"].invocations if i.purpose == "review_task")
    review_manifest = json.loads((Path(review_req.packet_dir) / "packet.json").read_text(encoding="utf-8"))
    assert any("self-assessment" in w["what"] for w in review_manifest["withheld"])
    assert "housing deepened" not in (Path(review_req.packet_dir) / "PACKET.md").read_text(encoding="utf-8")
    reconcile_req = next(i for i in adapters["B"].invocations if i.purpose == "review_reconcile")
    assert "housing deepened" in (Path(reconcile_req.packet_dir) / "PACKET.md").read_text(encoding="utf-8")
    f = store.list("finding")[0]
    assert f.version >= 1 and f.data["initial_recorded_before_reconciliation"] is True

    # both agents committed operations (R-7, R-9); handoff happened (R-44)
    committed = [o for o in store.list("operation", state="committed")]
    assert {o.data["agent_id"] for o in committed} == {"ag_A", "ag_B"} or {o.data["agent_id"] for o in committed} == set(status["contributions"])
    assert status["contributions"]["A"] >= 1 and status["contributions"]["B"] >= 1
    handoffs = store.list("handoff")
    assert handoffs and handoffs[0].data["from_holder"].endswith("A") and handoffs[0].data["to_holder"].endswith("B")

    # finding closed only after a verified verification on new renders (R-74)
    f = store.get("finding", f.id)
    assert f.state == "closed"
    assert f.data["after_render_ids"] and set(f.data["after_render_ids"]).isdisjoint(set(f.data["before_render_ids"]))
    assert f.data["resolution"]["verdict"] == "improved" and f.data["resolution"]["verified_by"].endswith("A")
    revs = store.list("revision")
    assert len(revs) == 3  # seed, A's build, B's correction
    renders = store.list("render", state="ok")
    assert renders and all(r.data["manifest"]["source"]["revision_id"] in {rv.id for rv in revs} for r in renders)
    assert store.list("coverage")

    # checkpoints exist for the base revision and the review-ready revision
    cks = store.list("checkpoint")
    assert len(cks) >= 2

    # user acceptance binds the exact revision and evidence (R-6, R-75)
    acc = eng.accept_component(comp["id"], user="user:josh")
    assert acc.state == "accepted_at_revision" and acc.data["revision_id"] == revs[-1].id
    assert acc.data["evidence_versions"] and acc.data["dependency_versions"] is not None
    # reopen supersedes visibly and returns review to unreviewed
    eng.reopen_component(comp["id"], reason="rear seam looks wrong", user="user:josh")
    assert store.get("acceptance", acc.id).state == "superseded"
    assert store.get("component", comp["id"]).state == "unreviewed"
    assert store.get("acceptance", acc.id).data["supersede_reason"] == "user_reopen"

    # checkpoint restore creates a child revision of the checkpointed revision, never rewrites history (R-46)
    base_ck = cks[0]
    restored = eng.restore_checkpoint(base_ck.id, user="user:josh")
    assert restored.data["parent_revision_id"] == base_ck.data["revision_id"]
    assert Path(restored.data["file"]).read_bytes() == Path(store.get("revision", base_ck.data["revision_id"]).data["file"]).read_bytes()
    assert len(store.list("revision")) == 4
    assert store.verify_rebuild()


def test_no_edit_warranted_is_a_valid_result(project):
    adapters = _adapters(a_extra={"build_task": [{"operations": [], "self_assessment": "matches", "questions_for_user": [],
                                                  "no_edit_warranted": {"reason": "already matches the side view",
                                                                        "evidence": ["ref:side"]}}]},
                         b_extra={"review_task": [findings()]})
    eng = _engine(project, adapters)
    eng.preflight(live=True)
    eng.start()
    status = eng.run_until_stop(max_steps=40)
    assert status["stop_reason"] == "ready_for_user_review"
    tasks = [t for t in project.store.list("task") if t.data["kind"] == "build"]
    assert tasks[0].state == "done" and tasks[0].data["result"]["no_edit_warranted"]["reason"].startswith("already")
    assert len(project.store.list("revision")) == 1  # no cosmetic edits forced (R-58)


def test_malformed_review_output_never_advances_review(project):
    adapters = _adapters(b_extra={"review_task": [{"__raw__": "I think it looks good, done!"},
                                                  {"__raw__": "still not json, we agree"}]})
    eng = _engine(project, adapters)
    eng.preflight(live=True)
    eng.start()
    status = eng.run_until_stop(max_steps=40)
    assert status["stop_reason"] == "execution_failure"
    comp = project.store.list("component")[0]
    assert comp.state == "unreviewed" and project.store.list("finding") == []
    review_task = [t for t in project.store.list("task") if t.data["kind"] == "review"][0]
    assert review_task.state == "blocked" and "malformed" in review_task.data["error"]
    invs = [i for i in project.store.list("invocation") if i.data["purpose"] == "review_task"]
    assert invs[-1].data["outcome"] == "repair_failed" and invs[-1].data["repair_rounds"] == 1


def test_request_limit_stops_run_and_dispatches_nothing_more(project):
    adapters = _adapters()
    eng = _engine(project, adapters, cfg=_config(limits={"max_requests": 2, "attempts_per_finding": 2}))
    eng.preflight(live=True)
    after_preflight = len(adapters["A"].invocations) + len(adapters["B"].invocations)
    eng.start()
    status = eng.run_until_stop(max_steps=40)
    assert status["stop_reason"] == "budget_limit" and status["execution"] == "paused"
    assert status["consumption"]["requests"]["completed"] == 2
    assert len(adapters["A"].invocations) + len(adapters["B"].invocations) == after_preflight + 2


def test_two_failed_corrections_lead_to_reassessment_not_oscillation(project):
    adapters = _adapters(
        a_extra={"verification": [verification("unchanged"), verification("unchanged")],
                 "reassessment": [{"cause": "geometry", "materially_different_approach": "rebuild bracket mount",
                                   "evidence_gap": False}]},
        b_extra={"correction_task": [build_result("ALLOY.get('p_bracket').location.z -= 0.01", part="p_bracket"),
                                     build_result("ALLOY.get('p_bracket').location.z -= 0.01", part="p_bracket")]})
    eng = _engine(project, adapters)
    eng.preflight(live=True)
    eng.start()
    status = eng.run_until_stop(max_steps=80)
    f = project.store.list("finding")[0]
    assert f.state == "reassess"
    attempts = project.store.list("correction_attempt")
    assert len(attempts) == 2 and all(a.state == "unchanged" for a in attempts)
    assert status["consumption"]["correction_attempts"][f.id] == 2
    reassess = [t for t in project.store.list("task") if t.data["kind"] == "reassess"]
    assert reassess and reassess[0].data["owner_agent_id"] == "ag_A"
    assert status["stop_reason"] in ("attempt_limit", "stalled")


def test_provider_budget_stop_pauses_the_run_with_budget_limit(project):
    """R-85: a provider-side cap that stops a call after the spend ends the run as budget_limit, not as a failure."""
    adapters = _adapters(a_extra={"build_task": [{"__outcome__": "budget_exhausted", "__error__": "claude stopped at its cap"}]})
    eng = _engine(project, adapters)
    eng.preflight(live=True)
    eng.start()
    status = eng.run_until_stop(max_steps=40)
    assert status["stop_reason"] == "budget_limit" and status["execution"] == "paused"
    assert "stopped at its cap" in status["notes"][-1]["note"]
    task = [t for t in project.store.list("task") if t.data["kind"] == "build"][0]
    assert task.state == "in_progress"      # nothing advanced; resume after raising the cap retries the task


def test_write_probe_is_not_verified_when_no_write_was_attempted(project):
    """R-17: an agent that never tried to write proves nothing about enforcement."""
    adapters = _adapters(b_extra={"write_probe": [{"attempted_path": "x", "outcome": "could_not_attempt", "write_succeeded": False,
                                                   "error_text": "could not read the packet"}]})
    eng = _engine(project, adapters)
    report = eng.preflight(live=True)
    assert report["B"]["capabilities"]["read_only_enforcement"]["live"] == "not_verified"
    assert "could not attempt" in report["B"]["capabilities"]["read_only_enforcement"]["evidence"]
    assert report["B"]["ok"] is False and any("read_only_enforcement" in b for b in report["B"]["blockers"])
    # no write tool at all is structural enforcement and verifies (claude with --tools Read,Glob,Grep, observed live)
    adapters = _adapters(b_extra={"write_probe": [{"attempted_path": "x", "outcome": "no_write_tool_available",
                                                   "write_succeeded": False, "error_text": "only Read, Glob, Grep"}]})
    report = _engine(project, adapters).preflight(live=True)
    assert report["B"]["capabilities"]["read_only_enforcement"]["live"] == "verified"


def test_failed_first_invocation_never_turns_into_a_resume(project):
    """R-10: a session exists only after a successful create; a failed `new` call must not advance the counter."""
    adapters = _adapters(a_extra={"probe": [{"__outcome__": "provider_error", "__error__": "model rejected"},
                                            {"shape": "triangle", "color": "red", "number": 7}]})
    eng = _engine(project, adapters)
    eng.preflight(live=True, labels=["A"])
    probes = [i for i in adapters["A"].invocations if i.purpose == "probe"]
    assert probes[0].session.kind == "new"
    later = [i for i in adapters["A"].invocations if i.purpose != "probe"]
    assert later and later[0].session.kind == "new", "the write probe must create the session, not resume a failed one"
    assert any(e.event == "provider_session.not_advanced" for e in project.store.journal())
    assert "B" not in {k for k in eng.preflight_reports} or eng.preflight_reports.get("B") is None
    with pytest.raises(Exception, match="unknown agent"):
        eng.preflight(live=False, labels=["Z"])


def test_pause_request_is_honoured_at_a_safe_boundary(project):
    adapters = _adapters()
    eng = _engine(project, adapters)
    eng.preflight(live=True)
    eng.start()
    project.store.push_control("pause", {"by": "test"})
    status = eng.run_until_stop(max_steps=40)
    assert status["execution"] == "paused" and status["stop_reason"] == "user_pause"
    assert not project.store.list("operation", state="running")
    eng.resume(user="user:josh")
    status = eng.run_until_stop(max_steps=60)
    assert status["stop_reason"] == "ready_for_user_review"
