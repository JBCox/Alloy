"""Speaker: SAPI read-aloud state machine, payload plumbing, probe honesty.

Token-free, network-free, audio-free -- no PowerShell process is ever
launched and nothing is spoken. The engine sits behind the `runner` seam
(dictation's `stream_factory` in reverse: a process instead of a stream), so
every test drives fakes and finishes in well under a second.

What this suite really guards:

 * The text never reaches a command line. The script carries only the decode
   machinery (base64 marker + Speak call), stays pure ASCII, and the reply
   itself crosses stdin as base64 UTF-8 -- decode it back and it must match.
 * Latest-wins is enforced by KILLING the previous child, not by hoping it
   finishes; a failed replacement must not silence the working one.
 * `speaking` is derived from the live child, so every simulated failure --
   broken pipe, dying runner, unreadable process, refused terminate -- leaves
   it consistent. No exception may escape a feeder or reaper thread.

Run:  python tests/test_speaker.py
"""

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import speaker


# ------------------------------------------------------------- test doubles --

class FakeStdin:
    """The write end of the child's stdin."""

    def __init__(self, proc):
        self.proc = proc
        self.chunks = []
        self.flushed = 0
        self.closed = False

    def write(self, data):
        if self.proc.fail_writes:
            raise BrokenPipeError("child lost the race against stop()")
        self.chunks.append(data)
        return len(data)

    def flush(self):
        self.flushed += 1

    def close(self):
        self.closed = True


class FakeProc:
    """Stands in for subprocess.Popen: poll/terminate/kill/wait + a stdin.

    `alive` models the real child's lifetime; PowerShell's Speak() is
    synchronous inside the child, so finish() IS the utterance ending.
    """

    def __init__(self, fail_terminate=False, fail_writes=False,
                 fail_poll=False):
        self.script = None
        self.stdin = FakeStdin(self)
        self.alive = True
        self.terminated = False
        self.killed = False
        self.waits = []
        self.fail_terminate = fail_terminate
        self.fail_writes = fail_writes
        self.fail_poll = fail_poll

    def poll(self):
        if self.fail_poll:
            raise ValueError("unreadable process")
        return None if self.alive else 0

    def terminate(self):
        if self.fail_terminate:
            raise RuntimeError("terminate refused")
        self.terminated = True
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return 0

    def payload(self):
        return base64.b64decode(b"".join(self.stdin.chunks)).decode("utf-8")

    def finish(self):
        """Simulate the synthesizer completing Speak() and the process exiting."""
        self.alive = False


class FakeRunner:
    """The runner seam: records scripts, hands out scripted procs."""

    def __init__(self, procs=None):
        self.queue = list(procs or [])
        self.made = []
        self.scripts = []
        self.error = None                    # set to simulate a dying spawn

    def __call__(self, script):
        if self.error:
            raise self.error
        self.scripts.append(script)
        proc = self.queue.pop(0) if self.queue else FakeProc()
        proc.script = script
        self.made.append(proc)
        return proc


def make_speaker(runner=None):
    spk = speaker.Speaker(runner=runner or FakeRunner())
    return spk


def drained(spk):
    """Join every feeder/reaper thread so assertions see settled state."""
    spk._drain_workers()


# ------------------------------------------------------- script and payload --

