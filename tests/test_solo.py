"""Token-free tests for the SOLO seat — Alloy as a harness for one agent.

Alloy is a relay between several AIs and, since 2026-08-26, a harness for a
single one (the DeepSeek Harness / Traycer shape). This suite is the guard on
that second shape, and it is deliberately layered the way this repo learned to
layer things:

* the ENGINE, driven through the real `run_rounds` with FakeAgents;
* the BRIDGE, driven through the real `app.Api` with a fake window — because
  "the engine is perfect and the bridge drops the key" is a bug class this
  repo has shipped twice (the permission pill, the desktop rungs);
* the UI, as static-source guards over `ui/index.html` (its executable
  cousins live in tests/test_ui_boot.py, the only suite that can run the
  page's one inline script).

The load-bearing test is `test_no_turn_ever_receives_an_empty_prompt`. The
measured bug it pins: multi-seat backlogs are refilled by commit_reply's
fan-out to PEERS, so with one seat the backlog was empty from turn 2 and the
engine handed the adapter "". Against a real CLI that is `claude -p ""` —
measured exit 1, "Input must be provided either through stdin or as a prompt
argument when using --print" — which the loop reads as a provider failure,
retries into the identical wall, parks the only seat and reports "Every seat
has failed twice this run". A FakeAgent cannot see that on its own: it answers
happily whatever it is handed, which is exactly why an earlier probe of five
modes at n=1 concluded "works".

Run:  python tests/test_solo.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import outcome as outcome_mod
import relay
from relay import ClaudeAgent, LoopIO, preamble, run_rounds

from test_loop import FakeAgent, RecordingIO, build_state, saved_meta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html")


def solo_state(tmp, script=None, turns=3, **kw):
    """A one-seat state built by the SAME helper every other suite uses, so a
    solo run differs from a group run only in its roster."""
    state = build_state(tmp, [list(script or ["ok"] * turns)], turns=turns,
                        labels=["Solo"], **kw)
    return state


class FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)
        return None


def scripted_agent_class(name, replies):
    """A real Agent subclass whose ONLY fake is `turn` — the shape the bridge
    suites use, so the assertions run against shipping build_cmd/state code."""
    class Scripted(relay.ClaudeAgent):
        def __init__(self, workspace, **kw):
            kw.setdefault("name", name)
            super().__init__(workspace, **kw)
            self._left = list(replies)

        def turn(self, message, on_activity=None):
            self.session_id = f"scripted-{self.uid}"
            return self._left.pop(0) if self._left else "(done)"
    return Scripted


# --------------------------------------------------------------- the engine

class SoloLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-solo-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_turn_ever_receives_an_empty_prompt(self):
        """THE solo bug. Nothing fans in to a lone seat, so from turn 2 the
        composed prompt was ''. Every adapter passes the prompt as one argv
        element; an empty one is a hard CLI error, not an empty turn."""
        state = solo_state(self.tmp, turns=3)
        run_rounds(state, RecordingIO())
        prompts = state["agents"][0].prompts
        self.assertEqual(len(prompts), 3, prompts)
        for n, p in enumerate(prompts, 1):
            self.assertTrue(p.strip(), f"turn {n} got an empty prompt")

    def test_the_continuation_is_named_and_pushes_against_padding(self):
        state = solo_state(self.tmp, turns=2)
        run_rounds(state, RecordingIO())
        later = state["agents"][0].prompts[1]
        self.assertIn("Nothing new from Josh", later)
        self.assertIn(relay.WRAP_TOKEN, later)
        self.assertNotIn("other participant", later)

    def test_a_multi_seat_backlog_gap_gets_the_group_wording(self):
        """The same hole exists at n>1 whenever every peer was skipped, so the
        guard is general — but a group room must not be told 'nothing new from
        Josh' when what happened is that its peers went quiet."""
        state = build_state(self.tmp, [["a"], ["b"]], turns=1)
        state["pending"] = {0: [], 1: []}
        state["introduced"] = [True, True]
        msg, consumed, first = relay.compose_prompt(state, 0)
        self.assertEqual((consumed, first), (0, False))
        self.assertIn("No new messages have reached you", msg)
        self.assertNotIn("Nothing new from Josh", msg)

    def test_a_solo_panel_phase_is_not_given_the_continuation_filler(self):
        """The filler exists so a seat is never handed "". _panel_prompt
        supplies its own body, so an empty compose_prompt is correct there --
        and injecting the filler would open a critique with a sentence its own
        payload contradicts."""
        state = solo_state(self.tmp, script=["draft", "critique", "final"],
                           turns=3)
        state["mode"] = "panel"
        state["panel"] = {"synthesizer": 0}
        run_rounds(state, RecordingIO())
        for prompt in state["agents"][0].prompts:
            self.assertTrue(prompt.strip())
            self.assertNotIn(relay.SOLO_CONTINUE, prompt)
            self.assertNotIn(relay.IDLE_CONTINUE, prompt)

    def test_a_continuous_solo_run_is_not_promised_a_limit_it_does_not_have(self):
        """`turn_ceiling` is None BY DESIGN in Keep Improving, and
        effective_ceiling is the only thing allowed to read it. compose_prompt
        read it raw and the preamble applied `or DEFAULT_CEILING`, so the seat
        was told a 60-turn safety limit existed in the one mode whose whole
        point is that it has none."""
        state = solo_state(self.tmp, turns=2)
        state["until_done"] = True
        # A NON-None turn_ceiling on a continuous run is the case that
        # separates the two readers: raw it says 60, effective_ceiling says
        # unbounded. Reachable on a chat that carried a ceiling before Keep
        # Improving was switched on.
        state["turn_ceiling"] = 60
        state["continuous"] = relay.continuous_policy({"on": True})
        self.assertIsNone(relay.effective_ceiling(state))
        state["introduced"] = [False]
        prompt, _consumed, _first = relay.compose_prompt(state, 0)
        self.assertIn("no turn limit at all", prompt)
        self.assertNotIn("safety limit of 60", prompt)
        # ...and a run that DOES have a ceiling still names it
        state["continuous"] = None
        state["turn_ceiling"] = 40
        state["introduced"] = [False]
        prompt, _c, _f = relay.compose_prompt(state, 0)
        self.assertIn("safety limit of 40 total turns", prompt)

    def test_the_refusal_reason_reaches_outcome_json_not_just_meta(self):
        """`seat_count` is persisted on state, but the loop returns the
        generic "starved" and outcome.py's inferred value used to overwrite
        it -- so hard_facts read "seats died after double failures" for a run
        that was never runnable and never asked anyone for a turn."""
        state = solo_state(self.tmp, turns=2)
        state["mode"] = "free"
        state["orchestration"] = relay.normalize_orchestration(
            None, "free", 2, False)
        self.assertEqual(run_rounds(state, RecordingIO()), "starved")
        path = os.path.join(state["store"].dir, "outcome.json")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8") as f:
            facts = json.load(f)["hard_facts"]
        self.assertEqual(facts["termination_reason"], "seat_count")

    def test_a_solo_run_reaches_its_cap_and_stays_resumable(self):
        state = solo_state(self.tmp, turns=2)
        outcome = run_rounds(state, RecordingIO())
        self.assertEqual(outcome, "cap")
        meta = saved_meta(state)
        self.assertEqual(len(meta["seats"]), 1)
        self.assertEqual(relay.continue_block(meta), "",
                         "a solo chat must be resumable, not view-only")

    def test_a_solo_seat_can_wrap(self):
        state = solo_state(self.tmp, script=["done here. " + relay.WRAP_TOKEN,
                                             "unused"], turns=3)
        self.assertEqual(run_rounds(state, RecordingIO()), "wrapped")
        self.assertEqual(state["closing"], [],
                         "no closing lap exists with nobody else to speak")

    def test_a_human_message_reads_as_conversation_not_interruption(self):
        state = solo_state(self.tmp, turns=2)
        run_rounds(state, RecordingIO(human_script=[[], ["look at x"]]))
        seen = "\n".join(state["agents"][0].prompts)
        self.assertIn("Josh (human) says: look at x", seen)
        self.assertNotIn("interjects", seen)

    def test_the_group_wording_survives_for_a_group(self):
        state = build_state(self.tmp, [["a", "a"], ["b", "b"]], turns=2)
        run_rounds(state, RecordingIO(human_script=[[], ["hello you two"]]))
        seen = "\n".join(state["agents"][0].prompts
                         + state["agents"][1].prompts)
        self.assertIn("Josh (human) interjects: hello you two", seen)

    def test_the_pause_note_speaks_about_one_agent_and_counts_nothing(self):
        """"Every seat has failed twice" is wrong twice over at n=1: there is
        one agent, and the run also reaches this state after a SINGLE
        no-retry event -- a Stop, or one turn timeout -- so a failure count is
        a claim the engine cannot back."""
        state = solo_state(self.tmp, script=[RuntimeError("boom")] * 4,
                           turns=3)
        io = RecordingIO()
        self.assertEqual(run_rounds(state, io), "starved")
        said = " ".join(p.get("text", "") for e, p in io.events
                        if e == "status")
        self.assertIn("The agent is no longer taking turns", said)
        self.assertNotIn("Every seat has failed", said)
        self.assertNotIn("failed twice", said)

    def test_stopping_the_only_agent_pauses_the_run_and_says_so(self):
        state = solo_state(self.tmp,
                           script=[relay.TurnCancelled("stopped")], turns=3)
        io = RecordingIO()
        self.assertEqual(run_rounds(state, io), "starved")
        said = " ".join(p.get("text", "") for e, p in io.events
                        if e == "status")
        self.assertIn("no longer taking turns", said)
        self.assertNotIn("failed twice", said)


