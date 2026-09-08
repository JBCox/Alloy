# Kickoff prompt: Collaborative Model Builder, Phase 2a

Paste everything below the line into a fresh Claude Code session started in `C:\Alloy`.

---

Continue the Collaborative Model Builder in this repo (`C:\Alloy`). Phase 0 and Phase 1 are done; this session is Phase 2a.

Read these completely before doing anything else, in this order:
1. `docs/specs/collaborative-model-builder.md` (the base spec; cite its R-numbers)
2. `docs/specs/collaborative-model-builder-addendum-concept-stage.md` (Addendum A: concept stage, seats, roles; Phase 2b, not this session, but it constrains the adapter design)
3. `docs/plans/2026-09-07-collaborative-model-builder-design.md` (the design; Section 1 has the verified tool facts, Section 6 the adapter contract, Section 12a what Phase 1 built, Section 14 the concept-stage design)

## State of the repo

- `builder/` is the Phase 1 package: engine, records, store, state machines, operations, ownership, render manifests, evidence packets, schemas, limits, references, fixture, CLI, Blender runner and scripts, provider base and scripted mocks. `tests/` has 152 tests (133 deterministic, 19 real Blender). Run them with `python -m pytest tests -q` (about five minutes with Blender).
- Entry points: `python -m builder <verb>` and `python main.py --build <verb>`. `python -m builder fixture create <dir>` builds the disposable fixture; `tests/_screenplay.py` writes a mock screenplay.
- Hooks already exist in `main.py`, `config.py`, `config.yaml` (the `builder:` section with the E10 Bearer preset), and `requirements.txt`. All tracked files are LF under `core.autocrlf=true`; keep them LF and never let git rewrite the tree.
- The working tree also holds unrelated uncommitted changes and untracked files (including a stray `nul`). Preserve all of it.

## Scope of Phase 2a

1. Real provider adapters: `builder/providers/claude.py`, `gemini.py`, `codex.py`, following Section 6.3 of the design. Argv only. Never `--continue`, `-c`, `--last`, `--fallback-model`, `--ephemeral`, `--no-session-persistence`, `--bare`, or any bypass flag. Prompts on stdin. Explicit session UUIDs. Model and reasoning from `config.yaml` (`fable` with `--effort max`; `gpt-6-astra` with `-c model_reasoning_effort=xhigh`, probing `high` if `xhigh` is rejected and reporting it; gemini has no effort flag, shown as "not exposed").
2. Three-tier preflight per agent (declared, local from `--help`, live probes): image probe (R-16), write-fail probe (R-17), session create and resume with a nonce (R-10), structured output and repair (R-5, R-24), usage field capture (R-25, read the real field names at runtime, unknown when absent), cancellation with process-tree confirmation (R-22). Cache keyed on CLI path, version, model, settings (R-18).
3. Per-part close-up views and crops in the review and verification packets (R-61, R-65), so findings about small elements have adequate-resolution evidence. The engine currently renders four component views and two whole-model views framed from measured bounding boxes.
4. Roles: extract the assignment rules into `builder/roles.py` per Section 14 (no behavior change for the existing loop; the seat that made an operation never reviews or verifies it).
5. A bounded live smoke test on the fixture with the real agents, only after I approve scope and budget in this chat. Before asking, report the exact invocations you would run, the expected number of provider calls, and which caps are enforceable.

Not this session: the concept stage and image generation (Phase 2b), the GUI (Phase 3), packaging and the efficiency harness (Phase 4).

## Authorization

Allowed: implementing and testing with fixtures, mocks, and read-only inspection of the installed CLIs (`claude --help`, `gemini --help`, `codex exec --help`, official docs). Local, no-cost checks of the CLIs (version, help, argument rejection) are fine. Creating new files under `builder/`, `tests/`, `docs/`.

Not allowed: reading, rendering, copying, or modifying anything under `C:\Death Factory`; any live provider invocation that spends money before I approve scope and budget; `git commit`, `git stash`, `git checkout --`, `git clean`, reformatting, or changing line endings; touching existing files beyond the minimal hooks the spec allows.

## Working rules

- Test first: write the failing deterministic test, then the code. Real-Blender tests are marked `blender`. Live tests are marked `live`, opt-in, and skipped by default with a stated reason.
- Windows is the target: paths with spaces and Unicode, argv lists, UTF-8 everywhere, process-tree kills confirmed.
- Never invent flags, model identifiers, reasoning levels, usage field names, or prices. Sources are `--help`, the CLIs' own JSON output, and official documentation; otherwise say "unknown".
- Bash heredocs on this machine halve doubled backslashes; write scripts with the Write tool and run them. Console stdout is cp1252; write UTF-8 files and reconfigure stdout.
- Stop and report when Phase 2a is done, when a required capability is missing, when a limit is hit, or when your context is about 70 percent used. Never lower a standard to declare success.

## Report format at every stop

1. What was built: files and entry points.
2. Architecture summary and key tradeoffs, kept short.
3. Launch instructions.
4. Test results by layer (deterministic, real Blender, CLI interaction, live provider) with actual command output, plus an explicit list of what was not verified and why.
5. Fixture artifacts and screenshots where they exist.
6. Remaining limitations and open questions.
7. Confirmation that nothing under `C:\Death Factory` was read or modified, shown as unchanged size and modification time, checked with `stat` without opening the files. Baseline: `locked-theme-concept-v1.png` 2,661,938 bytes, modified 2026-08-28 21:56:25 -0500; `E10_Bearer_v4_proportions.blend` 1,716,114 bytes, modified 2026-09-07 11:18:24 -0500.
