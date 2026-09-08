"""Shared fixtures for the Collaborative Model Builder test suite (pytest only: ``python -m pytest tests/builder``).

Layers (spec Section 6):
  D  deterministic: mock providers, no Blender, no network (default)
  B  real Blender: marked ``blender``; skipped with a stated reason when absent
  L  live: marked ``live``; opt-in with ALLOY_LIVE=1; spends provider usage

Markers are registered in the repo's pytest.ini (one source). The suite lives two levels below the repo
root, so ROOT is ``parents[2]``; the bare ``pytest`` invocation relies on this insertion.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


PROVIDER_EXECUTABLES = ("claude", "gemini", "codex")
FREE_ARGS = ("--help", "--version", "-h", "-V")


@pytest.fixture(autouse=True)
def _no_live_provider_calls(request, monkeypatch):
    """Safety net: no test may spawn a real provider CLI unless it is marked ``live`` (spends money) or only
    asks for ``--help``/``--version``. Applies to the argv head (the exe, or node plus the npm bundle)."""
    if request.node.get_closest_marker("live"):
        return
    import subprocess

    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, args, *a, **kw):
            argv = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
            head = [Path(x).name.lower() for x in argv[:2]]
            names = [n for n in head if n.startswith(PROVIDER_EXECUTABLES)]
            if names and not any(t in FREE_ARGS for t in argv):
                raise RuntimeError(f"blocked: real provider invocation {argv[:4]} in a test not marked live")
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)


def _find_blender() -> Path | None:
    from builder.config import discover_blender  # local import: builder may not exist yet at collection time

    return discover_blender(os.environ.get("ALLOY_BLENDER_EXE", ""))


@pytest.fixture(scope="session")
def blender_exe() -> Path:
    try:
        exe = _find_blender()
    except Exception as exc:  # pragma: no cover - only when builder.config is missing
        pytest.skip(f"builder.config unavailable: {exc}")
    if exe is None:
        pytest.skip(
            "Blender executable not found (set ALLOY_BLENDER_EXE or install under "
            r"C:\Program Files\Blender Foundation\Blender *\blender.exe)"
        )
    return exe


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A disposable directory whose name contains spaces and non-Latin characters."""
    d = tmp_path / "wörk dir 名"
    d.mkdir()
    return d
