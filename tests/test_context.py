"""W1.4 - an honest per-seat context readout.

The plan said: publish the last top-level assistant event's INPUT-TOKEN
COUNT, filter on absence of `parent_tool_use_id` (listed as unconfirmed),
and show no ring without a measured denominator. Captured stdout on
2026-08-27 settled all three, and moved two of them:

* **input_tokens is not the context.** In a real turn whose context was
  41,616 tokens, the last top-level assistant event reported
  `input_tokens: 8`. Everything else arrived as `cache_creation_` and
  `cache_read_input_tokens` - cached, but still in the prompt. Publishing
  input_tokens alone would have said a seat had used 8 tokens of a 200,000
  window, which is the same class of bug as summing GPT's cumulative
  counters: a number that looks measured and measures the wrong thing.
* **`parent_tool_use_id` is confirmed, and the filter is load-bearing.** A
  native subagent's assistant events ride the same stream carrying a
  non-None id. In the measured run the subagent sat at 21,184 tokens while
  the seat itself was at 39,613 - so an unfiltered read understates by 45%
  at exactly the moments a seat is delegating hardest.
* **There IS a measured denominator**, which the plan assumed there was not:
  the result object's `modelUsage[<model>].contextWindow` (200,000). So a
  proportion may honestly be drawn for a claude seat. Where no CLI reports
  one - OpenCode with a model missing from models.dev - the readout is words
  and no bar, which is the rule the plan actually cared about.
* **OpenCode can answer too**, and has the identical trap: its
  `step_finish.tokens.input` counts only what was NOT served from cache. The
  measured series ran 13,717 / 13,943 / 5,445(+8,640 cached) / … - reading
  `input` alone would have shown the context SHRINKING by 60% at the moment
  caching started working.
* **codex cannot**, honestly: `exec --json` emits one usage event for a whole
  run, summed over its internal iterations, so nothing there is a context.
  **gemini cannot**: agy streams nothing at all.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import relay  # noqa: E402
import test_ui_boot  # noqa: E402
from test_loop import RecordingIO, build_state, jsonl_rows  # noqa: E402

NODE = test_ui_boot.NODE


def assistant(usage, parent=None, model="claude-haiku-4-5-20251001"):
    """One real `assistant` line, in the shape a live stream produced."""
    return json.dumps({
        "type": "assistant", "parent_tool_use_id": parent,
        "message": {"model": model, "content": [{"type": "text", "text": "x"}],
                    "usage": usage},
    })


def result(model_usage=None, **extra):
    row = {"type": "result", "subtype": "success", "is_error": False,
           "result": "done", "session_id": "s1",
           "usage": {"input_tokens": 52, "output_tokens": 1565},
           "total_cost_usd": 0.069}
    if model_usage is not None:
        row["modelUsage"] = model_usage
    row.update(extra)
    return json.dumps(row)


# the literal usage dicts the measured run carried
FIRST = {"input_tokens": 10, "cache_creation_input_tokens": 15703,
         "cache_read_input_tokens": 22125, "output_tokens": 4}
LAST = {"input_tokens": 8, "cache_creation_input_tokens": 232,
        "cache_read_input_tokens": 41376, "output_tokens": 1}
SUBAGENT = {"input_tokens": 4, "cache_creation_input_tokens": 180,
            "cache_read_input_tokens": 21000, "output_tokens": 9}
WINDOW = {"claude-haiku-4-5": {"contextWindow": 200000,
                               "canonicalModel": "claude-haiku-4-5",
                               "inputTokens": 52, "outputTokens": 1565}}


def claude(**kw):
    return relay.ClaudeAgent(name="Claude", workspace=ROOT, **kw)


# ------------------------------------------------------------- the numbers --

class ContextUsedTests(unittest.TestCase):
    def test_the_cache_fields_are_the_context(self):
        # 8 + 232 + 41,376 - the number the plan's spec would have missed
        self.assertEqual(relay._context_used(LAST), 41616)

    def test_input_tokens_alone_is_not_it(self):
        self.assertNotEqual(relay._context_used(LAST), LAST["input_tokens"])

    def test_a_partial_usage_dict_still_answers(self):
        self.assertEqual(relay._context_used({"input_tokens": 700}), 700)
        self.assertEqual(
            relay._context_used({"cache_read_input_tokens": 900}), 900)

    def test_nothing_measured_is_None_and_never_zero(self):
        # absence is a blank; 0 would render as a real, wrong reading
        for junk in ({}, None, [], {"output_tokens": 40}, "no"):
            self.assertIsNone(relay._context_used(junk))

    def test_a_junk_value_is_ignored_rather_than_added(self):
        self.assertEqual(relay._context_used({"input_tokens": "lots",
                                              "cache_read_input_tokens": 5}), 5)


class ContextReportTests(unittest.TestCase):
    def test_a_measured_window_travels_with_the_number(self):
        self.assertEqual(relay.context_report(41616, 200000),
                         {"context_used": 41616, "context_window": 200000})

    def test_no_measured_denominator_means_no_denominator(self):
        # the key is ABSENT, not zero or null: a front end draws a
        # proportion only when it was given one
        for window in (None, 0, -5, "200000", 1.5):
            self.assertEqual(relay.context_report(1234, window),
                             {"context_used": 1234})

    def test_nothing_used_is_nothing_reported(self):
        for used in (0, None, -1, "4000"):
            self.assertIsNone(relay.context_report(used, 200000))


# ------------------------------------------------------------------ claude --

class ClaudeContextTests(unittest.TestCase):
    def report(self, lines):
        a = claude()
        a.parse("\n".join(lines))
        return a.last_context

    def test_the_last_top_level_event_wins(self):
        # the context only grows within a turn (22,125 -> 41,376 measured)
        self.assertEqual(
            self.report([assistant(FIRST), assistant(LAST), result(WINDOW)]),
            {"context_used": 41616, "context_window": 200000})

    def test_a_subagents_context_is_never_reported_as_the_seats(self):
        # THE measured rule: an unfiltered read takes the subagent's 21,184
        # over the seat's 41,616 - a 49% understatement, at exactly the
        # moment a seat is delegating hardest
        got = self.report([assistant(LAST),
                           assistant(SUBAGENT, parent="toolu_013JQ"),
                           result(WINDOW)])
        self.assertEqual(got["context_used"], 41616)

    def test_the_window_comes_from_the_clis_own_report(self):
        self.assertEqual(
            self.report([assistant(LAST), result(WINDOW)])["context_window"],
            200000)

    def test_no_model_usage_means_words_not_a_guessed_window(self):
        got = self.report([assistant(LAST), result(None)])
        self.assertEqual(got, {"context_used": 41616})

    def test_a_lone_entry_is_used_even_when_the_name_does_not_match(self):
        got = self.report([assistant(LAST),
                           result({"some-other-id": {"contextWindow": 123456}})])
        self.assertEqual(got["context_window"], 123456)

    def test_two_models_and_no_match_means_no_window(self):
        # a guess BETWEEN two real windows is still a guess
        got = self.report([assistant(LAST, model="mystery"),
                           result({"a": {"contextWindow": 200000},
                                   "b": {"contextWindow": 1000000}})])
        self.assertEqual(got, {"context_used": 41616})

    def test_the_matching_model_wins_when_several_ran(self):
        got = self.report([
            assistant(LAST, model="claude-haiku-4-5"),
            result({"claude-opus-4-8": {"contextWindow": 500000},
                    "claude-haiku-4-5": {"contextWindow": 200000,
                                         "canonicalModel": "claude-haiku-4-5"}})])
        self.assertEqual(got["context_window"], 200000)

    def test_a_turn_with_no_assistant_events_reports_nothing(self):
        self.assertIsNone(self.report([result(WINDOW)]))

    def test_a_dead_turn_reports_nothing_rather_than_the_last_number(self):
        # no result object at all: parse returns early, so nothing there
        # overwrites the previous number - only turn()'s reset does
        a = claude()
        a.parse("\n".join([assistant(LAST), result(WINDOW)]))
        self.assertTrue(a.last_context)
        a.parse("not json at all")
        self.assertTrue(a.last_context)          # parse alone cannot clear it

    def test_junk_lines_never_raise(self):
        self.assertIsNone(self.report(["", "not json", '{"type":"assistant"}',
                                       '{"type": "assistant", "message": 4}']))


class TurnResetTests(unittest.TestCase):
    """The reset itself, through the REAL `Agent.turn` — a `python -c` child
    standing in for the CLI, no tokens and no claude on PATH.

    Asserting it by hand-clearing the attribute would have tested `parse`
    twice and the reset not at all: the RED pass caught exactly that, and
    deleting the reset line left the suite green."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agent(self, lines):
        payload = "\n".join(lines)

        class Streamed(relay.ClaudeAgent):
            def build_cmd(self, message):
                return [sys.executable, "-c",
                        "import sys;sys.stdout.write(%r)" % (payload + "\n")]

        return Streamed(name="Claude", workspace=self.tmp)

    def test_a_turn_that_measures_nothing_clears_the_previous_number(self):
        a = self.agent([assistant(LAST), result(WINDOW)])
        a.turn("go")
        self.assertEqual(a.last_context["context_used"], 41616)
        # a second turn that dies with no result object at all. parse()
        # returns EARLY there and never touches last_context, so this is the
        # one path where only turn()'s own reset stands between the reader
        # and last turn's number presented as this one's.
        a.build_cmd = lambda message: [
            sys.executable, "-c", "import sys;sys.stdout.write('junk\n')"]
        with self.assertRaises(Exception):
            a.turn("go again")           # no reply: the never-forge path
        self.assertIsNone(a.last_context)

    def test_a_turn_that_measures_something_new_replaces_it(self):
        a = self.agent([assistant(LAST), result(WINDOW)])
        a.turn("go")
        smaller = {"input_tokens": 3, "cache_read_input_tokens": 9000}
        a.build_cmd = lambda message: [
            sys.executable, "-c", "import sys;sys.stdout.write(%r)"
            % ("\n".join([assistant(smaller), result(WINDOW)]) + "\n")]
        a.turn("again")
        self.assertEqual(a.last_context["context_used"], 9003)

    def test_context_is_not_folded_into_usage(self):
        # a level is not a spend, and record_usage sums what it is given
        a = claude()
        a.parse("\n".join([assistant(LAST), result(WINDOW)]))
        self.assertNotIn("context_used", a.last_usage)
        self.assertNotIn("context_window", a.last_usage)


