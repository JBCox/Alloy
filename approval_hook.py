"""PreToolUse approval bridge for Alloy's "Ask first" permission level.

Claude Code runs this as a hook BEFORE any write/exec tool. It is a tiny,
dependency-free blocker: write the request where the relay can see it, wait
for an answer file, then allow or deny. All the intelligence lives on the
relay side (relay.Agent._watch_approvals -> LoopIO.ask_human).

Two rules make this safe rather than decorative:
  * it fails CLOSED — a timeout, a crash, or a relay that never answers all
    come back DENY, because the alternative is a gate that silently opens
    when nobody is listening;
  * it never imports the relay. The hook runs inside the CLI's process tree,
    possibly under a different interpreter, and a broken import here would
    fail every tool call in the conversation.

Usage: python approval_hook.py <request-dir> [<seat-name>]
Contract: hook JSON on stdin, hook JSON on stdout (Claude Code hook API).
"""
import json
import os
import sys
import time
import uuid

POLL = 0.25
TIMEOUT = 600  # seconds; the relay's own turn watchdog is shorter


def decide(allow, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow" if allow else "deny",
        "permissionDecisionReason": reason,
    }}))
    return 0


def main():
    if len(sys.argv) < 2:
        return decide(False, "Alloy approval bridge misconfigured (no request dir).")
    reqdir = sys.argv[1]
    seat = sys.argv[2] if len(sys.argv) > 2 else "a seat"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    tool = payload.get("tool_name") or "a tool"
    tool_input = payload.get("tool_input") or {}

    rid = uuid.uuid4().hex[:12]
    req = {"id": rid, "seat": seat, "tool": tool, "input": tool_input,
           "cwd": payload.get("cwd") or os.getcwd(), "ts": time.time()}
    try:
        os.makedirs(reqdir, exist_ok=True)
        tmp = os.path.join(reqdir, rid + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(req, fh)
        os.replace(tmp, os.path.join(reqdir, rid + ".req"))
    except OSError as e:
        return decide(False, f"Alloy could not reach Josh for approval ({e}).")

    ansfile = os.path.join(reqdir, rid + ".ans")
    deadline = time.time() + TIMEOUT
    while time.time() < deadline:
        try:
            with open(ansfile, encoding="utf-8") as fh:
                ans = json.load(fh)
        except (OSError, ValueError):
            time.sleep(POLL)
            continue
        try:
            os.remove(ansfile)
        except OSError:
            pass
        return decide(bool(ans.get("allow")),
                      ans.get("reason") or ("Josh approved this." if ans.get("allow")
                                            else "Josh declined this."))
    return decide(False, "Alloy got no answer from Josh in time; declining.")


if __name__ == "__main__":
    sys.exit(main())
