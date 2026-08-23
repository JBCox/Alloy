"""Token-free app bridge tests for orchestration-v2 persistence."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import relay
from relay import Agent


class FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)

    def events(self):
        return [json.loads(script[len("uiEvent("):-1])
                for script in self.calls]


class FakeAgent(Agent):
    cli = "fake"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = f"fake-{self.uid}"

    def turn(self, message, on_activity=None):  # pragma: no cover - no loop
        raise AssertionError("orchestration app tests must spend no turns")


class OrchestrationAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-orchestration-app-")
        self.old_app_sessions = app.SESSIONS_DIR
        self.old_relay_sessions = relay.SESSIONS_DIR
        self.old_types = dict(relay.AGENT_TYPES)
        app.SESSIONS_DIR = relay.SESSIONS_DIR = self.tmp
        relay.AGENT_TYPES["claude"] = FakeAgent
        relay.AGENT_TYPES["gpt"] = FakeAgent

    def tearDown(self):
        app.SESSIONS_DIR = self.old_app_sessions
        relay.SESSIONS_DIR = self.old_relay_sessions
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self.old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start_without_turns(self, cfg):
        api = app.Api()
        api._window = FakeWindow()
        api._auth_blockers = lambda _providers: []
        api._rounds = lambda _state: None
        base = {
            "opener": "inspect orchestration",
            "turns": 4,
            "brief": False,
            "seats": [
                {"id": 10, "provider": "claude", "enabled": True},
                {"id": 20, "provider": "gpt", "enabled": True},
            ],
        }
        base.update(cfg)
        api._conversation(base)
        api._emit_q.join()
        self.assertIsNotNone(api._conv)
        return api

    def test_started_reports_every_backend_correction(self):
        """A correction the UI never hears about is a silent one."""
        api = self.start_without_turns({
            "mode": "panel",
            "orchestration": {"preset": "panel_review", "workflow": "panel",
                              "floor": "nomination", "routing": "addressed"},
        })
        started = [e for e in api._window.events()
                   if e.get("event") == "started"][0]
        changes = started["payload"]["orchestration_adjustments"]
        self.assertEqual({c["field"] for c in changes}, {"floor", "routing"})
        for change in changes:
            self.assertTrue(change["label"] and change["reason"])
            self.assertNotEqual(change["requested"], change["applied"])

    def test_started_stays_quiet_when_nothing_was_corrected(self):
        api = self.start_without_turns({
            "mode": "round_robin",
            "orchestration": {"preset": "open_discussion",
                              "workflow": "conversation", "floor": "cyclic"},
        })
        started = [e for e in api._window.events()
                   if e.get("event") == "started"][0]
        self.assertEqual(started["payload"]["orchestration_adjustments"], [])

    def test_panel_recipe_synthesizer_and_completion_survive_reopen(self):
        requested = {
            "preset": "panel_review",
            "concurrency": "sequential",  # canonicalized by workflow
            "floor": "cyclic",
            "workflow": "panel",
            "routing": "addressed",
            "budget": {"unit": "turns", "limit": 3},
            "completion": "participants",
            "fairness": {"opening_circuit": True, "max_lead": 3},
        }
        api = self.start_without_turns({
            "mode": "panel",
            "orchestration": requested,
            # Mirrors an HTML select: ids cross the bridge as strings.
            "panel": {"synthesizer": "20"},
        })
        expected = relay.normalize_orchestration(requested, "panel", 4, False)
        self.assertEqual(api._conv["orchestration"], expected)
        self.assertEqual(api._conv["max"], 3)
        self.assertEqual(api._conv["panel"], {"synthesizer": 20})
        started = next(event for event in api._window.events()
                       if event["event"] == "started")
        self.assertEqual(started["payload"]["session"]["orchestration"],
                         expected)
        self.assertEqual(started["payload"]["session"]["panel"],
                         {"synthesizer": 20})

        completion = {
            "termination_reason": "cap",
            "goal_verdict": "partial",
            "verdict_source": "synthesizer",
            "lifecycle": "paused",
        }
        api._conv["completion"] = completion
        api._conv["store"].save(api._conv)
        sid = os.path.basename(api._session_dir)

        fresh = app.Api()
        fresh._window = FakeWindow()
        reopened = fresh.open_session(sid)
        self.assertNotIn("error", reopened)
        self.assertEqual(reopened["session"]["orchestration"], expected)
        self.assertEqual(reopened["session"]["panel"], {"synthesizer": 20})
        self.assertEqual(reopened["session"]["completion"], completion)
        self.assertEqual(fresh._conv["orchestration"], expected)
        self.assertEqual(fresh._conv["panel"], {"synthesizer": 20})
        self.assertEqual(fresh._conv["completion"], completion)

    def test_orchestration_only_preset_derives_legacy_mode(self):
        api = self.start_without_turns({
            "orchestration": {
                "preset": "live_room",
                "concurrency": "reactive",
                "floor": "fair",
                "workflow": "conversation",
                "routing": "addressed",
                "budget": {"unit": "turns", "limit": 2},
                "completion": "participants",
            },
        })
        self.assertEqual(api._conv["mode"], "free")
        self.assertEqual(api._conv["orchestration"]["legacy_mode"], "free")
        self.assertEqual(api._conv["orchestration"]["routing"], "addressed")
        self.assertEqual(api._conv["max"], 2)

    def test_legacy_mode_without_recipe_keeps_legacy_routing(self):
        api = self.start_without_turns({"mode": "free"})
        recipe = api._conv["orchestration"]
        self.assertEqual(recipe, relay.normalize_orchestration(
            None, "free", 4, False))
        self.assertEqual(recipe["routing"], "broadcast")
        with open(os.path.join(api._session_dir, "meta.json"),
                  encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["mode"], "free")
        self.assertEqual(meta["orchestration"], recipe)

    def test_invalid_panel_synthesizer_is_rejected_before_session_creation(self):
        api = app.Api()
        api._window = FakeWindow()
        api._auth_blockers = lambda _providers: []
        api._rounds = lambda _state: None
        api._conversation({
            "opener": "panel",
            "turns": 1,
            "mode": "panel",
            "synthesizer": "missing",
            "seats": [
                {"id": 10, "provider": "claude", "enabled": True},
                {"id": 20, "provider": "gpt", "enabled": True},
            ],
        })
        api._emit_q.join()
        self.assertIsNone(api._conv)
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertTrue(any("Panel synthesizer" in call
                            for call in api._window.calls))


if __name__ == "__main__":
    unittest.main()
