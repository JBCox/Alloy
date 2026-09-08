"""The GUI's engine binding without Tk (layer D, R-91, R-92): a read model built from the records, and a session
that runs every engine call on a worker thread and publishes events and snapshots on a ``queue.Queue``.
Mock adapters and the fake runner script exactly what they return; the tests prove routing, threading, and the
labels the view shows (revision and render ids, observed/inferred, effective reasoning), never visual judgement."""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from builder.config import BuilderConfig
from builder.engine import Engine
from builder.project import Project
from builder.viewmodel import BuilderSession, overlay_rects, snapshot

from _fake_runner import FakeRunner
from test_engine import _adapters, _config, _runner, project  # noqa: F401  (fixture re-export)


def _drain(q: queue.Queue, *, until: str = "snapshot", timeout: float = 30.0) -> list[tuple]:
    """Collect messages for one job: until an error, or until the job's ``done`` and (when ``until`` is
    ``snapshot``) the snapshot published after it. Snapshots also arrive mid-run at stage boundaries."""
    out: list[tuple] = []
    deadline = time.time() + timeout
    done = False
    while time.time() < deadline:
        try:
            msg = q.get(timeout=0.2)
        except queue.Empty:
            continue
        out.append(msg)
        if msg[0] == "error":
            return out
        if msg[0] == "done":
            done = True
            if until == "done":
                return out
        elif done and msg[0] == until:
            return out
    raise AssertionError(f"no {until!r} message within {timeout}s; got {[m[0] for m in out]}")


def _kinds(msgs: list[tuple]) -> list[str]:
    return [m[0] for m in msgs]


# --- read model ---------------------------------------------------------------------------------------------

def test_snapshot_exposes_every_r91_panel_from_records(project):
    adapters = _adapters()
    runner = _runner()
    eng = Engine(project, _config(), adapters=adapters, runner=runner)
    eng.preflight(live=True)
    eng.start()
    eng.run_until_stop(max_steps=60)

    snap = snapshot(eng)
    # stage and ownership (R-91): execution state, stop reason, stage, last task with owner and rationale
    assert snap["stage"]["execution"] == "waiting_for_user" and snap["stage"]["stop_reason"] == "ready_for_user_review"
    assert snap["stage"]["task"]["owner"] in ("A", "B") and snap["stage"]["task"]["rationale"]
    assert snap["stage"]["ownership"] is None          # released at the gate (R-44)
    # agents with the effective reasoning setting and capability tiers (R-15, R-20)
    a = next(x for x in snap["agents"] if x["label"] == "A")
    assert a["reasoning_requested"] == "max" and a["reasoning_effective"] == "max"
    assert a["preflight"]["ok"] is True and a["preflight"]["capabilities"]["image_reading"]["live"] == "verified"
    # part tree with construction relations, distinct from the parent hierarchy (R-36)
    bracket = next(p for p in snap["parts"] if p["id"] == "p_bracket")
    assert (bracket["relations"][0]["type"], bracket["relations"][0]["to_part"]) == ("attached_to", "p_housing")
    assert bracket["evidence_status"] == "observed"
    # renders are actual artifacts labelled with revision and render ids and the evidence label (R-92, R-61)
    assert snap["renders"] and all(r["id"].startswith("rnd_") and r["revision_id"].startswith("rev_") for r in snap["renders"])
    assert {r["evidence_label"] for r in snap["renders"]} >= {"matched", "inferred_construction"}
    assert all(Path(r["file"]).is_file() for r in snap["renders"] if r["state"] == "ok")
    # references keep their canon state and whether they are evidence of the original (R-32, R-94)
    assert all(r["canon_state"] == "approved" and r["evidence_of_original"] is True for r in snap["references"])
    # findings linked to parts and images, with before and after renders (R-73)
    f = snap["findings"][0]
    assert f["part_id"] == "p_bracket" and f["state"] == "closed"
    assert f["before_render_ids"] and f["after_render_ids"] and set(f["before_render_ids"]).isdisjoint(f["after_render_ids"])
    assert f["severity"] == "high" and f["confidence"] == "high"
    # coverage by part, view, revision (R-72)
    assert any(c["part_id"] == "p_bracket" and c["view_name"] == "side" for c in snap["coverage"])
    # consumption and configured limits with enforceability (R-85)
    assert snap["consumption"]["requests"]["completed"] > 0
    assert "attempts_per_finding" in snap["consumption"]["limits"]
    # checkpoints and revisions, components with review and acceptance kept distinct (R-76)
    assert snap["checkpoints"] and len(snap["revisions"]) >= 2
    comp = snap["components"][0]
    assert comp["review_state"] == "ready_for_user_review" and comp["acceptance_state"] == "unaccepted"
    # the concept summary always carries the approval mode (D11)
    assert snap["concept"]["mode"] == "each" and snap["concept"]["mode_text"]
    # everything is plain data the Tk thread can render without touching the store
    json.dumps(snap, default=str)