# ---------------------------------------------------------------- opencode --

class OxContextTests(unittest.TestCase):
    def step(self, inp, read=0, write=0):
        return json.dumps({"type": "step_finish", "part": {
            "tokens": {"input": inp, "output": 90, "reasoning": 8,
                       "cache": {"read": read, "write": write}}}})

    def report(self, lines, model="opencode/nemotron-3-ultra-free"):
        a = relay.OpenCodeAgent(name="Ox", workspace=ROOT, model=model)
        a.parse("\n".join(lines))
        return a.last_context

    def test_the_cached_half_counts_too(self):
        # measured: input drops from 13,943 to 5,445 the moment caching
        # starts working, while the CONTEXT keeps growing
        got = self.report([self.step(5445, read=8640)])
        self.assertEqual(got["context_used"], 14085)

    def test_the_last_step_wins(self):
        got = self.report([self.step(13717), self.step(13943),
                           self.step(5445, read=8640), self.step(14701)])
        self.assertEqual(got["context_used"], 14701)

    def test_the_window_comes_from_the_model_catalog(self):
        got = self.report([self.step(14701)])
        self.assertEqual(got["context_window"],
                         relay.ox_model_details()["nemotron-3-ultra-free"]
                         ["context"])

    def test_a_model_the_catalog_does_not_know_gets_no_window(self):
        got = self.report([self.step(9000)], model="opencode/not-a-model")
        self.assertEqual(got, {"context_used": 9000})

    def test_a_turn_with_no_step_finish_reports_nothing(self):
        self.assertIsNone(self.report(
            [json.dumps({"type": "text", "part": {"id": "p", "text": "hi"}})]))

    def test_junk_tokens_never_raise(self):
        self.assertIsNone(self.report([
            json.dumps({"type": "step_finish", "part": {"tokens": "lots"}}),
            json.dumps({"type": "step_finish", "part": 4})]))

    def test_ox_reports_a_context_without_manufacturing_a_usage_dict(self):
        # the wall_ms rule one field over: a usage dict invented to carry
        # this would give OpenCode a by_seat entry reading "0 tokens"
        # exactly where the budget bar draws an honest blank
        a = relay.OpenCodeAgent(name="Ox", workspace=ROOT,
                                model="opencode/nemotron-3-ultra-free")
        a.parse(self.step(14701))
        self.assertTrue(a.last_context)
        self.assertIsNone(a.last_usage)


