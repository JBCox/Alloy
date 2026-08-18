"""Workstreams — token-free. Run: python tests/test_workstreams.py"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay  # noqa: E402
import workstreams as ws  # noqa: E402
from test_loop import RecordingIO, build_state  # noqa: E402

PASS = FAIL = 0


def ok(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("  FAIL:", label)


def eq(got, want, label):
    ok(got == want, "%s (got %r, want %r)" % (label, got, want))


def T(tid, owner, brief="work", files=None, deps=None, status="pending"):
    t = ws.make_task(tid, owner, brief, files, deps)
    t["status"] = status
    return t


# ------------------------------------------------------ fan-out scoping

def test_fanout_default_is_broadcast():
    # nobody has an active task -> the old behaviour, exactly
    tasks = [T("a", "s1"), T("b", "s2")]
    ok(ws.shares_stream(tasks, "s1", "s2"), "idle seats still hear each other")
    ok(ws.shares_stream(tasks, "s2", "s1"), "and the other way")
    ok(not ws.shares_stream(tasks, "s1", "s1"), "a seat never hears itself")
    ok(ws.shares_stream([], "s1", "s2"), "no tasks at all = main conversation")


def test_fanout_isolates_active_seats():
    tasks = [T("a", "s1", status="active"), T("b", "s2", status="active"),
             T("c", "s3")]
    ok(not ws.shares_stream(tasks, "s2", "s1"),
       "a heads-down seat does not hear an unrelated seat's chatter")
    ok(not ws.shares_stream(tasks, "s1", "s2"), "isolation is symmetric")
    # strict: a worker broadcasts to NOBODY, not even the main conversation
    ok(not ws.shares_stream(tasks, "s1", "s3"),
       "an active worker's drafts never reach the main rail")
    ok(not ws.shares_stream(tasks, "s3", "s1"),
       "and main-rail chatter never reaches an active worker")
    tasks[0]["status"] = "done"
    ok(ws.shares_stream(tasks, "s1", "s3"),
       "settling puts a seat back in the main conversation")
    eq(ws.active_owners(tasks), {"s2"}, "active owners reported")


def test_serialize_conflicts():
    tasks = [T("a", "s1", files=["app.py"]), T("b", "s2", files=["app.py"])]
    added = ws.serialize_conflicts(tasks)
    eq(len(added), 1, "one edge injected for the collision")
    eq(tasks[1]["deps"], ["a"], "later task now waits on the earlier one")
    eq(ws.file_conflicts(tasks), [("a", "b", "app.py", "app.py")],
       "the overlap still exists — it is now ordered, not removed")
    eq([t["id"] for t in ws.next_assignments(tasks)], ["a"],
       "so only one of them can start")
    eq(ws.serialize_conflicts(tasks), [], "re-running injects nothing twice")

    tasks = [T("a", "s1", files=["ui"]), T("b", "s2", files=["app.py"])]
    eq(ws.serialize_conflicts(tasks), [], "disjoint plans are left alone")


def test_task_directive_parser():
    body, tasks, unknown = relay.parse_task_directives(
        "Plan. [[TASK: api | owner=0 | files=app.py,outcome.py | build bridge]] "
        "[[TASK: ui | owner=seat-b | files=ui/index.html | deps=api | wire UI]]",
        slot_ids=[0, "seat-b"])
    eq(body, "Plan.", "TASK blocks peel from the supervisor reply")
    eq([t["id"] for t in tasks], ["api", "ui"],
       "stacked tasks preserve written order")
    eq(tasks[0]["owner"], 0, "numeric slot ids stay numeric")
    eq(tasks[0]["files"], ["app.py", "outcome.py"], "literal files parsed")
    eq(tasks[1]["deps"], ["api"], "dependencies parsed")
    eq(unknown, [], "valid task plan has no unknown directives")
    labelled = relay.parse_task("docs | owner=0 | brief=write the docs",
                                slot_ids=[0])
    eq(labelled["brief"], "write the docs",
       "real-planner brief= prefix is normalized")

    bad = [
        "x | brief",
        "x | owner=9 | brief",
        "x | owner=0 | files=../escape.py | brief",
        "x | owner=0 | files=*.py | brief",
        "x | owner=0 | deps=x | brief",
        "x | owner=0 | surprise=yes | brief",
    ]
    for arg in bad:
        try:
            relay.parse_task(arg, slot_ids=[0, "seat-b"])
            ok(False, "invalid TASK rejected: " + arg)
        except ValueError:
            ok(True, "invalid TASK rejected: " + arg)


# ---------------------------------------------------------- scheduling

def test_unblocked():
    tasks = [T("a", "s1", status="done"), T("b", "s2", deps=["a"]),
             T("c", "s3", deps=["b"])]
    eq([t["id"] for t in ws.unblocked(tasks)], ["b"], "only deps-satisfied run")
    tasks[1]["status"] = "done"
    eq([t["id"] for t in ws.unblocked(tasks)], ["c"], "chain advances")


def test_unknown_dep_stalls_visibly():
    tasks = [T("b", "s2", deps=["nope"])]
    eq(ws.unblocked(tasks), [], "a typo'd dependency stalls, never auto-runs")


def test_one_task_per_seat():
    tasks = [T("a", "s1", status="active"), T("b", "s1"), T("c", "s2")]
    eq([t["id"] for t in ws.next_assignments(tasks)], ["c"],
       "a busy seat is not given a second task")
    eq(ws.owners_busy(tasks), {"s1"}, "busy owners reported")
    tasks[0]["status"] = "done"
    eq([t["id"] for t in ws.next_assignments(tasks)], ["b", "c"],
       "freed seat picks up its next task, declaration order")


# ----------------------------------------------------- file ownership

def test_file_conflicts():
    tasks = [T("a", "s1", files=["ui/index.html"]),
             T("b", "s2", files=["app.py"])]
    eq(ws.file_conflicts(tasks), [], "disjoint files are fine")

    tasks = [T("a", "s1", files=["app.py"]), T("b", "s2", files=["app.py"])]
    eq(len(ws.file_conflicts(tasks)), 1, "same file is a conflict")

    tasks = [T("a", "s1", files=["ui"]), T("b", "s2", files=["ui/index.html"])]
    eq(len(ws.file_conflicts(tasks)), 1, "a file inside a claimed folder too")

    tasks = [T("a", "s1", files=["./App.py"]), T("b", "s2", files=["app.py"])]
    eq(len(ws.file_conflicts(tasks)), 1, "paths normalise before comparing")

    tasks = [T("a", "s1", files=["app.py"], status="done"),
             T("b", "s2", files=["app.py"])]
    eq(ws.file_conflicts(tasks), [], "a finished task no longer holds its file")


# ------------------------------------------------------- verification

def test_verify_deliverable():
    d = tempfile.mkdtemp(prefix="alloy-ws-")
    start = time.time()
    time.sleep(0.01)
    with open(os.path.join(d, "made.py"), "w") as f:
        f.write("x")

    t = T("a", "s1", files=["made.py"])
    t["started_ts"] = start
    res = ws.verify_deliverable(d, t)
    ok(res["ok"], "a file that appeared after the task started counts")
    eq(res["delivered"], ["made.py"], "delivered listed")

    t2 = T("b", "s2", files=["never.py"])
    t2["started_ts"] = start
    res = ws.verify_deliverable(d, t2)
    ok(not res["ok"], "a claimed file that does not exist fails")
    eq(res["missing"], ["never.py"], "missing listed by name")

    old = os.path.join(d, "ancient.py")
    with open(old, "w") as f:
        f.write("x")
    os.utime(old, (1000, 1000))
    t3 = T("c", "s3", files=["ancient.py"])
    t3["started_ts"] = start
    res = ws.verify_deliverable(d, t3)
    ok(not res["ok"], "'I updated it' with an untouched file fails")
    eq(res["stale"], ["ancient.py"], "stale listed separately from missing")

    t4 = T("d", "s4")           # research task, claims nothing
    res = ws.verify_deliverable(d, t4)
    ok(res["unverifiable"], "no claimed files = unverifiable, not verified")
    ok(not res["ok"], "and unverifiable is never reported as ok")

    t5 = T("e", "s5", files=["made.py"])
    t5["started_ts"] = start
    res = ws.verify_deliverable(os.path.join(d, "gone"), t5)
    eq(res["missing"], ["made.py"], "missing workspace degrades quietly")


def test_settle_and_summarize():
    d = tempfile.mkdtemp(prefix="alloy-ws-")
    start = time.time()
    time.sleep(0.01)
    with open(os.path.join(d, "real.py"), "w") as f:
        f.write("x")

    t = T("a", "s1", brief="write real.py", files=["real.py"])
    t["started_ts"] = start
    ws.settle(t, d)
    eq(t["status"], "done", "verified work settles as done")
    ok("verified on disk" in ws.summarize(t), "summary names the evidence")

    t = T("b", "s2", brief="write ghost.py", files=["ghost.py"])
    t["started_ts"] = start
    ws.settle(t, d)
    eq(t["status"], "failed",
       "a confident report cannot promote itself past a missing file")
    ok("NOT delivered" in ws.summarize(t), "summary says so plainly")
    ok("never created" in ws.summarize(t), "and says what was missing")

    t = T("c", "s3", brief="research the API")
    ws.settle(t, d)
    eq(t["status"], "done", "a no-files task settles done")
    ok("nothing was verified" in ws.summarize(t),
       "but the summary admits nothing was checked")


# ------------------------------------------- end-to-end through the loop

def test_loop_isolation_and_settlement():
    """Drive the REAL run_rounds with two seats and two independent tasks."""
    import tempfile as _tf
    tmp = _tf.mkdtemp(prefix="alloy-wsloop-")
    state = build_state(tmp, [["A done"], ["B done [[WRAP]]"]], turns=1,
                        labels=["Ana", "Ben"])
    wsdir = state["workspace"]
    state["workstreams"] = [
        ws.make_task("t1", 0, "write a.txt", files=["a.txt"]),
        ws.make_task("t2", 1, "write b.txt", files=["b.txt"]),
    ]
    io = RecordingIO()
    relay.assign_workstreams(state, io)
    eq([t["status"] for t in state["workstreams"]], ["active", "active"],
       "both independent tasks start at once — no one waits")
    ok("Your task [t1]" in state["pending"][0][0], "brief reached its owner")
    ok("t2" not in state["pending"][0][0], "and only its own task")

    # each seat "does the work" on disk, then replies
    for fn in ("a.txt", "b.txt"):
        with open(os.path.join(wsdir, fn), "w") as f:
            f.write("x")
    for t in state["workstreams"]:
        t["started_ts"] = 0

    ended = relay.run_rounds(state, io)

    eq([t["status"] for t in state["workstreams"]], ["done", "done"],
       "verified deliverables settle as done")
    # what each seat was actually HANDED is the ground truth here
    handed = " ".join(state["agents"][0].prompts)
    ok("B done" not in handed,
       "worker never received the other worker's raw reply")
    msgs = " ".join((p.get("text") or "") for e, p in io.events
                    if e == "message")
    ok("[t1]" in msgs and "[t2]" in msgs and "verified on disk" in msgs,
       "but both settlement summaries did cross the boundary")
    ok(any(e == "workstreams" for e, _ in io.events),
       "the UI gets a workstreams event")
    eq(ended, "cap", "a worker's WRAP closes its task, not the conversation")
    eq(state["rnd"], 1, "worker WRAP does not trigger a global closing round")


def test_loop_failed_task_is_not_forged():
    import tempfile as _tf
    tmp = _tf.mkdtemp(prefix="alloy-wsfail-")
    state = build_state(tmp, [["I created it, all done"]], turns=2,
                        labels=["Ana"])
    state["workstreams"] = [ws.make_task("t1", 0, "write ghost.txt",
                                         files=["ghost.txt"])]
    io = RecordingIO()
    relay.assign_workstreams(state, io)
    relay.run_rounds(state, io)
    eq(state["workstreams"][0]["status"], "failed",
       "a confident claim with no file on disk settles as failed")


def test_no_workstreams_is_unchanged_broadcast():
    import tempfile as _tf
    tmp = _tf.mkdtemp(prefix="alloy-wsoff-")
    state = build_state(tmp, [["hi"], ["yo"]], turns=1, labels=["Ana", "Ben"])
    relay.run_rounds(state, RecordingIO())
    ok(any("Ana said:" in p for p in state["pending"][1])
       or any("Ana said:" in p for p in state["agents"][1].prompts),
       "with no tasks, seats hear each other exactly as before")


# ------------------------------------------------------- capability gate

def test_capability_gate():
    # a file-writing task planned onto a seat that cannot write files
    tasks = [T("t1", "gem", files=["a.py"]), T("t2", "cl", files=["b.py"])]
    actions = ws.capability_gate(tasks, {"cl", "gp"})
    eq(actions, [("reassigned", "t1", "gem", "gp")],
       "misrouted task moves to the least-loaded capable seat, and says so")
    eq(tasks[0]["owner"], "gp", "owner actually changed")

    # a research task with no file claims may live anywhere
    tasks = [T("t1", "gem", files=[])]
    eq(ws.capability_gate(tasks, {"cl"}), [],
       "a no-files task is never reassigned")
    eq(tasks[0]["owner"], "gem", "research stays with the researcher")

    # nobody can write -> rejected, not silently left to fail on disk
    tasks = [T("t1", "gem", files=["a.py"])]
    actions = ws.capability_gate(tasks, set())
    eq(actions, [("rejected", "t1", "gem", None)], "rejected when nobody can")
    eq(tasks[0]["status"], "failed", "and marked failed immediately")

    # an already-capable owner is left completely alone
    tasks = [T("t1", "cl", files=["a.py"])]
    eq(ws.capability_gate(tasks, {"cl", "gp"}), [], "capable owner untouched")

    # an active task is not re-owned mid-flight
    tasks = [T("t1", "gem", files=["a.py"], status="active")]
    eq(ws.capability_gate(tasks, {"cl"}), [],
       "a task already running is never reassigned underneath its seat")


def test_engine_capability_gate():
    import tempfile as _tf
    tmp = _tf.mkdtemp(prefix="alloy-wsgate-")
    state = build_state(tmp, [["ok"], ["ok"]], turns=1, labels=["Cee", "Gem"])
    state["providers"] = ["claude", "gemini"]
    eq(relay.workstream_writers(state), {0}, "only the claude seat can write")
    state["workstreams"] = [ws.make_task("t1", 1, "write x.txt",
                                         files=["x.txt"])]
    io = RecordingIO()
    relay.assign_workstreams(state, io)
    eq(state["workstreams"][0]["owner"], 0,
       "engine reroutes the file task to the seat that can do it")
    msgs = " ".join((p.get("text") or "") for e, p in io.events
                    if e == "message")
    ok("reassigned" in msgs and "Cee" in msgs,
       "and the reroute is announced by seat name, never silent")


class _StubPlanner:
    """Stands in for the stateless planner adapter — never a real CLI call."""

    def __init__(self, reply=None, boom=None):
        self.reply, self.boom = reply, boom

    def turn(self, message, on_activity=None):
        if self.boom:
            raise self.boom
        return self.reply


def _sup_state(tmp, providers=("claude", "gemini")):
    state = build_state(tmp, [["ok"], ["ok"]], turns=1, labels=["Cee", "Gem"])
    state["providers"] = list(providers)
    state["mode"] = "supervisor"
    state["topic"] = "build a thing"
    return state


def test_supervisor_roster_block():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-sup-"))
    block = relay.supervisor_roster_block(state)
    ok("Cee" in block and "Gem" in block, "every seat is listed")
    ok("can write files: yes" in block, "the writer seat is marked capable")
    ok("can write files: no" in block, "and the non-writer is marked honestly")
    ok("seat id 0" in block, "slot ids are what the planner must name")


def test_plan_workstreams_dispatches(monkey=None):
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-sup-"))
    plan = ("Two independent pieces.\n"
            "[[TASK: eng | owner=0 | files=engine.py | write the engine]]\n"
            "[[TASK: doc | owner=1 | research the API surface]]")
    real = relay.build_supervisor
    relay.build_supervisor = lambda st: _StubPlanner(plan)
    try:
        io = RecordingIO()
        tasks = relay.plan_workstreams(state, io)
    finally:
        relay.build_supervisor = real
    eq([t["id"] for t in tasks], ["eng", "doc"], "both tasks parsed")
    eq([t["status"] for t in state["workstreams"]], ["active", "active"],
       "and both started immediately — neither waits on the other")
    ok("Your task [eng]" in state["pending"][0][0], "brief reached seat 0")
    ok("Your task [doc]" in state["pending"][1][0], "and seat 1 got its own")
    msgs = " ".join((p.get("text") or "") for e, p in io.events
                    if e == "message")
    ok("Supervisor's plan" in msgs, "the plan is shown, not hidden")


def test_plan_workstreams_degrades_safely():
    import tempfile as _tf
    real = relay.build_supervisor
    for label, stub in (("planner crashed", _StubPlanner(boom=RuntimeError("x"))),
                        ("planner returned prose", _StubPlanner("I'd start with the UI.")),
                        ("planner returned nothing", _StubPlanner(""))):
        state = _sup_state(_tf.mkdtemp(prefix="alloy-sup-"))
        relay.build_supervisor = lambda st, s=stub: s
        try:
            io = RecordingIO()
            eq(relay.plan_workstreams(state, io), [], label + " -> no tasks")
        finally:
            relay.build_supervisor = real
        ok(not state.get("workstreams"),
           label + " -> no plan is invented")
        ok(any(e == "status" for e, _ in io.events),
           label + " -> and it says so out loud")


def test_failed_task_gets_one_bounded_replan():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-replan-"))
    failed = T("eng", 0, "write the first path", files=["wrong.py"],
               deps=["research"], status="failed")
    failed["verified"] = {"ok": False, "missing": ["wrong.py"],
                          "stale": [], "delivered": [], "extra": [],
                          "unverifiable": False}
    state["workstreams"] = [T("research", 1, status="done"), failed]
    reply = ("Use the existing module path.\n"
             "[[TASK: eng | owner=0 | files=engine.py | implement engine]]")
    real = relay.build_supervisor
    relay.build_supervisor = lambda st: _StubPlanner(reply)
    try:
        io = RecordingIO()
        repaired = relay.replan_failed_workstreams(state, io)
    finally:
        relay.build_supervisor = real
    eq([t["id"] for t in repaired], ["eng"], "failed id is repaired")
    task = state["workstreams"][1]
    eq(task["id"], "eng", "replacement reuses the original task id")
    eq(task["deps"], ["research"], "original DAG dependencies are retained")
    eq(task["files"], ["engine.py"], "supervisor may correct file claims")
    eq(task["replans"], 1, "the one repair attempt is persisted on the task")
    eq(task["status"], "active", "repaired task is dispatched immediately")
    ok(any("replanned [eng]" in (p.get("text") or "")
           for e, p in io.events if e == "message"),
       "the repair is announced rather than silently replacing history")


def test_replan_failure_is_final_and_visible():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-replanbad-"))
    failed = T("eng", 0, files=["missing.py"], status="failed")
    failed["verified"] = {"missing": ["missing.py"]}
    state["workstreams"] = [failed]
    real = relay.build_supervisor
    relay.build_supervisor = lambda st: _StubPlanner("Try again without a task.")
    try:
        io = RecordingIO()
        eq(relay.replan_failed_workstreams(state, io), [],
           "prose-only repair invents no replacement")
        eq(failed["replans"], 1, "a malformed side call still spends the attempt")
        eq(relay.replan_failed_workstreams(state, io), [],
           "the same failure is never planned in an open-ended loop")
    finally:
        relay.build_supervisor = real
    eq(failed["status"], "failed", "objective failure remains on the record")
    ok(any(e == "status" and "no valid replacement" in p.get("text", "")
           for e, p in io.events), "planner failure is visible")


def test_parallel_barrier_triggers_replan():
    import tempfile as _tf
    tmp = _tf.mkdtemp(prefix="alloy-replanloop-")
    state = build_state(tmp, [["I finished, but wrote nothing"]], turns=1,
                        labels=["Cee"])
    state["providers"] = ["claude"]
    state["mode"] = "supervisor"
    state["topic"] = "create the deliverable"
    state["workstreams"] = [T("eng", 0, "write first.py",
                                  files=["first.py"])]
    io = RecordingIO()
    relay.assign_workstreams(state, io)
    real = relay.build_supervisor
    relay.build_supervisor = lambda st: _StubPlanner(
        "[[TASK: eng | owner=0 | files=second.py | try the correct path]]")
    try:
        relay.run_rounds(state, io)
    finally:
        relay.build_supervisor = real
    task = state["workstreams"][0]
    eq(task["files"], ["second.py"],
       "real parallel barrier replaced the failed file claim")
    eq(task["replans"], 1, "barrier repair remains bounded")
    eq(task["status"], "active",
       "replacement is queued for the next round or continuation")


def test_playbook_feeds_the_planner():
    """The last hop: a derived rule must reach the planning prompt."""
    import json as _json
    import tempfile as _tf
    d = _tf.mkdtemp(prefix="alloy-pb-")

    eq(relay.playbook_block(d), "", "no playbook file at all costs nothing")
    with open(os.path.join(d, "playbook.json"), "w", encoding="utf-8") as f:
        _json.dump({"playbook_version": 1, "heuristics": []}, f)
    eq(relay.playbook_block(d), "", "an empty playbook costs nothing either")

    with open(os.path.join(d, "playbook.json"), "w", encoding="utf-8") as f:
        _json.dump({"playbook_version": 1, "heuristics": [
            {"heuristic_id": "h1", "directive": "Verify claimed files",
             "evidence_count": 3, "status": "active", "pinned": False},
            {"heuristic_id": "h2", "directive": "Dropped rule",
             "evidence_count": 9, "status": "dismissed", "pinned": False},
            {"heuristic_id": "h3", "directive": "Josh's own rule",
             "evidence_count": 1, "status": "active", "pinned": True},
        ]}, f)
    block = relay.playbook_block(d)
    ok("Verify claimed files" in block, "an active rule reaches the prompt")
    ok("Dropped rule" not in block, "a dismissed rule stays dismissed")
    ok("seen in 3 sessions" in block, "provenance rides along with the rule")
    ok(block.index("Josh's own rule") < block.index("Verify claimed files"),
       "a pinned rule outranks a merely frequent one")
    ok("guidance, not orders" in block,
       "rules are advisory — a stale heuristic must not override the goal")

    # and it actually lands in the composed planning prompt
    prompt = relay.SUPERVISOR_PROMPT.format(roster="r", playbook=block,
                                            goal="build a thing")
    ok("Verify claimed files" in prompt and "build a thing" in prompt,
       "the planner is given the learned rules alongside the goal")


def test_supervisor_mode_registered():
    ok("supervisor" in relay.MODES, "supervisor is a mode")
    ok("supervisor" in relay.IMPLEMENTED_MODES, "and an implemented one")
    ok("supervisor" not in ("moderator",), "never an alias of moderator")


def main():
    for fn in (test_fanout_default_is_broadcast, test_fanout_isolates_active_seats,
               test_serialize_conflicts, test_task_directive_parser,
               test_unblocked,
               test_unknown_dep_stalls_visibly,
               test_one_task_per_seat, test_file_conflicts,
               test_verify_deliverable, test_settle_and_summarize,
               test_loop_isolation_and_settlement,
               test_loop_failed_task_is_not_forged,
               test_no_workstreams_is_unchanged_broadcast,
               test_capability_gate, test_engine_capability_gate,
               test_supervisor_roster_block, test_plan_workstreams_dispatches,
               test_plan_workstreams_degrades_safely,
               test_failed_task_gets_one_bounded_replan,
               test_replan_failure_is_final_and_visible,
               test_parallel_barrier_triggers_replan,
               test_playbook_feeds_the_planner,
               test_supervisor_mode_registered):
        print("--", fn.__name__)
        fn()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
