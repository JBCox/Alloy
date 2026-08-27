"""Desktop delivery: the rung ladder, the approval channel, the arg fence.

Token-free and hardware-free — a FakeDesk stands in for desktop.Desktop, and
the approval channel is driven by dropping answer files into a temp dir.

Run: python tests/test_desktop_mcp.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop
import desktop_mcp


WINDOW = {"window_id": "w:1", "title": "Untitled - Notepad", "pid": 42,
          "exe": r"C:\Windows\notepad.exe"}


class FakeDesk:
    """Records calls; never touches a real window."""

    def __init__(self, windows=None):
        self.calls = []
        self._windows = windows if windows is not None else [WINDOW]

    def app_list(self):
        self.calls.append(("app_list", {}))
        return {"ok": True, "windows": self._windows, "text": "1 window(s)"}

    def screen_read(self, window_id):
        self.calls.append(("screen_read", {"window_id": window_id}))
        return {"ok": True, "observation_id": "obs1", "text": "tree here"}

    def screen_shot(self, window_id):
        self.calls.append(("screen_shot", {"window_id": window_id}))
        return {"ok": True, "observation_id": "obs1", "text": "shot here"}

    def click(self, based_on, element_id=None, x=None, y=None, button="left",
              strict_pixels=None):
        self.calls.append(("click", {"element_id": element_id, "x": x,
                                     "button": button,
                                     "strict_pixels": strict_pixels}))
        return {"ok": True, "text": "clicked"}

    def type_text(self, based_on, element_id, text, allow_password=False,
                  strict_pixels=None):
        self.calls.append(("type_text", {"element_id": element_id,
                                         "text": text,
                                         "allow_password": allow_password}))
        return {"ok": True, "text": "typed"}

    def scroll(self, based_on, direction="down", notches=1, element_id=None,
               strict_pixels=None):
        self.calls.append(("scroll", {"direction": direction}))
        return {"ok": True, "text": "scrolled"}

    def key(self, based_on, keys, strict_pixels=None):
        self.calls.append(("key", {"keys": keys}))
        return {"ok": True, "text": "pressed"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-deskmcp-")
        self._env = dict(os.environ)
        for k in list(os.environ):
            if k.startswith("ALLOY_"):
                del os.environ[k]
        self.desk = FakeDesk()
        self.run = desktop_mcp.Runner(desk=self.desk)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cite(self):
        return {"observation_id": "obs1", "window_id": "w:1"}


class RungTests(Base):
    def test_off_is_the_default_and_refuses_even_observers(self):
        """An absent env var must not mean 'on'. Nothing is the safe reading."""
        self.assertEqual(desktop_mcp.rung(), "off")
        for tool, args in (("app_list", {}), ("screen_read", {"window_id": "w:1"}),
                           ("click", {"based_on": self.cite()})):
            out = self.run.call(tool, args)
            self.assertIn("off for this conversation", out)
        self.assertEqual(self.desk.calls, [])

    def test_garbage_rung_falls_back_to_off(self):
        os.environ["ALLOY_DESKTOP_RUNG"] = "yes-please"
        self.assertEqual(desktop_mcp.rung(), "off")

    def test_ask_lets_observers_through_untouched(self):
        os.environ["ALLOY_DESKTOP_RUNG"] = "ask"
        out = self.run.call("screen_read", {"window_id": "w:1"})
        self.assertEqual(out, "tree here")
        # no approval directory was needed, because observers never ask
        self.assertIn(("screen_read", {"window_id": "w:1"}), self.desk.calls)

    def test_full_runs_a_mutator_with_no_channel_at_all(self):
        os.environ["ALLOY_DESKTOP_RUNG"] = "full"
        out = self.run.call("click", {"based_on": self.cite(),
                                      "element_id": "e1"})
        self.assertIn("clicked", out)

    def test_ask_with_no_channel_configured_denies(self):
        """Fail closed: a gate nobody is listening to is a gate that says no."""
        os.environ["ALLOY_DESKTOP_RUNG"] = "ask"
        out = self.run.call("click", {"based_on": self.cite()})
        self.assertIn("Refused", out)
        self.assertIn("no approval channel", out)
        self.assertNotIn("click", [c[0] for c in self.desk.calls])


class AllowlistTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["ALLOY_DESKTOP_RUNG"] = "allowlist"
        # deliberately NO approval dir: an allowlist hit must not need one,
        # and a miss must then fail closed.
        os.environ["ALLOY_DESKTOP_ALLOWLIST"] = json.dumps([r"Notepad$",
                                                            r"\\calc\.exe$"])

    def test_a_matching_window_proceeds_without_asking(self):
        out = self.run.call("click", {"based_on": self.cite(),
                                      "element_id": "e1"})
        self.assertIn("clicked", out)
        self.assertIn("pre-approved", out)

    def test_a_window_off_the_list_still_asks_and_therefore_denies(self):
        self.desk._windows = [dict(WINDOW, title="Online Banking - Edge",
                                   exe=r"C:\edge.exe")]
        out = self.run.call("click", {"based_on": self.cite()})
        self.assertIn("Refused", out)
        self.assertNotIn("click", [c[0] for c in self.desk.calls])

    def test_a_broken_pattern_is_dropped_not_widened(self):
        os.environ["ALLOY_DESKTOP_ALLOWLIST"] = json.dumps(["*bad(", "Notepad$"])
        self.assertTrue(desktop_mcp._allowlisted(WINDOW))
        self.assertFalse(desktop_mcp._allowlisted(
            {"title": "Something else", "exe": "x.exe"}))

    def test_an_empty_allowlist_matches_nothing(self):
        os.environ["ALLOY_DESKTOP_ALLOWLIST"] = "[]"
        self.assertFalse(desktop_mcp._allowlisted(WINDOW))


class ApprovalChannelTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["ALLOY_DESKTOP_RUNG"] = "ask"
        os.environ["ALLOY_DESKTOP_APPROVAL_DIR"] = self.tmp
        os.environ["ALLOY_DESKTOP_SEAT"] = "Claude"

    def _answer(self, allow, reason="", delay=0.05):
        """Play Alloy: wait for the request, then write the verdict."""
        def worker():
            for _ in range(200):
                reqs = [n for n in os.listdir(self.tmp) if n.endswith(".req")]
                if reqs:
                    rid = reqs[0][:-4]
                    with open(os.path.join(self.tmp, reqs[0]),
                              encoding="utf-8") as fh:
                        self.request = json.load(fh)
                    with open(os.path.join(self.tmp, rid + ".ans"), "w",
                              encoding="utf-8") as fh:
                        json.dump({"allow": allow, "reason": reason}, fh)
                    return
                time.sleep(0.01)
        t = threading.Thread(target=worker, daemon=True)
        time.sleep(delay) if False else None
        t.start()
        return t

    def test_an_approval_runs_the_action(self):
        t = self._answer(True, "Josh approved this.")
        out = self.run.call("click", {"based_on": self.cite(),
                                      "element_id": "e1"})
        t.join(timeout=5)
        self.assertIn("clicked", out)
        self.assertIn("click", [c[0] for c in self.desk.calls])

    def test_a_denial_does_not_run_the_action(self):
        t = self._answer(False, "Not that window.")
        out = self.run.call("click", {"based_on": self.cite()})
        t.join(timeout=5)
        self.assertIn("Refused", out)
        self.assertIn("Not that window.", out)
        self.assertNotIn("click", [c[0] for c in self.desk.calls])

    def test_the_card_names_the_window_and_the_action(self):
        """Josh's decision is only as good as this sentence."""
        t = self._answer(True)
        self.run.call("type_text", {"based_on": self.cite(),
                                    "element_id": "e1", "text": "hello world"})
        t.join(timeout=5)
        self.assertEqual(self.request["seat"], "Claude")
        self.assertEqual(self.request["action"], "type_text")
        self.assertIn("hello world", self.request["detail"])
        self.assertIn("Notepad", self.request["detail"])
        self.assertEqual(self.request["window"]["exe"],
                         r"C:\Windows\notepad.exe")

    def test_a_long_secret_is_not_pasted_whole_into_the_card(self):
        t = self._answer(True)
        self.run.call("type_text", {"based_on": self.cite(),
                                    "element_id": "e1", "text": "x" * 400})
        t.join(timeout=5)
        self.assertLess(len(self.request["detail"]), 200)

    def test_a_timeout_denies(self):
        desktop_mcp.APPROVAL_TIMEOUT, old = 0.3, desktop_mcp.APPROVAL_TIMEOUT
        try:
            out = self.run.call("click", {"based_on": self.cite()})
        finally:
            desktop_mcp.APPROVAL_TIMEOUT = old
        self.assertIn("Refused", out)
        self.assertIn("did not answer", out)
        self.assertNotIn("click", [c[0] for c in self.desk.calls])

    def test_junk_in_the_answer_file_denies(self):
        def worker():
            for _ in range(200):
                reqs = [n for n in os.listdir(self.tmp) if n.endswith(".req")]
                if reqs:
                    with open(os.path.join(self.tmp, reqs[0][:-4] + ".ans"),
                              "w", encoding="utf-8") as fh:
                        fh.write("{not json")
                    return
                time.sleep(0.01)
        threading.Thread(target=worker, daemon=True).start()
        desktop_mcp.APPROVAL_TIMEOUT, old = 0.6, desktop_mcp.APPROVAL_TIMEOUT
        try:
            out = self.run.call("click", {"based_on": self.cite()})
        finally:
            desktop_mcp.APPROVAL_TIMEOUT = old
        self.assertIn("Refused", out)


