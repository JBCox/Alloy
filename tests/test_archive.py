"""Bridge-level tests for chat archiving: set_archived, the rail field, UI rules.

Archiving is rail decluttering, NOT deletion: the flag lives additively in
meta.json (exactly like fork_of), rides session_summary -> RAIL_SUMMARY_FIELDS
-> list_sessions, and the UI gathers archived chats into one collapsed group.
These tests pin the whole chain, because the allowlist in _rail_row drops any
field the tuple forgets — silently.

Run:  python tests/test_archive.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import relay


class FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)


def _sandbox_relay_paths():
    """Redirect EVERY sessions-dir global at once. session_path and the tabs
    file read relay's OWN module globals, not app's — patching only the app
    constant still writes the real sessions/ (it did, once; restored by hand)."""
    root = tempfile.mkdtemp(prefix="aichat-archive-test-")
    old = (app.SESSIONS_DIR, relay.SESSIONS_DIR, relay.TABS_FILE)
    app.SESSIONS_DIR = root
    relay.SESSIONS_DIR = root
    relay.TABS_FILE = os.path.join(root, "tabs.json")
    return root, old


def _make_session(root, sid, meta_extra=None):
    d = os.path.join(root, sid)
    os.makedirs(d, exist_ok=True)
    meta = {"id": sid, "title": sid.replace("-", " "), "created": "2026-08-25T10:00:00",
            "updated": "2026-08-25T10:05:00", "ended": True,
            "seats": [{"id": 0, "provider": "claude", "label": None}],
            "rnd": 2, "max": 4}
    meta.update(meta_extra or {})
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return d


class RunningStub:
    """Smallest object that answers the one question set_archived asks."""
    def __init__(self, running=True):
        self._running = running

    def is_running(self):
        return self._running


class TestArchive(unittest.TestCase):
    def setUp(self):
        self.root, self.old = _sandbox_relay_paths()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.addCleanup(setattr, app, "SESSIONS_DIR", self.old[0])
        self.addCleanup(setattr, relay, "SESSIONS_DIR", self.old[1])
        self.addCleanup(setattr, relay, "TABS_FILE", self.old[2])

    def _api(self):
        api = app.Api()
        api._window = FakeWindow()
        return api

    def test_archive_round_trip_persists_and_rides_the_rail(self):
        _make_session(self.root, "chat-a")
        api = self._api()
        r = api.set_archived("chat-a", True)
        self.assertTrue(r.get("ok"), r)
        self.assertTrue(r["session"]["archived"])
        # the flag landed in the file itself, not just the returned row
        with open(os.path.join(self.root, "chat-a", "meta.json"),
                  encoding="utf-8") as f:
            self.assertTrue(json.load(f)["archived"])
        # and the allowlisted rail row carries it
        rows = [s for s in relay.list_sessions() if s["id"] == "chat-a"]
        self.assertTrue(rows[0]["archived"])
        rail = app._rail_row(rows[0])
        self.assertIn("archived", rail)

        r = api.set_archived("chat-a", False)
        self.assertTrue(r.get("ok"), r)
        self.assertFalse(r["session"]["archived"])

    def test_archive_never_touches_resumable_state(self):
        _make_session(self.root, "chat-b", {"ended": False})
        api = self._api()
        before = relay.session_summary(os.path.join(self.root, "chat-b"))
        api.set_archived("chat-b", True)
        after = relay.session_summary(os.path.join(self.root, "chat-b"))
        self.assertEqual(before["can_continue"], after["can_continue"])
        self.assertEqual(before["title"], after["title"])
        self.assertEqual(before["rounds"], after["rounds"])

    def test_missing_chat_is_an_error_sentence(self):
        api = self._api()
        r = api.set_archived("nope", True)
        self.assertIn("error", r)
        self.assertNotIn("ok", r)

    def test_legacy_chats_refuse(self):
        d = os.path.join(self.root, "legacy-chat")
        os.makedirs(d)
        with open(os.path.join(d, "transcript.md"), "w", encoding="utf-8") as f:
            f.write("# old talk")
        api = self._api()
        r = api.set_archived("legacy-chat", True)
        self.assertIn("error", r)
        self.assertFalse(relay.session_summary(d).get("archived"))

    def test_running_chat_refuses(self):
        _make_session(self.root, "chat-live")
        api = self._api()
        api._runs._runs["chat-live"] = RunningStub(True)
        r = api.set_archived("chat-live", True)
        self.assertIn("error", r)
        self.assertFalse(relay.list_sessions()[0]["archived"])
        # ...but a paused run of the same shape goes through
        api._runs._runs["chat-live"] = RunningStub(False)
        self.assertTrue(api.set_archived("chat-live", True).get("ok"))

    def test_session_summary_defaults_false_without_the_key(self):
        _make_session(self.root, "chat-old")
        rows = [s for s in relay.list_sessions() if s["id"] == "chat-old"]
        self.assertFalse(rows[0]["archived"],
                         "every pre-archive chat must read as unarchived")


if __name__ == "__main__":
    unittest.main(verbosity=1)
