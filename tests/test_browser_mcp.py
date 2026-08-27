"""Browser delivery: the site fence, the rung ladder, the curated republish.

Token-free and browser-free. A FakeVendor stands in for the
chrome-devtools-mcp child, so every rule below is exercised without launching
node or Chrome — including the two that were proven against the real vendor
first and are pinned here so a refactor cannot quietly undo them:

* the fence is ALWAYS emitted, and an empty site list emits deny-all rather
  than nothing (a missing flag is no fence at all);
* the fence must PROVE itself on the first call, because the vendor silently
  accepts an unknown flag — a one-character typo in the flag name leaves
  `file://` reachable and looks identical from every other angle.

Run: python tests/test_browser_mcp.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser_mcp


_UNSET = object()

BLOCKED = ("Unable to navigate in the selected page: Navigation to %s is "
           "blocked by blocklist/allowlist rules.")


class FakeTool:
    def __init__(self, name, required=(), props=(), description="vendor prose"):
        self.name = name
        self.description = description
        self.inputSchema = {
            "type": "object",
            "properties": {key: {"type": "string"} for key in props},
            "required": list(required),
            # The vendor really does ship this true — measured 2026-08-26 —
            # which is exactly why the republish forces it false.
            "additionalProperties": True,
        }


class FakeResult:
    def __init__(self, text, is_error=False):
        self.content = [FakeBlock(text)]
        self.isError = is_error


class FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeVendor:
    """Stands in for the ClientSession talking to chrome-devtools-mcp.

    `fenced` False models the measured failure the self-test exists to catch:
    a vendor that accepted the flag, said nothing, and enforces nothing.
    """

    def __init__(self, tools=None, fenced=True, url="https://example.com/"):
        self.tools = tools if tools is not None else default_tools()
        self.fenced = fenced
        self.url = url
        self.calls = []
        self.raise_on = set()

    async def list_tools(self):
        return type("Listed", (), {"tools": self.tools})()

    async def call_tool(self, name, args=None):
        # A real vendor does I/O here. Without a yield this coroutine runs to
        # completion the moment it is awaited, no task can interleave, and
        # every concurrency test below would silently be a sequential one.
        await asyncio.sleep(0)
        self.calls.append((name, dict(args or {})))
        if name in self.raise_on:
            raise RuntimeError("the browser went away")
        if name == "list_pages":
            return FakeResult("## Pages\n1: Example (%s) [selected]" % self.url)
        if name == "navigate_page":
            url = (args or {}).get("url") or ""
            allowed = self.fenced is False or url.startswith("https://example.com")
            if allowed:
                return FakeResult("Successfully navigated to %s." % url)
            return FakeResult(BLOCKED % url)
        return FakeResult("vendor did %s" % name)


def default_tools():
    """A vendor list matching the real 1.7.0 shapes we depend on."""
    out = []
    for name, (_kind, keep) in browser_mcp.PUBLISH.items():
        required = {
            "click": ["uid"], "drag": ["from_uid", "to_uid"],
            "fill": ["uid", "value"], "fill_form": ["elements"],
            "hover": ["uid"], "press_key": ["key"], "type_text": ["text"],
            "handle_dialog": ["action"], "upload_file": ["uid", "filePath"],
            "evaluate_script": ["function"], "wait_for": ["text"],
            "close_page": ["pageId"], "select_page": ["pageId"],
            "new_page": ["url"], "resize_page": ["width", "height"],
            "get_console_message": ["msgid"],
        }.get(name, [])
        # The vendor offers MORE keys than Alloy keeps; include the dropped
        # ones so the republish is genuinely filtering something.
        props = list(keep) + ["filePath", "initScript", "extraHttpHeaders"]
        out.append(FakeTool(name, required=required, props=props))
    for name in browser_mcp.WITHHELD:
        out.append(FakeTool(name, required=["filePath"], props=["filePath"]))
    return out


def run(coro):
    return asyncio.run(coro)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-brwmcp-")
        self._env = dict(os.environ)
        for key in list(os.environ):
            if key.startswith("ALLOY_"):
                del os.environ[key]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def proxy(self, rung="ask", sites=("https://example.com/*",),
              fenced=True, workspace=_UNSET, vendor=None):
        os.environ["ALLOY_BROWSER_RUNG"] = rung
        os.environ["ALLOY_BROWSER_SITES"] = json.dumps(list(sites))
        vendor = vendor or FakeVendor(fenced=fenced)
        # Sentinel, not `or`: "" is a REAL value here (no working
        # folder at all) and `or` would silently turn it into one.
        prox = browser_mcp.Proxy(
            vendor=vendor,
            workspace=self.tmp if workspace is _UNSET else workspace)
        run(prox.load())
        return prox, vendor

    def text(self, result):
        return browser_mcp._text_of(result)


# ------------------------------------------------------------ the fence ----

class FenceArgvTests(Base):
    """Rules 1 and 2, at the one place the command line is built."""

    def test_the_allowlist_flag_is_spelled_exactly_once_and_correctly(self):
        argv = browser_mcp.vendor_argv(["https://example.com/*"])
        self.assertEqual(argv.count("--allowedUrlPattern"), 1)
        # The measured failure was the PLURAL. Nothing may drift into it.
        self.assertNotIn("--allowedUrlPatterns", argv)

    def test_a_blocklist_is_never_emitted(self):
        # The two flags are mutually exclusive: passing both means the vendor
        # never handshakes at all, so the capability silently disappears.
        for sites in ([], ["https://example.com/*"],
                      ["http://127.0.0.1:*/*", "https://a.test/*"]):
            self.assertNotIn("--blockedUrlPattern",
                             browser_mcp.vendor_argv(sites))

    def test_an_empty_site_list_denies_everything_rather_than_nothing(self):
        argv = browser_mcp.vendor_argv([])
        self.assertIn("--allowedUrlPattern", argv)
        self.assertIn(browser_mcp.DENY_ALL_PATTERN, argv)

    def test_every_site_gets_its_own_flag(self):
        argv = browser_mcp.vendor_argv(["https://a.test/*", "https://b.test/*"])
        self.assertEqual(argv.count("--allowedUrlPattern"), 2)
        self.assertIn("https://b.test/*", argv)

    def test_the_phone_home_defaults_are_switched_off(self):
        argv = browser_mcp.vendor_argv(["https://a.test/*"])
        for flag in ("--usageStatistics", "--performanceCrux"):
            self.assertEqual(argv[argv.index(flag) + 1], "false", flag)

    def test_network_headers_are_redacted_and_paths_stay_restricted(self):
        argv = browser_mcp.vendor_argv(["https://a.test/*"])
        self.assertEqual(argv[argv.index("--redactNetworkHeaders") + 1], "true")
        # Its ABSENCE is what keeps the vendor's file writes inside temp.
        self.assertNotIn("--allowUnrestrictedPaths", argv)
        self.assertNotIn("--allow-unrestricted-paths", argv)

    def test_a_throwaway_profile_is_used(self):
        self.assertIn("--isolated", browser_mcp.vendor_argv(["https://a.test/*"]))


class SiteClassifierTests(Base):
    """Rule 3's companion: what Josh may configure, and what is refused."""

    def keep(self, *patterns):
        return browser_mcp.classify_sites(list(patterns), webhook_port=None)[0]

    def why(self, pattern):
        rejected = browser_mcp.classify_sites([pattern], webhook_port=None)[1]
        return rejected[0][1] if rejected else ""

    def test_ordinary_https_sites_are_kept(self):
        self.assertEqual(self.keep("https://example.com/*"),
                         ["https://example.com/*"])

    def test_structural_schemes_are_refused_by_name(self):
        for pattern, word in (("file:///C:/*", "file"),
                              ("chrome://version", "chrome"),
                              ("devtools://x/*", "devtools"),
                              ("data:text/html,x", "data"),
                              ("javascript:1", "javascript"),
                              ("view-source:https://a.test/", "view-source")):
            why = self.why(pattern)
            self.assertTrue(why, pattern)
            self.assertIn(word, why)

    def test_a_scheme_less_pattern_is_refused_rather_than_guessed(self):
        # Guessing https:// for "example.com/*" would silently widen or
        # narrow a boundary Josh wrote by hand.
        self.assertIn("http://", self.why("example.com/*"))
        self.assertEqual(self.keep("example.com/*"), [])

    def test_non_web_schemes_are_refused(self):
        self.assertIn("http and https", self.why("ftp://x/*"))

    def test_a_pattern_on_alloys_own_webhook_port_is_refused(self):
        kept, rejected, _loop = browser_mcp.classify_sites(
            ["http://127.0.0.1:8765/*"], webhook_port=8765)
        self.assertEqual(kept, [])
        self.assertIn("webhook", rejected[0][1])
        # A DIFFERENT port on the same host is ordinary local dev and stays.
        kept, _r, _l = browser_mcp.classify_sites(
            ["http://127.0.0.1:3000/*"], webhook_port=8765)
        self.assertEqual(kept, ["http://127.0.0.1:3000/*"])

    def test_loopback_is_detected_by_resolution_not_by_spelling(self):
        # `lvh.me` is a public DNS name that resolves to 127.0.0.1. No list of
        # spellings could keep up, which is why the check resolves.
        for pattern in ("http://127.0.0.1:*/*", "http://localhost:3000/*",
                        "https://*/*", "http://lvh.me/*"):
            self.assertTrue(browser_mcp._reaches_loopback(pattern), pattern)
        self.assertFalse(browser_mcp._reaches_loopback("https://example.com/*"))

    def test_an_unresolvable_host_counts_as_loopback(self):
        # Fail closed: a name we could not check is not one we may assume safe.
        self.assertTrue(browser_mcp._reaches_loopback(
            "https://definitely-not-a-real-host.invalid/*"))

    def test_a_refused_pattern_never_reaches_the_command_line(self):
        kept, _r, _l = browser_mcp.classify_sites(
            ["file:///C:/*", "https://ok.test/*"], webhook_port=None)
        argv = browser_mcp.vendor_argv(kept)
        self.assertNotIn("file:///C:/*", argv)
        self.assertIn("https://ok.test/*", argv)


