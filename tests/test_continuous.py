"""Keep Improving — the continuous-improvement mode. Token-free.

The mode's whole risk is that it runs while nobody is watching, so these tests
care about exactly two things: that it does not stop when it should keep going,
and that it DOES stop when Josh said it should. Everything runs the real engine
with scripted FakeAgents and a stubbed side-call adapter; no CLI is invoked, no
git command is run, and no tokens are spent.

Run:  python tests/test_continuous.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import run_rounds

from test_loop import FakeAgent, RecordingIO, build_state, saved_meta


class StubSide:
    """The stateless side call (planner / manager / watchdog), scripted.

    Mirrors what `build_supervisor` returns: one `turn`, a `last_usage`, and
    nothing else the engine may lean on.
    """

    def __init__(self, *replies, boom=None):
        self.replies = list(replies)
        self.boom = boom
        self.prompts = []
        self.last_usage = None

    def turn(self, message, on_activity=None):
        self.prompts.append(message)
        if self.boom:
            raise self.boom
        return self.replies.pop(0) if self.replies else "(out of script)"


class ContinuousTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-cont-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------
    def sup_state(self, scripts=None, cont=None, turns=2):
        scripts = scripts or [["ok"], ["ok"]]
        state = build_state(os.path.join(self.tmp, "s"), scripts, turns=turns,
                            labels=["Cee", "Dee"])
        state["providers"] = ["claude"] * len(scripts)
        state["mode"] = "supervisor"
        state["topic"] = "make the thing better"
        state["supervisor_goal"] = "make the thing better"
        state["supervisor_waves"] = 0
        state["supervisor_wave_index"] = 1
        state["supervisor_trace"] = []
        state["until_done"] = True
        state["turn_ceiling"] = None
        state["continuous"] = relay.continuous_policy(
            dict({"on": True}, **(cont or {})))
        # ALWAYS stub the side call. A supervisor-workflow state with no plan
        # makes `_run_rounds` call `plan_workstreams`, which builds a REAL
        # ClaudeAgent and shells out to the CLI — two tests here did exactly
        # that and took 23 seconds between them before this line existed.
        # A suite that says "token-free" has to be structurally token-free.
        self.stub_side(StubSide("no plan today"))
        return state

    def stub_side(self, stub):
        real = relay.build_supervisor
        relay.build_supervisor = lambda st: stub
        self.addCleanup(lambda: setattr(relay, "build_supervisor", real))

    def messages(self, io):
        return " | ".join((p.get("text") or "")
                          for e, p in io.events if e == "message")

    def traces(self, io):
        return [p["entry"] for e, p in io.events if e == "supervisor"]

    # ---- policy ----------------------------------------------------------
    def test_policy_is_complete_and_json_safe(self):
        pol = relay.continuous_policy()
        self.assertEqual(json.loads(json.dumps(pol)), pol)
        self.assertFalse(pol["on"])
        self.assertEqual(pol["checkin"],
                         {"minutes": 30, "action": "notify"})

    def test_policy_refuses_values_it_cannot_run(self):
        pol = relay.continuous_policy({
            "on": True, "checkin": {"minutes": 99999, "action": "yolo"}})
        self.assertEqual(pol["checkin"]["minutes"], relay.CHECKIN_MAX_MINUTES)
        self.assertEqual(pol["checkin"]["action"], "notify")
        low = relay.continuous_policy({"on": True, "checkin": {"minutes": 1}})
        self.assertEqual(low["checkin"]["minutes"], relay.CHECKIN_MIN_MINUTES)

    def test_a_garbage_limit_is_no_limit_never_a_zero_one(self):
        """0 would read as 'stop immediately' — the opposite of unset."""
        pol = relay.continuous_policy({
            "on": True,
            "limits": {"spend_usd": "abc", "hours": 0,
                       "watchdog_may_stop": "yes please"}})
        self.assertIsNone(pol["limits"]["spend_usd"])
        self.assertIsNone(pol["limits"]["hours"])
        # a non-bool cannot silently revoke the authority Josh granted
        self.assertTrue(pol["limits"]["watchdog_may_stop"])

    def test_every_limit_off_is_a_legal_configuration(self):
        state = {"continuous": relay.continuous_policy({
            "on": True, "limits": {"spend_usd": None, "hours": None,
                                   "watchdog_may_stop": False}})}
        self.assertIsNone(relay.continuous_backstop(state))
        said = relay.describe_limits(state)
        self.assertIn("Nothing will stop this run", said)
        self.assertIn("Stop button", said)

    def test_describe_limits_names_the_numbers_it_will_stop_on(self):
        state = {"continuous": relay.continuous_policy({
            "on": True, "limits": {"spend_usd": 25, "hours": 8}}),
            "usage": {"total_cost_usd": 3.5}}
        said = relay.describe_limits(state)
        self.assertIn("$25.00", said)
        self.assertIn("$3.50", said)
        self.assertIn("8 h", said)

    # ---- the unbounded ceiling ------------------------------------------
    def test_effective_ceiling_is_none_only_in_continuous_mode(self):
        self.assertIsNone(relay.effective_ceiling(
            {"continuous": relay.continuous_policy({"on": True})}))
        self.assertEqual(relay.effective_ceiling({"turn_ceiling": 12}), 12)

    def test_zero_and_garbage_still_mean_the_default_not_unbounded(self):
        """`x or DEFAULT_CEILING` was the old idiom; 0 must not become None."""
        for value in (0, None, "", "abc", -4):
            self.assertEqual(relay.effective_ceiling({"turn_ceiling": value}),
                             relay.DEFAULT_CEILING, repr(value))

    # ---- directives are opted into, never granted -----------------------
    def test_the_new_directives_are_not_in_known_directives(self):
        for name in ("OBJECTIVE", "IDLE", "HEALTHY", "FIX", "STOP"):
            self.assertNotIn(name, relay.KNOWN_DIRECTIVES, name)

    def test_a_seat_playing_them_stays_visibly_unknown(self):
        body, hits, unknown = relay.peel_directives(
            "I think we are done here.\n[[OBJECTIVE: rewrite everything]]")
        self.assertEqual(hits, [], "it fires nothing")
        self.assertIn("OBJECTIVE", unknown, "and is reported as unknown")

    def test_the_manager_can_still_play_them(self):
        body, goal, idle = relay.parse_next_objective(
            "Tests are thin.\n[[OBJECTIVE: add tests for the parser]]")
        self.assertEqual(goal, "add tests for the parser")
        self.assertIsNone(idle)
        self.assertEqual(body, "Tests are thin.")

    def test_an_idle_reply_is_not_an_objective(self):
        _body, goal, idle = relay.parse_next_objective(
            "Nothing left.\n[[IDLE: the project is finished]]")
        self.assertIsNone(goal)
        self.assertEqual(idle, "the project is finished")

    # ---- objective rollover ---------------------------------------------
    def test_a_met_goal_starts_the_next_objective(self):
        state = self.sup_state()
        state["workstreams"] = [
            {"id": "t1", "owner": 0, "brief": "do it", "files": [],
             "deps": [], "status": "done",
             "verified": {"ok": True, "delivered": ["a.py"]}}]
        state["supervisor_waves"] = 4
        state["supervisor_wave_index"] = 3
        self.stub_side(StubSide(
            "a.py exists but has no tests.\n[[OBJECTIVE: test a.py]]",
            "[[TASK: t9 | owner=0 | files=test_a.py | write the tests]]"))
        io = RecordingIO()
        self.assertEqual(relay.next_objective(state, io), "assigned")
        pol = state["continuous"]
        self.assertEqual(state["supervisor_goal"], "test a.py")
        self.assertEqual(state["supervisor_waves"], 0,
                         "the wave cap measures ONE goal, so it resets")
        self.assertGreater(state["supervisor_wave_index"], 3,
                           "but the wave index keeps climbing for the UI")
        self.assertEqual(pol["objectives"][-1], "test a.py")
        self.assertEqual(len(pol["history"]), 1, "the old board is archived")
        self.assertEqual(pol["history"][0]["delivered"], ["a.py"])
        self.assertIn("Next objective (2): test a.py", self.messages(io))

    def test_a_met_objective_is_not_rendered_as_a_finished_job(self):
        """The UI lifts `goal_accepted` into a "Supervisor closed the job"
        verdict card. A run about to pick its next objective must not get one."""
        state = self.sup_state()
        state["workstreams"] = [
            {"id": "t1", "owner": 0, "brief": "b", "files": [], "deps": [],
             "status": "done", "verified": {"ok": True}}]
        self.stub_side(StubSide("[[DONE: the tests are in place]]"))
        io = RecordingIO()
        self.assertEqual(relay.supervise_next_wave(state, io), "done")
        types = [t["type"] for t in self.traces(io)]
        self.assertIn("objective_set", types)
        self.assertNotIn("goal_accepted", types)
        self.assertIn("Objective met", self.messages(io))
        self.assertNotIn("closed the job", self.messages(io))

    def test_an_ordinary_supervisor_run_still_closes_the_job(self):
        state = self.sup_state()
        state["continuous"] = None          # plain Build Together
        state["workstreams"] = [
            {"id": "t1", "owner": 0, "brief": "b", "files": [], "deps": [],
             "status": "done", "verified": {"ok": True}}]
        self.stub_side(StubSide("[[DONE: shipped]]"))
        io = RecordingIO()
        self.assertEqual(relay.supervise_next_wave(state, io), "done")
        self.assertIn("goal_accepted", [t["type"] for t in self.traces(io)])
        self.assertIn("closed the job", self.messages(io))

    def test_a_dead_side_call_never_forges_an_objective(self):
        state = self.sup_state()
        state["supervisor_goal"] = "keep this"
        self.stub_side(StubSide(boom=RuntimeError("CLI died")))
        io = RecordingIO()
        self.assertEqual(relay.next_objective(state, io), "idle")
        self.assertEqual(state["supervisor_goal"], "keep this")
        self.assertEqual([t["phase"] for t in self.traces(io)][-1], "error")

    def test_an_idle_verdict_says_so_rather_than_inventing_work(self):
        state = self.sup_state()
        self.stub_side(StubSide("[[IDLE: genuinely nothing left]]"))
        io = RecordingIO()
        self.assertEqual(relay.next_objective(state, io), "idle")
        self.assertIn("genuinely nothing left", self.messages(io))
        self.assertIsNone(state.get("workstreams"))

    # ---- backstops -------------------------------------------------------
    def test_the_spend_cap_reads_real_telemetry(self):
        state = self.sup_state(cont={"limits": {"spend_usd": 5}})
        state["usage"] = {"total_cost_usd": 4.99}
        self.assertIsNone(relay.continuous_backstop(state))
        state["usage"] = {"total_cost_usd": 5.0}
        reason = relay.continuous_backstop(state)
        self.assertIn("Spend cap reached", reason)
        self.assertIn("Gemini", reason, "and says what is NOT counted")

    def test_the_time_cap_uses_the_persisted_clock(self):
        state = self.sup_state(cont={"limits": {"hours": 2}})
        state["continuous"]["elapsed_s"] = 7199
        self.assertIsNone(relay.continuous_backstop(state))
        state["continuous"]["elapsed_s"] = 7200
        self.assertIn("Time limit reached", relay.continuous_backstop(state))

    def test_the_clock_survives_a_resume(self):
        """monotonic resets per run; the total must not."""
        state = self.sup_state()
        state["continuous"]["elapsed_s"] = 900.0
        relay.continuous_tick(state)              # first tick sets the mark
        self.assertEqual(state["continuous"]["elapsed_s"], 900.0)
        relay.continuous_tick(state)              # second accumulates
        self.assertGreaterEqual(state["continuous"]["elapsed_s"], 900.0)
        state["store"].save(state)
        back = relay.continuous_policy(saved_meta(state)["continuous"])
        self.assertGreaterEqual(back["elapsed_s"], 900.0)

    def test_a_tripped_limit_pauses_the_run_and_names_itself(self):
        state = self.sup_state(cont={"limits": {"spend_usd": 0.01}})
        state["usage"] = {"total_cost_usd": 1.0}
        io = RecordingIO()
        run_rounds(state, io)
        self.assertEqual(state.get("termination_reason"), "limit")
        self.assertIn("Spend cap reached", self.messages(io))
        self.assertIn("Continue the chat", self.messages(io))

    def test_the_limit_is_announced_once_not_every_barrier(self):
        state = self.sup_state(cont={"limits": {"spend_usd": 0.01}})
        state["usage"] = {"total_cost_usd": 1.0}
        io = RecordingIO()
        reason = relay.continuous_backstop(state)
        relay.announce_backstop(state, io, reason)
        relay.announce_backstop(state, io, reason)
        said = [p for e, p in io.events if e == "message"]
        self.assertEqual(len(said), 1)

    # ---- the watchdog ----------------------------------------------------
    def test_checkin_is_due_only_after_the_interval(self):
        state = self.sup_state(cont={"checkin": {"minutes": 10}})
        pol = state["continuous"]
        pol["elapsed_s"] = 599
        self.assertFalse(relay.checkin_due(state))
        pol["elapsed_s"] = 600
        self.assertTrue(relay.checkin_due(state))

    def test_checkin_schedule_survives_save_and_rehydrate(self):
        state = self.sup_state(cont={"checkin": {"minutes": 10}})
        state["continuous"]["elapsed_s"] = 1200.0
        state["continuous"]["last_checkin_s"] = 1200.0
        state["store"].save(state)
        back = {"continuous": relay.continuous_policy(
            saved_meta(state)["continuous"])}
        self.assertFalse(relay.checkin_due(back))
        back["continuous"]["elapsed_s"] = 1801.0
        self.assertTrue(relay.checkin_due(back))

    def test_health_is_measured_not_guessed(self):
        state = self.sup_state()
        state["turn"] = 7
        state["continuous"]["turn_at_checkin"] = 7
        state["workstreams"] = [
            {"id": "t1", "owner": 0, "brief": "b", "files": ["x.py"],
             "deps": [], "status": "active", "verified": {}}]
        health = relay.continuous_health(state)
        self.assertIn("Committed turns since the last check: 0", health)
        self.assertIn("[t1]", health)
        self.assertIn("ACTIVE", health)

    def test_a_healthy_verdict_changes_nothing(self):
        state = self.sup_state()
        state["workstreams"] = [
            {"id": "t1", "owner": 0, "brief": "b", "files": [], "deps": [],
             "status": "active", "verified": {}}]
        self.stub_side(StubSide("Turns are still landing.\n"
                                "[[HEALTHY: two turns since the last check]]"))
        io = RecordingIO()
        self.assertEqual(relay.run_checkin(state, io), "healthy")
        self.assertEqual(state["workstreams"][0]["status"], "active")

    def test_notify_raises_attention_and_auto_does_not(self):
        for action, expect in (("notify", 1), ("auto", 0)):
            state = self.sup_state(cont={"checkin": {"action": action}})
            self.stub_side(StubSide("[[HEALTHY: fine]]"))
            io = RecordingIO()
            relay.run_checkin(state, io)
            self.assertEqual(len([1 for e, _ in io.events if e == "checkin"]),
                             expect, action)

    def test_permission_asks_before_it_touches_anything(self):
        class Asking(RecordingIO):
            def __init__(self, answer):
                super().__init__()
                self.answer, self.asked = answer, []

            def ask_human(self, payload, abort=None):
                self.asked.append(payload)
                return self.answer

        state = self.sup_state(cont={"checkin": {"action": "permission"}})
        state["workstreams"] = [
            {"id": "t1", "owner": 0, "brief": "b", "files": [], "deps": [],
             "status": "active", "verified": {}}]
        self.stub_side(StubSide("[[FIX: requeue | t1 has not moved]]"))
        io = Asking("Apply the fix")
        self.assertEqual(relay.run_checkin(state, io), "fixed")
        self.assertEqual(len(io.asked), 1)
        self.assertIn("Proposed fix: requeue", io.asked[0]["question"])
        self.assertIn("Apply the fix", io.asked[0]["options"])

    def test_nobody_answering_is_a_skip_never_an_approval(self):
        """Same rule as never-forge-a-turn: silence is not consent."""
        state = self.sup_state(cont={"checkin": {"action": "permission"}})
        state["workstreams"] = [
            {"id": "t1", "owner": 0, "brief": "b", "files": [], "deps": [],
             "status": "active", "verified": {}}]
        self.stub_side(StubSide("[[FIX: requeue | stuck]]"))
        io = RecordingIO()            # headless ask_human returns None
        self.assertEqual(relay.run_checkin(state, io), "idle")
        self.assertEqual(state["workstreams"][0]["status"], "active",
                         "nothing was requeued")
        self.assertIn("Nobody answered", self.messages(io))

    def test_a_check_in_that_dies_does_not_re_fire_every_barrier(self):
        state = self.sup_state(cont={"checkin": {"minutes": 5}})
        state["continuous"]["elapsed_s"] = 600.0
        self.assertTrue(relay.checkin_due(state))
        self.stub_side(StubSide(boom=RuntimeError("no CLI")))
        io = RecordingIO()
        self.assertEqual(relay.run_checkin(state, io), "idle")
        self.assertFalse(relay.checkin_due(state),
                         "the attempt counts as the check")

    def test_stop_is_honored_only_when_josh_granted_it(self):
        for may, expect in ((True, "stop"), (False, "idle")):
            state = self.sup_state(
                cont={"limits": {"watchdog_may_stop": may}})
            self.stub_side(StubSide("[[STOP: the project is finished]]"))
            io = RecordingIO()
            self.assertEqual(relay.run_checkin(state, io), expect, str(may))
            if not may:
                self.assertIn("did not give it that authority",
                              self.messages(io))

    # ---- remedies --------------------------------------------------------
    def test_requeue_redispatches_only_what_was_stuck(self):
        state = self.sup_state()
        state["workstreams"] = [
            {"id": "a", "owner": 0, "brief": "one", "files": [], "deps": [],
             "status": "active", "verified": {}},
            {"id": "b", "owner": 1, "brief": "two", "files": [], "deps": [],
             "status": "done", "verified": {}}]
        io = RecordingIO()
        said = relay.apply_remedy(state, io, "requeue", "")
        self.assertIn("a", said)
        self.assertNotIn("b", said.split("task")[-1])
        self.assertEqual(state["workstreams"][1]["status"], "done")

    def test_replan_survives_a_first_plan_that_never_landed(self):
        """A planner that returns no tasks leaves supervisor_goal unset; the
        watchdog must still know what the run is for (seen live 2026-08-22)."""
        state = self.sup_state()
        state["supervisor_goal"] = None
        self.assertEqual(relay.current_objective(state),
                         "make the thing better")
        self.stub_side(StubSide(
            "[[TASK: r1 | owner=0 | files=a.py | try again]]"))
        said = relay.apply_remedy(state, RecordingIO(), "replan", "")
        self.assertIn("Re-planned", said)
        self.assertEqual([t["id"] for t in state["workstreams"]], ["r1"])

    def test_requeue_with_nothing_stuck_says_so(self):
        state = self.sup_state()
        state["workstreams"] = [
            {"id": "a", "owner": 0, "brief": "one", "files": [], "deps": [],
             "status": "done", "verified": {}}]
        self.assertIn("Nothing was stuck",
                      relay.apply_remedy(state, RecordingIO(), "requeue", ""))

    def test_nudge_reaches_every_seat(self):
        state = self.sup_state()
        relay.apply_remedy(state, RecordingIO(), "nudge", "check the log")
        for i in state["pending"]:
            self.assertIn("check the log", state["pending"][i][-1])

    def test_an_empty_nudge_sends_nothing(self):
        state = self.sup_state()
        relay.apply_remedy(state, RecordingIO(), "nudge", "")
        self.assertEqual(state["pending"][0], [])

    def test_clear_seat_uses_the_documented_recovery(self):
        """/clear is the OFFERED recovery for a dead session id — never a
        silent reseed, which would claim memory the seat no longer has."""
        state = self.sup_state()
        state["agents"][0].session_id = "dead-id"
        state["agents"][1].session_id = "live-id"
        state["introduced"] = [True, True]
        relay.apply_remedy(state, RecordingIO(), "clear_seat", "Cee")
        self.assertIsNone(state["agents"][0].session_id)
        self.assertEqual(state["agents"][1].session_id, "live-id",
                         "only the named seat is touched")
        self.assertFalse(state["introduced"][0], "it re-introduces itself")
        self.assertTrue(state["introduced"][1])

    def test_clear_seat_with_no_name_does_nothing(self):
        state = self.sup_state()
        state["agents"][0].session_id = "keep-me"
        self.assertIn("No seat was named",
                      relay.apply_remedy(state, RecordingIO(), "clear_seat", ""))
        self.assertEqual(state["agents"][0].session_id, "keep-me")

    def test_the_remedy_set_is_closed(self):
        self.assertEqual(relay.split_remedy("rm -rf / | it is stuck")[0], None)
        self.assertEqual(relay.split_remedy("requeue | stuck"),
                         ("requeue", "", "stuck"))
        self.assertEqual(relay.split_remedy("clear_seat:GPT | dead session"),
                         ("clear_seat", "GPT", "dead session"))

    def test_an_unknown_remedy_changes_nothing_and_says_so(self):
        state = self.sup_state()
        state["workstreams"] = [
            {"id": "a", "owner": 0, "brief": "one", "files": [], "deps": [],
             "status": "active", "verified": {}}]
        self.stub_side(StubSide("[[FIX: restart the computer | it is wedged]]"))
        io = RecordingIO()
        self.assertEqual(relay.run_checkin(state, io), "idle")
        self.assertEqual(state["workstreams"][0]["status"], "active")
        self.assertIn("not one of the remedies", self.messages(io))

    # ---- the verification gate ------------------------------------------
    def test_no_test_command_records_a_skip_not_a_pass(self):
        state = self.sup_state(cont={"gate": {"command": ""}})
        result = relay.wave_gate(state, RecordingIO())
        self.assertIsNone(result["ok"])
        self.assertIn("no test command", result["skipped"])
        self.assertIn("NOT RUN", relay._gate_block(state))

    def stub_gate(self, result):
        real = relay._gate_run
        relay._gate_run = lambda cmd, ws, timeout=None: dict(result)
        self.addCleanup(lambda: setattr(relay, "_gate_run", real))

    def stub_git(self, fn):
        real = relay._git
        relay._git = fn
        self.addCleanup(lambda: setattr(relay, "_git", real))

    def test_a_red_gate_becomes_the_next_job(self):
        state = self.sup_state(cont={"gate": {"command": "pytest"}})
        self.stub_gate({"ok": False, "seconds": 2, "tail": "3 failed"})
        io = RecordingIO()
        relay.wave_gate(state, io)
        self.assertIn("Verification FAILED", self.messages(io))
        self.assertIn("3 failed", self.messages(io))
        self.assertIn("FAILED", relay._gate_block(state))

    def test_a_green_gate_commits_only_when_asked(self):
        calls = []

        def fake_git(args, ws, timeout=120):
            calls.append(list(args))
            out = "abc1234" if args[0] == "rev-parse" else ""
            return type("R", (), {"returncode": 0, "stdout": out})()

        self.stub_gate({"ok": True, "seconds": 9, "tail": "all green"})
        self.stub_git(fake_git)
        off = self.sup_state(cont={"gate": {"command": "pytest",
                                            "commit": False}})
        relay.wave_gate(off, RecordingIO())
        self.assertEqual(calls, [], "commit off means git is never touched")
        on = self.sup_state(cont={"gate": {"command": "pytest",
                                           "commit": True}})
        io = RecordingIO()
        relay.wave_gate(on, io)
        self.assertIn(["add", "-A"], calls)
        self.assertIn("committed as abc1234", self.messages(io))

    def test_a_dirty_tree_refuses_the_commit(self):
        """Committing Josh's own edits under a wave label kills the rollback."""
        self.stub_git(lambda args, ws, timeout=120: type(
            "R", (), {"returncode": 0, "stdout": " M app.py"})())
        state = self.sup_state(cont={"gate": {"command": "x", "commit": True,
                                              "dirty_at_start": True}})
        said = relay.gate_commit(state, "alloy: wave")
        self.assertIn("nothing was committed", said)
        self.assertIn("rollback", said)

    def test_a_folder_that_is_not_a_repo_is_not_an_error(self):
        def boom(args, ws, timeout=120):
            raise OSError("git not found")
        self.stub_git(boom)
        state = self.sup_state(cont={"gate": {"commit": True}})
        self.assertIn("not a git repository",
                      relay.gate_commit(state, "alloy: wave"))

    def test_detect_test_command_prefers_the_repos_own_runner(self):
        root = os.path.join(self.tmp, "proj", "tests")
        os.makedirs(root)
        open(os.path.join(root, "run_all.py"), "w").close()
        self.assertEqual(
            relay.detect_test_command(os.path.dirname(root)),
            "python tests/run_all.py")

    def test_detect_test_command_is_empty_for_a_non_project(self):
        empty = os.path.join(self.tmp, "notes")
        os.makedirs(empty)
        self.assertEqual(relay.detect_test_command(empty), "")
        self.assertEqual(relay.detect_test_command(None), "")

    # ---- revival ---------------------------------------------------------
    # The revival loop lives in `run_rounds` AROUND `_run_rounds`, so these
    # drive `_run_rounds` through a seam. That is the honest unit: a real
    # continuous loop has no cap by design and would never return on its own,
    # which is precisely the property the layer below is there to exploit.
    def stub_loop(self, outcomes, commits=True):
        real = relay._run_rounds
        seen = {"n": 0}

        def fake(state, io):
            i = min(seen["n"], len(outcomes) - 1)
            seen["n"] += 1
            if commits:
                state["turn"] = int(state.get("turn") or 0) + 1
            return outcomes[i]

        relay._run_rounds = fake
        self.addCleanup(lambda: setattr(relay, "_run_rounds", real))
        return seen

    def test_a_run_that_falls_over_is_restarted(self):
        """A barrier check cannot fire on a loop that already exited."""
        state = self.sup_state()
        seen = self.stub_loop(["wrapped", "cap", "stopped"])
        io = RecordingIO()
        self.assertEqual(run_rounds(state, io), "stopped")
        self.assertEqual(seen["n"], 3, "it went back in twice")
        said = self.messages(io)
        self.assertIn("the seats wrapped it up", said)
        self.assertIn("Keep Improving is restarting it", said)

    def test_a_revival_clears_the_state_that_would_end_it_again(self):
        state = self.sup_state()
        state["closing"] = [1]
        state["deferred_wrap"] = 0
        self.stub_loop(["wrapped", "stopped"])
        run_rounds(state, RecordingIO())
        self.assertIsNone(state["closing"])
        self.assertIsNone(state["deferred_wrap"])

    def test_three_barren_restarts_stop_instead_of_spinning(self):
        state = self.sup_state()
        seen = self.stub_loop(["cap"], commits=False)
        io = RecordingIO()
        run_rounds(state, io)
        self.assertIn("not one turn was committed", self.messages(io))
        self.assertEqual(state["continuous"]["barren_revivals"],
                         relay.MAX_BARREN_REVIVALS)
        self.assertEqual(seen["n"], relay.MAX_BARREN_REVIVALS + 1)

    def test_a_restart_that_commits_forgives_the_barren_count(self):
        state = self.sup_state()
        state["continuous"]["barren_revivals"] = 2
        self.stub_loop(["cap", "stopped"], commits=True)
        run_rounds(state, RecordingIO())
        self.assertEqual(state["continuous"]["barren_revivals"], 0)

    def test_a_limit_reached_between_restarts_wins(self):
        state = self.sup_state(cont={"limits": {"spend_usd": 1}})
        state["usage"] = {"total_cost_usd": 9.0}
        seen = self.stub_loop(["cap"], commits=True)
        io = RecordingIO()
        run_rounds(state, io)
        self.assertEqual(seen["n"], 1, "it was not restarted")
        self.assertIn("Spend cap reached", self.messages(io))

    def test_josh_stopping_it_is_final(self):
        class Stopper(RecordingIO):
            def should_stop(self):
                return True

        state = self.sup_state()
        io = Stopper()
        self.assertEqual(run_rounds(state, io), "stopped")
        self.assertNotIn("restarting it", self.messages(io))

    def test_an_ordinary_chat_is_untouched_by_any_of_this(self):
        state = build_state(os.path.join(self.tmp, "plain"),
                            [["a"], ["b"]], turns=1)
        io = RecordingIO()
        run_rounds(state, io)
        self.assertNotIn("Keep Improving", self.messages(io))
        self.assertIsNone(state.get("continuous"))
        self.assertEqual(relay.effective_ceiling(state),
                         relay.DEFAULT_CEILING)

    # ---- an unattended run cannot be held open by a question ------------
    def test_a_seats_ask_gets_a_deadline_only_in_continuous_mode(self):
        plain = {"continuous": None}
        sentinel = object()
        self.assertIs(relay.ask_abort(plain, sentinel), sentinel,
                      "an ordinary chat waits as long as Josh needs")
        state = self.sup_state()
        self.assertTrue(callable(relay.ask_abort(state)))

    def test_the_deadline_never_outlasts_the_checkin_interval(self):
        state = self.sup_state(cont={"checkin": {"minutes": 5}})
        abort = relay.ask_abort(state)
        self.assertFalse(abort(), "it does not expire immediately")
        # a caller's own abort still wins straight away
        self.assertTrue(relay.ask_abort(state, lambda: True)())

    def test_an_expired_question_moves_on_without_forging_an_answer(self):
        """Live 2026-08-22: a seat's [[ASK]] wedged the barrier, and every
        brake — clock, spend cap, watchdog — is checked AT that barrier."""
        state = self.sup_state()
        state["ask"] = True
        io = RecordingIO()          # headless ask_human answers None at once
        reply = "Done." + chr(10) + "[[ASK: which way? | left | right]]"
        relay.handle_ask_directive(state, 0, reply, io)
        owed = state["pending"][0][-1]
        self.assertIn("nobody answered", owed.lower())
        self.assertNotIn("Josh (human) answers", self.messages(io))
        self.assertIsNone(state["ask_pending"])

    # ---- persistence -----------------------------------------------------
    def test_the_whole_block_survives_save_and_reopen(self):
        state = self.sup_state(cont={
            "checkin": {"minutes": 45, "action": "permission"},
            "limits": {"spend_usd": 12.5, "hours": None,
                       "watchdog_may_stop": False},
            "gate": {"command": "npm test", "commit": True}})
        state["continuous"]["objectives"] = ["first", "second"]
        state["store"].save(state)
        meta = saved_meta(state)
        summary = relay.session_summary(state["store"].dir)
        self.assertEqual(summary["continuous"]["checkin"]["action"],
                         "permission")
        back = relay.rehydrate(meta)["continuous"]
        self.assertEqual(back["checkin"]["minutes"], 45)
        self.assertEqual(back["limits"]["spend_usd"], 12.5)
        self.assertIsNone(back["limits"]["hours"])
        self.assertFalse(back["limits"]["watchdog_may_stop"])
        self.assertEqual(back["gate"]["command"], "npm test")
        self.assertEqual(back["objectives"], ["first", "second"])

    def test_an_ordinary_session_records_no_continuous_block(self):
        state = build_state(os.path.join(self.tmp, "plain2"), [["a"], ["b"]])
        state["store"].save(state)
        self.assertIsNone(saved_meta(state).get("continuous"))
        self.assertIsNone(relay.session_summary(state["store"].dir)["continuous"])

    # ---- commands --------------------------------------------------------
    def test_the_commands_refuse_outside_this_mode(self):
        state = build_state(os.path.join(self.tmp, "plain3"), [["a"], ["b"]])
        io = RecordingIO()
        relay.dispatch_command(state, "/limits", io)
        self.assertIn("only means something in a Keep Improving",
                      " ".join(p.get("text", "")
                               for e, p in io.events if e == "status"))

    def test_slash_objective_steers_the_next_one(self):
        state = self.sup_state()
        io = RecordingIO()
        relay.dispatch_command(state, "/objective ship the docs", io)
        self.assertEqual(state["supervisor_goal"], "ship the docs")
        for i in state["pending"]:
            self.assertIn("ship the docs", state["pending"][i][-1])

    def test_slash_checkin_arms_the_next_boundary(self):
        state = self.sup_state()
        state["continuous"]["last_checkin_s"] = 999.0
        relay.dispatch_command(state, "/checkin", RecordingIO())
        self.assertTrue(relay.checkin_due(state),
                        "asking for one must work even at t=0")
        self.stub_side(StubSide("[[HEALTHY: fine]]"))
        relay.run_checkin(state, RecordingIO())
        self.assertFalse(relay.checkin_due(state), "and it is consumed once")


