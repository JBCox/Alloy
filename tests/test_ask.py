"""Token-free tests for the [[ASK]] directive (questions to Josh).

Run:  python tests/test_ask.py
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import (announce_lost_ask, parse_ask, peel_directives, preamble,
                   rehydrate, run_rounds)
from test_loop import (FakeAgent, RecordingIO, build_state, jsonl_rows,
                       saved_meta)


class AskIO(RecordingIO):
    """RecordingIO with a scripted ask_human: each entry in `answers` is the
    reply to one question (None = Josh unavailable)."""

    def __init__(self, answers=None, human_script=None):
        super().__init__(human_script=human_script)
        self.asked = []            # captured payloads
        self._answers = list(answers or [])

    def ask_human(self, payload, abort=None):
        self.asked.append(payload)
        return self._answers.pop(0) if self._answers else None


class ParseAskTests(unittest.TestCase):
    def test_question_and_options(self):
        q, opts = parse_ask("pick one | A | B")
        self.assertEqual(q, "pick one")
        self.assertEqual(opts, ["A", "B"])

    def test_question_only(self):
        q, opts = parse_ask("what should we call it?")
        self.assertEqual(q, "what should we call it?")
        self.assertEqual(opts, [])

    def test_empty_option_segments_dropped(self):
        q, opts = parse_ask("q | A |  | B |")
        self.assertEqual(opts, ["A", "B"])

    def test_empty_question_raises(self):
        with self.assertRaises(ValueError):
            parse_ask("| A | B")
        with self.assertRaises(ValueError):
            parse_ask("")

    def test_too_many_options_raises(self):
        with self.assertRaises(ValueError):
            parse_ask("q | 1 | 2 | 3 | 4 | 5 | 6 | 7")

    def test_peel_recognizes_ask(self):
        body, hits, unknown = peel_directives("Hmm. [[ASK: q | A]]")
        self.assertEqual(hits, [("ASK", "q | A")])
        self.assertEqual(body, "Hmm.")
        self.assertEqual(unknown, [])

    def test_ask_stacked_with_wrap_peels_as_two(self):
        _, hits, _ = peel_directives("Done. [[ASK: q | A]] [[WRAP]]")
        self.assertEqual(sorted(n for n, _ in hits), ["ASK", "WRAP"])

    def test_mid_reply_mention_does_not_fire(self):
        _, hits, _ = peel_directives("You could use [[ASK: q]] for that. Bye.")
        self.assertEqual(hits, [])


class AskLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_happy_path(self):
        state = build_state(self.tmp,
                            [["Which color? [[ASK: pick one | red | blue]]",
                              "a2"], ["b1", "b2"]], turns=2)
        state["ask"] = True
        io = AskIO(answers=["red"])
        run_rounds(state, io)
        # payload shape
        self.assertEqual(len(io.asked), 1)
        p = io.asked[0]
        self.assertEqual(p["question"], "pick one")
        self.assertEqual(p["options"], ["red", "blue"])
        self.assertEqual(p["asker"], "Fake 1")
        self.assertEqual(p["speaker"], 0)
        self.assertTrue(p["qid"])
        # the answer is a real Josh row with a truthful caption
        rows = jsonl_rows(state)
        josh = [r for r in rows if r["speaker"] == "josh"]
        self.assertEqual(len(josh), 1)
        self.assertEqual(josh[0]["text"], "red")
        self.assertEqual(josh[0]["meta"], "answer to Fake 1")
        # the asker's reply row keeps the directive verbatim
        asker_rows = [r for r in rows if r["speaker"] == 0]
        self.assertIn("[[ASK: pick one | red | blue]]", asker_rows[0]["text"])
        # fan-out reached every seat; seat 2's next prompt saw the answer
        b = state["agents"][1]
        self.assertTrue(any("Josh (human) answers: red" in pr
                            for pr in b.prompts))
        # requester saw the answer too (front of its next prompt's backlog)
        a = state["agents"][0]
        self.assertTrue(any("Josh (human) answers: red" in pr
                            for pr in a.prompts))
        # marker cleared
        self.assertIsNone(state.get("ask_pending"))

    def test_options_free_ask(self):
        state = build_state(self.tmp, [["[[ASK: name the project]]", "a2"],
                                       ["b1", "b2"]], turns=2)
        state["ask"] = True
        io = AskIO(answers=["ai-chat"])
        run_rounds(state, io)
        self.assertEqual(io.asked[0]["options"], [])
        self.assertEqual(io.asked[0]["question"], "name the project")

    def test_default_io_means_unavailable_note(self):
        # base LoopIO.ask_human returns None immediately: no hang, and the
        # requester gets the relay note instead of a forged answer
        state = build_state(self.tmp, [["[[ASK: q | A]]", "a2"],
                                       ["b1", "b2"]], turns=2)
        state["ask"] = True
        io = RecordingIO()
        run_rounds(state, io)
        a = state["agents"][0]
        self.assertTrue(any("Josh was unavailable" in pr for pr in a.prompts))
        rows = jsonl_rows(state)
        self.assertFalse([r for r in rows if r["speaker"] == "josh"])
        self.assertIsNone(state.get("ask_pending"))

    def test_gate_off_rejects_without_asking(self):
        state = build_state(self.tmp, [["[[ASK: q | A]]", "a2"],
                                       ["b1", "b2"]], turns=2)
        io = AskIO(answers=["never delivered"])
        run_rounds(state, io)
        self.assertEqual(io.asked, [])
        a = state["agents"][0]
        self.assertTrue(any("asking Josh is not available" in pr
                            for pr in a.prompts))

    def test_two_asks_rejected(self):
        state = build_state(self.tmp,
                            [["[[ASK: q1]] [[ASK: q2]]", "a2"],
                             ["b1", "b2"]], turns=2)
        state["ask"] = True
        io = AskIO(answers=["x"])
        run_rounds(state, io)
        self.assertEqual(io.asked, [])
        a = state["agents"][0]
        self.assertTrue(any("only one ASK per reply" in pr
                            for pr in a.prompts))

    def test_malformed_ask_rejected(self):
        state = build_state(self.tmp, [["[[ASK: | A]]", "a2"],
                                       ["b1", "b2"]], turns=2)
        state["ask"] = True
        io = AskIO(answers=["x"])
        run_rounds(state, io)
        self.assertEqual(io.asked, [])
        a = state["agents"][0]
        self.assertTrue(any("your ASK was not shown to Josh" in pr
                            for pr in a.prompts))

    def test_ask_stacked_with_wrap(self):
        # the wrap fires AND the question is asked; the answer fans out to
        # the closing seats
        state = build_state(self.tmp,
                            [["Done here. [[ASK: happy? | yes | no]] [[WRAP]]"],
                             ["b1", "b2"]], turns=3)
        state["ask"] = True
        io = AskIO(answers=["yes"])
        outcome = run_rounds(state, io)
        self.assertEqual(outcome, "wrapped")
        self.assertEqual(len(io.asked), 1)
        b = state["agents"][1]
        self.assertTrue(any("Josh (human) answers: yes" in pr
                            for pr in b.prompts))

    def test_whitespace_answer_is_no_answer(self):
        state = build_state(self.tmp, [["[[ASK: q | A]]", "a2"],
                                       ["b1", "b2"]], turns=2)
        state["ask"] = True
        io = AskIO(answers=["   "])
        run_rounds(state, io)
        a = state["agents"][0]
        self.assertTrue(any("Josh was unavailable" in pr for pr in a.prompts))
        self.assertFalse([r for r in jsonl_rows(state)
                          if r["speaker"] == "josh"])

    def test_answer_with_newlines_and_pipes_survives(self):
        state = build_state(self.tmp, [["[[ASK: q | A]]", "a2"],
                                       ["b1", "b2"]], turns=2)
        state["ask"] = True
        answer = "line one | with pipe\nline two"
        io = AskIO(answers=[answer])
        run_rounds(state, io)
        rows = [r for r in jsonl_rows(state) if r["speaker"] == "josh"]
        self.assertEqual(rows[0]["text"], answer)

    def test_ask_pending_persisted_during_wait(self):
        # the marker must hit meta.json BEFORE ask_human blocks
        state = build_state(self.tmp, [["[[ASK: q | A]]", "a2"],
                                       ["b1", "b2"]], turns=2)
        state["ask"] = True
        seen = {}

        class SnoopIO(AskIO):
            def ask_human(self, payload, abort=None):
                seen["pending"] = saved_meta(state).get("ask_pending")
                return super().ask_human(payload, abort=abort)

        run_rounds(state, SnoopIO(answers=["A"]))
        self.assertEqual(seen["pending"], {"seat": 0, "question": "q"})
        self.assertIsNone(saved_meta(state).get("ask_pending"))

    def test_lost_ask_announced_once(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        state["ask"] = True
        state["ask_pending"] = {"seat": 1, "question": "old q"}
        io = RecordingIO()
        run_rounds(state, io)
        self.assertIsNone(state.get("ask_pending"))
        rows = jsonl_rows(state)
        notes = [r for r in rows if r["speaker"] == "system"
                 and "went unanswered" in r["text"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("Fake 2", notes[0]["text"])
        b = state["agents"][1]
        self.assertTrue(any("your question to Josh went unanswered" in pr
                            for pr in b.prompts))

    def test_lost_ask_for_vanished_seat(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        state["ask"] = True
        state["ask_pending"] = {"seat": 99, "question": "old q"}
        io = RecordingIO()
        run_rounds(state, io)          # must not crash
        self.assertIsNone(state.get("ask_pending"))

    def test_meta_round_trip(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        state["ask"] = True
        state["ask_pending"] = {"seat": 0, "question": "q"}
        state["store"].save(state)
        meta = saved_meta(state)
        self.assertTrue(meta["ask"])
        self.assertEqual(meta["ask_pending"], {"seat": 0, "question": "q"})
        re_state = rehydrate(meta)
        self.assertTrue(re_state["ask"])
        self.assertEqual(re_state["ask_pending"], {"seat": 0, "question": "q"})

    def test_old_meta_defaults(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        state["store"].save(state)
        meta = saved_meta(state)
        meta.pop("ask", None)
        meta.pop("ask_pending", None)
        re_state = rehydrate(meta)
        self.assertFalse(re_state["ask"])
        self.assertIsNone(re_state["ask_pending"])


class AskPreambleTests(unittest.TestCase):
    def _agents(self):
        tmp = tempfile.mkdtemp(prefix="ai-chat-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        a = FakeAgent(tmp, [], name="Fake 1")
        b = FakeAgent(tmp, [], name="Fake 2")
        return a, b, tmp

    def test_ask_block_present_when_on(self):
        a, b, tmp = self._agents()
        text = preamble(a, [b], "t", 3, tmp, ask=True)
        self.assertIn("Asking Josh:", text)
        self.assertIn("[[ASK:", text)
        self.assertIn("see 'Asking Josh' below", text)
        self.assertNotIn("he is otherwise not involved", text)

    def test_ask_block_absent_when_off(self):
        a, b, tmp = self._agents()
        text = preamble(a, [b], "t", 3, tmp)
        self.assertNotIn("Asking Josh:", text)
        self.assertNotIn("[[ASK:", text)
        # the exact old header sentence survives byte-for-byte
        self.assertIn("A human (Josh) set this up and may occasionally "
                      "interject; he is otherwise not involved -- talk to "
                      "the other AI(s), not to him.", text)

    def test_highlight_rule_always_present(self):
        a, b, tmp = self._agents()
        for kw in ({}, {"ask": True}):
            self.assertIn("==double equals==", preamble(a, [b], "t", 3, tmp,
                                                        **kw))


if __name__ == "__main__":
    unittest.main(verbosity=1)
