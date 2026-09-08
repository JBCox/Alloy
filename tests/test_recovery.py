"""R-47 restart recovery and R-23 duplicate-operation prevention (layer D, fake runner and Python child processes):
recorded PIDs and orphan kill with confirmation, interrupted operations classified from files and processes, tasks
and findings returned to a verified state, the uncertain outcome named in the next packet, and no stage oscillation
after a failed correction."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from builder.engine import Engine
from builder.operations import OperationRequest, Operations
from builder.ownership import OwnershipManager
from builder.project import Project
from builder.providers.process import pid_alive, run_process

from test_engine import _adapters, _config, _runner, build_result, project  # noqa: F401
from test_operations import FakeRunner, _request

CHILD = Path(__file__).with_name("_child.py")


# --- process layer: the pid is known while the process runs -------------------------------------------------------

def test_run_process_reports_the_pid_on_spawn_before_the_process_ends(workdir):
    seen: dict[str, object] = {}

    def on_spawn(pid: int) -> None:
        seen["pid"] = pid
        seen["alive_at_spawn"] = pid_alive(pid)

    res = run_process([sys.executable, str(CHILD), "silent", "0.5"], cwd=workdir, stdout_path=workdir / "o.log",
                      stderr_path=workdir / "e.log", response_s=30, on_spawn=on_spawn)
    assert res.outcome == "ok"
    assert seen["pid"] == res.pid and res.pid > 0
    assert seen["alive_at_spawn"] is True


# --- operations: pid recorded, orphans killed and confirmed, interrupted work classified ---------------------------

class SpawningRunner(FakeRunner):
    """Like the fake runner but reports a pid through ``on_spawn`` the way the Blender runner does."""

    def __init__(self):
        super().__init__()
        self.spawn_pid = 4242

    def apply(self, base_blend, out_blend, script_path, *, op_id, declared_effects, deadline_s=None, work_dir=None,
              cancel_event=None, on_spawn=None):
        if on_spawn is not None:
            on_spawn(self.spawn_pid)
        return super().apply(base_blend, out_blend, script_path, op_id=op_id, declared_effects=declared_effects,
                             deadline_s=deadline_s, work_dir=work_dir, cancel_event=cancel_event)


@pytest.fixture
def ops_project(workdir):
    prj = Project.create(workdir / "wf", name="Recovery", asset_name="Lamp")
    runner = SpawningRunner()
    owners = OwnershipManager(prj.store)
    o = Operations(prj, runner, owners)
    src = prj.path("staging", "seed.blend")
    src.write_bytes(b"BLENDER-fake-seed")
    rev0 = o.register_revision(src, parent_revision_id=None, created_by_op_id=None, actor="engine",
                               identity_map=runner.identity_map, note="revision 0")
    yield prj, o, runner, owners, rev0
    prj.close()


def test_operation_records_the_blender_pid_while_running(ops_project):
    prj, o, runner, owners, rev0 = ops_project
    own = owners.acquire("assembly", "agent:A", rev0.id, actor="engine", reason="t")
    op = o.execute(o.create(_request(rev0, own.data["token"]), actor="agent:A"), actor="engine")
    assert op.state == "committed"
    history = prj.store.history("operation", op.id) + [op]
    running = [h for h in history if h.state == "running"]
    assert running and running[-1].data.get("pid") == 4242
    assert any(e.event == "op.spawned" and e.inputs.get("pid") == 4242 for e in prj.store.journal())


def _sleeper(workdir: Path, tag: str) -> subprocess.Popen:
    # a live process whose command line carries the operation id, like a Blender run does through its args path
    return subprocess.Popen([sys.executable, str(CHILD), "silent", "120", tag], cwd=workdir,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_dead(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while pid_alive(pid) and time.time() < deadline:
        time.sleep(0.1)
    return not pid_alive(pid)


def test_recovery_kills_a_live_orphan_whose_command_line_names_the_operation_and_confirms(ops_project, workdir):
    prj, o, runner, owners, rev0 = ops_project
    own = owners.acquire("assembly", "agent:A", rev0.id, actor="engine", reason="t")
    op = o.create(_request(rev0, own.data["token"]), actor="agent:A")
    child = _sleeper(workdir, op.id)
    try:
        op.state = "running"
        op.data["pid"] = child.pid
        prj.store.upsert(op, actor="engine", event="op.transition")
        stage = prj.path("staging", op.id)
        stage.mkdir(parents=True)
        (stage / "out.blend").write_bytes(b"half written")
        report = o.reconcile_all(actor="engine:recovery")
        entry = next(r for r in report if r["op_id"] == op.id)
        assert entry["classification"] == "uncertain"
        assert entry["orphan"]["killed"] is True and entry["orphan"]["confirmed"] is True and entry["orphan"]["pid"] == child.pid
        assert _wait_dead(child.pid)
        rec = prj.store.get("operation", op.id)
        assert rec.state == "uncertain" and Path(rec.data["quarantine_dir"]).is_dir()
        assert any(e.event == "op.orphan_killed" for e in prj.store.journal())
    finally:
        if child.poll() is None:
            child.kill()


def test_recovery_never_kills_a_reused_pid_that_belongs_to_another_program(ops_project, workdir):
    prj, o, runner, owners, rev0 = ops_project
    own = owners.acquire("assembly", "agent:A", rev0.id, actor="engine", reason="t")
    op = o.create(_request(rev0, own.data["token"]), actor="agent:A")
    child = _sleeper(workdir, "unrelated-program")
    try:
        op.state = "running"
        op.data["pid"] = child.pid
        prj.store.upsert(op, actor="engine", event="op.transition")
        prj.path("staging", op.id).mkdir(parents=True)
        report = o.reconcile_all(actor="engine:recovery")
        entry = next(r for r in report if r["op_id"] == op.id)
        assert entry["orphan"]["killed"] is False and "another" in entry["orphan"]["note"]
        assert pid_alive(child.pid)
        # no output was ever produced: the operation is interrupted work, never started geometry
        assert entry["classification"] == "failed" and "interrupted" in entry["note"]
    finally:
        child.kill()


def test_interrupted_running_op_without_output_is_failed_not_never_started(ops_project):
    prj, o, runner, owners, rev0 = ops_project
    own = owners.acquire("assembly", "agent:A", rev0.id, actor="engine", reason="t")
    op = o.create(_request(rev0, own.data["token"]), actor="agent:A")
    op.state = "running"
    op.data["pid"] = 999_999_999          # recorded, long gone
    prj.store.upsert(op, actor="engine", event="op.transition")
    prj.path("staging", op.id).mkdir(parents=True)
    report = o.reconcile_all(actor="engine:recovery")
    entry = next(r for r in report if r["op_id"] == op.id)
    assert entry["classification"] == "failed" and entry["orphan"]["alive"] is False
    assert prj.store.get("operation", op.id).state == "failed"
    assert not prj.path("staging", op.id).exists()
    # an operation that was created but never spawned is the only never_started case
    never = o.create(_request(rev0, own.data["token"]), actor="agent:A")
    report = o.reconcile_all(actor="engine:recovery")
    assert next(r for r in report if r["op_id"] == never.id)["classification"] == "never_started"


# --- engine: tasks and findings return to a verified state; the next packet names the uncertain outcome -----------

def _uncertain_correction(runner):
    """Screenplay item: the correction op ends uncertain (Blender exit 0 without a result), like a crash mid-save."""
    def item(req):
        runner.apply_outcome = "uncertain"
        return build_result("ALLOY.get('p_bracket').location.z -= 0.15", part="p_bracket", assessment="bracket lowered")
    return item


def _run_to_failed_correction(project):
    runner = _runner()
    adapters = _adapters(b_extra={"correction_task": [_uncertain_correction(runner),
                                                       build_result("ALLOY.get('p_bracket').location.z -= 0.15", part="p_bracket")]})
    eng = Engine(project, _config(), adapters=adapters, runner=runner)
    eng.preflight(live=True)
    eng.start()
    status = eng.run_until_stop(max_steps=60)
    assert status["stop_reason"] == "execution_failure" and status["execution"] == "paused"
    f = project.store.list("finding")[0]
    assert f.state == "correcting"
    op = [o for o in project.store.list("operation") if o.state == "uncertain"][0]
    return eng, runner, adapters, f, op


def test_resume_after_a_failed_correction_retries_once_and_does_not_oscillate(project):
    eng, runner, adapters, f, op = _run_to_failed_correction(project)
    runner.apply_outcome = "ok"
    steps_before = len([e for e in project.store.journal() if e.event == "run.stage"])
    eng.resume(user="user:josh")
    status = eng.run_until_stop(max_steps=60)
    assert status["stop_reason"] == "ready_for_user_review", status["notes"]
    stages = [e.inputs.get("stage") for e in project.store.journal() if e.event == "run.stage"][steps_before:]
    assert stages.count("gate") <= 2 and stages.count("correct") <= 2, stages     # no gate/correct ping-pong
    f = project.store.get("finding", f.id)
    assert f.state == "closed"
    assert any("interrupted" in (n.get("note") or "").lower() or "uncertain" in (n.get("note") or "").lower()
               for n in f.data.get("recovery_notes", []))
    attempts = project.store.list("correction_attempt")
    assert [a.state for a in attempts] == ["uncertain", "improved"]
    revs = project.store.list("revision")
    assert len(revs) == 3                                              # seed, build, one correction: nothing duplicated
    assert project.store.get("operation", op.id).state == "uncertain"  # never replayed, never promoted
    packet_dir = Path(next(i for i in adapters["B"].invocations if i.purpose == "correction_task" and i is adapters["B"].invocations[-1]).packet_dir)
    text = (packet_dir / "PACKET.md").read_text(encoding="utf-8")
    assert op.id in text and "never promoted" in text and "quarantin" in text


def test_restart_recovery_resets_the_blocked_task_and_finding_then_resumes_cleanly(project):
    eng, runner, adapters, f, op = _run_to_failed_correction(project)
    # a new engine on the same project: the process that ran the correction is gone
    runner2 = _runner()
    adapters2 = _adapters(b_extra={"correction_task": [build_result("ALLOY.get('p_bracket').location.z -= 0.15", part="p_bracket")]})
    eng2 = Engine(project, _config(), adapters=adapters2, runner=runner2)
    eng2.load_preflight()
    assert eng2.attach().state == "paused"
    report = eng2.recover()
    assert report["operations"] == [] or all(r["classification"] != "uncertain" for r in report["operations"])
    assert report["findings_reset"] == [f.id]
    f2 = project.store.get("finding", f.id)
    assert f2.state == "open" and f2.data["recovery_notes"]
    task = project.store.require("task", f2.data["recovery_notes"][-1]["task_id"])
    assert task.state == "blocked"                     # blocked when the operation failed; recovery keeps it that way
    assert project.store.list("ownership", state="released") or eng2.ownership.current("assembly") is None
    eng2.resume(user="user:josh")
    status = eng2.run_until_stop(max_steps=60)
    assert status["stop_reason"] == "ready_for_user_review", status["notes"]
    assert len(project.store.list("revision")) == 3
    assert project.store.verify_rebuild()


def test_uncertain_build_operation_is_named_in_the_next_build_packet(project):
    runner = _runner()

    def flaky_build(req):
        runner.apply_outcome = "uncertain"
        return build_result("ALLOY.get('p_housing').scale.y = 1.0")

    adapters = _adapters(a_extra={"build_task": [flaky_build, build_result("ALLOY.get('p_housing').scale.y = 1.0")]})
    eng = Engine(project, _config(), adapters=adapters, runner=runner)
    eng.preflight(live=True)
    eng.start()
    status = eng.run_until_stop(max_steps=60)
    assert status["stop_reason"] == "execution_failure"
    op = [o for o in project.store.list("operation") if o.state == "uncertain"][0]
    runner.apply_outcome = "ok"
    eng.resume(user="user:josh")
    status = eng.run_until_stop(max_steps=60)
    assert status["stop_reason"] == "ready_for_user_review", status["notes"]
    second = [i for i in adapters["A"].invocations if i.purpose == "build_task"][-1]
    text = (Path(second.packet_dir) / "PACKET.md").read_text(encoding="utf-8")
    assert op.id in text and "never promoted" in text
    assert len(project.store.list("revision")) == 3
