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
import time
import unittest
from unittest import mock

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
        # the skip lost nothing and forged nothing — and because an empty
        # reply BENCHES the seat for this run (the engine's existing skip
        # semantics), the healthy peer's reply is now REFUSED to it with a
        # visible envelope receipt (comms-design.md section 3) instead of
        # silently piling into a queue nobody will drain this run:
        self.assertEqual(saved_meta(state)["seats"][0]["pending"], [])
        self.assertEqual(rows[-1]["rejected_to"],
                         [{"seat": 0,
                           "reason": "benched after repeated failures"}])
        self.assertEqual(rows[-1]["delivered_to"], [])

    def test_parked_seat_is_not_retried_on_later_laps(self):
        boom = RuntimeError("provider down")
        state = build_state(
            self.tmp,
            [["a1", "a2", "a3", "a4"],
             [boom, boom] + [RuntimeError("never again")] * 6],
            turns=4)
        io = RecordingIO()
        outcome = run_rounds(state, io)
        self.assertEqual(outcome, "cap")
        # B got exactly its double-failure attempts — the cursor skipped it
        # on every later lap instead of hammering the dead provider.
        self.assertEqual(len(state["agents"][1].prompts), 2)
        self.assertIn("failed twice; skipping",
                      "\n".join(r["text"] for r in jsonl_rows(state)))
        # A kept working the remaining laps alone
        rows = [r for r in jsonl_rows(state) if r["speaker"] == 0]
        self.assertEqual([r["text"] for r in rows], ["a1", "a2", "a3", "a4"])

    def test_all_seats_parked_ends_the_run_visibly(self):
        boom = RuntimeError("provider down")
        state = build_state(
            self.tmp,
            [[boom, boom], [RuntimeError("x"), RuntimeError("y")]],
            turns=3)
        outcome = run_rounds(state, RecordingIO())
        # Nothing can speak: a visible pause, never a forged turn or a spin.
        self.assertEqual(outcome, "starved")
        sys_rows = [r["text"] for r in jsonl_rows(state)
                    if r["speaker"] == "system"]
        self.assertTrue(any("failed twice" in t for t in sys_rows), sys_rows)
        self.assertTrue(
            any("Every seat has failed twice" in t for t in sys_rows),
            sys_rows)
        self.assertEqual(state["completion"]["termination_reason"], "starved")
        self.assertFalse([r for r in jsonl_rows(state)
                          if r.get("origin") == "seat"])

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


class ProfileProbe(FakeAgent):
    """Stands in for a real adapter in the side-call builders: records the
    kwargs it was built with, runs nothing."""

    def __init__(self, workspace, **kw):
        super().__init__(workspace, [], **kw)
        self.kw = kw


def ws_task(tid, owner, status="pending", brief="Do the thing", files=None,
            deps=None, started_ts=None):
    t = {"id": tid, "owner": owner, "status": status, "brief": brief,
         "files": list(files or [])}
    if deps is not None:
        t["deps"] = list(deps)
    if started_ts is not None:
        t["started_ts"] = started_ts
    return t


