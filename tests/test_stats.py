"""W1.5 - cross-session stats, and the playbook's first UI.

Two halves over the same pile of ``sessions/*/outcome.json`` records.

THE NUMBERS. Three things the real data forced, none of them in the plan:

* **The cache convention is PER PROVIDER**, and using one formula for both
  is wrong by ~44% one way or ~1000x the other. Measured 2026-08-27:
  claude reported ``input_tokens: 10`` with ``cached_tokens: 86,646`` on a
  turn whose context - derived independently from its assistant events -
  was exactly 86,656, so its cached tokens are DISJOINT from its input.
  codex reported ``input_tokens: 33,886`` with ``cached_tokens: 27,136`` on
  the second turn of a two-turn job, where a disjoint reading would mean
  33,886 tokens of *fresh* material for one short reply; its cached tokens
  are a SUBSET. Anyone else is unknown, so there is no prompt size and no
  hit rate rather than a plausible wrong one.
* **The old records hold the pre-W1.3 lie.** GPT's counters are
  thread-cumulative and were summed until 2026-08-27; the real sessions
  folder aggregates to **559,310,306** input tokens for GPT if you believe
  them. History is not rewritten, so those token counters are excluded and
  the exclusion is STATED - a footnote under a 559-million headline is a
  caption on a lie, not a correction.
* **outcome.py was dropping the two fields this needs.** It re-derives seat
  usage from rows and never carried ``cached_tokens`` or ``wall_ms``,
  though every row has had them since W1.3 - the same shape as the
  artifacts list the UI never read.

THE PLAYBOOK. It silently steers every Supervisor plan through
``relay.playbook_block`` and had no UI at all. ``retro.set_rule`` is the one
validated write path for a pin or a dismissal, and it refuses to edit the
wording of an UNPINNED rule, because ``merge_heuristics`` overwrites those
on the next refresh - an edit that looked accepted and was gone by morning.
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

import app  # noqa: E402
import outcome  # noqa: E402
import relay  # noqa: E402
import retro  # noqa: E402
import stats  # noqa: E402
import test_ui_boot  # noqa: E402
from test_app_headless import FakeWindow  # noqa: E402

NODE = test_ui_boot.NODE


def rec(seats, session="s"):
    return {"session": {"id": session},
            "hard_facts": {"seats": seats},
            "human_feedback": {"rating": None}}


def seat(provider, model="m", turns=1, basis=(2,), **usage):
    u = {"cost_usd": None, "input_tokens": 0, "output_tokens": 0,
         "total_tokens": 0}
    u.update(usage)
    if basis is not None:
        u["basis_versions"] = list(basis)
    return {"id": 0, "provider": provider, "model": model, "turns": turns,
            "usage": u}


# ------------------------------------------------------ the two conventions --

class CacheConventionTests(unittest.TestCase):
    def test_claude_cached_tokens_are_disjoint_from_its_input(self):
        # measured: input 10 + cached 86,646 == the turn's own context, 86,656
        self.assertEqual(stats.prompt_tokens("claude", 10, 86646), 86656)

    def test_codex_cached_tokens_are_a_subset_of_its_input(self):
        # measured: 33,886 in / 27,136 cached on turn 2 of a two-turn job
        self.assertEqual(stats.prompt_tokens("gpt", 33886, 27136), 33886)

    def test_one_formula_for_both_would_be_wrong_either_way(self):
        # the whole reason the table exists, stated as a test
        self.assertNotEqual(stats.prompt_tokens("gpt", 33886, 27136),
                            33886 + 27136)
        self.assertNotEqual(stats.prompt_tokens("claude", 10, 86646), 10)

    def test_an_unmeasured_provider_gets_no_prompt_and_no_rate(self):
        for provider in ("gemini", "ox", "unknown", None):
            self.assertIsNone(stats.prompt_tokens(provider, 100, 50))
            self.assertIsNone(stats.cache_hit(provider, 100, 50))

    def test_the_rates_come_out_right(self):
        self.assertEqual(stats.cache_hit("gpt", 33886, 27136), 0.8008)
        self.assertEqual(stats.cache_hit("claude", 10, 86646), 0.9999)

    def test_nothing_reported_is_None_not_a_rate(self):
        self.assertIsNone(stats.cache_hit("claude", None, None))
        self.assertIsNone(stats.cache_hit("claude", 500, None))
        self.assertIsNone(stats.prompt_tokens("claude", 500, None))

    def test_a_rate_over_one_is_a_broken_assumption_not_a_discovery(self):
        # claude's numbers read with codex's convention: cached >> input
        self.assertIsNone(stats.cache_hit("gpt", 10, 86646))

    def test_nothing_sent_has_no_rate(self):
        self.assertIsNone(stats.cache_hit("claude", 0, 0))

    def test_nothing_cached_is_a_real_zero(self):
        # measured absence is None; a measured zero is 0.0 and must stay so
        self.assertEqual(stats.cache_hit("claude", 4000, 0), 0.0)


# ------------------------------------------------------------ the old lie --

class TrustedBasisTests(unittest.TestCase):
    def test_gpt_token_counts_before_the_fix_are_not_believed(self):
        self.assertFalse(stats.trusted_basis("gpt", [1]))
        self.assertTrue(stats.trusted_basis("gpt", [2]))

    def test_a_mixed_chat_is_judged_by_its_oldest_basis(self):
        # the summed total is only as good as the worst number in it
        self.assertFalse(stats.trusted_basis("gpt", [1, 2]))

    def test_an_absent_basis_means_one(self):
        self.assertFalse(stats.trusted_basis("gpt", None))
        self.assertFalse(stats.trusted_basis("gpt", []))

    def test_claude_was_never_wrong_so_basis_one_is_fine(self):
        # its result object is genuinely per-turn; nothing to difference
        self.assertTrue(stats.trusted_basis("claude", [1]))
        self.assertTrue(stats.trusted_basis("gemini", None))

    def test_the_real_sessions_folder_would_report_half_a_billion(self):
        # the number that makes this a headline and not a footnote
        bad = [rec([seat("gpt", turns=20, basis=(1,),
                         input_tokens=559310306, output_tokens=1000)])]
        got = stats.collect(bad)
        row = got["providers"][0]
        self.assertIsNone(row["input_tokens"])
        self.assertEqual(row["superseded_sessions"], 1)
        self.assertEqual(row["turns"], 20)      # turns are unaffected

    def test_the_exclusion_is_counted_per_session_not_per_seat(self):
        two_seats = rec([seat("gpt", basis=(1,), input_tokens=5),
                         dict(seat("gpt", basis=(1,), input_tokens=7), id=1)])
        got = stats.collect([two_seats])
        self.assertEqual(got["providers"][0]["superseded_sessions"], 1)
        self.assertEqual(got["totals"]["superseded_sessions"], 1)

    def test_a_trusted_seat_beside_a_stale_one_still_counts(self):
        mixed = rec([seat("gpt", basis=(1,), input_tokens=999999),
                     dict(seat("claude", basis=(2,), input_tokens=40,
                               cached_tokens=8000), id=1)])
        got = stats.collect([mixed])
        rows = {r["key"]: r for r in got["providers"]}
        self.assertIsNone(rows["gpt"]["input_tokens"])
        self.assertEqual(rows["claude"]["input_tokens"], 40)
        self.assertEqual(rows["claude"]["prompt_tokens"], 8040)


# --------------------------------------------------------------- collect --

class CollectTests(unittest.TestCase):
    def test_rows_group_by_provider_and_by_model(self):
        got = stats.collect([
            rec([seat("claude", "haiku", turns=2, input_tokens=10)]),
            rec([seat("claude", "opus", turns=3, input_tokens=20)], "t"),
        ])
        self.assertEqual([r["key"] for r in got["providers"]], ["claude"])
        self.assertEqual(got["providers"][0]["turns"], 5)
        self.assertEqual(sorted(r["label"] for r in got["models"]),
                         ["haiku", "opus"])

    def test_a_session_counts_once_per_row_however_many_seats(self):
        two = rec([seat("claude", "haiku", turns=1),
                   dict(seat("claude", "haiku", turns=1), id=1)])
        got = stats.collect([two])
        self.assertEqual(got["providers"][0]["sessions"], 1)
        self.assertEqual(got["models"][0]["sessions"], 1)
        self.assertEqual(got["providers"][0]["turns"], 2)

    def test_a_never_reported_number_is_None_not_zero(self):
        # Gemini reports no cost at all; a 0 in a spend column reads as
        # "this was free" rather than "nobody said"
        got = stats.collect([rec([seat("gemini", turns=4)])])
        row = got["providers"][0]
        self.assertIsNone(row["cost_usd"])
        self.assertIsNone(row["wall_ms"])
        self.assertIsNone(row["cache_hit"])
        self.assertEqual(row["turns"], 4)

    def test_a_reported_number_survives_beside_unreported_ones(self):
        got = stats.collect([rec([seat("claude", cost_usd=0.25,
                                       input_tokens=10, cached_tokens=90,
                                       wall_ms=1200)])])
        row = got["providers"][0]
        self.assertEqual(row["cost_usd"], 0.25)
        self.assertEqual(row["wall_ms"], 1200)
        self.assertEqual(row["prompt_tokens"], 100)

    def test_totals_carry_no_combined_hit_rate(self):
        # it would average two different quantities
        got = stats.collect([rec([seat("claude", input_tokens=10,
                                       cached_tokens=90),
                                  dict(seat("gpt", input_tokens=100,
                                            cached_tokens=80), id=1)])])
        self.assertIsNone(got["totals"]["cache_hit"])
        self.assertIsNone(got["totals"]["prompt_tokens"])

    def test_a_record_with_no_seats_is_counted_but_adds_nothing(self):
        got = stats.collect([{"hard_facts": {}}, rec([seat("claude")])])
        self.assertEqual(got["sessions_counted"], 2)
        self.assertEqual(got["sessions_with_usage"], 1)

    def test_junk_never_raises(self):
        got = stats.collect([None, 4, {}, {"hard_facts": {"seats": "no"}},
                             rec([None, 7, {"provider": "claude"}])])
        # the two non-dicts are not records at all and are not counted
        self.assertEqual(got["sessions_counted"], 3)

    def test_a_missing_sessions_dir_is_empty_not_an_error(self):
        self.assertEqual(stats.scan(os.path.join(ROOT, "no-such-dir")), [])


# ---------------------------------------------------- outcome carries them --

class OutcomeFieldsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sess = os.path.join(self.dir, "sess")
        os.makedirs(self.sess)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def build(self, usages):
        rows = [{"speaker": 0, "provider": "claude", "name": "Claude",
                 "text": "hi", "round": i + 1, "ts": "2026-08-27T10:0%d:00" % i,
                 "usage": u}
                for i, u in enumerate(usages)]
        with open(os.path.join(self.sess, "messages.jsonl"), "w",
                  encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        with open(os.path.join(self.sess, "meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"id": "sess", "seats": [
                {"id": 0, "label": "Claude", "provider": "claude",
                 "model": "claude-haiku-4-5"}]}, f)
        return outcome.build_outcome(self.sess)

    def test_cached_tokens_and_wall_ms_reach_the_record(self):
        # they ride every row and the SECOND reader was dropping them
        got = self.build([
            {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 900,
             "wall_ms": 1200, "basis_version": 2},
            {"input_tokens": 8, "output_tokens": 3, "cached_tokens": 1100,
             "wall_ms": 800, "basis_version": 2}])
        u = got["hard_facts"]["seats"][0]["usage"]
        self.assertEqual(u["cached_tokens"], 2000)
        self.assertEqual(u["wall_ms"], 2000)

    def test_a_seat_that_reports_neither_gets_no_key(self):
        got = self.build([{"input_tokens": 4, "output_tokens": 1}])
        u = got["hard_facts"]["seats"][0]["usage"]
        self.assertNotIn("cached_tokens", u)
        self.assertNotIn("wall_ms", u)

    def test_the_basis_is_a_set_and_absent_means_one(self):
        got = self.build([{"input_tokens": 4, "output_tokens": 1},
                          {"input_tokens": 4, "output_tokens": 1,
                           "basis_version": 2}])
        self.assertEqual(got["hard_facts"]["seats"][0]["usage"]
                         ["basis_versions"], [1, 2])

    def test_a_real_record_feeds_stats_end_to_end(self):
        got = self.build([{"input_tokens": 10, "output_tokens": 5,
                           "cached_tokens": 86646, "cost_usd": 0.13,
                           "wall_ms": 7651, "basis_version": 2}])
        row = stats.collect([got])["providers"][0]
        self.assertEqual(row["prompt_tokens"], 86656)
        self.assertEqual(row["cache_hit"], 0.9999)
        self.assertEqual(row["wall_ms"], 7651)


# -------------------------------------------------------------- playbook --

class PlaybookRuleTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        retro.write_playbook(self.dir, {
            "playbook_version": 1, "updated": "2026-08-27T10:00:00",
            "heuristics": [
                {"heuristic_id": "a", "directive": "Do A", "status": "active",
                 "evidence_count": 3, "source": "human_reason"},
                {"heuristic_id": "b", "directive": "Do B", "status": "active",
                 "evidence_count": 1, "source": "inferred_pattern"},
            ]})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rules(self):
        return {r["heuristic_id"]: r
                for r in retro.rules_for_display(
                    retro.read_playbook(self.dir))}

    def test_display_carries_status_so_a_dismissal_can_be_undone(self):
        retro.set_rule(self.dir, "b", dismissed=True)
        self.assertEqual(self.rules()["b"]["status"], "dismissed")

    def test_pinning_and_unpinning_round_trip(self):
        retro.set_rule(self.dir, "a", pinned=True)
        self.assertTrue(self.rules()["a"]["pinned"])
        retro.set_rule(self.dir, "a", pinned=False)
        self.assertFalse(self.rules()["a"]["pinned"])

    def test_toggling_one_field_never_touches_the_other(self):
        retro.set_rule(self.dir, "a", dismissed=True)
        retro.set_rule(self.dir, "a", pinned=True)
        self.assertEqual(self.rules()["a"]["status"], "dismissed")
        self.assertTrue(self.rules()["a"]["pinned"])
        # and the OTHER direction, which is the one that can actually lose
        # something: an unconditional `pinned = bool(None)` would silently
        # unpin a rule every time it was dismissed (a RED pass found the
        # first half of this test could not see that)
        retro.set_rule(self.dir, "b", pinned=True)
        retro.set_rule(self.dir, "b", dismissed=True)
        self.assertTrue(self.rules()["b"]["pinned"])
        retro.set_rule(self.dir, "b", dismissed=False)
        self.assertTrue(self.rules()["b"]["pinned"])

    def test_wording_can_only_be_edited_on_a_pinned_rule(self):
        # merge_heuristics overwrites an unpinned rule's directive on the
        # next refresh, so an edit there looks accepted and is gone by
        # morning
        retro.set_rule(self.dir, "a", directive="Rewritten")
        self.assertEqual(self.rules()["a"]["directive"], "Do A")
        retro.set_rule(self.dir, "a", pinned=True, directive="Rewritten")
        self.assertEqual(self.rules()["a"]["directive"], "Rewritten")

    def test_an_unknown_id_is_refused_never_invented(self):
        # a rule with no derivation behind it is what provenance exists to
        # make impossible
        self.assertIsNone(retro.set_rule(self.dir, "nope", pinned=True))
        self.assertEqual(len(self.rules()), 2)

    def test_a_dismissed_rule_survives_a_full_refresh(self):
        retro.set_rule(self.dir, "a", dismissed=True)
        merged = retro.merge_heuristics(retro.read_playbook(self.dir), [])
        by_id = {h["heuristic_id"]: h for h in merged["heuristics"]}
        self.assertEqual(by_id["a"]["status"], "dismissed")

    def test_a_dismissed_rule_stops_steering_the_planner(self):
        block_before = relay.playbook_block(self.dir)
        self.assertIn("Do B", block_before)
        retro.set_rule(self.dir, "b", dismissed=True)
        self.assertNotIn("Do B", relay.playbook_block(self.dir))

    def test_display_ranks_pinned_first_and_dismissed_last(self):
        retro.set_rule(self.dir, "b", pinned=True)
        rows = retro.rules_for_display(retro.read_playbook(self.dir))
        self.assertEqual([r["heuristic_id"] for r in rows], ["b", "a"])
        retro.set_rule(self.dir, "b", pinned=False, dismissed=True)
        rows = retro.rules_for_display(retro.read_playbook(self.dir))
        self.assertEqual([r["heuristic_id"] for r in rows], ["a", "b"])


# ---------------------------------------------------------------- bridge --

class BridgeTests(unittest.TestCase):
    """The real app.Api against a fake window. Registered is not callable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_dir, self.old_tabs = relay.SESSIONS_DIR, relay.TABS_FILE
        relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        self.api = app.Api()
        self.api._window = FakeWindow()

    def tearDown(self):
        relay.SESSIONS_DIR, relay.TABS_FILE = self.old_dir, self.old_tabs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def event(self, name):
        self.api._emit_q.join()
        for e in self.api._window.events():
            if e["event"] == name:
                return e["payload"]
        return None

    def wait(self, name):
        for _ in range(200):
            got = self.event(name)
            if got is not None:
                return got
            import time
            time.sleep(0.02)
        self.fail("no %s event" % name)

    def session(self, provider="claude", **usage):
        d = os.path.join(self.tmp, "sess-" + provider)
        os.makedirs(d, exist_ok=True)
        outcome._atomic_write(
            os.path.join(d, "outcome.json"),
            json.dumps(rec([seat(provider, turns=2, **usage)])))
        return d

    def test_get_stats_answers_at_once_and_delivers_an_event(self):
        self.session(cost_usd=0.5, input_tokens=10, cached_tokens=90)
        self.assertEqual(self.api.get_stats(), {"ok": True})
        payload = self.wait("stats")
        self.assertEqual(payload["sessions_counted"], 1)
        self.assertEqual(payload["providers"][0]["prompt_tokens"], 100)

    def test_get_playbook_answers_at_once_and_delivers_an_event(self):
        self.session()
        self.assertEqual(self.api.get_playbook(), {"ok": True})
        payload = self.wait("playbook")
        self.assertIn("summary", payload)
        self.assertIsInstance(payload["rules"], list)

    def test_get_playbook_actually_writes_the_file_the_planner_reads(self):
        # a tab that only displayed a stale copy would show Josh rules the
        # Supervisor is not using
        self.session()
        self.api.get_playbook()
        self.wait("playbook")
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, retro.PLAYBOOK_FILE)))

    def test_set_playbook_rule_refuses_an_id_it_cannot_find(self):
        self.session()
        self.api.get_playbook()
        self.wait("playbook")
        got = self.api.set_playbook_rule("nope", pinned=True)
        self.assertIn("error", got)

    def test_set_playbook_rule_round_trips_a_pin(self):
        retro.write_playbook(self.tmp, {
            "playbook_version": 1, "updated": "x",
            "heuristics": [{"heuristic_id": "a", "directive": "Do A",
                            "status": "active", "evidence_count": 1}]})
        got = self.api.set_playbook_rule("a", pinned=True)
        self.assertTrue(got["ok"])
        self.assertTrue(got["rules"][0]["pinned"])
        self.assertTrue(retro.read_playbook(self.tmp)["heuristics"][0]["pinned"])

    def test_a_broken_scan_reports_rather_than_raising(self):
        relay.SESSIONS_DIR = None          # a stats page is decoration
        self.api.get_stats()
        self.assertIn("error", self.wait("stats"))


