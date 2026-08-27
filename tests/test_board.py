"""W2.2 — the Supervisor board review: Josh gates a wave before it dispatches.

The plan calls one thing MANDATORY and it is the state key. `state["plan"]`
belongs to Plan Mode: `start_plan` writes it with no mode check at all, and
the record persists AND rehydrates — so a supervisor run can arrive already
holding `{"phase": "drafting"}`, slip past an awaiting-only guard, and since
nothing on the supervisor path ever calls `approve_plan`, leave every seat
read-only for the rest of the run. Every file-claiming task would then fail
verification while the card read "Executing". Two features, two records.

The other rules, each of which cost something when it was missing somewhere
else in this repo:

  * The gate runs BEFORE `assign_workstreams`, which rewrites `owner`
    (capability_gate) and appends to `deps` (serialize_conflicts) as it
    dispatches. A review after it reviews work already in flight.
  * `supervise_next_wave` appends its fresh tasks IN PLACE, so a refusal must
    REMOVE them — a task left pending keeps `plan_drained` False forever and
    the manager never gets the floor again.
  * `merge_board_edits` WHITELISTS. `replans` limits a failed task to one
    repair attempt and `commit` binds it to its checkpoint; a merge that
    rebuilt each task from the card's fields would grant a repeat of both.
  * An owner is a slot id — a NUMBER — and an HTML <select>.value is a
    STRING. `assign_workstreams` tests `t["owner"] not in ids`.
  * The gate composes a deadline. Blocking the parallel barrier with no
    expiry reproduces the documented 2026-08-22 wedge, where the accumulated
    clock, the spend cap and the scheduled watchdog are ALL checked at that
    barrier and none of them could fire.
  * `CLIIO.ask_human` subscripts `payload['asker']`.

Run:  python tests/test_board.py
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from test_loop import RecordingIO, build_state


def task(tid, owner=0, brief="do a thing", **extra):
    t = {"id": tid, "owner": owner, "brief": brief, "files": [], "deps": [],
         "status": "pending", "started_ts": None, "verified": None,
         "replans": 0}
    t.update(extra)
    return t


class AnsweringIO(RecordingIO):
    """A front end that answers the board gate. `answer` may be a dict (the
    app's card) or a string (the CLI prompt); None is nobody there."""

    def __init__(self, answer=None, human_script=None):
        super().__init__(human_script)
        self.answer = answer
        self.asked = []

    def ask_human(self, payload, abort=None):
        self.asked.append(payload)
        return self.answer


class MergeTests(unittest.TestCase):
    def test_it_keeps_every_field_the_scheduler_owns(self):
        original = task("t1", brief="original", status="active",
                        started_ts=99.0, replans=1, commit="abc123",
                        verified={"ok": True}, files=["a.py"], deps=["t0"],
                        report="a claim", executed_by="Claude",
                        findings=[{"severity": "major"}], attempts=2)
        kept, dropped = merge = relay.merge_board_edits(
            [original], [{"id": "t1", "brief": "reworded"}], slot_ids=[0, 1])
        self.assertEqual(dropped, [])
        got = kept[0]
        self.assertEqual(got["brief"], "reworded")
        for key in ("status", "started_ts", "replans", "commit", "verified",
                    "files", "deps", "report", "executed_by", "findings",
                    "attempts"):
            self.assertEqual(got[key], original[key],
                             "%s did not survive the merge" % key)

    def test_blanking_replans_would_grant_a_second_repair(self):
        """`replans` is the selection predicate in replan_failed_workstreams;
        `commit` is the one in the checkpoint binding."""
        t = task("t1", replans=1, commit="abc")
        kept, _ = relay.merge_board_edits(
            [t], [{"id": "t1", "replans": 0, "commit": None}], slot_ids=[0])
        self.assertEqual(kept[0]["replans"], 1)
        self.assertEqual(kept[0]["commit"], "abc")

    def test_files_and_deps_are_not_editable(self):
        """They are the workstream isolation contract, and the only validator
        for a file claim parses a directive STRING, not a dict."""
        t = task("t1", files=["src/a.py"], deps=["t0"])
        kept, _ = relay.merge_board_edits(
            [t], [{"id": "t1", "files": ["../../etc/passwd"], "deps": []}],
            slot_ids=[0])
        self.assertEqual(kept[0]["files"], ["src/a.py"])
        self.assertEqual(kept[0]["deps"], ["t0"])

    def test_an_owner_comes_back_as_the_slot_ids_own_type(self):
        """assign_workstreams tests `t["owner"] not in ids` and would fail the
        task with "no seat '1' in this conversation"."""
        kept, _ = relay.merge_board_edits(
            [task("t1", owner=0)], [{"id": "t1", "owner": "1"}],
            slot_ids=[0, 1])
        self.assertEqual(kept[0]["owner"], 1)
        self.assertIsInstance(kept[0]["owner"], int)

    def test_an_owner_nobody_here_can_be_is_ignored(self):
        kept, _ = relay.merge_board_edits(
            [task("t1", owner=0)], [{"id": "t1", "owner": "9"}],
            slot_ids=[0, 1])
        self.assertEqual(kept[0]["owner"], 0)

    def test_excluding_a_task_drops_it_and_says_which(self):
        kept, dropped = relay.merge_board_edits(
            [task("t1"), task("t2")],
            [{"id": "t1", "include": True}, {"id": "t2", "include": False}],
            slot_ids=[0])
        self.assertEqual([t["id"] for t in kept], ["t1"])
        self.assertEqual(dropped, ["t2"])

    def test_dropping_a_task_strips_it_from_its_dependents(self):
        """Dropping is not editing. A surviving task that still depends on an
        id nobody will deliver stays `blocked` forever, so the board never
        drains and the manager never gets the floor again."""
        kept, dropped = relay.merge_board_edits(
            [task("t1"), task("t2", deps=["t1", "t0"])],
            [{"id": "t1", "include": False}, {"id": "t2"}], slot_ids=[0])
        self.assertEqual(dropped, ["t1"])
        self.assertEqual(kept[0]["deps"], ["t0"],
                         "the dropped task is still a dependency")

    def test_an_untouched_board_keeps_every_dependency(self):
        kept, dropped = relay.merge_board_edits(
            [task("t1"), task("t2", deps=["t1"])],
            [{"id": "t1"}, {"id": "t2"}], slot_ids=[0])
        self.assertEqual(dropped, [])
        self.assertEqual(kept[1]["deps"], ["t1"])

    def test_the_order_is_joshs(self):
        kept, _ = relay.merge_board_edits(
            [task("t1"), task("t2"), task("t3")],
            [{"id": "t3"}, {"id": "t1"}, {"id": "t2"}], slot_ids=[0])
        self.assertEqual([t["id"] for t in kept], ["t3", "t1", "t2"])

    def test_an_id_nobody_proposed_is_ignored(self):
        kept, _ = relay.merge_board_edits(
            [task("t1")], [{"id": "t1"}, {"id": "smuggled"}], slot_ids=[0])
        self.assertEqual([t["id"] for t in kept], ["t1"])

    def test_a_repeated_id_is_taken_once(self):
        kept, _ = relay.merge_board_edits(
            [task("t1")], [{"id": "t1"}, {"id": "t1"}], slot_ids=[0])
        self.assertEqual(len(kept), 1)

    def test_an_empty_retitle_leaves_the_original(self):
        kept, _ = relay.merge_board_edits(
            [task("t1", brief="original")], [{"id": "t1", "brief": "   "}],
            slot_ids=[0])
        self.assertEqual(kept[0]["brief"], "original")

    def test_junk_edits_do_not_raise(self):
        kept, _ = relay.merge_board_edits(
            [task("t1")], ["not a dict", None, 7, {"nope": 1}], slot_ids=[0])
        self.assertEqual(kept, [])


class BoardGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-board-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _state(self, review=True, name="s"):
        state = build_state(os.path.join(self.tmp, name), [["a"], ["b"]],
                            labels=["Cee", "Dee"])
        state["providers"] = ["claude", "claude"]
        state["mode"] = "supervisor"
        state["topic"] = "make it better"
        state["supervisor_goal"] = "make it better"
        state["supervisor_trace"] = []
        state["board_review"] = review
        state["continuous"] = None
        return state

    def test_it_is_a_no_op_when_the_gate_is_off(self):
        state = self._state(review=False)
        io = AnsweringIO()
        tasks = [task("t1")]
        self.assertEqual(relay.board_gate(state, io, tasks), tasks)
        self.assertEqual(io.asked, [], "it asked with the gate off")
        self.assertIsNone(state.get("board"))

    def test_it_never_uses_plan_modes_record(self):
        """MANDATORY. start_plan writes state["plan"] with no mode check and
        it rehydrates, so a supervisor chat can arrive already holding one."""
        state = self._state()
        state["plan"] = {"phase": "drafting", "goal": "someone else's",
                         "tasks": [], "revision": 1}
        relay.board_gate(state, AnsweringIO({"approved": True}), [task("t1")])
        self.assertEqual(state["plan"]["phase"], "drafting",
                         "the board wrote into Plan Mode's record")
        self.assertEqual(state["plan"]["tasks"], [])
        self.assertIsNotNone(state.get("board"))

    def test_an_approval_dispatches_joshs_edited_list(self):
        state = self._state()
        io = AnsweringIO({"approved": True, "tasks": [
            {"id": "t2", "brief": "second, first", "owner": "1"},
            {"id": "t1", "include": False},
        ]})
        out = relay.board_gate(state, io, [task("t1"), task("t2")])
        self.assertEqual([t["id"] for t in out], ["t2"])
        self.assertEqual(out[0]["brief"], "second, first")
        self.assertEqual(out[0]["owner"], 1)
        self.assertEqual(state["board"]["phase"], "approved")

    def test_approving_without_touching_anything_keeps_the_whole_board(self):
        """The CLI answers with a STRING, and a card can approve with no edits
        at all — neither may be read as "drop everything"."""
        state = self._state()
        for answer in ("Approve & dispatch", {"approved": True}):
            state["board"] = None
            out = relay.board_gate(state, AnsweringIO(answer),
                                   [task("t1"), task("t2")])
            self.assertEqual([t["id"] for t in out], ["t1", "t2"], answer)

    def test_a_refusal_dispatches_nothing_and_carries_the_reason(self):
        state = self._state()
        io = AnsweringIO({"approved": False, "feedback": "too broad"})
        self.assertEqual(relay.board_gate(state, io, [task("t1")]), [])
        self.assertEqual(state["board"]["phase"], "declined")
        self.assertEqual(state["board_feedback"], "too broad")
        # a real refusal is an explicit retry: the manager plans again
        self.assertFalse(state["supervisor_plan_attempted"])

    def test_silence_is_never_approval_and_stops_asking(self):
        """The same rule an unanswered [[ASK]] follows. A refusal means "plan
        again"; nobody answering means "Josh is not here"."""
        state = self._state()
        self.assertEqual(relay.board_gate(state, AnsweringIO(None),
                                          [task("t1")]), [])
        self.assertEqual(state["board"]["phase"], "declined")
        self.assertTrue(state["supervisor_plan_attempted"],
                        "an unanswered board would re-plan forever")

    def test_a_typed_cli_refusal_is_the_feedback(self):
        """The console prompt invites "a number or your own answer". Reading
        the note only out of a dict meant the sentence Josh typed went
        nowhere and the manager re-planned the identical board — measured
        live on 2026-08-27."""
        state = self._state(name="cli-note")
        io = AnsweringIO("the plan is too big — split it")
        self.assertEqual(relay.board_gate(state, io, [task("t1")]), [])
        self.assertEqual(state["board_feedback"],
                         "the plan is too big — split it")
        self.assertFalse(state["supervisor_plan_attempted"])

    def test_a_dict_refusal_with_no_note_is_still_a_refusal(self):
        state = self._state(name="no-note")
        self.assertEqual(relay.board_gate(state, AnsweringIO(
            {"approved": False}), [task("t1")]), [])
        self.assertEqual(state["board_feedback"], "")

    def test_a_refusal_is_flagged_apart_from_its_note(self):
        """The re-plan decision keys on the flag, not on the text: inferring
        "was this a refusal?" from `board_feedback` made "send it back" with
        nothing typed behave exactly like a dead planner."""
        state = self._state(name="flagged")
        relay.board_gate(state, AnsweringIO({"approved": False}), [task("t1")])
        self.assertTrue(state["board_declined"])
        self.assertEqual(state["board_feedback"], "")

    def test_an_unanswered_board_latches_its_own_flag(self):
        state = self._state(name="unanswered")
        relay.board_gate(state, AnsweringIO(None), [task("t1")])
        self.assertTrue(state["board_unanswered"])
        self.assertNotIn("board_declined", state)

    def test_an_approval_clears_both_latches(self):
        state = self._state(name="both")
        state["board_declined"] = True
        state["board_unanswered"] = True
        relay.board_gate(state, AnsweringIO({"approved": True}), [task("t1")])
        self.assertNotIn("board_declined", state)
        self.assertNotIn("board_unanswered", state)

    def test_a_trace_entry_carries_the_WHOLE_board(self):
        """appendSupervisorTrace replaces the task map from any entry carrying
        tasks, so a wave-sized list erases every settled task from the panel —
        the same reason supervise_next_wave traces the whole plan."""
        state = self._state(name="trace-all")
        state["workstreams"] = [task("old", status="done")]
        io = AnsweringIO({"approved": True})
        relay.board_gate(state, io, [task("new")])
        entry = [p["entry"] for e, p in io.events if e == "supervisor"][-1]
        self.assertEqual([t["id"] for t in entry["tasks"]], ["old", "new"])

    def test_the_payload_carries_what_the_cli_front_end_subscripts(self):
        """CLIIO.ask_human does payload['asker'] — a payload without it raises
        KeyError inside the front end instead of asking anybody."""
        state = self._state()
        io = AnsweringIO({"approved": True})
        relay.board_gate(state, io, [task("t1")])
        self.assertIn("asker", io.asked[0])
        self.assertTrue(io.asked[0]["asker"])
        self.assertEqual(io.asked[0]["kind"], "board")
        self.assertTrue(io.asked[0]["options"])

    def test_the_wait_has_a_deadline_that_actually_expires(self):
        """A gate that blocks the parallel barrier forever is the documented
        2026-08-22 wedge: the clock, the spend cap and the watchdog are all
        checked AT that barrier. Asserting only that the abort is CALLABLE is
        satisfied by `lambda: False` — i.e. by no deadline at all — so this
        drives the clock past it."""
        state = self._state(name="deadline")
        seen = {}

        class Recording(AnsweringIO):
            def ask_human(self, payload, abort=None):
                seen["abort"] = abort
                return {"approved": True}

        old = relay.BOARD_WAIT_MAX
        relay.BOARD_WAIT_MAX = 0.05
        self.addCleanup(lambda: setattr(relay, "BOARD_WAIT_MAX", old))
        relay.board_gate(state, Recording(), [task("t1")])
        self.assertTrue(callable(seen["abort"]), "the gate can never expire")
        self.assertFalse(seen["abort"](), "it expired before it was asked")
        time.sleep(0.1)
        self.assertTrue(seen["abort"](), "the deadline never arrives")

    def test_the_callers_own_abort_still_wins(self):
        state = self._state(name="caller-abort")
        seen = {}

        class Recording(AnsweringIO):
            def ask_human(self, payload, abort=None):
                seen["abort"] = abort
                return {"approved": True}

        relay.board_gate(state, Recording(), [task("t1")],
                         abort=lambda: True)
        self.assertTrue(seen["abort"]())

    def test_it_publishes_a_card_and_a_trace_entry(self):
        state = self._state()
        io = AnsweringIO({"approved": True})
        relay.board_gate(state, io, [task("t1")])
        boards = [p for e, p in io.events if e == "board"]
        self.assertEqual([b["phase"] for b in boards], ["proposed", "approved"])
        self.assertTrue(boards[0]["seats"], "the card has no seats to reassign to")
        traces = [p["entry"] for e, p in io.events if e == "supervisor"]
        self.assertEqual(traces[-1]["type"], "board_reviewed")

    def test_the_card_names_which_seats_can_actually_write(self):
        """Hard-coded, not re-derived from workstream_writers: an expectation
        computed by the code under test agrees with it however wrong both are.
        gemini is not a FILE_WRITER_PROVIDER — agy ignores the process cwd for
        file writes."""
        state = self._state(name="writes")
        state["providers"] = ["claude", "gemini"]
        self.assertNotIn("gemini", relay.FILE_WRITER_PROVIDERS)
        io = AnsweringIO({"approved": True})
        relay.board_gate(state, io, [task("t1")])
        seats = [p for e, p in io.events if e == "board"][0]["seats"]
        self.assertEqual([(s["id"], s["name"], s["writes"]) for s in seats],
                         [(0, "Cee", True), (1, "Dee", False)])

    def test_an_approval_that_keeps_nothing_is_a_refusal(self):
        """Otherwise it dispatches an empty wave and the manager re-plans the
        same board forever, with nothing to plan against."""
        state = self._state(name="empty")
        io = AnsweringIO({"approved": True, "tasks": []})
        self.assertEqual(relay.board_gate(state, io, [task("t1")]), [])
        self.assertEqual(state["board"]["phase"], "declined")
        self.assertIn("kept none", state["board_feedback"])

    def test_each_wave_gets_its_own_revision(self):
        state = self._state()
        relay.board_gate(state, AnsweringIO({"approved": True}), [task("t1")])
        first = state["board"]["revision"]
        relay.board_gate(state, AnsweringIO({"approved": True}), [task("t2")])
        self.assertEqual(state["board"]["revision"], first + 1)


class StubSide:
    """A supervisor side call with a scripted reply and no CLI behind it."""
    last_usage = None
    name = "Supervisor"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def turn(self, message, on_activity=None):
        self.prompts.append(message)
        return self.replies.pop(0) if self.replies else "(out of script)"


class DispatchTests(unittest.TestCase):
    """The gate where it actually runs: before assign_workstreams, in BOTH
    the initial plan and every rolling wave."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-board-run-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.assigned = []
        real = relay.assign_workstreams
        relay.assign_workstreams = lambda st, io: self.assigned.append(
            [t["id"] for t in (st.get("workstreams") or [])
             if t.get("status") == "pending"])
        self.addCleanup(lambda: setattr(relay, "assign_workstreams", real))

    def _state(self, name="s", review=True):
        state = build_state(os.path.join(self.tmp, name), [["a"], ["b"]],
                            labels=["Cee", "Dee"])
        state["providers"] = ["claude", "claude"]
        state["mode"] = "supervisor"
        state["topic"] = "make it better"
        state["supervisor_goal"] = "make it better"
        state["supervisor_trace"] = []
        state["board_review"] = review
        state["continuous"] = None
        return state

    def _side(self, stub):
        real = relay.build_supervisor
        relay.build_supervisor = lambda st: stub
        self.addCleanup(lambda: setattr(relay, "build_supervisor", real))
        return stub

    PLAN = ("Here is the plan.\n"
            "[[TASK: t1 | owner=0 | files=a.py | write a.py]]\n"
            "[[TASK: t2 | owner=1 | files=b.py | write b.py]]")

    # ---- the initial plan ------------------------------------------------
    def test_the_plan_waits_for_josh_before_it_dispatches(self):
        state = self._state()
        self._side(StubSide(self.PLAN))
        io = AnsweringIO({"approved": True, "tasks": [{"id": "t1"}]})
        out = relay.plan_workstreams(state, io, goal="make it better")
        self.assertEqual([t["id"] for t in out], ["t1"])
        self.assertEqual(self.assigned, [["t1"]],
                         "the dropped task was dispatched anyway")
        self.assertEqual([t["id"] for t in state["workstreams"]], ["t1"])

    def test_a_refused_plan_dispatches_nothing_at_all(self):
        state = self._state(name="refused")
        self._side(StubSide(self.PLAN))
        io = AnsweringIO({"approved": False, "feedback": "too broad"})
        self.assertEqual(relay.plan_workstreams(state, io, goal="g"), [])
        self.assertEqual(self.assigned, [], "a refused plan still dispatched")
        self.assertEqual(state["workstreams"], [])

    def test_the_next_plan_is_told_why_the_last_one_came_back(self):
        """The manager is a stateless side call, so the next prompt is the
        only channel the feedback has."""
        state = self._state(name="feedback")
        stub = self._side(StubSide(self.PLAN, self.PLAN))
        relay.plan_workstreams(state, AnsweringIO(
            {"approved": False, "feedback": "split t1 in two"}), goal="g")
        relay.plan_workstreams(state, AnsweringIO({"approved": True}),
                               goal="g")
        self.assertNotIn("split t1 in two", stub.prompts[0])
        self.assertIn("split t1 in two", stub.prompts[1])

    def test_an_approval_clears_the_note(self):
        state = self._state(name="cleared")
        # enough script for the re-plan loop: a refusal now costs
        # BOARD_REPLAN_MAX planning calls before it gives up
        self._side(StubSide(*([self.PLAN] * 8)))
        relay.plan_workstreams(state, AnsweringIO({"approved": False,
                                                  "feedback": "no"}),
                               goal="g")
        self.assertEqual(state["board_feedback"], "no")
        state["supervisor_plan_attempted"] = False
        relay.plan_workstreams(state, AnsweringIO({"approved": True}),
                               goal="g")
        self.assertNotIn("board_feedback", state)
        self.assertNotIn("board_declined", state)

    def test_sending_it_back_plans_again_in_the_SAME_run(self):
        """The auto-plan is a single pre-loop call and supervise_next_wave
        needs a non-empty drained board to get the floor — so without the
        re-plan loop a refused first board left the seats chatting for the
        rest of the run and the manager never planned again until a resume."""
        state = self._state(name="replan")
        stub = self._side(StubSide(self.PLAN, self.PLAN))
        answers = iter([{"approved": False, "feedback": "split t1"},
                        {"approved": True}])

        class Twice(AnsweringIO):
            def ask_human(self, payload, abort=None):
                self.asked.append(payload)
                return next(answers)

        io = Twice()
        out = relay.plan_workstreams(state, io, goal="g")
        self.assertEqual(len(io.asked), 2, "it only asked once")
        self.assertEqual([t["id"] for t in out], ["t1", "t2"])
        self.assertIn("split t1", stub.prompts[1])

    def test_sending_it_back_with_NO_reason_still_plans_again(self):
        """The loop used to decide "was this a refusal?" by whether a note
        existed, so an empty one behaved exactly like a dead planner — while
        the card said the Supervisor would plan again."""
        state = self._state(name="silent-refusal")
        stub = self._side(StubSide(self.PLAN, self.PLAN))
        answers = iter([{"approved": False}, {"approved": True}])

        class Twice(AnsweringIO):
            def ask_human(self, payload, abort=None):
                self.asked.append(payload)
                return next(answers)

        out = relay.plan_workstreams(state, Twice(), goal="g")
        self.assertEqual([t["id"] for t in out], ["t1", "t2"])
        self.assertEqual(len(stub.prompts), 2)

    def test_an_unanswered_wave_stops_the_rolling_manager(self):
        """supervise_next_wave does not read the plan latch, so without a flag
        of its own an absent Josh is asked once per wave, each time for the
        full deadline."""
        state = self._drained("wave-silent")
        stub = self._side(StubSide(self.WAVE, self.WAVE))
        self.assertEqual(relay.supervise_next_wave(state, AnsweringIO(None)),
                         "idle")
        self.assertTrue(state["board_unanswered"])
        self.assertEqual(relay.supervise_next_wave(state, AnsweringIO(None)),
                         "idle")
        self.assertEqual(len(stub.prompts), 1, "it asked the manager again")

    def test_a_fresh_objective_forgives_an_unanswered_board(self):
        """Josh typing /objective is proof he is back."""
        state = self._state(name="forgive")
        state["board_unanswered"] = True
        relay.retarget_supervisor(state, "something new")
        self.assertNotIn("board_unanswered", state)

    def test_a_board_nobody_answers_does_not_re_plan(self):
        """Silence means Josh is not here — asking again spends a real side
        call per attempt for nobody."""
        state = self._state(name="silent")
        stub = self._side(StubSide(self.PLAN, self.PLAN, self.PLAN))
        io = AnsweringIO(None)
        self.assertEqual(relay.plan_workstreams(state, io, goal="g"), [])
        self.assertEqual(len(io.asked), 1)
        self.assertEqual(len(stub.prompts), 1)

    def test_a_dead_planner_is_not_retried_either(self):
        state = self._state(name="dead")
        stub = self._side(StubSide("prose with no directives",
                                   "prose with no directives"))
        self.assertEqual(relay.plan_workstreams(state, AnsweringIO(
            {"approved": True}), goal="g"), [])
        self.assertEqual(len(stub.prompts), 1)

    def test_endless_refusals_give_up_and_say_so(self):
        state = self._state(name="giveup")
        stub = self._side(StubSide(*([self.PLAN] * 6)))
        io = AnsweringIO({"approved": False, "feedback": "no"})
        self.assertEqual(relay.plan_workstreams(state, io, goal="g"), [])
        self.assertEqual(len(stub.prompts), relay.BOARD_REPLAN_MAX)
        self.assertTrue(state["supervisor_plan_attempted"])
        notes = " ".join(p.get("text", "")
                         for e, p in io.events if e == "status")
        self.assertIn("came back", notes)

    def test_a_refused_wave_tells_the_next_review_why(self):
        """Both prompt sites, or the feedback works for the first board and
        silently not for any wave after it."""
        state = self._state(name="wave-note")
        state["workstreams"] = [task("t1", status="done")]
        state["supervisor_waves"] = 0
        stub = self._side(StubSide(self.WAVE, self.WAVE))
        relay.supervise_next_wave(state, AnsweringIO(
            {"approved": False, "feedback": "wrong seat"}))
        relay.supervise_next_wave(state, AnsweringIO({"approved": True}))
        self.assertNotIn("wrong seat", stub.prompts[0])
        self.assertIn("wrong seat", stub.prompts[1])

    def test_with_the_gate_off_nothing_pauses(self):
        state = self._state(name="off", review=False)
        self._side(StubSide(self.PLAN))
        io = AnsweringIO(None)
        out = relay.plan_workstreams(state, io, goal="g")
        self.assertEqual([t["id"] for t in out], ["t1", "t2"])
        self.assertEqual(io.asked, [])
        self.assertEqual(self.assigned, [["t1", "t2"]])

    # ---- the rolling wave -------------------------------------------------
    WAVE = ("Next.\n[[TASK: t9 | owner=0 | files=c.py | write c.py]]")

    def _drained(self, name):
        state = self._state(name=name)
        state["workstreams"] = [task("t1", status="done")]
        state["supervisor_waves"] = 0
        return state

    def test_a_wave_waits_for_josh_too(self):
        state = self._drained("wave")
        self._side(StubSide(self.WAVE))
        io = AnsweringIO({"approved": True})
        self.assertEqual(relay.supervise_next_wave(state, io), "assigned")
        self.assertEqual([t["id"] for t in state["workstreams"]],
                         ["t1", "t9"])
        self.assertEqual(self.assigned, [["t9"]])

    def test_a_refused_wave_is_REMOVED_from_the_board(self):
        """supervise_next_wave appends in place. A task left pending keeps
        plan_drained False forever, and the manager never gets the floor
        again — so skipping the dispatch is not enough."""
        state = self._drained("wave-refused")
        self._side(StubSide(self.WAVE))
        io = AnsweringIO({"approved": False, "feedback": "not yet"})
        self.assertEqual(relay.supervise_next_wave(state, io), "idle")
        self.assertEqual([t["id"] for t in state["workstreams"]], ["t1"])
        self.assertTrue(relay.plan_drained(state),
                        "the refused wave left the board un-drainable")
        self.assertEqual(self.assigned, [])

    def test_settled_work_survives_a_refused_wave(self):
        state = self._drained("wave-keep")
        state["workstreams"][0]["verified"] = {"ok": True}
        self._side(StubSide(self.WAVE))
        relay.supervise_next_wave(state, AnsweringIO({"approved": False}))
        self.assertEqual(state["workstreams"][0]["verified"], {"ok": True})

    def test_joshs_order_for_a_new_wave_is_kept(self):
        state = self._drained("wave-order")
        self._side(StubSide(
            "Next.\n"
            "[[TASK: t8 | owner=0 | files=x.py | first]]\n"
            "[[TASK: t9 | owner=0 | files=y.py | second]]"))
        io = AnsweringIO({"approved": True,
                          "tasks": [{"id": "t9"}, {"id": "t8"}]})
        relay.supervise_next_wave(state, io)
        self.assertEqual([t["id"] for t in state["workstreams"]],
                         ["t1", "t9", "t8"])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-board-meta-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_switch_and_the_card_survive_a_reopen(self):
        """Anything SessionStore.save reads off state has to be put BACK on
        state by rehydrate, or a resumed chat writes the default over the real
        value on its very next save."""
        state = build_state(os.path.join(self.tmp, "s"), [["a"], ["b"]])
        state["board_review"] = True
        state["board"] = {"id": "board-1", "revision": 1, "phase": "approved",
                          "tasks": [task("t1")]}
        state["board_feedback"] = "too broad"
        state["store"].save(state)
        meta = relay.read_meta(state["store"].dir)
        self.assertTrue(meta["board_review"])
        self.assertEqual(meta["board"]["id"], "board-1")
        back = relay.rehydrate(meta)
        self.assertTrue(back["board_review"])
        self.assertEqual(back["board"]["id"], "board-1")
        self.assertEqual(back["board_feedback"], "too broad")
        s = relay.session_summary(state["store"].dir)
        self.assertTrue(s["board_review"])
        self.assertEqual(s["board"]["phase"], "approved")

    def test_a_chat_that_predates_it_reads_as_off(self):
        state = build_state(os.path.join(self.tmp, "old"), [["a"], ["b"]])
        state["store"].save(state)
        meta = relay.read_meta(state["store"].dir)
        meta.pop("board_review", None)
        meta.pop("board", None)
        self.assertFalse(relay.rehydrate(meta)["board_review"])
        self.assertIsNone(relay.rehydrate(meta)["board"])


class BridgeTests(unittest.TestCase):
    """The app half. A safety-shaped control needs a test AT THE BRIDGE, not
    only at the engine — "registered" is not "callable"."""

    def setUp(self):
        import app as app_mod
        self.app = app_mod
        self.tmp = tempfile.mkdtemp(prefix="alloy-board-bridge-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.api = app_mod.Api()
        self.api._window = type("W", (), {"evaluate_js": lambda *a: None})()
        self.run = app_mod.Run()
        self.run.state = {"workspace": self.tmp,
                          "board": {"id": "board-1", "revision": 1,
                                    "phase": "proposed", "qid": "abc123",
                                    "tasks": [task("t1")]}}
        self.api._runs.adopt(self.run, "chat-a", focus=True)
        self.q = __import__("queue").Queue()
        self.api._ask_waiters["abc123"] = self.q

    def test_it_answers_the_question_the_loop_is_blocked_on(self):
        """It must not flip the board itself: doing that directly is the
        deadlock the plan card's own audit found — the flags changed, the card
        said done, and the thread stayed asleep in ask_human forever."""
        r = self.api.approve_board("chat-a", "board-1",
                                   {"revision": 1, "approved": True,
                                    "tasks": [{"id": "t1"}]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.q.get_nowait(),
                         {"approved": True, "tasks": [{"id": "t1"}],
                          "feedback": ""})
        self.assertEqual(self.run.state["board"]["phase"], "proposed",
                         "the bridge changed the board itself")

    def test_a_refusal_carries_its_note(self):
        self.api.approve_board("chat-a", "board-1",
                               {"revision": 1, "approved": False,
                                "feedback": "too broad"})
        answer = self.q.get_nowait()
        self.assertFalse(answer["approved"])
        self.assertEqual(answer["feedback"], "too broad")

    def test_a_stale_card_is_refused(self):
        for payload, board_id in (({"revision": 0, "approved": True}, None),
                                  ({"revision": 1, "approved": True},
                                   "board-99")):
            r = self.api.approve_board("chat-a", board_id, payload)
            self.assertFalse(r["ok"], (board_id, payload))
            self.assertIn("out of date", r["error"])
        self.assertTrue(self.q.empty())

    def test_it_refuses_when_nothing_is_waiting(self):
        self.run.state["board"]["qid"] = None
        r = self.api.approve_board("chat-a", "board-1", {"approved": True})
        self.assertFalse(r["ok"])
        self.assertIn("No board", r["error"])

    def test_it_refuses_an_unknown_chat_rather_than_the_focused_one(self):
        r = self.api.approve_board("no-such-chat", "board-1",
                                   {"approved": True})
        self.assertFalse(r["ok"])
        self.assertTrue(self.q.empty())

    def test_the_cfg_key_reaches_the_state(self):
        import relay as relay_mod
        seen = {}
        real = relay_mod.run_rounds
        self.app.run_rounds = lambda st, io: seen.update(st) or "cap"
        self.addCleanup(lambda: setattr(self.app, "run_rounds", real))
        old_types = dict(relay_mod.AGENT_TYPES)
        old_dir = self.app.SESSIONS_DIR
        self.app.SESSIONS_DIR = self.tmp
        self.addCleanup(lambda: setattr(self.app, "SESSIONS_DIR", old_dir))
        self.addCleanup(lambda: (relay_mod.AGENT_TYPES.clear(),
                                 relay_mod.AGENT_TYPES.update(old_types)))

        class Fake(relay_mod.Agent):
            name = "Claude"
            cli = "fake"

            def turn(self, message, on_activity=None):
                self.session_id = "fake-" + str(self.uid)
                return "ok"

        relay_mod.AGENT_TYPES["claude"] = Fake
        api = self.app.Api()
        api._window = type("W", (), {"evaluate_js": lambda *a: None})()
        api._conversation({"opener": "hi", "turns": 1, "board_review": True,
                           "seats": [{"id": 0, "provider": "claude",
                                      "enabled": True}]})
        api._emit_q.join()
        self.assertTrue(seen.get("board_review"),
                        "the checkbox never reached the engine")


class MarkupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(os.path.dirname(here), "ui", "index.html"),
                  encoding="utf-8") as f:
            cls.html = f.read()

    def test_the_board_card_is_its_own_card(self):
        self.assertIn("data-active-board", self.html)
        self.assertIn('card.dataset.activeBoard = "1";', self.html)
        # ...and never writes into Plan Mode's
        self.assertNotIn("run.plan = payload; renderBoard", self.html)

    def test_it_offers_no_file_or_dependency_editing(self):
        """The engine's whitelist accepts brief and owner only; offering more
        would be a control that silently does nothing."""
        i = self.html.index("function renderBoard(")
        j = self.html.index("function openBoardNote(")
        body = self.html[i:j]
        self.assertNotIn('"plan-files"', body)
        self.assertNotIn("task.deps", body)

    def test_the_refusal_note_uses_classes_that_exist(self):
        """The first draft invented `.note-editor` / `.note-actions`, which
        appear nowhere in the stylesheet, so the textarea rendered as a raw
        browser default: black on white, 168px wide, inside a dark card.
        Only getComputedStyle on a real page can see that — this guard is the
        cheap half."""
        i = self.html.index("function openBoardNote(")
        j = self.html.index("function mergePlanTasks(")
        body = self.html[i:j]
        for cls in ("msg-note-edit", "note-acts"):
            self.assertIn('"%s"' % cls, body)
            self.assertIn(".%s" % cls, self.html[:self.html.index("</style>")],
                          "%s is not in the stylesheet" % cls)
        self.assertNotIn('"note-editor"', body)
        self.assertNotIn('"note-actions"', body)

    def test_the_switch_is_in_both_cfg_builders(self):
        """The start cfg AND roomCfgFromStage: a saved room that dropped the
        gate would silently start an ungated run from a template with it."""
        self.assertEqual(
            self.html.count('board_review: $("boardReview").checked,'), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
