# References and target regions

Purpose: registering reference images (intake), labels and kinds, composite sheets and target regions from the CLI and the GUI, and how originals and versions are kept.

## Adding references

At project creation (`--ref` is repeatable; give one `--labels` value per `--ref`, comma-separated inside each):

```text
python -m builder new --workflow-dir "<wf>" --name "Lamp" --asset "Desk lamp" --first-component Head --ref "C:\refs\front.png" --labels front --ref "C:\refs\side.png" --labels side
```

Later, one or more at a time:

```text
python -m builder intake "<wf>" --ref "C:\refs\rear.png" --labels rear
python -m builder intake "<wf>" --ref "C:\refs\sheet.png" --labels three-quarter,detail --composite --notes "concept sheet, three subjects"
```

`--kind` and `--composite` on `new` and `intake` apply to every `--ref` in that call. `--notes` exists on `intake` only. In the GUI (Project tab, References card): set labels, kind, and the "composite sheet" checkbox, then "Add image..." (multiple files allowed; the same labels apply to all of them).

Each added reference prints its id, version, and size:

```text
added reference ref_... v1 1024x768 labels=['front']
2 reference(s); intake ready: True
```

## Labels and kinds

- Labels are free strings; the known view labels are `front`, `side`, `rear`, `top`, `underside`, `three-quarter`, `detail`, `other` (`builder/references.py::KNOWN_LABELS`). When no label is given the CLI uses `other`. Labels drive coverage in the concept stage (an approved reference labelled `front` covers the needed `front` view).
- Kinds (R-26): `target` (default; evidence of the original design), `previous_attempt`, `rejected`, `hypothesis`. A fifth kind, `generated`, is reserved for concept-stage imports and cannot be given to `new` or `intake`.
- Only `target` references (and approved generated canon) make intake ready: `intake_ready()` refuses when no approved target exists.
- Owner-supplied references enter as canon: `canon_state: approved`, precedence `owner_target` (0), the highest rank (addendum R-94, R-95). `evidence_of_original` is `true` for kind `target` and `false` for everything else; the GUI shows this column as "evidence of original: yes / no (hypothesis)".

## Composite sheets and target regions (R-27)

A composite sheet holds unrelated subjects. Mark it `--composite`; intake then stays not ready until the sheet has at least one region with purpose `target_region`, so nothing outside that region enters the evidence:

```text
2 reference(s); intake ready: False (composite reference ref_... needs an explicit target region so unrelated subjects never enter the evidence (R-27))
```

### CLI

```text
python -m builder intake "<wf>" --ref "C:\refs\sheet.png" --labels three-quarter --composite --region <reference_id>:<name>:x,y,w,h
```

`--region` is `<reference_id>:<name>:x,y,w,h` with integer coordinates in the original image's pixels (top-left origin). It is repeatable and always creates a `target_region`. Because the region needs the reference id, add the sheet first, read its id from the `added reference ref_...` line, then run `intake` again with `--ref` for another image plus `--region` for the sheet (`--ref` is required on `intake`). The bbox must lie inside the image and have a positive size, else the command fails with `bbox [...] is outside the WxH image or empty`.

### GUI

1. On the Project tab, select the reference in the References tree (a region row selects its parent reference). The comparison panel switches to reference/render mode and shows the reference on the left.
2. Type a region name (default `target`) and choose the purpose: `target_region` or `detail_crop`.
3. Click "Draw region on reference". The status bar says `drag a rectangle on reference ref_... to record target_region 'name'`.
4. Drag a rectangle on the reference preview. The pane converts the canvas rectangle back through the presentation scale and centring offset into the original image's pixels, clamps it to the image, and submits it (`ImagePane.canvas_to_original`, `BuilderSession.add_region`). One region per click of the button.

The region is stored in original pixels whatever the zoom; the preview draws it as an accent-coloured rectangle with its name and purpose, and the References tree lists it under its reference with `original pixels` and the bbox. The reference label in the comparison panel lists every region as `name [x, y, w, h] [purpose]`.

Region purposes (`REGION_PURPOSES`): `target_region` (required on composites) and `detail_crop`. Crops for evidence are cut by Pillow from the original at native resolution and carry `space=original_pixels` plus the source rectangle (`builder/evidence.py`).

## Originals, hashes, versions

- The file is copied byte-for-byte to `refs/<ref_id>_v<version><ext>` and hashed (sha256); a copy whose hash differs from the source is deleted and the intake fails (R-48). The original path is recorded in `original_path`; the original is never modified.
- Width, height, and format are read with Pillow and stored; regions are validated against them.
- Replacing a reference (`References.add(..., replaces_id=...)`, not exposed as a CLI flag) creates a new record at version `n+1` and marks the old one `replaced_by`; `current()` lists only unreplaced references, and the old record stays in history (R-27).
- Every reference file is registered in the store's `files` table so later hash mismatches are detected (R-45).
- Concept-stage imports live under `refs/generated/<request_id>/` with `kind: generated`, `evidence_of_original: false`, and `canon_state: candidate`; see [concept-stage.md](concept-stage.md).

## What packets receive

Only approved references enter intake and modeling packets (R-94), ranked by precedence: owner targets, then the approved anchor, then approved turnaround views, then approved studies (R-95). Generated references carry the note "canon by owner approval, not evidence of a pre-existing design (R-32, A.1)" in the evidence index. The GUI label reads `evidence of original: no (generated: a hypothesis, never evidence of the original, R-32)` and adds `declared by owner: vendor=... model=... (not verified)` when declarations exist.
