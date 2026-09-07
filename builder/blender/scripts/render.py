"""Render one view of a revision to PNG and report the settings that were actually applied
(spec R-60 to R-65, R-70). Never saves the .blend: cameras and lights created here are transient.
"""
import json
import os
import sys
import time

_argv = sys.argv[sys.argv.index("--") + 1:]
with open(_argv[0], "r", encoding="utf-8") as _f:
    sys.path.insert(0, json.load(_f)["_scripts_dir"])
import _alloy_common as C  # noqa: E402

ENGINE_FOR_MODE = {"clay": "BLENDER_WORKBENCH", "material": "BLENDER_EEVEE"}


def _set(obj, attr, value, warnings, label=None):
    try:
        setattr(obj, attr, value)
        return getattr(obj, attr)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"could not set {label or attr}={value!r}: {exc}")
        return getattr(obj, attr, None)


def main(args):
    import bpy

    view = args["view"]
    out = args["out_png"]
    warnings: list = []
    scene = bpy.context.scene
    mode = view.get("mode", "clay")
    engine = view.get("engine") or ENGINE_FOR_MODE.get(mode, "BLENDER_WORKBENCH")
    _set(scene.render, "engine", engine, warnings)

    # camera (transient)
    cam = view.get("camera") or {}
    cam_data = bpy.data.cameras.new("ALLOY_RenderCam")
    cam_obj = bpy.data.objects.new("ALLOY_RenderCam", cam_data)
    scene.collection.objects.link(cam_obj)
    cam_obj.location = tuple(cam.get("location", (4.0, -4.0, 3.0)))
    cam_obj.rotation_euler = tuple(cam.get("rotation_euler", (1.1, 0.0, 0.785)))
    cam_data.type = cam.get("type", "PERSP")
    cam_data.lens = float(cam.get("lens", 50.0))
    if cam_data.type == "ORTHO":
        cam_data.ortho_scale = float(cam.get("ortho_scale", 5.0))
    cam_data.clip_start = float(cam.get("clip_start", 0.01))
    cam_data.clip_end = float(cam.get("clip_end", 1000.0))
    if "sensor_width" in cam:
        cam_data.sensor_width = float(cam["sensor_width"])
    scene.camera = cam_obj

    # visibility from records
    vis = view.get("visibility") or {}
    isolate = vis.get("isolate")
    hide = set(vis.get("hide") or [])
    hidden: list = []
    transient = {cam_obj}
    for o in bpy.data.objects:
        if o in transient:
            continue
        aid = o.get(C.ID_PROP)
        should_hide = False
        if isolate is not None:
            if aid is not None:
                should_hide = aid not in isolate
            elif o.type not in ("LIGHT", "CAMERA"):
                should_hide = True
        if aid is not None and aid in hide:
            should_hide = True
        if should_hide:
            o.hide_render = True
            hidden.append(str(aid) if aid is not None else o.name)

    # lighting
    lighting = view.get("lighting", "neutral_studio")
    if engine == "BLENDER_WORKBENCH":
        sh = scene.display.shading
        _set(sh, "light", "STUDIO", warnings)
        _set(sh, "color_type", "SINGLE", warnings)
        _set(sh, "single_color", (0.8, 0.8, 0.8), warnings)
        _set(sh, "show_shadows", False, warnings)
        _set(sh, "show_cavity", False, warnings)
        _set(sh, "show_object_outline", False, warnings)
        _set(sh, "show_specular_highlight", True, warnings)
        applied_samples = _set(scene.display, "render_aa", "8", warnings, "display.render_aa")
    else:
        if lighting == "neutral_studio":
            for o in scene.objects:
                if o.type == "LIGHT":
                    o.hide_render = True
            key = bpy.data.lights.new("ALLOY_Key", "SUN")
            key.energy = 3.0
            key_obj = bpy.data.objects.new("ALLOY_Key", key)
            key_obj.rotation_euler = (0.9, 0.25, 0.9)
            fill = bpy.data.lights.new("ALLOY_Fill", "SUN")
            fill.energy = 1.0
            fill_obj = bpy.data.objects.new("ALLOY_Fill", fill)
            fill_obj.rotation_euler = (1.2, -0.3, -2.2)
            for lo in (key_obj, fill_obj):
                scene.collection.objects.link(lo)
            world = scene.world or bpy.data.worlds.new("ALLOY_World")
            scene.world = world
            tree = getattr(world, "node_tree", None)
            bg = next((n for n in tree.nodes if n.type == "BACKGROUND"), None) if tree else None
            if bg is not None and "Color" in bg.inputs:
                bg.inputs["Color"].default_value = (0.35, 0.35, 0.35, 1.0)
                if "Strength" in bg.inputs:
                    bg.inputs["Strength"].default_value = 1.0
            else:
                _set(world, "color", (0.35, 0.35, 0.35), warnings, "world.color")
        samples = int(view.get("samples", 16))
        eevee = getattr(scene, "eevee", None)
        applied_samples = _set(eevee, "taa_render_samples", samples, warnings, "eevee.taa_render_samples") if eevee else None

    # color management and output
    cm = view.get("color_management") or {}
    vt = _set(scene.view_settings, "view_transform", cm.get("view_transform", "Standard"), warnings)
    look = _set(scene.view_settings, "look", cm.get("look", "None"), warnings)
    _set(scene.view_settings, "exposure", 0.0, warnings)
    _set(scene.view_settings, "gamma", 1.0, warnings)
    if cm.get("display_device"):
        _set(scene.display_settings, "display_device", cm["display_device"], warnings)
    res = view.get("resolution") or [1024, 768]
    scene.render.resolution_x, scene.render.resolution_y = int(res[0]), int(res[1])
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = bool(view.get("film_transparent", False))
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    C.ensure_parent_dir(out)
    scene.render.filepath = out
    t0 = time.time()
    bpy.ops.render.render(write_still=True)
    elapsed = time.time() - t0
    exists = os.path.isfile(out)
    applied = {
        "engine": scene.render.engine, "blender_version": bpy.app.version_string,
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "resolution_percentage": scene.render.resolution_percentage,
        "view_transform": vt, "look": look, "display_device": scene.display_settings.display_device,
        "samples": applied_samples, "seed": None, "seed_note": "no seed setting for this engine",
        "lighting": lighting, "mode": mode, "hidden_ids": hidden, "isolated_ids": isolate,
        "film_transparent": scene.render.film_transparent,
        "camera": {"location": list(cam_obj.location), "rotation_euler": list(cam_obj.rotation_euler),
                   "lens": cam_data.lens, "type": cam_data.type, "clip_start": cam_data.clip_start,
                   "clip_end": cam_data.clip_end, "sensor_width": cam_data.sensor_width,
                   "ortho_scale": cam_data.ortho_scale},
        "render_time_s": elapsed, "output": out,
    }
    return exists, {"applied": applied}, [] if exists else [f"render produced no file at {out}"], warnings


C.run_stage("render", main)
