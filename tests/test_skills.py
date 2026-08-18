"""Skills authoring + MCP management (engine and bridge).

Token-free: no CLI is invoked. PROVIDERS' skills_dir / mcp entries are
redirected into a temp tree, and the CLI-backed MCP paths are exercised
through the pure parsers plus a stubbed runner.

Run:  python tests/test_skills.py
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
from test_app_headless import FakeWindow


class SkillsBase(unittest.TestCase):
    """Redirect every provider's skills dir into a temp tree. PROVIDERS is
    mutated IN PLACE and restored — never rebound, because AGENT_TYPES and
    the tests' own imports hold references to the same dict."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-skills-")
        self._saved = {}
        for pid in ("claude", "gpt", "gemini"):
            self._saved[pid] = (PROVIDERS := relay.PROVIDERS)[pid]["skills_dir"]
            relay.PROVIDERS[pid]["skills_dir"] = os.path.join(self.tmp, pid,
                                                              "skills")
        self._saved_grok = relay.PROVIDERS["grok"]["skills_dir"]

    def tearDown(self):
        for pid, old in self._saved.items():
            relay.PROVIDERS[pid]["skills_dir"] = old
        relay.PROVIDERS["grok"]["skills_dir"] = self._saved_grok
        shutil.rmtree(self.tmp, ignore_errors=True)

    def put(self, pid, name, text):
        p = os.path.join(relay.PROVIDERS[pid]["skills_dir"], name, "SKILL.md")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # newline="" so a test writing CRLF gets CRLF, not CR-CRLF
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return p


class SkillNameTests(unittest.TestCase):
    def test_accepts_plain_lowercase_names(self):
        for n in ("ai-chat", "release-checklist", "x", "a1-b2"):
            self.assertTrue(relay.valid_skill_name(n), n)

    def test_rejects_anything_that_could_escape_or_confuse(self):
        for n in ("../evil", "..", ".", "", "Upper", "with space",
                  "with.dot", "under_score", "/abs", "C:\\abs",
                  "a" * 65, None, 5):
            self.assertFalse(relay.valid_skill_name(n), repr(n))

    def test_skill_path_refuses_bad_names(self):
        self.assertIsNone(relay.skill_path("claude", "../evil"))
        self.assertIsNone(relay.skill_path("nosuch", "fine-name"))


class FrontmatterTests(unittest.TestCase):
    def test_round_trips_description_and_body(self):
        text = relay.render_skill("demo", "Use when X happens.", "# Demo\n\nDo it.")
        desc, body = relay.parse_skill(text)
        self.assertEqual(desc, "Use when X happens.")
        self.assertIn("# Demo", body)
        self.assertIn("Do it.", body)

    def test_description_newlines_are_folded(self):
        # a raw newline in the description breaks frontmatter for every CLI
        text = relay.render_skill("demo", "line one\nline two", "body")
        self.assertIn("description: line one line two\n", text)

    def test_reads_yaml_block_scalar_descriptions(self):
        # agy's own skills use ">-" folded blocks
        text = ("---\nname: agy-style\ndescription: >-\n  First part of it.\n"
                "  Second part.\n---\n\n# Body\n")
        desc, body = relay.parse_skill(text)
        self.assertEqual(desc, "First part of it. Second part.")
        self.assertIn("# Body", body)

    def test_file_without_frontmatter_still_yields_a_body(self):
        desc, body = relay.parse_skill("# Just markdown\n\nno frontmatter")
        self.assertEqual(desc, "")
        self.assertIn("Just markdown", body)


