"""Plan mode is a CAPABILITY gate, not an instruction.

The failure this guards against: telling seats "only plan, don't write yet"
while non-yolo claude still holds Write/Edit and codex still holds
workspace-write. A seat can then plan and implement in the same turn, and the
gate looks implemented while enforcing nothing — the same class of mistake as
a role that promises a capability the flags don't grant (ROLES_DESIGN.md).

So these tests read the ACTUAL argv build_cmd produces. Token-free.
"""
import os, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import relay
from relay import (ClaudeAgent, CodexAgent, GeminiAgent, start_plan,
                   approve_plan, set_plan_mode, plan_phase)


def argv(agent):
    return " ".join(agent.build_cmd("do the thing"))


class PlanCapabilityTests(unittest.TestCase):
    WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_claude_drafting_loses_the_write_tools(self):
        a = ClaudeAgent(self.WS)
        self.assertIn("Write", argv(a))              # normally granted
        a.plan_mode = True
        cmd = argv(a)
        self.assertIn("--permission-mode plan", cmd)
        self.assertIn("--disallowedTools=Write,Edit,NotebookEdit,Bash", cmd)
        self.assertNotIn("acceptEdits", cmd)

    def test_codex_drafting_gets_the_read_only_sandbox(self):
        a = CodexAgent(self.WS)
        self.assertIn('sandbox_mode="workspace-write"', argv(a))
        a.plan_mode = True
        cmd = argv(a)
        self.assertIn('sandbox_mode="read-only"', cmd)
        self.assertNotIn("workspace-write", cmd)

    def test_plan_mode_outranks_yolo_on_every_adapter(self):
        """The whole point is that nothing is written before Josh approves,
        so a yolo conversation is NOT exempt."""
        for cls, bypass in ((ClaudeAgent, "--dangerously-skip-permissions"),
                            (CodexAgent,
                             "--dangerously-bypass-approvals-and-sandbox")):
            a = cls(self.WS, yolo=True)
            self.assertIn(bypass, argv(a))           # yolo really is on
            a.plan_mode = True
            self.assertNotIn(bypass, argv(a), f"{cls.__name__} stayed unsandboxed")
        g = GeminiAgent(self.WS, yolo=True)
        self.assertNotIn("--sandbox", argv(g))
        g.plan_mode = True
        self.assertIn("--sandbox", argv(g))

    def test_approval_restores_normal_capability(self):
        agents = [ClaudeAgent(self.WS), CodexAgent(self.WS)]
        state = {"agents": agents}
        start_plan(state, "build the thing")
        self.assertTrue(all(a.plan_mode for a in agents))
        self.assertIn("--permission-mode plan", argv(agents[0]))
        approve_plan(state, tasks=[{"id": "t1", "title": "x"}])
        self.assertFalse(any(a.plan_mode for a in agents))
        self.assertIn("acceptEdits", argv(agents[0]))
        self.assertIn('sandbox_mode="workspace-write"', argv(agents[1]))

    def test_approval_records_the_edited_plan_not_the_proposed_one(self):
        state = {"agents": []}
        start_plan(state, "original goal")
        state["plan"]["tasks"] = [{"id": "t1", "title": "seats proposed this"}]
        approve_plan(state, goal="Josh's goal",
                     tasks=[{"id": "t1", "title": "Josh edited this"}])
        self.assertEqual(state["plan"]["goal"], "Josh's goal")
        self.assertEqual(state["plan"]["tasks"][0]["title"], "Josh edited this")

    def test_editing_an_approved_plan_is_a_new_revision(self):
        state = {"agents": []}
        start_plan(state, "g")
        approve_plan(state, tasks=[{"id": "t1"}])
        self.assertEqual(state["plan"]["revision"], 1)
        approve_plan(state, tasks=[{"id": "t2"}])
        self.assertEqual(state["plan"]["revision"], 2)   # never a silent mutation
        self.assertEqual(plan_phase(state), "approved")

    def test_set_plan_mode_never_touches_a_running_child(self):
        """Approval takes effect at the next turn's build_cmd, so a seat
        already mid-turn keeps the capabilities it started with."""
        a = ClaudeAgent(self.WS)
        a.plan_mode = True
        before = argv(a)
        set_plan_mode({"agents": [a]}, False)
        self.assertNotEqual(before, argv(a))     # only the NEXT command changes

    def test_a_seat_without_plan_mode_is_unchanged(self):
        """Every existing conversation must produce byte-identical argv."""
        for cls in (ClaudeAgent, CodexAgent, GeminiAgent):
            a, b = cls(self.WS), cls(self.WS)
            b.plan_mode = False
            self.assertEqual(argv(a).replace(a.uid, "U"),
                             argv(b).replace(b.uid, "U"))


PLAN_REPLY = """Here is the plan.
[[TASK: t1 | owner=0 | do the first thing]]
[[WRAP]]"""


