"""Phase 9 tests: sub-conversations ([[TEAM:]]).

Token-free — child seats are stubbed via AGENT_TYPES. Run:
python tests/test_spawn_teams.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import Agent, parse_team, run_rounds

from test_loop import FakeAgent, RecordingIO, build_state, saved_meta, jsonl_rows
from test_scheduler import agent_rows


class StubChild(Agent):
    """AGENT_TYPES stand-in for child seats AND the closing-report call.
    Every instance pops from the class-level queue."""
    name = "Stub"
    cli = "fake"
    replies = []

    def turn(self, message, on_activity=None):
        self.session_id = f"fake-{self.uid}"
        if not StubChild.replies:
            return "(dry)"
        return StubChild.replies.pop(0)


def team_state(tmp, scripts, max_teams=1, turns=3, labels=None):
    state = build_state(tmp, scripts, turns=turns, labels=labels)
    state["spawn"] = {"tier1": True, "max_helpers": 0, "helpers_used": 0,
                      "max_teams": max_teams, "teams_used": 0}
    return state


class ParseTeamTests(unittest.TestCase):
    def test_two_part_form(self):
        slots, opts, opener = parse_team("claude,gpt | build the thing")
        self.assertEqual(len(slots), 2)
        self.assertEqual(opts, {})
        self.assertEqual(opener, "build the thing")

    def test_three_part_form(self):
        slots, opts, opener = parse_team(
            "claude:claude-haiku-4-5:low,gemini | rounds=2 mode=speaker | go")
        self.assertEqual(opts, {"rounds": 2, "mode": "speaker"})
        self.assertEqual(slots[0][1], "claude-haiku-4-5")

    def test_rejects(self):
        for bad in ("claude | ",              # empty opener
                    "claude |x| y | z",       # too many parts
                    "claude | go",            # one seat
                    "claude,grok | go",       # unknown provider
                    "claude,gpt | rounds=x mode=speaker | go",  # bad opt val
                    "claude,gpt | pace=fast | go"):             # bad opt
            with self.assertRaises(ValueError, msg=bad):
                parse_team(bad)


class TeamFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-team-")
        self._old_types = dict(relay.AGENT_TYPES)
        self._old_sessions = relay.SESSIONS_DIR
        relay.SESSIONS_DIR = self.tmp        # children land in the temp dir
        relay.AGENT_TYPES["claude"] = StubChild
        StubChild.replies = []

    def tearDown(self):
        relay.SESSIONS_DIR = self._old_sessions
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_team_conversation(self, directive, max_teams=1):
        state = team_state(
            self.tmp,
            [[f"go {directive}", "a2", "a3"], ["b1", "b2", "b3"]],
            max_teams=max_teams, labels=["A", "B"])
        outcome = run_rounds(state, RecordingIO())
        return state, outcome

    def test_child_runs_and_reports_back(self):
        # child: two seats, 1 round (2 turns w/ wrap) + closing report call
        StubChild.replies = ["x says hi. [[WRAP]]", "y says bye",
                             "REPORT: we agreed on hi/bye"]
        state, outcome = self.run_team_conversation(
            "[[TEAM: claude,claude | rounds=1 | say hi and bye ]]")
        self.assertEqual(outcome, "cap")
        meta = saved_meta(state)
        # child session exists, linked both ways
        self.assertEqual(len(meta["children"] or []), 1)
        child_id = meta["children"][0]
        with open(os.path.join(self.tmp, child_id, "meta.json"),
                  encoding="utf-8") as f:
            child_meta = json.load(f)
        self.assertEqual(child_meta["parent"]["id"], meta["id"])
        self.assertEqual(child_meta["parent"]["label"], "A")
        self.assertTrue(child_meta["ended"])
        # depth 1: the child could not spawn anything
        self.assertEqual(child_meta["spawn"]["max_helpers"], 0)
        self.assertEqual(child_meta["spawn"]["max_teams"], 0)
        # the report reached the requester only
        a_seen = "\n\n".join(state["agents"][0].prompts
                             + meta["seats"][0]["pending"])
        b_seen = "\n\n".join(state["agents"][1].prompts
                             + meta["seats"][1]["pending"])
        self.assertIn("REPORT: we agreed on hi/bye", a_seen)
        self.assertNotIn("REPORT:", b_seen)
        self.assertIn(f"sessions/{child_id}", a_seen)
        # meta debt settled
        self.assertEqual(meta["spawn"]["pending_teams"], [])
        # the child transcript recorded the relayed opener
        with open(os.path.join(self.tmp, child_id, "messages.jsonl"),
                  encoding="utf-8") as f:
            child_rows = [json.loads(l) for l in f]
        self.assertTrue(any("relayed from A" in r["text"]
                            for r in child_rows))

    def test_teams_disabled_gives_a_note(self):
        state, _ = self.run_team_conversation(
            "[[TEAM: claude,claude | go ]]", max_teams=0)
        a_seen = "\n\n".join(state["agents"][0].prompts)
        self.assertIn("teams are disabled", a_seen)
        self.assertFalse(saved_meta(state).get("children"))

    def test_invalid_team_spec_is_surfaced(self):
        state, _ = self.run_team_conversation("[[TEAM: claude | solo ]]")
        a_seen = "\n\n".join(state["agents"][0].prompts
                             + saved_meta(state)["seats"][0]["pending"])
        self.assertIn("was not run", a_seen)
        self.assertIn("at least two seats", a_seen)

    def test_spawn_and_team_together_run_nothing(self):
        state, _ = self.run_team_conversation(
            "[[SPAWN: claude | x]] [[TEAM: claude,claude | y ]]")
        a_seen = "\n\n".join(state["agents"][0].prompts)
        self.assertIn("only one SPAWN or TEAM per reply", a_seen)
        self.assertFalse(saved_meta(state).get("children"))

    def test_rounds_clamped_to_child_ceiling(self):
        StubChild.replies = ["only. [[WRAP]]", "fine", "REPORT: done"]
        state, _ = self.run_team_conversation(
            "[[TEAM: claude,claude | rounds=99 | quick ]]")
        child_id = saved_meta(state)["children"][0]
        with open(os.path.join(self.tmp, child_id, "meta.json"),
                  encoding="utf-8") as f:
            child_meta = json.load(f)
        self.assertLessEqual(child_meta["max"], relay.CHILD_ROUNDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
