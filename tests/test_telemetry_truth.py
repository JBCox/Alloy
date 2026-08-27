"""W1.3 — the telemetry truth pass.

The lie this suite exists to kill, MEASURED not inferred (2026-08-27, a live
`codex exec --json` run of three one-word turns on this machine):

    turn 1  {"type":"turn.completed","usage":{"input_tokens":16194,
             "cached_input_tokens":11008,"cache_write_input_tokens":0,
             "output_tokens":5,"reasoning_output_tokens":0}}
    turn 2  ... input_tokens 34244, cached_input_tokens 26112, output_tokens 11
    turn 3  ... input_tokens 52313, cached_input_tokens 37120, output_tokens 17

Every one of those counters is THREAD-CUMULATIVE. `codex exec --json` emits
exactly one usage event per run (`turn.completed`) and it restates the whole
thread's totals, so `record_usage`'s additive accumulation counted turn 1
three times: 102,751 input tokens for three replies that really cost 52,313.
In a real chat on disk that reached 40,428,770 on a single row and
534,655,991 summed.

Two more bugs found in the same event while measuring it:
  - the field is spelled `cached_input_tokens`, and `_extract_usage` looked
    only for `cached_tokens` / `cache_read_input_tokens` — so 11,008 cached
    tokens arrived and were recorded as 0, on every GPT turn ever taken.
  - the wire carries no `total_tokens` at all; it is computed.

The codex BINARY does define a per-turn counter (`struct TokenUsageInfo with
3 elements: total_token_usage, last_token_usage, model_context_window`), but
that struct belongs to the app-server protocol — it is absent from the
`exec --json` surface, as the raw capture above shows. So a delta against a
baseline is not a preference here, it is the only route.

Claude is untouched and must stay untouched: its result object is genuinely
per-turn (a real chat on disk reads input [6, 4, 26] — non-monotone, so it
cannot be cumulative). Gemini and OpenCode write no `last_usage` at all and
stay honestly blank; there is nothing to difference and nothing to invent.
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import relay  # noqa: E402
import outcome  # noqa: E402


# The exact wire shape captured from codex-cli 0.147.0 on 2026-08-27.
def codex_stdout(thread_id, inp, cached, out, cache_write=0, reasoning=0):
    return "\n".join([
        json.dumps({"type": "thread.started", "thread_id": thread_id}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed",
                    "item": {"id": "item_0", "type": "agent_message",
                             "text": "ONE"}}),
        json.dumps({"type": "turn.completed",
                    "usage": {"input_tokens": inp,
                              "cached_input_tokens": cached,
                              "cache_write_input_tokens": cache_write,
                              "output_tokens": out,
                              "reasoning_output_tokens": reasoning}}),
    ])


# The three turns exactly as measured.
MEASURED = [(16194, 11008, 5), (34244, 26112, 11), (52313, 37120, 17)]
THREAD = "01a04372-b819-7d00-88df-c677c43bc4cf"


class CodexCumulativeTests(unittest.TestCase):
    """Drive the REAL CodexAgent.parse with the REAL captured bytes.

    Deliberately not a FakeAgent: the whole reason this bug survived is that
    a fake answers happily whatever it is handed. The artefact the rest of
    the app consumes is `agent.last_usage`, so that is what is asserted.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = relay.CodexAgent(self.tmp, name="GPT")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_turns(self, series, thread=THREAD):
        seen = []
        for inp, cached, out in series:
            self.turn_prelude()
            self.a.parse(codex_stdout(thread, inp, cached, out))
            seen.append(dict(self.a.last_usage or {}))
        return seen

    def turn_prelude(self):
        """Exactly what Agent.turn does to a seat before the child runs.

        Written out rather than assumed: `turn()` — not `parse()` — is what
        clears `last_usage`, so a harness that skipped it would leave the
        PREVIOUS turn's numbers standing and quietly pass a test about a turn
        that reported nothing.
        """
        self.a.last_usage = None
        self.a.before_run()

    def test_first_turn_on_a_new_thread_reports_the_whole_thread(self):
        # nothing came before it, so the cumulative total IS this turn's cost
        u = self.run_turns(MEASURED[:1])[0]
        self.assertEqual(u["input_tokens"], 16194)
        self.assertEqual(u["output_tokens"], 5)

    def test_later_turns_report_only_their_own_input_tokens(self):
        seen = self.run_turns(MEASURED)
        self.assertEqual([u["input_tokens"] for u in seen],
                         [16194, 18050, 18069])

    def test_later_turns_report_only_their_own_output_tokens(self):
        # the plan named only input tokens; output is cumulative too
        seen = self.run_turns(MEASURED)
        self.assertEqual([u["output_tokens"] for u in seen], [5, 6, 6])

    def test_cached_input_tokens_are_read_at_all(self):
        # measured arriving as `cached_input_tokens`, recorded as 0
        seen = self.run_turns(MEASURED)
        self.assertEqual([u["cached_tokens"] for u in seen],
                         [11008, 15104, 11008])

    def test_total_tokens_follow_the_differenced_halves(self):
        seen = self.run_turns(MEASURED)
        self.assertEqual([u["total_tokens"] for u in seen],
                         [16199, 18056, 18075])

    def test_the_summed_total_equals_the_threads_own_total(self):
        # the whole point: what record_usage accumulates must equal what the
        # provider says the thread cost, not N times the first turn
        seen = self.run_turns(MEASURED)
        state = {}
        for u in seen:
            relay.record_usage(state, u, seat_key=0, kind="seat")
        self.assertEqual(state["usage"]["input_tokens"], 52313)
        self.assertEqual(state["usage"]["output_tokens"], 17)

    def test_a_new_thread_resets_the_baseline(self):
        # /clear gives the seat a brand-new codex thread whose counters start
        # over. Differencing against the OLD thread's total would go negative.
        self.run_turns(MEASURED)
        fresh = self.run_turns([(900, 100, 7)], thread="a-different-thread")[0]
        self.assertEqual(fresh["input_tokens"], 900)
        self.assertEqual(fresh["output_tokens"], 7)

    def test_a_counter_that_goes_backwards_never_reports_a_negative(self):
        self.run_turns([(5000, 0, 50)])
        u = self.run_turns([(4000, 0, 40)])[0]
        self.assertEqual(u["input_tokens"], 0)
        self.assertEqual(u["output_tokens"], 0)
        self.assertEqual(u["total_tokens"], 0)

    def test_a_turn_reporting_no_usage_leaves_the_baseline_alone(self):
        # a turn that dies before turn.completed reports nothing at all; it
        # must not move the baseline, or the NEXT turn would be billed for
        # the gap as well
        self.run_turns(MEASURED[:2])
        before = dict(self.a.usage_baseline)
        self.turn_prelude()
        self.a.parse(json.dumps({"type": "thread.started",
                                 "thread_id": THREAD}))
        self.assertIsNone(self.a.last_usage)
        self.assertEqual(self.a.usage_baseline, before)
        u = self.run_turns(MEASURED[2:])[0]
        self.assertEqual(u["input_tokens"], 18069)

    def test_every_turn_is_labelled_with_its_basis(self):
        u = self.run_turns(MEASURED[:1])[0]
        self.assertEqual(u["basis_version"], relay.USAGE_BASIS)


