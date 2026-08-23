"""V2 policy normalization and legacy compatibility contracts."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from test_loop import RecordingIO, build_state, saved_meta


class OrchestrationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-policy-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_legacy_mode_has_the_approved_recipe(self):
        expected = {
            "round_robin": ("sequential", "cyclic", "conversation", "laps"),
            "speaker": ("sequential", "nomination", "conversation", "turns"),
            "moderator": ("sequential", "moderated", "conversation", "turns"),
            "parallel": ("barrier", "all", "conversation", "laps"),
            "free": ("reactive", "fair", "conversation", "turns"),
            "supervisor": ("barrier", "manager", "supervisor", "waves"),
            "panel": ("barrier", "all", "panel", "phases"),
        }
        for mode, want in expected.items():
            p = relay.normalize_orchestration(mode=mode, turns=4)
            got = (p["concurrency"], p["floor"], p["workflow"],
                   p["budget"]["unit"])
            self.assertEqual(got, want, mode)
            self.assertEqual(p["legacy_mode"], mode)

    def test_policy_is_persisted_additively(self):
        state = build_state(self.tmp, [["a"], ["b"]], turns=1)
        relay.run_rounds(state, RecordingIO())
        p = saved_meta(state)["orchestration"]
        self.assertEqual(p["legacy_mode"], "round_robin")
        self.assertEqual(p["floor"], "cyclic")
        self.assertEqual(p["fairness"],
                         {"opening_circuit": True, "max_lead": 2})

    def test_legacy_mode_change_invalidates_a_stale_cached_recipe(self):
        state = build_state(self.tmp, [["a"], ["b"]], turns=1)
        self.assertEqual(state["orchestration"]["floor"], "cyclic")
        state["mode"] = "speaker"
        self.assertEqual(relay.orchestration(state)["floor"], "nomination")
        self.assertEqual(relay.orchestration(state)["budget"]["unit"], "turns")

    def test_unknown_additive_values_fall_back_without_topology_drift(self):
        p = relay.normalize_orchestration(
            {"concurrency": "telepathy", "floor": "chaos",
             "routing": "broadcast", "budget": {"unit": "vibes"}},
            mode="moderator", turns=3)
        self.assertEqual(p["concurrency"], "sequential")
        self.assertEqual(p["floor"], "moderated")
        self.assertEqual(p["routing"], "broadcast")
        self.assertEqual(p["budget"]["unit"], "turns")

    def test_old_free_meta_keeps_compatibility_broadcast_routing(self):
        p = relay.normalize_orchestration(None, mode="free", turns=3)
        self.assertEqual(p["routing"], "broadcast")

    def test_invalid_panel_axis_mix_normalizes_to_panel_contract(self):
        p = relay.normalize_orchestration(
            {"workflow": "panel", "concurrency": "sequential",
             "floor": "moderated", "routing": "addressed",
             "completion": "moderator", "budget": {"unit": "turns"}},
            mode="round_robin", turns=7)
        self.assertEqual((p["concurrency"], p["floor"], p["routing"],
                          p["completion"], p["budget"]["unit"]),
                         ("barrier", "all", "broadcast", "synthesizer",
                          "phases"))

    def test_report_names_every_corrected_explicit_axis(self):
        """A malformed Panel recipe reports each override, with a reason."""
        raw = {"workflow": "panel", "concurrency": "sequential",
               "floor": "nomination", "routing": "isolated",
               "completion": "participants",
               "budget": {"unit": "laps", "limit": 0},
               "fairness": {"max_lead": 0}}
        policy, changes = relay.normalize_orchestration_report(
            raw, mode="panel", turns=5)
        self.assertEqual(policy,
                         relay.normalize_orchestration(raw, "panel", 5))
        moved = {c["field"]: c for c in changes}
        self.assertEqual(set(moved), {"concurrency", "floor", "routing",
                                      "completion", "budget.unit",
                                      "budget.limit", "fairness.max_lead"})
        self.assertEqual(moved["floor"]["requested"], "nomination")
        self.assertEqual(moved["floor"]["applied"], "all")
        self.assertIn("Compare & Decide", moved["floor"]["reason"])
        self.assertEqual(moved["budget.limit"]["applied"], 1)
        for change in changes:
            self.assertTrue(change["label"] and change["reason"])

    def test_report_never_invents_a_correction(self):
        """Fields the caller never sent are defaults, not corrections."""
        _, changes = relay.normalize_orchestration_report(
            {"workflow": "conversation"}, mode="round_robin", turns=4)
        self.assertEqual(changes, [])
        # A legacy meta with no recipe at all has nothing to report either.
        self.assertEqual(
            relay.normalize_orchestration_report(None, "free", 3)[1], [])
        # An honored value is silent even when other axes moved around it.
        _, changes = relay.normalize_orchestration_report(
            {"workflow": "supervisor", "floor": "manager"},
            mode="supervisor", turns=4)
        self.assertEqual(changes, [])

    def test_report_flags_a_value_the_app_cannot_run(self):
        _, changes = relay.normalize_orchestration_report(
            {"floor": "bogus"}, mode="round_robin", turns=4)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["applied"], "cyclic")
        self.assertIn("not a value this app can run", changes[0]["reason"])

    def test_panel_call_preview_is_exactly_two_n_plus_one(self):
        recipe = relay.normalize_orchestration(mode="panel", turns=9)
        self.assertEqual(relay.estimate_calls(recipe, 3),
                         {"seat_calls": 7, "side_calls": 0,
                          "total_calls": 7, "estimated": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
