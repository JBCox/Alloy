"""The TWO workspace-confinement copies must answer identically.

There are two implementations of "is this path inside the working folder" in
this repo, and that is DELIBERATE:

  * `relay.confine_to_workspace` -- the canonical one, used by the activity
    sink and the app's file/image bridge.
  * `browser_mcp._confine` -- a second copy, because browser_mcp.py is a
    standalone stdio MCP server spawned as its own process by a seat's CLI.
    Its top-level imports are stdlib ONLY (CLAUDE.md: "imports nothing from
    relay/app/webview"), and pulling relay into that child would trade a
    cheap, dependency-free import for a new silent failure mode in a
    fail-closed security component: an ImportError there means the server
    never handshakes and the whole capability vanishes with no sentence
    anywhere saying why.

Two copies is the accepted cost. THIS SUITE IS THE PRICE. It feeds one table
of cases -- `..` hops, absolute-elsewhere, junction/symlink escapes,
case-differing roots, drive roots, empty, non-str -- to both functions and
asserts the SAME verdict and the SAME resolved path from each. Add a rule to
one copy and this suite fails until the other learns it too.

They HAD diverged (measured 2026-08-27, before this suite existed):

  * empty path        -- relay refused; the browser copy returned the
                         workspace ROOT, so `upload_file` with `filePath: ""`
                         sailed past Alloy's refusal and handed the vendor a
                         directory.
  * None / int / bytes path -- relay returned None; the browser copy RAISED
                         TypeError, contradicting the canonical docstring's
                         "Never raises on malformed input".
  * a drive-root workspace (`C:\\`) -- relay allowed everything beneath it;
                         the browser copy refused EVERYTHING, because
                         `realpath("C:\\")` already ends in a separator and
                         nothing starts with `"C:\\" + os.sep`.
  * a case-differing root the folder for which does not exist -- relay
                         matched; the browser copy refused. (realpath fixes
                         the case only of components that EXIST, so a missing
                         folder keeps whatever the caller typed.)

The last two are both the SAME line: `commonpath` versus
`real.startswith(root + os.sep)`. relay's extra `normcase` is not what saved
it -- measured on Python 3.14, `ntpath.commonpath` already lowercases
internally -- so this suite pins the comparison, and does not pretend
normcase is doing work it is not.

Every one of those failed CLOSED or crashed -- none of them let a path out --
so this is a robustness and honesty fix, not a live security hole. What made
it worth fixing is that the browser copy's own docstring claimed parity
("the same rule `confine_to_workspace` follows in the app") while four rules
differed, and `_confine` had no test of its own at all.

Run:  python tests/test_confinement_parity.py     (token-free, no CLI calls)
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
import browser_mcp

WINDOWS = os.name == "nt"


def make_junction(link, target):
    """Windows directory junction (no admin needed). Returns True on success."""
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                           capture_output=True, timeout=15)
        return r.returncode == 0 and os.path.isdir(link)
    except OSError:
        return False


def verdict(fn, root, path):
    """('raised', text) | ('none', None) | ('path', resolved).

    A raise is a VERDICT here, not a test error: the canonical docstring
    promises never to raise on malformed input, so a copy that raises where
    the other returns None is exactly the drift this suite exists to catch.
    """
    try:
        got = fn(root, path)
    except Exception as exc:                       # noqa: BLE001 - see above
        return ("raised", "%s: %s" % (type(exc).__name__, exc))
    return ("none", None) if got is None else ("path", got)


class ConfinementParityBase(unittest.TestCase):
    """One workspace, one outside folder, and the case table built from them."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-confine-parity-")
        self.ws = os.path.join(self.tmp, "Workspace")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(os.path.join(self.ws, "sub"))
        os.makedirs(self.outside)
        with open(os.path.join(self.ws, "inside.txt"), "w") as fh:
            fh.write("x")
        with open(os.path.join(self.outside, "secret.txt"), "w") as fh:
            fh.write("x")
        # A sibling whose name has the workspace's name as a PREFIX. The
        # startswith form only survives this because of the `+ os.sep`; a
        # future edit that drops it fails here rather than in the wild.
        self.sibling = self.ws + "-next-door"
        os.makedirs(self.sibling)
        with open(os.path.join(self.sibling, "sib.txt"), "w") as fh:
            fh.write("x")

    def tearDown(self):
        link = os.path.join(self.ws, "jump")
        if os.path.isdir(link):
            subprocess.run(["cmd", "/c", "rmdir", link], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the shared table ---------------------------------------------------
    def cases(self):
        """[(name, root, path)] -- fed verbatim to BOTH implementations."""
        ws, out = self.ws, self.outside
        rows = [
            ("relative inside, file exists",   ws, "inside.txt"),
            ("relative inside, not yet",       ws, "brand-new.txt"),
            ("relative nested",                ws, os.path.join("sub", "deep.txt")),
            ("forward slashes",                ws, "sub/deep.txt"),
            ("the root itself",                ws, "."),
            ("dot-dot hop",                    ws, os.path.join("..", "outside",
                                                                "secret.txt")),
            ("dot-dot mid path",               ws, os.path.join("sub", "..", "..",
                                                                "outside",
                                                                "secret.txt")),
            ("absolute inside, file exists",   ws, os.path.join(ws, "inside.txt")),
            ("absolute inside, not yet",       ws, os.path.join(ws, "brand-new.txt")),
            ("absolute elsewhere",             ws, os.path.join(out, "secret.txt")),
            ("sibling sharing the prefix",     ws, os.path.join(self.sibling,
                                                                "sib.txt")),
            ("root with trailing separator",   ws + os.sep, "inside.txt"),
            ("path case differs, exists",      ws, os.path.join(ws.upper(),
                                                                "inside.txt")),
            ("path case differs, not yet",     ws, os.path.join(ws.upper(),
                                                                "brand-new.txt")),
            ("root case differs",              ws.upper(), "inside.txt"),
            ("empty path",                     ws, ""),
            ("empty root",                     "", "inside.txt"),
            ("None root",                      None, "inside.txt"),
            ("None path",                      ws, None),
            ("int path",                       ws, 5),
            ("bytes path",                     ws, b"inside.txt"),
            ("bytes root",                     b"x", "inside.txt"),
            ("embedded NUL",                   ws, "in\x00side.txt"),
        ]
        if WINDOWS:
            drive = os.path.splitdrive(self.tmp)[0] or "C:"
            rows += [
                ("other drive",                ws, "Z:\\secret.txt"),
                # realpath("C:\\") ALREADY ends in a separator, so the naive
                # `root + os.sep` prefix test can never match anything.
                ("drive-root workspace",       drive + os.sep,
                                               os.path.join("Windows", "win.ini")),
                ("drive-root, absolute path",  drive + os.sep,
                                               os.path.join(ws, "inside.txt")),
            ]
        # A workspace that does not exist, spelled with different case. This
        # is the one shape where realpath canonicalizes NOTHING -- it fixes
        # the case only of components that EXIST -- so the comparison itself
        # has to be case-insensitive.
        missing = os.path.join(self.tmp, "MISSING-WS")
        rows += [
            ("missing root, case differs",     missing,
                                               os.path.join(missing.lower(),
                                                            "x.txt")),
            ("missing root, relative",         missing, "x.txt"),
        ]
        link = os.path.join(self.ws, "jump")
        if WINDOWS and make_junction(link, self.outside):
            rows.append(("junction escape", ws,
                         os.path.join("jump", "secret.txt")))
        sym = os.path.join(self.ws, "sym.txt")
        try:
            os.symlink(os.path.join(self.outside, "secret.txt"), sym)
            rows.append(("symlink escape", ws, "sym.txt"))
        except (OSError, NotImplementedError, AttributeError):
            pass                       # symlinks need privilege; skip the row
        return rows

    def both(self, root, path):
        return (verdict(relay.confine_to_workspace, root, path),
                verdict(browser_mcp._confine, root, path))


class ParityTests(ConfinementParityBase):
    """The drift stopper: same table, same verdicts, same resolved paths."""

    def test_every_case_gets_the_same_verdict_from_both(self):
        for name, root, path in self.cases():
            with self.subTest(case=name):
                canon, copy = self.both(root, path)
                self.assertEqual(
                    canon, copy,
                    "%s: relay.confine_to_workspace said %r but "
                    "browser_mcp._confine said %r. The two copies are "
                    "deliberate (see this module's docstring); keeping them "
                    "in step is not." % (name, canon, copy))

    def test_no_case_in_the_table_raises_from_either_copy(self):
        # The canonical docstring promises "Never raises on malformed input",
        # and a stdio MCP server that raises answers its seat with a transport
        # error instead of Alloy's own sentence about the working folder.
        for name, root, path in self.cases():
            with self.subTest(case=name):
                canon, copy = self.both(root, path)
                self.assertNotEqual("raised", canon[0], "%s: %s" % (name, canon[1]))
                self.assertNotEqual("raised", copy[0], "%s: %s" % (name, copy[1]))


class SharedRuleTests(ConfinementParityBase):
    """Identical is not enough -- identically WRONG would also pass above."""

    def escapes(self):
        rows = [("dot-dot hop", os.path.join("..", "outside", "secret.txt")),
                ("dot-dot mid path", os.path.join("sub", "..", "..", "outside",
                                                  "secret.txt")),
                ("absolute elsewhere", os.path.join(self.outside, "secret.txt")),
                ("sibling sharing the prefix",
                 os.path.join(self.sibling, "sib.txt"))]
        if WINDOWS:
            rows.append(("other drive", "Z:\\secret.txt"))
        link = os.path.join(self.ws, "jump")
        if WINDOWS and make_junction(link, self.outside):
            # sanity: the junction really does reach out, so ONLY the
            # containment check can be what refuses it
            self.assertTrue(os.path.isfile(os.path.join(link, "secret.txt")))
            rows.append(("junction escape", os.path.join("jump", "secret.txt")))
        return rows

    def test_both_refuse_every_escape(self):
        for name, path in self.escapes():
            with self.subTest(case=name):
                for label, fn in (("relay", relay.confine_to_workspace),
                                  ("browser_mcp", browser_mcp._confine)):
                    self.assertIsNone(fn(self.ws, path),
                                      "%s let %s through" % (label, name))

    def test_both_allow_paths_inside_the_workspace(self):
        wanted = os.path.realpath(os.path.join(self.ws, "sub", "deep.txt"))
        for path in ("sub/deep.txt", os.path.join("sub", "deep.txt"),
                     os.path.join(self.ws, "sub", "deep.txt")):
            with self.subTest(path=path):
                for label, fn in (("relay", relay.confine_to_workspace),
                                  ("browser_mcp", browser_mcp._confine)):
                    self.assertEqual(wanted, fn(self.ws, path),
                                     "%s refused a path inside" % label)

    def test_both_refuse_an_empty_path(self):
        # NOT the same question as ".". An empty `filePath` reaching the
        # vendor as the workspace DIRECTORY means Alloy's own refusal never
        # fires and the approval card names a folder.
        for label, fn in (("relay", relay.confine_to_workspace),
                          ("browser_mcp", browser_mcp._confine)):
            self.assertIsNone(fn(self.ws, ""), "%s accepted an empty path" % label)

    def test_both_refuse_a_non_string_path_without_raising(self):
        for bad in (None, 5, b"inside.txt", ["inside.txt"]):
            with self.subTest(value=bad):
                for label, fn in (("relay", relay.confine_to_workspace),
                                  ("browser_mcp", browser_mcp._confine)):
                    self.assertIsNone(fn(self.ws, bad),
                                      "%s did not refuse %r" % (label, bad))

    def test_both_refuse_a_non_string_root_without_raising(self):
        for bad in (None, 5, b"C:\\ws"):
            with self.subTest(value=bad):
                for label, fn in (("relay", relay.confine_to_workspace),
                                  ("browser_mcp", browser_mcp._confine)):
                    self.assertIsNone(fn(bad, "inside.txt"),
                                      "%s did not refuse root %r" % (label, bad))

    @unittest.skipUnless(WINDOWS, "drive roots are a Windows shape")
    def test_both_confine_normally_when_the_workspace_is_a_drive_root(self):
        # `realpath("C:\\")` is "C:\\" -- it already ends in a separator, so a
        # `root + os.sep` prefix test refuses literally everything.
        drive = os.path.splitdrive(self.tmp)[0] or "C:"
        root = drive + os.sep
        inside = os.path.join(self.ws, "inside.txt")
        for label, fn in (("relay", relay.confine_to_workspace),
                          ("browser_mcp", browser_mcp._confine)):
            self.assertEqual(os.path.realpath(inside), fn(root, inside),
                             "%s refused a path on its own drive root" % label)
            self.assertIsNone(fn(root, "Z:\\secret.txt"),
                              "%s allowed another drive" % label)

    def test_both_match_a_case_differing_root_that_does_not_exist(self):
        # realpath canonicalizes the case only of components that EXIST, so a
        # missing workspace keeps whatever case the caller typed and the
        # containment check is the only thing left that can reconcile the two
        # spellings. `startswith` cannot; `commonpath` can.
        missing = os.path.join(self.tmp, "MISSING-WS")
        target = os.path.join(missing.lower(), "x.txt")
        for label, fn in (("relay", relay.confine_to_workspace),
                          ("browser_mcp", browser_mcp._confine)):
            self.assertIsNotNone(
                fn(missing, target),
                "%s refused a path inside its own workspace over case" % label)


class TwoCopiesOnPurposeTests(unittest.TestCase):
    """Why there are two, pinned so a well-meaning 'DRY' fix reads the note."""

    def test_browser_mcp_stays_standalone(self):
        # It is spawned as its own process by a seat's CLI. Importing relay
        # (or app, or webview) would make a fail-closed security component
        # depend on the whole engine loading in that child.
        #
        # Line-anchored, NOT a substring: the module's own docstring says
        # "imports nothing from relay/app/webview", and a bare `"from relay"
        # in src` check matched that sentence and failed on the prose that
        # promises the property. Same family as the wrap-token bug -- a
        # substring match cannot tell a statement from a mention.
        src = open(browser_mcp.__file__, encoding="utf-8").read()
        banned = re.compile(r"(?m)^[ \t]*(?:import|from)[ \t]+"
                            r"(relay|app|webview)\b")
        hit = banned.search(src)
        self.assertIsNone(hit,
                          "browser_mcp.py must stay standalone; found %r. If "
                          "sharing really is right, move the rule to a "
                          "stdlib-only module and update this test and both "
                          "docstrings." % (hit.group(0) if hit else ""))

    def test_each_copy_names_the_other(self):
        # The divergence survived because `_confine` claimed parity in prose
        # while four rules differed. A docstring that names its twin is what
        # sends the next editor to the parity suite.
        canonical = relay.confine_to_workspace.__doc__ or ""
        copy = browser_mcp._confine.__doc__ or ""
        self.assertIn("browser_mcp", canonical,
                      "confine_to_workspace must say a second copy exists")
        self.assertIn("confine_to_workspace", copy,
                      "_confine must name the copy it mirrors")
        for doc, who in ((canonical, "confine_to_workspace"), (copy, "_confine")):
            self.assertIn("test_confinement_parity", doc,
                          "%s must point at the suite that pins the pair" % who)


if __name__ == "__main__":
    unittest.main(verbosity=2)
