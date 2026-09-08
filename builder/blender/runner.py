"""BlenderRunner: every Blender invocation is ``blender.exe -b --factory-startup -noaudio --python-exit-code 3
[<file.blend>] --python <script> -- <args.json>`` as an argv list, with its own deadline, process-tree
cleanup, and a JSON result file (spec Section 5, R-22).

Outcome classification for a script run:
* ``ok``          exit 0 and ``result.json`` with ``ok: true`` for this op id
* ``failed``      exit 3 (Python exception), any other non-zero exit, or ``result.json`` with ``ok: false``
* ``uncertain``   exit 0 without a readable result for this op id (never treated as success)
* ``timeout`` / ``cancelled`` / ``spawn_failed`` from the process layer
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import DEFAULT_DEADLINES
from ..providers.process import ProcessResult, run_process

SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
PYTHON_EXIT_CODE = 3


class BlenderError(RuntimeError):
    pass


@dataclass
class BlenderResult:
    op_id: str
    outcome: str
    exit_code: int | None
    result: dict[str, Any] | None
    error: str
    stdout_path: str
    stderr_path: str
    elapsed_s: float
    kill_confirmed: bool | None
    work_dir: str
    process: ProcessResult | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    @property
    def data(self) -> dict[str, Any]:
        return (self.result or {}).get("data") or {}


class BlenderRunner:
    def __init__(self, executable: str | Path, logs_dir: str | Path, deadlines: dict[str, int] | None = None,
                 factory_startup: bool = True):
        self.executable = Path(executable)
        self.logs_dir = Path(logs_dir)
        self.deadlines = dict(DEFAULT_DEADLINES)
        if deadlines:
            self.deadlines.update(deadlines)
        self.factory_startup = factory_startup

    # --- argv and execution ---------------------------------------------------------

    def argv(self, script: str | Path, args_path: str | Path, blend: str | Path | None = None) -> list[str]:
        argv = [str(self.executable), "-b"]
        if self.factory_startup:
            argv.append("--factory-startup")
        argv += ["-noaudio", "--python-exit-code", str(PYTHON_EXIT_CODE)]
        if blend is not None:
            argv.append(str(blend))
        argv += ["--python", str(script), "--", str(args_path)]
        return argv

    def run_script(self, script: str | Path, args: dict[str, Any], *, op_id: str, blend: str | Path | None = None,
                   deadline_s: float | None = None, work_dir: str | Path | None = None,
                   cancel_event: threading.Event | None = None,
                   on_output: Callable[[str, str], None] | None = None,
                   on_spawn: Callable[[int], None] | None = None) -> BlenderResult:
        wd = Path(work_dir) if work_dir is not None else self.logs_dir / op_id
        wd.mkdir(parents=True, exist_ok=True)
        args_path = wd / "args.json"
        result_path = wd / "result.json"
        if result_path.exists():
            result_path.unlink()  # a stale result from an earlier attempt must never count
        full_args = dict(args)
        full_args.update({"_op_id": op_id, "_result_path": str(result_path), "_scripts_dir": str(SCRIPTS_DIR),
                          "_work_dir": str(wd)})
        with open(args_path, "w", encoding="utf-8") as f:
            json.dump(full_args, f, ensure_ascii=False, indent=1)
        pr = run_process(self.argv(script, args_path, blend), cwd=wd, stdout_path=wd / "stdout.log",
                         stderr_path=wd / "stderr.log", response_s=deadline_s, inactivity_s=None,
                         cancel_event=cancel_event, on_output=on_output, on_spawn=on_spawn)
        res = BlenderResult(op_id=op_id, outcome="uncertain", exit_code=pr.exit_code, result=None, error="",
                            stdout_path=pr.stdout_path, stderr_path=pr.stderr_path, elapsed_s=pr.elapsed_s,
                            kill_confirmed=pr.kill_confirmed, work_dir=str(wd), process=pr)
        if pr.outcome == "spawn_failed":
            res.outcome, res.error = "spawn_failed", pr.error
            return res
        if pr.outcome in ("timeout", "cancelled", "inactive"):
            res.outcome = "timeout" if pr.outcome == "inactive" else pr.outcome
            res.error = f"{pr.outcome} after {pr.elapsed_s:.1f}s (deadline {deadline_s}s); kill confirmed={pr.kill_confirmed}"
            return res
        result, read_error = self._read_result(result_path, op_id)
        res.result = result
        if pr.exit_code == PYTHON_EXIT_CODE:
            res.outcome = "failed"
            res.error = self._errors_text(result) or _traceback_tail(pr.stderr_tail) or "Python exception in Blender script"
        elif pr.exit_code != 0:
            res.outcome = "failed"
            res.error = f"blender exited with {pr.exit_code}: {pr.stderr_tail[-800:].strip()}"
        elif result is None:
            res.outcome = "uncertain"
            res.error = read_error or "exit 0 but no result.json was written"
        elif result.get("ok") is True:
            res.outcome = "ok"
        else:
            res.outcome = "failed"
            res.error = self._errors_text(result) or "script reported ok=false without errors"
        return res

    @staticmethod
    def _read_result(path: Path, op_id: str) -> tuple[dict[str, Any] | None, str]:
        if not path.is_file():
            return None, f"exit 0 but no result.json was written at {path}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                result = json.load(f)
        except (OSError, ValueError) as exc:
            return None, f"result.json unreadable: {exc}"
        if not isinstance(result, dict):
            return None, "result.json is not an object"
        if result.get("op_id") != op_id:
            return None, f"result.json belongs to op {result.get('op_id')!r}, expected {op_id!r}"
        return result, ""

    @staticmethod
    def _errors_text(result: dict[str, Any] | None) -> str:
        if not result:
            return ""
        errors = result.get("errors") or []
        return "; ".join(str(e).strip() for e in errors)

    # --- discovery ----------------------------------------------------------------------

    def version(self) -> str:
        pr = run_process([str(self.executable), "-b", "--version"], cwd=self.logs_dir if self.logs_dir.exists() else ".",
                         stdout_path=self.logs_dir / "version.stdout.log", stderr_path=self.logs_dir / "version.stderr.log",
                         response_s=60)
        for line in Path(pr.stdout_path).read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Blender "):
                return line[len("Blender "):].strip()
        raise BlenderError(f"could not read Blender version (outcome {pr.outcome}): {pr.stderr_tail[-300:]}")

    def engines_listed(self) -> list[str]:
        pr = run_process([str(self.executable), "-b", "-E", "help"], cwd=".", stdout_path=self.logs_dir / "engines.stdout.log",
                         stderr_path=self.logs_dir / "engines.stderr.log", response_s=60)
        text = Path(pr.stdout_path).read_text(encoding="utf-8", errors="replace")
        return [t for t in (line.strip() for line in text.splitlines()) if re.fullmatch(r"[A-Z][A-Z0-9_]+", t)]

    def smoke(self) -> dict[str, Any]:
        res = self.run_script(SCRIPTS_DIR / "smoke.py", {}, op_id="smoke", deadline_s=self.deadlines["validate"])
        if not res.ok:
            raise BlenderError(f"Blender smoke test failed ({res.outcome}): {res.error}")
        info = dict(res.data)
        info["engines_listed"] = self.engines_listed()
        info["executable"] = str(self.executable)
        return info

    # --- operations ---------------------------------------------------------------------

    def build_scene(self, out_blend: str | Path, spec: dict[str, Any], *, op_id: str,
                    deadline_s: float | None = None, work_dir: str | Path | None = None) -> BlenderResult:
        return self.run_script(SCRIPTS_DIR / "build_scene.py", {"spec": spec, "out_blend": str(out_blend)}, op_id=op_id,
                               deadline_s=deadline_s or self.deadlines["fixture"], work_dir=work_dir)

    def identities(self, blend: str | Path, *, op_id: str, work_dir: str | Path | None = None) -> BlenderResult:
        return self.run_script(SCRIPTS_DIR / "identities.py", {}, op_id=op_id, blend=blend,
                               deadline_s=self.deadlines["validate"], work_dir=work_dir)

    def validate(self, blend: str | Path, expectations: dict[str, Any], *, op_id: str,
                 work_dir: str | Path | None = None) -> BlenderResult:
        return self.run_script(SCRIPTS_DIR / "validate.py", {"expectations": expectations}, op_id=op_id, blend=blend,
                               deadline_s=self.deadlines["validate"], work_dir=work_dir)

    def apply(self, base_blend: str | Path, out_blend: str | Path, script_path: str | Path, *, op_id: str,
              declared_effects: dict[str, Any], deadline_s: float | None = None,
              work_dir: str | Path | None = None, cancel_event: threading.Event | None = None,
              on_spawn: Callable[[int], None] | None = None) -> BlenderResult:
        args = {"script_path": str(script_path), "out_blend": str(out_blend), "declared_effects": declared_effects}
        return self.run_script(SCRIPTS_DIR / "apply_operation.py", args, op_id=op_id, blend=base_blend,
                               deadline_s=deadline_s or self.deadlines["apply"], work_dir=work_dir,
                               cancel_event=cancel_event, on_spawn=on_spawn)

    def render(self, blend: str | Path, view: dict[str, Any], out_png: str | Path, *, op_id: str,
               deadline_s: float | None = None, work_dir: str | Path | None = None,
               cancel_event: threading.Event | None = None) -> BlenderResult:
        return self.run_script(SCRIPTS_DIR / "render.py", {"view": view, "out_png": str(out_png)}, op_id=op_id,
                               blend=blend, deadline_s=deadline_s or self.deadlines["render"], work_dir=work_dir,
                               cancel_event=cancel_event)

    def measure(self, blend: str | Path, requests: dict[str, Any], *, op_id: str,
                work_dir: str | Path | None = None) -> BlenderResult:
        return self.run_script(SCRIPTS_DIR / "measure.py", {"requests": requests}, op_id=op_id, blend=blend,
                               deadline_s=self.deadlines["measure"], work_dir=work_dir)


def _traceback_tail(stderr_tail: str, max_lines: int = 12) -> str:
    lines = [ln for ln in stderr_tail.splitlines() if ln.strip()]
    return "\n".join(lines[-max_lines:]).strip()


def script_sha256(name: str) -> str:
    from ..ids import sha256_file

    return sha256_file(SCRIPTS_DIR / name)