class ClaudeUntouchedTests(unittest.TestCase):
    """RED guard: claude's numbers are per-turn already and must not move."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = relay.ClaudeAgent(self.tmp, name="Claude")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def result(self, inp, out, cost, cache_read=0):
        return json.dumps({
            "type": "result", "subtype": "success", "is_error": False,
            "session_id": "sess-1", "result": "hello",
            "total_cost_usd": cost, "duration_ms": 1234,
            "usage": {"input_tokens": inp, "output_tokens": out,
                      "cache_read_input_tokens": cache_read,
                      "cache_creation_input_tokens": 0}})

    def test_claude_numbers_pass_through_unchanged(self):
        seen = []
        # the real non-monotone series from a chat on disk
        for inp, out, cost in ((6, 4867, 0.9904875), (4, 3893, 0.25512),
                               (26, 7819, 1.9220395)):
            self.a.last_usage = None
            self.a.parse(self.result(inp, out, cost))
            seen.append(dict(self.a.last_usage))
        self.assertEqual([u["input_tokens"] for u in seen], [6, 4, 26])
        self.assertEqual([u["output_tokens"] for u in seen],
                         [4867, 3893, 7819])
        self.assertEqual([u["cost_usd"] for u in seen],
                         [0.9904875, 0.25512, 1.9220395])

    def test_claude_never_grows_a_baseline(self):
        self.a.parse(self.result(10, 20, 0.5))
        self.assertIsNone(self.a.usage_baseline)

    def test_claude_is_labelled_with_its_basis_too(self):
        self.a.parse(self.result(10, 20, 0.5))
        self.assertEqual(self.a.last_usage["basis_version"],
                         relay.USAGE_BASIS)


class BaselineSurvivesResumeTests(unittest.TestCase):
    """A resumed chat must not re-count the thread it is resuming.

    Alloy resumes constantly (tabs, restart_resume, /continue), so a baseline
    that lived only in memory would re-count the WHOLE thread once per
    reopen — on the 26-turn chat that started this, tens of millions of
    tokens per resume. `session_id` and `usage_baseline` are two halves of
    the same fact, so they persist and rehydrate together.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def saved_meta(self):
        session_dir = os.path.join(self.tmp, "session")
        ws = os.path.join(session_dir, "workspace")
        os.makedirs(ws, exist_ok=True)
        a = relay.CodexAgent(ws, name="GPT")
        for inp, cached, out in MEASURED:
            a.before_run()
            a.parse(codex_stdout(THREAD, inp, cached, out))
        store = relay.SessionStore(session_dir)
        store.open_transcript("t", [a], 3)
        state = {"agents": [a], "slot_ids": [0], "providers": ["gpt"],
                 "workspace": ws, "transcript": store.transcript,
                 "topic": "t", "title": "t", "created": store.created,
                 "yolo": False, "turns": 3, "rnd": 1, "max": 3,
                 "ended": False, "pending": {0: []}, "introduced": [True],
                 "store": store}
        state["log"] = relay.make_log(state, store)
        store.save(state)
        with open(store.meta_path, encoding="utf-8") as f:
            return json.load(f)

    def test_meta_carries_the_baseline_beside_the_session_id(self):
        seat = self.saved_meta()["seats"][0]
        self.assertEqual(seat["session_id"], THREAD)
        self.assertEqual(seat["usage_baseline"]["session_id"], THREAD)
        self.assertEqual(seat["usage_baseline"]["input_tokens"], 52313)

    def test_a_resumed_seat_does_not_recount_the_thread(self):
        meta = self.saved_meta()
        state = relay.rehydrate(meta, workspace=self.tmp)
        a = state["agents"][0]
        a.before_run()
        a.parse(codex_stdout(THREAD, 70000, 40000, 25))
        self.assertEqual(a.last_usage["input_tokens"], 70000 - 52313)
        self.assertEqual(a.last_usage["output_tokens"], 25 - 17)

    def test_the_baseline_is_never_reported_as_usage(self):
        # it is bookkeeping, not spend: it must not reach the rail, the
        # summary or the outcome record
        meta = self.saved_meta()
        summary = relay.session_summary(
            os.path.join(self.tmp, "session"), meta=meta)
        self.assertNotIn("usage_baseline", json.dumps(summary))
        rec = outcome.build_outcome(os.path.join(self.tmp, "session"))
        self.assertNotIn("usage_baseline", json.dumps(rec))

    def test_a_meta_with_no_baseline_still_rehydrates(self):
        meta = self.saved_meta()
        meta["seats"][0].pop("usage_baseline", None)
        state = relay.rehydrate(meta, workspace=self.tmp)
        a = state["agents"][0]
        self.assertIsNone(a.usage_baseline)
        a.parse(codex_stdout(THREAD, 70000, 40000, 25))
        # honest fallback: with no baseline the thread total is all we know
        self.assertEqual(a.last_usage["input_tokens"], 70000)


