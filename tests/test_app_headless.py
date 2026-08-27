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
        """A finished run, so `_conv` is live and stoppable. Two seats
        because the per-seat stop tests need a seat to bench and one to keep
        going -- a solo conversation is legal now (see test_solo.py)."""
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

    def test_a_seat_mid_turn_survives_reopening_the_chat(self):
        """Typing indicators are live-only in the UI, so a reopened chat whose
        seats are mid-turn rendered as idle. open_session replays them."""
        api = self._stopped_api()
        run = api._runs.focused()
        io = app._AppIO(api, run)
        io.emit("thinking", {"speaker": 0, "provider": "claude",
                             "name": "Claude", "limit": 900})
        api._emit_q.join()
        self.assertEqual(len(run.thinking), 1)
        payload = api.open_session(run.id)["thinking"]
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["name"], "Claude")
        self.assertEqual(payload[0]["limit"], 900)
        self.assertGreater(payload[0]["started"], 0,
                           "the UI needs the true age, not a fresh 0:00")
        io.emit("thinking_done", {"speaker": 0})
        api._emit_q.join()
        self.assertEqual(api.open_session(run.id)["thinking"], [])

    def test_a_side_call_in_flight_survives_reopening_the_chat(self):
        """Same rule as the typing indicators, for the relay's OWN work: a
        chat reopened 90 seconds into a supervisor plan must not render as an
        idle one just because the indicator is live-only."""
        api = self._stopped_api()
        run = api._runs.focused()
        io = app._AppIO(api, run)
        with relay.working(io, "plan", "make it better"):
            api._emit_q.join()
            payload = api.open_session(run.id)["working"]
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["what"], "Planning the work")
            self.assertEqual(payload[0]["detail"], "make it better")
            self.assertGreater(payload[0]["started"], 0,
                               "the UI needs the true age, not a fresh 0:00")
        api._emit_q.join()
        self.assertEqual(api.open_session(run.id)["working"], [])

    def test_a_new_run_starts_with_no_side_call_left_over(self):
        api = self._stopped_api()
        run = api._runs.focused()
        run.working["stale"] = {"id": "stale", "what": "Planning the work"}
        api._rounds(run.state)
        api._emit_q.join()
        self.assertEqual(run.working, {})

    def test_a_finished_run_reports_nobody_mid_turn(self):
        """Whatever the last event managed to say before the loop ended."""
        api = self._stopped_api()
        run = api._runs.focused()
        run.thinking["0"] = {"speaker": 0, "name": "stale"}
        api._rounds(run.state)
        api._emit_q.join()
        self.assertEqual(run.thinking, {})

    # ---- picking a chat back up after the app was killed ----------------
    def _sandbox_relay_paths(self):
        """Point relay's OWN globals at the temp dir for this test.

        setUp only redirects `app.SESSIONS_DIR`; `session_path` and
        `TABS_FILE` are relay's, so without this a tabs test writes the real
        `sessions/tabs.json` and throws away whatever Josh had open. (It did,
        once. Restored by hand.)
        """
        old_dir, old_tabs = relay.SESSIONS_DIR, relay.TABS_FILE
        old_mem = relay.MEMORY_DIR
        relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        # MEMORY_DIR is a SIBLING of sessions/, so it needs its own line: a
        # bridge test left pointing at the real one reads Josh's notes into
        # its preambles and writes structural ones back.
        relay.MEMORY_DIR = os.path.join(self.tmp, "memory")

        def restore():
            relay.SESSIONS_DIR, relay.TABS_FILE = old_dir, old_tabs
            relay.MEMORY_DIR = old_mem
        self.addCleanup(restore)

    def _set_completion(self, run, completion):
        meta_path = os.path.join(run.session_dir, "meta.json")
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        meta["completion"] = completion
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f)

    def _killed_mid_run_chat(self):
        """A finished run, then doctored to look like the process died: the
        exact shape found on disk on 2026-08-23 (lifecycle active, no
        termination_reason, because run_rounds' finally never ran)."""
        api = self._stopped_api()
        run = api._runs.focused()
        self._sandbox_relay_paths()
        self._set_completion(run, {"lifecycle": "active",
                                   "goal_verdict": "unknown"})
        relay.write_tabs({"open": [{"id": run.id, "color": ""}],
                          "active": run.id})
        return api, run

    def test_a_chat_killed_mid_run_is_offered_for_resume(self):
        api, run = self._killed_mid_run_chat()
        plan = api.restart_resume()
        self.assertEqual(plan["session_id"], run.id)
        self.assertTrue(plan["resume"])
        self.assertIn("still running when the app closed", plan["reason"])

    def test_a_chat_that_ENDED_is_reopened_but_never_resumed(self):
        """Every ending except a killed process was somebody's decision."""
        api, run = self._killed_mid_run_chat()
        self._set_completion(run, {"lifecycle": "paused",
                                   "termination_reason": "wrap"})
        plan = api.restart_resume()
        self.assertEqual(plan["session_id"], run.id, "still reopened")
        self.assertFalse(plan["resume"], "but not resumed")

    def test_two_barren_auto_resumes_stop_the_third(self):
        """Otherwise a chat that crashes on resume bills itself in a loop."""
        api, run = self._killed_mid_run_chat()
        self.assertTrue(api.restart_resume()["resume"])
        self.assertEqual(api.note_auto_resume(run.id)["count"], 1)
        self.assertTrue(api.restart_resume()["resume"], "one is not a loop")
        self.assertEqual(api.note_auto_resume(run.id)["count"], 2)
        plan = api.restart_resume()
        self.assertFalse(plan["resume"])
        self.assertIn("no turns", plan["reason"])

    def test_a_resume_that_produced_a_turn_resets_the_guard(self):
        api, run = self._killed_mid_run_chat()
        api.note_auto_resume(run.id)
        api.note_auto_resume(run.id)
        self.assertFalse(api.restart_resume()["resume"])
        meta_path = os.path.join(run.session_dir, "meta.json")
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        meta["turn"] = int(meta.get("turn") or 0) + 1   # it committed one
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        self.assertTrue(api.restart_resume()["resume"],
                        "progress clears the guard")

    def test_no_saved_tab_means_nothing_to_resume(self):
        api, _run = self._killed_mid_run_chat()
        relay.write_tabs({"open": [], "active": None})
        self.assertEqual(api.restart_resume(), {})

    def test_supervisor_picker_and_public_trace_reach_the_live_run(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class(
            "Claude", ["Claude finished the research."])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", [
            "Independent research tracks.\n"
            "[[TASK: c | owner=0 | inspect the UI]]\n"
            "[[TASK: g | owner=1 | inspect the engine]]",
            "GPT finished the research.",
        ])
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({
            "opener": "audit the app", "turns": 1, "mode": "supervisor",
            "supervisor": {"provider": "gpt", "model": "gpt-test",
                           "effort": "low"},
            "seats": [{"id": 0, "provider": "claude", "enabled": True},
                      {"id": 1, "provider": "gpt", "enabled": True}],
        })
        api._emit_q.join()
        self.assertEqual(api._conv["supervisor"],
                         {"provider": "gpt", "model": "gpt-test",
                          "effort": "low"})
        phases = [e["phase"] for e in api._conv["supervisor_trace"]]
        self.assertIn("planning", phases)
        self.assertIn("instruction", phases)
        self.assertIn("verification", phases)
        events = api._window.events()
        self.assertTrue(any(e["event"] == "supervisor" for e in events))

    # ---- Keep Improving reaches the engine through the bridge ----------
    def test_keep_improving_cfg_reaches_the_engine_unbounded(self):
        """The app's job here is to hand the engine an unbounded run with the
        limits Josh acknowledged — nothing else can express "runs forever"."""
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["c1"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["g1"])
        api = app.Api()
        api._window = FakeWindow()
        # Continuous mode has NO cap, so the test needs a limit that actually
        # trips: scripted agents report no cost, so a spend cap never would,
        # while the accumulated clock passes any positive hours value at the
        # second barrier. That asymmetry is itself worth knowing.
        api._conv = None
        api._conversation({
            "opener": "keep improving it", "turns": 3, "mode": "supervisor",
            "continuous": {"on": True,
                           "checkin": {"minutes": 45, "action": "permission"},
                           "limits": {"spend_usd": 0.0001, "hours": 1e-9,
                                      "watchdog_may_stop": False},
                           "gate": {"command": "", "commit": True}},
            "seats": [{"id": 0, "provider": "claude", "enabled": True},
                      {"id": 1, "provider": "gpt", "enabled": True}],
        })
        api._emit_q.join()
        state = api._conv
        pol = state["continuous"]
        self.assertTrue(pol["on"])
        self.assertEqual(pol["checkin"], {"minutes": 45,
                                          "action": "permission"})
        self.assertEqual(pol["limits"]["spend_usd"], 0.0001)
        self.assertEqual(pol["limits"]["hours"], 1e-9)
        self.assertFalse(pol["limits"]["watchdog_may_stop"])
        self.assertEqual(state["completion"]["termination_reason"], "limit",
                         "a limit is not a cap and not a stop")
        # the two things only the app can get wrong
        self.assertTrue(state["until_done"])
        self.assertIsNone(state["turn_ceiling"])
        self.assertIsNone(relay.effective_ceiling(state))

    def test_an_ordinary_app_chat_gets_no_continuous_block(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["c1"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["g1"])
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({
            "opener": "hi", "turns": 1,
            "seats": [{"id": 0, "provider": "claude", "enabled": True},
                      {"id": 1, "provider": "gpt", "enabled": True}]})
        api._emit_q.join()
        self.assertIsNone(api._conv["continuous"])
        self.assertEqual(relay.effective_ceiling(api._conv),
                         relay.DEFAULT_CEILING)

    def test_continuous_probe_answers_with_an_event_not_a_return(self):
        """git status is a subprocess, and subprocess.run deadlocks on the
        pywebview bridge thread — so this must follow the recheck_auth shape."""
        api = app.Api()
        api._window = FakeWindow()
        proj = os.path.join(self.tmp, "proj", "tests")
        os.makedirs(proj)
        open(os.path.join(proj, "run_all.py"), "w").close()
        self.assertEqual(api.continuous_probe(os.path.dirname(proj)),
                         {"ok": True})
        deadline = time.monotonic() + 10
        payload = None
        while time.monotonic() < deadline and payload is None:
            api._emit_q.join()
            for e in api._window.events():
                if e["event"] == "continuous_probe":
                    payload = e["payload"]
            if payload is None:
                time.sleep(0.05)
        self.assertIsNotNone(payload, "no continuous_probe event arrived")
        self.assertEqual(payload["command"], "python tests/run_all.py")
        self.assertIn("dirty", payload)
        self.assertIn("git", payload)

    def test_the_probe_survives_a_folder_that_does_not_exist(self):
        api = app.Api()
        api._window = FakeWindow()
        self.assertEqual(api.continuous_probe(""), {"ok": True})
        deadline = time.monotonic() + 10
        payload = None
        while time.monotonic() < deadline and payload is None:
            api._emit_q.join()
            for e in api._window.events():
                if e["event"] == "continuous_probe":
                    payload = e["payload"]
            if payload is None:
                time.sleep(0.05)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["command"], "",
                         "an empty path must NOT detect this repo's own runner")

    def _mid_job_supervised_chat(self):
        """A supervised run that stops with work still open — the state Josh
        actually reopens, and the one a drained-plan restore would misreport.
        Task `g` depends on `c`, so it is dispatched at settlement and is
        still live when the round cap ends the run."""
        relay.AGENT_TYPES["claude"] = scripted_agent_class(
            "Claude", ["Claude finished [c]."])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", [
            "Two tracks, the second waits on the first.\n"
            "[[TASK: c | owner=0 | inspect the UI]]\n"
            "[[TASK: g | owner=0 | deps=c | inspect the engine]]",
            "Nothing assigned to me yet.",
        ])
        # a fresh Api resolves the id off disk through relay.SESSIONS_DIR, not
        # the in-memory registry — patch both or the reopen looks deleted
        old_relay = relay.SESSIONS_DIR
        relay.SESSIONS_DIR = self.tmp
        self.addCleanup(setattr, relay, "SESSIONS_DIR", old_relay)
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({
            "opener": "audit the app", "turns": 1, "mode": "supervisor",
            "supervisor": {"provider": "gpt", "model": "gpt-test",
                           "effort": "low"},
            "seats": [{"id": 0, "provider": "claude", "enabled": True},
                      {"id": 1, "provider": "gpt", "enabled": True}],
        })
        api._emit_q.join()
        return api

    def test_reopening_mid_job_restores_a_LIVE_plan(self):
        api = self._mid_job_supervised_chat()
        sid = api._runs.focused().id
        statuses = {t["id"]: t["status"] for t in api._conv["workstreams"]}
        self.assertEqual(statuses["c"], "done")
        self.assertEqual(statuses["g"], "active",
                         "the dependent task should still be in flight")

        # a SECOND Api is the honest simulation of reopening in a new process:
        # nothing is inherited in memory, everything comes back off disk
        fresh = app.Api()
        fresh._window = FakeWindow()
        r = fresh.open_session(sid)
        self.assertNotIn("error", r)
        summary = r["session"]
        self.assertEqual(summary["mode"], "supervisor")
        self.assertEqual({t["id"]: t["status"] for t in summary["tasks"]},
                         {"c": "done", "g": "active"},
                         "a reopened chat must show the plan as it stands, "
                         "not a drained or restarted one")
        self.assertTrue(summary["supervisor_trace"],
                        "the control log survives the reopen")
        self.assertTrue(all(isinstance(e.get("wave"), int)
                            for e in summary["supervisor_trace"]),
                        "and every entry still knows its wave")
        self.assertEqual(summary["supervisor"],
                         {"provider": "gpt", "model": "gpt-test",
                          "effort": "low"},
                         "including which model was managing")
        self.assertEqual(summary["supervisor_goal"], "audit the app",
                         "the goal the plan was judged against comes back too")
        if summary["can_continue"]:
            self.assertEqual(
                [t["id"] for t in fresh._conv["workstreams"]], ["c", "g"],
                "and the rebuilt live state carries the same plan")
            self.assertEqual(fresh._conv["supervisor_goal"], "audit the app")

    def test_rail_badge_prefers_live_work_over_a_past_non_verdict(self):
        """The run above ended on the turn limit, so it recorded
        goal_unresolved for that run — but the chat is resumable with a task
        still open, and a row reading 'No verdict' would say the opposite of
        what continuing actually does."""
        api = self._mid_job_supervised_chat()
        sid = api._runs.focused().id
        fresh = app.Api()
        fresh._window = FakeWindow()
        summary = fresh.open_session(sid)["session"]
        status = summary["supervisor_status"]
        self.assertEqual(status["state"], "working")
        self.assertEqual(status["open"], 1)
        self.assertIn("open", status["label"])

    def test_supervisor_status_distinguishes_every_ending(self):
        base = {"mode": "supervisor", "supervisor_wave_index": 2,
                "workstreams": [{"id": "a", "status": "done"}]}
        self.assertIsNone(relay.supervisor_status({"mode": "parallel"}),
                          "an ordinary chat carries no supervision badge")
        self.assertEqual(
            relay.supervisor_status(dict(base, workstreams=[]))["state"],
            "planning")
        self.assertEqual(relay.supervisor_status(base)["state"], "settled")
        closed = dict(base, supervisor_trace=[{"type": "goal_accepted"}])
        self.assertEqual(relay.supervisor_status(closed)["state"], "accepted")
        self.assertEqual(relay.supervisor_status(closed)["label"],
                         "Goal accepted")
        spent = dict(base, supervisor_trace=[{"type": "goal_unresolved"}])
        self.assertEqual(relay.supervisor_status(spent)["state"], "unresolved",
                         "settled work with no verdict is NOT 'accepted'")

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
        visible = api._runs.focused()
        r = api.stop("some-other-chat")
        self.assertEqual(killed, [])     # never stop the wrong conversation
        self.assertEqual(r["stopped"], 0)
        self.assertFalse(r["ok"])
        self.assertIn("No such chat", r["error"])
        # ...and the flag, which this test never checked: a NAMED chat it
        # does not own used to set the FOCUSED run's flag instead, which is
        # exactly what _resolve_chat exists to prevent
        self.assertFalse(visible.stop_flag.is_set())

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

    def test_new_chat_does_not_stop_a_running_one(self):
        """Josh's actual ask. reset_conversation used to answer "Stop the
        conversation first", which made the whole run registry unreachable."""
        api = self._stopped_api()
        run = api._runs.focused()
        run.thread = threading.Thread(target=lambda: time.sleep(5),
                                      daemon=True)
        run.thread.start()
        r = api.reset_conversation()
        self.assertTrue(r["ok"])
        self.assertEqual(r["backgrounded"], run.id)
        self.assertIsNotNone(run.state, "the running chat was torn down")
        self.assertIsNone(api._runs.focused().id, "no fresh stage to type in")

    def test_opening_another_chat_leaves_the_running_one_alone(self):
        api = self._stopped_api()
        first = api._runs.focused()
        first.thread = threading.Thread(target=lambda: time.sleep(5),
                                        daemon=True)
        first.thread.start()
        # reopening the SAME chat focuses it rather than rebuilding its agents
        # (two Agent objects on one CLI session id shred continuity)
        agents_before = first.state["agents"]
        r = api.open_session(first.id)
        self.assertTrue(r["ok"])
        self.assertTrue(r["live"])
        self.assertIs(api._conv["agents"], agents_before, "agents rebuilt")

    def test_interject_and_command_reach_the_named_chat(self):
        api = self._stopped_api()
        first = api._runs.focused()
        api._runs.new_draft()                 # Josh is composing a new chat
        api.interject("for the background chat", None, first.id)
        self.assertEqual(first.human_q.get_nowait(),
                         "for the background chat")
        first.thread = threading.Thread(target=lambda: time.sleep(5),
                                        daemon=True)
        first.thread.start()
        api.command("/compact", first.id)
        self.assertEqual(first.human_q.get_nowait(), "/compact")
        self.assertTrue(api._runs.focused().human_q.empty())

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
        # then the terminal state, so the rail never has to infer either. The
        # `working` pair brackets pre-flight setup - the stretch with no seat
        # in it yet, which is exactly the window that used to read as a dead
        # app; it closes before `started` because that is when setup is done.
        self.assertEqual(names, ["working", "working",
                                 "started", "message", "run_status",
                                 "thinking", "thinking_done", "message",
                                 "thinking", "thinking_done", "message",
                                 "run_status", "done"])
        setup = [e["payload"] for e in events if e["event"] == "working"]
        self.assertEqual(setup[0]["phase"], "setup")
        self.assertEqual(setup[0]["id"], setup[1]["id"])
        self.assertTrue(setup[1]["done"])
        status = [e for e in events if e["event"] == "run_status"]
        self.assertEqual([e["payload"]["status"] for e in status],
                         ["running", "done"])
        self.assertTrue(all(e["payload"]["chat_id"] for e in status))
        # opener row — by type, for the reason spelled out just below
        opener = next(e["payload"] for e in events if e["event"] == "message"
                      and e["payload"].get("speaker") == "josh")
        self.assertEqual(opener["text"], "hello agents")
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
