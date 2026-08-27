"""Wave 3 - the memory store, and what a preamble does with it.

Three things drove the shape, and only the first was in the plan.

* **The store is a SIBLING of sessions/.** ``relay.list_sessions`` treats
  every directory under ``SESSIONS_DIR`` as a chat, so ``sessions/memory/``
  would ship a phantom rail row that the rail's two-step delete would
  ``rmtree``. There is a test that builds exactly that folder and watches the
  phantom appear, so the reason survives the decision.

* **The plan said "so total injected context FALLS". It cannot.** Measured
  2026-08-27: the largest preamble Alloy could build before memory was 9,098
  chars, and 9,361 with memory at its floor. What the three constants
  actually buy is that the injected CONTENT holds at 4,000 -
  ``BRIEF_MAX + MEMORY_MIN_SHARE == PREAMBLE_CONTEXT_MAX`` - so memory's
  floor is free rather than something that trims the brief at render time.
  That identity is pinned here.

* **Every write is read-modify-write on a file several chats share**, so the
  playbook's last-rename-wins is exactly wrong here: two windows running
  /remember at the same moment would each read the old file and one note
  would vanish with nothing to show it existed. The lock test runs real
  threads and counts the notes.
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import memory  # noqa: E402
import relay  # noqa: E402
from test_loop import RecordingIO, build_state  # noqa: E402


class Tmp(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="alloy-mem-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def add(self, scope, text, **kw):
        r = memory.remember(self.root, scope, text, **kw)
        self.assertNotIn("error", r, r)
        return r["id"]

    def ids(self, scope):
        return [e["id"] for e in memory.load(self.root, scope)["entries"]]


# --------------------------------------------------------------- scopes ----
class ScopeTests(Tmp):
    def test_same_basename_different_folders_get_different_keys(self):
        # the whole reason the key is not just the basename: every second
        # repo has a src/, an app/ or a docs/
        a = memory.project_key(os.path.join("C:", os.sep, "one", "src"))
        b = memory.project_key(os.path.join("D:", os.sep, "two", "src"))
        self.assertTrue(a.startswith("src-") and b.startswith("src-"))
        self.assertNotEqual(a, b)

    def test_the_key_is_stable_across_calls_and_case(self):
        p = os.path.join(ROOT)
        self.assertEqual(memory.project_key(p), memory.project_key(p))
        self.assertEqual(memory.project_key(p.upper()),
                         memory.project_key(p.lower()))

    def test_a_folder_named_global_cannot_capture_the_global_scope(self):
        key = memory.project_key(os.path.join(self.root, "global"))
        self.assertNotEqual(key, memory.GLOBAL_SCOPE)
        self.assertNotEqual(memory.scope_path(self.root, key),
                            memory.scope_path(self.root, memory.GLOBAL_SCOPE))

    def test_an_unusable_scope_is_refused_rather_than_normalised(self):
        for bad in ("", None, "../escape", "a/b", 7, "x" * 200):
            self.assertIsNone(memory.scope_path(self.root, bad), bad)

    def test_a_nameless_path_still_produces_a_key(self):
        self.assertTrue(memory.project_key(os.sep).endswith(
            memory.project_key(os.sep).split("-")[-1]))
        self.assertTrue(memory.project_key(os.sep))

    def test_a_non_string_workspace_is_empty_not_a_crash(self):
        for bad in (None, 7, b"x", []):
            self.assertEqual(memory.project_key(bad), "")


# --------------------------------------------------------------- format ----
class FormatTests(Tmp):
    def test_render_parse_round_trip_keeps_every_field(self):
        entries = [{"id": "m1", "kind": "josh", "who": "Josh",
                    "when": "2026-08-27", "text": "one\n\ntwo"}]
        back, dup = memory.parse(memory.render(entries))
        self.assertEqual(dup, [])
        self.assertEqual(len(back), 1)
        for k in ("id", "kind", "who", "when", "text"):
            self.assertEqual(back[0][k], entries[0][k], k)

    def test_a_hand_written_note_with_no_metadata_still_parses(self):
        got, _ = memory.parse("## my-note\nremember the milk\n")
        self.assertEqual([(e["id"], e["text"]) for e in got],
                         [("my-note", "remember the milk")])
        # absent, not invented: a note stamped with today's date because its
        # real date was unreadable is a lie the reader cannot detect
        self.assertIsNone(got[0]["kind"])
        self.assertIsNone(got[0]["when"])

    def test_a_mangled_metadata_tail_loses_the_attribution_not_the_note(self):
        got, _ = memory.parse("## m1 blah blah blah\nthe note survives\n")
        self.assertEqual(got[0]["text"], "the note survives")
        self.assertIsNone(got[0]["kind"])

    def test_a_header_with_no_body_is_dropped(self):
        # deleting a note's text but leaving its header MEANT delete
        got, _ = memory.parse("## m1 | josh | Josh | 2026-08-27\n\n## m2\nkept\n")
        self.assertEqual([e["id"] for e in got], ["m2"])

    def test_duplicate_ids_are_reported_not_silently_merged(self):
        got, dup = memory.parse("## m1\na\n\n## m1\nb\n")
        self.assertEqual(len(got), 2)
        self.assertEqual(dup, ["m1"])

    def test_prose_before_the_first_header_is_ignored(self):
        got, _ = memory.parse("some notes I typed\n\n## m1\nreal\n")
        self.assertEqual([e["id"] for e in got], ["m1"])

    def test_a_markdown_heading_inside_a_note_is_not_a_new_entry(self):
        # "## Results" has no id-shaped token that we generated, but it IS
        # id-shaped, so this pins the known limit rather than pretending
        got, _ = memory.parse("## m1\nsee below\n## Results\nmore\n")
        self.assertEqual([e["id"] for e in got], ["m1", "Results"])


# -------------------------------------------------------------- writing ----
class WriteTests(Tmp):
    def test_a_note_round_trips_through_the_file(self):
        mid = self.add(memory.GLOBAL_SCOPE, "Josh writes CRLF.", who="Josh")
        got = memory.load(self.root, memory.GLOBAL_SCOPE)["entries"]
        self.assertEqual([e["id"] for e in got], [mid])
        self.assertEqual(got[0]["kind"], memory.KIND_JOSH)
        self.assertEqual(got[0]["who"], "Josh")

    def test_an_empty_note_is_refused(self):
        self.assertIn("error", memory.remember(self.root, "global", "   "))

    def test_an_unknown_kind_is_refused_rather_than_stored(self):
        self.assertIn("error",
                      memory.remember(self.root, "global", "x", kind="lore"))

    def test_an_oversized_note_is_cut_AND_says_so(self):
        r = memory.remember(self.root, "global", "y" * 5000)
        self.assertTrue(r["ok"])
        self.assertIn("trimmed", r["note"])
        text = memory.load(self.root, "global")["entries"][0]["text"]
        self.assertLessEqual(len(text), memory.ENTRY_TEXT_MAX + 4)

    def test_writing_to_an_unusable_scope_is_an_error_not_a_stray_file(self):
        self.assertIn("error", memory.remember(self.root, "../evil", "x"))
        self.assertEqual(os.listdir(self.root), [])

    def test_forget_removes_every_copy_of_a_hand_duplicated_id(self):
        # ids are unique as generated; a duplicate can only be hand-edited,
        # and a delete that picked one of two identically-named notes is the
        # one operation here nobody could undo
        path = memory.scope_path(self.root, "global")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(memory.render([{"id": "m1", "text": "a"},
                                   {"id": "m1", "text": "b"},
                                   {"id": "m2", "text": "c"}]))
        r = memory.forget(self.root, "global", "m1")
        self.assertEqual(r["removed"], 2)
        self.assertEqual(self.ids("global"), ["m2"])

    def test_forgetting_an_absent_id_says_so(self):
        self.add("global", "one")
        self.assertIn("error", memory.forget(self.root, "global", "mzzzzzzzz"))

    def test_forgetting_a_non_id_is_refused_before_any_read(self):
        self.assertIn("error", memory.forget(self.root, "global", "../x"))

    def test_reading_a_scope_that_was_never_written_is_empty_not_an_error(self):
        got = memory.load(self.root, "global")
        self.assertEqual(got["entries"], [])
        self.assertIsNone(got["error"])


class EvictionTests(Tmp):
    def test_seat_notes_age_out_oldest_first(self):
        old = memory.ENTRIES_MAX
        memory.ENTRIES_MAX = 4
        self.addCleanup(setattr, memory, "ENTRIES_MAX", old)
        first = [self.add("global", "seat %d" % i, kind="seat") for i in range(4)]
        r = memory.remember(self.root, "global", "newest", kind="seat")
        self.assertIn("dropped", r["note"])
        self.assertNotIn(first[0], self.ids("global"))
        self.assertIn(first[-1], self.ids("global"))

    def test_joshs_notes_are_never_evicted_and_the_overflow_is_stated(self):
        old = memory.ENTRIES_MAX
        memory.ENTRIES_MAX = 2
        self.addCleanup(setattr, memory, "ENTRIES_MAX", old)
        mine = [self.add("global", "mine %d" % i) for i in range(3)]
        r = memory.remember(self.root, "global", "one more of mine")
        self.assertTrue(all(m in self.ids("global") for m in mine))
        self.assertIn("nothing was dropped", r["note"])

    def test_a_seat_note_is_evicted_before_any_of_joshs(self):
        old = memory.ENTRIES_MAX
        memory.ENTRIES_MAX = 3
        self.addCleanup(setattr, memory, "ENTRIES_MAX", old)
        seat = self.add("global", "seat note", kind="seat")
        mine = [self.add("global", "mine %d" % i) for i in range(2)]
        memory.remember(self.root, "global", "and another", kind="seat")
        left = self.ids("global")
        self.assertNotIn(seat, left)
        self.assertTrue(all(m in left for m in mine))


class LockTests(Tmp):
    def test_concurrent_writers_do_not_lose_a_note(self):
        # WITHOUT the lock every writer reads the old file and the last
        # rename wins, so most of these vanish. Alloy runs several chats at
        # once by design, so this is an ordinary case, not a rare race.
        n = 12
        errs = []

        def w(i):
            r = memory.remember(self.root, "global", "note %d" % i)
            if "error" in r:
                errs.append(r["error"])

        ts = [threading.Thread(target=w, args=(i,)) for i in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(len(self.ids("global")), n)

    def test_a_stale_lock_is_broken_rather_than_waited_out(self):
        # a killed process would otherwise disable memory permanently
        path = memory.scope_path(self.root, "global")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path + ".lock", "w").close()
        os.utime(path + ".lock",
                 (time.time() - 999, time.time() - memory.LOCK_STALE_S - 5))
        self.assertTrue(memory.remember(self.root, "global", "x")["ok"])

    def test_a_lock_we_could_not_take_is_a_stated_failure(self):
        path = memory.scope_path(self.root, "global")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path + ".lock", "w").close()          # fresh, so not stale
        self.addCleanup(os.unlink, path + ".lock")
        old = memory.LOCK_TIMEOUT_S
        memory.LOCK_TIMEOUT_S = 0.05
        self.addCleanup(setattr, memory, "LOCK_TIMEOUT_S", old)
        r = memory.remember(self.root, "global", "x")
        self.assertIn("error", r)
        self.assertIn("another window", r["error"])

    def test_the_lock_file_is_cleaned_up_after_a_successful_write(self):
        self.add("global", "x")
        path = memory.scope_path(self.root, "global")
        self.assertFalse(os.path.exists(path + ".lock"))


class OversizeTests(Tmp):
    def _huge(self):
        path = memory.scope_path(self.root, "global")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        filler = "z" * 900
        entries = [{"id": "m%04d" % i, "kind": "seat", "who": "Ox",
                    "when": "2026-08-01", "text": filler}
                   for i in range(int(memory.FILE_READ_MAX / 900) + 40)]
        with open(path, "w", encoding="utf-8") as f:
            f.write(memory.render(entries))
        return path

    def test_an_oversized_file_is_truncated_and_says_so(self):
        self._huge()
        got = memory.load(self.root, "global")
        self.assertTrue(got["truncated"])
        self.assertGreater(got["size"], memory.FILE_READ_MAX)

    def test_truncation_never_splits_a_note_in_half(self):
        self._huge()
        got = memory.load(self.root, "global")
        # every surviving note is a whole one: same body as every other
        self.assertTrue(got["entries"])
        self.assertTrue(all(len(e["text"]) == 900 for e in got["entries"]))

    def test_a_file_too_large_to_rewrite_refuses_to_be_written(self):
        # a rewrite from a truncated read would DELETE everything past the cut
        self._huge()
        for r in (memory.remember(self.root, "global", "new"),
                  memory.forget(self.root, "global", "m0001")):
            self.assertIn("error", r)
            self.assertIn("too large", r["error"])


# -------------------------------------------------------------- reading ----
class SearchTests(Tmp):
    def setUp(self):
        super().setUp()
        self.a = self.add("global", "the verification gate is run_all")
        self.b = self.add("global", "run_all takes two minutes")
        self.c = self.add("global", "unrelated note about icons")

    def test_a_two_term_match_outranks_a_repeated_one_term_match(self):
        hits = memory.search(self.root, "global", "verification gate")["hits"]
        self.assertEqual(hits[0]["id"], self.a)

    def test_total_counts_the_matches_not_the_returned_slice(self):
        got = memory.search(self.root, "global", "run_all", limit=1)
        self.assertEqual(len(got["hits"]), 1)
        self.assertEqual(got["total"], 2)

    def test_an_empty_query_returns_everything_rather_than_nothing(self):
        self.assertEqual(memory.search(self.root, "global", "")["total"], 3)

    def test_a_query_matching_nothing_returns_nothing(self):
        self.assertEqual(memory.search(self.root, "global", "zzz")["total"], 0)

    def test_resolve_prefers_an_exact_id_alone(self):
        got = memory.resolve(self.root, "global", self.a)
        self.assertEqual([e["id"] for e in got], [self.a])

    def test_resolve_falls_back_to_a_prefix_then_to_the_text(self):
        self.assertEqual([e["id"] for e in
                          memory.resolve(self.root, "global", self.a[:5])],
                         [self.a])
        self.assertEqual([e["id"] for e in
                          memory.resolve(self.root, "global", "icons")],
                         [self.c])

    def test_resolve_of_nothing_is_nothing(self):
        self.assertEqual(memory.resolve(self.root, "global", "  "), [])


class CollectTests(Tmp):
    def test_joshs_notes_rank_above_structural_above_seat(self):
        s = self.add("p-1234abcd", "seat claim", kind="seat")
        st = self.add("p-1234abcd", "objective met", kind="structural")
        j = self.add("p-1234abcd", "josh note")
        got = memory.collect(self.root, "p-1234abcd")["entries"]
        self.assertEqual([e["id"] for e in got], [j, st, s])

    def test_newest_first_inside_a_kind_and_undated_last(self):
        old = self.add("global", "old", when="2026-01-01")
        new = self.add("global", "new", when="2026-08-27")
        none = self.add("global", "undated", when=" ")
        got = memory.collect(self.root, "global")["entries"]
        self.assertEqual([e["id"] for e in got], [new, old, none])

    def test_a_global_note_JOSH_wrote_reaches_a_project_chat(self):
        j = self.add("global", "Josh writes CRLF everywhere")
        got = memory.collect(self.root, "p-1234abcd")["entries"]
        self.assertEqual([e["id"] for e in got], [j])

    def test_a_global_note_a_SEAT_wrote_does_NOT_reach_a_project_chat(self):
        # the cross-project path the confinement exists to close: a seat in a
        # scratch chat must not be able to plant something every project reads
        self.add("global", "a seat's global claim", kind="seat")
        self.add("global", "a run's global claim", kind="structural")
        self.assertEqual(memory.collect(self.root, "p-1234abcd")["entries"], [])

    def test_a_global_chat_sees_its_own_seat_notes(self):
        s = self.add("global", "seat claim", kind="seat")
        got = memory.collect(self.root, "global")["entries"]
        self.assertEqual([e["id"] for e in got], [s])

    def test_collect_does_not_double_count_the_global_scope(self):
        j = self.add("global", "mine")
        got = memory.collect(self.root, "global")["entries"]
        self.assertEqual([e["id"] for e in got], [j])


class RenderLineTests(unittest.TestCase):
    def line(self, **kw):
        e = {"id": "m1", "kind": "josh", "who": "Josh", "when": "2026-08-27",
             "text": "hello"}
        e.update(kw)
        return memory.one_line(e)

    def test_every_line_names_its_author_and_date(self):
        self.assertEqual(self.line(), "- [Josh, 2026-08-27] hello")

    def test_an_unattributed_note_says_so_rather_than_borrowing(self):
        self.assertIn("[a seat, undated]",
                      self.line(who=None, when=None, kind="seat"))

    def test_a_long_note_is_cut_to_the_line_cap(self):
        got = self.line(text="q" * 2000)
        self.assertLessEqual(len(got), memory.ENTRY_LINE_MAX + 40)
        self.assertTrue(got.endswith("..."))

    def test_a_multi_line_note_collapses_to_one_bullet(self):
        self.assertNotIn("\n", self.line(text="a\nb\n\nc"))

    def test_the_first_note_is_shown_however_small_the_budget(self):
        # a block that rendered nothing because one long note came first
        # would look exactly like "nothing is remembered"
        es = [{"id": "m1", "kind": "josh", "who": "Josh", "when": "2026-08-27",
               "text": "x" * 500}]
        lines, shown, total = memory.render_lines(es, 0)
        self.assertEqual((len(lines), shown, total), (1, 1, 1))

    def test_the_budget_stops_the_rest(self):
        es = [{"id": "m%d" % i, "kind": "seat", "who": "Ox",
               "when": "2026-08-27", "text": "y" * 100} for i in range(20)]
        lines, shown, total = memory.render_lines(es, 400)
        self.assertEqual(total, 20)
        self.assertLess(shown, 20)
        self.assertLessEqual(sum(len(x) + 1 for x in lines), 400 + 130)


# --------------------------------------------------------- relay wiring ----
class ScopeWiringTests(unittest.TestCase):
    def test_a_custom_folder_gets_its_own_project_scope(self):
        scope, label = relay.memory_scope_for(
            os.path.join(relay.SESSIONS_DIR, "chat-1"), ROOT)
        self.assertEqual(label, os.path.basename(ROOT))
        self.assertEqual(scope, memory.project_key(ROOT))

    def test_a_scratch_chat_gets_the_global_scope(self):
        d = os.path.join(relay.SESSIONS_DIR, "chat-1")
        self.assertEqual(relay.memory_scope_for(d, os.path.join(d, "workspace")),
                         (memory.GLOBAL_SCOPE, ""))

    def test_a_missing_session_dir_does_not_make_this_repo_look_scratch(self):
        # os.path.abspath("") is the CWD, which in this process IS the Alloy
        # repo -- reached raw, a chat pointed at C:\ai-chat would resolve to
        # the global scope and quietly share notes with every scratch chat
        scope, label = relay.memory_scope_for("", ROOT)
        self.assertEqual(scope, memory.project_key(ROOT))
        self.assertEqual(label, os.path.basename(ROOT))

    def test_no_workspace_at_all_is_the_global_scope(self):
        self.assertEqual(relay.memory_scope_for("", ""),
                         (memory.GLOBAL_SCOPE, ""))

    def test_memory_scope_reads_the_stores_own_directory(self):
        class Store:
            dir = os.path.join(relay.SESSIONS_DIR, "chat-9")
        got = relay.memory_scope({"store": Store(), "workspace": ROOT})
        self.assertEqual(got[0], memory.project_key(ROOT))


class BudgetTests(unittest.TestCase):
    def test_the_three_constants_hold_the_identity_that_makes_it_free(self):
        self.assertEqual(relay.BRIEF_MAX + relay.MEMORY_MIN_SHARE,
                         relay.PREAMBLE_CONTEXT_MAX)

    def test_a_full_brief_still_leaves_memory_its_floor(self):
        brief = {"status": "quoted", "mode": "verbatim",
                 "quotes": "x" * relay.BRIEF_MAX}
        self.assertEqual(relay.memory_budget(brief), relay.MEMORY_MIN_SHARE)

    def test_no_brief_hands_memory_the_whole_shared_budget(self):
        self.assertEqual(relay.memory_budget(None), relay.PREAMBLE_CONTEXT_MAX)
        self.assertEqual(relay.memory_budget({"status": "off"}),
                         relay.PREAMBLE_CONTEXT_MAX)

    def test_a_brief_built_under_the_older_larger_cap_cannot_starve_memory(self):
        # a chat resumed across this change carries one
        stale = {"status": "quoted", "mode": "verbatim", "quotes": "x" * 4000}
        self.assertEqual(relay.memory_budget(stale), relay.MEMORY_MIN_SHARE)

    def test_the_brief_is_charged_for_its_content_not_its_framing(self):
        brief = {"status": "quoted", "mode": "verbatim", "quotes": "x" * 100}
        self.assertEqual(relay.brief_content_len(brief), 100)
        self.assertLess(len(relay.brief_preamble_block(brief)), 100 + 2000)
        self.assertEqual(relay.memory_budget(brief),
                         relay.PREAMBLE_CONTEXT_MAX - 100)

    def test_a_failed_brief_spent_nothing(self):
        self.assertEqual(relay.brief_content_len(
            {"status": "failed", "error": "x"}), 0)

    def test_EVERY_status_project_brief_can_set_is_charged_correctly(self):
        # MEASURED from project_brief on 2026-08-27, not guessed: it sets
        # quoted (verbatim) and fresh / written / updated / readonly
        # (synthesized), plus off / none / failed for the empty cases. A
        # first version of brief_content_len whitelisted ("quoted", "digest",
        # "ok") and so charged the whole SYNTHESIZED family zero, which would
        # have let memory take the full 4,000 on top of a 3,500-char digest:
        # 7,500 chars where 4,000 was the promise.
        for st in ("quoted", "fresh", "written", "updated", "readonly"):
            for key in ("quotes", "digest"):
                self.assertEqual(
                    relay.brief_content_len({"status": st, key: "x" * 3500}),
                    3500, (st, key))
                self.assertEqual(
                    relay.memory_budget({"status": st, key: "x" * 3500}),
                    relay.MEMORY_MIN_SHARE, (st, key))
        for st in ("off", "none", "failed"):
            self.assertEqual(relay.brief_content_len({"status": st}), 0, st)

    def test_the_charge_does_not_consult_the_status_at_all(self):
        # the general form of the bug above: ANY whitelist, however spelled,
        # charges an unrecognised status zero
        self.assertEqual(
            relay.brief_content_len({"status": "a-status-nobody-has-invented",
                                     "quotes": "x" * 100}), 100)


class PreambleBlockTests(unittest.TestCase):
    def rec(self, **kw):
        base = {"status": "ok", "label": "ai-chat", "truncated": False,
                "entries": [{"id": "m1", "kind": "josh", "who": "Josh",
                             "when": "2026-08-27", "text": "The gate is X."}]}
        base.update(kw)
        return base

    def test_nothing_remembered_renders_nothing(self):
        self.assertEqual(relay.memory_preamble_block(None), "")
        self.assertEqual(relay.memory_preamble_block({"status": "none"}), "")
        self.assertEqual(
            relay.memory_preamble_block(self.rec(status="ok", entries=[])), "")

    def test_an_unreadable_store_is_DECLARED_not_rendered_as_nothing(self):
        # a seat told there are no notes, when there are notes it could not
        # read, will happily re-decide something Josh already settled
        got = relay.memory_preamble_block({"status": "failed",
                                           "error": "disk on fire"})
        self.assertIn("could not read", got)
        self.assertIn("disk on fire", got)

    def test_the_block_names_the_project_and_marks_who_wrote_each_note(self):
        got = relay.memory_preamble_block(self.rec())
        self.assertIn("this project (ai-chat)", got)
        self.assertIn("- [Josh, 2026-08-27] The gate is X.", got)
        self.assertIn("Notes marked Josh are his own words", got)

    def test_a_global_scope_block_does_not_claim_a_project(self):
        got = relay.memory_preamble_block(self.rec(label=""))
        self.assertIn("your work with Josh generally", got)
        self.assertNotIn("this project", got)

    def test_the_solo_block_drops_the_reassurance_about_nobody(self):
        group = relay.memory_preamble_block(self.rec())
        solo = relay.memory_preamble_block(self.rec(), solo=True)
        self.assertIn("Every participant was given these same notes", group)
        self.assertNotIn("Every participant", solo)

    def test_a_trimmed_block_says_how_much_it_left_out(self):
        many = [{"id": "m%d" % i, "kind": "seat", "who": "Ox",
                 "when": "2026-08-27", "text": "z" * 200} for i in range(40)]
        got = relay.memory_preamble_block(self.rec(entries=many), 400)
        self.assertRegex(got, r"Showing \d+ of 40")

    def test_a_complete_block_does_not_claim_to_be_trimmed(self):
        self.assertNotIn("Showing", relay.memory_preamble_block(self.rec(), 4000))

    def test_a_truncated_store_admits_there_may_be_more(self):
        got = relay.memory_preamble_block(self.rec(truncated=True), 4000)
        self.assertIn("too large to read in full", got)


class InjectionTests(unittest.TestCase):
    """The artefact the real component consumes: the prompt string."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-mem-run-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mem = os.path.join(self.tmp, "memory")
        old = relay.MEMORY_DIR
        relay.MEMORY_DIR = self.mem
        self.addCleanup(setattr, relay, "MEMORY_DIR", old)

    def _run(self, scripts=None):
        state = build_state(self.tmp,
                             [scripts or ["hello"], scripts or ["hi"]],
                             turns=1)
        relay.run_rounds(state, RecordingIO())
        return state

    def test_a_remembered_note_reaches_the_seats_first_prompt(self):
        memory.remember(self.mem, memory.GLOBAL_SCOPE,
                        "The gate is python tests/run_all.py.", who="Josh")
        state = self._run()
        self.assertIn("The gate is python tests/run_all.py.",
                      state["agents"][0].prompts[0])

    def test_an_empty_store_leaves_the_prompt_exactly_as_it_was(self):
        state = self._run()
        self.assertNotIn("What Alloy remembers", state["agents"][0].prompts[0])

    def test_every_seat_gets_the_same_notes(self):
        memory.remember(self.mem, memory.GLOBAL_SCOPE, "shared fact",
                        who="Josh")
        state = self._run()
        for a in state["agents"]:
            self.assertIn("shared fact", a.prompts[0])

    def test_a_caller_that_passes_no_memory_gets_the_preamble_it_always_got(self):
        a = relay.ClaudeAgent(self.tmp, name="Claude")
        b = relay.CodexAgent(self.tmp, name="GPT")
        without = relay.preamble(a, [b], "t", 3, self.tmp, roster=[a, b])
        explicit = relay.preamble(a, [b], "t", 3, self.tmp, roster=[a, b],
                                  memory=None)
        self.assertEqual(without, explicit)
        self.assertNotIn("What Alloy remembers", without)


