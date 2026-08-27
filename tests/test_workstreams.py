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
    traces = [p["entry"] for e, p in io.events if e == "supervisor"]
    eq([x["phase"] for x in traces],
       ["planning", "plan", "instruction", "instruction"],
       "the full public control loop streams in order")
    eq([x["type"] for x in traces],
       ["plan_started", "plan_created", "task_assigned", "task_assigned"],
       "consumers get stable typed events instead of parsing prose")
    ok("Your task [eng]" in traces[2]["detail"],
       "the exact instruction sent to a worker is visible")
    eq(state["supervisor_trace"], traces,
       "the visible control log is also persisted for reopening")


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
    phases = [p["entry"]["phase"] for e, p in io.events
              if e == "supervisor"]
    ok("replanning" in phases and "replanned" in phases,
       "the repair decision is visible in the Supervisor log")
    correction = next(p["entry"] for e, p in io.events
                      if e == "supervisor" and
                      p["entry"].get("type") == "course_correction")
    eq(correction["before"]["files"], ["wrong.py"],
       "steering retains the before-side file claim")
    eq(correction["after"]["files"], ["engine.py"],
       "and exposes the corrected after-side claim")


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
    voice = relay.SUPERVISOR_VOICE["team"]
    prompt = relay.SUPERVISOR_PROMPT.format(roster="r", playbook=block,
                                            intro=voice["plan_intro"],
                                            teamwork=voice["plan_teamwork"],
                                            goal="build a thing")
    ok("Verify claimed files" in prompt and "build a thing" in prompt,
       "the planner is given the learned rules alongside the goal")


def test_supervisor_mode_registered():
    ok("supervisor" in relay.MODES, "supervisor is a mode")
    ok("supervisor" in relay.IMPLEMENTED_MODES, "and an implemented one")
    ok("supervisor" not in ("moderator",), "never an alias of moderator")


# ------------------------------------------- the Supervisor as a manager
# A planner decomposes once. A manager keeps reading what actually came back
# and keeps deciding. These cover the second half.

def test_plan_drained():
    ok(not relay.plan_drained({"workstreams": []}),
       "no plan is not a drained plan — nothing to review")
    ok(not relay.plan_drained({"workstreams": [T("a", 0, status="active")]}),
       "an active task keeps the manager out of the way")
    ok(not relay.plan_drained({"workstreams": [T("a", 0, status="done"),
                                               T("b", 1, status="pending")]}),
       "queued work is still work")
    ok(not relay.plan_drained({"workstreams": [T("a", 0, status="blocked")]}),
       "blocked is waiting, not finished")
    ok(relay.plan_drained({"workstreams": [T("a", 0, status="done"),
                                           T("b", 1, status="failed")]}),
       "done + failed is settled — a failure is a result to react to")


def test_wave_report_separates_verified_from_claimed():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-wave-"))
    shipped = T("eng", 0, "write engine.py", files=["engine.py"],
                status="done")
    shipped["verified"] = {"ok": True, "delivered": ["engine.py"],
                           "missing": [], "stale": [], "extra": ["notes.md"],
                           "unverifiable": False}
    shipped["report"] = "Engine written and unit-tested."
    lied = T("doc", 1, "write doc.md", files=["doc.md"], status="failed")
    lied["verified"] = {"ok": False, "delivered": [], "missing": ["doc.md"],
                        "stale": [], "extra": [], "unverifiable": False}
    lied["report"] = "All done, doc.md is complete."
    state["workstreams"] = [shipped, lied]
    report = relay.wave_report(state)
    ok("on disk: engine.py" in report, "verified delivery is stated as fact")
    ok("never created: doc.md" in report,
       "and a missing deliverable is stated just as plainly")
    ok("also wrote: notes.md" in report, "unclaimed output is surfaced too")
    ok("worker reported: All done" in report,
       "the worker's own words are present")
    ok(report.index("verified:") < report.index("worker reported: All done"),
       "but the filesystem verdict is read BEFORE the claim, never after")
    ok("DONE" in report and "FAILED" in report, "status is explicit")


