# Phase 4 reports (2026-09-08)

Real `BuilderWindow` (`gui/builder_view.py`) captured with Pillow's `ImageGrab` on this machine, opened on the workflow
that `python -m builder harness run <dir>` produced on real Blender 5.2.1 with the scripted seats (A builds, B finds and
corrects the floating bracket, A verifies; every render and measurement is a real backend artifact). Nothing under
`C:\Death Factory` was read. The floating "Claude" thumbnail at the bottom right of some captures is the desktop app's
own overlay, not part of the window.

| file | what it shows |
|---|---|
| `12-region-drag-rubber-band.png` | Project tab, "Draw region on reference" armed: the dashed rubber band during a real pointer drag on the reference preview (R-26). |
| `13-region-stored-in-original-pixels.png` | The stored region drawn on the reference and named in its label with original-pixel coordinates `[59, 80, 271, 340]` although the preview is scaled to 47 percent; the region is a child row of the reference in the References list (R-27). |
| `14-run-tab-limits-card.png` | Run tab scrolled to the Limits card: every limit in force with zero meaning unlimited, the enforceability note for `max_cost_usd`, and Apply (R-85, R-89). |
| `15-limits-applied.png` | After changing `max_requests` to 40 and `stall_steps` to 20 and pressing Apply: the snapshot returned by the worker shows the new values in the Consumption card; the change was journaled as `run.limits_changed`. |
| `16-concept-tab-wider-panes.png` | Concept tab with the wider anchor/candidate pair and the 16/22/62 column split. |
| `17-activity-with-limits-event.png` | Activity log with the `limits` event from the apply. |
| `harness-report-real-blender.txt` / `.json` | The R-84 efficiency comparison of that run: packet delivery measured (9 modeling invocations, 17,779,786 bytes, 128 images), the simulated shared-transcript replay (66,280,167 bytes, 464 images; nothing sent), preflight probes reported apart, tokens unknown (scripted seats report none), the withheld material a transcript would have exposed. No percentage, no equivalence claim. |

`screenshot-log.txt` is the capture script's log (image means prove the screen was unlocked; the stored region and the applied limits are printed from the live snapshot).

These screenshots show the legacy Tk window (branch `legacy-tk`, checkout `C:\Alloy-old`); the modern app shows the same panels in its Model Builder view.
