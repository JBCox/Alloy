"""The morning report — report.build, its persistence, and its bridge.

Token-free and subprocess-free: report.py reads one already-loaded meta dict
and nothing else, so every test here is a dict in and a dict out, plus a
BRIDGE section driving the real `app.Api.session_report` (test_permissions'
lesson: a reader shipped correct at the engine and wrong at the bridge is the
default outcome, not the unlucky one).

Most of these tests exist because the defect they pin was found by running
the module against the REAL sessions/ folder before any test was written,
and every one of those defects was an OVER-COUNT — a number that looked
plausible, contradicted nothing on screen, and was wrong:

  * `wave_gate` stamps its commit-binding entry `status="passed"`, so
    counting gate outcomes by status counted one extra pass per checkpointed
    wave — drifting further from the truth the better the run was doing.
  * `health_check` and `objective_set` are emitted 2-3 times per event from
    8 and 4 call sites, so counting entries counts occurrences of neither.
  * `supervisor_wave_index` is a CURSOR that `plan_workstreams` resets to 1,
    and the trace's own `wave` field inherits it — measured on a real
    two-hour run that dispatched at least six plans and reports index 1.
  * `created`→`updated` measures the CHAT, not the run: 72,949s for a
    session worked on across two days.
  * `relay.supervisor_status` returns a DICT, and stringifying it made every
    card render an empty status.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay          # noqa: E402
import report         # noqa: E402


def trace(*rows):
    """A control log. Positional shorthand keeps the tests readable."""
    out = []
    for i, row in enumerate(rows):
        entry = {"id": "e%d" % i, "ts": "2026-09-01T0%d:00:00" % min(9, i),
                 "wave": 1, "phase": "x", "title": "t", "detail": ""}
        entry.update(row)
        out.append(entry)
    return out


def meta(**kw):
    base = {"id": "chat", "title": "A chat", "mode": "supervisor",
            "created": "2026-09-01T01:00:00", "updated": "2026-09-01T04:00:00",
            "supervisor_trace": trace({"type": "plan_started"})}
    base.update(kw)
    return base


def r_keys(report_dict):
    """Top-level keys, for asserting a field was never published."""
    return set(report_dict)


def build(m, **facts):
    facts.setdefault("trace_cap", relay.SUPERVISOR_TRACE_MAX)
    return report.build(m, facts)


# --------------------------------------------------------------- counting ---

class CountingTests(unittest.TestCase):
    def test_a_commit_binding_entry_is_not_a_gate_pass(self):
        """wave_gate traces the sha-binding with status 'committed'.

        It used to be 'passed'. Nothing on the card would have contradicted
        the extra pass, and the error grew with the number of GREEN waves —
        the count would have been most wrong on the best runs.
        """
        m = meta(supervisor_trace=trace(
            {"type": "plan_started"},
            {"type": "gate_result", "status": "running"},
            {"type": "gate_result", "status": "committed", "commit": "abc1234"},
            {"type": "gate_result", "status": "passed"},
        ))
        r = build(m)
        self.assertEqual(r["gate"]["passed"], 1)
        self.assertEqual(r["gate"]["commits"], ["abc1234"])

    def test_the_engine_really_stamps_committed_not_passed(self):
        """The rule above is only worth anything if relay agrees.

        Read off the shipping source rather than a fixture: this is the
        engine half of a two-module rule, and a test over the fixture alone
        passes happily while the engine goes on writing 'passed'.
        """
        import inspect
        src = inspect.getsource(relay.wave_gate)
        head = src.split('"Gate passed"')[0]
        self.assertIn('status="committed"', head)
        self.assertNotIn('status="passed"', head)

    def test_the_checkpoint_sha_rides_as_a_field_not_prose(self):
        src = __import__("inspect").getsource(relay.wave_gate)
        self.assertIn("commit=sha", src)
        # and the whitelist actually lets it through
        state = {"mode": "supervisor", "supervisor_trace": []}

        class IO:
            def emit(self, *a, **k):
                pass
        entry = relay.supervisor_trace(state, IO(), "gate", "t", "d",
                                       status="passed", commit="deadbee")
        self.assertEqual(entry["commit"], "deadbee")

    def test_waves_are_counted_from_plan_created_not_from_a_wave_number(self):
        """`wave` is a per-plan CURSOR, so its max is not a count.

        The measured sequence from a real two-hour run: 1,1 -> 2,2 -> 1 ->
        3,3 -> 4,4 -> 5 -> 1. Six plans, max(wave) == 5, meta's own
        supervisor_wave_index == 1.
        """
        rows, waves = [], [1, 1, 2, 2, 1, 3, 3, 4, 4, 5, 1]
        for i, w in enumerate(waves):
            rows.append({"type": "plan_created" if i in (0, 2, 4, 5, 9, 10)
                         else "task_assigned", "wave": w})
        m = meta(supervisor_trace=trace({"type": "plan_started"}, *rows),
                 supervisor_wave_index=1)
        r = build(m)
        self.assertEqual(r["waves"]["dispatched"], 6)

    def test_check_ins_and_rollovers_change_no_number_on_the_card(self):
        """Both are emitted several times per event (8 and 4 call sites), so
        neither can be counted from the log at all.

        Asserted as "adding them changes NOTHING", not as "there is no key
        called checkins": the first version checked for the absence of a
        field name, so a mutation that published a wrong COUNT under any
        other name left it green. RED-verified.
        """
        base = meta(supervisor_trace=trace({"type": "plan_started"},
                                           {"type": "plan_created"}))
        noisy = meta(supervisor_trace=trace(
            {"type": "plan_started"}, {"type": "plan_created"},
            *([{"type": "health_check"}] * 8
              + [{"type": "objective_set"}] * 6)))
        a, b = build(base), build(noisy)
        for key in ("waves", "trouble", "gate", "objectives", "tasks"):
            self.assertEqual(a[key], b[key],
                             "%s moved when only check-ins/rollovers were "
                             "added" % key)
        self.assertNotIn("checkins", r_keys(b))
        self.assertNotIn("rollovers", r_keys(b))

    def test_settled_objectives_come_from_the_archive_not_the_log(self):
        m = meta(continuous={"on": True, "history": [
            {"goal": "one", "tasks": 3, "failed": 1, "delivered": ["a.py"],
             "gate": {"ok": True}},
            {"goal": "two", "tasks": 2, "failed": 0, "delivered": ["b.py"],
             "gate": {"ok": False}}]},
            supervisor_trace=trace({"type": "plan_started"},
                                   {"type": "objective_set"},
                                   {"type": "objective_set"},
                                   {"type": "objective_set"}))
        r = build(m)
        self.assertEqual(len(r["objectives"]["settled"]), 2)
        self.assertEqual(r["objectives"]["settled"][0]["gate_ok"], True)
        self.assertEqual(r["objectives"]["settled"][1]["gate_ok"], False)

    def test_a_gate_that_never_reported_back_is_its_own_fact(self):
        m = meta(supervisor_trace=trace(
            {"type": "plan_started"},
            {"type": "gate_result", "status": "running"},
            {"type": "gate_result", "status": "passed"},
            {"type": "gate_result", "status": "running"}))
        r = build(m)
        self.assertEqual(r["gate"]["passed"], 1)
        self.assertEqual(r["gate"]["unfinished"], 1)

    def test_an_unfinished_gate_is_not_claimed_on_a_trimmed_log(self):
        """On a trimmed log an early `running` can simply have outlived the
        `passed` that answered it, so the pairing proves nothing."""
        rows = [{"type": "gate_result", "status": "running"}] + \
               [{"type": "task_assigned"}] * 3
        m = meta(supervisor_trace=trace(*rows))
        r = build(m, trace_cap=4)
        self.assertTrue(r["trimmed"])
        self.assertEqual(r["gate"]["unfinished"], 0)


# ------------------------------------------------------------- truncation ---

class TruncationTests(unittest.TestCase):
    def test_a_full_log_is_trimmed_and_says_so(self):
        """The CAP signal, isolated.

        The log starts with `plan_started` on purpose, so the structural
        signal is silent and only the cap can detect the trim — the first
        version used six plan_created rows, which tripped BOTH, so removing
        the cap branch left the test green. RED-verified.
        """
        rows = [{"type": "plan_started"}] + [{"type": "plan_created"}] * 5
        r = build(meta(supervisor_trace=trace(*rows)), trace_cap=6)
        self.assertTrue(r["trimmed"])
        self.assertTrue(r["waves"]["floor"])
        self.assertTrue(any("floors" in n for n in r["notes"]))
        # ...and one entry under the cap is not trimmed
        fewer = build(meta(supervisor_trace=trace(*rows[:5])), trace_cap=6)
        self.assertFalse(fewer["trimmed"])

    def test_a_log_that_does_not_start_at_the_first_plan_is_trimmed(self):
        """The structural signal, which does not depend on the cap constant.

        `del entries[:-MAX]` trims the OLDEST, so a complete log always
        begins with the run's first `plan_started`. This keeps working if
        SUPERVISOR_TRACE_MAX is ever raised, lowered or removed.
        """
        r = build(meta(supervisor_trace=trace({"type": "task_assigned"})),
                  trace_cap=999)
        self.assertTrue(r["trimmed"])

    def test_a_short_whole_log_is_not_trimmed(self):
        r = build(meta(supervisor_trace=trace({"type": "plan_started"},
                                              {"type": "plan_created"})),
                  trace_cap=120)
        self.assertFalse(r["trimmed"])
        self.assertFalse(r["waves"]["floor"])

    def test_the_real_cap_is_what_the_bridge_passes(self):
        """A floor that is computed against the wrong constant is not a
        floor. The bridge hands relay's own SUPERVISOR_TRACE_MAX in."""
        import inspect
        src = inspect.getsource(__import__("app").Api.session_report)
        self.assertIn("relay.SUPERVISOR_TRACE_MAX", src)


