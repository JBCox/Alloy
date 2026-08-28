"""Token-free tests for the [[ASK]] directive (questions to Josh).

Run:  python tests/test_ask.py
"""

import ast
import json
import os
import re
import subprocess
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


class UnattendedAskTests(unittest.TestCase):
    """A run nobody is watching must not be held open by a question.

    Wave 4 shipped scheduled rooms and stated this as a note in #schedModal:
    "the run waits for an answer -- only Keep Improving runs give an
    unanswered question a deadline". True, and the wedge. `relay.ask_abort`
    returned the caller's abort UNCHANGED outside continuous mode, so a 01:00
    room (or a webhook start from a script) whose seat ended a reply with
    [[ASK]] blocked its barrier until Josh pressed Stop in the morning -- and
    every later brake, the accumulated clock and the spend cap and the
    watchdog, is checked AT that barrier.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- the deadline -----------------------------------------------------
    def test_an_attended_chat_still_waits_as_long_as_josh_needs(self):
        """The point of [[ASK]]. An ordinary chat gets the caller's abort
        back BY IDENTITY, so there is no deadline object to expire."""
        mine = lambda: False
        for state in ({}, {"continuous": None}, {"_unattended": False},
                      {"continuous": {"on": False}, "_unattended": 0}):
            self.assertIs(relay.ask_abort(state, mine), mine, state)
            self.assertIsNone(relay.ask_abort(state), state)

    def test_an_unattended_chat_gets_a_deadline_without_continuous_mode(self):
        mine = lambda: False
        state = {"continuous": None, "_unattended": True}
        composed = relay.ask_abort(state, mine)
        self.assertIsNot(composed, mine, "no deadline was composed")
        self.assertFalse(composed(), "it expired immediately")
        # the caller's own abort still wins at once
        self.assertTrue(relay.ask_abort(state, lambda: True)())

    def _deadline(self, state):
        """How many seconds this state's composed abort actually waits.

        A fake clock rather than ASK_WAIT_MAX wrangling: the first version of
        this asserted "expires when the constant is 0, does not when it is
        huge", which BOTH arms satisfy, so it could not see the arms being
        swapped -- a test that cannot see its own subject. Measuring the wait
        can. `relay.time` is swapped (not the stdlib module) and restored, and
        nothing else in this suite runs a thread.
        """
        real = relay.time

        class Clock:
            now = 1000.0

            def __getattr__(self, key):
                return getattr(real, key)

            def monotonic(self):
                return self.now

        clock = Clock()
        relay.time = clock
        try:
            abort = relay.ask_abort(state)
            self.assertTrue(callable(abort), "no deadline was composed")
            low, high = 0, 24 * 60 * 60
            while high - low > 1:      # bisect the instant it flips
                mid = (low + high) // 2
                clock.now = 1000.0 + mid
                if abort():
                    high = mid
                else:
                    low = mid
            return high
        finally:
            relay.time = real

    def test_the_unattended_deadline_is_the_engines_own_constant(self):
        self.assertEqual(self._deadline({"_unattended": True}),
                         relay.ASK_WAIT_MAX)

    def test_a_scheduled_keep_improving_room_keeps_the_tighter_cap(self):
        """Both unanswerable at once, and the continuous arm must win: a wait
        that outlasted the check-in interval would silence the very watchdog
        watching the run."""
        self.assertEqual(
            self._deadline({"continuous": {"on": True,
                                           "checkin": {"minutes": 5}},
                            "_unattended": True}),
            5 * 60, "min(ASK_WAIT_MAX, checkin) is not being taken")
        # ...and a check-in LONGER than the constant does not extend it
        self.assertEqual(
            self._deadline({"continuous": {"on": True,
                                           "checkin": {"minutes": 600}},
                            "_unattended": True}),
            relay.ASK_WAIT_MAX)

    # ---- the key itself ---------------------------------------------------
    def test_unattended_reads_one_private_bool_and_nothing_else(self):
        self.assertFalse(relay.unattended({}))
        self.assertFalse(relay.unattended({"_unattended": None}))
        self.assertTrue(relay.unattended({"_unattended": True}))
        self.assertTrue(relay.unattended({"_unattended": 1}))

    # One walker, shared by the two structural tests below. Returns
    # {"read": {func: [src, ...]}, "write": {func: [src, ...]}} for every
    # STATEMENT mentioning the key, keyed by the function it sits in.
    #
    # AST rather than a regex, and that is the point of the rewrite: the
    # regex this replaces had already been walked past by five write
    # spellings in one adversarial pass, and even patched it could only ever
    # say THAT relay wrote the key -- never WHERE, which is the whole rule
    # now that exactly one write is correct. It also gets the
    # statement-versus-mention distinction for free: a docstring naming a
    # private key in prose (as `unattended`'s does for `_usage_io` and
    # `_cont_mark`) is ONE Constant whose value is the whole docstring,
    # never the bare key. Comments are not in the tree at all.
    @staticmethod
    def _key_sites(path, key="_unattended"):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        sites = {"read": {}, "write": {}}

        def note(kind, func, node):
            sites[kind].setdefault(func, []).append(
                (ast.get_source_segment(src, node) or "").strip())

        def walk(node, func, parent):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = node.name
            # A KEYWORD argument carries the name as a plain str field,
            # not a Constant -- so `state.update(_unattended=True)` and
            # `dict(state, _unattended=True)` produce no Constant, reach
            # neither branch below, and slip past the fail-loud else as
            # though they were nothing. The REGEX this walker replaced
            # caught both, and both are live vocabulary in this repo.
            if isinstance(node, ast.keyword) and node.arg == key:
                note("write", func, node)
            if isinstance(node, ast.Constant) and node.value == key:
                if isinstance(parent, ast.Subscript):
                    note("read" if isinstance(parent.ctx, ast.Load)
                         else "write", func, parent)
                elif isinstance(parent, ast.Dict):
                    note("write", func, parent)
                elif isinstance(parent, ast.Compare):
                    note("read", func, parent)
                elif (isinstance(parent, ast.Call)
                      and isinstance(parent.func, ast.Attribute)):
                    note("read" if parent.func.attr == "get" else "write",
                         func, parent)
                else:
                    raise AssertionError(
                        "unclassified mention of %r in %s: %r -- a spelling "
                        "this walker does not understand must FAIL, never "
                        "count as neither" % (key, func, ast.dump(parent)))
            for child in ast.iter_child_nodes(node):
                walk(child, func, node)

        walk(tree, "<module>", None)
        return sites

    def test_only_a_front_end_declares_it(self):
        """The ENGINE never writes this key. A FRONT END does, because a
        front end is the only thing that can know how its own invocation was
        started, and there are two of them: `app.Api` answers from
        `Run.background`, `relay.main()` answers from --unattended.

        That is not the split authority the first version of this rule was
        written to prevent. Authority would be split if `run_rounds`, a loop,
        or `ask_abort` decided it -- two modules that cannot see each other
        disagreeing about whether anybody is watching. Two front ends each
        declaring for the run IT started is the `LoopIO` seam, one state key
        over."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sites = self._key_sites(os.path.join(root, "relay.py"))
        self.assertEqual(sorted(sites["write"]), ["main"], sites["write"])
        self.assertEqual(sorted(sites["read"]), ["unattended"], sites["read"])
        self.assertEqual(len(sites["read"]["unattended"]), 1,
                         sites["read"]["unattended"])
        self.assertEqual(len(sites["write"]["main"]), 1, sites["write"]["main"])

    def test_the_terminal_declares_it_from_its_own_flag(self):
        """--unattended is the whole point: without a flag, a `relay.py` run
        from Task Scheduler is indistinguishable from Josh sitting at a
        console, and it wedged on an unanswered [[ASK]] exactly as a
        scheduled room used to. Both halves are asserted, because either one
        alone passes while the feature is broken: argparse really accepts the
        flag (a subprocess, so it is the SHIPPING parser and not a copy), and
        main() really derives the state key from it."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        helped = subprocess.run(
            [sys.executable, os.path.join(root, "relay.py"), "--help"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        # NOT `assertIn`: a RED pass renamed the flag to
        # "--unattendedX" and the substring match sailed straight
        # past it while `args.unattended` was already an
        # AttributeError waiting to happen. The wrap-token family --
        # a match that cannot tell an option from a prefix of one.
        self.assertRegex(helped.stdout, r"--unattended(?![\w-])")

        with open(os.path.join(root, "relay.py"), encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        wired = [ast.get_source_segment(src, value)
                 for node in ast.walk(main) if isinstance(node, ast.Dict)
                 for k, value in zip(node.keys, node.values)
                 if isinstance(k, ast.Constant) and k.value == "_unattended"]
        self.assertEqual(len(wired), 1, wired)
        # EXACT, not `assertIn`. A substring match sees the name and never
        # the meaning: an adversarial pass flipped this to
        # `not args.unattended` (every console run expiring its asks, and
        # the flag itself wedging) and to `bool(args.unattended) and False`
        # (the flag inert, the wedge this whole change fixes restored), and
        # BOTH stayed green across all 2597 tests.
        self.assertEqual(wired[0], "bool(args.unattended)")
        # ...and the polarity of the flag ITSELF, which nothing else
        # reaches: store_false would invert the default just as quietly.
        flag = [c for c in ast.walk(main)
                if isinstance(c, ast.Call)
                and getattr(c.func, "attr", "") == "add_argument"
                and any(isinstance(a, ast.Constant) and
                        a.value == "--unattended" for a in c.args)]
        self.assertEqual(len(flag), 1, flag)
        self.assertEqual([k.value.value for k in flag[0].keywords
                          if k.arg == "action"], ["store_true"])

    def test_the_key_is_never_persisted(self):
        """Private like `_usage_io` and `_cont_mark`: SessionStore.save
        whitelists what it writes, so a resumed chat is attended by
        construction -- the thing resuming it is Josh."""
        state = build_state(self.tmp, [["a1"], ["b1"]])
        state["_unattended"] = True
        state["store"].save(state)
        meta = saved_meta(state)
        self.assertNotIn("_unattended", meta)
        self.assertNotIn("_unattended", json.dumps(meta))
        self.assertFalse(relay.unattended(rehydrate(meta)))

    def test_a_terminal_run_is_attended_unless_it_says_otherwise(self):
        """Attended is the DEFAULT, and that is right: a person at a console
        is exactly who [[ASK]] is for, so a bare `relay.py` waits as long as
        it always has.

        This used to be `test_the_cli_never_sets_it` and it asserted the CLI
        could not set the key at all. It drove `build_state`, never `main()`,
        so when main() gained --unattended the test went on passing while its
        own docstring became untrue -- a test that cannot see its own
        subject. `test_the_terminal_declares_it_from_its_own_flag` above is
        the half it was missing."""
        state = build_state(self.tmp, [["a1"], ["b1"]])
        self.assertNotIn("_unattended", state)
        self.assertFalse(relay.unattended(state))
        mine = lambda: False
        self.assertIs(relay.ask_abort(state, mine), mine)
        # ...and the flag is what flips it, through the one public reader
        self.assertTrue(relay.unattended(dict(state, _unattended=True)))
        self.assertIsNot(relay.ask_abort(dict(state, _unattended=True), mine),
                         mine)

    # ---- what the seat is told -------------------------------------------
    def _unanswered_note(self, **extra):
        state = build_state(self.tmp, [["a1"], ["b1"]])
        state["ask"] = True
        state.update(extra)
        io = AskIO()               # scripted: answers None at once
        relay.handle_ask_directive(
            state, 0, "Done." + chr(10) + "[[ASK: which way? | left | right]]",
            io)
        self.assertIsNone(state["ask_pending"])
        self.assertEqual([r for r in jsonl_rows(state)
                          if r["speaker"] == "josh"], [],
                         "an answer was forged")
        return state["pending"][0][-1]

    def test_three_arms_not_two(self):
        """The continuous arm names Keep Improving OUT LOUD, so a scheduled
        round-capped run falling into it would be told it is something it is
        not; the attended arm ("Josh was unavailable") is equally wrong for a
        run he never opened."""
        attended = self._unanswered_note()
        self.assertIn("Josh was unavailable", attended)

        background = self._unanswered_note(_unattended=True)
        self.assertIn("started in the background", background)
        self.assertNotIn("Keep Improving", background)
        self.assertNotIn("Josh was unavailable", background)

        cont = self._unanswered_note(
            continuous={"on": True, "checkin": {"minutes": 30}},
            _unattended=True)
        self.assertIn("Keep Improving", cont)

    def test_every_arm_tells_the_seat_to_decide_and_none_forges_an_answer(self):
        for extra in ({}, {"_unattended": True},
                      {"continuous": {"on": True}}):
            note = self._unanswered_note(**extra)
            self.assertTrue(note.startswith("(Relay:"), note)
            self.assertNotIn("Josh (human) answers", note)

    def test_the_unanswered_question_leaves_a_row_josh_can_read_later(self):
        """`announce_lost_ask` has always persisted its twin notice — a
        question lost to a crash. This is the question NOBODY ANSWERED, and
        for an unattended run it is the only durable record there is: the
        note the SEAT is owed lives in `pending`, which is meta, which Josh
        never reads. Without the row, a scheduled run opened in the morning
        shows a reply ending in [[ASK]] and nothing about what became of it.
        Found by the live run, not by reading.
        """
        state = build_state(self.tmp, [["a1"], ["b1"]])
        state["ask"] = True
        state["_unattended"] = True
        relay.handle_ask_directive(
            state, 0, "Done." + chr(10) + "[[ASK: which? | a | b]]", AskIO())
        rows = jsonl_rows(state)
        said = [r for r in rows if r["speaker"] == "system"
                and "went unanswered" in r["text"]]
        self.assertEqual(len(said), 1, rows)
        self.assertEqual(said[0]["origin"], "relay")
        self.assertIn("Fake 1", said[0]["text"])
        # ...and it is still not an answer
        self.assertEqual([r for r in rows if r["speaker"] == "josh"], [])

    def test_a_real_unattended_run_moves_on_instead_of_wedging(self):
        """Through the REAL loop: the wait expires, the requester is told,
        and the conversation keeps going."""
        state = build_state(
            self.tmp,
            [["Which one? [[ASK: pick | red | blue]]", "a2"], ["b1", "b2"]],
            turns=2)
        state["ask"] = True
        state["_unattended"] = True
        io = AskIO()
        run_rounds(state, io)
        self.assertEqual(len(io.asked), 1)
        self.assertTrue(any("started in the background" in pr
                            for pr in state["agents"][0].prompts),
                        state["agents"][0].prompts)
        self.assertEqual([r for r in jsonl_rows(state)
                          if r["speaker"] == "josh"], [])
        # ...and the run really did carry on rather than stopping there
        self.assertGreaterEqual(state["turn"], 3)


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