def test_supervisor_verdict_parser():
    body, tasks, done = relay.parse_supervisor_verdict(
        "Everything shipped.\n[[DONE: goal met]]")
    eq(done, "goal met", "a closing verdict is read")
    eq(tasks, [], "and carries no work with it")
    eq(body, "Everything shipped.", "the rationale survives as prose")
    _b, tasks, done = relay.parse_supervisor_verdict(
        "Round two.\n[[TASK: a | owner=0 | first]]\n"
        "[[TASK: b | owner=1 | second]]", slot_ids=[0, 1])
    eq(done, None, "no verdict means the job continues")
    eq([t["id"] for t in tasks], ["a", "b"], "written order is preserved")
    _b, tasks, done = relay.parse_supervisor_verdict(
        "Nothing to add.", slot_ids=[0])
    ok(done is None and tasks == [],
       "prose alone is neither a verdict nor a plan")
    ok("DONE" not in relay.KNOWN_DIRECTIVES,
       "DONE stays opted-in: an ordinary seat playing it must look unknown, "
       "not quietly acquire authority to close the conversation")


def test_review_issues_the_next_wave():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-wave2-"))
    state["supervisor_goal"] = "ship the tool"
    done = T("eng", 0, "write engine.py", files=["engine.py"], status="done")
    done["verified"] = {"ok": True, "delivered": ["engine.py"], "missing": [],
                        "stale": [], "extra": [], "unverifiable": False}
    state["workstreams"] = [done]
    real = relay.build_supervisor
    relay.build_supervisor = lambda st: _StubPlanner(
        "The engine landed; it still has no tests or docs.\n"
        "[[TASK: tests | owner=0 | files=test_engine.py | cover the engine]]\n"
        "[[TASK: readme | owner=1 | research how rivals document this]]")
    try:
        io = RecordingIO()
        eq(relay.supervise_next_wave(state, io), "assigned",
           "a drained plan does not end the job — it triggers the next call")
    finally:
        relay.build_supervisor = real
    eq([t["id"] for t in state["workstreams"]], ["eng", "tests", "readme"],
       "the new wave is appended; delivered work is not re-litigated")
    eq([t["status"] for t in state["workstreams"][1:]], ["active", "active"],
       "and both new tasks start at once")
    ok("Your task [tests]" in state["pending"][0][0],
       "the worker gets its brief through the ordinary queue")
    eq(state["supervisor_waves"], 1, "the wave is counted so it stays bounded")
    types = [p["entry"]["type"] for e, p in io.events if e == "supervisor"]
    ok("work_reviewed" in types,
       "the review itself is a visible control action, not a hidden call")
    ok(types.index("work_reviewed") < types.index("plan_created"),
       "and it is logged BEFORE the decision it produced")
    eq(types.count("task_assigned"), 2, "each assignment is its own event")
    review = next(p["entry"] for e, p in io.events
                  if e == "supervisor" and p["entry"]["type"] == "work_reviewed")
    ok("engine.py" in review["detail"],
       "the log shows what the manager was actually looking at")
    wave = next(p["entry"] for e, p in io.events
                if e == "supervisor" and p["entry"]["type"] == "plan_created")
    ok("no tests or docs" in wave["detail"],
       "and its stated reasoning for the new wave")
    msgs = " ".join((p.get("text") or "") for e, p in io.events
                    if e == "message")
    ok("Supervisor's next wave" in msgs,
       "the seats and Josh see the new plan in the transcript too")


def test_review_can_call_the_job_done():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-wavedone-"))
    state["supervisor_goal"] = "ship the tool"
    state["workstreams"] = [T("eng", 0, status="done")]
    real = relay.build_supervisor
    relay.build_supervisor = lambda st: _StubPlanner(
        "Engine, tests and docs are all on disk.\n[[DONE: shipped]]")
    try:
        io = RecordingIO()
        eq(relay.supervise_next_wave(state, io), "done",
           "the manager, not the round cap, ends a finished job")
    finally:
        relay.build_supervisor = real
    eq(len(state["workstreams"]), 1, "closing invents no extra work")
    entry = next(p["entry"] for e, p in io.events
                 if e == "supervisor" and p["entry"]["type"] == "goal_accepted")
    ok("all on disk" in entry["detail"], "the verdict's reasoning is kept")
    ok(any("Supervisor closed the job: shipped" in (p.get("text") or "")
           for e, p in io.events if e == "message"),
       "and the closing verdict is a real transcript row")


