"""``source register`` and ``source empty`` on a real Blender (layer B): the owner's file is validated in separate
Blender processes (identities enumerated, reopened), copied byte for byte with its untagged geometry recorded and
never tagged in place; a corrupted file is refused with Blender's own error and nothing is registered; the empty
scene reopens with one tagged collection and no objects, and the engine's measurement of it is empty."""
from __future__ import annotations

import io
import stat
from pathlib import Path

import pytest

from builder import cli
from builder.blender.runner import BlenderRunner
from builder.ids import sha256_file
from builder.project import Project

pytestmark = pytest.mark.blender


@pytest.fixture
def runner(blender_exe, workdir):
    return BlenderRunner(blender_exe, logs_dir=workdir / "logs", deadlines={"validate": 90, "apply": 120, "render": 180,
                                                                            "measure": 60, "fixture": 180})


def run_cli(runner, *argv):
    out = io.StringIO()
    code = cli.main([str(a) for a in argv], runner_factory=(lambda cfg, project: runner), stdout=out)
    return code, out.getvalue()


@pytest.fixture
def wf(runner, workdir):
    d = workdir / "wf ü"
    assert run_cli(runner, "new", "--workflow-dir", d, "--name", "Mast ü", "--asset", "E11 Mast", "--first-component", "Head")[0] == 0
    return d


OWNER_SPEC = {
    "collections": [{"alloy_id": "col_main", "name": "Main"}],
    "objects": [
        {"alloy_id": "p_head", "name": "Head", "primitive": "cube", "size": [0.5, 0.5, 0.5], "location": [0, 0, 2.0], "collection": "col_main",
         "material": {"alloy_id": "mat_base", "name": "Base", "base_color": [0.4, 0.4, 0.42, 1.0]}},
        {"alloy_id": "p_leg", "name": "Leg", "primitive": "cylinder", "size": [0.1, 0.1, 1.8], "location": [0.4, 0, 0.9], "collection": "col_main",
         "instances": [{"alloy_id": "p_leg.1", "location": [-0.2, 0.35, 0.9]}, {"alloy_id": "p_leg.2", "location": [-0.2, -0.35, 0.9]}]},
    ],
    "features": [],
}

UNTAG_SCRIPT = """
import bpy, sys
bpy.ops.wm.open_mainfile(filepath=sys.argv[sys.argv.index('--') + 1])
bpy.ops.mesh.primitive_cube_add(size=0.3, location=(0, 0, 0.15))
bpy.context.active_object.name = 'Untagged base'
bpy.ops.wm.save_as_mainfile(filepath=sys.argv[sys.argv.index('--') + 2])
"""


@pytest.fixture
def owner_blend(runner, blender_exe, workdir):
    """A source like an owner's: tagged parts, one instance pair, and one object without any alloy_id."""
    import subprocess

    tagged = workdir / "owner" / "tagged.blend"
    res = runner.build_scene(tagged, OWNER_SPEC, op_id="owner_build")
    assert res.ok, res.error
    script = workdir / "owner" / "untag.py"
    script.write_text(UNTAG_SCRIPT, encoding="utf-8")
    out = workdir / "owner" / "E11 owner ü.blend"
    proc = subprocess.run([str(blender_exe), "-b", "--factory-startup", "-noaudio", "--python-exit-code", "3",
                           "--python", str(script), "--", str(tagged), str(out)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=180)
    assert proc.returncode == 0 and out.is_file(), proc.stderr[-800:]
    return out


def test_source_register_validates_copies_unmodified_and_records_untagged_geometry(runner, wf, owner_blend):
    before = (sha256_file(owner_blend), owner_blend.stat().st_mtime_ns, owner_blend.stat().st_size)
    code, out = run_cli(runner, "source", "register", wf, owner_blend, "--note", "owner's blockout")
    assert code == 0, out
    assert (sha256_file(owner_blend), owner_blend.stat().st_mtime_ns, owner_blend.stat().st_size) == before
    assert not (owner_blend.parent / (owner_blend.name + "1")).exists()          # no .blend1 written next to the source
    with Project.open(wf) as prj:
        rev = prj.store.list("revision")[-1]
        parts = {p.id: p for p in prj.store.list("part")}
        events = [e.event for e in prj.store.journal()]
    dest = Path(rev.data["file"])
    assert dest.is_file() and sha256_file(dest) == before[0] and not (dest.stat().st_mode & stat.S_IWRITE)
    ids = {e["alloy_id"]: e for e in rev.data["identity_map"]}
    assert {"p_head", "p_leg", "p_leg.1", "p_leg.2", "col_main", "mat_base"} <= set(ids)
    assert ids["p_leg.1"]["alloy_kind"] == "instance" and ids["p_leg.1"]["instance_of"] == "p_leg"
    assert rev.data["source"]["unmapped_geometry"] == ["Untagged base"]
    assert rev.data["source"]["reopened"] is True and rev.data["source"]["object_count"] == 5
    assert "1 object(s) without alloy_id: Untagged base" in out
    assert set(parts) == {"p_head", "p_leg", "p_leg.1", "p_leg.2"} and parts["p_leg.1"].data["instance_of"] == "p_leg"
    assert "source.validated" in events and "source.registered" in events
    # the registered revision reopens in a fresh process and enumerates the same identities
    res = runner.identities(dest, op_id="reopen_check")
    assert res.ok and {e["alloy_id"] for e in res.data["identity_map"]} == set(ids)


def test_source_register_refuses_a_corrupted_file_with_blenders_error(runner, wf, workdir):
    bad = workdir / "corrupt ü.blend"
    bad.write_bytes(b"BLENDER-v502" + b"\x00" * 64 + b"not a blend file")
    code, out = run_cli(runner, "source", "register", wf, bad)
    assert code == 2 and "error" in out.lower()
    with Project.open(wf) as prj:
        assert prj.store.list("revision") == []
        events = [e.event for e in prj.store.journal()]
    assert "source.rejected" in events and "revision.created" not in events
    assert not any(Path(wf, "revisions").iterdir())


def test_source_empty_registers_a_scene_with_one_tagged_collection_and_no_objects(runner, wf):
    code, out = run_cli(runner, "source", "empty", wf)
    assert code == 0, out
    with Project.open(wf) as prj:
        rev = prj.store.list("revision")[-1]
        assert prj.store.list("part") == []
    dest = Path(rev.data["file"])
    assert dest.is_file() and not (dest.stat().st_mode & stat.S_IWRITE)
    assert rev.data["source"]["kind"] == "empty_scene" and rev.data["source"]["object_count"] == 0
    ids = runner.identities(dest, op_id="empty_reopen")
    assert ids.ok and [e["alloy_id"] for e in ids.data["identity_map"]] == ["col_model"]
    assert ids.data["object_count"] == 0
    meas = runner.measure(dest, {"bboxes": "all"}, op_id="empty_measure")
    assert meas.ok and not any(not b.get("empty") for b in (meas.data.get("bboxes") or {}).values())
    assert not any(p for p in Path(wf, "staging").rglob("*") if p.is_file())
    code, out = run_cli(runner, "source", "empty", wf)
    assert code == 2 and "already" in out