class ExecutionRecordTests(ContinuousTest):
    """Execution records + severity-graded focused re-verify.

    A settled task durably answers WHO ran it and WHERE the work landed
    (task -> executed_by -> gate commit), and a repair attempt cites exactly
    what verification proved missing instead of asking for a full re-analysis.
    The single-repair rule in replan_failed_workstreams is reused, never
    duplicated: these tests only pin what its ONE attempt is handed."""

    # ---- grade_findings ---------------------------------------------------
    def test_findings_grade_from_verified_facts_not_claims(self):
        t = {"verified": {"missing": ["x.py"], "stale": ["a/b.md"]}}
        graded = relay.grade_findings(t)
        self.assertEqual(graded, [
            {"severity": "critical", "finding": "never created: x.py"},
            {"severity": "major",
             "finding": "unchanged since the task started: a/b.md"},
        ])
        # nothing verified means nothing cited — an empty report invents no
        # findings, exactly like record_usage refuses to estimate spend
        self.assertEqual(relay.grade_findings({"verified": {}}), [])
        self.assertEqual(relay.grade_findings({}), [])

    # ---- settlement stamps the record -------------------------------------
    def test_settlement_binds_seat_and_clears_resolved_findings(self):
        state = self.sup_state(scripts=[["done"], []])
        ws_dir = state["workspace"]
        started = time.time() - 5
        task = {"id": "t1", "owner": 0, "brief": "make out.txt",
                "files": ["out.txt"], "deps": [], "status": "active",
                "started_ts": started,
                "findings": [{"severity": "major", "finding": "old news"}]}
        state["workstreams"] = [task]
        with open(os.path.join(ws_dir, "out.txt"), "w") as f:
            f.write("delivered\n")          # mtime AFTER started_ts
        relay.settle_workstream(state, 0, RecordingIO(), reply="did it")
        self.assertEqual(task["status"], "done")
        rec = task.get("executed_by") or {}
        self.assertEqual(rec.get("slot"), 0)
        self.assertEqual(rec.get("seat"), "Cee")
        self.assertIsNone(task.get("findings"),
                          "resolved findings must not haunt the next attempt")
        self.assertIn("did it", task.get("report") or "")

    def test_a_failed_settlement_keeps_the_record_too(self):
        """executed_by records the ATTEMPT whatever the outcome."""
        state = self.sup_state(scripts=[["oops"], []])
        task = {"id": "t1", "owner": 0, "brief": "make out.txt",
                "files": ["out.txt"], "deps": [], "status": "active",
                "started_ts": time.time() - 5}
        state["workstreams"] = [task]
        relay.settle_workstream(state, 0, RecordingIO(), reply="oops")
        self.assertEqual(task["status"], "failed")
        self.assertEqual((task.get("executed_by") or {}).get("seat"), "Cee")

    # ---- the commit binding ------------------------------------------------
    def test_green_gate_binds_the_sha_into_this_waves_records(self):
        def fake_git(args, ws, timeout=120):
            out = "abc1234" if args[0] == "rev-parse" else ""
            return type("R", (), {"returncode": 0, "stdout": out})()

        self.stub_gate({"ok": True, "seconds": 1, "tail": "green"})
        self.stub_git(fake_git)
        state = self.sup_state(cont={"gate": {"command": "pytest",
                                              "commit": True}})
        state["workstreams"] = [
            # this wave's work: ran, settled done, no commit yet -> BOUND
            {"id": "a", "owner": 0, "brief": "one", "files": [], "deps": [],
             "status": "done", "verified": {},
             "executed_by": {"slot": 0, "seat": "Cee"}},
            # an earlier wave's record: keeps ITS OWN sha, never clobbered
            {"id": "b", "owner": 1, "brief": "two", "files": [], "deps": [],
             "status": "done", "verified": {},
             "executed_by": {"slot": 1, "seat": "Dee"},
             "commit": "old9999"},
            # marked done WITHOUT ever running through settlement (e.g. a
            # hand-edited board): no executed_by, no honest claim to bind
            {"id": "c", "owner": 0, "brief": "three", "files": [],
             "deps": [], "status": "done", "verified": {}},
        ]
        io = RecordingIO()
        relay.wave_gate(state, io)
        self.assertEqual(state["workstreams"][0]["commit"], "abc1234")
        self.assertEqual(state["workstreams"][1]["commit"], "old9999")
        self.assertNotIn("commit", state["workstreams"][2])

    # ---- the focused re-verify ---------------------------------------------
    def test_replan_hands_the_repair_exactly_what_failed(self):
        state = self.sup_state()
        state["workstreams"] = [
            {"id": "a", "owner": 0, "brief": "one", "files": ["x.py"],
             "deps": [], "status": "failed", "replans": 0,
             "verified": {"missing": ["x.py"], "stale": ["y.py"]}}]
        self.stub_side(StubSide(
            "[[TASK: a | owner=0 | files=x.py | fix it]]"))
        relay.replan_failed_workstreams(state, RecordingIO())
        new = state["workstreams"][0]
        self.assertEqual(new["id"], "a")     # same id: the DAG stays valid
        self.assertEqual(new["findings"], [
            {"severity": "critical", "finding": "never created: x.py"},
            {"severity": "major",
             "finding": "unchanged since the task started: y.py"},
        ])
        self.assertEqual(new["attempts"], 1)

    def test_focused_findings_render_in_the_worker_brief(self):
        state = self.sup_state()
        state["workstreams"] = [
            {"id": "a", "owner": 0, "brief": "one", "files": ["x.py"],
             "deps": [], "status": "pending",
             "findings": [{"severity": "critical",
                           "finding": "never created: x.py"}]}]
        relay.assign_workstreams(state, RecordingIO())
        brief = state["pending"][0][0]
        self.assertIn("FOCUSED RE-VERIFY", brief)
        self.assertIn("Do NOT redo the whole task", brief)
        self.assertIn("- CRITICAL: never created: x.py", brief)
        self.assertIn("Critical, Major, or Minor", brief)
        # a clean task gets none of this
        state["workstreams"].append(
            {"id": "b", "owner": 1, "brief": "two", "files": [], "deps": [],
             "status": "pending"})
        state["pending"][1] = []
        relay.assign_workstreams(state, RecordingIO())
        self.assertNotIn("FOCUSED RE-VERIFY", state["pending"][1][-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
