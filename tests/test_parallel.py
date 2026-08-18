"""Phase 5 tests: the parallel (barrier) mode.

Token-free. Concurrency is exercised with randomized sleeps and gated fakes.
Run:  python tests/test_parallel.py
"""

import json
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
import app
from relay import Agent, run_rounds

from test_loop import FakeAgent, RecordingIO, build_state, saved_meta, jsonl_rows
from test_scheduler import agent_rows
from test_app_headless import FakeWindow, scripted_agent_class


def par_state(tmp, scripts, turns=3, labels=None, jitter=0.0):
    state = build_state(tmp, scripts, turns=turns, labels=labels)
    state["mode"] = "parallel"
    if jitter:
        for a in state["agents"]:
            real_turn = a.turn
            def jittered(message, on_activity=None, _real=real_turn):
                time.sleep(random.uniform(0, jitter))
                return _real(message, on_activity=on_activity)
            a.turn = jittered
    return state


def relayed_entries(text):
    """All 'NAME said:\\nreply' entries in a prompt/backlog blob."""
    return re.findall(r"(\w[\w ]*) said:\n([^\n]+)", text)


class BarrierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-par-")
        random.seed(20260816)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fanout_exactly_once_under_jitter(self):
        rounds, n = 4, 3
        scripts = [[f"{lb}{r}" for r in range(1, rounds + 1)]
                   for lb in ("a", "b", "c")]
        state = par_state(self.tmp, scripts, turns=rounds,
                          labels=["A", "B", "C"], jitter=0.02)
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")
        rows = agent_rows(state)
        self.assertEqual(len(rows), rounds * n)      # every turn committed
        # every reply reaches every OTHER seat exactly once: across all the
        # prompts a seat received plus its final pending backlog
        meta = saved_meta(state)
        for j, agent in enumerate(state["agents"]):
            seen = "\n\n".join(agent.prompts
                               + meta["seats"][j]["pending"])
            entries = relayed_entries(seen)
            for k, other in enumerate(state["agents"]):
                if k == j:
                    continue
                for reply in [f"{other.name.lower()}{r}"
                              for r in range(1, rounds + 1)]:
                    hits = [e for e in entries if e[1] == reply]
                    self.assertEqual(
                        len(hits), 1,
                        f"{agent.name} saw {reply!r} {len(hits)} times")

    def test_barrier_semantics_no_same_round_leak(self):
        rounds = 3
        scripts = [[f"{lb}{r}" for r in range(1, rounds + 1)]
                   for lb in ("a", "b")]
        state = par_state(self.tmp, scripts, turns=rounds, labels=["A", "B"],
                          jitter=0.01)
        run_rounds(state, RecordingIO())
        a, b = state["agents"]
        for r, prompt in enumerate(b.prompts, start=1):
            got = [e[1] for e in relayed_entries(prompt)]
            self.assertNotIn(f"a{r}", got,
                             f"round-{r} reply leaked into round-{r} prompt")
            if r > 1:
                self.assertIn(f"a{r-1}", got)

    def test_one_wrap_gives_everyone_else_a_closing_round(self):
        state = par_state(
            self.tmp,
            [["bye. [[WRAP]]", "a-should-not-happen"],
             ["b1", "b-close"], ["c1", "c-close"]],
            turns=5, labels=["A", "B", "C"])
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "wrapped")
        rows = jsonl_rows(state)
        by_round = {}
        for r in rows:
            if r["speaker"] not in ("system", "josh"):
                by_round.setdefault(r["round"], set()).add(r["text"])
        self.assertEqual(by_round[1],
                         {"bye. [[WRAP]]", "b1", "c1"})
        self.assertEqual(by_round[2], {"b-close", "c-close"})
        self.assertEqual(saved_meta(state)["closing"], [])

    def test_all_wrap_stops_immediately(self):
        state = par_state(
            self.tmp,
            [["done [[WRAP]]", "a-no"], ["same [[WRAP]]", "b-no"]],
            turns=5, labels=["A", "B"])
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "wrapped")
        self.assertEqual(len(agent_rows(state)), 2)

    def test_wrap_in_closing_round_does_not_extend(self):
        state = par_state(
            self.tmp,
            [["bye. [[WRAP]]"], ["b1", "b-close [[WRAP]]", "b-no"]],
            turns=5, labels=["A", "B"])
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "wrapped")
        self.assertEqual(len(agent_rows(state)), 3)   # a-wrap, b1, b-close

    def test_fatal_stops_after_the_barrier(self):
        dead = RuntimeError("No conversation found with session ID: bogus")
        state = par_state(
            self.tmp,
            [[dead], ["b1", "b2"], ["c1", "c2"]],
            turns=3, labels=["A", "B", "C"])
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "fatal")
        # the other seats' in-flight turns still committed
        self.assertEqual(set(agent_rows(state)), {"b1", "c1"})

    def test_skip_never_forges_or_loses(self):
        boom = RuntimeError("t1")
        state = par_state(
            self.tmp,
            [[boom, RuntimeError("t2"), "a2"], ["b1", "b2"]],
            turns=2, labels=["A", "B"])
        run_rounds(state, RecordingIO())
        rows = agent_rows(state)
        self.assertEqual(set(rows), {"b1", "a2", "b2"})
        # A's round-2 prompt still contains b1 (nothing was consumed on skip)
        a = state["agents"][0]
        self.assertIn("B said:\nb1", a.prompts[-1])


