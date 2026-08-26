"""Restart/continuity proof, token-free.

A session written by the REAL shared loop (FakeAgent seats, real SessionStore)
is reopened by a FRESH app.Api against a fake window — the honest simulation
of an app restart, where nothing survives in memory and everything must come
back off disk: seats, models, CLI session ids, owed queues, round/scheduler
state, until-done ceiling and spawn bookkeeping — and then actually CONTINUES
through the app's own resume path without forging or losing a turn.

The marker cases pin the RESTART_DESIGN §5 handoff contract (.alloy-restart.json:
atomic write, strict parse, consumed only on a verified open, corrupt markers
fail loudly and are quarantined — never half-interpreted). Wave 2 moves this
reader into the app's startup path; when it lands, swap the local helpers for
the real import and these tests become its acceptance criteria.

Run:  python tests/test_restart.py
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
from relay import Agent, run_rounds

from test_loop import (FakeAgent, RecordingIO, build_state, jsonl_rows,
                       saved_meta)


class ResumableFake(FakeAgent):
    """FakeAgent shaped for rehydrate(): built WITHOUT a script (the saved
    meta is the script), then scripted again for the resumed lap."""

    def __init__(self, workspace, name=None, **kw):
        super().__init__(workspace, [], name=name, **kw)


def scripted(cls_name, replies):
    """An adapter-shaped fake for the _conversation path: real constructor
    signature, one shared reply pool popped per turn."""
    pool = list(replies)

    class Scripted(Agent):
        name = cls_name
        cli = "fake"

        def __init__(self, workspace, name=None, **kw):
            super().__init__(workspace, name=name, **kw)
            self.script = pool

        def turn(self, message, on_activity=None):
            item = self.script.pop(0) if self.script else "(out of script)"
            # real adapters re-capture a session id in parse() every call;
            # without one, continue_block rightly rules the chat unresumable
            self.session_id = f"fake-session-{self.uid}"
            return item

    return Scripted


class FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)

    def events(self):
        out = []
        for s in self.calls:
            body = s[len("uiEvent("):-1]
            out.append(json.loads(body))
        return out


# ------------------------------------------------------------- marker ----
# The §5 handoff, both sides. The WRITER is the dying process's last act;
# the READER is the child's first decision: strict parse, loud refusal,
# quarantine on definitive failure — a half-valid marker must never launch
# anything and must never be silently eaten.

MARKER_NAME = ".alloy-restart.json"
MARKER_FAILED_SUFFIX = ".failed.json"


class MarkerError(Exception):
    """One legible sentence for why the handoff marker was refused."""


def write_marker(path, payload):
    """Atomic replace semantics (tmp + os.replace), UTF-8 JSON — what the
    integration must do as the last act before exit."""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_marker(path):
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except OSError as exc:
        raise MarkerError("restart marker unreadable: %s" % exc) from exc
    except ValueError as exc:
        raise MarkerError(
            "restart marker is not valid JSON — refusing to guess") from exc
    if not isinstance(record, dict):
        raise MarkerError("restart marker is not an object")
    if record.get("v") != 1:
        raise MarkerError("unknown restart marker version %r"
                          % (record.get("v"),))
    sid = record.get("session_id")
    if not isinstance(sid, str) or not sid.strip():
        raise MarkerError("restart marker names no session to reopen")
    return record


def quarantine_marker(path):
    """§5's definitive-refusal path: rename aside for the post-mortem,
    never delete silently; startup finds no active marker and proceeds."""
    os.replace(path, path + MARKER_FAILED_SUFFIX)


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-restart-")
        # _conversation builds the session dir from app.SESSIONS_DIR, but
        # open_session/session_path resolve ids via relay.SESSIONS_DIR
        self._old_app_sessions = app.SESSIONS_DIR
        self._old_relay_sessions = relay.SESSIONS_DIR
        self._old_types = dict(relay.AGENT_TYPES)
        app.SESSIONS_DIR = relay.SESSIONS_DIR = self.tmp
        relay.AGENT_TYPES["claude"] = ResumableFake

    def tearDown(self):
        app.SESSIONS_DIR = self._old_app_sessions
        relay.SESSIONS_DIR = self._old_relay_sessions
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------- loop-written --
    def test_loop_session_reopens_faithfully_and_continues(self):
        state = build_state(self.tmp, [["a1", "a2"], ["b1", "b2"]], turns=2)
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")

        sid = os.path.basename(state["store"].dir)
        snap = json.loads(json.dumps(saved_meta(state)))

        # a SECOND Api is the restart: nothing inherited in memory
        fresh = app.Api()
        fresh._window = FakeWindow()
        r = fresh.open_session(sid)
        fresh._emit_q.join()          # flush before asserting
        self.assertNotIn("error", r)
        self.assertTrue(r["ok"])
        self.assertFalse(r["live"])

        # --- summary restores the roster and round state faithfully --------
        s = r["session"]
        self.assertTrue(s["can_continue"], s["can_continue_reason"])
        self.assertEqual(s["rounds"], snap["rnd"])
        self.assertEqual(s["max"], snap["max"])
        self.assertEqual(s["mode"], snap["mode"])
        self.assertEqual(s["title"], snap["title"])
        self.assertEqual(s["until_done"], bool(snap.get("until_done")))
        self.assertEqual(s["workspace"], snap["workspace"])
        self.assertEqual([p["id"] for p in s["participants"]],
                         [x["id"] for x in snap["seats"]])
        for got, want in zip(s["participants"], snap["seats"]):
            self.assertEqual(got["provider"], want["provider"])
            self.assertEqual(got["name"], want.get("label"))
            self.assertEqual(got["model"], want.get("model") or "default")
            self.assertEqual(got["effort"], want.get("effort") or "")
            self.assertEqual(got["role"], want.get("role") or "")

        # --- messages replay exactly what the first run recorded ----------
        msgs = r["messages"]
        self.assertEqual([m["text"] for m in msgs], ["a1", "b1", "a2", "b2"])
        self.assertEqual([m["speaker"] for m in msgs], [0, 1, 0, 1])
        self.assertEqual([m["provider"] for m in msgs], ["claude"] * 4)
        self.assertEqual([m["name"] for m in msgs],
                         ["Fake 1", "Fake 2", "Fake 1", "Fake 2"])
        self.assertEqual([m["round"] for m in msgs], [1, 1, 2, 2])
        for got, want in zip(msgs, jsonl_rows(state)):
            self.assertEqual(got["message_id"], want["message_id"])
            self.assertEqual(got["ts"], want["ts"])

        # --- live state rebuilt from disk matches the snapshot -------------
        conv = fresh._conv
        self.assertIsNotNone(conv)
        self.assertEqual(conv["slot_ids"], [x["id"] for x in snap["seats"]])
        self.assertEqual(conv["providers"],
                         [x["provider"] for x in snap["seats"]])
        self.assertEqual([a.name for a in conv["agents"]],
                         [x.get("label") for x in snap["seats"]])
        self.assertEqual([a.model for a in conv["agents"]],
                         [x.get("model") for x in snap["seats"]])
        self.assertEqual([a.effort for a in conv["agents"]],
                         [x.get("effort") for x in snap["seats"]])
        # THE continuity field: each seat resumes ITS OWN CLI conversation
        self.assertEqual([a.session_id for a in conv["agents"]],
                         [x["session_id"] for x in snap["seats"]])
        # queues restore exactly as persisted: seat 1 is still owed b2
        self.assertEqual(conv["pending"],
                         {i: list(x.get("pending") or [])
                          for i, x in enumerate(snap["seats"])})
        self.assertEqual(conv["pending"][0], ["Fake 2 said:\nb2"])
        self.assertEqual(conv["pending"][1], [])
        self.assertEqual(conv["rnd"], snap["rnd"])
        self.assertEqual(conv["max"], snap["max"])
        self.assertEqual(conv["turn"], snap["turn"])
        self.assertEqual(conv["cursor"], snap["cursor"])
        self.assertEqual(conv["introduced"],
                         [bool(x.get("introduced")) for x in snap["seats"]])
        self.assertEqual(conv["floor_turns"], snap.get("floor_turns"))
        self.assertIsNone(conv.get("brief"))
        # runtime plumbing a resume needs, attached by open_session
        self.assertEqual(conv["store"].dir, os.path.join(self.tmp, sid))
        self.assertTrue(os.path.isfile(conv["transcript"]))
        self.assertTrue(callable(conv["log"]))

        # --- and it CONTINUES through the app's own resume path -----------
        conv["agents"][0].script = ["a3"]
        conv["agents"][1].script = ["b3"]
        fresh._continue({"opener": "go on", "turns": 1})
        fresh._emit_q.join()

        a, b = conv["agents"]
        resumed_prompt = a.prompts[-1]
        self.assertIn("Fake 2 said:\nb2", resumed_prompt,
                      "the reply owed at the cap must survive the restart")
        self.assertIn("Josh (human) says: go on", resumed_prompt)
        self.assertNotIn("You are Fake 1", resumed_prompt,
                         "an introduced seat must not be re-preambled")
        rows = jsonl_rows(conv)
        self.assertEqual([(x["speaker"], x["text"]) for x in rows],
                         [(0, "a1"), (1, "b1"), (0, "a2"), (1, "b2"),
                          ("josh", "go on"), (0, "a3"), (1, "b3")])
        self.assertEqual([x["round"] for x in rows][-2:], [3, 3])

        meta_after = saved_meta(conv)
        self.assertEqual(meta_after["rnd"], 3)
        self.assertEqual(meta_after["max"], 3)
        # the run capped on b3, so seat 1's reply is owed AGAIN — and the
        # restarted process must persist that debt just like the first did
        self.assertEqual([x["pending"] for x in meta_after["seats"]],
                         [["Fake 2 said:\nb3"], []])
        with open(conv["transcript"], encoding="utf-8") as f:
            tr = f.read()
        for piece in ("a1", "b2", "go on", "a3", "b3"):
            self.assertIn(piece, tr)

    # ----------------------------------------------- until-done + spawn ----
    def test_until_done_and_spawn_state_survive_a_restart(self):
        relay.AGENT_TYPES["claude"] = scripted(
            "Claude", ["working...", "done. [[WRAP]]"])
        relay.AGENT_TYPES["gpt"] = scripted("GPT", ["working too", "g-close"])
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({
            "opener": "work until done", "turns": 5,
            "until_done": True, "ceiling": 5,
            "spawn": {"tier1": True, "max_helpers": 2, "max_teams": 1},
            "seats": [
                {"id": 0, "provider": "claude", "enabled": True,
                 "model": "claude-haiku-4-5", "effort": "low"},
                {"id": 1, "provider": "gpt", "enabled": True,
                 "model": "gpt-5.6-sol", "effort": "low"}]})
        api._emit_q.join()
        st = api._conv
        agent_texts = [x["text"] for x in jsonl_rows(st)
                       if x["speaker"] not in ("system", "josh")]
        self.assertEqual(agent_texts,
                         ["working...", "working too",
                          "done. [[WRAP]]", "g-close"])
        # consume spawn budget the way the engine records it: plain counter
        # bumps on state["spawn"], persisted by the next save
        st["spawn"]["helpers_used"] = 1
        st["spawn"]["teams_used"] = 1
        st["store"].save(st)

        sid = os.path.basename(api._session_dir)
        snap = json.loads(json.dumps(relay.read_meta(api._session_dir)))

        fresh = app.Api()
        fresh._window = FakeWindow()
        r = fresh.open_session(sid)
        fresh._emit_q.join()
        s = r["session"]
        self.assertTrue(s["can_continue"], s["can_continue_reason"])
        self.assertTrue(s["until_done"])
        self.assertEqual(s["spawn"], snap["spawn"])
        self.assertEqual(s["spawn"]["helpers_used"], 1)
        self.assertEqual(s["spawn"]["teams_used"], 1)
        self.assertEqual(s["orchestration"]["budget"]["until_done"], True)
        self.assertEqual(s["orchestration"]["budget"]["limit"], 5)

        conv = fresh._conv
        self.assertTrue(conv["until_done"])
        self.assertEqual(conv["turn_ceiling"], snap["turn_ceiling"])
        self.assertEqual(conv["turn"], snap["turn"])
        self.assertEqual(conv["spawn"], snap["spawn"])
        self.assertEqual([(a.name, a.model, a.effort) for a in conv["agents"]],
                         [("Claude", "claude-haiku-4-5", "low"),
                          ("GPT", "gpt-5.6-sol", "low")])
        self.assertEqual([a.session_id for a in conv["agents"]],
                         [x["session_id"] for x in snap["seats"]])

        # resuming an until-done chat extends the SAFETY ceiling from the
        # restored turn count, never a round cap
        # resume honors the persisted cursor: whoever is owed the floor
        # speaks first, and each resumed reply lands in round rnd+1
        replies = {0: "c-again", 1: "g-again"}
        first_up = snap["cursor"] if snap["cursor"] is not None else 0
        conv["agents"][0].script = [replies[0]]
        conv["agents"][1].script = [replies[1]]
        fresh._continue({"opener": "", "ceiling": 2})
        fresh._emit_q.join()
        self.assertEqual(fresh._conv["turn_ceiling"],
                         snap["turn"] + 2)
        texts = [x["text"] for x in jsonl_rows(fresh._conv)
                 if x["speaker"] not in ("system", "josh")]
        self.assertEqual(texts, ["working...", "working too",
                                 "done. [[WRAP]]", "g-close",
                                 replies[first_up], replies[1 - first_up]])
        meta_after = saved_meta(fresh._conv)
        self.assertEqual(meta_after["turn_ceiling"], snap["turn"] + 2)
        self.assertEqual(meta_after["spawn"]["helpers_used"], 1)
        self.assertEqual(meta_after["spawn"]["teams_used"], 1)

    # ------------------------------------------------- §5 marker handoff --
    def test_marker_handoff_reopens_the_recorded_session(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        state["agents"][0].model = "claude-haiku-4-5"
        state["agents"][0].effort = "low"
        state["agents"][1].model = "gpt-5.6-sol"
        state["agents"][1].effort = "high"
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")
        snap = json.loads(json.dumps(saved_meta(state)))
        sid = os.path.basename(state["store"].dir)
        # the roster we are about to carry through the marker is real
        self.assertEqual([x["model"] for x in snap["seats"]],
                         ["claude-haiku-4-5", "gpt-5.6-sol"])
        self.assertEqual(snap["seats"][0]["pending"], ["Fake 2 said:\nb1"])

        marker = os.path.join(self.tmp, MARKER_NAME)
        write_marker(marker, {
            "v": 1, "session_id": sid, "wave": 3,
            "reason": "seat-requested",
            "gate": {"suites": 38, "tests": 939, "failed": []},
            "git_before": "ca8b859", "git_after": "ca8b859",
            "pid": os.getpid(), "ts": "2026-08-23T12:00:00",
        })

        # child side, exactly as the integration should consume it:
        # parse the marker, reopen THAT session off disk alone
        record = read_marker(marker)
        self.assertEqual(record["v"], 1)
        self.assertEqual(record["session_id"], sid)

        fresh = app.Api()
        fresh._window = FakeWindow()
        r = fresh.open_session(record["session_id"])
        fresh._emit_q.join()
        self.assertNotIn("error", r)
        conv = fresh._conv
        self.assertTrue(r["session"]["can_continue"])
        self.assertEqual(r["session"]["workspace"], snap["workspace"])
        self.assertEqual(conv["store"].dir, os.path.join(self.tmp, sid))
        self.assertEqual(conv["slot_ids"], [x["id"] for x in snap["seats"]])
        self.assertEqual([a.model for a in conv["agents"]],
                         [x["model"] for x in snap["seats"]])
        self.assertEqual([a.effort for a in conv["agents"]],
                         [x["effort"] for x in snap["seats"]])
        self.assertEqual([a.session_id for a in conv["agents"]],
                         [x["session_id"] for x in snap["seats"]])
        self.assertEqual(conv["pending"],
                         {i: list(x["pending"])
                          for i, x in enumerate(snap["seats"])})
        self.assertEqual(conv["pending"][0], ["Fake 2 said:\nb1"])
        self.assertEqual([m["text"] for m in r["messages"]], ["a1", "b1"])

        # consumed only after a VERIFIED open — never before
        self.assertTrue(os.path.exists(marker))
        os.remove(marker)
        self.assertFalse(os.path.exists(marker))

    def test_corrupt_marker_fails_loudly_and_never_half_restarts(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        run_rounds(state, RecordingIO())
        sid = os.path.basename(state["store"].dir)
        marker = os.path.join(self.tmp, MARKER_NAME)

        bad = [
            (b'{"v": 1, "sessi', "truncated JSON"),
            (b'[1, 2, 3]', "JSON array, not an object"),
            ('{"v": 99, "session_id": "%s"}' % sid, "future version"),
            ('{"v": 1}', "missing session_id"),
            ('{"v": 1, "session_id": "   "}', "blank session_id"),
        ]
        for raw, label in bad:
            data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
            with open(marker, "wb") as f:
                f.write(data)
            with self.assertRaises(MarkerError) as ctx:
                read_marker(marker)
            self.assertIn("marker", str(ctx.exception), label)
            # a refusing reader leaves every byte untouched — nothing was
            # half-consumed on the way to the loud failure
            with open(marker, "rb") as f:
                self.assertEqual(f.read(), data, label)

        # §5's definitive refusal quarantines for the post-mortem; startup
        # then finds NO active marker and proceeds normally
        quarantine_marker(marker)
        self.assertFalse(os.path.exists(marker))
        self.assertTrue(os.path.exists(marker + MARKER_FAILED_SUFFIX))

        # and nothing of Josh's chat depended on the marker: it is still
        # right there on the rail, continuable without any handoff
        fresh = app.Api()
        fresh._window = FakeWindow()
        r = fresh.open_session(sid)
        fresh._emit_q.join()
        self.assertNotIn("error", r)
        self.assertTrue(r["session"]["can_continue"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
