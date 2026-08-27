"""Desktop control: the observe-cite-verify-act contract, refusals, delivery.

Token-free, window-free, hardware-free. Every test drives a FakeBackend
through the `backend` seam (dictation's `stream_factory` and speaker's
`runner`, one layer up: a whole world instead of one device), so no real
window is ever read and no real click is ever posted. One test is the
exception and says so: the probe pair really does load the .NET assemblies,
because "pythonnet is installed" and "the assemblies load" are different
facts and only the second one made probe() lie.

What this suite really guards:

 * **All five staleness refusals fire, by name, with the fix in the sentence.**
   A mutator that acts on a stale tree clicks whatever slid into that
   rectangle since -- the single failure mode this entire category has.
 * **strict_pixels stays OFF by default and STILL works when asked for.** A
   raw-pixel hash refuses on a blinking caret; a check that fires on healthy
   behaviour teaches people to route around it (the same lesson Alloy learned
   from the duration-based turn watchdog), so the default is measured here.
 * **The two scroll paths agree on direction.** dsh-click's UIA path mapped
   "up" to SmallIncrement while its wheel path used +1, so the same call
   scrolled opposite ways depending on which path won. Both now read one
   table, and the structural test flips the table to prove it.
 * **The process-identity check brackets the action.** dsh-click reads before
   and after back-to-back AFTER the action, comparing a value with itself.
   Here the call log is asserted: identity, act, identity.
 * **Alloy refuses to drive Alloy, at every rung.** A seat that can click the
   approval modal makes every approval forgeable; that is the never-forge
   rule, not a safety preference, so it has no override parameter to test.

Run:  python tests/test_desktop.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import desktop


# ------------------------------------------------------------- test doubles --

def el(element_id, control_type="Button", name="OK", rect=(10, 10, 80, 24),
       patterns=("invoke",), enabled=True, is_password=False, depth=1,
       automation_id=""):
    x, y, w, h = rect
    return {"element_id": element_id, "control_type": control_type,
            "name": name, "automation_id": automation_id,
            "rect": {"x": x, "y": y, "width": w, "height": h},
            "enabled": enabled, "patterns": list(patterns),
            "is_password": is_password, "depth": depth}


def window(window_id="w:1", title="Notepad", cls="Notepad", exe=r"C:\W\np.exe",
           pid=4242, rect=(0, 0, 800, 600)):
    x, y, w, h = rect
    return {"window_id": window_id, "pid": pid, "exe": exe, "title": title,
            "class_name": cls,
            "rect": {"x": x, "y": y, "width": w, "height": h}}


class Handle:
    """Stands in for a live AutomationElement."""

    def __init__(self, element_id):
        self.element_id = element_id


class FakeBackend(desktop.Backend):
    """A whole synthetic desktop: windows, trees, pixels and a call log.

    Everything a test wants to steer is a plain attribute -- rename a window
    to force STALE_IDENTITY, edit a tree to force STALE_TREE, swap `pixels` to
    force STALE_PIXELS. `calls` is the ordered log every delivery and
    bracketing assertion reads.
    """

    name = "fake"

    def __init__(self, windows=None, trees=None):
        self.windows = {w["window_id"]: dict(w)
                        for w in (windows or [window()])}
        self.trees = dict(trees or {"w:1": [el("1.1")]})
        self.pixels = {wid: b"pixels-" + wid.encode() for wid in self.windows}
        self.identities = {}              # window_id -> [(pid, exe), ...] queue
        self.values = {}                  # element_id -> current value
        self.missing = set()              # element_ids resolve() cannot find
        self.set_value_error = None       # raised by the FIRST set_value
        self.restore_error = None         # raised by the SECOND set_value
        self.invoke_ok = True
        self.scroll_ok = True
        self.blank = False
        self.capture_error = None
        self.seen_extra = 0               # pretend the walk visited more
        self.hit_visit_cap = False
        self.calls = []
        self._sets = 0

    def _log(self, *row):
        self.calls.append(row)

    # ----- observation
    def list_windows(self):
        self._log("list_windows")
        return [dict(w) for w in self.windows.values()]

    def window(self, window_id):
        self._log("window", window_id)
        found = self.windows.get(window_id)
        return dict(found) if found else None

    def walk(self, window_id, max_depth=desktop.MAX_DEPTH,
             max_visit=desktop.MAX_VISIT):
        self._log("walk", window_id)
        rows = [dict(e) for e in self.trees.get(window_id, [])]
        return {"elements": rows, "seen": len(rows) + self.seen_extra,
                "hit_visit_cap": self.hit_visit_cap}

    def capture(self, window_id, max_side=desktop.MAX_SIDE):
        self._log("capture", window_id)
        if self.capture_error:
            raise RuntimeError(self.capture_error)
        raw = self.pixels.get(window_id, b"")
        return {"png": None if self.blank else b"\x89PNG-fake", "width": 800,
                "height": 600, "blank": self.blank, "raw": raw}

    def process_identity(self, window_id):
        self._log("identity", window_id)
        queue = self.identities.get(window_id)
        if queue:
            return queue.pop(0)
        found = self.windows.get(window_id) or {}
        return found.get("pid"), found.get("exe")

    # ----- action
    def resolve(self, window_id, element_id):
        self._log("resolve", window_id, element_id)
        if element_id in self.missing:
            return None
        return Handle(element_id)

    def invoke(self, handle):
        self._log("invoke", handle.element_id)
        return self.invoke_ok

    def get_value(self, handle):
        self._log("get_value", handle.element_id)
        return self.values.get(handle.element_id)

    def set_value(self, handle, text):
        self._log("set_value", handle.element_id, text)
        self._sets += 1
        if self._sets == 1 and self.set_value_error:
            raise RuntimeError(self.set_value_error)
        if self._sets == 2 and self.restore_error:
            raise RuntimeError(self.restore_error)
        self.values[handle.element_id] = text
        return True

    def scroll_pattern(self, handle, amount, axis):
        self._log("scroll_pattern", handle.element_id, amount, axis)
        return self.scroll_ok

    def post_click(self, window_id, x, y, button):
        self._log("post_click", window_id, x, y, button)
        return True

    def post_wheel(self, window_id, x, y, delta, horizontal):
        self._log("post_wheel", window_id, x, y, delta, horizontal)
        return True

    def post_key(self, window_id, kind, vk, char=None):
        self._log("post_key", window_id, kind, vk, char)
        return True


class Clock:
    def __init__(self, now=1000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


def build(backend=None, **kwargs):
    """A Desktop with an empty self-pid set, so nothing is refused by accident."""
    kwargs.setdefault("self_pids", set())
    return desktop.Desktop(backend=backend or FakeBackend(), **kwargs)


def kinds(backend, *names):
    return [row for row in backend.calls if row[0] in names]


# ---------------------------------------------------------- sanitization -----

class SanitizeTests(unittest.TestCase):
    def test_control_characters_and_newlines_are_folded(self):
        # a name carrying a newline would forge an extra row in the text render;
        # tabs/newlines become a space, every other control byte just vanishes
        self.assertEqual(desktop.strip_controls("a\nb\tc\x07d"), "a b cd")
        self.assertEqual(desktop.strip_controls("\x1b[31mred\x1b[0m"),
                         "[31mred[0m")
        self.assertEqual(desktop.strip_controls(None), "")

    def test_assignments_are_redacted(self):
        for raw in ("api_key=sk-live-123456", "TOKEN: abcdefgh",
                    "password = hunter2", "the secret is swordfish"):
            out = desktop.redact(raw)
            self.assertIn("[redacted]", out, raw)
        self.assertNotIn("hunter2", desktop.redact("password = hunter2"))

    def test_jwts_and_bearer_headers_are_redacted(self):
        jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0."
               "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        self.assertEqual(desktop.redact(jwt), "[redacted-jwt]")
        self.assertEqual(desktop.redact("Authorization: Bearer abcdefgh12345"),
                         "Authorization: Bearer [redacted]")

    def test_plain_text_is_truncated_from_the_tail(self):
        out = desktop.truncate("x" * 400, 200)
        self.assertEqual(len(out), 200)
        self.assertTrue(out.endswith("\u2026"))

    def test_paths_are_truncated_from_the_head_so_the_filename_survives(self):
        path = "C:\\Users\\joshu\\" + ("deep\\" * 60) + "quarterly-report.xlsx"
        out = desktop.sanitize(path, 60)
        self.assertEqual(len(out), 60)
        self.assertTrue(out.startswith("\u2026"))
        self.assertTrue(out.endswith("quarterly-report.xlsx"), out)

    def test_a_label_with_one_slash_is_not_treated_as_a_path(self):
        out = desktop.sanitize("Save/Load " + "y" * 400, 40)
        self.assertFalse(out.startswith("\u2026"))
        self.assertTrue(out.startswith("Save/Load"))

    def test_the_pipeline_runs_on_every_element_name(self):
        back = FakeBackend(trees={"w:1": [
            el("1.1", name="token=deadbeefcafe\nsecond line")]})
        obs = build(back).screen_read("w:1")
        name = obs["elements"][0]["name"]
        self.assertNotIn("deadbeefcafe", name)
        self.assertNotIn("\n", name)
        self.assertIn("[redacted]", name)

    def test_window_titles_are_sanitized_too(self):
        back = FakeBackend(windows=[window(title="Vault  api_key=sk-9999")])
        out = build(back).screen_read("w:1")
        self.assertNotIn("sk-9999", out["window"]["title"])
        self.assertNotIn("sk-9999", out["text"])


# ------------------------------------------------------------ pure helpers ---

class HelperTests(unittest.TestCase):
    def test_lparam_packs_y_high_x_low(self):
        self.assertEqual(desktop.pack_lparam(10, 20), (20 << 16) | 10)
        # negative client coords happen on a window scrolled off-screen
        self.assertEqual(desktop.pack_lparam(-1, 0) & 0xFFFF, 0xFFFF)

    def test_right_button_uses_the_documented_messages(self):
        self.assertEqual(desktop.MOUSE_MESSAGES["left"], (0x0201, 0x0202))
        self.assertEqual(desktop.MOUSE_MESSAGES["right"], (0x0204, 0x0205))

    def test_parse_keys_reads_chords_and_refuses_nonsense(self):
        self.assertEqual(desktop.parse_keys("ctrl+shift+s"),
                         (["ctrl", "shift"], "s"))
        self.assertEqual(desktop.parse_keys(["alt", "f4"]), (["alt"], "f4"))
        self.assertEqual(desktop.parse_keys("enter"), ([], "enter"))
        with self.assertRaises(desktop.DesktopError):
            desktop.parse_keys("ctrl+nope")
        with self.assertRaises(desktop.DesktopError):
            desktop.parse_keys("s+ctrl")      # a modifier in the key position
        with self.assertRaises(desktop.DesktopError):
            desktop.parse_keys("")

    def test_the_vk_map_covers_the_documented_set(self):
        for name in ("enter", "tab", "esc", "backspace", "delete", "space",
                     "insert", "home", "end", "pgup", "pgdn", "up", "down",
                     "left", "right", "ctrl", "shift", "alt", "win"):
            self.assertIn(name, desktop.VK, name)
        self.assertEqual(desktop.VK["f1"], 0x70)
        self.assertEqual(desktop.VK["f12"], 0x7B)
        self.assertEqual(desktop.VK["0"], 0x30)
        self.assertEqual(desktop.VK["a"], 0x41)
        self.assertEqual(desktop.VK["z"], 0x5A)

    def test_a_command_chord_sends_no_character(self):
        self.assertIsNone(desktop.key_char("s", ["ctrl"]))
        self.assertEqual(desktop.key_char("s", []), "s")
        self.assertEqual(desktop.key_char("s", ["shift"]), "S")
        # shift+digit is layout-dependent; guessing types the wrong symbol
        self.assertIsNone(desktop.key_char("1", ["shift"]))


class ScrollDirectionTests(unittest.TestCase):
    """dsh-click's real bug: the two scroll paths disagreed on 'up'."""

    def test_the_absolute_mapping_is_right(self):
        self.assertEqual(desktop.scroll_amount("up"),
                         ("SmallDecrement", "vertical"))
        self.assertEqual(desktop.scroll_amount("down"),
                         ("SmallIncrement", "vertical"))
        # positive vertical wheel delta = rotated away from the user = up
        self.assertEqual(desktop.wheel_delta("up", 1), (120, False))
        self.assertEqual(desktop.wheel_delta("down", 1), (-120, False))
        # positive horizontal wheel delta = right
        self.assertEqual(desktop.wheel_delta("right", 1), (120, True))
        self.assertEqual(desktop.wheel_delta("left", 1), (-120, True))

    def test_notches_multiply_only_the_wheel_path(self):
        self.assertEqual(desktop.wheel_delta("down", 3), (-360, False))
        self.assertEqual(desktop.scroll_amount("down")[0], "SmallIncrement")

    def test_both_paths_read_ONE_table(self):
        """Flip the table and BOTH paths must flip. This is the anti-drift test.

        Asserting the two mappings separately would pass even if they were two
        hand-written tables that happened to agree today -- which is exactly
        the state dsh-click was in before they drifted.
        """
        original = desktop.SCROLL_DIRECTIONS["up"]
        try:
            desktop.SCROLL_DIRECTIONS["up"] = (1, "vertical")
            self.assertEqual(desktop.scroll_amount("up")[0], "SmallIncrement")
            self.assertEqual(desktop.wheel_delta("up", 1), (-120, False))
        finally:
            desktop.SCROLL_DIRECTIONS["up"] = original
        self.assertEqual(desktop.scroll_amount("up")[0], "SmallDecrement")
        self.assertEqual(desktop.wheel_delta("up", 1), (120, False))

    def test_an_unknown_direction_is_a_hard_error_on_both_paths(self):
        for fn in (desktop.scroll_amount, desktop.wheel_delta):
            with self.assertRaises(desktop.DesktopError):
                fn("sideways")


