# Collaborative Model Builder: user documentation

Purpose: the index of the builder's user documentation (spec Section 8) and a ten-line quick start. The builder is the `builder/` package inside Alloy: two AI agents reconstruct a concept design as an editable Blender model while Alloy owns every state transition, runs every Blender operation, and gates progress on evidence (R-1, D1, D4).

Every page states what the code does as of 2026-09-07 (working tree, HEAD `362b752` plus uncommitted changes). Where a behaviour has not been exercised live, the page says so.

## Pages

| Page | What it covers |
|---|---|
| [installation-and-blender.md](installation-and-blender.md) | Python and packages, Blender discovery and the smoke test, deadlines, where workflow state lives, every `builder:` config key with its default. |
| [launching.md](launching.md) | The CLI (`python -m builder`, `python main.py --build`), the Tk window (View > Model Builder..., standalone module), launch scripts, and what each entry point refuses to do on its own. |
| [agents-and-preflight.md](agents-and-preflight.md) | Seats A and B, provider/model/reasoning/executable selection, the three preflight tiers, what `preflight --live` invokes and costs, cached reports, reasoning downgrades, the codex binary note, gemini status, the image seat. |
| [references-and-regions.md](references-and-regions.md) | Intake (`new --ref`, `intake`), labels and kinds, composite sheets and target regions (CLI and GUI), unmodified hashed originals, versions. |
| [concept-stage.md](concept-stage.md) | Starting from text or seed images, approval modes, the request/prompt/import loop with the manual image seat, verdicts, conflicts, coverage, studies, limits, the Concept tab. |
| [assignments-and-handoffs.md](assignments-and-handoffs.md) | Roles and the scheduler rules, ownership tokens and base revisions, handoff, what a retried interrupted build does. |
| [inspection.md](inspection.md) | Rendered views (standard set, close-ups, crops), clay versus material, measurements and their limitations, the comparison panel, the measured-boxes overlay, zoom. |
| [findings-and-acceptance.md](findings-and-acceptance.md) | Finding records, the review/reconcile withholding rule, verification on new renders, correction attempts and reassessment, user actions, ready-for-review versus accepted. |
| [pause-resume-recovery.md](pause-resume-recovery.md) | Pause, resume, cancel, feedback; what happens on restart (reconciliation, quarantine, orphan processes); checkpoints and restore. |
| [unattended-and-limits.md](unattended-and-limits.md) | Attended versus unattended, every limit and its enforceability, stall detection, stop reasons, editing limits, the budget-after-spend caveat. |
| [token-efficiency-and-usage.md](token-efficiency-and-usage.md) | Packets versus transcripts, changed-since deltas, isolated reviews, usage capture, cost reporting, the R-84 harness, probe spend seeding. |
| [output-layout.md](output-layout.md) | The workflow directory layout, what is immutable, how to reopen a revision in Blender, harness report files. |
| [known-limitations.md](known-limitations.md) | Guardrails that are not sandboxes, unverified integrations, unknown costs, caps that act after the spend, and gaps found while writing these pages. |

## Quick start (CLI)

`<wf>` is the workflow directory. Replace the image paths. Step 4 spends provider usage; nothing before it does.

```text
1.  python -m builder new --workflow-dir "<wf>" --name "Lamp" --asset "Desk lamp" --first-component Head --ref "C:\refs\front.png" --labels front
2.  python -m builder intake "<wf>" --ref "C:\refs\sheet.png" --labels three-quarter --composite
3.  python -m builder intake "<wf>" --ref "C:\refs\side.png" --labels side --region <ref_id_of_sheet>:subject:120,80,640,900
4.  python -m builder source empty "<wf>"                 (or: source register "<wf>" "C:\art\model.blend")
5.  python -m builder preflight "<wf>"
6.  python -m builder preflight "<wf>" --live
7.  python -m builder start "<wf>" [--assign build=B]
8.  python -m builder status "<wf>"
9.  python -m builder findings "<wf>" --open
10. python -m builder accept "<wf>" <component_id>
```

Notes on the quick start:

- Step 3 records a target region on the composite sheet from step 2 (`intake` prints each reference id as `added reference ref_...`). A composite sheet without a target region blocks intake (R-27); see [references-and-regions.md](references-and-regions.md).
- Step 4 registers revision 0 (Blender required): `source empty` builds an empty scene through the runner; `source register` validates an existing `.blend` in separate Blender processes and copies it unmodified. Creating the project never opens a source (R-93); without this step the build stage stops with `execution_failure` ("no revision exists"). See [output-layout.md](output-layout.md).
- Step 5 is the free local preflight (CLI present, version, `--help` flags). Step 6 runs the live probes and is required before `start` with real agents; `start` never runs it implicitly (R-18, R-21).
- Step 7 stops at the attended gate with stop reason `ready_for_user_review`; step 10 is the separate user acceptance (R-76). `--assign role=SEAT` pre-assigns a role for the run (see [assignments-and-handoffs.md](assignments-and-handoffs.md)).
- The same flow runs in the Tk window: `python main.py --gui`, then View > Model Builder... (see [launching.md](launching.md)).
