"""W2.0 — run pinning: every conversation thread belongs to a Run.

The bridge-level half of Wave 2, and the prerequisite for everything that
starts a chat nobody is watching (the webhook today; scheduled rooms next).

Three defects it exists to keep fixed, all of them live before it:

  * `Api._webhook_on_start` spawned a bare `threading.Thread` and threw the
    Thread away, so `Run.is_running()` was False forever for that chat,
    `RunManager.live()` could not see it, and EVERY "refuse while a chat is
    live" guard in the app refused nothing.
  * `_conversation` wrote its state, its directory and its identity through
    the `self._conv` / `self._session_dir` views, which resolve to whatever
    chat the WINDOW is showing — so a webhook start adopted Josh's draft, or
    whichever chat he had open, under its own id.
  * `run_event_hook` read `AICHAT_SESSION` off the focus pointer, so a
    background chat's hook fired naming the conversation Josh was reading.

Every test here drives the REAL spawning API (`Api.start`,
`Api.continue_chat`, `Api._webhook_on_start`). Before this suite, no test in
the repo called any of them — every one built its run by hand and assigned
`run.thread` itself, which is precisely why an unpinned conversation thread
could ship.

Run:  python tests/test_run_pinning.py
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
            out.append(json.loads(s[len("uiEvent("):-1]))
        return out

    def payloads(self, name):
        return [e["payload"] for e in self.events() if e["event"] == name]


def gated_agent_class(name_, gate, reply="ok"):
    """A seat whose turn BLOCKS on `gate`, so the conversation is provably
    still running while the assertions happen. A scripted agent that returns
    instantly can only ever prove that a thread once existed."""

    class Gated(Agent):
        name = name_
        cli = "fake"

        def turn(self, message, on_activity=None):
            self.session_id = "fake-session-%s" % self.uid
            gate.wait(10)
            return reply

    return Gated


def scripted_agent_class(name_, replies):
    replies = list(replies)

    class Scripted(Agent):
        name = name_
        cli = "fake"

        def turn(self, message, on_activity=None):
            self.session_id = "fake-session-%s" % self.uid
            return replies.pop(0) if replies else "…"

    return Scripted


SEAT = [{"id": 0, "provider": "claude", "enabled": True}]


class RunPinningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-runpin-")
        self._old_sessions = app.SESSIONS_DIR
        self._old_relay = (relay.SESSIONS_DIR, relay.TABS_FILE, relay.MEMORY_DIR)
        app.SESSIONS_DIR = self.tmp
        relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        relay.MEMORY_DIR = os.path.join(self.tmp, "memory")
        self._old_types = dict(relay.AGENT_TYPES)
        self.gates = []

    def tearDown(self):
        for g in self.gates:
            g.set()
        app.SESSIONS_DIR = self._old_sessions
        (relay.SESSIONS_DIR, relay.TABS_FILE,
         relay.MEMORY_DIR) = self._old_relay
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------
    def _api(self):
        api = app.Api()
        api._window = FakeWindow()
        return api

    def _gate(self):
        g = threading.Event()
        self.gates.append(g)
        return g

    def _wait(self, pred, what, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.01)
        self.fail("timed out waiting for %s" % what)

    def _cfg(self, **extra):
        cfg = {"opener": "hi", "turns": 1, "seats": [dict(s) for s in SEAT]}
        cfg.update(extra)
        return cfg

    # ---- the manager ------------------------------------------------------
    def test_spawn_records_the_thread_before_it_starts(self):
        """The whole point. A guard that reads is_running() between the
        thread being created and being recorded must not see a hole."""
        mgr = app.RunManager()
        gate = self._gate()
        seen = {}
        run = app.Run()

        def work():
            seen["running_from_inside"] = run.is_running()
            gate.wait(10)

        got = mgr.spawn(work, (), run=run)
        self.assertIs(got, run, "spawn must return the run it started")
        self.assertIsNotNone(run.thread, "spawn left run.thread None")
        self.assertTrue(run.is_running())
        self._wait(lambda: "running_from_inside" in seen, "the worker to start")
        self.assertTrue(seen["running_from_inside"],
                        "the thread was not recorded before it ran")
        gate.set()
        run.thread.join(5)
        self.assertFalse(run.is_running())

    def test_live_sees_a_run_that_has_no_id_yet(self):
        """A chat is live from the instant its worker starts. The session dir
        — and therefore adopt() — happens seconds INTO the work."""
        mgr = app.RunManager()
        gate = self._gate()
        run = mgr.spawn(gate.wait, (10,), run=mgr.background())
        self.assertIsNone(run.id)
        self.assertEqual([r.id for r in mgr.live()], [None])
        gate.set()
        run.thread.join(5)
        self.assertEqual(mgr.live(), [])

    def test_adopt_does_not_move_the_focus_unless_it_is_asked_to(self):
        mgr = app.RunManager()
        bg = mgr.background()
        mgr.adopt(bg, "chat-bg")
        self.assertIsNone(mgr.focused().id, "a background adopt stole focus")
        self.assertIs(mgr.get("chat-bg"), bg)
        fg = mgr.focused()
        mgr.adopt(fg, "chat-fg", focus=True)
        self.assertEqual(mgr.focused().id, "chat-fg")

    def test_re_adopting_a_run_drops_its_old_key(self):
        """Two ids pointing at one Run is how open_session(old) came to serve
        the new chat's state."""
        mgr = app.RunManager()
        run = mgr.focused()
        mgr.adopt(run, "chat-a", focus=True)
        mgr.adopt(run, "chat-b", focus=True)
        self.assertIsNone(mgr.get("chat-a"),
                          "the old id still points at the re-adopted run")
        self.assertIs(mgr.get("chat-b"), run)
        self.assertEqual(run.id, "chat-b")

    def test_fresh_stage_never_hands_back_an_adopted_run(self):
        """Josh typing into a reopened chat starts a NEW conversation: the UI
        clears activeId, but the python focus pointer still names the old
        one."""
        mgr = app.RunManager()
        reopened = mgr.focus("chat-old")
        self.assertEqual(mgr.focused().id, "chat-old")
        stage = mgr.fresh_stage()
        self.assertIsNone(stage.id)
        self.assertIsNot(stage, reopened)
        self.assertIs(mgr.get("chat-old"), reopened,
                      "the reopened chat lost its registration")

    # ---- the bridge: a foreground start ----------------------------------
    def test_start_pins_its_thread_and_takes_the_focus(self):
        gate = self._gate()
        relay.AGENT_TYPES["claude"] = gated_agent_class("Claude", gate)
        api = self._api()
        run = api._runs.focused()
        self.assertEqual(api.start(self._cfg()), {"ok": True})
        self.assertTrue(run.is_running(), "start left run.thread unset")
        self._wait(lambda: run.id is not None, "the chat to earn an id")
        self.assertEqual(api._runs.focused().id, run.id,
                         "a start from the visible stage must take the focus")
        gate.set()
        run.thread.join(10)

    def test_start_refuses_only_when_this_stage_is_already_running(self):
        gate = self._gate()
        relay.AGENT_TYPES["claude"] = gated_agent_class("Claude", gate)
        api = self._api()
        api.start(self._cfg())
        first = api._runs.focused()
        self._wait(lambda: first.id is not None, "the first chat's id")
        # a second start from a FRESH stage is the registry's whole point
        self.assertEqual(api.start(self._cfg()), {"ok": True})
        self.assertTrue(first.is_running(), "the first chat was torn down")
        gate.set()

    def test_starting_after_reopening_a_chat_does_not_re_register_it(self):
        """The double-adoption bug: `start` read the focus pointer, which
        still named the reopened chat, so `_conversation` adopted THAT run
        under the new chat's directory."""
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["one"])
        api = self._api()
        api._conversation(self._cfg())
        api._emit_q.join()
        first = api._runs.focused()
        old_id, old_state = first.id, first.state
        self.assertIsNotNone(old_id)
        gate = self._gate()
        relay.AGENT_TYPES["claude"] = gated_agent_class("Claude", gate)
        self.assertEqual(api.start(self._cfg(opener="a second chat")),
                         {"ok": True})
        new = api._runs.focused()
        self._wait(lambda: new.id is not None, "the second chat's id")
        self.assertIsNot(new, first, "the new chat reused the old chat's run")
        self.assertNotEqual(new.id, old_id)
        self.assertIs(api._runs.get(old_id), first,
                      "the reopened chat lost its own registration")
        self.assertIs(first.state, old_state,
                      "the first chat's state was overwritten")
        gate.set()
        new.thread.join(10)

    # ---- the bridge: a background start ----------------------------------
    def _webhook(self, api, topic="from a script"):
        return api._webhook_on_start({"topic": topic, "turns": 1,
                                      "seats": ["claude"]})

    def test_a_webhook_started_conversation_is_live(self):
        """Before W2.0 this run was live with is_running() False forever."""
        gate = self._gate()
        relay.AGENT_TYPES["claude"] = gated_agent_class("Claude", gate)
        api = self._api()
        self.assertEqual(self._webhook(api), {"started": True})
        self._wait(lambda: api._runs.live(), "the webhook run to look live")
        gate.set()

    def test_the_webhook_refuses_while_any_chat_is_live(self):
        """The guard that refused nothing. It reads live(), which is empty
        unless spawn() pinned the thread."""
        gate = self._gate()
        relay.AGENT_TYPES["claude"] = gated_agent_class("Claude", gate)
        api = self._api()
        api.start(self._cfg())
        self._wait(lambda: api._runs.live(), "the first chat to look live")
        with self.assertRaises(ValueError):
            self._webhook(api)
        gate.set()

    def test_a_background_start_leaves_the_focus_alone(self):
        gate = self._gate()
        relay.AGENT_TYPES["claude"] = gated_agent_class("Claude", gate)
        api = self._api()
        draft = api._runs.focused()
        self._webhook(api)
        self._wait(lambda: api._runs.live(), "the webhook run")
        bg = api._runs.live()[0]
        self._wait(lambda: bg.id is not None, "the webhook chat's id")
        self.assertIsNot(bg, draft, "the webhook borrowed Josh's draft")
        self.assertIs(api._runs.focused(), draft,
                      "a background start took the focus")
        self.assertIsNone(draft.id, "Josh's draft was registered as the chat")
        self.assertIs(api._runs.get(bg.id), bg)
        gate.set()
        bg.thread.join(10)

    def test_a_background_conversation_writes_onto_its_own_run(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["one"])
        api = self._api()
        draft = api._runs.focused()
        run = api._runs.background()
        api._conversation(self._cfg(), run)
        api._emit_q.join()
        self.assertIsNotNone(run.state, "the run got no state")
        self.assertIsNone(draft.state, "the draft was written to")
        self.assertIsNotNone(run.session_dir)
        self.assertIsNone(draft.session_dir)
        self.assertIs(run.state["_run"], run)

    def test_started_says_when_a_chat_is_in_the_background(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["one"])
        api = self._api()
        api._conversation(self._cfg(), api._runs.background())
        api._emit_q.join()
        started = api._window.payloads("started")
        self.assertEqual(len(started), 1)
        self.assertTrue(started[0].get("background"),
                        "the UI cannot tell it must not steal the transcript")

    def test_a_foreground_start_is_not_marked_background(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["one"])
        api = self._api()
        api._conversation(self._cfg())
        api._emit_q.join()
        started = api._window.payloads("started")[0]
        self.assertFalse(started.get("background"))

    def test_a_background_run_stamps_every_event_it_emits(self):
        """Its pre-identity events carry chat_id None, which the UI reads as
        "belongs to the chat on screen"."""
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["one"])
        api = self._api()
        api._conversation(self._cfg(), api._runs.background())
        api._emit_q.join()
        events = api._window.events()
        self.assertTrue(events)
        naked = [e for e in events if not e["payload"].get("background")]
        self.assertEqual(naked, [],
                         "unstamped events from a background run: %r"
                         % [e["event"] for e in naked])

    def test_continuing_a_background_chat_makes_it_the_foreground_one(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class(
            "Claude", ["one", "two"])
        api = self._api()
        run = api._runs.background()
        api._conversation(self._cfg(), run)
        api._emit_q.join()
        self.assertTrue(run.background)
        self.assertEqual(api.continue_chat({"session_id": run.id,
                                            "opener": "carry on", "turns": 1}),
                         {"ok": True})
        run.thread.join(10)
        api._emit_q.join()
        self.assertFalse(run.background,
                         "Josh typed into it; it is his chat now")
        self.assertEqual(api._runs.focused().id, run.id)

    # ---- event hooks ------------------------------------------------------
    def _hook_env(self, api, event, payload):
        captured = {}
        api._hook_command = lambda name: "echo hi"
        api._hook_worker = lambda name, cmd, env: captured.update(env)
        t = api.run_event_hook(event, payload)
        if t is not None:
            t.join(5)
        return captured

    def test_the_hook_takes_its_session_from_the_firing_run(self):
        api = self._api()
        api._runs.focus("the-chat-josh-is-reading")
        env = self._hook_env(api, "done",
                             {"session": {"id": "the-background-chat"}})
        self.assertEqual(env.get("AICHAT_SESSION"), "the-background-chat")

    def test_the_hook_reads_chat_id_when_the_payload_carries_one(self):
        api = self._api()
        api._runs.focus("visible")
        env = self._hook_env(api, "question", {"chat_id": "background"})
        self.assertEqual(env.get("AICHAT_SESSION"), "background")

    def test_the_hook_still_falls_back_to_the_focused_chat(self):
        api = self._api()
        api._runs.focus("visible")
        env = self._hook_env(api, "checkin", {})
        self.assertEqual(env.get("AICHAT_SESSION"), "visible")

    # ---- what was deleted -------------------------------------------------
    def test_the_run_registry_publishes_no_unread_count(self):
        """It was set to 0 in the constructor and never incremented; the UI
        read a key python never sent. A number nobody measures is not a
        number to publish."""
        api = self._api()
        run = api._runs.focus("a-chat")
        self.assertFalse(hasattr(run, "unread"))
        rows = api.run_status()["runs"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("unread", rows[0])
        api._set_status(run, "running")
        api._emit_q.join()
        for p in api._window.payloads("run_status"):
            self.assertNotIn("unread", p)

    def test_the_dead_focused_run_views_are_gone(self):
        """Each had zero callers left, and a focused-run view with no caller
        is a focus leak waiting for its first one."""
        for name in ("_thread", "_staged_roles", "_ask_lock", "_chat_id"):
            self.assertNotIn(name, vars(app.Api),
                             "app.Api.%s is back" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