class SoloModeAvailabilityTests(unittest.TestCase):
    """Which modes may run at n=1, decided in ONE table."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-solo-mode-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_zero_seats_always_refuses(self):
        for mode in relay.IMPLEMENTED_MODES:
            self.assertEqual(relay.seat_count_refusal(mode, 0),
                             "Pick at least one participant.", mode)

    def test_one_seat_is_allowed_for_every_mode_but_free_and_battle(self):
        blocked = {"free", "battle"}
        for mode in relay.IMPLEMENTED_MODES:
            why = relay.seat_count_refusal(mode, 1)
            if mode in blocked:
                self.assertTrue(why, mode)
            else:
                self.assertEqual(why, "", mode)

    def test_two_seats_are_fine_everywhere(self):
        for mode in relay.IMPLEMENTED_MODES:
            self.assertEqual(relay.seat_count_refusal(mode, 2), "", mode)

    def test_a_battle_still_needs_exactly_two(self):
        self.assertTrue(relay.seat_count_refusal("battle", 3))

    def test_each_bound_gets_its_own_reason(self):
        """One sentence for both bounds explained an n=1 problem to someone
        who brought three seats ("a blind A/B vote over one answer") -- with
        three there are three answers, not one."""
        few = relay.seat_count_refusal("battle", 1)
        many = relay.seat_count_refusal("battle", 3)
        self.assertNotEqual(few, many)
        self.assertIn("over one answer", few)
        self.assertIn("three answers", many)

    def test_free_refuses_up_front_instead_of_pausing_after_zero_turns(self):
        state = solo_state(self.tmp, turns=2)
        state["mode"] = "free"
        state["orchestration"] = relay.normalize_orchestration(
            None, "free", 2, False)
        io = RecordingIO()
        self.assertEqual(run_rounds(state, io), "starved")
        self.assertEqual(state.get("termination_reason"), "seat_count")
        self.assertEqual(state["agents"][0].prompts, [],
                         "no seat is asked for a turn it cannot take")
        said = " ".join(p.get("text", "") for e, p in io.events
                        if e == "status")
        self.assertIn("Talk Live needs at least two participants", said)

    def test_battle_refuses_in_the_engine_not_only_in_the_bridge(self):
        state = solo_state(self.tmp, turns=1)
        state["mode"] = "battle"
        state["orchestration"] = relay.normalize_orchestration(
            None, "battle", 1, False)
        state["battle"] = {"phase": "blind", "slots": [0]}
        io = RecordingIO()
        self.assertEqual(run_rounds(state, io), "starved")
        self.assertEqual(state.get("termination_reason"), "seat_count")
        said = " ".join(p.get("text", "") for e, p in io.events
                        if e == "status")
        self.assertIn("Arena Duel needs exactly two", said)
        self.assertNotIn("Both answers are in", said)

    def test_the_refusal_reason_survives_into_outcome_facts(self):
        self.assertIn("seat_count", outcome_mod.TERMINATION_REASONS)
        self.assertNotEqual("seat_count", "starved")


# ------------------------------------------------------------- the preamble

class SoloPreambleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-solo-pre-")
        self.agent = ClaudeAgent(self.tmp, name="Claude")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def solo(self, **kw):
        return preamble(self.agent, [], "make this better", 5, self.tmp,
                        roster=[self.agent], **kw)

    def test_the_multi_ai_sentence_never_comes_back(self):
        """RED guard. The visible symptom of the whole problem was a first
        sentence naming nobody: 'in a live multi-AI conversation with .'"""
        text = self.solo()
        self.assertNotIn("multi-AI conversation", text)
        self.assertNotIn("conversation with .", text)
        self.assertNotIn("other participant", text)
        self.assertNotIn("each other", text)

    def test_josh_is_the_correspondent_not_someone_to_ignore(self):
        text = self.solo()
        self.assertNotIn("talk to the other AI(s), not to him", text)
        self.assertIn("Talk to him directly", text)
        self.assertIn("only agent in this session", text)

    def test_no_turn_order_and_no_nomination_token(self):
        for mode in ("round_robin", "speaker", "moderator", "parallel"):
            text = self.solo(mode=mode)
            self.assertNotIn("[[NEXT:", text, mode)
            self.assertNotIn("Turn order", text, mode)

    def test_the_workspace_is_not_shared_with_anybody(self):
        text = self.solo()
        self.assertIn("scratch workspace", text)
        self.assertNotIn("co-write", text)

    def test_the_workspace_line_does_not_outrank_the_permission_rung(self):
        """At read_only claude's build_cmd really does remove Write/Edit/Bash,
        and the restored solo capability block says so three lines above -- so
        "write files there freely" would contradict a true sentence in the
        same prompt."""
        ro = ClaudeAgent(self.tmp, name="Claude", permission="read_only")
        text = preamble(ro, [], "t", 3, self.tmp, roster=[ro])
        self.assertIn("write tools are switched off", text)
        self.assertNotIn("write files there freely", text)
        rw = ClaudeAgent(self.tmp, name="Claude", permission="auto")
        self.assertIn("write files there freely",
                      preamble(rw, [], "t", 3, self.tmp, roster=[rw]))

    def test_the_cap_line_talks_about_work_not_an_exhausted_topic(self):
        text = self.solo()
        self.assertIn("at most 5 turns", text)
        self.assertNotIn("topic feels genuinely exhausted", text)
        self.assertIn(relay.WRAP_TOKEN, text)

    def test_asking_josh_is_the_normal_loop_not_a_rare_exception(self):
        text = self.solo(ask=True)
        self.assertIn("[[ASK:", text)
        self.assertNotIn("shared with everyone", text)
        self.assertNotIn("Ask sparingly", text)

    def test_the_reply_shape_rule_does_not_fight_a_work_session(self):
        text = self.solo()
        self.assertNotIn("No markdown headers", text)
        self.assertIn("what is next", text)

    def test_the_honest_rung_ceiling_still_reaches_a_solo_seat(self):
        """capability_note()'s block was suppressed entirely at n=1, which
        silently dropped advisory_rung_note() — the admission that at auto or
        full access the desktop/browser ladders are a guardrail, not a
        boundary. A solo harness is exactly what that was written for."""
        agent = ClaudeAgent(self.tmp, name="Claude", permission="full",
                            desktop="ask")
        text = preamble(agent, [], "t", 3, self.tmp, roster=[agent])
        self.assertIn("What you can actually do here", text)
        self.assertIn("guardrail against accident", text)
        self.assertNotIn("hand it over", text)

    def test_a_role_is_owned_without_anybody_to_hand_back_to(self):
        self.agent.role = "Auditor"
        self.agent.role_instructions = "Cite every claim."
        text = self.solo()
        self.assertIn("Your role is Auditor", text)
        self.assertNotIn("hand back", text)

    def test_the_group_preamble_is_untouched(self):
        """A regression net: everything above must be additive."""
        others = [ClaudeAgent(self.tmp, name="GPT")]
        text = preamble(self.agent, others, "t", 3, self.tmp,
                        roster=[self.agent] + others)
        self.assertIn("in a live multi-AI conversation with GPT", text)
        self.assertIn("build on each other's points", text)
        self.assertIn("You share a scratch workspace", text)


class SoloPromptConstantTests(unittest.TestCase):
    """Every relay-owned prompt a solo room can actually reach."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-solo-prompts-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_supervisor_plans_a_wave_not_one_task_per_seat(self):
        state = solo_state(self.tmp, turns=1)
        voice = relay.supervisor_voice(state)
        self.assertIn("ONE agent", voice["plan_intro"])
        self.assertIn("WAVE of two to five tasks", voice["plan_teamwork"])
        self.assertNotIn("AT THE SAME TIME", voice["plan_intro"])

    def test_a_group_supervisor_keeps_its_parallel_voice(self):
        state = build_state(self.tmp, [["a"], ["b"]], turns=1)
        voice = relay.supervisor_voice(state)
        self.assertIn("AT THE SAME TIME", voice["plan_intro"])
        self.assertIn("One task per seat", voice["plan_teamwork"])

    def test_both_supervisor_prompts_still_format(self):
        for template, keys in ((relay.SUPERVISOR_PROMPT,
                                dict(roster="r", playbook="", goal="g")),
                               (relay.SUPERVISOR_REVIEW_PROMPT,
                                dict(roster="r", playbook="", goal="g",
                                     report="rep", used="a", left=1,
                                     plural=""))):
            for shape in ("solo", "team"):
                v = relay.SUPERVISOR_VOICE[shape]
                intro = v["plan_intro"] if template is relay.SUPERVISOR_PROMPT \
                    else v["review_intro"]
                work = v["plan_teamwork"] \
                    if template is relay.SUPERVISOR_PROMPT \
                    else v["review_teamwork"]
                text = template.format(intro=intro, teamwork=work, **keys)
                self.assertIn("[[TASK:", text)

    def test_a_solo_workstream_brief_promises_no_audience(self):
        import workstreams as ws
        state = solo_state(self.tmp, turns=1)
        state["workstreams"] = [ws.make_task("t1", 0, "write x.txt",
                                             files=["x.txt"])]
        relay.assign_workstreams(state, RecordingIO())
        brief = state["pending"][0][0]
        self.assertNotIn("the other seats are not hearing this", brief)
        self.assertIn("reply when it is complete", brief)

    def test_compact_asks_for_task_state_not_a_roster_summary(self):
        self.assertIn("who is participating", relay.COMPACT_PROMPT)
        self.assertNotIn("who is participating", relay.COMPACT_PROMPT_SOLO)
        self.assertIn("what is still open", relay.COMPACT_PROMPT_SOLO)

    def test_the_plan_gate_does_not_ask_one_agent_to_agree_with_others(self):
        self.assertNotIn("with the others", relay.PLAN_PROMPT_SOLO)
        self.assertNotIn("ONE of you", relay.PLAN_PROMPT_SOLO)
        self.assertIn("[[TASK:", relay.PLAN_PROMPT_SOLO)

    def test_a_solo_moderator_is_told_the_pick_is_forced(self):
        self.assertIn("already decided", relay.MODERATOR_PROMPT_SOLO)
        self.assertIn("DONE", relay.MODERATOR_PROMPT_SOLO)

    def test_the_brief_keeps_its_content_and_changes_only_its_reason(self):
        brief = {"status": "verbatim-ok", "mode": "verbatim",
                 "sources": ["CLAUDE.md"], "quotes": "the docs"}
        group = relay.brief_preamble_block(brief, ClaudeAgent)
        solo = relay.brief_preamble_block(brief, ClaudeAgent, solo=True)
        for block in (group, solo):
            self.assertIn("the docs", block)
        self.assertIn("every participant", group)
        self.assertNotIn("every participant", solo)
        self.assertNotIn("everyone has the same text", solo)