# ------------------------------------------------------- the self-test -----

class FenceSelfTestTests(Base):
    """Rule 3: the fence demonstrates itself or the capability does not exist."""

    def test_a_live_fence_proves_itself_and_the_call_goes_through(self):
        prox, vendor = self.proxy(rung="read", fenced=True)
        result = run(prox.call("navigate_page", {"url": "https://example.com/"}))
        self.assertIn("Successfully navigated", self.text(result))
        # The probe ran FIRST, and it aimed at a URL no allowlist can contain.
        self.assertEqual(vendor.calls[0][0], "navigate_page")
        self.assertEqual(vendor.calls[0][1]["url"], browser_mcp.FENCE_PROBE_URL)

    def test_an_absent_fence_latches_the_whole_session_dead(self):
        prox, vendor = self.proxy(rung="read", fenced=False)
        first = run(prox.call("list_pages", {}))
        self.assertIn("was actually enforcing and it was not",
                      self.text(first))
        # Latched: every later call answers the same way, and NOTHING else
        # ever reaches the vendor.
        before = len(vendor.calls)
        for tool in ("take_snapshot", "navigate_page", "click"):
            again = run(prox.call(tool, {"url": "https://example.com/",
                                         "uid": "1_2"}))
            self.assertIn("switched off for the rest", self.text(again))
        self.assertEqual(len(vendor.calls), before)

    def test_the_probe_runs_once_not_per_call(self):
        prox, vendor = self.proxy(rung="read", fenced=True)
        for _ in range(3):
            run(prox.call("list_pages", {}))
        probes = [c for c in vendor.calls
                  if c[0] == "navigate_page"
                  and c[1].get("url") == browser_mcp.FENCE_PROBE_URL]
        self.assertEqual(len(probes), 1)

    def test_a_vendor_that_raises_during_the_probe_latches_too(self):
        vendor = FakeVendor()
        vendor.raise_on.add("navigate_page")
        prox, _v = self.proxy(rung="read", vendor=vendor)
        result = run(prox.call("list_pages", {}))
        self.assertIn("could not verify", self.text(result))

    def test_the_probe_target_can_never_be_allowlisted(self):
        # If the probe URL could be configured in, the self-test would pass
        # against a fence that lets it through. `file:` is refused outright.
        kept, rejected, _l = browser_mcp.classify_sites(
            [browser_mcp.FENCE_PROBE_URL + "*"], webhook_port=None)
        self.assertEqual(kept, [])
        self.assertTrue(rejected)


# --------------------------------------------------------- the republish ---

class CurateTests(Base):
    def test_additional_properties_is_forced_false(self):
        published, _dropped = browser_mcp.curate(default_tools())
        self.assertTrue(published)
        for spec in published:
            self.assertIs(spec["inputSchema"]["additionalProperties"], False)

    def test_only_the_kept_keys_survive(self):
        published, _dropped = browser_mcp.curate(default_tools())
        by_name = {spec["name"]: spec for spec in published}
        nav = by_name["navigate_page"]["inputSchema"]["properties"]
        self.assertIn("url", nav)
        # Script injection under another name.
        self.assertNotIn("initScript", nav)
        self.assertNotIn("extraHttpHeaders",
                         by_name["emulate"]["inputSchema"]["properties"])
        self.assertNotIn("filePath",
                         by_name["take_screenshot"]["inputSchema"]["properties"])

    def test_the_vendors_own_prose_is_kept(self):
        published, _dropped = browser_mcp.curate(default_tools())
        self.assertTrue(all(spec["description"] for spec in published))

    def test_withheld_tools_are_dropped_with_a_reason(self):
        _published, dropped = browser_mcp.curate(default_tools())
        names = dict(dropped)
        for name in browser_mcp.WITHHELD:
            self.assertIn(name, names)
            self.assertTrue(names[name])

    def test_a_tool_the_vendor_lacks_is_dropped_not_invented(self):
        tools = [t for t in default_tools() if t.name != "click"]
        published, dropped = browser_mcp.curate(tools)
        self.assertNotIn("click", [spec["name"] for spec in published])
        self.assertIn("does not offer it", dict(dropped)["click"])

    def test_a_renamed_required_argument_drops_the_tool(self):
        # The case a key whitelist alone cannot see: the vendor renames a
        # required argument on a version bump. Publishing it would offer a
        # tool that cannot work.
        tools = default_tools()
        for tool in tools:
            if tool.name == "click":
                tool.inputSchema["required"] = ["element_uid"]
        published, dropped = browser_mcp.curate(tools)
        self.assertNotIn("click", [spec["name"] for spec in published])
        self.assertIn("element_uid", dict(dropped)["click"])

    def test_upload_file_keeps_its_required_path(self):
        # RC5's mistake, mechanically: a blanket filePath strip would have
        # silently disabled this tool.
        published, _dropped = browser_mcp.curate(default_tools())
        by_name = {spec["name"]: spec for spec in published}
        self.assertIn("filePath",
                      by_name["upload_file"]["inputSchema"]["properties"])

    def test_scripting_is_withheld_when_loopback_is_reachable(self):
        published, dropped = browser_mcp.curate(default_tools(),
                                                allow_script=False)
        self.assertNotIn("evaluate_script", [s["name"] for s in published])
        self.assertIn("loopback", dict(dropped)["evaluate_script"])
        published, _d = browser_mcp.curate(default_tools(), allow_script=True)
        self.assertIn("evaluate_script", [s["name"] for s in published])

    def test_a_loopback_site_withholds_scripting_end_to_end(self):
        prox, _v = self.proxy(rung="full", sites=["http://127.0.0.1:*/*"])
        self.assertNotIn("evaluate_script",
                         [spec["name"] for spec in prox.published])
        # Withheld, and the refusal says WHY -- "unknown tool" would send a
        # seat hunting for a spelling mistake that is not there.
        result = run(prox.call("evaluate_script", {"function": "() => 1"}))
        text = self.text(result)
        self.assertIn("not available here", text)
        self.assertIn("loopback", text)


