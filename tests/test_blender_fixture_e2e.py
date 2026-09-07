"""Section 5 fixture and the end-to-end slice on a real Blender (layer B): build, find, correct, render,
measure, close, handoff, checkpoint restore, reopen; stale evidence detected (R-64); cancellation mid-operation (R-22)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from builder.blender.runner import BlenderRunner
from builder.config import BuilderConfig
from builder.engine import Engine
from builder.fixture import FIXTURE_DEFECTS, create_fixture
from builder.providers.mock import ScriptedAdapter
from builder.render import render_is_stale

from _screenplay import screenplay

pytestmark = pytest.mark.blender


@pytest.fixture
def runner(blender_exe, workdir):
    return BlenderRunner(blender_exe, logs_dir=workdir / "logs")


def _config():
    cfg = BuilderConfig.from_dict({"builder": {"limits": {"attempts_per_finding": 2}}})
    cfg.agents["A"].provider = "mock"
    cfg.agents["B"].provider = "mock"
    return cfg


def _adapters(play):
    return {label: ScriptedAdapter(label, play[label]) for label in ("A", "B")}


def test_fixture_has_controlled_defects_and_synthetic_references(runner, workdir):
    project, info = create_fixture(workdir / "fixture", runner)
    try:
        assert set(info["defects"]) == set(FIXTURE_DEFECTS)
        refs = project.store.list("reference")
        assert {r.data["labels"][0] for r in refs} >= {"front", "side", "three_quarter"}
        assert all(r.data["kind"] == "target" and "synthetic" in r.data["notes"] for r in refs)
        rev0 = project.store.list("revision")[0]
        assert Path(rev0.data["file"]).is_file() and rev0.data["note"].startswith("fixture")
        ids = {e["alloy_id"] for e in rev0.data["identity_map"]}
        assert {"p_base", "p_post", "p_housing", "p_bracket", "p_lens", "p_bolt"} <= ids
        stale = [r for r in project.store.list("render") if r.data.get("reason") == "fixture stale evidence"]
        assert stale and render_is_stale(stale[0], current_revision_id=rev0.id, expected_cache_key=None)[0]
        # the floating bracket really floats: measured gap above the housing
        meas = runner.measure(rev0.data["file"], {"bboxes": ["p_housing", "p_bracket"], "distances": [["p_housing", "p_bracket"]],
                                                  "ratios": [], "overlaps": {"ids": ["p_housing", "p_bracket"], "intended_contact": []}},
                              op_id="meas_fixture")
        assert meas.ok, meas.error
        gap = meas.data["distances"][0]["bbox_gap"]
        assert 0.1 < gap < 0.2
        assert meas.data["overlaps"][0]["bbox_intersects"] is False
        housing = meas.data["bboxes"]["p_housing"]["dimensions"]
        assert housing[1] < 0.45  # depth defect (truth is 0.6)
    finally:
        project.close()


def test_end_to_end_slice_on_real_blender(runner, workdir):
    project, info = create_fixture(workdir / "fixture", runner)
    try:
        adapters = _adapters(screenplay())
        eng = Engine(project, _config(), adapters=adapters, runner=runner)
        eng.preflight(live=True)
        eng.start()
        status = eng.run_until_stop(max_steps=60)
        store = project.store
        assert status["stop_reason"] == "ready_for_user_review", status["notes"]
        revs = store.list("revision")
        assert len(revs) == 3
        for rev in revs:
            assert Path(rev.data["file"]).is_file() and store.verify_file(rev.data["file"])[0]

        # real renders with manifests from exact revisions (R-62, R-63)
        renders = [r for r in store.list("render", state="ok") if r.data.get("reason") != "fixture stale evidence"]
        assert renders
        for r in renders:
            m = r.data["manifest"]
            assert m["success"] and Path(r.data["file"]).stat().st_size > 0
            assert m["renderer"]["engine"] in ("BLENDER_WORKBENCH", "BLENDER_EEVEE")
            assert m["renderer"]["blender_version"].startswith("5.")
            assert store.verify_file(r.data["file"])[0]
        stale_ids = {r.id for r in store.list("render") if r.data.get("reason") == "fixture stale evidence"}
        review_task = [t for t in store.list("task") if t.data["kind"] == "review"][0]
        assert stale_ids.isdisjoint(set(review_task.data["render_ids"]))

        # the defect was really corrected: measurement in the verification packet shows the bracket touching the housing
        f = store.list("finding")[0]
        assert f.state == "closed" and f.data["resolution"]["verdict"] == "improved"
        after_rev = store.get("revision", store.get("render", f.data["after_render_ids"][0]).data["revision_id"])
        meas = runner.measure(after_rev.data["file"], {"bboxes": ["p_housing", "p_bracket"],
                                                       "distances": [["p_housing", "p_bracket"]], "ratios": [],
                                                       "overlaps": {"ids": ["p_housing", "p_bracket"], "intended_contact": [["p_housing", "p_bracket"]]}},
                              op_id="meas_after")
        assert meas.ok and meas.data["distances"][0]["bbox_gap"] < 1e-4
        assert meas.data["overlaps"][0]["intended_contact"] is True
        housing = runner.measure(after_rev.data["file"], {"bboxes": ["p_housing"]}, op_id="meas_housing").data["bboxes"]["p_housing"]
        assert abs(housing["dimensions"][1] - 0.6) < 1e-3  # A's build restored the depth

        # both agents built; handoff recorded; identities intact in the final revision
        assert status["contributions"]["A"] >= 1 and status["contributions"]["B"] >= 1
        assert store.list("handoff")
        ids = runner.identities(revs[-1].data["file"], op_id="ids_final").data["identity_map"]
        assert {e["alloy_id"] for e in ids} >= {e["alloy_id"] for e in revs[0].data["identity_map"]}

        comp_id = status["components"][0]["id"]
        acc = eng.accept_component(comp_id, user="user:test")
        assert acc.data["revision_id"] == revs[-1].id
        eng.reopen_component(comp_id, reason="check the lens", user="user:test")
        assert store.get("component", comp_id).state == "unreviewed"
        ck = store.list("checkpoint")[0]
        restored = eng.restore_checkpoint(ck.id, user="user:test")
        assert restored.data["parent_revision_id"] == ck.data["revision_id"]
        reopened = runner.identities(restored.data["file"], op_id="ids_restored")
        assert reopened.ok
        assert store.verify_rebuild()
    finally:
        project.close()


def test_cancel_during_apply_kills_blender_and_leaves_no_inflight_operation(runner, workdir):
    project, info = create_fixture(workdir / "fixture", runner)
    try:
        play = screenplay(housing_script="import time\nfor i in range(120):\n    time.sleep(0.5)\n")
        adapters = _adapters(play)
        eng = Engine(project, _config(), adapters=adapters, runner=runner)
        eng.preflight(live=True)
        eng.start()

        def cancel_soon():
            deadline = time.time() + 60
            while time.time() < deadline:
                if any(o.state == "running" for o in project.store.list("operation")):
                    time.sleep(1.5)
                    eng.cancel_event.set()
                    return
                time.sleep(0.2)

        threading.Thread(target=cancel_soon, daemon=True).start()
        t0 = time.time()
        status = eng.run_until_stop(max_steps=60)
        assert time.time() - t0 < 90
        ops = project.store.list("operation")
        assert ops and ops[-1].state == "cancelled"
        assert ops[-1].data["apply"]["kill_confirmed"] is True
        assert Path(ops[-1].data["quarantine_dir"]).is_dir()
        assert status["execution"] == "paused" and status["stop_reason"] == "execution_failure"
        assert eng.recover()["uncertain"] == 0
        assert len(project.store.list("revision")) == 1
    finally:
        project.close()
