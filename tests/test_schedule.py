"""Wave 4 — scheduled rooms: the store, the clock, and the bridge.

`schedule.py` is stdlib-only and takes its path from the caller, so the store
half needs nothing but a temp directory. The bridge half drives the REAL
`app.Api` — a safety control needs a test where it is actually delivered, not
only where it is defined, which is the lesson W0.1 paid for and `test_permissions`
was rewritten around.

The rules this suite exists to keep fixed, in the order they would hurt:

  * A schedule over a room with standing access (Full permission, connected
    apps, unattended desktop or browser, an unbounded Keep Improving run)
    needs its OWN acknowledgement, and that acknowledgement is re-checked
    against the room AS IT IS AT FIRE TIME. Rooms are saved by NAME and
    overwriting one is documented behaviour, so an ack from March must not
    speak for a room that gained Full access in August.
  * A window Alloy was closed for is REPORTED, never fired late — and three
    days of downtime produce zero runs, not three.
  * The poller is started by main() and never by `Api.__init__`: twenty-nine
    suites in this repo build an `app.Api()`, and a constructor-started
    scheduler would poll Josh's real sessions/ folder inside every one of
    them and shell out to real CLIs.
  * The path is derived from `relay.SESSIONS_DIR` at CALL time (write_tabs).
  * `run_schedule_now` goes through the same gate as the timer. A second,
    friendlier code path is how a safety control acquires a way around it.

Run:  python tests/test_schedule.py
"""

import ast
import datetime
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import relay
import schedule as sched
from relay import Agent

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "ui", "index.html")
APP_SRC = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app.py")

NOW = datetime.datetime(2026, 8, 27, 9, 15, 0)          # a Thursday


def spec(**extra):
    out = {"room": "Nightly", "prompt": "do the thing", "kind": "daily",
           "at": "01:00"}
    out.update(extra)
    return out


# ============================================================== the policy ==

class GrantPolicyTests(unittest.TestCase):
    """Which room settings are STANDING grants, and what each one says."""

    def test_each_axis_produces_its_own_grant(self):
        self.assertEqual(sched.grants_for({"permission": "full"}),
                         ["permission_full"])
        self.assertEqual(sched.grants_for({"connectors": True}), ["connectors"])
        self.assertEqual(sched.grants_for({"desktop": "full"}),
                         ["desktop_full"])
        self.assertEqual(sched.grants_for({"browser": "full"}),
                         ["browser_full"])
        self.assertEqual(sched.grants_for({"continuous_unbounded": True}),
                         ["continuous_unbounded"])

    def test_the_desktop_allowlist_rung_is_a_standing_grant_too(self):
        """The plan named `desktop == "full"`. `allowlist` is the rung that
        clicks WITHOUT asking inside the apps Josh named, which on a nightly
        timer is the same promise with a smaller blast radius — not a
        different kind of promise."""
        self.assertEqual(sched.grants_for({"desktop": "allowlist"}),
                         ["desktop_allowlist"])

    def test_the_asking_rungs_are_not_grants(self):
        self.assertEqual(sched.grants_for(
            {"permission": "ask", "desktop": "ask", "browser": "ask"}), [])
        self.assertEqual(sched.grants_for({"permission": "auto"}), [])
        self.assertEqual(sched.grants_for({"browser": "read"}), [])

    def test_an_unknown_shape_claims_nothing(self):
        for junk in (None, [], "full", 7):
            self.assertEqual(sched.grants_for(junk), [])

    def test_grants_come_back_in_display_order(self):
        got = sched.grants_for({"connectors": True, "permission": "full",
                                "browser": "full"})
        self.assertEqual(got, ["permission_full", "connectors", "browser_full"])

    def test_every_grant_has_a_sentence(self):
        for key in sched.GRANT_ORDER:
            self.assertTrue(sched.GRANT_TEXT[key].strip(), key)
        self.assertEqual(len(sched.grant_sentences(sched.GRANT_ORDER)),
                         len(sched.GRANT_ORDER))

    def test_an_unknown_grant_key_is_dropped_not_shown_raw(self):
        self.assertEqual(sched.grant_sentences(["permission_full", "nope"]),
                         [sched.GRANT_TEXT["permission_full"]])

    def test_the_asking_rungs_earn_a_stated_note_instead(self):
        """A withholding is stated, not left as an absence (browser_mcp's
        WITHHELD rule, one surface over): at 01:00 an Ask rung is not a
        safeguard, it is a control that denies everything."""
        notes = " ".join(sched.unattended_notes(
            {"permission": "ask", "desktop": "ask", "browser": "ask"}))
        self.assertIn("Ask first", notes)
        self.assertIn("Desktop control is set to Ask", notes)
        self.assertIn("Browser control is set to Ask", notes)

    def test_a_round_capped_room_is_warned_about_an_unanswered_ask(self):
        """Measured, not guessed: relay.ask_abort gives an unanswered
        [[ASK]] a deadline in CONTINUOUS mode only."""
        # driven, not read off a docstring: a round-capped run gets the
        # caller's abort back UNCHANGED, so an unanswered question has no
        # deadline at all
        mine = lambda: False
        self.assertIs(relay.ask_abort({"continuous": {"on": False}}, mine),
                      mine)
        self.assertIsNot(
            relay.ask_abort({"continuous": {"on": True, "checkin":
                                            {"minutes": 5}}}, mine), mine)
        notes = sched.unattended_notes({"continuous": False})
        self.assertTrue(any("waits for an answer" in n for n in notes), notes)
        # ...and a Keep Improving room does NOT get that note, because there
        # the wait really does expire
        notes2 = sched.unattended_notes({"continuous": True})
        self.assertFalse(any("waits for an answer" in n for n in notes2),
                         notes2)

    def test_a_shell_rung_is_named_as_the_ceiling_on_the_other_ladders(self):
        """relay.advisory_rung_note's admission, at the surface where a rung
        is MOST likely to be read as the boundary. Full/auto permission
        already grants a shell, so "click only in the apps I allowlisted" is
        a description of what Alloy drives, not a fence around the seat."""
        note = " ".join(sched.unattended_notes(
            {"permission": "full", "desktop": "allowlist"}))
        self.assertIn("already gives every seat a shell", note)
        self.assertIn("command line", note)
        note = " ".join(sched.unattended_notes(
            {"permission": "auto", "browser": "full"}))
        self.assertIn("already gives every seat a shell", note)
        # ...and nothing to admit when neither ladder is even on
        quiet = " ".join(sched.unattended_notes(
            {"permission": "full", "desktop": "off", "browser": "off"}))
        self.assertNotIn("shell", quiet)

    def test_a_permission_checkin_is_named_as_a_wait(self):
        notes = sched.unattended_notes({"continuous": True,
                                        "checkin_action": "permission"})
        self.assertTrue(any("WAIT" in n for n in notes), notes)


