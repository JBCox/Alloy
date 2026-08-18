"""Token-free tests for the shared loop (relay.run_rounds).

No CLI is invoked and no tokens are spent: FakeAgent scripts replies/failures,
RecordingIO captures events and feeds scripted human input, and a real
SessionStore writes into a temp dir so persistence is tested for real.

Run:  python tests/test_loop.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import Agent, LoopIO, SessionStore, make_log, run_rounds


class FakeAgent(Agent):
    """Scripted seat. Each entry in `script` is either a reply string, an
    Exception instance to raise, "" to exercise the empty-reply backstop, or
    a (reply, [activity dicts]) tuple — the acts fire through `on_activity`
    (when the loop passed one) before the reply returns."""

    name = "Fake"
    cli = "fake"

    def __init__(self, workspace, script, name=None, **kw):
        super().__init__(workspace, name=name, **kw)
        self.script = list(script)
        self.prompts = []          # every prompt this seat actually received

    def turn(self, message, on_activity=None):
        self.prompts.append(message)
        if not self.script:
            item = "(out of script)"
        else:
            item = self.script.pop(0)
        acts = ()
        if isinstance(item, tuple):
            item, acts = item
        if isinstance(item, BaseException):
            raise item             # BaseException so tests can script a
                                   # KeyboardInterrupt "crash" too
        if on_activity:
            for act in acts:
                on_activity(act)
        if not (item or "").strip():
            return ""              # bypasses Agent.turn's raise-on-empty
        # real adapters re-capture a session id in parse() every call; without
        # one, continue_block rightly rules an introduced seat unresumable
        self.session_id = f"fake-session-{self.uid}"
        return item


class RecordingIO(LoopIO):
    def __init__(self, human_script=None):
        # human_script: list of lists — one list of lines per drain call
        self.human_script = list(human_script or [])
        self.events = []

    def emit(self, event, payload=None):
        self.events.append((event, payload or {}))

    def drain_human(self):
        if self.human_script:
            return self.human_script.pop(0)
        return []

    def names(self):
        return [e for e, _ in self.events]


def build_state(tmp, scripts, turns=3, labels=None, workspace=None,
                brief=None):
    """Mirror main()/_conversation state construction with FakeAgents.

    `workspace` overrides the default in-session scratch dir so the project
    context suite can point a state at a fake project folder; `brief` is
    project_brief()'s record. Both default to the old behaviour exactly."""
    session_dir = os.path.join(tmp, "session")
    workspace = workspace or os.path.join(session_dir, "workspace")
    os.makedirs(workspace, exist_ok=True)
    os.makedirs(session_dir, exist_ok=True)
    labels = labels or [f"Fake {i+1}" for i in range(len(scripts))]
    agents = [FakeAgent(workspace, s, name=lb)
              for s, lb in zip(scripts, labels)]
    store = SessionStore(session_dir)
    store.open_transcript("test", agents, turns)
    state = {"agents": agents, "slot_ids": list(range(len(agents))),
             "providers": ["claude"] * len(agents),
             "workspace": workspace, "transcript": store.transcript,
             "topic": "test", "title": "test", "created": store.created,
             "yolo": False, "turns": turns,
             "rnd": 0, "max": turns, "ended": False, "brief": brief,
             "pending": {i: [] for i in range(len(agents))},
             "introduced": [False] * len(agents), "store": store}
    state["log"] = make_log(state, store)
    store.save(state)
    return state


def saved_meta(state):
    with open(state["store"].meta_path, encoding="utf-8") as f:
        return json.load(f)


