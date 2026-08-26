"""Token-free tests for the relay's own "I am working" indicator.

Seats have `thinking`; everything that is NOT a seat — the supervisor
planning, the project brief, the moderator, the verification gate, the
one-shot auto-title, spawned helpers and teams, and the app's own pre-flight
setup — used to run in total silence, so "before the models start" was
indistinguishable from a frozen window.

`relay.working(io, phase, ...)` is the one mechanism. What is worth pinning is
not that it emits, but the properties that make it trustworthy: it always
closes (a spinner that outlives its work is worse than none), it never breaks
the work it decorates, and its ids are unique so concurrent callers cannot
close each other's rows.

Run:  python tests/test_working.py
"""

import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import relay
from relay import working
from test_loop import RecordingIO, build_state


class StubSide:
    """What every side-call builder returns: one `turn`, one `last_usage`."""

    name = "Supervisor"
    last_usage = None
    session_id = None

    def __init__(self, reply="", raises=None):
        self.reply = reply
        self.raises = raises
        self.calls = 0

    def turn(self, message, on_activity=None):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.reply


def opens(io, phase=None):
    return [p for e, p in io.events
            if e == "working" and not p.get("done")
            and (phase is None or p.get("phase") == phase)]


def closes(io, phase=None):
    return [p for e, p in io.events
            if e == "working" and p.get("done")
            and (phase is None or p.get("phase") == phase)]


class WorkingContextTests(unittest.TestCase):
    """The mechanism itself."""

    def test_it_opens_and_closes_as_a_pair(self):
        io = RecordingIO()
        with working(io, "plan", "make it better"):
            pass
        (start,), (end,) = opens(io), closes(io)
        self.assertEqual(start["id"], end["id"])
        self.assertEqual(start["phase"], "plan")
        self.assertEqual(start["what"], "Planning the work")
        self.assertEqual(start["detail"], "make it better")
        self.assertIn("elapsed", end)

    def test_an_exception_still_closes_the_row(self):
        """Every side call in the engine is wrapped in try/except; a spinner
        that survived the failure would say work is happening forever."""
        io = RecordingIO()
        with self.assertRaises(ValueError):
            with working(io, "gate"):
                raise ValueError("boom")
        self.assertEqual(len(opens(io)), 1)
        self.assertEqual(len(closes(io)), 1)

    def test_a_front_end_that_throws_never_breaks_the_work(self):
        """Same contract as activity narration: pure decoration, and it must
        NEVER fail the supervisor plan or the gate run it wraps."""

        class Hostile(RecordingIO):
            def emit(self, event, payload=None):
                raise RuntimeError("front end is on fire")

        ran = []
        with working(Hostile(), "plan"):
            ran.append(True)
        self.assertEqual(ran, [True])

    def test_no_io_is_a_legal_no_op(self):
        ran = []
        with working(None, "plan"):
            ran.append(True)
        self.assertEqual(ran, [True])

    def test_ids_are_unique_across_concurrent_callers(self):
        """Parallel/free seat threads and helper threads open rows at the same
        time; one shared flag would let either close the other's row."""
        io = RecordingIO()
        lock = threading.Lock()
        gate = threading.Event()

        def one():
            with working(io, "helper"):
                gate.wait(2)

        threads = [threading.Thread(target=one) for _ in range(6)]
        for t in threads:
            t.start()
        # every row is open at once, so the ids cannot be reused serially
        while True:
            with lock:
                if len(opens(io)) == 6:
                    break
        gate.set()
        for t in threads:
            t.join(5)
        ids = [p["id"] for p in opens(io)]
        self.assertEqual(len(set(ids)), 6)
        self.assertEqual(sorted(p["id"] for p in closes(io)), sorted(ids))

    def test_nested_rows_close_innermost_first(self):
        io = RecordingIO()
        with working(io, "team"):
            with working(io, "gate"):
                pass
        self.assertEqual([p["phase"] for p in closes(io)], ["gate", "team"])

    def test_a_custom_label_wins_over_the_phase_word(self):
        """The room names its own manager, so the row must read the name Josh
        gave it rather than the word "Supervisor"."""
        io = RecordingIO()
        with working(io, "plan", label="Ada is planning the work"):
            pass
        self.assertEqual(opens(io)[0]["what"], "Ada is planning the work")

    def test_an_unregistered_phase_still_says_something(self):
        io = RecordingIO()
        with working(io, "some_new_thing"):
            pass
        self.assertEqual(opens(io)[0]["what"], "Some new thing")

    def test_detail_is_bounded(self):
        """It rides an event to the UI on every side call; an unbounded field
        here is an unbounded field on the wire."""
        io = RecordingIO()
        with working(io, "gate", "x" * 5000):
            pass
        self.assertLessEqual(len(opens(io)[0]["detail"]), 160)

    def test_every_phase_has_human_wording(self):
        """The phrasing IS the feature — a bare key like "replan" on screen
        answers "is it stuck?" no better than an empty window."""
        for phase, text in relay.WORK_PHASES.items():
            self.assertTrue(text[:1].isupper(), phase)
            self.assertGreater(len(text.split()), 1, phase)


