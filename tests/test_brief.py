"""Token-free tests for the shared project context (relay's brief layer).

The bug being fixed: every seat runs with cwd = the working folder, and each
CLI auto-loads only its OWN doc there (claude -> CLAUDE.md, codex -> AGENTS.md,
agy -> AGENTS.md/GEMINI.md). Point a chat at a repo holding only CLAUDE.md and
the Claude seat arrives with the whole project in context while the others
arrive blind, with nothing in the transcript saying so.

No CLI is invoked: the small-docs path is deterministic by design, and the
synthesis path is driven by a stubbed relay.synthesize_brief.

Run:  python tests/test_brief.py
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import LoopIO, run_rounds

from test_loop import FakeAgent, RecordingIO, build_state, saved_meta


def write(folder, name, text):
    with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
        f.write(text)


class BriefTestCase(unittest.TestCase):
    """A project workspace that is NOT inside sessions/, plus a session dir —
    which is what makes session_project() report a custom folder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ws = os.path.join(self.tmp, "widget-factory")
        self.sd = os.path.join(self.tmp, "session")
        os.makedirs(self.ws)
        os.makedirs(self.sd)
        self._real_synth = relay.synthesize_brief

    def tearDown(self):
        relay.synthesize_brief = self._real_synth
        shutil.rmtree(self.tmp, ignore_errors=True)

    def stub_synth(self, body="A large project about widgets."):
        calls = []

        def fake(workspace, docs, spec=None):
            calls.append([d["name"] for d in docs])
            if isinstance(body, BaseException):
                raise body
            return body
        relay.synthesize_brief = fake
        return calls


class DiscoveryTests(BriefTestCase):

    def test_every_adapter_doc_is_scanned(self):
        # The scan set is a fixed list (so it does not shrink when a provider
        # is unseated or unimplemented) while the per-seat "you already load
        # this" line comes from the adapters. This guard is what keeps the two
        # from drifting apart.
        names = relay.project_doc_names()
        for provider, cls in ((p, c) for p, c in relay.AGENT_TYPES.items()):
            for doc in getattr(cls, "project_docs", ()):
                self.assertIn(doc, names, f"{provider} reads {doc}")
        self.assertIn("CLAUDE.md", names)     # claude
        self.assertIn("AGENTS.md", names)     # codex
        self.assertIn("GEMINI.md", names)     # agy
        self.assertIn("README.md", names)     # extra

    def test_scan_set_survives_an_unregistered_provider(self):
        # a GPT-only chat in a repo with CLAUDE.md must still quote it
        real = dict(relay.AGENT_TYPES)
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES["gpt"] = real["gpt"]
        try:
            write(self.ws, "CLAUDE.md", "tin")
            self.assertEqual([d["name"]
                              for d in relay.find_context_docs(self.ws)],
                             ["CLAUDE.md"])
        finally:
            relay.AGENT_TYPES.clear()
            relay.AGENT_TYPES.update(real)

    def test_finds_top_level_docs_in_fixed_order(self):
        write(self.ws, "README.md", "readme")
        write(self.ws, "CLAUDE.md", "claude")
        self.assertEqual([d["name"] for d in relay.find_context_docs(self.ws)],
                         ["CLAUDE.md", "README.md"])

    def test_does_not_recurse_into_subdirectories(self):
        sub = os.path.join(self.ws, "docs")
        os.makedirs(sub)
        write(sub, "CLAUDE.md", "nested")
        self.assertEqual(relay.find_context_docs(self.ws), [])

    def test_never_treats_its_own_output_as_a_source(self):
        write(self.ws, relay.BRIEF_NAME, "our own brief")
        self.assertEqual(relay.find_context_docs(self.ws), [])

    def test_oversize_doc_is_recorded_not_read(self):
        write(self.ws, "CLAUDE.md", "x" * 200)
        real = relay.BRIEF_READ_MAX
        relay.BRIEF_READ_MAX = 50
        try:
            doc, = relay.find_context_docs(self.ws)
        finally:
            relay.BRIEF_READ_MAX = real
        self.assertIn("too large", doc["error"])
        self.assertEqual(doc["text"], "")
        # kept, not dropped: the block has to be able to say it exists
        self.assertIn("could not be quoted", relay.quote_docs([doc]))

    def test_brief_path_refuses_to_escape_the_workspace(self):
        real = relay.BRIEF_NAME
        relay.BRIEF_NAME = os.path.join("..", "escaped.md")
        try:
            with self.assertRaises(ValueError):
                relay.brief_path(self.ws)
        finally:
            relay.BRIEF_NAME = real


