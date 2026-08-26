"""Tests for the /files slash command (workspace artifact listing).

Token-free — real files in a temp workspace, FakeAgents only.
Run:  python tests/test_files_command.py
"""

import datetime
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from test_loop import RecordingIO, build_state, jsonl_rows

OLD = 1_700_000_000            # fixed past stamp -> the date-formatted branch
MID = OLD + 100
NEW = OLD + 200


def touch(ws, rel, body=b"x", mtime=None):
    """Create one file under ws with a controlled mtime."""
    path = os.path.join(ws, rel.replace("/", os.sep))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write(body)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def status_texts(io):
    return [p.get("text", "") for e, p in io.events if e == "status"]


def listing_lines(note):
    """Just the '  path · size · when' rows of a listing note."""
    return [ln.strip() for ln in note.splitlines()
            if ln.startswith("  ")]


class FilesCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-files-")
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(self.ws)
        self.state = build_state(self.tmp, [["a"], ["b"]], turns=1,
                                 workspace=self.ws)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cmd(self, text):
        io = RecordingIO()
        stopped = relay.dispatch_command(self.state, text, io)
        return io, stopped, status_texts(io)

    def system_notes(self):
        return [r["text"] for r in jsonl_rows(self.state)
                if r.get("speaker") == "system"]

    # ------------------------------------------------------------ basics --
    def test_empty_workspace_says_so_and_persists_the_note(self):
        io, stopped, texts = self.cmd("/files")
        self.assertFalse(stopped)          # like every other non-stop command
        self.assertIn("empty so far", texts[0])
        self.assertIn(texts[0], self.system_notes())

    def test_listing_newest_first_with_sizes_and_times(self):
        touch(self.ws, "old.txt", b"a" * 10, mtime=OLD)
        touch(self.ws, "mid.log", b"c" * 20480, mtime=MID)
        touch(self.ws, "new/report.md", b"b" * 300000, mtime=NEW)
        _, _, texts = self.cmd("/files")
        note = texts[0]
        # the store keeps its rows OUT of the workspace, so exactly three
        self.assertIn("Workspace files (3, newest first):", note)
        lines = listing_lines(note)
        rel_new = os.path.join("new", "report.md")
        self.assertEqual([ln.split(" · ")[0] for ln in lines],
                         [rel_new, "mid.log", "old.txt"])
        # sizes mirror the UI's fmtSize shapes (1 decimal under 10 KB,
        # whole KB above — 20480 B renders "20 KB" in the rail too)
        self.assertIn("20 KB", lines[1])
        self.assertIn("293 KB", lines[0])
        self.assertIn("10 B", lines[2])
        # a past-date stamp carries the date, not just HH:MM
        self.assertIn(datetime.datetime.fromtimestamp(NEW)
                      .strftime("%Y-%m-%d %H:%M"), lines[0])
        # the snapshot lands in the durable record for reopened chats
        self.assertTrue(any("Workspace files" in n for n in self.system_notes()))

    # -------------------------------------------------------- boundaries --
    def test_junk_and_hidden_dirs_are_skipped_entirely(self):
        touch(self.ws, "real.txt", mtime=OLD)
        touch(self.ws, ".git/config", mtime=NEW)
        touch(self.ws, "node_modules/pkg/index.js", mtime=NEW)
        touch(self.ws, "__pycache__/m.pyc", mtime=NEW)
        touch(self.ws, ".venv/pyvenv.cfg", mtime=NEW)
        _, _, texts = self.cmd("/files")
        self.assertIn("Workspace files (1, newest first):", texts[0])
        self.assertIn("real.txt", texts[0])

    def test_limit_argument_cuts_rows_but_counts_everything(self):
        for i in range(5):
            touch(self.ws, f"f{i}.txt", mtime=OLD + i)
        _, _, texts = self.cmd("/files 2")
        note = texts[0]
        self.assertIn("(2 of 5, newest first):", note)
        names = [ln.split(" · ")[0] for ln in listing_lines(note)]
        self.assertEqual(names, ["f4.txt", "f3.txt"])

    def test_default_and_max_limits_are_enforced(self):
        n = relay.FILES_MAX_LIMIT + 3
        for i in range(n):
            touch(self.ws, f"f{i:02d}.txt", mtime=OLD + i)
        _, _, texts = self.cmd("/files")
        self.assertIn(f"({relay.FILES_DEFAULT_LIMIT} of {n},",
                      texts[0])
        self.assertEqual(len(listing_lines(texts[0])),
                         relay.FILES_DEFAULT_LIMIT)
        _, _, texts = self.cmd(f"/files {relay.FILES_MAX_LIMIT * 10}")
        self.assertIn(f"({relay.FILES_MAX_LIMIT} of {n},", texts[0])
        self.assertEqual(len(listing_lines(texts[0])),
                         relay.FILES_MAX_LIMIT)

    def test_zero_clamps_to_one_row(self):
        touch(self.ws, "a.txt", mtime=OLD)
        touch(self.ws, "b.txt", mtime=NEW)
        _, _, texts = self.cmd("/files 0")
        self.assertEqual(len(listing_lines(texts[0])), 1)
        self.assertIn("b.txt", texts[0])       # still newest-first

    def test_non_numeric_argument_is_a_usage_note(self):
        _, _, texts = self.cmd("/files soon")
        self.assertIn("Usage: /files [N]", texts[0])
        self.assertNotIn("Workspace files", texts[0])

    # ------------------------------------------------------------ safety --
    def test_missing_workspace_folder_is_reported_not_raised(self):
        # The engine always carries a workspace key; the real edge case is
        # one whose folder was deleted (or never created) on disk mid-run.
        self.state["workspace"] = os.path.join(self.tmp, "gone")
        _, _, texts = self.cmd("/files")
        self.assertIn("No working folder to list.", texts[0])

    def test_walk_budget_stops_the_scan_and_says_so(self):
        self.addCleanup(setattr, relay, "FILES_WALK_MAX",
                        relay.FILES_WALK_MAX)
        relay.FILES_WALK_MAX = 3
        for i in range(6):
            touch(self.ws, f"f{i}.txt", mtime=OLD + i)
        _, _, texts = self.cmd("/files")
        note = texts[0]
        self.assertIn("walk budget hit", note)
        self.assertLessEqual(len(listing_lines(note)),
                             relay.FILES_MAX_LIMIT)

    def test_command_is_in_help_text(self):
        self.assertIn("/files [N]", relay.HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