class CliFrontEndTests(unittest.TestCase):
    """The terminal shows the same thing, in its own idiom."""

    def setUp(self):
        self.lines = []
        real = relay.status
        relay.status = lambda text="", **kw: self.lines.append(text)
        self.addCleanup(lambda: setattr(relay, "status", real))
        self.io = relay.CLIIO(human_q=None, say_file=None)

    def test_the_open_line_names_the_work(self):
        self.io.emit("working", {"id": "w1", "phase": "plan",
                                 "what": "Planning the work",
                                 "detail": "make it better"})
        self.assertEqual(self.lines,
                         ["… Planning the work — make it better"])

    def test_a_quick_call_prints_no_close_line(self):
        """The console already streams; a "done" line for a 40 ms call is
        noise, and noise is what buries the 90-second one."""
        self.io.emit("working", {"id": "w1", "phase": "moderator",
                                 "what": "Choosing who speaks next"})
        self.lines.clear()
        self.io.emit("working", {"id": "w1", "phase": "moderator",
                                 "what": "Choosing who speaks next",
                                 "done": True, "elapsed": 0.4})
        self.assertEqual(self.lines, [])

    def test_a_long_wait_reports_how_long_it_took(self):
        self.io.emit("working", {"id": "w1", "phase": "plan",
                                 "what": "Planning the work",
                                 "done": True, "elapsed": 92.3})
        self.assertEqual(self.lines,
                         ["… Planning the work — done in 92.3s"])


