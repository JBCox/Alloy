#!/usr/bin/env python3
"""Restart the Alloy desktop app on Windows: prove the code, stop it gently,
relaunch detached, report where the conversation lives now.

Standalone by contract: stdlib only, no relay/app/webview imports, so it can
be run from inside a conversation that the very app it restarts is hosting.

The sequence (every earlier gate must pass before the app is touched):

1. TEST GATE: ``python tests/run_all.py`` (token-free, exit code decides) and
   a cold-import smoke ``python -c "import app"``. A red gate is a loud abort
   with the output tail -- the running app is never stopped on unproven code;
 2. discover every running ``pythonw.exe ... app.py`` candidate (CIM query);
 3. classify: HOST GUARD -- any candidate in THIS process's ancestry is the
    app hosting the caller's session and is reported, never touched; OWNERSHIP
    GUARD -- an auto-targeted candidate must also show a visible window titled
    ``Alloy*``, so another project's ``app.py`` is never killed (windowless
    candidates are reported and refused rather than guessed at);
 4. stop each target gently: ``taskkill /PID`` posts WM_CLOSE, which pywebview
    treats as a normal window close (session state is saved atomically per
    turn), wait up to GRACE_SECONDS, then force-kill and wait FORCE_SECONDS
    (a target that vanished before the kill reports "already gone").
    Anything that survives both is a loud abort with NOTHING relaunched.
    Two honest limits, learned the hard way: the gentle stop is gentle only
    while the app is IDLE -- every conversation thread is a daemon, so
    WM_CLOSE mid-turn amputates that turn -- and when several corroborated
    instances are running the script refuses to guess which one matters and
    demands explicit --pid values. Controlled self-restarts therefore belong
    INSIDE app.py (request_restart honored at a turn boundary,
    RESTART_DESIGN.md §4); this script is the manual / outside fallback;
 5. relaunch ONE detached ``pythonw app.py`` (DETACHED_PROCESS + new process
    group, DEVNULL on stdin/stdout/stderr). The AppUserModelID "Alloy.AIChat"
    and the window/taskbar icon are applied inside app.py's own main() BEFORE
    its window exists, so a plain relaunch preserves both -- nothing to redo;
 6. verify the launched child BY ITS OWN PID -- immune to WMI visibility
    lag and Windows PID reuse: it must stay alive for VERIFY_SECONDS (an
    instant death is a loud failure naming the manual command), then its
    Alloy-titled window is awaited. A live child whose window has not
    shown yet is only a warning line;
 7. print the newest sessions/<id> (ranked by meta.json mtime, else the
    strongest transcript marker) so the conversation can be reopened.

No .ps1 files: every shell call is an inline powershell.exe one-liner, and
every child gets stdin=subprocess.DEVNULL plus CREATE_NO_WINDOW. Ancestry
queries return only Name/ParentProcessId/IsApp (the app-hosting test computed
PowerShell-side): a full CommandLine would drag arbitrary multi-line text
through ConvertTo-Json, whose 5.1 output leaves raw control characters
unescaped and breaks json.loads.

Usage:
    python restart.py                  # gated restart of every non-host instance
    python restart.py --skip-tests     # skip the gate (manual convenience only)
    python restart.py --pid N          # restart exactly that instance
    python restart.py --dry-run        # print the plan, touch nothing

Exit codes: 0 success (or a completed dry-run), 1 loud failure with one
legible sentence on stderr. It never half-restarts silently: the new instance
is launched only after every target is confirmed dead, and a launch that does
not come up is reported as exactly that.
"""

import argparse
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_PY = ROOT / "app.py"

GRACE_SECONDS = 15
FORCE_SECONDS = 8
VERIFY_SECONDS = 8
WINDOW_SECONDS = 20
TEST_TIMEOUT_SECONDS = 900
TAIL_CHARS = 1800

CREATE_NO_WINDOW = 0x08000000

ALLOY_TITLE_PREFIX = "Alloy"
RUN_ALL_SUMMARY = re.compile(r"(\d+) suites?, (\d+) tests?, (\d+) failed")


class RestartError(Exception):
    """One legible sentence for why the restart aborted."""