class ArgumentFenceTests(Base):
    def setUp(self):
        super().setUp()
        os.environ["ALLOY_DESKTOP_RUNG"] = "full"

    def test_the_model_cannot_turn_off_the_password_refusal(self):
        """allow_password is a SETTING, not an argument. If the model could
        name it, the password refusal would be advisory."""
        self.run.call("type_text", {"based_on": self.cite(),
                                    "element_id": "e1", "text": "hunter2",
                                    "allow_password": True})
        sent = dict(self.desk.calls[-1][1])
        self.assertFalse(sent["allow_password"])

    def test_the_model_cannot_turn_off_staleness_checking(self):
        self.run.call("click", {"based_on": self.cite(), "element_id": "e1",
                                "strict_pixels": False})
        self.assertIsNone(dict(self.desk.calls[-1][1])["strict_pixels"])

    def test_an_unknown_tool_is_refused_not_dispatched(self):
        self.assertIn("Unknown tool", self.run.call("rm_rf", {}))
        self.assertEqual(self.desk.calls, [])

    def test_every_schema_key_is_a_real_parameter_of_its_method(self):
        """The schema and the library signature must not drift: a key the
        method does not accept becomes a TypeError at the worst moment."""
        import inspect
        for name, _, schema in desktop_mcp.TOOLS:
            params = inspect.signature(getattr(desktop.Desktop, name)).parameters
            for key in schema["properties"]:
                self.assertIn(key, params, f"{name}.{key}")

    def test_the_safety_settings_are_absent_from_every_schema(self):
        for name, _, schema in desktop_mcp.TOOLS:
            for forbidden in ("allow_password", "strict_pixels", "self_pids",
                              "deny_windows"):
                self.assertNotIn(forbidden, schema["properties"], name)


