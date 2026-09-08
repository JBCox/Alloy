"""R-47 restart recovery on a real Blender (layer B): the engine process dies at the edit, save, and commit boundaries
of an operation; reopening classifies each from the journal, staging, revisions, and live processes, never replays a
mutation, keeps every uncertain output for diagnosis, and resumes to a verified state with no duplicate geometry."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from builder.blender.runner import BlenderRunner
from builder.config import BuilderConfig
from builder.engine import Engine
from builder.fixture import create_fixture
from builder.providers.mock import ScriptedAdapter
from builder.providers.process import kill_tree, pid_alive
from builder.store import Store

from _screenplay import screenplay

pytestmark = pytest.mark.blender

CHILD = Path(__file__).with_name("_recovery_child.py")


@pytest.fixture
def runner(blender_exe, workdir):
    return BlenderRunner(blender_exe, logs_dir=workdir / "logs")


def _config():
    cfg = BuilderConfig.from_dict({"builder": {"limits": {"attempts_per_finding": 2}}})
    cfg.agents["A"].provider = cfg.agents["B"].provider = "mock"
    return cfg


def _adapters(play):
    return {label: ScriptedAdapter(label, play[label]) for label in ("A", "B")}


def _spawn_child(wf: Path, point: str, exe: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(CHILD), str(wf), point, str(exe)], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", cwd=str(wf))


def _wait_for_running_op(wf: Path, timeout: float = 90.0) -> tuple[str, int]:
    """Poll the store (a second reader on the WAL database) until an operation is running with a recorded pid."""
    deadline = time.time() + timeout
    with Store(wf / "builder.sqlite3") as store:
        while time.time() < deadline:
            for op in store.list("operation"):
                if op.state == "running" and op.data.get("pid"):
                    return op.id, int(op.data["pid"])
            time.sleep(0.3)
    raise AssertionError("no running operation with a pid appeared")


def test_restart_at_edit_save_and_commit_boundaries_recovers_without_duplicate_geometry(runner, blender_exe, workdir):
    project, info = create_fixture(workdir / "fixture", runner)
    wf = project.workflow_dir
    # the engine reaches the build stage (intake, brief, plan: scripted seats, no Blender) and "crashes" there
    eng = Engine(project, _config(), adapters=_adapters(screenplay()), runner=runner)
    eng.preflight(live=True)
    eng.start()
    eng.run_until_stop(max_steps=3)
    assert eng.run.state == "running" and eng.run.data["stage"] == "build"
    base_rev = eng.operations.latest_revision()
    base_ids = {e["alloy_id"] for e in base_rev.data["identity_map"]}
    project.close()

    # 1. edit boundary: the host dies while the agent script runs inside Blender (no output yet)
    child = _spawn_child(wf, "edit", blender_exe)
    op_edit, blender_pid = _wait_for_running_op(wf)
    time.sleep(2.0)
    assert pid_alive(blender_pid)
    kill_tree(child.pid)                                  # the host and (through the Job Object) its Blender
    child.wait(timeout=30)

    # 2. save boundary: Blender saved and reported; the host dies before validation
    child = _spawn_child(wf, "save", blender_exe)
    out, _ = child.communicate(timeout=180)
    assert "SAVED ok" in out, out
    op_save = [ln.split()[1] for ln in out.splitlines() if ln.startswith("OP ")][0]

    # 3. commit boundary: the revision file was moved; the host dies before the commit record
    child = _spawn_child(wf, "commit", blender_exe)
    out, _ = child.communicate(timeout=180)
    assert "PROMOTED" in out, out
    op_commit = [ln.split()[1] for ln in out.splitlines() if ln.startswith("OP ")][0]

    # --- reopen: classify, never replay, keep uncertain outputs, complete the announced commit ---------------------
    project = __import__("builder.project", fromlist=["Project"]).Project.open(wf)
    try:
        eng2 = Engine(project, _config(), adapters=_adapters(screenplay()), runner=runner)
        eng2.load_preflight()
        assert eng2.attach().state == "running"          # the store still says running: the previous engine died
        report = eng2.recover()
        classes = {r["op_id"]: r for r in report["operations"]}
        assert classes[op_edit]["classification"] == "failed" and "interrupted" in classes[op_edit]["note"]
        assert classes[op_edit]["orphan"]["alive"] is False           # killed with the host through the Job Object
        assert not pid_alive(blender_pid)
        assert classes[op_save]["classification"] == "uncertain"
        assert classes[op_commit]["classification"] == "committed"
        store = project.store
        saved = store.get("operation", op_save)
        q = Path(saved.data["quarantine_dir"])
        assert (q / "out.blend").is_file()                              # preserved for diagnosis, never promoted
        assert runner.identities(q / "out.blend", op_id="ids_quarantine").ok   # it is a complete .blend that reopens
        committed = store.get("operation", op_commit)
        rev_commit = store.get("revision", committed.data["result_revision_id"])
        assert rev_commit is not None and rev_commit.data["parent_revision_id"] == base_rev.id
        assert store.verify_file(rev_commit.data["file"])[0]
        assert runner.identities(rev_commit.data["file"], op_id="ids_commit").ok
        assert eng2.run.state == "paused" and eng2.run.data["stop_reason"] == "execution_failure"
        assert eng2.ownership.current("assembly") is None or report["tasks_reset"] == []
        # no mutation was replayed: exactly the committed child revision was added
        assert len(store.list("revision")) == 2

        # --- resume: the loop continues from the verified state to the gate -----------------------------------------
        eng2.resume(user="user:test")
        status = eng2.run_until_stop(max_steps=60)
        assert status["stop_reason"] == "ready_for_user_review", status["notes"]
        revs = store.list("revision")
        assert len(revs) == 4                                            # seed, committed child op, A's build, B's correction
        final_ids = runner.identities(revs[-1].data["file"], op_id="ids_final").data["identity_map"]
        ids = [e["alloy_id"] for e in final_ids]
        assert len(ids) == len(set(ids))                                 # no duplicate identities
        assert "p_extra" not in ids                                      # the uncertain save-boundary output never entered canon
        assert set(ids) >= base_ids
        # the interrupted operations stay exactly as classified: never replayed, never promoted (R-23, R-47)
        assert store.get("operation", op_edit).state == "failed" and store.get("operation", op_save).state == "uncertain"
        assert (q / "out.blend").is_file()
        assert store.verify_rebuild()
    finally:
        project.close()