class WiredSiteTests(unittest.TestCase):
    """The sites that actually produce the dead air, driven for real."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.io = RecordingIO()
        self.state = build_state(self.tmp, [["a"], ["b"]])

    def _stub_supervisor(self, agent):
        real = relay.build_supervisor
        relay.build_supervisor = lambda st: agent
        self.addCleanup(lambda: setattr(relay, "build_supervisor", real))

    def test_the_moderator_pick_shows(self):
        """Between two turns of a moderated room, this call is the whole
        gap — and it was completely silent."""
        relay.moderator_pick(self.state, self.io, StubSide("Fake 1"))
        self.assertEqual(len(opens(self.io, "moderator")), 1)
        self.assertEqual(len(closes(self.io, "moderator")), 1)

    def test_a_failing_moderator_still_closes_its_row(self):
        relay.moderator_pick(self.state, self.io,
                             StubSide(raises=RuntimeError("nope")))
        self.assertEqual(len(closes(self.io, "moderator")), 1)

    def test_supervisor_planning_shows_and_names_the_manager(self):
        """The longest pre-first-turn wait in the app."""
        self.state["supervisor"] = {"name": "Ada"}
        self._stub_supervisor(StubSide("[[TASK: 0 | notes.md | write notes]]"))
        relay.plan_workstreams(self.state, self.io, goal="make it better")
        row = opens(self.io, "plan")
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["what"], "Ada is planning the work")
        self.assertEqual(row[0]["detail"], "make it better")
        self.assertEqual(len(closes(self.io, "plan")), 1)

    def test_a_dead_planner_still_closes_its_row(self):
        self._stub_supervisor(StubSide(raises=RuntimeError("CLI died")))
        relay.plan_workstreams(self.state, self.io, goal="make it better")
        self.assertEqual(len(opens(self.io, "plan")), 1)
        self.assertEqual(len(closes(self.io, "plan")), 1)

    def test_the_verification_gate_shows(self):
        """Not a CLI call at all — a whole test suite, and often the longest
        silence in a Keep Improving run."""
        self.state["continuous"] = relay.continuous_policy(
            {"on": True, "gate": {"command": "pytest -q"}})
        real = relay._gate_run
        relay._gate_run = lambda cmd, ws, timeout=None: {
            "ok": True, "seconds": 3, "tail": ""}
        self.addCleanup(lambda: setattr(relay, "_gate_run", real))
        relay.wave_gate(self.state, self.io)
        row = opens(self.io, "gate")
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["detail"], "pytest -q")
        self.assertEqual(len(closes(self.io, "gate")), 1)

    def test_the_auto_title_side_call_shows(self):
        self.state["turn"] = 1
        real = relay.build_title_agent
        relay.build_title_agent = lambda st: StubSide("Notes About Cheese")
        self.addCleanup(lambda: setattr(relay, "build_title_agent", real))
        relay.maybe_auto_title(self.state, self.io)
        self.assertEqual(len(opens(self.io, "title")), 1)
        self.assertEqual(len(closes(self.io, "title")), 1)

    def test_compacting_a_seat_shows(self):
        """/compact is one full CLI turn with no typing indicator behind it."""
        agent = self.state["agents"][0]
        agent.script = ["here is my summary"]
        relay.seat_command(self.state, "compact", "", self.io)
        self.assertEqual(len(opens(self.io, "compact")), 2)   # both seats
        self.assertEqual(len(closes(self.io, "compact")), 2)

    def test_an_ordinary_seat_turn_opens_no_row(self):
        """Seats already have typing indicators; a second spinner for the same
        wait would double-count every turn in the room."""
        relay.run_rounds(self.state, self.io)
        self.assertEqual(opens(self.io), [])


class AppBridgeTests(unittest.TestCase):
    """The app tracks what is open so a REOPENED chat is not drawn as idle.

    Same reason `Run.thinking` exists: the indicator is live-only, so a chat
    reopened 90 seconds into a supervisor plan rendered as a finished one.
    """

    class Api:
        def __init__(self):
            self.sent = []

        def emit(self, event, payload=None):
            self.sent.append((event, payload))

    def setUp(self):
        self.api = self.Api()
        self.run = app.Run("chat-1")
        self.io = app._AppIO(self.api, self.run)

    def test_an_open_row_is_recorded_against_the_chat(self):
        with working(self.io, "plan", "make it better"):
            self.assertEqual(len(self.run.working), 1)
            row = list(self.run.working.values())[0]
            self.assertEqual(row["phase"], "plan")
            self.assertEqual(row["what"], "Planning the work")
            self.assertTrue(row["started"])
        self.assertEqual(self.run.working, {})

    def test_concurrent_rows_are_tracked_separately(self):
        with working(self.io, "gate"):
            with working(self.io, "helper"):
                self.assertEqual(len(self.run.working), 2)
            self.assertEqual(len(self.run.working), 1)
        self.assertEqual(self.run.working, {})

    def test_a_failed_side_call_leaves_nothing_behind(self):
        with self.assertRaises(RuntimeError):
            with working(self.io, "plan"):
                raise RuntimeError("CLI died")
        self.assertEqual(self.run.working, {})

    def test_every_working_event_carries_its_chat_id(self):
        """The rail routes on it; an event with no chat would be applied to
        whatever transcript happens to be visible."""
        with working(self.io, "plan"):
            pass
        rows = [p for e, p in self.api.sent if e == "working"]
        self.assertEqual([r["chat_id"] for r in rows], ["chat-1", "chat-1"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
