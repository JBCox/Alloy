"""Shared helpers for scripts that run INSIDE Blender (bundled Python 3.13, no Alloy imports).

Protocol: ``sys.argv`` after ``--`` holds the path of a UTF-8 JSON args file. The args carry
``_op_id``, ``_result_path``, ``_scripts_dir`` and ``_work_dir``. A script writes exactly one
result JSON: ``{ok, op_id, stage, data, errors, warnings, blender_version, elapsed_s}``.
On an unhandled exception the result is written with ``ok: false`` and the exception is re-raised so
``--python-exit-code 3`` makes the process exit non-zero.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback

ID_PROP = "alloy_id"
KIND_PROP = "alloy_kind"
INSTANCE_PROP = "alloy_instance_of"
GEOMETRY_TYPES = ("MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME", "GREASEPENCIL")


def load_args() -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:]
    with open(argv[0], "r", encoding="utf-8") as f:
        return json.load(f)


def write_result(args: dict, *, ok: bool, stage: str, data: dict | None = None, errors: list | None = None,
                 warnings: list | None = None, elapsed_s: float | None = None) -> None:
    import bpy

    payload = {"ok": bool(ok), "op_id": args.get("_op_id"), "stage": stage, "data": data or {},
               "errors": list(errors or []), "warnings": list(warnings or []),
               "blender_version": bpy.app.version_string, "elapsed_s": elapsed_s}
    path = args["_result_path"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def run_stage(stage: str, fn) -> None:
    args = load_args()
    t0 = time.time()
    try:
        ok, data, errors, warnings = fn(args)
    except Exception:
        tb = traceback.format_exc()
        try:
            write_result(args, ok=False, stage=stage, data={"traceback": tb}, errors=[tb], elapsed_s=time.time() - t0)
        finally:
            raise
    write_result(args, ok=ok, stage=stage, data=data, errors=errors, warnings=warnings, elapsed_s=time.time() - t0)


# --- identities -------------------------------------------------------------------------

def tag(datablock, alloy_id: str, kind: str = "part", instance_of: str | None = None) -> None:
    datablock[ID_PROP] = str(alloy_id)
    datablock[KIND_PROP] = str(kind)
    if instance_of:
        datablock[INSTANCE_PROP] = str(instance_of)


def get_id(datablock):
    try:
        return datablock.get(ID_PROP)
    except (AttributeError, TypeError):
        return None


def identity_map() -> list[dict]:
    import bpy

    entries: list[dict] = []
    for o in bpy.data.objects:
        aid = o.get(ID_PROP)
        if aid is None:
            continue
        entries.append({
            "alloy_id": str(aid), "alloy_kind": str(o.get(KIND_PROP) or "part"), "type": "OBJECT", "name": o.name,
            "object_type": o.type, "parent_alloy_id": get_id(o.parent) if o.parent else None,
            "collections": [c.name for c in o.users_collection],
            "instance_of": o.get(INSTANCE_PROP), "data_name": o.data.name if o.data else None,
            "material_ids": [get_id(m) for m in getattr(o.data, "materials", []) if m is not None] if o.data else [],
        })
    for c in bpy.data.collections:
        aid = c.get(ID_PROP)
        if aid is None:
            continue
        entries.append({"alloy_id": str(aid), "alloy_kind": str(c.get(KIND_PROP) or "collection"), "type": "COLLECTION",
                        "name": c.name, "object_type": None, "parent_alloy_id": None,
                        "collections": [], "instance_of": None, "data_name": None, "material_ids": []})
    for m in bpy.data.materials:
        aid = m.get(ID_PROP)
        if aid is None:
            continue
        entries.append({"alloy_id": str(aid), "alloy_kind": str(m.get(KIND_PROP) or "material"), "type": "MATERIAL",
                        "name": m.name, "object_type": None, "parent_alloy_id": None,
                        "collections": [], "instance_of": None, "data_name": None, "material_ids": []})
    return entries


def find(alloy_id: str):
    import bpy

    for coll in (bpy.data.objects, bpy.data.collections, bpy.data.materials):
        for db in coll:
            if db.get(ID_PROP) == alloy_id:
                return db
    return None


class AlloyHelper:
    """Namespace offered to agent scripts as ``ALLOY``."""

    def get(self, alloy_id: str):
        db = find(alloy_id)
        if db is None:
            raise KeyError(f"no datablock carries alloy_id={alloy_id!r}")
        return db

    def find(self, alloy_id: str):
        return find(alloy_id)

    def ids(self) -> list[str]:
        return [e["alloy_id"] for e in identity_map()]

    def tag(self, datablock, alloy_id: str, kind: str = "part", instance_of: str | None = None) -> None:
        tag(datablock, alloy_id, kind, instance_of)

    def objects(self):
        import bpy

        return [o for o in bpy.data.objects if o.get(ID_PROP) is not None]


def is_finite_matrix(m) -> bool:
    for row in m:
        for v in row:
            if not math.isfinite(v):
                return False
    return True


def external_assets() -> list[dict]:
    import bpy

    out: list[dict] = []
    for img in bpy.data.images:
        if img.source != "FILE" or not img.filepath:
            continue
        packed = img.packed_file is not None
        path = bpy.path.abspath(img.filepath)
        out.append({"kind": "image", "name": img.name, "path": path, "raw": img.filepath, "packed": packed,
                    "exists": packed or os.path.exists(path)})
    for lib in bpy.data.libraries:
        path = bpy.path.abspath(lib.filepath)
        out.append({"kind": "library", "name": lib.name, "path": path, "raw": lib.filepath, "packed": False,
                    "exists": os.path.exists(path)})
    return out


def ensure_parent_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