class SkillCrudTests(SkillsBase):
    def test_writes_to_every_requested_provider(self):
        res = relay.write_skill("demo-skill", "Use when demoing.",
                                "# Demo\n\nSteps here.",
                                ["claude", "gpt", "gemini"])
        self.assertEqual(res, {"claude": None, "gpt": None, "gemini": None})
        listed = relay.list_skills()
        for pid in ("claude", "gpt", "gemini"):
            self.assertEqual([r["name"] for r in listed[pid]], ["demo-skill"])
            self.assertEqual(listed[pid][0]["description"], "Use when demoing.")

    def test_installed_copies_are_byte_identical(self):
        relay.write_skill("same", "d", "body", ["claude", "gpt", "gemini"])
        blobs = set()
        for pid in ("claude", "gpt", "gemini"):
            with open(relay.skill_path(pid, "same"), encoding="utf-8") as f:
                blobs.add(f.read())
        self.assertEqual(len(blobs), 1)
        # ...which is what makes the sha a valid divergence signal
        shas = {r["sha"] for pid in ("claude", "gpt", "gemini")
                for r in relay.list_skills()[pid]}
        self.assertEqual(len(shas), 1)

    def test_divergent_copies_show_different_shas(self):
        relay.write_skill("drift", "d", "original", ["claude", "gpt"])
        self.put("gpt", "drift", relay.render_skill("drift", "d", "EDITED"))
        listed = relay.list_skills()
        self.assertNotEqual(listed["claude"][0]["sha"], listed["gpt"][0]["sha"])

    def test_write_rejects_a_bad_name_before_touching_disk(self):
        with self.assertRaises(ValueError):
            relay.write_skill("../evil", "d", "b", ["claude"])
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, "claude", "skills")))

    def test_read_skill_returns_none_when_absent(self):
        self.assertIsNone(relay.read_skill("claude", "nope"))

    def test_delete_removes_only_the_named_skill(self):
        relay.write_skill("keep", "d", "b", ["claude"])
        relay.write_skill("gone", "d", "b", ["claude"])
        res = relay.delete_skill("gone", ["claude"])
        self.assertEqual(res, {"claude": None})
        self.assertEqual([r["name"] for r in relay.list_skills()["claude"]],
                         ["keep"])

    def test_delete_refuses_a_folder_with_no_skill_file(self):
        stray = os.path.join(relay.PROVIDERS["claude"]["skills_dir"], "stray")
        os.makedirs(stray)
        with open(os.path.join(stray, "notes.txt"), "w") as f:
            f.write("important")
        res = relay.delete_skill("stray", ["claude"])
        self.assertEqual(res["claude"], "not installed")
        self.assertTrue(os.path.isdir(stray))       # untouched

    def test_missing_dirs_and_dotfolders_are_not_errors(self):
        # codex keeps .system in its skills dir; nothing is installed yet
        listed = relay.list_skills()
        self.assertEqual(listed["gemini"], [])
        os.makedirs(os.path.join(relay.PROVIDERS["gpt"]["skills_dir"],
                                 ".system", "x"))
        self.assertEqual(relay.list_skills()["gpt"], [])

    def test_unreadable_skill_is_reported_not_raised(self):
        p = self.put("claude", "weird", "\x00\x01 not really text")
        rows = relay.list_skills()["claude"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "weird")   # listed, never fatal
        self.assertTrue(os.path.isfile(p))

    def test_grok_is_never_manageable(self):
        self.assertNotIn("grok", relay.manageable_providers())

    def test_lowercase_skill_md_is_found(self):
        # 5 of 6 real skills on this machine use lowercase skill.md
        d = os.path.join(relay.PROVIDERS["claude"]["skills_dir"], "lower")
        os.makedirs(d)
        with open(os.path.join(d, "skill.md"), "w", encoding="utf-8") as f:
            f.write(relay.render_skill("lower", "Lower case file.", "body"))
        rows = relay.list_skills()["claude"]
        self.assertEqual([r["name"] for r in rows], ["lower"])
        self.assertEqual(rows[0]["description"], "Lower case file.")
        self.assertEqual(relay.read_skill("claude", "lower")[0],
                         "Lower case file.")

    def test_bom_does_not_swallow_the_description(self):
        # a real skill here has a BOM, and its description reads back as '---'
        self.put("claude", "bommed",
                 "﻿" + relay.render_skill("bommed", "Real description.",
                                               "body"))
        self.assertEqual(relay.list_skills()["claude"][0]["description"],
                         "Real description.")

    def test_line_endings_alone_are_not_divergence(self):
        text = relay.render_skill("crlf", "d", "line one\nline two")
        self.put("claude", "crlf", text)
        self.put("gpt", "crlf", "﻿" + text.replace("\n", "\r\n"))
        listed = relay.list_skills()
        self.assertEqual(listed["claude"][0]["sha"], listed["gpt"][0]["sha"])


