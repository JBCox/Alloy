"""Ox (OpenCode CLI / OpenCode Zen) stays a truthful fourth seat.

Token-free: every fact below was observed live against opencode 1.18.21 on
2026-08-22 and is pinned here as a contract, so a CLI upgrade that changes the
vocabulary fails in the suite instead of in a conversation.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _events(*objs):
    """The CLI's real shape: JSONL, one event per line."""
    return "\n".join(json.dumps(o) for o in objs) + "\n"


class RegistryTests(unittest.TestCase):
    def test_ox_is_a_seatable_provider(self):
        self.assertIn("ox", relay.PROVIDERS)
        self.assertIs(relay.PROVIDERS["ox"]["agent"], relay.OpenCodeAgent)
        # agent=None would list it in Accounts only, like grok
        self.assertIn("ox", relay.AGENT_TYPES)

    def test_install_hint_names_the_package_that_provides_the_cli(self):
        self.assertIn("opencode-ai", relay.PROVIDERS["ox"]["install_hint"])

    def test_ox_can_deliver_files(self):
        # opencode's build agent writes in the process cwd (verified), so the
        # supervisor may assign it a file-producing workstream.
        self.assertIn("ox", relay.FILE_WRITER_PROVIDERS)


class BuildCmdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-ox-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agent(self, **kw):
        return relay.OpenCodeAgent(self.tmp, **kw)

    def test_model_is_always_pinned(self):
        # Unpinned, opencode falls back to its configured default, which can
        # be a PAID Zen model - an auth failure on a seat the Accounts panel
        # correctly reports as signed in.
        cmd = self.agent().build_cmd("hello")
        self.assertIn("-m", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], relay.OX_DEFAULT_MODEL)

    def test_chosen_model_wins(self):
        cmd = self.agent(model="opencode/big-pickle").build_cmd("hello")
        self.assertEqual(cmd[cmd.index("-m") + 1], "opencode/big-pickle")

    def test_prompt_is_the_trailing_positional(self):
        self.assertEqual(self.agent().build_cmd("hello")[-1], "hello")

    def test_read_only_and_ask_run_the_plan_agent(self):
        for level in ("read_only", "ask"):
            cmd = self.agent(permission=level).build_cmd("hello")
            self.assertIn("--agent", cmd, level)
            self.assertEqual(cmd[cmd.index("--agent") + 1], "plan", level)
            self.assertNotIn("--auto", cmd, level)

    def test_writing_rungs_preapprove_so_a_piped_turn_cannot_stall(self):
        for level in ("auto", "full"):
            cmd = self.agent(permission=level).build_cmd("hello")
            self.assertIn("--auto", cmd, level)
            self.assertNotIn("plan", cmd, level)

    def test_resume_names_this_seats_session_never_continue(self):
        a = self.agent()
        a.session_id = "ses_abc123"
        cmd = a.build_cmd("hello")
        self.assertIn("--session", cmd)
        self.assertEqual(cmd[cmd.index("--session") + 1], "ses_abc123")
        # `--continue` resumes "the last session" - wrong with several seats -
        # and before the subcommand it silently prints help and exits 0.
        self.assertNotIn("--continue", cmd)
        self.assertNotIn("-c", cmd)

    def test_first_turn_has_no_session_flag(self):
        self.assertNotIn("--session", self.agent().build_cmd("hello"))


class PermissionEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-ox-env-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def perm(self, level):
        env = relay.OpenCodeAgent(self.tmp, permission=level).extra_env()
        self.assertIn("OPENCODE_CONFIG_CONTENT", env)
        return json.loads(env["OPENCODE_CONFIG_CONTENT"])["permission"]

    def test_read_only_denies_every_write_path(self):
        perm = self.perm("read_only")
        self.assertEqual(perm.get("edit"), "deny")
        self.assertEqual(perm.get("bash"), "deny")

    def test_auto_is_the_working_folder_and_nothing_else(self):
        # The one rung whose whole meaning is a boundary: writes inside the
        # workspace, refusal outside it.
        self.assertEqual(self.perm("auto").get("external_directory"), "deny")

    def test_full_is_the_only_unbounded_rung(self):
        perm = self.perm("full")
        self.assertEqual(perm.get("*"), "allow")
        self.assertNotIn("external_directory", perm)

    def test_every_rung_below_full_keeps_the_workspace_boundary(self):
        for level in ("read_only", "ask", "auto"):
            self.assertEqual(self.perm(level).get("external_directory"),
                             "deny", level)

    def test_the_gate_reaches_the_child_process(self):
        # A permission config the turn never puts in the environment is not a
        # gate at all, so the seam itself is pinned.
        self.assertEqual(relay.Agent.extra_env(
            relay.Agent(self.tmp)), {})
        with open(os.path.join(ROOT, "relay.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("env.update(self.extra_env() or {})", src)


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-ox-parse-")
        self.a = relay.OpenCodeAgent(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reply_and_session_id_come_out_of_the_stream(self):
        out = _events(
            {"type": "step_start", "sessionID": "ses_1",
             "part": {"id": "p0", "type": "step-start"}},
            {"type": "text", "sessionID": "ses_1",
             "part": {"id": "p1", "type": "text", "text": "SEAT_OK"}},
            {"type": "step_finish", "sessionID": "ses_1",
             "part": {"id": "p2", "reason": "stop"}},
        )
        self.assertEqual(self.a.parse(out), "SEAT_OK")
        self.assertEqual(self.a.session_id, "ses_1")

    def test_separate_text_parts_are_joined_once(self):
        out = _events(
            {"type": "text", "sessionID": "s", "part": {"id": "p1", "text": "one"}},
            {"type": "text", "sessionID": "s", "part": {"id": "p2", "text": "two"}},
        )
        self.assertEqual(self.a.parse(out), "one\n\ntwo")

    def test_a_repeated_part_id_keeps_the_last_text_not_every_prefix(self):
        # 1.18.21 emits one complete text event per block. If a later version
        # streams growing deltas instead, concatenating them would repeat the
        # whole reply; keying on part id is what prevents that.
        out = _events(
            {"type": "text", "sessionID": "s", "part": {"id": "p1", "text": "Hel"}},
            {"type": "text", "sessionID": "s", "part": {"id": "p1", "text": "Hello"}},
        )
        self.assertEqual(self.a.parse(out), "Hello")

    def test_junk_lines_and_empty_output_never_raise(self):
        self.assertEqual(self.a.parse("not json\n\n"), "")
        self.assertEqual(self.a.parse(""), "")
        self.assertEqual(self.a.parse("{bad json\n"), "")

    def test_failure_sentence_uses_the_error_event(self):
        out = _events({"type": "error", "sessionID": "s",
                       "error": {"name": "UnknownError",
                                 "data": {"message": "Unexpected server error.",
                                          "ref": "err_680b99d8"}}})
        msg = self.a.describe_failure(out, "")
        self.assertIn("UnknownError", msg)
        self.assertIn("Unexpected server error.", msg)
        self.assertIn("err_680b99d8", msg)   # what OpenCode support asks for


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-ox-act-")
        self.a = relay.OpenCodeAgent(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tool(self, name, inp):
        return json.dumps({"type": "tool_use", "sessionID": "s",
                           "part": {"id": "p", "type": "tool", "tool": name,
                                    "state": {"status": "completed",
                                              "input": inp}}})

    def test_edits_carry_the_raw_path_for_the_sink_to_confine(self):
        acts = self.a.activity(self.tool("write", {"filePath": r"C:\w\out.txt"}))
        self.assertEqual(acts[0]["kind"], "edit")
        self.assertEqual(acts[0]["path_raw"], r"C:\w\out.txt")
        self.assertIn("out.txt", acts[0]["text"])

    def test_reads_are_narrated_without_a_path(self):
        acts = self.a.activity(self.tool("read", {"filePath": r"C:\w\note.txt"}))
        self.assertEqual(acts[0]["kind"], "read")
        # a read is not an edit: nothing for the file rail to confine
        self.assertNotIn("path_raw", acts[0])

    def test_shell_commands_are_narrated_as_commands(self):
        acts = self.a.activity(self.tool("bash", {"command": "ls -la"}))
        self.assertEqual(acts[0]["kind"], "command")
        self.assertTrue(acts[0]["text"].startswith("$ "))

    def test_search_tools_share_one_kind(self):
        for name, inp in (("glob", {"pattern": "**/*.py"}),
                          ("grep", {"pattern": "TODO"}),
                          ("webfetch", {"url": "https://example.com"})):
            acts = self.a.activity(self.tool(name, inp))
            self.assertEqual(acts[0]["kind"], "search", name)

    def test_an_unknown_tool_is_still_narrated(self):
        acts = self.a.activity(self.tool("wibble", {}))
        self.assertEqual(acts[0]["kind"], "tool")
        self.assertIn("wibble", acts[0]["text"])

    def test_unknown_shapes_return_nothing_rather_than_raising(self):
        self.assertEqual(self.a.activity("not json"), ())
        self.assertEqual(self.a.activity("{bad"), ())
        self.assertEqual(self.a.activity('{"type":"text","part":{}}'), ())
        self.assertEqual(self.a.activity(self.tool("write", {})), [])


class ProbeTests(unittest.TestCase):
    def test_zero_credentials_is_signed_in_not_signed_out(self):
        # THE point of this provider: the free Zen models need no account, so
        # reporting "0 credentials" as signed_out would grey out a seat that
        # works perfectly (verified live 2026-08-22).
        fake = mock.Mock(stdout="Credentials\n0 credentials", stderr="",
                         returncode=0)
        with mock.patch.object(relay, "_run_probe", return_value=fake):
            st = relay.probe_opencode()
        self.assertEqual(st["state"], "signed_in")
        self.assertIn("no sign-in", st["detail"])

    def test_credentials_present_is_reported_too(self):
        fake = mock.Mock(stdout="2 credentials", stderr="", returncode=0)
        with mock.patch.object(relay, "_run_probe", return_value=fake):
            st = relay.probe_opencode()
        self.assertEqual(st["state"], "signed_in")
        self.assertIn("2", st["detail"])

    def test_missing_cli_is_the_one_blocking_state(self):
        with mock.patch.object(relay, "_run_probe", side_effect=RuntimeError):
            st = relay.probe_opencode()
        self.assertEqual(st["state"], "not_installed")

    def test_a_broken_probe_never_guesses_signed_out(self):
        with mock.patch.object(relay, "_run_probe", side_effect=OSError("boom")):
            st = relay.probe_opencode()
        self.assertEqual(st["state"], "unknown")


class CatalogAndUiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-ox-cat-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ox_alpha_leads_the_free_catalog(self):
        self.assertEqual(relay.OX_FREE_MODELS[0]["id"], relay.OX_DEFAULT_MODEL)
        for m in relay.OX_FREE_MODELS:
            self.assertTrue(m["id"].startswith("opencode/"), m)
            self.assertTrue(m["label"])

    def test_model_details_read_real_reasoning_options(self):
        cache = os.path.join(self.tmp, "models.json")
        with open(cache, "w", encoding="utf-8") as f:
            json.dump({"opencode": {"models": {
                "with-effort": {"reasoning_options": [
                    {"type": "effort", "values": ["low", "high", "max"]}],
                    "limit": {"context": 1000000}},
                # a toggle is reasoning on/off, NOT a level: it maps onto no
                # --variant value, so it must yield an empty list
                "toggle-only": {"reasoning_options": [{"type": "toggle"}]},
                "plain": {},
            }}}, f)
        details = relay.ox_model_details(cache)
        self.assertEqual(details["with-effort"]["levels"], ["low", "high", "max"])
        self.assertEqual(details["with-effort"]["context"], 1000000)
        self.assertEqual(details["toggle-only"]["levels"], [])
        self.assertEqual(details["plain"]["levels"], [])

    def test_a_missing_catalog_offers_no_levels_rather_than_crashing(self):
        self.assertEqual(relay.ox_model_details(
            os.path.join(self.tmp, "nope.json")), {})

    def test_default_level_prefers_high_then_the_middle(self):
        self.assertEqual(relay.ox_default_level(["low", "high", "max"]), "high")
        self.assertEqual(relay.ox_default_level(["a", "b", "c"]), "b")
        self.assertEqual(relay.ox_default_level([]), "")

    def test_app_publishes_the_catalog_even_when_the_probe_never_ran(self):
        import app
        cfg = app.Api._fallback_config()
        self.assertTrue(cfg["ox_models"])
        self.assertEqual(cfg["ox_default_model"], relay.OX_DEFAULT_MODEL)

    def test_the_ui_can_seat_ox(self):
        with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('<option value="ox">Ox</option>', src)
        self.assertIn("--ox:", src)                       # colour token
        self.assertIn(".seat[data-provider=ox]", src)
        self.assertIn('seat.provider === "ox"', src)      # its own fill branch

    def test_thinking_levels_come_from_each_model_not_one_shared_list(self):
        # The levels really do differ across this catalog, and opencode does
        # NOT validate --variant - an unsupported level is accepted and
        # silently ignored - so a shared list would quietly do nothing on most
        # models. models.dev is the source; the UI fills per selected model.
        with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("function fillOxLevels", src)
        # anchor on the fillSeat branch specifically: `seat.provider === "ox"`
        # also appears in the naming helper
        branch = src.split('} else if (seat.provider === "ox") {', 1)[1]                     .split("} else {", 1)[0]
        self.assertIn("fillOxLevels(eSel, mSel.value", branch)
        # changing the model must re-fill the levels, like the GPT seat
        self.assertIn("fillOxLevels(eSel, mSel.value, eSel.value)", branch)

    def test_a_model_with_no_reasoning_control_shows_no_picker(self):
        with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as f:
            src = f.read()
        body = src.split("function fillOxLevels", 1)[1].split("function fillGptLevels", 1)[0]
        self.assertIn("eSel.hidden = !levels.length", body)

    def test_the_adapter_sends_the_effort_it_was_given(self):
        agent = relay.OpenCodeAgent(self.tmp, effort="max")
        cmd = agent.build_cmd("hello")
        self.assertIn("--variant", cmd)
        self.assertEqual(cmd[cmd.index("--variant") + 1], "max")

    def test_no_variant_flag_when_the_model_has_no_levels(self):
        self.assertNotIn("--variant",
                         relay.OpenCodeAgent(self.tmp).build_cmd("hello"))


class ShimResolutionTests(unittest.TestCase):
    """A prompt must reach the CLI whole.

    opencode installs a .cmd shim that launches a NATIVE binary, a shape
    resolve_cmd did not recognise, so it fell back to `cmd /c` - which
    truncates every argument at the first newline. It shipped that way: four Ox
    seats held a conversation in which every relayed message arrived as
    "Ox Alpha 4 said:" and nothing else, and they spent their turns politely
    telling each other the messages were empty (2026-08-22).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-shim-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _shim(self, name, body, target=None):
        shim = os.path.join(self.tmp, name)
        with open(shim, "w", encoding="utf-8") as f:
            f.write(body)
        if target:
            full = os.path.join(self.tmp, target)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write("binary")
        return shim

    def test_a_native_binary_shim_resolves_to_the_binary(self):
        shim = self._shim(
            "tool.cmd",
            '@ECHO off\r\n"%dp0%\\node_modules\\pkg\\bin\\tool.exe"   %*\r\n',
            target=os.path.join("node_modules", "pkg", "bin", "tool.exe"))
        with mock.patch.object(relay.shutil, "which", return_value=shim):
            cmd = relay.resolve_cmd(["tool", "run", "line one\nline two"])
        self.assertTrue(cmd[0].lower().endswith("tool.exe"), cmd[0])
        # cmd.exe must NEVER see it: that is the whole point
        self.assertNotEqual(cmd[0].lower(), "cmd")
        self.assertNotIn("/c", cmd)
        # and the multi-line argument survives as ONE argv element
        self.assertIn("line one\nline two", cmd)

    def test_a_node_script_shim_still_resolves_to_node(self):
        shim = self._shim(
            "js.cmd", '@ECHO off\r\n"%dp0%\\node_modules\\p\\cli.js"   %*\r\n',
            target=os.path.join("node_modules", "p", "cli.js"))
        with mock.patch.object(relay.shutil, "which",
                               side_effect=lambda n: shim if n == "js" else "node"):
            cmd = relay.resolve_cmd(["js", "go"])
        self.assertTrue(cmd[0].endswith("node"), cmd[0])
        self.assertTrue(cmd[1].endswith("cli.js"), cmd[1])

    def test_an_unrecognised_shim_still_falls_back(self):
        # single-line args only, but better than refusing to run at all
        shim = self._shim("weird.cmd", "@ECHO off\r\nsomething else %*\r\n")
        with mock.patch.object(relay.shutil, "which", return_value=shim):
            cmd = relay.resolve_cmd(["weird", "go"])
        self.assertEqual(cmd[:2], ["cmd", "/c"])

    def test_a_shim_naming_a_missing_binary_does_not_pretend(self):
        shim = self._shim("gone.cmd",
                          '@ECHO off\r\n"%dp0%\\bin\\gone.exe"   %*\r\n')
        with mock.patch.object(relay.shutil, "which", return_value=shim):
            cmd = relay.resolve_cmd(["gone", "go"])
        self.assertEqual(cmd[:2], ["cmd", "/c"])

    def test_the_real_opencode_shim_never_routes_through_cmd(self):
        if not relay.shutil.which("opencode"):
            self.skipTest("opencode CLI not installed")
        cmd = relay.resolve_cmd(["opencode", "run"])
        self.assertNotEqual(cmd[0].lower(), "cmd", cmd)


class SeatNamingTests(unittest.TestCase):
    """A gateway is not an identity: the seat is named for its model."""

    def test_the_provider_is_opencode_not_one_of_its_models(self):
        self.assertEqual(relay.PROVIDERS["ox"]["label"], "OpenCode")
        self.assertEqual(relay.OpenCodeAgent.name, "OpenCode")

    def test_seats_are_named_for_the_model_they_run(self):
        self.assertEqual(
            relay.OpenCodeAgent.seat_name("opencode/x-preview-f-free"),
            "Ox Alpha")
        self.assertEqual(
            relay.OpenCodeAgent.seat_name("opencode/nemotron-3-ultra-free"),
            "Nemotron 3 Ultra")

    def test_an_unknown_model_still_beats_the_gateway_name(self):
        # paid Zen model, or one newer than this catalog
        self.assertEqual(relay.OpenCodeAgent.seat_name("opencode/gpt-5.6-sol"),
                         "gpt-5.6-sol")
        self.assertEqual(relay.OpenCodeAgent.seat_name(), "OpenCode")

    def test_a_vendor_provider_is_still_named_for_the_vendor(self):
        # Claude is Claude whichever model it runs
        self.assertEqual(relay.ClaudeAgent.seat_name("claude-opus-5"), "Claude")

    def test_ordinals_count_seats_sharing_a_name(self):
        labels = relay.assign_labels([
            ("ox", None, "opencode/x-preview-f-free"),
            ("ox", None, "opencode/nemotron-3-ultra-free"),
            ("ox", None, "opencode/x-preview-f-free"),
            ("claude", None), ("claude", None)])
        self.assertEqual(labels, ["Ox Alpha", "Nemotron 3 Ultra", "Ox Alpha 2",
                                  "Claude", "Claude 2"])

    def test_two_tuple_callers_still_work(self):
        # the model is optional: older call sites pass (provider, label)
        self.assertEqual(relay.assign_labels([("claude", None), ("gpt", "Bob")]),
                         ["Claude", "Bob"])

    def test_the_ui_derives_the_same_name(self):
        # the engine only receives TYPED labels, so an auto name that differs
        # between the two would put a different roster in the transcript
        with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("function seatBaseName", src)
        self.assertIn('ox: "OpenCode"', src)
        block = src.split("function seatBaseName", 1)[1][:600]
        self.assertIn("uiCfg.ox_models", block)


class OxRunsTheRoomTests(unittest.TestCase):
    """Ox must be able to occupy every seat AND do the routing work.

    A provider that can only speak when another provider picks the speaker is
    not a full participant, and a room of Ox seats that quietly spends a
    Claude call for its side work is not the all-Ox room Josh asked for.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-ox-room-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state(self, **kw):
        base = {"workspace": self.tmp}
        base.update(kw)
        return base

    def test_ox_can_be_the_moderator(self):
        mod = relay.build_moderator(self.state(moderator={"provider": "ox"}))
        self.assertIsInstance(mod, relay.OpenCodeAgent)
        # no model in the spec: the adapter still pins one, never the CLI default
        self.assertEqual(mod.build_cmd("x")[mod.build_cmd("x").index("-m") + 1],
                         relay.OX_DEFAULT_MODEL)

    def test_ox_can_be_the_supervisor(self):
        sup = relay.build_supervisor(self.state(
            supervisor={"provider": "ox", "model": "opencode/big-pickle"}))
        self.assertIsInstance(sup, relay.OpenCodeAgent)
        cmd = sup.build_cmd("x")
        self.assertEqual(cmd[cmd.index("-m") + 1], "opencode/big-pickle")

    def test_the_digest_follows_an_ox_moderator(self):
        agent = relay.build_digest_agent(self.state(moderator={"provider": "ox"}))
        self.assertIsInstance(agent, relay.OpenCodeAgent)

    def test_an_ox_only_room_never_falls_back_to_claude(self):
        # the brief is the last relay-authored call that used to be hardcoded
        self.assertEqual(relay.helper_spec(["ox", "ox"]), {"provider": "ox"})
        self.assertEqual(
            relay.helper_spec(["ox"], {"provider": "ox",
                                       "model": "opencode/hy3-free"}),
            {"provider": "ox", "model": "opencode/hy3-free"})

    def test_an_ox_supervisor_owns_the_side_calls_too(self):
        # moderator and supervisor are one job under two labels, and a Build
        # Together room sets ONLY state["supervisor"] - so this is what used
        # to hand every digest to claude while Ox ran the room.
        agent = relay.build_digest_agent(self.state(supervisor={"provider": "ox"}))
        self.assertIsInstance(agent, relay.OpenCodeAgent)
        self.assertEqual(relay.helper_spec(["claude"], None, {"provider": "ox"}),
                         {"provider": "ox"})

    def test_a_claude_room_keeps_its_cheap_default(self):
        # no model in the returned spec, so synthesize_brief still resolves
        # claude-haiku-4-5 rather than inheriting a seat's Opus
        self.assertEqual(relay.helper_spec(["claude", "gpt"]),
                         {"provider": "claude"})

    def test_an_unusable_moderator_spec_is_ignored_not_obeyed(self):
        self.assertEqual(relay.helper_spec(["gpt"], {"provider": "nope"}),
                         {"provider": "gpt"})
        self.assertEqual(relay.helper_spec([]), {})

    def test_both_front_ends_hand_the_brief_the_rooms_own_helper(self):
        # A resolver nothing calls is not a fix, so both call sites are pinned.
        for name in ("relay.py", "app.py"):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                src = f.read()
            self.assertIn("spec=helper_spec(", src, name)

    def test_the_moderator_picker_is_built_from_the_registry(self):
        # Hand-kept <option> lists are exactly why Ox shipped seatable but
        # unable to moderate; the picker must follow PROVIDERS like the seat
        # picker does, so the next adapter needs no UI edit at all.
        with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("function syncProviderPicker", src)
        self.assertIn('syncProviderPicker($("modProv"), seatable)', src)
        self.assertIn('syncProviderPicker($("addSeatProvider"), seatable)', src)
        # and it must compare LABELS, not just how many options there are: the
        # static markup ships one per provider, so a count check matches on
        # the first paint and the hand-written label never gets replaced
        body = src.split("function syncProviderPicker", 1)[1][:900]
        self.assertIn("o.textContent", body)

    def test_the_moderator_picker_can_show_ox(self):
        with open(os.path.join(ROOT, "ui", "index.html"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('p === "ox"', src)          # fillModerator branch
        # switching provider in place must restore the box the Ox branch hides
        head = src.split("function fillModerator", 1)[1][:700]
        self.assertIn("eSel.hidden = false", head)


if __name__ == "__main__":
    unittest.main(verbosity=2)