class GeometryTests(unittest.TestCase):
    def test_rect_helpers(self):
        a = {"x": 0, "y": 0, "width": 10, "height": 10}
        b = {"x": 5, "y": 5, "width": 10, "height": 10}
        c = {"x": 20, "y": 20, "width": 5, "height": 5}
        self.assertTrue(desktop.rects_intersect(a, b))
        self.assertFalse(desktop.rects_intersect(a, c))
        self.assertFalse(desktop.rects_intersect(a, None))
        self.assertTrue(desktop.point_in_rect(9, 9, a))
        self.assertFalse(desktop.point_in_rect(10, 10, a))
        self.assertEqual(desktop.rect_centre(a), (5, 5))


# -------------------------------------------------------------- observation --

class ObservationTests(unittest.TestCase):
    def test_screen_read_returns_a_citable_observation(self):
        out = build().screen_read("w:1")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["observation_id"]),
                         desktop.OBSERVATION_ID_LEN)
        self.assertEqual(out["window_id"], "w:1")
        self.assertIn("observation_id", out["text"])
        self.assertIn(out["observation_id"], out["text"])

    def test_the_id_covers_the_tree_AND_the_pixels(self):
        back = FakeBackend()
        d = build(back)
        first = d.screen_read("w:1")["observation_id"]
        self.assertEqual(d.screen_read("w:1")["observation_id"], first)
        back.pixels["w:1"] = b"different"     # same tree, different frame
        self.assertNotEqual(d.screen_read("w:1")["observation_id"], first)
        back.pixels["w:1"] = b"pixels-w:1"
        back.trees["w:1"] = [el("1.1", name="Cancel")]
        self.assertNotEqual(d.screen_read("w:1")["observation_id"], first)

    def test_screen_shot_and_screen_read_agree_on_the_id(self):
        d = build()
        self.assertEqual(d.screen_shot("w:1")["observation_id"],
                         d.screen_read("w:1")["observation_id"])

    def test_a_blank_frame_says_so_instead_of_returning_black(self):
        back = FakeBackend()
        back.blank = True
        out = build(back).screen_shot("w:1")
        self.assertTrue(out["blank"])
        self.assertIsNone(out["image_png"])
        self.assertIn("blank", out["note"].lower())
        self.assertIn("screen_read", out["note"])

    def test_a_capture_failure_is_reported_not_fatal(self):
        back = FakeBackend()
        back.capture_error = "PrintWindow said no"
        out = build(back).screen_shot("w:1")
        self.assertTrue(out["ok"])
        self.assertTrue(out["blank"])
        self.assertIn("PrintWindow said no", out["note"])

    def test_a_missing_window_is_a_clear_error(self):
        with self.assertRaises(desktop.DesktopError) as caught:
            build().screen_read("w:404")
        self.assertIn("app_list", str(caught.exception))

    def test_the_text_render_is_one_line_per_element(self):
        back = FakeBackend(trees={"w:1": [
            el("1.1", "Button", "Save", (10, 20, 80, 24), ("invoke",)),
            el("1.2", "Edit", "Name", (10, 60, 200, 24), ("value",),
               enabled=False)]})
        text = build(back).screen_read("w:1")["text"]
        self.assertIn('- [1.1] Button "Save" at (10, 20) 80x24 patterns: invoke',
                      text)
        self.assertIn('- [1.2] Edit "Name" at (10, 60) 200x24 (disabled) '
                      'patterns: value', text)