# ------------------------------------------------------------- the rungs --

class RungTests(Base):
    def test_anything_unrecognised_reads_as_off(self):
        for value in ("yes-please", "", "FULLish", "1"):
            os.environ["ALLOY_BROWSER_RUNG"] = value
            self.assertEqual(browser_mcp.rung(), "off")

    def test_off_refuses_everything_including_observers(self):
        prox, vendor = self.proxy(rung="off")
        for tool in ("list_pages", "navigate_page", "click"):
            result = run(prox.call(tool, {"url": "https://example.com/"}))
            self.assertIn("Browser control is off", self.text(result))
        self.assertEqual(vendor.calls, [])

    def test_read_allows_looking_and_refuses_touching(self):
        prox, _v = self.proxy(rung="read")
        for tool, args in (("list_pages", {}), ("take_snapshot", {}),
                           ("navigate_page", {"url": "https://example.com/"})):
            self.assertNotIn("Refused", self.text(run(prox.call(tool, args))))
        for tool, args in (("click", {"uid": "1"}),
                           ("fill", {"uid": "1", "value": "x"}),
                           ("press_key", {"key": "Enter"}),
                           ("evaluate_script", {"function": "() => 1"})):
            result = self.text(run(prox.call(tool, args)))
            self.assertIn("look-only", result, tool)

    def test_read_never_asks_anybody(self):
        # There is nothing to approve at this rung, so no request may be
        # written — a card Josh cannot act on is worse than a plain refusal.
        os.environ["ALLOY_BROWSER_APPROVAL_DIR"] = self.tmp
        prox, _v = self.proxy(rung="read")
        run(prox.call("click", {"uid": "1"}))
        self.assertEqual(os.listdir(self.tmp), [])

    def test_full_acts_with_no_prompt(self):
        os.environ["ALLOY_BROWSER_APPROVAL_DIR"] = self.tmp
        prox, vendor = self.proxy(rung="full")
        result = run(prox.call("click", {"uid": "1_2"}))
        self.assertIn("vendor did click", self.text(result))
        self.assertEqual(os.listdir(self.tmp), [])
        self.assertIn(("click", {"uid": "1_2"}), vendor.calls)

    def test_ask_blocks_on_josh_and_relays_his_answer(self):
        os.environ["ALLOY_BROWSER_APPROVAL_DIR"] = self.tmp
        prox, vendor = self.proxy(rung="ask")
        answers = answer_thread(self.tmp, allow=True)
        try:
            result = run(prox.call("click", {"uid": "1_2"}))
        finally:
            answers.set()
        self.assertIn("vendor did click", self.text(result))
        self.assertIn("(Josh approved this.)", self.text(result))

    def test_ask_refuses_when_josh_says_no_and_never_calls_the_vendor(self):
        os.environ["ALLOY_BROWSER_APPROVAL_DIR"] = self.tmp
        prox, vendor = self.proxy(rung="ask")
        answers = answer_thread(self.tmp, allow=False)
        try:
            result = run(prox.call("click", {"uid": "1_2"}))
        finally:
            answers.set()
        self.assertIn("Refused", self.text(result))
        self.assertNotIn("click", [name for name, _a in vendor.calls])

    def test_no_approval_channel_denies_rather_than_proceeding(self):
        # The whole fail-closed rule in one test: a gate that opens because
        # nobody was listening is worse than no gate.
        prox, vendor = self.proxy(rung="ask")
        result = run(prox.call("click", {"uid": "1_2"}))
        self.assertIn("no approval channel", self.text(result))
        self.assertNotIn("click", [name for name, _a in vendor.calls])

    def test_the_card_names_the_page_and_what_will_happen(self):
        os.environ["ALLOY_BROWSER_APPROVAL_DIR"] = self.tmp
        prox, _v = self.proxy(rung="ask")
        seen = []
        answers = answer_thread(self.tmp, allow=False, seen=seen)
        try:
            run(prox.call("fill", {"uid": "1_2", "value": "hunter2"}))
        finally:
            answers.set()
        self.assertEqual(len(seen), 1)
        detail = seen[0]["detail"]
        self.assertIn("hunter2", detail)
        self.assertIn("https://example.com/", detail)
        self.assertEqual(seen[0]["kind"], "browser")

    def test_observers_do_not_cost_a_page_lookup(self):
        os.environ["ALLOY_BROWSER_APPROVAL_DIR"] = self.tmp
        prox, vendor = self.proxy(rung="ask")
        run(prox.call("take_snapshot", {}))
        self.assertNotIn("list_pages", [name for name, _a in vendor.calls])


class ConcurrencyTests(Base):
    """An MCP server is handed overlapping requests. Two rules follow."""

    def test_waiting_on_josh_does_not_block_the_event_loop(self):
        """The approval wait polls a directory for up to three minutes, and
        this loop is also the one draining the vendor child's stdio. If the
        wait blocked it, Chrome's whole session would stall while Josh read
        the card — and a full pipe would wedge the child outright."""
        os.environ["ALLOY_BROWSER_APPROVAL_DIR"] = self.tmp
        prox, _v = self.proxy(rung="ask")

        async def both():
            # Josh answers only after `gate` is set, and nothing sets it until
            # the observer has been served. The assertion is on TIME, not on
            # order: with an inline wait the observer still succeeds — it just
            # succeeds SLOW_ANSWER seconds late, which is exactly the stall a
            # real three-minute card would inflict on Chrome's session.
            gate = threading.Event()
            answers = answer_thread(self.tmp, allow=True, ready=gate,
                                    ready_timeout=SLOW_ANSWER)
            try:
                started = time.monotonic()
                click = asyncio.ensure_future(prox.call("click", {"uid": "1"}))
                await asyncio.sleep(0)
                observer = await prox.call("list_pages", {})
                waited = time.monotonic() - started
                gate.set()                       # only now may Josh answer
                return observer, await click, waited
            finally:
                answers.set()

        observer, click, waited = asyncio.run(
            asyncio.wait_for(both(), SLOW_ANSWER * 3))
        self.assertIn("Pages", self.text(observer))
        self.assertIn("(Josh approved this.)", self.text(click))
        self.assertLess(waited, SLOW_ANSWER / 2,
                        "the observer had to wait for the approval — the "
                        "event loop was blocked")

    def test_the_fence_is_proven_exactly_once_under_overlap(self):
        # Three calls arriving together must not each navigate the probe.
        vendor = FakeVendor()
        prox, _v = self.proxy(rung="read", vendor=vendor)

        async def three():
            return await asyncio.gather(*[prox.call("list_pages", {})
                                          for _ in range(3)])

        asyncio.run(three())
        probes = [c for c in vendor.calls
                  if c[0] == "navigate_page"
                  and c[1].get("url") == browser_mcp.FENCE_PROBE_URL]
        self.assertEqual(len(probes), 1)


SLOW_ANSWER = 4.0          # how long the scripted Josh takes to answer