# --------------------------------------------------------- who cannot say --

class NoContextTests(unittest.TestCase):
    def test_codex_reports_no_context(self):
        # exec --json emits ONE usage event for a whole run, summed over its
        # internal model calls - nothing in it is a context size
        a = relay.CodexAgent(name="GPT", workspace=ROOT)
        a.parse(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 65922, "cached_input_tokens": 59392,
            "output_tokens": 415}}))
        self.assertIsNone(a.last_context)

    def test_gemini_reports_no_context(self):
        a = relay.GeminiAgent(name="Gemini", workspace=ROOT)
        self.assertIsNone(a.last_context)

    def test_no_adapter_folds_context_into_usage(self):
        import inspect
        for name, cls in relay.AGENT_TYPES.items():
            if cls is None:
                continue
            for attr in ("parse", "activity"):
                fn = getattr(cls, attr, None)
                if fn is None:
                    continue
                try:
                    src = inspect.getsource(fn)
                except (OSError, TypeError):
                    continue
                self.assertNotIn("last_usage[\"context", src, name)


# --------------------------------------------------------------- the row --

class RowTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_one(self, contexts):
        state = build_state(self.dir, [["a"], ["b"]], turns=1)
        for agent, ctx in zip(state["agents"], contexts):
            agent.last_context = ctx
        io = RecordingIO()
        relay.run_rounds(state, io)
        return state, io

    def test_the_row_carries_it(self):
        state, io = self.run_one([{"context_used": 41616,
                                   "context_window": 200000}, None])
        rows = [r for r in jsonl_rows(state) if r.get("name") == "Fake 1"]
        self.assertEqual(rows[0]["context"],
                         {"context_used": 41616, "context_window": 200000})

    def test_absence_is_an_absent_key_not_a_null(self):
        state, io = self.run_one([None, None])
        for r in jsonl_rows(state):
            self.assertNotIn("context", r)

    def test_the_live_event_carries_it_too(self):
        state, io = self.run_one([{"context_used": 900}, None])
        msgs = [e[1] for e in io.events
                if e[0] == "message" and e[1].get("context")]
        self.assertEqual(msgs[0]["context"], {"context_used": 900})

    def test_it_is_never_accumulated_into_the_chats_usage(self):
        # a level, not a spend: record_usage sums everything it is handed
        state, io = self.run_one([{"context_used": 41616,
                                   "context_window": 200000}] * 2)
        blob = json.dumps(state.get("usage") or {})
        self.assertNotIn("context", blob)