# ------------------------------------------------------------------ clock ---

class ClockTests(unittest.TestCase):
    def test_open_for_is_never_the_duration_the_card_may_print(self):
        """created->updated measures the CHAT. A real session reports
        72,949s (20h) for a run that worked for well under two."""
        m = meta(created="2026-09-01T01:00:00", updated="2026-09-02T21:15:49",
                 supervisor_trace=trace(
                     {"type": "plan_started", "ts": "2026-09-01T01:00:00"},
                     {"type": "plan_created", "ts": "2026-09-01T02:00:00"}))
        r = build(m)
        self.assertEqual(r["clock"]["open_for_s"], 159349)   # 44h15m49s
        # ...and NOTHING promotes it to a printable duration. The control
        # log's own first->last bracket is not one either: measured on the
        # real chat 20260822-220535 it spans 22:09 to 09:04 across eleven
        # entries, so an earlier version chipped "10h 55m active" for a run
        # that worked for minutes.
        self.assertIsNone(r["clock"]["worked_s"])
        self.assertNotIn("best_s", r["clock"])
        self.assertNotIn("active_s", r["clock"])

    def test_the_accumulated_clock_wins_when_there_is_one(self):
        m = meta(continuous={"on": True, "elapsed_s": 6998.5},
                 supervisor_trace=trace(
                     {"type": "plan_started", "ts": "2026-09-01T01:00:00"},
                     {"type": "plan_created", "ts": "2026-09-01T01:05:00"}))
        r = build(m)
        self.assertEqual(r["clock"]["worked_s"], 6998)

    def test_no_timestamps_means_no_duration_rather_than_zero(self):
        m = meta(created="", updated="", supervisor_trace=[])
        r = build(m)
        self.assertIsNone(r["clock"]["worked_s"])
        self.assertEqual(report.human_duration(None), "")

    def test_the_away_threshold_reads_the_working_clock_only(self):
        """A chat open for twenty hours that worked for four minutes was
        watched; the open_for span proves nothing about it."""
        m = meta(created="2026-09-01T01:00:00", updated="2026-09-02T01:00:00",
                 started_by={"kind": "josh"},
                 supervisor_trace=trace(
                     {"type": "plan_started", "ts": "2026-09-01T01:00:00"},
                     {"type": "plan_created", "ts": "2026-09-01T01:04:00"}))
        r = build(m)
        self.assertFalse(r["show"])
        # no accumulated clock => no honest duration at all, and the reason
        # says that rather than inventing one from the trace span
        self.assertIn("no measured working time", r["reason"])
        m2 = dict(m, continuous={"on": True, "elapsed_s": 240})
        r2 = build(m2)
        self.assertFalse(r2["show"])
        self.assertIn("4m", r2["reason"])