class SoloSupervisorTests(unittest.TestCase):
    """The flagship solo shape: planner + one executor + verification. That is
    the Traycer/DeepSeek-Harness architecture, and it is the reason lowering
    the seat floor is worth more than convenience."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-solo-sup-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sup_state(self, script, turns=4):
        state = solo_state(self.tmp, script=script, turns=turns)
        state["mode"] = "supervisor"
        state["orchestration"] = relay.normalize_orchestration(
            None, "supervisor", turns, False)
        state["topic"] = "build the thing"
        return state

    def test_one_agent_works_a_whole_wave_before_the_manager_reviews(self):
        """One owner does NOT mean one task: next_assignments starts at most
        one task per owner and settle_workstream immediately starts the next,
        so a wave runs through in order under a single review."""
        plan = ("Three ordered pieces.\n"
                "[[TASK: one | owner=0 | files=a.txt | write a.txt]]\n"
                "[[TASK: two | owner=0 | files=b.txt | write b.txt]]\n"
                "[[TASK: three | owner=0 | files=c.txt | write c.txt]]")
        state = self.sup_state(["did a", "did b", "did c", "idle"])
        # the seat must actually DELIVER: workstream status is verified
        # against the filesystem, not against what the worker claims
        agent = state["agents"][0]
        real_turn, made = agent.turn, []

        def writing_turn(message, on_activity=None):
            for name in ("a.txt", "b.txt", "c.txt"):
                if name in message and name not in made:
                    made.append(name)
                    with open(os.path.join(state["workspace"], name), "w") as f:
                        f.write(name)
                    break
            return real_turn(message, on_activity=on_activity)

        agent.turn = writing_turn
        real_build, real_gate = relay.build_supervisor, relay.wave_gate
        relay.build_supervisor = lambda st: _StubSide(plan)
        relay.wave_gate = lambda st, io: None
        try:
            run_rounds(state, RecordingIO())
        finally:
            relay.build_supervisor, relay.wave_gate = real_build, real_gate
        self.assertEqual(made, ["a.txt", "b.txt", "c.txt"],
                         "each task's brief reached the one seat in order")
        tasks = state["workstreams"]
        self.assertEqual([t["id"] for t in tasks], ["one", "two", "three"])
        # Terminal, not specifically "done": verify_deliverable compares file
        # mtime against the task's start time, and a synthetic write that
        # lands in the same clock tick can read as stale. What this test is
        # about is that all three tasks were DISPATCHED and SETTLED under one
        # owner in one wave, which is exactly the claim the solo planner
        # prompt now makes.
        self.assertEqual([t["status"] in ("done", "failed") for t in tasks],
                         [True, True, True], tasks)

    def test_the_planner_is_told_it_has_one_worker(self):
        state = self.sup_state(["ok"])
        seen = {}

        class Spy(_StubSide):
            def turn(self, message, on_activity=None):
                seen["prompt"] = message
                return "[[TASK: one | owner=0 | think about it]]"

        real = relay.build_supervisor
        relay.build_supervisor = lambda st: Spy("")
        try:
            relay.plan_workstreams(state, RecordingIO())
        finally:
            relay.build_supervisor = real
        self.assertIn("ONE agent", seen["prompt"])
        self.assertIn("WAVE of two to five tasks", seen["prompt"])
        self.assertNotIn("AT THE SAME TIME", seen["prompt"])

    def test_a_degraded_plan_still_leaves_a_working_solo_conversation(self):
        """Planner failure must never strand the one seat with no prompt."""
        state = self.sup_state(["a", "b"], turns=2)
        real = relay.build_supervisor
        relay.build_supervisor = lambda st: _StubSide("no directives here")
        try:
            run_rounds(state, RecordingIO())
        finally:
            relay.build_supervisor = real
        self.assertFalse(state.get("workstreams"))
        for p in state["agents"][0].prompts:
            self.assertTrue(p.strip())


class _StubSide:
    """A stateless side call — never a real CLI."""

    def __init__(self, reply):
        self.reply = reply
        self.last_usage = None
        self.session_id = None

    def turn(self, message, on_activity=None):
        return self.reply


# --------------------------------------------------------------- resumption

class SoloResumeTests(unittest.TestCase):
    """The non-obvious floor. continue_block feeds session_summary's
    can_continue, which drives setSeated, the composer's continue branch and
    the rail tooltip — and rehydrate RAISES on it. Left at two seats, every
    solo chat would start fine and then be permanently view-only, with typing
    into it silently starting a brand new conversation."""

    def meta(self, n):
        return {"v": 2, "workspace": ".", "seats": [
            {"id": i, "provider": "claude", "model": "claude-haiku-4-5",
             "effort": "low", "label": f"S{i}", "pending": [],
             "introduced": False, "session_id": None} for i in range(n)]}

    def test_one_seat_is_resumable(self):
        self.assertEqual(relay.continue_block(self.meta(1)), "")

    def test_zero_seats_is_still_view_only(self):
        self.assertEqual(relay.continue_block(self.meta(0)),
                         "Incomplete chat — view only")

    def test_rehydrate_rebuilds_a_solo_chat(self):
        state = relay.rehydrate(self.meta(1))
        self.assertEqual(len(state["agents"]), 1)

    def test_session_summary_says_it_can_continue(self):
        tmp = tempfile.mkdtemp(prefix="alloy-solo-sum-")
        try:
            meta = self.meta(1)
            meta.update({"title": "solo", "created": "2026-08-26T00:00:00",
                         "topic": "t"})
            with open(os.path.join(tmp, "meta.json"), "w",
                      encoding="utf-8") as f:
                json.dump(meta, f)
            open(os.path.join(tmp, "transcript.md"), "w").close()
            summary = relay.session_summary(tmp)
            self.assertTrue(summary["can_continue"], summary)
            self.assertEqual(len(summary["participants"]), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- the seats

class SoloSeatPlumbingTests(unittest.TestCase):
    def test_one_auto_named_seat_has_no_ordinal(self):
        self.assertEqual(relay.assign_labels([("claude", None, None)]),
                         ["Claude"])

    def test_side_calls_stay_on_the_solo_seats_own_provider(self):
        """A solo Ox room must not quietly spend a Claude call — the exact
        bug the helper_spec chain was written to stop, checked at n=1."""
        for provider in ("ox", "gpt", "gemini", "claude"):
            self.assertEqual(relay.helper_spec([provider])["provider"],
                             provider)

    def test_a_one_seat_team_is_legal(self):
        slots, _opts, opener = relay.parse_team("claude | do the thing")
        self.assertEqual(len(slots), 1)
        self.assertEqual(opener, "do the thing")

    def test_a_zero_seat_team_is_not(self):
        with self.assertRaises(ValueError):
            relay.parse_team(" | do the thing")

    def test_a_team_mode_must_suit_the_team_roster(self):
        """Lowering the floor to one made `[[TEAM: claude | mode=free | x]]`
        parse. The child is then refused at zero turns -- and _team_body still
        spent a call asking a seat with no session and no memory to REPORT on
        it, handing the requester a forged account of work that never
        happened."""
        for spec in ("claude | rounds=2 mode=free | x",
                     "claude | rounds=2 mode=battle | x"):
            with self.assertRaises(ValueError, msg=spec):
                relay.parse_team(spec)
        # ...and a mode that DOES suit one seat still parses
        slots, opts, _ = relay.parse_team("claude | mode=round-robin | x")
        self.assertEqual((len(slots), opts["mode"]), (1, "round_robin"))

    def test_a_solo_room_round_trips_through_a_saved_room(self):
        tmp = tempfile.mkdtemp(prefix="alloy-solo-room-")
        try:
            path = os.path.join(tmp, "rooms.json")
            cfg = {"seats": [{"id": 0, "provider": "claude",
                              "enabled": True}], "turns": 4}
            relay.save_room("solo harness", cfg, path=path)
            rooms = relay.list_rooms(path=path)["rooms"]
            self.assertEqual(len(rooms), 1)
            self.assertEqual(rooms[0]["cfg"]["seats"], cfg["seats"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- the bridge

class SoloBridgeTests(unittest.TestCase):
    """The house rule that cost this repo two silently-dead rungs: a delivery
    control needs a test at the BRIDGE, not only at the engine. These drive
    the real app.Api, whose only fake is the seat's `turn`."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-solo-bridge-")
        self._old_sessions = app.SESSIONS_DIR
        self._old_relay_sessions = relay.SESSIONS_DIR
        self._old_tabs = relay.TABS_FILE
        app.SESSIONS_DIR = self.tmp
        relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        self._old_types = dict(relay.AGENT_TYPES)

    def tearDown(self):
        app.SESSIONS_DIR = self._old_sessions
        relay.SESSIONS_DIR = self._old_relay_sessions
        relay.TABS_FILE = self._old_tabs
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def api(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["c1"])
        api = app.Api()
        api._window = FakeWindow()
        return api

    def events(self, api):
        # emit() wraps payloads as uiEvent({"event": ..., "payload": ...})
        api._emit_q.join()
        out = []
        for call in api._window.calls:
            body = json.loads(call[len("uiEvent("):-1])
            out.append((body["event"], body.get("payload") or {}))
        return out

    def start(self, api, seats, **cfg):
        cfg.setdefault("opener", "hi")
        cfg.setdefault("turns", 1)
        cfg["seats"] = seats
        api._conversation(cfg)
        return self.events(api)

    def test_one_seat_starts_a_real_conversation(self):
        api = self.api()
        events = self.start(api, [{"id": 0, "provider": "claude",
                                   "enabled": True}])
        names = [e for e, _ in events]
        self.assertIn("started", names, names)
        self.assertNotIn("error", names,
                         [p for e, p in events if e == "error"])
        self.assertIsNotNone(api._conv, "the run never started")

    def test_zero_seats_still_refuses(self):
        api = self.api()
        events = self.start(api, [])
        errors = [p["message"] for e, p in events if e == "error"]
        self.assertEqual(errors, ["Pick at least one participant."])

    def test_a_solo_battle_is_refused_by_name(self):
        api = self.api()
        events = self.start(api, [{"id": 0, "provider": "claude",
                                   "enabled": True}],
                            orchestration={"legacy_mode": "battle"},
                            mode="battle")
        errors = " ".join(p["message"] for e, p in events if e == "error")
        self.assertIn("Arena Duel needs exactly two", errors)

    def test_a_solo_live_room_is_refused_by_name(self):
        api = self.api()
        events = self.start(api, [{"id": 0, "provider": "claude",
                                   "enabled": True}],
                            orchestration={"legacy_mode": "free"},
                            mode="free")
        errors = " ".join(p["message"] for e, p in events if e == "error")
        self.assertIn("Talk Live needs at least two", errors)

    def test_a_solo_chat_reopens_as_continuable(self):
        api = self.api()
        self.start(api, [{"id": 0, "provider": "claude", "enabled": True}])
        run = api._runs.focused()
        reopened = api.open_session(run.id)["session"]
        self.assertTrue(reopened.get("can_continue"), reopened)
        self.assertEqual(len(reopened["participants"]), 1)

    def test_the_bridge_tells_the_brief_writer_it_is_a_solo_room(self):
        """The engine side is tested in test_brief; this is the half that has
        bitten this repo twice - the bridge computing the value and never
        passing it on."""
        seen = {}
        # app.py imports project_brief BY NAME, so patching relay's attribute
        # would leave app's own reference untouched and the spy would never
        # fire -- a test that quietly exercises nothing.
        real = app.project_brief

        def spy(workspace, session_dir, **kw):
            seen["solo"] = kw.get("solo")
            return real(workspace, session_dir, **kw)

        app.project_brief = spy
        try:
            api = self.api()
            self.start(api, [{"id": 0, "provider": "claude", "enabled": True}])
        finally:
            app.project_brief = real
        self.assertIs(seen.get("solo"), True, seen)

    def test_the_webhook_refuses_a_payload_with_no_seatable_provider(self):
        """It used to silently become the default three-seat room and report
        started — a script would see success for a room it never asked for."""
        api = self.api()
        with self.assertRaises(ValueError):
            api._webhook_on_start({"topic": "t", "seats": ["not-a-provider"]})


