"""A stand-in for the claude, gemini, and codex executables (layer: CLI interaction without cost).

It states exactly what it emulates: the *output shapes* the adapters parse, taken from the CLIs' official
documentation and ``--help`` (claude ``--output-format json`` result object; gemini ``-o json`` object with
``response``/``stats``/``error``; codex ``--json`` JSONL events plus ``-o`` last-message file). It proves the
adapters' argv, stdin, file, and parsing contracts, never provider behaviour.

    python _fake_cli.py <claude|gemini|codex> [cli args...]

Environment knobs (all optional):
  ALLOY_FAKE_CLI_RECORD     path: the fake appends one JSON line {argv, stdin, cwd, env subset} per call
  ALLOY_FAKE_CLI_MODE       ok (default) | error | text_only | reject_xhigh | slow | no_usage | empty | budget
  ALLOY_FAKE_CLI_REPLY      JSON text the model "replies" with (default {"nonce": "n-1"})
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid


FAKE_HELP = {
    "claude": """Usage: claude [options] [command] [prompt]
Options:
  --add-dir <directories...>            Additional directories to allow tool access to
  --append-system-prompt <prompt>       Append a system prompt to the default system prompt
  --disable-slash-commands              Disable all skills
  --effort <level>                      Effort level (low, medium, high, xhigh, max)
  --json-schema <schema>                JSON Schema for structured output validation
  --max-budget-usd <amount>             Maximum dollar amount to spend on API calls
  --model <model>                       Model for the current session
  --output-format <format>              Output format (choices: "text", "json", "stream-json")
  --permission-mode <mode>              Permission mode (choices: "acceptEdits", "auto", "plan")
  -p, --print                           Print response and exit
  -r, --resume [value]                  Resume a conversation by session ID
  --session-id <uuid>                   Use a specific session ID for the conversation
  --strict-mcp-config                   Only use MCP servers from --mcp-config
  --tools <tools...>                    Specify the list of available tools
""",
    "gemini": """Usage: gemini [options] [command]
Options:
  -m, --model                     Model  [string]
  -p, --prompt                    Run in non-interactive (headless) mode with the given prompt  [string]
      --skip-trust                Trust the current workspace for this session  [boolean]
      --approval-mode             Set the approval mode: default, auto_edit, yolo, plan (read-only mode)  [string]
  -r, --resume                    Resume a previous session  [string]
      --session-id                Start a new session with a manually provided UUID  [string]
      --include-directories       Additional directories to include in the workspace  [array]
  -o, --output-format             The format of the CLI output  [string] [choices: "text", "json", "stream-json"]
""",
    "codex": """Run Codex non-interactively
Usage: codex exec [OPTIONS] [PROMPT]
Commands:
  resume  Resume a previous session by id
Options:
  -c, --config <key=value>  Override a configuration value
  -i, --image <FILE>...     Optional image(s) to attach to the initial prompt
  -m, --model <MODEL>       Model the agent should use
  -s, --sandbox <SANDBOX_MODE>  [possible values: read-only, workspace-write, danger-full-access]
  -C, --cd <DIR>            Tell the agent to use the specified directory as its working root
      --skip-git-repo-check Allow running Codex outside a Git repository
      --output-schema <FILE>  Path to a JSON Schema file describing the model's final response shape
      --json                Print events to stdout as JSONL
  -o, --output-last-message <FILE>  Specifies file where the last message from the agent should be written