# ------------------------------------------------------------------ blank ---

class BlankTests(unittest.TestCase):
    def test_a_cost_nobody_reported_stays_none(self):
        m = meta(seats=[{"id": 0, "provider": "gemini", "label": "Gem"}],
                 usage={"total_cost_usd": None,
                        "by_seat": {"0": {"cost_usd": None, "turns": 4}}})
        r = build(m)
        self.assertIsNone(r["spend"]["by_seat"][0]["cost_usd"])
        self.assertEqual(r["spend"]["by_seat"][0]["turns"], 4)
        self.assertFalse(r["spend"]["reported"])
        self.assertTrue(any("not a spend of zero" in n for n in r["notes"]))

    def test_a_measured_zero_is_still_a_zero(self):
        m = meta(seats=[{"id": 0, "provider": "claude"}],
                 usage={"total_cost_usd": 0.0,
                        "by_seat": {"0": {"cost_usd": 0.0, "turns": 1}}})
        r = build(m)
        self.assertEqual(r["spend"]["by_seat"][0]["cost_usd"], 0.0)
        self.assertTrue(r["spend"]["reported"])

    def test_a_seat_that_reports_nothing_keeps_its_row(self):
        """It took turns. Dropping the row would understate the roster."""
        m = meta(seats=[{"id": 0, "provider": "claude", "label": "C"},
                        {"id": 1, "provider": "ox", "label": "Ox"}],
                 usage={"total_cost_usd": 0.5, "by_seat": {
                     "0": {"cost_usd": 0.5, "turns": 3},
                     "1": {"cost_usd": None, "turns": 3}}})
        r = build(m)
        self.assertEqual([s["name"] for s in r["spend"]["by_seat"]], ["C", "Ox"])
        self.assertTrue(r["spend"]["reported"])

    def test_no_token_figure_is_published_anywhere(self):
        """GPT's counters were thread-CUMULATIVE before 2026-08-27 and the
        records on disk still carry the lie; stats.py is the one reader that
        knows which to believe. A second, wrong copy here is worse than the
        absence, and the absence is STATED."""
        m = meta(usage={"total_cost_usd": 1.0, "input_tokens": 559310306,
                        "output_tokens": 4, "by_seat": {}})
        r = build(m)
        blob = json.dumps(r)
        self.assertNotIn("559310306", blob)
        self.assertNotIn("input_tokens", blob)
        self.assertTrue(any("Token counts are not shown" in n for n in r["notes"]))

    def test_a_missing_gate_command_is_a_stated_reason_not_a_missing_row(self):
        r = build(meta(continuous={"on": True}))
        self.assertIn("no verification command", r["gate"]["skipped"])
        self.assertTrue(any("Verification:" in n for n in r["notes"]))

    def test_a_status_dict_survives_as_a_dict(self):
        """relay.supervisor_status returns a dict. Stringifying it made every
        real card render an empty status — found only by running the module
        against the real sessions folder."""
        m = meta(workstreams=[{"id": "T1", "status": "pending"}])
        r = build(m, status=relay.supervisor_status(m))
        self.assertIsInstance(r["status"], dict)
        self.assertIn("open", r["status"]["label"])


# ------------------------------------------------------------- provenance ---