class PruningTests(unittest.TestCase):
    def test_truncation_announces_itself_and_says_how_many_were_seen(self):
        rows = [el(f"1.{i}", "Button", f"B{i}", (0, i, 20, 10)) for i in range(10)]
        back = FakeBackend(trees={"w:1": rows})
        out = build(back, max_elements=3).screen_read("w:1")
        self.assertTrue(out["truncated"])
        self.assertEqual(out["kept"], 3)
        self.assertEqual(out["seen"], 10)
        self.assertIn("3", out["note"])
        self.assertIn("10", out["note"])
        self.assertIn("TRUNCATED", out["text"])

    def test_ranking_keeps_the_actionable_ones_over_document_order(self):
        """DIVERGENCE from dsh-click's flat DFS truncate.

        Document order on a Chromium window hands back 500 nodes of
        scaffolding and not one button.
        """
        rows = [el(f"1.s{i}", "Pane", f"pane {i}", (0, i, 100, 10), ())
                for i in range(6)]
        rows.append(el("1.btn", "Button", "Save", (10, 10, 80, 24),
                       ("invoke",)))
        back = FakeBackend(trees={"w:1": rows})
        out = build(back, max_elements=3).screen_read("w:1")
        ids = [e["element_id"] for e in out["elements"]]
        self.assertIn("1.btn", ids, "the only actionable element was truncated")

    def test_survivors_are_presented_in_document_order(self):
        rows = [el("1.a", "Pane", "top", (0, 0, 100, 10), ()),
                el("1.b", "Button", "Save", (10, 10, 80, 24), ("invoke",)),
                el("1.c", "Text", "hint", (10, 40, 80, 10), ())]
        back = FakeBackend(trees={"w:1": rows})
        out = build(back).screen_read("w:1")
        self.assertEqual([e["element_id"] for e in out["elements"]],
                         ["1.a", "1.b", "1.c"])

    def test_nameless_patternless_containers_are_dropped(self):
        rows = [el("1.a", "Pane", "", (0, 0, 100, 10), ()),
                el("1.b", "Group", "", (0, 0, 100, 10), ()),
                el("1.c", "Button", "Save", (10, 10, 80, 24), ("invoke",))]
        out = build(FakeBackend(trees={"w:1": rows})).screen_read("w:1")
        self.assertEqual([e["element_id"] for e in out["elements"]], ["1.c"])

    def test_the_visit_cap_announces_itself(self):
        back = FakeBackend()
        back.hit_visit_cap = True
        out = build(back).screen_read("w:1")
        self.assertIn("visit cap", out["note"])


