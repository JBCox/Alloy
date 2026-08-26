"""Cross-chat search: relay.search_sessions and the app bridge method.

Token-free: every chat here is a hand-written meta.json plus real
SessionStore rows, scanned by the REAL search. Guards the ranking rules,
the bounds, the legacy-transcript fallback, and the bridge's error shape.

Run:  python tests/test_search.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
import app


def make_chat(tmp, name, title, rows=(), transcript_lines=(), meta=None):
    """One session folder the way the app really leaves it on disk."""
    d = os.path.join(tmp, name)
    os.makedirs(d, exist_ok=True)
    if meta is not None:
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(dict(meta, title=title), f, ensure_ascii=False)
    if rows:
        store = relay.SessionStore(d)
        for name_, text, origin in rows:
            speaker = {"josh": "josh", "system": "system"}.get(origin, name_.lower())
            env = {"origin": origin} if origin else None
            store.record(name_, text, speaker=speaker,
                         envelope={"origin": origin} if origin else None)
    for line in transcript_lines:
        with open(os.path.join(d, "transcript.md"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    return d


class SearchSessionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-search-test-")
        self._old_dir = relay.SESSIONS_DIR
        relay.SESSIONS_DIR = self.tmp

    def tearDown(self):
        relay.SESSIONS_DIR = self._old_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- matching -------------------------------------------------------
    def test_finds_words_inside_message_rows(self):
        make_chat(self.tmp, "chat-a", "Rivers", rows=[
            ("Claude", "The Nile carries sediment to its delta.", "seat"),
            ("GPT", "What about the Amazon basin?", "seat"),
        ])
        out = relay.search_sessions("sediment")
        self.assertEqual([c["id"] for c in out["chats"]], ["chat-a"])
        hit = out["chats"][0]
        self.assertEqual(hit["count"], 1)
        self.assertEqual(len(hit["snippets"]), 1)
        self.assertIn("sediment", hit["snippets"][0]["excerpt"].lower())
        self.assertEqual(hit["snippets"][0]["name"], "Claude")

    def test_case_and_whitespace_do_not_block_a_match(self):
        make_chat(self.tmp, "chat-a", "Notes", rows=[
            ("Josh", "Mixed CASE words here.", "human"),
        ])
        self.assertEqual(relay.search_sessions("case words")["chats"][0]["count"], 1)
        self.assertEqual(relay.search_sessions("  MIXED   case  ")["chats"][0]["count"], 1)

    def test_every_occurrence_is_counted(self):
        make_chat(self.tmp, "chat-a", "Echoes", rows=[
            ("Claude", "echo echo echo", "seat"),
            ("GPT", "one echo more", "seat"),
        ])
        self.assertEqual(relay.search_sessions("echo")["chats"][0]["count"], 4)

    def test_title_match_found_without_body_hit(self):
        make_chat(self.tmp, "chat-a", "Harbour Lights", rows=[
            ("Claude", "Nothing nautical in here at all.", "seat"),
        ], meta={"v": 2})
        out = relay.search_sessions("harbour")
        self.assertTrue(out["chats"][0]["title_match"])
        self.assertEqual(out["chats"][0]["count"], 0)

    def test_relay_service_notes_are_not_content(self):
        make_chat(self.tmp, "chat-a", "Chatter", rows=[
            ("relay", "Reopened — the agents still remember this chat.", "relay"),
            ("Claude", "actual words", "seat"),
        ])
        self.assertEqual(relay.search_sessions("reopened")["chats"], [])
        self.assertEqual(relay.search_sessions("actual words")["chats"][0]["count"], 1)

    def test_legacy_transcript_only_chats_are_searched_too(self):
        make_chat(self.tmp, "legacy-one", "",   # no meta.json at all
                  transcript_lines=["## Claude said something about lighthouses"])
        out = relay.search_sessions("lighthouses")
        self.assertEqual([c["id"] for c in out["chats"]], ["legacy-one"])
        self.assertEqual(out["chats"][0]["count"], 1)

    def test_rows_present_but_silent_means_no_transcript_rescan(self):
        """A chat with parseable rows never falls back to its transcript:
        the fallback exists for chats WITHOUT rows, and re-scanning would
        re-find exactly the relay furniture the rows filtered out."""
        make_chat(self.tmp, "chat-a", "Tide tables",
                  rows=[("Claude", "irrelevant", "seat")],
                  transcript_lines=["but the transcript mentions a boathook"])
        self.assertEqual(relay.search_sessions("boathook")["chats"], [])

    def test_all_rows_corrupt_falls_back_to_the_transcript(self):
        d = make_chat(self.tmp, "chat-a", "Wrecked rows",
                      transcript_lines=["the only intact copy mentions a boathook"])
        with open(os.path.join(d, relay.SESSION_MSGS), "w", encoding="utf-8") as f:
            f.write('{"speaker": "claude", "text": NOPE\n')
        out = relay.search_sessions("boathook")
        self.assertEqual([c["id"] for c in out["chats"]], ["chat-a"])

    def test_degraded_log_merges_the_transcript_back_in(self):
        """F2: ONE parseable row beside corrupt lines must not silence the
        transcript fallback — a mid-write crash leaves exactly this mixed
        shape, and its transcript-only words must stay findable."""
        d = make_chat(self.tmp, "chat-a", "Mixed wreck",
                      rows=[("Claude", "an intact row about tides", "seat")],
                      transcript_lines=["ghostword survives only here"])
        with open(os.path.join(d, relay.SESSION_MSGS), "a", encoding="utf-8") as f:
            f.write('{"speaker": "claude", "text": NOPE\n')   # torn mid-write
            f.write("not json at all\n")                       # garbage line
        hit = relay.search_sessions("ghostword")["chats"]
        self.assertEqual([c["id"] for c in hit], ["chat-a"],
                         "corruption beside rows hid the transcript")
        # the intact words still surface — via the mirrored transcript,
        # counted ONCE (replace semantics; merging would double-count,
        # since transcript.md mirrors every recorded message)
        self.assertEqual(
            relay.search_sessions("intact row")["chats"][0]["count"], 1)

    def test_oversized_line_is_searched_before_the_budget_stops(self):
        """F3: the byte bound must not skip a line BEFORE looking inside
        it — an oversized message keeps its chance, and scanning stops
        after it rather than silently eating the rest of the file."""
        make_chat(self.tmp, "big", "Big line",
                  transcript_lines=["needle-in-the-deep " * 40000])
        hit = relay.search_sessions("needle-in-the-deep")["chats"]
        self.assertGreaterEqual(hit[0]["count"], 1,
                                "the oversized line itself was skipped")

    def test_scanning_still_stops_after_the_budget_buster(self):
        """The F3 fix relaxes WHERE the line lands, not the bound: nothing
        after an oversized line is scanned."""
        make_chat(self.tmp, "tail", "Tail only",
                  transcript_lines=["x" * 300000,
                                    "needle only here"])
        self.assertEqual(relay.search_sessions("needle only here")["chats"], [])

    def test_excerpt_survives_casefold_expansion(self):
        """F1: match positions live in the casefolded text, so the excerpt
        window is cut from the same string — ß→ss can no longer slide the
        window off its own hit."""
        make_chat(self.tmp, "chat-a", "Straße", rows=[
            ("Claude", "ß" * 100 + "TARGET-7f3a", "seat")])
        snip = relay.search_sessions("target-7f3a")["chats"][0]["snippets"][0]
        self.assertIn("target-7f3a", snip["excerpt"].casefold())

    # ---- ranking & bounds -------------------------------------------------
    def test_title_matches_rank_above_body_counts(self):
        make_chat(self.tmp, "many-hits", "Unrelated title", rows=[
            ("Claude", "delta delta delta delta delta", "seat"),
        ], meta={"v": 2})
        make_chat(self.tmp, "title-only", "Delta works", rows=[
            ("GPT", "no deltas here", "seat"),
        ], meta={"v": 2})
        order = [c["id"] for c in relay.search_sessions("delta")["chats"]]
        self.assertEqual(order, ["title-only", "many-hits"])

    def test_more_hits_rank_higher_within_the_same_class(self):
        make_chat(self.tmp, "two", "A", rows=[("C", "tide tide", "seat")], meta={"v": 2})
        make_chat(self.tmp, "five", "B", rows=[("G", "tide " * 5, "seat")], meta={"v": 2})
        order = [c["id"] for c in relay.search_sessions("tide")["chats"]]
        self.assertEqual(order, ["five", "two"])

    def test_snippets_capped_at_three_per_chat(self):
        make_chat(self.tmp, "chat-a", "Many", rows=[
            (f"Seat{n}", f"mention {n} of the thing", "seat") for n in range(6)])
        hit = relay.search_sessions("thing")["chats"][0]
        self.assertEqual(hit["count"], 6)
        self.assertEqual(len(hit["snippets"]), 3)

    def test_result_chats_capped_with_truncation_flagged(self):
        for n in range(relay.SEARCH_CHATS_MAX + 5):
            make_chat(self.tmp, f"chat-{n:03d}", f"Needle {n}",
                      rows=[("Claude", "needle here", "seat")], meta={"v": 2})
        out = relay.search_sessions("needle")
        self.assertEqual(len(out["chats"]), relay.SEARCH_CHATS_MAX)
        self.assertTrue(out["truncated"])
        self.assertGreaterEqual(
            sum(c["count"] + c["title_match"] for c in out["chats"]),
            relay.SEARCH_CHATS_MAX)

    def test_empty_and_tiny_queries_scan_nothing(self):
        make_chat(self.tmp, "chat-a", "Anything", rows=[("Claude", "words", "seat")])
        for q in ("", "   ", "x"):
            out = relay.search_sessions(q)
            self.assertEqual(out["chats"], [])
            self.assertFalse(out["truncated"])

    def test_empty_and_none_queries_report_a_clean_empty_payload(self):
        """No query means no answer — but the shape stays honest: the query is
        echoed back untouched (None → "") and nothing else is implied."""
        make_chat(self.tmp, "chat-a", "Anything", rows=[("Claude", "words", "seat")])
        for q in ("", " \t\n ", None):
            out = relay.search_sessions(q)
            self.assertEqual(out["chats"], [])
            self.assertIs(out["truncated"], False)
        self.assertEqual(relay.search_sessions("")["query"], "")
        self.assertEqual(relay.search_sessions(None)["query"], "")
        # the empty path echoes the ORIGINAL query untouched (only a real
        # search collapses whitespace into its returned needle)
        self.assertEqual(relay.search_sessions("  ")["query"], "  ")

    def test_unicode_titles_match_case_insensitively(self):
        make_chat(self.tmp, "chat-a", "Café Strategy", rows=[
            ("Claude", "nothing relevant here", "seat")], meta={"v": 2})
        for q in ("café", "CAFÉ", "Café"):
            hit = relay.search_sessions(q)["chats"][0]
            self.assertTrue(hit["title_match"], q)
            self.assertEqual(hit["count"], 0)

    def test_unicode_titles_beyond_latin_still_rank_first(self):
        title = "東京レビュー ☕"
        make_chat(self.tmp, "cjk", title, rows=[
            ("Claude", "plain words only", "seat")], meta={"v": 2})
        make_chat(self.tmp, "latin", "Unrelated", rows=[
            ("GPT", "mentions 東京レビュー once", "seat")], meta={"v": 2})
        out = relay.search_sessions("東京レビュー")
        ids = [c["id"] for c in out["chats"]]
        self.assertIn("cjk", ids)
        self.assertEqual(ids[0], "cjk",
                         "a unicode TITLE match must outrank a body-count match")
        hit = next(c for c in out["chats"] if c["id"] == "cjk")
        self.assertTrue(hit["title_match"])
        self.assertTrue(hit["title"] == title,
                        "the original title text survives the round trip")
        # an emoji alone is a legitimate needle too
        self.assertTrue(relay.search_sessions("☕")["chats"][0]["title_match"])

    def test_unicode_body_words_are_found_verbatim(self):
        make_chat(self.tmp, "chat-a", "Notes", rows=[
            ("Claude", "a naïve résumé of 東京タワー heights", "seat"),
            ("GPT", "naïve repetition of naïve ideas", "seat"),
        ])
        self.assertEqual(relay.search_sessions("naïve")["chats"][0]["count"], 3)
        self.assertEqual(
            relay.search_sessions("résumé")["chats"][0]["snippets"][0]["name"],
            "Claude")
        self.assertEqual(relay.search_sessions("東京タワー")["chats"][0]["count"], 1)

    def test_no_match_reports_clean_zero(self):
        make_chat(self.tmp, "chat-a", "Quiet", rows=[("Claude", "calm", "seat")])
        out = relay.search_sessions("zzz-not-there")
        self.assertEqual(out["chats"], [])
        self.assertFalse(out["truncated"])

    def test_missing_sessions_directory_is_not_fatal(self):
        relay.SESSIONS_DIR = os.path.join(self.tmp, "does-not-exist")
        try:
            self.assertEqual(relay.search_sessions("anything")["chats"], [])
        finally:
            relay.SESSIONS_DIR = self.tmp

    def test_corrupt_jsonl_line_does_not_kill_the_scan(self):
        d = make_chat(self.tmp, "chat-a", "Broken", rows=[
            ("Claude", "good words survive", "seat")])
        with open(os.path.join(d, relay.SESSION_MSGS), "a", encoding="utf-8") as f:
            f.write('{"speaker": "claude", "text": NOPE\n')
        self.assertEqual(relay.search_sessions("survive")["chats"][0]["count"], 1)


class SearchBridgeTests(unittest.TestCase):
    """app.Api.search_sessions: same sandboxing discipline as the tabs tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-search-bridge-")
        self._old = app.SESSIONS_DIR
        app.SESSIONS_DIR = self.tmp
        self._old_relay_dir = relay.SESSIONS_DIR
        relay.SESSIONS_DIR = self.tmp

    def tearDown(self):
        app.SESSIONS_DIR = self._old
        relay.SESSIONS_DIR = self._old_relay_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bridge_returns_engine_payload(self):
        make_chat(self.tmp, "chat-a", "Findable", rows=[
            ("Claude", "the trefoil glows", "seat")], meta={"v": 2})
        api = app.Api()
        out = api.search_sessions("trefoil")
        self.assertEqual([c["id"] for c in out["chats"]], ["chat-a"])

    def test_bridge_never_raises_on_bad_input(self):
        api = app.Api()
        for bad in (None, 12345, ["list"], {"dict"}):
            out = api.search_sessions(bad)
            self.assertIsInstance(out, dict)
            self.assertTrue("chats" in out or "error" in out,
                            "garbage query produced neither result nor error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
