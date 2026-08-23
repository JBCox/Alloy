#!/usr/bin/env python3
"""Canonical token-free test runner.

Every ``test_*.py`` file is a supported standalone entrypoint, including the
three historical custom runners that pytest only partially collects.  Run each
in a fresh process so imports and globals cannot leak between suites, then
report one reproducible suite/test total.
"""

from pathlib import Path
import re
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = ROOT / "tests"
UNITTEST_COUNT = re.compile(r"Ran\s+(\d+)\s+tests?\b")
CUSTOM_COUNT = re.compile(r"(?m)^(\d+)\s+passed,\s*(\d+)\s+failed\s*$")


def reported_count(output):
    """Read the suite's own total; never infer it from pytest collection."""
    custom = CUSTOM_COUNT.findall(output)
    if custom:
        passed, failed = map(int, custom[-1])
        return passed + failed
    standard = UNITTEST_COUNT.findall(output)
    return int(standard[-1]) if standard else None


def main():
    suites = sorted(TEST_DIR.glob("test_*.py"))
    total = 0
    failed = []
    started = time.monotonic()
    for path in suites:
        tick = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(path)], cwd=ROOT,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        output = (result.stdout or "") + (result.stderr or "")
        count = reported_count(output)
        elapsed = time.monotonic() - tick
        ok = result.returncode == 0 and count is not None
        if ok:
            total += count
            print(f"PASS {path.name}: {count} tests ({elapsed:.2f}s)")
        else:
            failed.append(path.name)
            why = f"exit {result.returncode}" if result.returncode else "no test count reported"
            print(f"FAIL {path.name}: {why}")
            if output.strip():
                print(output.rstrip())
    elapsed = time.monotonic() - started
    print()
    print(f"{len(suites)} suites, {total} tests, {len(failed)} failed "
          f"({elapsed:.2f}s)")
    if failed:
        print("Failed suites: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
