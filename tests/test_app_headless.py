"""Headless test of the app engine: real app.Api, fake window, fake agents.

Verifies the app front end drives relay.run_rounds correctly end-to-end:
event order, opener handling, done payload — with zero tokens spent.

Run:  python tests/test_app_headless.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
import app
from relay import Agent


class FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)

    def events(self):
        out = []
        for s in self.calls:
            # emit() wraps payloads as uiEvent({"event": ..., "payload": ...})
            body = s[len("uiEvent("):-1]
            out.append(json.loads(body))
        return out


def scripted_agent_class(name_, replies):
    """A class with the real adapter constructor signature, scripted turns."""
    replies = list(replies)

    class Scripted(Agent):
        name = name_
        cli = "fake"

        def turn(self, message):
            # real adapters re-capture a session id in parse() every call;
            # without one, continue_block rightly rules the chat unresumable
            self.session_id = f"fake-session-{self.uid}"
            return replies.pop(0)

    return Scripted


class HeadlessAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-app-test-")
        self._old_sessions = app.SESSIONS_DIR
        app.SESSIONS_DIR = self.tmp
        self._old_types = dict(relay.AGENT_TYPES)

    def tearDown(self):
        app.SESSIONS_DIR = self._old_sessions
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_conversation_event_stream(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["c1"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["g1"])
        api = app.Api()
        api._window = FakeWindow()
        cfg = {"opener": "hello agents", "turns": 1,
               "seats": [{"id": 0, "provider": "claude", "enabled": True},
                         {"id": 1, "provider": "gpt", "enabled": True}]}
        api._conversation(cfg)

        api._emit_q.join()
        events = api._window.events()
        names = [e["event"] for e in events]
        self.assertEqual(names, ["started", "message",
                                 "thinking", "thinking_done", "message",
                                 "thinking", "thinking_done", "message",
                                 "done"])
        # opener row
        self.assertEqual(events[1]["payload"]["speaker"], "josh")
        self.assertEqual(events[1]["payload"]["text"], "hello agents")
        # agent rows carry seat id + provider + name (make_log rows)
        m1 = events[4]["payload"]
        self.assertEqual((m1["speaker"], m1["provider"], m1["name"],
                          m1["text"], m1["round"]),
                         (0, "claude", "Claude", "c1", 1))
        m2 = events[7]["payload"]
        self.assertEqual((m2["speaker"], m2["name"], m2["text"]),
                         (1, "GPT", "g1"))
        # done promises a resumable chat, read back from what was persisted
        done = events[-1]["payload"]
        self.assertTrue(done["can_continue"], done.get("can_continue_reason"))

    def test_mode_flows_from_cfg_to_meta(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class(
            "Claude", ["hi. [[NEXT: GPT]]"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["hey"])
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({"opener": "go", "turns": 1, "mode": "speaker",
                           "seats": [{"id": 0, "provider": "claude",
                                      "enabled": True},
                                     {"id": 1, "provider": "gpt",
                                      "enabled": True}]})
        api._emit_q.join()
        events = api._window.events()
        started = next(e for e in events if e["event"] == "started")
        self.assertEqual(started["payload"]["mode"], "speaker")
        import json as _json
        meta = _json.load(open(os.path.join(api._session_dir, "meta.json"),
                               encoding="utf-8"))
        self.assertEqual(meta["mode"], "speaker")

    def test_unknown_mode_is_a_clear_error(self):
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({"opener": "go", "turns": 1, "mode": "banana",
                           "seats": [{"id": 0, "provider": "claude",
                                      "enabled": True},
                                     {"id": 1, "provider": "gpt",
                                      "enabled": True}]})
        api._emit_q.join()
        events = api._window.events()
        self.assertEqual(events[0]["event"], "error")
        self.assertIn("Unknown mode", events[0]["payload"]["message"])

    def test_idle_command_help(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["c1"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["g1"])
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({"opener": "hi", "turns": 1,
                           "seats": [{"id": 0, "provider": "claude",
                                      "enabled": True},
                                     {"id": 1, "provider": "gpt",
                                      "enabled": True}]})
        api._emit_q.join()
        api._window.calls.clear()
        # idle-path command goes through the shared dispatcher via the shim
        stop = api._do_command(api._conv, "/help")
        api._emit_q.join()
        self.assertFalse(stop)
        api._emit_q.join()
        events = api._window.events()
        self.assertEqual(events[0]["event"], "status")
        self.assertIn("Commands:", events[0]["payload"]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
