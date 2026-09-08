"""R-61 per-part close-up views and R-65 adequate-resolution crops in review and verification packets (layer D)."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from PIL import Image

from builder.render import (
    CLOSEUP_PREFIX,
    closeup_view_def,
    frame_camera,
    part_of_closeup,
    project_bbox,
    project_point,
)


def _cam(direction="front", center=(0.0, 0.0, 1.1), size=(1.0, 1.0, 1.0)):
    return frame_camera(list(center), list(size), direction)


def test_project_point_maps_the_framed_center_to_the_image_center():
    cam = _cam()
    px, py, depth = project_point(cam, [0.0, 0.0, 1.1], (640, 480))
    assert abs(px - 320) < 1e-6 and abs(py - 240) < 1e-6 and depth > 0
    # +X is to the right and +Z is up in a front view (camera on -Y looking +Y)
    rx, ry, _ = project_point(cam, [0.3, 0.0, 1.1], (640, 480))
    assert rx > 320 and abs(ry - 240) < 1e-6
    ux, uy, _ = project_point(cam, [0.0, 0.0, 1.4], (640, 480))
    assert abs(ux - 320) < 1e-6 and uy < 240
    # a point behind the camera is not projected
    assert project_point(cam, [0.0, cam["location"][1] - 1.0, 1.1], (640, 480)) is None


def test_projection_agrees_with_frame_camera_for_every_direction():
    center, size = [0.2, -0.1, 1.8], [0.9, 0.4, 0.5]
    for direction in ("front", "side", "three_quarter", "top", "rear", "underside", "three_quarter_rear", "side_left"):
        cam = frame_camera(center, size, direction)
        rect = project_bbox(cam, {"min": [center[i] - size[i] / 2 for i in range(3)],
                                  "max": [center[i] + size[i] / 2 for i in range(3)]}, (640, 480), pad=0.0)
        assert rect is not None and not rect["clipped"], direction
        assert 0 <= rect["x"] and rect["x"] + rect["w"] <= 640 and 0 <= rect["y"] and rect["y"] + rect["h"] <= 480
        assert 0.05 < rect["fraction"] < 1.0


def test_project_bbox_pads_clamps_and_reports_fraction():
    cam = _cam(size=(4.0, 4.0, 4.0))
    small = {"min": [-0.2, -0.2, 0.9], "max": [0.2, 0.2, 1.3]}
    rect = project_bbox(cam, small, (640, 480), pad=0.5)
    assert rect["fraction"] < 0.2 and rect["w"] > 0 and rect["h"] > 0
    unpadded = project_bbox(cam, small, (640, 480), pad=0.0)
    assert rect["w"] > unpadded["w"] and rect["h"] > unpadded["h"]
    huge = {"min": [-50, -50, -50], "max": [50, 50, 50]}
    r2 = project_bbox(cam, huge, (640, 480))
    assert r2 is None or r2["clipped"]
    assert project_bbox(cam, {"min": [0, 0, 0], "max": [0, 0, 0], "empty": True}, (640, 480)) is None


def test_closeup_view_def_names_the_part_and_uses_a_tighter_frame():
    vd = closeup_view_def("p_bolt.1")
    assert vd["name"] == f"{CLOSEUP_PREFIX}p_bolt.1" and vd["subject"] == "part" and vd["part_id"] == "p_bolt.1"
    assert vd["mode"] == "clay" and vd["direction"] == "three_quarter"
    assert part_of_closeup(vd["name"]) == "p_bolt.1" and part_of_closeup("front") is None


# ------------------------------------------------------------------ engine integration (fake runner)

from builder.config import BuilderConfig  # noqa: E402
from builder.engine import Engine  # noqa: E402
from builder.operations import Operations  # noqa: E402
from builder.ownership import OwnershipManager  # noqa: E402
from builder.project import Project  # noqa: E402
from builder.providers.mock import ScriptedAdapter  # noqa: E402
from builder.records import Record  # noqa: E402
from builder.references import References  # noqa: E402

from _fake_runner import FakeRunner  # noqa: E402
from _screenplay import screenplay  # noqa: E402

PARTS = ["p_base", "p_post", "p_housing", "p_bracket", "p_lens", "p_bolt", "p_bolt.1", "p_bolt.2", "p_bolt.3"]
BBOXES = {
    "p_base": {"min": [-1, -1, 0], "max": [1, 1, 0.2]}, "p_post": {"min": [-0.15, -0.15, 0.2], "max": [0.15, 0.15, 1.7]},
    "p_housing": {"min": [-0.45, -0.18, 1.7], "max": [0.45, 0.18, 2.2]},
    "p_bracket": {"min": [-0.15, -0.15, 2.35], "max": [0.15, 0.15, 2.45]},
    "p_lens": {"min": [-0.2, -0.53, 1.75], "max": [0.2, -0.13, 2.15]},
    "p_bolt": {"min": [0.75, 0.75, 0.2], "max": [0.85, 0.85, 0.3]}, "p_bolt.1": {"min": [-0.85, 0.75, 0.2], "max": [-0.75, 0.85, 0.3]},
    "p_bolt.2": {"min": [-0.85, -0.85, 0.2], "max": [-0.75, -0.75, 0.3]}, "p_bolt.3": {"min": [0.75, -0.85, 0.2], "max": [0.85, -0.75, 0.3]},
}
for _b in BBOXES.values():
    _b["center"] = [(_b["min"][i] + _b["max"][i]) / 2 for i in range(3)]
    _b["dimensions"] = [_b["max"][i] - _b["min"][i] for i in range(3)]
    _b["empty"] = False


@pytest.fixture
def project(workdir):
    prj = Project.create(workdir / "wf", name="Lamp", asset_name="Lamp", extra={"first_component": "Head"})
    refs = References(prj)
    for label in ("front", "side", "three_quarter"):
        p = workdir / f"{label}.png"
        Image.new("RGB", (640, 480), (90, 90, 90)).save(p)
        refs.add(p, labels=[label], kind="target")
    seed = workdir / "seed.blend"
    seed.write_bytes(b"BLENDER-fake-seed")
    Operations(prj, None, OwnershipManager(prj.store)).register_revision(
        seed, parent_revision_id=None, created_by_op_id=None, actor="engine",
        identity_map=[{"alloy_id": p, "type": "OBJECT"} for p in PARTS], note="seed")
    for p in PARTS:
        prj.store.upsert(Record.new("part", {"name": p, "blender_ids": [p]}, id=p), actor="engine", event="part.created")
    prj.store.upsert(Record.new("component", {"name": "Head", "part_ids": ["p_housing", "p_bracket", "p_lens"]}),
                     actor="engine", event="component.created")
    yield prj
    prj.close()


def _engine(project):
    cfg = BuilderConfig.from_dict({"builder": {"limits": {"attempts_per_finding": 2}}})
    cfg.agents["A"].provider = cfg.agents["B"].provider = "mock"
    runner = FakeRunner()
    runner.identity_map = [{"alloy_id": p, "type": "OBJECT"} for p in PARTS]
    runner.measure_data = {"bboxes": BBOXES, "distances": [], "ratios": {}, "overlaps": [], "limitations": ["fake"]}
    play = screenplay()
    adapters = {label: ScriptedAdapter(label, play[label]) for label in ("A", "B")}
    return Engine(project, cfg, adapters=adapters, runner=runner), adapters, runner


def _manifest(req) -> dict:
    return json.loads((Path(req.packet_dir) / "packet.json").read_text(encoding="utf-8"))


def test_review_and_verification_packets_carry_closeups_and_crops(project):
    eng, adapters, runner = _engine(project)
    eng.preflight(live=True)
    eng.start()
    status = eng.run_until_stop(max_steps=60)
    assert status["stop_reason"] == "ready_for_user_review", status["notes"]
    store = project.store
    views = {v.data["name"]: v for v in store.list("view")}
    for pid in ("p_housing", "p_bracket", "p_lens"):
        name = f"{CLOSEUP_PREFIX}{pid}"
        assert name in views and views[name].data["part_id"] == pid and views[name].data["subject"] == "part"
        framing = views[name].data["spec"]["camera"]["framing"]
        assert framing["center"] == BBOXES[pid]["center"]
        assert framing["distance"] < views["three_quarter"].data["spec"]["camera"]["framing"]["distance"]
    # closeups of whole-model parts outside the component are not rendered (bounded evidence)
    assert f"{CLOSEUP_PREFIX}p_bolt" not in views

    review = next(i for i in adapters["B"].invocations if i.purpose == "review_task")
    files = _manifest(review)["files"]
    renders = [f for f in files if f["role"] == "render"]
    assert {f["meta"]["view"] for f in renders} >= {"front", "side", "three_quarter", "three_quarter_material",
                                                   f"{CLOSEUP_PREFIX}p_housing", f"{CLOSEUP_PREFIX}p_bracket", f"{CLOSEUP_PREFIX}p_lens"}
    crops = [f for f in files if f["role"] == "crop" and f["meta"].get("render_id")]
    assert crops, "no render crops in the review packet"
    for c in crops:
        m = c["meta"]
        assert m["space"] == "render_pixels" and m["part_id"] in ("p_housing", "p_bracket", "p_lens")
        assert m["view"] in ("front", "side", "three_quarter") and m["revision_id"].startswith("rev_")
        x, y, w, h = m["bbox"]
        with Image.open(Path(review.packet_dir) / c["path"]) as im:
            assert im.size == (w, h) and w > 0 and h > 0
    assert any(c["meta"]["part_id"] == "p_bracket" and c["meta"]["view"] == "side" for c in crops)
    text = (Path(review.packet_dir) / "PACKET.md").read_text(encoding="utf-8")
    assert "close-up" in text.lower() and "render_pixels" in text

    verify = next(i for i in adapters["A"].invocations if i.purpose == "verification")
    vfiles = _manifest(verify)["files"]
    before = [f for f in vfiles if f["role"] == "render" and f["meta"].get("note") == "BEFORE"]
    after = [f for f in vfiles if f["role"] == "render" and f["meta"].get("note") == "AFTER"]
    assert any(f["meta"]["view"] == f"{CLOSEUP_PREFIX}p_bracket" for f in before)
    assert any(f["meta"]["view"] == f"{CLOSEUP_PREFIX}p_bracket" for f in after)
    vcrops = [f for f in vfiles if f["role"] == "crop" and f["meta"].get("render_id")]
    assert vcrops and all(c["meta"]["part_id"] == "p_bracket" for c in vcrops)
    assert {c["meta"].get("note") for c in vcrops} >= {"BEFORE", "AFTER"}
    # the closeup renders exist as real files with manifests naming their part
    for r in store.list("render", state="ok"):
        if r.data["view_name"].startswith(CLOSEUP_PREFIX):
            assert r.data["manifest"]["view"]["part_id"] == part_of_closeup(r.data["view_name"])
            assert Path(r.data["file"]).is_file()


def test_closeup_view_def_can_align_to_a_named_view():
    """Phase 2b carry-over: a close-up framed in the finding's own view direction (R-61, R-65)."""
    vd = closeup_view_def("p_bracket", direction="side")
    assert vd["name"] == f"{CLOSEUP_PREFIX}p_bracket@side" and vd["direction"] == "side" and vd["part_id"] == "p_bracket"
    assert part_of_closeup(vd["name"]) == "p_bracket"
    with pytest.raises(ValueError):
        closeup_view_def("p_bracket", direction="sideways")