def test_review_will_not_reuse_a_task_id():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-waveclash-"))
    state["supervisor_goal"] = "ship the tool"
    old = T("eng", 0, "the original engine work", files=["engine.py"],
            status="done")
    state["workstreams"] = [old]
    real = relay.build_supervisor
    relay.build_supervisor = lambda st: _StubPlanner(
        "More engine work.\n"
        "[[TASK: eng | owner=0 | files=engine2.py | redo it]]\n"
        "[[TASK: docs | owner=1 | write the docs]]")
    try:
        io = RecordingIO()
        eq(relay.supervise_next_wave(state, io), "assigned",
           "the usable half of the wave still runs")
    finally:
        relay.build_supervisor = real
    eq([t["id"] for t in state["workstreams"]], ["eng", "docs"],
       "the duplicate is dropped rather than shadowing settled history")
    eq(state["workstreams"][0]["brief"], "the original engine work",
       "the completed task is left exactly as it was recorded")
    ok(any("reused task id" in (p.get("text") or "")
           for e, p in io.events if e == "message"),
       "and the drop is announced, never silent")


def test_review_degrades_safely():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-wavebad-"))
    state["supervisor_goal"] = "ship the tool"
    state["workstreams"] = [T("eng", 0, status="done")]
    real = relay.build_supervisor
    try:
        relay.build_supervisor = lambda st: _StubPlanner(boom=RuntimeError("no"))
        io = RecordingIO()
        eq(relay.supervise_next_wave(state, io), "idle",
           "a dead side call never kills the conversation")
        relay.build_supervisor = lambda st: _StubPlanner("I think we continue.")
        eq(relay.supervise_next_wave(state, io), "idle",
           "prose with no verdict and no tasks invents neither")
    finally:
        relay.build_supervisor = real
    eq(len(state["workstreams"]), 1, "and the plan is untouched either way")
    ok(all(p["entry"]["type"] == "supervisor_error"
           for e, p in io.events if e == "supervisor"
           and p["entry"]["phase"] == "error"),
       "each failure is logged as a failure")


def test_review_budget_is_bounded():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-wavecap-"))
    state["supervisor_goal"] = "ship the tool"
    state["workstreams"] = [T("eng", 0, status="done")]
    state["supervisor_waves"] = relay.SUPERVISOR_MAX_WAVES
    real = relay.build_supervisor
    relay.build_supervisor = lambda st: _StubPlanner(
        "[[TASK: more | owner=0 | keep going forever]]")
    try:
        io = RecordingIO()
        eq(relay.supervise_next_wave(state, io), "idle",
           "a manager that never says done cannot spend the account forever")
        eq(relay.supervise_next_wave(state, io), "idle", "and stays stopped")
    finally:
        relay.build_supervisor = real
    eq(len(state["workstreams"]), 1, "no work is issued past the budget")
    notes = [p.get("text") or "" for e, p in io.events if e == "message"]
    eq(len([n for n in notes if "review waves" in n]), 1,
       "the budget is announced once, not every barrier")


def test_review_only_runs_in_supervisor_mode():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-wavemode-"))
    state["mode"] = "parallel"
    state["supervisor_goal"] = "ship the tool"
    state["workstreams"] = [T("eng", 0, status="done")]
    real = relay.build_supervisor

    def _boom(st):
        raise AssertionError("no side call may happen outside supervisor mode")

    relay.build_supervisor = _boom
    try:
        eq(relay.supervise_next_wave(state, RecordingIO()), "idle",
           "an ordinary parallel chat is byte-for-byte unaffected")
    finally:
        relay.build_supervisor = real


def test_worker_report_survives_settlement():
    import tempfile as _tf
    tmp = _tf.mkdtemp(prefix="alloy-wavereport-")
    state = build_state(tmp, [["done"]], turns=1, labels=["Cee"])
    state["providers"] = ["claude"]
    state["mode"] = "supervisor"
    state["workstreams"] = [T("look", 0, "research the API", status="active")]
    relay.settle_workstream(state, 0, RecordingIO(),
                            reply="x" * (relay.WORKSTREAM_REPORT_MAX + 500))
    task = state["workstreams"][0]
    eq(task["status"], "done",
       "a task claiming no files settles as unverifiable-but-done")
    eq(len(task["report"]), relay.WORKSTREAM_REPORT_MAX,
       "the report is kept but bounded — the whole prompt is one argv element")


