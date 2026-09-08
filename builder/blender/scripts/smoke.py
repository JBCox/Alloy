"""Preflight smoke test run inside Blender: versions, engines that can be set, custom-property round trip."""
import json
import os
import sys

_argv = sys.argv[sys.argv.index("--") + 1:]
with open(_argv[0], "r", encoding="utf-8") as _f:
    sys.path.insert(0, json.load(_f)["_scripts_dir"])
import _alloy_common as C  # noqa: E402


def main(args):
    import bpy

    scene = bpy.context.scene
    engines_ok, problems = [], []
    for eng in ("BLENDER_WORKBENCH", "BLENDER_EEVEE"):
        try:
            scene.render.engine = eng
            if scene.render.engine == eng:
                engines_ok.append(eng)
            else:
                problems.append(f"{eng}: set but read back as {scene.render.engine}")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{eng}: {exc}")
    probe = bpy.data.objects.new("ALLOY_probe", None)
    C.tag(probe, "probe_id", "feature")
    prop_ok = probe.get(C.ID_PROP) == "probe_id"
    bpy.data.objects.remove(probe)
    data = {
        "blender_version": bpy.app.version_string, "python_version": sys.version.split()[0],
        "binary_path": bpy.app.binary_path, "engines_ok": engines_ok, "engine_problems": problems,
        "custom_property_ok": prop_ok, "background": bpy.app.background, "pid": os.getpid(),
    }
    return (bool(engines_ok) and prop_ok), data, [] if engines_ok else ["no render engine could be set"], problems


C.run_stage("smoke", main)