def jsonl_rows(state):
    rows = []
    try:
        with open(state["store"].messages, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    except FileNotFoundError:
        pass          # no rows recorded yet (mid-round polls hit this)
    return rows


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ basics --
    def test_round_robin_fanout_and_rows(self):
        state = build_state(self.tmp, [["a1", "a2"], ["b1", "b2"]], turns=2)
        io = RecordingIO()
        outcome = run_rounds(state, io)
        self.assertEqual(outcome, "cap")
        self.assertEqual(state["rnd"], 2)
        a, b = state["agents"]
        # seat 2 heard everything seat 1 said, and vice versa
        self.assertIn("Fake 1 said:\na1", b.prompts[0])
        self.assertIn("Fake 2 said:\nb1", a.prompts[1])
        # first prompt carries the preamble + opener nudge; later ones don't
        self.assertIn("You are Fake 1", a.prompts[0])
        self.assertIn("You open the conversation. Go.", a.prompts[0])
        self.assertNotIn("You are Fake 1", a.prompts[1])
        # the persisted queues are exactly what each seat is still owed:
        # seat 1 never heard seat 2's final reply (the run capped there)
        meta = saved_meta(state)
        self.assertEqual([s["pending"] for s in meta["seats"]],
                         [["Fake 2 said:\nb2"], []])
        rows = [r for r in jsonl_rows(state) if r["speaker"] != "system"]
        self.assertEqual([r["text"] for r in rows], ["a1", "b1", "a2", "b2"])
        # event stream: per turn thinking -> thinking_done -> message
        per_turn = [e for e in io.names()
                    if e in ("thinking", "thinking_done", "message")]
        self.assertEqual(per_turn, ["thinking", "thinking_done", "message"] * 4)

    def test_failed_twice_skips_restores_and_saves(self):
        boom = RuntimeError("transient")
        state = build_state(
            self.tmp, [["a1"], [boom, RuntimeError("transient2"), "b-late"]],
            turns=1)
        io = RecordingIO()
        outcome = run_rounds(state, io)
        self.assertEqual(outcome, "cap")
        # seat 2 skipped: no forged message, queue restored AND persisted
        rows = [r for r in jsonl_rows(state) if r["speaker"] != "system"]
        self.assertEqual([r["text"] for r in rows], ["a1"])
        meta = saved_meta(state)
        self.assertEqual(meta["seats"][1]["pending"], ["Fake 1 said:\na1"])
        # live state matches what was saved (the app's old missing-save bug)
        self.assertEqual(state["pending"][1], ["Fake 1 said:\na1"])
        self.assertIn("agent_error", io.names())
        # retry happened exactly once (two prompts recorded, same content)
        self.assertEqual(len(state["agents"][1].prompts), 2)
        self.assertEqual(state["agents"][1].prompts[0],
                         state["agents"][1].prompts[1])

    def test_fatal_stops_without_retry(self):
        dead = RuntimeError("No conversation found with session ID: bogus")
        state = build_state(self.tmp, [[dead, "never"], ["b1"]], turns=3)
        io = RecordingIO()
        outcome = run_rounds(state, io)
        self.assertEqual(outcome, "fatal")
        # no retry: only one attempt recorded
        self.assertEqual(len(state["agents"][0].prompts), 1)
        errs = [p for e, p in io.events if e == "agent_error"]
        self.assertTrue(errs and errs[0].get("fatal"))
        # nothing was relayed; the run stopped in round 1
        self.assertEqual(state["rnd"], 1)

    def test_empty_reply_backstop(self):
        state = build_state(self.tmp, [[""], ["b1"]], turns=1)
        io = RecordingIO()
        run_rounds(state, io)
        rows = [r for r in jsonl_rows(state) if r["speaker"] != "system"]
        self.assertEqual([r["text"] for r in rows], ["b1"])
        errs = [p for e, p in io.events if e == "agent_error"]
        self.assertTrue(any("empty reply" in p["message"] for p in errs))
        # the skip lost nothing and forged nothing: seat 1 is owed exactly
        # seat 2's reply, and that's what was persisted
        self.assertEqual(saved_meta(state)["seats"][0]["pending"],
                         ["Fake 2 said:\nb1"])

    # -------------------------------------------------------------- wrap --
    def test_wrap_gives_others_one_closing_turn(self):
        state = build_state(
            self.tmp,
            [["done here. [[WRAP]]", "a-should-not-happen"],
             ["b1-closing", "b-should-not-happen"],
             ["c1-closing", "c-should-not-happen"]],
            turns=5)
        io = RecordingIO()
        outcome = run_rounds(state, io)
        self.assertEqual(outcome, "wrapped")
        rows = [r for r in jsonl_rows(state) if r["speaker"] != "system"]
        self.assertEqual([r["text"] for r in rows],
                         ["done here. [[WRAP]]", "b1-closing", "c1-closing"])

    def test_wrap_mention_does_not_fire(self):
        state = build_state(
            self.tmp,
            [["the token is [[WRAP]] which I will not play now", "a2"],
             ["b1", "b2"]],
            turns=2)
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")
        self.assertEqual(state["rnd"], 2)

    # ------------------------------------------------------ human input --
    def test_stop_command(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=5)
        io = RecordingIO(human_script=[["/stop"]])
        outcome = run_rounds(state, io)
        self.assertEqual(outcome, "stopped")
        rows = [r for r in jsonl_rows(state) if r["speaker"] != "system"]
        self.assertEqual([r["text"] for r in rows], ["/stop"])

    def test_turns_takes_effect_at_the_lap_boundary(self):
        # /turns N arriving between rounds applies BEFORE the next lap starts
        # (the old nested loop incremented rnd first, so the same command got
        # clamped one round higher and the extra round still ran)
        state = build_state(self.tmp, [["a1", "a2"], ["b1", "b2"]], turns=5)
        io = RecordingIO(human_script=[[], [], ["/turns 1"]])
        outcome = run_rounds(state, io)
        self.assertEqual(outcome, "cap")
        self.assertEqual(state["max"], 1)
        self.assertEqual(state["rnd"], 1)
        rows = [r for r in jsonl_rows(state)
                if r["speaker"] not in ("system", "josh")]
        self.assertEqual([r["text"] for r in rows], ["a1", "b1"])

    def test_turns_still_clamps_mid_round(self):
        # delivered before seat 2's turn in round 1: the round finishes, and
        # the cap can never be set below the round already underway
        state = build_state(self.tmp, [["a1", "a2"], ["b1", "b2"]], turns=5)
        io = RecordingIO(human_script=[[], ["/turns 1"]])
        run_rounds(state, io)
        self.assertEqual(state["max"], 1)
        rows = [r for r in jsonl_rows(state)
                if r["speaker"] not in ("system", "josh")]
        self.assertEqual([r["text"] for r in rows], ["a1", "b1"])

    def test_interjection_fans_to_all(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        io = RecordingIO(human_script=[["hello you two"]])
        run_rounds(state, io)
        a, b = state["agents"]
        self.assertIn("Josh (human) interjects: hello you two", a.prompts[0])
        self.assertIn("Josh (human) interjects: hello you two", b.prompts[0])
        rows = [r for r in jsonl_rows(state) if r["speaker"] == "josh"]
        self.assertEqual([r["text"] for r in rows], ["hello you two"])

    def test_external_stop_flag(self):
        class StopIO(RecordingIO):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def should_stop(self):
                self.calls += 1
                return self.calls > 2   # let round 1 start, then stop

        state = build_state(self.tmp, [["a1", "a2"], ["b1", "b2"]], turns=5)
        outcome = run_rounds(state, StopIO())
        self.assertEqual(outcome, "stopped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
