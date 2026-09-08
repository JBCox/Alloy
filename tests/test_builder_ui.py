"""Static contracts of the Model Builder view in ui/index.html (text-level, like test_rooms.py).

The executing checks live in test_ui_boot.py (the node harness drives the real uiEvent); these pin what
must be true of the SOURCE: the section is delimited once, sits last in <main>, every control the script
needs exists, the modals are registered everywhere, the honesty strings survive verbatim, no interval is
armed, and the builder branch of uiEvent sits above the chat routing gate.
Run: python tests/test_builder_ui.py
"""
from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
UI = os.path.join(ROOT, "ui", "index.html")

IDS = """builderBtn builderDot builderView bProjectName bModeLine bPills bJob bNewBtn bOpenBtn bRecentSel bCloseBtn
bTabBtnProject bTabBtnAgents bTabBtnRun bTabProject bTabAgents bTabRun bProjectText bRefs bRefLabels bRefKind
bRefComposite bAddRef bSourceEmpty bSourceRegister bAgents bAgentText bPreflight bPreflightLive bRecoverNote
bStageText bComponentsText bStart bResume bPause bCancel bAttended bAssign bAssignNote bComponentSel bAccept
bReopen bCorrect bWaive bCheckpointSel bRestore bFeedback bLimits bApplyLimits bLimitsNote bConsumption
bCmpModeRR bCmpModeBA bMeasure bZoom bZoomVal bRefSel bRenderSel bPaneL bPaneR bTabBtnParts bTabBtnFindings
bTabBtnCoverage bTabBtnActivity bTabParts bTabFindings bTabCoverage bTabActivity bParts bPartText bFindings
bFindingText bCoverage bActivity bConfirmModal bConfirmTitle bConfirmBody bConfirmAckRow bConfirmAck
bConfirmAckText bConfirmOk bConfirmCancel bConfirmClose bPromptModal bPromptTitle bPromptLabel bPromptInput
bPromptArea bPromptNote bPromptOk bPromptCancel bPromptClose bNewModal bNewName bNewAsset bNewDir bNewDirBtn
bNewFirst bNewPreset bNewNote bNewFixtureBtn bNewOk bNewCancel bNewClose""".split()

# rule-bearing text from the legacy Tk view (gui/builder_view.py on branch legacy-tk); abbreviating any of
# these for layout breaks the spec it names
HONESTY = [
    "): not current evidence (R-64)",
    "FAILED: ",
    "missing file: this artifact cannot be shown (R-64)",
    "no (generated: a hypothesis, never evidence of the original, R-32)",
    "composite sheet: target region required (R-27)",
    "declared by owner: vendor=",
    "(not verified)",
    "regions (original pixels): ",
    "Bounding boxes only, not calibrated to the references (R-68). Nothing is drawn without a measurement.",
    "(bounding boxes, not calibrated to the references)",
    "measurements: no measurement recorded for this revision",
    "the evidence file is unmodified",
    "no after render yet: the finding has not been verified on a new render (R-74)",
    "no before render recorded for this finding",
    "(unknown is never zero)",
    "enforceable=",
    "'Ready for user review' is never 'accepted'.",
    "REASONING DOWNGRADE (reported, not silent): ",
    "BLOCKER: ",
    "codex reports no cost (unknown, never zero)",
    "QUESTION for you: ",
    "Blender: ",
    "NOT FOUND",
    "approval mode: -",
    "free (no holder)",
    "zero means unlimited",
    "history is never rewritten and reviews are invalidated (R-46, R-39)",
    "A waiver needs a rationale (R-75).",
    "revision 0 (never registered by creating or opening the project, R-93)",
]


def read():
    with open(UI, encoding="utf-8") as f:
        return f.read()


def section(src, marker):
    start = src.index(f"===== {marker} =====")
    end = src.index(f"===== /{marker} =====", start)
    return src[start:end]


class BuilderViewSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = read()

    def test_the_section_is_delimited_once_in_css_markup_and_script(self):
        # CSS + JS share the BUILDER VIEW fence (two open, two close); the markup has its own two fences
        self.assertEqual(self.src.count("===== BUILDER VIEW ====="), 3)     # css, markup, js
        self.assertEqual(self.src.count("===== /BUILDER VIEW ====="), 3)
        self.assertEqual(self.src.count("===== BUILDER MODALS ====="), 1)
        self.assertEqual(self.src.count("===== /BUILDER MODALS ====="), 1)

    def test_the_view_is_the_last_child_of_main_and_hidden_by_default(self):
        main_end = self.src.index("</main>")
        view = self.src.index('<section id="builderView" hidden aria-label="Model Builder">')
        file_rail = self.src.index('<aside id="fileRail"')
        self.assertLess(file_rail, view)
        self.assertLess(view, main_end)
        self.assertNotIn("<main", self.src[view:main_end])          # no second <main>: bindRail queries the one

    def test_the_switch_hides_the_chat_surface_by_negation_and_beats_the_ua_hidden_rule(self):
        self.assertIn("body.builder-open main > :not(#appNav):not(#builderView) { display: none; }", self.src)
        self.assertIn("#builderView[hidden] { display: none; }", self.src)
        self.assertIn("body.builder-open #stopBtn", self.src)

    def test_every_control_the_script_needs_exists_exactly_once(self):
        missing = [i for i in IDS if f'id="{i}"' not in self.src]
        self.assertEqual(missing, [])
        dupes = [i for i in IDS if self.src.count(f'id="{i}"') != 1]
        self.assertEqual(dupes, [])

    def test_the_nav_button_is_in_both_shared_nav_rules(self):
        self.assertIn("#hooksBtn, #builderBtn {", self.src)
        self.assertIn("#hooksBtn:hover, #builderBtn:hover {", self.src)
        nav = self.src[self.src.index('<nav id="appNav"'):self.src.index("</nav>")]
        self.assertIn('id="builderBtn"', nav)
        self.assertIn('class="nav-lbl">Model Builder<', nav)

    def test_the_modals_are_registered_in_both_selector_lists_and_the_escape_block(self):
        marker = "position: fixed; inset: 0; z-index: 50; display: none;"
        i = self.src.index(marker)
        hidden = self.src[self.src.rindex("}", 0, i) + 1:i]
        j = self.src.index("#acctModal.show")
        shown = self.src[j:self.src.index("}", j) + 1]
        k = self.src.index("closeAccounts(); closeRole();")
        escape = self.src[self.src.rindex('if (e.key === "Escape") {', 0, k):]
        escape = escape.split("\n  }", 1)[0]
        for mid in ("bConfirmModal", "bPromptModal", "bNewModal"):
            self.assertIn("#" + mid, hidden)
            self.assertIn("#" + mid + ".show", shown)
        self.assertIn("bCloseModals(null)", escape)

    def test_the_builder_branch_of_uievent_sits_above_the_chat_routing_gate(self):
        ui_event = self.src.index("window.uiEvent = packet =>")
        branch = self.src.index('if (event === "builder") {', ui_event)
        gate = self.src.index("if (payload.background && !chatId) return;", ui_event)
        self.assertLess(branch, gate)
        self.assertIn("return;", self.src[branch:gate])

    def test_chat_shortcuts_stay_off_the_hidden_chat(self):
        self.assertIn('if (builderOpen && e.ctrlKey && !e.altKey && (e.key === "Tab" || /^[1-9tTkK]$/.test(e.key))) return;',
                      self.src)

    def test_every_honesty_string_survives_verbatim(self):
        missing = [s for s in HONESTY if s not in self.src]
        self.assertEqual(missing, [])

    def test_no_interval_is_armed_and_every_bridge_call_is_literal(self):
        js = section(self.src[self.src.index("<script>"):], "BUILDER VIEW")
        self.assertNotIn("setInterval(", js)
        self.assertNotIn('pywebview.api[', js)                     # computed names would dodge the contract test
        calls = set(re.findall(r"pywebview\.api\.(builder_\w+)\(", js))
        self.assertIn("builder_snapshot", calls)
        self.assertIn("builder_start", calls)
        self.assertIn("builder_read_image", calls)
        self.assertIn("builder_new_fixture", calls)
        self.assertNotIn("innerHTML = '<", js)                    # structure is built with createElement (stub DOM rule)

    def test_pause_cancel_and_feedback_are_never_disabled_by_a_busy_job(self):
        js = section(self.src[self.src.index("<script>"):], "BUILDER VIEW")
        block = js[js.index("const B_BUSY_DISABLED"):js.index("];", js.index("const B_BUSY_DISABLED"))]
        for never in ("bPause", "bCancel", "bFeedback", "bApplyLimits"):
            self.assertNotIn(f'"{never}"', block)
        for always in ("bStart", "bResume", "bAccept", "bReopen", "bWaive", "bCorrect", "bRestore",
                       "bPreflight", "bPreflightLive", "bOpenBtn", "bAddRef"):
            self.assertIn(f'"{always}"', block)

    def test_icon_only_buttons_carry_title_and_aria_label(self):
        markup = section(self.src, "BUILDER VIEW") + section(self.src, "BUILDER MODALS")
        for m in re.finditer(r"<button([^>]*)>([^<]*)</button>", markup):
            attrs, text = m.group(1), m.group(2).strip()
            if text and not text.startswith("✕"):
                continue
            self.assertIn("title=", attrs, m.group(0))
            self.assertIn("aria-label=", attrs, m.group(0))

    def test_the_view_never_borrows_a_provider_colour_for_chrome(self):
        css = section(self.src, "BUILDER VIEW")
        for var in ("--claude", "--gpt", "--gemini", "--ox", "--josh"):
            self.assertNotIn(var, css.replace(".b-seat[data-provider", ""))


if __name__ == "__main__":
    unittest.main()
