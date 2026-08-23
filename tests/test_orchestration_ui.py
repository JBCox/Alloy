"""Static UI contract for orchestration-v2's goal-first composer.

The executable boot suite catches JavaScript failures.  These assertions pin
the product vocabulary and bridge keys so a harmless-looking markup refactor
cannot silently send an old mode-only launch or conflate stopping with success.
"""

import os
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html")


class OrchestrationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_primary_composer_is_goal_first(self):
        for name, value in [
            # Verb-first titles: the card must say what YOU are doing, not
            # name an orchestration concept (Josh, 2026-08-22).
            ("Discuss in Turns", "open_discussion"),
            ("Compare &amp; Decide", "panel_review"),
            ("Build Together", "build_execute"),
            ("Talk Live", "live_room"),
        ]:
            self.assertIn(name, self.source)
            self.assertIn(f'data-preset="{value}"', self.source)
        self.assertIn('id="advancedOrchestration"', self.source)

    def test_choosing_the_moderator_is_a_primary_control(self):
        # It used to be reachable only by opening Advanced and finding "Who
        # speaks next", so the picker could not appear until you had already
        # found the knob that summons it (Josh, 2026-08-22).
        toggle = self.source.index('id="modToggleRow"')
        picker = self.source.index('id="modCtl"')
        drawer = self.source.index('id="advancedOrchestration"')
        self.assertLess(toggle, drawer)
        self.assertLess(picker, drawer)

    def test_the_moderator_toggle_mirrors_the_policy_axis(self):
        # A checkbox that kept its own state would be a second source of truth
        # the normalizer knows nothing about; it writes floorSel and re-runs.
        self.assertIn('$("floorSel").value = $("modOn").checked', self.source)
        self.assertIn('normalizePolicyControls("floorSel")', self.source)
        self.assertIn('$("modOn").checked = $("floorSel").value === "moderated"',
                      self.source)

    def test_advanced_drawer_exposes_each_policy_axis(self):
        for control in ["workflowSel", "concurrencySel", "floorSel",
                        "routingSel", "completionSel", "budgetUnitSel",
                        "fairnessLeadSel"]:
            self.assertIn(f'id="{control}"', self.source)
        # Josh, 2026-08-22: unsupported combinations no longer DISABLE a control.
        # Every knob stays pickable, the pick is honored, and the settings that
        # moved to accommodate it are named out loud.
        self.assertIn("POLICY_IDS.forEach(id => { $(id).disabled = seated; });",
                      self.source)
        self.assertIn('id="policyChanges"', self.source)
        self.assertIn("adjusted to match ", self.source)
        for anchored in ["AXIS_WORKFLOW", "showPolicyAdjustments"]:
            self.assertIn(anchored, self.source)
        # Changing WHEN they reply must leave Panel/Build rather than snapping
        # the user's timing back: concurrency is an anchor like the others.
        self.assertIn("concurrencySel: {},", self.source)
        # Compatibility recipe is saved metadata, not a steering control.
        self.assertIn('<label hidden id="modeSelRow">', self.source)
        # `.ctl label { display: block }` outranks the UA [hidden] rule, so the
        # attribute alone left it on screen. Verified live 2026-08-22.
        self.assertIn(".ctl label[hidden] { display: none; }", self.source)
        self.assertIn('legacy_mode: $("modeSel").value', self.source)

    def test_hand_tuned_recipes_are_labelled_custom(self):
        self.assertIn("function presetForCurrentRecipe()", self.source)
        self.assertIn('return "custom";', self.source)
        self.assertIn("setSelectedPreset(presetForCurrentRecipe())", self.source)

    def test_backend_corrections_reach_the_same_badges(self):
        self.assertIn("renderBackendAdjustments(payload.orchestration_adjustments",
                      self.source)
        self.assertIn("function renderBackendAdjustments(changes)", self.source)
        # Painted AFTER restoreOrchestration, whose anchorless normalize
        # would otherwise wipe the badges the engine just explained.
        restore = self.source.index("restoreOrchestration(payload.session")
        self.assertLess(restore, self.source.index("renderBackendAdjustments(payload"))
        # Launch must not re-normalize; that also clears the report.
        self.assertNotIn("function orchestrationCfg() {\n  normalizePolicyControls();",
                         self.source)

    def test_panel_preview_and_synthesizer_contract(self):
        self.assertIn("seatCalls = 2 * n + 1", self.source)
        self.assertIn('id="synthSel"', self.source)
        self.assertIn("orchestration,", self.source)
        self.assertIn("panel: orchestration.workflow === \"panel\" ? {synthesizer}",
                      self.source)

    def test_live_room_is_addressed_but_legacy_free_reopens_broadcast(self):
        self.assertIn('live_room: {mode: "free"', self.source)
        self.assertIn('routing: "addressed"', self.source)
        self.assertIn('restoring ? "broadcast" : "addressed"', self.source)
        self.assertIn('applyLegacyMode($("modeSel").value, false)', self.source)
        self.assertIn('applyLegacyMode(session.mode || "round_robin", true)',
                      self.source)

    def test_completion_is_three_separate_facts(self):
        for key in ["termination_reason", "goal_verdict", "verdict_source",
                    "lifecycle"]:
            self.assertIn(key, self.source)
        self.assertIn('term.textContent = `stopped: ${termination}`', self.source)
        self.assertIn('goal.textContent = `goal: ${verdict}`', self.source)
        self.assertNotIn('termination === "cap" ? "resolved"', self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
