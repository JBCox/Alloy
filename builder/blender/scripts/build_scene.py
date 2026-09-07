"""Build a .blend from a declarative spec of tagged primitives (used by fixtures and tests).

Spec::

    {"collections": [{"alloy_id", "name"}],
     "objects": [{"alloy_id", "name", "primitive": cube|cylinder|sphere|cone|plane|torus,
                  "size": [x, y, z], "location": [x, y, z], "rotation": [rx, ry, rz],
                  "collection": <collection alloy_id>, "parent": <object alloy_id>,
                  "material": {"alloy_id", "name", "base_color": [r, g, b, a], "metallic", "roughness"},
                  "instances": [{"alloy_id", "location", "rotation"}]}],
     "features": [{"alloy_id", "name", "location", "parent"}]}
"""
import json
import sys

_argv = sys.argv[sys.argv.index("--") + 1:]
with open(_argv[0], "r", encoding="utf-8") as _f:
    sys.path.insert(0, json.load(_f)["_scripts_dir"])
import _alloy_common as C  # noqa: E402


def _primitive(kind: str):
    import bpy

    ops = {
        "cube": lambda: bpy.ops.mesh.primitive_cube_add(size=1.0),
        "cylinder": lambda: bpy.ops.mesh.primitive_cylinder_add(radius=0.5, depth=1.0, vertices=32),
        "sphere": lambda: bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, segments=32, ring_count=16),
        "cone": lambda: bpy.ops.mesh.primitive_cone_add(radius1=0.5, radius2=0.0, depth=1.0, vertices=32),
        "plane": lambda: bpy.ops.mesh.primitive_plane_add(size=1.0),
        "torus": lambda: bpy.ops.mesh.primitive_torus_add(major_radius=0.35, minor_radius=0.15),
    }
    if kind not in ops:
        raise ValueError(f"unknown primitive {kind!r}")
    ops[kind]()
    return bpy.context.active_object


def _material(spec: dict, cache: dict, warnings: list):
    import bpy

    aid = spec["alloy_id"]
    if aid in cache:
        return cache[aid]
    mat = bpy.data.materials.new(spec.get("name") or aid)
    if getattr(mat, "node_tree", None) is None:
        try:
            mat.use_nodes = True  # older API; 5.2 warns that this attribute is going away
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"material {aid}: could not enable nodes: {exc}")
    color = spec.get("base_color")
    tree = getattr(mat, "node_tree", None)
    bsdf = next((n for n in tree.nodes if n.type == "BSDF_PRINCIPLED"), None) if tree else None
    if bsdf is not None:
        if color is not None and "Base Color" in bsdf.inputs:
            bsdf.inputs["Base Color"].default_value = tuple(color)
        if "metallic" in spec and "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = float(spec["metallic"])
        if "roughness" in spec and "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = float(spec["roughness"])
    elif color is not None:
        warnings.append(f"material {aid}: no Principled BSDF node found; base color set on viewport color only")
    if color is not None:
        mat.diffuse_color = tuple(color)
    C.tag(mat, aid, spec.get("kind", "material"))
    cache[aid] = mat
    return mat


def main(args):
    import bpy

    spec = args["spec"]
    out = args["out_blend"]
    warnings: list = []
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    collections = {}
    for cspec in spec.get("collections", []):
        coll = bpy.data.collections.new(cspec.get("name") or cspec["alloy_id"])
        scene.collection.children.link(coll)
        C.tag(coll, cspec["alloy_id"], "collection")
        collections[cspec["alloy_id"]] = coll
    objects = {}
    materials: dict = {}

    def place(obj, ospec, collection_id):
        obj.location = tuple(ospec.get("location", (0.0, 0.0, 0.0)))
        obj.rotation_euler = tuple(ospec.get("rotation", (0.0, 0.0, 0.0)))
        target = collections.get(collection_id) if collection_id else None
        for c in list(obj.users_collection):
            c.objects.unlink(obj)
        (target or scene.collection).objects.link(obj)

    for ospec in spec.get("objects", []):
        obj = _primitive(ospec.get("primitive", "cube"))
        obj.name = ospec.get("name") or ospec["alloy_id"]
        size = ospec.get("size", (1.0, 1.0, 1.0))
        obj.scale = tuple(float(s) for s in size)
        place(obj, ospec, ospec.get("collection"))
        C.tag(obj, ospec["alloy_id"], ospec.get("kind", "part"))
        if ospec.get("material"):
            obj.data.materials.append(_material(ospec["material"], materials, warnings))
        objects[ospec["alloy_id"]] = obj
        for ispec in ospec.get("instances", []):
            inst = obj.copy()  # shares mesh data: a linked duplicate
            inst.name = ispec.get("name") or ispec["alloy_id"]
            place(inst, {"location": ispec.get("location", obj.location[:]),
                         "rotation": ispec.get("rotation", obj.rotation_euler[:])}, ospec.get("collection"))
            C.tag(inst, ispec["alloy_id"], "instance", instance_of=ospec["alloy_id"])
            objects[ispec["alloy_id"]] = inst
    for ospec in spec.get("objects", []):
        parent_id = ospec.get("parent")
        if parent_id:
            child, parent = objects[ospec["alloy_id"]], objects[parent_id]
            child.parent = parent
            child.matrix_parent_inverse = parent.matrix_world.inverted()
    for fspec in spec.get("features", []):
        empty = bpy.data.objects.new(fspec.get("name") or fspec["alloy_id"], None)
        empty.empty_display_size = 0.05
        (collections.get(fspec.get("collection")) or scene.collection).objects.link(empty)
        empty.location = tuple(fspec.get("location", (0.0, 0.0, 0.0)))
        C.tag(empty, fspec["alloy_id"], "feature")
        if fspec.get("parent"):
            parent = objects[fspec["parent"]]
            empty.parent = parent
            empty.matrix_parent_inverse = parent.matrix_world.inverted()
        objects[fspec["alloy_id"]] = empty
    bpy.context.view_layer.update()
    C.ensure_parent_dir(out)
    bpy.ops.wm.save_as_mainfile(filepath=out, relative_remap=True)
    data = {"saved": bpy.data.filepath, "identity_map": C.identity_map(), "object_count": len(bpy.data.objects)}
    return True, data, [], warnings


C.run_stage("build_scene", main)
