"""R-63 manifests and cache keys cover every input; R-64 stale, missing, mismatched renders block review; R-81 reuse."""
from __future__ import annotations

from pathlib import Path

import pytest

from builder.records import Record
from builder.render import (
    ViewSpec,
    applied_settings_match,
    build_manifest,
    engine_for,
    render_cache_key,
    render_is_stale,
    renders_fresh,
)
from builder.store import Store


def _key(**over):
    base = dict(revision_sha256="r" * 64, asset_dependency_hashes={"tex.png": "t" * 64},
                view={"name": "front", "mode": "clay", "camera": {"location": [0, -5, 1]}, "resolution": [640, 480],
                      "visibility": {"hide": []}, "lighting": "neutral_studio",
                      "color_management": {"view_transform": "Standard"}, "samples": 16, "seed": 0},
                view_version=1, blender_version="5.2.1 LTS", render_script_sha256="s" * 64)
    base.update(over)
    return render_cache_key(**base)


def test_cache_key_changes_with_every_input():
    k0 = _key()
    assert k0 == _key()
    assert k0 != _key(revision_sha256="x" * 64)
    assert k0 != _key(asset_dependency_hashes={"tex.png": "u" * 64})
    assert k0 != _key(view_version=2)
    assert k0 != _key(blender_version="5.3.0")
    assert k0 != _key(render_script_sha256="z" * 64)
    view = _key.__defaults__ if False else None  # noqa: F841 - readability
    for change in ({"mode": "material"}, {"camera": {"location": [0, -6, 1]}}, {"resolution": [64, 64]},
                   {"visibility": {"hide": ["p_1"]}}, {"lighting": "reference_match"},
                   {"color_management": {"view_transform": "AgX"}}, {"samples": 32}, {"seed": 7}):
        v = {"name": "front", "mode": "clay", "camera": {"location": [0, -5, 1]}, "resolution": [640, 480],
             "visibility": {"hide": []}, "lighting": "neutral_studio",
             "color_management": {"view_transform": "Standard"}, "samples": 16, "seed": 0}
        v.update(change)
        assert k0 != _key(view=v), change


def test_engine_for_mode():
    assert engine_for("clay") == "BLENDER_WORKBENCH" and engine_for("material") == "BLENDER_EEVEE"
    with pytest.raises(ValueError):
        engine_for("photoreal")


def test_viewspec_round_trip_and_defaults():
    v = ViewSpec(name="front", mode="clay", camera={"location": [0, -5, 1]})
    d = v.to_dict()
    assert d["engine"] == "BLENDER_WORKBENCH" and d["resolution"] == [1024, 768]
    assert d["color_management"]["view_transform"] == "Standard" and d["evidence_label"] == "matched"
    assert ViewSpec.from_dict(d).to_dict() == d


def test_applied_settings_mismatch_is_reported():
    v = ViewSpec(name="front", mode="clay", camera={"location": [0, -5, 1]}, resolution=(640, 480)).to_dict()
    ok = {"engine": "BLENDER_WORKBENCH", "resolution": [640, 480], "view_transform": "Standard"}
    assert applied_settings_match(v, ok) == []
    bad = {"engine": "BLENDER_EEVEE", "resolution": [640, 400], "view_transform": "AgX"}
    problems = applied_settings_match(v, bad)
    assert len(problems) == 3


@pytest.fixture
def store(workdir):
    s = Store(workdir / "b.sqlite3").open()
    yield s
    s.close()


def _render_record(store, workdir, *, revision_id="rev_1", cache_key="k1", content=b"png", status="ok"):
    f = workdir / f"{revision_id}_{cache_key}.png"
    f.write_bytes(content)
    from builder.ids import sha256_file

    manifest = build_manifest(revision_id=revision_id, revision_sha256="r" * 64, asset_dependencies={},
                              view={"name": "front"}, view_version=1, applied={"engine": "BLENDER_WORKBENCH"},
                              output_path=f, output_sha256=sha256_file(f), success=status == "ok", error="",
                              elapsed_s=1.0, render_script_sha256="s", cache_key=cache_key)
    rec = Record.new("render", {"view_id": "view_front", "revision_id": revision_id, "file": str(f),
                                "manifest": manifest, "cache_key": cache_key}, state=status)
    return store.upsert(rec, actor="engine", event="render.created"), f


