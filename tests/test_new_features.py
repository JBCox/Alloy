"""Keyboard-shortcut cheat sheet: app.KEYBOARD_SHORTCUTS + the bridge method.

The feature: "?" toggles an overlay listing every shortcut the app really
binds. The data lives ONCE in app.py; the UI fetches it through
``Api.get_shortcuts()`` and renders it verbatim — a hand-written copy in the
HTML would drift from actual bindings exactly like every other duplicated
list this repo has grown out of.

Token-free: a constant return over object construction only. No file I/O,
no subprocess — which is also what makes it legal on pywebview's bridge
thread, so the tests pin THAT property too.

Run:  python tests/test_new_features.py
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class ShortcutDataTests(unittest.TestCase):
    """The catalog itself: shape and honesty."""

    def setUp(self):
        self.shortcuts = app.KEYBOARD_SHORTCUTS

    def test_catalog_is_a_nonempty_list_of_dicts(self):
        self.assertIsInstance(self.shortcuts, list)
        self.assertTrue(self.shortcuts, "a cheat sheet with no rows is dead UI")
        for s in self.shortcuts:
            self.assertIsInstance(s, dict)
            self.assertIn("keys", s)
            self.assertIn("action", s)
            self.assertIsInstance(s["keys"], str) and self.assertTrue(s["keys"])
            self.assertIsInstance(s["action"], str) and self.assertTrue(s["action"])

    def test_question_mark_toggles_the_sheet_itself(self):
        """The feature's headline binding must be IN the sheet — an overlay
        you cannot discover from the overlay would be a locked door."""
        first = self.shortcuts[0]
        self.assertEqual(first["keys"], "?")
        self.assertIn("cheat sheet", first["action"])

    def test_every_documented_binding_is_unique(self):
        keys = [s["keys"] for s in self.shortcuts]
        self.assertEqual(len(keys), len(set(keys)),
                         "duplicate key rows would make one of them a lie")

    def test_dictation_binding_matches_the_real_composer(self):
        """Pinned against ui/index.html's actual handler (Ctrl+Shift+Space
        toggles dictation) — if the UI ever rebinds, this row must move too."""
        row = next(s for s in self.shortcuts if "ictation" in s["action"])
        self.assertEqual(row["keys"], "Ctrl+Shift+Space")


class ShortcutBridgeTests(unittest.TestCase):
    """Api.get_shortcuts(): bridge-thread safety is the contract."""

    def setUp(self):
        self.api = app.Api()

    def test_returns_the_catalog_under_shortcuts_key(self):
        out = self.api.get_shortcuts()
        self.assertIsInstance(out, dict)
        self.assertIn("shortcuts", out)
        self.assertEqual(out["shortcuts"], app.KEYBOARD_SHORTCUTS)

    def test_returned_rows_are_copies_not_live_aliases(self):
        """A caller mutating the returned payload (JS can do worse than that)
        must not corrupt the module constant for the next call."""
        out = self.api.get_shortcuts()
        out["shortcuts"][0]["action"] = "MANGLED"
        self.assertNotEqual(
            self.api.get_shortcuts()["shortcuts"][0]["action"], "MANGLED")

    def test_never_raises_and_is_json_serializable(self):
        """Bridge methods that raise take down the whole js call with them;
        the payload also has to survive pywebview's JSON serialization."""
        try:
            out = self.api.get_shortcuts()
        except Exception as exc:  # pragma: no cover - failure IS the bug
            self.fail("get_shortcuts raised on a bare Api(): %r" % (exc,))
        json.dumps(out)

    def test_no_subprocess_on_the_bridge_thread(self):
        """The repo's hard-won rule: subprocess work on the js-bridge thread
        deadlocks. A cheat sheet is constant data — prove it stays that way
        by stubbing the one function every shell-out funnels through."""
        real = app.subprocess.run
        calls = []
        app.subprocess.run = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
            AssertionError("bridge subprocess"))
        try:
            self.api.get_shortcuts()
        finally:
            app.subprocess.run = real
        self.assertEqual(calls, [])


class CheatSheetUiTests(unittest.TestCase):
    """The overlay itself lives in ui/index.html's inline markup/script; this
    suite reads that file as TEXT (the same discipline other suites use) and
    pins the structural facts the wiring depends on — plus cross-consistency
    between the bridge catalog and the rendered sheet, so the two copies
    cannot drift apart silently."""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "ui", "index.html"), encoding="utf-8") as f:
            self.ui = f.read()

    def test_kbd_modal_exists_with_the_standard_shell(self):
        self.assertIn('id="kbdModal"', self.ui)
        # same .modal shell + close button as every other overlay
        m = self.ui[self.ui.index('id="kbdModal"'):]
        self.assertIn('<div class="modal">', m[:200])
        self.assertIn('id="kbdClose"', m[:400])

    def test_kbd_modal_joins_the_shared_show_rule_and_tab_trap(self):
        # the hand-kept display rule (a count-based guard once shipped one
        # option short — this list must gain the id explicitly)
        self.assertTrue(any(line.lstrip().startswith("#contModal.show, #kbdModal.show")
                            for line in self.ui.splitlines()),
                        "#kbdModal.show missing from the grouped .show display rule")
        # Tab-trapping selects [id$="Modal"].show dynamically, so membership
        # is automatic — pin the selector it relies on still exists.
        self.assertIn('[id$="Modal"].show', self.ui)

    def test_escape_closes_it_via_the_shared_listener(self):
        esc = self.ui[self.ui.index('e.key === "Escape"'):]
        self.assertIn("closeKbd()", esc[:esc.index("}", 0) + 40],
                      "Escape branch must call closeKbd alongside the other modals")

    def test_question_mark_branch_ignores_text_boxes(self):
        q = self.ui[self.ui.index('e.key === "?"'):]
        self.assertIn("input, textarea, select", q[:400],
                      "? typed into the composer is prose, not a shortcut")
        self.assertIn("isContentEditable", q[:400])
        self.assertIn("toggleKbd", q[:400])

    def test_sheet_documents_every_catalog_binding(self):
        """The HTML sheet and app.KEYBOARD_SHORTCUTS are two renderings of
        one catalog: every `keys` value in the Python constant must appear
        verbatim in the kbd-grid markup."""
        start = self.ui.index('id="kbdGrid"')
        grid = self.ui[start:self.ui.index("kbd-note", start)]
        for s in app.KEYBOARD_SHORTCUTS:
            self.assertIn(s["keys"], grid,
                          "catalog binding %r missing from the cheat sheet"
                          % s["keys"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
