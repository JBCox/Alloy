"""Token-free tests for the one-shot auto-title side call (feature #15).

After the FIRST committed reply, the loop's front-end seam `io.auto_title`
runs relay.maybe_auto_title: ONE stateless side call over opener + first
reply renames the session. The builder seam (relay.build_title_agent) is
stubbed here exactly the way test_continuous stubs build_supervisor — a real
loop suite must be structurally token-free, and the headless LoopIO default
is a no-op, so every OTHER suite keeps paying nothing.

Run:  python tests/test_auto_title.py
"""

import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
import fork as fork_mod
from test_loop import FakeAgent, RecordingIO, build_state, saved_meta


class TitleIO(RecordingIO):
    """RecordingIO plus the REAL front-end behavior: run the engine's
    maybe_auto_title at the barrier, like CLIIO/_AppIO do."""

    def auto_title(self, state):
        relay.maybe_auto_title(state, self)


class AutoTitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-title-")
        self.built = []
        self.reply = "A Short Title"
        self.builder_calls = 0
        self.boom = False

        def fake_builder(state):
            self.builder_calls += 1
            if self.boom:
                raise RuntimeError("provider down")
            agent = FakeAgent(state["workspace"], [self.reply],
                              name="Relay title")
            self.built.append(agent)
            return agent

        self._orig = relay.build_title_agent
        relay.build_title_agent = fake_builder
        # record what prompt the side call actually received
        self.prompts = []

        orig_turn = FakeAgent.turn

        def spy_turn(self_agent, message, on_activity=None):
            self.prompts.append(message)
            return orig_turn(self_agent, message, on_activity)

        self._orig_turn = FakeAgent.turn
        FakeAgent.turn = spy_turn

    def tearDown(self):
        FakeAgent.turn = self._orig_turn
        relay.build_title_agent = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_titles_once_after_first_commit(self):
        state = build_state(self.tmp, [["first reply", "a2"], ["b1", "b2"]],
                            turns=2)
        io = TitleIO()
        outcome = relay.run_rounds(state, io)
        self.assertEqual(outcome, "cap")
        # exactly one side call, made only AFTER the first committed turn
        self.assertEqual(len(self.built), 1)
        prompt = "".join(self.prompts)
        self.assertIn("test", prompt)          # opener/topic text rode along
        self.assertIn("first reply", prompt)   # so did the first reply
        # meta.json carries the new title AND the once-flag
        meta = saved_meta(state)
        self.assertEqual(meta["title"], "A Short Title")
        self.assertTrue(meta["auto_titled"])
        self.assertEqual(state["title"], "A Short Title")
        # the rail can refresh live: a session_title event named the chat id
        titles = [p for e, p in io.events if e == "session_title"]
        self.assertEqual(titles, [{"session_id": state["store"].id,
                                   "title": "A Short Title"}])

    def test_helper_spec_routing_ox_room_never_spends_claude(self):
        # all-Ox room: helper_spec falls to the seat provider, not claude
        spec = relay.helper_spec(["ox", "ox"])
        self.assertEqual(spec.get("provider"), "ox")
        # a moderated room hands the side work to Josh's chosen helper
        spec = relay.helper_spec(["ox", "ox"],
                                 moderator_spec={"provider": "gpt"})
        self.assertEqual(spec.get("provider"), "gpt")

    def test_side_call_failure_is_a_silent_one_time_skip(self):
        self.boom = True
        state = build_state(self.tmp, [["a1", "a2"], ["b1", "b2"]], turns=2)
        io = TitleIO()
        outcome = relay.run_rounds(state, io)
        self.assertEqual(outcome, "cap")      # never fails the conversation
        meta = saved_meta(state)
        self.assertEqual(meta["title"], "test")     # old title kept
        self.assertTrue(meta["auto_titled"])  # marked BEFORE the failed call
        titles = [p for e, p in io.events if e == "session_title"]
        self.assertEqual(titles, [])          # no event without a new title
        # no retry at later boundaries: still exactly one builder attempt
        self.assertEqual(self.builder_calls, 1)

    def test_no_retitle_on_resume_or_fork(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        relay.run_rounds(state, TitleIO())
        calls_after_first_run = self.builder_calls
        self.assertEqual(calls_after_first_run, 1)
        # resume: same state, another run — the once-flag forbids a second call
        state["max"] += 1
        relay.run_rounds(state, TitleIO())
        self.assertEqual(self.builder_calls, calls_after_first_run)
        # fork: fork.py copies meta wholesale and only sanitizes identity
        # fields, so the flag travels with the copy and the fork never re-titles
        sid = state["store"].id
        root = os.path.dirname(state["store"].dir)
        out = fork_mod.fork_session(sid, sessions_dir=root)
        self.assertNotIn("error", out)
        with open(os.path.join(root, out["id"], "meta.json"),
                  encoding="utf-8") as f:
            fork_meta = json.load(f)
        self.assertTrue(fork_meta["auto_titled"])
        # fork.py suffixes its own provenance marker onto the copied title
        self.assertEqual(fork_meta["title"], "A Short Title (fork)")

    def test_clean_title_sanitizes_model_prose(self):
        cases = {
            '"Quoted Title"': "Quoted Title",
            "**Bold** start": "Bold start",
            "first line\nsecond line": "first line",
            "one two three four five six seven eight nine ten":
                "one two three four five six seven eight",
            "": "",
            "  ": "",
        }
        for raw, want in cases.items():
            self.assertEqual(relay.clean_title(raw), want, repr(raw))

    def test_app_io_gate(self):
        # The app front end runs the side call ONLY on the production flag:
        # headless Api instances stay token-free structurally.
        import types
        import app as app_mod
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)

        def make_io(enabled):
            io = app_mod._AppIO.__new__(app_mod._AppIO)   # no real Api/Run
            api = types.SimpleNamespace(_side_calls_enabled=enabled)
            api.emit = lambda event, payload=None: None   # capture-free stub
            io._api = api
            io._run = types.SimpleNamespace(
                id="fake-run", stop_flag=threading.Event(),
                human_q=queue.Queue(), thinking={}, staged_roles=[])
            return io

        io = make_io(False)
        relay.run_rounds(state, io)
        self.assertEqual(saved_meta(state)["title"], "test")
        self.assertEqual(self.builder_calls, 0)      # gate held: no side call
        # enabled: same conversation resumes WITHOUT re-titling (flag was not
        # persisted by the gated run because the side call never ran)... it
        # HAS run now, so this second pass must fire exactly once
        state["max"] += 1
        relay.run_rounds(state, make_io(True))
        self.assertEqual(self.builder_calls, 1)
        self.assertEqual(state["title"], "A Short Title")


if __name__ == "__main__":
    unittest.main(verbosity=2)