class StepModelProfileTests(unittest.TestCase):
    """Per-step model profiles: which model runs the relay's OWN side calls.

    A profile for a step wins before helper_spec's chain (moderator -> first
    seat -> default); an unconfigured step keeps the old chain byte-for-byte;
    garbage profiles fall back rather than looking configured."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_normalization_keeps_only_real_steps_and_providers(self):
        good = relay.normalize_step_models({
            "planner": "ox",
            "moderator": "claude:claude-haiku-4-5:low",
            "title": "gpt",
            # all of these DROP, never sanitize:
            "typo": "ox",                    # unknown step name
            "planner2": "ox",                # near-miss key
            "checker": "ox",                 # not one of STEP_MODEL_KEYS
            "title": "",                     # empty value
        })
        self.assertEqual(sorted(good), ["moderator", "planner"])
        self.assertEqual(good["planner"], {"provider": "ox"})
        self.assertEqual(good["moderator"],
                         {"provider": "claude",
                          "model": "claude-haiku-4-5", "effort": "low"})
        # unknown provider: dropped — a profile must never route a side call
        # to a CLI that is not installed
        self.assertNotIn("planner",
                         relay.normalize_step_models({"planner": "nope"}))
        # a label segment makes it a SEAT spec, not a step profile
        self.assertEqual(relay.normalize_step_models(
            {"title": "ox=some-label"}), {})
        # garbage shapes fall back entirely
        self.assertEqual(relay.normalize_step_models(None), {})
        self.assertEqual(relay.normalize_step_models("ox"), {})
        self.assertEqual(relay.normalize_step_models({"title": 7}), {})

    def test_profile_wins_and_unconfigured_chain_is_byte_identical(self):
        seats = ["claude", "gpt"]
        mod = {"provider": "ox", "model": "opencode/hy3-free"}
        # no profile at all -> exactly the historical answers
        self.assertEqual(relay.helper_spec(seats, mod),
                         {"provider": "ox", "model": "opencode/hy3-free"})
        self.assertEqual(relay.helper_spec(["claude", "gpt"]),
                         {"provider": "claude"})
        # a title profile overrides ONLY the title step
        spec = relay.helper_spec(seats, mod, step="title",
                                 step_models={"title": "ox"})
        self.assertEqual(spec, {"provider": "ox"})
        # ...and the same call without that step still resolves through the
        # chain (helper_spec takes the step explicitly; brief synthesis does)
        self.assertEqual(relay.helper_spec(["claude", "gpt"], None,
                                           step=None,
                                           step_models={"title": "ox"}),
                         {"provider": "claude"})
        # an unusable profile map falls back to the chain rather than lying
        self.assertEqual(relay.helper_spec(["claude"], None, step="title",
                                           step_models={"title": "nope"}),
                         {"provider": "claude"})

    def _probe_state(self, **extra):
        state = {"workspace": self.tmp, "providers": ["claude"],
                 "supervisor": None, "moderator": None, "step_models": None}
        state.update(extra)
        return state

    def test_build_supervisor_honors_the_planner_profile(self):
        with mock.patch.dict(relay.AGENT_TYPES, {"ox": ProfileProbe}):
            agent = relay.build_supervisor(
                self._probe_state(step_models={"planner": "ox"}))
        self.assertIsInstance(agent, ProfileProbe)
        self.assertEqual(agent.kw.get("model"), None)
        # an explicit supervisor spec still applies when no profile exists
        with mock.patch.dict(relay.AGENT_TYPES, {"ox": ProfileProbe}):
            agent = relay.build_supervisor(self._probe_state(
                supervisor={"provider": "ox", "effort": "low"}))
        self.assertIsInstance(agent, ProfileProbe)
        self.assertEqual(agent.kw.get("effort"), "low")
        # and the PROFILE beats the supervisor spec when both exist — it is
        # the later, more specific instruction about internal side work
        with mock.patch.dict(relay.AGENT_TYPES, {"gpt": ProfileProbe}):
            agent = relay.build_supervisor(self._probe_state(
                supervisor={"provider": "ox"},
                step_models={"planner": "gpt:gpt-5.6-sol:low"}))
        self.assertIsInstance(agent, ProfileProbe)
        self.assertEqual(agent.kw.get("model"), "gpt-5.6-sol")

    def test_build_moderator_honors_the_moderator_profile(self):
        with mock.patch.dict(relay.AGENT_TYPES, {"ox": ProfileProbe}):
            agent = relay.build_moderator(
                self._probe_state(moderator={"provider": "claude"},
                                  step_models={"moderator": "ox"}))
        self.assertIsInstance(agent, ProfileProbe)
        # no profile -> Josh's moderator picker stands
        with mock.patch.dict(relay.AGENT_TYPES, {"ox": ProfileProbe}):
            agent = relay.build_moderator(self._probe_state(
                moderator={"provider": "ox", "model": "opencode/hy3-free"}))
        self.assertIsInstance(agent, ProfileProbe)
        self.assertEqual(agent.kw.get("model"), "opencode/hy3-free")

    def test_build_title_agent_honors_the_title_profile(self):
        state = self._probe_state()
        state["step_models"] = {"title": "ox:muse-spark"}
        with mock.patch.dict(relay.AGENT_TYPES, {"ox": ProfileProbe}):
            agent = relay.build_title_agent(state)
        self.assertIsInstance(agent, ProfileProbe)
        self.assertEqual(agent.kw.get("model"), "muse-spark")

    def test_profiles_and_note_persist_additively(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        state["step_models"] = {"planner": "ox", "junk": "nope"}
        state["handoff_note"] = "Always include a test list."
        state["store"].save(state)
        meta = saved_meta(state)
        # saved NORMALIZED: junk gone, valid entry kept
        self.assertEqual(meta["step_models"], {"planner": {"provider": "ox"}})
        self.assertEqual(meta["handoff_note"],
                         "Always include a test list.")
        # rehydrate restores them so a resumed chat keeps its recipe
        rstate = relay.rehydrate(meta)
        self.assertEqual(rstate["step_models"],
                         {"planner": {"provider": "ox"}})
        self.assertEqual(rstate["handoff_note"],
                         "Always include a test list.")


class HandoffNoteTests(unittest.TestCase):
    """The standing handoff note rides every worker brief. assign_workstreams
    is the ONE dispatch point (initial AND post-settlement), so testing it
    covers both; the settle path below proves the next worker after a
    settlement gets it too."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_note_reaches_worker_briefs(self):
        state = build_state(self.tmp, [["w1"], ["w2"]], turns=1)
        state["workstreams"] = [
            ws_task("t1", 0),
            ws_task("t2", 1, deps=["t1"]),
        ]
        state["handoff_note"] = "Every plan must include a test list."
        relay.assign_workstreams(state, RecordingIO())
        brief = state["pending"][0][0]
        self.assertIn("Your task [t1]: Do the thing", brief)
        self.assertIn("Standing handoff instructions for every task in "
                      "this room (from Josh): Every plan must include a "
                      "test list.", brief)

    def test_no_note_leaves_briefs_unchanged(self):
        state = build_state(self.tmp, [["w1"], ["w2"]], turns=1)
        state["workstreams"] = [ws_task("t1", 0)]
        relay.assign_workstreams(state, RecordingIO())
        self.assertNotIn("Standing handoff instructions",
                         state["pending"][0][0])

    def test_note_is_capped_plain_text(self):
        self.assertEqual(len(relay.normalize_handoff_note("x" * 10000)),
                         relay.HANDOFF_NOTE_MAX)
        self.assertEqual(relay.normalize_handoff_note("  hi  "), "hi")
        self.assertEqual(relay.normalize_handoff_note(123), "")
        self.assertEqual(relay.normalize_handoff_note(None), "")

    def test_next_worker_after_settlement_gets_the_note(self):
        workspace = os.path.join(self.tmp, "session", "workspace")
        started = time.time() - 5
        tasks = [
            ws_task("t1", 0, status="active", files=["out.txt"],
                    started_ts=started),
            ws_task("t2", 1, deps=["t1"]),
        ]
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        state["workstreams"] = tasks
        state["handoff_note"] = "Commit only green work."
        with open(os.path.join(workspace, "out.txt"), "w") as f:
            f.write("delivered\n")          # mtime AFTER started_ts: verifies
        io = RecordingIO()
        relay.settle_workstream(state, 0, io, reply="done")
        self.assertEqual(tasks[0]["status"], "done")
        # settlement unblocked t2 and dispatched it to seat 1 WITH the note
        self.assertEqual(tasks[1]["status"], "active")
        brief = state["pending"][1][-1]
        self.assertIn("Your task [t2]", brief)
        self.assertIn("Standing handoff instructions for every task in "
                      "this room (from Josh): Commit only green work.", brief)


