"""The builder suite's safety net for the unittest-style suites (which pytest's autouse fixture in
tests/builder/conftest.py cannot reach): no test may spawn a real provider CLI unless it only asks for
``--help``/``--version``. Import and call ``install()`` at the top of a suite; it patches ``subprocess.Popen``
for the whole process, which is what a standalone script wants."""
from __future__ import annotations

import subprocess
from pathlib import Path

PROVIDER_EXECUTABLES = ("claude", "gemini", "codex")
FREE_ARGS = ("--help", "--version", "-h", "-V")
_real_popen = subprocess.Popen


class GuardedPopen(_real_popen):  # type: ignore[misc,valid-type]
    def __init__(self, args, *a, **kw):
        argv = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
        head = [Path(x).name.lower() for x in argv[:2]]
        names = [n for n in head if n.startswith(PROVIDER_EXECUTABLES)]
        if names and not any(t in FREE_ARGS for t in argv):
            raise RuntimeError(f"blocked: real provider invocation {argv[:4]} in a token-free suite")
        super().__init__(args, *a, **kw)


def install() -> None:
    subprocess.Popen = GuardedPopen  # type: ignore[misc]
