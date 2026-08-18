"""Capability awareness: seats must know what the OTHERS can actually do.

The bug this guards: asked for an image in a Claude+GPT chat, Claude drew one
in code. Codex ships a real image tool (feature flag `image_generation`,
verified end-to-end 2026-08-17) and nothing in the preamble said so, so the
seat with the floor attempted work that belonged to its peer.

Token-free: no CLI is invoked (codex_features is stubbed).

Run:  python tests/test_capabilities.py
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import ClaudeAgent, CodexAgent, GeminiAgent, preamble

from test_loop import FakeAgent


class CapabilityBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-cap-test-")
        self._old = relay._CODEX_FEATURES
        relay._CODEX_FEATURES = {"image_generation": True,
                                 "multi_agent": False}

    def tearDown(self):
        relay._CODEX_FEATURES = self._old


class CapabilityNoteTests(CapabilityBase):
    def test_claude_states_it_cannot_generate_images(self):
        note = ClaudeAgent(self.tmp).capability_note()
        self.assertIn("CANNOT generate images", note)
        self.assertIn("web search", note)
        # non-yolo has no Bash in the allowlist, so it must not claim shell
        self.assertNotIn("shell", note)

    def test_claude_claims_shell_only_in_yolo(self):
        self.assertIn("shell", ClaudeAgent(self.tmp, yolo=True).capability_note())

    def test_codex_claims_images_when_the_flag_is_on(self):
        note = CodexAgent(self.tmp).capability_note()
        self.assertIn("GENERATING IMAGES", note)
        self.assertIn("shared folder", note)

    def test_codex_stays_silent_about_images_when_the_flag_is_off(self):
        relay._CODEX_FEATURES = {"image_generation": False}
        note = CodexAgent(self.tmp).capability_note()
        self.assertNotIn("IMAGE", note.upper())

    def test_codex_stays_silent_when_the_probe_fails(self):
        # any failure -> {} -> never promise what the CLI may not grant
        relay._CODEX_FEATURES = {}
        self.assertNotIn("IMAGE", CodexAgent(self.tmp).capability_note().upper())

    def test_gemini_does_not_offer_itself_for_image_work(self):
        # it HAS the tool but writes into its own private dir (verified) —
        # an image the others cannot see is not a routable capability
        note = GeminiAgent(self.tmp).capability_note()
        self.assertIn("NOT the seat", note)
        self.assertIn("private", note)


class PreambleCapabilityBlockTests(CapabilityBase):
    def seats(self):
        return [ClaudeAgent(self.tmp), CodexAgent(self.tmp)]

    def test_every_seat_sees_every_participant_capability(self):
        seated = self.seats()
        for me in seated:
            text = preamble(me, [a for a in seated if a is not me], "topic",
                            3, self.tmp, roster=seated)
            self.assertIn("What each participant can actually do", text)
            self.assertIn("Claude:", text)
            self.assertIn("GPT:", text)
            # the routing fact itself reaches BOTH seats
            self.assertIn("GENERATING IMAGES", text)
            self.assertIn("CANNOT generate images", text)

    def test_block_tells_seats_to_hand_work_over(self):
        seated = self.seats()
        text = preamble(seated[0], seated[1:], "topic", 3, self.tmp,
                        roster=seated)
        self.assertIn("let them do it instead of approximating it yourself",
                      text)

    def test_absent_when_no_seat_declares_capabilities(self):
        # FakeAgent inherits the base no-op, so every existing preamble test
        # and transcript stays byte-identical
        seated = [FakeAgent(self.tmp, []), FakeAgent(self.tmp, [], name="B")]
        text = preamble(seated[0], seated[1:], "topic", 3, self.tmp,
                        roster=seated)
        self.assertNotIn("What each participant can actually do", text)

    def test_absent_for_a_single_seat_chat(self):
        # nobody to defer to: the block would be noise
        solo = [ClaudeAgent(self.tmp)]
        text = preamble(solo[0], [], "topic", 3, self.tmp, roster=solo)
        self.assertNotIn("What each participant can actually do", text)

    def test_stays_within_a_few_lines(self):
        # the whole prompt is ONE argv element under Windows' ~32k cap
        seated = [ClaudeAgent(self.tmp), CodexAgent(self.tmp),
                  GeminiAgent(self.tmp)]
        text = preamble(seated[0], seated[1:], "t", 3, self.tmp, roster=seated)
        block = text.split("What each participant")[1].split("\n\n")[0]
        self.assertLess(len(block), 1200, block)


class FeatureFlagParsingTests(unittest.TestCase):
    def test_flags_parse_from_the_features_table(self):
        relay._CODEX_FEATURES = None
        rows = ["image_generation                     stable             true",
                "multi_agent                          under development  false",
                "apply_patch_freeform                 removed            false"]
        parsed = {}
        for line in rows:
            toks = line.split()
            if len(toks) >= 2:
                parsed[toks[0]] = toks[-1].lower() == "true"
        self.assertEqual(parsed, {"image_generation": True,
                                  "multi_agent": False,
                                  "apply_patch_freeform": False})
        relay._CODEX_FEATURES = parsed
        self.assertTrue(relay.codex_image_gen_enabled())
        self.assertFalse(relay.codex_multi_agent_enabled())
        relay._CODEX_FEATURES = None


if __name__ == "__main__":
    unittest.main(verbosity=1)