def test_overlay_rects_project_measured_boxes_into_render_pixels(project):
    adapters = _adapters()
    runner = _runner()
    runner.measure_data = {"bboxes": {"p_bracket": {"min": [-0.15, -0.15, 2.3], "max": [0.15, 0.15, 2.4]},
                                      "p_housing": {"min": [-0.45, -0.18, 1.7], "max": [0.45, 0.18, 2.2]}},
                           "distances": [], "ratios": {}, "overlaps": [], "limitations": ["fake"]}
    eng = Engine(project, _config(), adapters=adapters, runner=runner)
    eng.preflight(live=True)
    eng.start()
    eng.run_until_stop(max_steps=60)
    snap = snapshot(eng)
    side = next(r for r in snap["renders"] if r["view_name"] == "side" and r["state"] == "ok")
    rects = overlay_rects(snap, side["id"])
    assert {r["part_id"] for r in rects} == {"p_bracket", "p_housing"}
    w, h = side["resolution"]
    for r in rects:
        assert 0 <= r["x"] < w and 0 <= r["y"] < h and r["w"] > 0 and r["h"] > 0
        assert r["space"] == "render_pixels" and r["source"] == "measurement"
    # a render without a measurement for its revision has nothing to overlay: never invented (R-68)
    assert overlay_rects(snap, "rnd_missing") == []


# --- session: worker thread and queue -----------------------------------------------------------------------

@pytest.fixture
def session(project, workdir):
    project.close()
    cfg = _config()
    sess = BuilderSession(cfg, runner_factory=lambda cfg, prj: _runner(), adapters_factory=lambda cfg, mock: _adapters())
    yield sess
    sess.close(timeout=10)


def test_session_runs_the_engine_off_the_calling_thread_and_publishes_snapshots(session, workdir):
    session.open(workdir / "wf")
    msgs = _drain(session.events, until="snapshot")
    assert _kinds(msgs)[:2] == ["busy", "done"] and msgs[1][1] == "open"
    assert msgs[-1][1]["project"]["name"] == "Lamp"
    assert session.worker_thread_name != threading.current_thread().name

    session.preflight(live=True)
    msgs = _drain(session.events, until="snapshot")
    assert all(a["preflight"]["ok"] for a in msgs[-1][1]["agents"])

    session.start_run(attended=True)
    msgs = _drain(session.events, until="snapshot", timeout=60)
    kinds = _kinds(msgs)
    assert "event" in kinds and kinds[0] == "busy"
    stages = [m[1]["stage"] for m in msgs if m[0] == "event" and m[1]["event"] == "stage"]
    assert stages[0] == "intake" and "review" in stages and "gate" in stages
    snap = msgs[-1][1]
    assert snap["stage"]["execution"] == "waiting_for_user" and snap["components"][0]["acceptance_state"] == "unaccepted"
    # progress during the run (R-92): a snapshot at every stage boundary, not only when the job ends
    snaps = [m[1] for m in msgs if m[0] == "snapshot"]
    assert len(snaps) >= len(stages)
    assert any(s["stage"]["execution"] == "running" and s["stage"]["stage"] == "review" for s in snaps)