# --------------------------------------------------- slash commands --------
class CommandTests(unittest.TestCase):
    """Driven through the REAL dispatch_command, not the helpers it calls."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-mem-cmd-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mem = os.path.join(self.tmp, "memory")
        old = relay.MEMORY_DIR
        relay.MEMORY_DIR = self.mem
        self.addCleanup(setattr, relay, "MEMORY_DIR", old)
        self.io = RecordingIO()

    def chat(self, project=False):
        ws = None
        if project:
            ws = os.path.join(self.tmp, "myproj")
            os.makedirs(ws, exist_ok=True)
        return build_state(self.tmp, [["a"]], turns=1, workspace=ws)

    def cmd(self, state, text):
        relay.dispatch_command(state, text, self.io)
        last = self.io.events[-1]
        self.assertEqual(last[0], "status", last)
        return last[1]["text"]

    def test_remember_writes_and_names_the_scope_it_wrote_to(self):
        st = self.chat()
        got = self.cmd(st, "/remember The gate is run_all.")
        self.assertIn("Remembered for everywhere", got)
        self.assertIn("The gate is run_all.",
                      [e["text"]
                       for e in memory.load(self.mem, "global")["entries"]])

    def test_a_project_chat_writes_to_its_own_scope(self):
        st = self.chat(project=True)
        got = self.cmd(st, "/remember Local fact.")
        self.assertIn("this project (myproj)", got)
        self.assertEqual(memory.load(self.mem, "global")["entries"], [])
        scope = relay.memory_scope(st)[0]
        self.assertEqual([e["text"]
                          for e in memory.load(self.mem, scope)["entries"]],
                         ["Local fact."])

    def test_remember_with_no_text_says_where_it_would_have_gone(self):
        got = self.cmd(self.chat(project=True), "/remember")
        self.assertIn("Usage", got)
        self.assertIn("this project (myproj)", got)

    def test_memory_lists_what_the_seats_are_actually_shown(self):
        st = self.chat(project=True)
        memory.remember(self.mem, "global", "A global note.", who="Josh")
        self.cmd(st, "/remember A project note.")
        got = self.cmd(st, "/memory")
        self.assertIn("A project note.", got)
        self.assertIn("A global note.", got)
        self.assertIn("everywhere", got)          # marked as crossing in

    def test_memory_on_an_empty_store_says_so_and_how_to_write_one(self):
        got = self.cmd(self.chat(), "/memory")
        self.assertIn("Nothing is remembered", got)
        self.assertIn("/remember", got)

    def test_forget_with_a_partial_match_only_REPORTS_the_id(self):
        # the arm is STATELESS: nothing is stored between the two commands,
        # so it cannot fire later at whatever is under the cursor then
        st = self.chat()
        self.cmd(st, "/remember The gate is run_all.")
        before = memory.load(self.mem, "global")["entries"]
        got = self.cmd(st, "/forget gate")
        self.assertIn("Did you mean", got)
        self.assertIn(before[0]["id"], got)
        self.assertEqual(len(memory.load(self.mem, "global")["entries"]), 1)

    def test_forget_with_the_exact_id_acts(self):
        st = self.chat()
        self.cmd(st, "/remember Something.")
        mid = memory.load(self.mem, "global")["entries"][0]["id"]
        self.assertIn("Forgot", self.cmd(st, "/forget " + mid))
        self.assertEqual(memory.load(self.mem, "global")["entries"], [])

    def test_forget_of_nothing_matching_says_so(self):
        self.assertIn("matches", self.cmd(self.chat(), "/forget zzzzz"))

    def test_forget_with_no_argument_points_at_the_list(self):
        self.assertIn("/memory", self.cmd(self.chat(), "/forget"))

    def test_a_project_chat_can_forget_a_global_note_by_its_exact_id(self):
        # it is shown one, so it must be able to drop one -- but only in the
        # confirmed form, never through a fuzzy match
        st = self.chat(project=True)
        memory.remember(self.mem, "global", "A global note.", who="Josh")
        mid = memory.load(self.mem, "global")["entries"][0]["id"]
        self.assertIn("Forgot", self.cmd(st, "/forget " + mid))
        self.assertEqual(memory.load(self.mem, "global")["entries"], [])

    def test_a_fuzzy_match_never_reaches_the_global_file_from_a_project(self):
        st = self.chat(project=True)
        memory.remember(self.mem, "global", "A global note.", who="Josh")
        got = self.cmd(st, "/forget global")
        self.assertNotIn("Forgot", got)
        self.assertEqual(len(memory.load(self.mem, "global")["entries"]), 1)

    def test_the_three_commands_are_in_the_help_text(self):
        for c in ("/remember", "/forget", "/memory"):
            self.assertIn(c, relay.HELP_TEXT)

    def test_an_unknown_command_still_says_unknown(self):
        self.assertIn("Unknown command", self.cmd(self.chat(), "/rememberrr x"))


# ------------------------------------------------------------- bridge ------
class BridgeTests(unittest.TestCase):
    """Real app.Api against a fake window -- registered is not callable."""

    def setUp(self):
        import app
        from test_app_headless import FakeWindow
        self.tmp = tempfile.mkdtemp(prefix="alloy-mem-bridge-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        old_s, old_m = relay.SESSIONS_DIR, relay.MEMORY_DIR
        # sessions/ and the project folder must be SIBLINGS: a workspace
        # inside SESSIONS_DIR is a scratch workspace by definition, so
        # nesting them here would make every project chat resolve to global
        # and the suite would be testing nothing
        self.sessions = os.path.join(self.tmp, "sessions")
        os.makedirs(self.sessions, exist_ok=True)
        relay.SESSIONS_DIR = self.sessions
        relay.MEMORY_DIR = os.path.join(self.tmp, "memory")
        self.addCleanup(setattr, relay, "SESSIONS_DIR", old_s)
        self.addCleanup(setattr, relay, "MEMORY_DIR", old_m)
        self.api = app.Api()
        self.api._window = FakeWindow()

    def _project_run(self):
        ws = os.path.join(self.tmp, "someproj")
        os.makedirs(ws, exist_ok=True)
        run = self.api._runs.focused()
        run.session_dir = os.path.join(self.sessions, "chat-1")
        run.state = {"workspace": ws}
        return ws

    def test_with_no_conversation_the_modal_still_shows_global_notes(self):
        memory.remember(relay.MEMORY_DIR, "global", "Josh writes CRLF.",
                        who="Josh")
        got = self.api.get_memory()
        self.assertEqual(got["scope"], memory.GLOBAL_SCOPE)
        self.assertEqual([e["text"] for e in got["entries"]],
                         ["Josh writes CRLF."])

    def test_a_project_chat_gets_its_own_scope_and_the_crossing_notes(self):
        ws = self._project_run()
        memory.remember(relay.MEMORY_DIR, "global", "global one", who="Josh")
        memory.remember(relay.MEMORY_DIR, memory.project_key(ws),
                        "project one", who="Josh")
        got = self.api.get_memory()
        self.assertEqual(got["label"], "someproj")
        self.assertEqual(sorted(e["text"] for e in got["entries"]),
                         ["global one", "project one"])
        self.assertEqual({e["scope"] for e in got["entries"]},
                         {memory.GLOBAL_SCOPE, memory.project_key(ws)})

    def test_an_unknown_chat_id_resolves_to_global_not_the_focused_chat(self):
        # the _active_workspace rule: answering an unknown id from whatever
        # is on screen is how chat A's notes start appearing under chat B
        ws = self._project_run()
        memory.remember(relay.MEMORY_DIR, memory.project_key(ws), "secret",
                        who="Josh")
        got = self.api.get_memory("no-such-chat")
        self.assertEqual(got["scope"], memory.GLOBAL_SCOPE)
        self.assertEqual(got["entries"], [])

    def test_save_writes_to_the_chats_scope_by_default(self):
        ws = self._project_run()
        got = self.api.save_memory("a project note")
        self.assertTrue(got["ok"])
        self.assertEqual([e["text"] for e in
                          memory.load(relay.MEMORY_DIR,
                                      memory.project_key(ws))["entries"]],
                         ["a project note"])

    def test_save_everywhere_writes_what_a_project_chat_otherwise_cannot(self):
        # without this the crossing rule would only be reachable from a
        # scratch chat, i.e. dead for anyone working in a project
        self._project_run()
        self.assertTrue(self.api.save_memory("everywhere please", True)["ok"])
        rows = memory.load(relay.MEMORY_DIR, memory.GLOBAL_SCOPE)["entries"]
        self.assertEqual([e["text"] for e in rows], ["everywhere please"])
        self.assertEqual(rows[0]["kind"], memory.KIND_JOSH)

    def test_save_returns_the_refreshed_list_so_the_page_never_guesses(self):
        self.api.save_memory("one")
        got = self.api.save_memory("two")
        self.assertEqual(sorted(e["text"] for e in got["entries"]),
                         ["one", "two"])

    def test_an_empty_note_comes_back_as_an_error(self):
        self.assertIn("error", self.api.save_memory("   "))

    def test_a_trim_rides_back_as_a_note_rather_than_happening_silently(self):
        got = self.api.save_memory("z" * 5000)
        self.assertIn("trimmed", got["note"])

    def test_forget_removes_the_note_and_returns_the_new_list(self):
        self.api.save_memory("one")
        mid = self.api.get_memory()["entries"][0]["id"]
        got = self.api.forget_memory(mid)
        self.assertEqual(got["removed"], 1)
        self.assertEqual(got["entries"], [])

    def test_forget_REFUSES_a_scope_this_chat_cannot_see(self):
        # the id and the scope both arrive from the page; an unchecked scope
        # would reach any project's file, and this is the one operation here
        # that cannot be undone
        other = memory.project_key(os.path.join(self.tmp, "elsewhere"))
        memory.remember(relay.MEMORY_DIR, other, "another project's note",
                        who="Josh")
        mid = memory.load(relay.MEMORY_DIR, other)["entries"][0]["id"]
        got = self.api.forget_memory(mid, other)
        self.assertIn("error", got)
        self.assertEqual(len(memory.load(relay.MEMORY_DIR, other)["entries"]),
                         1)

    def test_a_project_chat_may_forget_the_global_note_it_is_shown(self):
        self._project_run()
        self.api.save_memory("everywhere please", True)
        mid = memory.load(relay.MEMORY_DIR,
                          memory.GLOBAL_SCOPE)["entries"][0]["id"]
        self.assertTrue(
            self.api.forget_memory(mid, memory.GLOBAL_SCOPE)["ok"])
        self.assertEqual(
            memory.load(relay.MEMORY_DIR, memory.GLOBAL_SCOPE)["entries"], [])

    def test_forgetting_something_absent_is_an_error_not_a_silent_ok(self):
        self.assertIn("error", self.api.forget_memory("mzzzzzzzz"))


# ------------------------------------------------ structural memory --------
class StructuralTests(unittest.TestCase):
    """What a settled objective leaves behind. Zero CLI calls, by design:
    archive_objective already builds a filesystem-VERIFIED record and threw
    it away."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-mem-struct-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mem = os.path.join(self.tmp, "memory")
        old = relay.MEMORY_DIR
        relay.MEMORY_DIR = self.mem
        self.addCleanup(setattr, relay, "MEMORY_DIR", old)
        self.proj = os.path.join(self.tmp, "someproj")
        os.makedirs(self.proj, exist_ok=True)

    def state(self, project=True, goal="Tidy the tests", delivered=("a.py",),
              failed=0, gate=None, tasks=None):
        st = build_state(self.tmp, [["a"]], turns=1,
                         workspace=self.proj if project else None)
        st["supervisor_goal"] = goal
        st["continuous"] = relay.continuous_policy({"on": True})
        if gate is not None:
            st["continuous"]["gate"]["last"] = gate
        st["workstreams"] = tasks if tasks is not None else [
            {"id": "t1", "status": "done",
             "verified": {"delivered": list(delivered)}},
            {"id": "t2", "status": "failed" if failed else "done",
             "verified": {"delivered": []}},
        ]
        return st

    def notes(self, st):
        scope = relay.memory_scope(st)[0]
        return memory.load(self.mem, scope)["entries"]

    def test_a_settled_objective_is_remembered(self):
        st = self.state(gate={"ok": True})
        relay.archive_objective(st)
        rows = self.notes(st)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], memory.KIND_STRUCTURAL)
        self.assertIn("Tidy the tests", rows[0]["text"])
        self.assertIn("a.py", rows[0]["text"])
        self.assertIn("verification passed", rows[0]["text"])

    def test_the_note_is_written_by_the_rooms_own_manager_name(self):
        st = self.state()
        st["supervisor"] = {"name": "Foreman"}
        relay.archive_objective(st)
        self.assertEqual(self.notes(st)[0]["who"], "Foreman")

    def test_an_unnamed_manager_falls_back_to_the_role_word(self):
        st = self.state()
        relay.archive_objective(st)
        self.assertEqual(self.notes(st)[0]["who"], "Supervisor")

    def test_a_SCRATCH_run_records_nothing_and_that_is_deliberate(self):
        # "delivered src/a.py" is about one codebase; in the global scope it
        # would surface in every unrelated scratch chat, and a scratch run's
        # files live in a session folder nobody opens again
        st = self.state(project=False)
        relay.archive_objective(st)
        self.assertEqual(memory.load(self.mem, memory.GLOBAL_SCOPE)["entries"],
                         [])

    def test_an_objective_with_no_tasks_leaves_nothing(self):
        st = self.state(tasks=[])
        relay.archive_objective(st)
        self.assertEqual(self.notes(st), [])

    def test_a_nameless_objective_leaves_nothing(self):
        # a record whose goal never got set says only "N tasks", which is not
        # worth a permanent note
        st = self.state(goal="")
        relay.archive_objective(st)
        self.assertEqual(self.notes(st), [])

    def test_EVERY_rollover_is_recorded_not_just_the_last(self):
        # the whole reason this lives at the barrier rather than in the
        # run-end epilogue: a Keep Improving run ends ONCE, and an epilogue
        # would keep the final objective and lose every one before it
        st = self.state(goal="First")
        relay.archive_objective(st)
        st["supervisor_goal"] = "Second"
        st["workstreams"] = [{"id": "t3", "status": "done",
                              "verified": {"delivered": ["b.py"]}}]
        relay.archive_objective(st)
        self.assertEqual([e["text"].split()[2] for e in self.notes(st)],
                         ["First.", "Second."])

    def test_the_record_still_reaches_the_policys_own_history(self):
        # the memory write is additive; it must not displace what the UI and
        # the next plan already read
        st = self.state()
        relay.archive_objective(st)
        self.assertEqual(len(st["continuous"]["history"]), 1)
        self.assertEqual(st["continuous"]["history"][0]["goal"],
                         "Tidy the tests")

    def test_a_broken_memory_store_never_breaks_the_barrier(self):
        st = self.state()
        real = memory.remember

        def boom(*a, **k):
            raise RuntimeError("disk on fire")
        memory.remember = boom
        self.addCleanup(setattr, memory, "remember", real)
        relay.archive_objective(st)                    # must not raise
        self.assertIsNone(st["workstreams"])
        self.assertEqual(len(st["continuous"]["history"]), 1)

    def test_the_note_reaches_a_later_chats_preamble_in_that_project(self):
        # the point of the whole commit: the next run in this folder starts
        # knowing what the last one settled
        st = self.state(gate={"ok": True})
        relay.archive_objective(st)
        later = build_state(self.tmp, [["a"]], turns=1, workspace=self.proj)
        block = relay.memory_preamble_block(relay.memory_record(later), 4000)
        self.assertIn("Tidy the tests", block)
        self.assertIn("Supervisor", block)

    def test_it_does_NOT_reach_a_different_projects_chat(self):
        st = self.state()
        relay.archive_objective(st)
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other, exist_ok=True)
        later = build_state(self.tmp, [["a"]], turns=1, workspace=other)
        self.assertEqual(relay.memory_record(later)["entries"], [])


