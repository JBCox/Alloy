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
import app


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

    def test_watcher_hands_the_callback_a_CALLABLE_abort(self):
        """RED GUARD — this exact bug shipped and nothing could see it.

        Every consumer of the abort seam CALLS it: `_AppIO.ask_human` and
        `CLIIO.ask_human` both evaluate `abort and abort()`, and `ask_abort`
        calls `abort()`. The watcher used to hand over the `threading.Event`
        itself, which is truthy but NOT callable — so the TypeError landed in
        `_watch_approvals`' blanket except and was answered as DENY, with the
        reason "Alloy approval failed ('Event' object is not callable)".
        Result: every mid-turn approval in the app silently auto-denied while
        the modal flashed open and shut.

        The old stubs all took `abort` and ignored it, which is precisely why
        six passing tests proved nothing. This one uses the argument the way
        the real consumers do.
        """
        captured = {}

        def on_approval(req, abort=None):
            captured["callable"] = callable(abort)
            captured["abort"] = abort
            # byte-for-byte what the real front ends do with it
            captured["during_turn"] = bool(abort and abort())
            return True, "yes"

        a = relay.ClaudeAgent(self.tmp, permission="ask",
                              on_approval=on_approval)
        self._queue(a, "r-abort", "Write")
        verdict = self._drain_one(a, "r-abort")

        self.assertTrue(captured.get("callable"),
                        "the watcher must pass a CALLABLE abort, not the Event")
        self.assertFalse(captured["during_turn"],
                         "abort must read False while the turn is still live")
        # _drain_one sets the stop event before returning, so the same callable
        # must now report the turn is over — proving it is wired to the Event
        # and not just any no-op lambda.
        self.assertTrue(captured["abort"]())
        self.assertTrue(verdict["allow"])
        self.assertNotIn("approval failed", verdict["reason"])

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

class FakeWindow:
    """Enough window for Api.emit; the events themselves are not the subject
    here — the AGENTS are."""

    def evaluate_js(self, script):
        pass


def scripted_from(cls, reply):
    """A REAL adapter subclass whose turn is canned.

    Deliberately not a hand-written FakeAgent: the whole question here is what
    `build_cmd` emits, so the class under test has to be the shipping one.
    Only `turn` is replaced, which is the one method that would spend tokens.
    The session id is re-captured exactly as the real `parse()` does, or
    `continue_block` rightly rules the chat unresumable.
    """

    class Scripted(cls):
        def turn(self, message, on_activity=None):
            self.session_id = f"fake-session-{self.uid}"
            return reply

    return Scripted