def test_session_pause_request_stops_the_run_at_a_safe_boundary(session, workdir):
    session.open(workdir / "wf")
    _drain(session.events, until="snapshot")
    session.preflight(live=True)
    _drain(session.events, until="snapshot")
    session.start_run(attended=True)
    msgs: list[tuple] = []
    deadline = time.time() + 60
    done = False
    while time.time() < deadline:
        msg = session.events.get(timeout=1)
        msgs.append(msg)
        if msg[0] == "event" and msg[1]["event"] == "stage" and msg[1]["stage"] == "intake":
            session.pause()                           # from the Tk thread while the worker is inside a step
        if msg[0] == "done":
            done = True
        if msg[0] == "error" or (done and msg[0] == "snapshot"):
            break
    snap = msgs[-1][1]
    assert snap["stage"]["execution"] == "paused" and snap["stage"]["stop_reason"] == "user_pause"
    stages = [m[1]["stage"] for m in msgs if m[0] == "event" and m[1]["event"] == "stage"]
    assert 1 <= len(stages) <= 2, stages                 # the current step finishes; nothing new is dispatched after it
    # a paused run resumes from where it stopped
    session.resume_run(user="josh")
    snap = _drain(session.events, until="snapshot", timeout=60)[-1][1]
    assert snap["stage"]["execution"] == "waiting_for_user" and snap["stage"]["stop_reason"] == "ready_for_user_review"


def test_session_user_actions_accept_and_reopen_keep_review_and_acceptance_distinct(session, workdir):
    session.open(workdir / "wf")
    _drain(session.events, until="snapshot")
    session.preflight(live=True)
    _drain(session.events, until="snapshot")
    session.start_run(attended=True)
    snap = _drain(session.events, until="snapshot", timeout=60)[-1][1]
    comp_id = snap["components"][0]["id"]
    session.accept_component(comp_id, user="josh")
    snap = _drain(session.events, until="snapshot")[-1][1]
    assert snap["components"][0]["acceptance_state"] == "accepted_at_revision"
    session.reopen_component(comp_id, reason="lens colour still wrong", user="josh")
    snap = _drain(session.events, until="snapshot")[-1][1]
    assert snap["components"][0]["acceptance_state"] == "unaccepted" and snap["components"][0]["review_state"] == "unreviewed"
    assert snap["stage"]["stage"] == "build"


def test_session_create_mirrors_cli_new_then_opens(session, workdir):
    """The app's New project: the CLI's `new` (or `preset create`) and then `open`, as ONE job, so the window
    never sees a half-created project. R-93 still holds: nothing is read from the references or sources."""
    session.create(name="Lamp two", asset_name="Lamp", workflow_dir=workdir / "wf2", first_component="Head", user="josh")
    msgs = _drain(session.events, until="snapshot")
    assert _kinds(msgs)[:2] == ["busy", "done"] and msgs[1][1] == "new_project"
    result = msgs[1][2]
    assert result["created"] is True and result["project_id"].startswith("prj") and result["name"] == "Lamp two"
    assert Path(result["workflow_dir"]) == workdir / "wf2" and (workdir / "wf2" / "project.json").is_file()
    assert result["run_state"] == "idle" and result["needs_recovery"] is False
    snap = msgs[-1][1]
    assert snap["project"]["name"] == "Lamp two" and snap["project"]["first_component"] == "Head"
    assert snap["project"]["asset_name"] == "Lamp"
    # the same directory again is an error, never a silent reopen
    session.create(name="Lamp two", workflow_dir=workdir / "wf2")
    msgs = _drain(session.events, until="error")
    assert msgs[-1][1] == "new_project" and "already exists" in msgs[-1][2]
    assert session.events.get(timeout=10)[0] == "snapshot"      # the still-open project's snapshot follows a failed job
    # a preset goes through Project.create_from_preset (intake data recorded, sources untouched)
    session.config.presets["demo"] = {"name": "Demo asset", "asset": "Demo", "project_dir": str(workdir / "proj"),
                                      "target_reference": str(workdir / "art" / "c.png"), "first_component": "Base"}
    session.create(preset_id="demo", workflow_dir=workdir / "wf3", user="josh")
    msgs = _drain(session.events, until="snapshot")
    assert msgs[1][1] == "new_project" and msgs[1][2]["name"] == "Demo asset"
    snap = msgs[-1][1]
    assert snap["project"]["preset_id"] == "demo" and snap["project"]["first_component"] == "Base"
    assert snap["project"]["target_reference"]["path"] == str(workdir / "art" / "c.png")
    assert not (workdir / "proj").exists()
    # no name and no preset is refused up front
    session.create()
    msgs = _drain(session.events, until="error")
    assert "needs a name" in msgs[-1][2]
    assert session.events.get(timeout=10)[0] == "snapshot"
    session.create(preset_id="nope")
    msgs = _drain(session.events, until="error")
    assert "unknown preset" in msgs[-1][2]
    assert session.events.get(timeout=10)[0] == "snapshot"


