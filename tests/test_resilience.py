"""Retry policy when the PROVIDER wobbles, not when Alloy is wrong. Token-free.

Written from a real 2026-08-23 session: four `ox` seats at effort max, whose
free endpoint answered `finish_reason: network_error` / `Endpoint is
unavailable` on nearly every turn. Two things made a bad provider look like a
dead app:

  * the automatic second attempt fired INSTANTLY, hitting the identical wall;
  * it then got the FULL effort-scaled watchdog, so a seat that had just been
    told the endpoint was unavailable spent another 15 minutes finding out
    again. Three seats did exactly that, and the conversation showed nothing
    for a quarter of an hour.

Run:  python tests/test_resilience.py
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import run_rounds

from test_loop import FakeAgent, RecordingIO, build_state


# The exact strings from the session that prompted this.
REAL_FAILURES = [
    "Ox Alpha 2 exited 1: APIError · Provider finish_reason: network_error",
    "Ox Alpha exited 1: APIError · Upstream request failed: Endpoint is "
    "unavailable.",
]


class TimingAgent(FakeAgent):
    """Records the watchdog in force for every attempt it is given."""

    def __init__(self, workspace, script, name=None, **kw):
        super().__init__(workspace, script, name=name, **kw)
        self.windows = []

    def turn(self, message, on_activity=None):
        self.windows.append(relay.armed_window(self)[1])
        return super().turn(message, on_activity=on_activity)


class ClassifyTests(unittest.TestCase):
    def test_the_real_failures_are_recognized_as_the_provider_wobbling(self):
        for text in REAL_FAILURES:
            self.assertTrue(relay.transient_error(RuntimeError(text)), text)

    def test_common_provider_wobbles_are_covered(self):
        for text in ("HTTP 503 Service Unavailable", "429 rate limit exceeded",
                     "API Error: 529 overloaded", "socket hang up",
                     "ECONNRESET", "502 Bad gateway",
                     "upstream temporarily unavailable"):
            self.assertTrue(relay.transient_error(RuntimeError(text)), text)

    def test_our_own_bugs_are_NOT_treated_as_the_provider_wobbling(self):
        """A backoff would only delay a failure that will never heal."""
        for text in ("No conversation found with session ID: abc123",
                     "thread/resume failed: no rollout found for thread id x",
                     "Invalid API key", "command not found: opencode",
                     "You are not logged in"):
            self.assertFalse(relay.transient_error(RuntimeError(text)), text)

    def test_a_timeout_of_ours_is_not_a_provider_error(self):
        """It already has its own no-retry path; double-classifying it would
        hand a hung seat a second window it must never get."""
        self.assertFalse(relay.transient_error(
            relay.TurnTimeout("Ox Alpha timed out after 15 minutes")))


class RetryPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-resil-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.agent = FakeAgent(self.tmp, [], name="Seat")
        # effort: max. The armed window for a streaming seat is SILENCE
        # (relay.armed_window) — turn_timeout is the optional duration cap
        # and is None by default, so probation has to reach idle_timeout.
        self.agent.idle_timeout = 900

    def test_a_provider_wobble_earns_a_pause_and_a_short_window(self):
        delay, window = relay.retry_plan(self.agent,
                                         RuntimeError(REAL_FAILURES[0]))
        self.assertEqual(delay, relay.RETRY_BACKOFF)
        self.assertEqual(window, relay.PROBATION_TIMEOUT)
        self.assertLess(window, 900, "the whole point is not to wait 15 min")

    def test_an_ordinary_failure_keeps_todays_behaviour_exactly(self):
        delay, window = relay.retry_plan(self.agent, RuntimeError("boom"))
        self.assertEqual(delay, 0)
        self.assertEqual(window, 900)

    def test_probation_never_LENGTHENS_a_short_window(self):
        """A cheap seat with a 2-minute window must not be given more time
        because its provider failed."""
        self.agent.idle_timeout = 60
        _delay, window = relay.retry_plan(self.agent,
                                          RuntimeError(REAL_FAILURES[1]))
        self.assertEqual(window, 60)

    def test_the_window_is_restored_after_the_retry(self):
        with relay.retry_window(self.agent, 30):
            self.assertEqual(self.agent.idle_timeout, 30)
        self.assertEqual(self.agent.idle_timeout, 900)

    def test_the_window_is_restored_even_when_the_retry_raises(self):
        with self.assertRaises(ValueError):
            with relay.retry_window(self.agent, 30):
                raise ValueError("second attempt died too")
        self.assertEqual(self.agent.idle_timeout, 900)


class BackoffTests(unittest.TestCase):
    def test_the_wait_gives_up_the_moment_josh_presses_stop(self):
        class Stopping(RecordingIO):
            def should_stop(self):
                return True

        import time
        start = time.monotonic()
        stopped = relay.backoff_wait(Stopping(), 30)
        self.assertTrue(stopped)
        self.assertLess(time.monotonic() - start, 2.0,
                        "Stop must not wait out the backoff")

    def test_a_zero_delay_is_not_a_wait(self):
        self.assertFalse(relay.backoff_wait(RecordingIO(), 0))


class LoopTests(unittest.TestCase):
    """The policy where it actually matters: inside the real loop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-resil-loop-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._backoff = relay.RETRY_BACKOFF
        relay.RETRY_BACKOFF = 0          # the wait itself is tested above
        self.addCleanup(lambda: setattr(relay, "RETRY_BACKOFF", self._backoff))

    def _state(self, script):
        state = build_state(self.tmp, [script, ["fine"]], turns=1,
                            labels=["Seat", "Other"])
        state["agents"][0] = TimingAgent(state["workspace"], script,
                                         name="Seat")
        for a in state["agents"]:
            a.idle_timeout = 900
        return state

    def test_the_retry_after_a_provider_wobble_runs_on_the_short_window(self):
        state = self._state([RuntimeError(REAL_FAILURES[0]), "recovered"])
        io = RecordingIO()
        run_rounds(state, io)
        seat = state["agents"][0]
        self.assertEqual(seat.windows, [900, relay.PROBATION_TIMEOUT],
                         "attempt 1 full window, attempt 2 on probation")
        self.assertEqual(seat.idle_timeout, 900, "and it is put back")

    def test_an_ordinary_failure_still_retries_on_the_full_window(self):
        state = self._state([RuntimeError("something we broke"), "recovered"])
        run_rounds(state, RecordingIO())
        self.assertEqual(state["agents"][0].windows, [900, 900])

    def test_the_retry_notice_says_what_it_is_about_to_do(self):
        """'retrying once…' then 15 minutes of silence is what made this look
        like a hang. The notice has to name the wait and the shorter limit."""
        relay.RETRY_BACKOFF = 20
        state = self._state([RuntimeError(REAL_FAILURES[0]), "recovered"])
        io = RecordingIO()
        note = []
        real = relay.backoff_wait
        relay.backoff_wait = lambda io_, secs, abort=None: note.append(secs)
        try:
            run_rounds(state, io)
        finally:
            relay.backoff_wait = real
        said = " ".join(p.get("text", "") for e, p in io.events
                        if e == "status")
        self.assertIn("20s", said)
        self.assertIn("2 min", said)
        self.assertEqual(note, [20], "and it actually waited that long")

    def test_a_seat_that_recovers_gets_its_full_window_back_next_turn(self):
        state = self._state([RuntimeError(REAL_FAILURES[0]), "recovered",
                             "and again"])
        state["max"] = 2
        run_rounds(state, RecordingIO())
        seat = state["agents"][0]
        self.assertEqual(seat.windows[-1], 900,
                         "probation is per-retry, never sticky")


