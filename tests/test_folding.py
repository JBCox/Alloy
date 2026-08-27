"""W1.6 folding a turn away, and W1.7 a note on a reaction.

Two small things, both about a long transcript staying readable, and both
carrying one rule each that is easy to get wrong.

FOLDING uses the class ``.msg-folded``. ``.folded`` is already taken by
``.sup-wave`` in the Supervisor control log, and two meanings for one class
name is how a fold in one panel silently collapses a box in another. It
refuses to fold a row carrying a ``.dir-chip``: a trailing directive is the
line the conversation ACTED on - the wrap that ended it, the nomination that
chose the next speaker, the question still waiting on Josh - and one check
covers the plan's two named cases, because ``md()`` peels an unanswered
``[[ASK]]`` into a ``.dir-chip.dir-ask`` on the row that asked. The refusal
is SAID, on the button, rather than left as a dead control. And a find hit
inside a folded row opens it before scrolling: a ``display:none`` body has
no box at all, so both scroll paths would land nowhere.

A NOTE belongs to its reaction, and is three-state all the way down.
``None`` leaves an existing note alone, ``""`` clears it, text sets it - so
the thumb buttons, which pass no note at all, can never delete words
somebody typed. ``export.py`` renders the note but NOT its stored ``ts``:
the export is byte-identical for identical input by design, and a
timestamp that moves whenever Josh re-clicks a thumb would break that for a
fact nobody reads.
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

import export  # noqa: E402
import outcome  # noqa: E402
import test_ui_boot  # noqa: E402

NODE = test_ui_boot.NODE


class ReactionNoteTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sess = os.path.join(self.dir, "s")
        os.makedirs(self.sess)
        with open(os.path.join(self.sess, "messages.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({"speaker": 0, "provider": "claude",
                                "name": "Claude", "text": "hi", "round": 1,
                                "message_id": "m1",
                                "ts": "2026-08-27T10:00:00"}) + "\n")
        with open(os.path.join(self.sess, "meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"id": "s", "title": "t", "seats": [
                {"id": 0, "label": "Claude", "provider": "claude"}]}, f)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def react(self, verdict, note=None):
        rec = outcome.set_reaction(self.sess, "m1", verdict, note=note)
        return (rec["human_feedback"]["reactions"] or {}).get("m1")

    def test_a_note_rides_the_reaction(self):
        got = self.react("not_helpful", "It answered a different question.")
        self.assertEqual(got["verdict"], "not_helpful")
        self.assertEqual(got["note"], "It answered a different question.")

    def test_a_plain_thumb_never_deletes_an_existing_note(self):
        # THE rule: the thumb buttons pass no note at all
        self.react("helpful", "keep me")
        self.assertEqual(self.react("not_helpful")["note"], "keep me")

    def test_an_empty_string_clears_it_deliberately(self):
        self.react("helpful", "temporary")
        self.assertNotIn("note", self.react("helpful", ""))

    def test_an_absent_note_is_an_absent_key(self):
        self.assertNotIn("note", self.react("helpful"))

    def test_whitespace_alone_is_not_a_note(self):
        self.assertNotIn("note", self.react("helpful", "   \n  "))

    def test_removing_the_reaction_removes_the_note_with_it(self):
        self.react("helpful", "gone too")
        rec = outcome.set_reaction(self.sess, "m1", None)
        self.assertEqual(rec["human_feedback"]["reactions"], {})

    def test_a_long_note_is_capped_not_refused(self):
        got = self.react("helpful", "x" * 5000)
        self.assertEqual(len(got["note"]), outcome.REACTION_NOTE_MAX)

    def test_a_non_string_note_is_refused(self):
        with self.assertRaises(ValueError):
            self.react("helpful", 7)

    def test_the_end_card_and_the_note_do_not_touch_each_other(self):
        # different questions about different scopes
        outcome.set_feedback(self.sess, "helpful", [], "the whole chat")
        self.react("not_helpful", "this one reply though")
        fb = outcome.read_outcome(self.sess)["human_feedback"]
        self.assertEqual(fb["note"], "the whole chat")
        self.assertEqual(fb["reactions"]["m1"]["note"], "this one reply though")

    def test_a_note_survives_a_rebuild(self):
        self.react("helpful", "still here")
        outcome.write_outcome(self.sess)
        fb = outcome.read_outcome(self.sess)["human_feedback"]
        self.assertEqual(fb["reactions"]["m1"]["note"], "still here")


class ExportReactionTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sess = os.path.join(self.dir, "s")
        os.makedirs(self.sess)
        with open(os.path.join(self.sess, "messages.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({"speaker": 0, "provider": "claude",
                                "name": "Claude", "text": "hi", "round": 1,
                                "message_id": "m1"}) + "\n")
            f.write(json.dumps({"speaker": 1, "provider": "gpt",
                                "name": "GPT", "text": "yo", "round": 2,
                                "message_id": "m2"}) + "\n")
        with open(os.path.join(self.sess, "meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"id": "s", "title": "t"}, f)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_outcome(self, reactions):
        with open(os.path.join(self.sess, "outcome.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"human_feedback": {"reactions": reactions}}, f)

    def html(self):
        out = os.path.join(self.dir, "out.html")
        res = export.export_session(self.sess, out)
        self.assertNotIn("error", res or {})
        with open(out, encoding="utf-8") as f:
            return f.read()

    def test_the_thumb_and_the_note_both_render(self):
        self.write_outcome({"m1": {"verdict": "not_helpful",
                                   "ts": "2026-08-27T10:00:00",
                                   "note": "answered the wrong question"}})
        html = self.html()
        self.assertIn("not helpful", html)
        self.assertIn("answered the wrong question", html)

    def test_the_stored_timestamp_is_never_rendered(self):
        # the export is byte-identical for identical input by design
        self.write_outcome({"m1": {"verdict": "helpful",
                                   "ts": "2026-08-27T10:00:00"}})
        self.assertNotIn("2026-08-27T10:00:00", self.html())

    def test_two_exports_of_the_same_input_are_byte_identical(self):
        self.write_outcome({"m1": {"verdict": "helpful", "note": "good",
                                   "ts": "2026-08-27T10:00:00"}})
        first = self.html()
        self.write_outcome({"m1": {"verdict": "helpful", "note": "good",
                                   "ts": "2026-08-27T23:59:59"}})
        self.assertEqual(first, self.html())

    def test_the_reaction_lands_on_ITS_row(self):
        self.write_outcome({"m2": {"verdict": "helpful", "note": "this one"}})
        html = self.html()
        first, second = html.split("<article")[1], html.split("<article")[2]
        self.assertNotIn("this one", first)
        self.assertIn("this one", second)

    def test_a_note_is_escaped(self):
        self.write_outcome({"m1": {"verdict": "helpful",
                                   "note": "<img src=x onerror=1>"}})
        html = self.html()
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)

    def test_a_session_with_no_outcome_exports_unchanged(self):
        # the class name lives in the stylesheet either way; what must be
        # absent is a row that USES it
        self.assertNotIn("class='reaction'", self.html())

    def test_junk_in_outcome_json_never_raises(self):
        with open(os.path.join(self.sess, "outcome.json"), "w",
                  encoding="utf-8") as f:
            f.write("not json at all")
        self.assertIn("<article", self.html())
        self.write_outcome({"m1": "not a dict", "m2": {"verdict": None}})
        self.assertIn("<article", self.html())


class BridgeTests(unittest.TestCase):
    """The real app.Api against a fake window — registered is not callable,
    and a bridge that quietly drops an argument looks exactly like one that
    forwards it (W0.1's whole lesson, one parameter over)."""

    def setUp(self):
        import app
        import relay
        from test_app_headless import FakeWindow
        self.app, self.relay = app, relay
        self.tmp = tempfile.mkdtemp()
        self.old_dir, self.old_tabs = relay.SESSIONS_DIR, relay.TABS_FILE
        relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        self.api = app.Api()
        self.api._window = FakeWindow()
        self.sess = os.path.join(self.tmp, "sess")
        os.makedirs(self.sess)
        with open(os.path.join(self.sess, "messages.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({"speaker": 0, "provider": "claude",
                                "name": "Claude", "text": "hi", "round": 1,
                                "message_id": "m1"}) + "\n")
        with open(os.path.join(self.sess, "meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"id": "sess", "seats": []}, f)

        class Run:
            session_dir = self.sess
        self.api._runs.focused = lambda: Run()

    def tearDown(self):
        self.relay.SESSIONS_DIR = self.old_dir
        self.relay.TABS_FILE = self.old_tabs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stored(self):
        rec = outcome.read_outcome(self.sess) or {}
        return (rec.get("human_feedback") or {}).get("reactions", {}).get("m1")

    def test_the_note_reaches_the_record(self):
        got = self.api.react_message("m1", "not_helpful",
                                     note="it missed the point")
        self.assertTrue(got.get("ok"))
        self.assertEqual(self.stored()["note"], "it missed the point")

    def test_a_thumb_with_no_note_leaves_an_existing_one_alone(self):
        self.api.react_message("m1", "helpful", note="keep me")
        self.api.react_message("m1", "not_helpful")
        self.assertEqual(self.stored()["note"], "keep me")

    def test_the_answer_carries_the_note_back(self):
        got = self.api.react_message("m1", "helpful", note="worth keeping")
        self.assertEqual(got["reaction"]["note"], "worth keeping")

    def test_get_reactions_hands_the_note_to_replay(self):
        self.api.react_message("m1", "helpful", note="from the record")
        self.assertEqual(self.api.get_reactions()["m1"]["note"],
                         "from the record")

    def test_a_bad_note_is_refused_rather_than_stored(self):
        got = self.api.react_message("m1", "helpful", note=7)
        self.assertIn("error", got)
        self.assertIsNone(self.stored())


@unittest.skipUnless(NODE, "node not installed")
class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.rep = test_ui_boot.boot(test_ui_boot.UI, cls._tmp.name)
        cls.p = cls.rep.get("fold") or {}
        cls.err = cls.rep.get("foldError")
        with open(os.path.join(ROOT, "ui", "index.html"),
                  encoding="utf-8") as f:
            cls.ui = f.read()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        if self.err:
            self.fail("fold probe threw: %s" % self.err)
        self.assertIsNone(self.rep.get("topLevelError"))

    # ---- folding
    def test_a_turn_folds_and_unfolds(self):
        self.assertEqual(self.p["foldedClass"], True)
        self.assertEqual(self.p["unfoldedClass"], False)

    def test_a_folded_turn_keeps_one_line_of_itself(self):
        self.assertEqual(self.p["peek"], "The first line of the reply.")

    def test_the_peek_comes_from_the_rows_own_text(self):
        # never scraped back out of rendered HTML, which carries markdown
        # scaffolding the reader never typed
        self.assertEqual(self.p["peekMarkdown"], "# A heading line")

    def test_a_row_with_a_directive_refuses_to_fold(self):
        self.assertFalse(self.p["directiveFolded"])

    def test_the_refusal_is_said_on_the_button(self):
        self.assertIn("directive the conversation acted on",
                      self.p["directiveTitle"])

    def test_alt_click_folds_every_turn_from_that_speaker(self):
        self.assertEqual(self.p["afterAltClick"], [True, False, True])

    def test_alt_click_leaves_a_refusing_row_alone(self):
        self.assertEqual(self.p["altClickWithDirective"], [True, False])

    def test_a_system_row_gets_no_fold_button(self):
        self.assertFalse(self.p["systemHasFold"])

    def test_a_find_hit_opens_the_row_it_landed_in(self):
        # a display:none body has no box, so the scroll would land nowhere
        self.assertTrue(self.p["foldedBeforeFind"],
                        "the probe never folded the row it searched")
        self.assertFalse(self.p["stillFoldedAfterFind"])

    def test_the_class_does_not_collide_with_the_supervisor_log(self):
        # `.folded` already means something on .sup-wave
        self.assertIn(".sup-wave.folded .sup-wave-rows", self.ui)
        self.assertIn(".msg-folded .msg-body", self.ui)
        self.assertNotIn(".msg.folded", self.ui)

    # ---- the note
    def test_a_note_renders_under_the_reply(self):
        self.assertEqual(self.p["noteText"], "It answered a different question.")

    def test_replay_repaints_the_note(self):
        # driven through openChat, not by handing addMsg the field: the line
        # that maps get_reactions onto the row is the one that can be lost
        self.assertEqual(self.p["replayNote"], "from the record")

    def test_the_editor_saves_through_the_bridge_with_the_verdict(self):
        self.assertEqual(self.p["saveCall"],
                         ["m9", "not_helpful", "typed words"])

    def test_cancelling_changes_nothing(self):
        self.assertEqual(self.p["cancelCalls"], 0)
        self.assertEqual(self.p["noteAfterCancel"], "It answered a different question.")

    def test_a_note_with_no_thumb_adopts_the_gentle_reading(self):
        self.assertEqual(self.p["bareNoteCall"], ["m7", "helpful", "just saying"])

    def test_removing_the_thumb_removes_the_note_from_the_row(self):
        self.assertEqual(self.p["noteAfterUnreact"], "")

    def test_a_plain_thumb_click_passes_no_note_at_all(self):
        # so the engine's three-state read can leave it alone
        self.assertEqual(self.p["thumbCall"], ["m9", "helpful", None])

    def test_the_note_is_never_built_into_html(self):
        body = self.ui.split("const paintNote = text =>")[1].split("};")[0]
        self.assertIn("box.textContent = text", body)
        self.assertNotIn("innerHTML", body)

    def test_a_button_carrying_state_stays_visible(self):
        # .copy-btn is opacity 0 until hover, so a marked reply showed
        # nothing at all until Josh happened to hover it
        self.assertIn(".msg .copy-btn.on { opacity: 1; }", self.ui)


if __name__ == "__main__":
    unittest.main(verbosity=2)