def test_session_close_project_publishes_done_without_a_snapshot(session, workdir):
    session.open(workdir / "wf")
    _drain(session.events, until="snapshot")
    session.close_project()
    msgs = _drain(session.events, until="done")
    assert msgs[-1][1] == "close_project" and msgs[-1][2] == {"closed": True}
    assert session.project is None and session.engine is None and session.concept is None
    # the open result names the project, so a host can list it without a snapshot
    session.open(workdir / "wf")
    msgs = _drain(session.events, until="snapshot")
    assert msgs[1][2]["name"] == "Lamp"
    session.close_project()
    msgs = _drain(session.events, until="done")
    session.close_project()
    msgs = _drain(session.events, until="done")
    assert msgs[-1][2] == {"closed": False}


@pytest.mark.blender
def test_session_create_fixture_builds_the_demo_project_and_runs_it_with_scripted_seats(blender_exe, workdir):
    """The app's "demo" path: the fixture lamp on real Blender with the built-in screenplay, nothing spent."""
    from builder.harness import fixture_screenplay

    cfg = _config()
    sess = BuilderSession(cfg, mock=fixture_screenplay())
    try:
        sess.create_fixture(workdir / "fx")
        msgs = _drain(session_events := sess.events, until="snapshot", timeout=300)
        assert msgs[1][1] == "new_fixture" and msgs[1][2]["created"] is True and msgs[1][2]["fixture"] is True
        assert "floating_bracket" in " ".join(msgs[1][2]["defects"]) or msgs[1][2]["defects"]
        snap = msgs[-1][1]
        assert snap["project"]["fixture"] is True and len(snap["references"]) >= 2
        sess.preflight(live=True)                      # scripted seats: free
        _drain(session_events, until="snapshot")
        sess.start_run(attended=True)
        snap = _drain(session_events, until="snapshot", timeout=600)[-1][1]
        assert snap["stage"]["execution"] == "waiting_for_user"
    finally:
        sess.close(timeout=30)


def test_session_reports_job_errors_and_stays_usable(session, workdir):
    session.open(workdir / "nope")
    msgs = _drain(session.events, until="error")
    assert msgs[-1][1] == "open" and "no workflow project" in msgs[-1][2]
    session.open(workdir / "wf")
    msgs = _drain(session.events, until="snapshot")
    assert msgs[-1][1]["project"]["name"] == "Lamp"