# ------------------------------------------------------------------- the UI

class SoloUiSourceTests(unittest.TestCase):
    """Static guards. The executable ones live in tests/test_ui_boot.py, the
    only suite that can run ui/index.html's single inline script."""

    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as f:
            cls.source = f.read()

    def test_the_send_floor_is_one_not_two(self):
        self.assertNotIn("Pick at least two participants", self.source)
        self.assertIn("presetSeatRefusal(selectedPreset(), enabled.length)",
                      self.source)

    def test_the_ui_table_mirrors_the_engines(self):
        """Two tables, one truth. relay.MODE_SEAT_LIMITS is keyed by engine
        mode and the UI's by preset, so this maps one onto the other rather
        than trusting them to agree -- and they must agree EXACTLY. Refusing
        in the UI something the engine allows is how one Josh-facing string
        came to promise a run another Josh-facing string refused."""
        for preset, mode in (("live_room", "free"), ("arena", "battle")):
            self.assertIn(f"{preset}: {{min: 2", self.source, preset)
            self.assertEqual(relay.MODE_SEAT_LIMITS[mode][0], 2, mode)
        self.assertEqual(set(relay.MODE_SEAT_LIMITS), {"free", "battle"})
        # panel runs solo (draft -> self-critique -> synthesis), so neither
        # side refuses it; it is OFFERED under a name that says what it does.
        self.assertNotIn("panel_review: {min:", self.source)
        self.assertIn('soloName: "Draft, Critique, Finalise"', self.source)

    def test_unavailable_modes_state_why_instead_of_vanishing(self):
        self.assertIn('b.classList.add("mode-off")', self.source)
        self.assertIn('const why = presetSeatRefusal(m.v, n);', self.source)
        self.assertIn('why || (solo && m.soloDesc) || m.desc', self.source)

    def test_the_offered_modes_read_for_one_agent(self):
        for label in ('soloName: "Work in Turns"',
                      'soloName: "Plan and Build"'):
            self.assertIn(label, self.source)

    def test_the_recipe_sentence_has_a_solo_voice(self):
        self.assertIn("SOLO_POLICY_REASON", self.source)
        self.assertIn("if (soloStage()) reason = soloPolicyReason(workflow);",
                      self.source)
        # ...and the FLOOR still matters at one seat: a moderated solo room
        # spends a real side call before every turn.
        self.assertIn("one extra billed call per turn", self.source)

    def test_the_empty_state_headline_is_not_a_crowd_slogan_at_one_seat(self):
        self.assertIn("One agent. Your project.", self.source)
        self.assertIn("Different metals. One alloy.", self.source)

    def test_the_headline_claims_nothing_about_tools(self):
        """Desktop, browser and connectors reach claude seats only
        (relay.MCP_DELIVERING_PROVIDERS), and the headline is keyed on seat
        COUNT -- so any tool claim there would be false for a solo GPT,
        Gemini or Ox stage, and axis_unreachable_note would contradict it at
        start time."""
        self.assertEqual(relay.MCP_DELIVERING_PROVIDERS, ("claude",))
        self.assertNotIn("Every tool Alloy", self.source)

    def test_the_moderator_toggle_is_not_offered_but_never_hidden_when_on(self):
        """Hiding a switch that is currently ON left moderation running with
        no way to turn it off and its provider picker still on screen."""
        self.assertIn('|| (soloStage() && $("floorSel").value !== "moderated")',
                      self.source)

    def test_the_rounds_label_stops_talking_about_rounds(self):
        self.assertIn("Turns — how many turns the agent gets", self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