def test_verification_packet_carries_a_closeup_in_the_findings_own_view(project):
    """The floating-bracket finding names the side view: BEFORE and AFTER close-ups aligned to 'side' are rendered for
    the verifier in addition to the standard three-quarter close-up; the review packet does not carry them."""
    eng, adapters, runner = _engine(project)
    eng.preflight(live=True)
    eng.start()
    status = eng.run_until_stop(max_steps=60)
    assert status["stop_reason"] == "ready_for_user_review", status["notes"]
    verify = next(i for i in adapters["A"].invocations if i.purpose == "verification")
    vfiles = _manifest(verify)["files"]
    aligned = f"{CLOSEUP_PREFIX}p_bracket@side"
    before = [f for f in vfiles if f["role"] == "render" and f["meta"].get("note") == "BEFORE" and f["meta"]["view"] == aligned]
    after = [f for f in vfiles if f["role"] == "render" and f["meta"].get("note") == "AFTER" and f["meta"]["view"] == aligned]
    assert len(before) == 1 and len(after) == 1
    assert before[0]["meta"]["revision_id"] != after[0]["meta"]["revision_id"]
    text = (Path(verify.packet_dir) / "PACKET.md").read_text(encoding="utf-8")
    assert "finding's own view" in text
    view = next(v for v in project.store.list("view") if v.data["name"] == aligned)
    assert view.data["spec"]["camera"]["framing"]["direction"] == "side" and view.data["part_id"] == "p_bracket"
    review = next(i for i in adapters["B"].invocations if i.purpose == "review_task")
    assert not any(f["meta"].get("view") == aligned for f in _manifest(review)["files"])