class PayloadTests(unittest.TestCase):
    def test_the_script_decodes_but_never_carries_text(self):
        runner = FakeRunner()
        spk = make_speaker(runner)
        self.assertTrue(spk.speak("hello there"))
        drained(spk)
        script = runner.scripts[0]
        # the decode machinery must be present...
        for marker in ("FromBase64String", ".Speak(", "System.Speech",
                       "ReadToEnd"):
            self.assertIn(marker, script, marker)
        # ...and the text itself must NOT be interpolated into it
        self.assertNotIn("hello", script)
        self.assertLess(len(script), 500)

    def test_the_script_is_pure_ascii(self):
        # PowerShell reads BOM-less non-ASCII as ANSI; this script never gets
        # the chance because only ASCII ever reaches the command line.
        runner = FakeRunner()
        spk = make_speaker(runner)
        spk.speak("caf\u00e9 r\u00e9sum\u00e9")
        drained(spk)
        self.assertTrue(all(ord(ch) < 128 for ch in runner.scripts[0]))

    def test_the_payload_rides_stdin_as_base64_and_round_trips(self):
        runner = FakeRunner()
        spk = make_speaker(runner)
        spk.speak("Read me aloud, please.")
        drained(spk)
        proc = runner.made[0]
        self.assertEqual(proc.payload(), "Read me aloud, please.")
        self.assertTrue(proc.stdin.closed, "stdin left open; ReadToEnd hangs")
        self.assertTrue(proc.stdin.flushed >= 1)

    def test_newlines_survive_the_trip(self):
        runner = FakeRunner()
        spk = make_speaker(runner)
        spk.speak("line one\nline two\tline three")
        drained(spk)
        self.assertEqual(runner.made[0].payload(),
                         "line one\nline two\tline three")


# ---------------------------------------------------------- state machine ----

class LifecycleTests(unittest.TestCase):
    def test_speak_flips_speaking_until_the_child_exits(self):
        runner = FakeRunner()
        spk = make_speaker(runner)
        self.assertFalse(spk.speaking)
        self.assertTrue(spk.speak("once"))
        drained(spk)
        self.assertTrue(spk.speaking)
        runner.made[0].finish()
        self.assertFalse(spk.speaking)

    def test_a_second_speak_kills_the_first_child(self):
        first, second = FakeProc(), FakeProc()
        runner = FakeRunner([first, second])
        spk = make_speaker(runner)
        self.assertTrue(spk.speak("stale"))
        drained(spk)
        self.assertTrue(first.alive, "precondition")
        self.assertTrue(spk.speak("latest"))
        drained(spk)
        self.assertTrue(first.terminated, "the old child was never stopped")
        self.assertFalse(first.killed, "it exited on terminate; no escalation")
        self.assertFalse(second.terminated, "the new child was harmed")
        self.assertTrue(spk.speaking)
        self.assertEqual(len(runner.scripts), 2)

    def test_empty_text_is_a_noop_that_never_spawns_a_runner(self):
        runner = FakeRunner()
        spk = make_speaker(runner)
        self.assertFalse(spk.speak(""))
        self.assertFalse(spk.speak(None))
        self.assertFalse(spk.speak("\x07\r\x00\x1b"))     # controls only
        drained(spk)
        self.assertEqual(runner.scripts, [])
        self.assertEqual(runner.made, [])
        self.assertFalse(spk.speaking)


class SanitizeTests(unittest.TestCase):
    def test_control_characters_are_stripped_but_layout_survives(self):
        runner = FakeRunner()
        spk = make_speaker(runner)
        spk.speak("a\x07b\tc\rd\x00e\nf\x1b[31mz\x9b")
        drained(spk)
        self.assertEqual(runner.made[0].payload(),
                         "ab\tcde\nf[31mz")

    def test_long_text_is_capped_silently(self):
        runner = FakeRunner()
        spk = make_speaker(runner)
        spk.speak("na" * 10 * speaker.MAX_CHARS)          # way over the cap
        drained(spk)
        payload = runner.made[0].payload()
        self.assertEqual(len(payload), speaker.MAX_CHARS)
        self.assertTrue(spk.speaking)                     # capped, not refused


# ------------------------------------------------------------------ stopping --

class StopTests(unittest.TestCase):
    def test_stop_when_idle_is_false_and_harmless(self):
        spk = make_speaker()
        self.assertFalse(spk.stop())
        self.assertFalse(spk.speaking)

    def test_double_stop_is_safe(self):
        runner = FakeRunner()
        spk = make_speaker(runner)
        spk.speak("hello")
        drained(spk)
        self.assertTrue(spk.stop())
        self.assertFalse(spk.stop())                      # second click punished?
        drained(spk)
        self.assertTrue(runner.made[0].terminated)
        self.assertFalse(spk.speaking)

    def test_stop_interrupts_current_speech_immediately(self):
        runner = FakeRunner()
        spk = make_speaker(runner)
        spk.speak("a long paragraph of agent prose")
        self.assertTrue(spk.stop())                       # before any draining
        drained(spk)
        self.assertTrue(runner.made[0].terminated)
        self.assertFalse(spk.speaking)


