"""W2.4 — /objective for ordinary supervisor runs, and a truthful goal chip.

`/objective` shared one branch with `/checkin` and `/limits`, gated on
`continuous_on(state)`. Build Together and Keep Improving are the SAME
workflow — the only difference between the two presets is `continuous.on` —
so a manager that could be re-targeted only when the run was unattended was
exactly backwards.

And the reset the plan names is not decoration. `SUPERVISOR_MAX_WAVES`
measures "can this manager converge on ONE goal": a re-target that left
`supervisor_waves` alone handed the manager a goal it had no waves left to
pursue, and `supervise_next_wave` then returned "idle" at every barrier,
silently, forever. That defect already shipped for Keep Improving too — the
continuous path resets the counter only when the MANAGER picks the next
objective (`next_objective`), never when Josh does.

The watchdog's own `replan` remedy had the identical gap, so both go through
one helper.

Run:  python tests/test_objective.py
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from test_loop import RecordingIO, build_state


class ObjectiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-objective-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _state(self, continuous=False, name="s"):
        state = build_state(os.path.join(self.tmp, name), [["a"], ["b"]],
                            labels=["Cee", "Dee"])
        state["providers"] = ["claude", "claude"]
        state["mode"] = "supervisor"
        state["topic"] = "make the thing better"
        state["supervisor_goal"] = "make the thing better"
        state["supervisor_wave_index"] = 3
        state["supervisor_trace"] = []
        state["continuous"] = (relay.continuous_policy({"on": True})
                               if continuous else None)
        return state

    def _notes(self, io):
        return " | ".join(p.get("text", "")
                          for e, p in io.events if e == "status")

    # ---- the gate ---------------------------------------------------------
    def test_an_ordinary_build_together_run_accepts_it(self):
        """Build Together is the same workflow as Keep Improving; only
        `continuous.on` differs."""
        state = self._state()
        io = RecordingIO()
        relay.dispatch_command(state, "/objective ship the docs", io)
        self.assertEqual(state["supervisor_goal"], "ship the docs")
        self.assertIn("Next objective set", self._notes(io))
        for i in state["pending"]:
            self.assertIn("ship the docs", state["pending"][i][-1])

    def test_a_keep_improving_run_still_accepts_it(self):
        state = self._state(continuous=True, name="k")
        relay.dispatch_command(state, "/objective ship the docs",
                               RecordingIO())
        self.assertEqual(state["supervisor_goal"], "ship the docs")
        self.assertEqual(state["continuous"]["objectives"][-1],
                         "ship the docs")

    def test_a_room_with_no_supervisor_is_refused_by_name(self):
        state = self._state(name="plain")
        state["mode"] = "round_robin"
        io = RecordingIO()
        relay.dispatch_command(state, "/objective ship the docs", io)
        self.assertIn("only means something in a conversation with a "
                      "Supervisor", self._notes(io))
        self.assertEqual(state["supervisor_goal"], "make the thing better")

    def test_it_gates_on_the_same_test_the_manager_gates_on(self):
        """`state["mode"] == "supervisor"` is what supervise_next_wave,
        replan_failed_workstreams, note_unfinished_supervision and
        supervisor_status all read. Gating this one on the orchestration
        workflow instead would reintroduce the divergence."""
        state = self._state(name="mode")
        state["orchestration"] = relay.normalize_orchestration(
            mode="round_robin")
        io = RecordingIO()
        relay.dispatch_command(state, "/objective from the mode", io)
        self.assertEqual(state["supervisor_goal"], "from the mode")

    def test_an_empty_argument_says_how_to_use_it(self):
        state = self._state(name="empty")
        io = RecordingIO()
        relay.dispatch_command(state, "/objective", io)
        self.assertIn("Usage: /objective", self._notes(io))
        self.assertEqual(state["supervisor_goal"], "make the thing better")

    def test_an_ordinary_run_has_no_objectives_list_to_append_to(self):
        """`state["continuous"]` is None outside Keep Improving, and the old
        code did an unguarded setdefault on it."""
        state = self._state(name="nocont")
        relay.dispatch_command(state, "/objective ship it", RecordingIO())
        self.assertIsNone(state["continuous"])

    # ---- the reset --------------------------------------------------------
    def test_it_gives_the_manager_a_fresh_wave_budget(self):
        state = self._state(name="waves")
        state["supervisor_waves"] = relay.SUPERVISOR_MAX_WAVES
        state["supervisor_capped"] = True
        relay.dispatch_command(state, "/objective something new",
                               RecordingIO())
        self.assertEqual(state["supervisor_waves"], 0)
        self.assertNotIn("supervisor_capped", state)

    def test_a_capped_manager_can_actually_run_again(self):
        """Without the reset, supervise_next_wave returns "idle" at every
        barrier for the rest of the run — silently."""
        state = self._state(name="capped")
        state["supervisor_waves"] = relay.SUPERVISOR_MAX_WAVES
        state["supervisor_capped"] = True
        # a DRAINED board (plan_drained is False with no tasks at all), so the
        # only thing standing between the manager and a review is the cap
        state["workstreams"] = [
            {"id": "t1", "owner": 0, "brief": "done", "files": [], "deps": [],
             "status": "done", "started_ts": None, "verified": None,
             "replans": 0}]
        calls = []
        real = relay.build_supervisor
        relay.build_supervisor = lambda st: calls.append(1) or _Stub()
        self.addCleanup(lambda: setattr(relay, "build_supervisor", real))
        self.assertEqual(relay.supervise_next_wave(state, RecordingIO()),
                         "idle")
        self.assertEqual(calls, [], "a capped manager should not be asked")
        relay.dispatch_command(state, "/objective something new",
                               RecordingIO())
        relay.supervise_next_wave(state, RecordingIO())
        self.assertEqual(len(calls), 1,
                         "the manager was still capped after a re-target")

    def test_the_wave_index_keeps_climbing(self):
        """The UI cuts its collapsible wave boxes on the INDEX; restarting it
        would fold the new objective into the old one's box."""
        state = self._state(name="index")
        relay.dispatch_command(state, "/objective new one", RecordingIO())
        self.assertEqual(state["supervisor_wave_index"], 3)

    def test_it_clears_the_planning_latch(self):
        state = self._state(name="latch")
        state["supervisor_plan_attempted"] = True
        relay.dispatch_command(state, "/objective new one", RecordingIO())
        self.assertFalse(state["supervisor_plan_attempted"])

    def test_it_does_not_touch_the_board(self):
        """It runs at a drain site, and in run_parallel that is MID-ROUND with
        seat threads alive — where supervise_next_wave's barrier-only contract
        says nothing may re-plan or archive."""
        state = self._state(name="board")
        board = [{"id": "t1", "owner": 0, "brief": "a", "files": [],
                  "deps": [], "status": "active", "started_ts": None,
                  "verified": None, "replans": 0}]
        state["workstreams"] = board
        relay.dispatch_command(state, "/objective new one", RecordingIO())
        self.assertIs(state["workstreams"], board)
        self.assertEqual(board[0]["status"], "active")

    # ---- the goal chip ----------------------------------------------------
    def test_it_records_a_trace_entry_carrying_the_new_goal(self):
        """The UI updates its Master goal chip from a trace entry's `goal`."""
        state = self._state(name="trace")
        io = RecordingIO()
        relay.dispatch_command(state, "/objective ship the docs", io)
        entries = [p["entry"] for e, p in io.events if e == "supervisor"]
        self.assertTrue(entries, "no trace entry was recorded")
        self.assertEqual(entries[-1]["type"], "objective_set")
        self.assertEqual(entries[-1]["goal"], "ship the docs")

    def test_session_summary_publishes_the_managers_goal_apart_from_the_topic(self):
        state = self._state(name="summary")
        state["supervisor_goal"] = "ship the docs"
        state["store"].save(state)
        s = relay.session_summary(state["store"].dir)
        self.assertEqual(s["supervisor_goal"], "ship the docs")
        self.assertEqual(s["goal"], "make the thing better")

    # ---- the watchdog's own repair ---------------------------------------
    def test_the_replan_remedy_gets_the_same_fresh_budget(self):
        """Fixing only /objective would leave the watchdog unable to revive a
        capped run: it re-plans, and every later barrier still returns idle."""
        state = self._state(continuous=True, name="remedy")
        state["supervisor_waves"] = relay.SUPERVISOR_MAX_WAVES
        state["supervisor_capped"] = True
        real = relay.plan_workstreams
        relay.plan_workstreams = lambda st, io, goal=None: []
        self.addCleanup(lambda: setattr(relay, "plan_workstreams", real))
        relay.apply_remedy(state, RecordingIO(), "replan", "")
        self.assertEqual(state["supervisor_waves"], 0)
        self.assertNotIn("supervisor_capped", state)

    def test_the_helper_refuses_an_empty_goal(self):
        state = self._state(name="blank")
        state["supervisor_waves"] = 4
        self.assertEqual(relay.retarget_supervisor(state, "   "), "")
        self.assertEqual(state["supervisor_goal"], "make the thing better")
        self.assertEqual(state["supervisor_waves"], 4,
                         "a refused re-target must change nothing")


class _Stub:
    """A supervisor side call that answers nothing useful — enough to prove it
    was ASKED, which is the whole assertion."""
    last_usage = None
    name = "Supervisor"

    def turn(self, message, on_activity=None):
        return "nothing to add"


if __name__ == "__main__":
    unittest.main(verbosity=2)
