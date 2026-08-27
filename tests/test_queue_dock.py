"""W2.1 — the queue dock: messages Josh lines up for a busy chat.

The engine half is deliberately small, and its shape IS the design:

  * `HumanQueue` replaces the bare `queue.Queue` behind `Run.human_q` and adds
    exactly one method — `snapshot()`. It has no `edit` and no `drop`, because
    the loops drain at a moment no front end can predict: the SEQUENTIAL loop
    reads the queue once per TURN, which is minutes, while parallel and free
    drain every 250 ms. An edit applied to the engine queue would therefore
    silently miss whenever the drain won the race and look perfect in the two
    modes anyone would test it in. The editable hold is client-side.
  * `Api.prepare_message` saves a queued row's attachments WITHOUT delivering
    it, so the row's text is exactly what will be sent.
  * `Api.interject` refuses a leading `/`: four of the five loops tell a
    command from a message by that one test, and once on the queue the two
    are indistinguishable.

And one defect the drain-site survey turned up: `run_battle`'s drain had no
`/` guard at all, so every plain interjection during a blind round was handed
to `dispatch_command`, recorded as a command and answered "Unknown command
/<first word>." — destroyed, never queued to any seat. That is the repo's own
documented shape (N sites, one guard makes the edit meaningless), and a dock
that reported "sent" there would have been lying.

Run:  python tests/test_queue_dock.py
"""

import os
import queue
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import relay
from test_loop import FakeAgent, RecordingIO, build_state


class HumanQueueTests(unittest.TestCase):
    def test_it_is_a_fifo_with_the_three_methods_the_loops_use(self):
        q = app.HumanQueue()
        self.assertTrue(q.empty())
        q.put("first")
        q.put("second")
        self.assertFalse(q.empty())
        self.assertEqual(q.qsize(), 2)
        self.assertEqual(q.get_nowait(), "first")
        self.assertEqual(q.get_nowait(), "second")
        self.assertTrue(q.empty())
        with self.assertRaises(queue.Empty):
            q.get_nowait()

    def test_snapshot_is_a_copy_and_does_not_consume(self):
        q = app.HumanQueue()
        q.put("a")
        q.put("b")
        snap = q.snapshot()
        self.assertEqual(snap, ["a", "b"])
        snap.append("c")                      # a caller's list, not ours
        self.assertEqual(q.qsize(), 2)
        self.assertEqual(q.get_nowait(), "a")

    def test_it_offers_no_edit_or_drop(self):
        """Not an oversight. An edit against the engine queue races the drain,
        and the sequential loop's drain is minutes away — so it would look
        perfect in parallel and free and silently lose in the other three."""
        for name in ("edit", "drop", "remove", "replace"):
            self.assertFalse(hasattr(app.HumanQueue, name),
                             "HumanQueue.%s is back" % name)

    def test_a_run_uses_it(self):
        self.assertIsInstance(app.Run().human_q, app.HumanQueue)

    def test_concurrent_writers_lose_nothing(self):
        q = app.HumanQueue()
        def push(n):
            for i in range(50):
                q.put("%d-%d" % (n, i))
        ts = [threading.Thread(target=push, args=(n,)) for n in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        self.assertEqual(q.qsize(), 300)
        self.assertEqual(len(set(q.snapshot())), 300)


def _fake_run(tmp):
    run = app.Run()
    run.state = {"workspace": tmp}
    return run


class PrepareMessageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-dock-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.api = app.Api()
        self.api._window = type("W", (), {"evaluate_js": lambda *a: None})()
        self.run = _fake_run(self.tmp)
        self.api._runs.adopt(self.run, "chat-a", focus=True)

    def test_it_saves_the_bytes_and_returns_the_lines_it_will_send(self):
        r = self.api.prepare_message(
            "look at this", [{"name": "note.txt", "data": "aGk="}], "chat-a")
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["attached"], 1)
        self.assertIn("look at this", r["text"])
        lines = [l for l in r["text"].splitlines()
                 if l.startswith("[Josh attached a file:")]
        self.assertEqual(len(lines), 1)
        path = lines[0][len("[Josh attached a file: "):-1]
        self.assertTrue(os.path.isfile(path), path)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"hi")

    def test_it_does_not_deliver(self):
        """The whole point: the row is still Josh's until he presses send."""
        self.api.prepare_message("held", None, "chat-a")
        self.assertTrue(self.run.human_q.empty())

    def test_it_refuses_a_leading_slash(self):
        r = self.api.prepare_message("/compact", None, "chat-a")
        self.assertIn("error", r)
        self.assertTrue(self.run.human_q.empty())

    def test_it_refuses_an_empty_row(self):
        self.assertIn("error", self.api.prepare_message("   ", None, "chat-a"))

    def test_it_refuses_an_unknown_chat_rather_than_using_the_focused_one(self):
        r = self.api.prepare_message("hello", None, "no-such-chat")
        self.assertIn("error", r)
        self.assertTrue(self.run.human_q.empty())

    def test_the_attachment_format_is_the_one_the_ui_recomposes(self):
        """ui/index.html's withAttachmentLines is this function's twin: prose,
        a blank line, then one line per file — and lines alone when there is
        no prose."""
        self.assertEqual(app.with_attachments("hi", ["C:/a.png"]),
                         "hi\n\n[Josh attached a file: C:/a.png]")
        self.assertEqual(app.with_attachments("", ["C:/a.png"]),
                         "[Josh attached a file: C:/a.png]")
        self.assertEqual(app.with_attachments("hi", []), "hi")


class InterjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-dock2-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.api = app.Api()
        self.api._window = type("W", (), {"evaluate_js": lambda *a: None})()
        self.run = _fake_run(self.tmp)
        self.api._runs.adopt(self.run, "chat-a", focus=True)

    def test_it_refuses_a_leading_slash(self):
        """Four of the five drain sites tell a command from a message by this
        exact test, and once on the queue the two are indistinguishable."""
        r = self.api.interject("/stop", None, "chat-a")
        self.assertIn("error", r)
        self.assertTrue(self.run.human_q.empty())

    def test_it_refuses_a_slash_BEFORE_it_writes_anything(self):
        """Refusing after saving leaves the files in the workspace with no
        message that names them; prepare_message, its twin, checks first."""
        att = os.path.join(self.tmp, "attachments")
        r = self.api.interject("/compact",
                               [{"name": "note.txt", "data": "aGk="}],
                               "chat-a")
        self.assertIn("error", r)
        self.assertFalse(os.path.isdir(att), "it wrote an orphan attachment")

    def test_it_reports_how_many_are_waiting(self):
        self.assertEqual(self.api.interject("one", None, "chat-a")["waiting"], 1)
        self.assertEqual(self.api.interject("two", None, "chat-a")["waiting"], 2)
        self.run.human_q.get_nowait()
        self.assertEqual(self.api.interject("three", None, "chat-a")["waiting"], 2)


class BattleDrainTests(unittest.TestCase):
    """run_battle's drain had no `/` guard: an interjection during the blind
    round was recorded as a command and destroyed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-battle-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _state(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        state["mode"] = "battle"
        state["orchestration"] = relay.normalize_orchestration(mode="battle")
        state["battle"] = {"phase": "blind", "slots": [0, 1]}
        return state

    def test_a_plain_interjection_reaches_the_seats(self):
        state = self._state()
        # the drain runs while the seat threads are alive, so the script has
        # to answer more than once: one list of lines per drain call
        io = RecordingIO([["what about the edge case?"]])
        relay.run_battle(state, io)
        pending = "\n".join("\n".join(p) for p in state["pending"].values())
        self.assertIn("what about the edge case?", pending,
                      "the interjection never reached a seat's queue")
        notes = [e[1].get("text", "") for e in io.events if e[0] == "status"]
        self.assertFalse([n for n in notes if "Unknown command" in n],
                         "the interjection was read as a command: %r" % notes)

    def test_a_slash_command_still_runs(self):
        state = self._state()
        io = RecordingIO([["/turns 4"]])
        relay.run_battle(state, io)
        notes = [e[1].get("text", "") for e in io.events if e[0] == "status"]
        self.assertTrue([n for n in notes if "Round cap is now 4" in n], notes)


class DockMarkupTests(unittest.TestCase):
    """The parts of the dock only the page can carry."""

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(os.path.dirname(here), "ui", "index.html"),
                  encoding="utf-8") as f:
            cls.html = f.read()

    def test_the_row_is_a_textarea_not_an_input(self):
        """An <input> collapses multi-line text — and a queued row with an
        attachment is ALWAYS multi-line, because with_attachments separates
        the prose from the path block with a blank line.

        Anchored on the ROW's own construction. `createElement("textarea")`
        occurs six times in this file, so asserting its presence proves
        nothing about what the dock builds."""
        i = self.html.index("function renderQueueDock(")
        j = self.html.index("function dropQueued(")
        body = self.html[i:j]
        self.assertIn('const ta = document.createElement("textarea");\n'
                      '    ta.className = "q-text";', body)
        self.assertNotIn('createElement("input")', body.split("q-acts")[0])

    def test_the_refusal_note_lives_outside_the_dock(self):
        """All three of the dock's refusals fire with an EMPTY queue, and the
        dock is hidden when the queue is empty — so a note inside it was
        invisible exactly when it was written."""
        # the note is the dock's SIBLING: its opening tag comes after
        # the dock's closing </div>, at the composer's own indent
        self.assertIn('        <div id="queueList"></div>\n'
                      '      </div>\n'
                      '      <div id="queueNote" hidden></div>', self.html)

    def test_the_empty_dock_does_not_erase_its_own_note(self):
        i = self.html.index("function renderQueueDock(")
        j = self.html.index("function dropQueued(")
        body = self.html[i:j]
        self.assertIn("if (!rows.length) { list.replaceChildren(); return; }",
                      body)

    def test_the_dock_is_registered_in_the_composers_hidden_idiom(self):
        """Its neighbours (#attRow, #battleBar, #askPill, .mention-hint) all
        use the [hidden] attribute; mixing that with the `.show` class idiom is
        how a control ends up permanently invisible."""
        self.assertIn("#queueDock[hidden] { display: none; }", self.html)
        self.assertIn('<div id="queueDock" hidden>', self.html)

    def test_the_queue_button_is_in_the_shared_composer_selector(self):
        """#statsBtn shipped outside the sidebar's equivalent list and drew as
        a raw browser default among five styled siblings for a whole wave."""
        self.assertIn("#attachBtn, #micBtn, #queueBtn {", self.html)
        self.assertIn("#attachBtn:hover, #micBtn:hover, #queueBtn:hover",
                      self.html)

    def test_the_delete_arm_says_what_it_cannot_undo(self):
        """The bytes were written into the working folder when the row was
        queued, so dropping the row leaves them there."""
        self.assertIn('"drop? (keeps "', self.html)
        self.assertIn("stay in the working folder", self.html)

    def test_ctrl_enter_queues_and_plain_enter_still_sends(self):
        self.assertIn("e.ctrlKey || e.metaKey", self.html)
        self.assertIn("e.preventDefault(); queueSay(); return;", self.html)
        self.assertIn('if (e.key === "Enter" && !e.shiftKey) '
                      "{ e.preventDefault(); sendSay(); return; }", self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
