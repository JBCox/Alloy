"""Apply an agent's bpy script to a staged copy and save the result (spec D4, Section 5).

The base file was loaded by the command line. The script runs with ``bpy`` and an ``ALLOY`` helper in
its namespace. Any exception is reported and nothing is saved. Objects that are linked to no
collection are reported before saving, because Blender drops zero-user datablocks on save.
"""
import json
import sys
import traceback

_argv = sys.argv[sys.argv.index("--") + 1:]
with open(_argv[0], "r", encoding="utf-8") as _f:
    sys.path.insert(0, json.load(_f)["_scripts_dir"])
import _alloy_common as C  # noqa: E402


def main(args):
    import bpy

    script_path = args["script_path"]
    out = args["out_blend"]
    effects = args.get("declared_effects") or {}
    with open(script_path, "r", encoding="utf-8") as f:
        source = f.read()
    base_map = C.identity_map()
    namespace = {"bpy": bpy, "ALLOY": C.AlloyHelper(), "__name__": "__alloy_operation__"}
    try:
        exec(compile(source, script_path, "exec"), namespace)  # noqa: S102 - the agent script by design (D4)
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc()
        return False, {"stage_failed": "exec", "traceback": tb, "base_identity_map": base_map}, \
            [f"agent script raised: {tb.strip().splitlines()[-1]}", tb], []
    bpy.context.view_layer.update()
    orphans = [str(o.get(C.ID_PROP) or o.name) for o in bpy.data.objects if not o.users_collection]
    warnings = [f"orphan {o}: linked to no collection, so it would be lost on save" for o in orphans]
    C.ensure_parent_dir(out)
    bpy.ops.wm.save_as_mainfile(filepath=out, relative_remap=True)
    data = {"saved": bpy.data.filepath, "identity_map": C.identity_map(), "base_identity_map": base_map,
            "orphans_before_save": orphans, "external_assets": C.external_assets(), "declared_effects": effects,
            "object_count": len(bpy.data.objects)}
    return True, data, [], warnings


C.run_stage("apply", main)
