"""Phase 3 tests: peel_directives, speaker mode, moderator mode.

Token-free — FakeAgents and a scripted fake moderator. Run:
python tests/test_modes.py
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import Agent, peel_directives, wrap_called, run_rounds

from test_loop import FakeAgent, RecordingIO, build_state, saved_meta, jsonl_rows
from test_scheduler import RehydratableFake, attach_runtime, agent_rows


class PeelDirectiveTests(unittest.TestCase):
    def test_wrap_sentence_close_fires(self):
        self.assertTrue(wrap_called("Good place to stop. [[WRAP]]"))

    def test_wrap_own_line_fires(self):
        self.assertTrue(wrap_called("All done.\n[[WRAP]]"))

    def test_mid_reply_mention_does_not_fire(self):
        self.assertFalse(wrap_called(
            "the token is [[WRAP]] which ends things"))
        _, hits, _ = peel_directives(
            "as I said, [[NEXT: GPT]] is the format we agreed on")
        self.assertEqual(hits, [])

    def test_quoted_and_backticked_do_not_fire(self):
        self.assertFalse(wrap_called('you would write "[[WRAP]]"'))
        self.assertFalse(wrap_called("you would write `[[WRAP]]`"))
        _, hits, _ = peel_directives("the token is `[[NEXT: GPT]]`")
        self.assertEqual(hits, [])

    def test_trailing_period_does_not_fire(self):
        _, hits, _ = peel_directives("Over to you, [[NEXT: GPT]].")
        self.assertEqual(hits, [])

    def test_next_sentence_close_fires(self):
        _, hits, _ = peel_directives("Over to you. [[NEXT: gpt]]")
        self.assertEqual(hits, [("NEXT", "gpt")])

    def test_stacked_directives_both_orders(self):
        for text in ("bye. [[WRAP]] [[NEXT: Claude 2]]",
                     "bye. [[NEXT: Claude 2]] [[WRAP]]"):
            body, hits, _ = peel_directives(text)
            names = {n for n, _ in hits}
            self.assertEqual(names, {"WRAP", "NEXT"}, text)
            self.assertTrue(wrap_called(text), text)
            self.assertEqual(body, "bye.")
        # the NEXT arg survives with its spaces
        _, hits, _ = peel_directives("bye. [[WRAP]] [[NEXT: Claude 2]]")
        self.assertIn(("NEXT", "Claude 2"), hits)

    def test_multiline_arg(self):
        _, hits, _ = peel_directives("go.\n[[NEXT: Claude\n2]]")
        self.assertEqual(hits[0][0], "NEXT")

    def test_unknown_directive_surfaces(self):
        body, hits, unknown = peel_directives("done [[WRAP]] [[FOO]]")
        self.assertEqual(unknown, ["FOO"])
        self.assertIn(("WRAP", None), hits)

    def test_last_written_next_wins(self):
        _, hits, _ = peel_directives("hm [[NEXT: A]] [[NEXT: B]]")
        # peel order = last-written first; consumers take the first NEXT hit
        target = next(arg for name, arg in hits if name == "NEXT")
        self.assertEqual(target, "B")


def speaker_state(tmp, scripts, turns=3, labels=None):
    from test_loop import build_state
    state = build_state(tmp, scripts, turns=turns, labels=labels)
    state["mode"] = "speaker"
    return state


class SpeakerModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-speaker-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_opening_circuit_precedes_next_directive(self):
        state = speaker_state(
            self.tmp,
            [["A here. [[NEXT: C]]", "a2"], ["b1"], ["c here. [[NEXT: A]]"]],
            turns=1, labels=["A", "B", "C"])
        run_rounds(state, RecordingIO())
        # NEXT is retained, but it cannot silence B before everybody opens.
        # The one-round budget is exactly the three-seat opening circuit.
        self.assertEqual(agent_rows(state),
                         ["A here. [[NEXT: C]]", "b1",
                          "c here. [[NEXT: A]]"])

    def test_missing_directive_falls_back_in_order(self):
        state = speaker_state(
            self.tmp, [["a1"], ["b1"], ["c1"]], turns=1,
            labels=["A", "B", "C"])
        run_rounds(state, RecordingIO())
        self.assertEqual(agent_rows(state), ["a1", "b1", "c1"])

    def test_self_pick_falls_back_with_note(self):
        state = speaker_state(
            self.tmp, [["mine! [[NEXT: A]]"], ["b1"], ["c1"]], turns=1,
            labels=["A", "B", "C"])
        io = RecordingIO()
        run_rounds(state, io)
        self.assertEqual(agent_rows(state), ["mine! [[NEXT: A]]", "b1", "c1"])
        notes = [p["text"] for e, p in io.events if e == "status"]
        self.assertTrue(any("picked itself" in t for t in notes), notes)

    def test_bogus_pick_falls_back_with_note(self):
        state = speaker_state(
            self.tmp, [["over to [[NEXT: Grok]]"], ["b1"], ["c1"]], turns=1,
            labels=["A", "B", "C"])
        io = RecordingIO()
        run_rounds(state, io)
        self.assertEqual(agent_rows(state),
                         ["over to [[NEXT: Grok]]", "b1", "c1"])
        notes = [p["text"] for e, p in io.events if e == "status"]
        self.assertTrue(any("no such seat" in t for t in notes), notes)

    def test_budget_is_turns_times_seats(self):
        # A/B always hand back to one another: budget still caps the run, but
        # the opening circuit guarantees C one turn first.
        state = speaker_state(
            self.tmp,
            [["a. [[NEXT: B]]"] * 9, ["b. [[NEXT: A]]"] * 9, ["c-never"]],
            turns=2, labels=["A", "B", "C"])
        run_rounds(state, RecordingIO())
        rows = agent_rows(state)
        self.assertEqual(len(rows), 2 * 3)
        self.assertIn("c-never", rows)

    def test_wrap_beats_next(self):
        state = speaker_state(
            self.tmp,
            [["bye. [[WRAP]] [[NEXT: C]]"], ["b-close"], ["c-close"]],
            turns=3, labels=["A", "B", "C"])
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "wrapped")
        # closing order is fixed (after the wrapper), the NEXT is ignored
        self.assertEqual(agent_rows(state),
                         ["bye. [[WRAP]] [[NEXT: C]]", "b-close", "c-close"])

    def test_next_pick_survives_a_crash(self):
        state = speaker_state(
            self.tmp,
            [["go C. [[NEXT: C]]"], ["b-no"], [KeyboardInterrupt()]],
            turns=3, labels=["A", "B", "C"])
        relay.AGENT_TYPES["claude"] = RehydratableFake
        try:
            with self.assertRaises(KeyboardInterrupt):
                run_rounds(state, RecordingIO())
            meta = saved_meta(state)
            self.assertEqual(meta["next_speaker"], 2)
            st = relay.rehydrate(meta)
            attach_runtime(st, os.path.join(self.tmp, "session"))
            for a, s in zip(st["agents"], [["a2"], ["b2"], ["c-back"]]):
                a.script = list(s)
            run_rounds(st, RecordingIO())
            self.assertEqual(agent_rows(st)[:3],
                             ["go C. [[NEXT: C]]", "b-no", "c-back"])
        finally:
            relay.AGENT_TYPES["claude"] = relay.ClaudeAgent


class FakeModerator(Agent):
    """Scripted moderator; installed via AGENT_TYPES so build_moderator finds
    it. Class-level script because build_moderator constructs the instance."""
    name = "Moderator"
    cli = "fake"
    picks = []
    prompts = []

    def turn(self, message, on_activity=None):
        FakeModerator.prompts.append(message)
        item = FakeModerator.picks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class ModeratorModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-mod-")
        self._old = dict(relay.AGENT_TYPES)
        relay.AGENT_TYPES["claude"] = FakeModerator
        FakeModerator.picks = []
        FakeModerator.prompts = []

    def tearDown(self):
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mod_state(self, scripts, turns=5, labels=None):
        state = build_state(self.tmp, scripts, turns=turns, labels=labels)
        state["mode"] = "moderator"
        state["moderator"] = {"provider": "claude", "model": None,
                              "effort": None}
        return state

    def test_moderator_picks_and_done(self):
        # "hmm not sure" contains none of the labels a/b/c, so the lenient
        # pass finds nothing and the pick falls back to listed order
        FakeModerator.picks = ["C", "hmm not sure", "DONE"]
        state = self.mod_state(
            [["a1", "a2", "a-close"], ["b1", "b-close"],
             ["c1", "c2", "c-close"]],
            labels=["A", "B", "C"])
        io = RecordingIO()
        outcome = run_rounds(state, io)
        self.assertEqual(outcome, "wrapped")
        # A/B/C open deterministically before the moderator is called. Then
        # C is picked, an unusable answer falls back to A, and DONE closes all.
        self.assertEqual(agent_rows(state),
                         ["a1", "b1", "c1", "c2", "a2",
                          "a-close", "b-close", "c-close"])
        picks = [p["text"] for e, p in io.events if e == "status"]
        self.assertTrue(any("C speaks next" in t for t in picks), picks)

    def test_moderator_done_waits_until_every_seat_opens(self):
        FakeModerator.picks = ["DONE"]
        state = self.mod_state(
            [["a-open", "a-close"], ["b-open", "b-close"],
             ["c-open", "c-close"]], labels=["A", "B", "C"])
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "wrapped")
        self.assertEqual(agent_rows(state),
                         ["a-open", "b-open", "c-open",
                          "a-close", "b-close", "c-close"])
        self.assertEqual(len(FakeModerator.prompts), 1)

    def test_moderator_cannot_starve_a_quiet_seat(self):
        FakeModerator.picks = ["A", "A", "A", "DONE"]
        state = self.mod_state(
            [["a1", "a2", "a3", "a-close"],
             ["b1", "b2", "b-close"], ["c1", "c-close"]],
            turns=5, labels=["A", "B", "C"])
        io = RecordingIO()
        run_rounds(state, io)
        # The third consecutive A proposal would create a lead of three, so
        # the cursor's least-heard seat B is forced onto the floor.
        self.assertEqual(agent_rows(state)[:6],
                         ["a1", "b1", "c1", "a2", "a3", "b2"])
        notes = [p["text"] for e, p in io.events if e == "status"]
        self.assertTrue(any("Fairness override" in t for t in notes), notes)

    def test_no_moderator_rows_in_transcript(self):
        FakeModerator.picks = ["B", "DONE"]
        state = self.mod_state([["a1", "a-close"],
                                ["b1", "b2", "b-close"]],
                               labels=["A", "B"])
        run_rounds(state, RecordingIO())
        speakers = {r.get("name") for r in jsonl_rows(state)}
        self.assertNotIn("Moderator", speakers)

    def test_three_failures_disable_moderator(self):
        FakeModerator.picks = [RuntimeError("x"), RuntimeError("y"),
                               RuntimeError("z")]
        state = self.mod_state(
            [["a1", "a2"], ["b1", "b2"], ["c1", "c2"]],
            turns=2, labels=["A", "B", "C"])
        io = RecordingIO()
        run_rounds(state, io)
        # all picks failed -> pure round-robin; run completes on budget
        self.assertEqual(agent_rows(state),
                         ["a1", "b1", "c1", "a2", "b2", "c2"])
        self.assertTrue(state.get("_mod_disabled"))
        sys_rows = [r["text"] for r in jsonl_rows(state)
                    if r["speaker"] == "system"]
        self.assertTrue(any("Moderator is failing" in t for t in sys_rows))
        # exactly 3 calls, then never again
        self.assertEqual(len(FakeModerator.prompts), 3)



class RoomHelperNameTests(unittest.TestCase):
    """Josh can name the moderator/supervisor, and the name is what shows."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-helper-name-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_typed_name_reaches_the_agent(self):
        mod = relay.build_moderator(
            {"workspace": self.tmp,
             "moderator": {"provider": "claude", "name": "Referee"}})
        self.assertEqual(mod.name, "Referee")
        sup = relay.build_supervisor(
            {"workspace": self.tmp,
             "supervisor": {"provider": "claude", "name": "Foreman"}})
        self.assertEqual(sup.name, "Foreman")

    def test_unnamed_keeps_the_role_word(self):
        self.assertEqual(
            relay.build_moderator({"workspace": self.tmp,
                                   "moderator": {"provider": "claude"}}).name,
            "Moderator")
        self.assertEqual(
            relay.build_supervisor({"workspace": self.tmp,
                                    "supervisor": {}}).name,
            "Supervisor")

    def test_blank_and_whitespace_are_not_names(self):
        for value in ("", "   ", None):
            self.assertEqual(
                relay.room_helper_name({"moderator": {"name": value}},
                                       "moderator"), "Moderator")

    def test_a_name_is_bounded_like_a_seat_label(self):
        self.assertEqual(
            len(relay.room_helper_name({"moderator": {"name": "x" * 80}},
                                       "moderator")), 24)

    def test_the_visible_sentences_use_the_name_not_the_role(self):
        # The Supervisor is the most visible non-seat in the app: a control
        # log, status lines and a transcript row all name it. A room that
        # renamed it and still read "Supervisor produced no tasks" would be
        # told about someone who is not in it.
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "relay.py"), encoding="utf-8") as f:
            src = f.read()
        for gone in ('"Supervisor produced no tasks',
                     '"Supervisor could not repair failed ',
                     '"Supervisor returned no valid replacement '):
            self.assertNotIn(gone, src, gone)
        # the name comes from STATE, not off the agent object: build_* took
        # it from there, and the agent is sometimes a test double
        self.assertIn('room_helper_name(state, "supervisor")', src)
        self.assertIn('room_helper_name(state, "moderator")', src)



if __name__ == "__main__":
    unittest.main(verbosity=2)
