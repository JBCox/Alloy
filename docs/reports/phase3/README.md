# Phase 3 screenshots (2026-09-07)

Real `BuilderWindow` (`gui/builder_view.py`) captured with Pillow's `ImageGrab` on this machine. Two projects, both
disposable and generated under the session scratchpad; nothing under `C:\Death Factory` was read.

- **Fixture project** (`builder/fixture.py` on real Blender 5.2.1): the synthetic lamp with its planted defects,
  run with the scripted mock agents from `tests/_screenplay.py` (A builds, B finds the floating bracket, B corrects it
  by handoff, A verifies on new renders). Real Blender executes every operation and render; the mocks only script the
  agent replies, so the images are actual backend artifacts and the findings are routed by the engine, never judged
  visually by anything in the loop.
- **Concept project** (fake runner, no Blender): the concept stage with prepared PNGs imported through the manual image
  seat as anchor candidates and a front view; both LLM seats are scripted mocks that return consistency verdicts.

| file | what it shows |
|---|---|
| `01-project-opened.png` | Project tab: project facts, Blender discovery, the fixture's six synthetic references with canon state and "evidence of original"; the comparison panel with the fixture's stale render labelled `STALE (hash_mismatch)` (R-64). |
| `02-agents-preflight.png` | Agents tab after a (scripted, free) preflight: requested vs effective reasoning, per-capability declared/local/live tiers, the image seat's tiers (R-15, R-20, R-109). |
| `03-run-in-progress-responsive.png` | Run tab while the worker thread runs the engine on real Blender: status "working", execution `running`, stage `build`, consumption updating, controls disabled, window responsive (R-92). |
| `04-run-ready-for-user-review.png` | The attended gate: execution `waiting_for_user`, stop reason `ready_for_user_review`, review state `ready_for_user_review` and acceptance `unaccepted` kept distinct (R-76); the last task with owner and rationale; ownership released; checkpoint listed. |
| `05-parts-and-closeup.png` | Parts tab: part tree with construction relations as child rows and evidence/confidence/owner/component columns; selecting a part shows its close-up render (R-35, R-36, R-61). |
| `06-finding-before-after-measurements.png` | Findings tab: the closed finding with severity and confidence separate, its evidence render ids; the comparison in before/after mode with the measured boxes overlaid on both renders (R-73, R-74, R-68). |
| `07-coverage-and-zoom.png` | Coverage tab (part × view × revision × inspector) and the comparison at zoom 1.8 with the presentation line stating the scale (R-72, R-92). |
| `08-agents-and-activity.png` | Activity log of engine events (stages, invocations, operations, stop) beside the agents tab. |
| `09-concept-approval-panel.png` | Concept tab: approval mode always visible, coverage line, an open request with its prompt and Copy button, the front candidate with both seats' verdicts beside the anchor (R-110). |
| `10-concept-anchor-request.png` | The anchor request right after `concept start` from text: prompt to paste, attachments none, CLI equivalent. |
| `11-concept-anchor-candidates.png` | Two imported anchor candidates with declared vendor/model (not verified), awaiting the owner's pick in `each` mode. |

`screenshot-log.txt`, when present, is the capture script's log.

These screenshots show the legacy Tk window (branch `legacy-tk`, checkout `C:\Alloy-old`); the modern app shows the same panels in its Model Builder view.
