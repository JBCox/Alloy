"""Validate a staged .blend in a fresh Blender process (spec Section 5, R-37, R-48).

The file reopened (Blender loaded it before this script ran). Checks: identity map versus the base
map (deleted, duplicated, unmapped, orphaned), expected parts present and declared deletions absent,
NaN or infinite transforms, missing external assets. Read-only: never saves.
"""
import json
import sys
from collections import Counter

_argv = sys.argv[sys.argv.index("--") + 1:]
with open(_argv[0], "r", encoding="utf-8") as _f:
    sys.path.insert(0, json.load(_f)["_scripts_dir"])
import _alloy_common as C  # noqa: E402


def main(args):
    import bpy

    exp = args.get("expectations") or {}
    errors, warnings = [], []
    ids = C.identity_map()
    current_ids = [e["alloy_id"] for e in ids]
    current = set(current_ids)
    duplicates = sorted(k for k, v in Counter(current_ids).items() if v > 1)
    base_ids = {e["alloy_id"] for e in (exp.get("base_identity_map") or [])}
    declared_absent = set(exp.get("expect_absent") or [])
    deleted = sorted(base_ids - current)
    unexpected_deleted = [d for d in deleted if d not in declared_absent]
    unmapped = [o.name for o in bpy.data.objects if o.type in C.GEOMETRY_TYPES and o.get(C.ID_PROP) is None]
    orphans = [str(o.get(C.ID_PROP) or o.name) for o in bpy.data.objects if not o.users_collection]
    nan_transforms = [str(o.get(C.ID_PROP) or o.name) for o in bpy.data.objects
                      if not C.is_finite_matrix(o.matrix_world)]
    missing_present = [i for i in (exp.get("expect_present") or []) if i not in current]
    unexpected_present = sorted(i for i in declared_absent if i in current)
    assets = C.external_assets()
    missing_assets = [a for a in assets if not a["exists"]]

    if unexpected_deleted:
        errors.append(f"unexpected deletions: {', '.join(unexpected_deleted)}")
    if duplicates:
        errors.append(f"duplicate alloy_id: {', '.join(duplicates)}")
    if unmapped and not exp.get("allow_unmapped"):
        errors.append(f"geometry without alloy_id: {', '.join(unmapped)}")
    if orphans:
        errors.append(f"objects linked to no collection: {', '.join(orphans)}")
    if nan_transforms:
        errors.append(f"non-finite transforms: {', '.join(nan_transforms)}")
    if missing_present:
        errors.append(f"expected parts missing: {', '.join(missing_present)}")
    if unexpected_present:
        errors.append(f"declared deletions still present: {', '.join(unexpected_present)}")
    if missing_assets:
        errors.append("missing external assets: " + ", ".join(f"{a['kind']} {a['name']} -> {a['path']}" for a in missing_assets))
    data = {
        "reopened": True, "file": bpy.data.filepath, "object_count": len(bpy.data.objects),
        "identity_map": ids, "deleted": deleted, "unexpected_deleted": unexpected_deleted, "duplicates": duplicates,
        "unmapped": unmapped, "orphans": orphans, "nan_transforms": nan_transforms,
        "missing_present": missing_present, "unexpected_present": unexpected_present,
        "external_assets": assets, "missing_assets": missing_assets,
    }
    return (not errors), data, errors, warnings


C.run_stage("validate", main)