def test_render_is_stale_for_each_failure_mode(store, workdir):
    rec, f = _render_record(store, workdir)
    assert render_is_stale(rec, current_revision_id="rev_1", expected_cache_key="k1") == (False, None)
    assert render_is_stale(rec, current_revision_id="rev_2", expected_cache_key="k1")[1] == "revision_mismatch"
    assert render_is_stale(rec, current_revision_id="rev_1", expected_cache_key="k2")[1] == "inputs_changed"
    f.write_bytes(b"tampered")
    assert render_is_stale(rec, current_revision_id="rev_1", expected_cache_key="k1")[1] == "hash_mismatch"
    f.unlink()
    assert render_is_stale(rec, current_revision_id="rev_1", expected_cache_key="k1")[1] == "missing_file"
    failed, _ = _render_record(store, workdir, cache_key="k9", status="failed")
    assert render_is_stale(failed, current_revision_id="rev_1", expected_cache_key="k9")[1] == "render_failed"


def test_renders_fresh_requires_every_required_view(store, workdir):
    rec, _ = _render_record(store, workdir)
    fresh, reasons = renders_fresh([rec], required_view_ids=["view_front", "view_side"],
                                   current_revision_id="rev_1", expected_keys={"view_front": "k1", "view_side": "k2"})
    assert fresh is False and any("view_side" in r for r in reasons)
    fresh, reasons = renders_fresh([rec], required_view_ids=["view_front"], current_revision_id="rev_1",
                                   expected_keys={"view_front": "k1"})
    assert fresh is True and reasons == []


# --- R-60 framing: cameras derive from the measured bounding box, not from fixed presets -----------------

def test_look_at_euler_reproduces_axis_aligned_front_and_side_cameras():
    import math

    from builder.render import look_at_euler

    rx, ry, rz = look_at_euler([0.0, -6.0, 1.1], [0.0, 0.0, 1.1])
    assert abs(rx - math.pi / 2) < 1e-9 and abs(ry) < 1e-9 and abs(rz) < 1e-9
    rx, ry, rz = look_at_euler([6.0, 0.0, 1.1], [0.0, 0.0, 1.1])
    assert abs(rx - math.pi / 2) < 1e-9 and abs(rz - math.pi / 2) < 1e-9
    rx, ry, rz = look_at_euler([0.0, 0.0, 10.0], [0.0, 0.0, 0.0])  # straight down
    assert abs(rx) < 1e-9


def test_frame_camera_fits_the_bounding_sphere_and_records_framing():
    from builder.render import bbox_union, frame_camera, framing_contains

    center, size = bbox_union([{"min": [-1, -1, 0], "max": [1, 1, 0.2]}, {"min": [-0.45, -0.3, 1.7], "max": [0.45, 0.3, 2.5]}])
    assert center == [0.0, 0.0, 1.25] and size == [2.0, 2.0, 2.5]
    cam = frame_camera(center, size, "front")
    assert cam["location"][0] == 0.0 and cam["location"][1] < -2.0 and cam["location"][2] == 1.25
    assert cam["lens"] == 50 and cam["framing"]["center"] == center and cam["framing"]["size"] == size
    assert cam["framing"]["fit_radius"] > 0 and cam["framing"]["distance"] > cam["framing"]["fit_radius"]
    near = frame_camera([0, 0, 1.0], [1.0, 1.0, 1.0], "front")
    assert near["framing"]["distance"] < cam["framing"]["distance"]
    assert framing_contains(cam["framing"], center, size)
    assert framing_contains(cam["framing"], center, [s * 0.5 for s in size])
    assert not framing_contains(cam["framing"], center, [s * 3 for s in size])
    assert not framing_contains(cam["framing"], [c + 5 for c in center], size)
    three_quarter = frame_camera(center, size, "three_quarter")
    assert three_quarter["location"][0] > 0 and three_quarter["location"][1] < 0 and three_quarter["location"][2] > center[2]
    with pytest.raises(ValueError):
        frame_camera(center, size, "sideways")