# ------------------------------------------------- failure stays consistent --

class ConsistencyTests(unittest.TestCase):
    def test_a_dying_runner_leaves_any_working_utterance_talking(self):
        good = FakeRunner()
        spk = make_speaker(good)
        self.assertTrue(spk.speak("first"))
        drained(spk)
        first = good.made[0]

        bad = FakeRunner()
        bad.error = OSError("powershell is gone")
        spk._runner = bad
        self.assertFalse(spk.speak("replacement"))
        self.assertTrue(first.alive, "a failed spawn silenced the working run")
        self.assertTrue(spk.speaking)

        spk._runner = good                                # still usable afterwards
        self.assertTrue(spk.speak("third"))
        drained(spk)
        self.assertTrue(first.terminated, "the recovery displaced the old run")
        self.assertTrue(spk.speaking)

    def test_a_broken_pipe_while_feeding_keeps_state_consistent(self):
        proc = FakeProc(fail_writes=True)
        runner = FakeRunner([proc])
        spk = make_speaker(runner)
        self.assertTrue(spk.speak("nobody will hear this"))
        drained(spk)                                      # must not raise
        self.assertEqual(proc.stdin.chunks, [])
        self.assertTrue(proc.stdin.closed, "close must run even after failure")
        self.assertTrue(spk.speaking, "child alive; flag must say so")
        proc.finish()
        self.assertFalse(spk.speaking)

    def test_an_unreadable_process_is_never_claimed_as_speaking(self):
        proc = FakeProc(fail_poll=True)
        spk = make_speaker(FakeRunner([proc]))
        self.assertTrue(spk.speak("x"))
        self.assertFalse(spk.speaking)                    # unknown != true
        proc.finish()
        self.assertFalse(spk.speaking)
        self.assertTrue(spk.stop())
        drained(spk)

    def test_a_refused_terminate_escalates_to_kill(self):
        proc = FakeProc(fail_terminate=True)
        runner = FakeRunner([proc])
        spk = make_speaker(runner)
        spk.speak("stubborn")
        drained(spk)
        self.assertTrue(spk.stop())
        drained(spk)
        self.assertTrue(proc.killed, "escalation never happened")
        self.assertTrue(proc.waits, "no grace was given before escalating")
        self.assertFalse(proc.alive)
        self.assertFalse(spk.speaking)

    def test_feeder_threads_are_all_daemons(self):
        runner = FakeRunner()
        spk = make_speaker(runner)
        spk.speak("x")
        for thread in list(spk._daemons):
            self.assertTrue(thread.daemon)


# --------------------------------------------------------------------- probe --

class ProbeTests(unittest.TestCase):
    def _probe_under(self, system, which):
        real_system, real_which = speaker.platform.system, speaker.shutil.which
        speaker.platform.system = lambda: system
        speaker.shutil.which = which
        try:
            return speaker.probe()
        finally:
            speaker.platform.system = real_system
            speaker.shutil.which = real_which

    def test_off_windows_it_declines_plainly(self):
        info = self._probe_under(
            "Linux", lambda name: "/usr/bin/powershell")
        self.assertFalse(info["available"])
        self.assertIn("windows", info["detail"].lower())

    def test_a_missing_powershell_is_named(self):
        info = self._probe_under("Windows", lambda name: None)
        self.assertFalse(info["available"])
        self.assertIn("powershell", info["detail"].lower())

    def test_ready_when_windows_has_powershell_on_path(self):
        info = self._probe_under(
            "Windows",
            lambda name: "C:\\Windows\\System32\\WindowsPowerShell"
                         "\\v1.0\\powershell.exe")
        self.assertTrue(info["available"])
        self.assertTrue(info["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
