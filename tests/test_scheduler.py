"""Phase 2 tests: meta v2, the per-turn scheduler, and resume positions.

Token-free — FakeAgents only. Run:  python tests/test_scheduler.py
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import (Agent, SessionStore, make_log, run_rounds,
                   rehydrate, continue_block, META_VERSION)

from test_loop import FakeAgent, RecordingIO, build_state, saved_meta, jsonl_rows


class RehydratableFake(Agent):
    """Adapter-shaped fake: the real constructor signature so rehydrate can
    build it through AGENT_TYPES. Scripts are assigned after construction."""
    name = "Fake"
    cli = "fake"

    def turn(self, message):
        item = self.script.pop(0) if getattr(self, "script", None) else "(dry)"
        if isinstance(item, BaseException):
            raise item
        self.session_id = f"fake-session-{self.uid}"
        return item


def attach_runtime(state, session_dir):
    store = SessionStore(session_dir)
    state["store"] = store
    state["transcript"] = store.transcript
    state["log"] = make_log(state, store)
    return state


def agent_rows(state):
    return [r["text"] for r in jsonl_rows(state)
            if r["speaker"] not in ("system", "josh")]


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-sched-")
        self._old_types = dict(relay.AGENT_TYPES)
        relay.AGENT_TYPES["claude"] = RehydratableFake

    def tearDown(self):
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def resume(self, state, scripts):
        """Rehydrate from the on-disk meta and re-run with fresh scripts."""
        meta = saved_meta(state)
        self.assertEqual(continue_block(meta), "", continue_block(meta))
        st = rehydrate(meta)
        attach_runtime(st, os.path.join(self.tmp, "session"))
        for a, script in zip(st["agents"], scripts):
            a.script = list(script)
        return st

    # ------------------------------------------------------------- meta --
    def test_meta_v2_fields_written(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        run_rounds(state, RecordingIO())
        meta = saved_meta(state)
        self.assertEqual(meta["v"], 2)
        self.assertEqual(meta["mode"], "round_robin")
        self.assertEqual(meta["turn"], 2)
        # after seat 2 spoke, the cursor points back at seat 1 (slot 0)
        self.assertEqual(meta["cursor"], 0)
        self.assertIsNone(meta["next_speaker"])
        self.assertIsNone(meta["closing"])

    def test_v1_meta_still_continuable(self):
        # run a chat, then strip the meta back to the v1 shape by hand
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        run_rounds(state, RecordingIO())
        meta = saved_meta(state)
        for k in ("mode", "turn", "cursor", "next_speaker", "closing"):
            del meta[k]
        meta["v"] = 1
        self.assertEqual(continue_block(meta), "")
        st = rehydrate(meta)
        self.assertEqual(st["mode"], "round_robin")
        self.assertEqual(st["turn"], 1 * 2)        # rnd * seats approximation
        self.assertIsNone(st["cursor"])            # -> loop starts at seat 0
        self.assertIsNone(st["closing"])

    def test_newer_meta_is_view_only(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        run_rounds(state, RecordingIO())
        meta = saved_meta(state)
        meta["v"] = META_VERSION + 1
        self.assertIn("different version", continue_block(meta))

    # ----------------------------------------------------------- resume --
    def test_resume_continues_at_the_right_seat(self):
        # seat B dies fatally in round 1 -> the cursor stays ON B; a resume
        # (fresh process) retries B first instead of restarting at A
        dead = RuntimeError("No conversation found with session ID: bogus")
        state = build_state(self.tmp, [["a1", "a2"], [dead], ["c1", "c2"]],
                            turns=2, labels=["A", "B", "C"])
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "fatal")
        self.assertEqual(agent_rows(state), ["a1"])
        self.assertEqual(saved_meta(state)["cursor"], 1)     # still B's turn
        st = self.resume(state, [["a-late"], ["b-back"], ["c-first"]])
        run_rounds(st, RecordingIO())
        # B speaks FIRST on resume (round 1 completes with B, then C)
        self.assertEqual(agent_rows(st)[:3], ["a1", "b-back", "c-first"])

    def test_wrap_survives_a_process_crash_mid_closing(self):
        # A wraps (closing = [B, C]); B closes; the process dies during C's
        # turn (KeyboardInterrupt = nothing committed). The DISK still owes C
        # its closing word — resume delivers exactly that one turn.
        state = build_state(
            self.tmp,
            [["over. [[WRAP]]"], ["b-closing"], [KeyboardInterrupt()]],
            turns=5, labels=["A", "B", "C"])
        with self.assertRaises(KeyboardInterrupt):
            run_rounds(state, RecordingIO())
        self.assertEqual(saved_meta(state)["closing"], [2])
        st = self.resume(state, [["a-no"], ["b-no"], ["c-final"]])
        outcome = run_rounds(st, RecordingIO())
        self.assertEqual(outcome, "wrapped")
        self.assertEqual(agent_rows(st),
                         ["over. [[WRAP]]", "b-closing", "c-final"])

    def test_fatal_closing_seat_loses_its_slot(self):
        # pop-before-attempt: a closing seat that FAILS its turn (clean fatal,
        # state saved) has spent its slot — no second closing lap
        dead = RuntimeError("No conversation found with session ID: bogus")
        state = build_state(
            self.tmp,
            [["over. [[WRAP]]"], ["b-closing"], [dead]],
            turns=5, labels=["A", "B", "C"])
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "fatal")
        self.assertEqual(saved_meta(state)["closing"], [])

    def test_closing_truncated_by_cap_is_persisted(self):
        # wrapper at index 1 -> closing = [C, A]; A's closing turn sits at the
        # lap boundary and the cap cuts it (matches the old countdown), but
        # the debt is now persisted truthfully instead of evaporating
        state = build_state(
            self.tmp,
            [["a1"], ["b-wrap. [[WRAP]]"], ["c-close"]],
            turns=1, labels=["A", "B", "C"])
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")
        self.assertEqual(agent_rows(state),
                         ["a1", "b-wrap. [[WRAP]]", "c-close"])
        self.assertEqual(saved_meta(state)["closing"], [0])

    # -------------------------------------------------------- scheduling --
    def test_failed_seat_advances_cursor(self):
        state = build_state(
            self.tmp,
            [["a1"], [RuntimeError("t1"), RuntimeError("t2")], ["c1"]],
            turns=1, labels=["A", "B", "C"])
        run_rounds(state, RecordingIO())
        # B failed twice and was skipped; C still spoke in round 1
        self.assertEqual(agent_rows(state), ["a1", "c1"])
        self.assertEqual(saved_meta(state)["cursor"], 0)

    def test_round_numbers_stamped_per_lap(self):
        state = build_state(self.tmp, [["a1", "a2"], ["b1", "b2"]], turns=2)
        run_rounds(state, RecordingIO())
        rows = [r for r in jsonl_rows(state)
                if r["speaker"] not in ("system", "josh")]
        self.assertEqual([r["round"] for r in rows], [1, 1, 2, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
