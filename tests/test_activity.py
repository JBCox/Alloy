"""Live activity narration: streaming runner, adapter mapping, sink, loop,
persistence, and the read_text bridge for the live code viewer.

Token-free: the streaming runner is exercised with `python -c` child
processes (no CLI, no tokens), the adapter hooks with canned JSON lines,
and the loop with FakeAgents scripting (reply, [acts]) tuples.

Run:  python tests/test_activity.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
import app
from relay import (Agent, ClaudeAgent, CodexAgent, LoopIO, TurnTimeout,
                   make_activity_sink, run_rounds,
                   ACTIVITY_MAX, ACTIVITY_KEEP)
from test_loop import FakeAgent, RecordingIO, build_state, jsonl_rows
from test_app_headless import FakeWindow


class PythonAgent(Agent):
    """Real _run_streaming against `python -c` children — no CLI, no tokens."""
    name = "Py"
    cli = "python"

    def __init__(self, workspace, code, **kw):
        super().__init__(workspace, **kw)
        self.code = code

    def build_cmd(self, message):
        return [sys.executable, "-c", self.code]

    def parse(self, stdout):
        self.session_id = "py-session"
        return stdout.strip()


class StreamingRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-act-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lines_stream_in_order_and_parse_sees_all(self):
        agent = PythonAgent(self.tmp, "print('a'); print('b'); print('c')")
        seen = []
        agent.activity = lambda line: [{"kind": "note", "text": line.strip()}]
        reply = agent.turn("go", on_activity=lambda a: seen.append(a["text"]))
        self.assertEqual(seen, ["a", "b", "c"])
        self.assertEqual(reply.splitlines(), ["a", "b", "c"])

    def test_stderr_flood_does_not_deadlock(self):
        # >64KB of stderr would wedge a naive single-pipe reader on Windows
        agent = PythonAgent(
            self.tmp,
            "import sys\n"
            "sys.stderr.write('x' * 200000)\n"
            "print('done')")
        self.assertEqual(agent.turn("go"), "done")

    def test_timeout_kills_and_raises_turn_timeout(self):
        agent = PythonAgent(self.tmp, "import time; time.sleep(30)")
        agent.turn_timeout = 1
        with self.assertRaises(TurnTimeout):
            agent.turn("go")

    def test_nonzero_exit_raises_with_stderr(self):
        agent = PythonAgent(
            self.tmp,
            "import sys; sys.stderr.write('boom-detail'); sys.exit(3)")
        with self.assertRaises(RuntimeError) as ctx:
            agent.turn("go")
        self.assertIn("exited 3", str(ctx.exception))
        self.assertIn("boom-detail", str(ctx.exception))

    def test_adapter_describes_its_own_failure(self):
        agent = PythonAgent(self.tmp, "import sys; sys.exit(1)")
        agent.describe_failure = lambda out, err: "the CLI's own sentence"
        with self.assertRaises(RuntimeError) as ctx:
            agent.turn("go")
        self.assertIn("the CLI's own sentence", str(ctx.exception))

    def test_exit_zero_empty_output_raises_no_reply(self):
        agent = PythonAgent(self.tmp, "pass")
        with self.assertRaises(RuntimeError) as ctx:
            agent.turn("go")
        self.assertIn("no reply", str(ctx.exception))

    def test_unlaunchable_names_the_sizes(self):
        # a text file named .exe passes resolve_cmd's which() but makes
        # CreateProcess raise OSError — the size-naming wrap path
        bogus = os.path.join(self.tmp, "bogus.exe")
        with open(bogus, "w") as f:
            f.write("not a program")
        agent = PythonAgent(self.tmp, "print('hi')")
        agent.build_cmd = lambda m: [bogus]
        with self.assertRaises(RuntimeError) as ctx:
            agent.turn("go")
        self.assertIn("could not be launched", str(ctx.exception))
        self.assertIn("32767", str(ctx.exception))

    def test_raising_activity_callback_never_fails_the_turn(self):
        agent = PythonAgent(self.tmp, "print('ok')")
        agent.activity = lambda line: [{"kind": "note", "text": "x"}]

        def bad(_act):
            raise RuntimeError("callback bug")
        self.assertEqual(agent.turn("go", on_activity=bad), "ok")

    def test_raising_activity_hook_never_fails_the_turn(self):
        agent = PythonAgent(self.tmp, "print('ok')")

        def bad_hook(line):
            raise ValueError("hook bug")
        agent.activity = bad_hook
        self.assertEqual(agent.turn("go", on_activity=lambda a: None), "ok")


def claude_line(**event):
    return json.dumps(event)


def claude_tool(name, inp):
    return claude_line(type="assistant",
                       message={"content": [
                           {"type": "tool_use", "name": name, "input": inp}]})


class ClaudeMappingTests(unittest.TestCase):
    def setUp(self):
        self.a = ClaudeAgent(tempfile.gettempdir())

    def test_thinking_block(self):
        line = claude_line(type="assistant", message={"content": [
            {"type": "thinking", "thinking": "First I will look\nat files"}]})
        self.assertEqual(self.a.activity(line),
                         [{"kind": "reasoning", "text": "First I will look"}])

    def test_tool_blocks(self):
        cases = [
            (claude_tool("Bash", {"command": "pytest -x"}),
             ("command", "$ pytest -x")),
            (claude_tool("Edit", {"file_path": r"C:\ws\app.py"}),
             ("edit", "editing app.py")),
            (claude_tool("Read", {"file_path": r"C:\ws\a\b.txt"}),
             ("read", "reading b.txt")),
            (claude_tool("Grep", {"pattern": "def turn"}),
             ("search", "searching: def turn")),
            (claude_tool("WebSearch", {"query": "python popen"}),
             ("search", "web: python popen")),
            (claude_tool("Task", {"description": "scan the repo"}),
             ("tool", "subagent: scan the repo")),
            (claude_tool("SomethingNew", {"x": 1}),
             ("tool", "tool: SomethingNew")),
        ]
        for line, (kind, text) in cases:
            acts = self.a.activity(line)
            self.assertEqual(len(acts), 1, line)
            self.assertEqual((acts[0]["kind"], acts[0]["text"]), (kind, text))

    def test_edit_carries_raw_path(self):
        acts = self.a.activity(claude_tool("Write",
                                           {"file_path": r"C:\ws\new.py"}))
        self.assertEqual(acts[0]["path_raw"], r"C:\ws\new.py")

    def test_ignores_non_assistant_and_garbage(self):
        for line in ("", "not json", "{broken",
                     claude_line(type="system", subtype="init"),
                     claude_line(type="user", message={"content": []}),
                     claude_line(type="result", result="done")):
            self.assertEqual(self.a.activity(line), ())

    def test_parse_stream_json_last_line(self):
        out = "\n".join([
            claude_line(type="system", subtype="init"),
            claude_tool("Bash", {"command": "x"}),
            claude_line(type="result", result="the reply",
                        session_id="sess-1"),
        ])
        self.assertEqual(self.a.parse(out), "the reply")
        self.assertEqual(self.a.session_id, "sess-1")

    def test_parse_survives_trailing_diagnostics(self):
        out = "\n".join([
            claude_line(type="result", result="the reply",
                        session_id="sess-2"),
            claude_line(type="system", subtype="late-noise"),
            "plain text trailer",
        ])
        self.assertEqual(self.a.parse(out), "the reply")
        self.assertEqual(self.a.session_id, "sess-2")

    def test_parse_no_result_object_is_empty(self):
        self.assertEqual(self.a.parse("garbage\n{\"type\": \"system\"}"), "")

    def test_failed_result_is_never_relayed_as_a_reply(self):
        # the CLI puts its own error sentence in `result`; returning it would
        # send "API Error…" to every other seat as if Claude had said it
        for bad in (claude_line(type="result", is_error=True,
                                subtype="error_during_execution",
                                result="API Error: 529 overloaded",
                                session_id="s"),
                    claude_line(type="result", subtype="error_max_turns",
                                result="Reached max turns", session_id="s")):
            self.assertEqual(self.a.parse(bad), "")

    def test_describe_failure_pulls_the_error_sentence(self):
        line = claude_line(type="result", is_error=True,
                           subtype="error_during_execution",
                           api_error_status=529,
                           result="API Error: overloaded_error",
                           session_id="s",
                           usage={"ephemeral_1h_input_tokens": 0})
        msg = self.a.describe_failure(line, "")
        self.assertIn("API Error: overloaded_error", msg)
        self.assertIn("error_during_execution", msg)
        self.assertIn("529", msg)
        self.assertNotIn("ephemeral_1h_input_tokens", msg)   # no JSON soup

    def test_describe_failure_falls_back_to_stderr(self):
        self.assertIn("boom", self.a.describe_failure("", "boom happened"))

    def test_thinking_tokens_become_a_progress_act(self):
        line = claude_line(type="system", subtype="thinking_tokens",
                           estimated_tokens=1240)
        self.assertEqual(self.a.activity(line),
                         [{"kind": "progress", "text": "thinking… 1,240 tokens"}])
        zero = claude_line(type="system", subtype="thinking_tokens",
                           estimated_tokens=0)
        self.assertEqual(self.a.activity(zero), ())


def codex_line(typ, item):
    return json.dumps({"type": typ, "item": item})


class CodexMappingTests(unittest.TestCase):
    def setUp(self):
        self.a = CodexAgent(tempfile.gettempdir())

    def test_reasoning_completed(self):
        line = codex_line("item.completed",
                          {"type": "reasoning", "text": "Weighing options"})
        self.assertEqual(self.a.activity(line),
                         [{"kind": "reasoning", "text": "Weighing options"}])

    def test_command_started_and_failed(self):
        started = codex_line("item.started",
                             {"type": "command_execution",
                              "command": "pwsh -Command 'echo hi'"})
        self.assertEqual(self.a.activity(started)[0]["text"],
                         "$ pwsh -Command 'echo hi'")
        ok = codex_line("item.completed",
                        {"type": "command_execution", "command": "x",
                         "exit_code": 0})
        self.assertEqual(self.a.activity(ok), ())      # successes are noise
        bad = codex_line("item.completed",
                         {"type": "command_execution", "command": "x",
                          "exit_code": 2})
        self.assertEqual(self.a.activity(bad),
                         [{"kind": "command", "text": "command exited 2"}])

    def test_file_change_yields_edit_per_path(self):
        line = codex_line("item.started", {
            "type": "file_change",
            "changes": [{"path": r"C:\ws\a.py", "kind": "update"},
                        {"path": r"C:\ws\b.py", "kind": "add"}]})
        acts = self.a.activity(line)
        self.assertEqual([a["kind"] for a in acts], ["edit", "edit"])
        self.assertEqual([a["path_raw"] for a in acts],
                         [r"C:\ws\a.py", r"C:\ws\b.py"])

    def test_describe_failure_pulls_error_events(self):
        out = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({"type": "error",
                        "message": "stream disconnected before completion"}),
        ])
        self.assertEqual(self.a.describe_failure(out, ""),
                         "stream disconnected before completion")

    def test_ignores_lifecycle_and_garbage(self):
        for line in ("", "nope", "{bad",
                     json.dumps({"type": "thread.started", "thread_id": "t"}),
                     json.dumps({"type": "turn.completed", "usage": {}}),
                     codex_line("item.completed",
                                {"type": "agent_message", "text": "hi"}),
                     codex_line("item.completed", {"type": "brand_new_kind"})):
            self.assertEqual(self.a.activity(line), ())


class SinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-sink-test-")
        self.io = RecordingIO()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sink(self):
        return make_activity_sink(self.io, 0, "claude", "Claude", self.tmp)

    def acts_events(self):
        return [p for e, p in self.io.events if e == "activity"]

    def test_emits_and_collects(self):
        cb, acts = self.sink()
        cb({"kind": "command", "text": "$ ls"})
        self.assertEqual(acts, [{"kind": "command", "text": "$ ls"}])
        ev = self.acts_events()[0]
        self.assertEqual((ev["speaker"], ev["provider"], ev["name"],
                          ev["kind"], ev["text"]),
                         (0, "claude", "Claude", "command", "$ ls"))

    def test_dedupes_consecutive_repeats(self):
        cb, acts = self.sink()
        cb({"kind": "read", "text": "reading x"})
        cb({"kind": "read", "text": "reading x"})
        cb({"kind": "read", "text": "reading y"})
        self.assertEqual([a["text"] for a in acts], ["reading x", "reading y"])

    def test_caps_at_activity_max(self):
        cb, acts = self.sink()
        for i in range(ACTIVITY_MAX + 50):
            cb({"kind": "note", "text": f"line {i}"})
        self.assertEqual(len(acts), ACTIVITY_MAX + 1)
        self.assertIn("not shown", acts[-1]["text"])
        self.assertEqual(len(self.acts_events()), ACTIVITY_MAX + 1)

    def test_edit_path_confined_to_relpath(self):
        cb, acts = self.sink()
        target = os.path.join(self.tmp, "sub", "file.py")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        cb({"kind": "edit", "text": "editing file.py", "path_raw": target})
        self.assertEqual(acts[0]["path"], os.path.join("sub", "file.py"))

    def test_escaping_edit_paths_are_dropped_silently(self):
        cb, acts = self.sink()
        for raw in (r"..\outside.txt",
                    r"C:\Windows\System32\drivers\etc\hosts",
                    "sub/../../escape.py"):
            cb({"kind": "edit", "text": "editing evil", "path_raw": raw})
        self.assertEqual(acts, [])
        self.assertEqual(self.acts_events(), [])

    def test_progress_acts_are_live_only(self):
        cb, acts = self.sink()
        cb({"kind": "progress", "text": "thinking… 100 tokens"})
        cb({"kind": "progress", "text": "thinking… 100 tokens"})   # dedupe
        cb({"kind": "progress", "text": "thinking… 900 tokens"})
        cb({"kind": "command", "text": "$ go"})
        # emitted live, but NEVER persisted onto the row
        self.assertEqual(acts, [{"kind": "command", "text": "$ go"}])
        kinds = [p["kind"] for p in self.acts_events()]
        self.assertEqual(kinds, ["progress", "progress", "command"])

    def test_progress_does_not_spend_the_cap(self):
        cb, acts = self.sink()
        for i in range(ACTIVITY_MAX + 20):
            cb({"kind": "progress", "text": f"thinking… {i} tokens"})
        cb({"kind": "note", "text": "real step"})
        self.assertEqual(acts, [{"kind": "note", "text": "real step"}])

    def test_blank_and_malformed_acts_ignored(self):
        cb, acts = self.sink()
        cb({"kind": "note", "text": "   "})
        cb({"kind": "note"})
        cb("not a dict")
        cb({"text": "kindless is fine"})
        self.assertEqual([a["text"] for a in acts], ["kindless is fine"])
        self.assertEqual(acts[0]["kind"], "note")


class LoopActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-loopact-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_activity_events_flow_and_row_persists_them(self):
        acts = [{"kind": "command", "text": "$ build"},
                {"kind": "edit", "text": "editing x.py"}]
        state = build_state(self.tmp, [[("did it", acts)], ["plain"]],
                            turns=1, labels=["A", "B"])
        io = RecordingIO()
        run_rounds(state, io)
        names = [e for e, _ in io.events]
        # activity lands between A's thinking and its message
        self.assertEqual(names.index("thinking") + 1, names.index("activity"))
        self.assertLess(names.index("activity"), names.index("message"))
        rows = [r for r in jsonl_rows(state) if r.get("name") == "A"]
        self.assertEqual(rows[0]["activity"],
                         [{"kind": "command", "text": "$ build"},
                          {"kind": "edit", "text": "editing x.py"}])
        self.assertIn("ts", rows[0])         # Part 1: rows carry a timestamp
        # seats with no activity persist no key at all
        brow = [r for r in jsonl_rows(state) if r.get("name") == "B"][0]
        self.assertNotIn("activity", brow)

    def test_persisted_activity_is_capped_to_keep(self):
        many = [{"kind": "note", "text": f"n{i}"}
                for i in range(ACTIVITY_KEEP + 30)]
        state = build_state(self.tmp, [[("done", many)]], turns=1,
                            labels=["A"])
        run_rounds(state, RecordingIO())
        row = [r for r in jsonl_rows(state) if r.get("name") == "A"][0]
        self.assertEqual(len(row["activity"]), ACTIVITY_KEEP)
        self.assertEqual(row["activity"][-1]["text"],
                         f"n{ACTIVITY_KEEP + 29}")


class ReadTextBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-readtext-test-")
        self.ws = os.path.join(self.tmp, "workspace")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.ws)
        os.makedirs(self.outside)
        self.api = app.Api()
        self.api._window = FakeWindow()
        self.api._conv = {"workspace": self.ws}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reads_text_inside_workspace(self):
        p = os.path.join(self.ws, "sub", "code.py")
        os.makedirs(os.path.dirname(p))
        with open(p, "w", encoding="utf-8") as f:
            f.write("print('hi')\n")
        r = self.api.read_text(os.path.join("sub", "code.py"))
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["text"], "print('hi')\n")
        self.assertEqual(r["name"], "code.py")
        self.assertFalse(r["truncated"])
        self.assertIsInstance(r["mtime"], float)

    def test_escapes_and_missing_are_identical_quiet_errors(self):
        with open(os.path.join(self.outside, "secret.txt"), "w") as f:
            f.write("secret")
        escaped = self.api.read_text(r"..\outside\secret.txt")
        absolute = self.api.read_text(
            os.path.join(self.outside, "secret.txt"))
        missing = self.api.read_text("no-such-file.txt")
        self.assertEqual(escaped, {"error": "not available"})
        self.assertEqual(absolute, {"error": "not available"})
        self.assertEqual(missing, {"error": "not available"})

    def test_binary_is_refused(self):
        with open(os.path.join(self.ws, "blob.bin"), "wb") as f:
            f.write(b"\x00\x01\x02rest")
        self.assertEqual(self.api.read_text("blob.bin"),
                         {"error": "not a text file"})

    def test_truncates_at_cap(self):
        with open(os.path.join(self.ws, "big.txt"), "w") as f:
            f.write("x" * (app.TEXT_MAX_BYTES + 100))
        r = self.api.read_text("big.txt")
        self.assertTrue(r["ok"])
        self.assertTrue(r["truncated"])
        self.assertEqual(len(r["text"]), app.TEXT_MAX_BYTES)

    def test_no_workspace_is_a_clear_error(self):
        self.api._conv = None
        self.api._view_workspace = None
        self.assertIn("error", self.api.read_text("x.txt"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