# ------------------------------------------------------- the five refusals ---

class StalenessTests(unittest.TestCase):
    def setUp(self):
        self.back = FakeBackend(trees={"w:1": [
            el("1.1", "Button", "Save", (10, 10, 80, 24), ("invoke",))]})
        self.clock = Clock()
        self.d = build(self.back, clock=self.clock)
        self.obs = self.d.screen_read("w:1")
        self.cite = {"observation_id": self.obs["observation_id"],
                     "window_id": "w:1"}

    def act(self):
        return self.d.click(self.cite, element_id="1.1")

    def test_a_clean_citation_acts(self):
        out = self.act()
        self.assertTrue(out["ok"])
        self.assertEqual(out["delivered"], "uia")

    def test_unknown_observation(self):
        out = self.d.click({"observation_id": "n" * 32, "window_id": "w:1"},
                           element_id="1.1")
        self.assertEqual(out["refusal"], desktop.UNKNOWN_OBSERVATION)
        self.assertEqual(out["delivered"], "none")
        self.assertTrue(out["message"].endswith(
            "run screen_read again before acting."))

    def test_unknown_observation_on_a_window_id_mismatch(self):
        self.back.windows["w:2"] = window(window_id="w:2", title="Other")
        self.back.trees["w:2"] = [el("2.1")]
        self.back.pixels["w:2"] = b"px2"
        out = self.d.click({"observation_id": self.obs["observation_id"],
                            "window_id": "w:2"}, element_id="2.1")
        self.assertEqual(out["refusal"], desktop.UNKNOWN_OBSERVATION)
        self.assertIn("w:1", out["message"])

    def test_based_on_without_both_halves_is_refused(self):
        for cite in ({}, {"observation_id": "x"}, {"window_id": "w:1"}):
            out = self.d.click(cite, element_id="1.1")
            self.assertEqual(out["refusal"], desktop.UNKNOWN_OBSERVATION)

    def test_expired(self):
        self.clock.advance(desktop.OBSERVATION_TTL + 1)
        out = self.act()
        self.assertEqual(out["refusal"], desktop.EXPIRED)
        self.assertIn("31s", out["message"])
        self.assertTrue(out["message"].endswith(
            "run screen_read again before acting."))

    def test_expiry_is_a_boundary_not_a_guess(self):
        self.clock.advance(desktop.OBSERVATION_TTL - 0.5)
        self.assertTrue(self.act()["ok"])

    def test_stale_identity(self):
        self.back.windows["w:1"]["title"] = "Notepad *"
        out = self.act()
        self.assertEqual(out["refusal"], desktop.STALE_IDENTITY)
        self.assertTrue(out["message"].endswith(
            "run screen_read again before acting."))

    def test_stale_identity_on_a_moved_window(self):
        self.back.windows["w:1"]["rect"] = {"x": 40, "y": 0, "width": 800,
                                            "height": 600}
        self.assertEqual(self.act()["refusal"], desktop.STALE_IDENTITY)

    def test_stale_identity_on_a_recycled_handle(self):
        # same hwnd, different program: the classic handle-reuse trap
        self.back.windows["w:1"]["pid"] = 9999
        self.back.windows["w:1"]["exe"] = r"C:\W\other.exe"
        self.assertEqual(self.act()["refusal"], desktop.STALE_IDENTITY)

    def test_stale_tree(self):
        self.back.trees["w:1"] = [
            el("1.1", "Button", "Delete", (10, 10, 80, 24), ("invoke",))]
        out = self.act()
        self.assertEqual(out["refusal"], desktop.STALE_TREE)
        self.assertTrue(out["message"].endswith(
            "run screen_read again before acting."))

    def test_stale_tree_catches_a_moved_element(self):
        self.back.trees["w:1"] = [
            el("1.1", "Button", "Save", (10, 400, 80, 24), ("invoke",))]
        self.assertEqual(self.act()["refusal"], desktop.STALE_TREE)

    def test_stale_pixels_only_when_asked_for(self):
        """The default is OFF on purpose, and that default is the design.

        A raw-pixel hash refuses on a caret, a spinner, a clock -- i.e. on
        healthy behaviour. Alloy already learned once (the duration-based turn
        watchdog) that a check firing on normal operation gets routed around
        rather than obeyed.
        """
        self.back.pixels["w:1"] = b"one blinking caret later"
        self.assertFalse(self.d.strict_pixels)
        self.assertTrue(self.act()["ok"], "the default refused on a caret")

    def test_stale_pixels(self):
        strict = desktop.Desktop(backend=self.back, clock=self.clock,
                                 self_pids=set(), strict_pixels=True)
        obs = strict.screen_read("w:1")
        cite = {"observation_id": obs["observation_id"], "window_id": "w:1"}
        self.assertTrue(strict.click(cite, element_id="1.1")["ok"])
        obs = strict.screen_read("w:1")
        cite = {"observation_id": obs["observation_id"], "window_id": "w:1"}
        self.back.pixels["w:1"] = b"repainted"
        out = strict.click(cite, element_id="1.1")
        self.assertEqual(out["refusal"], desktop.STALE_PIXELS)
        self.assertTrue(out["message"].endswith(
            "run screen_read again before acting."))

    def test_per_call_strict_pixels_overrides_the_instance_default(self):
        self.back.pixels["w:1"] = b"repainted"
        out = self.d.click(self.cite, element_id="1.1", strict_pixels=True)
        self.assertEqual(out["refusal"], desktop.STALE_PIXELS)

    def test_every_refusal_code_is_one_of_the_five(self):
        cases = [
            ({"observation_id": "z" * 32, "window_id": "w:1"}, None),
        ]
        for cite, _ in cases:
            self.assertIn(self.d.click(cite, element_id="1.1")["refusal"],
                          desktop.STALENESS_CODES)

    def test_acting_consumes_the_observation(self):
        """The window just changed, so the snapshot that described it is a lie."""
        self.assertTrue(self.act()["ok"])
        again = self.act()
        self.assertEqual(again["refusal"], desktop.UNKNOWN_OBSERVATION)