# -------------------------------------------------------------------- UI --

@unittest.skipUnless(NODE, "node not installed")
class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.rep = test_ui_boot.boot(test_ui_boot.UI, cls._tmp.name)
        cls.p = cls.rep.get("stats") or {}
        cls.err = cls.rep.get("statsError")
        with open(os.path.join(ROOT, "ui", "index.html"),
                  encoding="utf-8") as f:
            cls.ui = f.read()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        if self.err:
            self.fail("stats probe threw: %s" % self.err)
        self.assertIsNone(self.rep.get("topLevelError"))

    def test_an_unreported_number_renders_as_a_blank_never_a_zero(self):
        self.assertEqual(self.p["geminiCells"],
                         ["1", "4", "—", "—", "—", "—", "—"])

    def test_a_reported_zero_still_renders_as_zero(self):
        self.assertEqual(self.p["zeroHit"], "0%")

    def test_a_reported_row_renders_every_column(self):
        self.assertEqual(self.p["claudeCells"],
                         ["2", "9", "$0.25", "100", "42", "90%", "1s"])

    def test_the_withheld_token_counts_are_stated_not_silent(self):
        self.assertTrue(self.p["caveatShown"])
        self.assertIn("token counts are left out", self.p["caveatText"])
        self.assertIn("half a billion", self.p["caveatText"])
        self.assertIn("turns and costs still count", self.p["caveatText"])

    def test_no_caveat_when_nothing_was_withheld(self):
        self.assertFalse(self.p["caveatAfterClean"])

    def test_the_playbook_lists_every_rule_including_dismissed_ones(self):
        self.assertEqual(self.p["bookRows"],
                         ["Do A", "Do B", "Old one"])
        self.assertEqual(self.p["bookDismissed"], [False, False, True])

    def test_a_dismissed_rule_offers_restore_not_dismiss(self):
        self.assertEqual(self.p["bookActions"],
                         [["Pin", "Dismiss"], ["Pinned", "Dismiss"],
                          ["Pin", "Restore"]])

    def test_clicking_pin_calls_the_bridge_with_that_rule(self):
        self.assertEqual(self.p["pinCall"], ["a", True, None])

    def test_clicking_dismiss_calls_the_bridge_with_that_rule(self):
        self.assertEqual(self.p["dismissCall"], ["a", None, True])

    def test_a_bridge_error_is_shown_rather_than_swallowed(self):
        self.assertEqual(self.p["noteAfterError"], "no such rule")

    def test_an_empty_playbook_says_why_rather_than_nothing(self):
        self.assertIn("derive themselves", self.p["bookEmpty"])

    def test_the_tabs_switch_panes(self):
        # [stPane.hidden, bookPane.hidden] after book, then after stats
        self.assertEqual(self.p["panes"],
                         [[True, False], [False, True]])

    def test_a_rule_directive_is_never_built_into_html(self):
        # derived from session data, which carries whatever a reason tag said
        body = self.ui.split("function renderPlaybook(")[1].split(
            "\nasync function ")[0]
        self.assertIn("dir.textContent", body)
        self.assertNotIn("book-directive\">' +", body)

    def test_the_modal_is_registered_in_all_three_places(self):
        # miss one and it is invisible, or Escape leaves it open
        self.assertIn("#deskModal, #brwsModal, #statsModal {", self.ui)
        self.assertIn("#brwsModal.show, #statsModal.show { display: flex; }",
                      self.ui)
        self.assertIn("closeHooks(); closeStats();", self.ui)


if __name__ == "__main__":
    unittest.main(verbosity=2)
