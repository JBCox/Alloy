"""The command table behind the composer's slash autocomplete.

relay.COMMANDS is the one machine-readable answer to "what slash commands
exist". Nothing derives it: dispatch_command is an if/elif chain and
HELP_TEXT is hand-written prose, so THIS SUITE is the sync mechanism — it
asserts the table, the chain and the help string all name the same set, in
both directions, and that the UI reads the table through get_config rather
than keeping a JS copy (the HELP_TEXT drift rule).

Token-free: no CLI calls, no subprocesses.
"""

import inspect
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay          # noqa: E402
import app            # noqa: E402

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "ui", "index.html")


def table_names():
    return [c["name"] for c in relay.COMMANDS]


def chain_names():
    """Every command name dispatch_command's chain actually compares against.

    Extracted from the source, so a branch added without a table entry (or a
    table entry with no branch) fails here rather than shipping a menu that
    offers a command the engine does not know — or hides one it does.
    """
    src = inspect.getsource(relay.dispatch_command)
    names = set(re.findall(r'cmd == "([a-z]+)"', src))
    for tup in re.findall(r'cmd in \(([^)]*)\)', src):
        names.update(re.findall(r'"([a-z]+)"', tup))
    return names


class TableShapeTests(unittest.TestCase):
    def test_every_entry_has_name_args_hint(self):
        for c in relay.COMMANDS:
            self.assertTrue(c.get("name"), c)
            self.assertIn("args", c, c)
            self.assertTrue(c.get("hint"), c)

    def test_names_are_lowercase_and_unique(self):
        names = table_names()
        self.assertEqual(names, [n.lower() for n in names])
        self.assertEqual(len(names), len(set(names)))


class SyncTests(unittest.TestCase):
    def test_every_table_entry_is_in_help_text(self):
        for name in table_names():
            # not a bare substring: "/turn" must not be satisfied by "/turns"
            # (the --unattended --help lesson: substring tests are prefix
            # tests until proven otherwise)
            self.assertRegex(relay.HELP_TEXT, r"/%s(?![\w-])" % re.escape(name),
                             "COMMANDS names /%s but /help never mentions it"
                             % name)

    def test_every_help_command_is_in_the_table(self):
        # "(?<!\w)/" keeps prose like "claude/gpt/gemini" from reading as
        # commands — those slashes follow a word character.
        in_help = set(re.findall(r"(?<![\w])/([a-z]+)", relay.HELP_TEXT))
        self.assertEqual(in_help, set(table_names()),
                         "HELP_TEXT and COMMANDS disagree")

    def test_every_table_entry_has_a_dispatch_branch(self):
        chain = chain_names()
        for name in table_names():
            self.assertIn(name, chain,
                          "COMMANDS offers /%s but dispatch_command has no "
                          "branch for it" % name)

    def test_every_dispatch_branch_is_in_the_table(self):
        self.assertEqual(chain_names(), set(table_names()),
                         "dispatch_command and COMMANDS disagree")


class ExposureTests(unittest.TestCase):
    def test_fallback_config_carries_the_table(self):
        cfg = app.Api._fallback_config()
        self.assertEqual([c["name"] for c in cfg["commands"]], table_names())

    def test_fallback_rows_are_copies_not_the_table_itself(self):
        # the bridge hands these to JS; a caller mutating a row must not be
        # editing relay's own table
        cfg = app.Api._fallback_config()
        cfg["commands"][0]["name"] = "mutated"
        self.assertNotEqual(relay.COMMANDS[0]["name"], "mutated")

    def test_ui_reads_the_table_from_config(self):
        with open(UI, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("SLASH_CMDS = uiCfg.commands || []", src,
                      "the UI must take its command vocabulary from "
                      "get_config, never a JS copy")

    def test_ui_keeps_no_command_table_of_its_own(self):
        # The one legitimate spelling of a command name in the UI is routing
        # ("/"-prefix checks); a JS array of {name, hint} command objects
        # would be the drift this table exists to prevent.
        with open(UI, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotRegex(
            src, r'SLASH_CMDS\s*=\s*\[\s*\{',
            "SLASH_CMDS must never be seeded with a literal table")


class ScopeWordingTests(unittest.TestCase):
    def test_scoped_commands_name_their_scope(self):
        scoped = {c["name"]: c.get("scope") for c in relay.COMMANDS
                  if c.get("scope")}
        # the three room-gated commands the engine refuses outside their room
        for name in ("ceiling", "checkin", "limits", "objective"):
            self.assertIn(name, scoped,
                          "/%s is gated engine-side; the menu must say so "
                          "rather than hide it" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