class LruTests(unittest.TestCase):
    def test_the_oldest_observation_falls_out_at_nine(self):
        wins, trees, back = [], {}, None
        for i in range(desktop.OBSERVATION_MAX + 1):
            wid = f"w:{i}"
            wins.append(window(window_id=wid, title=f"Win {i}"))
            trees[wid] = [el(f"{i}.1", name=f"Save {i}")]
        back = FakeBackend(windows=wins, trees=trees)
        d = build(back)
        ids = [d.screen_read(f"w:{i}")["observation_id"]
               for i in range(desktop.OBSERVATION_MAX + 1)]
        self.assertEqual(len(set(ids)), desktop.OBSERVATION_MAX + 1)
        evicted = d.click({"observation_id": ids[0], "window_id": "w:0"},
                          element_id="0.1")
        self.assertEqual(evicted["refusal"], desktop.UNKNOWN_OBSERVATION)
        self.assertIn(str(desktop.OBSERVATION_MAX), evicted["message"])
        newest = d.click({"observation_id": ids[-1],
                          "window_id": f"w:{desktop.OBSERVATION_MAX}"},
                         element_id=f"{desktop.OBSERVATION_MAX}.1")
        self.assertTrue(newest["ok"])

    def test_citing_an_observation_keeps_it_alive(self):
        """A refused citation still refreshes the LRU: the model is clearly
        still working on that window, and evicting it mid-retry is the worst
        possible moment."""
        wins, trees = [], {}
        for i in range(desktop.OBSERVATION_MAX + 1):
            wid = f"w:{i}"
            wins.append(window(window_id=wid, title=f"Win {i}"))
            trees[wid] = [el(f"{i}.1", name=f"Save {i}")]
        back = FakeBackend(windows=wins, trees=trees)
        clock = Clock()
        d = build(back, clock=clock)
        ids = [d.screen_read(f"w:{i}")["observation_id"]
               for i in range(desktop.OBSERVATION_MAX)]
        # touch the oldest, then add one more -> the SECOND oldest is evicted
        clock.advance(desktop.OBSERVATION_TTL + 1)
        self.assertEqual(
            d.click({"observation_id": ids[0], "window_id": "w:0"},
                    element_id="0.1")["refusal"], desktop.EXPIRED)
        d.screen_read(f"w:{desktop.OBSERVATION_MAX}")
        self.assertEqual(
            d.click({"observation_id": ids[0], "window_id": "w:0"},
                    element_id="0.1")["refusal"], desktop.EXPIRED)
        self.assertEqual(
            d.click({"observation_id": ids[1], "window_id": "w:1"},
                    element_id="1.1")["refusal"], desktop.UNKNOWN_OBSERVATION)


# ----------------------------------------------------------------- clicking --

class ClickTests(unittest.TestCase):
    def setUp(self):
        self.back = FakeBackend(trees={"w:1": [
            el("1.inv", "Button", "Save", (10, 10, 80, 24), ("invoke",)),
            el("1.raw", "Custom", "Canvas", (100, 100, 200, 200), ())]})
        self.d = build(self.back)
        self.cite = self._cite()

    def _cite(self):
        obs = self.d.screen_read("w:1")
        return {"observation_id": obs["observation_id"], "window_id": "w:1"}

    def test_invoke_pattern_wins_and_says_uia(self):
        out = self.d.click(self.cite, element_id="1.inv")
        self.assertEqual(out["delivered"], "uia")
        self.assertIn(("invoke", "1.inv"), self.back.calls)
        self.assertFalse(kinds(self.back, "post_click"))

    def test_element_is_RE_RESOLVED_by_id_never_by_index(self):
        """dsh-click indexes into the cached list; the tree is not a snapshot."""
        self.d.click(self.cite, element_id="1.inv")
        self.assertIn(("resolve", "w:1", "1.inv"), self.back.calls)

    def test_a_vanished_element_refuses_instead_of_clicking_its_neighbour(self):
        self.back.missing.add("1.inv")
        out = self.d.click(self.cite, element_id="1.inv")
        self.assertEqual(out["refusal"], desktop.ELEMENT_GONE)
        self.assertTrue(out["message"].endswith(
            "run screen_read again before acting."))
        self.assertFalse(kinds(self.back, "post_click"))

    def test_an_element_not_in_the_fresh_tree_refuses(self):
        out = self.d.click(self.cite, element_id="1.ghost")
        self.assertEqual(out["refusal"], desktop.ELEMENT_GONE)

    def test_no_invoke_pattern_falls_back_to_a_posted_click(self):
        out = self.d.click(self.cite, element_id="1.raw")
        self.assertEqual(out["delivered"], "posted")
        self.assertIn(("post_click", "w:1", 200, 200, "left"), self.back.calls)
        self.assertIn("best-effort", out["note"])

    def test_a_throwing_invoke_falls_through_to_posting(self):
        def boom(handle):
            raise RuntimeError("provider said no")
        self.back.invoke = boom
        out = self.d.click(self.cite, element_id="1.inv")
        self.assertEqual(out["delivered"], "posted")

    def test_a_refusing_invoke_falls_through_to_posting(self):
        self.back.invoke_ok = False
        self.assertEqual(self.d.click(self.cite, element_id="1.inv")["delivered"],
                         "posted")

    def test_the_right_button_is_carried_through(self):
        out = self.d.click(self.cite, element_id="1.raw", button="right")
        self.assertIn(("post_click", "w:1", 200, 200, "right"), self.back.calls)
        self.assertEqual(out["button"], "right")

    def test_an_unknown_button_is_a_hard_error(self):
        with self.assertRaises(desktop.DesktopError):
            self.d.click(self.cite, element_id="1.raw", button="middle")

    def test_a_coordinate_inside_the_window_is_allowed(self):
        out = self.d.click(self.cite, x=400, y=300)
        self.assertEqual(out["delivered"], "posted")
        self.assertIn(("post_click", "w:1", 400, 300, "left"), self.back.calls)

    def test_a_coordinate_OUTSIDE_the_window_RAISES(self):
        """There is no desktop-wide click primitive, on purpose."""
        for point in ((900, 300), (-5, 5), (400, 900)):
            with self.assertRaises(desktop.DesktopRefused) as caught:
                self.d.click(self.cite, x=point[0], y=point[1])
            self.assertEqual(caught.exception.code, "OUT_OF_WINDOW")
            self.assertIn("outside window", str(caught.exception))
        self.assertFalse(kinds(self.back, "post_click"))

    def test_a_click_with_neither_element_nor_point_is_a_hard_error(self):
        with self.assertRaises(desktop.DesktopError):
            self.d.click(self.cite)