class InterruptedTests(unittest.TestCase):
    """Telling "the process died" apart from "the run ended"."""

    def test_a_killed_mid_run_chat_is_recognised(self):
        """`run_rounds` stamps lifecycle active on entry and paused + a reason
        on EVERY exit including crashes, so active-with-no-reason means the
        process itself went away."""
        self.assertTrue(relay.was_interrupted(
            {"completion": {"lifecycle": "active", "goal_verdict": "unknown"}}))

    def test_every_real_ending_is_NOT_an_interruption(self):
        for reason in ("wrap", "cap", "ceiling", "stop", "fatal", "limit",
                       "starved", "supervisor_done", "moderator_done"):
            self.assertFalse(relay.was_interrupted(
                {"completion": {"lifecycle": "paused",
                                "termination_reason": reason}}), reason)

    def test_a_chat_that_never_ran_is_not_an_interruption(self):
        self.assertFalse(relay.was_interrupted({}))
        self.assertFalse(relay.was_interrupted(None))
        self.assertFalse(relay.was_interrupted({"completion": {}}))

    def test_the_real_session_that_started_all_this_reads_as_interrupted(self):
        """The exact shape found on disk on 2026-08-23."""
        self.assertTrue(relay.was_interrupted({
            "completion": {"lifecycle": "active", "goal_verdict": "unknown"}}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
