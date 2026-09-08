"""Measurements with stated limitations (spec R-68): world bounding boxes, distances, ratios, overlap candidates."""
import json
import sys

_argv = sys.argv[sys.argv.index("--") + 1:]
with open(_argv[0], "r", encoding="utf-8") as _f:
    sys.path.insert(0, json.load(_f)["_scripts_dir"])
import _alloy_common as C  # noqa: E402

LIMITATIONS = [
    "Bounding boxes are axis-aligned in world space on evaluated geometry; rotated parts are overestimated.",
    "A bounding box intersection is a candidate only, not conclusive collision evidence (R-68).",
    "Distances are between bounding-box centers and between box faces (gap), not surface-to-surface.",
    "Mesh overlap uses BVH triangle intersection of evaluated meshes; coplanar touching faces may not register.",
    "Declared intended contacts are reported but not excluded; the reviewer decides.",
]
AXES = {"x": 0, "y": 1, "z": 2}


def _bbox(obj, depsgraph):
    from mathutils import Vector

    ev = obj.evaluated_get(depsgraph)
    if obj.type == "EMPTY" or not hasattr(ev, "bound_box"):
        loc = list(ev.matrix_world.translation)
        return {"min": loc, "max": loc, "center": loc, "dimensions": [0.0, 0.0, 0.0], "empty": True}
    pts = [ev.matrix_world @ Vector(c) for c in ev.bound_box]
    mn = [min(p[i] for p in pts) for i in range(3)]
    mx = [max(p[i] for p in pts) for i in range(3)]
    return {"min": mn, "max": mx, "center": [(mn[i] + mx[i]) / 2 for i in range(3)],
            "dimensions": [mx[i] - mn[i] for i in range(3)], "empty": False}


def _intersects(a, b, eps=1e-6):
    return all(a["min"][i] <= b["max"][i] + eps and b["min"][i] <= a["max"][i] + eps for i in range(3))


def _gap(a, b):
    per_axis = [max(0.0, max(a["min"][i] - b["max"][i], b["min"][i] - a["max"][i])) for i in range(3)]
    return sum(g * g for g in per_axis) ** 0.5, per_axis


def _mesh_overlap(oa, ob, depsgraph):
    try:
        from mathutils.bvhtree import BVHTree

        ta = BVHTree.FromObject(oa, depsgraph)
        tb = BVHTree.FromObject(ob, depsgraph)
        return len(ta.overlap(tb)) > 0
    except Exception:  # noqa: BLE001
        return None


def main(args):
    import bpy

    req = args.get("requests") or {}
    dg = bpy.context.evaluated_depsgraph_get()
    objs = {str(o.get(C.ID_PROP)): o for o in bpy.data.objects if o.get(C.ID_PROP) is not None}
    errors, warnings = [], []

    def bbox_of(aid):
        if aid not in objs:
            raise KeyError(f"no object with alloy_id {aid!r}")
        return _bbox(objs[aid], dg)

    wanted = req.get("bboxes", [])
    if wanted == "all":
        wanted = list(objs)
    bboxes = {}
    for aid in wanted:
        try:
            bboxes[aid] = bbox_of(aid)
        except KeyError as exc:
            errors.append(str(exc))
    distances = []
    for a, b in req.get("distances", []):
        try:
            ba, bb = bbox_of(a), bbox_of(b)
        except KeyError as exc:
            errors.append(str(exc))
            continue
        center = sum((ba["center"][i] - bb["center"][i]) ** 2 for i in range(3)) ** 0.5
        gap, per_axis = _gap(ba, bb)
        distances.append({"between": [a, b], "center_distance": center, "bbox_gap": gap, "gap_per_axis": per_axis,
                          "bbox_intersects": _intersects(ba, bb)})
    ratios = {}
    for r in req.get("ratios", []):
        try:
            num = bbox_of(r["numerator"][0])["dimensions"][AXES[r["numerator"][1]]]
            den = bbox_of(r["denominator"][0])["dimensions"][AXES[r["denominator"][1]]]
            ratios[r["name"]] = {"value": (num / den) if den else None, "numerator": num, "denominator": den}
        except (KeyError, IndexError) as exc:
            errors.append(f"ratio {r.get('name')}: {exc}")
    overlaps = []
    ov = req.get("overlaps") or {}
    ids = ov.get("ids", [])
    if ids == "all":
        ids = list(objs)
    intended = {tuple(sorted(p)) for p in ov.get("intended_contact", [])}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            try:
                ba, bb = bbox_of(a), bbox_of(b)
            except KeyError as exc:
                errors.append(str(exc))
                continue
            inter = _intersects(ba, bb)
            mesh = None
            if inter and objs[a].type == "MESH" and objs[b].type == "MESH":
                mesh = _mesh_overlap(objs[a], objs[b], dg)
            overlaps.append({"pair": [a, b], "bbox_intersects": inter, "mesh_overlap": mesh,
                             "intended_contact": tuple(sorted((a, b))) in intended})
    data = {"bboxes": bboxes, "distances": distances, "ratios": ratios, "overlaps": overlaps,
            "limitations": LIMITATIONS, "units": {"system": bpy.context.scene.unit_settings.system,
                                                  "scale_length": bpy.context.scene.unit_settings.scale_length}}
    return (not errors), data, errors, warnings


C.run_stage("measure", main)
