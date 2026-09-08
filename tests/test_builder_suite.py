"""The Model Builder's pytest suite (tests/builder) as one run_all.py entry.

Runs pytest in a subprocess and ends with the `N passed, M failed` line run_all.py's CUSTOM_COUNT regex
reads. The real-Blender layer (`blender`) and the paid layer (`live`) are deselected here so the token-free
gate stays fast; run them on their own:

    python -m pytest tests/builder -q -m blender -p no:cacheprovider
    set ALLOY_LIVE=1 && python -m pytest tests/builder -q -m live -p no:cacheprovider   (spends provider usage)

ALLOY_BUILDER_PYTEST adds arguments (e.g. `-x`, or `-m "not cli"` on a machine without the CLIs).
Run: python tests/test_builder_suite.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNTS = re.compile(r"(\d+) (passed|failed|error|errors|skipped|deselected|xfailed|xpassed|warnings?)")


def main():
    argv = [sys.executable, "-m", "pytest", "tests/builder", "-q", "-p", "no:cacheprovider", "--no-header",
            "-m", "not blender and not live"]
    argv += os.environ.get("ALLOY_BUILDER_PYTEST", "").split()
    try:
        r = subprocess.run(argv, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired as exc:
        print((exc.stdout or "") + (exc.stderr or ""))
        print("pytest did not finish within 900 s")
        print("0 passed, 1 failed")
        return 1
    out = (r.stdout or "") + (r.stderr or "")
    summary = next((line for line in reversed(out.splitlines()) if " in " in line and COUNTS.search(line)), "")
    counts = {name: int(n) for n, name in COUNTS.findall(summary)}
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    ok = r.returncode == 0 and bool(summary)
    if not ok:
        print(out.rstrip())                 # pytest's own report, verbatim, so run_all shows the failure
        failed = max(failed, 1)
    else:
        print(summary.strip())
    print(f"{passed} passed, {failed} failed")   # LAST line: the shape run_all.py's CUSTOM_COUNT expects
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