def test_session_concept_panel_data_prompts_candidates_and_both_verdicts(workdir):
    from test_concept import AD_PROMPT, CANON, PICK, VERDICT_OK, png

    prj = Project.create(workdir / "wf c", name="Drone", asset_name="Drone", extra={"first_component": "Hull"})
    prj.close()
    cfg = BuilderConfig.from_dict({"builder": {"concept": {"approval": "each", "views": ["front"], "anchor_candidates": 2},
                                               "image_generation": {"seat": "manual", "vendor": "chatgpt"}}})
    cfg.agents["A"].provider = cfg.agents["B"].provider = "mock"
    extra = {"art_director_prompt": [AD_PROMPT] * 4, "canon_description": [CANON], "consistency_verdict": [VERDICT_OK] * 4,
             "anchor_pick": [PICK]}
    sess = BuilderSession(cfg, runner_factory=lambda c, p: FakeRunner(),
                          adapters_factory=lambda c, mock: _adapters(a_extra=extra, b_extra=extra))
    try:
        sess.open(workdir / "wf c")
        _drain(sess.events, until="snapshot")
        sess.preflight(live=True)
        _drain(sess.events, until="snapshot")
        sess.concept_start(from_text="a hexapod maintenance drone", user="josh")
        snap = _drain(sess.events, until="snapshot")[-1][1]
        c = snap["concept"]
        assert c["mode"] == "each" and "owner approves every" in c["mode_text"] and c["canon_state"] == "anchor_pending"
        assert len(c["prompts"]) == 1 and c["prompts"][0]["prompt"] == AD_PROMPT["prompt"] and c["prompts"][0]["import_command"]
        req = c["prompts"][0]["request_id"]
        a = png(workdir / "a.png", (1, 2, 3))
        b = png(workdir / "b.png", (4, 5, 6))
        sess.concept_import(req, [a, b], vendor="chatgpt", model="GPT Image", user="josh")
        snap = _drain(sess.events, until="snapshot")[-1][1]
        cands = snap["concept"]["candidates"]
        assert len(cands) == 2 and all(Path(x["file"]).is_file() and x["declared"]["vendor"] == "chatgpt" for x in cands)
        sess.concept_approve([cands[0]["id"]], user="josh")
        snap = _drain(sess.events, until="snapshot")[-1][1]
        c = snap["concept"]
        assert c["anchor"]["id"] == cands[0]["id"] and c["canon_state"] == "turnaround_pending"
        assert [p["target"] for p in c["prompts"]] == ["view front"] and c["prompts"][0]["attachments"][0]["reference_id"] == cands[0]["id"]
        front = png(workdir / "front.png", (7, 7, 7))
        sess.concept_import(c["prompts"][0]["request_id"], [front], user="josh")
        snap = _drain(sess.events, until="snapshot")[-1][1]
        cand = next(x for x in snap["concept"]["candidates"] if "front" in x["labels"])
        assert set(cand["verdicts"]) == {"A", "B"} and cand["verdicts"]["A"]["verdict"] == "consistent"
        assert cand["summary"] == "consistent" and snap["concept"]["coverage"]["front"]["status"] == "candidates"
    finally:
        sess.close(timeout=10)


# --- Phase 4: reference regions and editable limits through the read model and session --------------------------------

def test_snapshot_lists_reference_regions_in_original_pixels(project):
    from builder.references import References

    refs = References(project)
    ref = refs.current()[0]
    reg = refs.add_region(ref.id, "lower-left humanoid", [10, 12, 50, 40], purpose="target_region")
    eng = Engine(project, _config(), adapters=_adapters(), runner=_runner())
    snap = snapshot(eng)
    entry = next(r for r in snap["references"] if r["id"] == ref.id)
    assert entry["regions"] == [{"id": reg.id, "name": "lower-left humanoid", "bbox": [10, 12, 50, 40], "purpose": "target_region",
                                 "space": "original_pixels"}]
    other = next(r for r in snap["references"] if r["id"] != ref.id)
    assert other["regions"] == []


def test_session_add_region_and_set_limits_run_on_the_worker(session, workdir):
    session.open(workdir / "wf")
    snap = _drain(session.events, until="snapshot")[-1][1]
    ref_id = snap["references"][0]["id"]
    session.add_region(ref_id, "cap", [5, 5, 40, 30], purpose="detail_crop", user="josh")
    msgs = _drain(session.events, until="snapshot")
    assert msgs[1][0] == "done" and msgs[1][1] == "add_region" and msgs[1][2]["region_id"].startswith("reg_")
    entry = next(r for r in msgs[-1][1]["references"] if r["id"] == ref_id)
    assert entry["regions"][0]["bbox"] == [5, 5, 40, 30] and entry["regions"][0]["purpose"] == "detail_crop"
    session.add_region(ref_id, "off", [500, 500, 10, 10], purpose="target_region", user="josh")
    msgs = _drain(session.events, until="error")
    assert msgs[-1][1] == "add_region" and "outside" in msgs[-1][2]
    assert session.events.get(timeout=10)[0] == "snapshot"        # the snapshot published after the failed job
    # limits: applied on the worker and visible in the next snapshot; a bad name is an error, never a silent default
    session.set_limits({"max_requests": "7", "stall_steps": 3}, user="josh")
    msgs = _drain(session.events, until="snapshot")
    done = next(m for m in msgs if m[0] == "done")                 # the engine's "limits" event precedes it
    assert done[2]["applied"] == {"max_requests": 7, "stall_steps": 3}
    lim = msgs[-1][1]["consumption"]["limits"]
    assert lim["max_requests"]["value"] == 7 and lim["stall_steps"]["value"] == 3
    session.set_limits({"max_tokens": 1}, user="josh")
    msgs = _drain(session.events, until="error")
    assert "unknown limit" in msgs[-1][2]
