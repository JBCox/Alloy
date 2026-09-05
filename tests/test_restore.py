"""Checkpoint and rewind — relay.git_restore_preview / git_restore and their
bridge (app.Api.restore_preview / restore_workspace).

Token-free in the repo's sense (no CLI or model calls) but it runs REAL git
against throwaway repos, for the reason test_diff.py gives: every rule here
was measured against git's actual behaviour — `git restore --source` deleting
files the source does not have, `git diff` not seeing untracked ones, a
partial commit refusing an untracked path without `add -A`, a restore to a
commit older than the working folder deleting the folder itself — and a
stubbed `_git` would only prove the code agrees with itself.

THE SHAPE THAT MATTERS IS THE NESTED ONE. Alloy's default working folder is
`sessions/<id>/workspace/`, inside Alloy's own checkout, and the Changes tab
shipped wrong in 2026-08-29 because 37 tests passed without one of them
putting the workspace below the repo root. Every restore test here therefore
runs in a workspace that is a SUBDIRECTORY of the repo, with a file of the
same name sitting outside it; RootWorkspaceTests covers the other shape.
"""

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
from test_diff import GIT, run_git, write, rmtree                # noqa: E402


@unittest.skipUnless(GIT, "git not installed")
class NestedBase(unittest.TestCase):
    """A working folder INSIDE a bigger repo — the ordinary Alloy shape."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-restore-")
        self.addCleanup(rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "repo")
        self.ws = os.path.join(self.root, "ws")
        os.makedirs(self.ws)
        run_git(["init", "-q", "-b", "alloybr"], self.root)
        run_git(["config", "user.email", "t@t"], self.root)
        run_git(["config", "user.name", "t"], self.root)
        write(self.root, ".gitignore", "ignored.txt\n")
        # a file OUTSIDE the working folder, sharing a name with one inside:
        # the collision the Changes tab's first version conflated
        write(self.root, "a.txt", "outer\n")

    def commit(self, message):
        run_git(["add", "-A"], self.root)
        run_git(["commit", "-q", "-m", message], self.root)

    def git(self, *args):
        done = subprocess.run(
            ["git"] + list(args), cwd=self.root, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return done.stdout or ""

    def read(self, rel):
        with open(os.path.join(self.ws, rel), encoding="utf-8") as fh:
            return fh.read()

    def seed(self):
        """Two waves, then uncommitted work on top of the second.

        wave one: a.txt=A1, b.txt=B1
        wave two: a.txt=A2, b.txt deleted, and an outer-only change
        working:  a.txt=A3, new.txt untracked, ignored.txt untracked+ignored
        """
        write(self.ws, "a.txt", "A1\n")
        write(self.ws, "b.txt", "B1\n")
        self.commit("alloy: wave one")
        self.wave1 = self.git("rev-parse", "--short", "HEAD").strip()
        write(self.ws, "a.txt", "A2\n")
        os.remove(os.path.join(self.ws, "b.txt"))
        write(self.root, "a.txt", "outer two\n")
        self.commit("alloy: wave two")
        self.wave2 = self.git("rev-parse", "--short", "HEAD").strip()
        write(self.ws, "a.txt", "A3\n")
        write(self.ws, "new.txt", "NEW\n")
        write(self.ws, "ignored.txt", "IGNORED\n")


# --------------------------------------------------------------- prefixes --

class CommitMarkTests(unittest.TestCase):
    """Which of Alloy's own commits a subject names."""

    def test_no_alloy_prefix_contains_another(self):
        """THE property that makes _commit_mark a lookup and not an ordered
        chain. An earlier draft spelled the restore commit "alloy: restore …",
        which DOES start with the gate's prefix — so every restore would have
        worn ⛭ and claimed to be a verified checkpoint."""
        prefixes = [relay.GATE_COMMIT_PREFIX, relay.RESTORE_COMMIT_PREFIX,
                    relay.SAVE_COMMIT_PREFIX]
        for a in prefixes:
            for b in prefixes:
                if a is not b:
                    self.assertFalse(a.startswith(b), "%r starts with %r" % (a, b))

    def test_each_kind_is_named(self):
        self.assertEqual(relay._commit_mark(relay.GATE_COMMIT_PREFIX + "x"),
                         "gate")
        self.assertEqual(relay._commit_mark(relay.RESTORE_COMMIT_PREFIX + "x"),
                         "restore")
        self.assertEqual(relay._commit_mark(relay.SAVE_COMMIT_PREFIX + "x"),
                         "saved")

    def test_somebody_elses_commit_is_unmarked(self):
        for subject in ("fix the parser", "", None, "alloyish", "  alloy: x"):
            self.assertEqual(relay._commit_mark(subject), "")

    def test_a_folders_own_trailing_slash_spelling_is_not_a_file_in_it(self):
        """`?? ws/` is how git names a wholly-untracked directory, and it
        slices to "" — a blank-named row in whichever list the caller is
        building. git_overview still reads status in the collapsed mode, so
        this is reachable there even now that the preview asks for -uall."""
        self.assertIsNone(relay._under_prefix("ws", "ws"))
        self.assertIsNone(relay._under_prefix("ws/", "ws"))
        self.assertEqual(relay._under_prefix("ws/a.txt", "ws"), "a.txt")
        self.assertIsNone(relay._under_prefix("wsx/a.txt", "ws"))
        self.assertEqual(relay._under_prefix("a.txt", ""), "a.txt")

    def test_each_prefix_is_spelled_once(self):
        """Same rule GATE_COMMIT_PREFIX already has: the writer and the reader
        must not be able to drift apart."""
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "relay.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(src.count('"alloy restore: "'), 1)
        self.assertEqual(src.count('"alloy saved: "'), 1)


