# Kickoff prompt: Collaborative Model Builder, Phase 3 (GUI) with the optional live concept pass first

Paste everything below the line into a fresh Claude Code session started in `C:\Alloy`.

---

Continue the Collaborative Model Builder in this repo (`C:\Alloy`). Phases 0, 1, 2a, and 2b are done and committed on `master` (the commit whose subject starts with "Collaborative Model Builder: Phase 2b"; see `git log -1`). This session is Phase 3, the GUI, preceded by the owner-driven live concept pass that Phase 2b left pending.

Read these completely before doing anything else, in this order:

1. `docs/specs/collaborative-model-builder.md` (the base spec; cite its R-numbers; R-91 and R-92 are this phase)
2. `docs/specs/collaborative-model-builder-addendum-concept-stage.md` (Addendum A; R-110's approval panel is this phase)
3. `docs/plans/2026-09-07-collaborative-model-builder-design.md` (the design; Section 9.2 is the GUI design; Section 12c records what Phase 2b built, its decisions, and the live proposal; Section 12b items 15 and 16 hold the live provider facts)

State of the repo

* `builder/` holds the engine, records, store, five state machines (execution, review, acceptance, stop reason, canon), operations, ownership, renders with per-part close-ups (plus close-ups aligned to a finding's view in verification packets), evidence packets, schemas, limits (preflight probe spend counts toward the cap), references with canon states and precedence, roles, the concept stage (`builder/concept.py`), the manual image seat (`builder/providers/imagegen/`), the real provider adapters, the fixture, the CLI, and the Blender runner. `tests/` has 248 tests: 227 deterministic (3 read the installed CLIs locally with `--help` and `--version` only), 21 real Blender (marker `blender`), 1 live (marker `live`, skipped unless `ALLOY_LIVE=1`). Run them with `python -m pytest tests -q -m "not live"` (about seven minutes with Blender).
* Entry points: `python -m builder <verb>` and `python main.py --build <verb>`. Concept verbs: `concept start | prompts | import | list | show | approve | reject | regenerate | study | proceed | abandon | failed`; every one prints the approval mode first. `preflight <wf> --live` is the only path that spends provider usage on probes; `start` and the concept verbs refuse to probe implicitly. `tests/_fake_cli.py` emulates the CLIs at no cost; `tests/conftest.py` blocks any real provider spawn in a test not marked `live`.
* Live facts (design 12b items 15 and 16): claude `fable` with `--effort max` is verified; codex `gpt-6-astra` at `xhigh` is verified only with the Codex desktop app's binary `C:\Users\joshu\.codex\plugins\.plugin-appserver\codex.exe` (set `builder.agents.B.executable`); gemini is unusable under the owner's account and must not be planned as a seat. Costs by the CLI's own estimates: a claude preflight probe 0.5 to 0.8 USD, an intake or brief call 2 to 3 USD; codex reports no cost. OpenAI structured output needs the strict schema form (`cli_common.strict_schema`).
* The working tree also holds unrelated uncommitted changes in `main.py`, `config.py`, `config.yaml` (outside the `builder:` key), `requirements.txt`, `gui/`, `orchestrator.py`, `prompts.py`, `ai_installer.py`, `alloy.bat`, and untracked `actions.py`, `context.py`, `invocation.py`, `sessions.py`, `tools.py`, and a stray `nul`. Preserve all of it. All tracked files are LF under `core.autocrlf=true`; keep them LF and never let git rewrite the tree.

Scope of this session

1. Owner-driven live concept pass (Addendum A.4, layer U; design 12c has the proposal). Before spending anything, report the exact invocations, the expected number of provider calls, the expected cost from the figures above, and which caps are enforceable, then wait for the owner's approval in this chat. The owner generates the images in ChatGPT or Gemini; consistency verdicts come from the real LLM seats. Record every live finding in design Section 12c the way 12b items 15 and 16 record theirs.
2. Phase 3 per base spec Phase 3 and design Section 9.2: `gui/builder_view.py` (`BuilderWindow`, a `tk.Toplevel`) connected to the engine through a worker thread and a `queue.Queue` polled with `after`, the same pattern as `gui/app.py`; the panels of R-91 (intake, agents and capability status with the effective reasoning setting, stage and ownership, part tree with relations, reference and render comparison with zoom and selection, before and after, measurements toggle, observed and inferred labels, findings linked to parts and images, coverage, consumption and limits, controls for correction, acceptance, pause and resume, checkpoints); R-92 (actual artifacts labelled with revision and render ids, aspect preserved, presentation alignment distinct from altered evidence, responsive during provider and Blender work); the concept approval panel of R-110 (open requests with copyable prompts, candidates side by side with the anchor, both seats' verdicts, approve, reject, regenerate, the approval mode always visible); attended and unattended controls.
3. Hooks allowed by the design (Section 9.4): one `add_command` in `gui/app.py` for the builder view and a Builder tab in `gui/settings.py` that proves the `builder` key (including `concept` and `image_generation`) survives a save. Touch nothing else in the existing GUI.
4. Tests: deterministic tests for the view's engine binding through the queue with mock adapters and the fake runner (no Tk main loop where avoidable; where Tk is needed, create a hidden root and skip with a reason when no display is available); screenshots of the real window on the fixture project for the report.

Not this session: packaging and the efficiency harness (Phase 4), any API image seat, changes to the engine beyond what the view needs to observe state.

Authorization

Allowed: implementing and testing with fixtures, mocks, and read-only inspection of the installed CLIs; local, no-cost checks; creating new files under `builder/`, `gui/` (only `gui/builder_view.py` and tests for it), `tests/`, `docs/`; editing files under `builder/`, `tests/`, `docs/`, the `builder:` section of `config.yaml`, and the two hooks in `gui/app.py` and `gui/settings.py`.
Not allowed: reading, rendering, copying, or modifying anything under `C:\Death Factory`; any live provider invocation before the owner approves scope and budget in this chat; installing or upgrading global packages; `git commit`, `git stash`, `git checkout --`, `git clean`, reformatting, or changing line endings, unless the owner asks; touching existing Alloy files beyond the hooks above.

Working rules

* Test first: write the failing deterministic test, then the code. Real-Blender tests are marked `blender`. Live tests are marked `live`, opt-in, and skipped by default with a stated reason.
* Windows is the target: paths with spaces and Unicode, argv lists, UTF-8 everywhere, process-tree kills confirmed. The Tk thread never blocks on provider or Blender work.
* Never invent flags, model identifiers, reasoning levels, usage field names, vendor names, or prices. Sources are `--help`, the CLIs' own output, and official documentation; otherwise say "unknown". A generated image is never evidence of a pre-existing design (R-32); vendor and model of manual imports are declarations.
* Bash heredocs on this machine halve doubled backslashes and a path ending in a backslash before a closing quote breaks the command; write scripts with the Write tool into the scratchpad and run them. Console stdout is cp1252; write UTF-8 files and reconfigure stdout.
* Stop and report when Phase 3 is done, when a required capability is missing, when a limit is hit, or when your context is about 70 percent used. Never lower a standard to declare success.

Report format at every stop

1. What was built: files and entry points.
2. Architecture summary and key tradeoffs, kept short.
3. Launch instructions for the GUI and the concept approval panel.
4. Test results by layer (deterministic, real Blender, CLI and GUI interaction with screenshots, live provider) with actual command output, plus an explicit list of what was not verified and why.
5. Fixture artifacts and screenshots where they exist.
6. Remaining limitations and open questions.
7. Confirmation that nothing under `C:\Death Factory` was read or modified, shown as unchanged size and modification time, checked with `stat` without opening the files. Baseline: `locked-theme-concept-v1.png` 2,661,938 bytes, modified 2026-08-28 21:56:25 -0500; `E10_Bearer_v4_proportions.blend` 1,716,114 bytes, modified 2026-09-07 11:18:24 -0500.