class ProvenanceTests(unittest.TestCase):
    def test_a_scheduled_run_shows_no_matter_how_short(self):
        m = meta(started_by={"kind": "schedule", "name": "Nightly"},
                 created="2026-09-01T01:00:00", updated="2026-09-01T01:00:30")
        r = build(m)
        self.assertTrue(r["show"])
        self.assertEqual(r["started_by"]["kind"], "schedule")
        self.assertEqual(r["started_by"]["name"], "Nightly")

    def test_an_absent_stamp_is_not_read_as_josh(self):
        r = build(meta())
        self.assertIsNone(r["started_by"]["kind"])
        self.assertFalse(r["started_by"]["known"])
        # and an unrecognised kind reads the same way, since report._provenance
        # reads meta DIRECTLY and never calls relay's normalizer
        odd = build(meta(started_by={"kind": "cron"}))
        self.assertIsNone(odd["started_by"]["kind"])
        self.assertFalse(odd["started_by"]["known"])
        # ...and the note only claims the inference that was AVAILABLE: with
        # no accumulated clock there is no duration to have inferred from,
        # and saying otherwise describes a method that was not used.
        self.assertTrue(any("no working time was measured" in n
                            for n in r["notes"]), r["notes"])
        timed = build(meta(continuous={"on": True, "elapsed_s": 30}))
        self.assertTrue(any("inferred from how long it worked" in n
                            for n in timed["notes"]), timed["notes"])

    def test_an_unknown_kind_is_dropped_rather_than_stored(self):
        self.assertIsNone(relay.started_by_record({"kind": "cron"}))
        self.assertIsNone(relay.started_by_record(None))
        self.assertIsNone(relay.started_by_record("schedule"))
        self.assertEqual(relay.started_by_record({"kind": "josh"}),
                         {"kind": "josh"})

    def test_an_unattended_terminal_run_shows_however_short(self):
        """`--unattended` is the terminal declaring that nobody is at the
        console — the same authority the engine already trusts for
        `[[ASK]]`. A cron run is exactly a chat that ran while you were
        away, whatever its duration."""
        m = meta(started_by={"kind": "terminal", "unattended": True},
                 created="2026-09-01T01:00:00", updated="2026-09-01T01:00:20")
        r = build(m)
        self.assertTrue(r["show"])
        self.assertTrue(r["started_by"]["unattended"])

    def test_an_attended_terminal_run_is_judged_on_duration_like_any_other(self):
        m = meta(started_by={"kind": "terminal"},
                 created="2026-09-01T01:00:00", updated="2026-09-01T01:00:20")
        r = build(m)
        self.assertFalse(r["show"])
        self.assertFalse(r["started_by"]["unattended"])

    def test_unattended_is_a_property_not_a_kind(self):
        """Folding it into `kind` would either invent a who or lose it."""
        self.assertNotIn("unattended", relay.STARTED_BY_KINDS)
        self.assertEqual(relay.started_by_record({"kind": "terminal"}),
                         {"kind": "terminal"})
        self.assertEqual(
            relay.started_by_record({"kind": "terminal", "unattended": True}),
            {"kind": "terminal", "unattended": True})
        # only ever True: a falsy flag is an absence, not a claim
        self.assertEqual(
            relay.started_by_record({"kind": "terminal", "unattended": False}),
            {"kind": "terminal"})

    def test_the_terminal_stamps_its_own_provenance(self):
        """relay.main() is app.Api's peer, not part of the engine — the same
        rule that lets it declare `--unattended`."""
        import inspect
        src = inspect.getsource(relay.main)
        self.assertIn('"started_by"', src)
        self.assertIn('"kind": "terminal"', src)
        self.assertIn("args.unattended", src)

    def test_the_stamp_survives_a_save_and_a_rehydrate(self):
        """Anything SessionStore.save reads off state must be put BACK by
        rehydrate, or a resumed chat writes the default over the real value
        on its very next save."""
        import inspect
        save = inspect.getsource(relay.SessionStore.save)
        rehy = inspect.getsource(relay.rehydrate)
        self.assertIn('"started_by"', save)
        self.assertIn('"started_by"', rehy)

    def test_an_interrupted_run_shows_however_short(self):
        m = meta(completion={"lifecycle": "active"},
                 created="2026-09-01T01:00:00", updated="2026-09-01T01:00:20")
        r = build(m, interrupted=relay.was_interrupted(m))
        self.assertTrue(r["interrupted"])
        self.assertTrue(r["show"])

    def test_a_pending_question_shows_however_short(self):
        m = meta(ask_pending={"seat": 0, "question": "Which one?"},
                 created="2026-09-01T01:00:00", updated="2026-09-01T01:00:20")
        r = build(m)
        self.assertTrue(r["show"])
        self.assertEqual(r["waiting"]["question"], "Which one?")

    def test_a_scheduled_room_of_any_recipe_still_reports(self):
        """THE ordering rule. With the "is there anything to report" test on
        top, a nightly round-robin / Talk Live / solo room produced no card
        at all — a schedule starts a SAVED ROOM of any recipe, and those are
        exactly the runs nobody watched."""
        m = meta(mode="round_robin", supervisor_trace=[],
                 started_by={"kind": "schedule", "name": "Nightly"})
        r = build(m)
        self.assertTrue(r["show"])
        self.assertTrue(any("no manager" in n for n in r["notes"]))

    def test_a_room_with_no_manager_has_no_verification_row(self):
        """It never had a verification stage, so "no command was configured"
        would read as a setting somebody forgot."""
        r = build(meta(mode="round_robin", supervisor_trace=[],
                       started_by={"kind": "webhook"}))
        self.assertFalse(r["gate"]["applicable"])
        self.assertIsNone(r["gate"]["skipped"])
        self.assertFalse(r["waves"]["applicable"],
                         "a room with no manager has no waves either")

    def test_only_a_keep_improving_room_is_asked_about_verification(self):
        """`wave_gate` returns None unless `continuous_on(state)`, so a Build
        Together room never had a verification stage — telling its reader "no
        verification command was configured" names a setting they forgot to
        fill in for a field the room does not have."""
        build_together = build(meta())                 # supervisor, not continuous
        self.assertFalse(build_together["gate"]["applicable"])
        self.assertIsNone(build_together["gate"]["skipped"])
        keep_improving = build(meta(continuous={"on": True}))
        self.assertTrue(keep_improving["gate"]["applicable"])
        self.assertIn("no verification command",
                      keep_improving["gate"]["skipped"])

    def test_a_chat_with_no_manager_and_no_provenance_never_shows(self):
        """Nothing recorded, nothing ran, nobody asked — no card."""
        r = build(meta(mode="round_robin", supervisor_trace=[]))
        self.assertFalse(r["show"])
        self.assertFalse(r["available"])
        self.assertIn("nothing recorded", r["reason"])


