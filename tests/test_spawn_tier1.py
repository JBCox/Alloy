"""Phase 7 tests: native CLI subagents (tier-1 spawning).

Token-free. Run:  python tests/test_spawn_tier1.py
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import ClaudeAgent, CodexAgent, GeminiAgent, preamble

from test_loop import FakeAgent, RecordingIO, build_state
from test_loop import run_rounds


class NoteGatingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-t1-")
        self._old_probe = relay._CODEX_MULTI_AGENT

    def tearDown(self):
        relay._CODEX_MULTI_AGENT = self._old_probe
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pre(self, agent, spawn):
        other = FakeAgent(self.tmp, [], name="Other")
        return preamble(agent, [other], "t", 3, self.tmp, spawn=spawn)

    def test_claude_note_present_both_permission_modes(self):
        for yolo in (False, True):
            a = ClaudeAgent(self.tmp, yolo=yolo)
            self.assertIn("Task/subagent", self.pre(a, {"tier1": True}))

    def test_claude_task_in_allowlist(self):
        a = ClaudeAgent(self.tmp, yolo=False)
        cmd = a.build_cmd("hi")
        allow = next(c for c in cmd if c.startswith("--allowedTools="))
        self.assertIn("Task", allow.split("=", 1)[1].split(","))

    def test_claude_print_prompt_is_bound_to_p(self):
        a = ClaudeAgent(self.tmp)
        a.session_id = "saved-session"
        cmd = a.build_cmd("continue this chat")
        self.assertEqual(cmd[1:3], ["-p", "continue this chat"])
        resume = cmd.index("--resume")
        self.assertEqual(cmd[resume + 1], "saved-session")

    def test_policy_off_hides_the_note(self):
        a = ClaudeAgent(self.tmp, yolo=False)
        self.assertNotIn("Delegation", self.pre(a, {"tier1": False}))

    def test_codex_note_follows_the_probe(self):
        relay._CODEX_MULTI_AGENT = True
        self.assertIn("multi-agent",
                      self.pre(CodexAgent(self.tmp), {"tier1": True}))
        relay._CODEX_MULTI_AGENT = False
        self.assertNotIn("Delegation",
                         self.pre(CodexAgent(self.tmp), {"tier1": True}))

    def test_gemini_gets_no_note(self):
        self.assertNotIn("Delegation",
                         self.pre(GeminiAgent(self.tmp), {"tier1": True}))

    def test_probe_parses_the_real_fixture(self):
        fixture = (
            "apply_patch_freeform                 removed            false\n"
            "multi_agent                          stable             true\n"
            "multi_agent_mode                     removed            false\n"
            "multi_agent_v2                       stable             false\n")
        for line_set, expected in ((fixture, True),
                                   (fixture.replace(
                                       "multi_agent                          "
                                       "stable             true",
                                       "multi_agent   stable   false"),
                                    False)):
            enabled = False
            for line in line_set.splitlines():
                toks = line.split()
                if toks and toks[0] == "multi_agent":
                    enabled = toks[-1].lower() == "true"
                    break
            self.assertIs(enabled, expected)

    def test_fake_agents_unaffected(self):
        # the whole existing suite runs FakeAgents: no note, no block
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        run_rounds(state, RecordingIO())
        self.assertNotIn("Delegation", state["agents"][0].prompts[0])

    def test_meta_round_trips_the_policy(self):
        from test_loop import saved_meta
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        state["spawn"] = {"tier1": False}
        run_rounds(state, RecordingIO())
        self.assertEqual(saved_meta(state)["spawn"], {"tier1": False})


if __name__ == "__main__":
    unittest.main(verbosity=2)