class FingerprintTests(BriefTestCase):

    def test_content_change_is_detected_mtime_change_is_not(self):
        write(self.ws, "CLAUDE.md", "one")
        before, = relay.find_context_docs(self.ws)
        os.utime(os.path.join(self.ws, "CLAUDE.md"), (0, 0))
        same, = relay.find_context_docs(self.ws)
        # a git checkout / cloud sync moves mtime with identical bytes; keying
        # off it would churn a rebuild (and a CLI call) for no reason
        self.assertEqual(before["sha256"], same["sha256"])
        write(self.ws, "CLAUDE.md", "two")
        after, = relay.find_context_docs(self.ws)
        self.assertNotEqual(before["sha256"], after["sha256"])

    def test_status_walks_none_missing_fresh_stale(self):
        self.assertEqual(relay.brief_status(self.ws)[0], "none")
        write(self.ws, "CLAUDE.md", "hello")
        self.assertEqual(relay.brief_status(self.ws)[0], "missing")
        relay.write_brief(self.ws, "a brief", relay.find_context_docs(self.ws))
        self.assertEqual(relay.brief_status(self.ws)[0], "fresh")
        write(self.ws, "CLAUDE.md", "changed")
        status, _, changed = relay.brief_status(self.ws)
        self.assertEqual((status, changed), ("stale", ["CLAUDE.md"]))

    def test_unverifiable_brief_is_stale_not_trusted(self):
        write(self.ws, "CLAUDE.md", "hello")
        write(self.ws, relay.BRIEF_NAME, "hand-written, no fingerprints")
        self.assertEqual(relay.brief_status(self.ws)[0], "missing")

    def test_round_trip_through_the_file(self):
        write(self.ws, "CLAUDE.md", "hello")
        docs = relay.find_context_docs(self.ws)
        relay.write_brief(self.ws, "the prose", docs)
        self.assertEqual(relay.read_brief(self.ws), "the prose")
        with open(relay.brief_path(self.ws), encoding="utf-8") as f:
            self.assertEqual(relay.brief_fingerprints(f.read()),
                             {"CLAUDE.md": docs[0]["sha256"]})

    def test_generated_file_says_so_without_telling_the_seats(self):
        write(self.ws, "CLAUDE.md", "hello")
        relay.write_brief(self.ws, "the prose", relay.find_context_docs(self.ws))
        with open(relay.brief_path(self.ws), encoding="utf-8") as f:
            raw = f.read()
        # a human opening it in the repo sees the warning at the top...
        self.assertIn("Generated file — do not edit.", raw)
        self.assertLess(raw.index("Generated file"), raw.index("the prose"))
        # ...and none of our bookkeeping reaches a preamble
        prose = relay.read_brief(self.ws)
        self.assertEqual(prose, "the prose")
        self.assertNotIn("ai-chat:", prose)

    def test_drift_reports_changed_added_and_removed(self):
        write(self.ws, "CLAUDE.md", "hello")
        brief = relay.project_brief(self.ws, self.sd)
        self.assertEqual(relay.brief_drift(brief, self.ws), [])
        write(self.ws, "CLAUDE.md", "edited")
        write(self.ws, "AGENTS.md", "new")
        self.assertEqual(relay.brief_drift(brief, self.ws),
                         ["CLAUDE.md changed", "AGENTS.md added"])
        os.remove(os.path.join(self.ws, "CLAUDE.md"))
        self.assertIn("CLAUDE.md removed", relay.brief_drift(brief, self.ws))


