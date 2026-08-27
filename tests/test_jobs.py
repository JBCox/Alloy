"""W2.3 — the background jobs view: what every chat is doing right now.

`Api.jobs()` is bridge-thread synchronous like `run_status` and
`list_sessions`, because it is a bounded in-memory read with no file I/O and
no subprocess. It takes NO conversation lock — it is POLLED while runs are
mid-turn, and a jobs view that can block behind a seat is worse than none.

The one lock it does take is each Run's own `clock_lock`, and that is new:
`Run.thinking` and `Run.working` are written from SEAT threads and read from
the bridge thread, and `list(d.values())` raises RuntimeError if the dict
changes size mid-iteration. `open_session` got away with an unguarded read
because it happens once, when Josh clicks; polling every run at once turns a
theoretical race into a likely one.

Run:  python tests/test_jobs.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class FakeWindow:
    def evaluate_js(self, script):
        pass


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-jobs-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.api = app.Api()
        self.api._window = FakeWindow()

    def _run(self, chat_id, **kw):
        run = app.Run()
        run.state = {"workspace": self.tmp}
        for k, v in kw.items():
            setattr(run, k, v)
        self.api._runs.adopt(run, chat_id)
        return run

    # ---- the shape --------------------------------------------------------
    def test_it_reports_every_chat_this_window_holds(self):
        self._run("chat-a", status="thinking")
        self._run("chat-b", status="done")
        jobs = self.api.jobs()
        self.assertEqual(sorted(j["chat_id"] for j in jobs["jobs"]),
                         ["chat-a", "chat-b"])
        self.assertGreater(jobs["now"], 0)

    def test_a_background_run_says_so(self):
        run = app.Run(background=True)
        run.state = {}
        self.api._runs.adopt(run, "chat-bg")
        row = self.api.jobs()["jobs"][0]
        self.assertTrue(row["background"])
        self.assertFalse(row["running"], "no thread was ever spawned")

    def test_it_carries_the_seats_that_are_mid_turn(self):
        run = self._run("chat-a", status="thinking")
        io = app._AppIO(self.api, run)
        io.emit("thinking", {"speaker": 0, "provider": "claude",
                             "name": "Claude", "limit": None, "idle": 300})
        row = self.api.jobs()["jobs"][0]
        self.assertEqual(len(row["thinking"]), 1)
        self.assertEqual(row["thinking"][0]["name"], "Claude")
        self.assertIsNone(row["thinking"][0]["limit"])
        self.assertEqual(row["thinking"][0]["idle"], 300)
        self.assertGreater(row["thinking"][0]["started"], 0)

    def test_limit_and_idle_stay_different_fields(self):
        """Conflating them reproduces the "0:00 of 15:00" lie. In the desktop
        app the turn cap is unreachable (it is a CLI-only flag), so claude,
        gpt and ox ALWAYS have limit None and only gemini carries one."""
        run = self._run("chat-a")
        io = app._AppIO(self.api, run)
        io.emit("thinking", {"speaker": 0, "provider": "gemini",
                             "name": "Gemini", "limit": 3600, "idle": 3600})
        t = self.api.jobs()["jobs"][0]["thinking"][0]
        self.assertEqual(t["limit"], 3600)
        self.assertEqual(t["idle"], 3600)

    def test_it_carries_the_relays_own_work(self):
        run = self._run("chat-a")
        io = app._AppIO(self.api, run)
        io.emit("working", {"id": "w1", "phase": "plan",
                            "what": "Planning the work", "detail": "a goal",
                            "started": 100.0})
        row = self.api.jobs()["jobs"][0]
        self.assertEqual(row["working"],
                         [{"id": "w1", "phase": "plan",
                           "what": "Planning the work", "detail": "a goal",
                           "started": 100.0}])
        io.emit("working", {"id": "w1", "done": True})
        self.assertEqual(self.api.jobs()["jobs"][0]["working"], [])

    def test_it_counts_the_messages_still_waiting_to_be_picked_up(self):
        run = self._run("chat-a")
        self.assertEqual(self.api.jobs()["jobs"][0]["queued"], 0)
        self.api.interject("one", None, "chat-a")
        self.api.interject("two", None, "chat-a")
        self.assertEqual(self.api.jobs()["jobs"][0]["queued"], 2)
        run.human_q.get_nowait()
        self.assertEqual(self.api.jobs()["jobs"][0]["queued"], 1)

    def test_it_invents_no_title(self):
        """A Run does not carry one — the rail gets it from the session
        summary — and a slug where Josh expects the name he gave the chat is
        worse than joining the two client-side."""
        self._run("chat-a")
        self.assertNotIn("title", self.api.jobs()["jobs"][0])

    def test_it_hands_out_copies(self):
        """A dict handed out under a lock stops being protected the moment
        the lock is released."""
        run = self._run("chat-a")
        io = app._AppIO(self.api, run)
        io.emit("thinking", {"speaker": 0, "provider": "claude",
                             "name": "Claude"})
        row = self.api.jobs()["jobs"][0]
        row["thinking"][0]["name"] = "vandalised"
        self.assertEqual(self.api.jobs()["jobs"][0]["thinking"][0]["name"],
                         "Claude")

    # ---- the race ---------------------------------------------------------
    def test_reading_the_clocks_survives_a_seat_thread_writing_them(self):
        """`list(d.values())` raises RuntimeError when the dict changes size
        mid-iteration, and these dicts are written from seat threads."""
        run = self._run("chat-a")
        io = app._AppIO(self.api, run)
        stop = threading.Event()
        errors = []

        def churn():
            i = 0
            while not stop.is_set():
                i += 1
                io.emit("thinking", {"speaker": i % 40, "provider": "claude",
                                     "name": "Claude"})
                io.emit("thinking_done", {"speaker": (i - 12) % 40})

        def read():
            try:
                while not stop.is_set():
                    self.api.jobs()
            except Exception as e:          # noqa: BLE001 — that is the point
                errors.append(e)

        ws = [threading.Thread(target=churn, daemon=True) for _ in range(3)]
        rs = [threading.Thread(target=read, daemon=True) for _ in range(3)]
        for t in ws + rs:
            t.start()
        time.sleep(0.6)
        stop.set()
        for t in ws + rs:
            t.join(5)
        self.assertEqual(errors, [], "jobs() raised while seats were writing")

    def test_jobs_takes_no_conversation_lock(self):
        """It is polled while runs are mid-turn. A jobs view that can block
        behind a seat is worse than no jobs view."""
        run = self._run("chat-a")
        held = threading.Lock()
        run.state["lock"] = held
        with held:                    # a seat thread holding the state lock
            done = threading.Event()

            def poll():
                self.api.jobs()
                done.set()

            threading.Thread(target=poll, daemon=True).start()
            self.assertTrue(done.wait(3),
                            "jobs() blocked behind a conversation lock")

    # ---- the status path --------------------------------------------------
    def test_stopping_is_announced_like_every_other_state(self):
        """The one status write that bypassed _set_status and emitted
        nothing, so no view could see a chat enter "stopping"."""
        seen = []
        self.api.emit = lambda ev, p=None: seen.append((ev, p or {}))
        run = self._run("chat-a")
        run.thread = threading.Thread(target=lambda: time.sleep(2),
                                      daemon=True)
        run.thread.start()
        self.api.stop("chat-a")
        self.assertEqual(run.status, "stopping")
        self.assertIn(("stopping"),
                      [p.get("status") for e, p in seen if e == "run_status"])
        self.assertEqual(self.api.jobs()["jobs"][0]["status"], "stopping")


class ClockRuleMarkupTests(unittest.TestCase):
    """The transcript's clock rule and the jobs popover's are ONE function."""

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(os.path.dirname(here), "ui", "index.html"),
                  encoding="utf-8") as f:
            cls.html = f.read()

    def test_there_is_exactly_one_copy_of_the_rule(self):
        self.assertEqual(self.html.count("function turnClockText("), 1)
        # the two renderers both call it, and neither keeps its own copy
        self.assertEqual(self.html.count("turnClockText("), 3)
        self.assertEqual(self.html.count("` · quiet ${fmtClock("), 1)

    def test_the_popover_polls_only_while_it_is_open(self):
        """An always-armed interval keeps the node boot harness alive forever
        — the typing ticker's lesson."""
        self.assertIn("if (!jobsTimer) jobsTimer = setInterval(refreshJobs, 1000);",
                      self.html)
        self.assertIn("if (jobsTimer) { clearInterval(jobsTimer); jobsTimer = null; }",
                      self.html)

    def test_the_badge_repaint_is_hoisted_above_the_routing_gate(self):
        i = self.html.index("renderJobsBadge();\n  if (chatId && chatId !== activeId) {")
        self.assertGreater(i, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
