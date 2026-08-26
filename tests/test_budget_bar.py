"""Feature #20 — the live budget bar.

Two halves, both token-free:

Engine: record_usage must push its additive totals at the front end live
(the `usage` event through the LoopIO seam stashed by run_rounds), without
changing what meta.json persists. Seats whose CLI reports nothing stay out
of the payload entirely — never estimated.

UI: the strip is driven through the real uiEvent path in Node via
test_ui_boot's harness; the projection math is pinned as pure functions.
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


def snap_of(state):
    return relay.usage_snapshot(state)


class UsageSnapshotTests(unittest.TestCase):
    def test_snapshot_is_reported_truth_only(self):
        state = {"usage": {
            "total_cost_usd": 1.234567, "input_tokens": 100,
            "output_tokens": 50, "total_tokens": 150,
            "by_seat": {"0": {"cost_usd": 1.234567, "input_tokens": 100,
                              "output_tokens": 50, "total_tokens": 150,
                              "turns": 2},
                        # a seat that reports tokens but no cost (none do
                        # today, but the shape must not invent a number)
                        "1": {"cost_usd": None, "total_tokens": 999}},
        }}
        snap = snap_of(state)
        self.assertEqual(snap["total_cost_usd"], 1.2346)
        self.assertEqual(snap["total_tokens"], 150)
        self.assertEqual(snap["by_seat"]["0"],
                         {"cost_usd": 1.2346, "total_tokens": 150})
        self.assertIsNone(snap["by_seat"]["1"]["cost_usd"])
        self.assertEqual(snap["by_seat"]["1"]["total_tokens"], 999)

    def test_snapshot_of_no_usage_is_zeroed_and_empty(self):
        snap = snap_of({})
        self.assertEqual(snap["total_cost_usd"], 0.0)
        self.assertEqual(snap["total_tokens"], 0)
        self.assertEqual(snap["by_seat"], {})

    def test_reporting_nothing_seats_have_no_entry(self):
        """Gemini/Ox report nothing, so record_usage never creates their
        by_seat entry — absence IS the honest blank."""
        state = {}
        relay.record_usage(state, {"cost_usd": 0.25}, seat_key=0)
        self.assertEqual(list(snap_of(state)["by_seat"].keys()), ["0"])


class _RecordingIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload=None):
        self.events.append((event, payload or {}))


class RecordUsageEmitTests(unittest.TestCase):
    def test_emit_carries_additive_totals(self):
        io = _RecordingIO()
        state = {"_usage_io": io}
        relay.record_usage(state, {"cost_usd": 0.10, "total_tokens": 10},
                           seat_key=0)
        relay.record_usage(state, {"cost_usd": 0.32, "total_tokens": 15},
                           seat_key=1, kind="supervisor")
        usage_events = [p for e, p in io.events if e == "usage"]
        self.assertEqual(len(usage_events), 2)
        self.assertEqual(usage_events[-1]["total_cost_usd"], 0.42)
        self.assertEqual(usage_events[-1]["total_tokens"], 25)
        self.assertEqual(sorted(usage_events[-1]["by_seat"].keys()),
                         ["0", "1"])
        self.assertEqual(usage_events[-1]["by_seat"]["1"]["cost_usd"], 0.32)

    def test_no_emit_without_a_front_end(self):
        """Outside a run (the app's brief precompute) there is no seam —
        recording must stay silent, never crash."""
        state = {}
        relay.record_usage(state, {"cost_usd": 0.10}, seat_key=0)
        self.assertEqual(relay.usage_snapshot(state)["total_cost_usd"], 0.10)

    def test_no_emit_for_empty_usage(self):
        io = _RecordingIO()
        relay.record_usage({"_usage_io": io}, None, seat_key=0)
        relay.record_usage({"_usage_io": io}, {}, seat_key=0)
        self.assertEqual(io.events, [])

    def test_emit_never_breaks_the_turn(self):
        """Telemetry is best-effort, same contract as activity narration."""

        class Boom:
            def emit(self, event, payload=None):
                raise RuntimeError("front end exploded")

        relay.record_usage({"_usage_io": Boom()}, {"cost_usd": 0.10},
                           seat_key=0)


class LoopWiringTests(unittest.TestCase):
    """The seam must be live inside the REAL loop: a turn that records usage
    emits, and meta.json persistence semantics are untouched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-budget-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_rounds_wires_the_usage_emit(self):
        from test_loop import RecordingIO, build_state, saved_meta

        state = build_state(self.tmp, [["hi"]], turns=1)
        agent = state["agents"][0]

        def turn(message, on_activity=None):
            relay.record_usage(
                state, {"cost_usd": 0.25, "input_tokens": 10,
                        "output_tokens": 5, "total_tokens": 15},
                seat_key=state["slot_ids"][0], kind="seat")
            agent.session_id = "fake-session-x"
            return "hello"

        agent.turn = turn
        io = RecordingIO()
        relay.run_rounds(state, io)
        events = [p for e, p in io.events if e == "usage"]
        self.assertTrue(events, "no usage event reached the front end")
        self.assertEqual(events[-1]["total_cost_usd"], 0.25)
        # persistence unchanged: meta still snapshots the same accumulator
        meta = saved_meta(state)
        self.assertEqual((meta.get("usage") or {}).get("total_cost_usd"), 0.25)


NODE = test_ui_boot.NODE


@unittest.skipUnless(NODE, "node not installed")
class BudgetBarUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.report = test_ui_boot.boot(test_ui_boot.UI, cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def b(self, key):
        return (self.report.get("budget") or {}).get(key)

    def test_probe_ran_clean(self):
        self.assertIsNone(self.report.get("budgetError"),
                          "budget probe threw: %s" % self.report.get("budgetError"))
        self.assertIsNone(self.report.get("topLevelError"))

    def test_strip_ships_hidden_until_something_is_reported(self):
        self.assertTrue(self.b("present"), "#budgetStrip missing from markup")
        self.assertTrue(self.b("hiddenAtBoot"))

    def test_projection_math_edges(self):
        # no cap ⇒ no projection, ever
        self.assertEqual(self.b("noCap"), "null")
        # zero burn ⇒ no projection (a flat line predicts nothing)
        self.assertEqual(self.b("zeroBurn"), "null")
        # no elapsed time ⇒ no projection
        self.assertEqual(self.b("noTime"), "null")
        # already over ⇒ an explicit state, not a negative minute count
        self.assertIn('"reached":true', self.b("overCap") or "")
        proj = self.b("proj") or {}
        self.assertEqual(proj.get("label"), "10:07",
                         "$0.42 in 18 min against $2.00 from 09:00 → ~10:07")
        self.assertTrue(60 < proj.get("mins", -1) < 75)

    def test_strip_wording_pins_the_estimate_and_honesty(self):
        self.assertEqual(
            self.b("textWithCap"),
            "$0.42 spent · of $2.00 cap · 18 min · ≈ hits cap ~10:07")
        self.assertEqual(self.b("textNoCap"),
                         "$0.05 spent · no cap set")
        self.assertEqual(self.b("textZeroBurn"),
                         "$0.00 spent · of $2.00 cap · 30 min",
                         "zero burn must show NO projection")
        self.assertTrue(self.b("textOver").endswith("cap reached"))
        self.assertEqual(self.b("textNothing"),
                         "no spend reported yet · no cap set")

    def test_live_usage_event_lights_the_strip(self):
        self.assertTrue(self.b("shownAfterUsage"))
        text = self.b("textAfterUsage") or ""
        self.assertTrue(text.startswith("$0.42 spent"))
        self.assertIn("no cap set", text)
        self.assertNotIn("≈", text, "no projection without a cap and a clock")

    def test_tooltip_reports_per_seat_and_keeps_blanks_explicit(self):
        """Seats 0 (Claude) reported; GPT and Gemini have no cost entry, so
        they land together in ONE explicit blank group — never a number."""
        tip = self.b("tipAfterUsage") or ""
        self.assertIn("Claude — $0.4200", tip)
        self.assertIn("GPT, Gemini — not reported", tip)

    def test_second_event_replaces_with_new_additive_totals(self):
        self.assertTrue((self.b("textAfterSecond") or "").startswith("$0.90 spent"))
        tip = self.b("tipAfterSecond") or ""
        self.assertIn("$0.5000", tip)
        self.assertIn("$0.4000", tip)
        # now only Gemini is blank
        self.assertIn("Gemini — not reported", tip)

    def test_fresh_stage_clears_the_bar(self):
        self.assertTrue(self.b("hiddenAfterNewChat"))
        self.assertEqual(self.b("textAfterNewChat"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
