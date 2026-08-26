"""Token-free tests for feature #16 — user-configured event hooks.

relay owns the config (event-hooks.json beside tabs.json, resolved through
relay.SESSIONS_DIR at call time); app.Api runs the commands from the ONE
emitter thread but never ON it. Everything here stubs the runner or
subprocess.run — no real process is ever spawned, no CLI is called.

Run:  python tests/test_hooks.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
import app


class FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)
        return threading.get_ident()

    def events(self):
        return [json.loads(s[len("uiEvent("):-1]) for s in self.calls]


class HookConfigTests(unittest.TestCase):
    """relay.read_event_hooks / write_event_hooks."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-hooks-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self):
        return os.path.join(self.tmp, "event-hooks.json")

    def test_round_trip_normalizes_and_drops_empty(self):
        p = self.path()
        out = relay.write_event_hooks(
            {"question": "  notify me  ", "done": "   ", "checkin": "b"},
            path=p)
        self.assertEqual(out["version"], 1)
        self.assertEqual(out["hooks"], {"question": "notify me",
                                        "checkin": "b"})
        with open(p, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk, out)

    def test_unknown_event_name_rejects_loudly(self):
        p = self.path()
        relay.write_event_hooks({"question": "a"}, path=p)
        with self.assertRaises(ValueError) as ctx:
            relay.write_event_hooks({"questoin": "typo"}, path=p)
        self.assertIn("questoin", str(ctx.exception))
        # a rejected write must not clobber what was already saved
        self.assertEqual(relay.read_event_hooks(path=p)["hooks"],
                         {"question": "a"})

    def test_non_dict_hooks_reject(self):
        with self.assertRaises(ValueError):
            relay.write_event_hooks(["question"], path=self.path())

    def test_read_missing_and_corrupt_degrade_to_empty(self):
        self.assertEqual(relay.read_event_hooks(
            path=os.path.join(self.tmp, "absent.json")),
            {"version": 1, "hooks": {}})
        bad = self.path()
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(relay.read_event_hooks(path=bad),
                         {"version": 1, "hooks": {}})

    def test_unknown_names_on_disk_are_dropped_not_trusted(self):
        p = self.path()
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"version": 1,
                       "hooks": {"question": "ok", "later": "nope",
                                 "done": ""}}, f)
        self.assertEqual(relay.read_event_hooks(path=p)["hooks"],
                         {"question": "ok"})

    def test_valid_event_hook_name_is_strict(self):
        for name in relay.HOOK_EVENTS:
            self.assertTrue(relay.valid_event_hook_name(name), name)
        for name in ("Gate_Red", "", None, "questions", "gate"):
            self.assertFalse(relay.valid_event_hook_name(name), repr(name))

    def test_path_resolves_through_relay_sessions_dir_at_call_time(self):
        old = relay.SESSIONS_DIR
        relay.SESSIONS_DIR = self.tmp
        try:
            relay.write_event_hooks({"done": "d"})
            self.assertTrue(os.path.isfile(
                os.path.join(self.tmp, "event-hooks.json")))
            self.assertEqual(relay.read_event_hooks()["hooks"], {"done": "d"})
        finally:
            relay.SESSIONS_DIR = old

    def test_gate_red_is_a_known_event(self):
        self.assertIn("gate_red", relay.HOOK_EVENTS)


def _configured(api, **hooks):
    with api._hooks_lock:
        api._hooks_cache = dict(hooks)