class ProcessIdentityTests(unittest.TestCase):
    """dsh-click reads before and after back-to-back AFTER the action ran."""

    def setUp(self):
        self.back = FakeBackend(trees={"w:1": [
            el("1.inv", "Button", "Save", (10, 10, 80, 24), ("invoke",))]})
        self.d = build(self.back)

    def _cite(self):
        obs = self.d.screen_read("w:1")
        return {"observation_id": obs["observation_id"], "window_id": "w:1"}

    def test_the_check_BRACKETS_the_action(self):
        cite = self._cite()
        self.back.calls = []
        self.d.click(cite, element_id="1.inv")
        order = [row[0] for row in
                 kinds(self.back, "identity", "invoke", "post_click")]
        self.assertEqual(order, ["identity", "invoke", "identity"])

    def test_a_process_swap_during_the_action_is_reported(self):
        cite = self._cite()
        self.back.identities["w:1"] = [(4242, r"C:\W\np.exe"),
                                       (7777, r"C:\W\evil.exe")]
        out = self.d.click(cite, element_id="1.inv")
        self.assertTrue(out["ok"])
        self.assertFalse(out["process_stable"])
        self.assertIn("changed process", out["warning"])
        self.assertIn("7777", out["warning"])

    def test_a_stable_process_says_so(self):
        out = self.d.click(self._cite(), element_id="1.inv")
        self.assertTrue(out["process_stable"])
        self.assertNotIn("warning", out)

    def test_the_after_read_happens_even_when_the_action_throws(self):
        cite = self._cite()

        def boom(*_a):
            raise RuntimeError("post failed")
        self.back.post_click = boom
        self.back.invoke_ok = False
        self.back.calls = []
        with self.assertRaises(RuntimeError):
            self.d.click(cite, element_id="1.inv")
        self.assertEqual(len(kinds(self.back, "identity")), 2)


# ------------------------------------------------------------------ typing ---

class TypeTests(unittest.TestCase):
    def setUp(self):
        self.back = FakeBackend(trees={"w:1": [
            el("1.edit", "Edit", "Name", (10, 10, 200, 24), ("value",)),
            el("1.pw", "Edit", "Password", (10, 50, 200, 24), ("value",),
               is_password=True),
            el("1.btn", "Button", "Save", (10, 90, 80, 24), ("invoke",))]})
        self.back.values["1.edit"] = "old text"
        self.d = build(self.back)

    def _cite(self):
        obs = self.d.screen_read("w:1")
        return {"observation_id": obs["observation_id"], "window_id": "w:1"}

    def test_a_plain_set_value_works(self):
        out = self.d.type_text(self._cite(), "1.edit", "hello")
        self.assertTrue(out["ok"])
        self.assertEqual(out["delivered"], "uia")
        self.assertEqual(self.back.values["1.edit"], "hello")

    def test_no_value_pattern_REFUSES_instead_of_focusing_something(self):
        """Focusing and typing blind is how a password lands in a chat window."""
        out = self.d.type_text(self._cite(), "1.btn", "hello")
        self.assertEqual(out["refusal"], desktop.NO_VALUE_PATTERN)
        self.assertIn("value", out["message"])
        self.assertFalse(kinds(self.back, "post_key", "set_value"))

    def test_a_password_field_is_refused_by_default(self):
        out = self.d.type_text(self._cite(), "1.pw", "hunter2")
        self.assertEqual(out["refusal"], desktop.PASSWORD_FIELD)
        self.assertIn("transcript", out["message"])
        self.assertIn("allow_password", out["message"])
        self.assertFalse(kinds(self.back, "set_value"))

    def test_the_password_refusal_is_overridable(self):
        out = self.d.type_text(self._cite(), "1.pw", "hunter2",
                               allow_password=True)
        self.assertTrue(out["ok"])
        self.assertEqual(self.back.values["1.pw"], "hunter2")

    def test_a_vanished_field_refuses(self):
        self.back.missing.add("1.edit")
        out = self.d.type_text(self._cite(), "1.edit", "hello")
        self.assertEqual(out["refusal"], desktop.ELEMENT_GONE)

    def test_type_text_without_an_element_is_a_hard_error(self):
        with self.assertRaises(desktop.DesktopError):
            self.d.type_text(self._cite(), None, "hello")

    def test_a_failure_ROLLS_BACK_and_says_the_restore_worked(self):
        cite = self._cite()
        self.back.set_value_error = "field rejected it"
        with self.assertRaises(desktop.DesktopError) as caught:
            self.d.type_text(cite, "1.edit", "hello")
        message = str(caught.exception)
        self.assertIn("field rejected it", message)
        self.assertIn("original value was restored", message)
        self.assertNotIn("NOT restored", message)
        self.assertEqual(self.back.values["1.edit"], "old text")

    def test_a_FAILED_restore_says_NOT_restored_in_those_words(self):
        cite = self._cite()
        self.back.set_value_error = "field rejected it"
        self.back.restore_error = "and refused the restore too"
        with self.assertRaises(desktop.DesktopError) as caught:
            self.d.type_text(cite, "1.edit", "hello")
        message = str(caught.exception)
        self.assertIn("NOT restored", message)
        self.assertIn("and refused the restore too", message)
        self.assertIn("partial text", message)

    def test_an_unreadable_original_says_no_restore_was_attempted(self):
        cite = self._cite()
        del self.back.values["1.edit"]        # get_value returns None
        self.back.set_value_error = "field rejected it"
        with self.assertRaises(desktop.DesktopError) as caught:
            self.d.type_text(cite, "1.edit", "hello")
        message = str(caught.exception)
        self.assertIn("NO restore was attempted", message)
        self.assertIn("partial text", message)

    def test_typing_still_requires_a_fresh_observation(self):
        cite = self._cite()
        self.back.windows["w:1"]["title"] = "Name *"
        out = self.d.type_text(cite, "1.edit", "hello")
        self.assertEqual(out["refusal"], desktop.STALE_IDENTITY)
        self.assertEqual(self.back.values["1.edit"], "old text")


# ---------------------------------------------------------------- scrolling --

