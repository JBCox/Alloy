"""Plan-quota readout, warning and unattended brake (2026-08-28).

Token-free. The fixture is REAL: PAYLOAD is the exact `rate_limit_info` a live
`claude -p --output-format stream-json --verbose` turn emitted on 2026-08-28,
captured twice with identical contents. Nothing here invents a shape.

Run: python tests/test_plan_limits.py
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

import plan_limits
import relay

# Measured, not invented. `resetsAt` 1788044400 -> Sat 29 Aug 2026 6:00 PM,
# which is the reset the account's own usage panel showed at the same moment.
PAYLOAD = {"status": "allowed_warning", "resetsAt": 1788044400,
           "rateLimitType": "seven_day", "utilization": 0.79,
           "isUsingOverage": False, "surpassedThreshold": 0.75}
EVENT = {"type": "rate_limit_event", "session_id": "s", "uuid": "u",
         "rate_limit_info": dict(PAYLOAD)}
# Comfortably inside the measured window, so nothing here expires by accident.
NOW = 1787894378.0


def reading(**over):
    info = dict(PAYLOAD)
    info.update(over)
    return plan_limits.parse_event(
        {"type": "rate_limit_event", "rate_limit_info": info}, now=NOW)


class ParsingTests(unittest.TestCase):
    def test_the_real_payload_parses_to_every_field_we_rely_on(self):
        got = plan_limits.parse_event(EVENT, now=NOW)
        self.assertEqual(got["kind"], "seven_day")
        self.assertEqual(got["title"], "Weekly limit")
        self.assertAlmostEqual(got["utilization"], 0.79)
        self.assertEqual(got["resets_at"], 1788044400.0)
        self.assertEqual(got["reported_above"], 0.75)
        self.assertNotIn("overage", got)

    def test_a_line_that_is_not_a_rate_limit_event_is_ignored(self):
        for evt in ({"type": "result", "rate_limit_info": dict(PAYLOAD)},
                    {"rate_limit_info": dict(PAYLOAD)},
                    {"type": "rate_limit_event"},
                    {"type": "rate_limit_event", "rate_limit_info": []},
                    "not a dict", None, 42):
            self.assertIsNone(plan_limits.parse_event(evt, now=NOW))

    def test_a_bool_utilization_is_refused_not_read_as_one(self):
        """bool is an int subclass, so a naive number check turns True into
        100% quota used and refuses every unattended run forever."""
        self.assertIsNone(reading(utilization=True))
        self.assertIsNone(reading(utilization=False))
        self.assertIsNone(reading(utilization="0.79"))
        self.assertIsNone(reading(utilization=float("nan")))
        self.assertIsNone(reading(utilization=-0.1))

    def test_a_missing_rate_limit_type_is_refused(self):
        for bad in (None, "", "   ", 7):
            self.assertIsNone(reading(rateLimitType=bad))

    def test_an_unknown_limit_type_is_kept_and_titled_from_its_own_key(self):
        """This vocabulary came from one account on one day. A limit Alloy
        cannot name is still a limit it must be able to refuse on."""
        got = reading(rateLimitType="five_minute_experiment")
        self.assertIsNotNone(got)
        self.assertEqual(got["kind"], "five_minute_experiment")
        self.assertEqual(got["title"], "Five minute experiment")

    def test_overage_is_only_set_when_it_is_literally_true(self):
        self.assertNotIn("overage", reading(isUsingOverage=False))
        self.assertNotIn("overage", reading(isUsingOverage="yes"))
        self.assertTrue(reading(isUsingOverage=True)["overage"])

    def test_a_broken_resets_at_loses_the_expiry_never_the_reading(self):
        for bad in (None, 0, -5, "soon", True):
            got = reading(resetsAt=bad)
            self.assertIsNotNone(got)
            self.assertNotIn("resets_at", got)


class SnapshotTests(unittest.TestCase):
    def test_merge_keeps_a_limit_the_new_reading_did_not_mention(self):
        """A turn reports only the windows that crossed a threshold, so a lone
        seven_day reading must not erase an earlier five_hour one."""
        snap = plan_limits.merge({}, reading(rateLimitType="five_hour",
                                             utilization=0.4))
        snap = plan_limits.merge(snap, reading())
        self.assertEqual(sorted(snap), ["five_hour", "seven_day"])

    def test_merge_replaces_the_same_limit_and_returns_a_new_dict(self):
        first = plan_limits.merge({}, reading(utilization=0.4))
        second = plan_limits.merge(first, reading(utilization=0.9))
        self.assertAlmostEqual(second["seven_day"]["utilization"], 0.9)
        self.assertAlmostEqual(first["seven_day"]["utilization"], 0.4)

    def test_a_reading_past_its_reset_is_dropped_from_live(self):
        """Past resetsAt the window is a NEW one and the old percentage
        describes a period that is over. Without this a brake refuses a nightly
        job all week over a quota that reset on Saturday."""
        snap = plan_limits.merge({}, reading())
        self.assertTrue(plan_limits.live_readings(snap, now=NOW))
        self.assertEqual(plan_limits.live_readings(snap, now=1788044400.0), [])
        self.assertEqual(plan_limits.live_readings(snap, now=1788044401.0), [])

    def test_a_reading_with_no_reset_cannot_expire(self):
        snap = plan_limits.merge({}, reading(resetsAt=None))
        self.assertTrue(plan_limits.live_readings(snap, now=NOW + 10 ** 7))

    def test_worst_is_none_when_nothing_was_reported_never_zero(self):
        """stats.py's rule, and here it decides whether a brake can fire."""
        self.assertIsNone(plan_limits.worst({}))
        self.assertIsNone(plan_limits.worst(None))
        self.assertIsNone(plan_limits.worst({"x": "junk"}))

    def test_worst_picks_the_most_consumed_live_limit(self):
        snap = plan_limits.merge({}, reading(rateLimitType="five_hour",
                                             utilization=0.9))
        snap = plan_limits.merge(snap, reading(utilization=0.2))
        self.assertEqual(plan_limits.worst(snap, now=NOW)["kind"], "five_hour")

    def test_a_measured_zero_still_reads_as_zero_percent(self):
        snap = plan_limits.merge({}, reading(utilization=0))
        self.assertEqual(plan_limits.pct(
            plan_limits.worst(snap, now=NOW)["utilization"]), "0%")

    def test_the_displayed_percent_never_understates_the_decision(self):
        """0.796 shown as 79% and then refused by an 80% brake reads as a
        bug, so the number rounds toward the scary side."""
        self.assertEqual(plan_limits.pct(0.796), "80%")
        self.assertEqual(plan_limits.pct(0.79), "79%")
        self.assertEqual(plan_limits.pct(0.0), "0%")
        self.assertIsNone(plan_limits.pct(None))
        self.assertIsNone(plan_limits.pct(True))

    def test_enforceable_floor_is_none_until_a_payload_reports_one(self):
        """Never a hardcoded 0.75 — that would state one account's observed
        behaviour as a property of the product."""
        self.assertIsNone(plan_limits.enforceable_floor({}))
        snap = plan_limits.merge({}, reading(surpassedThreshold=None))
        self.assertIsNone(plan_limits.enforceable_floor(snap))
        snap = plan_limits.merge({}, reading())
        self.assertAlmostEqual(plan_limits.enforceable_floor(snap), 0.75)


