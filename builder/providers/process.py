"""Argv-only subprocess execution with separate timers and process-tree cleanup (spec D3, R-22, R-23).

* ``argv`` is always a list and ``shell`` is never used.
* stdout and stderr are drained concurrently into log files; the tails are kept for diagnostics.
* Three independent timers: total response deadline, inactivity (no output for N seconds while the
  process is alive), and the caller's cancel event. A caller that runs Blender passes only the
  per-operation deadline, so a quiet render is never mistaken for a stalled chat.
* On timeout, inactivity, or cancel the whole process tree is killed: Windows Job Object first,
  ``taskkill /T /F`` as fallback, then every known PID is polled until it is gone. The result records
  whether the kill was confirmed.
* Transport retries are allowed only for calls declared side-effect free (R-23).
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ids import utc_now

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
BASE_ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "NO_COLOR": "1"}
# The same rule as the chat app's relay.clean_env: when Alloy itself runs inside a Claude Code session, the
# inherited CLAUDE*/ANTHROPIC* variables make a nested `claude` CLI believe it has host auth and fail
# "Not logged in". Every child (provider CLIs and Blender alike) starts without them.
HOST_ENV_PREFIXES = ("CLAUDE", "ANTHROPIC")


def child_env(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a child gets: the host's, minus CLAUDE*/ANTHROPIC*, plus BASE_ENV and env_extra."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(HOST_ENV_PREFIXES)}
    env.update(BASE_ENV)
    if env_extra:
        env.update(env_extra)
    return env


class ProcessError(Exception):
    pass


@dataclass
class ProcessResult:
    argv: list[str]
    cwd: str
    outcome: str                      # ok | nonzero_exit | timeout | inactive | cancelled | spawn_failed
    exit_code: int | None
    pid: int
    stdout_path: str
    stderr_path: str
    stdout_tail: str = ""
    stderr_tail: str = ""
    started_at: str = ""
    ended_at: str = ""
    elapsed_s: float = 0.0
    last_output_age_s: float | None = None
    kill_confirmed: bool | None = None
    kill_details: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    job_assigned: bool = False
    pipes_closed: bool = True

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


# --- Windows Job Objects (ctypes, stdlib only) -----------------------------------------

if IS_WINDOWS:  # pragma: no branch
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259


class JobObject:
    """A kill-on-close job. ``assign`` returns False (and the caller falls back to taskkill) when
    the OS refuses, for example when the parent is already in a job that forbids nesting."""

    def __init__(self) -> None:
        self.handle: Any = None
        if not IS_WINDOWS:
            return
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(handle, _JobObjectExtendedLimitInformation, ctypes.byref(info),
                                            ctypes.sizeof(info)):
            _k32.CloseHandle(handle)
            return
        self.handle = handle

    def assign(self, process_handle: int) -> bool:
        if not self.handle:
            return False
        return bool(_k32.AssignProcessToJobObject(self.handle, wintypes.HANDLE(process_handle)))

    def terminate(self) -> bool:
        if not self.handle:
            return False
        return bool(_k32.TerminateJobObject(self.handle, 1))

    def close(self) -> None:
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None


# --- process inspection ---------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def descendants(pid: int, timeout_s: float = 20.0) -> list[int]:
    """Best-effort list of descendant PIDs (Windows: CIM query through PowerShell)."""
    if not IS_WINDOWS:
        return []
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId | ConvertTo-Csv -NoTypeInformation"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=timeout_s, creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in out.splitlines()[1:]:
        parts = [p.strip().strip('"') for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    queue = [pid]
    seen = {pid}
    while queue:
        cur = queue.pop()
        for child in children.get(cur, []):
            if child not in seen:
                seen.add(child)
                found.append(child)
                queue.append(child)
    return found


def command_line(pid: int, timeout_s: float = 20.0) -> str | None:
    """The command line of a live process (Windows: CIM query through PowerShell), or None when unknown. Recovery
    uses it to make sure a recorded pid still belongs to the operation before killing anything (pids are reused)."""
    if pid <= 0 or not IS_WINDOWS:
        return None
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
           f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=timeout_s, creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    text = out.strip()
    return text or None


def kill_tree(pid: int, job: JobObject | None = None, grace_s: float = 10.0) -> dict[str, Any]:
    """Kill ``pid`` and everything below it, then confirm every known PID is gone."""
    details: dict[str, Any] = {"pid": pid, "descendants": [], "job_terminated": False, "taskkill_rc": None}
    try:
        details["descendants"] = descendants(pid)
    except Exception as exc:  # pragma: no cover - diagnostics only
        details["descendants_error"] = str(exc)
    if job is not None:
        details["job_terminated"] = job.terminate()
    if IS_WINDOWS:
        try:
            rc = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                                creationflags=CREATE_NO_WINDOW, timeout=30).returncode
            details["taskkill_rc"] = rc
        except (OSError, subprocess.SubprocessError) as exc:
            details["taskkill_error"] = str(exc)
    else:  # pragma: no cover - Windows is the target platform
        try:
            os.killpg(os.getpgid(pid), 9)
        except OSError as exc:
            details["killpg_error"] = str(exc)
    targets = [pid, *details["descendants"]]
    deadline = time.monotonic() + grace_s
    survivors = targets
    while survivors and time.monotonic() < deadline:
        survivors = [p for p in targets if pid_alive(p)]
        if survivors:
            time.sleep(0.1)
    details["targets"] = targets
    details["survivors"] = survivors
    details["confirmed"] = not survivors
    return details


# --- runner ---------------------------------------------------------------------------

class _DrainState:
    def __init__(self) -> None:
        self.last_output_at = time.monotonic()
        self.tails: dict[str, bytes] = {"stdout": b"", "stderr": b""}
        self.lock = threading.Lock()


def _drain(pipe: Any, path: Path, name: str, state: _DrainState,
           on_output: Callable[[str, str], None] | None) -> None:
    with open(path, "ab") as f:
        while True:
            try:
                chunk = pipe.read(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            f.write(chunk)
            f.flush()
            with state.lock:
                state.last_output_at = time.monotonic()
                state.tails[name] = (state.tails[name] + chunk)[-4096:]
            if on_output is not None:
                try:
                    on_output(name, chunk.decode("utf-8", "replace"))
                except Exception:  # a misbehaving observer must never break the run
                    pass


def run_process(argv: list[str], *, cwd: str | Path, stdout_path: str | Path, stderr_path: str | Path,
                stdin_bytes: bytes | None = None, env_extra: dict[str, str] | None = None,
                response_s: float | None = None, inactivity_s: float | None = None,
                cancel_event: threading.Event | None = None,
                on_output: Callable[[str, str], None] | None = None,
                on_spawn: Callable[[int], None] | None = None,
                poll_s: float = 0.05, kill_grace_s: float = 10.0) -> ProcessResult:
    """``on_spawn(pid)`` is called on the calling thread right after the process exists (and after the Job Object
    assignment), so a caller can record the pid durably while the process runs (R-47 live-process recovery)."""
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    argv = [str(a) for a in argv]
    stdout_path, stderr_path = Path(stdout_path), Path(stderr_path)
    for p in (stdout_path, stderr_path):
        p.parent.mkdir(parents=True, exist_ok=True)
    env = child_env(env_extra)
    started = utc_now()
    t0 = time.monotonic()
    result = ProcessResult(argv=argv, cwd=str(cwd), outcome="spawn_failed", exit_code=None, pid=0,
                           stdout_path=str(stdout_path), stderr_path=str(stderr_path), started_at=started)
    creationflags = (CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if IS_WINDOWS else 0
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env, shell=False, bufsize=0,
            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=creationflags)
    except (OSError, ValueError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        result.ended_at = utc_now()
        result.elapsed_s = time.monotonic() - t0
        return result

    result.pid = proc.pid
    job: JobObject | None = None
    if IS_WINDOWS:
        job = JobObject()
        result.job_assigned = job.assign(proc._handle)  # type: ignore[attr-defined]
    if on_spawn is not None:
        try:
            on_spawn(proc.pid)
        except Exception:  # noqa: BLE001 - a recording failure must never orphan the process we just started
            pass

    state = _DrainState()
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, stdout_path, "stdout", state, on_output), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, stderr_path, "stderr", state, on_output), daemon=True),
    ]
    for t in readers:
        t.start()
    if stdin_bytes is not None:
        def _feed() -> None:
            try:
                proc.stdin.write(stdin_bytes)  # type: ignore[union-attr]
            except (OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdin.close()  # type: ignore[union-attr]
                except (OSError, ValueError):
                    pass
        threading.Thread(target=_feed, daemon=True).start()

    outcome: str | None = None
    while True:
        rc = proc.poll()
        if rc is not None:
            break
        now = time.monotonic()
        if cancel_event is not None and cancel_event.is_set():
            outcome = "cancelled"
            break
        if response_s is not None and now - t0 > response_s:
            outcome = "timeout"
            break
        with state.lock:
            last = state.last_output_at
        if inactivity_s is not None and now - last > inactivity_s:
            outcome = "inactive"
            break
        time.sleep(poll_s)

    if outcome is not None:
        result.kill_details = kill_tree(proc.pid, job, grace_s=kill_grace_s)
        result.kill_confirmed = bool(result.kill_details.get("confirmed"))
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rc = None
        result.outcome = outcome
    else:
        result.outcome = "ok" if rc == 0 else "nonzero_exit"
    result.exit_code = rc
    if job is not None:
        job.close()
    join_deadline = time.monotonic() + (kill_grace_s if outcome else 5.0)
    for t in readers:
        t.join(timeout=max(0.0, join_deadline - time.monotonic()))
    result.pipes_closed = not any(t.is_alive() for t in readers)
    with state.lock:
        result.stdout_tail = state.tails["stdout"].decode("utf-8", "replace")
        result.stderr_tail = state.tails["stderr"].decode("utf-8", "replace")
        result.last_output_age_s = time.monotonic() - state.last_output_at
    result.ended_at = utc_now()
    result.elapsed_s = time.monotonic() - t0
    return result


def retry_transport(call: Callable[[], Any], *, attempts: int = 2, side_effect_free: bool = False,
                    retry_on: tuple[type[BaseException], ...] = (ProcessError,), delay_s: float = 0.0) -> Any:
    """Retry ``call`` only when it is declared side-effect free (R-23). Otherwise run it once."""
    if not side_effect_free:
        attempts = 1
    last: BaseException | None = None
    for i in range(max(1, attempts)):
        try:
            return call()
        except retry_on as exc:
            last = exc
            if i + 1 < attempts and delay_s:
                time.sleep(delay_s)
    assert last is not None
    raise last


def python_executable() -> str:
    return sys.executable
