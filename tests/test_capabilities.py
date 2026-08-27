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
import re
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

    def test_claude_claims_shell_in_both_modes(self):
        # --allowedTools is an auto-approve list, NOT a whitelist: a non-yolo
        # seat really did run Bash and load a Skill in the live probe, so the
        # note must not deny capability the seat demonstrably has
        for a in (ClaudeAgent(self.tmp), ClaudeAgent(self.tmp, yolo=True)):
            self.assertIn("shell", a.capability_note())

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


class ConnectorGateTests(CapabilityBase):
    """MCP is the ONE thing --allowedTools actually gates (Bash and Skill run
    without being listed — verified live), and it reaches Josh's real Gmail /
    Drive / Calendar / ERP, so it stays off unless he opts in."""

    def setUp(self):
        super().setUp()
        self._old_mcp = relay._CLAUDE_MCP
        relay._CLAUDE_MCP = ["mcp__claude_ai_Gmail",
                             "mcp__plugin_superpowers-chrome_chrome"]

    def tearDown(self):
        relay._CLAUDE_MCP = self._old_mcp
        super().tearDown()

    def allowlist(self, agent):
        for c in agent.build_cmd("hi"):
            if str(c).startswith("--allowedTools="):
                return str(c).split("=", 1)[1].split(",")
        return None

    def test_off_by_default(self):
        allowed = self.allowlist(ClaudeAgent(self.tmp))
        self.assertNotIn("mcp__claude_ai_Gmail", allowed)
        self.assertIn("Task", allowed)          # the old list is untouched

    def test_on_adds_every_server(self):
        allowed = self.allowlist(ClaudeAgent(self.tmp, connectors=True))
        self.assertIn("mcp__claude_ai_Gmail", allowed)
        self.assertIn("mcp__plugin_superpowers-chrome_chrome", allowed)

    def test_yolo_needs_no_allowlist_at_all(self):
        self.assertIsNone(self.allowlist(ClaudeAgent(self.tmp, yolo=True)))

    def fenced(self, agent):
        """(strict flag present, the --mcp-config payload or None)."""
        cmd = [str(c) for c in agent.build_cmd("hi")]
        payload = None
        if "--mcp-config" in cmd:
            payload = cmd[cmd.index("--mcp-config") + 1]
        return ("--strict-mcp-config" in cmd), payload

    def test_connectors_off_fences_mcp_at_EVERY_rung(self):
        """RED GUARD — the shipped `full` rung leaked every connected server.

        `--allowedTools` gates MCP only in the `auto` branch. `full` emits
        --dangerously-skip-permissions, which bypasses every permission check
        including MCP, so a Full-access seat held Josh's real Gmail, Drive,
        Calendar, M365 and Epicor servers regardless of the Connected-apps
        checkbox — in runs that go unattended for hours. Verified live
        2026-08-26: without the fence a haiku seat at `full` listed
        mcp__claude_ai_Corvaer_Epicor__* tools; with it, NONE.

        The fence has to hold at every rung, so this asserts every rung.
        """
        for rung in ("read_only", "ask", "auto", "full"):
            with self.subTest(rung=rung):
                strict, payload = self.fenced(
                    ClaudeAgent(self.tmp, permission=rung))
                self.assertTrue(
                    strict,
                    f"{rung} must pass --strict-mcp-config when connectors "
                    f"are off")
                self.assertEqual(json.loads(payload), {"mcpServers": {}})

    def test_connectors_on_does_not_fence(self):
        for rung in ("auto", "full"):
            with self.subTest(rung=rung):
                strict, _ = self.fenced(
                    ClaudeAgent(self.tmp, permission=rung, connectors=True))
                self.assertFalse(strict)

    def test_fence_is_a_whitelist_not_a_blacklist(self):
        """A --disallowedTools built from claude_mcp_prefixes() fails OPEN:
        the helper returns [] on any probe failure, and an empty blacklist
        grants everything — the gate would vanish exactly when the probe
        breaks. The fence must not depend on the probe at all."""
        relay._CLAUDE_MCP = []          # simulate a dead probe
        strict, payload = self.fenced(ClaudeAgent(self.tmp, permission="full"))
        self.assertTrue(strict)
        self.assertEqual(json.loads(payload), {"mcpServers": {}})

    def test_capability_note_only_claims_connectors_when_on(self):
        self.assertNotIn("connected apps",
                         ClaudeAgent(self.tmp).capability_note())
        self.assertIn("connected apps",
                      ClaudeAgent(self.tmp, connectors=True).capability_note())

    def test_notes_do_not_promise_writes_the_rung_removes(self):
        """Peers route work by these sentences, so a rung that cannot write
        must not say it can. At read_only claude gets
        --disallowedTools=Write,Edit,NotebookEdit,Bash (which, unlike
        --allowedTools, really removes them) and codex gets
        sandbox_mode="read-only"."""
        for cls in (ClaudeAgent, relay.CodexAgent):
            with self.subTest(cls=cls.__name__):
                ro = cls(self.tmp, permission="read_only").capability_note()
                self.assertNotIn("writing files", ro)
                self.assertIn("read-only", ro)
                for rung in ("auto", "full"):
                    note = cls(self.tmp, permission=rung).capability_note()
                    self.assertIn("writing files", note)
                    self.assertIn("running shell commands", note)
        # claude's shell really is gone at read_only; codex's is only fenced
        self.assertNotIn("running shell commands",
                         ClaudeAgent(self.tmp,
                                     permission="read_only").capability_note())

    def test_no_seat_claims_a_browser_codex_exec_does_not_expose(self):
        """`codex features list` reports browser_use and computer_use as
        `stable true`, and neither is exposed in exec (print) mode — measured
        2026-08-26 by asking codex exec to enumerate its own tools inside
        Alloy's sandbox. Reading the feature flags and concluding GPT can
        browse is the trap; this pins the measurement."""
        for rung in ("read_only", "auto", "full"):
            note = relay.CodexAgent(self.tmp, permission=rung).capability_note()
            self.assertNotIn("browser", note.lower())
            self.assertNotIn("computer use", note.lower())

    def test_note_no_longer_denies_shell_or_skills(self):
        # the earlier note claimed non-yolo "CANNOT run shell commands";
        # live probe: Bash ran and a Skill loaded with neither in the list
        note = ClaudeAgent(self.tmp).capability_note()
        self.assertIn("running shell commands", note)
        self.assertIn("Skills", note)
        self.assertIn("Word, PDF", note)

    def test_probe_failure_grants_nothing(self):
        relay._CLAUDE_MCP = []
        allowed = self.allowlist(ClaudeAgent(self.tmp, connectors=True))
        self.assertTrue(all(not a.startswith("mcp__") for a in allowed))

    def test_prefix_spelling_matches_real_tool_names(self):
        # dots/colons/spaces -> _, hyphens preserved (verified against the
        # live `claude mcp list` output and real tool names)
        self.assertEqual(re.sub(r"[.\s:]", "_", "claude.ai Corvaer Epicor"),
                         "claude_ai_Corvaer_Epicor")
        self.assertEqual(re.sub(r"[.\s:]", "_",
                                "plugin:superpowers-chrome:chrome"),
                         "plugin_superpowers-chrome_chrome")


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