class SelfPidTests(Base):
    def test_alloys_pid_is_read_from_the_environment(self):
        """It must be TOLD, never inferred: this server is a grandchild of a
        seat CLI, and desktop.alloy_pids() would climb its own ancestry."""
        os.environ["ALLOY_APP_PID"] = "1234, 5678"
        self.assertEqual(desktop_mcp._app_pids(), {1234, 5678})

    def test_junk_pids_are_ignored_rather_than_crashing(self):
        os.environ["ALLOY_APP_PID"] = "abc,,-1,99"
        self.assertEqual(desktop_mcp._app_pids(), {99})

    def test_no_pid_configured_is_an_empty_set_not_a_guess(self):
        self.assertEqual(desktop_mcp._app_pids(), set())


class RefusalPassthroughTests(Base):
    def test_a_library_refusal_reaches_the_model_as_a_refusal(self):
        os.environ["ALLOY_DESKTOP_RUNG"] = "full"

        class Refusing(FakeDesk):
            def click(self, **kw):
                raise desktop.DesktopRefused(
                    "Refused: stale observation - run screen_read again "
                    "before acting.", code=desktop.STALE_TREE)

        out = desktop_mcp.Runner(desk=Refusing()).call(
            "click", {"based_on": self.cite()})
        self.assertIn("Refused", out)
        self.assertIn("run screen_read again", out)