class ThresholdTests(unittest.TestCase):
    def test_percent_and_fraction_both_mean_the_same_brake(self):
        self.assertAlmostEqual(plan_limits.normalize_threshold(80), 0.8)
        self.assertAlmostEqual(plan_limits.normalize_threshold(0.8), 0.8)
        self.assertAlmostEqual(plan_limits.normalize_threshold("80"), 0.8)
        self.assertAlmostEqual(plan_limits.normalize_threshold(100), 1.0)

    def test_junk_and_zero_mean_no_brake_and_never_a_zero_brake(self):
        """The `x or DEFAULT` lesson one field over: a 0.0 threshold refuses
        every unattended run forever, so it must be None, and callers must
        test `is None` rather than truthiness."""
        for bad in (None, 0, 0.0, -1, "", "abc", True, False, [], 101, 1e9):
            self.assertIsNone(plan_limits.normalize_threshold(bad),
                              "%r became a brake" % (bad,))


class BrakeTests(unittest.TestCase):
    def setUp(self):
        self.snap = plan_limits.merge({}, reading())

    def test_no_brake_configured_allows_and_says_so(self):
        allow, why = plan_limits.brake_verdict(self.snap, None, now=NOW)
        self.assertTrue(allow)
        self.assertIn("No plan-limit brake", why)

    def test_a_reading_at_or_past_the_brake_refuses_and_names_it(self):
        allow, why = plan_limits.brake_verdict(self.snap, 75, now=NOW)
        self.assertFalse(allow)
        self.assertIn("Weekly limit", why)
        self.assertIn("79%", why)
        self.assertIn("75%", why)

    def test_at_is_included_not_just_past(self):
        snap = plan_limits.merge({}, reading(utilization=0.8))
        self.assertFalse(plan_limits.brake_verdict(snap, 80, now=NOW)[0])

    def test_a_reading_under_the_brake_allows_and_reports_the_number(self):
        allow, why = plan_limits.brake_verdict(self.snap, 80, now=NOW)
        self.assertTrue(allow)
        self.assertIn("79%", why)

    def test_overage_refuses_whatever_the_percentage_says(self):
        snap = plan_limits.merge({}, reading(utilization=0.1,
                                             isUsingOverage=True))
        self.assertFalse(plan_limits.brake_verdict(snap, 99, now=NOW)[0])

    def test_no_measurement_fails_OPEN_and_says_the_check_did_not_happen(self):
        """A nightly job silently cancelled because a probe broke is worse
        than one that ran. The trade is stated rather than hidden — so the
        sentence must admit it, and a silent allow would fail this."""
        allow, why = plan_limits.brake_verdict({}, 80, now=NOW)
        self.assertTrue(allow)
        self.assertIn("could not be checked", why)
        self.assertIn("starting anyway", why)

    def test_an_expired_reading_does_not_refuse(self):
        allow, _ = plan_limits.brake_verdict(self.snap, 75, now=1788044401.0)
        self.assertTrue(allow)

    def test_every_verdict_carries_a_sentence(self):
        """Nobody is watching at 01:00; a refusal with no words on the record
        is indistinguishable from a schedule that never fired."""
        for snap in ({}, self.snap):
            for thr in (None, 50, 75, 80, "junk"):
                _, why = plan_limits.brake_verdict(snap, thr, now=NOW)
                self.assertTrue(why.strip())