class HookRunTests(unittest.TestCase):
    """Api.run_event_hook + the module-level command runner."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-hooks-run-")
        self._old_dir = relay.SESSIONS_DIR
        relay.SESSIONS_DIR = self.tmp
        self.api = app.Api()
        self.api._window = FakeWindow()
        self._old_exec = app._execute_command

    def tearDown(self):
        app._execute_command = self._old_exec
        relay.SESSIONS_DIR = self._old_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fires_with_the_right_command_and_env_vars(self):
        calls = []
        app._execute_command = lambda cmd, env: calls.append((cmd, env))
        _configured(self.api, question="termux-notification")
        t = self.api.run_event_hook("question",
                                    {"question": "What now?", "qid": "q1"})
        self.assertIsInstance(t, threading.Thread)
        t.join(5)
        self.assertEqual(len(calls), 1)

    def test_detail_is_truncated_to_an_excerpt(self):
        calls = []
        app._execute_command = lambda cmd, env: calls.append(env)
        _configured(self.api, done="x")
        t = self.api.run_event_hook("done", {"text": "z" * 500})
        t.join(5)
        self.assertEqual(len(calls[0]["AICHAT_DETAIL"]),
                         app.HOOK_DETAIL_MAX)

    def test_unconfigured_is_a_zero_work_noop(self):
        calls = []
        app._execute_command = lambda cmd, env: calls.append(cmd)
        _configured(self.api, question="q-hook")
        self.assertIsNone(self.api.run_event_hook("message", {"text": "hi"}))
        self.assertIsNone(self.api.run_event_hook("checkin", {}))
        self.assertIsNone(self.api.run_event_hook("done", {}))
        self.assertEqual(calls, [])

    def test_gate_green_never_fires_gate_red(self):
        calls = []
        app._execute_command = lambda cmd, env: calls.append(cmd)
        _configured(self.api, gate_red="buzz")
        self.assertIsNone(self.api.run_event_hook("gate", {"ok": True}))
        self.assertEqual(calls, [])
        t = self.api.run_event_hook("gate", {"ok": False,
                                             "command": "pytest -q"})
        t.join(5)
        self.assertEqual(calls, ["buzz"])

    def test_timeout_and_every_exception_are_swallowed(self):
        _configured(self.api, checkin="c")
        boom = RuntimeError("hook exploded")
        app._execute_command = mock.Mock(side_effect=boom)
        t = self.api.run_event_hook("checkin", {"kind": "fixed"})
        t.join(5)
        self.assertFalse(t.is_alive())
        # the REAL runner must surface timeouts (the Api worker swallows them)
        app._execute_command = self._old_exec
        with mock.patch.object(app.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(
                                   "cmd", app.HOOK_TIMEOUT_S)):
            with self.assertRaises(subprocess.TimeoutExpired):
                app._execute_command("x", {})
            t2 = self.api.run_event_hook("checkin", {})
            t2.join(5)

    def test_corrupt_config_file_degrades_to_noop(self):
        with open(os.path.join(self.tmp, "event-hooks.json"), "w") as f:
            f.write("garbage{")
        calls = []
        app._execute_command = lambda cmd, env: calls.append(cmd)
        self.assertIsNone(self.api.run_event_hook("question", {}))
        self.assertEqual(calls, [])

    def test_emitter_thread_dispatches_without_blocking(self):
        """The full path: emit → drain thread → hook on ITS OWN thread."""
        idents = {"main": threading.get_ident(), "js": None, "hook": None}
        window = self.api._window

        real_eval = window.evaluate_js

        def eval_ident(script):
            idents["js"] = threading.get_ident()
            return real_eval(script)
        window.evaluate_js = eval_ident
        app._execute_command = lambda cmd, env: idents.update(
            hook=threading.get_ident())
        _configured(self.api, done="echo bye")
        self.api.emit("done", {"text": "finished"})
        self.api._emit_q.join()          # the emitter finished its pass
        # the worker thread is the one run_event_hook returned control to the
        # emitter without joining; wait for it by polling, never enumerate-join
        for _ in range(200):
            if idents["hook"] is not None:
                break
            threading.Event().wait(0.01)
        self.assertIsNotNone(idents["hook"], "hook never ran")
        self.assertNotEqual(idents["hook"], idents["main"])
        self.assertNotEqual(idents["hook"], idents["js"])
        events = window.events()
        self.assertTrue(any(e["event"] == "done" for e in events))


class HookBridgeTests(unittest.TestCase):
    """get_event_hooks / set_event_hooks through the real bridge shapes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-hooks-bridge-")
        self._old_dir = relay.SESSIONS_DIR
        relay.SESSIONS_DIR = self.tmp
        self.api = app.Api()
        self.api._window = FakeWindow()

    def tearDown(self):
        relay.SESSIONS_DIR = self._old_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_lists_events_and_reads_disk(self):
        r = self.api.get_event_hooks()
        self.assertTrue(r.get("ok"))
        self.assertEqual(r["events"], list(relay.HOOK_EVENTS))
        self.assertEqual(r["hooks"], {})
        relay.write_event_hooks({"gate_red": "buzz"})
        r2 = self.api.get_event_hooks()
        self.assertEqual(r2["hooks"], {"gate_red": "buzz"})

    def _wait_status(self):
        """The status event comes from a worker thread AFTER set_event_hooks
        answered, so join() alone can win the race. Poll for it."""
        for _ in range(300):
            self.api._emit_q.join()
            evs = [e for e in self.api._window.events()
                   if e["event"] == "hooks_status"]
            if evs:
                return evs
            threading.Event().wait(0.01)
        return []

    def test_set_answers_immediately_then_reports_status(self):
        r = self.api.set_event_hooks({"question": "ping"})
        self.assertEqual(r, {"ok": True})     # answered before any file work
        evs = self._wait_status()
        self.assertEqual(len(evs), 1)
        self.assertTrue(evs[0]["payload"]["ok"])
        self.assertEqual(relay.read_event_hooks()["hooks"], {"question": "ping"})
        # the fire-path cache must see the new command without a restart
        self.assertEqual(self.api._hook_command("question"), "ping")

    def test_set_unknown_name_rejects_without_spawning_work(self):
        r = self.api.set_event_hooks({"nonsense": "x"})
        self.assertIn("error", r)
        self.api._emit_q.join()
        self.assertEqual([e for e in self.api._window.events()
                          if e["event"] == "hooks_status"], [])
        self.assertFalse(os.path.isfile(
            os.path.join(self.tmp, "event-hooks.json")))

    def test_worker_failure_comes_back_as_a_status_error(self):
        with mock.patch.object(app, "write_event_hooks",
                               side_effect=OSError("disk gone")):
            r = self.api.set_event_hooks({"done": "d"})
        self.assertEqual(r, {"ok": True})
        evs = self._wait_status()
        self.assertEqual(len(evs), 1)
        self.assertFalse(evs[0]["payload"]["ok"])