class RelayWiringTests(unittest.TestCase):
    """The relay half: how a seat is handed the server, and who answers."""

    def setUp(self):
        import relay
        self.relay = relay
        self.tmp = tempfile.mkdtemp(prefix="alloy-deskwire-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def agent(self, **kw):
        kw.setdefault("permission", "auto")
        return self.relay.ClaudeAgent(self.tmp, **kw)

    def mcp_config(self, agent):
        cmd = [str(c) for c in agent.build_cmd("hi")]
        if "--mcp-config" not in cmd:
            return None, cmd
        return json.loads(cmd[cmd.index("--mcp-config") + 1]), cmd

    def test_off_hands_over_no_server_at_all(self):
        cfg, cmd = self.mcp_config(self.agent(desktop="off"))
        self.assertEqual(cfg["mcpServers"], {})
        self.assertIn("--strict-mcp-config", cmd)

    def test_on_hands_over_exactly_one_server(self):
        for rung in ("ask", "allowlist", "full"):
            cfg, cmd = self.mcp_config(self.agent(desktop=rung))
            self.assertEqual(list(cfg["mcpServers"]), ["alloy_desktop"])
            self.assertIn("--strict-mcp-config", cmd)
            self.assertTrue(any("mcp__alloy_desktop" in c for c in cmd
                                if c.startswith("--allowedTools=")))

    def test_connectors_on_ADDS_the_server_rather_than_replacing_them(self):
        """Josh asked for his real servers; --strict-mcp-config here would
        silently delete the connectors he switched on."""
        cfg, cmd = self.mcp_config(self.agent(desktop="ask", connectors=True))
        self.assertEqual(list(cfg["mcpServers"]), ["alloy_desktop"])
        self.assertNotIn("--strict-mcp-config", cmd)

    def test_the_rung_travels_in_the_env_not_in_an_argument(self):
        spec = self.agent(desktop="allowlist",
                          desktop_allowlist=["Notepad$"]).desktop_server_spec()
        self.assertEqual(spec["env"]["ALLOY_DESKTOP_RUNG"], "allowlist")
        self.assertEqual(json.loads(spec["env"]["ALLOY_DESKTOP_ALLOWLIST"]),
                         ["Notepad$"])
        self.assertEqual(spec["env"]["ALLOY_APP_PID"], str(os.getpid()))

    def test_the_server_is_never_launched_with_pythonw(self):
        """pythonw.exe is a GUI-subsystem binary with no usable stdio, and
        this server speaks JSON-RPC over exactly that: it would connect and
        then say nothing. The app itself runs as pythonw."""
        spec = self.agent(desktop="ask").desktop_server_spec()
        self.assertNotIn("pythonw", os.path.basename(spec["command"]).lower())
        self.assertTrue(spec["args"][0].endswith("desktop_mcp.py"))

    def test_off_produces_no_spec(self):
        self.assertIsNone(self.agent(desktop="off").desktop_server_spec())

    def test_a_typo_is_OFF_not_a_grant(self):
        for junk in ("yes please", "ON!!", "sure", None, "", "read_only"):
            self.assertEqual(self.relay.normalize_desktop(junk), "off",
                             repr(junk))
        # ...but the real spellings still work, including the bare switch
        self.assertEqual(self.relay.normalize_desktop(True), "full")
        self.assertEqual(self.relay.normalize_desktop("unattended"), "full")

    # ---- the watcher -------------------------------------------------
    def _drain(self, agent, rid):
        stop = threading.Event()
        t = threading.Thread(target=agent._watch_desktop, args=(stop,),
                             daemon=True)
        t.start()
        ans = os.path.join(agent.desktop_dir(), rid + ".ans")
        for _ in range(200):
            if os.path.exists(ans):
                break
            time.sleep(0.02)
        stop.set()
        t.join(timeout=2)
        with open(ans, encoding="utf-8") as fh:
            return json.load(fh)

    def _queue(self, agent, rid, action="click"):
        with open(os.path.join(agent.desktop_dir(), rid + ".req"), "w",
                  encoding="utf-8") as fh:
            json.dump({"id": rid, "action": action, "detail": "d",
                       "window": {"title": "Notepad"}}, fh)

    def test_the_watcher_answers_through_the_desktop_callback(self):
        seen = []
        a = self.agent(desktop="ask", on_desktop_approval=lambda req, abort:
                       (seen.append(req.get("action")) or (True, "ok")))
        self._queue(a, "d1")
        verdict = self._drain(a, "d1")
        self.assertTrue(verdict["allow"])
        self.assertEqual(seen, ["click"])

    def test_a_STANDING_TURN_VERDICT_DOES_NOT_ANSWER_A_DESKTOP_REQUEST(self):
        """THE hole this separate axis exists to close.

        `_watch_approvals` short-circuits on `_turn_verdict`, so a "rest of
        this turn" that Josh said to an unrelated Bash prompt would otherwise
        pre-approve every click and keystroke that followed it. Allowed-once
        would be a lie the first time he used the convenient button.
        """
        asked = []
        a = self.agent(desktop="ask", on_desktop_approval=lambda req, abort:
                       (asked.append(req) or (False, "no")))
        a.set_turn_verdict(True)          # "allow rest of turn", for TOOLS
        self._queue(a, "d2")
        verdict = self._drain(a, "d2")
        self.assertFalse(verdict["allow"], "a tool verdict answered a click")
        self.assertEqual(len(asked), 1, "Josh was not asked about the click")

    def test_the_desktop_dir_is_not_the_tool_approval_dir(self):
        a = self.agent(desktop="ask")
        self.assertNotEqual(os.path.normcase(a.desktop_dir()),
                            os.path.normcase(a.approval_dir()))

    def test_a_raising_callback_denies_rather_than_hanging_the_seat(self):
        def boom(req, abort):
            raise RuntimeError("ui gone")
        a = self.agent(desktop="ask", on_desktop_approval=boom)
        self._queue(a, "d3")
        verdict = self._drain(a, "d3")
        self.assertFalse(verdict["allow"])
        self.assertIn("approval failed", verdict["reason"])

    def test_no_callback_wired_denies(self):
        a = self.agent(desktop="ask")
        self._queue(a, "d4")
        self.assertFalse(self._drain(a, "d4")["allow"])

    def test_the_abort_handed_over_is_CALLABLE(self):
        """Same lesson as the tool-approval seam, one axis over."""
        captured = {}
        a = self.agent(desktop="ask", on_desktop_approval=lambda req, abort:
                       (captured.update(ok=callable(abort),
                                        during=bool(abort and abort()))
                        or (True, "y")))
        self._queue(a, "d5")
        self._drain(a, "d5")
        self.assertTrue(captured["ok"])
        self.assertFalse(captured["during"])

    def test_the_rung_survives_a_reopen_and_a_legacy_chat_comes_back_OFF(self):
        """A reopened chat must show what it actually ran with — and the one
        direction that must never happen by accident is a chat quietly
        holding the screen, so an old meta with no key at all is off."""
        seat = lambda i: {"provider": "claude", "slot_id": f"s{i}",
                          "model": None, "effort": None, "label": f"Claude {i}"}
        meta = {"v": self.relay.META_VERSION, "title": "x", "created": "",
                "updated": "", "ended": False, "workspace": self.tmp,
                "topic": "x", "turns": 1, "rnd": 0, "max": 1,
                "permission": "auto", "desktop": "allowlist",
                "desktop_allowlist": ["Notepad$"],
                "seats": [seat(1), seat(2)]}
        agent = self.relay.rehydrate(meta, self.tmp)["agents"][0]
        self.assertEqual(agent.desktop, "allowlist")
        self.assertEqual(agent.desktop_allowlist, ["Notepad$"])

        legacy = {k: v for k, v in meta.items()
                  if k not in ("desktop", "desktop_allowlist")}
        reopened = self.relay.rehydrate(legacy, self.tmp)["agents"][0]
        self.assertEqual(reopened.desktop, "off")
        self.assertIsNone(reopened.desktop_server_spec())

    def test_the_note_says_nothing_when_desktop_is_off(self):
        self.assertEqual(
            self.relay.desktop_capability_clause(self.agent(desktop="off")), [])
        for rung in ("ask", "allowlist", "full"):
            clause = self.relay.desktop_capability_clause(
                self.agent(desktop=rung))
            self.assertTrue(clause and "DESKTOP" in clause[0].upper())
        # only `full` may promise no prompt
        self.assertIn("no prompt", self.relay.desktop_capability_clause(
            self.agent(desktop="full"))[0])
        self.assertIn("waits for Josh", self.relay.desktop_capability_clause(
            self.agent(desktop="ask"))[0])


def main():
    unittest.main(verbosity=1, exit=False)


if __name__ == "__main__":
    main()
