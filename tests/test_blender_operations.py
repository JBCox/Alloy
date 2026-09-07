"""Section 5 scripts on a real Blender: build_scene, identities, validate, apply, render, measure (layer B).
R-37 identity diff, R-48 missing assets, R-60 to R-65 renders and manifests, R-68 measurement limitations."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from builder.blender.runner import BlenderRunner
from builder.ids import sha256_file
from builder.render import ViewSpec, applied_settings_match, build_manifest, engine_for

pytestmark = pytest.mark.blender


@pytest.fixture
def runner(blender_exe, workdir):
    return BlenderRunner(blender_exe, logs_dir=workdir / "logs", deadlines={"validate": 60, "apply": 120, "render": 180,
                                                                            "measure": 60, "fixture": 180})


BASE_SPEC = {
    "collections": [{"alloy_id": "col_main", "name": "Main"}],
    "objects": [
        {"alloy_id": "p_base", "name": "Base", "primitive": "cube", "size": [2.0, 2.0, 0.2], "location": [0, 0, 0.1],
         "collection": "col_main",
         "material": {"alloy_id": "mat_paint", "name": "Paint", "base_color": [0.1, 0.2, 0.8, 1.0], "roughness": 0.5}},
        {"alloy_id": "p_post", "name": "Post", "primitive": "cylinder", "size": [0.3, 0.3, 1.5], "location": [0, 0, 0.95],
         "collection": "col_main", "parent": "p_base"},
        {"alloy_id": "p_cap", "name": "Cap", "primitive": "sphere", "size": [0.5, 0.5, 0.5], "location": [0, 0, 1.95],
         "collection": "col_main",
         "material": {"alloy_id": "mat_lens", "name": "Lens", "base_color": [0.8, 0.1, 0.1, 1.0], "metallic": 0.0}},
        {"alloy_id": "p_bolt", "name": "Bolt", "primitive": "cylinder", "size": [0.1, 0.1, 0.1], "location": [0.8, 0.8, 0.25],
         "collection": "col_main", "instances": [{"alloy_id": "p_bolt.1", "location": [-0.8, 0.8, 0.25]},
                                                {"alloy_id": "p_bolt.2", "location": [-0.8, -0.8, 0.25]}]},
    ],
    "features": [{"alloy_id": "feat_post_top", "name": "post top", "location": [0, 0, 1.7]}],
}


@pytest.fixture
def base_blend(runner, workdir):
    out = workdir / "revisions" / "rev base ü.blend"
    res = runner.build_scene(out, BASE_SPEC, op_id="op_build")
    assert res.outcome == "ok", res.error
    assert out.is_file()
    return out


def test_identities_enumerates_tagged_datablocks(runner, base_blend):
    res = runner.identities(base_blend, op_id="op_ids")
    assert res.outcome == "ok", res.error
    ids = {e["alloy_id"]: e for e in res.result["data"]["identity_map"]}
    assert {"p_base", "p_post", "p_cap", "p_bolt", "p_bolt.1", "p_bolt.2", "feat_post_top", "col_main",
            "mat_paint", "mat_lens"} <= set(ids)
    assert ids["p_base"]["type"] == "OBJECT" and ids["col_main"]["type"] == "COLLECTION"
    assert ids["mat_lens"]["type"] == "MATERIAL"
    assert ids["p_bolt.1"]["alloy_kind"] == "instance" and ids["p_bolt.1"]["instance_of"] == "p_bolt"
    assert ids["p_post"]["parent_alloy_id"] == "p_base"
    assert ids["feat_post_top"]["alloy_kind"] == "feature"


def test_validate_passes_on_clean_file_and_reports_counts(runner, base_blend):
    ids = runner.identities(base_blend, op_id="op_ids").result["data"]["identity_map"]
    res = runner.validate(base_blend, {"base_identity_map": ids, "expect_present": ["p_cap"]}, op_id="op_val")
    assert res.outcome == "ok", res.error
    d = res.result["data"]
    assert d["deleted"] == [] and d["duplicates"] == [] and d["unmapped"] == [] and d["orphans"] == []
    assert d["nan_transforms"] == [] and d["missing_assets"] == [] and d["missing_present"] == []
    assert d["reopened"] is True and d["object_count"] >= 7


def test_apply_then_validate_detects_deletion_duplicate_unmapped_and_orphan(runner, base_blend, workdir):
    ids = runner.identities(base_blend, op_id="op_ids").result["data"]["identity_map"]
    script = workdir / "ops" / "bad op.py"
    script.parent.mkdir(parents=True)
    script.write_text(textwrap.dedent("""
        import bpy
        cap = ALLOY.get('p_cap'); bpy.data.objects.remove(cap)                # deletion
        post = ALLOY.get('p_post'); dup = post.copy(); dup.data = post.data.copy()
        bpy.context.scene.collection.objects.link(dup)                         # duplicate alloy_id
        bpy.ops.mesh.primitive_cube_add(size=0.3, location=(3, 3, 3))          # unmapped mesh
        orphan = bpy.data.objects.new('Orphan', bpy.data.meshes.new('om'))     # orphan: no collection
        orphan['alloy_id'] = 'p_orphan'; orphan['alloy_kind'] = 'part'
    """), encoding="utf-8")
    out = workdir / "staging" / "op_bad" / "out.blend"
    res = runner.apply(base_blend, out, script, op_id="op_bad", declared_effects={"creates": [], "modifies": ["p_post"],
                                                                                "deletes": []})
    assert res.outcome == "ok", res.error
    # An object linked to no collection has zero users and is dropped by Blender on save, so the
    # apply stage must report it before saving (R-37 "unintentionally orphaned").
    assert "p_orphan" in res.result["data"]["orphans_before_save"]
    assert any("p_orphan" in w for w in res.result["warnings"])
    val = runner.validate(out, {"base_identity_map": ids, "expect_present": ["p_cap"], "expect_absent": []},
                          op_id="op_bad_val")
    assert val.outcome == "failed"
    d = val.result["data"]
    assert "p_cap" in d["deleted"] and "p_cap" in d["missing_present"]
    assert "p_post" in d["duplicates"]
    assert len(d["unmapped"]) == 1
    assert d["orphans"] == []  # the orphan never reached the file; validate sees a consistent file


def test_apply_script_exception_is_reported_and_nothing_saved(runner, base_blend, workdir):
    script = workdir / "ops" / "raise.py"
    script.parent.mkdir(parents=True)
    script.write_text("raise ValueError('agent script exploded')\n", encoding="utf-8")
    out = workdir / "staging" / "op_raise" / "out.blend"
    res = runner.apply(base_blend, out, script, op_id="op_raise", declared_effects={})
    assert res.outcome == "failed" and "agent script exploded" in res.error
    assert not out.exists()


def test_validate_reports_missing_external_asset(runner, base_blend, workdir):
    script = workdir / "ops" / "img.py"
    script.parent.mkdir(parents=True)
    script.write_text(textwrap.dedent(r"""
        import bpy
        img = bpy.data.images.new('tex', 4, 4)
        img.filepath = r'C:\definitely\missing\texture ü.png'
        img.source = 'FILE'
        mat = ALLOY.get('mat_paint')
        node = mat.node_tree.nodes.new('ShaderNodeTexImage')   # gives the image a user so it is saved
        node.image = img
    """), encoding="utf-8")
    out = workdir / "staging" / "op_img" / "out.blend"
    res = runner.apply(base_blend, out, script, op_id="op_img", declared_effects={})
    assert res.outcome == "ok", res.error
    val = runner.validate(out, {}, op_id="op_img_val")
    assert val.outcome == "failed"
    assert any("texture ü.png" in m["path"] for m in val.result["data"]["missing_assets"])


@pytest.mark.parametrize("mode", ["clay", "material"])
def test_render_writes_png_and_reports_applied_settings(runner, base_blend, workdir, mode):
    view = ViewSpec(name="three_quarter", mode=mode,
                    camera={"location": [4, -4, 3], "rotation_euler": [1.1, 0, 0.785], "lens": 50, "type": "PERSP"},
                    resolution=(256, 192), visibility={"hide": ["p_bolt.2"]})
    out = workdir / "renders" / f"rnd {mode} ü.png"
    res = runner.render(base_blend, view.to_dict(), out, op_id=f"op_rnd_{mode}")
    assert res.outcome == "ok", res.error
    assert out.is_file() and out.stat().st_size > 0
    applied = res.result["data"]["applied"]
    assert applied["engine"] == engine_for(mode)
    assert applied["resolution"] == [256, 192]
    assert applied["view_transform"] == "Standard"
    assert "p_bolt.2" in applied["hidden_ids"]
    assert applied_settings_match(view.to_dict(), applied) == []
    manifest = build_manifest(revision_id="rev_1", revision_sha256=sha256_file(base_blend), asset_dependencies={},
                              view=view.to_dict(), view_version=1, applied=applied, output_path=out,
                              output_sha256=sha256_file(out), success=True, error="", elapsed_s=res.elapsed_s,
                              render_script_sha256="abc", cache_key="k")
    for key in ("source", "camera", "visibility", "materials_lighting", "renderer", "output", "success"):
        assert key in manifest
    assert manifest["renderer"]["blender_version"].startswith("5.")


def test_render_isolate_hides_everything_else(runner, base_blend, workdir):
    view = ViewSpec(name="cap_only", mode="clay", camera={"location": [3, -3, 3], "rotation_euler": [1.0, 0, 0.785]},
                    resolution=(64, 64), visibility={"isolate": ["p_cap"]})
    res = runner.render(base_blend, view.to_dict(), workdir / "iso.png", op_id="op_iso")
    assert res.outcome == "ok", res.error
    hidden = set(res.result["data"]["applied"]["hidden_ids"])
    assert "p_base" in hidden and "p_post" in hidden and "p_cap" not in hidden


def test_measure_reports_bboxes_distances_and_overlap_candidates_with_limitations(runner, base_blend):
    req = {"bboxes": ["p_base", "p_post", "p_cap"],
           "distances": [["feat_post_top", "p_cap"], ["p_base", "p_cap"]],
           "ratios": [{"name": "post_height_to_base_width", "numerator": ["p_post", "z"], "denominator": ["p_base", "x"]}],
           "overlaps": {"ids": ["p_base", "p_post", "p_cap", "p_bolt"], "intended_contact": [["p_base", "p_post"]]}}
    res = runner.measure(base_blend, req, op_id="op_meas")
    assert res.outcome == "ok", res.error
    d = res.result["data"]
    assert abs(d["bboxes"]["p_base"]["dimensions"][0] - 2.0) < 1e-3
    assert abs(d["bboxes"]["p_post"]["dimensions"][2] - 1.5) < 1e-3
    assert abs(d["ratios"]["post_height_to_base_width"]["value"] - 0.75) < 1e-3
    dist = {tuple(x["between"]): x for x in d["distances"]}
    assert abs(dist[("feat_post_top", "p_cap")]["center_distance"] - 0.25) < 1e-3
    assert dist[("p_base", "p_cap")]["bbox_gap"] > 1.0
    pairs = {tuple(o["pair"]): o for o in d["overlaps"]}
    assert pairs[("p_base", "p_post")]["intended_contact"] is True
    assert pairs[("p_base", "p_post")]["bbox_intersects"] is True
    assert pairs[("p_base", "p_cap")]["bbox_intersects"] is False
    assert any("bounding box" in lim.lower() for lim in d["limitations"])
