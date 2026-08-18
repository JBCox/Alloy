"""Phase 8 tests: relay-spawned one-shot helpers ([[SPAWN:]]).

Token-free — helper CLIs are stubbed via AGENT_TYPES. Run:
python tests/test_spawn_helpers.py
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import Agent, parse_spawn, run_rounds

from test_loop import FakeAgent, RecordingIO, build_state, saved_meta, jsonl_rows
from test_scheduler import agent_rows


class StubHelper(Agent):
    """What AGENT_TYPES['claude'] resolves to for helper construction."""
    name = "Stub"
    cli = "fake"
    replies = ["PONG"]
    prompts = []
    fail = False

    def turn(self, message, on_activity=None):
        StubHelper.prompts.append(message)
        if StubHelper.fail:
            raise RuntimeError("helper exploded")
        return StubHelper.replies[0]


def spawn_state(tmp, scripts, max_helpers=3, turns=2, labels=None):
    state = build_state(tmp, scripts, turns=turns, labels=labels)
    state["spawn"] = {"tier1": True, "max_helpers": max_helpers,
                      "helpers_used": 0}
    return state


class ParseSpawnTests(unittest.TestCase):
    def test_full_spec(self):
        self.assertEqual(parse_spawn("gpt:gpt-5.6-sol:low | do the thing"),
                         ("gpt", "gpt-5.6-sol", "low", "do the thing"))

    def test_provider_only(self):
        self.assertEqual(parse_spawn("gemini | reply PONG"),
                         ("gemini", None, None, "reply PONG"))

    def test_missing_task(self):
        for bad in ("gpt |", "gpt", " | task"):
            with self.assertRaises(ValueError):
                parse_spawn(bad)

    def test_unknown_provider(self):
        with self.assertRaises(ValueError) as cm:
            parse_spawn("grok | hi")
        self.assertIn("unknown provider", str(cm.exception))

    def test_label_rejected(self):
        with self.assertRaises(ValueError):
            parse_spawn("claude=Helper | hi")


class HelperFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-spawn-")
        self._old = dict(relay.AGENT_TYPES)
        relay.AGENT_TYPES["claude"] = StubHelper
        StubHelper.replies = ["PONG"]
        StubHelper.prompts = []
        StubHelper.fail = False

    def tearDown(self):
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_helper_result_reaches_requester_only(self):
        state = spawn_state(
            self.tmp,
            [["research this [[SPAWN: claude | say PONG]]", "a2"],
             ["b1", "b2"]],
            labels=["A", "B"])
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")
        a, b = state["agents"]
        meta = saved_meta(state)
        a_seen = "\n\n".join(a.prompts + meta["seats"][0]["pending"])
        b_seen = "\n\n".join(b.prompts + meta["seats"][1]["pending"])
        self.assertIn("returned for A:)\nPONG", a_seen)
        self.assertNotIn("PONG", b_seen.replace(
            "research this [[SPAWN: claude | say PONG]]", ""))
        # the helper got the task + one-shot framing
        self.assertEqual(len(StubHelper.prompts), 1)
        self.assertIn("one-shot helper spawned by A", StubHelper.prompts[0])
        self.assertIn("say PONG", StubHelper.prompts[0])
        # transcript: request system row + helper row with caption
        rows = jsonl_rows(state)
        self.assertTrue(any("spawned a" in r["text"]
                            for r in rows if r["speaker"] == "system"))
        helper_rows = [r for r in rows
                       if str(r["speaker"]).startswith("helper-")]
        self.assertEqual(len(helper_rows), 1)
        self.assertEqual(helper_rows[0]["text"], "PONG")
        self.assertEqual(helper_rows[0]["meta"], "helper for A")
        # meta debt settled
        self.assertEqual(meta["spawn"]["pending_helpers"], [])
        self.assertEqual(meta["spawn"]["helpers_used"], 1)

    def test_disabled_helpers_give_a_note(self):
        state = spawn_state(
            self.tmp,
            [["try [[SPAWN: claude | say PONG]]", "a2"], ["b1", "b2"]],
            max_helpers=0, labels=["A", "B"])
        run_rounds(state, RecordingIO())
        a_seen = "\n\n".join(state["agents"][0].prompts)
        self.assertIn("helpers are disabled", a_seen)
        self.assertEqual(len(StubHelper.prompts), 0)

    def test_budget_exhaustion(self):
        state = spawn_state(
            self.tmp,
            [["a [[SPAWN: claude | one]]",
              "b [[SPAWN: claude | two]]"],
             ["b1", "b2"]],
            max_helpers=1, turns=2, labels=["A", "B"])
        run_rounds(state, RecordingIO())
        a_seen = "\n\n".join(state["agents"][0].prompts
                             + saved_meta(state)["seats"][0]["pending"])
        self.assertIn("budget (1) is exhausted", a_seen)
        self.assertEqual(len(StubHelper.prompts), 1)

    def test_invalid_provider_is_surfaced(self):
        state = spawn_state(
            self.tmp,
            [["go [[SPAWN: grok | hi]]", "a2"], ["b1", "b2"]],
            labels=["A", "B"])
        run_rounds(state, RecordingIO())
        a_seen = "\n\n".join(state["agents"][0].prompts
                             + saved_meta(state)["seats"][0]["pending"])
        self.assertIn("was not run", a_seen)
        self.assertIn("unknown provider", a_seen)

    def test_helper_failure_is_noted_never_forged(self):
        StubHelper.fail = True
        state = spawn_state(
            self.tmp,
            [["go [[SPAWN: claude | boom]]", "a2"], ["b1", "b2"]],
            labels=["A", "B"])
        run_rounds(state, RecordingIO())
        meta = saved_meta(state)
        a_seen = "\n\n".join(state["agents"][0].prompts
                             + meta["seats"][0]["pending"])
        self.assertIn("failed and was NOT retried", a_seen)
        self.assertIn("helper exploded", a_seen)
        # no forged helper message row
        self.assertFalse([r for r in jsonl_rows(state)
                          if str(r["speaker"]).startswith("helper-")])
        self.assertEqual(meta["spawn"]["pending_helpers"], [])

    def test_two_spawns_in_one_reply_run_nothing(self):
        state = spawn_state(
            self.tmp,
            [["x [[SPAWN: claude | one]] [[SPAWN: claude | two]]", "a2"],
             ["b1", "b2"]],
            labels=["A", "B"])
        run_rounds(state, RecordingIO())
        self.assertEqual(len(StubHelper.prompts), 0)
        a_seen = "\n\n".join(state["agents"][0].prompts)
        self.assertIn("only one SPAWN or TEAM per reply", a_seen)

    def test_lost_helper_note_on_next_run(self):
        # simulate a crash: meta carries an unresolved pending helper
        state = spawn_state(self.tmp, [["a1"], ["b1"]], labels=["A", "B"],
                            turns=1)
        state["spawn"]["pending_helpers"] = [
            {"requester": 0, "spec": "gpt:gpt-5.6-sol", "task_head": "x"}]
        run_rounds(state, RecordingIO())
        a_seen = "\n\n".join(state["agents"][0].prompts)
        self.assertIn("was lost when the last run ended", a_seen)
        self.assertEqual(saved_meta(state)["spawn"]["pending_helpers"], [])

    def test_spawn_works_in_parallel_mode(self):
        state = spawn_state(
            self.tmp,
            [["go [[SPAWN: claude | say PONG]]", "a2"], ["b1", "b2"]],
            labels=["A", "B"])
        state["mode"] = "parallel"
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")
        meta = saved_meta(state)
        a_seen = "\n\n".join(state["agents"][0].prompts
                             + meta["seats"][0]["pending"])
        self.assertIn("returned for A:)\nPONG", a_seen)
        self.assertEqual(meta["spawn"]["pending_helpers"], [])

    def test_preamble_documents_spawn_only_when_enabled(self):
        state = spawn_state(self.tmp, [["a1"], ["b1"]], max_helpers=3,
                            turns=1, labels=["A", "B"])
        run_rounds(state, RecordingIO())
        self.assertIn("[[SPAWN:", state["agents"][0].prompts[0])
        state2 = spawn_state(self.tmp + "", [["a1"], ["b1"]], max_helpers=0,
                             turns=1, labels=["A", "B"])
        # fresh dir for the second store
        d2 = tempfile.mkdtemp(prefix="ai-chat-spawn2-")
        try:
            state2 = spawn_state(d2, [["a1"], ["b1"]], max_helpers=0,
                                 turns=1, labels=["A", "B"])
            run_rounds(state2, RecordingIO())
            self.assertNotIn("[[SPAWN:", state2["agents"][0].prompts[0])
        finally:
            shutil.rmtree(d2, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