class AppBridgePermissionTests(unittest.TestCase):
    """The composer's permission pill, end to end through the real app.Api.

    Everything above this line tests relay, and relay was always right. The
    APP was not, and had no coverage here at all: `Api._conversation` read
    only the legacy `yolo` key, so a cfg of permission="read_only" or "ask"
    arrived as False, `Agent.__init__` fell back to DEFAULT_PERMISSION, and
    two of the four rungs on Josh's pill did nothing whatsoever — the seat ran
    at "auto" and meta.json recorded "auto", so even the reopened chat agreed
    with itself. It looked healthy from every angle a relay-only suite can
    see, which is the whole argument for testing the bridge and not just the
    engine.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-app-permissions-")
        self._old_app_dir = app.SESSIONS_DIR
        self._old_relay_dir, self._old_tabs = relay.SESSIONS_DIR, relay.TABS_FILE
        # relay's OWN globals, not just the app's: session_path() and
        # write_tabs() read relay's, so a test that redirects only
        # app.SESSIONS_DIR still writes the real sessions/tabs.json.
        app.SESSIONS_DIR = relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        self._old_types = dict(relay.AGENT_TYPES)
        relay.AGENT_TYPES["claude"] = scripted_from(relay.ClaudeAgent, "c1")
        relay.AGENT_TYPES["gpt"] = scripted_from(relay.CodexAgent, "g1")

    def tearDown(self):
        app.SESSIONS_DIR = self._old_app_dir
        relay.SESSIONS_DIR, relay.TABS_FILE = self._old_relay_dir, self._old_tabs
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self, **cfg):
        """One real conversation with two scripted seats (the app refuses a
        solo room), driven through the same `_conversation` the Send button
        reaches."""
        api = app.Api()
        api._window = FakeWindow()
        api._conversation(dict(
            {"opener": "hi", "turns": 1,
             "seats": [{"id": 0, "provider": "claude", "enabled": True},
                       {"id": 1, "provider": "gpt", "enabled": True}]},
            **cfg))
        api._emit_q.join()
        self.assertIsNotNone(api._conv, "the conversation never started")
        return api

    def _meta(self, api):
        with open(os.path.join(api._conv["store"].dir, "meta.json"),
                  encoding="utf-8") as f:
            return json.load(f)

    def test_read_only_cfg_reaches_the_seats_and_their_command_lines(self):
        api = self._start(permission="read_only")
        agents = api._conv["agents"]
        self.assertEqual([a.effective_permission() for a in agents],
                         ["read_only", "read_only"])
        claude = agents[0].build_cmd("hello")
        # The real plan-mode pair: --permission-mode plan is Claude's own
        # read-only mode, and --disallowedTools actually REMOVES the write
        # tools (--allowedTools would merely auto-approve them).
        self.assertIn("--permission-mode", claude)
        self.assertEqual(claude[claude.index("--permission-mode") + 1], "plan")
        self.assertIn("--disallowedTools=Write,Edit,NotebookEdit,Bash", claude)
        self.assertNotIn("--dangerously-skip-permissions", claude)
        # Not a claude-shaped accident: the rung reached the other seat too.
        self.assertIn('sandbox_mode="read-only"', agents[1].build_cmd("hello"))

    def test_ask_first_cfg_reaches_the_seats_and_wires_the_approval_channel(self):
        """`ask` needs more than a flag: run_rounds hangs the approval
        callback off any seat whose permission is "ask" and NULLS it for every
        other rung, and `effective_permission` collapses "ask" to read-only
        when that channel is missing (a gate nobody is listening to must fail
        closed). An "ask" that survives both is proof the pill reached the
        engine, not merely the constructor."""
        api = self._start(permission="ask")
        agents = api._conv["agents"]
        self.assertEqual([a.permission for a in agents], ["ask", "ask"])
        for a in agents:
            self.assertTrue(callable(a.on_approval),
                            f"{a.name} has no approval channel")
            self.assertEqual(a.effective_permission(), "ask")
        claude = agents[0].build_cmd("hello")
        self.assertIn("--settings", claude, "approval hook not installed")

    def test_meta_records_the_rung_the_chat_actually_ran_with(self):
        api = self._start(permission="read_only")
        self.assertEqual(api._conv["permission"], "read_only")
        meta = self._meta(api)
        self.assertEqual(meta["permission"], "read_only")
        self.assertFalse(meta["yolo"])
        self.assertEqual(
            relay.session_summary(api._conv["store"].dir)["permission"],
            "read_only")

    def test_legacy_yolo_cfg_is_still_the_full_rung(self):
        """Older saved configs — and the UI's own compatibility key — say
        `yolo: true` and nothing else."""
        api = self._start(yolo=True)
        agents = api._conv["agents"]
        self.assertEqual([a.permission for a in agents], ["full", "full"])
        self.assertIn("--dangerously-skip-permissions",
                      agents[0].build_cmd("hello"))
        self.assertEqual(self._meta(api)["permission"], "full")

    def test_the_full_rung_keeps_the_legacy_yolo_spelling_truthful(self):
        """The other direction: a named "full" must still read as yolo to
        every old reader (state, meta, Agent.yolo), or the two spellings of
        one fact drift apart."""
        api = self._start(permission="full")
        self.assertTrue(api._conv["yolo"])
        self.assertTrue(all(a.yolo for a in api._conv["agents"]))
        self.assertTrue(self._meta(api)["yolo"])

    def test_an_unrecognised_rung_falls_back_and_never_grants_more(self):
        api = self._start(permission="wishful-thinking")
        self.assertEqual(api._conv["permission"], relay.DEFAULT_PERMISSION)
        self.assertEqual([a.permission for a in api._conv["agents"]],
                         ["auto", "auto"])

    def test_continuing_a_chat_keeps_the_rung_it_started_with(self):
        """`_continue` never rebuilds agents, so the rung rides the state —
        but the pill is locked once seated, and a continue that quietly reset
        it to the default would be the same bug one turn later."""
        api = self._start(permission="read_only")
        api._continue({"opener": "again", "turns": 1})
        api._emit_q.join()
        self.assertEqual(api._conv["permission"], "read_only")
        self.assertEqual([a.effective_permission() for a in api._conv["agents"]],
                         ["read_only", "read_only"])
        self.assertEqual(self._meta(api)["permission"], "read_only")

    def test_reopening_a_chat_restores_the_rung(self):
        """The third leg: a new process rebuilds the seats from meta through
        relay.rehydrate. That side was always correct — it was simply fed a
        meta that said "auto" no matter what Josh had picked."""
        api = self._start(permission="read_only")
        session_id = os.path.basename(api._conv["store"].dir)
        reopened = app.Api()
        reopened._window = FakeWindow()
        result = reopened.open_session(session_id)
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["session"]["permission"], "read_only")
        self.assertEqual(reopened._conv["permission"], "read_only")
        self.assertEqual([a.permission for a in reopened._conv["agents"]],
                         ["read_only", "read_only"])

if __name__ == "__main__":
    unittest.main()
