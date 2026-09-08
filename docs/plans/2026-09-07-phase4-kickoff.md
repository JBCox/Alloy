# Kickoff prompt: Collaborative Model Builder, Phase 4 (hardening, harness, docs, packaging), with the live concept pass first

Paste everything below the line into a fresh Claude Code session started in `C:\Alloy`.

---

Continue the Collaborative Model Builder in this repo (`C:\Alloy`). Phases 0, 1, 2a, 2b, and 3 are done and committed on `master` (the commit whose subject starts with "Collaborative Model Builder: Phase 3"; see `git log -1`). This session is Phase 4, preceded by the owner-driven live concept pass that Phases 2b and 3 left pending.

Read these completely before doing anything else, in this order:

1. `docs/specs/collaborative-model-builder.md` (the base spec; cite its R-numbers; Phase 4 covers R-47, R-84 to R-89, Section 8 documentation, and packaging)
2. `docs/specs/collaborative-model-builder-addendum-concept-stage.md` (Addendum A; concept-stage documentation belongs to Phase 4)
3. `docs/plans/2026-09-07-collaborative-model-builder-design.md` (the design; Section 12b items 15 and 16 hold the live provider facts; Section 12c holds the concept stage and, at its end, the revised live proposal with today's call counts, costs, and caps; Section 12d records what Phase 3 built)

State of the repo

* `builder/` holds the engine, records, store, five state machines, operations, ownership, renders with close-ups, evidence packets, schemas, limits, references with canon states, roles, the concept stage, the manual image seat, the real adapters (claude, codex; gemini is unusable under the owner's account), the fixture, the CLI, the Blender runner, and `builder/viewmodel.py` (the Tk-free read model `snapshot()` plus `BuilderSession`, a worker thread publishing events and snapshots on a queue). `gui/builder_view.py` is the Tk window (`BuilderWindow`, opened from View > Model Builder... or `python -m gui.builder_view <workflow_dir> [screenplay.json]`), and `gui/settings.py` has a Builder tab. `tests/` has 266 tests: 245 deterministic (3 read the installed CLIs with `--help` and `--version` only; 19 need a display and skip with a reason otherwise), 20 real Blender (marker `blender`), 1 live (marker `live`, skipped unless `ALLOY_LIVE=1`). Run them with `python -m pytest tests -q -m "not live"` (about eight minutes with Blender). Screenshots of the window are under `docs/reports/phase3/`.
* Entry points: `python -m builder <verb>` and `python main.py --build <verb>`; the GUI through `python main.py --gui`. `preflight <wf> --live` is the only path that spends provider usage on probes; `start`, the concept verbs, and the GUI refuse to probe implicitly (the GUI's live preflight is behind a confirmation dialog).
* Live facts (design 12b items 15 and 16): claude `fable` with `--effort max` is verified; codex `gpt-6-astra` at `xhigh` is verified only with the Codex desktop app's binary `C:\Users\joshu\.codex\plugins\.plugin-appserver\codex.exe` (0.153.1; set `builder.agents.B.executable`); the npm codex on PATH (0.147.0) is refused by the API; gemini must not be planned as a seat. Costs by the CLI's own estimates: a claude preflight probe 0.5 to 0.8 USD, an intake or brief call 2 to 3 USD; codex reports no cost. Both caps act after the spend (12b item 16), so the tracker refuses to dispatch when the remaining budget is below the agent's largest reported call.
* The working tree also holds unrelated uncommitted changes in `main.py`, `config.py`, `config.yaml` (outside the `builder:` key), `requirements.txt`, `gui/app.py`, `gui/settings.py`, `gui/styles.py`, `gui/wizard.py`, `orchestrator.py`, `prompts.py`, `ai_installer.py`, `alloy.bat`, and untracked `actions.py`, `context.py`, `invocation.py`, `sessions.py`, `tools.py`, and a stray `nul`. Preserve all of it. All tracked files are LF under `core.autocrlf=true`; keep them LF and never let git rewrite the tree. Phase 3 staged only its own hunks of `gui/app.py` and `gui/settings.py`; do the same for any file that mixes the owner's edits with yours.

Scope of this session

1. Owner-driven live concept pass (Addendum A.4, layer U). The revised proposal at the end of design Section 12c is the plan: report its exact invocations, call counts, expected cost, and enforceable caps again in this chat, then wait for the owner's approval before spending anything. Use a scratchpad config passed with `--config` so `config.yaml` stays untouched. The owner generates the images in ChatGPT or Gemini; the consistency verdicts come from the real LLM seats. Record every live finding in design Section 12c the way 12b items 15 and 16 record theirs, including the measured cost of prompt-writing and verdict calls, which have never been measured live.
2. Phase 4 per base spec Phase 4: recovery hardening (R-47: restart at edit, save, commit boundaries on real Blender, `test_blender_recovery.py` from the design's test plan; duplicate-operation prevention after uncertain outcomes), limits and stalls (R-85 to R-89: wall clock and stall detection, the attempt-limit reassessment on the fixture, limits shown and editable in the GUI Run tab), the efficiency comparison harness (R-84: `builder/harness.py`, the same fixture under transcript replay and packet delivery, reporting delivered bytes and tokens next to preserved evidence and coverage, no invented percentages, no quality-equivalence claims without evidence), the claude activity signal candidate from design 12c item 14c only if a paid probe is approved, documentation per spec Section 8 plus the concept stage (a `docs/builder/` set: installation and Blender discovery, launching from CLI and GUI, selecting and verifying agents, reference labelling and target regions, concept stage and approval modes, assignments and handoffs, inspection, findings and acceptance, pause, resume, recovery, checkpoints, unattended limits, token-efficiency behaviour and usage reporting, output layout, known limitations and unverified integrations), and packaging (`requirements.txt` already lists Pillow; `alloy.spec`: remove `PIL` from excludes, add the `builder` package and `builder/blender/scripts/*.py` as data; a launch script for the builder window). PyInstaller is not installed; do not install it globally; a build is verified only if the owner asks and provides it.
3. GUI follow-ups from the Phase 3 review, all inside `gui/builder_view.py` and `builder/viewmodel.py`: reference target-region selection by dragging on the image (R-26, R-27, stored in original pixels), limits editing on the Run tab, a wider default for the Concept tab's image pair, and any visual polish the owner asks for in this chat. Touch nothing else in the existing GUI.
4. Tests: deterministic tests first for every engine change; real-Blender recovery tests marked `blender`; the harness covered by tests that assert what it measures, never a saving; Tk tests through the session-scoped `tk_root` fixture.

Not this session: any API image seat, remodeling anything real, migrating the GUI framework.

Authorization

Allowed: implementing and testing with fixtures, mocks, and read-only inspection of the installed CLIs; local, no-cost checks; creating new files under `builder/`, `docs/`, `tests/`, `scripts/` (launch scripts); editing files under `builder/`, `tests/`, `docs/`, `gui/builder_view.py`, `alloy.spec`, the `builder:` section of `config.yaml`, and `requirements.txt` for builder needs only.
Not allowed: reading, rendering, copying, or modifying anything under `C:\Death Factory`; any live provider invocation before the owner approves scope and budget in this chat; installing or upgrading global packages; `git commit`, `git stash`, `git checkout --`, `git clean`, reformatting, or changing line endings, unless the owner asks; touching existing Alloy files beyond the ones listed above.

Working rules

* Test first: write the failing deterministic test, then the code. Real-Blender tests are marked `blender`. Live tests are marked `live`, opt-in, and skipped by default with a stated reason.
* Windows is the target: paths with spaces and Unicode, argv lists, UTF-8 everywhere, process-tree kills confirmed. The Tk thread never blocks on provider or Blender work.
* Never invent flags, model identifiers, reasoning levels, usage field names, vendor names, or prices. Sources are `--help`, the CLIs' own output, and official documentation; otherwise say "unknown". A generated image is never evidence of a pre-existing design (R-32); vendor and model of manual imports are declarations.
* Bash heredocs on this machine halve doubled backslashes and a path ending in a backslash before a closing quote breaks the command; write scripts with the Write tool into the scratchpad and run them. Console stdout is cp1252; write UTF-8 files and reconfigure stdout. Screenshots need an unlocked screen; a black capture means the session was locked, and a Tk window that processes events can loop where the hidden-root tests do not (Phase 3 found a select-handler re-entrancy that way), so drive the real window at least once for anything new.
* Stop and report when Phase 4 is done, when a required capability is missing, when a limit is hit, or when your context is about 70 percent used. Never lower a standard to declare success.

Report format at every stop

1. What was built: files and entry points.
2. Architecture summary and key tradeoffs, kept short.
3. Launch instructions (CLI, GUI, harness, packaging).
4. Test results by layer (deterministic, real Blender, CLI and GUI interaction with screenshots, live provider) with actual command output, plus an explicit list of what was not verified and why.
5. Fixture artifacts, harness output, and screenshots where they exist.
6. Remaining limitations and open questions.
7. Confirmation that nothing under `C:\Death Factory` was read or modified, shown as unchanged size and modification time, checked with `stat` without opening the files. Baseline: `locked-theme-concept-v1.png` 2,661,938 bytes, modified 2026-08-28 21:56:25 -0500; `E10_Bearer_v4_proportions.blend` 1,716,114 bytes, modified 2026-09-07 11:18:24 -0500.
