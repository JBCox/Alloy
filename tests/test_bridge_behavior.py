"""Behavioral checks for chat-scoped bridge calls used by WebView2."""

import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import relay


class AliveThread:
    def is_alive(self):
        return True


class BridgeBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-bridge-behavior-")
        self.old_app_sessions = app.SESSIONS_DIR
        self.old_relay_sessions = relay.SESSIONS_DIR
        app.SESSIONS_DIR = self.tmp
        relay.SESSIONS_DIR = self.tmp

    def tearDown(self):
        app.SESSIONS_DIR = self.old_app_sessions
        relay.SESSIONS_DIR = self.old_relay_sessions
        shutil.rmtree(self.tmp, ignore_errors=True)

    def owned_run(self, chat_id="chat-a", running=True):
        api = app.Api()
        run = api._runs.focused()
        agent = SimpleNamespace(name="Claude", role=None,
                                role_instructions=None)
        run.state = {"workspace": self.tmp, "slot_ids": [0],
                     "agents": [agent]}
        run.view_workspace = self.tmp
        if running:
            run.thread = AliveThread()
        api._runs.adopt(run, chat_id)
        return api, run

    def test_ui_positional_calls_route_to_the_named_chat(self):
        api, run = self.owned_run()
        with open(os.path.join(self.tmp, "note.txt"), "w", encoding="utf-8") as f:
            f.write("hello")

        self.assertEqual(api.read_text("note.txt", "chat-a")["text"], "hello")
        self.assertTrue(api.interject("from Josh", [], "chat-a")["ok"])
        self.assertEqual(run.human_q.get_nowait(), "from Josh")
        self.assertTrue(api.command("/help", "chat-a")["ok"])
        self.assertEqual(run.human_q.get_nowait(), "/help")
        role = api.apply_role(0, "Reviewer", "Check the work", "chat-a")
        self.assertTrue(role["ok"], role)
        self.assertIn("Queued", role["note"])
        self.assertEqual(run.staged_roles[0], ("Reviewer", "Check the work"))

    def test_unknown_chat_never_falls_back_to_focus(self):
        api, run = self.owned_run()
        self.assertIn("error", api.interject("wrong", [], "missing"))
        self.assertIn("error", api.command("/help", "missing"))
        self.assertIn("error", api.apply_role(0, "X", "Y", "missing"))
        self.assertTrue(run.human_q.empty())
        self.assertEqual(run.staged_roles, {})

    def test_opening_owned_live_session_focuses_without_rehydrating(self):
        chat_id = "chat-live"
        os.makedirs(os.path.join(self.tmp, chat_id))
        api, run = self.owned_run(chat_id)
        run.session_dir = os.path.join(self.tmp, chat_id)
        original_state = run.state
        api._runs.new_draft()

        result = api.open_session(chat_id)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["live"])
        self.assertIs(api._runs.focused(), run)
        self.assertIs(run.state, original_state)

    def test_new_chat_backgrounds_live_run(self):
        api, run = self.owned_run()
        result = api.reset_conversation()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["backgrounded"], "chat-a")
        self.assertIsNotNone(run.state)
        self.assertIsNone(api._runs.focused().id)
        self.assertIsNot(api._runs.focused(), run)

    def test_new_chat_after_idle_run_uses_fresh_draft(self):
        api, run = self.owned_run(running=False)
        result = api.reset_conversation()
        self.assertTrue(result["ok"], result)
        self.assertIsNone(run.state)
        self.assertIsNone(api._runs.focused().id)
        self.assertIsNot(api._runs.focused(), run)

    def test_background_live_chat_cannot_be_renamed_or_deleted(self):
        chat_id = "chat-live"
        path = os.path.join(self.tmp, chat_id)
        os.makedirs(path)
        with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as f:
            f.write('{"title": "Original", "seats": []}')
        api, run = self.owned_run(chat_id)
        run.session_dir = path
        api._runs.new_draft()  # the dangerous case: another chat has focus

        renamed = api.rename_session(chat_id, "Changed")
        deleted = api.delete_session(chat_id)

        self.assertIn("error", renamed)
        self.assertIn("error", deleted)
        self.assertTrue(os.path.isdir(path))
        self.assertIs(api._runs.get(chat_id), run)


if __name__ == "__main__":
    unittest.main(verbosity=2)