# ---------------------------------------------------------------- preview --

class PreviewTests(NestedBase):
    def test_the_preview_is_scoped_to_the_working_folder(self):
        """git resolves a repository by walking UP: `a.txt` exists both inside
        and outside the folder, and only the inside one is this panel's."""
        self.seed()
        p = relay.git_restore_preview(self.ws, self.wave1)
        self.assertTrue(p["ok"], p)
        self.assertEqual(p["prefix"], "ws")
        for f in p["changes"] + p["save"]:
            self.assertFalse(f["path"].startswith("ws/"),
                             "paths cross as WORKSPACE-relative: " + f["path"])
        self.assertEqual(self.git("diff", "--name-only", "HEAD", "--",
                                  "a.txt").strip(), "",
                         "the outer a.txt is not part of this at all")

    def test_verbs_are_the_restores_not_the_diffs(self):
        """`git diff <sha>` answers sha -> worktree and a restore is its
        inverse, so a file ADDED since the checkpoint is one the restore
        REMOVES. Rendering git's own letters would say the opposite."""
        self.seed()
        p = relay.git_restore_preview(self.ws, self.wave1)
        verbs = {f["path"]: f["verb"] for f in p["changes"]}
        self.assertEqual(verbs["a.txt"], "revert")
        self.assertEqual(verbs["b.txt"], "bring back")   # deleted in wave two
        self.assertEqual(verbs["new.txt"], "remove")     # never committed

    def test_an_untracked_file_is_listed_though_git_diff_cannot_see_it(self):
        """MEASURED: `git diff --name-status <sha> -- <prefix>` does not
        mention untracked files at all. They are exactly what the save commit
        turns into tracked files and the restore then deletes, so a preview
        built from the diff alone omits the files most likely to matter."""
        self.seed()
        raw = self.git("diff", "--name-status", self.wave1, "--", "ws")
        self.assertNotIn("new.txt", raw, "if git starts reporting these, the "
                                         "merge below needs re-measuring")
        p = relay.git_restore_preview(self.ws, self.wave1)
        self.assertIn("new.txt", [f["path"] for f in p["changes"]])

    def test_counts_are_swapped_into_the_restores_direction(self):
        """b.txt was deleted after wave one, so the restore ADDS its line
        back — the diff reports that as a deletion."""
        self.seed()
        p = relay.git_restore_preview(self.ws, self.wave1)
        b = [f for f in p["changes"] if f["path"] == "b.txt"][0]
        self.assertEqual((b["add"], b["del"]), (1, 0))

    def test_a_count_nobody_measured_is_none_never_zero(self):
        self.seed()
        p = relay.git_restore_preview(self.ws, self.wave1)
        new = [f for f in p["changes"] if f["path"] == "new.txt"][0]
        self.assertIsNone(new["add"])
        self.assertIsNone(new["del"])

    def test_ignored_files_are_in_neither_list(self):
        self.seed()
        p = relay.git_restore_preview(self.ws, self.wave1)
        names = [f["path"] for f in p["changes"] + p["save"]]
        self.assertNotIn("ignored.txt", names)

    def test_the_save_list_is_everything_uncommitted_under_the_folder(self):
        self.seed()
        p = relay.git_restore_preview(self.ws, self.wave1)
        self.assertEqual(p["save_total"], 2)
        got = {f["path"]: f["status"] for f in p["save"]}
        self.assertEqual(got, {"a.txt": "M", "new.txt": "??"})

    def test_an_untracked_FOLDER_is_counted_as_its_files(self):
        """MEASURED: git's default status mode collapses a wholly-untracked
        directory into one `?? dir/` row while `git add -A` stages each file
        inside it — so the card promised "1 file" for an action that commits
        and then deletes three. This list is a promise about an action, not a
        listing, so it has to count what git will actually touch."""
        write(self.ws, "a.txt", "A1\n")
        self.commit("one")
        base = self.git("rev-parse", "--short", "HEAD").strip()
        write(self.ws, "out/one.txt", "1\n")
        write(self.ws, "out/two.txt", "2\n")
        write(self.ws, "out/deep/three.txt", "3\n")
        self.assertIn("?? ws/out/",
                      self.git("status", "--porcelain", "--", "ws"),
                      "git still collapses it — that is what this defends")
        p = relay.git_restore_preview(self.ws, base)
        self.assertEqual(p["save_total"], 3)
        self.assertEqual({f["path"] for f in p["changes"]},
                         {"out/one.txt", "out/two.txt", "out/deep/three.txt"})
        self.assertNotIn("", [f["path"] for f in p["changes"] + p["save"]],
                         "and no blank-named row from the folder's own "
                         "trailing-slash spelling")
        r = relay.git_restore(self.ws, base)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["files"], 3, "the number Josh was shown is the "
                                        "number that moved")
        self.assertFalse(os.path.isdir(os.path.join(self.ws, "out")))

    def test_a_submodule_under_the_folder_is_refused_not_described(self):
        """A restore can only move the POINTER, so it cannot put another
        repository's files back — "revert" would be a promise nothing here
        can keep (gitlinks are mode 160000, measured)."""
        sub = os.path.join(self.tmp, "sub")
        os.makedirs(sub)
        run_git(["init", "-q", "-b", "main"], sub)
        run_git(["config", "user.email", "t@t"], sub)
        run_git(["config", "user.name", "t"], sub)
        write(sub, "s.txt", "s1\n")
        run_git(["add", "-A"], sub)
        run_git(["commit", "-q", "-m", "s1"], sub)
        write(self.ws, "a.txt", "A1\n")
        self.commit("before")
        base = self.git("rev-parse", "--short", "HEAD").strip()
        run_git(["-c", "protocol.file.allow=always", "submodule", "add", "-q",
                 sub.replace(os.sep, "/"), "ws/sub"], self.root)
        self.commit("with submodule")
        p = relay.git_restore_preview(self.ws, base)
        self.assertEqual(p["reason"], "submodule")
        self.assertIn("sub", p["detail"])

    def test_the_save_list_truncation_announces_itself(self):
        write(self.ws, "keep.txt", "k\n")
        self.commit("seed")
        base = self.git("rev-parse", "--short", "HEAD").strip()
        for i in range(4):
            write(self.ws, "u%d.txt" % i, "x\n")
        old = relay.RESTORE_FILES_MAX
        relay.RESTORE_FILES_MAX = 2
        self.addCleanup(setattr, relay, "RESTORE_FILES_MAX", old)
        p = relay.git_restore_preview(self.ws, base)
        self.assertEqual(len(p["save"]), 2)
        self.assertEqual(p["save_total"], 4)
        self.assertTrue(p["save_truncated"])

    def test_behind_counts_only_commits_that_touched_this_folder(self):
        self.seed()
        write(self.root, "a.txt", "outer three\n")
        # ONLY the outer file: `self.commit` stages everything, which would
        # sweep the workspace's own uncommitted work into this commit and so
        # make it touch `ws` after all
        run_git(["add", "a.txt"], self.root)
        run_git(["commit", "-q", "-m", "outside work", "--", "a.txt"],
                self.root)
        p = relay.git_restore_preview(self.ws, self.wave1)
        self.assertEqual(p["behind"], 1, "wave two touched ws; the outer "
                                         "commit did not")
        self.assertTrue(p["ancestor"])
        self.assertFalse(p["detached"])

    def test_a_rename_reads_as_two_files(self):
        """--no-renames on purpose: D + A is the pair of things the restore
        actually performs, while one R100 row hides both halves."""
        write(self.ws, "old.txt", "same\n")
        self.commit("seed")
        base = self.git("rev-parse", "--short", "HEAD").strip()
        run_git(["mv", "ws/old.txt", "ws/new.txt"], self.root)
        self.commit("rename")
        p = relay.git_restore_preview(self.ws, base)
        verbs = {f["path"]: f["verb"] for f in p["changes"]}
        self.assertEqual(verbs, {"old.txt": "bring back", "new.txt": "remove"})
        # The NUMSTAT call needs the flag too, and the verbs alone cannot see
        # that: with detection on, numstat emits one `{old => new}` row that
        # _numstat_counts resolves to the NEW path, so `old.txt` silently
        # loses its counts and `new.txt` reports the rename's zeroes.
        counts = {f["path"]: (f["add"], f["del"]) for f in p["changes"]}
        self.assertEqual(counts["old.txt"], (1, 0), "restoring brings it back")
        self.assertEqual(counts["new.txt"], (0, 1), "and takes the other away")

    def test_truncation_announces_itself(self):
        write(self.ws, "keep.txt", "k\n")
        self.commit("seed")
        base = self.git("rev-parse", "--short", "HEAD").strip()
        for i in range(5):
            write(self.ws, "f%d.txt" % i, "x\n")
        self.commit("five more")
        old = relay.RESTORE_FILES_MAX
        relay.RESTORE_FILES_MAX = 2
        self.addCleanup(setattr, relay, "RESTORE_FILES_MAX", old)
        p = relay.git_restore_preview(self.ws, base)
        self.assertEqual(len(p["changes"]), 2)
        self.assertEqual(p["total"], 5)
        self.assertTrue(p["truncated"])

    def test_a_detached_head_says_so(self):
        self.seed()
        run_git(["checkout", "-q", "--detach"], self.root)
        p = relay.git_restore_preview(self.ws, self.wave1)
        self.assertTrue(p["detached"])


