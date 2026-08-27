"""Token-free tests for the persisted Panel Review workflow.

FakeAgent scripts every reply/failure, so these tests exercise the real
draft -> critique -> synthesis state machine without invoking any CLI.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import run_rounds

from test_loop import RecordingIO, build_state, jsonl_rows, saved_meta
from test_scheduler import RehydratableFake, attach_runtime


class RecordingRehydratableFake(RehydratableFake):
    """Rehydrate-compatible fake that also exposes received prompts."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prompts = []

    def turn(self, message, on_activity=None):
        self.prompts.append(message)
        return super().turn(message, on_activity=on_activity)


def panel_state(tmp, scripts, labels=None, synthesizer=0):
    state = build_state(tmp, scripts, turns=3, labels=labels)
    state["mode"] = "panel"
    state["panel"] = {"synthesizer": synthesizer}
    state["store"].save(state)
    return state


def seat_rows(state):
    return [row for row in jsonl_rows(state)
            if row["speaker"] not in ("system", "josh")]


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-panel-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exact_call_budget_phase_envelopes_and_complete_sources(self):
        state = panel_state(
            self.tmp,
            [["A draft", "A critique"],
             ["B draft", "B critique", "B synthesis"],
             ["C draft", "C critique"]],
            labels=["A", "B", "C"], synthesizer=1)

        outcome = run_rounds(state, RecordingIO())

        self.assertEqual(outcome, "wrapped")
        # N drafts + N critiques + one designated synthesis.
        self.assertEqual(sum(len(a.prompts) for a in state["agents"]), 7)
        self.assertEqual([len(a.prompts) for a in state["agents"]], [2, 3, 2])
        self.assertIn(relay.PANEL_SYNTHESIS_PROMPT,
                      state["agents"][1].prompts[-1])
        self.assertNotIn(relay.PANEL_SYNTHESIS_PROMPT,
                         state["agents"][0].prompts[-1])
        self.assertNotIn(relay.PANEL_SYNTHESIS_PROMPT,
                         state["agents"][2].prompts[-1])

        rows = seat_rows(state)
        self.assertEqual(len(rows), 7)
        by_intent = {
            intent: [r for r in rows if r.get("intent") == intent]
            for intent in ("answer", "critique", "synthesis")
        }
        self.assertEqual({k: len(v) for k, v in by_intent.items()},
                         {"answer": 3, "critique": 3, "synthesis": 1})
        self.assertEqual(by_intent["synthesis"][0]["speaker"], 1)
        self.assertEqual({r.get("thread_id") for r in rows}, {"panel:1"})

        panel = state["panel"]
        for phase, intent in (("draft", "answer"),
                              ("critique", "critique"),
                              ("synthesis", "synthesis")):
            self.assertEqual(
                panel["source_rows"][phase],
                [r["message_id"] for r in rows if r.get("intent") == intent])

        draft_rows = by_intent["answer"]
        for agent in state["agents"]:
            critique_prompt = next(
                p for p in agent.prompts if relay.PANEL_CRITIQUE_PROMPT in p)
            for draft in draft_rows:
                self.assertIn(draft["message_id"], critique_prompt)
                self.assertIn(draft["text"], critique_prompt)

    def test_prompts_carry_each_contribution_once(self):
        """Drafts reached critique prompts twice — once via the queue fan-out,
        once via the collected-drafts packet (~2x prompt tokens). The packet
        is the carrier; the fan-out must not double it."""
        state = panel_state(
            self.tmp,
            [["A draft", "A critique"],
             ["B draft", "B critique", "B synthesis"],
             ["C draft", "C critique"]],
            labels=["A", "B", "C"], synthesizer=1)
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "wrapped")
        for agent in state["agents"]:
            critique_prompt = next(
                p for p in agent.prompts if relay.PANEL_CRITIQUE_PROMPT in p)
            self.assertEqual(critique_prompt.count("B draft"), 1,
                             agent.name)
            self.assertEqual(critique_prompt.count("C draft"), 1,
                             agent.name)
        synth_prompt = state["agents"][1].prompts[-1]
        self.assertEqual(synth_prompt.count("A draft"), 1)
        self.assertEqual(synth_prompt.count("B critique"), 1)
        # rows still broadcast to the UI/transcript exactly as before
        rows = seat_rows(state)
        self.assertEqual(len(rows), 7)

    def test_a_phase_prompt_starts_with_its_phase(self):
        """_panel_prompt supplies the entire prompt body, so an EMPTY
        compose_prompt is correct here -- panel commits with fan_out=False, so
        every backlog is empty from the critique phase on, by design. A guard
        added elsewhere to stop a solo seat being handed "" once filled these
        prompts with "the other participants produced nothing" directly above
        those participants' drafts, and offered [[WRAP]] one line above "Do
        not use [[WRAP]]" -- in the shipping multi-seat preset."""
        state = panel_state(
            self.tmp,
            [["A draft", "A critique"],
             ["B draft", "B critique", "B synthesis"]],
            labels=["A", "B"], synthesizer=1)
        self.assertEqual(run_rounds(state, RecordingIO()), "wrapped")
        for agent in state["agents"]:
            for prompt in agent.prompts:
                if relay.PANEL_CRITIQUE_PROMPT in prompt:
                    self.assertTrue(prompt.startswith(
                        relay.PANEL_CRITIQUE_PROMPT), prompt[:200])
                if relay.PANEL_SYNTHESIS_PROMPT in prompt:
                    self.assertTrue(prompt.startswith(
                        relay.PANEL_SYNTHESIS_PROMPT), prompt[:200])
                self.assertNotIn(relay.IDLE_CONTINUE, prompt)
                self.assertNotIn(relay.SOLO_CONTINUE, prompt)

    def test_draft_and_critique_wrap_tokens_do_not_add_a_closing_lap(self):
        state = panel_state(
            self.tmp,
            [["A draft [[WRAP]]", "A critique [[WRAP]]", "A synthesis"],
             ["B draft", "B critique"]],
            labels=["A", "B"], synthesizer=0)
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "wrapped")
        self.assertEqual([len(a.prompts) for a in state["agents"]], [3, 2])
        self.assertEqual(state["panel"]["phase"], "done")
        self.assertIsNone(state.get("closing"))

    def test_failed_draft_and_critique_are_visible_but_do_not_block_synthesis(self):
        state = panel_state(
            self.tmp,
            [[RuntimeError("draft attempt one"),
              RuntimeError("draft attempt two"), "A critique"],
             ["B draft", RuntimeError("critique attempt one"),
              RuntimeError("critique attempt two")],
             ["C draft", "C critique", "C synthesis"]],
            labels=["A", "B", "C"], synthesizer=2)

        outcome = run_rounds(state, RecordingIO())

        self.assertEqual(outcome, "wrapped")
        self.assertEqual(state["panel"]["failed"]["draft"], [0])
        self.assertEqual(state["panel"]["failed"]["critique"], [1])
        self.assertEqual(state["panel"]["phase"], "done")
        self.assertCountEqual(
            [r["text"] for r in seat_rows(state)],
            ["B draft", "C draft", "A critique", "C critique",
             "C synthesis"])

        system_text = "\n".join(
            row["text"] for row in jsonl_rows(state)
            if row["speaker"] == "system")
        self.assertIn("A failed twice in Panel draft", system_text)
        self.assertIn("its contribution is absent", system_text)
        self.assertIn("B failed twice in Panel critique", system_text)

        synth_prompt = state["agents"][2].prompts[-1]
        self.assertIn("B draft", synth_prompt)
        self.assertIn("C draft", synth_prompt)
        self.assertIn("A critique", synth_prompt)
        self.assertIn("C critique", synth_prompt)
        self.assertNotIn("draft attempt", synth_prompt)
        self.assertNotIn("critique attempt", synth_prompt)

    def test_synthesis_double_failure_is_fatal_without_author_substitution(self):
        state = panel_state(
            self.tmp,
            [["A draft", "A critique"],
             ["B draft", "B critique", RuntimeError("synthesis one"),
              RuntimeError("synthesis two")],
             ["C draft", "C critique"]],
            labels=["A", "B", "C"], synthesizer=1)

        outcome = run_rounds(state, RecordingIO())

        self.assertEqual(outcome, "fatal")
        self.assertEqual(state["panel"]["phase"], "failed")
        self.assertEqual(state["panel"]["synthesizer"], 1)
        self.assertEqual([len(a.prompts) for a in state["agents"]], [2, 4, 2])
        self.assertEqual(
            [r for r in seat_rows(state) if r.get("intent") == "synthesis"],
            [])
        for i in (0, 2):
            self.assertFalse(any(relay.PANEL_SYNTHESIS_PROMPT in prompt
                                 for prompt in state["agents"][i].prompts))
        system_text = "\n".join(
            row["text"] for row in jsonl_rows(state)
            if row["speaker"] == "system")
        self.assertIn("B failed twice in Panel synthesis", system_text)

    def test_resume_does_not_replay_an_already_committed_draft(self):
        state = panel_state(
            self.tmp,
            [["A durable draft"], [], []],
            labels=["A", "B", "C"], synthesizer=2)
        panel = relay.ensure_panel_state(state)
        state["rnd"] = 1
        prompt, consumed, _first = relay._panel_prompt(state, 0, "draft")
        reply = state["agents"][0].turn(prompt)
        row = relay.commit_reply(
            state, 0, reply, consumed, RecordingIO(), force_broadcast=True,
            envelope_extra={"thread_id": panel["thread_id"],
                            "intent": "answer"})
        # Simulate the durable-row-before-meta boundary that resume recovery
        # promises to reconcile, then persist the recovered snapshot.
        panel = relay.ensure_panel_state(state)
        self.assertIn(0, panel["completed"]["draft"])
        self.assertIn(row["message_id"], panel["source_rows"]["draft"])
        state["store"].save(state)
        meta = saved_meta(state)

        with patch.dict(relay.AGENT_TYPES,
                        {"claude": RecordingRehydratableFake}):
            resumed = relay.rehydrate(meta)
        attach_runtime(resumed, state["store"].dir)
        scripts = [
            ["A critique"],
            ["B draft", "B critique"],
            ["C draft", "C critique", "C synthesis"],
        ]
        for agent, script in zip(resumed["agents"], scripts):
            agent.script = list(script)

        outcome = run_rounds(resumed, RecordingIO())

        self.assertEqual(outcome, "wrapped")
        self.assertEqual([len(a.prompts) for a in resumed["agents"]], [1, 2, 3])
        self.assertEqual(resumed["agents"][0].script, [])
        rows = seat_rows(resumed)
        a_drafts = [r for r in rows
                    if r["speaker"] == 0 and r.get("intent") == "answer"]
        self.assertEqual([r["text"] for r in a_drafts], ["A durable draft"])
        for agent in resumed["agents"]:
            critique_prompt = next(
                p for p in agent.prompts if relay.PANEL_CRITIQUE_PROMPT in p)
            self.assertIn(row["message_id"], critique_prompt)
            self.assertIn("A durable draft", critique_prompt)


if __name__ == "__main__":
    unittest.main()
