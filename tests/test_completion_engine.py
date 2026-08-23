"""Engine persistence for mechanical termination and semantic completion."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from test_loop import RecordingIO, build_state


def meta(state):
    with open(state["store"].meta_path, encoding="utf-8") as f:
        return json.load(f)


class CompletionEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-completion-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cap_is_paused_and_does_not_claim_resolution(self):
        state = build_state(self.tmp, [["a"], ["b"]], turns=1)
        self.assertEqual(relay.run_rounds(state, RecordingIO()), "cap")
        completion = meta(state)["completion"]
        self.assertEqual(completion,
                         {"lifecycle": "paused", "termination_reason": "cap",
                          "goal_verdict": "unknown"})

    def test_participant_wrap_is_mechanical_only(self):
        state = build_state(self.tmp, [["done [[WRAP]]"], ["last word"]],
                            turns=1)
        self.assertEqual(relay.run_rounds(state, RecordingIO()), "wrapped")
        completion = meta(state)["completion"]
        self.assertEqual(completion["termination_reason"], "wrap")
        self.assertEqual(completion["goal_verdict"], "unknown")
        self.assertNotIn("verdict_source", completion)

    def test_until_done_generic_cap_persists_precise_ceiling(self):
        state = build_state(self.tmp, [["a"], ["b"]], turns=5)
        state["until_done"] = True
        state["turn_ceiling"] = 2
        state["store"].save(state)
        self.assertEqual(relay.run_rounds(state, RecordingIO()), "cap")
        completion = meta(state)["completion"]
        self.assertEqual(completion["termination_reason"], "ceiling")
        record = __import__("outcome").read_outcome(state["store"].dir)
        self.assertEqual(record["hard_facts"]["termination_reason"], "ceiling")

    def test_panel_completion_has_synthesizer_author_but_no_invented_verdict(self):
        state = build_state(
            self.tmp,
            [["A draft", "A critique", "final synthesis"],
             ["B draft", "B critique"]], turns=3,
            labels=["A", "B"])
        state["mode"] = "panel"
        state["panel"] = {"synthesizer": 0}
        self.assertEqual(relay.run_rounds(state, RecordingIO()), "wrapped")
        completion = meta(state)["completion"]
        self.assertEqual(completion["termination_reason"], "wrap")
        self.assertEqual(completion["goal_verdict"], "unknown")
        synth = [r for r in relay.read_messages(state["store"].dir)
                 if r.get("intent") == "synthesis"]
        self.assertEqual([r["speaker"] for r in synth], [0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
