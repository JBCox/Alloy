"""Operation lifecycle: create, stage, run in Blender, validate in a fresh Blender, promote atomically to
an immutable revision, and reconcile in-flight operations after a restart (spec D4, Section 5,
R-23, R-37, R-41, R-45, R-47, R-48).

Only this module writes to ``revisions/``. Agent scripts run against a staged copy; nothing an agent
returns is executed before ownership, base revision, file hash, and script screening all pass.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ids import new_id, sha256_file, utc_now
from .ownership import OwnershipError, OwnershipManager
from .project import Project
from .providers.process import command_line, kill_tree, pid_alive
from .records import Record

IN_FLIGHT_STATES = ("created", "staged", "running", "validating", "promoting")

_SCREEN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*(?:import|from)\s+(os|subprocess|shutil|pathlib|sys|ctypes|socket|urllib|http|importlib|"
                r"builtins|io|tempfile|glob|multiprocessing|threading|pickle|marshal|code|runpy)\b"),
     "forbidden module {0}"),
    (re.compile(r"\bopen\s*\("), "forbidden open( call: agents never write files, Alloy saves"),
    (re.compile(r"\bexec\s*\("), "forbidden exec( call"),
    (re.compile(r"\beval\s*\("), "forbidden eval( call"),
    (re.compile(r"\bcompile\s*\("), "forbidden compile( call"),
    (re.compile(r"__import__"), "forbidden __import__"),
    (re.compile(r"bpy\.ops\.wm\.(?:save\w*|open\w*|link|append|read\w*|recover\w*|revert\w*|quit\w*)"),
     "forbidden call {0}: only Alloy saves, opens, links, or appends"),
    (re.compile(r"bpy\.data\.libraries"), "forbidden bpy.data.libraries access"),
    (re.compile(r"bpy\.app\.(?:handlers|timers)"), "forbidden {0}"),
]


def screen_script(source: str) -> list[str]:
    """Static guardrail (not a sandbox, see the design doc): reject scripts that reach outside Blender data."""
    problems: list[str] = []
    for lineno, line in enumerate(source.splitlines(), 1):
        code = line.split("#", 1)[0]
        if not code.strip():
            continue
        for pattern, label in _SCREEN_PATTERNS:
            m = pattern.search(code)
            if m:
                detail = m.group(1) if m.groups() else m.group(0)
                problems.append(f"line {lineno}: {label.format(detail)}")
    return problems


class OperationError(Exception):
    pass


@dataclass
class OperationRequest:
    kind: str
    task_id: str
    agent_id: str
    intent: str
    expected_outcome: str
    target_part_ids: list[str]
    declared_effects: dict[str, list[str]]
    script_source: str
    expected_base_revision_id: str
    ownership_token: str
    resource: str = "assembly"
    deadline_s: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Operations:
    def __init__(self, project: Project, runner: Any, ownership: OwnershipManager, *, run_id: str | None = None):
        self.project = project
        self.store = project.store
        self.runner = runner
        self.ownership = ownership
        self.run_id = run_id
        self.revisions_dir = project.path("revisions")
        self.staging_dir = project.path("staging")
        self.ops_dir = project.path("ops")
        self.logs_dir = project.path("logs")

    # --- revisions ------------------------------------------------------------------

    def latest_revision(self) -> Record | None:
        revs = self.store.list("revision")
        return revs[-1] if revs else None

    def register_revision(self, source_file: str | Path, *, parent_revision_id: str | None, created_by_op_id: str | None,
                          actor: str, identity_map: list[dict[str, Any]] | None = None,
                          asset_dependencies: dict[str, str] | None = None, note: str = "",
                          revision_id: str | None = None, move: bool = False,
                          extra: dict[str, Any] | None = None) -> Record:
        rev_id = revision_id or new_id("rev")
        dest = self.revisions_dir / f"{rev_id}.blend"
        if dest.exists():
            raise OperationError(f"revision file already exists: {dest}")
        self.revisions_dir.mkdir(parents=True, exist_ok=True)
        if move:
            os.replace(source_file, dest)
        else:
            shutil.copyfile(source_file, dest)
        return self._finalize_revision(dest, rev_id, parent_revision_id=parent_revision_id, created_by_op_id=created_by_op_id,
                                       actor=actor, identity_map=identity_map or [], asset_dependencies=asset_dependencies or {},
                                       note=note, extra=extra)

    def _finalize_revision(self, dest: Path, rev_id: str, *, parent_revision_id: str | None, created_by_op_id: str | None,
                           actor: str, identity_map: list[dict[str, Any]], asset_dependencies: dict[str, str],
                           note: str, expected_sha256: str | None = None, extra: dict[str, Any] | None = None) -> Record:
        os.chmod(dest, stat.S_IREAD)  # immutable revision (R-40)
        digest = sha256_file(dest)
        if expected_sha256 is not None and digest != expected_sha256:
            raise OperationError(f"promoted file hash {digest} differs from validated output {expected_sha256}")
        rec = Record.new("revision", {
            "file": str(dest), "sha256": digest, "size": dest.stat().st_size, "parent_revision_id": parent_revision_id,
            "created_by_op_id": created_by_op_id, "identity_map": identity_map,
            "asset_dependencies": asset_dependencies, "note": note, "immutable": True, "created_at": utc_now(),
            **(extra or {}),
        }, id=rev_id)
        rec = self.store.upsert(rec, actor=actor, event="revision.created", op_id=created_by_op_id, run_id=self.run_id)
        self.store.register_file(dest, kind="revision", record_id=rev_id)
        return rec

    # --- operations -------------------------------------------------------------------

    def create(self, req: OperationRequest, *, actor: str) -> Record:
        op = Record.new("operation", {
            "task_id": req.task_id, "agent_id": req.agent_id, "kind": req.kind,
            "expected_base_revision_id": req.expected_base_revision_id, "intent": req.intent,
            "expected_outcome": req.expected_outcome, "target_part_ids": list(req.target_part_ids),
            "declared_effects": {k: list(v) for k, v in (req.declared_effects or {}).items()},
            "ownership_token": req.ownership_token, "resource": req.resource, "deadline_s": req.deadline_s,
            "script_path": None, "error": "", "created_at": utc_now(), **req.extra,
        })
        op_dir = self.ops_dir / op.id
        op_dir.mkdir(parents=True, exist_ok=True)
        script_path = op_dir / "script.py"
        with open(script_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(req.script_source)
        op.data["script_path"] = str(script_path)
        op.data["script_sha256"] = sha256_file(script_path)
        problems = screen_script(req.script_source) if req.kind == "apply_script" else []
        if problems:
            op.state = "rejected"
            op.data["screening"] = problems
            op.data["error"] = "script screening: " + "; ".join(problems)
            return self.store.upsert(op, actor=actor, event="op.rejected", inputs={"screening": problems}, run_id=self.run_id)
        return self.store.upsert(op, actor=actor, event="op.created", run_id=self.run_id)

    def _set_state(self, op: Record, state: str, *, actor: str, event: str = "op.transition", **inputs: Any) -> Record:
        op.state = state
        return self.store.upsert(op, actor=actor, event=event, inputs=inputs or None, outcome=state, op_id=op.id,
                                 run_id=self.run_id)

    def _reject(self, op: Record, error: str, *, actor: str) -> Record:
        op.data["error"] = error
        return self._set_state(op, "rejected", actor=actor, event="op.rejected", error=error)

    def _fail(self, op: Record, error: str, *, actor: str, state: str = "failed") -> Record:
        op.data["error"] = error
        return self._set_state(op, state, actor=actor, event=f"op.{state}", error=error)

    def _quarantine(self, op: Record) -> Path:
        src = self.staging_dir / op.id
        dest = self.staging_dir / "_quarantine" / op.id
        dest.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            for item in list(src.iterdir()):
                shutil.move(str(item), str(dest / item.name))
            shutil.rmtree(src, ignore_errors=True)
        op.data["quarantine_dir"] = str(dest)
        return dest

    def execute(self, op: Record, *, actor: str, cancel_event: threading.Event | None = None) -> Record:
        if op.state != "created":
            raise OperationError(f"operation {op.id} is {op.state}, not created")
        token = op.data["ownership_token"]
        base_id = op.data["expected_base_revision_id"]
        # Boundary checks first: ownership, base revision, file integrity (R-41, R-45).
        try:
            self.ownership.check(token, op.data.get("resource", "assembly"), base_id)
        except OwnershipError as exc:
            return self._reject(op, f"ownership: {exc}", actor=actor)
        base = self.store.get("revision", base_id)
        if base is None:
            return self._reject(op, f"base revision {base_id} not found", actor=actor)
        ok, expected, actual = self.store.verify_file(base.data["file"])
        if not ok:
            self.store.append_journal(actor=actor, event="revision.external_modification", op_id=op.id,
                                      inputs={"revision_id": base.id, "expected_sha256": expected, "actual_sha256": actual,
                                              "file": base.data["file"]}, run_id=self.run_id)
            return self._reject(op, f"revision {base.id} file hash mismatch (expected {expected}, found {actual}): "
                                    "external modification detected; reconcile it explicitly before continuing", actor=actor)
        # Stage a working copy of the base revision.
        stage_dir = self.staging_dir / op.id
        stage_dir.mkdir(parents=True, exist_ok=True)
        base_copy = stage_dir / "base.blend"
        shutil.copyfile(base.data["file"], base_copy)
        os.chmod(base_copy, stat.S_IWRITE | stat.S_IREAD)
        if sha256_file(base_copy) != expected:
            return self._fail(op, "staged copy hash differs from the revision; disk problem (R-48)", actor=actor)
        op.data["staging_dir"] = str(stage_dir)
        op = self._set_state(op, "staged", actor=actor)
        # Run the agent script inside Blender against the staged copy.
        out = stage_dir / "out.blend"
        logs = self.logs_dir / op.id
        op = self._set_state(op, "running", actor=actor)

        def on_spawn(pid: int) -> None:
            # durable while Blender runs, so a restart can find, kill, and confirm an orphan (R-47)
            op.data["pid"] = int(pid)
            self.store.upsert(op, actor=actor, event="op.spawned", inputs={"pid": int(pid)}, op_id=op.id, run_id=self.run_id)

        res = self.runner.apply(base_copy, out, op.data["script_path"], op_id=op.id,
                                declared_effects=op.data.get("declared_effects") or {},
                                deadline_s=op.data.get("deadline_s"), work_dir=logs, cancel_event=cancel_event,
                                on_spawn=on_spawn)
        op.data["pid"] = None                     # the process is gone once apply returns (killed and confirmed, or exited)
        op.data["apply"] = {"outcome": res.outcome, "exit_code": res.exit_code, "error": res.error,
                            "stdout_path": res.stdout_path, "stderr_path": res.stderr_path, "elapsed_s": res.elapsed_s,
                            "kill_confirmed": res.kill_confirmed}
        if res.outcome in ("timeout", "cancelled", "uncertain", "inactive"):
            self._quarantine(op)
            state = "cancelled" if res.outcome == "cancelled" else "uncertain"
            return self._fail(op, f"apply {res.outcome}: {res.error}; staged output quarantined for diagnosis", actor=actor,
                              state=state)
        if res.outcome != "ok":
            op.data["staged_output"] = str(out) if out.exists() else None
            return self._fail(op, f"apply {res.outcome}: {res.error}", actor=actor)
        # Validate in a separate Blender process (proves the file reopens).
        op = self._set_state(op, "validating", actor=actor)
        effects = op.data.get("declared_effects") or {}
        expect_present = sorted(set(effects.get("modifies", [])) | set(effects.get("creates", []))
                                | set(op.data.get("target_part_ids") or []))
        expectations = {"base_identity_map": res.data.get("base_identity_map") or base.data.get("identity_map") or [],
                        "expect_present": [i for i in expect_present if i not in set(effects.get("deletes", []))],
                        "expect_absent": list(effects.get("deletes", [])), "allow_unmapped": False}
        vres = self.runner.validate(out, expectations, op_id=op.id, work_dir=logs)
        problems: list[str] = []
        if vres.outcome != "ok":
            problems.append(vres.error or f"validation {vres.outcome}")
        apply_orphans = res.data.get("orphans_before_save") or []
        if apply_orphans:
            problems.append(f"objects left without a collection (would be lost on save): {', '.join(apply_orphans)}")
        op.data["validation"] = dict(vres.data)
        op.data["validation"]["apply_orphans"] = apply_orphans
        if problems:
            op.data["staged_output"] = str(out)
            return self._reject(op, "validation failed: " + "; ".join(problems), actor=actor)
        # Promote atomically: announce, move, commit.
        digest = sha256_file(out)
        rev_id = new_id("rev")
        dest = self.revisions_dir / f"{rev_id}.blend"
        op.data.update({"pending_revision_id": rev_id, "pending_sha256": digest, "pending_file": str(dest)})
        op = self._set_state(op, "promoting", actor=actor)
        self.store.append_journal(actor=actor, event="op.promoting", op_id=op.id,
                                  inputs={"rev_id": rev_id, "sha256": digest, "file": str(dest)}, run_id=self.run_id)
        os.replace(out, dest)
        return self._commit(op, dest, rev_id, digest, base.id, actor=actor,
                            identity_map=vres.data.get("identity_map") or [],
                            asset_dependencies=_asset_hashes(vres.data.get("external_assets") or []))

    def _commit(self, op: Record, dest: Path, rev_id: str, digest: str, parent_id: str, *, actor: str,
                identity_map: list[dict[str, Any]], asset_dependencies: dict[str, str]) -> Record:
        if self.store.get("revision", rev_id) is None:
            try:
                self._finalize_revision(dest, rev_id, parent_revision_id=parent_id, created_by_op_id=op.id, actor=actor,
                                        identity_map=identity_map, asset_dependencies=asset_dependencies,
                                        note=op.data.get("intent", ""), expected_sha256=digest)
            except OperationError as exc:
                return self._fail(op, str(exc), actor=actor)
        op.data["result_revision_id"] = rev_id
        op.data["committed_at"] = utc_now()
        op = self._set_state(op, "committed", actor=actor, event="op.committed", revision_id=rev_id, sha256=digest)
        try:
            self.ownership.advance(op.data["ownership_token"], rev_id, actor=actor, reason=f"{op.id} committed")
        except OwnershipError:
            pass  # recovery path: the holder may already have moved on
        shutil.rmtree(self.staging_dir / op.id, ignore_errors=True)
        return op

    # --- restart reconciliation (R-47) -------------------------------------------------

    def reconcile_all(self, *, actor: str) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        for op in self.store.list("operation"):
            if op.state not in IN_FLIGHT_STATES:
                continue
            stage_dir = self.staging_dir / op.id
            out = stage_dir / "out.blend"
            if op.state == "promoting":
                pending_file = Path(op.data.get("pending_file") or "")
                pending_sha = op.data.get("pending_sha256")
                if pending_file.is_file() and pending_sha and sha256_file(pending_file) == pending_sha:
                    self._commit(op, pending_file, op.data["pending_revision_id"], pending_sha,
                                 op.data["expected_base_revision_id"], actor=actor,
                                 identity_map=(op.data.get("validation") or {}).get("identity_map") or [],
                                 asset_dependencies=_asset_hashes((op.data.get("validation") or {}).get("external_assets") or []))
                    report.append({"op_id": op.id, "classification": "committed",
                                   "note": "revision file present with the announced hash; commit completed"})
                else:
                    if pending_file.is_file():
                        os.chmod(pending_file, stat.S_IWRITE | stat.S_IREAD)
                        q = self.staging_dir / "_quarantine" / op.id
                        q.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(pending_file), str(q / pending_file.name))
                        op.data["quarantine_dir"] = str(q)
                    self._fail(op, "promotion did not complete: announced revision file missing or hash mismatch", actor=actor)
                    report.append({"op_id": op.id, "classification": "failed", "note": op.data["error"]})
                continue
            orphan = self._reap_orphan(op, actor=actor)
            interrupted_state = op.state
            if out.exists():
                self._quarantine(op)
                self._fail(op, f"interrupted while {interrupted_state}: staged output exists without a verified result; "
                               "quarantined, never replayed", actor=actor, state="uncertain")
                report.append({"op_id": op.id, "classification": "uncertain", "note": op.data["error"], "orphan": orphan})
            elif op.data.get("pid") or op.state in ("running", "validating"):
                # the process had been spawned (or the state says so) and died without writing an output: interrupted
                # work with nothing to promote; the base revision is untouched, so the task may be redone
                if stage_dir.exists():
                    shutil.rmtree(stage_dir, ignore_errors=True)
                op.data["recovery"] = "interrupted"
                self._fail(op, f"interrupted while {interrupted_state} before any output was written (pid {op.data.get('pid')}); "
                               "nothing was promoted; the base revision is unchanged", actor=actor)
                report.append({"op_id": op.id, "classification": "failed", "note": op.data["error"], "orphan": orphan})
            else:
                if stage_dir.exists():
                    shutil.rmtree(stage_dir, ignore_errors=True)
                op.data["error"] = "never started (no staged output); safe to recreate"
                op.data["recovery"] = "never_started"
                self._set_state(op, "cancelled", actor=actor, event="op.recovered", classification="never_started")
                report.append({"op_id": op.id, "classification": "never_started", "note": op.data["error"], "orphan": orphan})
        return report

    def _reap_orphan(self, op: Record, *, actor: str) -> dict[str, Any]:
        """Kill a Blender process the previous engine left behind, but only when its command line still names this
        operation (pids are reused); confirm it is gone (R-22, R-47)."""
        pid = int(op.data.get("pid") or 0)
        info: dict[str, Any] = {"pid": pid or None, "alive": False, "killed": False, "confirmed": None, "note": ""}
        if not pid:
            info["note"] = "no pid recorded"
            return info
        if not pid_alive(pid):
            info["note"] = "process already gone"
            return info
        info["alive"] = True
        cmdline = command_line(pid) or ""
        if op.id not in cmdline:
            info["note"] = (f"pid {pid} is alive but its command line does not name {op.id}: it belongs to another program "
                            "(pid reuse); not killed")
            self.store.append_journal(actor=actor, event="op.orphan_not_killed", op_id=op.id,
                                      inputs={"pid": pid, "command_line": cmdline[:300]}, run_id=self.run_id)
            return info
        details = kill_tree(pid)
        info.update({"killed": True, "confirmed": bool(details.get("confirmed")), "note": "orphaned process killed",
                     "details": {k: details.get(k) for k in ("descendants", "job_terminated", "taskkill_rc", "survivors")}})
        self.store.append_journal(actor=actor, event="op.orphan_killed", op_id=op.id,
                                  inputs={"pid": pid, "confirmed": info["confirmed"], "survivors": details.get("survivors")},
                                  run_id=self.run_id)
        return info


def _asset_hashes(assets: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in assets:
        path = a.get("path")
        if not path:
            continue
        try:
            out[path] = sha256_file(path) if a.get("exists") and not a.get("packed") else ("packed" if a.get("packed") else "missing")
        except OSError:
            out[path] = "unreadable"
    return out
