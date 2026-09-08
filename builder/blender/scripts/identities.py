"""Enumerate every datablock that carries an alloy_id (spec R-37). Read-only."""
import json
import sys

_argv = sys.argv[sys.argv.index("--") + 1:]
with open(_argv[0], "r", encoding="utf-8") as _f:
    sys.path.insert(0, json.load(_f)["_scripts_dir"])
import _alloy_common as C  # noqa: E402


def main(args):
    import bpy

    return True, {"identity_map": C.identity_map(), "object_count": len(bpy.data.objects),
                  "file": bpy.data.filepath, "external_assets": C.external_assets()}, [], []


C.run_stage("identities", main)