class ScrollTests(unittest.TestCase):
    def setUp(self):
        self.back = FakeBackend(trees={"w:1": [
            el("1.list", "List", "Items", (0, 0, 400, 400), ("scroll",)),
            el("1.plain", "Text", "Body", (0, 0, 400, 400), ())]})
        self.d = build(self.back)

    def _cite(self):
        obs = self.d.screen_read("w:1")
        return {"observation_id": obs["observation_id"], "window_id": "w:1"}

    def test_scroll_pattern_wins(self):
        out = self.d.scroll(self._cite(), "up", element_id="1.list")
        self.assertEqual(out["delivered"], "uia")
        self.assertIn(("scroll_pattern", "1.list", "SmallDecrement",
                       "vertical"), self.back.calls)

    def test_a_patternless_element_posts_the_wheel_the_SAME_way(self):
        out = self.d.scroll(self._cite(), "up", element_id="1.plain")
        self.assertEqual(out["delivered"], "posted")
        posted = kinds(self.back, "post_wheel")[0]
        self.assertEqual(posted[4], 120)      # positive = up, same as above
        self.assertFalse(posted[5])           # not horizontal

    def test_down_agrees_across_both_paths_too(self):
        cite = self._cite()
        self.d.scroll(cite, "down", element_id="1.list")
        self.assertIn(("scroll_pattern", "1.list", "SmallIncrement",
                       "vertical"), self.back.calls)
        self.back.calls = []
        self.d.scroll(self._cite(), "down", element_id="1.plain")
        self.assertEqual(kinds(self.back, "post_wheel")[0][4], -120)

    def test_horizontal_scrolling_uses_the_horizontal_wheel(self):
        self.d.scroll(self._cite(), "right", element_id="1.plain")
        posted = kinds(self.back, "post_wheel")[0]
        self.assertEqual(posted[4], 120)
        self.assertTrue(posted[5])

    def test_notches_become_that_many_wheel_messages(self):
        self.d.scroll(self._cite(), "down", element_id="1.plain", notches=3)
        posted = kinds(self.back, "post_wheel")
        self.assertEqual(len(posted), 3)
        self.assertTrue(all(row[4] == -120 for row in posted))

    def test_with_no_element_the_wheel_goes_to_the_window_centre(self):
        self.d.scroll(self._cite(), "down")
        posted = kinds(self.back, "post_wheel")[0]
        self.assertEqual((posted[2], posted[3]), (400, 300))

    def test_a_failing_scroll_pattern_falls_through_to_the_wheel(self):
        self.back.scroll_ok = False
        out = self.d.scroll(self._cite(), "up", element_id="1.list")
        self.assertEqual(out["delivered"], "posted")

    def test_scrolling_still_requires_a_fresh_observation(self):
        cite = self._cite()
        self.back.trees["w:1"] = [el("1.list", "List", "Items",
                                     (0, 0, 400, 500), ("scroll",))]
        self.assertEqual(self.d.scroll(cite, "up")["refusal"],
                         desktop.STALE_TREE)


# --------------------------------------------------------------------- keys --

class KeyTests(unittest.TestCase):
    def setUp(self):
        self.back = FakeBackend()
        self.d = build(self.back)

    def _cite(self):
        obs = self.d.screen_read("w:1")
        self.back.calls = []
        return {"observation_id": obs["observation_id"], "window_id": "w:1"}

    def test_a_bare_key_is_down_char_up(self):
        out = self.d.key(self._cite(), "enter")
        self.assertEqual(out["delivered"], "posted")
        self.assertEqual(
            [(row[2], row[3], row[4]) for row in kinds(self.back, "post_key")],
            [("down", 0x0D, None), ("char", 0x0D, "\r"), ("up", 0x0D, None)])

    def test_modifiers_go_down_in_order_and_up_in_REVERSE(self):
        self.d.key(self._cite(), "ctrl+shift+s")
        seq = [(row[2], row[3]) for row in kinds(self.back, "post_key")]
        self.assertEqual(seq, [
            ("down", desktop.VK["ctrl"]), ("down", desktop.VK["shift"]),
            ("down", desktop.VK["s"]), ("up", desktop.VK["s"]),
            ("up", desktop.VK["shift"]), ("up", desktop.VK["ctrl"])])

    def test_a_command_chord_sends_no_character(self):
        self.d.key(self._cite(), "ctrl+s")
        self.assertFalse([row for row in kinds(self.back, "post_key")
                          if row[2] == "char"])

    def test_shift_alone_still_types_a_character(self):
        self.d.key(self._cite(), "shift+a")
        chars = [row[4] for row in kinds(self.back, "post_key")
                 if row[2] == "char"]
        self.assertEqual(chars, ["A"])

    def test_an_unknown_key_is_a_hard_error_before_anything_is_posted(self):
        cite = self._cite()
        with self.assertRaises(desktop.DesktopError):
            self.d.key(cite, "ctrl+banana")
        self.assertFalse(kinds(self.back, "post_key"))

    def test_keys_still_require_a_fresh_observation(self):
        cite = self._cite()
        self.back.windows["w:1"]["title"] = "changed"
        out = self.d.key(cite, "enter")
        self.assertEqual(out["refusal"], desktop.STALE_IDENTITY)
        self.assertFalse(kinds(self.back, "post_key"))

    def test_the_note_never_claims_the_key_arrived(self):
        out = self.d.key(self._cite(), "enter")
        self.assertIn("best-effort", out["note"])


# ------------------------------------------------------- the refusal policy --

class SelfApprovalTests(unittest.TestCase):
    """Absolute and non-overridable: there is no parameter to test here."""

    def _desktop(self, win, **kwargs):
        back = FakeBackend(windows=[win], trees={win["window_id"]: [el("1.1")]})
        back.pixels[win["window_id"]] = b"px"
        return desktop.Desktop(backend=back, self_pids={999}, **kwargs), back

    def test_a_webview2_class_is_refused(self):
        d, _ = self._desktop(window(cls="Chrome_WidgetWin_1", pid=1234,
                                    exe=r"C:\W\msedgewebview2.exe"))
        with self.assertRaises(desktop.DesktopRefused) as caught:
            d.screen_read("w:1")
        self.assertEqual(caught.exception.code, "SELF_APPROVAL")
        self.assertIn("forgeable", str(caught.exception))
        self.assertIn("no override", str(caught.exception))

    def test_alloys_own_process_tree_is_refused(self):
        d, _ = self._desktop(window(pid=999, cls="Notepad",
                                    exe=r"C:\W\python.exe"))
        with self.assertRaises(desktop.DesktopRefused) as caught:
            d.screen_read("w:1")
        self.assertIn("Alloy", str(caught.exception))

    def test_a_seat_cli_console_is_refused(self):
        for exe in (r"C:\W\node.exe", r"C:\W\powershell.exe",
                    r"C:\W\WindowsTerminal.exe", r"C:\W\claude.exe"):
            d, _ = self._desktop(window(pid=1234, cls="ConsoleWindowClass",
                                        exe=exe))
            with self.assertRaises(desktop.DesktopRefused) as caught:
                d.screen_read("w:1")
            self.assertIn("shell", str(caught.exception).lower())

    def test_the_refusal_also_covers_the_OBSERVERS(self):
        """Reading Alloy's approval modal is how a seat learns to forge it."""
        d, _ = self._desktop(window(cls="Chrome_WidgetWin_1"))
        for call in (lambda: d.screen_read("w:1"),
                     lambda: d.screen_shot("w:1")):
            with self.assertRaises(desktop.DesktopRefused):
                call()

    def test_a_mutator_on_a_forbidden_window_cannot_slip_past_verify(self):
        allowed = window(window_id="w:1")
        back = FakeBackend(windows=[allowed], trees={"w:1": [el("1.1")]})
        d = desktop.Desktop(backend=back, self_pids=set())
        obs = d.screen_read("w:1")
        back.windows["w:1"]["class_name"] = "Chrome_WidgetWin_1"
        with self.assertRaises(desktop.DesktopRefused):
            d.click({"observation_id": obs["observation_id"],
                     "window_id": "w:1"}, element_id="1.1")

    def test_the_deny_list_is_configurable_and_says_how_to_undo_it(self):
        d, _ = self._desktop(window(title="1Password \u2014 Vault"),
                             deny_windows=["1password"])
        with self.assertRaises(desktop.DesktopRefused) as caught:
            d.screen_read("w:1")
        self.assertIn("deny-list", str(caught.exception))
        self.assertIn("Remove the entry", str(caught.exception))

    def test_an_ordinary_window_is_not_refused(self):
        d, _ = self._desktop(window())
        self.assertTrue(d.screen_read("w:1")["ok"])


