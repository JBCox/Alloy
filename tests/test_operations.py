"""Operation lifecycle with a fake Blender runner (layer D): staging, validation, atomic promotion,
stale rejection (R-41, R-45), failure classes (R-23), and restart reconciliation (R-47)."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from builder.blender.runner import BlenderResult
from builder.operations import OperationRequest, Operations, screen_script
from builder.ownership import OwnershipManager
from builder.project import Project


class FakeRunner:
    """States exactly what it scripts: copies the base to the output with a marker appended, and returns
    canned validation outcomes. Proves the host-side lifecycle, not Blender behaviour."""

    def __init__(self):
        self.calls: list[str] = []
        self.apply_outcome = "ok"
        self.validate_ok = True
        self.identity_map = [{"alloy_id": "p_base", "type": "OBJECT"}, {"alloy_id": "p_cap", "type": "OBJECT"}]

    def _result(self, op_id, outcome, data=None, error="", exit_code=0):
        return BlenderResult(op_id=op_id, outcome=outcome, exit_code=exit_code,
                             result=None if outcome in ("timeout", "uncertain", "cancelled") else
                             {"ok": outcome == "ok", "op_id": op_id, "stage": "x", "data": data or {}, "errors": [error] if error else []},
                             error=error, stdout_path="", stderr_path="", elapsed_s=0.01, kill_confirmed=None, work_dir="")

    def apply(self, base_blend, out_blend, script_path, *, op_id, declared_effects, deadline_s=None, work_dir=None,
              cancel_event=None):
        self.calls.append(f"apply:{op_id}")
        out = Path(out_blend)
        if self.apply_outcome == "ok":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(Path(base_blend).read_bytes() + b"|edit:" + op_id.encode())
            return self._result(op_id, "ok", {"identity_map": self.identity_map, "orphans_before_save": [],
                                             "external_assets": []})
        if self.apply_outcome == "timeout":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"partial")
            return self._result(op_id, "timeout", error="timeout after 1.0s", exit_code=None)
        if self.apply_outcome == "uncertain":
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(Path(base_blend).read_bytes() + b"|maybe")
            return self._result(op_id, "uncertain", error="exit 0 but no result.json", exit_code=0)
        return self._result(op_id, "failed", error="agent script raised: ValueError", exit_code=3)

    def validate(self, blend, expectations, *, op_id, work_dir=None):
        self.calls.append(f"validate:{op_id}")
        data = {"identity_map": self.identity_map, "deleted": [], "duplicates": [], "unmapped": [], "orphans": [],
                "missing_assets": [], "reopened": True}
        if self.validate_ok:
            return self._result(op_id, "ok", data)
        data["duplicates"] = ["p_cap"]
        return self._result(op_id, "failed", data, error="duplicate alloy_id: p_cap")

    def identities(self, blend, *, op_id, work_dir=None):
        self.calls.append(f"identities:{op_id}")
        return self._result(op_id, "ok", {"identity_map": self.identity_map})


@pytest.fixture
def project(workdir):
    prj = Project.create(workdir / "wf", name="Ops test", asset_name="Lamp")
    yield prj
    prj.close()


@pytest.fixture
def ops(project):
    runner = FakeRunner()
    owners = OwnershipManager(project.store)
    o = Operations(project, runner, owners)
    src = project.path("staging", "seed.blend")
    src.write_bytes(b"BLENDER-fake-seed")
    rev0 = o.register_revision(src, parent_revision_id=None, created_by_op_id=None, actor="engine",
                               identity_map=runner.identity_map, note="fixture revision 0")
    return o, runner, owners, rev0


def _request(rev0, token, script="import bpy\nALLOY.get('p_cap').location.z += 0.1\n", base=None):
    return OperationRequest(kind="apply_script", task_id="t_1", agent_id="agent:A", intent="lift cap",
                            expected_outcome="cap sits 0.1 higher in side view", target_part_ids=["p_cap"],
                            declared_effects={"creates": [], "modifies": ["p_cap"], "deletes": []},
                            script_source=script, expected_base_revision_id=base or rev0.id, ownership_token=token)


def test_screen_script_flags_dangerous_constructs_and_passes_clean_scripts():
    clean = "import bpy\nimport math\nobj = ALLOY.get('p_cap')\nobj.location.z += math.sin(0.1)\n"
    assert screen_script(clean) == []
    bad = ("import os\nfrom subprocess import run\nopen('x','w')\nexec('1')\nbpy.ops.wm.save_as_mainfile(filepath='a')\n"
           "bpy.ops.wm.open_mainfile(filepath='b')\n__import__('shutil')\nbpy.data.libraries.load('c')\n")
    problems = screen_script(bad)
    joined = "\n".join(problems)
    for needle in ("os", "subprocess", "open(", "exec(", "save_as_mainfile", "open_mainfile", "__import__", "libraries"):
        assert needle in joined, needle


def test_register_revision_makes_immutable_registered_file(ops, project):
    o, runner, owners, rev0 = ops
    path = Path(rev0.data["file"])
    assert path.parent == project.path("revisions") and path.is_file()
    assert not os.access(path, os.W_OK) or not (path.stat().st_mode & stat.S_IWRITE)
    ok, expected, actual = project.store.verify_file(path)
    assert ok and rev0.data["sha256"] == expected
    assert o.latest_revision().id == rev0.id


def test_execute_stages_validates_promotes_and_journals(ops, project):
    o, runner, owners, rev0 = ops
    own = owners.acquire("assembly", "agent:A", rev0.id, actor="engine", reason="t_1")
    op = o.create(_request(rev0, own.data["token"]), actor="agent:A")
    assert op.state == "created" and Path(op.data["script_path"]).is_file()
    op = o.execute(op, actor="engine")
    assert op.state == "committed", op.data.get("error")
    rev1 = project.store.require("revision", op.data["result_revision_id"])
    assert rev1.data["parent_revision_id"] == rev0.id and rev1.data["created_by_op_id"] == op.id
    rev_file = Path(rev1.data["file"])
    assert rev_file.is_file() and rev_file.read_bytes().endswith(b"|edit:" + op.id.encode())
    assert project.store.verify_file(rev_file)[0]
    assert runner.calls == [f"apply:{op.id}", f"validate:{op.id}"]
    events = [e.event for e in project.store.journal() if e.op_id == op.id or e.record_id == op.id]
    assert events.index("op.promoting") < events.index("op.committed")
    assert o.latest_revision().id == rev1.id
    assert owners.check(own.data["token"], "assembly", rev1.id).data["base_revision_id"] == rev1.id
    assert not project.path("staging", op.id, "out.blend").exists()  # promoted, not copied


def test_stale_token_and_base_mismatch_rejected_before_blender_runs(ops, project):
    o, runner, owners, rev0 = ops
    own = owners.acquire("assembly", "agent:A", rev0.id, actor="engine", reason="t_1")
    op = o.create(_request(rev0, "stale-token"), actor="agent:A")
    op = o.execute(op, actor="engine")
    assert op.state == "rejected" and "unknown" in op.data["error"]
    op2 = o.create(_request(rev0, own.data["token"], base="rev_wrong"), actor="agent:A")
    op2 = o.execute(op2, actor="engine")
    assert op2.state == "rejected" and "base revision" in op2.data["error"]
    assert runner.calls == []


def test_external_modification_of_revision_blocks_operation(ops, project):
    o, runner, owners, rev0 = ops
    own = owners.acquire("assembly", "agent:A", rev0.id, actor="engine", reason="t_1")
    path = Path(rev0.data["file"])
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    path.write_bytes(b"tampered outside alloy")
    op = o.execute(o.create(_request(rev0, own.data["token"]), actor="agent:A"), actor="engine")
    assert op.state == "rejected" and "hash" in op.data["error"].lower()
    assert runner.calls == []
    assert any(e.event == "revision.external_modification" for e in project.store.journal())


def test_validation_failure_rejects_and_preserves_output_for_diagnosis(ops, project):
    o, runner, owners, rev0 = ops
    runner.validate_ok = False
    own = owners.acquire("assembly", "agent:A", rev0.id, actor="engine", reason="t_1")
    op = o.execute(o.create(_request(rev0, own.data["token"]), actor="agent:A"), actor="engine")
    assert op.state == "rejected" and "duplicate" in op.data["error"]
    assert project.store.list("revision") == [rev0] or len(project.store.list("revision")) == 1
    assert Path(op.data["staged_output"]).is_file()
    assert o.latest_revision().id == rev0.id


def test_apply_failure_is_failed_and_timeout_is_uncertain(ops, project):
    o, runner, owners, rev0 = ops
    own = owners.acquire("assembly", "agent:A", rev0.id, actor="engine", reason="t_1")
    runner.apply_outcome = "failed"
    op = o.execute(o.create(_request(rev0, own.data["token"]), actor="agent:A"), actor="engine")
    assert op.state == "failed" and "ValueError" in op.data["error"]
    runner.apply_outcome = "timeout"
    op2 = o.execute(o.create(_request(rev0, own.data["token"]), actor="agent:A"), actor="engine")
    assert op2.state == "uncertain"
    assert Path(op2.data["quarantine_dir"]).is_dir()
    assert len(project.store.list("revision")) == 1


def test_screened_script_is_rejected_at_creation(ops):
    o, runner, owners, rev0 = ops
    own = owners.acquire("assembly", "agent:A", rev0.id, actor="engine", reason="t_1")
    op = o.create(_request(rev0, own.data["token"], script="import os\nos.remove('x')\n"), actor="agent:A")
    assert op.state == "rejected" and "os" in op.data["error"]


def test_reconcile_classifies_inflight_operations_without_replaying(ops, project):
    o, runner, owners, rev0 = ops
    own = owners.acquire("assembly", "agent:A", rev0.id, actor="engine", reason="t_1")
    tok = own.data["token"]
    store = project.store
    # 1. never started: created, no staging directory
    never = o.create(_request(rev0, tok), actor="agent:A")
    # 2. running with a staged output but no result: uncertain
    running = o.create(_request(rev0, tok), actor="agent:A")
    running.state = "running"
    store.upsert(running, actor="engine", event="op.transition")
    stage_dir = project.path("staging", running.id)
    stage_dir.mkdir(parents=True)
    (stage_dir / "out.blend").write_bytes(b"half written")
    # 3. promoting: revision file present with the announced hash but no committed entry -> complete the commit
    promoting = o.create(_request(rev0, tok), actor="agent:A")
    promoting.state = "promoting"
    pdir = project.path("staging", promoting.id)
    pdir.mkdir(parents=True)
    content = b"BLENDER-fake-seed|edit:" + promoting.id.encode()
    from builder.ids import new_id, sha256_bytes

    rev_id = new_id("rev")
    rev_file = project.path("revisions", f"{rev_id}.blend")
    rev_file.write_bytes(content)
    promoting.data.update({"pending_revision_id": rev_id, "pending_sha256": sha256_bytes(content),
                           "pending_file": str(rev_file), "validation": {"identity_map": runner.identity_map}})
    store.upsert(promoting, actor="engine", event="op.transition")
    store.append_journal(actor="engine", event="op.promoting", op_id=promoting.id,
                         inputs={"rev_id": rev_id, "sha256": sha256_bytes(content), "file": str(rev_file)})
    # 4. promoting whose file never appeared -> failed
    lost = o.create(_request(rev0, tok), actor="agent:A")
    lost.state = "promoting"
    lost.data.update({"pending_revision_id": "rev_missing", "pending_sha256": "0" * 64,
                      "pending_file": str(project.path("revisions", "rev_missing.blend"))})
    store.upsert(lost, actor="engine", event="op.transition")

    report = o.reconcile_all(actor="engine:recovery")
    classes = {r["op_id"]: r["classification"] for r in report}
    assert classes[never.id] == "never_started"
    assert classes[running.id] == "uncertain"
    assert classes[promoting.id] == "committed"
    assert classes[lost.id] == "failed"
    assert store.get("operation", never.id).state == "cancelled"
    assert store.get("operation", running.id).state == "uncertain"
    assert Path(store.get("operation", running.id).data["quarantine_dir"]).is_dir()
    assert not (stage_dir / "out.blend").exists()
    assert store.get("operation", promoting.id).state == "committed"
    assert store.get("revision", rev_id) is not None and store.verify_file(rev_file)[0]
    assert store.get("operation", lost.id).state == "failed"
    assert runner.calls == []  # nothing was replayed
    assert o.reconcile_all(actor="engine:recovery") == []