class VerbatimTests(BriefTestCase):

    def test_small_docs_are_quoted_verbatim_with_no_cli_call(self):
        calls = self.stub_synth()
        write(self.ws, "CLAUDE.md", "Widgets are made of tin.")
        brief = relay.project_brief(self.ws, self.sd)
        self.assertEqual((brief["status"], brief["mode"]),
                         ("quoted", "verbatim"))
        self.assertEqual(calls, [])                 # nothing spent
        self.assertIn("Widgets are made of tin.", brief["quotes"])
        # and nothing was written into Josh's project folder
        self.assertEqual(os.listdir(self.ws), ["CLAUDE.md"])

    def test_every_seat_gets_the_same_bytes(self):
        write(self.ws, "CLAUDE.md", "Widgets are made of tin.")
        brief = relay.project_brief(self.ws, self.sd)
        blocks = [relay.brief_preamble_block(brief, cls)
                  for cls in (relay.ClaudeAgent, relay.CodexAgent,
                              relay.GeminiAgent)]
        for block in blocks:
            self.assertIn("Widgets are made of tin.", block)

    def test_each_seat_is_told_which_docs_it_already_loads(self):
        write(self.ws, "CLAUDE.md", "tin")
        brief = relay.project_brief(self.ws, self.sd)
        self.assertIn("already load CLAUDE.md",
                      relay.brief_preamble_block(brief, relay.ClaudeAgent))
        # codex reads AGENTS.md, which this folder does not have
        self.assertIn("loads none of these",
                      relay.brief_preamble_block(brief, relay.CodexAgent))

    def test_identical_docs_are_quoted_once(self):
        write(self.ws, "CLAUDE.md", "same bytes")
        write(self.ws, "AGENTS.md", "same bytes")
        quotes = relay.project_brief(self.ws, self.sd)["quotes"]
        self.assertEqual(quotes.count("same bytes"), 1)
        self.assertIn("byte-identical to", quotes)

    def test_truncation_is_always_declared_and_budget_is_enforced(self):
        write(self.ws, "CLAUDE.md", "a" * 50_000)
        write(self.ws, "AGENTS.md", "b" * 50_000)
        quotes = relay.quote_docs(relay.find_context_docs(self.ws))
        self.assertLessEqual(len(quotes), relay.BRIEF_MAX + 500)  # + headers
        self.assertIn("TRUNCATED", quotes)

    def test_quoting_is_deterministic(self):
        write(self.ws, "CLAUDE.md", "x" * 9000)
        write(self.ws, "README.md", "y" * 9000)
        a = relay.quote_docs(relay.find_context_docs(self.ws))
        b = relay.quote_docs(relay.find_context_docs(self.ws))
        self.assertEqual(a, b)


class SynthesisTests(BriefTestCase):

    def big(self, name="CLAUDE.md"):
        write(self.ws, name, "z" * (relay.BRIEF_MAX + 1000))

    def test_large_docs_synthesize_once_then_cache(self):
        calls = self.stub_synth("Widgets, at length.")
        self.big()
        first = relay.project_brief(self.ws, self.sd)
        self.assertEqual((first["status"], first["mode"]),
                         ("written", "synthesized"))
        self.assertTrue(os.path.exists(relay.brief_path(self.ws)))
        second = relay.project_brief(self.ws, self.sd)
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(len(calls), 1)         # cached, not re-synthesized

    def test_changed_source_regenerates_and_announces(self):
        self.stub_synth()
        self.big()
        relay.project_brief(self.ws, self.sd)
        write(self.ws, "CLAUDE.md", "q" * (relay.BRIEF_MAX + 1000))
        notes = []
        again = relay.project_brief(self.ws, self.sd, on_status=notes.append)
        self.assertEqual(again["status"], "updated")
        self.assertTrue(any("CLAUDE.md changed" in n for n in notes))

    def test_failure_is_declared_never_fabricated(self):
        self.stub_synth(RuntimeError("cli exploded"))
        self.big()
        brief = relay.project_brief(self.ws, self.sd)
        self.assertEqual(brief["status"], "failed")
        block = relay.brief_preamble_block(brief, relay.ClaudeAgent)
        self.assertIn("could not build", block)
        self.assertIn("do not assume the others know", block)
        self.assertEqual(brief["digest"], "")   # nothing invented
        self.assertFalse(os.path.exists(relay.brief_path(self.ws)))

    def test_empty_reply_counts_as_failure(self):
        self.stub_synth("")
        self.big()
        self.assertEqual(relay.project_brief(self.ws, self.sd)["status"],
                         "failed")

    def test_unsaveable_brief_is_still_used_but_says_so(self):
        self.stub_synth("Widgets, at length.")
        self.big()
        real = relay._atomic_write

        def boom(path, text):
            raise OSError("folder is read-only")
        relay._atomic_write = boom
        try:
            brief = relay.project_brief(self.ws, self.sd)
        finally:
            relay._atomic_write = real
        self.assertEqual(brief["status"], "readonly")
        self.assertIn("Widgets, at length.", brief["digest"])
        self.assertIn("could not be saved",
                      relay.brief_preamble_block(brief, relay.ClaudeAgent))


class GateTests(BriefTestCase):

    def test_default_in_session_workspace_is_left_alone(self):
        # the reason all the pre-existing suites are unaffected
        sd = os.path.join(relay.SESSIONS_DIR, "20990101-000000-x")
        brief = relay.project_brief(os.path.join(sd, "workspace"), sd)
        self.assertEqual(brief["status"], "off")
        self.assertEqual(relay.brief_preamble_block(brief), "")

    def test_opt_out_spends_nothing_and_says_nothing(self):
        write(self.ws, "CLAUDE.md", "tin")
        brief = relay.project_brief(self.ws, self.sd, enabled=False)
        self.assertEqual(brief["status"], "off")
        self.assertEqual(relay.brief_preamble_block(brief), "")

    def test_folder_with_no_docs_says_so_without_a_call(self):
        calls = self.stub_synth()
        brief = relay.project_brief(self.ws, self.sd)
        self.assertEqual(brief["status"], "none")
        self.assertEqual(calls, [])
        self.assertIn("no AI instruction docs",
                      relay.brief_preamble_block(brief, relay.ClaudeAgent))