def answer_thread(directory, allow, seen=None, ready=None, ready_timeout=15):
    """Answer every approval request that appears, like the relay's watcher.

    `ready`, when given, is an Event this thread waits on before answering —
    the stand-in for a Josh who takes a moment, which is what makes the
    non-blocking test above able to observe the wait.
    """
    stop = threading.Event()

    def loop():
        if ready is not None:
            ready.wait(ready_timeout)
        while not stop.is_set():
            try:
                names = [n for n in os.listdir(directory) if n.endswith(".req")]
            except OSError:
                names = []
            for name in names:
                path = os.path.join(directory, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        req = json.load(fh)
                    os.remove(path)
                except (OSError, ValueError):
                    continue
                if seen is not None:
                    seen.append(req)
                with open(os.path.join(directory, req["id"] + ".ans"), "w",
                          encoding="utf-8") as fh:
                    json.dump({"allow": allow,
                               "reason": ("Josh approved this." if allow
                                          else "Josh declined this.")}, fh)
            stop.wait(0.02)

    threading.Thread(target=loop, daemon=True).start()
    return stop


# ---------------------------------------------------------- the arg fence --

class ArgumentFenceTests(Base):
    def test_keys_outside_the_table_are_dropped_before_the_vendor_sees_them(self):
        prox, vendor = self.proxy(rung="full")
        run(prox.call("take_screenshot", {"fullPage": True,
                                          "filePath": r"C:\evil.png"}))
        _name, args = [c for c in vendor.calls if c[0] == "take_screenshot"][0]
        self.assertIn("fullPage", args)
        self.assertNotIn("filePath", args)

    def test_init_script_cannot_ride_in_on_a_navigation(self):
        prox, vendor = self.proxy(rung="full")
        run(prox.call("navigate_page", {"url": "https://example.com/",
                                        "initScript": "fetch('/x')"}))
        nav = [c for c in vendor.calls
               if c[0] == "navigate_page"
               and c[1].get("url") != browser_mcp.FENCE_PROBE_URL][0]
        self.assertNotIn("initScript", nav[1])

    def test_extra_http_headers_cannot_ride_in_on_emulate(self):
        prox, vendor = self.proxy(rung="full")
        run(prox.call("emulate", {"userAgent": "x",
                                  "extraHttpHeaders": {"Authorization": "y"}}))
        _name, args = [c for c in vendor.calls if c[0] == "emulate"][0]
        self.assertNotIn("extraHttpHeaders", args)

    def test_a_withheld_tool_is_refused_even_if_named_directly(self):
        prox, vendor = self.proxy(rung="full")
        for name in browser_mcp.WITHHELD:
            result = run(prox.call(name, {"filePath": r"C:\x"}))
            self.assertIn("Unknown tool", self.text(result))
        self.assertNotIn("lighthouse_audit", [n for n, _a in vendor.calls])


class UploadConfinementTests(Base):
    def test_a_path_outside_the_workspace_is_refused(self):
        prox, vendor = self.proxy(rung="full", workspace=self.tmp)
        result = run(prox.call("upload_file",
                               {"uid": "1", "filePath": r"C:\Windows\win.ini"}))
        self.assertIn("outside this conversation's working folder",
                      self.text(result))
        self.assertNotIn("upload_file", [n for n, _a in vendor.calls])

    def test_a_path_inside_the_workspace_goes_through_absolute(self):
        inside = os.path.join(self.tmp, "report.pdf")
        open(inside, "w").close()
        prox, vendor = self.proxy(rung="full", workspace=self.tmp)
        run(prox.call("upload_file", {"uid": "1", "filePath": "report.pdf"}))
        _name, args = [c for c in vendor.calls if c[0] == "upload_file"][0]
        self.assertEqual(os.path.realpath(args["filePath"]),
                         os.path.realpath(inside))

    def test_dot_dot_cannot_climb_out(self):
        prox, _v = self.proxy(rung="full", workspace=self.tmp)
        result = run(prox.call("upload_file",
                               {"uid": "1", "filePath": r"..\..\secrets.txt"}))
        self.assertIn("outside this conversation's working folder",
                      self.text(result))

    def test_no_workspace_means_no_upload(self):
        prox, _v = self.proxy(rung="full", workspace="")
        result = run(prox.call("upload_file",
                               {"uid": "1", "filePath": "anything"}))
        self.assertIn("outside this conversation's working folder",
                      self.text(result))


# ------------------------------------------------------------- forwarding --

class ForwardingTests(Base):
    def test_a_policy_refusal_is_forwarded_verbatim_and_carries_no_approval(self):
        # Rule 4: a URL refusal comes back isError FALSE, so nothing may
        # decide from isError alone — and Alloy must never stamp "Josh
        # approved this" onto a call the fence actually refused.
        os.environ["ALLOY_BROWSER_APPROVAL_DIR"] = self.tmp
        prox, _v = self.proxy(rung="read")
        result = run(prox.call("navigate_page",
                               {"url": "https://not-listed.test/"}))
        text = self.text(result)
        self.assertIn("blocked by blocklist/allowlist rules", text)
        self.assertIs(result.isError, False)
        self.assertNotIn("Josh approved", text)

    def test_a_vendor_error_is_forwarded_rather_than_restated(self):
        vendor = FakeVendor()
        prox, _v = self.proxy(rung="full", vendor=vendor)
        run(prox.call("list_pages", {}))          # get past the self-test
        vendor.raise_on.add("click")
        result = run(prox.call("click", {"uid": "1"}))
        self.assertIn("do not assume the last action happened",
                      self.text(result))

    def test_a_dead_vendor_latches_and_never_retries(self):
        vendor = FakeVendor()
        prox, _v = self.proxy(rung="full", vendor=vendor)
        run(prox.call("list_pages", {}))
        vendor.raise_on.add("click")
        run(prox.call("click", {"uid": "1"}))
        before = len(vendor.calls)
        run(prox.call("take_snapshot", {}))
        self.assertEqual(len(vendor.calls), before)

    def test_refusals_are_answers_not_errors(self):
        # isError would make a CLI treat a decision as a malfunction and
        # retry it. A refusal has to read as a decision.
        prox, _v = self.proxy(rung="read")
        result = run(prox.call("click", {"uid": "1"}))
        self.assertIs(result.isError, False)


# ---------------------------------------------------------- instructions ---

class InstructionsTests(Base):
    def test_the_sites_are_named(self):
        prox, _v = self.proxy(rung="ask", sites=["https://example.com/*"])
        text = prox.instructions()
        self.assertIn("https://example.com/*", text)

    def test_no_sites_says_so_plainly(self):
        prox, _v = self.proxy(rung="ask", sites=[])
        self.assertIn("reach NOTHING", prox.instructions())

    def test_a_refused_pattern_is_stated_not_silently_dropped(self):
        prox, _v = self.proxy(rung="ask",
                              sites=["file:///C:/*", "https://ok.test/*"])
        text = prox.instructions()
        self.assertIn("refused", text.lower())
        self.assertIn("file:///C:/*", text)

    def test_withheld_tools_are_stated(self):
        prox, _v = self.proxy(rung="ask")
        self.assertIn("lighthouse_audit is not available", prox.instructions())

    def test_the_rung_is_stated_in_words(self):
        self.assertIn("LOOK-ONLY", self.proxy(rung="read")[0].instructions())
        self.assertIn("waits for Josh", self.proxy(rung="ask")[0].instructions())
        self.assertIn("without being asked",
                      self.proxy(rung="full")[0].instructions())


# ------------------------------------------------------------- the relay ---

class RelayAxisTests(unittest.TestCase):
    """The engine side: normalization, the clamp, the note, the argv."""

    @classmethod
    def setUpClass(cls):
        import relay
        cls.relay = relay

    def agent(self, **kw):
        kw.setdefault("workspace", tempfile.gettempdir())
        return self.relay.ClaudeAgent(**kw)

    def test_anything_unrecognised_normalizes_to_off(self):
        for value in ("nonsense", "", "  ", "maybe", 3.5):
            self.assertEqual(self.relay.normalize_browser(value), "off")
        self.assertEqual(self.relay.normalize_browser(None), "off")
        self.assertEqual(self.relay.normalize_browser(True), "full")
        self.assertEqual(self.relay.normalize_browser("read-only"), "read")

    def test_a_refused_pattern_caps_an_unattended_run_at_ask(self):
        self.assertEqual(
            self.relay.clamp_browser_rung("full",
                                          ["file:///C:/*", "https://ok.test/*"]),
            "ask")
        self.assertEqual(
            self.relay.clamp_browser_rung("full", ["https://ok.test/*"]),
            "full")

    def test_no_usable_sites_caps_the_rung_at_look_only(self):
        for asked in ("read", "ask", "full"):
            self.assertEqual(self.relay.clamp_browser_rung(asked, []), "read")
        # off stays off — a clamp never turns a capability ON.
        self.assertEqual(self.relay.clamp_browser_rung("off", []), "off")

    def test_the_server_is_registered_under_its_own_name(self):
        agent = self.agent(browser="ask", browser_sites=["https://ok.test/*"],
                           desktop="ask")
        cmd = agent.build_cmd("hello")
        config = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]
        self.assertIn(self.relay.BROWSER_SERVER, config)
        self.assertIn(self.relay.DESKTOP_SERVER, config)
        self.assertNotEqual(self.relay.BROWSER_SERVER, self.relay.DESKTOP_SERVER)

    def test_both_servers_reach_the_allowed_tools_list(self):
        # A list naming one of two would leave the other prompting on every
        # single call at rung `auto`.
        agent = self.agent(browser="ask", browser_sites=["https://ok.test/*"],
                           desktop="ask", permission="auto")
        cmd = agent.build_cmd("hello")
        allowed = [c for c in cmd if str(c).startswith("--allowedTools=")][0]
        self.assertIn("mcp__" + self.relay.BROWSER_SERVER, allowed)
        self.assertIn("mcp__" + self.relay.DESKTOP_SERVER, allowed)

    def test_the_spec_carries_policy_in_the_environment_only(self):
        agent = self.agent(browser="ask", browser_sites=["https://ok.test/*"])
        spec = agent.browser_server_spec()
        if spec is None:
            self.skipTest("chrome-devtools-mcp is not installed here")
        self.assertEqual(spec["env"]["ALLOY_BROWSER_RUNG"], "ask")
        self.assertIn("ok.test", spec["env"]["ALLOY_BROWSER_SITES"])
        # Nothing policy-shaped may be an ARGUMENT: the model writes those.
        self.assertEqual(len(spec["args"]), 1)
        self.assertTrue(spec["args"][0].endswith("browser_mcp.py"))

    def test_off_registers_nothing_at_all(self):
        agent = self.agent(browser="off")
        self.assertIsNone(agent.browser_server_spec())
        cmd = agent.build_cmd("hello")
        config = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]
        self.assertNotIn(self.relay.BROWSER_SERVER, config)

    def test_the_live_webhook_port_reaches_the_classifier(self):
        """The port is discovered at bind time, not read from config: the
        webhook usually takes an ephemeral port, so the thing worth refusing
        is the socket that actually exists."""
        old = self.relay.WEBHOOK_PORT
        try:
            self.relay.WEBHOOK_PORT = 8765
            kept, rejected = self.relay.browser_site_report(
                ["http://127.0.0.1:8765/*", "http://127.0.0.1:3000/*"])
            self.assertEqual(kept, ["http://127.0.0.1:3000/*"])
            self.assertIn("webhook", rejected[0][1])
            # Nothing listening means nothing to aim at; the blanket loopback
            # rule and webhook.py's JSON-only check still stand.
            self.relay.WEBHOOK_PORT = None
            kept, rejected = self.relay.browser_site_report(
                ["http://127.0.0.1:8765/*"])
            self.assertEqual(rejected, [])
        finally:
            self.relay.WEBHOOK_PORT = old

    def test_the_approval_directory_is_a_third_one(self):
        # Separate directories mean separate watchers mean separate verdicts.
        agent = self.agent(browser="ask")
        self.assertNotEqual(agent.browser_dir(), agent.desktop_dir())
        self.assertNotEqual(agent.browser_dir(), agent.approval_dir())

    def test_the_capability_note_says_nothing_when_off(self):
        self.assertEqual(
            self.relay.browser_capability_clause(self.agent(browser="off")), [])

    def test_the_note_names_the_sites_and_the_ceiling(self):
        sites = ["https://ok.test/*"]
        read = self.relay.browser_capability_clause(
            self.agent(browser="read", browser_sites=sites))[0]
        self.assertIn("ok.test", read)
        self.assertIn("cannot click", read)
        ask = self.relay.browser_capability_clause(
            self.agent(browser="ask", browser_sites=sites))[0]
        self.assertIn("waits for Josh", ask)
        full = self.relay.browser_capability_clause(
            self.agent(browser="full", browser_sites=sites))[0]
        self.assertIn("no prompt", full)

    def test_the_note_admits_when_the_browser_reaches_nothing(self):
        note = self.relay.browser_capability_clause(
            self.agent(browser="ask", browser_sites=[]))[0]
        self.assertIn("NOTHING", note)

    def test_the_advisory_ceiling_appears_only_where_it_is_true(self):
        # The ladder ENFORCES at read_only and ask; at auto and full the seat
        # holds a shell and can go around it. Saying otherwise is the
        # over-claim capability_note exists to stop.
        for permission in ("read_only", "ask"):
            self.assertEqual(self.relay.advisory_rung_note(
                self.agent(browser="ask", permission=permission)), [])
        for permission in ("auto", "full"):
            note = self.relay.advisory_rung_note(
                self.agent(browser="ask", permission=permission))
            self.assertTrue(note, permission)
            self.assertIn("guardrail against accident", note[0])
            # It must NOT carve the fence out of its own admission: the site
            # list bounds the Chrome ALLOY spawned, and a shell reaches any
            # site with curl. Claiming otherwise hands back in the last
            # clause exactly the over-claim the first clause gave up.
            self.assertIn("does not bound a shell", note[0])
            self.assertNotIn("still holds", note[0])

    def test_no_advisory_note_when_neither_axis_is_on(self):
        self.assertEqual(self.relay.advisory_rung_note(
            self.agent(permission="full")), [])

    def test_the_rung_survives_a_save_and_reopen(self):
        meta = {"browser": "ask", "browser_sites": ["https://ok.test/*"]}
        summary = self.relay.session_summary.__wrapped__ if hasattr(
            self.relay.session_summary, "__wrapped__") else None
        # Round-trip through the normalizer the summary uses.
        self.assertEqual(self.relay.normalize_browser(meta["browser"]), "ask")
        # A meta saved before browser control existed reads as off.
        self.assertEqual(self.relay.normalize_browser({}.get("browser")), "off")


