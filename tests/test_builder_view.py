"""The Tk view (layer D with a hidden Tk root; skipped with a reason when no display is available): panels of
R-91 render a snapshot, previews are actual artifacts labelled with revision and render ids with the aspect ratio
preserved and presentation scaling distinguished from the evidence (R-92), the measurements toggle draws only
measured boxes, the concept approval panel shows the mode, prompts, candidates beside the anchor, and both seats'
verdicts (R-110), and every control submits a job to the session instead of touching the engine on the Tk thread."""
from __future__ import annotations

import queue
from pathlib import Path

import pytest
from PIL import Image

from builder.config import BuilderConfig
from builder.engine import Engine
from builder.viewmodel import snapshot

from test_engine import _adapters, _config, _runner, project  # noqa: F401


class StubSession:
    """Records every call the window makes; nothing runs."""

    def __init__(self):
        self.events: queue.Queue = queue.Queue()
        self.calls: list[tuple] = []
        self.engine = None
        self.worker_thread_name = "stub"

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return call


@pytest.fixture(scope="module")
def snap(tmp_path_factory):
    """One real snapshot from a mock run (render files exist on disk)."""
    from PIL import Image as _Image
    from builder.operations import Operations
    from builder.ownership import OwnershipManager
    from builder.project import Project
    from builder.records import Record
    from builder.references import References
    from test_engine import PARTS

    d = tmp_path_factory.mktemp("view ü")
    prj = Project.create(d / "wf", name="Lamp", asset_name="Lamp", extra={"first_component": "Head"})
    refs = References(prj)
    for label, size in (("front", (120, 90)), ("side", (200, 50))):
        p = d / f"{label}.png"
        _Image.new("RGB", size, (90, 90, 90)).save(p)
        refs.add(p, labels=[label], kind="target")
    seed = d / "seed.blend"
    seed.write_bytes(b"BLENDER-fake-seed")
    Operations(prj, None, OwnershipManager(prj.store)).register_revision(
        seed, parent_revision_id=None, created_by_op_id=None, actor="engine",
        identity_map=[{"alloy_id": p, "type": "OBJECT"} for p in PARTS], note="fixture revision 0")
    for p in PARTS:
        prj.store.upsert(Record.new("part", {"name": p, "blender_ids": [p]}, id=p), actor="engine", event="part.created")
    runner = _runner()
    runner.measure_data = {"bboxes": {"p_bracket": {"min": [-0.15, -0.15, 2.3], "max": [0.15, 0.15, 2.4]}},
                           "distances": [], "ratios": {}, "overlaps": [], "limitations": ["fake"]}
    eng = Engine(prj, _config(), adapters=_adapters(), runner=runner)
    eng.preflight(live=True)
    eng.start()
    eng.run_until_stop(max_steps=60)
    s = snapshot(eng)
    prj.close()
    return s


@pytest.fixture
def window(snap, tk_root):
    from gui.builder_view import BuilderWindow

    sess = StubSession()
    win = BuilderWindow(tk_root, session=sess, config=BuilderConfig.from_dict({}), autopoll=False)
    win.withdraw()
    yield win, sess
    win.destroy()


def test_panels_render_the_snapshot(window, snap):
    win, sess = window
    win.apply_snapshot(snap)
    win.update_idletasks()
    assert "waiting_for_user" in win.stage_var.get() and "ready_for_user_review" in win.stage_var.get()
    assert "each" in win.mode_var.get()                                  # approval mode always visible (D11)
    agents = [win.agents_tree.item(i, "values") for i in win.agents_tree.get_children()]
    assert any("max" in str(v) and "confirmed" in str(v) for v in agents)   # effective reasoning shown (R-20)
    parts = {win.parts_tree.item(i, "text"): i for i in win.parts_tree.get_children("")}
    assert "p_bracket" in parts
    rel_rows = [win.parts_tree.item(c, "text") for c in win.parts_tree.get_children(parts["p_bracket"])]
    assert any("attached_to" in r and "p_housing" in r for r in rel_rows)   # relations under the part (R-36)
    findings = [win.findings_tree.item(i, "values") for i in win.findings_tree.get_children()]
    assert findings and "p_bracket" in findings[0] and "closed" in findings[0]
    assert win.coverage_tree.get_children()
    text = win.consumption_text.get("1.0", "end")
    assert "attempts_per_finding" in text and "enforceable" in text
    assert "unaccepted" in win.components_var.get() and "ready_for_user_review" in win.components_var.get()


