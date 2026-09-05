"""The Changes tab's engine (relay.git_overview / git_file_diff) and bridge
(app.Api.get_diff / get_file_diff).

Token-free in the repo's sense — no CLI/model calls — but it DOES run real
git against throwaway repos, because the parsers here were written against
measured git output (merged stderr noise, unborn HEAD's rc=128, rename
spellings) and a stubbed _git would only prove the code agrees with itself.
Every subprocess is a local git call on a tmp dir; suites skip when git is
absent.

Bridge rules under test (the shipping ones, per test_permissions's lesson —
a safety-shaped control needs a test at the bridge, not just the engine):
an unknown chat_id resolves to NOTHING, answers ride the chat-stamped
'diff'/'file_diff' events, and a view-only chat's recorded workspace is
still diffable.
"""

import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay          # noqa: E402
import app            # noqa: E402

from test_app_headless import FakeWindow, scripted_agent_class   # noqa: E402

GIT = shutil.which("git")


def run_git(args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t"] + args,
        cwd=cwd, check=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def rmtree(path):
    # git objects are read-only on Windows; chmod on the way down
    def onerr(fn, p, exc):
        try:
            os.chmod(p, 0o700)
            fn(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=onerr)


@unittest.skipUnless(GIT, "git not installed")
class RepoBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-diff-")
        self.addCleanup(rmtree, self.tmp)
        self.ws = os.path.join(self.tmp, "repo")
        os.makedirs(self.ws)
        run_git(["init", "-q", "-b", "alloybr"], self.ws)

    def commit(self, message):
        run_git(["add", "-A"], self.ws)
        run_git(["commit", "-q", "-m", message], self.ws)


class OverviewTests(RepoBase):
    def seed(self):
        write(self.ws, "a.txt", "one\n")
        write(self.ws, "sub/b.txt", "two\n")
        self.commit("alloy: first wave")
        write(self.ws, "a.txt", "one\nmore\n")       # modified: +1
        write(self.ws, "new.txt", "brand new\n")     # untracked
        os.remove(os.path.join(self.ws, "sub", "b.txt"))   # deleted

    def by_path(self, overview):
        return {f["path"]: f for f in overview["dirty"]}

    def test_non_repo_answers_git_false_not_empty_diff(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        o = relay.git_overview(plain)
        self.assertFalse(o["git"])
        self.assertEqual(o["reason"], "no_repo")

    def test_missing_and_falsy_workspace_say_GONE_not_not_a_repo(self):
        """A folder that has been deleted is not "not a git repository" — it
        may well be one; nobody could look. One flag for five causes is what
        turns a sentinel into a false sentence on screen."""
        for bad in (None, "", os.path.join(self.tmp, "gone"), 7):
            o = relay.git_overview(bad)
            self.assertFalse(o["git"], bad)
            self.assertEqual(o["reason"], "gone", bad)

    def test_a_repo_git_refuses_carries_gits_own_explanation(self):
        """An unknown format version (and, in the wild, `detected dubious
        ownership`) is a real repository git declined to open, and git's own
        message names the fix — _git already captured it on the merged
        stream, so throwing it away is a choice."""
        bad = os.path.join(self.tmp, "badver")
        os.makedirs(bad)
        run_git(["init", "-q"], bad)
        cfg = os.path.join(bad, ".git", "config")
        with open(cfg, encoding="utf-8") as fh:
            text = fh.read()
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write(text.replace("repositoryformatversion = 0",
                                  "repositoryformatversion = 99"))
        o = relay.git_overview(bad)
        self.assertFalse(o["git"])
        self.assertEqual(o["reason"], "refused")
        self.assertIn("repo version", o["detail"])

    def test_dirty_files_with_statuses(self):
        self.seed()
        o = relay.git_overview(self.ws)
        self.assertTrue(o["git"])
        rows = self.by_path(o)
        self.assertEqual(rows["a.txt"]["status"], "M")
        self.assertEqual(rows["sub/b.txt"]["status"], "D")
        self.assertEqual(rows["new.txt"]["status"], "??")

    def test_counts_join_from_numstat(self):
        self.seed()
        rows = self.by_path(relay.git_overview(self.ws))
        self.assertEqual(rows["a.txt"]["add"], 1)
        self.assertEqual(rows["a.txt"]["del"], 0)
        self.assertEqual(rows["sub/b.txt"]["del"], 1)

    def test_untracked_counts_are_none_never_zero(self):
        # numstat never lists an untracked file; a 0 here would claim
        # "nothing added" about a file that is all additions
        self.seed()
        rows = self.by_path(relay.git_overview(self.ws))
        self.assertIsNone(rows["new.txt"]["add"])
        self.assertIsNone(rows["new.txt"]["del"])

    def test_branch_name(self):
        self.seed()
        self.assertEqual(relay.git_overview(self.ws)["branch"], "alloybr")

    def test_detached_head_is_labelled_not_passed_off_as_a_branch(self):
        self.seed()
        run_git(["checkout", "-q", "--detach", "HEAD"], self.ws)
        branch = relay.git_overview(self.ws)["branch"]
        self.assertTrue(branch.startswith("detached @ "), branch)

    def test_commits_newest_first_with_gate_mark(self):
        self.seed()
        commits = relay.git_overview(self.ws)["commits"]
        self.assertEqual(commits[0]["subject"], "alloy: first wave")
        self.assertEqual(commits[0]["mark"], "gate")
        self.assertIsInstance(commits[0]["when"], int)
        self.assertRegex(commits[0]["sha"], r"^[0-9a-f]{4,40}$")

    def test_somebody_elses_commit_carries_no_mark(self):
        write(self.ws, "a.txt", "one\n")
        self.commit("plain human commit")
        commits = relay.git_overview(self.ws)["commits"]
        self.assertEqual(commits[0]["mark"], "")

    def test_unborn_head_is_a_repo_with_no_commits(self):
        # rev-parse HEAD exits 128 here exactly like a non-repo (measured);
        # the overview must still say "repo, nothing committed yet"
        write(self.ws, "x.txt", "hi\n")
        o = relay.git_overview(self.ws)
        self.assertTrue(o["git"])
        self.assertEqual(o["commits"], [])
        self.assertEqual(o["branch"], "alloybr")
        rows = self.by_path(o)
        self.assertEqual(rows["x.txt"]["status"], "??")
        self.assertIsNone(rows["x.txt"]["add"])

    def test_rename_joins_counts_on_the_new_path(self):
        write(self.ws, "a.txt", "one\n")
        self.commit("seed")
        run_git(["mv", "a.txt", "renamed.txt"], self.ws)
        rows = self.by_path(relay.git_overview(self.ws))
        self.assertIn("renamed.txt", rows)
        self.assertNotIn("a.txt", rows)


@unittest.skipUnless(GIT, "git not installed")
class NestedWorkspaceTests(unittest.TestCase):
    """The working folder is NOT the repo root — which is every default chat.

    git resolves a repository by walking UP, so `status`/`numstat`/`log` run
    with cwd=workspace answer for the WHOLE enclosing repo, with paths
    relative to its ROOT. The first version of this feature believed those
    answers described the workspace: a scratch chat was shown Alloy's own
    uncommitted source as "what changed in the working folder", and every row
    it listed then produced an EMPTY patch, because the root-relative path was
    re-rooted under the workspace and matched nothing. Not one test here used
    a nested workspace, so all 37 passed.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-nested-")
        self.addCleanup(rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "outer")
        os.makedirs(self.root)
        run_git(["init", "-q", "-b", "main"], self.root)
        write(self.root, ".gitignore", "sessions/\n")
        write(self.root, "top.txt", "top one\n")
        write(self.root, "pkg/mod.py", "print(1)\n")
        run_git(["add", "-A"], self.root)
        run_git(["commit", "-q", "-m", "alloy: seed"], self.root)
        write(self.root, "top.txt", "top one\ntop two\n")     # OUTSIDE edit
        write(self.root, "pkg/mod.py", "print(1)\nprint(2)\n")
        write(self.root, "pkg/new.txt", "fresh\n")
        self.pkg = os.path.join(self.root, "pkg")
        # the default-chat shape: a gitignored workspace inside the repo
        self.ws = os.path.join(self.root, "sessions", "chat1", "workspace")
        os.makedirs(self.ws)
        write(self.ws, "notes.md", "a seat wrote this\n")

    def test_a_subfolder_reports_itself_not_its_parent(self):
        o = relay.git_overview(self.pkg)
        self.assertTrue(o["git"])
        self.assertEqual(o["prefix"], "pkg")
        paths = sorted(f["path"] for f in o["dirty"])
        self.assertEqual(paths, ["mod.py", "new.txt"])
        self.assertNotIn("top.txt", paths)
        self.assertNotIn("pkg/mod.py", paths,
                         "paths must be WORKSPACE-relative — that is what "
                         "read_text and git_file_diff both take")

    def test_the_row_a_subfolder_lists_actually_opens(self):
        """The defect that made the tab useless: the listing and the per-file
        diff disagreed about which tree the path was relative to."""
        o = relay.git_overview(self.pkg)
        row = [f for f in o["dirty"] if f["path"] == "mod.py"][0]
        self.assertEqual(row["add"], 1)
        d = relay.git_file_diff(self.pkg, row["path"])
        self.assertIn("+print(2)", d["diff"],
                      "the panel said +1; the pane must not say nothing "
                      "changed")

    def test_an_untracked_row_in_a_subfolder_opens_too(self):
        d = relay.git_file_diff(self.pkg, "new.txt")
        self.assertIn("+fresh", d["diff"])

    def test_commits_are_scoped_to_the_subfolder(self):
        o = relay.git_overview(self.pkg)
        self.assertTrue(o["commits"])
        patch = relay.git_file_diff(self.pkg, sha=o["commits"][0]["sha"])
        self.assertIn("pkg/mod.py", patch["diff"])
        self.assertNotIn("top.txt", patch["diff"],
                         "a commit's patch is scoped to the working folder — "
                         "this pane is not a window onto the parent repo")

    def test_a_gitignored_workspace_says_so_rather_than_clean(self):
        """Alloy's own default: sessions/<id>/workspace inside its checkout.
        Before the fix this listed Alloy's source; "clean" would be true and
        useless, because git is not watching this folder at all."""
        o = relay.git_overview(self.ws)
        self.assertTrue(o["git"])
        self.assertTrue(o["ignored"])
        self.assertEqual(o["dirty"], [])
        self.assertEqual(o["commits"], [])

    def test_a_file_outside_the_workspace_is_refused(self):
        self.assertEqual(relay.git_file_diff(self.pkg, "../top.txt"),
                         {"error": "not available"})

    def test_pathspec_magic_cannot_reach_outside_the_workspace(self):
        """confine_to_workspace treats git's pathspec magic as ordinary
        characters, so ':(top)top.txt' passed containment and git then
        resolved it against the REPO TOP: measured, it returned the full
        patch of a file outside the workspace. --literal-pathspecs turns the
        whole grammar off rather than blacklisting prefixes.

        Both workspaces on purpose. Magic is only magic at POSITION 0
        (measured: `pkg/:(top)top.txt` is an ordinary filename), so a NESTED
        workspace defuses it by accident when the prefix is prepended — and a
        test that used only the nested one passed with the flag removed. The
        case the flag actually protects is the workspace that IS the repo
        root, which is the main documented use (`--workspace C:\\ai-chat`).
        """
        for ws, secret in ((self.pkg, "top two"), (self.root, "print(2)")):
            for magic in (":(top)top.txt", ":/top.txt", ":!mod.py",
                          ":(exclude)pkg/mod.py", ":(glob)**/*.txt"):
                r = relay.git_file_diff(ws, magic)
                self.assertNotIn(secret, r.get("diff", ""),
                                 "%s via %s" % (magic, ws))
                self.assertEqual(r, {"error": "not available"},
                                 "%s via %s" % (magic, ws))

    def test_the_commit_LIST_is_scoped_to_the_subfolder(self):
        """Not just each patch: a commit that never touched the working
        folder does not belong in its history at all — and the ⛭ would
        credit the wave gate for checkpoints of somebody else's tree."""
        write(self.root, "top.txt", "top one\ntop two\ntop three\n")
        run_git(["add", "top.txt"], self.root)
        run_git(["commit", "-q", "-m", "alloy: outside only"], self.root)
        subjects = [c["subject"]
                    for c in relay.git_overview(self.pkg)["commits"]]
        self.assertNotIn("alloy: outside only", subjects)
        self.assertIn("alloy: seed", subjects)
        # ...and the parent repo itself still sees both
        self.assertIn("alloy: outside only",
                      [c["subject"]
                       for c in relay.git_overview(self.root)["commits"]])

    def test_the_literal_flag_rides_every_command_that_accepts_it(self):
        """...and NOT check-ignore, which rejects it outright ('pathspec
        magic not supported by this command') with rc 128 — passing it there
        turned every ignored folder into an un-ignored one in silence."""
        src = inspect.getsource(relay.git_scope) + inspect.getsource(
            relay.git_overview) + inspect.getsource(relay.git_file_diff)
        self.assertIn('_git(["check-ignore"', src)
        self.assertNotIn("_LITERAL + [\"check-ignore\"", src)
        for cmd in ('"status"', '"log"', '"ls-files"', '"show"',
                    '"rev-parse"'):
            idx = src.find(cmd)
            self.assertGreater(idx, 0, cmd)
            self.assertIn("_LITERAL", src[max(0, idx - 90):idx], cmd)


class NumstatParseTests(unittest.TestCase):
    def test_plain_rows(self):
        got = relay._numstat_counts("3\t1\ta.txt\n0\t2\tsub/b.txt\n")
        self.assertEqual(got["a.txt"], (3, 1))
        self.assertEqual(got["sub/b.txt"], (0, 2))

    def test_binary_dashes_become_none(self):
        got = relay._numstat_counts("-\t-\tlogo.png\n")
        self.assertEqual(got["logo.png"], (None, None))

    def test_merged_stderr_noise_is_skipped(self):
        # _git merges stderr into stdout, so autocrlf warnings ride the same
        # stream as the rows (measured 2026-08-29)
        noise = ("warning: in the working copy of 'a.txt', LF will be "
                 "replaced by CRLF the next time Git touches it\n"
                 "1\t0\ta.txt\n")
        self.assertEqual(relay._numstat_counts(noise), {"a.txt": (1, 0)})

    def test_rename_spellings_resolve_to_the_new_path(self):
        got = relay._numstat_counts("1\t0\told.txt => new.txt\n")
        self.assertEqual(list(got), ["new.txt"])
        got = relay._numstat_counts("1\t0\tsrc/{old => new}/f.py\n")
        self.assertEqual(list(got), ["src/new/f.py"])

    def test_quoted_path_is_unquoted(self):
        got = relay._numstat_counts('1\t0\t"caf\\303\\251.txt"\n')
        self.assertEqual(list(got), ["café.txt"])


class UnquoteTests(unittest.TestCase):
    def test_plain_path_untouched(self):
        self.assertEqual(relay._unquote_git_path("a b.txt"), "a b.txt")

    def test_octal_utf8_bytes_decode(self):
        self.assertEqual(relay._unquote_git_path('"caf\\303\\251.txt"'),
                         "café.txt")

    def test_escapes(self):
        self.assertEqual(relay._unquote_git_path('"a\\tb\\"c\\\\d"'),
                         'a\tb"c\\d')


class FileDiffTests(RepoBase):
    def seed(self):
        write(self.ws, "a.txt", "one\n")
        write(self.ws, "keep.txt", "kept\n")
        self.commit("seed")
        write(self.ws, "a.txt", "one\nmore\n")
        write(self.ws, "new.txt", "brand new\n")

    def test_modified_file_diff(self):
        self.seed()
        d = relay.git_file_diff(self.ws, "a.txt")
        self.assertIn("+more", d["diff"])
        self.assertFalse(d["truncated"])

    def test_untracked_file_diffs_against_nothing(self):
        self.seed()
        d = relay.git_file_diff(self.ws, "new.txt")
        self.assertIn("+brand new", d["diff"])

    def test_unchanged_tracked_file_is_empty_not_all_additions(self):
        # the ls-files guard: without it the no-index fallback would claim
        # every line of an unchanged file was just added
        self.seed()
        d = relay.git_file_diff(self.ws, "keep.txt")
        self.assertEqual(d["diff"], "")
        self.assertEqual(d["empty"], "unchanged",
                         "the pane has to word 'no changes' differently from "
                         "'I could not resolve that'")

    def test_a_path_that_resolves_to_nothing_is_refused_not_called_unchanged(self):
        """`git diff HEAD -- <unmatched>` exits 0 with no output, exactly
        like a clean file (measured) — so an empty patch as the catch-all
        answer states 'nothing changed' about a request that failed."""
        self.seed()
        r = relay.git_file_diff(self.ws, "no/such/file.txt")
        self.assertEqual(r, {"error": "not available"})

    def test_a_staged_file_on_an_unborn_head_shows_its_content(self):
        """There is no HEAD to diff against, and a plain `git diff` compares
        the INDEX to the tree — empty for a file just `git add`ed, which is
        exactly this state. --cached is the query that answers."""
        write(self.ws, "staged.txt", "staged content\n")
        run_git(["add", "staged.txt"], self.ws)
        row = [f for f in relay.git_overview(self.ws)["dirty"]
               if f["path"] == "staged.txt"]
        self.assertEqual(row[0]["status"], "A")
        d = relay.git_file_diff(self.ws, "staged.txt")
        self.assertIn("+staged content", d["diff"])

    def test_deleted_file_diff(self):
        self.seed()
        os.remove(os.path.join(self.ws, "keep.txt"))
        d = relay.git_file_diff(self.ws, "keep.txt")
        self.assertIn("-kept", d["diff"])

    def test_leading_stderr_noise_is_stripped(self):
        self.seed()
        d = relay.git_file_diff(self.ws, "a.txt")
        self.assertTrue(d["diff"].startswith("diff --git"), d["diff"][:80])

    def test_escaping_path_is_refused_quietly(self):
        self.seed()
        write(self.tmp, "outside.txt", "secret\n")
        for bad in (os.path.join("..", "outside.txt"),
                    os.path.join(self.tmp, "outside.txt"),
                    "", None):
            self.assertEqual(relay.git_file_diff(self.ws, bad),
                             {"error": "not available"})

    def test_commit_patch_by_sha(self):
        self.seed()
        sha = relay.git_overview(self.ws)["commits"][0]["sha"]
        d = relay.git_file_diff(self.ws, sha=sha)
        self.assertIn("seed", d["diff"])          # subject line kept
        self.assertIn("+one", d["diff"])

    def test_sha_is_syntax_checked(self):
        self.seed()
        for bad in ("xyz", "abc123; rm -rf", "HEAD", "--help", 7):
            self.assertEqual(relay.git_file_diff(self.ws, sha=bad),
                             {"error": "not available"})

    def test_valid_syntax_unknown_sha_is_refused(self):
        self.seed()
        self.assertEqual(relay.git_file_diff(self.ws, sha="deadbeef"),
                         {"error": "not available"})

    def test_truncation_announces_itself_and_cuts_on_a_line(self):
        write(self.ws, "big.txt", "x\n")
        self.commit("seed")
        write(self.ws, "big.txt",
              "".join("line %06d %s\n" % (i, "y" * 30) for i in range(9000)))
        d = relay.git_file_diff(self.ws, "big.txt")
        self.assertTrue(d["truncated"])
        self.assertLessEqual(len(d["diff"]), relay.DIFF_LIMIT)
        self.assertTrue(d["diff"].endswith("\n"))

    def test_non_repo_is_refused(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        write(plain, "f.txt", "hi\n")
        self.assertEqual(relay.git_file_diff(plain, "f.txt"),
                         {"error": "not available"})


class GatePrefixTests(unittest.TestCase):
    def test_wave_gate_and_overview_share_one_prefix(self):
        # the "alloy: " spelling must exist exactly once — in the constant —
        # or the commit writer and the ⛭ reader drift apart silently
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "relay.py")
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(src.count('"alloy: "'), 1,
                         "spell the gate prefix only as GATE_COMMIT_PREFIX")
        self.assertIn("GATE_COMMIT_PREFIX + goal", src)


def wait_event(api, name, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        api._emit_q.join()
        for e in api._window.events():
            if e["event"] == name:
                return e["payload"]
        time.sleep(0.05)
    return None


@unittest.skipUnless(GIT, "git not installed")
class BridgeTests(unittest.TestCase):
    """The shipping Api methods, driven the way the UI drives them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-diffbridge-")
        self.addCleanup(rmtree, self.tmp)
        self._old = (app.SESSIONS_DIR, relay.SESSIONS_DIR, relay.TABS_FILE,
                     relay.MEMORY_DIR)
        app.SESSIONS_DIR = relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        relay.MEMORY_DIR = os.path.join(self.tmp, "memory")
        self.addCleanup(self._restore)
        self._old_types = dict(relay.AGENT_TYPES)
        self.addCleanup(lambda: (relay.AGENT_TYPES.clear(),
                                 relay.AGENT_TYPES.update(self._old_types)))
        self.ws = os.path.join(self.tmp, "repo")
        os.makedirs(self.ws)
        run_git(["init", "-q", "-b", "alloybr"], self.ws)
        write(self.ws, "a.txt", "one\n")
        run_git(["add", "-A"], self.ws)
        run_git(["commit", "-q", "-m", "seed"], self.ws)
        write(self.ws, "a.txt", "one\nmore\n")

    def _restore(self):
        (app.SESSIONS_DIR, relay.SESSIONS_DIR, relay.TABS_FILE,
         relay.MEMORY_DIR) = self._old

    def _chat(self):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["c1"])
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({
            "opener": "hi", "turns": 1, "brief": False,
            "workspace": self.ws,
            "seats": [{"id": 0, "provider": "claude", "enabled": True}],
        })
        api._emit_q.join()
        return api

    def test_get_diff_answers_with_a_chat_stamped_event(self):
        api = self._chat()
        sid = os.path.basename(api._runs.focused().session_dir)
        self.assertEqual(api.get_diff(sid), {"ok": True})
        payload = wait_event(api, "diff")
        self.assertIsNotNone(payload, "no diff event arrived")
        self.assertTrue(payload["git"])
        self.assertEqual(payload["chat_id"], sid)
        self.assertEqual(payload["dirty"][0]["path"], "a.txt")

    def test_get_file_diff_echoes_its_request_key(self):
        api = self._chat()
        sid = os.path.basename(api._runs.focused().session_dir)
        self.assertEqual(api.get_file_diff("a.txt", None, sid), {"ok": True})
        payload = wait_event(api, "file_diff")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["path"], "a.txt")
        self.assertIsNone(payload["sha"])
        self.assertIn("+more", payload["diff"])

    def test_unknown_chat_id_is_refused_never_the_focused_run(self):
        api = self._chat()
        r = api.get_diff("no-such-chat")
        self.assertIn("error", r)
        r = api.get_file_diff("a.txt", None, "no-such-chat")
        self.assertIn("error", r)

    def test_no_workspace_is_refused_inline(self):
        api = app.Api()
        api._window = FakeWindow()
        r = api.get_diff(None)
        self.assertIn("error", r)

    def test_view_only_workspace_is_still_diffable(self):
        # a reopened legacy chat has no live state; its RECORDED workspace is
        # exactly what the Changes tab should read (the read_image lookup,
        # not _resolve_chat, which refuses state-less runs)
        api = app.Api()
        api._window = FakeWindow()
        run = api._runs.focused()
        run.view_workspace = self.ws
        self.assertEqual(api.get_diff(None), {"ok": True})
        payload = wait_event(api, "diff")
        self.assertIsNotNone(payload)
        self.assertTrue(payload["git"])

    def test_escape_and_bad_sha_come_back_quiet_through_the_bridge(self):
        api = self._chat()
        sid = os.path.basename(api._runs.focused().session_dir)
        api.get_file_diff(os.path.join("..", "..", "secrets.txt"), None, sid)
        payload = wait_event(api, "file_diff")
        self.assertEqual(payload["error"], "not available")


if __name__ == "__main__":
    unittest.main(verbosity=2)