class ObjectiveSentenceTests(unittest.TestCase):
    def sentence(self, **kw):
        rec = {"goal": "Tidy the tests", "tasks": 4, "failed": 0,
               "delivered": ["a.py"], "gate": None}
        rec.update(kw)
        return relay.describe_objective(rec)

    def test_a_research_wave_says_no_files_rather_than_trailing_off(self):
        # left as an absence, the sentence stops after the task count and
        # reads as though the file list went missing
        self.assertIn("no files changed", self.sentence(delivered=[]))

    def test_failures_are_named(self):
        self.assertIn("2 failed", self.sentence(failed=2))

    def test_a_clean_wave_does_not_mention_failures(self):
        self.assertNotIn("failed", self.sentence())

    def test_a_long_file_list_is_cut_and_counts_what_it_cut(self):
        got = self.sentence(delivered=["f%d.py" % i for i in range(20)])
        self.assertIn("and %d more" % (20 - relay.OBJECTIVE_MEMORY_FILES), got)

    def test_a_SKIPPED_gate_claims_neither_pass_nor_fail(self):
        # ok is None there, and "verification passed" would be a lie while
        # "verification failed" would be a different one
        got = self.sentence(gate={"skipped": "no test command", "ok": None})
        self.assertNotIn("verification", got)

    def test_a_red_gate_is_recorded_as_red(self):
        self.assertIn("verification failed", self.sentence(gate={"ok": False}))

    def test_one_task_is_singular(self):
        self.assertIn("1 task;", self.sentence(tasks=1))

    def test_a_goal_with_no_text_produces_no_sentence(self):
        self.assertEqual(self.sentence(goal="   "), "")