def test_previews_are_labelled_artifacts_with_aspect_preserved(window, snap):
    win, _ = window
    win.apply_snapshot(snap)
    ok = [r for r in snap["renders"] if r["state"] == "ok" and r["view_name"] == "side"]
    render = ok[-1]
    win.compare.right.show_render(render["id"])
    win.update_idletasks()
    pane = win.compare.right
    with Image.open(render["file"]) as im:
        w, h = im.size
    dw, dh = pane.image_size
    assert abs((dw / dh) - (w / h)) < 0.02                              # aspect preserved (R-92)
    label = pane.label_var.get()
    assert render["id"] in label and render["revision_id"] in label and "side" in label
    assert "matched" in label or "inferred_construction" in label         # evidence label (R-61)
    assert "scaled" in pane.presentation_var.get().lower()                 # presentation, not altered evidence (R-92)
    pane.set_zoom(2.0)
    win.update_idletasks()
    assert pane.image_size[0] > dw
    # references: canon state and evidence-of-original label (R-32, R-94)
    ref = snap["references"][0]
    win.compare.left.show_reference(ref["id"])
    assert ref["id"] in win.compare.left.label_var.get() and "approved" in win.compare.left.label_var.get()


def test_measurements_toggle_draws_only_measured_boxes(window, snap):
    win, _ = window
    win.apply_snapshot(snap)
    side = [r for r in snap["renders"] if r["state"] == "ok" and r["view_name"] == "side"][-1]
    pane = win.compare.right
    pane.show_render(side["id"])
    assert not pane.canvas.find_withtag("measure")
    pane.set_measurements(True)
    boxes = pane.canvas.find_withtag("measure")
    assert len(boxes) >= 1                                                 # p_bracket was measured for this revision
    pane.set_measurements(False)
    assert not pane.canvas.find_withtag("measure")
    front_whole = [r for r in snap["renders"] if r["state"] == "ok" and r["view_name"] == "whole_front"]
    if front_whole:
        pane.show_render(front_whole[-1]["id"])
        pane.set_measurements(True)
        assert "no measurement" in pane.presentation_var.get().lower() or pane.canvas.find_withtag("measure") is not None


def test_before_and_after_follow_the_selected_finding(window, snap):
    win, _ = window
    win.apply_snapshot(snap)
    fid = snap["findings"][0]["id"]
    win.select_finding(fid)
    win.update_idletasks()
    assert win.compare.mode_var.get() == "before/after"
    before = win.compare.left.current_render_id
    after = win.compare.right.current_render_id
    assert before in snap["findings"][0]["before_render_ids"] and after in snap["findings"][0]["after_render_ids"]
    # the finding's part is selected in the tree and the finding detail names the images
    assert win.parts_tree.selection() and win.parts_tree.item(win.parts_tree.selection()[0], "text") == "p_bracket"
    assert before in win.finding_text.get("1.0", "end")