class PreambleTests(BriefTestCase):

    def test_no_brief_leaves_the_preamble_byte_identical(self):
        a = FakeAgent(self.ws, [], name="A")
        b = FakeAgent(self.ws, [], name="B")
        text = relay.preamble(a, [b], "t", 3, self.ws)
        self.assertIn("- You share a scratch workspace (your current "
                      "directory) with the other participant(s) -- you may "
                      "read/write files there if useful, e.g. to co-write a "
                      "document.", text)
        self.assertNotIn("Project context", text)

    def test_a_real_project_folder_is_not_called_scratch(self):
        write(self.ws, "CLAUDE.md", "tin")
        brief = relay.project_brief(self.ws, self.sd)
        a = FakeAgent(self.ws, [], name="A")
        b = FakeAgent(self.ws, [], name="B")
        text = relay.preamble(a, [b], "t", 3, self.ws, brief=brief)
        self.assertNotIn("scratch workspace", text)
        self.assertIn("real project folder", text)
        self.assertIn(os.path.abspath(self.ws), text)
        self.assertIn("do not create, edit or delete files", text)
        self.assertIn("never quote credentials", text)


class LoopIntegrationTests(BriefTestCase):

    def run_chat(self, brief, turns=2, human=None):
        state = build_state(self.tmp, [["hi"] * 4, ["yo"] * 4], turns=turns,
                            workspace=self.ws, brief=brief)
        io = RecordingIO(human)
        run_rounds(state, io)
        return state, io

    def test_the_block_reaches_every_seat_not_just_claude(self):
        write(self.ws, "CLAUDE.md", "Widgets are made of tin.")
        brief = relay.project_brief(self.ws, self.sd)
        state, _ = self.run_chat(brief)
        for agent in state["agents"]:
            self.assertIn("Widgets are made of tin.", agent.prompts[0])

    def test_context_survives_clear(self):
        # /clear resets introduced[], so the preamble is the only channel that
        # comes back — a pending[] note would have evaporated
        write(self.ws, "CLAUDE.md", "Widgets are made of tin.")
        brief = relay.project_brief(self.ws, self.sd)
        state, _ = self.run_chat(brief, turns=3, human=[[], ["/clear"], []])
        reintroduced = [a for a in state["agents"] if len(a.prompts) > 1]
        self.assertTrue(reintroduced, "no seat spoke after the /clear")
        self.assertTrue(any("Widgets are made of tin." in p
                            for a in reintroduced for p in a.prompts[1:]))

    def test_meta_records_provenance_and_the_sidecar_holds_the_text(self):
        write(self.ws, "CLAUDE.md", "Widgets are made of tin.")
        brief = relay.project_brief(self.ws, self.sd)
        state, _ = self.run_chat(brief)
        relay.write_project_context(state["store"].dir, brief)
        rec = saved_meta(state)["brief"]
        self.assertEqual(rec["mode"], "verbatim")
        self.assertEqual(rec["sources"], ["CLAUDE.md"])
        # the injected text is NOT in meta (save runs after every turn)
        self.assertNotIn("Widgets are made of tin.", str(rec))
        with open(os.path.join(state["store"].dir,
                               relay.PROJECT_CONTEXT_FILE),
                  encoding="utf-8") as f:
            self.assertIn("Widgets are made of tin.", f.read())

    def test_resume_replays_the_recorded_text_not_the_changed_file(self):
        write(self.ws, "CLAUDE.md", "Widgets are made of tin.")
        brief = relay.project_brief(self.ws, self.sd)
        state, _ = self.run_chat(brief)
        relay.write_project_context(state["store"].dir, brief)
        meta = saved_meta(state)
        write(self.ws, "CLAUDE.md", "Widgets are made of PLASTIC.")
        restored = relay.read_project_context(state["store"].dir, meta)
        self.assertIn("tin", restored["quotes"])
        self.assertNotIn("PLASTIC", restored["quotes"])
        # the change is reported instead of silently swapped in
        self.assertEqual(relay.brief_drift(restored, self.ws),
                         ["CLAUDE.md changed"])

    def test_no_brief_means_no_project_text_anywhere(self):
        state, _ = self.run_chat(None)
        for agent in state["agents"]:
            self.assertNotIn("Project context", agent.prompts[0])
        self.assertIsNone(saved_meta(state)["brief"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