class NoteTests(unittest.TestCase):
    def test_a_brake_below_the_reporting_floor_is_declared_unenforceable(self):
        """The repo's most-repeated defect is a control that looks configured
        and does nothing. Below its own warning threshold the CLI is silent,
        so a 50% brake can never fire and the modal has to say so."""
        snap = plan_limits.merge({}, reading())
        lines = " ".join(plan_limits.brake_note(snap, 50))
        self.assertIn("cannot fire", lines)
        self.assertIn("75%", lines)

    def test_an_enforceable_brake_gets_no_such_warning(self):
        snap = plan_limits.merge({}, reading())
        self.assertNotIn("cannot fire",
                         " ".join(plan_limits.brake_note(snap, 90)))

    def test_with_no_readings_the_note_says_why_it_is_blank(self):
        lines = " ".join(plan_limits.brake_note({}, 80))
        self.assertIn("No plan usage reported yet", lines)

    def test_no_brake_set_is_stated_rather_than_left_as_an_absence(self):
        lines = " ".join(plan_limits.brake_note({}, None))
        self.assertIn("No brake set", lines)

    def test_no_public_function_raises_on_junk(self):
        junk = (None, 0, "", [], {"a": 1}, object())
        for value in junk:
            plan_limits.worst(value)
            plan_limits.summary(value)
            plan_limits.live_readings(value)
            plan_limits.enforceable_floor(value)
            plan_limits.describe(value)
            plan_limits.limit_title(value)
            plan_limits.merge(value, value)
            plan_limits.brake_verdict(value, value)
            plan_limits.brake_note(value, value)


