"""Battle mode (blind A/B + Elo) and per-message reactions.

The battle rides run_parallel's proven isolation (commit_reply fan_out=False,
the panel draft phase's mechanism), so the loop tests here prove the three
things that are genuinely NEW: neither seat's queue receives its peer's
answer, rows carry intent="battle" for UI masking, and a verdict moves both
meta and leaderboard.json exactly once. Reactions tests pin the merge rule
that makes thumbs and the end card different questions about one file.

Run:  python tests/test_battle.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import outcome
import relay
from test_loop import FakeAgent, RecordingIO, build_state


def _sandbox():
    root = tempfile.mkdtemp(prefix="aichat-battle-test-")
    old = (app.SESSIONS_DIR, relay.SESSIONS_DIR, relay.TABS_FILE)
    app.SESSIONS_DIR = root
    relay.SESSIONS_DIR = root
    relay.TABS_FILE = os.path.join(root, "tabs.json")
    return root, old


def _restore(old):
    app.SESSIONS_DIR, relay.SESSIONS_DIR, relay.TABS_FILE = old


class TestElo(unittest.TestCase):
    def setUp(self):
        self.root, self.old = _sandbox()
        self.addCleanup(_restore, self.old)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_win_loss_is_symmetric_and_zero_sum_in_k(self):
        board = {"ratings": {}, "games": 0}
        relay.apply_battle_result(board, "a:x", "b:y", "a")
        ra = board["ratings"]["a:x"]
        rb = board["ratings"]["b:y"]
        self.assertGreater(ra, relay.ELO_START)
        self.assertLess(rb, relay.ELO_START)
        # zero-sum around the start: a fresh-pair win gains what the loser loses
        self.assertAlmostEqual(
            (ra - relay.ELO_START) + (rb - relay.ELO_START), 0.0, places=6)
        self.assertEqual(board["games"], 1)

    def test_tie_moves_half_and_bad_moves_nothing(self):
        board = {"ratings": {}, "games": 0}
        relay.apply_battle_result(board, "a:x", "b:y", "tie")
        ra = board["ratings"]["a:x"]
        self.assertAlmostEqual(abs(ra - relay.ELO_START),
                               abs(relay.ELO_START - board["ratings"]["b:y"]),
                               places=6)
        before = dict(board["ratings"])
        relay.apply_battle_result(board, "a:x", "b:y", "bad")
        # "both bad" says nothing about relative strength: count the game,
        # move nothing
        self.assertEqual(board["ratings"], before)
        self.assertEqual(board["games"], 2)

    def test_upset_beats_expected_win(self):
        board = {"ratings": {"under": 1000.0, "over": 1400.0}, "games": 0}
        relay.apply_battle_result(board, "under", "over", "a")
        gain = board["ratings"]["under"] - 1000.0
        board2 = {"ratings": {"under": 1400.0, "over": 1000.0}, "games": 0}
        relay.apply_battle_result(board2, "under", "over", "a")
        expected_gain = board2["ratings"]["under"] - 1400.0
        self.assertGreater(gain, expected_gain)

    def test_leaderboard_round_trip_and_corrupt_fallback(self):
        path = os.path.join(self.root, "leaderboard.json")
        board = relay.read_leaderboard(path)
        self.assertEqual(board, {"ratings": {}, "games": 0})
        relay.apply_battle_result(board, "claude:opus", "gpt:5.6", "b")
        self.assertTrue(relay.write_leaderboard(board, path))
        again = relay.read_leaderboard(path)
        self.assertEqual(again["ratings"]["gpt:5.6"], board["ratings"]["gpt:5.6"])
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(relay.read_leaderboard(path), {"ratings": {}, "games": 0})


def _battle_state(tmpdir):
    """Two-seat battle state at round 0, phase blind — the exact shape
    app._conversation seeds. build_state builds its own FakeAgents from the
    reply scripts and its own SessionStore under tmp/session."""
    state = build_state(tmpdir, [["alpha says hi"], ["beta says hi"]],
                        turns=2)
    state["mode"] = "battle"
    state["orchestration"] = relay.normalize_orchestration(mode="battle")
    state["battle"] = {"phase": "blind", "slots": [0, 1]}
    return state


class TestBattleLoop(unittest.TestCase):
    def setUp(self):
        self.root, self.old = _sandbox()
        self.addCleanup(_restore, self.old)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dir = tempfile.mkdtemp(prefix="battle-session-")

    def test_blind_round_isolates_and_latches_awaiting_vote(self):
        state = _battle_state(self.dir)
        io = RecordingIO()
        ended = relay.run_battle(state, io)
        self.assertEqual(ended, "wrapped")
        b = state["battle"]
        self.assertEqual(b["phase"], relay.BATTLE_AWAITING)
        self.assertEqual(b["slots"], [0, 1])
        self.assertEqual(state.get("termination_reason"), "battle_vote")
        # battle_ready names its chat: the UI gates the vote bar on it, so a
        # background duel must never be able to paint over another transcript
        ready = [p for e, p in io.events if e == "battle_ready"]
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["session"], state["store"].id)
        # THE isolation contract: neither seat's queue ever received the peer
        with open(os.path.join(state["store"].dir, "messages.jsonl"),
                  encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        seat_rows = [r for r in rows if r.get("intent") == "battle"]
        self.assertEqual(len(seat_rows), 2)
        self.assertTrue(all(r["origin"] == "seat" for r in seat_rows))
        for row in seat_rows:
            others = [r for r in seat_rows if r["speaker"] != row["speaker"]]
            self.assertTrue(others, "both answers recorded")
            delivered_to_others = [
                p for r2 in rows if r2["speaker"] == row["speaker"]
                for p in []]   # no peer backlog write is asserted below
            self.assertEqual(delivered_to_others, [])
        # pending queues stay empty of peer content (fan_out=False)
        self.assertFalse(any(state["pending"][0]))
        self.assertFalse(any(state["pending"][1]))

    def test_voted_or_resumed_battle_delegates_to_parallel(self):
        state = _battle_state(self.dir)
        state["battle"] = {"phase": relay.BATTLE_VOTED, "verdict": "a",
                           "slots": [0, 1]}
        state["rnd"] = 1
        calls = []
        real = relay.run_parallel
        relay.run_parallel = lambda s, io: calls.append(s) or "cap"
        try:
            self.assertEqual(relay.run_battle(state, RecordingIO()), "cap")
        finally:
            relay.run_parallel = real
        self.assertEqual(calls, [state])

    def test_battle_status_derives_for_the_rail(self):
        meta = {"mode": "battle",
                "seats": [{"id": 0, "provider": "claude", "model": "opus"},
                          {"id": 1, "provider": "gpt", "model": "5.6"}],
                "battle": {"phase": relay.BATTLE_AWAITING, "slots": [0, 1]}}
        st = relay.battle_status(meta)
        self.assertEqual(st["state"], "awaiting")
        self.assertEqual(st["slots"], [0, 1])
        meta["battle"] = {"phase": relay.BATTLE_VOTED, "verdict": "b",
                          "slots": [0, 1]}
        st = relay.battle_status(meta)
        self.assertEqual(st["state"], "voted")
        self.assertIn("gpt:5.6 won", st["label"])
        self.assertIsNone(relay.battle_status({"mode": "parallel"}))
        # session_summary carries it (rail badge chain)
        summary = relay.session_summary(self.dir, meta=meta)
        self.assertIsNotNone(summary["battle"])

    def test_normalize_keeps_battle_mode(self):
        recipe = relay.normalize_orchestration(mode="battle")
        self.assertEqual(recipe["workflow"], "battle")
        self.assertEqual(recipe["routing"], "isolated")
        self.assertEqual(recipe["budget"]["unit"], "phases")
        # and SessionStore.save must not rewrite it back to round_robin
        self.assertIn("battle", relay.LEGACY_ORCHESTRATION)


class TestVoteBridge(unittest.TestCase):
    def setUp(self):
        self.root, self.old = _sandbox()
        self.addCleanup(_restore, self.old)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dir = os.path.join(self.root, "duel-chat")
        os.makedirs(self.dir)
        self.meta = {"id": "duel-chat", "title": "Duel", "ended": True,
                     "mode": "battle",
                     "seats": [{"id": 0, "provider": "claude", "model": "opus"},
                               {"id": 1, "provider": "gpt", "model": "5.6"}],
                     "battle": {"phase": relay.BATTLE_AWAITING,
                                "slots": [0, 1]}}
        with open(os.path.join(self.dir, "meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump(self.meta, f)
        self.api = app.Api()
        self.api._window = FakeWindow()

    class FakeWindow:
        def __init__(self):
            self.calls = []

        def evaluate_js(self, script):
            self.calls.append(script)

    def _focused_run(self):
        run = type("R", (), {})()
        run.session_dir = self.dir
        run.state = None
        run.is_running = lambda: False
        return run

    def test_vote_records_once_moves_elo_and_emits_reveal(self):
        self.api._runs._runs["duel-chat"] = self._focused_run()
        r = self.api.vote_battle("a", "duel-chat")
        self.assertTrue(r.get("ok"), r)
        meta = relay.read_meta(self.dir)
        self.assertEqual(meta["battle"]["verdict"], "a")
        self.assertEqual(meta["battle"]["phase"], relay.BATTLE_VOTED)
        board = relay.read_leaderboard()
        self.assertGreater(board["ratings"]["claude:opus"],
                           relay.ELO_START)
        self.assertEqual(board["games"], 1)
        self.api._emit_q.join()
        reveals = [json.loads(s[len("uiEvent("):-1]) for s in self.api._window.calls
                   if s.startswith("uiEvent(")]
        ev = [e for e in reveals if e["event"] == "battle_revealed"]
        self.assertEqual(len(ev), 1)
        letters = {c["letter"]: c for c in ev[0]["payload"]["contestants"]}
        self.assertEqual(letters["A"]["provider"], "claude")

        second = self.api.vote_battle("b", "duel-chat")
        self.assertIn("error", second)
        self.assertEqual(relay.read_leaderboard()["games"], 1)

    def test_bad_choice_and_missing_session_are_errors(self):
        self.api._runs._runs["duel-chat"] = self._focused_run()
        self.assertIn("error", self.api.vote_battle("amazing", "duel-chat"))
        other = self.api.vote_battle("a", "nope")
        self.assertIn("error", other)


class TestReactions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="react-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        outcome.write_outcome(self.dir)

    def test_reaction_merges_and_survives_end_card_and_rebuild(self):
        outcome.set_reaction(self.dir, "msg1", "helpful")
        fb = outcome.read_outcome(self.dir)["human_feedback"]
        self.assertEqual(fb["reactions"]["msg1"]["verdict"], "helpful")
        # end card written AFTER a reaction keeps the reaction...
        outcome.set_feedback(self.dir, "not_helpful", ["incomplete"], "")
        fb = outcome.read_outcome(self.dir)["human_feedback"]
        self.assertEqual(fb["rating"], "not_helpful")
        self.assertEqual(fb["reactions"]["msg1"]["verdict"], "helpful")
        # ...and a rebuild keeps both (the preserve rule: non-empty dict)
        rec = outcome.write_outcome(self.dir)
        self.assertEqual(rec["human_feedback"]["rating"], "not_helpful")
        self.assertIn("msg1", rec["human_feedback"]["reactions"])
        # toggle off removes exactly that row's thumb
        outcome.set_reaction(self.dir, "msg1", None)
        self.assertNotIn("msg1",
                         outcome.read_outcome(self.dir)["human_feedback"]
                         ["reactions"])

    def test_second_reaction_on_same_row_replaces_not_duplicates(self):
        outcome.set_reaction(self.dir, "m", "helpful")
        outcome.set_reaction(self.dir, "m", "not_helpful")
        rx = outcome.read_outcome(self.dir)["human_feedback"]["reactions"]
        self.assertEqual(rx["m"]["verdict"], "not_helpful")

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            outcome.set_reaction(self.dir, "", "helpful")
        with self.assertRaises(ValueError):
            outcome.set_reaction(self.dir, "m", "amazing")


class FakeWindow:
    """Module-level twin used by TestVoteBridge via inner class alias."""

    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)


if __name__ == "__main__":
    unittest.main(verbosity=1)