""",
}


def _record(argv: list[str], stdin: str) -> None:
    path = os.environ.get("ALLOY_FAKE_CLI_RECORD")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "stdin": stdin, "cwd": os.getcwd(),
                            "env": {k: v for k, v in os.environ.items() if k.startswith("ALLOY_TEST_")}},
                           ensure_ascii=False) + "\n")


def _opt(argv: list[str], *names: str) -> str | None:
    for i, tok in enumerate(argv):
        if tok in names and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _opts(argv: list[str], *names: str) -> list[str]:
    out = []
    for i, tok in enumerate(argv):
        if tok in names and i + 1 < len(argv):
            out.append(argv[i + 1])
    return out


def _packet_dir(argv: list[str], stdin: str) -> str | None:
    for flag in ("--add-dir", "--include-directories"):
        v = _opt(argv, flag)
        if v:
            return v
    m = re.search(r"packet directory \((.+?)\)", stdin or "")
    return m.group(1) if m else None


def _memory_path() -> str:
    rec = os.environ.get("ALLOY_FAKE_CLI_RECORD")
    return os.environ.get("ALLOY_FAKE_CLI_MEMORY") or (rec + ".memory.json" if rec else os.path.join(os.getcwd(), "fake-memory.json"))


def _memory() -> dict:
    try:
        with open(_memory_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _remember(session: str, nonce: str) -> None:
    mem = _memory()
    mem[session] = nonce
    with open(_memory_path(), "w", encoding="utf-8") as f:
        json.dump(mem, f)


def _reply(argv: list[str] | None = None, stdin: str = "", session: str | None = None) -> str:
    """What the fake 'model' answers. When told to read a packet, it follows the packet's schema the way a
    cooperative agent would: reports the probe image contents (it cannot see images: this is emulation of the
    routing, never of vision), refuses the write probe, and echoes the session nonce it was told to remember
    (kept in a scratch memory file keyed by session id, emulating provider session persistence)."""
    pdir = _packet_dir(argv or [], stdin)
    text = ""
    if pdir and os.path.isfile(os.path.join(pdir, "PACKET.md")):
        with open(os.path.join(pdir, "PACKET.md"), "r", encoding="utf-8") as f:
            text = f.read()
    schema = re.search(r"schema `([a-z_]+)`", text)
    schema_name = schema.group(1) if schema else None
    if schema_name == "probe_report":
        return json.dumps({"shape": "triangle", "color": "red", "number": 7})
    if schema_name == "write_probe_report":
        m = re.search(r"create the file (.+?) containing", text)
        return json.dumps({"attempted_path": m.group(1) if m else "?", "outcome": "attempted_and_refused",
                           "write_succeeded": False, "error_text": "write refused (fake read-only mode)"})
    if schema_name == "session_probe_report":
        m = re.search(r"Remember this nonce: ([0-9a-f]+)", text)
        if m and session:
            _remember(session, m.group(1))
            return json.dumps({"nonce": m.group(1)})
        if m:
            return json.dumps({"nonce": m.group(1)})
        if "cancellation probe" in text:
            return json.dumps({"nonce": "cancel"})
        return json.dumps({"nonce": _memory().get(session or "", "forgotten")})
    if os.environ.get("ALLOY_FAKE_CLI_REPLY"):
        return os.environ["ALLOY_FAKE_CLI_REPLY"]
    if pdir and os.path.isfile(os.path.join(pdir, "schema.json")):
        # any other packet: the smallest object that satisfies the packet's own schema.json (routing, not judgement)
        with open(os.path.join(pdir, "schema.json"), "r", encoding="utf-8") as f:
            return json.dumps(_minimal(json.load(f)))
    return json.dumps({"nonce": "n-1"})


def _minimal(schema: dict):
    """Smallest value satisfying a JSON-Schema subset: required keys only, empty arrays, first enum value."""
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    t = t[0] if isinstance(t, list) else t
    if t == "object" or "properties" in schema:
        return {k: _minimal(schema.get("properties", {}).get(k, {})) for k in schema.get("required", [])}
    if t == "array":
        return []
    if t == "string":
        return "fake"
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    return None


def claude(argv: list[str], stdin: str, mode: str) -> int:
    session = _opt(argv, "--session-id") or _opt(argv, "--resume") or str(uuid.uuid4())
    if mode == "slow":
        for i in range(600):
            time.sleep(0.1)
        return 0
    if mode == "error":
        # Documented: failures inside the run are printed as the result on stdout (headless docs).
        print(json.dumps({"type": "result", "subtype": "error", "is_error": True,
                          "result": "Invalid model name: nope-1", "session_id": session}))
        return 1
    if mode == "budget":
        # Observed live 2.1.233: stopped by --max-budget-usd -> is_error, result null, cost at/above the cap
        cap = float(_opt(argv, "--max-budget-usd") or 0)
        print(json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": None,
                          "stop_reason": "tool_use", "num_turns": 2, "session_id": session, "total_cost_usd": cap + 2.0,
                          "usage": {"input_tokens": 0, "output_tokens": 0}}))
        return 1
    reply = _reply(argv, stdin, session)
    out = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1234, "num_turns": 1,
           "result": reply if mode != "text_only" else f"Here is my answer:\n```json\n{reply}\n```\nDone.",
           "session_id": session, "total_cost_usd": 0.0123}
    if mode != "no_usage":
        out["usage"] = {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 50,
                        "cache_creation_input_tokens": 7, "server_tool_use": {"web_search_requests": 0}}
        out["modelUsage"] = {"claude-fable-5-1-fake": {"inputTokens": 120, "outputTokens": 30, "costUSD": 0.0123}}
    if "--json-schema" in argv and mode != "text_only":
        try:
            out["structured_output"] = json.loads(reply)
        except ValueError:
            pass
    print(json.dumps(out, ensure_ascii=False))
    return 0


def gemini(argv: list[str], stdin: str, mode: str) -> int:
    if mode == "slow":
        time.sleep(60)
        return 0
    if mode == "error":
        print(json.dumps({"error": {"type": "FatalInputError", "message": "invalid model: nope-1", "code": 42}}))
        return 42
    reply = _reply(argv, stdin, _opt(argv, "--session-id") or _opt(argv, "--resume"))
    out = {"response": reply if mode != "text_only" else f"Sure.\n```json\n{reply}\n```",
           "stats": {"models": {"fake-gemini": {"api": {"totalRequests": 1}, "tokens": {"prompt": 100, "candidates": 20}}},
                     "tools": {"totalCalls": 1}, "files": {}}}
    if mode == "no_usage":
        out.pop("stats")
    print(json.dumps(out, ensure_ascii=False))
    return 0


def codex(argv: list[str], stdin: str, mode: str) -> int:
    if mode == "slow":
        time.sleep(60)
        return 0
    thread_id = argv[argv.index("resume") + 1] if "resume" in argv else str(uuid.uuid4())
    reasoning = [v for v in _opts(argv, "-c") if v.startswith("model_reasoning_effort")]
    if mode == "reject_xhigh" and any("xhigh" in v for v in reasoning):
        print(json.dumps({"type": "error", "message": "unsupported value for model_reasoning_effort: xhigh is not "
                                                     "available for this model"}))
        return 1
    if mode == "error":
        print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
        print(json.dumps({"type": "turn.failed", "error": {"message": "model nope-1 not found"}}))
        return 1
    if mode == "error_event_only":
        # an `error` event with no completed turn afterwards: the reply never came (observed shape, 2026-09-08)
        print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
        print(json.dumps({"type": "error", "message": "Reconnecting... 1/5 (stream disconnected before completion: "
                                                     "websocket closed by server before response.completed)"}))
        return 0
    schema = _opt(argv, "--output-schema")
    if schema:
        with open(schema, "r", encoding="utf-8") as f:
            json.load(f)  # must be readable JSON
    reply = _reply(argv, stdin, thread_id)
    text = reply if mode != "text_only" else f"Final answer below.\n{reply}"
    print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
    print(json.dumps({"type": "turn.started"}))
    if mode == "reconnect_then_ok":
        # the live shape of 2026-09-08: the CLI's stream dropped, it reconnected (repeated `error` events, then an
        # `item` of type error announcing the HTTPS fallback) and the turn still completed with a full reply
        for n in (2, 3):
            print(json.dumps({"type": "error", "message": f"Reconnecting... {n}/5 (stream disconnected before completion: "
                                                         "websocket closed by server before response.completed)"}))
        print(json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "error", "message":
                          "Falling back from WebSockets to HTTPS transport. stream disconnected before completion"}}))
    print(json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}}, ensure_ascii=False))
    if mode != "no_usage":
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 200, "cached_input_tokens": 80,
                                                              "output_tokens": 40, "reasoning_output_tokens": 15}}))
    else:
        print(json.dumps({"type": "turn.completed"}))
    last = _opt(argv, "-o", "--output-last-message")
    if last:
        with open(last, "w", encoding="utf-8") as f:
            f.write(text)
    print("model: fake-codex\nreasoning effort: " + (reasoning[-1].split("=", 1)[1].strip('"') if reasoning else "?"),
          file=sys.stderr)
    return 0


def main() -> int:
    provider = sys.argv[1] if len(sys.argv) > 1 else ""
    argv = sys.argv[2:]
    stdin = ""
    try:
        if not sys.stdin.isatty():
            stdin = sys.stdin.read()
    except (OSError, ValueError):
        stdin = ""
    _record(argv, stdin)
    if "--version" in argv:
        print({"claude": "9.9.9 (Claude Code)", "gemini": "9.9.9", "codex": "codex-cli 9.9.9"}.get(provider, "0"))
        return 0
    if "--help" in argv:
        # The fake's help lists exactly the options the fake understands (its own contract), in the layout the real
        # CLIs use, so the adapters' local tier can be exercised without the real executables.
        print(FAKE_HELP.get(provider, "Usage: fake"))
        print("  -h, --help   Display help for command")
        print("(fake argv: " + " ".join(argv) + ")")
        return 0
    mode = os.environ.get("ALLOY_FAKE_CLI_MODE", "ok")
    if mode == "empty":
        return 0
    fn = {"claude": claude, "gemini": gemini, "codex": codex}.get(provider)
    if fn is None:
        print(f"unknown provider {provider!r}", file=sys.stderr)
        return 2
    return fn(argv, stdin, mode)


if __name__ == "__main__":
    sys.exit(main())