# ------------------------------------------------- adversarial regressions --

class ReviewRegressionTests(Base):
    """Every defect an adversarial review of this code actually found.

    Each of these was a live bug on 2026-08-26 and each is now a decision, so
    the test says WHICH decision rather than merely that a string is present.
    """

    def test_the_look_only_rung_does_not_publish_tools_it_always_refuses(self):
        """The published list is the strongest capability claim a model reads.
        Advertising click/fill/type_text at look-only and then refusing every
        call against them is the inverse of why WITHHELD is stated out loud —
        and it costs the seat a tool call per attempt."""
        prox, _v = self.proxy(rung="read")
        names = {spec["name"] for spec in prox.published}
        for tool in ("click", "fill", "type_text", "press_key",
                     "evaluate_script", "upload_file", "handle_dialog"):
            self.assertNotIn(tool, names, tool)
        # Reading is still fully available.
        for tool in ("take_snapshot", "navigate_page", "list_network_requests"):
            self.assertIn(tool, names, tool)

    def test_a_withheld_tool_is_refused_with_its_reason_not_as_unknown(self):
        prox, _v = self.proxy(rung="read")
        text = self.text(run(prox.call("click", {"uid": "1"})))
        self.assertIn("look-only", text)
        self.assertNotIn("Unknown tool", text)

    def test_dismissing_an_unsaved_changes_guard_is_not_a_navigation(self):
        """handleBeforeUnload discards a page's own data-loss guard. That is a
        CHANGE to the page, and it was reachable at the look-only rung."""
        published, _dropped = browser_mcp.curate(default_tools(), level="full")
        nav = [s for s in published if s["name"] == "navigate_page"][0]
        self.assertNotIn("handleBeforeUnload", nav["inputSchema"]["properties"])
        prox, vendor = self.proxy(rung="full")
        run(prox.call("navigate_page", {"url": "https://example.com/",
                                        "handleBeforeUnload": "accept"}))
        real = [c for c in vendor.calls
                if c[0] == "navigate_page"
                and c[1].get("url") != browser_mcp.FENCE_PROBE_URL][0]
        self.assertNotIn("handleBeforeUnload", real[1])

    def test_an_approval_note_never_rides_a_call_the_fence_refused(self):
        """Rule 4, applied to Alloy's own note. A click can approve fine and
        THEN be refused by the fence when the link leaves the allowlist — and
        a policy refusal arrives isError FALSE, so `reason` alone cannot tell
        the two apart. "Josh approved this" above "…is blocked" would read as
        an approval that carried the action through."""
        os.environ["ALLOY_BROWSER_APPROVAL_DIR"] = self.tmp

        class Refusing(FakeVendor):
            async def call_tool(self, name, args=None):
                result = await FakeVendor.call_tool(self, name, args)
                if name == "click":
                    return FakeResult(BLOCKED % "https://elsewhere.test/")
                return result

        prox, _v = self.proxy(rung="ask", vendor=Refusing())
        answers = answer_thread(self.tmp, allow=True)
        try:
            text = self.text(run(prox.call("click", {"uid": "1_3"})))
        finally:
            answers.set()
        self.assertIn("blocked by blocklist/allowlist rules", text)
        self.assertNotIn("Josh approved", text)

    def test_loopback_is_reported_as_could_not_rule_out_not_as_fact(self):
        """`https://*.github.com/*` cannot match localhost, and a DNS failure
        proves nothing either — but both fail closed and withhold scripting.
        Fail-closed is right; stating a false REASON is not."""
        prox, _v = self.proxy(rung="ask", sites=["https://*.github.com/*"])
        text = prox.instructions()
        self.assertIn("could not rule out", text)
        self.assertNotIn("can reach this machine's loopback", text)


