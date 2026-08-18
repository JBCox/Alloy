"""Phase 4 tests: the until-done end condition and its safety ceiling.

Token-free. Run:  python tests/test_until_done.py
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import run_rounds

from test_loop import RecordingIO, build_state, saved_meta, jsonl_rows
from test_scheduler import agent_rows


def ud_state(tmp, scripts, ceiling, mode="round_robin", labels=None):
    state = build_state(tmp, scripts, turns=3, labels=labels)
    state["mode"] = mode
    state["until_done"] = True
    state["turn_ceiling"] = ceiling
    return state


class UntilDoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-ud-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_never_wrapping_seats_hit_the_ceiling(self):
        # scripts of 10 replies each, ceiling 4 -> exactly 4 turns, note saved
        state = ud_state(self.tmp,
                         [[f"a{n}" for n in range(10)],
                          [f"b{n}" for n in range(10)]], ceiling=4)
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")
        self.assertEqual(len(agent_rows(state)), 4)
        sys_rows = [r["text"] for r in jsonl_rows(state)
                    if r["speaker"] == "system"]
        self.assertTrue(any("Safety ceiling reached" in t for t in sys_rows))
        self.assertEqual(saved_meta(state)["until_done"], True)
        self.assertEqual(saved_meta(state)["turn_ceiling"], 4)

    def test_wrap_ends_before_the_ceiling(self):
        state = ud_state(self.tmp,
                         [["a1", "done. [[WRAP]]"], ["b1", "b-close"]],
                         ceiling=40)
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "wrapped")
        self.assertEqual(agent_rows(state),
                         ["a1", "b1", "done. [[WRAP]]", "b-close"])

    def test_closing_is_exempt_from_the_ceiling(self):
        # wrap lands exactly ON the ceiling turn; the other seat still closes
        state = ud_state(self.tmp,
                         [["done. [[WRAP]]"], ["b-close"]], ceiling=1)
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "wrapped")
        self.assertEqual(agent_rows(state), ["done. [[WRAP]]", "b-close"])

    def test_until_done_composes_with_speaker_mode(self):
        # budget check replaced by the ceiling in dynamic modes too
        state = ud_state(self.tmp,
                         [["a. [[NEXT: B]]"] * 5, ["b. [[NEXT: A]]"] * 5],
                         ceiling=3, mode="speaker", labels=["A", "B"])
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")
        self.assertEqual(len(agent_rows(state)), 3)

    def test_ceiling_command(self):
        state = ud_state(self.tmp,
                         [[f"a{n}" for n in range(10)],
                          [f"b{n}" for n in range(10)]], ceiling=10)
        io = RecordingIO(human_script=[[], [], ["/ceiling 3"]])
        run_rounds(state, io)
        # /ceiling 3 arrives with 2 turns taken -> clamps to max(turn, 3) = 3
        self.assertEqual(state["turn_ceiling"], 3)
        self.assertEqual(len(agent_rows(state)), 3)

    def test_turns_command_redirects_in_until_done(self):
        state = ud_state(self.tmp, [["a1", "a2"], ["b1", "b2"]], ceiling=4)
        io = RecordingIO(human_script=[["/turns 1"]])
        run_rounds(state, io)
        sys_rows = [r["text"] for r in jsonl_rows(state)
                    if r["speaker"] == "system"]
        self.assertTrue(any("until done" in t and "/ceiling" in t
                            for t in sys_rows), sys_rows)
        self.assertEqual(len(agent_rows(state)), 4)   # cap unchanged

    def test_ceiling_only_for_until_done(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        io = RecordingIO(human_script=[["/ceiling 5"]])
        run_rounds(state, io)
        self.assertIsNone(state.get("turn_ceiling"))
        sys_rows = [r["text"] for r in jsonl_rows(state)
                    if r["speaker"] == "system"]
        self.assertTrue(any("only applies to until-done" in t
                            for t in sys_rows))

    def test_preamble_swaps_the_cap_line(self):
        state = ud_state(self.tmp, [["a1"], ["b1"]], ceiling=7)
        run_rounds(state, RecordingIO())
        first_prompt = state["agents"][0].prompts[0]
        self.assertIn("runs until the task is genuinely done", first_prompt)
        self.assertIn("7 total turns", first_prompt)
        self.assertNotIn("runs at most", first_prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