def test_parallel_barrier_runs_the_manager():
    """The whole point, driven through the real loop: work settles, the
    manager reviews it, issues a second wave, and closes the job itself
    instead of letting the round cap decide."""
    import tempfile as _tf
    tmp = _tf.mkdtemp(prefix="alloy-waveloop-")
    state = build_state(tmp, [["engine reviewed", "tests reviewed"]], turns=6,
                        labels=["Cee"])
    state["providers"] = ["claude"]
    state["mode"] = "supervisor"
    state["supervisor_goal"] = "ship the tool"
    state["workstreams"] = [T("eng", 0, "audit the engine")]
    io = RecordingIO()
    relay.assign_workstreams(state, io)
    seen = []

    class _Rolling:
        """Two review waves, then a verdict. Any third call is a bug."""

        def turn(self, message, on_activity=None):
            seen.append(message)
            if len(seen) == 1:
                return ("The audit landed; nothing covers it yet." + chr(10)
                        + "[[TASK: tests | owner=0 | write the coverage plan]]")
            return "Both pieces are in." + chr(10) + "[[DONE: shipped]]"

    real = relay.build_supervisor
    relay.build_supervisor = lambda st: _Rolling()
    try:
        eq(relay.run_rounds(state, io), "wrapped",
           "the run ends on the manager's verdict")
    finally:
        relay.build_supervisor = real
    eq([t["id"] for t in state["workstreams"]], ["eng", "tests"],
       "the manager kept the job moving after its first plan drained")
    eq([t["status"] for t in state["workstreams"]], ["done", "done"],
       "and every wave settled through the ordinary verification path")
    eq(len(seen), 2, "one side call per barrier — never one per round")
    ok("audit the engine" in seen[0] and "worker reported" in seen[0],
       "the manager reviews the real record, not a summary of a summary")
    ok("tests" in seen[1],
       "and the second review sees the work its own first wave produced")
    eq(state["supervisor_waves"], 2, "both waves are on the record")
    ok(any(e == "status" and "Supervisor called the job done" in p.get("text", "")
           for e, p in io.events),
       "the run ends because the manager said so, not because rounds ran out")
    ok(state["rnd"] < state["max"],
       "which means it stopped early instead of padding to the cap")
    types = [p["entry"]["type"] for e, p in io.events if e == "supervisor"]
    eq(types.count("work_reviewed"), 2, "each review is a visible event")
    eq(types[-1], "goal_accepted", "and the last thing it does is close out")


def test_trace_entries_carry_their_wave():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-waveidx-"))
    state["supervisor_goal"] = "ship the tool"
    state["workstreams"] = [T("eng", 0, status="done")]
    real = relay.build_supervisor
    relay.build_supervisor = lambda st: _StubPlanner(
        "Next.\n[[TASK: docs | owner=1 | write the docs]]")
    try:
        io = RecordingIO()
        relay.supervise_next_wave(state, io)
    finally:
        relay.build_supervisor = real
    entries = [p["entry"] for e, p in io.events if e == "supervisor"]
    by_type = {x["type"]: x["wave"] for x in entries}
    eq(by_type["work_reviewed"], 1,
       "the review closes the wave it is reviewing, not the next one")
    eq(by_type["plan_created"], 2, "the wave it dispatches is the next one")
    eq(by_type["task_assigned"], 2, "and its assignments belong there too")
    eq(state["supervisor_wave_index"], 2, "the index is persisted state")
    ok(all(isinstance(x.get("wave"), int) for x in entries),
       "every control action says which wave it belongs to — the UI must not "
       "have to infer it from a trace that is capped and can truncate")