class AppListTests(unittest.TestCase):
    def test_refused_windows_are_LISTED_with_the_reason(self):
        """Hiding them makes a model hunt; 'you may not' is a better answer."""
        back = FakeBackend(windows=[
            window(window_id="w:1", title="Notepad"),
            window(window_id="w:2", title="Alloy", cls="Chrome_WidgetWin_1")])
        out = build(back).app_list()
        by_id = {w["window_id"]: w for w in out["windows"]}
        self.assertTrue(by_id["w:1"]["controllable"])
        self.assertFalse(by_id["w:2"]["controllable"])
        self.assertIn("forgeable", by_id["w:2"]["refusal"])
        self.assertIn("REFUSED", out["text"])
        self.assertIn("screen_read", out["text"])

    def test_titles_and_paths_are_sanitized_in_the_listing(self):
        back = FakeBackend(windows=[window(title="tok\ten=deadbeef\nsecond")])
        out = build(back).app_list()
        self.assertNotIn("\n", out["windows"][0]["title"])


# ------------------------------------------------------------------- probe ---

class ProbeTests(unittest.TestCase):
    def test_a_non_windows_box_gets_a_clean_verdict_by_name(self):
        for system in ("Darwin", "Linux"):
            info = desktop.probe(system=system)
            self.assertFalse(info["available"])
            self.assertEqual(info["system"], system)
            self.assertIn("Windows", info["reason"])
            self.assertIn("out of scope", info["reason"])
        # and the two out-of-scope trees are named, not hand-waved
        self.assertIn("AXUIElement", desktop.probe(system="Darwin")["reason"])
        self.assertIn("AT-SPI", desktop.probe(system="Linux")["reason"])

    def test_a_missing_piece_is_named_one_at_a_time(self):
        real = desktop._has
        try:
            for missing, needle in (("win32gui", "pywin32"), ("clr", "pythonnet"),
                                    ("PIL", "Pillow"), ("psutil", "psutil")):
                desktop._has = lambda m, bad=missing: m != bad
                info = desktop.probe(system="Windows")
                self.assertFalse(info["available"], needle)
                self.assertIn(needle, info["reason"])
                self.assertIn("pip install", info["reason"])
        finally:
            desktop._has = real

    def test_an_already_imported_module_counts_as_present(self):
        """RED guard for a real bug: probe() called AFTER the first real use.

        pythonnet swaps its own `clr` module for one whose __spec__ is None, so
        importlib.util.find_spec("clr") RAISES from that moment on and a
        find_spec-only check reported "pythonnet is missing" in the very
        process that had just walked a window with it.
        """
        import sys
        import types
        fake = types.ModuleType("alloy_probe_fixture")
        fake.__spec__ = None                  # exactly pythonnet's shape
        sys.modules["alloy_probe_fixture"] = fake
        try:
            self.assertTrue(desktop._has("alloy_probe_fixture"))
        finally:
            del sys.modules["alloy_probe_fixture"]
        self.assertFalse(desktop._has("alloy_probe_fixture"))

    def test_probe_agrees_with_itself_after_loading_the_assemblies(self):
        """The end-to-end form of the bug above, and the only slow test here.

        On Windows the first probe IMPORTS clr (to check the assemblies really
        load), which is what poisoned find_spec for every later call. Whatever
        this machine can do, it must still say the same thing the second time.
        """
        first = desktop.probe()
        self.assertEqual(desktop.probe()["available"], first["available"])
        self.assertEqual(desktop.probe()["pieces"], first["pieces"])

    def test_a_backend_that_cannot_load_the_assemblies_says_so(self):
        class Dead(desktop.Backend):
            def available(self):
                return False, "The .NET UIAutomation assemblies were not found."
        info = desktop.probe(system="Windows", backend=Dead())
        self.assertFalse(info["available"])
        self.assertFalse(info["pieces"]["UIAutomation"])
        self.assertIn("UIAutomation", info["reason"])

    def test_a_throwing_backend_is_reported_not_raised(self):
        class Boom(desktop.Backend):
            def available(self):
                raise OSError("clr exploded")
        info = desktop.probe(system="Windows", backend=Boom())
        self.assertFalse(info["available"])
        self.assertIn("clr exploded", info["reason"])

    def test_a_healthy_backend_reports_available(self):
        class Fine(desktop.Backend):
            def available(self):
                return True, ""
        info = desktop.probe(system="Windows", backend=Fine())
        self.assertTrue(info["available"])
        self.assertIn("cursor never moves", info["reason"])


class ImportHygieneTests(unittest.TestCase):
    def test_the_module_imports_nothing_from_relay_or_app(self):
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "desktop.py"), encoding="utf-8").read()
        for banned in ("import relay", "import app", "import webview",
                       "from relay", "from app "):
            self.assertNotIn(banned, source, banned)

    def test_no_foreground_or_cursor_api_is_used_anywhere(self):
        """The one promise the whole design rests on: Josh keeps his mouse."""
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "desktop.py"), encoding="utf-8").read()
        for banned in ("SendInput", "mouse_event", "SetCursorPos",
                       "SetForegroundWindow", "keybd_event", "BringWindowToTop",
                       "SetActiveWindow", "SetFocus"):
            self.assertNotIn(banned + "(", source, banned)

    def test_the_uia_assemblies_are_not_loaded_at_import_time(self):
        # a cheap import is what keeps `import desktop` safe on any box
        source = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "desktop.py"), encoding="utf-8").read()
        head = source.split("class Backend", 1)[0]
        self.assertNotIn("\nimport clr", head)
        self.assertNotIn("\nimport win32gui", head)


if __name__ == "__main__":
    unittest.main(verbosity=2)