class DeliveryGateTests(unittest.TestCase):
    """The engine half of comms-design.md section 3: one deliverability
    answer (delivery_gate) unifying the park/runtime and mode/stream gates,
    with refusals stamped onto the sender's row envelope as
    rejected_to [{seat, reason}] — the exact payload ui/index.html's
    refusalPill renders. Ordinary chats stay byte-identical: no refusals,
    no new keys."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-gate-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- the gate itself --------------------------------------------------
    def test_gate_refuses_benched_targets_and_names_the_gate(self):
        state = build_state(self.tmp, [["a"], ["b"]])
        relay.mark_floor_unavailable(state, 1)
        reason = relay.delivery_gate(state, 0, 1)
        self.assertEqual(reason, "benched after repeated failures")
        # the healthy direction is unaffected
        self.assertIsNone(relay.delivery_gate(state, 1, 0))

    def test_gate_refuses_a_busy_worker_for_seats_but_not_for_josh(self):
        state = build_state(self.tmp, [["a"], ["b"], ["c"]])
        state["workstreams"] = [
            {"id": "t1", "owner": 1, "brief": "w", "files": [], "deps": [],
             "status": "active"}]
        self.assertEqual(
            relay.delivery_gate(state, 0, 1),
            "worker radio-silent until their task settles")
        self.assertIsNone(relay.delivery_gate(state, 0, 2))
        # the room's owner is not bound by task isolation...
        self.assertIsNone(relay.delivery_gate(state, None, 1, kind="human"))
        # ...but even Josh cannot deliver to a benched seat
        relay.mark_floor_unavailable(state, 2)
        self.assertEqual(relay.delivery_gate(state, None, 2, kind="human"),
                         "benched after repeated failures")

    def test_ordinary_chats_pass_every_gate(self):
        state = build_state(self.tmp, [["a"], ["b"]])
        self.assertIsNone(relay.delivery_gate(state, 0, 1))

    # ---- fan-out stamps refusals on the row --------------------------------
    def test_broadcast_to_a_benched_seat_is_stamped_not_silently_dropped(self):
        boom = RuntimeError("provider down")
        state = build_state(self.tmp, [["a1", "a2"],
                                       [boom, boom, "never"]],
                            turns=2)
        io = RecordingIO()
        run_rounds(state, io)
        rows = [r for r in jsonl_rows(state)
                if r.get("origin") == "seat" and r["speaker"] == 0]
        self.assertEqual([r["text"] for r in rows], ["a1", "a2"])
        # round 1: seat 1 was still healthy -> delivered normally
        self.assertEqual(rows[0]["delivered_to"], [1])
        self.assertNotIn("rejected_to", rows[0])
        # round 2: benched -> refused VISIBLY, queue never touched again
        self.assertEqual(rows[1]["delivered_to"], [])
        self.assertEqual(rows[1]["rejected_to"],
                         [{"seat": 1,
                           "reason": "benched after repeated failures"}])
        self.assertEqual(state["pending"][1], ["Fake 1 said:\na1"])

    def test_worker_radio_silence_is_stamped_with_its_reason(self):
        state = build_state(self.tmp, [["a1"], ["b1"], ["c1"]])
        state["workstreams"] = [
            {"id": "t1", "owner": 1, "brief": "w", "files": [], "deps": [],
             "status": "active", "started_ts": time.time()}]
        io = RecordingIO()
        run_rounds(state, io)
        row = next(r for r in jsonl_rows(state)
                   if r.get("origin") == "seat" and r["speaker"] == 0)
        self.assertEqual(row["rejected_to"],
                         [{"seat": 1,
                           "reason": "worker radio-silent until their "
                                     "task settles"}])
        self.assertEqual(row["delivered_to"], [2])

    def test_ordinary_rows_carry_no_refusal_keys(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        run_rounds(state, RecordingIO())
        for r in jsonl_rows(state):
            if r.get("origin") == "seat":
                self.assertNotIn("rejected_to", r)
                self.assertNotIn("narrowing_failed", r)

    # ---- [[TO]] narrowing -------------------------------------------------
    def test_a_misresolved_to_sets_narrowing_failed_on_the_row(self):
        state = build_state(self.tmp, [["hello\n\n[[TO: ghost]]"], ["b1"]],
                            turns=1)
        io = RecordingIO()
        run_rounds(state, io)
        row = next(r for r in jsonl_rows(state)
                   if r.get("origin") == "seat" and r["speaker"] == 0)
        self.assertTrue(row["narrowing_failed"])
        # it still broadcast: nobody's text disappeared — seat 2's prompt
        # carries Fake 1's reply (directives relay verbatim)
        self.assertIn("Fake 1 said:", state["agents"][1].prompts[0])
        self.assertIn("[[TO: ghost]]", state["agents"][1].prompts[0])

    def test_a_valid_to_narrows_without_any_flag(self):
        state = build_state(self.tmp, [["hi\n\n[[TO: Fake 2]]"], ["b1"]],
                            turns=1)
        run_rounds(state, RecordingIO())
        row = next(r for r in jsonl_rows(state)
                   if r.get("origin") == "seat" and r["speaker"] == 0)
        self.assertEqual(row["audience"], [1])
        self.assertNotIn("narrowing_failed", row)
        self.assertNotIn("rejected_to", row)

    # ---- Josh's addressed path ---------------------------------------------
    def test_josh_mention_to_a_benched_seat_is_refused_visibly(self):
        boom = RuntimeError("down")
        state = build_state(self.tmp, [[boom, boom], ["never"]], turns=1)
        io = RecordingIO()
        run_rounds(state, io)          # seat 0 ends up benched
        before = list(state["pending"][0])
        relay.enqueue_josh_message(state, io, "@Fake 1 are you there?")
        rows = [r for r in jsonl_rows(state) if r["speaker"] == "josh"]
        row = rows[-1]
        self.assertEqual(row["delivered_to"], [])
        self.assertEqual(row["rejected_to"],
                         [{"seat": 0,
                           "reason": "benched after repeated failures"}])
        # nothing queued into a queue nobody will drain this run
        self.assertEqual(state["pending"][0], before)
        notes = " ".join(r["text"] for r in jsonl_rows(state)
                         if r["speaker"] == "system")
        self.assertIn("NOT delivered to Fake 1", notes)
        self.assertIn("benched after repeated failures", notes)

    def test_josh_mention_to_a_healthy_seat_is_unchanged(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        relay.enqueue_josh_message(state, RecordingIO(), "@Fake 2 just for you")
        rows = [r for r in jsonl_rows(state) if r["speaker"] == "josh"]
        self.assertEqual(rows[-1]["delivered_to"], [1])
        self.assertNotIn("rejected_to", rows[-1])
        self.assertIn("Josh (human) says to you: just for you",
                      state["pending"][1][-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
