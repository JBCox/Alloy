"""R-22 separate timers, concurrent drains, process-tree kill with confirmation; R-23 transport retry policy; D3 argv only."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from builder.providers.process import (
    ProcessError,
    pid_alive,
    retry_transport,
    run_process,
)

CHILD = Path(__file__).with_name("_child.py")


def _run(workdir, mode, *args, **kw):
    kw.setdefault("cwd", workdir)
    return run_process([sys.executable, str(CHILD), mode, *args],
                       stdout_path=workdir / "out.log", stderr_path=workdir / "err.log", **kw)


def test_captures_output_exit_code_and_passes_awkward_args(workdir):
    awkward = [r"C:\tmp dir\ü nï 名.json", 'a&b|c^d%e "q" \'sq\'', "--flag=with space"]
    res = _run(workdir, "echo", *awkward, stdin_bytes="héllo\n".encode("utf-8"),
               env_extra={"ALLOY_TEST_MARKER": "m1"})
    assert res.outcome == "ok" and res.exit_code == 0
    payload = json.loads(Path(res.stdout_path).read_text(encoding="utf-8").splitlines()[0])
    assert payload["args"] == awkward
    assert payload["stdin"] == "héllo\n"
    assert payload["env_marker"] == "m1"
    assert Path(payload["cwd"]).resolve() == workdir.resolve()
    assert "to stderr ünï" in Path(res.stderr_path).read_text(encoding="utf-8")
    assert res.elapsed_s >= 0 and res.pid > 0 and res.started_at.endswith("Z")


def test_nonzero_exit_is_reported_not_swallowed(workdir):
    res = _run(workdir, "echo", env_extra={"ALLOY_TEST_EXIT": "7"})
    assert res.outcome == "nonzero_exit" and res.exit_code == 7
    assert Path(res.stdout_path).stat().st_size > 0  # output present, still not success


def test_response_deadline_kills_process_and_confirms(workdir):
    t0 = time.time()
    res = _run(workdir, "slow", "60", response_s=1.0, inactivity_s=30)
    assert res.outcome == "timeout"
    assert time.time() - t0 < 15
    assert res.kill_confirmed is True
    assert not pid_alive(res.pid)


def test_inactivity_detection_fires_independently_of_deadline(workdir):
    t0 = time.time()
    res = _run(workdir, "silent", "60", response_s=60, inactivity_s=1.0)
    assert res.outcome == "inactive"
    assert time.time() - t0 < 15
    assert res.kill_confirmed is True


def test_steady_output_is_not_inactivity(workdir):
    res = _run(workdir, "slow", "2", response_s=30, inactivity_s=1.0)
    assert res.outcome == "ok"
    assert "tick" in Path(res.stdout_path).read_text(encoding="utf-8")


def test_no_timers_means_no_timeout(workdir):
    res = _run(workdir, "slow", "1")
    assert res.outcome == "ok"


def test_cancel_kills_whole_tree_and_confirms_grandchild_gone(workdir):
    cancel = threading.Event()
    seen: dict[str, int] = {}

    def on_output(stream: str, text: str) -> None:
        for line in text.splitlines():
            if line.startswith("GRANDCHILD "):
                seen["pid"] = int(line.split()[1])
                cancel.set()

    res = _run(workdir, "spawn", cancel_event=cancel, on_output=on_output, response_s=60)
    assert res.outcome == "cancelled"
    assert "pid" in seen, "grandchild pid was never observed via on_output"
    assert res.kill_confirmed is True
    assert not pid_alive(res.pid)
    deadline = time.time() + 10
    while pid_alive(seen["pid"]) and time.time() < deadline:
        time.sleep(0.2)
    assert not pid_alive(seen["pid"]), "grandchild survived the tree kill"


def test_large_interleaved_output_does_not_deadlock(workdir):
    res = _run(workdir, "flood", response_s=60)
    assert res.outcome == "ok"
    assert Path(res.stdout_path).stat().st_size == 2048 * 1024
    assert Path(res.stderr_path).stat().st_size == 2048 * 1024


def test_never_uses_shell(workdir, monkeypatch):
    calls = []
    real_popen = subprocess.Popen

    class SpyPopen(real_popen):
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", SpyPopen)
    _run(workdir, "echo")
    assert calls
    args, kwargs = calls[0]
    assert isinstance(args[0], list)
    assert kwargs.get("shell", False) is False


def test_spawn_failure_is_reported(workdir):
    res = run_process([str(workdir / "does-not-exist.exe")], cwd=workdir,
                      stdout_path=workdir / "o.log", stderr_path=workdir / "e.log")
    assert res.outcome == "spawn_failed" and res.exit_code is None and res.error


def test_transport_retry_only_for_side_effect_free_calls():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ProcessError("transient")
        return "ok"

    assert retry_transport(flaky, attempts=3, side_effect_free=True) == "ok"
    assert attempts["n"] == 3
    attempts["n"] = 0
    with pytest.raises(ProcessError):
        retry_transport(flaky, attempts=3, side_effect_free=False)
    assert attempts["n"] == 1
