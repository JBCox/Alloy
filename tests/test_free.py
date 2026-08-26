"""Phase 6 tests: free-running mode.

Token-free; timing exercised with sleeping fakes. Run:
python tests/test_free.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import run_rounds

from test_loop import FakeAgent, RecordingIO, build_state, saved_meta, jsonl_rows
from test_scheduler import agent_rows


def free_state(tmp, scripts, turns=2, labels=None, opener=None):
    state = build_state(tmp, scripts, turns=turns, labels=labels)
    state["mode"] = "free"
    if opener:
        for j in state["pending"]:
            state["pending"][j].append(
                f"Josh (human) opens the conversation: {opener}")
    return state


class SleepyAgent(FakeAgent):
    def __init__(self, workspace, script, delay, name=None, **kw):
        super().__init__(workspace, script, name=name, **kw)
        self.delay = delay

    def turn(self, message, on_activity=None):
        time.sleep(self.delay)
        return super().turn(message)


class FreeModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-free-")
        self._old_backoff = relay.FREE_RETRY_BACKOFF
        relay.FREE_RETRY_BACKOFF = 0.05     # keep failing-seat tests fast

    def tearDown(self):
        relay.FREE_RETRY_BACKOFF = self._old_backoff
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_basic_run_commits_the_full_budget(self):
        state = free_state(self.tmp,
                           [[f"a{k}" for k in range(9)],
                            [f"b{k}" for k in range(9)]],
                           turns=2, labels=["A", "B"], opener="go")
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")
        rows = agent_rows(state)
        self.assertEqual(len(rows), 2 * 2)         # budget = turns x seats
        meta = saved_meta(state)
        self.assertEqual(meta["turn"], 4)
        self.assertEqual(meta["mode"], "free")

    def test_throttle_bounds_the_lead(self):
        # A is 20x faster than B; the lead may never exceed FREE_MAX_LEAD
        state = free_state(self.tmp, [[], []], turns=3, labels=["A", "B"],
                           opener="go")
        a = SleepyAgent(state["workspace"], [f"a{k}" for k in range(12)],
                        0.005, name="A")
        b = SleepyAgent(state["workspace"], [f"b{k}" for k in range(12)],
                        0.1, name="B")
        state["agents"] = [a, b]
        run_rounds(state, RecordingIO())
        # replay the committed order and track the running lead
        lead, worst = 0, 0
        counts = {"A": 0, "B": 0}
        for r in jsonl_rows(state):
            if r["speaker"] in ("system", "josh"):
                continue
            counts[r["name"]] += 1
            worst = max(worst, counts["A"] - counts["B"])
        self.assertLessEqual(worst, relay.FREE_MAX_LEAD,
                             f"lead reached {worst}")

    def test_wrap_gives_others_exactly_one_more_turn(self):
        # timing-independent invariant: after the wrap row lands, the wrapper
        # never speaks again and every other seat speaks EXACTLY once more
        state = free_state(self.tmp,
                           [["a1", "bye. [[WRAP]]", "a-no"],
                            ["b1", "b2", "b3", "b4"],
                            ["c1", "c2", "c3", "c4"]],
                           turns=5, labels=["A", "B", "C"], opener="go")
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "wrapped")
        named = [(r["name"], r["text"]) for r in jsonl_rows(state)
                 if r["speaker"] not in ("system", "josh")]
        wrap_at = next(k for k, (_, text) in enumerate(named)
                       if "[[WRAP]]" in text)
        after = [name for name, _ in named[wrap_at + 1:]]
        # the wrapper never speaks again; a seat mid-turn when the wrap lands
        # commits normally AND still gets its closing word, so others appear
        # 1-2 times after the wrap ROW — but each gets exactly ONE turn whose
        # prompt actually contained the wrap (the real "last word" semantics)
        self.assertNotIn("A", after)
        for k in (1, 2):
            agent = state["agents"][k]
            aware = [p for p in agent.prompts if "bye. [[WRAP]]" in p]
            self.assertEqual(len(aware), 1, agent.name)
            self.assertLessEqual(after.count(agent.name), 2, named)
            self.assertGreaterEqual(after.count(agent.name), 1, named)

    def test_interjection_reaches_every_seat_once(self):
        class InjectIO(RecordingIO):
            def __init__(self):
                super().__init__()
                self.sent = False
            def drain_human(self):
                if not self.sent:
                    self.sent = True
                    return ["hey both of you"]
                return []

        state = free_state(self.tmp,
                           [["a1", "a2"], ["b1", "b2"]],
                           turns=2, labels=["A", "B"], opener="go")
        run_rounds(state, InjectIO())
        meta = saved_meta(state)
        for agent, seat in zip(state["agents"], meta["seats"]):
            seen = "\n\n".join(agent.prompts + seat["pending"])
            self.assertEqual(
                seen.count("Josh (human) interjects: hey both of you"), 1,
                agent.name)

    def test_failing_seat_parks_and_run_pauses(self):
        boom = [RuntimeError(f"t{k}") for k in range(12)]
        state = free_state(self.tmp,
                           [[f"a{k}" for k in range(9)], list(boom)],
                           turns=3, labels=["A", "B"], opener="go")
        outcome = run_rounds(state, RecordingIO())
        # B parks after 3 double-failures; <2 live seats -> the run pauses.
        # A parked-seat pause is benign ("starved"), never a dead CLI's fatal.
        self.assertEqual(outcome, "starved")
        sys_rows = [r["text"] for r in jsonl_rows(state)
                    if r["speaker"] == "system"]
        self.assertTrue(any("parked" in t for t in sys_rows), sys_rows)
        self.assertTrue(any("Fewer than two live seats" in t
                            for t in sys_rows), sys_rows)
        # the benign pause reaches outcome hard facts as its own reason
        self.assertEqual(state["completion"]["termination_reason"], "starved")

    def test_free_mode_rows_carry_no_refusal_keys_when_nothing_refused(self):
        """The delivery gate must leave the reactive engine byte-identical:
        no parks, no workstreams -> no row ever grows rejected_to or
        narrowing_failed (comms-design.md section 3's identity rule)."""
        state = free_state(self.tmp,
                           [["a1", "a2"], ["b1", "b2"]],
                           turns=2, labels=["A", "B"], opener="go")
        run_rounds(state, RecordingIO())
        seat_rows = [r for r in jsonl_rows(state)
                     if r.get("origin") == "seat"]
        self.assertTrue(seat_rows)
        for r in seat_rows:
            self.assertNotIn("rejected_to", r)
            self.assertNotIn("narrowing_failed", r)

    def test_a_parked_peer_stops_absorbing_broadcasts_in_free_mode(self):
        """Free mode parks failing seats through the SAME shared set the
        delivery gate reads, so once B parks, A's later commits are REFUSED
        to it visibly (envelope receipt) instead of piling into a queue
        nobody will drain this run. C (slow, healthy) keeps two live seats
        so the benign starve-pause cannot preempt the scenario."""
        boom = RuntimeError("down")
        state = free_state(self.tmp,
                           [[f"a{k}" for k in range(5)],
                            [boom] * 9,
                            [f"c{k}" for k in range(10)]],
                           turns=3, labels=["A", "B", "C"], opener="go")
        state["agents"][2] = SleepyAgent(state["workspace"],
                                         [f"c{k}" for k in range(10)],
                                         0.01, name="C")
        run_rounds(state, RecordingIO())
        rows = [r for r in jsonl_rows(state)
                if r.get("origin") == "seat" and r["name"] == "A"]
        self.assertTrue(rows)
        refused = [r for r in rows if r.get("rejected_to")]
        self.assertTrue(refused, "at least one commit must be refused to B")
        for r in refused:
            self.assertEqual([x["seat"] for x in r["rejected_to"]], [1])
            self.assertEqual(r["rejected_to"][0]["reason"],
                             "benched after repeated failures")
            self.assertNotIn(1, r["delivered_to"])

    def test_stop_command(self):
        class StopIO(RecordingIO):
            def __init__(self):
                super().__init__()
                self.calls = 0
            def drain_human(self):
                self.calls += 1
                return ["/stop"] if self.calls == 2 else []

        state = free_state(self.tmp, [[], []], turns=10,
                           labels=["A", "B"], opener="go")
        # slow the seats down so the stop lands mid-run, not post-budget
        # (the coordinator's second drain poll comes ~0.5s in; at 0.15s per
        # turn only a handful of the 20 budgeted turns exist by then)
        state["agents"] = [
            SleepyAgent(state["workspace"], [f"a{k}" for k in range(30)],
                        0.15, name="A"),
            SleepyAgent(state["workspace"], [f"b{k}" for k in range(30)],
                        0.15, name="B")]
        outcome = run_rounds(state, StopIO())
        self.assertEqual(outcome, "stopped")
        self.assertLess(len(agent_rows(state)), 20)

    def test_resume_after_kill_restarts_cleanly(self):
        from test_scheduler import RehydratableFake, attach_runtime
        relay.AGENT_TYPES["claude"] = RehydratableFake
        try:
            state = free_state(self.tmp, [["a1", "a2"], ["b1", "b2"]],
                               turns=1, labels=["A", "B"], opener="go")
            run_rounds(state, RecordingIO())        # budget 2 -> pauses
            meta = saved_meta(state)
            self.assertEqual(meta["turn"], 2)
            st = relay.rehydrate(meta)
            attach_runtime(st, os.path.join(self.tmp, "session"))
            st["max"] = st["rnd"] + 1               # continue: one more round
            for a, s in zip(st["agents"], [["a-back"], ["b-back"]]):
                a.script = list(s)
            outcome = run_rounds(st, RecordingIO())
            self.assertEqual(outcome, "cap")
            rows = agent_rows(st)
            self.assertIn("a-back", rows)
            self.assertIn("b-back", rows)
        finally:
            relay.AGENT_TYPES["claude"] = relay.ClaudeAgent

    def test_stress_no_message_lost_or_duplicated(self):
        import random
        random.seed(7)
        rounds = 20                     # budget = 60 turns across 3 seats
        state = free_state(self.tmp, [[], [], []], turns=rounds,
                           labels=["A", "B", "C"], opener="go")
        state["agents"] = [
            SleepyAgent(state["workspace"],
                        # fixed width: "B001" can't be a prefix of "B010"
                        [f"{lb}{k:03d}" for k in range(rounds * 3)],
                        0.002, name=lb)
            for lb in ("A", "B", "C")]
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")
        rows = [r for r in jsonl_rows(state)
                if r["speaker"] not in ("system", "josh")]
        self.assertEqual(len(rows), rounds * 3)
        # every committed reply reached both other seats exactly once
        meta = saved_meta(state)
        for j, agent in enumerate(state["agents"]):
            seen = "\n\n".join(agent.prompts + meta["seats"][j]["pending"])
            for r in rows:
                if r["name"] == agent.name:
                    continue
                needle = f"{r['name']} said:\n{r['text']}"
                self.assertEqual(seen.count(needle), 1,
                                 f"{agent.name} saw {r['text']} "
                                 f"{seen.count(needle)}x")

    def test_clear_runs_on_the_owning_thread(self):
        class ClearIO(RecordingIO):
            def __init__(self):
                super().__init__()
                self.sent = False
            def drain_human(self):
                if not self.sent:
                    self.sent = True
                    return ["/clear B"]
                return []

        state = free_state(self.tmp,
                           [["a1", "a2"], ["b1", "b2"]],
                           turns=2, labels=["A", "B"], opener="go")
        run_rounds(state, ClearIO())
        b = state["agents"][1]
        # after the clear, B was re-introduced with a fresh preamble + note
        recleared = [p for p in b.prompts if "cleared your context" in p]
        self.assertTrue(recleared)
        self.assertIn("You are B", recleared[0])

    # ------------------------------------------------------- [[ASK]] flow --
    def test_ask_answered_in_free_mode(self):
        from test_ask import AskIO

        state = free_state(self.tmp,
                           [["q [[ASK: pick | A | B]]", "a2"],
                            ["b1", "b2"]],
                           turns=2, labels=["A", "B"], opener="go")
        state["ask"] = True
        io = AskIO(answers=["B"])
        outcome = run_rounds(state, io)
        self.assertEqual(outcome, "cap")
        self.assertEqual(len(io.asked), 1)
        b = state["agents"][1]
        self.assertTrue(any("Josh (human) answers: B" in p
                            for p in b.prompts))
        self.assertIsNone(state.get("ask_pending"))

    def test_blocked_ask_unblocked_by_fatal_abort(self):
        # seat A waits on Josh; seat B dies fatally -> flow-stop -> abort()
        # unblocks the waiter (should_stop never sees free mode's stop)
        asked_evt = threading.Event()
        result = {}

        class BlockingAskIO(RecordingIO):
            def ask_human(self, payload, abort=None):
                asked_evt.set()
                deadline = time.time() + 10
                while time.time() < deadline:
                    if abort and abort():
                        result["aborted"] = True
                        return None
                    time.sleep(0.05)
                result["aborted"] = False
                return None

        class WaitThenFail(FakeAgent):
            def turn(self, message, on_activity=None):
                self.prompts.append(message)
                asked_evt.wait(10)
                raise RuntimeError(
                    "No conversation found with session ID: dead")

        state = free_state(self.tmp,
                           [["q [[ASK: pick | A ]]"], []],
                           turns=2, labels=["A", "B"], opener="go")
        b_old = state["agents"][1]
        state["agents"][1] = WaitThenFail(b_old.workspace, [], name="B")
        state["ask"] = True
        outcome = run_rounds(state, BlockingAskIO())
        self.assertEqual(outcome, "fatal")
        self.assertTrue(result.get("aborted"),
                        "ask_human was not unblocked by the abort signal")


if __name__ == "__main__":
    unittest.main(verbosity=2)