class ForgetThreadTests(unittest.TestCase):
    """The baseline is the third half of "this seat has a live thread"."""

    def test_forget_thread_drops_both_halves(self):
        tmp = tempfile.mkdtemp()
        try:
            a = relay.CodexAgent(tmp, name="GPT")
            a.last_usage = None
            a.before_run()
            a.parse(codex_stdout(THREAD, 100, 0, 5))
            self.assertIsNotNone(a.session_id)
            self.assertIsNotNone(a.usage_baseline)
            a.forget_thread()
            self.assertIsNone(a.session_id)
            self.assertIsNone(a.usage_baseline)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_every_thread_discard_site_uses_it(self):
        # /clear (sequential and free-mode) and /compact are the three places
        # a live provider thread is thrown away. A fourth appearing without
        # this call is how the pairing rots.
        import inspect
        src = inspect.getsource(relay)
        body = src[src.index("class Agent"):]
        self.assertEqual(body.count("agent.forget_thread()"), 3)
        # The only surviving bare clears are the throwaway side-call adapters
        # (brief, auto-title), which null their id to FORCE statelessness on
        # an object that is about to be dropped — a different act, marked as
        # such in the source. A new bare clear without that marker is a live
        # thread being discarded and half-forgotten.
        bare = [ln.strip() for ln in body.splitlines()
                if "agent.session_id = None" in ln]
        self.assertTrue(bare, "the marker line vanished — recheck this test")
        for ln in bare:
            self.assertIn("stateless by design", ln)

    def test_a_fork_drops_the_baseline_too(self):
        import inspect
        import fork
        self.assertIn('seat.pop("usage_baseline", None)',
                      inspect.getsource(fork.fork_session))