class ModuleShapeTests(unittest.TestCase):
    def test_it_imports_nothing_from_relay_or_app(self):
        """Standalone like export/fork/stats/memory/schedule.

        An AST walker, not a text match, and the first two attempts show why.
        `"import relay" in src` matches the module docstring's own promise that
        it imports nothing from relay (the confinement-parity lesson). Anchoring
        to the start of a stripped LINE fixed that and then failed too, on the
        docstring line that happens to begin "from relay or app, ..." — the
        wrap-token family again, one formalism up. Only a parse can tell a
        statement from a mention.
        """
        import ast
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "plan_limits.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        banned = {"relay", "app"}
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found += [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.append(node.module.split(".")[0])
        self.assertFalse(banned & set(found),
                         "plan_limits.py must stay standalone; imports: %s"
                         % sorted(set(found)))

    def test_it_owns_no_root_directory(self):
        """relay owns where Alloy's data lives; a second default is how two
        halves of the app disagree about it (fork.py's gotcha)."""
        self.assertFalse([n for n in dir(plan_limits)
                          if n.endswith("_DIR") or n.endswith("_ROOT")
                          or n.endswith("_FILE")])


class AdapterTests(unittest.TestCase):
    def agent(self):
        return relay.ClaudeAgent(tempfile.gettempdir(),
                                 model="claude-haiku-4-5")

    def test_a_real_stream_line_is_captured_onto_the_agent(self):
        agent = self.agent()
        acts = agent.activity(json.dumps(EVENT))
        self.assertEqual(len(agent.last_limits), 1)
        self.assertEqual(agent.last_limits[0]["kind"], "seven_day")
        self.assertFalse(list(acts or ()),
                         "account state must not be narrated as a work step")

    def test_a_malformed_line_captures_nothing_and_never_raises(self):
        agent = self.agent()
        for line in ("{not json", "", "   ", "null",
                     json.dumps({"type": "rate_limit_event"})):
            self.assertFalse(list(agent.activity(line) or ()))
        self.assertEqual(agent.last_limits, [])

    def test_two_windows_in_one_turn_both_land(self):
        agent = self.agent()
        agent.activity(json.dumps(EVENT))
        other = {"type": "rate_limit_event",
                 "rate_limit_info": dict(PAYLOAD, rateLimitType="five_hour")}
        agent.activity(json.dumps(other))
        self.assertEqual(len(agent.last_limits), 2)

    def test_only_claude_reads_these_lines(self):
        """Honestly blank elsewhere, like wall_ms and cost. A GPT/Gemini/Ox
        adapter that invented a reading would publish one account's quota as
        another provider's."""
        for cls in (relay.CodexAgent, relay.GeminiAgent, relay.OpenCodeAgent):
            agent = cls(tempfile.gettempdir())
            try:
                agent.activity(json.dumps(EVENT))
            except Exception as exc:            # must never raise either
                self.fail("%s.activity raised %r" % (cls.__name__, exc))
            self.assertEqual(getattr(agent, "last_limits", []), [])


class RecordTests(unittest.TestCase):
    class IO:
        def __init__(self):
            self.events = []

        def emit(self, event, payload=None):
            self.events.append((event, payload))

    class Boom(IO):
        def emit(self, event, payload=None):
            raise RuntimeError("front end exploded")

    def state(self, io=None):
        return {"_usage_io": io} if io is not None else {}

    def agent_with(self, *readings):
        agent = relay.ClaudeAgent(tempfile.gettempdir())
        agent.last_limits = [r for r in readings]
        return agent

    def test_readings_are_merged_into_the_private_state_key(self):
        io = self.IO()
        state = self.state(io)
        relay.record_plan_limits(state, self.agent_with(reading()))
        self.assertIn("seven_day", state["_plan_limits"])
        self.assertEqual(io.events[0][0], "plan_limits")

    def test_the_state_key_is_private_so_it_never_reaches_disk(self):
        """An ACCOUNT fact written into a session's meta records it as
        something that was true of that conversation."""
        self.assertTrue("_plan_limits".startswith("_"))
        state = self.state()
        relay.record_plan_limits(state, self.agent_with(reading()))
        saved = relay.SessionStore.save.__doc__ or ""
        self.assertNotIn("_plan_limits", saved)

    def test_a_turn_that_reported_nothing_leaves_the_snapshot_alone(self):
        state = self.state()
        relay.record_plan_limits(state, self.agent_with(reading()))
        before = dict(state["_plan_limits"])
        relay.record_plan_limits(state, self.agent_with())
        self.assertEqual(state["_plan_limits"], before)

    def test_a_second_window_merges_rather_than_replacing(self):
        state = self.state()
        relay.record_plan_limits(state, self.agent_with(
            reading(rateLimitType="five_hour")))
        relay.record_plan_limits(state, self.agent_with(reading()))
        self.assertEqual(sorted(state["_plan_limits"]),
                         ["five_hour", "seven_day"])

    def test_a_front_end_that_raises_never_fails_the_turn(self):
        """Same contract as activity narration and record_usage: telemetry
        must never break the work it describes."""
        state = self.state(self.Boom())
        relay.record_plan_limits(state, self.agent_with(reading()))
        self.assertIn("seven_day", state["_plan_limits"])

    def test_the_turn_reset_clears_last_limits(self):
        """Source-level, because driving a real turn costs a CLI call: the
        reset must sit beside last_context's, or a turn that reports nothing
        republishes the previous one's numbers."""
        import inspect
        src = inspect.getsource(relay.Agent.turn)
        self.assertIn("self.last_limits = []", src)


class StreamWiringTests(unittest.TestCase):
    """A real Agent.turn against a `python -c` child. No CLI, no tokens."""

    def test_the_stream_is_parsed_even_with_no_narration_sink(self):
        """The second bug the first live run found.

        `on_line` was built only `if on_activity is not None`, so the adapter
        parsed its own stdout only when a front end happened to be narrating.
        That was harmless while activity() did nothing but narrate, and wrong
        the moment it also captured the account's plan quota: a real turn set
        last_context and left last_limits empty. Whether anyone is WATCHING is
        a front-end question; what an adapter reads off its own stream is not.
        """
        payload = json.dumps({"type": "rate_limit_event",
                              "rate_limit_info": dict(PAYLOAD)})

        class Child(relay.ClaudeAgent):
            def build_cmd(self, message):
                return [sys.executable, "-c",
                        "print(%r);print(%r)" % (payload, json.dumps(
                            {"type": "result", "subtype": "success",
                             "result": "ok", "is_error": False,
                             "session_id": "sid"}))]

        agent = Child(tempfile.gettempdir(), model="claude-haiku-4-5")
        reply = agent.turn("hi")                       # NO on_activity
        self.assertEqual(reply.strip(), "ok")
        self.assertEqual([r["kind"] for r in agent.last_limits], ["seven_day"])

    def test_a_narration_sink_still_receives_ordinary_activity(self):
        """The fix must not stop acts reaching a sink that IS attached."""
        seen = []

        class Child(relay.ClaudeAgent):
            def build_cmd(self, message):
                return [sys.executable, "-c",
                        "print(%r);print(%r)" % (
                            json.dumps({"type": "system",
                                        "subtype": "thinking_tokens",
                                        "estimated_tokens": 12}),
                            json.dumps({"type": "result",
                                        "subtype": "success", "result": "ok",
                                        "is_error": False,
                                        "session_id": "sid"}))]

        agent = Child(tempfile.gettempdir(), model="claude-haiku-4-5")
        agent.turn("hi", on_activity=seen.append)
        self.assertTrue([a for a in seen if a.get("kind") == "progress"])


class ProbeTests(unittest.TestCase):
    """The probe, driven against a real child process — no CLI, no tokens."""

    def _child(self, script):
        old = relay.resolve_cmd
        relay.resolve_cmd = lambda argv: [sys.executable, "-c", script]
        self.addCleanup(lambda: setattr(relay, "resolve_cmd", old))

    def test_a_byte_the_ansi_codepage_cannot_map_does_not_kill_the_probe(self):
        """THE bug the first live run found, and no token-free test had.

        `subprocess.run(text=True)` decodes with the ANSI codepage (cp1252
        here), a real claude stream carries byte 0x90, and the resulting
        UnicodeDecodeError was swallowed by the probe's own `except` — so the
        brake returned {} and failed OPEN on every real invocation while the
        whole suite stayed green. U+0090 encodes to C2 90 in UTF-8, which is
        exactly the byte that raised.
        """
        payload = json.dumps({"type": "rate_limit_event",
                              "rate_limit_info": dict(PAYLOAD)})
        self._child(
            "import sys;"
            "sys.stdout.buffer.write('\\u0090\\n'.encode('utf-8'));"
            "sys.stdout.buffer.write((%r + '\\n').encode('utf-8'))" % payload)
        snap = relay.probe_plan_limits()
        self.assertIn("seven_day", snap,
                      "an undecodable byte must not lose the reading")
        self.assertAlmostEqual(snap["seven_day"]["utilization"], 0.79)

    def test_a_child_that_prints_nothing_is_an_empty_snapshot_not_a_crash(self):
        """Silence is the NORMAL case — the CLI reports a limit only once it
        passes its own warning threshold."""
        self._child("pass")
        self.assertEqual(relay.probe_plan_limits(), {})

    def test_junk_on_the_stream_is_skipped_line_by_line(self):
        payload = json.dumps({"type": "rate_limit_event",
                              "rate_limit_info": dict(PAYLOAD)})
        self._child("print('not json');print('{');print(%r)" % payload)
        self.assertIn("seven_day", relay.probe_plan_limits())

    def test_a_child_that_fails_returns_empty_and_never_raises(self):
        self._child("import sys; sys.exit(3)")
        self.assertEqual(relay.probe_plan_limits(), {})
        old = relay.resolve_cmd
        relay.resolve_cmd = lambda argv: ["a-binary-that-does-not-exist-xyz"]
        self.addCleanup(lambda: setattr(relay, "resolve_cmd", old))
        self.assertEqual(relay.probe_plan_limits(), {})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-brake-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old = relay.SESSIONS_DIR
        relay.SESSIONS_DIR = self.tmp
        self.addCleanup(lambda: setattr(relay, "SESSIONS_DIR", self.old))

    def test_round_trip_normalizes_percent_to_a_fraction(self):
        relay.write_plan_brake(80)
        self.assertAlmostEqual(relay.read_plan_brake()["threshold"], 0.8)

    def test_off_is_a_legitimate_saved_state(self):
        relay.write_plan_brake(80)
        relay.write_plan_brake(None)
        self.assertIsNone(relay.read_plan_brake()["threshold"])

    def test_a_missing_or_corrupt_file_means_no_brake(self):
        self.assertIsNone(relay.read_plan_brake()["threshold"])
        with open(os.path.join(self.tmp, relay.PLAN_BRAKE_FILE), "w") as f:
            f.write("{not json")
        self.assertIsNone(relay.read_plan_brake()["threshold"])

    def test_a_junk_threshold_on_disk_reads_as_no_brake_not_zero(self):
        with open(os.path.join(self.tmp, relay.PLAN_BRAKE_FILE), "w") as f:
            json.dump({"version": 1, "threshold": "eighty"}, f)
        self.assertIsNone(relay.read_plan_brake()["threshold"])

    def test_the_path_is_joined_at_call_time_not_import_time(self):
        """The write_tabs lesson: a module constant captured at import
        survives a test's redirect and writes into Josh's real sessions."""
        relay.write_plan_brake(70)
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, relay.PLAN_BRAKE_FILE)))
        other = tempfile.mkdtemp(prefix="alloy-brake2-")
        self.addCleanup(shutil.rmtree, other, True)
        relay.SESSIONS_DIR = other
        self.assertIsNone(relay.read_plan_brake()["threshold"])

    def test_concurrent_writers_do_not_collide(self):
        errors = []

        def hammer(n):
            try:
                for _ in range(8):
                    relay.write_plan_brake(50 + n)
                    relay.read_plan_brake()
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=hammer, args=(i,))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