class RoomReachTests(unittest.TestCase):
    """A room-level over-claim: the pickers accept settings nothing can use."""

    @classmethod
    def setUpClass(cls):
        import relay
        cls.relay = relay

    def test_a_room_with_no_claude_seat_is_told_so(self):
        note = self.relay.axis_unreachable_note(["gpt", "gemini"],
                                                browser="full")
        self.assertIn("no seat in this room can use it", note)
        self.assertIn("Browser control", note)

    def test_both_axes_read_as_plural(self):
        note = self.relay.axis_unreachable_note(["ox"], desktop="ask",
                                                browser="read")
        self.assertIn("are set", note)
        self.assertIn("them", note)

    def test_a_claude_seat_anywhere_in_the_room_silences_it(self):
        self.assertEqual(
            self.relay.axis_unreachable_note(["gpt", "claude"], browser="full"),
            "")

    def test_nothing_is_said_when_no_axis_is_on(self):
        self.assertEqual(self.relay.axis_unreachable_note(["gpt"]), "")
        self.assertEqual(
            self.relay.axis_unreachable_note(["gpt"], desktop="off",
                                             browser="off"), "")

    def test_the_delivering_set_matches_which_adapter_actually_registers(self):
        """The sentence has to come from the same fact build_cmd uses. A
        hand-kept list would drift the moment another adapter gains a route."""
        import inspect
        for provider in self.relay.MCP_DELIVERING_PROVIDERS:
            cls = self.relay.AGENT_TYPES[provider]
            self.assertIn("browser_server_spec", inspect.getsource(cls.build_cmd),
                          "%s is listed as delivering but its build_cmd never "
                          "registers the server" % provider)
        for provider, spec in self.relay.PROVIDERS.items():
            cls = spec.get("agent")
            if cls is None or provider in self.relay.MCP_DELIVERING_PROVIDERS:
                continue
            src = inspect.getsource(cls.build_cmd)
            self.assertNotIn("browser_server_spec", src,
                             "%s registers the server but is not listed as "
                             "delivering, so rooms are told a lie" % provider)


