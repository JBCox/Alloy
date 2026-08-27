"""Saved room templates: the store beside tabs.json + the app bridge.

relay.save_room/list_rooms/delete_room own one JSON file derived from
relay.SESSIONS_DIR exactly like TABS_FILE — a template exists before and
across conversations, so it must never live in a session's meta.json (the
loop rewrites meta after every fan-out). The Api methods are synchronous
bridge-thread file I/O, same class as get_skills/save_tabs.

Run:  python tests/test_rooms.py
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


class FakeWindow:
    def evaluate_js(self, script):
        pass


CFG = {
    "permission": "ask", "workspace": None, "brief": False,
    "mode": "round_robin",
    "orchestration": {"preset": "open_discussion", "legacy_mode": "round_robin",
                      "workflow": "conversation", "concurrency": "sequential",
                      "floor": "cyclic", "routing": "broadcast",
                      "completion": "participants",
                      "budget": {"unit": "laps", "limit": 10,
                                 "until_done": False}},
    "moderator": None, "until_done": False, "ceiling": 60,
    "continuous": None,
    "spawn": {"tier1": True, "max_helpers": 0, "max_teams": 0},
    "seats": [
        {"id": 0, "provider": "claude", "enabled": True,
         "model": "claude-opus-5", "effort": "high", "label": "Optimist",
         "role": "Researcher", "role_instructions": "Cite every claim."},
        {"id": 1, "provider": "ox", "enabled": True,
         "model": "opencode/ox-alpha", "effort": "max", "label": None,
         "role": "", "role_instructions": ""},
    ],
}


class RoomStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-rooms-test-")
        self._old = (relay.SESSIONS_DIR, relay.TABS_FILE, relay.ROOMS_FILE)
        relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        relay.ROOMS_FILE = os.path.join(self.tmp, "rooms.json")

    def tearDown(self):
        relay.SESSIONS_DIR, relay.TABS_FILE, relay.ROOMS_FILE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_list_round_trip_preserves_the_cfg_verbatim(self):
        r = relay.save_room("Code review", CFG)
        self.assertTrue(r["ok"])
        self.assertEqual(r["name"], "Code review")
        self.assertTrue(r["saved_at"])
        got = relay.list_rooms()
        self.assertEqual(got["version"], 1)
        self.assertEqual(len(got["rooms"]), 1)
        row = got["rooms"][0]
        self.assertEqual(row["name"], "Code review")
        self.assertEqual(row["cfg"], CFG)
        self.assertEqual(row["saved_at"], r["saved_at"])
        # the file lives where SESSIONS_DIR says, beside tabs.json
        self.assertEqual(relay.ROOMS_FILE, os.path.join(self.tmp, "rooms.json"))
        with open(relay.ROOMS_FILE, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["rooms"]["Code review"]["cfg"], CFG)

    def test_names_are_trimmed_and_sorted_newest_first(self):
        relay.save_room("  Two-Opus argument  ", CFG)
        rooms = [r["name"] for r in relay.list_rooms()["rooms"]]
        self.assertEqual(rooms, ["Two-Opus argument"])
        second = dict(CFG, mode="free")
        relay.save_room("Cheap grunt work", second)
        rows = relay.list_rooms()["rooms"]
        self.assertEqual([r["name"] for r in rows],
                         ["Cheap grunt work", "Two-Opus argument"],
                         "the room saved LAST must list FIRST")
        self.assertEqual(rows[0]["cfg"]["mode"], "free")

    def test_saving_an_existing_name_overwrites(self):
        relay.save_room("Debate", dict(CFG, ceiling=30))
        relay.save_room("Debate", dict(CFG, ceiling=99))
        rows = relay.list_rooms()["rooms"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cfg"]["ceiling"], 99)

    def test_bad_names_are_rejected_never_sanitized(self):
        for bad in ("", "   ", None, 42, "x" * (relay.ROOM_NAME_MAX + 1)):
            with self.assertRaises(ValueError, msg=repr(bad)):
                relay.save_room(bad, CFG)
        self.assertEqual(relay.list_rooms()["rooms"], [])

    def test_non_dict_cfg_is_rejected(self):
        with self.assertRaises(ValueError):
            relay.save_room("ok name", ["not", "a", "dict"])
        self.assertEqual(relay.list_rooms()["rooms"], [])

    def test_delete_missing_is_a_clean_false_and_existing_removes(self):
        self.assertFalse(relay.delete_room("ghost"))
        relay.save_room("gone soon", CFG)
        self.assertTrue(relay.delete_room("gone soon"))
        self.assertEqual(relay.list_rooms()["rooms"], [])
        self.assertFalse(relay.delete_room("gone soon"),
                         "deleting twice must stay a clean False")

    def test_corrupt_or_missing_store_reads_as_no_rooms(self):
        self.assertEqual(relay.list_rooms(), {"version": 1, "rooms": []})
        self.assertFalse(relay.delete_room("anything"))
        with open(relay.ROOMS_FILE, "w", encoding="utf-8") as f:
            f.write("{not json at all")
        self.assertEqual(relay.list_rooms()["rooms"], [])
        self.assertFalse(relay.delete_room("anything"))
        with open(relay.ROOMS_FILE, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "rooms": "not a dict"}, f)
        self.assertEqual(relay.list_rooms()["rooms"], [])

    def test_malformed_entries_are_dropped_not_fatal(self):
        with open(relay.ROOMS_FILE, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "rooms": {
                "good": {"cfg": {"mode": "parallel"}, "saved_at":
                         "2026-08-25T10:00:00"},
                "no-cfg": {"saved_at": "2026-08-25T09:00:00"},
                "garbage": "just a string",
            }}, f)
        rows = relay.list_rooms()["rooms"]
        self.assertEqual([r["name"] for r in rows], ["good"])

    def test_explicit_path_argument_wins_over_the_module_global(self):
        other = os.path.join(self.tmp, "other.json")
        relay.save_room("elsewhere", CFG, path=other)
        self.assertEqual([r["name"] for r in relay.list_rooms(path=other)["rooms"]],
                         ["elsewhere"])
        self.assertEqual(relay.list_rooms()["rooms"],
                         [], "the patched global's store stays untouched")


class RoomBridgeTests(unittest.TestCase):
    """The real app.Api against a fake window — the get_skills shape:
    synchronous, bounded file I/O only, never a subprocess."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-rooms-bridge-")
        self._old_app_dir = app.SESSIONS_DIR
        self._old = (relay.SESSIONS_DIR, relay.TABS_FILE, relay.ROOMS_FILE)
        app.SESSIONS_DIR = self.tmp
        relay.SESSIONS_DIR = self.tmp
        relay.TABS_FILE = os.path.join(self.tmp, "tabs.json")
        relay.ROOMS_FILE = os.path.join(self.tmp, "rooms.json")
        self.api = app.Api()
        self.api._window = FakeWindow()

    def tearDown(self):
        app.SESSIONS_DIR = self._old_app_dir
        relay.SESSIONS_DIR, relay.TABS_FILE, relay.ROOMS_FILE = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_then_get_round_trips_through_the_bridge(self):
        r = self.api.save_room("Code review", CFG)
        self.assertTrue(r.get("ok"), r)
        got = self.api.get_rooms()
        self.assertEqual(got["version"], 1)
        self.assertEqual(got["rooms"][0]["name"], "Code review")
        self.assertEqual(got["rooms"][0]["cfg"], CFG)

    def test_bridge_save_rejects_a_bad_name_as_an_error_sentence(self):
        r = self.api.save_room("   ", CFG)
        self.assertIn("error", r)
        self.assertNotIn("ok", r)
        self.assertEqual(self.api.get_rooms()["rooms"], [])

    def test_bridge_delete_unknown_is_an_error_not_an_exception(self):
        r = self.api.delete_room("ghost")
        self.assertIn("error", r)
        self.api.save_room("real", CFG)
        self.assertEqual(self.api.delete_room("real"), {"ok": True})
        self.assertEqual(self.api.get_rooms()["rooms"], [])