class BasisVersionTests(unittest.TestCase):
    """History is never rewritten, so the totals say which basis made them."""

    def test_a_fresh_chat_records_only_the_new_basis(self):
        state = {}
        relay.record_usage(state, {"input_tokens": 10, "output_tokens": 2,
                                   "total_tokens": 12,
                                   "basis_version": relay.USAGE_BASIS},
                           seat_key=0)
        self.assertEqual(state["usage"]["basis_versions"],
                         [relay.USAGE_BASIS])

    def test_totals_that_predate_the_label_are_seeded_as_basis_one(self):
        # a chat resumed from before this fix carries summed-cumulative
        # totals; adding correct ones to them makes the total a mixture and
        # it has to say so
        state = {"usage": {"total_cost_usd": 0.0, "input_tokens": 999,
                           "output_tokens": 9, "total_tokens": 1008,
                           "by_seat": {}, "by_kind": {}}}
        relay.record_usage(state, {"input_tokens": 10, "output_tokens": 2,
                                   "total_tokens": 12,
                                   "basis_version": relay.USAGE_BASIS},
                           seat_key=0)
        self.assertEqual(state["usage"]["basis_versions"], [1, 2])

    def test_an_old_chat_with_no_spend_is_not_called_mixed(self):
        state = {"usage": {"total_cost_usd": 0.0, "input_tokens": 0,
                           "output_tokens": 0, "total_tokens": 0,
                           "by_seat": {}, "by_kind": {}}}
        relay.record_usage(state, {"input_tokens": 10, "output_tokens": 2,
                                   "total_tokens": 12,
                                   "basis_version": relay.USAGE_BASIS},
                           seat_key=0)
        self.assertEqual(state["usage"]["basis_versions"],
                         [relay.USAGE_BASIS])

    def test_usage_with_no_label_counts_as_the_old_basis(self):
        state = {}
        relay.record_usage(state, {"input_tokens": 10, "output_tokens": 2,
                                   "total_tokens": 12}, seat_key=0)
        self.assertEqual(state["usage"]["basis_versions"], [1])