class PlanLoopTests(unittest.TestCase):
    """The gate inside the REAL loop, driven by FakeAgents. Token-free."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-plan-test-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self, replies, answer, turns=3):
        from test_loop import build_state, RecordingIO
        state = build_state(self.tmp, replies, turns=turns)
        relay.start_plan(state, "goal")
        io = RecordingIO()
        pending = [answer]
        io.ask_human = lambda payload, abort=None: (
            pending.pop(0) if pending else None)
        return state, io

    def test_wrap_during_drafting_opens_the_gate_not_closing_remarks(self):
        state, io = self._state([[PLAN_REPLY], ["sounds right"]],
                                "Approve & Execute")
        relay.run_rounds(state, io)
        self.assertEqual(relay.plan_phase(state), "approved")
        self.assertEqual(state["plan"]["tasks"][0]["id"], "t1")
        self.assertFalse(any(a.plan_mode for a in state["agents"]),
                         "approval did not restore the write tools")

    def test_declining_keeps_every_seat_read_only(self):
        state, io = self._state([[PLAN_REPLY, "more thought"], ["ok", "ok"]], "Keep planning")
        relay.run_rounds(state, io)
        self.assertEqual(relay.plan_phase(state), "drafting")
        self.assertTrue(all(a.plan_mode for a in state["agents"]))

    def test_an_unanswered_gate_is_never_read_as_approval(self):
        """Josh is away. Silence must not unlock the write tools — the same
        rule that stops an unanswered [[ASK]] becoming a forged answer."""
        state, io = self._state([[PLAN_REPLY, "more thought"], ["ok", "ok"]], None)
        relay.run_rounds(state, io)
        self.assertEqual(relay.plan_phase(state), "drafting")
        self.assertTrue(all(a.plan_mode for a in state["agents"]))

    def test_the_drafting_reply_is_still_relayed(self):
        """The gate runs AFTER commit_reply. An earlier draft returned before
        the commit, which silently dropped the plan reply itself."""
        state, io = self._state([[PLAN_REPLY], ["ok"]],
                                "Approve & Execute")
        relay.run_rounds(state, io)
        said = [p for p in io.events if p[0] == "message"]
        self.assertTrue(any("Here is the plan" in str(p[1]) for p in said),
                        "the plan reply never reached the transcript")

    def test_the_card_answer_carries_joshs_edits(self):
        """The edited task list is what gets approved — the proposed one and
        the approved one differ exactly when the gate did its job."""
        state, io = self._state(
            [[PLAN_REPLY], ["ok"]],
            {"approved": True, "goal": "Josh's goal",
             "tasks": [{"id": "t9", "title": "Josh wrote this"}]})
        relay.run_rounds(state, io)
        self.assertEqual(state["plan"]["goal"], "Josh's goal")
        self.assertEqual(state["plan"]["tasks"][0]["id"], "t9")

    def test_a_structured_rejection_keeps_seats_read_only(self):
        state, io = self._state([[PLAN_REPLY, "more"], ["ok", "ok"]],
                                {"approved": False})
        relay.run_rounds(state, io)
        self.assertEqual(relay.plan_phase(state), "drafting")
        self.assertTrue(all(a.plan_mode for a in state["agents"]))

    def test_the_awaiting_artifact_carries_id_revision_and_qid(self):
        state, io = self._state([[PLAN_REPLY], ["ok"]], "Approve & Execute")
        relay.run_rounds(state, io)
        plans = [p for e, p in io.events if e == "plan"]
        awaiting = [p for p in plans if p["phase"] == "awaiting"]
        self.assertTrue(awaiting)
        self.assertEqual(awaiting[0]["id"], "plan-1")
        self.assertEqual(awaiting[0]["revision"], 1)

    def test_a_malformed_task_never_takes_the_conversation_down(self):
        bad = "plan\n[[TASK: nonsense without fields]]\n[[WRAP]]"
        state, io = self._state([[bad], ["ok"]], "Approve & Execute")
        relay.run_rounds(state, io)          # must not raise
        self.assertEqual(relay.plan_phase(state), "approved")
        self.assertEqual(state["plan"]["tasks"], [])

    def test_the_plan_survives_a_save_and_rehydrate(self):
        state, io = self._state([[PLAN_REPLY], ["ok"]], "Approve & Execute")
        relay.run_rounds(state, io)
        state["store"].save(state)
        meta = relay.read_meta(state["store"].session_dir) \
            if hasattr(state["store"], "session_dir") else None
        if meta is None:
            import json
            with open(os.path.join(os.path.dirname(state["transcript"]),
                                   "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        self.assertEqual((meta.get("plan") or {}).get("phase"), "approved")

    def test_preamble_states_the_rules_only_while_drafting(self):
        from test_loop import build_state
        state = build_state(self.tmp, [["a"], ["b"]], turns=1)
        relay.start_plan(state, "g")
        agent = state["agents"][0]
        draft = relay.preamble(agent, state["agents"][1:], "", 1,
                               state["workspace"], roster=state["agents"],
                               plan=state["plan"])
        self.assertIn("PLANNING PHASE", draft)
        relay.approve_plan(state)
        after = relay.preamble(agent, state["agents"][1:], "", 1,
                               state["workspace"], roster=state["agents"],
                               plan=state["plan"])
        self.assertNotIn("PLANNING PHASE", after)


if __name__ == "__main__":
    load = unittest.TestLoader().loadTestsFromTestCase
    r = unittest.TextTestRunner(verbosity=0).run(
        unittest.TestSuite([load(PlanCapabilityTests), load(PlanLoopTests)]))
    print("OK" if r.wasSuccessful() else "FAILED")
    sys.exit(0 if r.wasSuccessful() else 1)
