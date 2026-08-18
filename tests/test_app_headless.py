"""Headless test of the app engine: real app.Api, fake window, fake agents.

Verifies the app front end drives relay.run_rounds correctly end-to-end:
event order, opener handling, done payload — with zero tokens spent.

Run:  python tests/test_app_headless.py
"""

import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
import app
import outcome
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

    # ---- one-press stop / per-seat stop (2026-08-18) --------------------
    def _stopped_api(self):
        """A finished run, so `_conv` is live and stoppable. Two seats: the
        app refuses a solo conversation ("Pick at least two participants")."""
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["c1"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["g1"])
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({"opener": "hi", "turns": 1,
                           "seats": [{"id": 0, "provider": "claude", "enabled": True},
                                     {"id": 1, "provider": "gpt", "enabled": True}]})
        api._emit_q.join()
        self.assertIsNotNone(api._conv, "run did not start")
        return api

    def test_stop_sets_the_flag_and_cancels_every_seat(self):
        api = self._stopped_api()
        killed = []
        for a in api._conv["agents"]:
            a.cancel = lambda _a=a: killed.append(_a) or True
        r = api.stop()
        self.assertTrue(r["ok"])
        self.assertTrue(api._stop_flag.is_set())      # loop ends at boundary
        self.assertEqual(len(killed), 2)              # AND children die now

    def test_stop_honours_the_press_with_nothing_running(self):
        api = app.Api()
        api._window = FakeWindow()
        r = api.stop()
        self.assertTrue(r["ok"])
        self.assertTrue(api._stop_flag.is_set())
        self.assertEqual(r["stopped"], 0)

    def test_stop_refuses_a_chat_this_window_does_not_own(self):
        api = self._stopped_api()
        killed = []
        for a in api._conv["agents"]:
            a.cancel = lambda: killed.append(1) or True
        r = api.stop("some-other-chat")
        self.assertEqual(killed, [])     # never stop the wrong conversation
        self.assertEqual(r["stopped"], 0)
        self.assertIn("No such chat", r["note"])

    def test_two_chats_coexist_and_stop_independently(self):
        """The registry's whole point: a second chat starts without ending the
        first, and stopping one leaves the other's flag and agents alone."""
        api = self._stopped_api()
        first = api._runs.focused()
        self.assertIsNotNone(first.id)
        api._runs.new_draft()                       # Josh clicks "new chat"
        self.assertIsNone(api._runs.focused().id)   # a fresh stage...
        self.assertIsNotNone(first.state)           # ...and chat one survives
        killed = []
        for a in first.state["agents"]:
            a.cancel = lambda: killed.append(1) or True
        # stop the BACKGROUND chat by id while the draft is focused
        r = api.stop(first.id)
        self.assertEqual(r["stopped"], 2)
        self.assertTrue(first.stop_flag.is_set())
        self.assertFalse(api._runs.focused().stop_flag.is_set())

    def test_each_loop_keeps_its_own_stop_flag_after_focus_moves(self):
        api = self._stopped_api()
        first = api._runs.focused()
        io_first = app._AppIO(api, first)
        api._runs.new_draft()                       # focus moves away
        self.assertFalse(io_first.should_stop())
        api._runs.focused().stop_flag.set()         # stop the DRAFT
        self.assertFalse(io_first.should_stop(),
                         "stopping the visible chat stopped a background run")
        first.stop_flag.set()
        self.assertTrue(io_first.should_stop())

    def test_run_events_carry_their_chat_id(self):
        api = self._stopped_api()
        run = api._runs.focused()
        app._AppIO(api, run).emit("status", {"text": "x"})
        api._emit_q.join()
        last = api._window.events()[-1]
        self.assertEqual(last["payload"]["chat_id"], run.id)

    def test_run_status_snapshot_covers_every_chat(self):
        api = self._stopped_api()
        first = api._runs.focused()
        snap = api.run_status()["runs"]
        self.assertEqual([r["chat_id"] for r in snap], [first.id])
        self.assertEqual(snap[0]["status"], "done")
        self.assertFalse(snap[0]["running"])
        self.assertIsNone(snap[0]["pending_ask"])
        # scoped read returns just that chat; an unknown id returns nothing
        self.assertEqual(len(api.run_status(first.id)["runs"]), 1)
        self.assertEqual(api.run_status("nope")["runs"], [])

    def test_stop_marks_the_run_stopping(self):
        api = self._stopped_api()
        run = api._runs.focused()
        for a in run.state["agents"]:
            a.cancel = lambda: True
        api.stop(run.id)
        self.assertEqual(run.status, "stopping")

    def test_a_bad_status_never_freezes_a_rail_row(self):
        api = self._stopped_api()
        run = api._runs.focused()
        api._set_status(run, "gibberish")
        self.assertIn(run.status, app.Api.RUN_STATES)

    def test_workspace_reads_are_scoped_to_their_chat(self):
        """chat A's files must not resolve through chat B, and an unknown
        chat resolves to NOTHING rather than to the focused run."""
        api = self._stopped_api()
        first = api._runs.focused()
        ws = api._active_workspace(first.id)
        self.assertTrue(ws)
        self.assertEqual(api._active_workspace(), ws)      # focused default
        self.assertIsNone(api._active_workspace("no-such-chat"))
        r = api.read_text("anything.txt", chat_id="no-such-chat")
        self.assertIn("error", r)
        self.assertEqual(api.list_workspace_files(chat_id="no-such-chat"),
                         {"workspace": None, "files": []})

    def test_approve_plan_wakes_the_blocked_loop(self):
        """approve_plan must ANSWER the question the loop sleeps on. Flipping
        capability flags directly left the card saying "Executing" while the
        conversation thread stayed blocked forever."""
        import threading
        api = app.Api()
        api._window = FakeWindow()
        run = api._runs.focused()
        run.state = {"plan": {"phase": "awaiting", "id": "plan-1",
                              "revision": 1, "qid": "q1", "tasks": []}}
        waiter = queue.Queue()
        api._ask_waiters["q1"] = waiter
        r = api.approve_plan(None, "plan-1",
                             {"approved": True, "tasks": [{"id": "t1"}]})
        self.assertTrue(r["ok"])
        answer = waiter.get(timeout=2)          # the loop would now wake
        self.assertTrue(answer["approved"])
        self.assertEqual(answer["tasks"][0]["id"], "t1")

    def test_approve_plan_rejects_a_stale_card(self):
        api = app.Api()
        api._window = FakeWindow()
        run = api._runs.focused()
        run.state = {"plan": {"phase": "awaiting", "id": "plan-2",
                              "revision": 2, "qid": "q2", "tasks": []}}
        api._ask_waiters["q2"] = queue.Queue()
        old = api.approve_plan(None, "plan-1", {"approved": True})
        self.assertFalse(old["ok"])             # wrong id
        stale = api.approve_plan(None, "plan-2",
                                 {"approved": True, "revision": 1})
        self.assertFalse(stale["ok"])           # wrong revision
        self.assertTrue(api._ask_waiters["q2"].empty())

    def test_approve_plan_with_nothing_waiting_is_an_honest_error(self):
        api = self._stopped_api()
        r = api.approve_plan()
        self.assertFalse(r["ok"])
        self.assertIn("waiting", r["error"])

    def test_stop_seat_cancels_one_seat_and_leaves_the_flag_clear(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["c1"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["g1"])
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({"opener": "hi", "turns": 1,
                           "seats": [{"id": 0, "provider": "claude", "enabled": True},
                                     {"id": 1, "provider": "gpt", "enabled": True}]})
        api._emit_q.join()
        hit = []
        for a in api._conv["agents"]:
            a.cancel = lambda _a=a: hit.append(_a.name) or True
        r = api.stop_seat(None, api._conv["slot_ids"][1])
        self.assertEqual(r["stopped"], 1)
        self.assertEqual(hit, ["GPT"])                # only that seat
        self.assertFalse(api._stop_flag.is_set())     # conversation continues

    def test_stop_seat_reports_a_seat_that_is_not_mid_turn(self):
        api = self._stopped_api()
        r = api.stop_seat(None, api._conv["slot_ids"][0])
        self.assertTrue(r["ok"])
        self.assertEqual(r["stopped"], 0)             # honest, not a lie

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
        # run_status brackets the loop: `running` once the run really starts,
        # then the terminal state, so the rail never has to infer either.
        self.assertEqual(names, ["started", "message", "run_status",
                                 "thinking", "thinking_done", "message",
                                 "thinking", "thinking_done", "message",
                                 "run_status", "done"])
        status = [e for e in events if e["event"] == "run_status"]
        self.assertEqual([e["payload"]["status"] for e in status],
                         ["running", "done"])
        self.assertTrue(all(e["payload"]["chat_id"] for e in status))
        # opener row
        self.assertEqual(events[1]["payload"]["speaker"], "josh")
        self.assertEqual(events[1]["payload"]["text"], "hello agents")
        # agent rows carry seat id + provider + name (make_log rows).
        # Selected BY TYPE, not by index: positional picks break every time a
        # new event joins the stream, which says nothing about the behaviour.
        agent_rows = [e["payload"] for e in events if e["event"] == "message"
                      and e["payload"].get("speaker") != "josh"]
        m1 = agent_rows[0]
        self.assertEqual((m1["speaker"], m1["provider"], m1["name"],
                          m1["text"], m1["round"]),
                         (0, "claude", "Claude", "c1", 1))
        m2 = agent_rows[1]
        self.assertEqual((m2["speaker"], m2["name"], m2["text"]),
                         (1, "GPT", "g1"))
        # done promises a resumable chat, read back from what was persisted
        done = events[-1]["payload"]
        self.assertTrue(done["can_continue"], done.get("can_continue_reason"))
        self.assertIsNone(done["feedback"].get("rating"))

        saved = api.submit_feedback("not_helpful", ["incomplete"],
                                    "Needed another round")
        self.assertTrue(saved["ok"])
        feedback = outcome.read_outcome(api._session_dir)["human_feedback"]
        self.assertEqual(feedback["rating"], "not_helpful")
        self.assertEqual(feedback["reasons"], ["incomplete"])
        self.assertEqual(feedback["note"], "Needed another round")

    def test_feedback_bridge_rejects_invalid_or_missing_session(self):
        api = app.Api()
        self.assertIn("error", api.submit_feedback("helpful"))
        api._session_dir = self.tmp
        self.assertIn("error", api.submit_feedback("amazing"))

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