# ------------------------------------------------------------- robustness ---

class RobustnessTests(unittest.TestCase):
    def test_junk_never_raises(self):
        for bad in (None, [], "meta", 7, {"supervisor_trace": "nope"},
                    {"continuous": 5}, {"usage": []}, {"seats": "x"},
                    {"workstreams": [None, 3, {"status": None}]},
                    {"supervisor_trace": [None, 3, {"type": None}]},
                    {"created": "not-a-date", "updated": {}},
                    {"ask_pending": "yes"}, {"started_by": 4}):
            r = report.build(bad, {"trace_cap": None})
            self.assertIn("ok", r)
            self.assertIn("show", r)
            json.dumps(r)          # and it is serialisable for the bridge

    def test_facts_are_optional(self):
        self.assertTrue(report.build(meta())["ok"])
        self.assertTrue(report.build(meta(), None)["ok"])

    def test_the_module_imports_nothing_from_the_app(self):
        """Standalone in the house style. An AST walk, not a substring — a
        grep matches the docstring's own promise (the confinement-parity
        lesson: a line-anchored guard was needed there for the same reason)."""
        import ast
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "report.py"), encoding="utf-8").read()
        banned = {"relay", "app", "webview", "workstreams", "outcome"}
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], banned)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], banned)

    def test_the_module_owns_no_root_directory(self):
        """fork.py's surviving SESSIONS_DIR is the counter-example memory.py
        and schedule.py both cite by name."""
        self.assertEqual([n for n in dir(report)
                          if n.endswith("_DIR") or n.endswith("_ROOT")], [])

    def test_delivered_merges_the_archive_and_the_live_board_without_repeats(self):
        m = meta(continuous={"on": True, "history": [
                     {"goal": "g", "delivered": ["a.py", "b.py"]}]},
                 workstreams=[{"id": "T1", "status": "done",
                               "verified": {"delivered": ["b.py", "c.py"]}}])
        r = build(m)
        self.assertEqual(r["delivered"], ["a.py", "b.py", "c.py"])
        self.assertEqual(r["delivered_total"], 3)

    def test_the_opener_is_never_printed_as_the_managers_objective(self):
        r = build(meta(topic="make it better", supervisor_goal=""))
        self.assertEqual(r["objective"]["text"], "make it better")
        self.assertEqual(r["objective"]["source"], "opener")
        r2 = build(meta(topic="make it better", supervisor_goal="Refactor X"))
        self.assertEqual(r2["objective"]["source"], "supervisor")

    def test_the_mechanical_stop_and_the_goal_verdict_stay_apart(self):
        r = build(meta(completion={"lifecycle": "paused",
                                   "termination_reason": "cap",
                                   "goal_verdict": "unknown"}))
        self.assertEqual(r["outcome"]["stopped"], "cap")
        self.assertEqual(r["outcome"]["verdict"], "unknown")


# ------------------------------------------------- what the review found ---