# ================================================================ the clock ==

class ClockTests(unittest.TestCase):
    def test_hhmm_is_strict(self):
        self.assertEqual(sched.parse_hhmm("01:00"), (1, 0))
        self.assertEqual(sched.parse_hhmm(" 23:59 "), (23, 59))
        for junk in ("1:00", "25:00", "01:60", "01-00", "0100", "", None,
                     "aa:bb", "01:0", "1:0", "010:0"):
            self.assertIsNone(sched.parse_hhmm(junk), junk)

    def test_a_stamp_reads_back_with_or_without_seconds(self):
        self.assertEqual(sched.parse_dt("2026-09-01T01:00:00"),
                         datetime.datetime(2026, 9, 1, 1, 0))
        self.assertEqual(sched.parse_dt("2026-09-01 01:00"),
                         datetime.datetime(2026, 9, 1, 1, 0))
        for junk in ("", None, "tomorrow", "2026-13-01 01:00"):
            self.assertIsNone(sched.parse_dt(junk), junk)

    def test_daily_picks_today_when_the_time_is_still_ahead(self):
        rec = sched.normalize(spec(at="23:30"), now=NOW)
        self.assertEqual(rec["next_run"], "2026-08-27T23:30:00")

    def test_daily_rolls_to_tomorrow_when_the_time_has_passed(self):
        rec = sched.normalize(spec(at="01:00"), now=NOW)
        self.assertEqual(rec["next_run"], "2026-08-28T01:00:00")

    def test_daily_never_drifts_off_its_wall_clock_time(self):
        """Walked over 400 days, so a DST boundary is crossed twice. Every
        occurrence must read 01:00 on the local clock — the reason the next
        one is re-anchored with `_at_time` rather than computed by adding a
        day of seconds to the last."""
        rec = sched.normalize(spec(at="01:00"), now=NOW)
        cursor = sched.parse_dt(rec["next_run"])
        for _ in range(400):
            self.assertEqual((cursor.hour, cursor.minute), (1, 0))
            nxt = sched.next_occurrence(rec, cursor)
            self.assertEqual((nxt - cursor).days, 1)
            cursor = nxt

    def test_weekly_skips_today_once_its_time_has_gone(self):
        # NOW is a Thursday 09:15; Thursday=3
        rec = sched.normalize(spec(kind="weekly", at="01:00", days=[3]),
                              now=NOW)
        self.assertEqual(rec["next_run"], "2026-09-03T01:00:00")

    def test_weekly_takes_today_when_the_time_is_still_ahead(self):
        rec = sched.normalize(spec(kind="weekly", at="23:00", days=[3]),
                              now=NOW)
        self.assertEqual(rec["next_run"], "2026-08-27T23:00:00")

    def test_weekly_picks_the_soonest_of_several_days(self):
        rec = sched.normalize(spec(kind="weekly", at="01:00",
                                   days=[0, 3, 5]), now=NOW)
        # Thursday has passed 01:00, so Saturday (5) is next
        self.assertEqual(rec["next_run"], "2026-08-29T01:00:00")

    def test_interval_counts_from_now(self):
        rec = sched.normalize(spec(kind="interval", every_min=90), now=NOW)
        self.assertEqual(rec["next_run"], "2026-08-27T10:45:00")

    def test_once_refuses_a_time_that_has_passed(self):
        with self.assertRaises(ValueError):
            sched.normalize(spec(kind="once", start="2026-08-27 09:00"),
                            now=NOW)

    def test_describe_says_the_recurrence_in_words(self):
        self.assertEqual(
            sched.describe(sched.normalize(spec(at="01:00"), now=NOW)),
            "Every day at 01:00")
        self.assertEqual(
            sched.describe(sched.normalize(spec(kind="weekly", at="07:30",
                                                days=[0, 4]), now=NOW)),
            "Every Mon, Fri at 07:30")
        self.assertEqual(
            sched.describe(sched.normalize(spec(kind="interval",
                                                every_min=120), now=NOW)),
            "Every 2 hours")
        self.assertEqual(
            sched.describe(sched.normalize(spec(kind="interval",
                                                every_min=45), now=NOW)),
            "Every 45 minutes")
        self.assertTrue(sched.describe(
            sched.normalize(spec(kind="once", start="2026-09-01 01:00"),
                            now=NOW)).startswith("Once, at 2026-09-01"))


class ValidationTests(unittest.TestCase):
    def _refuses(self, **kw):
        with self.assertRaises(ValueError):
            sched.normalize(spec(**kw), now=NOW)

    def test_the_required_fields_are_refused_not_repaired(self):
        self._refuses(room="")
        self._refuses(prompt="   ")
        self._refuses(kind="cron")
        self._refuses(at="1am")
        self._refuses(kind="weekly", days=[])
        self._refuses(kind="weekly", days=[9])
        self._refuses(kind="interval", every_min=1)
        self._refuses(kind="interval", every_min=99999)
        self._refuses(kind="once", start="not a time")
        with self.assertRaises(ValueError):
            sched.normalize("nope", now=NOW)

    def test_the_name_falls_back_to_the_room(self):
        self.assertEqual(sched.normalize(spec(), now=NOW)["name"], "Nightly")
        self.assertEqual(sched.normalize(spec(name=" Late  work "),
                                         now=NOW)["name"], "Late work")

    def test_rounds_are_clamped_into_the_legal_range(self):
        self.assertEqual(sched.normalize(spec(turns=0), now=NOW)["turns"], 10)
        self.assertEqual(sched.normalize(spec(turns=-4), now=NOW)["turns"], 1)
        self.assertEqual(sched.normalize(spec(turns=10 ** 6), now=NOW)["turns"],
                         sched.TURNS_MAX)

    def test_a_long_prompt_is_refused_rather_than_truncated(self):
        with self.assertRaises(ValueError):
            sched.normalize(spec(prompt="x" * (sched.PROMPT_MAX + 1)), now=NOW)


