"""Child-process helper for process-runner tests. Modes are chosen by argv[1]."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    if mode == "echo":
        data = sys.stdin.read() if not sys.stdin.isatty() else ""
        print(json.dumps({"args": sys.argv[2:], "stdin": data, "cwd": os.getcwd(),
                          "env_marker": os.environ.get("ALLOY_TEST_MARKER", "")}, ensure_ascii=False))
        print("to stderr ünï", file=sys.stderr)
        return int(os.environ.get("ALLOY_TEST_EXIT", "0"))
    if mode == "slow":
        seconds = float(sys.argv[2])
        end = time.time() + seconds
        i = 0
        while time.time() < end:
            print(f"tick {i}", flush=True)
            i += 1
            time.sleep(0.2)
        return 0
    if mode == "silent":
        time.sleep(float(sys.argv[2]))
        print("woke", flush=True)
        return 0
    if mode == "spawn":
        child = subprocess.Popen([sys.executable, __file__, "silent", "120"])
        print(f"GRANDCHILD {child.pid}", flush=True)
        time.sleep(120)
        return 0
    if mode == "flood":
        chunk_out = ("o" * 1023 + "\n").encode()
        chunk_err = ("e" * 1023 + "\n").encode()
        for _ in range(2048):
            sys.stdout.buffer.write(chunk_out)
            sys.stderr.buffer.write(chunk_err)
        sys.stdout.flush()
        sys.stderr.flush()
        return 0
    print(f"unknown mode {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