class ReviewTests(unittest.TestCase):
    """One test per defect the adversarial pass confirmed. Each names the
    failure it prevents, because that is what makes it re-readable."""

    def test_a_live_run_is_not_reported_as_an_interrupted_one(self):
        """`was_interrupted` is TRUE for every healthy run in progress -- its
        evidence is lifecycle "active" with no termination reason, which is
        exactly what a live run looks like. Without the `running` half,
        pressing Send and switching tabs popped "While you were away" on a
        forty-second-old chat Josh had never left."""
        m = meta(completion={"lifecycle": "active"},
                 created="2026-09-01T01:00:00", updated="2026-09-01T01:00:40")
        live = build(m, interrupted=True, running=True)
        self.assertFalse(live["show"])
        dead = build(m, interrupted=True, running=False)
        self.assertTrue(dead["show"])

    def test_a_seeded_zero_total_is_not_a_spend(self):
        """record_usage SEEDS total_cost_usd: 0.0 on its first call whatever
        the turn reported, so the total's presence proves nothing. An earlier
        version chipped "$0.00 spent" directly above a row reading "no cost
        reported" -- the two halves of one card contradicting each other."""
        m = meta(seats=[{"id": 0, "provider": "ox", "label": "Ox"}],
                 usage={"total_cost_usd": 0.0, "input_tokens": 900,
                        "by_seat": {"0": {"cost_usd": None, "turns": 3}},
                        "by_kind": {}})
        r = build(m)
        self.assertFalse(r["spend"]["reported"])
        self.assertIsNone(r["spend"]["total_usd"])

    def test_a_side_call_cost_alone_still_counts_as_reported(self):
        """A room of Gemini/OpenCode seats with a Claude manager really did
        spend money -- on the manager. Reading only by_seat calls it free."""
        m = meta(usage={"total_cost_usd": 0.42, "by_seat": {},
                        "by_kind": {"supervisor": {"cost_usd": 0.42}}})
        r = build(m)
        self.assertTrue(r["spend"]["reported"])
        self.assertEqual(r["spend"]["by_kind"]["supervisor"], 0.42)

    def test_a_watchdog_abandoned_objective_is_not_counted_as_met(self):
        """archive_objective has TWO callers meaning opposite things: the
        manager settling a finished board, and the check-in throwing a stuck
        one away. Both wrote the identical record."""
        m = meta(continuous={"on": True, "history": [
            {"goal": "a", "tasks": 2, "outcome": "met"},
            {"goal": "b", "tasks": 2, "outcome": "abandoned"},
            {"goal": "c", "tasks": 2}]})           # legacy: unknown
        r = build(m)
        self.assertEqual(r["objectives"]["met"], 1)
        self.assertEqual(r["objectives"]["unknown"], 1)
        self.assertEqual(r["objectives"]["recorded"], 3)
        self.assertTrue(any("counted as neither" in n for n in r["notes"]))

    def test_the_engine_records_which_kind_of_ending_it_was(self):
        state = {"continuous": {}, "supervisor_goal": "g",
                 "workstreams": [{"id": "T1", "status": "done"}]}
        relay.archive_objective(state, outcome="abandoned")
        self.assertEqual(state["continuous"]["history"][0]["outcome"],
                         "abandoned")
        state2 = {"continuous": {}, "supervisor_goal": "g",
                  "workstreams": [{"id": "T1", "status": "done"}]}
        relay.archive_objective(state2)
        self.assertEqual(state2["continuous"]["history"][0]["outcome"], "met")
        # ...and the memory note follows the RECORD, not the call site: it is
        # written into Alloy's own memory, where a later run reads it as fact
        self.assertTrue(relay.describe_objective(
            {"goal": "g", "tasks": 1, "outcome": "abandoned"}
        ).startswith("Objective abandoned"))
        self.assertTrue(relay.describe_objective(
            {"goal": "g", "tasks": 1}).startswith("Objective closed"))

    def test_the_watchdog_replan_archives_as_abandoned(self):
        import inspect
        src = inspect.getsource(relay.apply_remedy)
        self.assertIn('archive_objective(state, outcome="abandoned")', src)

    def test_the_objective_list_announces_its_own_cut(self):
        hist = [{"goal": "g%d" % i, "tasks": 1, "outcome": "met"}
                for i in range(30)]
        r = build(meta(continuous={"on": True, "history": hist}))
        self.assertEqual(r["objectives"]["met"], 30)
        self.assertEqual(r["objectives"]["shown"], report.OBJECTIVES_MAX)
        self.assertFalse(r["objectives"]["listed_all"])
        self.assertTrue(any("30 objectives are recorded" in n
                            for n in r["notes"]))

    def test_the_failing_gate_keeps_its_end_not_its_beginning(self):
        """wave_gate already tail-slices the command output into `detail`, so
        head-slicing that keeps the middle and throws the assertion away."""
        tail = "\n".join("line %d" % i for i in range(600)) \
            + "\nAssertionError: X"
        r = build(meta(supervisor_trace=trace(
            {"type": "plan_started"},
            {"type": "gate_result", "status": "failed", "detail": tail})))
        self.assertTrue(r["gate"]["last_failure"].endswith("AssertionError: X"))
        self.assertIn("earlier output not kept", r["gate"]["last_failure"])

    def test_the_trimming_note_states_the_cap_not_the_entry_count(self):
        """They coincide only on the path where the cap detected the trim. On
        the structural path an earlier version told the reader Alloy keeps
        three control-log entries -- a false claim about the product, made
        inside the one sentence whose whole job is to be trustworthy."""
        r = build(meta(supervisor_trace=trace({"type": "task_assigned"},
                                              {"type": "task_assigned"})),
                  trace_cap=120)
        self.assertTrue(r["trimmed"])
        note = [n for n in r["notes"] if "control-log" in n][0]
        self.assertIn("120", note)
        self.assertNotIn("2 control-log", note)
        # ...and with no cap to quote it does not invent one
        r2 = build(meta(supervisor_trace=trace({"type": "task_assigned"})),
                   trace_cap=None)
        note2 = [n for n in r2["notes"] if "control log" in n][0]
        self.assertIn("no longer kept", note2)

    def test_a_manual_schedule_fire_is_not_unattended(self):
        """Run now had a person behind it. Stamped as a plain schedule, the
        card headed a run Josh started thirty seconds earlier "While you were
        away" and told him a timer had done it at 01:00."""
        m = meta(started_by={"kind": "schedule", "name": "N", "manual": True},
                 created="2026-09-01T01:00:00", updated="2026-09-01T01:00:30")
        r = build(m)
        self.assertTrue(r["started_by"]["manual"])
        self.assertFalse(r["show"])
        auto = build(meta(started_by={"kind": "schedule", "name": "N"},
                          created="2026-09-01T01:00:00",
                          updated="2026-09-01T01:00:30"))
        self.assertTrue(auto["show"])

    def test_the_bridge_carries_the_manual_flag(self):
        """Sliced to the `started_by` dict, not searched across the whole
        function: `_launch_schedule` writes `"manual": bool(manual)` TWICE —
        once into `start["scheduled"]` for the transcript row and once into
        the durable stamp — so removing the second left the first, and a
        substring assertion over the whole source was satisfied by the wrong
        one. RED-verified after the slice."""
        import inspect
        import app
        src = inspect.getsource(app.Api._launch_schedule)
        stamp = src[src.index('start["started_by"]'):]
        self.assertIn('"manual": bool(manual)', stamp)
        self.assertEqual(
            relay.started_by_record({"kind": "schedule", "manual": True}),
            {"kind": "schedule", "manual": True})

    def test_the_activity_bracket_carries_its_floor(self):
        """The bracket is trace-derived, so a trimmed log has lost its
        beginning and the range starts wherever the survivors do. The UI
        probe could not see this: it feeds a hand-built payload, so a
        mutation in report.py never reached it."""
        rows = [{"type": "task_assigned", "ts": "2026-09-01T07:40:00"},
                {"type": "task_assigned", "ts": "2026-09-01T09:01:00"}]
        r = build(meta(supervisor_trace=trace(*rows)), trace_cap=120)
        self.assertTrue(r["trimmed"])
        self.assertTrue(r["clock"]["active_floor"])
        whole = build(meta(supervisor_trace=trace(
            {"type": "plan_started"}, {"type": "plan_created"})),
            trace_cap=120)
        self.assertFalse(whole["clock"]["active_floor"])

    def test_a_fork_does_not_inherit_who_started_the_original(self):
        """A fork is made by hand, now, by a person clicking the branch
        button -- inherited provenance says a 01:00 timer made a chat that
        did not exist at 01:00, and auto-opens its card on that claim."""
        import inspect
        import fork as fork_mod
        src = inspect.getsource(fork_mod.fork_session)
        self.assertIn('meta.pop("started_by", None)', src)

    def test_the_provenance_vocabulary_matches_relays(self):
        """report.py imports nothing from relay, so KINDS is a deliberate
        second copy -- kept honest by this test rather than by a comment, the
        way browser_mcp._confine is. Drift would silently stop a new front
        end's runs from ever opening with a card."""
        self.assertEqual(tuple(report.KINDS), tuple(relay.STARTED_BY_KINDS))
        for kind in relay.STARTED_BY_KINDS:
            r = build(meta(started_by={"kind": kind}))
            self.assertTrue(r["started_by"]["known"], kind)

    def test_a_room_with_no_manager_reports_no_wave_count(self):
        """repCount(0) is the truthy string "0", so an unconditional chip
        reported a count of a concept the room does not have -- a few pixels
        above a note saying it has no waves."""
        r = build(meta(mode="round_robin", supervisor_trace=[],
                       started_by={"kind": "schedule", "name": "N"}))
        self.assertFalse(r["waves"]["applicable"])
        self.assertTrue(build(meta())["waves"]["applicable"])

    def test_available_and_show_are_different_questions(self):
        """A chat we can describe perfectly well may still be one Josh sat
        and watched; tying the button to `show` made its report unreachable
        and dropped the sentence explaining why."""
        r = build(meta(continuous={"on": True, "elapsed_s": 60}))
        self.assertTrue(r["available"])
        self.assertFalse(r["show"])
        self.assertTrue(r["reason"])

    def test_a_broken_record_says_so_rather_than_looking_quiet(self):
        """Not a non-dict — `_d` absorbs those by design, and an early
        version of this test used one and proved nothing. It takes a meta
        that raises while being READ, which is what a corrupt record does."""
        class Hostile(dict):
            def get(self, *a, **k):
                raise ValueError("boom")

        r = report.build(Hostile(), {"trace_cap": 120})
        self.assertFalse(r["ok"])
        self.assertFalse(r["show"])
        self.assertIn("could not be read", r["reason"])
        # and a non-dict is still absorbed, not reported as broken
        self.assertTrue(report.build(object(), {"trace_cap": 120})["ok"])

    def test_the_gate_sha_is_read_once_into_a_local(self):
        """`gate_commit.last_sha` is a module-level function attribute and
        Alloy runs several chats at once, so re-reading it further down let a
        second conversation's gate substitute its sha into this one's
        persisted, Josh-facing record."""
        import inspect
        src = inspect.getsource(relay.wave_gate)
        self.assertEqual(src.count('getattr(gate_commit, "last_sha"'), 1)
        self.assertIn("commit=sha", src)
        self.assertIn("sha = None", src)      # bound before the commit branch


