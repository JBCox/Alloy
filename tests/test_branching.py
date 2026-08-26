"""Bridge-level tests for the branching features: export, fork, sound cues.

export.py / fork.py carry their own standalone suites (test_export /
test_fork); this one exercises what only the REAL app.Api adds: session-path
resolution, the running-chat refusal, rail-visible fork provenance via
session_summary, and the sound-cue path through the one emitter thread.

Run:  python tests/test_branching.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
import app


class FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)

    def events(self):
        return [json.loads(s[len("uiEvent("):-1]) for s in self.calls]


def make_session(root, sid, rows=None, seats=None, **extra):
    """A minimal on-disk chat: meta.json + messages.jsonl (+ transcript.md,
    which every real session has and fork regenerates)."""
    d = os.path.join(root, sid)
    os.makedirs(d)
    meta = {"v": 2, "id": sid, "title": f"Chat {sid}", "created":
            "2026-08-25T10:00:00", "updated": "2026-08-25T10:30:00",
            "ended": True, "workspace": "", "topic": "", "turns": 1,
            "rnd": 1, "max": 3, "seats": seats if seats is not None else []}
    meta.update(extra)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    rows = rows if rows is not None else [
        {"message_id": "m1", "speaker": "josh", "origin": "human",
         "name": "Josh", "provider": None, "text": "hello", "round": 0,
         "meta": "", "role": None, "ts": "2026-08-25T10:00:01"},
        {"message_id": "m2", "speaker": "s0", "origin": "seat",
         "name": "Claude", "provider": "claude", "text": "<hi> & bye",
         "round": 1, "meta": "", "role": None, "ts": "2026-08-25T10:01:00",
         "usage": {"cost_usd": 0.01}},
    ]
    with open(os.path.join(d, "messages.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(d, "transcript.md"), "w", encoding="utf-8") as f:
        f.write("# AI Chat\n")
    return d


class BranchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-branch-test-")
        self._old_app_dir = app.SESSIONS_DIR
        self._old_relay_dir = relay.SESSIONS_DIR
        self._old_tabs = relay.TABS_FILE
        app.SESSIONS_DIR = self.tmp
        relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")

    def tearDown(self):
        app.SESSIONS_DIR = self._old_app_dir
        relay.SESSIONS_DIR = self._old_relay_dir
        relay.TABS_FILE = self._old_tabs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _api(self):
        api = app.Api()
        api._window = FakeWindow()
        return api

    # ---- export ----------------------------------------------------------
    def test_export_bridge_writes_html_and_reports_path(self):
        make_session(self.tmp, "chat-a")
        r = self._api().export_session("chat-a")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["messages"], 2)
        self.assertTrue(os.path.isfile(r["path"]))
        self.assertEqual(os.path.dirname(r["path"]),
                         os.path.join(self.tmp, "chat-a"))

    def test_export_bridge_unknown_chat_is_an_error_sentence(self):
        r = self._api().export_session("nope")
        self.assertIn("error", r)
        self.assertNotIn("ok", r)

    # ---- fork ------------------------------------------------------------
    SEATS = [{"id": 0, "provider": "claude", "label": "Claude",
              "model": "claude-opus-5", "effort": "high",
              "session_id": "live-thread-abc", "introduced": True}]

    def test_fork_bridge_creates_a_fresh_memory_sibling(self):
        make_session(self.tmp, "chat-b", seats=self.SEATS)
        api = self._api()
        r = api.fork_session("chat-b", "m1")
        self.assertTrue(r.get("ok"), r)
        new_id = r["id"]
        self.assertTrue(new_id.startswith("chat-b-fork-"))
        d = os.path.join(self.tmp, new_id)
        self.assertTrue(os.path.isdir(d))
        s = r["session"]
        self.assertFalse(s["ended"], "a fork is continuable")
        self.assertEqual(s["fork_of"]["id"], "chat-b")
        self.assertEqual(s["fork_of"]["message_id"], "m1")
        self.assertIn("(fork)", s["title"])
        # fresh AI memory: the provider-side thread id must not survive
        self.assertEqual(s["participants"][0].get("session_id", None), None)
        with open(os.path.join(d, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertNotIn("session_id", meta["seats"][0])
        # only rows up to AND INCLUDING m1 survive
        with open(os.path.join(d, "messages.jsonl"), encoding="utf-8") as f:
            kept = [json.loads(line) for line in f if line.strip()]
        self.assertEqual([r_["message_id"] for r_ in kept], ["m1"])

    def test_fork_refuses_while_the_source_is_running(self):
        make_session(self.tmp, "chat-c")
        api = self._api()
        run = app.Run("chat-c")
        run.session_dir = os.path.join(self.tmp, "chat-c")
        run.thread = threading.Thread(target=lambda: threading.Event().wait(30),
                                      daemon=True)
        run.thread.start()
        api._runs.adopt(run, "chat-c")
        try:
            r = api.fork_session("chat-c")
            self.assertIn("error", r)
            self.assertIn("Stop", r["error"])
        finally:
            run.stop_flag.set()

    def test_fork_leaves_the_source_and_the_rail_untouched_for_garbage(self):
        make_session(self.tmp, "chat-d")
        api = self._api()
        self.assertIn("error", api.fork_session("chat-d", "missing-id"))
        self.assertIn("error", api.fork_session("ghost"))
        self.assertEqual([n for n in os.listdir(self.tmp)
                          if "fork" in n], [])

    # ---- sound cues ------------------------------------------------------
    def test_cues_exist_for_the_events_that_wait_on_josh(self):
        for kind in ("question", "checkin", "done"):
            tones = app.SOUND_CUES[kind]
            self.assertTrue(tones)
            for freq, ms in tones:
                self.assertTrue(20 <= freq <= 20000, kind)
                self.assertTrue(0 < ms <= 2000, kind)

    def test_set_sound_toggles_and_the_emitter_honors_it(self):
        played = []
        old = app._play_cue
        app._play_cue = lambda kind: played.append(kind)
        try:
            api = self._api()
            api.emit("question", {"q": "?"})
            api.emit("done", {})
            api._emit_q.join()
            self.assertEqual(sorted(played), ["done", "question"])
            api.set_sound(False)
            api.emit("checkin", {})
            api._emit_q.join()
            self.assertEqual(len(played), 2, "muted cue must not play")
            self.assertEqual(played, ["question", "done"])
            api.set_sound(True)
            self.assertTrue(api._sound)
        finally:
            app._play_cue = old

    def test_non_cue_events_stay_silent(self):
        played = []
        old = app._play_cue
        app._play_cue = lambda kind: played.append(kind)
        try:
            api = self._api()
            api.emit("message", {"text": "hi"})
            api.emit("activity", {})
            api._emit_q.join()
            self.assertEqual(played, [])
        finally:
            app._play_cue = old


if __name__ == "__main__":
    unittest.main(verbosity=1)
