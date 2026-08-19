"""Named permission profiles stay honest across UI, persistence and CLIs."""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay


class PermissionProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-permissions-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_named_profiles_and_legacy_aliases_normalize(self):
        self.assertEqual(relay.normalize_permission(None), "auto")
        self.assertEqual(relay.normalize_permission(True), "full")
        self.assertEqual(relay.normalize_permission("read-only"), "read_only")
        self.assertEqual(relay.normalize_permission("ask"), "ask")
        self.assertEqual(relay.normalize_permission("wishful-thinking"), "auto")

    def test_full_profile_is_the_only_unsandboxed_profile(self):
        for cls in (relay.ClaudeAgent, relay.CodexAgent, relay.GeminiAgent):
            normal = cls(self.tmp, yolo=False).build_cmd("hello")
            full = cls(self.tmp, yolo=True).build_cmd("hello")
            if cls is relay.ClaudeAgent:
                bypass = "--dangerously-skip-permissions"
            elif cls is relay.CodexAgent:
                bypass = "--dangerously-bypass-approvals-and-sandbox"
            else:
                # Gemini auto-approves in both headless modes; the sandbox is
                # the load-bearing distinction.
                self.assertIn("--sandbox", normal)
                self.assertNotIn("--sandbox", full)
                continue
            self.assertNotIn(bypass, normal)
            self.assertIn(bypass, full)

    def test_saved_summary_exposes_named_profile(self):
        meta = {
            "v": relay.META_VERSION, "title": "x", "created": "",
            "updated": "", "ended": True, "workspace": self.tmp,
            "topic": "x", "yolo": True, "turns": 1, "rnd": 1,
            "max": 1, "seats": [],
        }
        summary = relay.session_summary(self.tmp, meta)
        self.assertEqual(summary["permission"], "full")

    def test_ui_uses_one_permission_selector_not_conflicting_toggles(self):
        ui = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "ui", "index.html")
        with open(ui, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('id="permissionMode"', src)
        self.assertNotIn('id="yolo"', src)
        self.assertNotIn('id="planMode"', src)

    def test_ask_turn_approval_unlocks_non_hook_adapter_once(self):
        class Probe(relay.Agent):
            cli = "probe"

            def build_cmd(self, message):
                self.seen_permission = self.effective_permission()
                return [sys.executable, "-c", "pass"]

            def _run_streaming(self, cmd, env, on_line):
                return 0, "ok", ""

            def parse(self, stdout):
                return stdout

        a = Probe(self.tmp, permission="ask",
                  on_approval=lambda request, abort: (True, "approved"))
        self.assertEqual(a.turn("work"), "ok")
        self.assertEqual(a.seen_permission, "auto")
        self.assertFalse(a._turn_approved, "approval leaked into the next turn")

    def test_ask_turn_denial_stays_gated(self):
        class Probe(relay.Agent):
            cli = "probe"

            def build_cmd(self, message):
                self.seen_permission = self.effective_permission()
                return [sys.executable, "-c", "pass"]

            def _run_streaming(self, cmd, env, on_line):
                return 0, "analysis only", ""

            def parse(self, stdout):
                return stdout

        a = Probe(self.tmp, permission="ask",
                  on_approval=lambda request, abort: (False, "denied"))
        a.turn("work")
        self.assertEqual(a.seen_permission, "ask")

    def test_rehydrate_empty_roster_has_a_defined_permission(self):
        seat = lambda i, provider: {
            "id": i, "provider": provider, "label": provider,
            "introduced": False, "pending": [], "session_id": None,
        }
        meta = {
            "v": relay.META_VERSION, "workspace": self.tmp,
            "ended": True, "yolo": False, "turns": 1, "rnd": 0,
            "max": 1, "seats": [seat(0, "claude"), seat(1, "gpt")],
        }
        state = relay.rehydrate(meta)
        self.assertEqual(state["permission"], "auto")


class StandingTurnVerdictTests(unittest.TestCase):
    """A denied tool is not the end of a turn.

    Verified live 2026-08-18: a Claude seat whose `Write` was denied retried
    the identical edit as `Bash`, so one refusal cost two modals. The "rest of
    turn" answers exist so a determined seat cannot out-click Josh — and they
    must expire with the turn, or they become a permission level nobody chose.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-verdict-")
        self.dir = os.path.join(self.tmp, "reqs")
        os.makedirs(self.dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_answer_grammar(self):
        self.assertEqual(relay.read_permission_answer("Allow once"), (True, False))
        self.assertEqual(relay.read_permission_answer("Allow rest of turn"),
                         (True, True))
        self.assertEqual(relay.read_permission_answer("Deny"), (False, False))
        self.assertEqual(relay.read_permission_answer("Deny rest of turn"),
                         (False, True))
        # Anything unrecognised — silence, a typo, an aborted modal — denies.
        for junk in ("", None, "maybe", "allowe", 7, "no"):
            self.assertEqual(relay.read_permission_answer(junk), (False, False))

    def _drain_one(self, agent, req_id):
        """Run the watcher long enough to answer one queued request."""
        stop = threading.Event()
        t = threading.Thread(target=agent._watch_approvals, args=(stop,),
                             daemon=True)
        t.start()
        ans = os.path.join(agent.approval_dir(), req_id + ".ans")
        for _ in range(200):
            if os.path.exists(ans):
                break
            time.sleep(0.02)
        stop.set()
        t.join(timeout=2)
        with open(ans, encoding="utf-8") as fh:
            return json.load(fh)

    def _queue(self, agent, req_id, tool):
        path = os.path.join(agent.approval_dir(), req_id + ".req")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"id": req_id, "tool": tool, "input": {}}, fh)

    def test_standing_verdict_answers_without_re_asking(self):
        asked = []
        a = relay.ClaudeAgent(self.tmp, permission="ask",
                              on_approval=lambda req, stop=None: (
                                  asked.append(req.get("tool")) or (False, "no")))
        a.set_turn_verdict(False)
        self._queue(a, "r1", "Write")
        verdict = self._drain_one(a, "r1")
        self.assertFalse(verdict["allow"])
        self.assertIn("rest of this turn", verdict["reason"])
        # The whole point: Josh was never consulted a second time.
        self.assertEqual(asked, [])

    def test_standing_allow_also_short_circuits(self):
        asked = []
        a = relay.ClaudeAgent(self.tmp, permission="ask",
                              on_approval=lambda req, stop=None: (
                                  asked.append(req.get("tool")) or (False, "no")))
        a.set_turn_verdict(True)
        self._queue(a, "r2", "Bash")
        verdict = self._drain_one(a, "r2")
        self.assertTrue(verdict["allow"])
        self.assertEqual(asked, [])

    def test_no_standing_verdict_still_asks(self):
        asked = []
        a = relay.ClaudeAgent(self.tmp, permission="ask",
                              on_approval=lambda req, stop=None: (
                                  asked.append(req.get("tool")) or (True, "yes")))
        self._queue(a, "r3", "Edit")
        verdict = self._drain_one(a, "r3")
        self.assertTrue(verdict["allow"])
        self.assertEqual(asked, ["Edit"])

    def test_verdict_does_not_survive_the_turn(self):
        class Stub(relay.ClaudeAgent):
            def build_cmd(self, message):
                return [sys.executable, "-c", "print('{\"result\": \"hi\"}')"]

            def parse(self, stdout):
                return "hi"

        a = Stub(self.tmp, permission="ask",
                 on_approval=lambda req, stop=None: (True, "yes"))
        a.set_turn_verdict(True)
        a.turn("go")
        self.assertIsNone(a._turn_verdict)


class ApprovalHubTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-approval-hub-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rich_decisions_fail_closed_and_preserve_feedback(self):
        self.assertEqual(relay.read_permission_decision("Approve once"),
                         (True, "once", ""))
        self.assertEqual(
            relay.read_permission_decision("Always allow Bash this session"),
            (True, "session", ""))
        self.assertEqual(
            relay.read_permission_decision("Deny: use the safer command"),
            (False, "once", "use the safer command"))
        self.assertEqual(relay.read_permission_decision("probably"),
                         (False, "once", ""))

    def test_risk_details_are_deterministic(self):
        high = relay.approval_request_details(
            {"tool": "Bash", "input": {"command": "rm -rf build"}},
            self.tmp)
        low = relay.approval_request_details(
            {"tool": "Bash", "input": {"command": "python -m pytest -q"}},
            self.tmp)
        edit = relay.approval_request_details({
            "tool": "Edit",
            "input": {"file_path": "relay.py", "old_string": "old",
                      "new_string": "new"},
        }, self.tmp)
        self.assertEqual(high["risk"], "high")
        self.assertEqual(low["risk"], "low")
        self.assertEqual(edit["risk"], "medium")
        self.assertEqual(edit["context_kind"], "diff")
        self.assertIn("relay.py", edit["blast_radius"])
        self.assertIn("- old", edit["context"])
        self.assertIn("+ new", edit["context"])

    def test_session_tool_grant_persists_and_skips_next_modal(self):
        class Store:
            def __init__(self, path):
                self.dir = path
                self.saved = 0

            def save(self, state):
                self.saved += 1

        class IO:
            def __init__(self):
                self.asked = 0
                self.events = []

            def ask_human(self, payload, abort=None):
                self.asked += 1
                self.payload = payload
                return "Always allow Bash this session"

            def emit(self, event, payload):
                self.events.append((event, payload))

        store, io = Store(self.tmp), IO()
        agent = relay.ClaudeAgent(self.tmp, permission="ask", name="Claude")
        state = {
            "agents": [agent], "slot_ids": [0], "providers": ["claude"],
            "workspace": self.tmp, "permission": "ask",
            "permission_grants": [], "store": store,
        }
        with mock.patch.object(relay, "_run_rounds", return_value="cap"), \
                mock.patch.object(relay, "write_outcome"):
            relay.run_rounds(state, io)

        request = {"id": "one", "tool": "Bash",
                   "input": {"command": "python -m pytest -q"},
                   "cwd": self.tmp}
        allowed, reason = agent.on_approval(request)
        self.assertTrue(allowed)
        self.assertIn("conversation", reason)
        self.assertEqual(io.asked, 1)
        self.assertIn("claude:bash", state["permission_grants"])
        self.assertEqual(store.saved, 1)
        self.assertEqual(io.payload["risk"], "low")
        self.assertIn("Always allow Bash this session", io.payload["options"])

        allowed, _ = agent.on_approval({**request, "id": "two"})
        self.assertTrue(allowed)
        self.assertEqual(io.asked, 1, "persisted grant reopened the modal")

    def test_session_permission_label_round_trip(self):
        for tool in ("Bash", "Edit", "Write", "Custom Tool", "Python"):
            label = relay.session_permission_label(tool)
            self.assertEqual(relay.read_permission_decision(label),
                             (True, "session", ""))
            self.assertEqual(relay.read_permission_decision(label.lower()),
                             (True, "session", ""))

        # Test canonical modal options roundtrip
        for tool in ("Bash", "Edit"):
            options = ["Approve once", relay.session_permission_label(tool),
                       "Deny", "Deny with feedback"]
            expected = [
                (True, "once", ""),
                (True, "session", ""),
                (False, "once", ""),
                (False, "once", "")
            ]
            results = [relay.read_permission_decision(opt) for opt in options]
            self.assertEqual(results, expected)

        # Extended options
        self.assertEqual(relay.read_permission_decision("Allow rest of turn"),
                         (True, "turn", ""))
        self.assertEqual(relay.read_permission_decision("Deny rest of turn"),
                         (False, "turn", ""))
        self.assertEqual(relay.read_permission_decision("Deny: avoid altering prod DB"),
                         (False, "once", "avoid altering prod DB"))

    def test_destructive_command_expansion(self):
        destructive = [
            "git push --force origin main",
            "git push -f origin main",
            "git push origin +feature:main",
            "mv old_file.py new_file.py",
            "Move-Item src dst",
            "ren old.txt new.txt",
            "rename-item a b",
            "echo 'hello' > output.txt",
            "pip install requests",
            "npm install lodash",
            "npm i express",
            "pnpm add axios",
            "yarn add react",
            "cargo install ripgrep",
        ]
        for cmd in destructive:
            req = relay.approval_request_details(
                {"tool": "Bash", "input": {"command": cmd}},
                self.tmp)
            self.assertEqual(req["risk"], "high", f"Expected high risk for: {cmd}")

        non_destructive = [
            "git push origin main",
            "python -m pytest",
            "npm test",
            "npm run build",
            "git status",
            "git diff",
            "Get-ChildItem",
        ]
        for cmd in non_destructive:
            req = relay.approval_request_details(
                {"tool": "Bash", "input": {"command": cmd}},
                self.tmp)
            self.assertNotEqual(req["risk"], "high", f"Did not expect high risk for: {cmd}")


if __name__ == "__main__":
    unittest.main()