class GatedAgent(FakeAgent):
    """Blocks inside turn() until the test opens its gate — lets a test hold
    the round open and inspect on-disk state mid-flight, deterministically."""

    def __init__(self, workspace, script, name=None, **kw):
        super().__init__(workspace, script, name=name, **kw)
        self.gate = threading.Event()
        self.entered = threading.Event()

    def turn(self, message, on_activity=None):
        self.entered.set()
        if not self.gate.wait(timeout=10):
            raise RuntimeError("test gate never opened")
        return super().turn(message)


class MidRoundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-parmid-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_bg(self, state, io):
        t = threading.Thread(target=lambda: run_rounds(state, io),
                             daemon=True)
        t.start()
        return t

    def test_meta_valid_mid_round_commit_consume(self):
        # B commits while C is still thinking: the on-disk meta must show B's
        # reply already owed to A and C, and C's queue untouched
        state = par_state(self.tmp, [["a1"], ["b1"], ["c1"]],
                          turns=1, labels=["A", "B", "C"])
        gated = GatedAgent(state["workspace"], ["c1"], name="C")
        state["agents"][2] = gated
        t = self.run_bg(state, RecordingIO())
        self.assertTrue(gated.entered.wait(timeout=5))
        deadline = time.time() + 5
        while time.time() < deadline:
            meta = saved_meta(state)
            owed_a = meta["seats"][0]["pending"]
            if any("b1" in e for e in owed_a) and \
               any("a1" in e for e in owed_a[:0] + meta["seats"][2]["pending"]):
                break
            time.sleep(0.05)
        meta = saved_meta(state)
        self.assertTrue(any("b1" in e for e in meta["seats"][0]["pending"]))
        self.assertTrue(any("a1" in e for e in meta["seats"][2]["pending"]))
        # C consumed nothing yet: its queue still owes it a1 AND b1
        gated.gate.set()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())

    def test_interjection_lands_mid_round_and_reaches_all(self):
        state = par_state(self.tmp, [["a1", "a2"], ["b1", "b2"]],
                          turns=2, labels=["A", "B"])
        gated = GatedAgent(state["workspace"], ["b1", "b2"], name="B")
        state["agents"][1] = gated

        class InjectIO(RecordingIO):
            def __init__(self):
                super().__init__()
                self.inject = []
            def drain_human(self):
                out, self.inject = self.inject, []
                return out

        io = InjectIO()
        t = self.run_bg(state, io)
        self.assertTrue(gated.entered.wait(timeout=5))
        io.inject.append("hello from josh")
        # wait until the interjection was recorded, then release the round
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(r["speaker"] == "josh" for r in jsonl_rows(state)):
                break
            time.sleep(0.05)
        gated.gate.set()
        gated.gate = threading.Event(); gated.gate.set()  # later rounds flow
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        # both seats heard it exactly once
        for agent in state["agents"]:
            seen = "\n\n".join(agent.prompts)
            self.assertEqual(seen.count("Josh (human) interjects: "
                                        "hello from josh"), 1, agent.name)

    def test_clear_defers_to_the_round_boundary(self):
        state = par_state(self.tmp, [["a1", "a2"], ["b1", "b2"]],
                          turns=2, labels=["A", "B"])
        gated = GatedAgent(state["workspace"], ["b1", "b2"], name="B")
        state["agents"][1] = gated

        class InjectIO(RecordingIO):
            def __init__(self):
                super().__init__()
                self.inject = []
            def drain_human(self):
                out, self.inject = self.inject, []
                return out

        io = InjectIO()
        t = self.run_bg(state, io)
        self.assertTrue(gated.entered.wait(timeout=5))
        io.inject.append("/clear B")
        deadline = time.time() + 5
        while time.time() < deadline:
            if any("queued — runs after this round" in p.get("text", "")
                   for e, p in io.events if e == "status"):
                break
            time.sleep(0.05)
        gated.gate.set()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        # B's round-2 prompt re-introduces it (fresh preamble + clear note)
        self.assertEqual(len(gated.prompts), 2)
        self.assertIn("You are B", gated.prompts[1])
        self.assertIn("cleared your context", gated.prompts[1])


class AppParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-parapp-")
        self._old_sessions = app.SESSIONS_DIR
        app.SESSIONS_DIR = self.tmp
        self._old_types = dict(relay.AGENT_TYPES)

    def tearDown(self):
        app.SESSIONS_DIR = self._old_sessions
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_event_order_and_single_emit_thread(self):
        class ThreadTrackingWindow(FakeWindow):
            def __init__(self):
                super().__init__()
                self.threads = set()
            def evaluate_js(self, script):
                self.threads.add(threading.get_ident())
                super().evaluate_js(script)

        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["c1", "c2"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["g1", "g2"])
        api = app.Api()
        api._window = ThreadTrackingWindow()
        api._conversation({"opener": "go", "turns": 2, "mode": "parallel",
                           "seats": [{"id": 0, "provider": "claude",
                                      "enabled": True},
                                     {"id": 1, "provider": "gpt",
                                      "enabled": True}]})
        api._emit_q.join()
        self.assertEqual(len(api._window.threads), 1)   # ONE emitting thread
        events = api._window.events()
        names = [e["event"] for e in events]
        # per round: both thinking events precede any message of that round
        first_think = [i for i, e in enumerate(events)
                       if e["event"] == "thinking"
                       and e["payload"]["round"] == 1]
        first_msgs = [i for i, e in enumerate(events)
                      if e["event"] == "message"
                      and e["payload"].get("round") == 1
                      and e["payload"].get("speaker") != "josh"]
        self.assertEqual(len(first_think), 2)
        self.assertTrue(max(first_think) < min(first_msgs),
                        f"thinking after message: {names}")
        done = events[-1]
        self.assertEqual(done["event"], "done")
        self.assertTrue(done["payload"]["can_continue"])


class ParallelAskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-par-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ask_mid_round_without_holding_the_lock(self):
        from test_ask import AskIO

        state = par_state(self.tmp,
                          [["q [[ASK: pick | A | B]]", "a2"],
                           ["b1", "b2"]], turns=2, labels=["A", "B"])
        probe = {}

        class LockProbeAskIO(AskIO):
            def ask_human(self, payload, abort=None):
                # a DIFFERENT thread must be able to take state["lock"]
                # while the asking seat waits (RLock reentrancy would let
                # the owner thread lie to us, so probe from outside)
                res = {}

                def try_lock():
                    got = state["lock"].acquire(timeout=2)
                    res["got"] = got
                    if got:
                        state["lock"].release()
                t = threading.Thread(target=try_lock)
                t.start()
                t.join()
                probe["lock_free"] = res.get("got", False)
                time.sleep(0.1)          # let the barrier visibly wait on us
                return super().ask_human(payload, abort=abort)

        state["ask"] = True
        io = LockProbeAskIO(answers=["A"])
        outcome = run_rounds(state, io)
        self.assertEqual(outcome, "cap")
        self.assertTrue(probe["lock_free"],
                        "ask_human ran with state['lock'] held")
        self.assertEqual(len(io.asked), 1)
        # both seats saw the answer in a later prompt
        for ag in state["agents"]:
            self.assertTrue(any("Josh (human) answers: A" in p
                                for p in ag.prompts), ag.name)
        self.assertIsNone(state.get("ask_pending"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