# ----------------------------------------------------------------- bridge ---

class BridgeTests(unittest.TestCase):
    """The real app.Api, per test_permissions' rule: a reader that is right
    at the engine and wrong at the bridge is the ordinary outcome."""

    def setUp(self):
        import tempfile
        import shutil
        import app
        self.app = app
        self.tmp = tempfile.mkdtemp(prefix="alloy-report-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = (relay.SESSIONS_DIR, relay.TABS_FILE, app.SESSIONS_DIR)
        relay.SESSIONS_DIR = app.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")

        def restore():
            relay.SESSIONS_DIR, relay.TABS_FILE, app.SESSIONS_DIR = old
        self.addCleanup(restore)

    def _chat(self, **kw):
        d = os.path.join(self.tmp, "20260901-010000-overnight")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta(**kw), f)
        return os.path.basename(d)

    def test_a_real_report_comes_back_through_the_bridge(self):
        sid = self._chat(started_by={"kind": "schedule", "name": "Nightly"},
                         supervisor_trace=trace(
                             {"type": "plan_started"},
                             {"type": "plan_created"},
                             {"type": "gate_result", "status": "passed"}))
        api = self.app.Api()
        r = api.session_report(sid)
        self.assertTrue(r["ok"])
        self.assertTrue(r["show"])
        self.assertEqual(r["waves"]["dispatched"], 1)
        self.assertEqual(r["gate"]["passed"], 1)
        self.assertIsInstance(r["status"], dict)

    def test_an_unknown_chat_is_refused_not_answered_about_another(self):
        """Naming the wrong chat is worse than naming none — Api.stop's
        lesson, where a fallback to the focused run stopped the wrong one.

        The focused run is given a REAL session dir first. Without that
        there was nothing for a fallback to fall back to, so the guard was
        untestable and a mutation replacing it stayed green. RED-verified.
        """
        sid = self._chat(started_by={"kind": "schedule", "name": "N"})
        api = self.app.Api()
        api._runs.focused().session_dir = os.path.join(self.tmp, sid)
        # sanity: that focused chat really would produce a report
        self.assertTrue(api.session_report(sid)["show"])
        r = api.session_report("no-such-chat")
        self.assertFalse(r["ok"])
        self.assertFalse(r["show"])
        self.assertIn("no longer exists", r["reason"])

    def test_the_bridge_reads_no_subprocess_and_no_workspace(self):
        """Bridge-thread synchronous, so it must stay bounded file I/O.
        `subprocess.run` on the pywebview bridge thread deadlocks.

        Checked over the NAMES the body actually references, not over the
        source text: the first version banned the substring "subprocess" and
        was failed by the docstring's own promise not to use one — the same
        family as `"--flag" in help_output` matching `--flagX`, and as the
        confinement-parity guard that matched a module docstring.
        """
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(self.app.Api.session_report).strip())
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
               {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        # not "run": it is a legitimate local here (the live Run object), and
        # banning `subprocess` already covers `subprocess.run`.
        for banned in ("subprocess", "Popen", "Thread", "git_overview",
                       "git_scope", "read_messages", "listdir", "walk"):
            self.assertNotIn(banned, used)

    def test_a_chat_with_no_meta_is_refused_with_a_sentence(self):
        d = os.path.join(self.tmp, "20260901-020000-legacy")
        os.makedirs(d, exist_ok=True)
        api = self.app.Api()
        r = api.session_report(os.path.basename(d))
        self.assertFalse(r["ok"])
        self.assertIn("no saved record", r["reason"])

    def test_session_summary_does_not_carry_the_cards_fields(self):
        """It runs once per session folder inside list_sessions and its
        docstring pins it at one meta read. The report gets a whole meta of
        its own, so these here would be payload on every boot for a reader
        that does not exist — and RAIL_SUMMARY_FIELDS strips them anyway."""
        sid = self._chat(started_by={"kind": "webhook"},
                         ask_pending={"seat": 1, "question": "Which?"})
        s = relay.session_summary(os.path.join(self.tmp, sid))
        self.assertNotIn("started_by", s)
        self.assertNotIn("ask_pending", s)
        # ...and the report reads them straight off meta regardless
        r = self.app.Api().session_report(sid)
        self.assertEqual(r["started_by"]["kind"], "webhook")
        self.assertEqual(r["waiting"]["question"], "Which?")


# ------------------------------------------------------------ the real UI ---

class MarkupTests(unittest.TestCase):
    """Guards a browser would catch and a DOM stub cannot."""

    def setUp(self):
        self.ui = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "ui", "index.html"),
            encoding="utf-8").read()

    def test_the_card_is_alloy_chrome_and_never_a_provider_colour(self):
        """This is the APP accounting for a run, not a participant speaking —
        the rule .working rows and the Changes tab already follow."""
        block = self.ui[self.ui.index(".rep-card"):self.ui.index(".plan-head {")]
        for provider in ("--claude", "--gpt", "--gemini", "--ox"):
            self.assertNotIn(provider, block)
        self.assertIn("--alloy", block)

    def test_the_report_button_is_hidden_when_a_stage_resets(self):
        """The card lives in #feed and dies with it; the BUTTON is outside
        the feed and nothing else would ever hide it again."""
        reset = self.ui[self.ui.index("function resetStage()"):]
        reset = reset[:reset.index('$("feed").innerHTML = EMPTY_HTML;')]
        self.assertIn('$("openReport").hidden = true;', reset)

    def test_the_card_renders_after_the_feed_is_cleared(self):
        """openChat wipes #feed mid-function; a card built before that line
        is silently deleted two lines later."""
        body = self.ui[self.ui.index("async function openChat("):]
        body = body[:body.index("\nfunction restoreSeats")]
        self.assertLess(body.index('$("feed").innerHTML = "";'),
                        body.index("await loadReport("))

    def test_a_long_path_cannot_overflow_the_card(self):
        """A flex item will not shrink below its content without min-width: 0,
        and a workspace-relative path has no break opportunities — so one long
        deliverable painted outside the card and gave the transcript a
        horizontal scrollbar. The W1.4 .msg-head defect, one class over."""
        block = self.ui[self.ui.index(".rep-file {"):]
        block = block[:block.index("}")]
        self.assertIn("min-width: 0", block)
        self.assertIn("overflow-wrap: anywhere", block)

    def test_the_em_rule_targets_the_element_that_exists(self):
        """The parenthetical is built as an <em> inside .rep-v; the lede is
        set with textContent and can never contain an element, so the rule
        sat in the sheet matching nothing while the note rendered italic."""
        self.assertIn(".rep-v em {", self.ui)
        self.assertNotIn(".rep-lede em {", self.ui)

    def test_the_loader_takes_the_chat_id_rather_than_reading_activeid(self):
        """openChat awaits it and Ctrl+Tab can move the focus meanwhile —
        the memory modal's lesson, where a note for project A landed in B."""
        fn = self.ui[self.ui.index("async function loadReport("):]
        fn = fn[:fn.index("\nfunction syncReportBtn")]
        self.assertIn("if (id !== activeId) return;", fn)


if __name__ == "__main__":
    unittest.main(verbosity=2)