def test_exhausted_waves_is_not_an_error():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-wavespent-"))
    state["supervisor_goal"] = "ship the tool"
    state["workstreams"] = [T("eng", 0, status="done")]
    state["supervisor_waves"] = relay.SUPERVISOR_MAX_WAVES
    io = RecordingIO()
    relay.supervise_next_wave(state, io)
    entry = next(p["entry"] for e, p in io.events if e == "supervisor")
    eq(entry["type"], "goal_unresolved",
       "running out of waves is a different ENDING, not a malfunction")


def test_cap_without_a_verdict_says_so():
    import tempfile as _tf
    state = _sup_state(_tf.mkdtemp(prefix="alloy-wavecapend-"))
    state["supervisor_trace"] = [{"id": "x", "type": "plan_created"}]
    state["workstreams"] = [T("eng", 0, status="active")]
    io = RecordingIO()
    entry = relay.note_unfinished_supervision(state, io, "cap")
    eq(entry["type"], "goal_unresolved",
       "a supervised run that merely ran out of turns must not read as done")
    ok("eng" in entry["detail"], "and names what was still open")
    ok(relay.note_unfinished_supervision(state, io, "cap") is None,
       "it is stated once, not on every continuation")
    state2 = _sup_state(_tf.mkdtemp(prefix="alloy-wavecapend2-"))
    state2["supervisor_trace"] = [{"id": "y", "type": "goal_accepted"}]
    ok(relay.note_unfinished_supervision(state2, io, "cap") is None,
       "a run the manager DID close is never relabelled unfinished")
    ok(relay.note_unfinished_supervision(state, io, "wrapped") is None,
       "and a wrap is not an unfinished ending either")


def test_plan_attempt_latches_until_explicitly_cleared():
    """A planner attempt that yields no tasks must run ONCE — not again on
    every resume. The watchdog's replan remedy is the explicit retry."""
    import tempfile as _tf
    real = relay.build_supervisor
    calls = []

    def failing_planner(st):
        calls.append(1)
        return _StubPlanner("I would start by asking what 'better' means.")

    state = _sup_state(_tf.mkdtemp(prefix="alloy-latch-"))
    io = RecordingIO()
    relay.build_supervisor = failing_planner
    try:
        eq(relay._run_rounds(state, io), "cap", "the first run completes")
        eq(len(calls), 1, "the doomed planning side call runs exactly once")
        ok(state.get("supervisor_plan_attempted"), "and latches the session")
        # A resume (new run over the same state) must not re-plan.
        relay._run_rounds(state, RecordingIO())
        eq(len(calls), 1, "a resumed run does not re-invoke the planner")
        # The watchdog's explicit remedy clears the latch and plans again.
        state["continuous"] = {"on": True, "objectives": [],
                               "checkin": {"minutes": 5}}
        relay.build_supervisor = lambda st: _StubPlanner(
            "Plan.\n[[TASK: eng | owner=0 | files=engine.py | build it]]")
        note = relay.apply_remedy(state, RecordingIO(), "replan", "")
        ok("engine" in note or "task" in note.lower(),
           "the remedy reports what it did: %r" % note)
        ok(state.get("workstreams"), "the retried plan produced tasks")
        eq(not state.get("supervisor_plan_attempted"),
           True, "and the latch is cleared for future resumes")
    finally:
        relay.build_supervisor = real


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
               test_plan_attempt_latches_until_explicitly_cleared,
               test_failed_task_gets_one_bounded_replan,
               test_replan_failure_is_final_and_visible,
               test_parallel_barrier_triggers_replan,
               test_playbook_feeds_the_planner,
               test_supervisor_mode_registered,
               test_plan_drained,
               test_wave_report_separates_verified_from_claimed,
               test_supervisor_verdict_parser,
               test_review_issues_the_next_wave,
               test_review_can_call_the_job_done,
               test_review_will_not_reuse_a_task_id,
               test_review_degrades_safely,
               test_review_budget_is_bounded,
               test_review_only_runs_in_supervisor_mode,
               test_worker_report_survives_settlement,
               test_parallel_barrier_runs_the_manager,
               test_trace_entries_carry_their_wave,
               test_exhausted_waves_is_not_an_error,
               test_cap_without_a_verdict_says_so):
        print("--", fn.__name__)
        fn()
    print("\n%d passed, %d failed" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
