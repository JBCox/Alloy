# Kickoff prompt: Collaborative Model Builder, Phase 2b (concept stage)

Paste everything below the line into a fresh Claude Code session started in `C:\Alloy`.

---

Continue the Collaborative Model Builder in this repo (`C:\Alloy`). Phases 0, 1, and 2a are done and committed on `master` (the commit whose subject starts with "Collaborative Model Builder: Phases 1 and 2a"; see `git log -1`); this session is Phase 2b, the concept stage.

Read these completely before doing anything else, in this order:

1. `docs/specs/collaborative-model-builder.md` (the base spec; cite its R-numbers)
2. `docs/specs/collaborative-model-builder-addendum-concept-stage.md` (Addendum A: this phase's requirements R-94 to R-110, decisions D10 to D13, test matrix A.4)
3. `docs/plans/2026-09-07-collaborative-model-builder-design.md` (the design; Section 14 is the concept-stage design you implement; Section 12b records what Phase 2a built and every live finding, items 15 and 16 in particular; Section 6 the adapter contract)

State of the repo

* `builder/` holds the engine, records, store, state machines, operations, ownership, render manifests and per-part close-ups, evidence packets with crops, schemas, limits, references, roles (`builder/roles.py`), fixture, CLI, Blender runner and scripts, and the real provider adapters (`builder/providers/claude.py`, `codex.py`, `gemini.py` on `cli_common.py`, plus scripted mocks). `tests/` has 202 tests: 181 deterministic (3 read the installed CLIs locally with `--help` and `--version` only), 20 real Blender (marker `blender`), 1 live (marker `live`, skipped unless `ALLOY_LIVE=1`). Run them with `python -m pytest tests -q -m "not live"` (about six and a half minutes with Blender).
* Entry points: `python -m builder <verb>` and `python main.py --build <verb>`. `python -m builder fixture create <dir>` builds the disposable fixture. `preflight <wf> --live` spends provider usage and is the only path that runs live probes; `start` refuses to probe implicitly. `tests/_fake_cli.py` emulates the three CLIs' documented output shapes at no cost, and `tests/conftest.py` blocks any real provider spawn in a test not marked `live`.
* Live facts (design 12b items 15 and 16): claude `fable` with `--effort max` is verified on every capability; codex `gpt-6-astra` at `xhigh` is verified with the Codex desktop app's binary `C:\Users\joshu\.codex\plugins\.plugin-appserver\codex.exe` (0.153.1), because the npm `codex` 0.147.0 on PATH is refused by the API for that model (set `builder.agents.B.executable` for live runs; npm has 0.153.4, not installed); gemini 0.55.1 aborts under the owner's account ("IneligibleTierError", migrate to Antigravity), so the gemini adapter is unverified and gemini must not be planned as a seat. Costs: a claude preflight probe is 0.5 to 0.8 USD, an intake or brief call 2 to 3 USD (the CLI's own estimates); codex reports no cost. OpenAI structured output needs the strict schema form (`cli_common.strict_schema`); a new Alloy schema must stay expressible in it (no free-form objects, single types, every object closed).
* The working tree also holds unrelated uncommitted changes in `main.py`, `config.py`, `config.yaml`, `requirements.txt`, `gui/`, `orchestrator.py`, `prompts.py`, `ai_installer.py`, `alloy.bat`, and untracked `actions.py`, `context.py`, `invocation.py`, `sessions.py`, `tools.py`, and a stray `nul`. Preserve all of it. All tracked files are LF under `core.autocrlf=true`; keep them LF and never let git rewrite the tree.

Scope of Phase 2b (Addendum A.5, design Section 14)

1. `builder/concept.py`: canon states and precedence (R-94, R-95, conflicts recorded as `evidence_conflict`, never averaged), generation requests and manifests written for success and failure (R-96), originals unmodified under `refs/generated/` (R-97), approval as a recorded user action or auto-approval under the mode in force (R-98, D11), the canon state machine (`no_canon` to `complete`, added to `state.py` as the fifth machine with the intake guard), coverage of the needed set with an explicit `proceed` (R-103), on-demand studies attached to parts with lower precedence (R-104), limits `max_images` and `max_regenerations_per_view` and cost recorded as not applicable for the manual seat (R-105).
2. `builder/providers/imagegen/base.py` and `manual.py`: the manual image seat (D12): prompts out as `concept/requests/<id>/PROMPT.md` with the attachment list, imports in through `concept import` with declarations recorded as declarations (R-96a), `--as-anchor` and `--as-view`, optional `--watch`. No API key handling in this phase; keep the contract so an API seat can be added later with keys from the environment only.
3. Roles: art director on seat A with rotation after two rejected rounds (R-108), independent consistency verdicts from both LLM seats before either sees the other's (R-102), every assignment with a one-line rationale (R-107). `builder/roles.py` already has the table and rules; wire them.
4. The concept loop from text and from seed images (R-99 to R-103, R-106): anchor candidates (default 4), canon description (R-101, versioned), per-view generation conditioned on the anchor, verdicts, regenerate or escalate, coverage check, hand-off to intake.
5. Preflight for the image seat (R-109): declared, local (import directory writable, flow confirmed once), live not applicable for the manual seat, reported as such.
6. CLI verbs per R-110 (`concept start | prompts | import | list | show | approve | reject | regenerate | study | proceed`), with the approval mode shown at all times.
7. Records and schemas: `reference` gains `canon_state`, `derived_from`, `generation_id`, `precedence`; new kinds `generation`, `canon_description`, `evidence_conflict`, `study_request`, `concept_plan`; agent output schemas for the art director prompt, the canon description, and the consistency verdict (strict-form compatible).
8. Tests per Addendum A.4, all deterministic with a mock image seat that imports prepared PNGs (the fixture's truth renders are fine) through the real `concept import` path. One owner-driven end-to-end pass with real images the owner generates in ChatGPT or Gemini, and consistency verdicts from the real LLM seats, only after the owner approves scope and budget in this chat. Before asking, report the exact invocations, the expected number of provider calls, the expected cost from the figures above, and which caps are enforceable.

Carry-over follow-ups from Phase 2a, do them if they fit without displacing the scope above, else list them: an activity signal for claude during long calls (`--output-format stream-json`, so the inactivity timer can be shorter than the response timeout); a close-up aligned to the finding's own view in verification packets; counting preflight probes toward the monetary cap; a decision from the owner on upgrading the global codex package.

Not this session: the GUI approval panel (Phase 3), packaging and the efficiency harness (Phase 4), any API image seat.

Authorization

Allowed: implementing and testing with fixtures, mocks, and read-only inspection of the installed CLIs; local, no-cost checks; creating new files under `builder/`, `tests/`, `docs/`; editing files under `builder/`, `tests/`, `docs/` and the `builder:` section of `config.yaml`.
Not allowed: reading, rendering, copying, or modifying anything under `C:\Death Factory`; any live provider invocation before the owner approves scope and budget in this chat; installing or upgrading global packages; `git commit`, `git stash`, `git checkout --`, `git clean`, reformatting, or changing line endings, unless the owner asks; touching existing Alloy files beyond the hooks the spec allows.

Working rules

* Test first: write the failing deterministic test, then the code. Real-Blender tests are marked `blender`. Live tests are marked `live`, opt-in, and skipped by default with a stated reason.
* Windows is the target: paths with spaces and Unicode, argv lists, UTF-8 everywhere, process-tree kills confirmed.
* Never invent flags, model identifiers, reasoning levels, usage field names, vendor names, or prices. Sources are `--help`, the CLIs' own output, and official documentation; otherwise say "unknown". A generated image is never evidence of a pre-existing design (R-32); vendor and model of manual imports are declarations.
* Bash heredocs on this machine halve doubled backslashes; write scripts with the Write tool and run them. Console stdout is cp1252; write UTF-8 files and reconfigure stdout.
* Stop and report when Phase 2b is done, when a required capability is missing, when a limit is hit, or when your context is about 70 percent used. Never lower a standard to declare success.

Report format at every stop

1. What was built: files and entry points.
2. Architecture summary and key tradeoffs, kept short.
3. Launch instructions, including the manual generate-and-import flow step by step.
4. Test results by layer (deterministic, real Blender, CLI interaction, live provider) with actual command output, plus an explicit list of what was not verified and why.
5. Fixture artifacts and screenshots where they exist.
6. Remaining limitations and open questions.
7. Confirmation that nothing under `C:\Death Factory` was read or modified, shown as unchanged size and modification time, checked with `stat` without opening the files. Baseline: `locked-theme-concept-v1.png` 2,661,938 bytes, modified 2026-08-28 21:56:25 -0500; `E10_Bearer_v4_proportions.blend` 1,716,114 bytes, modified 2026-09-07 11:18:24 -0500.
