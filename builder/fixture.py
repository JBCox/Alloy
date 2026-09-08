"""The disposable fixture project (spec Section 5): a small multi-part object built twice from primitives.

The *truth* model's renders become the fixture's reference images (synthetic by construction, labelled
as such). The *defective* model becomes revision 0 with controlled defects:

1. ``housing_depth_scaled_0.6``      the housing is 0.36 deep instead of 0.6 (incorrect depth)
2. ``bracket_floating_0.15``          the bracket fitting floats 0.15 above the housing top (floating fitting)
3. ``lens_material_red_not_blue``     the lens material is red instead of the reference blue (material mismatch)
4. ``stale_render_evidence``          a render record whose file no longer matches its manifest (stale evidence)

Never the Bearer files. Generated into any directory the caller chooses.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .ids import new_id, utc_now
from .operations import Operations
from .ownership import OwnershipManager
from .project import Project
from .records import Record
from .references import References
from .render import STANDARD_VIEW_DEFS, ViewSpec, bbox_union, build_manifest, frame_camera

FIXTURE_DEFECTS = ("housing_depth_scaled_0.6", "bracket_floating_0.15", "lens_material_red_not_blue",
                   "stale_render_evidence")
HEAD_PARTS = ("p_housing", "p_bracket", "p_lens")


def fixture_spec(defective: bool) -> dict[str, Any]:
    housing_depth = 0.36 if defective else 0.6
    bracket_z = 2.25 + (0.15 if defective else 0.0)          # housing top is at z = 2.2; bracket is 0.1 tall
    lens_color = [0.85, 0.1, 0.1, 1.0] if defective else [0.1, 0.25, 0.9, 1.0]
    return {
        "collections": [{"alloy_id": "col_main", "name": "Main"}],
        "objects": [
            {"alloy_id": "p_base", "name": "Base plate", "primitive": "cube", "size": [2.0, 2.0, 0.2], "location": [0, 0, 0.1],
             "collection": "col_main",
             "material": {"alloy_id": "mat_paint", "name": "Paint", "base_color": [0.35, 0.35, 0.38, 1.0], "roughness": 0.6}},
            {"alloy_id": "p_post", "name": "Post", "primitive": "cylinder", "size": [0.3, 0.3, 1.5], "location": [0, 0, 0.95],
             "collection": "col_main", "parent": "p_base",
             "material": {"alloy_id": "mat_metal", "name": "Metal", "base_color": [0.6, 0.6, 0.62, 1.0], "metallic": 1.0,
                          "roughness": 0.35}},
            {"alloy_id": "p_housing", "name": "Housing", "primitive": "cube", "size": [0.9, housing_depth, 0.5],
             "location": [0, 0, 1.95], "collection": "col_main", "material": {"alloy_id": "mat_paint"}},
            {"alloy_id": "p_bracket", "name": "Bracket fitting", "primitive": "cube", "size": [0.3, 0.3, 0.1],
             "location": [0, 0, bracket_z], "collection": "col_main", "material": {"alloy_id": "mat_metal"}},
            {"alloy_id": "p_lens", "name": "Lens", "primitive": "sphere", "size": [0.4, 0.4, 0.4],
             "location": [0, -(housing_depth / 2) - 0.15, 1.95], "collection": "col_main",
             "material": {"alloy_id": "mat_lens", "name": "Lens", "base_color": lens_color, "metallic": 0.0, "roughness": 0.1}},
            {"alloy_id": "p_bolt", "name": "Bolt", "primitive": "cylinder", "size": [0.1, 0.1, 0.1], "location": [0.8, 0.8, 0.25],
             "collection": "col_main", "material": {"alloy_id": "mat_metal"},
             "instances": [{"alloy_id": "p_bolt.1", "location": [-0.8, 0.8, 0.25]},
                           {"alloy_id": "p_bolt.2", "location": [-0.8, -0.8, 0.25]},
                           {"alloy_id": "p_bolt.3", "location": [0.8, -0.8, 0.25]}]},
        ],
        "features": [{"alloy_id": "feat_housing_top", "name": "housing top centre", "location": [0, 0, 2.2]},
                     {"alloy_id": "feat_bracket_bottom", "name": "bracket bottom centre", "location": [0, 0, bracket_z - 0.05]}],
    }


def create_fixture(directory: str | Path, runner: Any, *, workflow_dir: str | Path | None = None,
                   name: str = "Fixture lamp") -> tuple[Project, dict[str, Any]]:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    truth = d / "truth" / "truth.blend"
    res = runner.build_scene(truth, fixture_spec(False), op_id="fixture_truth", work_dir=d / "logs" / "truth")
    if not res.ok:
        raise RuntimeError(f"fixture truth build failed: {res.error}")
    defective = d / "defective" / "defective.blend"
    res2 = runner.build_scene(defective, fixture_spec(True), op_id="fixture_defective", work_dir=d / "logs" / "defective")
    if not res2.ok:
        raise RuntimeError(f"fixture defective build failed: {res2.error}")
    refs_dir = d / "references"
    reference_files: dict[str, Path] = {}
    # Frame the reference cameras from the TRUTH model's measured bounding boxes with the same helper the engine
    # uses, so fixture references and model views match by construction (R-60 for the fixture only).
    meas = runner.measure(truth, {"bboxes": "all"}, op_id="fixture_truth_bboxes", work_dir=d / "logs" / "truth_bboxes")
    if not meas.ok:
        raise RuntimeError(f"fixture truth measurement failed: {meas.error}")
    boxes = meas.data.get("bboxes") or {}
    framing = {"component": bbox_union([boxes[p] for p in HEAD_PARTS if p in boxes]),
               "assembly": bbox_union([b for b in boxes.values() if not b.get("empty")])}
    for spec in STANDARD_VIEW_DEFS:
        center, size = framing[spec["subject"]]
        view = ViewSpec(name=spec["name"], mode=spec["mode"], camera=frame_camera(center, size, spec["direction"]),
                        resolution=(640, 480))
        out = refs_dir / f"{spec['name']}.png"
        rr = runner.render(truth, view.to_dict(), out, op_id=f"fixture_ref_{spec['name']}", work_dir=d / "logs" / f"ref_{spec['name']}")
        if not rr.ok:
            raise RuntimeError(f"fixture reference render {spec['name']} failed: {rr.error}")
        reference_files[spec["name"]] = out

    project = Project.create(workflow_dir or d / "workflow", name=name, asset_name=name,
                             extra={"first_component": "Head", "fixture": True, "fixture_defects": list(FIXTURE_DEFECTS)})
    refs = References(project)
    for view_name, path in reference_files.items():
        refs.add(path, labels=[view_name], kind="target",
                 notes=f"synthetic fixture reference rendered from the truth model ({view_name} view); not a real concept image")
    identity_map = res2.data.get("identity_map") or []
    ops = Operations(project, runner, OwnershipManager(project.store))
    rev0 = ops.register_revision(defective, parent_revision_id=None, created_by_op_id=None, actor="engine",
                                 identity_map=identity_map,
                                 note="fixture revision 0 with controlled defects: " + ", ".join(FIXTURE_DEFECTS[:3]))
    for e in identity_map:
        if e.get("type") == "OBJECT" and e.get("alloy_kind") in ("part", "instance"):
            rec = Record.new("part", {"name": e.get("name"), "blender_ids": [e["alloy_id"]], "alloy_kind": e.get("alloy_kind"),
                                      "instance_of": e.get("instance_of")}, id=e["alloy_id"],
                             parent_id=e.get("parent_alloy_id"))
            project.store.upsert(rec, actor="engine", event="part.created")
    for a, b in (("p_bracket", "p_housing"), ("p_post", "p_base"), ("p_housing", "p_post"), ("p_lens", "p_housing")):
        project.store.upsert(Record.new("relation", {"from_part": a, "to_part": b, "type": "attached_to", "evidence": "fixture"}),
                             actor="engine", event="relation.recorded")
    comp = Record.new("component", {"name": "Head", "part_ids": list(HEAD_PARTS), "created_at": utc_now()})
    project.store.upsert(comp, actor="engine", event="component.created")

    # Stale evidence: a render record whose file no longer matches its manifest (R-64 must catch it).
    stale_id = new_id("rnd")
    stale_file = project.path("renders", f"{stale_id}.png")
    shutil.copyfile(reference_files["front"], stale_file)
    manifest = build_manifest(revision_id=rev0.id, revision_sha256=rev0.data["sha256"], asset_dependencies={},
                              view={"name": "front", "mode": "clay"}, view_version=1,
                              applied={"engine": "BLENDER_WORKBENCH", "blender_version": "unknown"},
                              output_path=stale_file, output_sha256="0" * 64, success=True, error="", elapsed_s=0.0,
                              render_script_sha256="fixture", cache_key="fixture-stale", view_id="view_front")
    stale = Record.new("render", {"view_id": "view_front", "view_name": "front", "revision_id": rev0.id, "component_id": comp.id,
                                  "file": str(stale_file), "manifest": manifest, "cache_key": "fixture-stale", "mode": "clay",
                                  "reason": "fixture stale evidence"}, id=stale_id, state="ok")
    project.store.upsert(stale, actor="engine", event="render.created", inputs={"note": "fixture stale evidence"})
    info = {"defects": list(FIXTURE_DEFECTS), "truth": str(truth), "defective": str(defective),
            "references": {k: str(v) for k, v in reference_files.items()}, "revision_id": rev0.id,
            "workflow_dir": str(project.workflow_dir), "stale_render_id": stale_id}
    project.update(actor="engine", event="project.fixture", fixture_info=info)
    return project, info
