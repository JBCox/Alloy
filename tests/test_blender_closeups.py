"""R-61/R-65 on a real Blender: a close-up view framed on one small part fills the frame far more than the
component view does, and the projected crop rectangle in the overview render really contains the part."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from builder.blender.runner import BlenderRunner
from builder.fixture import HEAD_PARTS, create_fixture
from builder.render import CLOSEUP_MARGIN, ViewSpec, bbox_union, closeup_view_def, frame_camera, project_bbox

pytestmark = pytest.mark.blender


def _distinct_colors(im: Image.Image) -> int:
    return len(set(im.convert("RGB").getdata()))


def test_closeup_render_and_overview_crop_of_the_bracket(blender_exe, workdir):
    runner = BlenderRunner(blender_exe, logs_dir=workdir / "logs")
    project, info = create_fixture(workdir / "fixture", runner)
    try:
        rev0 = project.store.list("revision")[0]
        meas = runner.measure(rev0.data["file"], {"bboxes": "all"}, op_id="bb")
        assert meas.ok, meas.error
        boxes = meas.data["bboxes"]
        res = (640, 480)

        # component (head) three-quarter view vs. close-up on the bracket
        center, size = bbox_union([boxes[p] for p in HEAD_PARTS])
        comp_cam = frame_camera(center, size, "three_quarter")
        vd = closeup_view_def("p_bracket")
        c_center, c_size = bbox_union([boxes["p_bracket"]])
        close_cam = frame_camera(c_center, c_size, vd["direction"], margin=CLOSEUP_MARGIN)
        frac_comp = project_bbox(comp_cam, boxes["p_bracket"], res, pad=0.0)["fraction"]
        frac_close = project_bbox(close_cam, boxes["p_bracket"], res, pad=0.0)["fraction"]
        assert frac_close > 4 * frac_comp, (frac_comp, frac_close)

        out = workdir / "closeup ü.png"
        rr = runner.render(rev0.data["file"], ViewSpec(name=vd["name"], mode="clay", camera=close_cam, resolution=res).to_dict(),
                           out, op_id="closeup")
        assert rr.ok, rr.error
        with Image.open(out) as im:
            assert im.size == res and _distinct_colors(im) > 20

        # the crop rectangle from the front overview render contains shaded geometry, not flat background
        front_cam = frame_camera(center, size, "front")
        front = workdir / "front.png"
        rf = runner.render(rev0.data["file"], ViewSpec(name="front", mode="clay", camera=front_cam, resolution=res).to_dict(),
                           front, op_id="front")
        assert rf.ok, rf.error
        rect = project_bbox(front_cam, boxes["p_bracket"], res)
        assert rect is not None and not rect["clipped"]
        with Image.open(front) as im:
            crop = im.crop((rect["x"], rect["y"], rect["x"] + rect["w"], rect["y"] + rect["h"]))
            outside = im.crop((0, 0, 40, 40))     # top-left corner: background only in the front framing
            assert _distinct_colors(crop) > _distinct_colors(outside)
            assert crop.size[0] < res[0] // 2 and crop.size[1] < res[1] // 2
        assert Path(rr.stdout_path).is_file()
    finally:
        project.close()
