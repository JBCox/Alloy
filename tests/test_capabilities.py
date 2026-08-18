"""Capability awareness: seats must know what the OTHERS can actually do.

The bug this guards: asked for an image in a Claude+GPT chat, Claude drew one
in code. Codex ships a real image tool (feature flag `image_generation`,
verified end-to-end 2026-08-17) and nothing in the preamble said so, so the
seat with the floor attempted work that belonged to its peer.

Token-free: no CLI is invoked (codex_features is stubbed).

Run:  python tests/test_capabilities.py
"""

import json
import os
import shutil
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

    def test_gemini_claims_images_because_the_relay_delivers_them(self):
        note = GeminiAgent(self.tmp).capability_note()
        self.assertIn("GENERATING IMAGES", note)
        self.assertIn("shared folder", note)


class GeminiImageHarvestTests(unittest.TestCase):
    """agy writes generated images into its own per-conversation folder and
    ignores the process cwd, so the relay copies them into the workspace."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-harvest-")
        self.ws = os.path.join(self.tmp, "workspace")
        self.brain = os.path.join(self.tmp, "brain")
        os.makedirs(self.ws)
        self._old_brain = relay.GEMINI_BRAIN
        relay.GEMINI_BRAIN = self.brain

    def tearDown(self):
        relay.GEMINI_BRAIN = self._old_brain
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agent(self, convo="convo-1"):
        a = GeminiAgent(self.ws)
        a.session_id = convo
        return a

    def brain_file(self, name, convo="convo-1", data=b"\x89PNG-ish"):
        d = os.path.join(self.brain, convo)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_copies_new_images_into_the_workspace(self):
        a = self.agent()
        a.before_run()
        self.brain_file("apple_1786992618499.jpg")
        copied = a.harvest_images()
        self.assertEqual([os.path.basename(p) for p in copied], ["apple.jpg"])
        # the epoch-ms suffix generate_image appends is stripped
        self.assertTrue(os.path.isfile(os.path.join(self.ws, "apple.jpg")))

    def test_ignores_images_from_earlier_turns(self):
        a = self.agent()
        self.brain_file("old_1786000000000.jpg")
        a.before_run()                      # snapshot: old one already there
        self.brain_file("new_1786992618499.jpg")
        copied = a.harvest_images()
        self.assertEqual([os.path.basename(p) for p in copied], ["new.jpg"])
        self.assertFalse(os.path.exists(os.path.join(self.ws, "old.jpg")))

    def test_never_overwrites_an_existing_file(self):
        with open(os.path.join(self.ws, "apple.jpg"), "wb") as f:
            f.write(b"mine")
        a = self.agent()
        a.before_run()
        self.brain_file("apple_1786992618499.jpg")
        copied = a.harvest_images()
        self.assertEqual([os.path.basename(p) for p in copied], ["apple-2.jpg"])
        with open(os.path.join(self.ws, "apple.jpg"), "rb") as f:
            self.assertEqual(f.read(), b"mine")

    def test_non_images_are_left_alone(self):
        a = self.agent()
        a.before_run()
        self.brain_file("notes_1786992618499.txt")
        self.assertEqual(a.harvest_images(), [])

    def test_missing_brain_dir_is_not_an_error(self):
        a = self.agent(convo="never-ran")
        a.before_run()
        self.assertEqual(a.harvest_images(), [])

    def test_no_session_id_yet_is_not_an_error(self):
        a = GeminiAgent(self.ws)            # fresh conversation, no id
        a.before_run()
        self.assertEqual(a.harvest_images(), [])

    def test_parse_harvests_using_the_id_it_just_learned(self):
        # a FIRST turn only learns the conversation id from this very reply,
        # so the harvest has to run after session_id is set
        a = GeminiAgent(self.ws)
        a.before_run()
        self.brain_file("pear_1786993636710.jpg", convo="fresh-id")
        reply = a.parse(json.dumps({"conversation_id": "fresh-id",
                                    "response": "Here is the pear."}))
        self.assertEqual(reply, "Here is the pear.")
        self.assertEqual([os.path.basename(p) for p in a.last_images],
                         ["pear.jpg"])
        self.assertTrue(os.path.isfile(os.path.join(self.ws, "pear.jpg")))

    def test_a_copy_failure_never_breaks_the_turn(self):
        a = self.agent()
        a.before_run()
        self.brain_file("apple_1786992618499.jpg")
        orig = relay.shutil.copy2

        def boom(*args, **kw):
            raise OSError("disk gone")
        relay.shutil.copy2 = boom
        try:
            self.assertEqual(a.harvest_images(), [])
        finally:
            relay.shutil.copy2 = orig


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
