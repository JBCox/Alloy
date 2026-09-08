"""Child process for the real-Blender recovery tests: runs ONE operation on a workflow project and dies at a chosen
boundary, the way a crashed engine would. argv: <workflow_dir> <crash_point> <blender_exe>

crash points:
  edit    the agent script sleeps inside Blender before any save; the parent kills this process while it runs
  save    Blender saved out.blend and wrote its result; this process exits before validation (state: running)
  commit  the revision file was moved into revisions/ (state: promoting); this process exits before the commit record
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from builder.blender.runner import BlenderRunner  # noqa: E402
from builder.operations import OperationRequest, Operations  # noqa: E402
from builder.ownership import OwnershipManager  # noqa: E402
from builder.project import Project  # noqa: E402

SCRIPTS = {
    "edit": "import time\nfor i in range(600):\n    time.sleep(0.1)\nALLOY.get('p_bracket').location.z -= 0.15\n",
    "save": ("import bpy\nobj = bpy.data.objects.new('Extra', bpy.data.meshes.new('extra_mesh'))\n"
             "bpy.context.scene.collection.objects.link(obj)\nALLOY.tag(obj, 'p_extra', 'part')\n"
             "ALLOY.get('p_bracket').location.z -= 0.15\n"),
    "commit": "ALLOY.get('p_housing')['recovery_note'] = 'commit boundary test'\n",
}


def main() -> int:
    wf, point, exe = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    prj = Project.open(wf)
    runner = BlenderRunner(exe, logs_dir=prj.path("logs"), deadlines={"apply": 120, "validate": 60})
    owners = OwnershipManager(prj.store)
    ops = Operations(prj, runner, owners)
    rev = ops.latest_revision()
    holder = owners.current("assembly")
    own = holder if holder is not None else owners.acquire("assembly", "ag_A", rev.id, actor="engine", reason=f"child {point}")
    if point == "save":
        real_apply = runner.apply

        def apply_then_die(*a, **kw):
            res = real_apply(*a, **kw)
            print(f"SAVED {res.outcome}", flush=True)
            os._exit(7)          # host dies after Blender saved and reported, before validation

        runner.apply = apply_then_die  # type: ignore[method-assign]
    elif point == "commit":
        def die_before_commit(*a, **kw):
            print("PROMOTED", flush=True)
            os._exit(9)          # host dies after os.replace into revisions/, before the commit record

        ops._commit = die_before_commit  # type: ignore[method-assign]
    req = OperationRequest(kind="apply_script", task_id=f"t_child_{point}", agent_id="ag_A", intent=f"child op at {point} boundary",
                           expected_outcome="test", target_part_ids=["p_bracket", "p_housing"],
                           declared_effects={"creates": ["p_extra"] if point == "save" else [], "modifies": ["p_bracket", "p_housing"], "deletes": []},
                           script_source=SCRIPTS[point], expected_base_revision_id=rev.id, ownership_token=own.data["token"])
    op = ops.create(req, actor="agent:A")
    print(f"OP {op.id}", flush=True)
    op = ops.execute(op, actor="engine")
    print(f"DONE {op.state}", flush=True)
    prj.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