class WallClockTests(unittest.TestCase):
    """`wall_ms` is OUR measurement of the whole child process.

    Deliberately distinct from `duration_ms`, which is the CLI's own
    self-report: claude sends one, codex sends none. Two different facts, so
    two different keys — and TTFT is not shipped at all, because the first
    line out of these CLIs is the process booting, not the model answering.
    """

    class Recorder(relay.Agent):
        streams_progress = False

        def __init__(self, ws, usage, sleep=0.02):
            super().__init__(ws, name="Rec")
            self._usage = usage
            self._sleep = sleep

        def build_cmd(self, message):
            import sys as _s
            return [_s.executable, "-c",
                    "import time,sys; time.sleep(%r); print('ok')"
                    % self._sleep]

        def parse(self, stdout):
            if self._usage is not None:
                self.last_usage = dict(self._usage)
            return "reply"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_wall_ms_rides_a_usage_dict_that_exists(self):
        a = self.Recorder(self.tmp, {"input_tokens": 5, "output_tokens": 1,
                                     "total_tokens": 6})
        a.turn("hi")
        self.assertIn("wall_ms", a.last_usage)
        self.assertGreaterEqual(a.last_usage["wall_ms"], 10)

    def test_wall_ms_never_invents_usage_for_a_seat_that_reports_none(self):
        # Gemini and OpenCode report nothing. Manufacturing a usage dict just
        # to carry a wall time would give them a by_seat entry reading
        # "0 tokens" where the budget bar renders an honest blank today.
        a = self.Recorder(self.tmp, None)
        a.turn("hi")
        self.assertIsNone(a.last_usage)

    def test_wall_ms_is_kept_apart_from_the_clis_own_duration(self):
        a = self.Recorder(self.tmp, {"input_tokens": 5, "output_tokens": 1,
                                     "total_tokens": 6, "duration_ms": 999})
        a.turn("hi")
        self.assertEqual(a.last_usage["duration_ms"], 999)
        self.assertNotEqual(a.last_usage["wall_ms"], 999)

    def test_wall_ms_accumulates_per_seat(self):
        state = {}
        relay.record_usage(state, {"input_tokens": 1, "output_tokens": 1,
                                   "total_tokens": 2, "wall_ms": 100},
                           seat_key=0)
        relay.record_usage(state, {"input_tokens": 1, "output_tokens": 1,
                                   "total_tokens": 2, "wall_ms": 250},
                           seat_key=0)
        self.assertEqual(state["usage"]["by_seat"]["0"]["wall_ms"], 350)


class NothingToDifferenceTests(unittest.TestCase):
    """Gemini and OpenCode write no usage at all — measured, not assumed."""

    def test_only_claude_and_codex_report_usage(self):
        import inspect
        writes = set()
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
                if "self.last_usage" in src:
                    writes.add(name)
        self.assertEqual(writes, {"claude", "gpt"})


import test_ui_boot  # noqa: E402


@unittest.skipUnless(test_ui_boot.NODE, "node not installed")
class UsagePillTests(unittest.TestCase):
    """The pill, through the REAL script in Node."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.report = test_ui_boot.boot(test_ui_boot.UI, cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def p(self, key):
        return (self.report.get("usagePill") or {}).get(key)

    def test_probe_ran_clean(self):
        self.assertIsNone(self.report.get("usagePillError"),
                          "pill probe threw: %s"
                          % self.report.get("usagePillError"))

    def test_a_seat_that_reports_nothing_shows_nothing(self):
        self.assertEqual(self.p("none"), "")
        self.assertEqual(self.p("nothingReported"), "")

    def test_the_clis_own_duration_still_wins_where_it_exists(self):
        # claude's 60905 ms, not the relay's 61500 ms wall clock
        self.assertEqual(self.p("claude"), "$0.255 · 3,897 toks · 60.9s")

    def test_a_gpt_row_finally_shows_a_time(self):
        # no cost and no duration_ms from codex; wall_ms is the only clock
        self.assertEqual(self.p("codex"), "18,056 toks · 4.2s")

    def test_no_clock_at_all_is_still_no_clock(self):
        self.assertEqual(self.p("neitherClock"), "6 toks")


if __name__ == "__main__":
    unittest.main(verbosity=2)
