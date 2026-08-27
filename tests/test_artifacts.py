"""W1.1 — produced-file chips.

`relay.artifact_descriptors` has stamped an `artifacts` list onto message
rows since it shipped, and `ui/index.html` had never read the field: on
2026-08-27 the repo held 58 such rows across 9 session logs and the only two
matches for "artifact" in the UI were prose and a comment. The engine was
computing the answer and throwing it away.

Two halves, both token-free:

ENGINE — the descriptors themselves, including the one producer they could
never see. Gemini has no activity stream at all (`streams_progress` is False
and it has no `activity()` hook), and `GeminiAgent.harvest_images` copies the
turn's generated images into the workspace itself — so an image that really
existed on disk had no route into the list. `extra_paths` closes that, and it
goes through the SAME gate (confine, isfile, normcase-dedupe) as a streamed
path rather than being appended raw: a path is a claim until the filesystem
agrees with it, and where the claim came from does not change that.

UI — driven through the REAL `message` event in Node (test_ui_boot's
harness), and through the replay call too, because a chip that only appeared
live would vanish on reopen — the exact failure typing indicators once had.
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

NODE = test_ui_boot.NODE


class DescriptorTests(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def touch(self, rel, body="x"):
        full = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(body)
        return full

    def desc(self, activity=(), extra=None):
        return relay.artifact_descriptors(self.ws, activity, 0, "mid",
                                          extra_paths=extra)

    def test_a_streamed_edit_becomes_a_verified_descriptor(self):
        self.touch("notes/plan.md", "hello")
        got = self.desc([{"kind": "edit", "path": "notes/plan.md"}])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["path"], os.path.join("notes", "plan.md"))
        self.assertEqual(got[0]["kind"], "text/markdown")
        self.assertEqual(got[0]["size"], 5)
        self.assertEqual(got[0]["producer"], 0)
        self.assertEqual(got[0]["source_message_id"], "mid")

    def test_a_path_no_file_backs_is_dropped(self):
        self.assertEqual(self.desc([{"kind": "edit", "path": "ghost.md"}]), [])

    def test_extra_paths_reach_the_list_at_all(self):
        # the Gemini case: nothing in the stream, a real file on disk
        img = self.touch("chart.png", "PNG")
        self.assertEqual(self.desc(), [])
        got = self.desc(extra=[img])
        self.assertEqual([a["path"] for a in got], ["chart.png"])
        self.assertEqual(got[0]["kind"], "image/png")

    def test_extra_paths_are_confined_like_any_other(self):
        outside = tempfile.mkdtemp()
        try:
            stray = os.path.join(outside, "secret.txt")
            with open(stray, "w", encoding="utf-8") as f:
                f.write("no")
            self.assertEqual(self.desc(extra=[stray]), [])
            self.assertEqual(self.desc(extra=["../secret.txt"]), [])
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_extra_paths_must_exist_like_any_other(self):
        self.assertEqual(
            self.desc(extra=[os.path.join(self.ws, "never-written.png")]), [])

    def test_a_file_named_twice_is_listed_once(self):
        img = self.touch("chart.png")
        got = self.desc([{"kind": "edit", "path": "chart.png"}], extra=[img])
        self.assertEqual(len(got), 1)

    def test_a_directory_never_becomes_an_artifact(self):
        # `os.path.getsize` answers happily for a folder on Windows (0), so
        # the isfile gate is the ONLY thing standing between a seat that
        # names a directory and a chip offering to open one. A nonexistent
        # path is caught twice over (getsize raises), a directory is not.
        os.makedirs(os.path.join(self.ws, "build"), exist_ok=True)
        self.assertEqual(self.desc([{"kind": "edit", "path": "build"}]), [])
        self.assertEqual(self.desc(extra=[os.path.join(self.ws, "build")]), [])

    def test_two_spellings_of_one_file_are_listed_once(self):
        # NOTE what does the work here: on Windows os.path.realpath already
        # resolves to the true on-disk casing, so this passes with or without
        # the normcase in the dedupe key. The normcase is the belt for a
        # case-insensitive mount that PRESERVES case, where realpath does
        # not canonicalize — untestable on this machine, and said out loud
        # rather than pinned by a test that would not actually exercise it.
        self.touch("Chart.png")
        got = self.desc([{"kind": "edit", "path": "Chart.png"},
                         {"kind": "edit", "path": "chart.png"}])
        self.assertEqual(len(got), 1)

    def test_junk_in_extra_paths_is_ignored_not_raised(self):
        self.touch("real.txt")
        got = self.desc(extra=[None, 7, "", {"path": "real.txt"}, "real.txt"])
        self.assertEqual([a["path"] for a in got], ["real.txt"])

    def test_no_extra_paths_is_byte_for_byte_the_old_behaviour(self):
        self.touch("a.txt")
        acts = [{"kind": "edit", "path": "a.txt"}]
        self.assertEqual(self.desc(acts), self.desc(acts, extra=None))
        self.assertEqual(self.desc(acts), self.desc(acts, extra=[]))

    def test_ids_are_stable_for_the_same_row(self):
        self.touch("a.txt")
        acts = [{"kind": "edit", "path": "a.txt"}]
        self.assertEqual(self.desc(acts)[0]["artifact_id"],
                         self.desc(acts)[0]["artifact_id"])


class CommitStampsGeminiImagesTests(unittest.TestCase):
    """The wiring, not just the helper: commit_reply must actually pass them.

    A helper that accepts `extra_paths` while its one caller never supplies
    any is a feature that does nothing — and nothing anywhere would say so.
    """

    def test_commit_reply_hands_the_agents_harvested_images_over(self):
        import inspect
        src = inspect.getsource(relay.commit_reply)
        self.assertIn("extra_paths=getattr(agent, \"last_images\", None)", src)

    def test_gemini_is_the_only_adapter_that_harvests(self):
        # if another adapter grows a last_images, this test is the reminder
        # that its reset discipline has to be checked too
        import inspect
        holders = [name for name, cls in relay.AGENT_TYPES.items()
                   if cls is not None
                   and "last_images" in inspect.getsource(cls)]
        self.assertEqual(holders, ["gemini"])

    def test_a_seat_with_no_images_contributes_nothing(self):
        ws = tempfile.mkdtemp()
        try:
            class Bare:
                pass
            self.assertEqual(
                relay.artifact_descriptors(
                    ws, [], 0, "m",
                    extra_paths=getattr(Bare(), "last_images", None)), [])
        finally:
            shutil.rmtree(ws, ignore_errors=True)


class ArtifactRowTests(unittest.TestCase):
    """The field has to survive record() into messages.jsonl, or the UI can
    read it all it likes and still see nothing."""

    def test_artifacts_ride_the_row_and_the_log(self):
        d = tempfile.mkdtemp()
        try:
            store = relay.SessionStore(d)
            store.open_transcript("t", [], 1)
            arts = [{"artifact_id": "a", "path": "x.txt", "kind": "text/plain",
                     "operation": "created_or_modified", "producer": 0,
                     "source_message_id": "m1", "size": 3}]
            row = store.record("Claude", "done", speaker=0, provider="claude",
                               envelope={"message_id": "m1", "audience": "*",
                                         "delivered_to": [], "artifacts": arts})
            self.assertEqual(row["artifacts"], arts)
            with open(store.messages, encoding="utf-8") as f:
                saved = json.loads(f.readline())
            self.assertEqual(saved["artifacts"], arts)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_an_empty_list_leaves_no_key_at_all(self):
        d = tempfile.mkdtemp()
        try:
            store = relay.SessionStore(d)
            store.open_transcript("t", [], 1)
            row = store.record("Claude", "hi", speaker=0, provider="claude",
                               envelope={"message_id": "m1", "audience": "*",
                                         "delivered_to": [], "artifacts": []})
            self.assertNotIn("artifacts", row)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class ExportRendersThemTooTests(unittest.TestCase):
    """export.py is the SECOND renderer over these rows.

    It already reads the sibling `activity` and `usage` keys and dropped this
    one — so an exported transcript silently lost the file list the app was
    about to start showing.
    """

    def setUp(self):
        import export
        self.export = export
        self.d = tempfile.mkdtemp()
        self.session = os.path.join(self.d, "s")
        os.makedirs(self.session)
        rows = [
            {"message_id": "m1", "speaker": 0, "provider": "claude",
             "name": "Claude", "text": "done", "round": 1, "meta": "",
             "ts": "2026-08-27T10:00:00",
             "artifacts": [
                 {"artifact_id": "a1", "path": "notes\\plan.md",
                  "kind": "text/markdown", "size": 3288, "producer": 0,
                  "source_message_id": "m1",
                  "operation": "created_or_modified"}]},
            {"message_id": "m2", "speaker": 1, "provider": "gpt",
             "name": "GPT", "text": "talked", "round": 1, "meta": "",
             "ts": "2026-08-27T10:01:00"},
        ]
        with open(os.path.join(self.session, "messages.jsonl"), "w",
                  encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        with open(os.path.join(self.session, "meta.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"v": 2, "title": "t", "seats": [
                {"id": 0, "provider": "claude", "label": "Claude"},
                {"id": 1, "provider": "gpt", "label": "GPT"}]}, f)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def html(self):
        out = os.path.join(self.d, "out.html")
        res = self.export.export_session(self.session, out_path=out)
        self.assertNotIn("error", res, res)
        with open(out, encoding="utf-8") as f:
            return f.read()

    def test_the_produced_files_are_in_the_export(self):
        html = self.html()
        self.assertIn("Produced 1 file<", html)
        self.assertIn("plan.md", html)
        self.assertIn("3288 bytes", html)

    def test_a_row_that_produced_nothing_says_nothing(self):
        self.assertEqual(self.html().count("Produced"), 1)

    def test_the_export_is_still_byte_identical_across_runs(self):
        # the property the whole module is built around: no timestamp, so
        # identical input renders identical bytes
        self.assertEqual(self.html(), self.html())

    def test_paths_are_text_not_links(self):
        # an export travels away from the machine holding the workspace
        html = self.html()
        block = html[html.index("Produced 1 file"):]
        block = block[:block.index("</details>")]
        self.assertNotIn("<a ", block)
        self.assertNotIn("href", block)

    def test_a_malformed_descriptor_cannot_break_the_export(self):
        p = os.path.join(self.session, "messages.jsonl")
        rows = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
        rows[0]["artifacts"] = [{"no": "path"}, None, 7,
                                {"path": "ok.txt", "size": "big"}]
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        html = self.html()
        self.assertIn("Produced 1 file<", html)
        self.assertIn("ok.txt", html)
        self.assertNotIn("big bytes", html)

    def test_a_path_can_never_smuggle_markup(self):
        p = os.path.join(self.session, "messages.jsonl")
        rows = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
        rows[0]["artifacts"] = [{"path": "<script>x</script>.txt", "size": 1}]
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        self.assertNotIn("<script>x</script>", self.html())


@unittest.skipUnless(NODE, "node not installed")
class ChipUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.report = test_ui_boot.boot(test_ui_boot.UI, cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def a(self, key):
        return (self.report.get("artifacts") or {}).get(key)

    def test_probe_ran_clean(self):
        self.assertIsNone(self.report.get("artifactsError"),
                          "chip probe threw: %s"
                          % self.report.get("artifactsError"))
        self.assertIsNone(self.report.get("topLevelError"))

    def test_absence_stays_silent(self):
        self.assertTrue(self.a("silentWhenAbsent"))
        self.assertTrue(self.a("silentWhenEmpty"))
        self.assertTrue(self.a("silentWhenPathless"))

    def test_one_chip_per_file_naming_the_file(self):
        chips = self.a("chips") or []
        self.assertEqual(len(chips), 2)
        self.assertEqual([c["name"] for c in chips], ["plan.md", "chart.png"])

    def test_a_chip_shows_its_size_and_the_full_path_on_hover(self):
        chips = self.a("chips") or []
        self.assertEqual(chips[0]["size"], "3.2 KB")
        self.assertEqual(chips[1]["size"], "20 KB")
        self.assertIn("notes", chips[0]["title"])
        self.assertIn("plan.md", chips[0]["title"])

    def test_an_image_is_marked_as_one(self):
        chips = self.a("chips") or []
        self.assertEqual(chips[1]["ico"], "\U0001F5BC️")
        self.assertNotEqual(chips[0]["ico"], chips[1]["ico"])

    def test_a_chip_is_a_button_so_it_is_reachable_by_keyboard(self):
        for c in self.a("chips") or []:
            self.assertEqual(c["tag"], "button")

    def test_clicking_routes_by_kind(self):
        routed = self.a("routed") or []
        self.assertEqual(len(routed), 2)
        self.assertEqual(routed[0][0], "code")
        self.assertIn("plan.md", routed[0][1])
        self.assertEqual(routed[0][2], "claude")
        self.assertEqual(routed[1][0], "lightbox")
        self.assertEqual(routed[1][1], "chart.png")

    def test_replay_renders_the_same_chips(self):
        chips = self.a("replay") or []
        self.assertEqual(len(chips), 1)
        self.assertEqual(chips[0]["name"], "out.txt")


class ChipSourceGuards(unittest.TestCase):
    """Rules an executing page cannot show, pinned against the source."""

    def setUp(self):
        with open(os.path.join(ROOT, "ui", "index.html"),
                  encoding="utf-8") as f:
            self.ui = f.read()

    def test_the_renderer_is_called_from_addmsg(self):
        self.assertIn("artifactChips(d.querySelector(\".msg-body\"), "
                      "row && row.artifacts, kind);", self.ui)

    def test_chips_never_build_html_out_of_a_path(self):
        # a path is arbitrary text a CLI chose; the same rule the activity
        # block follows
        body = self.ui.split("function artifactChips(")[1].split(
            "\nfunction ")[0]
        self.assertNotIn("innerHTML = '<span class=\"art-ico\">' +", body)
        for field in ("art-ico", "art-name", "art-size"):
            self.assertIn('querySelector(".%s").textContent' % field, body)

    def test_the_provider_is_passed_in_not_read_back_off_the_row(self):
        # a masked battle row has had its provider deliberately cleared;
        # reading it back would restore the colour the mask just removed
        body = self.ui.split("function artifactChips(")[1].split(
            "\nfunction ")[0]
        self.assertNotIn("closest", body)
        self.assertIn("provider || null", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