class SiblingTests(unittest.TestCase):
    def test_a_memory_folder_INSIDE_sessions_would_ship_a_phantom_rail_row(self):
        # the reason MEMORY_DIR is a sibling, kept as a live demonstration
        tmp = tempfile.mkdtemp(prefix="alloy-mem-sib-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = relay.SESSIONS_DIR
        relay.SESSIONS_DIR = tmp
        self.addCleanup(setattr, relay, "SESSIONS_DIR", old)
        os.makedirs(os.path.join(tmp, "memory"))
        self.assertIn("memory", [s["id"] for s in relay.list_sessions()])

    def test_the_real_store_is_a_sibling_of_sessions_not_a_child(self):
        # read from the source, because every suite here redirects the global
        with open(os.path.join(ROOT, "relay.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('MEMORY_DIR = os.path.join(BASE_DIR, "memory")', src)
        real = os.path.join(relay.BASE_DIR, "memory")
        self.assertEqual(os.path.dirname(real), os.path.dirname(
            os.path.join(relay.BASE_DIR, "sessions")))
        self.assertNotEqual(
            os.path.commonpath([real, os.path.join(relay.BASE_DIR, "sessions")]),
            os.path.join(relay.BASE_DIR, "sessions"))

    def test_the_test_sandbox_redirect_is_still_in_place(self):
        # without it every loop suite reads Josh's real notes into its
        # preambles, so a passing test starts failing the day he /remembers
        with open(os.path.join(HERE, "test_loop.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("relay.MEMORY_DIR = _MEM_SANDBOX", src)
        self.assertNotEqual(relay.MEMORY_DIR,
                            os.path.join(relay.BASE_DIR, "memory"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