# ================================================================ the store ==

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-sched-")
        self.path = os.path.join(self.tmp, "schedules.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_missing_or_corrupt_store_reads_as_empty(self):
        self.assertEqual(sched.read_schedules(self.path)["schedules"], [])
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(sched.read_schedules(self.path)["schedules"], [])
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        self.assertEqual(sched.read_schedules(self.path)["schedules"], [])
        self.assertEqual(sched.read_schedules(None)["schedules"], [])

    def test_one_bad_row_does_not_hide_the_good_ones(self):
        good = sched.save_schedule(self.path, spec(), now=NOW)
        raw = json.load(open(self.path, encoding="utf-8"))
        raw["schedules"].insert(0, "junk")
        raw["schedules"].append({"id": ""})
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        rows = sched.read_schedules(self.path)["schedules"]
        self.assertEqual([r["id"] for r in rows], [good["id"]])

    def test_the_reader_keeps_a_once_whose_time_has_passed(self):
        """`normalize` refuses a past `once` — correct for a NEW schedule and
        catastrophic for a reader, which would silently delete the record of
        the one that already ran."""
        rec = sched.save_schedule(self.path,
                                  spec(kind="once", start="2026-09-01 01:00"),
                                  now=NOW)
        raw = json.load(open(self.path, encoding="utf-8"))
        raw["schedules"][0]["start"] = "2020-01-01T00:00:00"
        raw["schedules"][0]["enabled"] = False
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        rows = sched.read_schedules(self.path)["schedules"]
        self.assertEqual([r["id"] for r in rows], [rec["id"]])
        self.assertEqual(rows[0]["start"], "2020-01-01T00:00:00")

    def test_save_replaces_by_id_and_delete_is_idempotent(self):
        a = sched.save_schedule(self.path, spec(name="A"), now=NOW)
        sched.save_schedule(self.path, spec(name="B", id=a["id"]), now=NOW)
        rows = sched.read_schedules(self.path)["schedules"]
        self.assertEqual([r["name"] for r in rows], ["B"])
        self.assertTrue(sched.delete_schedule(self.path, a["id"]))
        self.assertFalse(sched.delete_schedule(self.path, a["id"]))
        self.assertFalse(sched.delete_schedule(self.path, "nope"))

    def test_the_store_is_capped(self):
        for i in range(sched.SCHEDULES_MAX):
            sched.save_schedule(self.path, spec(name="s%d" % i), now=NOW)
        with self.assertRaises(ValueError):
            sched.save_schedule(self.path, spec(name="over"), now=NOW)

    def test_rows_sort_soonest_first_with_unarmed_ones_last(self):
        sched.save_schedule(self.path, spec(name="late", at="23:00"), now=NOW)
        sched.save_schedule(self.path, spec(name="early", at="10:00"), now=NOW)
        off = sched.save_schedule(self.path, spec(name="off", at="11:00"),
                                  now=NOW)
        sched.set_enabled(self.path, off["id"], False, now=NOW)
        # a paused row keeps its next_run, so pause alone does not reorder;
        # what must never happen is an EMPTY next_run sorting first
        raw = json.load(open(self.path, encoding="utf-8"))
        for row in raw["schedules"]:
            if row["name"] == "off":
                row["next_run"] = ""
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        sched.save_schedule(self.path, spec(name="mid", at="10:30"), now=NOW)
        names = [r["name"] for r in
                 sched.read_schedules(self.path)["schedules"]]
        self.assertEqual(names[-1], "off")
        self.assertEqual(names[:3], ["early", "mid", "late"])

    def test_re_arming_recomputes_the_window_from_now(self):
        """A schedule switched back on must not inherit a window that passed
        while it was off — that fires it on the very next poll."""
        rec = sched.save_schedule(self.path, spec(at="10:00"), now=NOW)
        self.assertEqual(rec["next_run"], "2026-08-27T10:00:00")
        sched.set_enabled(self.path, rec["id"], False, now=NOW)
        later = datetime.datetime(2026, 8, 29, 15, 0)
        back = sched.set_enabled(self.path, rec["id"], True, now=later)
        self.assertEqual(back["next_run"], "2026-08-30T10:00:00")
        self.assertIsNone(sched.set_enabled(self.path, "nope", True))

    # ---- the missed-window rule ------------------------------------------
    def test_a_window_inside_the_grace_still_fires(self):
        rec = sched.save_schedule(self.path, spec(at="10:00"), now=NOW)
        just_late = datetime.datetime(2026, 8, 27, 10, 5)
        self.assertEqual([r["id"] for r in
                          sched.due([rec], just_late)], [rec["id"]])
        verdict, note = sched.fire_verdict(rec, just_late)
        self.assertEqual(verdict, "run")
        self.assertEqual(note, "")

    def test_a_window_alloy_was_closed_for_is_reported_not_fired(self):
        rec = sched.save_schedule(self.path, spec(at="01:00"), now=NOW)
        breakfast = datetime.datetime(2026, 8, 28, 9, 15)
        verdict, note = sched.fire_verdict(rec, breakfast)
        self.assertEqual(verdict, "missed")
        self.assertIn("Missed 2026-08-28 01:00", note)

    def test_three_days_of_downtime_produce_zero_runs_not_three(self):
        """The cron-catchup trap. Both branches recompute the next window
        from NOW, so a gap is one decision, never a queue of them."""
        rec = sched.save_schedule(self.path, spec(at="01:00"), now=NOW)
        back = datetime.datetime(2026, 8, 31, 9, 15)
        claims = []
        for _ in range(5):
            rows = sched.read_schedules(self.path)["schedules"]
            for row in sched.due(rows, back):
                got, verdict, _note = sched.claim(self.path, row["id"],
                                                  row["next_run"], back)
                if got:
                    claims.append(verdict)
        self.assertEqual(claims, ["missed"])
        after = sched.read_schedules(self.path)["schedules"][0]
        self.assertEqual(after["next_run"], "2026-09-01T01:00:00")
        self.assertEqual(after["misses"], 1)

    def test_claim_advances_the_record_before_anything_runs(self):
        """`run_checkin`'s rule. A fire recorded afterwards repeats at every
        poll for as long as the conversation lasts."""
        rec = sched.save_schedule(self.path, spec(at="10:00"), now=NOW)
        at = datetime.datetime(2026, 8, 27, 10, 0, 30)
        got, verdict, _ = sched.claim(self.path, rec["id"], rec["next_run"], at)
        self.assertEqual(verdict, "run")
        self.assertEqual(got["next_run"], "2026-08-28T10:00:00")
        on_disk = sched.read_schedules(self.path)["schedules"][0]
        self.assertEqual(on_disk["next_run"], "2026-08-28T10:00:00")
        self.assertEqual(sched.due([on_disk], at), [])

    def test_claim_is_a_compare_and_set(self):
        rec = sched.save_schedule(self.path, spec(at="10:00"), now=NOW)
        at = datetime.datetime(2026, 8, 27, 10, 0, 30)
        first, _, _ = sched.claim(self.path, rec["id"], rec["next_run"], at)
        self.assertIsNotNone(first)
        second, _, _ = sched.claim(self.path, rec["id"], rec["next_run"], at)
        self.assertIsNone(second, "the same window was claimed twice")

    def test_a_paused_or_unknown_schedule_is_never_claimed(self):
        rec = sched.save_schedule(self.path, spec(at="10:00"), now=NOW)
        sched.set_enabled(self.path, rec["id"], False, now=NOW)
        at = datetime.datetime(2026, 8, 27, 10, 0, 30)
        self.assertIsNone(sched.claim(self.path, rec["id"],
                                      rec["next_run"], at)[0])
        self.assertIsNone(sched.claim(self.path, "nope", "x", at)[0])
        self.assertEqual(sched.due(
            sched.read_schedules(self.path)["schedules"], at), [])

    def test_a_once_disarms_itself_when_it_has_fired(self):
        rec = sched.save_schedule(self.path,
                                  spec(kind="once", start="2026-08-27 10:00"),
                                  now=NOW)
        at = datetime.datetime(2026, 8, 27, 10, 0, 5)
        got, verdict, _ = sched.claim(self.path, rec["id"], rec["next_run"], at)
        self.assertEqual(verdict, "run")
        self.assertEqual(got["next_run"], "")
        self.assertFalse(got["enabled"],
                         "a spent `once` stayed armed, promising a run that "
                         "can never come")

    def test_record_result_counts_only_real_runs(self):
        rec = sched.save_schedule(self.path, spec(), now=NOW)
        sched.record_result(self.path, rec["id"], "Skipped — busy.", ran=False)
        row = sched.read_schedules(self.path)["schedules"][0]
        self.assertEqual(row["runs"], 0)
        self.assertIn("Skipped", row["last_result"])
        sched.record_result(self.path, rec["id"], "Started Nightly.", ran=True)
        row = sched.read_schedules(self.path)["schedules"][0]
        self.assertEqual(row["runs"], 1)
        self.assertIsNone(sched.record_result(self.path, "nope", "x"))

    def test_two_threads_writing_the_store_lose_nothing(self):
        made = []

        def work(i):
            made.append(sched.save_schedule(self.path, spec(name="s%d" % i),
                                            now=NOW)["id"])

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        rows = sched.read_schedules(self.path)["schedules"]
        self.assertEqual(len(rows), 8, [r["name"] for r in rows])
        self.assertEqual(sorted(r["id"] for r in rows), sorted(made))


class AckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-sched-ack-")
        self.path = os.path.join(self.tmp, "schedules.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_risky_room_cannot_be_scheduled_without_an_acknowledgement(self):
        with self.assertRaises(ValueError) as cm:
            sched.save_schedule(self.path, spec(),
                                grants=["permission_full"], now=NOW)
        self.assertIn("Full access", str(cm.exception))

    def test_the_acknowledgement_lets_it_through(self):
        rec = sched.save_schedule(
            self.path, spec(ack={"grants": ["permission_full"]}),
            grants=["permission_full"], now=NOW)
        self.assertEqual(rec["ack"]["grants"], ["permission_full"])
        self.assertTrue(rec["ack"]["at"])

    def test_an_acknowledgement_for_a_different_grant_does_not_transfer(self):
        with self.assertRaises(ValueError):
            sched.save_schedule(self.path, spec(ack={"grants": ["connectors"]}),
                                grants=["permission_full"], now=NOW)

    def test_narrowing_the_room_is_silent_and_widening_is_not(self):
        rec = sched.normalize(spec(ack={"grants": ["permission_full",
                                                   "connectors"]}), now=NOW)
        self.assertEqual(sched.ack_gap(rec, ["connectors"]), [])
        self.assertEqual(sched.ack_gap(rec, []), [])
        self.assertEqual(sched.ack_gap(rec, ["desktop_full"]), ["desktop_full"])

    def test_a_schedule_with_no_ack_at_all_reports_every_grant(self):
        rec = sched.normalize(spec(), now=NOW)
        self.assertIsNone(rec["ack"])
        self.assertEqual(sched.ack_gap(rec, ["connectors", "browser_full"]),
                         ["connectors", "browser_full"])
        self.assertEqual(sched.ack_gap(None, ["connectors"]), ["connectors"])

    def test_an_edit_keeps_what_already_happened(self):
        """The caller sends a FORM, not a record. Without this, changing
        the time of a nightly job silently reported it as having never
        run -- measured before the fix: runs 1 -> 0, last_result wiped."""
        rec = sched.save_schedule(self.path, spec(), now=NOW)
        sched.record_result(self.path, rec["id"], "Started Nightly.",
                            ran=True)
        sched.save_schedule(self.path, spec(id=rec["id"], at="02:00"),
                            now=NOW)
        row = sched.read_schedules(self.path)["schedules"][0]
        self.assertEqual(row["at"], "02:00")
        self.assertEqual(row["runs"], 1)
        self.assertEqual(row["last_result"], "Started Nightly.")
        self.assertTrue(row["last_run"])
        self.assertEqual(row["created"], rec["created"])

    def test_re_arming_something_with_nothing_left_stays_off(self):
        """A spent `once` switched back on used to report `enabled: true`
        with an empty next_run -- a row promising a run that can never
        come, which is exactly what `claim` disarms it to avoid."""
        rec = sched.save_schedule(self.path,
                                  spec(kind="once", start="2026-08-27 10:00"),
                                  now=NOW)
        at = datetime.datetime(2026, 8, 27, 10, 0, 5)
        sched.claim(self.path, rec["id"], rec["next_run"], at)
        back = sched.set_enabled(self.path, rec["id"], True, now=at)
        self.assertFalse(back["enabled"])
        self.assertEqual(back["next_run"], "")
        self.assertIn("Nothing left to run", back["last_result"])

    def test_an_unknown_grant_in_a_stored_ack_is_dropped(self):
        rec = sched.normalize(spec(ack={"grants": ["permission_full", "moon"]}),
                              now=NOW)
        self.assertEqual(rec["ack"]["grants"], ["permission_full"])


# ================================================================ the bridge ==

class FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)

    def events(self):
        return [json.loads(s[len("uiEvent("):-1]) for s in self.calls]

    def payloads(self, name):
        return [e["payload"] for e in self.events() if e["event"] == name]


def gated_agent_class(name_, gate, reply="ok"):
    class Gated(Agent):
        name = name_
        cli = "fake"

        def turn(self, message, on_activity=None):
            self.session_id = "fake-session-%s" % self.uid
            gate.wait(10)
            return reply

    return Gated


def scripted_agent_class(name_, replies):
    replies = list(replies)

    class Scripted(Agent):
        name = name_
        cli = "fake"

        def turn(self, message, on_activity=None):
            self.session_id = "fake-session-%s" % self.uid
            return replies.pop(0) if replies else "…"

    return Scripted


ROOM_SEATS = [{"id": 0, "provider": "claude", "enabled": True}]


class BridgeTests(unittest.TestCase):
    """The REAL app.Api. A control that decides what runs unattended at 01:00
    needs a test where it is delivered, not only where it is defined."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-sched-bridge-")
        self._old_app = app.SESSIONS_DIR
        self._old = (relay.SESSIONS_DIR, relay.TABS_FILE, relay.ROOMS_FILE,
                     relay.MEMORY_DIR)
        app.SESSIONS_DIR = self.tmp
        relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        relay.ROOMS_FILE = os.path.join(self.tmp, "rooms.json")
        relay.MEMORY_DIR = os.path.join(self.tmp, "memory")
        self._old_types = dict(relay.AGENT_TYPES)
        self.gates = []

    def tearDown(self):
        for g in self.gates:
            g.set()
        app.SESSIONS_DIR = self._old_app
        (relay.SESSIONS_DIR, relay.TABS_FILE, relay.ROOMS_FILE,
         relay.MEMORY_DIR) = self._old
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------
    def _api(self):
        api = app.Api()
        api._window = FakeWindow()
        return api

    def _gate(self):
        g = threading.Event()
        self.gates.append(g)
        return g

    def _wait(self, pred, what, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.01)
        self.fail("timed out waiting for %s" % what)

    def _room(self, name="Nightly", **cfg):
        base = {"seats": [dict(s) for s in ROOM_SEATS], "permission": "ask",
                "connectors": False, "desktop": "off", "browser": "off"}
        base.update(cfg)
        relay.save_room(name, base)
        return base

    def _save(self, api, **extra):
        return api.save_schedule(spec(**extra))

    # ---- paths and construction -----------------------------------------
    def test_the_path_follows_relay_sessions_dir_at_call_time(self):
        """write_tabs' lesson: a module constant captured at import survives
        a test's redirect and writes into Josh's real sessions/ folder."""
        self.assertEqual(app._schedule_path(),
                         os.path.join(self.tmp, "schedules.json"))
        moved = os.path.join(self.tmp, "elsewhere")
        relay.SESSIONS_DIR = moved
        try:
            self.assertEqual(app._schedule_path(),
                             os.path.join(moved, "schedules.json"))
        finally:
            relay.SESSIONS_DIR = self.tmp

    def test_the_poller_is_not_started_by_the_constructor(self):
        """Twenty-nine suites in this repo build an app.Api(). A scheduler
        started here would poll Josh's real sessions/ inside every one of
        them and shell out to real CLIs."""
        api = self._api()
        self.assertIsNone(api._sched_thread)
        self.assertFalse(api._sched_stop.is_set())
        with open(APP_SRC, encoding="utf-8") as f:
            src = f.read()
        init = src.split("class Api", 1)[1].split("def __init__", 1)[1]
        init = init.split("# ---- focused-run views", 1)[0]
        # comments STRIPPED: this body explains in prose why the poller is
        # not started here, and a substring match cannot tell a statement
        # from a mention (the wrap-token bug's family)
        code = chr(10).join(l for l in init.splitlines()
                            if not l.strip().startswith("#"))
        self.assertNotIn("start_scheduler", code,
                         "the constructor starts the scheduler")
        self.assertIn("api.start_scheduler()", src,
                      "main() does not start it either")

    def test_start_and_stop_scheduler_are_idempotent(self):
        api = self._api()
        self.assertTrue(api.start_scheduler())
        try:
            self.assertFalse(api.start_scheduler(), "a second poller started")
            self.assertTrue(api._sched_thread.is_alive())
        finally:
            api.stop_scheduler()
        api._sched_thread.join(5)
        self.assertFalse(api._sched_thread.is_alive())

    # ---- normalization lives in relay ------------------------------------
    def test_the_legacy_yolo_spelling_is_resolved_by_relay(self):
        """The 2026-08-26 bug one surface over: the compatibility key must be
        resolved by the READER. A room saved by an older UI carries
        `yolo: true` and no `permission`, and reading it as "not full" would
        make a nightly Full-access run look harmless."""
        axes = app.Api._room_axes({"yolo": True})
        self.assertEqual(axes["permission"], "full")
        self.assertIn("permission_full", sched.grants_for(axes))

    def test_a_garbage_axis_reads_as_off(self):
        axes = app.Api._room_axes({"desktop": "FULL-ish", "browser": "yes"})
        self.assertEqual(axes["desktop"], "off")
        self.assertEqual(axes["browser"], "off")
        self.assertEqual(sched.grants_for(axes), [])

    def test_an_unbounded_keep_improving_room_is_a_grant(self):
        cont = {"on": True, "limits": {"spend_usd": None, "hours": None,
                                       "watchdog_may_stop": False}}
        axes = app.Api._room_axes({"continuous": cont})
        self.assertTrue(axes["continuous_unbounded"])
        self.assertIn("continuous_unbounded", sched.grants_for(axes))
        # one real limit and it stops being unbounded
        cont["limits"]["hours"] = 2
        self.assertFalse(app.Api._room_axes({"continuous": cont})
                         ["continuous_unbounded"])

    def test_a_missing_room_answers_none_never_no_grants(self):
        api = self._api()
        self.assertIsNone(api._room_grants("Ghost"))
        self.assertIn("error", api.room_risk("Ghost"))

    def test_room_risk_reports_grants_and_dead_controls(self):
        api = self._api()
        self._room("Loud", permission="full", connectors=True, desktop="ask")
        got = api.room_risk("Loud")
        self.assertEqual(got["grants"], ["permission_full", "connectors"])
        self.assertEqual(len(got["sentences"]), 2)
        self.assertTrue(any("Desktop control is set to Ask" in n
                            for n in got["notes"]), got["notes"])

    # ---- save / list -----------------------------------------------------
    def test_saving_needs_a_real_room(self):
        api = self._api()
        self.assertIn("error", self._save(api))
        self._room()
        self.assertTrue(self._save(api).get("ok"))

    def test_saving_a_risky_room_is_refused_without_the_acknowledgement(self):
        api = self._api()
        self._room(permission="full")
        got = self._save(api)
        self.assertIn("error", got)
        self.assertIn("Full access", got["error"])
        self.assertEqual(api.get_schedules()["schedules"], [])
        ok = self._save(api, ack={"grants": ["permission_full"]})
        self.assertTrue(ok.get("ok"), ok)

    def test_the_list_judges_every_row_against_the_room_as_it_is_now(self):
        """The trap the plan does not mention: rooms are saved by NAME and
        overwriting one is documented behaviour, so an acknowledged schedule
        can wake up pointing at a room that has since gained Full access."""
        api = self._api()
        self._room()
        self.assertTrue(self._save(api).get("ok"))
        row = api.get_schedules()["schedules"][0]
        self.assertEqual(row["grants"], [])
        self.assertEqual(row["ack_gap"], [])
        self.assertFalse(row["missing_room"])
        self._room(permission="full", connectors=True)      # overwritten
        row = api.get_schedules()["schedules"][0]
        self.assertEqual(row["grants"], ["permission_full", "connectors"])
        self.assertEqual(row["ack_gap"], ["permission_full", "connectors"])
        self.assertEqual(len(row["ack_sentences"]), 2)

    def test_a_deleted_room_is_named_as_missing_not_as_harmless(self):
        api = self._api()
        self._room()
        self._save(api)
        relay.delete_room("Nightly")
        row = api.get_schedules()["schedules"][0]
        self.assertTrue(row["missing_room"])
        self.assertEqual(row["grants"], [])
        self.assertEqual(row["ack_gap"], [])

    def test_the_list_carries_the_rooms_and_the_poll_interval(self):
        api = self._api()
        self._room("A")
        self._room("B")
        got = api.get_schedules()
        self.assertEqual(sorted(got["rooms"]), ["A", "B"])
        self.assertEqual(got["poll_seconds"], app.SCHEDULE_POLL_S)

    def test_enable_and_delete_answer_a_bad_id(self):
        api = self._api()
        self.assertIn("error", api.set_schedule_enabled("nope", True))
        self.assertIn("error", api.set_schedule_enabled("nope", "yes"))
        self.assertIn("error", api.delete_schedule("nope"))
        self._room()
        rec = self._save(api)["schedule"]
        self.assertTrue(api.set_schedule_enabled(rec["id"], False)["ok"])
        self.assertFalse(api.get_schedules()["schedules"][0]["enabled"])
        self.assertTrue(api.delete_schedule(rec["id"])["ok"])

    # ---- firing ----------------------------------------------------------
    def _armed(self, api, **room):
        self._room(**room)
        grants = api._room_grants("Nightly")
        ack = {"grants": grants} if grants else None
        # NOT the default 10: `turns` has to be shown travelling, and
        # both sides default to 10, so asserting 10 proves nothing
        got = self._save(api, ack=ack, turns=6)
        self.assertTrue(got.get("ok"), got)
        return got["schedule"]

    def test_a_fire_refuses_when_the_room_widened_since_it_was_armed(self):
        """The safety test this whole wave turns on. Save time is not enough:
        the room can change afterwards and the schedule is never touched."""
        api = self._api()
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["one"])
        rec = self._armed(api)
        self._room(permission="full")               # overwritten afterwards
        ok, text = api._launch_schedule(rec)
        self.assertFalse(ok)
        self.assertIn("never acknowledged", text)
        self.assertEqual(api._runs.live(), [])

    def test_a_fire_refuses_when_the_room_is_gone(self):
        api = self._api()
        rec = self._armed(api)
        relay.delete_room("Nightly")
        ok, text = api._launch_schedule(rec)
        self.assertFalse(ok)
        self.assertIn("no longer exists", text)

    def test_a_fire_is_skipped_while_another_conversation_is_running(self):
        """The webhook RAISES here because a script is waiting for an answer.
        A schedule has nobody to answer to, so it SKIPS — and says so."""
        gate = self._gate()
        relay.AGENT_TYPES["claude"] = gated_agent_class("Claude", gate)
        api = self._api()
        rec = self._armed(api)
        api.start({"opener": "hi", "turns": 1,
                   "seats": [dict(s) for s in ROOM_SEATS]})
        self._wait(lambda: api._runs.live(), "the first chat to look live")
        ok, text = api._launch_schedule(rec)
        self.assertFalse(ok)
        self.assertIn("Skipped", text)
        gate.set()

    def test_a_fire_takes_a_background_run_and_leaves_the_focus_alone(self):
        gate = self._gate()
        relay.AGENT_TYPES["claude"] = gated_agent_class("Claude", gate)
        api = self._api()
        draft = api._runs.focused()
        rec = self._armed(api)
        ok, text = api._launch_schedule(rec)
        self.assertTrue(ok, text)
        self._wait(lambda: api._runs.live(), "the scheduled run")
        run = api._runs.live()[0]
        self.assertTrue(run.background, "a scheduled run was not background")
        self.assertIsNot(run, draft, "the schedule borrowed Josh's draft")
        self.assertIsNotNone(run.thread, "spawn() did not pin the thread")
        self._wait(lambda: run.id is not None, "the scheduled chat's id")
        self.assertIs(api._runs.focused(), draft,
                      "a scheduled run took the focus")
        gate.set()
        run.thread.join(10)

    def test_the_conversation_gets_the_prompt_and_the_rounds(self):
        api = self._api()
        seen = {}
        api._runs.spawn = lambda target, args=(), run=None: seen.update(
            cfg=args[0], run=args[1]) or args[1]
        rec = self._armed(api)
        api._launch_schedule(rec)
        self.assertEqual(seen["cfg"]["opener"], "do the thing")
        self.assertEqual(rec["turns"], 6, "the fixture stopped being distinct")
        self.assertEqual(seen["cfg"]["turns"], 6)
        self.assertEqual(seen["cfg"]["seats"][0]["provider"], "claude")
        self.assertEqual(seen["cfg"]["scheduled"]["name"], "Nightly")
        self.assertEqual(seen["cfg"]["scheduled"]["when"], "Every day at 01:00")
        self.assertFalse(seen["cfg"]["scheduled"]["manual"])

    def test_a_scheduled_run_says_so_in_its_own_transcript(self):
        """Josh reads these hours later with no memory of arming them, and
        `background` says nobody was watching — not what decided it."""
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["one"])
        api = self._api()
        run = api._runs.background()
        api._conversation({"opener": "hi", "turns": 1,
                           "seats": [dict(s) for s in ROOM_SEATS],
                           "scheduled": {"id": "s1", "name": "Nightly",
                                         "room": "Nightly",
                                         "when": "Every day at 01:00",
                                         "manual": False}}, run)
        api._emit_q.join()
        rows = []
        with open(os.path.join(run.session_dir, "messages.jsonl"),
                  encoding="utf-8") as f:
            for line in f:
                rows.append(json.loads(line))
        said = [r for r in rows
                if "Started by the schedule" in (r.get("text") or "")]
        self.assertEqual(len(said), 1, [r.get("text") for r in rows])
        self.assertIn("Every day at 01:00", said[0]["text"])

    # ---- the tick --------------------------------------------------------
    def test_a_tick_fires_what_is_due_and_records_it(self):
        gate = self._gate()
        relay.AGENT_TYPES["claude"] = gated_agent_class("Claude", gate)
        api = self._api()
        rec = self._armed(api)
        when = sched.parse_dt(rec["next_run"]) + datetime.timedelta(seconds=30)
        fired = api._scheduler_tick(now=when)
        self.assertEqual([f[1] for f in fired], [True], fired)
        row = api.get_schedules()["schedules"][0]
        self.assertEqual(row["runs"], 1)
        self.assertIn("Started Nightly", row["last_result"])
        self.assertNotEqual(row["next_run"], rec["next_run"])
        gate.set()

    def test_a_tick_never_starts_a_window_alloy_was_closed_for(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["one"])
        api = self._api()
        rec = self._armed(api)
        late = sched.parse_dt(rec["next_run"]) + datetime.timedelta(hours=8)
        self.assertEqual(api._scheduler_tick(now=late), [])
        self.assertEqual(api._runs.live(), [])
        row = api.get_schedules()["schedules"][0]
        self.assertEqual(row["runs"], 0)
        self.assertEqual(row["misses"], 1)
        self.assertIn("Missed", row["last_result"])
        api._emit_q.join()
        said = api._window.payloads("scheduled")
        self.assertEqual(len(said), 1)
        self.assertFalse(said[0]["started"])
        self.assertIn("Missed", said[0]["text"])

    def test_a_tick_with_nothing_due_does_nothing(self):
        api = self._api()
        rec = self._armed(api)
        early = sched.parse_dt(rec["next_run"]) - datetime.timedelta(hours=1)
        self.assertEqual(api._scheduler_tick(now=early), [])
        api._emit_q.join()
        self.assertEqual(api._window.payloads("scheduled"), [])

    def test_a_refused_fire_is_recorded_and_announced(self):
        api = self._api()
        rec = self._armed(api)
        self._room(permission="full")
        when = sched.parse_dt(rec["next_run"]) + datetime.timedelta(seconds=30)
        fired = api._scheduler_tick(now=when)
        self.assertEqual([f[1] for f in fired], [False])
        row = api.get_schedules()["schedules"][0]
        self.assertEqual(row["runs"], 0)
        self.assertIn("never acknowledged", row["last_result"])
        api._emit_q.join()
        said = api._window.payloads("scheduled")
        self.assertEqual(len(said), 1)
        self.assertFalse(said[0]["started"])

    def test_the_scheduled_event_names_no_chat(self):
        """It has no chat_id on purpose: the app is reporting on a timer, and
        stamping it with the focused chat would paint it into whatever Josh
        has open (the _emit_for lesson)."""
        api = self._api()
        api._announce_schedule({"id": "s", "name": "N", "room": "R"}, False,
                               "Skipped — busy.")
        api._emit_q.join()
        ev = api._window.events()[-1]
        self.assertEqual(ev["event"], "scheduled")
        self.assertNotIn("chat_id", ev["payload"])
        self.assertIn("Skipped", ev["payload"]["text"])

    # ---- run now ---------------------------------------------------------
    def test_run_now_goes_through_the_same_gate(self):
        """A second, friendlier code path is how a safety control acquires a
        way around itself."""
        api = self._api()
        rec = self._armed(api)
        self._room(permission="full")
        got = api.run_schedule_now(rec["id"])
        self.assertTrue(got["ok"])
        self.assertFalse(got["started"])
        self.assertIn("never acknowledged", got["text"])
        self.assertIn("error", api.run_schedule_now("nope"))

    def test_run_now_does_not_use_up_the_next_window(self):
        gate = self._gate()
        relay.AGENT_TYPES["claude"] = gated_agent_class("Claude", gate)
        api = self._api()
        rec = self._armed(api)
        got = api.run_schedule_now(rec["id"])
        self.assertTrue(got["started"], got)
        row = api.get_schedules()["schedules"][0]
        self.assertEqual(row["next_run"], rec["next_run"],
                         "a manual run consumed the scheduled window")
        self.assertEqual(row["runs"], 1)
        self.assertIn("run now", row["last_result"])
        gate.set()

    def test_run_now_does_not_consume_a_window_that_is_already_due(self):
        """The sharp half of the rule. A run-now on a schedule that is NOT
        yet due leaves the clock alone for free (its next occurrence is the
        same one either way), so only a DUE schedule can tell a real
        hands-off implementation from one that quietly claims the window."""
        gate = self._gate()
        relay.AGENT_TYPES["claude"] = gated_agent_class("Claude", gate)
        api = self._api()
        rec = self._armed(api)
        # backdate the window by hand: the bridge has no clock to inject
        path = app._schedule_path()
        raw = json.load(open(path, encoding="utf-8"))
        due_at = (datetime.datetime.now()
                  - datetime.timedelta(minutes=1)).replace(
                      microsecond=0).isoformat()
        raw["schedules"][0]["next_run"] = due_at
        with open(path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        got = api.run_schedule_now(rec["id"])
        self.assertTrue(got["started"], got)
        row = api.get_schedules()["schedules"][0]
        self.assertEqual(row["next_run"], due_at,
                         "Run now consumed the window the timer still owes")
        self.assertEqual(row["misses"], 0)
        gate.set()

    def test_a_launch_that_blows_up_leaves_a_sentence_not_starting(self):
        """`claim` advances the window FIRST, so a launch that raises has
        already spent the night: the record must not sit on "starting…"
        until the next occurrence."""
        api = self._api()
        rec = self._armed(api)

        def boom(_rec, manual=False):
            raise RuntimeError("disk on fire")

        api._launch_schedule = boom
        when = sched.parse_dt(rec["next_run"]) + datetime.timedelta(seconds=30)
        fired = api._scheduler_tick(now=when)
        self.assertEqual([f[1] for f in fired], [False])
        row = api.get_schedules()["schedules"][0]
        self.assertEqual(row["runs"], 0)
        self.assertNotIn("starting", row["last_result"])
        self.assertIn("Could not start", row["last_result"])

    def test_run_now_announces_itself_like_the_timer_does(self):
        """Same event, so a hook sees every fire attempt -- and `manual`
        tells it which kind this was."""
        api = self._api()
        rec = self._armed(api)
        self._room(permission="full")          # refused, so no CLI turn
        api.run_schedule_now(rec["id"])
        api._emit_q.join()
        said = api._window.payloads("scheduled")
        self.assertEqual(len(said), 1, said)
        self.assertFalse(said[0]["started"])
        self.assertTrue(said[0]["manual"])
        self.assertNotIn("chat_id", said[0])

    # ---- the hook event --------------------------------------------------
    def test_scheduled_is_a_known_hook_event(self):
        self.assertIn("scheduled", relay.HOOK_EVENTS)
        relay.write_event_hooks({"scheduled": "echo hi"})
        self.assertEqual(relay.read_event_hooks()["hooks"],
                         {"scheduled": "echo hi"})

    def test_the_hook_fires_for_a_skip_as_well_as_a_start(self):
        api = self._api()
        relay.write_event_hooks({"scheduled": "echo hi"})
        api._hooks_cache = None
        seen = []
        api._hook_worker = lambda name, cmd, env: seen.append((name, env))
        for started in (True, False):
            worker = api.run_event_hook(
                "scheduled", {"name": "N", "started": started,
                              "text": "N — Skipped." if not started
                                      else "N — Started."})
            if worker is not None:
                worker.join(5)
        self.assertEqual([n for n, _ in seen], ["scheduled", "scheduled"])
        self.assertIn("Started", seen[0][1]["AICHAT_DETAIL"])
        self.assertIn("Skipped", seen[1][1]["AICHAT_DETAIL"])


    def test_the_scheduled_hook_never_borrows_the_chat_on_screen(self):
        """W2.0's lesson one event over. A `scheduled` payload carries no
        chat_id BY DESIGN, and the generic fallback then handed the script
        whatever conversation Josh happened to be reading -- measured:
        AICHAT_SESSION="some-other-chat". A hook acting on that would act on
        the wrong chat, which is worse than acting on none."""
        api = self._api()
        relay.write_event_hooks({"scheduled": "echo hi", "done": "echo hi"})
        api._hooks_cache = None
        api._runs.focus("some-other-chat")
        api._runs.focused().state = {"title": "the chat Josh is reading"}
        seen = []
        api._hook_worker = lambda name, cmd, env: seen.append(env)
        api.run_event_hook("scheduled", {"name": "Nightly", "room": "R",
                                         "started": False, "text": "N - x."})
        self.assertEqual(seen[-1]["AICHAT_SESSION"], "")
        self.assertEqual(seen[-1]["AICHAT_TITLE"], "Nightly")
        # ...while an ordinary conversation event still falls back, as before
        api.run_event_hook("done", {"text": "finished"})
        self.assertEqual(seen[-1]["AICHAT_SESSION"], "some-other-chat")

class SourceGuardTests(unittest.TestCase):
    """Three edits, one row: the trap the plan named, pinned."""

    def test_every_hook_event_has_a_ui_label(self):
        with open(UI, encoding="utf-8") as f:
            src = f.read()
        table = src.split("const hookLabels = {", 1)[1].split("};", 1)[0]
        for name in relay.HOOK_EVENTS:
            self.assertIn(name, table,
                          "%s has no hookLabels entry — its row renders from "
                          "the bridge's `events` list and would save as its "
                          "raw name" % name)

    def test_hook_save_iterates_what_was_rendered(self):
        """The fourth place to keep in sync, removed. Save used to walk
        `hookLabels`, so an event Python knew about and the table did not
        rendered a row and was silently dropped."""
        with open(UI, encoding="utf-8") as f:
            src = f.read()
        # the handler ends with a `};` at column 0 -- splitting on a bare
        # "};" stops at `const hooks = {};` on its FIRST line
        save = src.split('$("hookSave").onclick', 1)[1]
        save = save.split(chr(10) + "};", 1)[0]
        self.assertIn("for (const ev of hookRowIds)", save)
        self.assertNotIn("Object.keys(hookLabels)", save)

    def test_the_schedule_module_is_standalone(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "schedule.py")
        # PARSED, not grepped. browser_mcp's own standalone guard had to be
        # line-anchored because `"from relay" in src` matched the module
        # docstring's own promise that it imports nothing from relay -- and
        # even column-0 anchoring is not enough here, because a wrapped
        # docstring line legitimately BEGINS "from ``relay...". The AST can
        # tell a statement from a mention; a substring match never can.
        tree = ast.parse(open(path, encoding="utf-8").read())
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        self.assertTrue(names)
        for name in names:
            self.assertNotIn(name.split(".")[0],
                             ("relay", "app", "workstreams", "browser_mcp"),
                             "schedule.py imported %s" % name)

    def test_the_module_owns_no_root_directory(self):
        """relay owns SESSIONS_DIR; a second default here is how two halves
        of an app come to disagree about where the data lives (fork.py)."""
        self.assertEqual(sched.SCHEDULE_FILE, "schedules.json")
        self.assertNotIn(os.sep, sched.SCHEDULE_FILE)
        self.assertFalse([n for n in dir(sched)
                          if n.endswith("_DIR") or n.endswith("_ROOT")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
