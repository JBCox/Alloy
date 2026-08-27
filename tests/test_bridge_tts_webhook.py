"""Bridge-level tests for the two new local bridges: read-aloud (speaker.py)
and the webhook trigger (webhook.py).

Both follow the same house rules the features themselves obey: token-free,
offline except loopback sockets on ephemeral ports, and every bridge call
that could block answers at once with the real work on a worker thread.

Run:  python tests/test_bridge_tts_webhook.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import relay


class FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)


class FakeSpeaker:
    """Records calls; never spawns anything."""
    def __init__(self):
        self.spoken = []
        self.stops = 0
        self.speaking = False

    def speak(self, text):
        self.spoken.append(text)

    def stop(self):
        self.stops += 1


class RunningStub:
    def __init__(self, running=True):
        self._running = running

    def is_running(self):
        return self._running


def _sandbox_relay_paths():
    root = tempfile.mkdtemp(prefix="aichat-tts-wh-test-")
    old = (app.SESSIONS_DIR, relay.SESSIONS_DIR, relay.TABS_FILE,
           relay.MEMORY_DIR)
    app.SESSIONS_DIR = root
    relay.SESSIONS_DIR = root
    relay.TABS_FILE = os.path.join(root, "tabs.json")
    relay.MEMORY_DIR = os.path.join(root, "memory")
    return root, old


class TTSTests(unittest.TestCase):
    def setUp(self):
        self.api = app.Api()
        self.api._window = FakeWindow()
        self.fake = FakeSpeaker()
        self.api._speaker = self.fake

    def test_speak_text_passes_text_and_reports_ok(self):
        r = self.api.speak_text("hello alloy")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(self.fake.spoken, ["hello alloy"])

    def test_speak_text_caps_at_engine_limit(self):
        import speaker as spk
        r = self.api.speak_text("x" * (spk.MAX_CHARS + 500))
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(len(self.fake.spoken[0]), spk.MAX_CHARS)

    def test_empty_or_non_string_is_an_error_not_a_spawn(self):
        for bad in ("", "   ", None, 42):
            self.assertIn("error", self.api.speak_text(bad))
        self.assertEqual(self.fake.spoken, [])

    def test_stop_and_state_are_honest_pass_throughs(self):
        self.assertTrue(self.api.stop_speech().get("ok"))
        self.assertEqual(self.fake.stops, 1)
        self.assertFalse(self.api.speaker_state()["speaking"])
        self.fake.speaking = True
        self.assertTrue(self.api.speaker_state()["speaking"])

    def test_fallback_config_claims_nothing_before_the_probe(self):
        cfg = app.Api._fallback_config()
        self.assertFalse(cfg["speaker"]["available"])
        self.assertTrue(cfg["speaker"]["detail"],
                        "an unfinished probe states why, like dictation")


def _post(url, payload, headers=None):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        e.close()          # an unread HTTPError warns as an unclosed handle
        return e.code, json.loads(body)


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.root, self.old = _sandbox_relay_paths()
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.addCleanup(setattr, app, "SESSIONS_DIR", self.old[0])
        self.addCleanup(setattr, relay, "SESSIONS_DIR", self.old[1])
        self.addCleanup(setattr, relay, "TABS_FILE", self.old[2])
        self.addCleanup(setattr, relay, "MEMORY_DIR", self.old[3])
        self.api = app.Api()
        self.api._window = FakeWindow()
        self.started = []
        # the real signature is _conversation(cfg, run=None): Api._run passes
        # the Run it was spawned on, and a one-argument fake would swallow a
        # TypeError on the worker thread and report nothing at all
        self.api._conversation = lambda cfg, run=None: self.started.append(cfg)

    def _enable(self, **kw):
        r = self.api.set_webhook(True, **kw)
        self.assertTrue(r.get("ok"), r)
        deadline = time.time() + 10
        while time.time() < deadline:
            st = self.api.get_webhook()
            if st["running"]:
                return st
            time.sleep(0.05)
        self.fail("webhook server never came up")

    def test_disabled_by_default_then_enable_binds_loopback(self):
        st = self.api.get_webhook()
        self.assertFalse(st["enabled"])
        self.assertFalse(st["running"])
        st = self._enable()
        self.assertTrue(st["url"].startswith("http://127.0.0.1:"), st)
        # a stable token was generated once and persisted beside tabs.json
        tok = self.api.get_webhook()["token"]
        self.assertTrue(tok)
        with open(os.path.join(self.root, "webhook.json"),
                  encoding="utf-8") as f:
            self.assertEqual(json.load(f)["token"], tok)
        self.api.set_webhook(False)
        self.api._emit_q.join()
        self.assertFalse(self.api.get_webhook()["running"])
        # socket actually released. On Windows a just-closed listener can sit
        # in TIME_WAIT for a moment, so probe with SO_REUSEADDR and a retry
        # window — the assertion is "a server can have this port back", not
        # "the kernel forgot it instantly".
        import socket
        port = int(st["url"].rsplit(":", 1)[1])
        deadline = time.time() + 5
        while True:
            try:
                s = socket.socket()
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                s.close()
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.1)

    def test_post_start_launches_a_conversation_thread(self):
        st = self._enable()
        code, body = _post(st["url"] + "/start",
                           {"topic": "debate bridges", "turns": 4},
                           {"X-Alloy-Token": st["token"]})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        deadline = time.time() + 5
        while not self.started and time.time() < deadline:
            time.sleep(0.02)
        cfg = self.started[0]
        self.assertEqual(cfg["opener"], "debate bridges")
        self.assertEqual(cfg["turns"], 4)
        ids = [s["provider"] for s in cfg["seats"]]
        self.assertTrue(ids and all(p in ("claude", "gpt", "gemini",
                                          "opencode") for p in ids))

    def test_unknown_seat_providers_are_dropped_not_forged(self):
        st = self._enable()
        _post(st["url"] + "/start",
              {"topic": "t", "seats": ["claude", "not-a-provider"]},
              {"X-Alloy-Token": st["token"]})
        deadline = time.time() + 5
        while not self.started and time.time() < deadline:
            time.sleep(0.02)
        ids = [s["provider"] for s in self.started[0]["seats"]]
        self.assertIn("claude", ids)
        self.assertNotIn("not-a-provider", ids)

    def test_token_is_enforced_end_to_end(self):
        st = self._enable()
        code, _ = _post(st["url"] + "/start", {"topic": "x"})
        self.assertEqual(code, 401)
        code, _ = _post(st["url"] + "/start", {"topic": "x"},
                        {"X-Alloy-Token": "wrong"})
        self.assertEqual(code, 401)

    def test_refuses_while_any_chat_is_running(self):
        st = self._enable()
        self.api._runs._runs["bg"] = RunningStub(True)
        code, body = _post(st["url"] + "/start", {"topic": "x"},
                           {"X-Alloy-Token": st["token"]})
        self.assertEqual(code, 500, body)
        self.assertIn("already running", body.get("error", ""))

    def test_bad_payloads_never_start_anything(self):
        st = self._enable()
        hdr = {"X-Alloy-Token": st["token"]}
        for bad in ({}, {"topic": ""}, {"topic": "x", "bogus": 1},
                    {"topic": "x", "turns": 9999}):
            code, body = _post(st["url"] + "/start", bad, hdr)
            self.assertNotEqual(code, 200, (bad, body))
        self.assertEqual(self.started, [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
