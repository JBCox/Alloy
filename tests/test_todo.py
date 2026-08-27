"""W1.2 - the per-seat todo strip.

Every streaming CLI here already narrates the checklist the model is working
to, and Alloy rendered all of it as one generic line ("tool: TaskCreate").
The plan named `TodoWrite` as the branch to add. It was wrong, and only
capturing real stdout could show that - so every shape below is MEASURED,
2026-08-27, and the fixtures are the literal bytes those captures produced:

* claude 2.1.233 exposes NO `TodoWrite` to this account (the string is in
  the binary; the tool list carries TaskCreate/TaskGet/TaskList/TaskUpdate).
  Its checklist is INCREMENTAL - no event carries the whole list - and the
  number identifying a task appears for the first time in the RESULT text of
  the TaskCreate that made it. It also SURVIVES A RESUME: a second turn's
  TaskList returned both tasks the first turn created, which is why the
  state is per-thread and forget_thread() drops it.
* codex streams a whole-list snapshot as `todo_list` items on
  item.started -> item.updated xN -> item.completed. `item.updated` is a
  type the existing hook's gate never accepted, so every change after the
  first was being dropped.
* opencode's `todowrite` carries `{todos:[{content,status,priority}]}` and
  echoes the same list back as pretty-printed JSON in its output - which the
  generic result note rendered as "16 lines: [" under the strip.
* agy streams nothing at all, so Gemini gets an honest blank.

The UI half is driven through the REAL `activity` and `message` events in
test_ui_boot's node harness, because the trap the plan named is a rendering
one: ACT_LOG_MAX removes `log.firstChild`, so a strip pinned inside the
activity log would be deleted on exactly the forty-step turns it exists for.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import relay  # noqa: E402
import test_ui_boot  # noqa: E402
from test_loop import RecordingIO, build_state, jsonl_rows  # noqa: E402

NODE = test_ui_boot.NODE


def cl(**event):
    return json.dumps(event)


def cl_tool(name, inp, tid="t1"):
    """A real `assistant` line carrying one tool_use block."""
    return cl(type="assistant", message={"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp,
         "caller": {"type": "direct"}}]})


def cl_result(tid, content, is_error=False):
    """A real `user` line carrying one tool_result block."""
    block = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error:
        block["is_error"] = True
    return cl(type="user", message={"content": [block]})


def cx(typ, item):
    return json.dumps({"type": typ, "item": item})


def ox(typ, part):
    return json.dumps({"type": typ, "part": part})


def agent(cls, ws=None):
    return cls(name=cls.name, workspace=ws or ROOT)


def todos(acts):
    return [a for a in (acts or ()) if a.get("kind") == "todo"]


# ---------------------------------------------------------------- helpers --

class NormalizeTests(unittest.TestCase):
    """Three CLIs, three vocabularies, one set of states."""

    def test_every_measured_status_word_maps(self):
        # claude and opencode both spell it these three ways (measured)
        self.assertEqual(relay._todo_state("completed"), "done")
        self.assertEqual(relay._todo_state("in_progress"), "active")
        self.assertEqual(relay._todo_state("pending"), "pending")

    def test_codex_has_only_a_bool_and_it_is_honoured(self):
        # its todo_list items carry no status field at all
        self.assertEqual(relay._todo_state(None, completed=True), "done")
        self.assertEqual(relay._todo_state(None, completed=False), "pending")

    def test_a_status_word_beats_the_bool(self):
        # the defensive read: if exec --json ever grows update_plan's
        # in_progress, the strip must show it rather than call it pending
        self.assertEqual(relay._todo_state("in_progress", completed=False),
                         "active")

    def test_an_unreadable_state_is_pending_never_done(self):
        for junk in ("blocked", "???", 7, {}, "deferred"):
            self.assertEqual(relay._todo_state(junk), "pending")

    def test_the_act_is_empty_when_the_list_is(self):
        for empty in ([], (), None, [{}], [{"text": "  "}], ["x", 3]):
            self.assertEqual(relay._todo_act(empty), ())

    def test_the_caption_names_the_active_item(self):
        act, = relay._todo_act([
            {"text": "read the file", "state": "done"},
            {"text": "run the tests", "state": "active"},
            {"text": "report", "state": "pending"}])
        self.assertEqual(act["kind"], "todo")
        self.assertEqual(act["text"], "plan 1/3 · run the tests")
        self.assertEqual(act["todo"]["done"], 1)
        self.assertEqual(act["todo"]["total"], 3)

    def test_with_nothing_active_it_names_the_next_unfinished_item(self):
        act, = relay._todo_act([{"text": "a", "state": "done"},
                                {"text": "b", "state": "pending"}])
        self.assertEqual(act["text"], "plan 1/2 · b")

    def test_a_finished_plan_says_so(self):
        act, = relay._todo_act([{"text": "a", "state": "done"}])
        self.assertEqual(act["text"], "plan 1/1 · all done")

    def test_an_unreadable_state_never_inflates_the_done_count(self):
        act, = relay._todo_act([{"text": "a", "state": "finished"},
                                {"text": "b", "state": "done"}])
        # "finished" is not a TODO_STATES member: pending, not done
        self.assertEqual(act["todo"]["done"], 1)

    def test_a_runaway_plan_is_capped_and_each_line_is_clipped(self):
        act, = relay._todo_act(
            [{"text": "x" * 400, "state": "pending"}] * 500)
        self.assertEqual(act["todo"]["total"], relay.TODO_ITEMS_MAX)
        self.assertEqual(len(act["todo"]["items"][0]["text"]),
                         relay.TODO_ITEM_MAX)


# ------------------------------------------------------------------ claude --

class ClaudeChecklistTests(unittest.TestCase):
    """Driven with the literal bytes a live `claude -p --output-format
    stream-json --verbose` run produced on 2026-08-27."""

    def setUp(self):
        self.a = agent(relay.ClaudeAgent)
        self.a.before_run()

    def feed(self, line):
        return list(self.a.activity(line) or ())

    def create(self, tid, subject, num):
        self.feed(cl_tool("TaskCreate", {"subject": subject,
                                         "description": subject}, tid))
        return self.feed(cl_result(
            tid, "Task #%s created successfully: %s" % (num, subject)))

    def test_a_create_says_nothing_until_its_result_names_the_number(self):
        # the tool_use block carries only the subject; the id exists nowhere
        # else, so there is nothing truthful to draw yet
        self.assertEqual(
            self.feed(cl_tool("TaskCreate", {"subject": "alpha"}, "t1")), [])

    def test_the_real_create_result_binds_the_task(self):
        acts = self.create("t1", 'Create hello.txt with "hello"', 1)
        self.assertEqual(todos(acts)[0]["text"],
                         'plan 0/1 · Create hello.txt with "hello"')

    def test_the_generic_result_note_is_suppressed_for_the_task_tools(self):
        # "Task #1 created successfully" beside a strip already saying 0/1
        # is the same fact twice, and the second copy has no state in it
        acts = self.create("t1", "alpha", 1)
        self.assertEqual([a["kind"] for a in acts], ["todo"])

    def test_a_failed_task_call_still_reports_its_failure(self):
        self.feed(cl_tool("TaskCreate", {"subject": "alpha"}, "t1"))
        acts = self.feed(cl_result("t1", "no such task list", is_error=True))
        self.assertEqual([a["kind"] for a in acts], ["result"])
        self.assertTrue(acts[0]["text"].startswith("failed"))

    def test_an_update_moves_the_strip_immediately(self):
        self.create("t1", "alpha", 1)
        self.create("t2", "beta", 2)
        acts = self.feed(cl_tool("TaskUpdate",
                                 {"taskId": "1", "status": "in_progress"},
                                 "t3"))
        self.assertEqual(todos(acts)[0]["text"], "plan 0/2 · alpha")
        self.assertEqual(todos(acts)[0]["todo"]["items"][0]["state"], "active")
        acts = self.feed(cl_tool("TaskUpdate",
                                 {"taskId": "1", "status": "completed"}, "t4"))
        self.assertEqual(todos(acts)[0]["text"], "plan 1/2 · beta")

    def test_an_incomplete_view_never_invents_a_denominator(self):
        # A task first seen through an UPDATE was created before this
        # process was watching - which is every reopened chat. Drawing a
        # checklist from that would read "plan 1/1 · all done" for a
        # five-item plan. No complete list, no strip: the event is said as
        # the one line it actually is.
        acts = self.feed(cl_tool("TaskUpdate",
                                 {"taskId": "7", "status": "completed"}, "u"))
        self.assertEqual(todos(acts), [])
        self.assertEqual(acts, [{"kind": "tool",
                                 "text": "task #7 marked done"}])

    def test_one_unknown_task_withholds_a_strip_that_was_working(self):
        self.create("t1", "alpha", 1)
        acts = self.feed(cl_tool("TaskUpdate",
                                 {"taskId": "9", "status": "in_progress"},
                                 "u"))
        self.assertEqual(todos(acts), [])
        # ...and a TaskList makes the view whole again
        self.feed(cl_tool("TaskList", {}, "L"))
        acts = self.feed(cl_result("L", "#1 [completed] alpha\n"
                                        "#9 [in_progress] the missing one"))
        self.assertEqual(todos(acts)[0]["text"], "plan 1/2 · the missing one")

    def test_a_task_known_only_by_number_still_keeps_its_place(self):
        # tracked, not discarded: a later TaskList fills in its subject
        self.feed(cl_tool("TaskUpdate",
                          {"taskId": "7", "status": "completed"}, "u"))
        self.assertEqual(self.a._todo["7"]["text"], "task #7")
        self.assertFalse(self.a._todo["7"]["known"])

    def test_an_update_that_carries_its_own_subject_is_a_complete_view(self):
        acts = self.feed(cl_tool("TaskUpdate",
                                 {"taskId": "7", "status": "in_progress",
                                  "subject": "the one from before"}, "u"))
        self.assertEqual(todos(acts)[0]["todo"]["items"],
                         [{"text": "the one from before", "state": "active"}])

    def test_an_unparseable_create_result_cannot_bind_the_wrong_row(self):
        # the item stays visible (its text is certainly right) under a key
        # no taskId can ever match: an untrackable task, not a mistracked one
        self.feed(cl_tool("TaskCreate", {"subject": "alpha"}, "t1"))
        self.feed(cl_result("t1", "ok"))
        acts = self.feed(cl_tool("TaskUpdate",
                                 {"taskId": "1", "status": "completed"}, "u"))
        self.assertEqual([i["text"] for i in self.a._todo_items()],
                         ["alpha", "task #1"])
        self.assertEqual(self.a._todo["tool:t1"]["state"], "pending")
        # and the strip is withheld, because #1 is a task we cannot name
        self.assertEqual(todos(acts), [])

    def test_tasklist_reconciles_the_whole_list(self):
        # measured result format: "#1 [pending] alpha", one line per task
        self.feed(cl_tool("TaskList", {}, "L"))
        acts = self.feed(cl_result(
            "L", "#1 [completed] alpha\n#2 [in_progress] beta\n"
                 "#3 [pending] gamma"))
        t = todos(acts)[0]["todo"]
        self.assertEqual(t["done"], 1)
        self.assertEqual([i["state"] for i in t["items"]],
                         ["done", "active", "pending"])
        self.assertEqual(todos(acts)[0]["text"], "plan 1/3 · beta")

    def test_tasklist_replaces_rather_than_merges(self):
        self.create("t1", "gone", 9)
        self.feed(cl_tool("TaskList", {}, "L"))
        acts = self.feed(cl_result("L", "#1 [pending] only this"))
        self.assertEqual([i["text"] for i in todos(acts)[0]["todo"]["items"]],
                         ["only this"])

    def test_an_unparseable_tasklist_leaves_the_plan_alone(self):
        self.create("t1", "alpha", 1)
        self.feed(cl_tool("TaskList", {}, "L"))
        self.assertEqual(self.feed(cl_result("L", "no tasks yet")), [])
        acts = self.feed(cl_tool("TaskUpdate",
                                 {"taskId": "1", "status": "completed"}, "u"))
        self.assertEqual(todos(acts)[0]["text"], "plan 1/1 · all done")

    def test_the_checklist_survives_into_the_next_turn(self):
        # THE measured property: a resumed turn's TaskList returned tasks
        # created in the turn before it, so a per-turn reset would orphan
        # every second turn's updates
        self.create("t1", "alpha", 1)
        self.create("t2", "beta", 2)
        self.a.before_run()                     # a new turn starts
        acts = self.feed(cl_tool("TaskUpdate",
                                 {"taskId": "1", "status": "completed"}, "u"))
        self.assertEqual(todos(acts)[0]["text"], "plan 1/2 · beta")

    def test_a_dangling_create_does_not_survive_into_the_next_turn(self):
        # The pending map is the one half that IS per turn: a tool_use id
        # cannot outlive the turn that issued it, so a late result adds
        # nothing to the checklist. (It falls through to the generic note
        # instead, because _tool_names is per turn too and the CLI's own
        # sentence is then all we know about it — which is the honest
        # answer to a result whose call we no longer recognise.)
        self.feed(cl_tool("TaskCreate", {"subject": "alpha"}, "t1"))
        self.a.before_run()
        acts = self.feed(cl_result("t1", "Task #1 created: a"))
        self.assertEqual(todos(acts), [])
        self.assertFalse(self.a._todo)

    def test_forget_thread_drops_the_checklist(self):
        self.create("t1", "alpha", 1)
        self.a.forget_thread()
        self.assertIsNone(self.a._todo)
        self.assertIsNone(self.a._todo_pending)

    def test_forget_thread_is_safe_on_an_adapter_with_no_checklist(self):
        g = agent(relay.GeminiAgent)
        g.forget_thread()                       # must not raise
        self.assertIsNone(g._todo)

    def test_the_todowrite_snapshot_branch_replaces_the_whole_list(self):
        # NOT measured live - this build exposes no TodoWrite - so the shape
        # comes from the binary's own prose: content / status / activeForm
        self.create("t1", "stale", 1)
        acts = self.feed(cl_tool("TodoWrite", {"todos": [
            {"content": "one", "status": "completed"},
            {"content": "two", "status": "in_progress"},
            {"content": "three", "status": "pending"}]}, "w"))
        t = todos(acts)[0]["todo"]
        self.assertEqual([i["text"] for i in t["items"]],
                         ["one", "two", "three"])
        self.assertEqual(t["done"], 1)

    def test_todowrite_junk_is_ignored_rather_than_drawn(self):
        self.assertEqual(self.feed(cl_tool("TodoWrite", {"todos": "no"}, "w")),
                         [])
        self.assertEqual(
            self.feed(cl_tool("TodoWrite",
                              {"todos": [None, 3, {"status": "x"}]}, "w")), [])

    def test_every_other_tool_is_untouched(self):
        acts = self.feed(cl_tool("Bash", {"command": "pytest -q"}, "b"))
        self.assertEqual(acts, [{"kind": "command", "text": "$ pytest -q"}])


# ------------------------------------------------------------------- codex --

class CodexChecklistTests(unittest.TestCase):
    ITEMS = [{"text": "Create hello.txt containing the word hello",
              "completed": False},
             {"text": "Read hello.txt back", "completed": False},
             {"text": "Report completion", "completed": False}]

    def setUp(self):
        self.a = agent(relay.CodexAgent)

    def feed(self, line):
        return list(self.a.activity(line) or ())

    def test_the_opening_snapshot_becomes_a_plan(self):
        acts = self.feed(cx("item.started",
                            {"id": "item_1", "type": "todo_list",
                             "items": self.ITEMS}))
        self.assertEqual(
            acts[0]["text"],
            "plan 0/3 · Create hello.txt containing the word hello")

    def test_item_updated_is_no_longer_dropped(self):
        # THE bug: the hook's gate accepted only item.started and
        # item.completed, so every checklist change after the first - which
        # is every actual tick of progress - never reached the UI at all
        done = [dict(i, completed=True) for i in self.ITEMS[:1]] \
            + self.ITEMS[1:]
        acts = self.feed(cx("item.updated",
                            {"id": "item_1", "type": "todo_list",
                             "items": done}))
        self.assertEqual(acts[0]["text"], "plan 1/3 · Read hello.txt back")

    def test_a_finished_list_reads_as_finished(self):
        all_done = [dict(i, completed=True) for i in self.ITEMS]
        acts = self.feed(cx("item.completed",
                            {"id": "item_1", "type": "todo_list",
                             "items": all_done}))
        self.assertEqual(acts[0]["text"], "plan 3/3 · all done")

    def test_an_unknown_event_type_for_a_todo_list_says_nothing(self):
        self.assertEqual(self.feed(cx("item.deleted",
                                      {"type": "todo_list",
                                       "items": self.ITEMS})), [])

    def test_an_empty_or_junk_list_says_nothing(self):
        self.assertEqual(self.feed(cx("item.started",
                                      {"type": "todo_list", "items": []})), [])
        self.assertEqual(self.feed(cx("item.started",
                                      {"type": "todo_list",
                                       "items": [None, 4]})), [])

    def test_the_other_item_types_still_gate_on_started_and_completed(self):
        # Opening the gate for todo_list must not open it for everything.
        # The payload has to be one that WOULD produce an act if the gate
        # let it through: an item.updated with no aggregated_output answers
        # () either way, so a bare {"command": "ls"} fixture reports this
        # rule as held whether it is or not (caught in the RED pass).
        loud = {"type": "command_execution", "command": "ls",
                "exit_code": 0, "aggregated_output": "a\nb\nc"}
        self.assertEqual(self.feed(cx("item.updated", loud)), [])
        self.assertEqual(self.feed(cx("item.completed", loud)),
                         [{"kind": "result", "text": "3 lines: a"}])
        self.assertEqual(self.feed(cx("item.started", loud)),
                         [{"kind": "command", "text": "$ ls"}])


# ---------------------------------------------------------------- opencode --

class OxChecklistTests(unittest.TestCase):
    # the literal input a live opencode run wrote, priority key included
    TODOS = [{"content": "create hello.txt with 'hello'",
              "status": "in_progress", "priority": "high"},
             {"content": "read hello.txt", "status": "pending",
              "priority": "high"},
             {"content": "report completion", "status": "pending",
              "priority": "high"}]

    def setUp(self):
        self.a = agent(relay.OpenCodeAgent)

    def feed(self, line):
        return list(self.a.activity(line) or ())

    def part(self, todo_rows, output="[]"):
        return ox("tool_use", {"tool": "todowrite",
                               "state": {"status": "completed",
                                         "input": {"todos": todo_rows},
                                         "output": output}})

    def test_the_measured_input_becomes_a_plan(self):
        acts = self.feed(self.part(self.TODOS))
        self.assertEqual(acts[0]["text"],
                         "plan 0/3 · create hello.txt with 'hello'")
        self.assertEqual(acts[0]["todo"]["items"][0]["state"], "active")

    def test_its_echoed_output_is_not_narrated_a_second_time(self):
        # opencode returns the same list again as pretty-printed JSON, which
        # the generic note rendered as "16 lines: [" right under the strip
        echo = json.dumps(self.TODOS, indent=2)
        acts = self.feed(self.part(self.TODOS, output=echo))
        self.assertEqual([a["kind"] for a in acts], ["todo"])

    def test_every_other_ox_tool_still_reports_its_outcome(self):
        acts = self.feed(ox("tool_use", {
            "tool": "bash", "state": {"status": "completed",
                                      "input": {"command": "ls"},
                                      "output": "a\nb\nc"}}))
        self.assertEqual([a["kind"] for a in acts], ["command", "result"])


# ------------------------------------------------------------------ gemini --

class GeminiTests(unittest.TestCase):
    def test_gemini_gets_an_honest_blank(self):
        # agy prints its JSON at the end and has no activity() hook at all,
        # so there is no checklist to show and none is invented
        self.assertNotIn("activity", relay.GeminiAgent.__dict__)
        self.assertFalse(relay.GeminiAgent.streams_progress)


# -------------------------------------------------------------------- sink --

class SinkTests(unittest.TestCase):
    def setUp(self):
        self.io = RecordingIO()
        self.cb, self.acts = relay.make_activity_sink(
            self.io, 0, "claude", "Claude", ROOT)

    def plan(self, done, total):
        items = [{"text": "i%d" % i,
                  "state": "done" if i < done else "pending"}
                 for i in range(total)]
        return relay._todo_act(items)[0]

    def emitted(self):
        return [e for e in self.io.events if e[0] == "activity"
                and e[1].get("kind") == "todo"]

    def test_one_slot_however_many_times_the_seat_replans(self):
        for i in range(1, 6):
            self.cb(self.plan(i, 5))
        self.assertEqual(len(self.acts), 1)
        self.assertEqual(self.acts[0]["todo"]["done"], 5)

    def test_the_checklist_is_always_the_last_entry(self):
        # so commit_reply's [-ACTIVITY_KEEP:] tail slice can never drop it
        self.cb(self.plan(1, 3))
        for i in range(200):
            self.cb({"kind": "command", "text": "$ step %d" % i})
        self.cb(self.plan(2, 3))
        self.assertEqual(self.acts[-1]["kind"], "todo")
        kept = self.acts[-relay.ACTIVITY_KEEP:]
        self.assertEqual(kept[-1]["kind"], "todo")

    def test_a_plan_settled_early_survives_the_rest_of_a_long_turn(self):
        # THE case a live run exposed: the checklist finishes, 200 more
        # steps run, and ACTIVITY_KEEP trims from the FRONT - so a plan
        # parked only when a NEW plan arrives is dropped from exactly the
        # turns it exists for. (The first version of the test above fed a
        # plan LAST, so it could not see this.)
        self.cb(self.plan(3, 3))
        for i in range(200):
            self.cb({"kind": "command", "text": "$ step %d" % i})
        self.assertEqual(self.acts[-1]["kind"], "todo")
        kept = self.acts[-relay.ACTIVITY_KEEP:]
        self.assertEqual([a for a in kept if a["kind"] == "todo"],
                         [self.acts[-1]])

    def test_parking_the_plan_does_not_break_consecutive_dedupe(self):
        # the repeat check used to compare acts[-1], which stops being the
        # previous STEP once a checklist is parked behind it
        self.cb(self.plan(1, 2))
        self.cb({"kind": "command", "text": "$ ls"})
        self.cb({"kind": "command", "text": "$ ls"})
        self.assertEqual([a["kind"] for a in self.acts], ["command", "todo"])

    def test_it_stays_last_even_when_nothing_changed(self):
        self.cb(self.plan(1, 2))
        self.cb({"kind": "command", "text": "$ ls"})
        self.cb(self.plan(1, 2))
        self.assertEqual(self.acts[-1]["kind"], "todo")

    def test_an_unchanged_checklist_is_not_re_emitted(self):
        # codex repeats the final list verbatim on item.completed
        self.cb(self.plan(2, 2))
        self.cb(self.plan(2, 2))
        self.assertEqual(len(self.emitted()), 1)

    def test_a_changed_checklist_is_emitted_every_time(self):
        self.cb(self.plan(1, 3))
        self.cb(self.plan(2, 3))
        self.cb(self.plan(3, 3))
        self.assertEqual(len(self.emitted()), 3)

    def test_the_live_event_carries_the_structured_list(self):
        self.cb(self.plan(1, 2))
        payload = self.emitted()[0][1]
        self.assertEqual(payload["kind"], "todo")
        self.assertEqual(payload["speaker"], 0)
        self.assertEqual(payload["name"], "Claude")
        self.assertEqual(payload["todo"]["total"], 2)

    def test_a_first_checklist_still_respects_the_cap(self):
        for i in range(relay.ACTIVITY_MAX + 5):
            self.cb({"kind": "command", "text": "$ step %d" % i})
        before = len(self.acts)
        self.cb(self.plan(1, 2))
        self.assertEqual(len(self.acts), before)
        self.assertFalse(self.emitted())

    def test_a_checklist_with_a_slot_keeps_updating_at_the_cap(self):
        self.cb(self.plan(0, 2))
        for i in range(relay.ACTIVITY_MAX + 5):
            self.cb({"kind": "command", "text": "$ step %d" % i})
        self.cb(self.plan(2, 2))
        self.assertEqual(self.acts[-1]["todo"]["done"], 2)
        self.assertEqual(len(todos(self.acts)), 1)

    def test_a_captionless_checklist_is_dropped_like_any_other_act(self):
        self.cb({"kind": "todo", "text": "   ", "todo": {"items": []}})
        self.assertFalse(self.acts)

    def test_a_checklist_never_holds_back_the_seats_own_words(self):
        # the one-slot `say` hold lives in cb, not accept: a todo arriving
        # between two say blocks must release the earlier one
        self.cb({"kind": "say", "text": "I'll plan this out"})
        self.cb(self.plan(0, 2))
        self.assertEqual([a["kind"] for a in self.acts], ["say", "todo"])


# -------------------------------------------------------------------- loop --

class LoopTests(unittest.TestCase):
    """Through the REAL loop, the real sink and the real persistence."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_row_carries_the_final_plan(self):
        plan = list(relay._todo_act([{"text": "write it", "state": "done"},
                                     {"text": "test it", "state": "active"}]))
        script = ("done", [{"kind": "command", "text": "$ pytest"}] + plan)
        state = build_state(self.dir, [[script], [script]], turns=1)
        io = RecordingIO()
        relay.run_rounds(state, io)
        rows = [r for r in jsonl_rows(state) if r.get("activity")]
        self.assertTrue(rows)
        acts = rows[0]["activity"]
        self.assertEqual([a["kind"] for a in acts], ["command", "todo"])
        self.assertEqual(acts[-1]["todo"]["done"], 1)
        self.assertEqual(acts[-1]["text"], "plan 1/2 · test it")

    def test_the_plan_reaches_the_ui_live_as_well_as_on_the_row(self):
        plan = list(relay._todo_act([{"text": "a", "state": "active"}]))
        state = build_state(self.dir, [[("done", plan)], [("done", [])]],
                            turns=1)
        io = RecordingIO()
        relay.run_rounds(state, io)
        live = [e[1] for e in io.events
                if e[0] == "activity" and e[1].get("kind") == "todo"]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["todo"]["items"],
                         [{"text": "a", "state": "active"}])