class GateEmitTests(unittest.TestCase):
    """wave_gate now emits one honest 'gate' signal for both colours."""

    class IO:
        def __init__(self):
            self.emits = []

        def emit(self, name, payload=None):
            self.emits.append((name, payload))

    def _state(self, command="pytest -q"):
        return {"mode": "supervisor",
                "continuous": {"on": True,
                               "gate": {"command": command},
                               },
                "workspace": self.tmp,
                # wave_gate logs transcript rows beside the signal itself
                "log": lambda speaker, text: {"speaker": speaker,
                                              "text": text}}

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-gate-test-")
        self._old_gate_run = relay._gate_run

    def tearDown(self):
        relay._gate_run = self._old_gate_run
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_red_gate_emits_ok_false(self):
        relay._gate_run = lambda cmd, ws: {"ok": False, "seconds": 1.0,
                                           "tail": "boom"}
        io = self.IO()
        res = relay.wave_gate(self._state(), io)
        self.assertFalse(res["ok"])
        self.assertIn(("gate", {"ok": False, "command": "pytest -q"}),
                      io.emits)

    def test_green_gate_emits_ok_true(self):
        relay._gate_run = lambda cmd, ws: {"ok": True, "seconds": 2.0}
        io = self.IO()
        res = relay.wave_gate(self._state(), io)
        self.assertTrue(res["ok"])
        self.assertIn(("gate", {"ok": True, "command": "pytest -q"}),
                      io.emits)


if __name__ == "__main__":
    unittest.main(verbosity=1)