def test_programmatic_selection_does_not_re_enter_the_select_handlers(window, snap, monkeypatch):
    """A programmatic selection fires <<TreeviewSelect>>, whose handler must not select again (an event loop that
    reloads images forever once the window processes events)."""
    win, _ = window
    win.apply_snapshot(snap)
    calls = {"finding": 0, "candidate": 0}

    def counted(name, tree, original):
        # an exception inside a Tk callback is swallowed, so the loop is cut by unbinding after a few rounds
        def wrapper(*args, **kwargs):
            calls[name] += 1
            if calls[name] > 3:
                tree.unbind("<<TreeviewSelect>>")
            return original(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(win.compare, "show_finding", counted("finding", win.findings_tree, win.compare.show_finding))
    win.select_finding(snap["findings"][0]["id"])
    win.update()                       # deliver the virtual events a real window would process
    win.update()
    assert calls["finding"] == 1, calls
    concept = {**(snap["concept"]), "prompts": [{"request_id": "gen_R", "target": "view front", "prompt": "P", "prompt_file": "f",
                                                  "expected_count": 1, "round": 1, "art_director": "A", "attachments": [], "import_command": "c"}],
               "candidates": [{"id": "ref_C", "file": "", "labels": ["front"], "canon_state": "candidate", "precedence_label": "turnaround",
                               "evidence_of_original": False, "verdicts": {}, "summary": None, "declared": {}}]}
    panel = win.concept_panel
    monkeypatch.setattr(panel.candidate_pane, "show_reference_entry",
                        counted("candidate", panel.candidates_tree, panel.candidate_pane.show_reference_entry))
    win.apply_snapshot({**snap, "concept": concept})
    win.update()
    win.update()
    assert calls["candidate"] <= 2, calls      # once from bind_snapshot, at most once from the event


def test_controls_submit_jobs_to_the_session(window, snap):
    win, sess = window
    win.apply_snapshot(snap)
    comp = snap["components"][0]["id"]
    win.buttons["pause"].invoke()
    win.buttons["accept"].invoke()
    win.select_finding(snap["findings"][0]["id"])
    win.waive_rationale.set("out of scope for this pass")
    win.buttons["waive"].invoke()
    win.feedback_var.set("keep the lens blue")
    win.buttons["feedback"].invoke()
    names = [c[0] for c in sess.calls]
    assert names[:2] == ["pause", "accept_component"]
    assert sess.calls[1][1] == (comp,)
    assert ("waive_finding", (snap["findings"][0]["id"],), {"rationale": "out of scope for this pass", "user": win.user_var.get()}) in sess.calls
    assert ("feedback", ("keep the lens blue",), {"user": win.user_var.get()}) in sess.calls
    # the unattended switch is an explicit opt-in (D8): start passes the value through
    win.attended_var.set(False)
    win.buttons["start"].invoke()
    assert sess.calls[-1] == ("start_run", (), {"attended": False, "component_name": None})


def test_queue_messages_update_status_and_activity(window, snap):
    win, sess = window
    sess.events.put(("busy", "start_run"))
    win._poll()
    assert "start_run" in win.status_var.get() and str(win.buttons["start"]["state"]) == "disabled"
    sess.events.put(("event", {"event": "stage", "stage": "review", "at": "t"}))
    sess.events.put(("event", {"event": "invocation", "label": "B", "purpose": "review_task", "outcome": "ok", "at": "t"}))
    sess.events.put(("error", "start_run", "RuntimeError: boom"))
    sess.events.put(("snapshot", snap))
    win._poll()
    log = win.activity.get("1.0", "end")
    assert "review" in log and "review_task" in log and "boom" in log
    assert "waiting_for_user" in win.stage_var.get() and str(win.buttons["start"]["state"]) == "normal"


def test_concept_panel_shows_mode_prompts_candidates_and_verdicts(window, snap, tmp_path):
    win, sess = window
    a = tmp_path / "anchor.png"
    c = tmp_path / "cand.png"
    Image.new("RGB", (80, 60), (10, 20, 30)).save(a)
    Image.new("RGB", (60, 80), (30, 20, 10)).save(c)
    concept = {
        "plan_id": "cplan_1", "mode": "each", "mode_text": "approval mode each: the owner approves every generated image",
        "canon_state": "turnaround_pending",
        "anchor": {"id": "ref_ANCHOR", "file": str(a), "labels": ["anchor"], "canon_state": "approved", "precedence_label": "anchor",
                   "evidence_of_original": False, "width": 80, "height": 60, "declared": {"vendor": "chatgpt"}},
        "prompts": [{"request_id": "gen_REQ1", "target": "view front", "prompt": "PROMPT-TEXT front view", "prompt_file": "x/PROMPT.md",
                     "expected_count": 1, "round": 1, "art_director": "A",
                     "attachments": [{"reference_id": "ref_ANCHOR", "path": str(a), "sha256": "abc", "role": "anchor"}],
                     "import_command": "python -m builder concept import wf gen_REQ1 <files>"}],
        "candidates": [{"id": "ref_CAND", "file": str(c), "labels": ["front"], "canon_state": "candidate", "precedence_label": "turnaround",
                        "evidence_of_original": False, "width": 60, "height": 80, "generation_id": "gen_REQ0",
                        "declared": {"vendor": "chatgpt", "model": "GPT Image"},
                        "verdicts": {"A": {"verdict": "consistent", "inconsistencies": [], "rationale": "matches"},
                                     "B": {"verdict": "inconsistent", "rationale": "toe count",
                                           "inconsistencies": [{"part": "leg", "region": "knee", "what_differs": "three toes", "severity": "high"}]}},
                        "verdict_summary": "inconsistent", "summary": "inconsistent"}],
        "coverage": {"front": {"status": "candidates", "approved": [], "candidates": ["ref_CAND"], "requests_open": []}},
        "parts": {}, "missing": ["front"], "complete": False, "conflicts": [], "escalations": [],
        "images": {"count": 2, "max": 8}, "proceeded_partial": None, "seat": {"seat": "manual", "vendor": "chatgpt"},
        "cost": {"kind": "not_applicable"}, "art_director": {"seat": "A"}, "rejected_rounds": 0, "flow_confirmed_at": None,
        "requests": {"open": ["gen_REQ1"], "by_state": {}},
    }
    win.apply_snapshot({**snap, "concept": concept})
    win.update_idletasks()
    panel = win.concept_panel
    assert "each" in panel.mode_var.get() and "owner approves every" in panel.mode_var.get()
    reqs = [panel.requests_tree.item(i, "values") for i in panel.requests_tree.get_children()]
    assert reqs and "gen_REQ1" in reqs[0]
    panel.select_request("gen_REQ1")
    assert "PROMPT-TEXT front view" in panel.prompt_text.get("1.0", "end") and str(a) in panel.attachments_var.get()
    panel.buttons["copy_prompt"].invoke()
    assert win.clipboard_get() == "PROMPT-TEXT front view"
    cands = [panel.candidates_tree.item(i, "values") for i in panel.candidates_tree.get_children()]
    assert cands and "ref_CAND" in cands[0] and "A=consistent" in str(cands[0]) and "B=inconsistent" in str(cands[0])
    panel.select_candidate("ref_CAND")
    win.update_idletasks()
    assert panel.anchor_pane.current_reference_id == "ref_ANCHOR" and panel.candidate_pane.current_reference_id == "ref_CAND"
    assert "hypothesis" in panel.candidate_pane.label_var.get().lower()      # a generated image is never evidence (R-32)
    assert "three toes" in panel.verdict_text.get("1.0", "end") and "toe count" in panel.verdict_text.get("1.0", "end")
    panel.buttons["approve"].invoke()
    panel.reject_reason.set("wrong toe count")
    panel.buttons["reject"].invoke()
    panel.regenerate_note.set("two toes")
    panel.buttons["regenerate"].invoke()
    assert ("concept_approve", (["ref_CAND"],), {"user": win.user_var.get()}) in sess.calls
    assert ("concept_reject", (["ref_CAND"],), {"reason": "wrong toe count", "user": win.user_var.get()}) in sess.calls
    assert ("concept_regenerate", ("gen_REQ0",), {"note": "two toes", "user": win.user_var.get()}) in sess.calls
    assert "front" in panel.coverage_var.get() and "candidates" in panel.coverage_var.get()


# --- Phase 4: target regions by dragging (R-26, R-27), editable limits (R-85), concept pane width -------------------

def _drag(canvas, x0, y0, x1, y1):
    canvas.event_generate("<ButtonPress-1>", x=x0, y=y0)
    canvas.event_generate("<B1-Motion>", x=(x0 + x1) // 2, y=(y0 + y1) // 2)
    canvas.event_generate("<B1-Motion>", x=x1, y=y1)
    canvas.event_generate("<ButtonRelease-1>", x=x1, y=y1)


def test_dragging_a_region_on_the_reference_pane_submits_it_in_original_pixels(window, snap):
    win, sess = window
    win.apply_snapshot(snap)
    ref = snap["references"][0]                       # 120x90 image
    pane = win.compare.left
    pane.show_reference(ref["id"])
    win.deiconify()                                   # Tk drops synthetic pointer events on an unmapped window
    win.update()
    win.region_name.set("lower-left humanoid")
    win.region_purpose.set("target_region")
    win.buttons["draw_region"].invoke()               # arms one drag on the reference pane
    assert pane.region_mode is True
    ox, oy = pane._origin
    s = pane._scale
    # a drag from image pixel (12, 9) to (60, 45), expressed in canvas pixels, crossing back to test normalisation
    x0, y0 = ox + round(12 * s), oy + round(9 * s)
    x1, y1 = ox + round(60 * s), oy + round(45 * s)
    _drag(pane.canvas, x1, y1, x0, y0)
    win.update()
    calls = [c for c in sess.calls if c[0] == "add_region"]
    assert len(calls) == 1
    args, kwargs = calls[0][1], calls[0][2]
    assert args[0] == ref["id"] and args[1] == "lower-left humanoid"
    x, y, w, h = args[2]
    assert abs(x - 12) <= 1 and abs(y - 9) <= 1 and abs(w - 48) <= 2 and abs(h - 36) <= 2      # original pixels, not canvas
    assert kwargs == {"purpose": "target_region", "user": win.user_var.get()}
    assert pane.region_mode is False                  # one region per arming; the next drag does nothing
    _drag(pane.canvas, x0, y0, x1, y1)
    assert len([c for c in sess.calls if c[0] == "add_region"]) == 1
    # a drag that leaves the image is clamped to it, never a region outside the original
    win.buttons["draw_region"].invoke()
    _drag(pane.canvas, ox - 30, oy - 30, ox + round(20 * s), oy + round(20 * s))
    x, y, w, h = [c for c in sess.calls if c[0] == "add_region"][-1][1][2]
    assert (x, y) == (0, 0) and abs(w - 20) <= 1 and abs(h - 20) <= 1
    win.withdraw()


def test_existing_regions_are_drawn_on_the_reference_and_listed(window, snap):
    win, _ = window
    ref = dict(snap["references"][0])
    ref["regions"] = [{"id": "reg_1", "name": "lower-left", "bbox": [10, 10, 40, 30], "purpose": "target_region", "space": "original_pixels"}]
    snap2 = {**snap, "references": [ref] + snap["references"][1:]}
    win.apply_snapshot(snap2)
    pane = win.compare.left
    pane.show_reference(ref["id"])
    win.update_idletasks()
    assert pane.canvas.find_withtag("region")
    assert "lower-left" in pane.label_var.get() and "original pixels" in pane.label_var.get()
    rows = [win.references_tree.item(i, "values") for i in win.references_tree.get_children(ref["id"])]
    assert rows and "lower-left" in str(rows[0]) and "[10, 10, 40, 30]" in str(rows[0])


def test_limits_card_shows_the_limits_in_force_and_apply_submits_only_the_changes(window, snap):
    win, sess = window
    win.apply_snapshot(snap)
    lim = snap["consumption"]["limits"]
    assert win.limit_vars["attempts_per_finding"].get() == str(lim["attempts_per_finding"]["value"])
    assert win.limit_vars["stall_steps"].get() == str(lim["stall_steps"]["value"])
    assert "enforceable" in win.limits_note_var.get()
    win.limit_vars["max_requests"].set("5")
    win.limit_vars["stall_steps"].set("20")
    win.buttons["apply_limits"].invoke()
    assert sess.calls[-1] == ("set_limits", ({"max_requests": "5", "stall_steps": "20"},), {"user": win.user_var.get()})


def test_concept_image_pair_has_a_wider_default(window):
    win, _ = window
    panel = win.concept_panel
    assert int(panel.anchor_pane.canvas["width"]) >= 300 and int(panel.candidate_pane.canvas["width"]) >= 300
    assert win._sash_fractions(concept=True)[1] <= 0.42 < win._sash_fractions(concept=False)[1]


def test_stage_card_shows_assignment_overrides_with_their_rationale(window, snap):
    win, sess = window
    s = dict(snap)
    s["stage"] = dict(snap["stage"], assignment_overrides=[
        {"role": "build", "seat": "B", "source": "cli",
         "rationale": "user override (cli: build=B); R-107 still applies: B never reviews, verifies, or reassesses its own operation"}])
    win.apply_snapshot(s)
    text = win.stage_text.get("1.0", "end")
    assert "assignment override build=B (cli)" in text and "user override" in text and "R-107" in text


def test_start_passes_the_assignments_entry_as_overrides(window, snap):
    win, sess = window
    win.apply_snapshot(snap)
    win.assign_var.set("build=B plan=A")
    win.buttons["start"].invoke()
    assert sess.calls[-1] == ("start_run", (), {"attended": True, "component_name": None, "assignments": {"build": "B", "plan": "A"}})


def test_project_tab_offers_source_registration(window, snap):
    win, sess = window
    win.apply_snapshot(snap)
    win.buttons["source_empty"].invoke()
    assert sess.calls[-1][0] == "register_empty_source"