# -------------------------------------------------------------- the export --

class ExportTests(unittest.TestCase):
    """export.py is the SECOND renderer over these rows."""

    def setUp(self):
        import export
        self.export = export
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, activity):
        os.makedirs(os.path.join(self.dir, "s"), exist_ok=True)
        with open(os.path.join(self.dir, "s", "messages.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({"kind": "claude", "name": "Claude",
                                "text": "done", "round": 1,
                                "activity": activity}) + "\n")
        with open(os.path.join(self.dir, "s", "meta.json"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({"id": "s", "title": "t"}))
        out = os.path.join(self.dir, "out.html")
        result = self.export.export_session(os.path.join(self.dir, "s"), out)
        self.assertNotIn("error", result or {})
        with open(out, encoding="utf-8") as f:
            return f.read()

    def test_the_plan_is_rendered_and_not_counted_as_a_step(self):
        html = self.write([
            {"kind": "command", "text": "$ pytest"},
            {"kind": "todo", "text": "plan 1/2 · test it",
             "todo": {"items": [{"text": "write it", "state": "done"},
                                {"text": "test it", "state": "active"}],
                      "done": 1, "total": 2}}])
        self.assertIn("Worked through 1 step<", html)
        self.assertIn("Plan &mdash; 1 of 2 done", html)
        self.assertIn("[x] write it", html)
        self.assertIn("[&gt;] test it", html)

    def test_an_old_row_with_no_structured_plan_still_renders_its_line(self):
        html = self.write([{"kind": "todo", "text": "plan 1/2 · test it"}])
        self.assertIn("plan 1/2", html)

    def test_the_export_stays_byte_identical(self):
        row = [{"kind": "todo", "text": "plan 1/1 · all done",
                "todo": {"items": [{"text": "go", "state": "done"}],
                         "done": 1, "total": 1}}]
        self.assertEqual(self.write(row), self.write(row))


# ---------------------------------------------------------------------- UI --

@unittest.skipUnless(NODE, "node not installed")
class UiTests(unittest.TestCase):
    """The rendering half, in node against the real inline script."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.rep = test_ui_boot.boot(test_ui_boot.UI, cls._tmp.name)
        cls.p = cls.rep.get("todo") or {}
        cls.err = cls.rep.get("todoError")
        with open(os.path.join(ROOT, "ui", "index.html"),
                  encoding="utf-8") as f:
            cls.ui = f.read()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        if self.err:
            self.fail("todo probe threw: %s" % self.err)
        self.assertIsNone(self.rep.get("topLevelError"))

    def test_the_strip_renders_one_line_per_item(self):
        self.assertEqual(self.p["marks"], ["✓", "▸", "○"])
        self.assertEqual(self.p["texts"], ["write it", "test it", "ship it"])
        self.assertEqual(self.p["head"], "plan 1/3")

    def test_each_item_carries_its_state_as_a_class(self):
        self.assertEqual(self.p["classes"],
                         ["todo-item s-done", "todo-item s-active",
                          "todo-item s-pending"])

    def test_the_strip_lives_outside_the_activity_log(self):
        # THE named trap: ACT_LOG_MAX removes log.firstChild, so a strip
        # pinned inside the log is deleted on exactly the long turns it
        # exists for
        self.assertTrue(self.p["stripOutsideLog"])

    def test_the_strip_survives_a_long_turn(self):
        # 40 steps through the real ACT_LOG_MAX trim
        self.assertTrue(self.p["survivesTrim"])
        self.assertEqual(self.p["afterTrimHead"], "plan 3/3")

    def test_the_strip_is_replaced_in_place_never_stacked(self):
        self.assertEqual(self.p["stripCount"], 1)

    def test_a_checklist_is_not_counted_as_a_step(self):
        # the header's counter is a count of things the seat DID - the same
        # rule the engine's sink follows for the token stopwatch
        self.assertEqual(self.p["stepsAfterTodo"], "1")

    def test_the_finished_row_says_the_plan_without_being_expanded(self):
        self.assertEqual(self.p["rowSummary"],
                         "Claude worked through 2 steps · plan 1/3")

    def test_a_reply_that_only_planned_does_not_claim_to_have_worked(self):
        self.assertEqual(self.p["planOnlySummary"],
                         "Claude planned 3 steps · 1 done")

    def test_the_old_summary_wording_is_untouched_without_a_plan(self):
        self.assertEqual(self.p["noPlanSummary"],
                         "Claude worked through 2 steps")

    def test_the_finished_row_draws_the_checklist_too(self):
        self.assertEqual(self.p["rowMarks"], ["✓", "▸", "○"])

    def test_replay_renders_it_identically(self):
        self.assertEqual(self.p["replayMarks"], self.p["rowMarks"])

    def test_a_structureless_todo_falls_back_to_its_plain_line(self):
        # export.py, the CLI echo and any row persisted before this shipped
        # carry only `text`; a renderer that knew nothing would show that
        self.assertIn("plan 1/2", self.p["fallbackHtml"])
        self.assertIn("act-line", self.p["fallbackHtml"])

    def test_nothing_is_drawn_for_an_empty_plan(self):
        self.assertEqual(self.p["emptyHtml"], "")

    def test_item_text_is_escaped(self):
        self.assertNotIn("<img", self.p["escapedHtml"])
        self.assertIn("&lt;img", self.p["escapedHtml"])

    def test_an_unknown_state_draws_the_empty_box_never_a_tick(self):
        self.assertEqual(self.p["unknownStateMark"], "○")

    def test_the_item_style_avoids_the_font_shorthand(self):
        # its family slot rejects `inherit`, and an invalid shorthand
        # silently drops the whole declaration
        block = self.ui.split(".todo-item {")[1].split("}")[0]
        self.assertNotIn("font:", block)
        self.assertIn("font-family: inherit", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
