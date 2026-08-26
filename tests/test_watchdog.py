"""The turn watchdog: silence, not duration.

A turn is one CLI invocation running an agentic loop, and neither `claude -p`
nor `codex exec` imposes any wall-clock limit of its own — the only thing that
ever stopped a turn at 15 minutes was OUR `threading.Timer`. Worse, it measured
total duration while `on_line` fired for every tool call the child made, so a
seat streaming furiously at 14:59 was killed on exactly the same schedule as
one hung on a dead socket at 0:30. We held the liveness signal and threw it
away (Josh, 2026-08-23: "a hard time limit is silly").

So the watchdog now measures SILENCE. A child that keeps talking runs as long
as the work takes; a child that goes quiet is hung and dies in minutes — which
is FASTER than the old window, not slower. Adapters that stream nothing
(GeminiAgent) keep a duration bound, because for them silence carries no
information at all.

Token-free: real `_run_streaming` against `python -c` children, no CLI.

Run:  python tests/test_watchdog.py
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import relay
from relay import (Agent, ClaudeAgent, CodexAgent, GeminiAgent, OpenCodeAgent,
                   TurnTimeout, IDLE_TIMEOUT, NO_STREAM_TIMEOUT,
                   TIMEOUT_SCALE, retry_plan, retry_window)
from test_activity import PythonAgent


# A child that talks steadily for `secs`, then answers. Under the old
# duration watchdog a window shorter than `secs` killed it mid-sentence.
CHATTY = ("import sys, time\n"
          "end = time.monotonic() + {secs}\n"
          "while time.monotonic() < end:\n"
          "    print('working', flush=True)\n"
          "    time.sleep(0.1)\n"
          "print('DONE', flush=True)\n")

SILENT = "import time; time.sleep({secs})"

# Talks only on stderr, then answers. stdout-only liveness reads this as hung.
STDERR_ONLY = ("import sys, time\n"
               "end = time.monotonic() + {secs}\n"
               "while time.monotonic() < end:\n"
               "    sys.stderr.write('progress\\n'); sys.stderr.flush()\n"
               "    time.sleep(0.1)\n"
               "print('DONE', flush=True)\n")


class IdleWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-watchdog-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agent(self, code, idle=None, cap=None):
        a = PythonAgent(self.tmp, code)
        a.idle_timeout = idle
        a.turn_timeout = cap
        return a

    def test_a_talking_child_outlives_a_window_that_would_have_killed_it(self):
        """THE test. 2s of streaming work under a 1s idle window survives."""
        a = self.agent(CHATTY.format(secs=2), idle=1)
        started = time.monotonic()
        reply = a.turn("go")
        self.assertIn("DONE", reply)
        self.assertGreater(time.monotonic() - started, 1.5)

    def test_a_silent_child_dies_at_the_idle_window(self):
        a = self.agent(SILENT.format(secs=30), idle=1)
        started = time.monotonic()
        with self.assertRaises(TurnTimeout) as ctx:
            a.turn("go")
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("silent", str(ctx.exception).lower())

    def test_the_silence_clock_restarts_on_every_line(self):
        """Talk for 2s at 0.1s intervals, go quiet, die ~1s later — not 2s in."""
        code = CHATTY.format(secs=2).replace("print('DONE', flush=True)",
                                             "time.sleep(30)")
        a = self.agent(code, idle=1)
        started = time.monotonic()
        with self.assertRaises(TurnTimeout):
            a.turn("go")
        self.assertGreater(time.monotonic() - started, 2.5)
        self.assertLess(time.monotonic() - started, 10)

    def test_stderr_output_counts_as_liveness(self):
        a = self.agent(STDERR_ONLY.format(secs=2), idle=1)
        self.assertIn("DONE", a.turn("go"))

    def test_the_hard_cap_still_bounds_a_chatty_runaway(self):
        """A seat that talks forever must remain stoppable by a cap Josh set."""
        a = self.agent(CHATTY.format(secs=60), idle=30, cap=1)
        started = time.monotonic()
        with self.assertRaises(TurnTimeout) as ctx:
            a.turn("go")
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("limit", str(ctx.exception).lower())

    def test_timeout_message_still_warns_about_workspace_edits(self):
        a = self.agent(SILENT.format(secs=30), idle=1)
        with self.assertRaises(TurnTimeout) as ctx:
            a.turn("go")
        self.assertIn("workspace", str(ctx.exception))

    def test_no_cap_by_default_means_the_child_is_never_killed_on_duration(self):
        a = self.agent(CHATTY.format(secs=1), idle=5)
        self.assertIsNone(a.turn_timeout)
        self.assertIn("DONE", a.turn("go"))


class WindowDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-watchdog-def-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_streaming_adapters_get_an_idle_window_and_no_duration_cap(self):
        for cls in (ClaudeAgent, CodexAgent, OpenCodeAgent):
            a = cls(self.tmp)
            self.assertTrue(a.streams_progress, cls.__name__)
            self.assertIsNone(a.turn_timeout, cls.__name__)
            self.assertEqual(a.idle_timeout, IDLE_TIMEOUT, cls.__name__)

    def test_effort_scales_the_silence_window(self):
        a = ClaudeAgent(self.tmp, effort="max")
        self.assertEqual(a.idle_timeout, IDLE_TIMEOUT * TIMEOUT_SCALE["max"])

    def test_gemini_streams_nothing_so_it_keeps_a_duration_bound(self):
        """agy emits its JSON at the end, so silence proves nothing about it."""
        a = GeminiAgent(self.tmp)
        self.assertFalse(a.streams_progress)
        self.assertEqual(a.turn_timeout, NO_STREAM_TIMEOUT)
        self.assertIsNone(a.idle_timeout)

    def test_a_hard_cap_can_be_configured_for_every_seat(self):
        a = ClaudeAgent(self.tmp, turn_cap=7200)
        self.assertEqual(a.turn_timeout, 7200)


class ProbationTests(unittest.TestCase):
    """A transient provider failure shrinks the retry's window. That has to
    reach the window that is actually armed, or the ox outage of 2026-08-23
    (four seats, 15 minutes each, discovering a dead endpoint) repeats."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-watchdog-prob-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_probation_shortens_the_idle_window(self):
        a = ClaudeAgent(self.tmp)
        delay, window = retry_plan(a, RuntimeError("network_error"))
        self.assertEqual(delay, relay.RETRY_BACKOFF)
        self.assertEqual(window, relay.PROBATION_TIMEOUT)
        with retry_window(a, window):
            self.assertEqual(a.idle_timeout, relay.PROBATION_TIMEOUT)
        self.assertEqual(a.idle_timeout, IDLE_TIMEOUT)

    def test_probation_never_lengthens_a_short_window(self):
        a = ClaudeAgent(self.tmp)
        a.idle_timeout = 30
        _, window = retry_plan(a, RuntimeError("503 upstream request failed"))
        self.assertEqual(window, 30)

    def test_an_ordinary_failure_keeps_the_full_window(self):
        a = ClaudeAgent(self.tmp)
        delay, window = retry_plan(a, RuntimeError("something we broke"))
        self.assertEqual(delay, 0)
        self.assertEqual(window, IDLE_TIMEOUT)

    def test_probation_bounds_a_non_streaming_seat_too(self):
        a = GeminiAgent(self.tmp)
        _, window = retry_plan(a, RuntimeError("overloaded"))
        self.assertEqual(window, relay.PROBATION_TIMEOUT)
        with retry_window(a, window):
            self.assertEqual(a.turn_timeout, relay.PROBATION_TIMEOUT)
        self.assertEqual(a.turn_timeout, NO_STREAM_TIMEOUT)


class PreambleTests(unittest.TestCase):
    """The preamble used to promise every seat its turn must finish inside a
    fixed number of minutes. That sentence is now false, and a false promise
    in the preamble makes seats abandon work they had time to finish."""

    def test_preamble_does_not_promise_a_fixed_turn_limit(self):
        tmp = tempfile.mkdtemp(prefix="ai-chat-watchdog-pre-")
        try:
            agent = ClaudeAgent(tmp)
            other = ClaudeAgent(tmp)
            other.name = "GPT"
            # Force the note on: without it the whole block is skipped and
            # this test would pass while saying nothing.
            agent.native_spawn_note = lambda: "You can run subagents."
            text = relay.preamble(agent, [other], "topic", 10, tmp,
                                  spawn={"tier1": True})
            self.assertIn("You can run subagents.", text)
            self.assertNotIn("must finish within", text)
            self.assertIn("no time limit", text)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