class RoomsUIMarkupTests(unittest.TestCase):
    """Text-level guards for the one-inline-script UI: a new modal id MUST be
    in BOTH the display:none and .show selector groups AND the shared Escape
    listener, or it ships half-wired (the skillModal checklist, verbatim)."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "ui", "index.html")
        with open(path, encoding="utf-8") as f:
            cls.html = f.read()

    def test_modal_id_is_in_both_css_selector_groups(self):
        # Both patterns check that the rooms id is IN its group, not that it
        # is the LAST id in it — every later modal (event hooks, desktop
        # control) legitimately joins the same two groups, and pinning the
        # tail made this suite fail for the crime of someone adding one.
        self.assertRegex(
            self.html, r"#acctModal,\s*#roleModal[^{]*#roomsModal[^{]*\{")
        self.assertRegex(
            self.html,
            r"#contModal\.show,\s*#kbdModal\.show,[^{]*#roomsModal\.show[^{]*\{")

    def test_escape_listener_closes_the_rooms_modal(self):
        # Anchored on the SHARED document listener, not on the first
        # `e.key === "Escape"` in the file: a textarea that handles its own
        # Escape (the W1.7 note editor) is an earlier match, and this test
        # then read a slice of unrelated code and failed for it. Same family
        # as the wrap-token bug — a substring match that cannot tell one
        # occurrence from another.
        m = self.html.index('closeAccounts(); closeRole();')
        branch = self.html[m - 200:m + 400]
        self.assertIn('e.key === "Escape"', branch)
        self.assertIn("closeRooms()", branch)

    def test_button_and_modal_markup_exist_with_wired_ids(self):
        for needle in ('id="roomsBtn"', 'id="roomsModal"', 'id="roomsClose"',
                       'id="roomSaveBtn"', 'id="roomName"', 'id="roomList"',
                       '$("roomsBtn").onclick', "$(\"roomSaveBtn\").onclick"):
            self.assertIn(needle, self.html)


if __name__ == "__main__":
    unittest.main(verbosity=1)
