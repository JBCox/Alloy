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
import threading
import time
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

        def turn(self, message, on_activity=None):
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

    def project_cfg(self, **extra):
        """A chat pointed at a real project folder (outside the session dir,
        which is what makes session_project call it a custom workspace)."""
        proj = os.path.join(self.tmp, "widget-factory")
        os.makedirs(proj, exist_ok=True)
        with open(os.path.join(proj, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write("Widgets are made of tin.")
        cfg = {"opener": "hi", "turns": 1, "workspace": proj,
               "seats": [{"id": 0, "provider": "claude", "enabled": True},
                         {"id": 1, "provider": "gpt", "enabled": True}]}
        cfg.update(extra)
        return proj, cfg

    def test_project_docs_reach_both_seats_and_are_recorded(self):
        seen = []

        def spy(name_):
            cls = scripted_agent_class(name_, ["ok"])
            turn = cls.turn
            cls.turn = lambda self, m, on_activity=None: (
                seen.append(m), turn(self, m))[1]
            return cls
        relay.AGENT_TYPES["claude"] = spy("Claude")
        relay.AGENT_TYPES["gpt"] = spy("GPT")
        api = app.Api()
        api._window = FakeWindow()
        proj, cfg = self.project_cfg()
        api._conversation(cfg)
        api._emit_q.join()

        # the whole point: BOTH seats, not just the one whose CLI reads it
        self.assertEqual(len(seen), 2)
        for prompt in seen:
            self.assertIn("Widgets are made of tin.", prompt)
        # discovery is a status row, never a forged message turn
        texts = [e["payload"].get("text", "") for e in api._window.events()
                 if e["event"] == "status"]
        self.assertTrue(any("CLAUDE.md" in t for t in texts), texts)
        rec = relay.read_meta(api._session_dir)["brief"]
        self.assertEqual((rec["mode"], rec["sources"]),
                         ("verbatim", ["CLAUDE.md"]))
        with open(os.path.join(api._session_dir, relay.PROJECT_CONTEXT_FILE),
                  encoding="utf-8") as f:
            self.assertIn("Widgets are made of tin.", f.read())
        # nothing was written into the project folder on the verbatim path
        self.assertEqual(os.listdir(proj), ["CLAUDE.md"])

    def test_project_context_can_be_turned_off(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["ok"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["ok"])
        api = app.Api()
        api._window = FakeWindow()
        _, cfg = self.project_cfg(brief=False)
        api._conversation(cfg)
        api._emit_q.join()
        self.assertIsNone(relay.read_meta(api._session_dir)["brief"])

    def test_default_workspace_records_no_brief(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["ok"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["ok"])
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({"opener": "hi", "turns": 1,
                           "seats": [{"id": 0, "provider": "claude",
                                      "enabled": True},
                                     {"id": 1, "provider": "gpt",
                                      "enabled": True}]})
        api._emit_q.join()
        self.assertIsNone(relay.read_meta(api._session_dir)["brief"])

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

    # ------------------------------------------------------- [[ASK]] flow --
    def _ask_setup(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class(
            "Claude", ["Which one? [[ASK: pick one | A | B]]", "c2"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["g1", "g2"])
        api = app.Api()
        api._window = FakeWindow()
        cfg = {"opener": "go", "turns": 1,
               "seats": [{"id": 0, "provider": "claude", "enabled": True},
                         {"id": 1, "provider": "gpt", "enabled": True}]}
        return api, cfg

    def _await_question(self, api, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for e in api._window.events():
                if e["event"] == "question":
                    return e["payload"]
            time.sleep(0.05)
        self.fail("no question event within timeout")

    def test_ask_answered_through_bridge(self):
        api, cfg = self._ask_setup()
        worker = threading.Thread(target=api._conversation, args=(cfg,),
                                  daemon=True)
        worker.start()
        q = self._await_question(api)
        self.assertEqual(q["question"], "pick one")
        self.assertEqual(q["options"], ["A", "B"])
        self.assertEqual(q["asker"], "Claude")
        res = api.answer_question(q["qid"], "B")
        self.assertEqual(res, {"ok": True})
        worker.join(timeout=15)
        self.assertFalse(worker.is_alive())
        api._emit_q.join()
        events = api._window.events()
        names = [e["event"] for e in events]
        self.assertIn("question_done", names)
        josh = [e["payload"] for e in events if e["event"] == "message"
                and e["payload"].get("speaker") == "josh"
                and e["payload"].get("meta") == "answer to Claude"]
        self.assertEqual(len(josh), 1)
        self.assertEqual(josh[0]["text"], "B")
        # answer arrived before GPT's turn, so GPT's queue carried it —
        # verify from the persisted meta rather than live objects
        meta = relay.read_meta(api._session_dir)
        self.assertTrue(meta["ask"])
        self.assertIsNone(meta["ask_pending"])

    def test_stale_qid_is_an_error(self):
        api = app.Api()
        api._window = FakeWindow()
        self.assertIn("error", api.answer_question("nope", "x"))

    def test_stop_during_question(self):
        api, cfg = self._ask_setup()
        worker = threading.Thread(target=api._conversation, args=(cfg,),
                                  daemon=True)
        worker.start()
        self._await_question(api)
        api._stop_flag.set()
        worker.join(timeout=15)
        self.assertFalse(worker.is_alive())
        api._emit_q.join()
        events = api._window.events()
        self.assertIn("question_done", [e["event"] for e in events])
        # no forged answer row
        josh_answers = [e for e in events if e["event"] == "message"
                        and e["payload"].get("speaker") == "josh"
                        and "answer to" in (e["payload"].get("meta") or "")]
        self.assertEqual(josh_answers, [])
        # the requester was told, persistently
        meta = relay.read_meta(api._session_dir)
        claude_pending = meta["seats"][0]["pending"]
        self.assertTrue(any("Josh was unavailable" in p
                            for p in claude_pending))


if __name__ == "__main__":
    unittest.main(verbosity=2)
