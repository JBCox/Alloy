"""Static contracts for the permission HUD and per-seat live telemetry."""

import os
import re
import unittest
from html.parser import HTMLParser


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html")


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])


class TelemetryUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as f:
            cls.source = f.read()

    def test_permission_hud_has_visible_and_detail_surfaces(self):
        ids = _Ids()
        ids.feed(self.source)
        for expected in ("permissionHud", "permissionBadge",
                         "permissionBadgeText", "permissionPopover",
                         "permissionDetail", "permissionScope"):
            self.assertIn(expected, ids.ids)
        for level in ("read_only", "ask", "auto", "full"):
            self.assertIn(level, self.source)

    def test_hud_uses_persisted_session_permission(self):
        self.assertIn("seed?.permission || seed?.session?.permission", self.source)
        self.assertIn("payload.session.permission || payload.permission", self.source)
        self.assertRegex(self.source, r"renderPermissionHud\(run\?\.permission")

    def test_seat_telemetry_tracks_time_and_human_waits(self):
        self.assertIn("const seatTelemetry = new Map()", self.source)
        self.assertIn("setInterval(tick, 1000)", self.source)
        self.assertIn('"awaiting approval"', self.source)
        self.assertRegex(self.source,
                         r'setSeatTelemetry\(payload\.speaker, activityState')

    def test_approval_hub_exposes_risk_context_and_feedback(self):
        ids = _Ids()
        ids.feed(self.source)
        for expected in ("approvalReview", "approvalRisk", "approvalTool",
                         "approvalWhy", "approvalBlast", "approvalCwd",
                         "approvalRationale", "approvalContext"):
            self.assertIn(expected, ids.ids)
        self.assertIn('p.kind === "permission"', self.source)
        self.assertIn('option === "Deny with feedback"', self.source)
        self.assertIn('"Deny: " + t', self.source)

    def test_supervisor_control_log_is_live_and_reopenable(self):
        ids = _Ids()
        ids.feed(self.source)
        for expected in ("supervisorPanel", "supervisorHead",
                         "supervisorTrace", "supervisorCount",
                         "supervisorDisclosure", "supervisorOverview",
                         "supervisorGoal", "supervisorTaskMap"):
            self.assertIn(expected, ids.ids)
        self.assertIn('event === "supervisor"', self.source)
        self.assertIn("appendSupervisorTrace(chatId, payload.entry)", self.source)
        self.assertIn("s.supervisor_trace || []", self.source)
        self.assertIn("not private model reasoning", self.source)
        self.assertIn("entry.type || entry.phase", self.source)
        self.assertIn('className = "sup-change"', self.source)
        self.assertIn("mergeSupervisorTasks(chatId", self.source)

    def test_rolling_manager_states_are_visually_distinct(self):
        """Review, new wave and closing verdict are the three things Josh
        asked to actually SEE. The pill renders from entry.type generically,
        so the only thing that can silently go missing is their styling."""
        for phase in ("review", "wave", "accepted"):
            self.assertIn(".sup-event[data-phase=%s] .sup-phase" % phase,
                          self.source)

    def test_control_log_is_segmented_into_waves(self):
        """The log has to read as cycles, not a scrolling list: one container
        per dispatched wave, cut on plan_created."""
        self.assertIn("function supervisorWaves(entries)", self.source)
        self.assertIn('entry.type === "plan_created"', self.source)
        self.assertIn('className = "sup-wave"', self.source)
        self.assertIn("`Wave ${wave.n || i + 1}`", self.source)
        self.assertIn(".sup-wave.folded .sup-wave-rows", self.source)

    def test_closing_verdict_is_not_just_another_row(self):
        """'The manager decided this is finished' must never look like 'the
        round cap ran out' — the verdict is lifted out of the event stream."""
        self.assertIn('entry.type === "goal_accepted"', self.source)
        self.assertIn('className = "sup-verdict"', self.source)
        self.assertIn("Supervisor closed the job", self.source)
        self.assertIn(".sup-verdict-title", self.source)

    def test_an_unfinished_review_pulses(self):
        """work_reviewed is logged when the side call starts, so the newest
        such entry IS the manager still deliberating — no invented signal."""
        self.assertIn('entry.type === "work_reviewed"', self.source)
        self.assertIn(".sup-event.live .sup-phase", self.source)
        self.assertIn("@keyframes supPulse", self.source)

    def test_waves_come_from_the_engine_not_inference(self):
        """The trace is capped, so cutting on plan_created renumbers waves the
        moment the first plan scrolls out. entry.wave is authoritative."""
        self.assertIn("const n = entry.wave || null;", self.source)
        self.assertIn("`Wave ${wave.n || i + 1}`", self.source)

    def test_unfinished_supervision_has_its_own_terminal_card(self):
        self.assertIn('entry.type === "goal_unresolved"', self.source)
        self.assertIn('className = "sup-unresolved"', self.source)
        self.assertIn("No verdict", self.source)
        self.assertIn(".sup-unresolved-tag", self.source)

    def test_rail_row_shows_supervision_state(self):
        """One glance in the chat list: is this job being managed, was it
        closed by the manager, or did it just stop?"""
        self.assertIn("s.supervisor_status", self.source)
        self.assertIn("Supervisor · ${st.label}", self.source)
        self.assertIn("sub.dataset.sup = st.state", self.source)
        self.assertIn("The Supervisor closed this job itself", self.source)
        self.assertIn("Stopped without a Supervisor verdict", self.source)
        for st in ("accepted", "unresolved", "working"):
            self.assertIn(".chat-row .sub[data-sup=%s]" % st, self.source)

    def test_message_usage_telemetry_rendered_in_live_and_replay_paths(self):
        """Ensure per-message cost/token usage is styled, parsed from payloads,
        and rendered in both live emit and session replay paths."""
        self.assertIn(".msg-head .msg-usage", self.source)
        self.assertIn("function formatUsage(usage)", self.source)
        self.assertIn("usage.cost_usd", self.source)
        self.assertIn("usage.total_tokens", self.source)
        self.assertIn("payload.role, payload.ts, payload.activity, payload.usage", self.source)
        self.assertIn("m.meta || \"\", m.role, m.ts, m.activity, m.usage", self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