# --------------------------------------------------------------- refusals --

class RefusalTests(NestedBase):
    def test_a_commit_older_than_the_folder_is_refused(self):
        """MEASURED: `git restore --source` removes every tracked file under
        the prefix AND the emptied directories, so the working folder itself
        disappears while Alloy is still pointing at it."""
        write(self.root, "only-outside.txt", "x\n")
        self.commit("before the folder existed")
        before = self.git("rev-parse", "--short", "HEAD").strip()
        write(self.ws, "a.txt", "A1\n")
        self.commit("the folder appears")
        p = relay.git_restore_preview(self.ws, before)
        self.assertFalse(p["ok"])
        self.assertEqual(p["reason"], "no_folder")
        r = relay.git_restore(self.ws, before)
        self.assertEqual(r["reason"], "no_folder")
        self.assertTrue(os.path.isdir(self.ws), "the folder must still be there")

    def test_bad_shas_are_refused(self):
        self.seed()
        for bad in ("HEAD", "--help", "xyz", "", None, 7, "wave1..HEAD",
                    "deadbeef"):
            p = relay.git_restore_preview(self.ws, bad)
            self.assertFalse(p["ok"], bad)
            self.assertEqual(p["reason"], "bad_sha", bad)

    def test_a_blob_sha_is_not_a_commit(self):
        self.seed()
        blob = self.git("rev-parse", "HEAD:ws/a.txt").strip()
        p = relay.git_restore_preview(self.ws, blob)
        self.assertEqual(p["reason"], "bad_sha")

    def test_an_ignored_working_folder_has_nothing_to_restore(self):
        """The DEFAULT chat's shape: sessions/ is gitignored inside Alloy's
        own checkout, so git is not watching the folder at all."""
        write(self.root, ".gitignore", "junk/\n")
        self.commit("ignore it")
        junk = os.path.join(self.root, "junk")
        os.makedirs(junk)
        write(junk, "x.txt", "hi\n")
        head = self.git("rev-parse", "--short", "HEAD").strip()
        p = relay.git_restore_preview(junk, head)
        self.assertEqual(p["reason"], "ignored")

    def test_a_folder_already_matching_is_refused(self):
        write(self.ws, "a.txt", "A1\n")
        self.commit("one")
        head = self.git("rev-parse", "--short", "HEAD").strip()
        p = relay.git_restore_preview(self.ws, head)
        self.assertEqual(p["reason"], "same")

    def test_an_unborn_head_has_nothing_to_restore_to(self):
        """The `unborn` branch needs a sha that RESOLVES while HEAD does not,
        which is an orphan branch — a repo with no commits at all fails
        rev-parse first and answers bad_sha, so the obvious version of this
        test never reached the line it was named for."""
        self.seed()
        run_git(["checkout", "-q", "--orphan", "fresh"], self.root)
        p = relay.git_restore_preview(self.ws, self.wave1)
        self.assertFalse(p["ok"])
        self.assertEqual(p["reason"], "unborn")

    def test_a_repo_with_no_commits_at_all_is_a_bad_sha(self):
        write(self.ws, "a.txt", "A1\n")
        p = relay.git_restore_preview(self.ws, "abc1234")
        self.assertEqual(p["reason"], "bad_sha")

    def test_an_unresolved_merge_is_refused_before_anything_is_staged(self):
        """MEASURED twice: `git add -A` RESOLVES a conflict by staging the
        marker text and discards stages 1/2/3, and the partial commit that
        would follow refuses outright ("cannot do a partial commit during a
        merge") — so without this the run reports save_failed while
        `git ls-files -u` is already empty and the merge is unrecoverable."""
        write(self.ws, "f.txt", "base\n")
        self.commit("base")
        base = self.git("rev-parse", "--short", "HEAD").strip()
        run_git(["checkout", "-q", "-b", "other"], self.root)
        write(self.ws, "f.txt", "theirs\n")
        self.commit("theirs")
        run_git(["checkout", "-q", "alloybr"], self.root)
        write(self.ws, "f.txt", "mine\n")
        self.commit("mine")
        self.git("merge", "other")
        self.assertIn("UU", self.git("status", "--porcelain"))
        p = relay.git_restore_preview(self.ws, base)
        self.assertEqual(p["reason"], "conflict")
        self.assertIn("f.txt", p["detail"])
        r = relay.git_restore(self.ws, base)
        self.assertEqual(r["reason"], "conflict")
        self.assertIn("UU", self.git("status", "--porcelain"),
                      "the conflict stages must still be there")
        self.assertTrue(self.git("ls-files", "-u", "--", "ws").strip(),
                        "git mergetool still has something to work with")

    def test_a_missing_git_identity_is_refused_before_anything_moves(self):
        """`git var GIT_COMMITTER_IDENT` fails in exactly the conditions
        `git commit` fails in (measured), so this is checkable up front —
        and finding out afterwards would leave the folder restored with the
        commit that records it missing."""
        self.seed()
        run_git(["config", "--unset", "user.email"], self.root)
        run_git(["config", "user.useConfigOnly", "true"], self.root)
        empty = os.path.join(self.tmp, "empty.cfg")
        open(empty, "w").close()
        old = {k: os.environ.get(k)
               for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")}

        def restore_env():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore_env)
        os.environ["GIT_CONFIG_GLOBAL"] = empty
        os.environ["GIT_CONFIG_SYSTEM"] = empty
        p = relay.git_restore_preview(self.ws, self.wave1)
        self.assertEqual(p["reason"], "no_identity")
        self.assertIn("email", p["detail"].lower())
        r = relay.git_restore(self.ws, self.wave1)
        self.assertEqual(r["reason"], "no_identity")
        self.assertEqual(self.read("a.txt"), "A3\n", "nothing may have moved")

    def test_a_head_that_moved_since_the_card_is_refused(self):
        self.seed()
        r = relay.git_restore(self.ws, self.wave1, head="0000000")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "moved")
        self.assertEqual(self.read("a.txt"), "A3\n")

    def test_a_folder_that_is_not_a_repository_passes_the_reason_through(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        p = relay.git_restore_preview(plain, "abc1234")
        self.assertEqual(p["reason"], "no_repo")
        p = relay.git_restore_preview(os.path.join(self.tmp, "gone"), "abc1234")
        self.assertEqual(p["reason"], "gone")
        for junk in (None, "", 7):
            self.assertFalse(relay.git_restore_preview(junk, "abc1234")["ok"])


# ---------------------------------------------------------------- restore --

class RestoreTests(NestedBase):
    def test_the_folder_matches_the_checkpoint_afterwards(self):
        self.seed()
        r = relay.git_restore(self.ws, self.wave1)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.read("a.txt"), "A1\n")
        self.assertEqual(self.read("b.txt"), "B1\n", "deleted files come back")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "new.txt")),
                         "a file added since the checkpoint is removed")
        self.assertEqual(relay.git_overview(self.ws)["dirty"], [],
                         "and the folder is clean, so the next restore and "
                         "the next gate commit are both possible")

    def test_history_only_grows(self):
        self.seed()
        before = self.git("rev-list", "--count", "HEAD").strip()
        r = relay.git_restore(self.ws, self.wave1)
        after = self.git("rev-list", "--count", "HEAD").strip()
        self.assertEqual(int(after), int(before) + 2)
        self.assertIn(self.wave2, self.git("log", "--format=%h"),
                      "the work being undone is still in history")
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").strip(),
                         "alloybr", "the branch does not move anywhere else")
        self.assertEqual(r["commit"],
                         self.git("rev-parse", "--short", "HEAD").strip())

    def test_files_outside_the_working_folder_are_untouched(self):
        """Both pathspecs are load-bearing and this has to be able to see
        BOTH. The staged outer change catches a `commit` that lost its
        pathspec; only an UNTRACKED outer file catches an `add -A` that lost
        its one — with the outer change already staged, dropping the add's
        pathspec changed nothing a test could observe (verified by running
        that exact mutation: 47 tests, all green)."""
        self.seed()
        write(self.root, "a.txt", "outer edited\n")
        run_git(["add", "a.txt"], self.root)
        write(self.root, "stranger.txt", "never mine\n")
        relay.git_restore(self.ws, self.wave1)
        with open(os.path.join(self.root, "a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "outer edited\n")
        status = self.git("status", "--porcelain")
        self.assertIn("M  a.txt", status,
                      "the outer file's staged change is still uncommitted")
        self.assertIn("?? stranger.txt", status,
                      "an untracked file outside the folder was never staged")
        self.assertNotIn("a.txt", self.git("show", "--stat", "--format=", "HEAD")
                         .replace("ws/a.txt", ""),
                         "and it is not in the restore commit")

    @unittest.skipUnless(os.name == "nt", "the unlink refusal is a Windows fact")
    def test_a_restore_that_could_not_finish_is_not_reported_as_done(self):
        """MEASURED: git only WARNS when it cannot unlink a worktree file —
        rc 0, the index entry gone, the old content still on disk untracked.
        Committing then re-adds it from the worktree and `git status` reads
        CLEAN, so every surface afterwards would state as measured fact that
        the folder was put back."""
        write(self.ws, "a.txt", "A1\n")
        self.commit("one")
        base = self.git("rev-parse", "--short", "HEAD").strip()
        write(self.ws, "locked.txt", "added later\n")
        self.commit("two")
        before = int(self.git("rev-list", "--count", "HEAD").strip())
        held = open(os.path.join(self.ws, "locked.txt"), "r+", encoding="utf-8")
        try:
            r = relay.git_restore(self.ws, base)
        finally:
            held.close()
        self.assertFalse(r["ok"], r)
        self.assertEqual(r["reason"], "partial")
        self.assertEqual(r["left"], ["locked.txt"])
        self.assertTrue(r["files_restored"])
        self.assertEqual(int(self.git("rev-list", "--count", "HEAD").strip()),
                         before, "a folder that did not move must not be "
                                 "recorded as a checkpoint")
        self.assertTrue(os.path.exists(os.path.join(self.ws, "locked.txt")))

    def test_ignored_files_survive(self):
        self.seed()
        relay.git_restore(self.ws, self.wave1)
        self.assertEqual(self.read("ignored.txt"), "IGNORED\n")

    def test_uncommitted_work_is_saved_first_and_can_be_restored_back(self):
        """The round trip that makes this feature usable rather than a trap:
        a red gate leaves the last wave UNCOMMITTED, so refusing on a dirty
        tree would refuse in exactly the scenario this exists for."""
        self.seed()
        r = relay.git_restore(self.ws, self.wave1)
        self.assertTrue(r["saved"], "the uncommitted work earned a commit")
        self.assertEqual(r["saved_files"], 2)
        back = relay.git_restore(self.ws, r["saved"])
        self.assertTrue(back["ok"], back)
        self.assertEqual(self.read("a.txt"), "A3\n")
        self.assertEqual(self.read("new.txt"), "NEW\n")

    def test_the_two_commits_are_marked_in_the_overview(self):
        self.seed()
        relay.git_restore(self.ws, self.wave1)
        commits = relay.git_overview(self.ws)["commits"]
        self.assertEqual(commits[0]["mark"], "restore")
        self.assertEqual(commits[1]["mark"], "saved")
        self.assertEqual(commits[2]["mark"], "gate")

    def test_a_clean_tree_makes_only_one_commit(self):
        self.seed()
        run_git(["add", "-A"], self.root)
        run_git(["commit", "-q", "-m", "tidy first"], self.root)
        before = int(self.git("rev-list", "--count", "HEAD").strip())
        r = relay.git_restore(self.ws, self.wave1)
        self.assertIsNone(r["saved"])
        self.assertEqual(int(self.git("rev-list", "--count", "HEAD").strip()),
                         before + 1)

    def test_restoring_twice_in_a_row_is_possible(self):
        """The trap the design avoids: a restore that left the tree dirty
        would refuse the very next one, so Josh could look at wave one and
        never get back."""
        self.seed()
        self.assertTrue(relay.git_restore(self.ws, self.wave1)["ok"])
        self.assertTrue(relay.git_restore(self.ws, self.wave2)["ok"])
        self.assertEqual(self.read("a.txt"), "A2\n")

    def _reject_commits(self):
        hooks = os.path.join(self.root, ".git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        path = os.path.join(hooks, "pre-commit")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("#!/bin/sh\necho 'hook says no'\nexit 1\n")
        os.chmod(path, 0o755)

    def test_a_save_that_fails_leaves_the_folder_exactly_as_it_was(self):
        """Order is what makes the failure modes survivable: the save commit
        happens FIRST, so a refusal there has overwritten nothing."""
        self.seed()
        self._reject_commits()
        r = relay.git_restore(self.ws, self.wave1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "save_failed")
        self.assertIn("hook says no", r["detail"])
        self.assertEqual(self.read("a.txt"), "A3\n")
        self.assertTrue(os.path.exists(os.path.join(self.ws, "new.txt")))

    def test_a_failed_commit_still_says_the_files_moved(self):
        """A bare failure here would read as 'nothing changed' while the
        folder on disk had already been rewritten."""
        self.seed()
        run_git(["add", "-A"], self.root)
        run_git(["commit", "-q", "-m", "tidy"], self.root)
        self._reject_commits()
        r = relay.git_restore(self.ws, self.wave1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "commit_failed")
        self.assertTrue(r["files_restored"])
        self.assertEqual(self.read("a.txt"), "A1\n")

    def test_the_restore_commit_quotes_the_checkpoint_it_used(self):
        self.seed()
        relay.git_restore(self.ws, self.wave1)
        subject = self.git("log", "-1", "--format=%s").strip()
        self.assertTrue(subject.startswith(relay.RESTORE_COMMIT_PREFIX))
        self.assertIn(self.wave1, subject)
        self.assertEqual(relay._commit_mark(subject), "restore",
                         "a restore of an 'alloy: ' commit is still a restore")


@unittest.skipUnless(GIT, "git not installed")
class RootWorkspaceTests(unittest.TestCase):
    """The other shape: the working folder IS the repository root."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-restore-root-")
        self.addCleanup(rmtree, self.tmp)
        self.ws = os.path.join(self.tmp, "repo")
        os.makedirs(self.ws)
        run_git(["init", "-q", "-b", "alloybr"], self.ws)
        run_git(["config", "user.email", "t@t"], self.ws)
        run_git(["config", "user.name", "t"], self.ws)
        write(self.ws, "a.txt", "A1\n")
        run_git(["add", "-A"], self.ws)
        run_git(["commit", "-q", "-m", "one"], self.ws)
        self.base = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=self.ws,
            stdout=subprocess.PIPE, text=True).stdout.strip()
        write(self.ws, "a.txt", "A2\n")
        write(self.ws, "extra.txt", "E\n")

    def test_an_empty_prefix_still_scopes_every_call(self):
        p = relay.git_restore_preview(self.ws, self.base)
        self.assertEqual(p["prefix"], "")
        self.assertEqual({f["path"] for f in p["changes"]},
                         {"a.txt", "extra.txt"})
        r = relay.git_restore(self.ws, self.base)
        self.assertTrue(r["ok"], r)
        with open(os.path.join(self.ws, "a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "A1\n")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "extra.txt")))
        self.assertTrue(os.path.isdir(self.ws))


# ----------------------------------------------------------------- bridge --

def wait_event(api, name, timeout=15):
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
    """The shipping Api methods, driven the way the UI drives them.

    test_permissions' lesson: a control shaped like a safety control needs a
    test at the BRIDGE, not only at the engine.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-restorebridge-")
        self.addCleanup(rmtree, self.tmp)
        self._old = (app.SESSIONS_DIR, relay.SESSIONS_DIR, relay.TABS_FILE,
                     relay.MEMORY_DIR)
        app.SESSIONS_DIR = relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        relay.MEMORY_DIR = os.path.join(self.tmp, "memory")
        self.addCleanup(self._put_back)
        self._old_types = dict(relay.AGENT_TYPES)
        self.addCleanup(lambda: (relay.AGENT_TYPES.clear(),
                                 relay.AGENT_TYPES.update(self._old_types)))
        self.ws = os.path.join(self.tmp, "repo")
        os.makedirs(self.ws)
        run_git(["init", "-q", "-b", "alloybr"], self.ws)
        run_git(["config", "user.email", "t@t"], self.ws)
        run_git(["config", "user.name", "t"], self.ws)
        write(self.ws, "a.txt", "one\n")
        run_git(["add", "-A"], self.ws)
        run_git(["commit", "-q", "-m", "seed"], self.ws)
        self.base = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=self.ws,
            stdout=subprocess.PIPE, text=True).stdout.strip()
        write(self.ws, "a.txt", "one\nmore\n")

    def _put_back(self):
        (app.SESSIONS_DIR, relay.SESSIONS_DIR, relay.TABS_FILE,
         relay.MEMORY_DIR) = self._old

    def _chat(self, workspace=None):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["c1"])
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({
            "opener": "hi", "turns": 1, "brief": False,
            "workspace": workspace or self.ws,
            "seats": [{"id": 0, "provider": "claude", "enabled": True}],
        })
        api._emit_q.join()
        return api

    def test_the_preview_rides_a_chat_stamped_event(self):
        api = self._chat()
        sid = os.path.basename(api._runs.focused().session_dir)
        self.assertEqual(api.restore_preview(self.base, sid), {"ok": True})
        p = wait_event(api, "restore_preview")
        self.assertIsNotNone(p, "no restore_preview event arrived")
        self.assertEqual(p["chat_id"], sid)
        self.assertEqual(p["request_sha"], self.base)
        self.assertTrue(p["ok"], p)
        self.assertEqual([f["path"] for f in p["changes"]], ["a.txt"])

    def test_an_unknown_chat_id_is_refused_never_the_focused_run(self):
        api = self._chat()
        self.assertIn("error", api.restore_preview(self.base, "no-such-chat"))
        self.assertIn("error",
                      api.restore_workspace(self.base, None, "no-such-chat"))

    def test_no_workspace_is_refused_inline(self):
        api = app.Api()
        api._window = FakeWindow()
        self.assertIn("error", api.restore_preview(self.base))

    def test_a_view_only_chat_can_still_restore(self):
        api = app.Api()
        api._window = FakeWindow()
        api._runs.focused().view_workspace = self.ws
        self.assertEqual(api.restore_workspace(self.base, None), {"ok": True})
        p = wait_event(api, "restored")
        self.assertTrue(p["ok"], p)
        with open(os.path.join(self.ws, "a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "one\n")

    def test_the_restore_is_written_into_the_transcript(self):
        """A discontinuity the chat has to keep explaining once reopened —
        which is what SessionStore.system exists for."""
        api = self._chat()
        sid = os.path.basename(api._runs.focused().session_dir)
        api.restore_workspace(self.base, None, sid)
        self.assertTrue(wait_event(api, "restored")["ok"])
        rows = relay.read_messages(api._runs.get(sid).session_dir)
        notes = [r for r in rows if "restored the working folder" in
                 (r.get("text") or "")]
        self.assertEqual(len(notes), 1, "exactly one persisted note")
        self.assertEqual(notes[0]["speaker"], "system")

    def _live_run_in(self, api, workspace):
        """A run that is genuinely alive (a thread that will not finish) with
        `workspace` on its state — what _restore_blocked has to see.

        Registered the way the app registers one: `background()` puts it in
        the pending list and `adopt` moves it into the map once it has an id.
        A run holding an id that was never adopted is in neither, so `live()`
        cannot see it — a fact about this harness, not a hole in the product,
        since `adopt` is the only thing that hands out ids.
        """
        import threading
        run = api._runs.background()
        if workspace:
            run.state = {"workspace": workspace}
            api._runs.adopt(run, "other-chat")
        gate = threading.Event()
        self.addCleanup(gate.set)
        api._runs.spawn(gate.wait, run=run)
        for _ in range(200):
            if run.is_running():
                break
            time.sleep(0.01)
        self.assertTrue(run.is_running())
        self.assertIn(run, api._runs.live())
        return run

    def _blocked(self, api, sha=None):
        api.restore_workspace(sha or self.base, None, None)
        return wait_event(api, "restored")

    def test_a_live_chat_in_the_same_folder_blocks_the_restore(self):
        api = app.Api()
        api._window = FakeWindow()
        api._runs.focused().view_workspace = self.ws
        self._live_run_in(api, self.ws)
        p = self._blocked(api)
        self.assertFalse(p["ok"])
        self.assertEqual(p["reason"], "busy")
        with open(os.path.join(self.ws, "a.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "one\nmore\n", "nothing was touched")

    def test_a_live_chat_in_a_PARENT_folder_blocks_it_too(self):
        """It is not enough to compare the chat the button was clicked in:
        the run that gets corrupted is the OTHER one, and either folder
        containing the other is a conflict."""
        api = app.Api()
        api._window = FakeWindow()
        api._runs.focused().view_workspace = self.ws
        self._live_run_in(api, self.tmp)          # the workspace's parent
        self.assertEqual(self._blocked(api)["reason"], "busy")

    def test_a_chat_that_is_still_starting_blocks(self):
        """A live run with no workspace yet is a chat seconds from having
        one; a missed conflict overwrites files, a refusal costs a retry.

        It answers with its OWN reason: the engine does not know where that
        chat is going, so "a conversation is running in this working folder"
        would be a measured-sounding sentence nobody measured."""
        api = app.Api()
        api._window = FakeWindow()
        api._runs.focused().view_workspace = self.ws
        self._live_run_in(api, None)
        p = self._blocked(api)
        self.assertEqual(p["reason"], "starting")
        self.assertEqual(p["detail"], "")

    def test_a_live_chat_somewhere_else_does_not_block(self):
        api = app.Api()
        api._window = FakeWindow()
        api._runs.focused().view_workspace = self.ws
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        self._live_run_in(api, other)
        self.assertTrue(self._blocked(api)["ok"])

    def test_the_preview_refuses_while_a_chat_is_live_too(self):
        api = app.Api()
        api._window = FakeWindow()
        api._runs.focused().view_workspace = self.ws
        self._live_run_in(api, self.ws)
        api.restore_preview(self.base, None)
        p = wait_event(api, "restore_preview")
        self.assertEqual(p["reason"], "busy")

    def test_two_restores_of_one_folder_do_not_interleave(self):
        """The card's button disables itself, but clicking ⟲ again mints a
        fresh enabled one — so the disable is per-BUTTON while the corruption
        is per-FOLDER. Exactly one of these may succeed."""
        import threading
        api = app.Api()
        api._window = FakeWindow()
        api._runs.focused().view_workspace = self.ws
        before = int(subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=self.ws,
            stdout=subprocess.PIPE, text=True).stdout.strip())
        done = []
        lock = threading.Lock()

        def fire():
            api.restore_workspace(self.base, None, None)
        threads = [threading.Thread(target=fire) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            api._emit_q.join()
            done = [e["payload"] for e in api._window.events()
                    if e["event"] == "restored"]
            if len(done) >= 2:
                break
            time.sleep(0.05)
        with lock:
            self.assertEqual(len(done), 2, "both answered")
        self.assertEqual(sum(1 for p in done if p.get("ok")), 1,
                         "exactly one restore may happen")
        other = [p for p in done if not p.get("ok")][0]
        self.assertIn(other["reason"], ("same", "moved"),
                      "the loser re-ran its preview inside the lock and saw "
                      "the state the winner left")
        after = int(subprocess.run(
            ["git", "rev-list", "--count", "HEAD"], cwd=self.ws,
            stdout=subprocess.PIPE, text=True).stdout.strip())
        self.assertEqual(after, before + 2, "one save + one restore, no more")

    def test_a_restore_that_moved_files_but_failed_is_still_recorded(self):
        """`commit_failed` leaves the seats looking at different files, and a
        chat that cannot explain its own discontinuity is the thing
        SessionStore.system exists to prevent."""
        api = self._chat()
        sid = os.path.basename(api._runs.focused().session_dir)
        hooks = os.path.join(self.ws, ".git", "hooks")
        os.makedirs(hooks, exist_ok=True)
        with open(os.path.join(hooks, "pre-commit"), "w", newline="\n") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(os.path.join(hooks, "pre-commit"), 0o755)
        # a clean tree, so there is no save commit to fail first
        run_git(["add", "-A"], self.ws)
        run_git(["commit", "-q", "--no-verify", "-m", "tidy"], self.ws)
        api.restore_workspace(self.base, None, sid)
        p = wait_event(api, "restored")
        self.assertEqual(p["reason"], "commit_failed")
        rows = relay.read_messages(api._runs.get(sid).session_dir)
        notes = [r for r in rows if "commit recording it failed" in
                 (r.get("text") or "")]
        self.assertEqual(len(notes), 1)
        self.assertIn(self.base, notes[0]["text"])

    def test_overlaps_answers_both_directions_and_never_raises(self):
        self.assertTrue(app.Api._overlaps(self.ws, self.ws))
        self.assertTrue(app.Api._overlaps(self.tmp, self.ws))
        self.assertTrue(app.Api._overlaps(self.ws, self.tmp))
        self.assertFalse(app.Api._overlaps(self.ws, self.ws + "-sibling"))
        for junk in ((None, self.ws), (self.ws, None), (7, self.ws), ("", "")):
            self.assertFalse(app.Api._overlaps(*junk))


if __name__ == "__main__":
    unittest.main(verbosity=2)