class SecondReviewRegressionTests(Base):
    """The second adversarial pass. Same rule: each test says which decision."""

    def test_a_wildcard_loopback_port_cannot_cover_the_webhook(self):
        """`http://localhost:*/*` is the natural way to write "my dev server,
        whatever port it took" — and it INCLUDES Alloy's own front door. It
        also used to sail past the refusal entirely, which meant the rung
        clamp never fired either, so the run stayed at Unattended."""
        for pattern in ("http://localhost:*/*",
                        "http://localhost:{8765,3000}/*"):
            kept, rejected, _l = browser_mcp.classify_sites(
                [pattern], webhook_port=8765)
            self.assertEqual(kept, [], pattern)
            self.assertIn("literal port", rejected[0][1], pattern)
        # A named port that is NOT the webhook's is ordinary local dev.
        kept, rejected, _l = browser_mcp.classify_sites(
            ["http://localhost:3000/*"], webhook_port=8765)
        self.assertEqual(kept, ["http://localhost:3000/*"])

    def test_an_absent_port_is_the_default_port_not_a_wildcard(self):
        """MEASURED against real Chrome: allowlisting `http://127.0.0.1/*`
        BLOCKS `http://127.0.0.1:8765/start` and permits `:80`. So an omitted
        port matches exactly one port, and refusing such a pattern because the
        webhook is on 8765 would be a false positive that costs a valid
        config."""
        self.assertEqual(browser_mcp._port_of("http://127.0.0.1/*"), 80)
        self.assertEqual(browser_mcp._port_of("https://localhost/*"), 443)
        self.assertIsNone(browser_mcp._port_of("http://localhost:*/*"))
        kept, rejected, _l = browser_mcp.classify_sites(
            ["http://127.0.0.1/*"], webhook_port=8765)
        self.assertEqual(kept, ["http://127.0.0.1/*"])
        self.assertEqual(rejected, [])
        # ...and it IS refused when the webhook really is on the default port.
        kept, rejected, _l = browser_mcp.classify_sites(
            ["http://127.0.0.1/*"], webhook_port=80)
        self.assertEqual(kept, [])
        # And with nothing listening there is no front door to cover.
        kept, _r, _l = browser_mcp.classify_sites(["http://localhost:*/*"],
                                                  webhook_port=None)
        self.assertEqual(kept, ["http://localhost:*/*"])

    def test_a_wildcard_port_also_clamps_the_rung(self):
        import relay
        self.assertEqual(
            relay.clamp_browser_rung("full", ["http://localhost:*/*"], 8765),
            "read")            # nothing kept at all -> look only

    def test_there_is_exactly_one_gate_and_it_is_the_one_that_runs(self):
        """A documented `gate()` that nothing calls is a trap: a maintainer
        hardening "the gate" edits a function that never executes. `decide` is
        the single interpreter and `Proxy.call` uses it directly."""
        self.assertFalse(hasattr(browser_mcp, "gate"))
        with open(browser_mcp.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertEqual(source.count("def decide("), 1)

    def test_the_look_only_wording_does_not_promise_an_outcome(self):
        """The rung is enforced as an enumeration (no click/type/script). It
        was WORDED as an outcome ("nothing can be changed"), and a GET is a
        real request — a logout or unsubscribe link acts by itself."""
        import relay
        blurb = relay.BROWSER_RUNGS["read"]["blurb"]
        self.assertNotIn("nothing on those sites can be changed", blurb)
        self.assertIn("still a real request", blurb)
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "ui", "index.html"),
                encoding="utf-8") as fh:
            ui = fh.read()
        self.assertIn("still a real request", ui)
        self.assertNotIn("so nothing on those sites can be changed", ui)

    def test_the_workspace_is_negotiated_as_the_vendors_only_root(self):
        """Without this `upload_file` passed Alloy's check, asked Josh, and
        then ALWAYS failed at the vendor: its `verifyFilesSchema` is
        ['filePath'] and with no roots negotiated the only root is the OS temp
        dir. Measured live against 1.7.0 — "Access denied: ... not within any
        of the configured workspace roots"."""
        with open(browser_mcp.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("list_roots_callback=list_roots", source)
        self.assertIn("ALLOY_BROWSER_WORKSPACE", source)
        # No workspace must mean NO roots, so the vendor denies every path.
        self.assertIn("if workspace and os.path.isdir(workspace)", source)


class ApprovalCardTests(Base):
    """Josh's decision is only ever as good as the sentence on the card."""

    def test_a_page_cannot_choose_the_url_on_the_card(self):
        """The vendor renders `<id>: <title> (<url>) [selected]` and the TITLE
        IS WRITTEN BY THE PAGE. A leftmost match reads a URL the page chose —
        so a title of `(https://safe.test) [selected]` makes Josh approve a
        click on a site the action never touches. Same family as the wrap
        token: a substring match on text somebody else controls."""

        class Spoofing(FakeVendor):
            async def call_tool(self, name, args=None):
                if name == "list_pages":
                    await asyncio.sleep(0)
                    self.calls.append((name, dict(args or {})))
                    return FakeResult(
                        "## Pages\n"
                        "0: (https://safe.test) [selected] "
                        "(https://evil.test/pay) [selected]")
                return await FakeVendor.call_tool(self, name, args)

        prox, _v = self.proxy(rung="ask", vendor=Spoofing())
        self.assertEqual(run(prox._current_url()), "https://evil.test/pay")

    def test_a_title_that_mimics_the_marker_on_another_page_is_ignored(self):
        class Spoofing(FakeVendor):
            async def call_tool(self, name, args=None):
                if name == "list_pages":
                    await asyncio.sleep(0)
                    self.calls.append((name, dict(args or {})))
                    return FakeResult(
                        "## Pages\n"
                        "0: X (https://decoy.test) [selected] "
                        "(https://not-selected.test)\n"
                        "1: Bank (https://bank.test/transfer) [selected]")
                return await FakeVendor.call_tool(self, name, args)

        prox, _v = self.proxy(rung="ask", vendor=Spoofing())
        self.assertEqual(run(prox._current_url()),
                         "https://bank.test/transfer")

    def test_a_non_web_or_missing_url_names_nothing_rather_than_guessing(self):
        for text in ("## Pages\n0: blank (about:blank) [selected]",
                     "## Pages\n0: untitled [selected]",
                     "nothing useful at all"):

            class Odd(FakeVendor):
                async def call_tool(self, name, args=None, _t=text):
                    if name == "list_pages":
                        await asyncio.sleep(0)
                        return FakeResult(_t)
                    return await FakeVendor.call_tool(self, name, args)

            prox, _v = self.proxy(rung="ask", vendor=Odd())
            self.assertEqual(run(prox._current_url()), "", text)

    def test_fill_form_names_the_values_not_just_a_count(self):
        """`fill` showed its text and `fill_form` showed only "2 fields", so
        the way to keep a secret off the card was to send it through the
        plural tool."""
        card = browser_mcp._detail("fill_form", {"elements": [
            {"uid": "1_2", "value": "4111111111111111"},
            {"uid": "1_3", "value": "123"}]}, "https://bank.test/")
        self.assertIn("4111111111111111", card)
        self.assertIn("123", card)

    def test_a_long_fill_form_is_truncated_but_still_says_how_many(self):
        card = browser_mcp._detail("fill_form", {"elements": [
            {"uid": "u%d" % i, "value": "v" * 80} for i in range(9)]},
            "https://x.test/")
        self.assertIn("and 3 more", card)
        self.assertIn("...", card)

    def test_a_dialog_card_names_what_gets_typed_into_it(self):
        card = browser_mcp._detail("handle_dialog",
                                   {"action": "accept",
                                    "promptText": "transfer everything"},
                                   "https://bank.test/")
        self.assertIn("transfer everything", card)
        # ...and stays clean when there is nothing to type.
        plain = browser_mcp._detail("handle_dialog", {"action": "dismiss"},
                                    "https://bank.test/")
        self.assertNotIn("typing", plain)


class DeliveryReachTests(unittest.TestCase):
    """Registered is not the same as callable. Both were measured live."""

    @classmethod
    def setUpClass(cls):
        import relay
        cls.relay = relay

    def agent(self, permission):
        agent = self.relay.ClaudeAgent(
            tempfile.gettempdir(), permission=permission, browser="read",
            browser_sites=["https://ok.test/*"], desktop="ask")
        if permission == "ask":
            agent.on_approval = lambda *args, **kwargs: (True, "")
        return agent

    def servers(self, cmd):
        return sorted(json.loads(
            cmd[cmd.index("--mcp-config") + 1])["mcpServers"])

    def test_read_only_registers_nothing_and_claims_nothing(self):
        """MEASURED with a real seat: read_only emits `--permission-mode plan`
        and claude answers every MCP call with "Cannot call
        mcp__alloy_browser__new_page while in plan mode." No allowlist lifts
        that, so registering would advertise tools that cannot be called once
        — and capability_note, which gates on a spec existing, would promise
        them."""
        agent = self.agent("read_only")
        self.assertIsNone(agent.browser_server_spec())
        self.assertIsNone(agent.desktop_server_spec())
        self.assertEqual(self.servers(agent.build_cmd("hi")), [])
        self.assertEqual(self.relay.browser_capability_clause(agent), [])
        self.assertEqual(self.relay.desktop_capability_clause(agent), [])

    def test_read_only_says_out_loud_that_nothing_was_handed_over(self):
        note = self.relay.axis_blocked_by_permission_note(
            "read_only", desktop="ask", browser="read")
        self.assertIn("Read only", note)
        self.assertIn("Nothing was handed to the seats", note)
        self.assertIn("are set", note)          # two axes -> plural
        # Nothing to say at a rung that works, or with no axis on.
        self.assertEqual(self.relay.axis_blocked_by_permission_note(
            "auto", browser="read"), "")
        self.assertEqual(self.relay.axis_blocked_by_permission_note(
            "read_only"), "")

    def test_ask_grants_the_two_servers_and_nothing_else(self):
        """MEASURED with a real seat: without this every call came back
        "Claude requested permissions to use mcp__alloy_browser__…, but you
        haven't granted it yet". `--allowedTools` is the ONE thing that gates
        MCP, and only the `auto` branch used to emit it."""
        agent = self.agent("ask")
        cmd = agent.build_cmd("hi")
        allowed = [c for c in cmd if str(c).startswith("--allowedTools=")]
        self.assertEqual(len(allowed), 1)
        self.assertIn("mcp__" + self.relay.BROWSER_SERVER, allowed[0])
        self.assertIn("mcp__" + self.relay.DESKTOP_SERVER, allowed[0])
        # The ask rung itself is untouched: writes and shell still route
        # through the approval hook rather than being auto-approved here.
        for tool in ("Write", "Edit", "Bash"):
            self.assertNotIn(tool, allowed[0])

    def test_no_pointless_allowlist_when_nothing_was_registered(self):
        agent = self.relay.ClaudeAgent(tempfile.gettempdir(), permission="ask")
        agent.on_approval = lambda *args, **kwargs: (True, "")
        cmd = agent.build_cmd("hi")
        self.assertFalse([c for c in cmd
                          if str(c).startswith("--allowedTools=")])

    def test_full_needs_no_allowlist_because_it_skips_permissions(self):
        agent = self.agent("full")
        cmd = agent.build_cmd("hi")
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(len(self.servers(cmd)), 2)


class CapabilityHonestyTests(unittest.TestCase):
    """What the capability note may and may not promise."""

    @classmethod
    def setUpClass(cls):
        import relay
        cls.relay = relay

    def agent(self, **kw):
        kw.setdefault("workspace", tempfile.gettempdir())
        return self.relay.ClaudeAgent(**kw)

    def test_no_vendor_on_disk_means_no_browser_claim(self):
        """browser_server_spec has a SECOND way to return None. A note gated
        only on the rung tells every peer this seat is driving Chrome while it
        holds zero browser tools."""
        original = browser_mcp.find_vendor
        browser_mcp.find_vendor = lambda: ""
        try:
            agent = self.agent(browser="full",
                               browser_sites=["https://ok.test/*"])
            self.assertIsNone(agent.browser_server_spec())
            self.assertEqual(self.relay.browser_capability_clause(agent), [])
            cmd = agent.build_cmd("hello")
            config = json.loads(
                cmd[cmd.index("--mcp-config") + 1])["mcpServers"]
            self.assertNotIn(self.relay.BROWSER_SERVER, config)
        finally:
            browser_mcp.find_vendor = original

    def test_a_loopback_site_is_admitted_at_every_rung_that_acts(self):
        """Scripting disappears exactly in the dev-server case, which is the
        rungs where it matters most. A peer routing 'run this in the page'
        work has to know."""
        for rung in ("ask", "full"):
            agent = self.agent(browser=rung,
                               browser_sites=["http://localhost:5173/*"])
            note = self.relay.browser_capability_clause(agent)
            if not note:
                self.skipTest("chrome-devtools-mcp is not installed here")
            self.assertIn("cannot run scripts", note[0], rung)
        # The control case has to be a host that DEMONSTRABLY does not reach
        # loopback. It cannot be a real domain: this suite must not depend on
        # DNS, and `.test`/`.invalid` fail to resolve — which the fail-closed
        # rule correctly reads as "could not rule it out".
        original = browser_mcp._resolves_to_loopback
        browser_mcp._resolves_to_loopback = lambda host: False
        try:
            agent = self.agent(browser="ask",
                               browser_sites=["https://ok.test/*"])
            note = self.relay.browser_capability_clause(agent)
            self.assertNotIn("cannot run scripts", note[0])
        finally:
            browser_mcp._resolves_to_loopback = original

    def test_a_host_that_cannot_be_resolved_fails_closed(self):
        """Offline, or a typo'd domain: scripting goes away. That is the right
        direction to fail, and the reason given says so honestly rather than
        asserting the site reaches loopback."""
        self.assertTrue(browser_mcp._reaches_loopback(
            "https://nothing-here.invalid/*"))

    def test_look_only_admits_that_opening_a_page_is_still_a_request(self):
        """The rung is enforced as an enumeration (no click/type/script) but
        was WORDED as an outcome ("nothing can be changed"). A GET can act."""
        agent = self.agent(browser="read", browser_sites=["https://ok.test/*"])
        note = self.relay.browser_capability_clause(agent)
        if not note:
            self.skipTest("chrome-devtools-mcp is not installed here")
        self.assertIn("still a real request", note[0])

    def test_the_access_axes_survive_a_resume_intact(self):
        """rehydrate fed the saved values into the AGENTS but not back into
        the state dict SessionStore.save reads, so a resumed chat wrote
        connectors=false / desktop=off / browser=off over the real values on
        its very next save. Silent, one-way, and it looked like the setting had
        simply been forgotten."""
        import inspect
        src = inspect.getsource(self.relay.rehydrate)
        for key in ('"connectors"', '"desktop"', '"desktop_allowlist"',
                    '"browser"', '"browser_sites"'):
            self.assertIn(key + ":", src,
                          "%s never returns to state, so the next save wipes "
                          "it" % key)


# ------------------------------------------------------------- the bridge --

class FakeWindow:
    def evaluate_js(self, script):
        pass


def scripted_from(cls, reply):
    """A REAL adapter subclass whose only fake is `turn` — the same pattern
    test_permissions uses, and for the same reason: the question here is what
    the SHIPPING code does with a cfg key."""

    class Scripted(cls):
        def turn(self, message, on_activity=None):
            self.session_id = "fake-session-%s" % self.uid
            return reply

    return Scripted


class AppBridgeBrowserTests(unittest.TestCase):
    """The composer's browser picker, end to end through the real app.Api.

    This suite exists because of W0.1: the permission pill had 18 passing
    relay tests and every one of them was right, while `Api._conversation`
    read a key nobody wrote and two of four rungs did nothing at all. It
    looked healthy from every angle a relay-only suite can see. A safety
    control needs a test at the BRIDGE.
    """

    def setUp(self):
        import app
        import relay
        self.app, self.relay = app, relay
        self.tmp = tempfile.mkdtemp(prefix="alloy-app-browser-")
        self._old_app_dir = app.SESSIONS_DIR
        self._old_relay_dir, self._old_tabs = relay.SESSIONS_DIR, relay.TABS_FILE
        # relay's OWN globals too: session_path() and write_tabs() read those,
        # so redirecting only the app's would write the REAL sessions/tabs.json.
        app.SESSIONS_DIR = relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        self._old_types = dict(relay.AGENT_TYPES)
        relay.AGENT_TYPES["claude"] = scripted_from(relay.ClaudeAgent, "c1")
        relay.AGENT_TYPES["gpt"] = scripted_from(relay.CodexAgent, "g1")

    def tearDown(self):
        self.app.SESSIONS_DIR = self._old_app_dir
        self.relay.SESSIONS_DIR = self._old_relay_dir
        self.relay.TABS_FILE = self._old_tabs
        self.relay.AGENT_TYPES.clear()
        self.relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, **cfg):
        api = self.app.Api()
        api._window = FakeWindow()
        api._conversation(dict(
            {"opener": "hi", "turns": 1,
             "seats": [{"id": 0, "provider": "claude", "enabled": True},
                       {"id": 1, "provider": "gpt", "enabled": True}]},
            **cfg))
        api._emit_q.join()
        self.assertIsNotNone(api._conv, "the conversation never started")
        return api

    def meta(self, api):
        with open(os.path.join(api._conv["store"].dir, "meta.json"),
                  encoding="utf-8") as fh:
            return json.load(fh)

    def test_the_picker_reaches_the_seats_and_the_command_line(self):
        api = self.start(browser="ask", browser_sites=["https://ok.test/*"])
        agents = api._conv["agents"]
        self.assertEqual([a.browser for a in agents], ["ask", "ask"])
        cmd = agents[0].build_cmd("hello")
        config = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]
        spec = config.get(self.relay.BROWSER_SERVER)
        if spec is None:
            self.skipTest("chrome-devtools-mcp is not installed here")
        self.assertEqual(spec["env"]["ALLOY_BROWSER_RUNG"], "ask")
        self.assertIn("ok.test", spec["env"]["ALLOY_BROWSER_SITES"])

    def test_off_is_the_default_and_registers_nothing(self):
        api = self.start()
        self.assertEqual([a.browser for a in api._conv["agents"]],
                         ["off", "off"])
        cmd = api._conv["agents"][0].build_cmd("hello")
        config = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]
        self.assertNotIn(self.relay.BROWSER_SERVER, config)

    def test_a_refused_site_clamps_the_rung_and_is_said_out_loud(self):
        api = self.start(browser="full",
                         browser_sites=["file:///C:/*", "https://ok.test/*"])
        # Clamped, everywhere the truth is kept: on the live agents AND in the
        # meta a reopened chat reads back.
        self.assertEqual([a.browser for a in api._conv["agents"]],
                         ["ask", "ask"])
        self.assertEqual(self.meta(api)["browser"], "ask")
        with open(os.path.join(api._conv["store"].dir, "transcript.md"),
                  encoding="utf-8") as fh:
            rows = fh.read()
        self.assertIn("file:///C:/*", rows)
        self.assertIn("lowered", rows)

    def test_an_empty_site_list_clamps_to_look_only(self):
        api = self.start(browser="full", browser_sites=[])
        self.assertEqual([a.browser for a in api._conv["agents"]],
                         ["read", "read"])

    def test_the_rung_survives_a_reopen_truthfully(self):
        api = self.start(browser="read", browser_sites=["https://ok.test/*"])
        session_id = os.path.basename(api._conv["store"].dir)
        reopened = self.app.Api()
        reopened._window = FakeWindow()
        opened = reopened.open_session(session_id)
        reopened._emit_q.join()
        # The UI reads `restoreBrowser(s.browser, s.browser_sites)` off this
        # object, so a missing key here is a reopened chat silently showing
        # browser control OFF when it ran at `read` — the W0.1 shape exactly.
        summary = opened["session"]
        self.assertEqual(summary["browser"], "read")
        self.assertEqual(summary["browser_sites"], ["https://ok.test/*"])
        # And the seats really were rebuilt with it, not just the label.
        self.assertEqual([a.browser for a in reopened._conv["agents"]],
                         ["read", "read"])

    def test_a_chat_saved_before_browser_control_reads_as_off(self):
        summary_off = self.relay.normalize_browser({}.get("browser"))
        self.assertEqual(summary_off, "off")


def main():
    unittest.main(verbosity=1, exit=False)


if __name__ == "__main__":
    main()
