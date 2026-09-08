# Inspection: renders, measurements, and the comparison panel

Purpose: which views the backend renders and why, clay versus material renders, what the measurements report and cannot report, and how the GUI comparison panel presents evidence.

All renders are produced by Blender from an exact committed revision (R-62); agent-supplied or retouched images are never evidence. Each render carries a manifest (source revision hash, asset hashes, camera, visibility, mode and lighting, engine and Blender version, resolution, colour management, seed, output hash, success) and a cache key over every input that affects the image (R-63). A render is reused only when its key matches and its file hash still matches the manifest (R-81); anything stale, failed, missing, unreadable, or mismatched is labelled and blocks the gate (R-64).

## Views rendered

Cameras are never fixed presets. They are framed from the measured bounding box of the subject (`render.frame_camera`: a perspective camera along a direction whose vertical field of view contains the bounding sphere with a margin), recorded per component with `calibration_status: hypothesis` and the assumption "framed from the measured bounding box; not calibrated to the references" (R-60), frozen so before and after renders compare, and re-framed as a new view version with a journal entry (`view.reframed`) only when parts leave the frame (R-39).

### The standard set (R-61)

`builder/render.py::STANDARD_VIEW_DEFS`, all at 640x480 with the `Standard` view transform:

| view name | mode | subject | evidence label |
|---|---|---|---|
| `front` | clay | the component | matched |
| `side` | clay | the component | matched |
| `three_quarter` | clay | the component | matched |
| `three_quarter_material` | material | the component | matched |
| `whole_front` | clay | the whole assembly | inferred_construction |
| `whole_three_quarter` | clay | the whole assembly | inferred_construction |

The gate requires fresh renders of the first four (`REVIEW_VIEWS`); coverage is counted on `front`, `side`, `three_quarter` (`COVERAGE_VIEWS`). Assembly views carry `inferred_construction` because no source evidence matches them (R-61).

### Close-ups (R-61, R-65)

One clay view `closeup:<part_id>` per part of the component that has a measured bounding box, framed tight on that part in the three-quarter direction with margin 1.35 so neighbours stay visible. Verification packets add a close-up aligned to the finding's own view, `closeup:<part_id>@<direction>`, rendered BEFORE from the attempt's base revision and AFTER from the new one; these aligned close-ups are packet-only and never part of the gate set. Render manifests of close-ups name the part.

### Crops

For each overview render (`front`, `side`, `three_quarter`) the engine projects a part's world bounding box through the recorded camera into render pixels (`render.project_bbox`) and cuts that rectangle from the full-resolution PNG with 15 percent padding, clamped to the image; the crop is skipped when the part already fills more than 60 percent of the frame or has no measured box. Crops carry `space=render_pixels`, the source render and revision ids, and the part id. Their purpose line reads "where the part sits in the overview render; the closeup view carries the detail". Review packets carry close-ups and crops for every part of the component; correction packets for the finding's part; verification packets BEFORE and AFTER for the finding's part.

## Clay versus material (R-70)

- Clay: engine `BLENDER_WORKBENCH`, studio lighting, a single flat colour (0.8 grey), shadows, cavity, and outlines off, specular highlight on, 8x anti-aliasing (`builder/blender/scripts/render.py`).
- Material: engine `BLENDER_EEVEE`; with the `neutral_studio` lighting preset the scene's own lights are hidden for the render and two sun lights (key and fill) are added.

Comparing the clay and material renders of the same view separates geometry errors from material and lighting errors. The script reports the settings it actually applied (engine, resolution, view transform, samples); a mismatch against the requested view fails the render (`applied_settings_match`) so the manifest records reality, not intent.

Engine identifiers were confirmed from `blender -b -E help` in the design (`BLENDER_EEVEE`, `BLENDER_WORKBENCH`, `CYCLES`).

## Measurements and their limitations (R-68)

`builder/blender/scripts/measure.py` reports, per request: world-space bounding boxes of evaluated meshes, distances between tagged features, part dimensions and ratios, and overlap candidates (bounding-box intersection plus a BVH triangle-intersection test), with declared `intended_contact` relations flagged. The engine measures the component's parts at every build, review, correction, verification, and reassessment and delivers the JSON as evidence (`logs/meas_<id>/measurement.json`, "limitations listed inside").

Every result carries this `limitations` list, verbatim from the script:

- Bounding boxes are axis-aligned in world space on evaluated geometry; rotated parts are overestimated.
- A bounding box intersection is a candidate only, not conclusive collision evidence (R-68).
- Distances are between bounding-box centers and between box faces (gap), not surface-to-surface.
- Mesh overlap uses BVH triangle intersection of evaluated meshes; coplanar touching faces may not register.
- Declared intended contacts are reported but not excluded; the reviewer decides.

Agent-generated numerical scores are judgements, not calibrated measurements (R-71). Operation counts and render time are diagnostics, never quality metrics (R-25).

## The comparison panel (GUI centre column; R-91, R-92)

Two image panes: left "Reference (owner-supplied or approved canon)", right "Render (actual artifact from the backend)". Two modes:

- reference / render: choose any reference (id, labels, canon state) and any render (`<view> @ <revision>  <render id>  <evidence label>`, with `STALE` or `FAILED` appended). On a new snapshot the panel picks the first reference and the latest fresh render when nothing is selected. Selecting a part in the part tree shows its latest close-up on the right.
- before / after (finding): selecting a finding switches to its before and after renders, preferring the finding's own view, and selects its part in the tree. A finding without an after render shows "no after render yet: the finding has not been verified on a new render (R-74)".

Every pane label carries the ids: `render rnd_...  revision rev_...  view front (clay)  evidence: matched  part p_...`, plus `STALE (<reason>): not current evidence (R-64)` or `FAILED: <error>` when applicable; a missing or unreadable file is reported as such and never substituted.

### Zoom is presentation only

The zoom slider (0.25 to 4) rescales the on-screen image of both panes; aspect ratio is preserved and the file is never touched. The presentation line under each pane says so: `presentation: scaled to N% to fit (zoom xZ), aspect W:H preserved; the evidence file is unmodified`.

### Measured boxes overlay

The "measured boxes" checkbox draws, on a render, the bounding boxes from the latest `measurement.json` of that render's revision, projected through the view's recorded camera into render pixels with no padding (`viewmodel.overlay_rects`), labelled with part ids. Nothing is invented: a render whose revision has no measurement shows `measurements: no measurement recorded for this revision` and draws nothing. The line reads `measurements: N measured box(es) projected from revision rev_... (bounding boxes, not calibrated to the references)`. The overlay is presentation; it does not change any record or file.

## Coverage tab (R-72)

Rows of part, view, revision, inspected by (seat), instances inspected, sampling strategy. A part is covered for the gate when a coverage row exists for each of `front`, `side`, `three_quarter` at a revision no older than the part's last modification; verification records coverage for the corrected part at the after revision. Sampling is recorded and never presented as individual inspection of every instance.
