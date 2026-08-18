"""Stop must reach the CLI child process.

`io.should_stop()` is only consulted at ROUND boundaries, so before this a
Stop pressed mid-fan-out waited for every in-flight child to finish its turn
— minutes, with replies still landing. Josh read that as "Stop did nothing"
and "I have to stop each seat separately" (2026-08-18).

Token-free: real `python -c` children, killed for real. No CLI, no tokens.
"""
import os, sys, threading, tempfile, shutil, time, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import relay
from relay import TurnCancelled, cancel_all, cancel_seat, rearm_seats, no_retry
from test_activity import PythonAgent

# a child that would outlive any test if nothing killed it
SLEEPER = "import time,sys\nprint('started', flush=True)\ntime.sleep(30)\nprint('finished')"


class CancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-cancel-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _running(self, agent):
        """True once the child has actually started (no sleep-and-hope)."""
        deadline = time.time() + 10
        while time.time() < deadline:
            with agent._proc_lock:
                if agent._proc is not None:
                    return True
            time.sleep(0.01)
        return False

    def test_cancel_kills_a_live_child_and_raises(self):
        agent = PythonAgent(self.tmp, SLEEPER)
        box = {}
        t = threading.Thread(target=lambda: box.setdefault(
            "exc", self._catch(agent)), daemon=True)
        started = time.time()
        t.start()
        self.assertTrue(self._running(agent), "child never started")
        self.assertTrue(agent.cancel(), "cancel found nothing to kill")
        t.join(timeout=15)
        self.assertFalse(t.is_alive(), "turn did not return after cancel")
        self.assertIsInstance(box["exc"], TurnCancelled)
        # the whole point: we did NOT wait out the 30s child
        self.assertLess(time.time() - started, 15)

    def _catch(self, agent):
        try:
            agent.turn("go")
        except Exception as e:
            return e
        return None

    def test_cancel_before_the_turn_never_starts_the_child(self):
        agent = PythonAgent(self.tmp, SLEEPER)
        agent.cancel()                       # queued behind another seat
        with self.assertRaises(TurnCancelled):
            agent.turn("go")
        with agent._proc_lock:
            self.assertIsNone(agent._proc)   # nothing was ever spawned

    def test_cancel_is_safe_when_nothing_is_running(self):
        agent = PythonAgent(self.tmp, "print('x')")
        self.assertFalse(agent.cancel())     # no process, no exception
        rearm_seats({"agents": [agent]})
        self.assertEqual(agent.turn("go"), "x")

    def test_rearm_lets_a_stopped_seat_speak_again(self):
        agent = PythonAgent(self.tmp, "print('x')")
        agent.cancel()
        with self.assertRaises(TurnCancelled):
            agent.turn("go")
        rearm_seats({"agents": [agent]})
        self.assertEqual(agent.turn("go"), "x")   # sticky flag cleared

    def test_cancel_is_never_retried_and_is_not_labelled_a_timeout(self):
        self.assertTrue(no_retry(TurnCancelled("stopped")))
        self.assertEqual(relay.skip_kind(TurnCancelled("stopped")), "stopped")
        self.assertEqual(relay.skip_kind(relay.TurnTimeout("slow")), "timeout")

    def test_cancel_all_stops_every_seat_in_one_press(self):
        agents = [PythonAgent(self.tmp, SLEEPER) for _ in range(3)]
        boxes = [{} for _ in agents]
        threads = [threading.Thread(
            target=lambda a=a, b=b: b.setdefault("exc", self._catch(a)),
            daemon=True) for a, b in zip(agents, boxes)]
        for t in threads:
            t.start()
        for a in agents:
            self.assertTrue(self._running(a))
        self.assertEqual(cancel_all({"agents": agents}), 3)   # ONE call
        for t in threads:
            t.join(timeout=15)
        for b in boxes:
            self.assertIsInstance(b["exc"], TurnCancelled)

    def test_cancel_seat_leaves_the_others_running(self):
        a, b = PythonAgent(self.tmp, SLEEPER), PythonAgent(self.tmp, SLEEPER)
        a.name, b.name = "Alpha", "Beta"
        boxes = [{}, {}]
        ta = threading.Thread(target=lambda: boxes[0].setdefault("e", self._catch(a)), daemon=True)
        tb = threading.Thread(target=lambda: boxes[1].setdefault("e", self._catch(b)), daemon=True)
        ta.start(); tb.start()
        self.assertTrue(self._running(a)); self.assertTrue(self._running(b))
        self.assertEqual(cancel_seat({"agents": [a, b]}, "beta"), [1])
        tb.join(timeout=15)
        self.assertIsInstance(boxes[1]["e"], TurnCancelled)
        self.assertTrue(ta.is_alive(), "stopping one seat killed another")
        with a._proc_lock:
            self.assertIsNotNone(a._proc)
        cancel_all({"agents": [a]})          # clean up
        ta.join(timeout=15)

    def test_cancel_all_never_raises_on_a_broken_seat(self):
        class Broken:
            def cancel(self):
                raise RuntimeError("boom")
        self.assertEqual(cancel_all({"agents": [Broken()]}), 0)


if __name__ == "__main__":
    r = unittest.TextTestRunner(verbosity=0).run(
        unittest.TestLoader().loadTestsFromTestCase(CancelTests))
    print("OK" if r.wasSuccessful() else "FAILED")
    sys.exit(0 if r.wasSuccessful() else 1)