def _tail(text, limit=TAIL_CHARS):
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def _run(argv, timeout=60):
    try:
        return subprocess.run(
            argv, cwd=str(ROOT), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW, text=True,
            encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RestartError(
            "command did not finish within %ss: %s" % (timeout, argv[0]))


def _ps(command, timeout=60):
    return _run(["powershell.exe", "-NoProfile", "-NonInteractive",
                 "-Command",
                 "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                 + command], timeout=timeout)


def _cim_json(command):
    done = _ps(command, timeout=45)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()[:300]
        raise RestartError("process query failed: " + detail)
    out = (done.stdout or "").strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        raise RestartError("could not parse process query output: "
                           + out[:300])
    if isinstance(data, dict):
        data = [data]
    return data or []


def run_gate(timeout=TEST_TIMEOUT_SECONDS):
    """Run the token-free suites + cold-import smoke; abort on red."""
    done = _run([sys.executable, str(ROOT / "tests" / "run_all.py")],
                timeout=timeout)
    if done.returncode != 0:
        raise RestartError(
            "test gate FAILED (tests/run_all.py exit %d); the running app "
            "was NOT touched. Output tail:\n%s"
            % (done.returncode, _tail((done.stdout or "") + "\n"
                                      + (done.stderr or ""))))
    summary = RUN_ALL_SUMMARY.search(done.stdout or "")
    counts = ("%s suites / %s tests, %s failed"
              % (summary.group(1), summary.group(2), summary.group(3))
              ) if summary else "all suites passed"
    smoke = _run([sys.executable, "-c", "import app"], timeout=120)
    if smoke.returncode != 0:
        raise RestartError(
            "cold-import smoke FAILED (import app, exit %d); the running "
            "app was NOT touched. Output tail:\n%s"
            % (smoke.returncode,
               _tail((smoke.stderr or "") + (smoke.stdout or ""))))
    return "gate passed: %s" % counts


def find_instances():
    """Every pythonw.exe whose command line mentions app.py.

    Returns [{pid, has_window}] -- has_window means a VISIBLE top-level
    window titled Alloy*, i.e. corroborated as OUR app and not some other
    project's app.py.
    """
    rows = _cim_json(
        "$i = Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -match 'app\\.py' } | "
        "Select-Object ProcessId; "
        "ConvertTo-Json -Compress -InputObject @($i)")
    pids = sorted(int(r["ProcessId"]) for r in rows)
    alloy = {pid for pid, title in _window_pids_titles()
             if title.startswith(ALLOY_TITLE_PREFIX)}
    return [{"pid": pid, "has_window": pid in alloy} for pid in pids]


def proc_info(pid):
    rows = _cim_json(
        "$o = Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" | "
        "Select-Object Name, ParentProcessId, "
        "@{n='IsApp';e={ ($_.Name -eq 'pythonw.exe') -and "
        "($_.CommandLine -match 'app\\.py') }}; "
        "ConvertTo-Json -Compress -InputObject @($o)" % pid)
    return rows[0] if rows else None


def alive(pid):
    return proc_info(pid) is not None


def host_pids():
    """App-hosting processes in this process's ancestry; never killed."""
    hosts, seen, cur = [], set(), os.getpid()
    while cur and cur > 4 and cur not in seen:
        seen.add(cur)
        info = proc_info(cur)
        if not info:
            break
        if info.get("IsApp"):
            hosts.append(cur)
        cur = int(info.get("ParentProcessId") or 0)
    return hosts


def _kill(argv):
    try:
        subprocess.run(argv, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=CREATE_NO_WINDOW, timeout=30)
    except subprocess.TimeoutExpired:
        raise RestartError("taskkill did not answer within 30s")


def stop_instance(pid):
    if not alive(pid):
        return "already gone"
    _kill(["taskkill", "/PID", str(pid)])
    deadline = time.monotonic() + GRACE_SECONDS
    while time.monotonic() < deadline:
        if not alive(pid):
            return "graceful close"
        time.sleep(0.4)
    _kill(["taskkill", "/F", "/PID", str(pid)])
    deadline = time.monotonic() + FORCE_SECONDS
    while time.monotonic() < deadline:
        if not alive(pid):
            return "force kill"
        time.sleep(0.4)
    raise RestartError(
        "pid %d survived both a graceful close and a force kill; "
        "nothing was relaunched" % pid)


def pick_pythonw():
    sibling = Path(sys.executable).with_name("pythonw.exe")
    if sibling.exists():
        return sibling
    found = shutil.which("pythonw")
    if found:
        return Path(found)
    raise RestartError(
        "pythonw.exe not found beside %s or on PATH; cannot relaunch the app "
        "without a console" % sys.executable)


def launch_detached(pythonw):
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [str(pythonw), str(APP_PY)], cwd=str(ROOT),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    return proc.pid


def _window_pids_titles():
    user32 = ctypes.windll.user32
    found = []

    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((pid.value, buf.value))
        return True

    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(proto(callback), 0)
    return found


def window_up(pid, timeout=WINDOW_SECONDS):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return False
        if any(p == pid and t.startswith(ALLOY_TITLE_PREFIX)
               for p, t in _window_pids_titles()):
            return True
        time.sleep(0.4)
    return False


def verify_launched(pid, timeout=VERIFY_SECONDS):
    """The child we spawned must stay alive for a grace window; dying early
    is detected in well under a second instead of after a discovery wait."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not alive(pid):
            return False
        time.sleep(0.3)
    return True


def newest_session():
    """Print hint only: newest dir by marker mtime. Resumability filtering
    (skip spawned-team children, legacy view-only dirs) is wave-2's relay.
    newest_resumable_session() -- do not grow that logic here."""
    base = ROOT / "sessions"
    best, best_stamp = None, None
    if base.is_dir():
        for entry in base.iterdir():
            if not entry.is_dir():
                continue
            stamp = None
            for marker in ("meta.json", "messages.jsonl", "transcript.md"):
                candidate = entry / marker
                try:
                    if candidate.exists():
                        stamp = candidate.stat().st_mtime
                        break
                except OSError:
                    continue
            if stamp is None:
                continue
            if best_stamp is None or stamp > best_stamp:
                best, best_stamp = entry, stamp
    return (best.name, str(best)) if best else ("(none yet)", "")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Restart the Alloy desktop app gracefully.")
    parser.add_argument("--pid", type=int, default=None,
                        help="restart exactly this instance instead of all "
                             "non-host ones")
    parser.add_argument("--skip-tests", action="store_true",
                        help="skip the test gate (manual convenience only; "
                             "an improvement wave must never skip it)")
    parser.add_argument("--test-timeout", type=int,
                        default=TEST_TIMEOUT_SECONDS,
                        help="seconds allowed for the test gate")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan without stopping or launching")
    args = parser.parse_args(argv)

    if not APP_PY.exists():
        raise RestartError("app.py not found beside restart.py (%s)" % ROOT)

    if args.skip_tests:
        print("test gate SKIPPED (--skip-tests)")
    else:
        print("running test gate: tests/run_all.py + import-app smoke "
              "(up to %ds)..." % args.test_timeout)
        print(run_gate(args.test_timeout))

    pythonw = pick_pythonw()

    instances = find_instances()
    hosts = host_pids()
    host_set = set(hosts)
    known = {row["pid"] for row in instances}
    strongs = {row["pid"] for row in instances if row["has_window"]}
    weaks = known - strongs - host_set

    if args.pid is not None:
        if args.pid not in known:
            raise RestartError(
                "pid %d is not a running pythonw app.py instance right now "
                "(found: %s)" % (args.pid, ", ".join(map(str, sorted(known)))
                                 or "none"))
        if args.pid in host_set:
            raise RestartError(
                "refusing pid %d: it sits in this process's ancestry (the "
                "host of the calling session); restarting it would kill the "
                "caller" % args.pid)
        targets = [args.pid]
    else:
        targets = sorted(strongs - host_set)
        if len(targets) > 1:
            raise RestartError(
                "%d corroborated instances are running (%s); restarting all "
                "of them is ambiguous -- name one explicitly with --pid N"
                % (len(targets), ", ".join(map(str, sorted(targets)))))

    print("found pythonw app.py instances: "
          + (", ".join("%d%s" % (r["pid"],
                                 "" if r["has_window"] else " (no Alloy "
                                      "window)")
                       for r in instances) or "none"))
    if hosts:
        print("host guard (never touched): "
              + ", ".join(map(str, sorted(hosts))))
    if weaks:
        print("ownership guard (skipped, no Alloy window seen): "
              + ", ".join(map(str, sorted(weaks))))
    print("targets to stop: " + (", ".join(map(str, targets)) or "none"))
    print('relaunch command: "%s" "%s"' % (pythonw, APP_PY))

    if args.dry_run:
        print("dry run: nothing was stopped or launched")
        return 0

    if not targets:
        if host_set and not weaks and known <= host_set:
            raise RestartError(
                "every running instance (%s) hosts the calling session; "
                "restarting it would kill the caller -- request the restart "
                "from outside the app or after the conversation ends"
                % ", ".join(map(str, sorted(host_set))))
        if weaks:
            raise RestartError(
                "matched pythonw app.py process(es) %s show no Alloy window; "
                "refusing to guess whether they are ours -- rerun with "
                "--pid N to name the target explicitly"
                % ", ".join(map(str, sorted(weaks))))
        print("no running instance to stop - starting fresh")

    for pid in targets:
        how = stop_instance(pid)
        print("stopped pid %d (%s)" % (pid, how))

    new_pid = launch_detached(pythonw)
    print("launched detached pythonw app.py: pid %d" % new_pid)
    if not verify_launched(new_pid):
        raise RestartError(
            'new app process %d exited immediately; run "pythonw app.py" '
            "manually in %s to see why" % (new_pid, ROOT))

    seen_window = window_up(new_pid)
    if not alive(new_pid):
        raise RestartError(
            "new app process %d exited during verification; run \"pythonw "
            "app.py\" manually in %s to see why" % (new_pid, ROOT))

    name, path = newest_session()
    if seen_window:
        print("relaunched: pid %d, Alloy window is up" % new_pid)
    else:
        print("warning: pid %d is running but no 'Alloy' window appeared "
              "within %ss" % (new_pid, WINDOW_SECONDS))
    print("newest session: %s" % name)
    if path:
        print("reopen path: %s" % path)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RestartError as exc:
        print("restart failed: %s" % exc, file=sys.stderr)
        raise SystemExit(1)