class BridgeTests(unittest.TestCase):
    """The REAL app.Api. W0.1's lesson: the engine can be perfect while the
    bridge drops the key, so the brake is driven through the shipping
    `_launch_schedule`, not a reimplementation of it."""

    def setUp(self):
        import app as app_mod
        self.app_mod = app_mod
        self.tmp = tempfile.mkdtemp(prefix="alloy-brake-bridge-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        olds = (relay.SESSIONS_DIR, relay.TABS_FILE, app_mod.SESSIONS_DIR)
        relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        app_mod.SESSIONS_DIR = self.tmp

        def restore():
            (relay.SESSIONS_DIR, relay.TABS_FILE,
             app_mod.SESSIONS_DIR) = olds
        self.addCleanup(restore)
        self.api = app_mod.Api()

    def test_the_snapshot_starts_empty_and_is_not_a_public_attr(self):
        """Public attributes on the js_api object are walked by the pywebview
        bridge at page load and deadlock it — the oldest rule in app.py."""
        self.assertEqual(self.api._plan_limits, {})
        self.assertFalse([n for n in dir(self.api)
                          if "plan_limit" in n and not n.startswith("_")
                          and not callable(getattr(self.api, n, None))])

    def test_get_plan_limits_reports_absence_as_absence(self):
        got = self.api.get_plan_limits()
        self.assertEqual(got["limits"], {})
        self.assertIsNone(got["worst"])
        self.assertIsNone(got["threshold"])
        self.assertEqual(got["summary"], "")

    def test_set_plan_brake_round_trips_percent_through_the_bridge(self):
        self.assertAlmostEqual(
            self.api.set_plan_brake(90)["threshold"], 0.9)
        self.assertAlmostEqual(
            self.api.get_plan_limits()["threshold"], 0.9)
        self.assertIsNone(self.api.set_plan_brake(None)["threshold"])

    def test_an_emitted_reading_lands_in_the_process_store(self):
        run = self.api._runs.background()
        io = self.app_mod._AppIO(self.api, run)
        io.emit("plan_limits", {"limits": {"seven_day": reading()}})
        self.assertIn("seven_day", self.api._plan_limits)
        self.assertAlmostEqual(
            self.api.get_plan_limits()["worst"]["utilization"], 0.79)

    def test_the_store_merges_across_runs_rather_than_replacing(self):
        """Two conversations observe the same account. A second run's lone
        seven_day reading must not erase the five_hour one the first saw."""
        run = self.api._runs.background()
        io = self.app_mod._AppIO(self.api, run)
        io.emit("plan_limits",
                {"limits": {"five_hour": reading(rateLimitType="five_hour")}})
        other = self.app_mod._AppIO(self.api, self.api._runs.background())
        other.emit("plan_limits", {"limits": {"seven_day": reading()}})
        self.assertEqual(sorted(self.api._plan_limits),
                         ["five_hour", "seven_day"])

    def test_no_brake_set_means_the_launch_path_spends_no_cli_call(self):
        """A run must never pay for a probe to check a limit nobody set."""
        calls = []
        old = relay.probe_plan_limits
        relay.probe_plan_limits = lambda *a, **k: calls.append(1) or {}
        self.addCleanup(lambda: setattr(relay, "probe_plan_limits", old))
        allow, why = self.api._plan_brake_verdict()
        self.assertTrue(allow)
        self.assertEqual(calls, [])

    def test_the_brake_refuses_a_launch_and_the_sentence_reaches_the_record(self):
        """Driven through the SHIPPING _launch_schedule, which is the one fire
        path Run-now uses too."""
        self.api.set_plan_brake(75)
        old = relay.probe_plan_limits
        relay.probe_plan_limits = lambda *a, **k: {"seven_day": reading()}
        self.addCleanup(lambda: setattr(relay, "probe_plan_limits", old))
        ok, text = self.api._launch_schedule(self._stub_room())
        self.assertFalse(ok)
        self.assertIn("79%", text)
        self.assertIn("75%", text)

    def _stub_room(self):
        """Everything _launch_schedule needs EXCEPT the brake, so the brake is
        the only thing the assertion can be measuring."""
        self.api._room_cfg = lambda name: {"seats": []}
        old_grants = self.app_mod.schedule_mod.grants_for
        old_gap = self.app_mod.schedule_mod.ack_gap
        self.app_mod.schedule_mod.grants_for = lambda axes: []
        self.app_mod.schedule_mod.ack_gap = lambda rec, grants: []
        self.addCleanup(lambda: setattr(
            self.app_mod.schedule_mod, "grants_for", old_grants))
        self.addCleanup(lambda: setattr(
            self.app_mod.schedule_mod, "ack_gap", old_gap))
        return {"id": "s1", "name": "Nightly", "room": "Room A",
                "prompt": "go", "turns": 4}

    def _count_probes(self):
        calls = []
        old = relay.probe_plan_limits
        relay.probe_plan_limits = lambda *a, **k: calls.append(1) or {}
        self.addCleanup(lambda: setattr(relay, "probe_plan_limits", old))
        return calls

    def test_run_now_decides_on_the_stored_snapshot_without_a_subprocess(self):
        """Run-now arrives on the pywebview BRIDGE THREAD, where a subprocess
        deadlocks the window — so `manual` must reach the probe flag.

        Driven through the real `_launch_schedule`, NOT by calling
        _plan_brake_verdict(probe=False) directly: the first version of this
        test did that, and a RED pass proved it could not see its own subject.
        Mutating the call site to `probe=True` — reintroducing the deadlock —
        left it green.
        """
        self.api.set_plan_brake(75)
        rec = self._stub_room()
        calls = self._count_probes()
        with self.api._limits_lock:
            self.api._plan_limits = {"seven_day": reading()}
        ok, text = self.api._launch_schedule(rec, manual=True)
        self.assertFalse(ok)             # same verdict, stale-but-sound basis
        self.assertIn("79%", text)
        self.assertEqual(calls, [], "Run-now must not shell out")

    def test_the_timer_path_DOES_take_a_fresh_reading(self):
        """The other half of the same rule: a scheduled fire at 01:00 may be
        deciding on a snapshot from yesterday evening, so it probes."""
        self.api.set_plan_brake(75)
        rec = self._stub_room()
        calls = self._count_probes()
        self.api._launch_schedule(rec, manual=False)
        self.assertEqual(len(calls), 1)

    def test_a_probe_that_explodes_never_breaks_the_launch(self):
        self.api.set_plan_brake(75)
        old = relay.probe_plan_limits

        def boom(*a, **k):
            raise RuntimeError("cli gone")
        relay.probe_plan_limits = boom
        self.addCleanup(lambda: setattr(relay, "probe_plan_limits", old))
        allow, why = self.api._plan_brake_verdict()
        self.assertTrue(allow)           # fails OPEN, by design
        self.assertIn("could not be checked", why)


class UiWiringTests(unittest.TestCase):
    """The rendering half is driven through the REAL page in
    tests/test_ui_boot.py (`report["plan"]`) — that harness owns the node
    runner and is the only level that can see a top-level JS throw. What is
    checked HERE is the wiring that harness could not, because a swallowed
    ReferenceError looks exactly like a quiet no-op."""

    def setUp(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "ui", "index.html")
        with open(path, encoding="utf-8") as f:
            self.src = f.read()

    def test_the_bridge_is_reached_through_pywebview_at_top_level(self):
        """`const api = pywebview.api` is FUNCTION-scoped in this file, so a
        top-level `api.get_plan_limits()` is a ReferenceError — and both call
        sites sit inside a try/catch whose whole job is to never break the
        app, so it failed silently and the strip simply never appeared. Only
        driving the real page found it."""
        for name in ("get_plan_limits", "set_plan_brake"):
            self.assertIn("pywebview.api.%s(" % name, self.src)
            self.assertNotIn("await api.%s(" % name, self.src)

    def test_the_quota_strip_is_its_own_row_not_folded_into_the_budget(self):
        """#budgetStrip is THIS CHAT's spend; this is the whole ACCOUNT's. One
        row mixing them invites reading the quota as something this
        conversation caused."""
        self.assertIn('id="planStrip"', self.src)
        self.assertIn("#contStrip, #budgetStrip, #planStrip {", self.src)

    def test_the_brake_field_is_never_a_number_input(self):
        """WebView2 draws duplicate spinners and reports "" for partial input,
        which makes clamp-on-blur lie (the #rVal lesson).

        Comments are stripped FIRST: the comment above that input explains why
        it is not type="number" and therefore contains the exact string this
        asserts against. A substring test that cannot tell a statement from a
        mention is this repo's most-repeated test bug, and it caught me
        writing the test itself."""
        import re
        clean = re.sub(r"<!--.*?-->", "", self.src, flags=re.S)
        i = clean.index('id="schedBrake"')
        tag_start = clean.rindex("<input", 0, i)
        tag = clean[tag_start:clean.index(">", i) + 1]
        self.assertNotIn('type="number"', tag)
        self.assertIn('type="text"', tag)

    def test_the_brake_note_lives_outside_the_container_that_hides_itself(self):
        """#schedGrants is hidden for an innocuous room, and the brake applies
        to every unattended fire (#queueNote's lesson).

        Measured by DIV DEPTH, not by index order: the note does sit between
        `id="schedGrants"` and `id="schedNote"` in the source and is still
        outside the container, because the container closes in between. An
        ordering assertion would have failed a correct layout — and, worse,
        passed a broken one the moment a sibling moved."""
        import re
        clean = re.sub(r"<!--.*?-->", "", self.src, flags=re.S)
        start = clean.index('id="schedGrants"')
        depth, i, end = 0, clean.rindex("<div", 0, start), None
        for m in re.finditer(r"<div\b|</div>", clean[i:]):
            depth += 1 if m.group(0) == "<div" else -1
            if depth == 0:
                end = i + m.end()
                break
        self.assertIsNotNone(end, "could not find the end of #schedGrants")
        brake = clean.index('id="schedBrakeNote"')
        self.assertFalse(start < brake < end,
                         "the brake note must not sit inside #schedGrants")


if __name__ == "__main__":
    unittest.main(verbosity=2)