# ------------------------------------------------------------- the export --

class ExportTests(unittest.TestCase):
    def setUp(self):
        import export
        self.export = export
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, row_extra):
        os.makedirs(os.path.join(self.dir, "s"), exist_ok=True)
        row = {"kind": "claude", "name": "Claude", "text": "done", "round": 1}
        row.update(row_extra)
        with open(os.path.join(self.dir, "s", "messages.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        with open(os.path.join(self.dir, "s", "meta.json"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({"id": "s", "title": "t"}))
        out = os.path.join(self.dir, "out.html")
        self.assertNotIn(
            "error", self.export.export_session(
                os.path.join(self.dir, "s"), out) or {})
        with open(out, encoding="utf-8") as f:
            return f.read()

    def test_a_measured_window_gets_a_share(self):
        html = self.write({"context": {"context_used": 41616,
                                       "context_window": 200000}})
        self.assertIn("context: 41,616 / 200,000 (21%)", html)

    def test_no_window_says_so_rather_than_inventing_one(self):
        html = self.write({"context": {"context_used": 14701}})
        self.assertIn("context: 14,701 (no window reported)", html)
        self.assertNotIn("%)", html)

    def test_a_context_shows_even_when_no_usage_was_reported(self):
        # exactly the OpenCode case: no tokens, but a real context
        html = self.write({"context": {"context_used": 9000}})
        self.assertIn("context: 9,000", html)

    def test_a_row_with_neither_renders_no_pill_row(self):
        html = self.write({})
        self.assertNotIn("class='pill'", html)

    def test_the_export_stays_byte_identical(self):
        row = {"context": {"context_used": 41616, "context_window": 200000}}
        self.assertEqual(self.write(row), self.write(row))


# ---------------------------------------------------------------------- UI --

@unittest.skipUnless(NODE, "node not installed")
class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.rep = test_ui_boot.boot(test_ui_boot.UI, cls._tmp.name)
        cls.p = cls.rep.get("ctx") or {}
        cls.err = cls.rep.get("ctxError")
        with open(os.path.join(ROOT, "ui", "index.html"),
                  encoding="utf-8") as f:
            cls.ui = f.read()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        if self.err:
            self.fail("context probe threw: %s" % self.err)
        self.assertIsNone(self.rep.get("topLevelError"))

    def test_the_pill_shows_the_ratio_when_a_window_was_measured(self):
        self.assertEqual(self.p["pillWithWindow"], "ctx 41.6k/200k")

    def test_the_pill_shows_words_alone_without_one(self):
        self.assertEqual(self.p["pillNoWindow"], "ctx 14.7k")

    def test_absence_is_silent(self):
        self.assertEqual(self.p["pillNone"], "")
        self.assertEqual(self.p["pillZero"], "")

    def test_the_seat_card_draws_a_bar_only_with_a_window(self):
        # NO MEASURED DENOMINATOR, NO RING
        self.assertTrue(self.p["barWithWindow"])
        self.assertFalse(self.p["barNoWindow"])

    def test_the_seat_card_says_the_number_either_way(self):
        self.assertEqual(self.p["textWithWindow"], "41.6k / 200k · 21%")
        self.assertEqual(self.p["textNoWindow"], "14.7k in context")

    def test_a_seat_near_its_window_is_flagged(self):
        self.assertFalse(self.p["tightAtHalf"])
        self.assertTrue(self.p["tightAtNinety"])

    def test_the_bar_never_overflows_its_track(self):
        self.assertEqual(self.p["overFullWidth"], "100%")

    def test_the_seat_card_follows_the_newest_row(self):
        self.assertEqual(self.p["afterTwoRows"], "50k / 200k · 25%")

    def test_replay_repaints_it(self):
        self.assertEqual(self.p["afterReplay"], "33k / 200k · 17%")

    def test_a_masked_duel_row_never_repaints_a_seat_card(self):
        # the mask deliberately removed that identity, so restoring it on
        # the seat card would undo exactly what it did
        self.assertTrue(self.p["maskWorks"], "the probe never masked a row, "
                        "so it would pass with or without the guard")
        self.assertEqual(self.p["afterMasked"], "33k / 200k · 17%")

    def test_a_new_chat_clears_every_seat_card(self):
        self.assertEqual(self.p["afterNewChat"], "")

    def test_reopening_a_chat_clears_them_before_replaying_its_own(self):
        # a chat whose rows carry no context at all - every seat on Gemini,
        # say - would otherwise keep showing the LAST chat's numbers
        self.assertEqual(self.p["beforeReopen"], "60k / 200k · 30%")
        self.assertEqual(self.p["afterReopen"], "")

    def test_the_message_head_wraps_rather_than_overflowing(self):
        # Found in a REAL browser, which is the only thing that can see it:
        # the head is a flex row of pills and it was `nowrap`, so a row
        # carrying cost + context + delivery measured 559px inside a 490px
        # box and the transcript grew a horizontal scrollbar.
        block = self.ui.split("  .msg-head {")[1].split("}")[0]
        self.assertIn("flex-wrap: wrap", block)

    def test_the_tight_pill_rule_outranks_the_one_it_overrides(self):
        # `.msg-usage.ctx-tight` up with the seat CSS has EQUAL specificity
        # to `.msg-head .msg-usage` and loses on source order, so the pill
        # silently never turned red. Same class of miss as the `font:`
        # shorthand: valid CSS, no error anywhere, no effect.
        base = self.ui.index(".msg-head .msg-usage {")
        tight = self.ui.index(".msg-head .msg-usage.ctx-tight")
        self.assertGreater(tight, base)

    def test_short_tokens_never_invents_a_number(self):
        self.assertEqual(self.p["short"],
                         ["842", "1.2k", "41.6k", "200k", "1.5M", "12M"])
        self.assertEqual(self.p["shortJunk"], [None, None, None])


if __name__ == "__main__":
    unittest.main(verbosity=2)