class SkillTreeTests(SkillsBase):
    """A skill is a FOLDER: 5 of 6 real ones carry scripts/ or references/
    that the markdown links to. A SKILL.md-only copy is a broken skill."""

    def make_rich(self, pid="claude", name="rich"):
        root = os.path.join(relay.PROVIDERS[pid]["skills_dir"], name)
        os.makedirs(os.path.join(root, "scripts"))
        os.makedirs(os.path.join(root, "references"))
        with open(os.path.join(root, "skill.md"), "w", encoding="utf-8") as f:
            f.write(relay.render_skill(name, "Rich skill.", "See scripts/go.py"))
        with open(os.path.join(root, "scripts", "go.py"), "w") as f:
            f.write("print('hi')\n")
        with open(os.path.join(root, "references", "notes.md"), "w") as f:
            f.write("# notes\n")
        return root

    def test_sync_copies_the_whole_tree(self):
        self.make_rich()
        res = relay.write_skill("rich", "Rich skill.", "See scripts/go.py",
                                ["gpt", "gemini"], source="claude")
        self.assertEqual(res, {"gpt": None, "gemini": None})
        for pid in ("gpt", "gemini"):
            root = os.path.dirname(relay.skill_path(pid, "rich"))
            self.assertTrue(os.path.isfile(
                os.path.join(root, "scripts", "go.py")), pid)
            self.assertTrue(os.path.isfile(
                os.path.join(root, "references", "notes.md")), pid)
            self.assertTrue(os.path.isfile(os.path.join(root, "SKILL.md")))

    def test_sync_does_not_leave_two_skill_files(self):
        self.make_rich()
        relay.write_skill("rich", "Rich skill.", "body", ["gpt"],
                          source="claude")
        root = os.path.dirname(relay.skill_path("gpt", "rich"))
        mds = [n for n in os.listdir(root) if n.lower() == "skill.md"]
        self.assertEqual(len(mds), 1, mds)

    def test_editing_in_place_keeps_existing_sidecars(self):
        self.make_rich()
        relay.write_skill("rich", "Rich skill.", "edited body", ["claude"])
        root = os.path.dirname(relay.skill_path("claude", "rich"))
        self.assertTrue(os.path.isfile(os.path.join(root, "scripts", "go.py")))
        self.assertIn("edited body", relay.read_skill("claude", "rich")[1])

    def test_extras_are_counted_for_the_ui(self):
        self.make_rich()
        row = relay.list_skills()["claude"][0]
        self.assertEqual(row["extras"], 2)      # go.py + notes.md

    def test_oversized_tree_is_refused_not_half_copied(self):
        self.make_rich()
        old = relay.SKILL_TREE_MAX_FILES
        relay.SKILL_TREE_MAX_FILES = 1
        try:
            res = relay.write_skill("rich", "d", "b", ["gpt"], source="claude")
        finally:
            relay.SKILL_TREE_MAX_FILES = old
        self.assertIn("too large", res["gpt"])
        self.assertFalse(os.path.exists(
            os.path.dirname(relay.skill_path("gpt", "rich"))))

    def test_failed_write_leaves_no_temp_folders(self):
        self.make_rich()
        relay.write_skill("rich", "d", "b", ["gpt"], source="claude")
        root = relay.PROVIDERS["gpt"]["skills_dir"]
        self.assertEqual([n for n in os.listdir(root) if "alloy-" in n], [])


class McpParseTests(unittest.TestCase):
    def test_claude_line_format_keeps_colons_inside_names(self):
        out = ("Checking MCP server health…\n\n"
               "claude.ai Gmail: https://gmailmcp.googleapis.com/mcp/v1 - Connected\n"
               "plugin:superpowers-chrome:chrome: node C:/x/index.js - Connected\n")
        rows = relay._parse_mcp_line_list(out)
        self.assertEqual([r["name"] for r in rows],
                         ["claude.ai Gmail", "plugin:superpowers-chrome:chrome"])
        self.assertTrue(rows[1]["detail"].startswith("node C:/x"))

    def test_codex_json_reads_the_nested_transport(self):
        out = json.dumps([{"name": "node_repl", "enabled": True,
                           "transport": {"type": "stdio",
                                         "command": r"C:\bin\node_repl.exe"}}])
        rows = relay._parse_mcp_json_list(out)
        self.assertEqual(rows, [{"name": "node_repl",
                                 "detail": r"C:\bin\node_repl.exe"}])

    def test_codex_json_marks_disabled_servers(self):
        out = json.dumps([{"name": "off", "enabled": False,
                           "transport": {"command": "x"}}])
        self.assertIn("(disabled)", relay._parse_mcp_json_list(out)[0]["detail"])

    def test_garbage_parses_to_nothing(self):
        self.assertEqual(relay._parse_mcp_json_list("not json"), [])
        self.assertEqual(relay._parse_mcp_line_list(""), [])


class GeminiMcpFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-mcpfile-")
        self.path = os.path.join(self.tmp, "mcp_config.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"GitKraken": {
                "type": "stdio", "command": "gk.exe", "args": ["mcp"]}}}, f)
        self._old = relay.PROVIDERS["gemini"]["mcp"]
        relay.PROVIDERS["gemini"]["mcp"] = {"kind": "file", "path": self.path}

    def tearDown(self):
        relay.PROVIDERS["gemini"]["mcp"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def servers(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)["mcpServers"]

    def test_add_preserves_unrelated_servers(self):
        self.assertIsNone(relay.add_mcp("gemini", "ctx7", "npx",
                                        args=["-y", "@upstash/context7-mcp"]))
        s = self.servers()
        self.assertIn("ctx7", s)
        # the entry that was already there must survive byte-for-byte
        self.assertEqual(s["GitKraken"], {"type": "stdio", "command": "gk.exe",
                                          "args": ["mcp"]})

    def test_remove_only_the_named_server(self):
        relay.add_mcp("gemini", "ctx7", "npx")
        self.assertIsNone(relay.remove_mcp("gemini", "ctx7"))
        self.assertEqual(list(self.servers()), ["GitKraken"])

    def test_remove_unknown_is_a_clear_error(self):
        self.assertEqual(relay.remove_mcp("gemini", "nope"), "No such server.")

    def test_http_server_records_a_url(self):
        relay.add_mcp("gemini", "remote", None, transport="http",
                      url="https://example.com/mcp")
        self.assertEqual(self.servers()["remote"]["url"],
                         "https://example.com/mcp")

    def test_bad_names_and_missing_targets_are_refused(self):
        self.assertIn("names", relay.add_mcp("gemini", "../x", "cmd"))
        self.assertIn("command", relay.add_mcp("gemini", "ok-name", ""))
        self.assertIn("URL", relay.add_mcp("gemini", "ok-name", None,
                                           transport="http", url=""))

    def test_mutations_invalidate_the_prefix_cache(self):
        # claude_mcp_prefixes() caches for the process lifetime; a server added
        # mid-session must not stay invisible to the connectors switch
        relay._CLAUDE_MCP = ["mcp__stale"]
        relay.add_mcp("gemini", "ctx7", "npx")
        self.assertIsNone(relay._CLAUDE_MCP)
        relay._CLAUDE_MCP = ["mcp__stale"]
        relay.remove_mcp("gemini", "ctx7")
        self.assertIsNone(relay._CLAUDE_MCP)


class BridgeTests(SkillsBase):
    def setUp(self):
        super().setUp()
        self.api = app.Api()
        self.api._window = FakeWindow()

    def test_skill_crud_round_trips_through_the_bridge(self):
        r = self.api.save_skill("bridge-skill", "Use when bridging.",
                                "# Bridge\n\nbody", ["claude", "gpt"])
        self.assertTrue(r.get("ok"), r)
        got = self.api.get_skills()
        names = [s["name"] for s in got["skills"]]
        self.assertIn("bridge-skill", names)
        row = next(s for s in got["skills"] if s["name"] == "bridge-skill")
        self.assertEqual(sorted(row["providers"]), ["claude", "gpt"])
        self.assertFalse(row["diverged"])
        one = self.api.read_skill("claude", "bridge-skill")
        self.assertEqual(one["description"], "Use when bridging.")
        self.assertIn("# Bridge", one["body"])
        self.assertTrue(self.api.remove_skill("bridge-skill",
                                              ["claude", "gpt"]).get("ok"))
        self.assertNotIn("bridge-skill",
                         [s["name"] for s in self.api.get_skills()["skills"]])

    def test_get_skills_merges_providers_and_flags_divergence(self):
        relay.write_skill("shared", "d", "same", ["claude", "gpt"])
        relay.write_skill("only-claude", "d", "x", ["claude"])
        self.put("gpt", "shared", relay.render_skill("shared", "d", "DIFFERENT"))
        rows = {s["name"]: s for s in self.api.get_skills()["skills"]}
        self.assertTrue(rows["shared"]["diverged"])
        self.assertFalse(rows["only-claude"]["diverged"])
        self.assertEqual(rows["only-claude"]["missing"],
                         [p for p in relay.manageable_providers()
                          if p != "claude"])

    def test_bad_name_is_an_instant_error_not_a_crash(self):
        r = self.api.save_skill("../evil", "d", "b", ["claude"])
        self.assertIn("error", r)

    def test_get_skills_runs_no_subprocess(self):
        # it is called on the pywebview bridge thread, where a subprocess
        # deadlocks the whole app
        import subprocess as sp
        real_run, real_popen = sp.run, sp.Popen

        def boom(*a, **k):
            raise AssertionError("subprocess on the bridge thread")
        sp.run, sp.Popen = boom, boom
        try:
            self.api.get_skills()
        finally:
            sp.run, sp.Popen = real_run, real_popen


if __name__ == "__main__":
    unittest.main(verbosity=1)
