# Collaborative Model Builder - Design

**Status:** Phase 0 deliverable, 2026-09-07. Written against spec `docs/specs/collaborative-model-builder.md` (Draft v1) and the working tree at HEAD `593df76` plus uncommitted changes.
**How to read:** Section 1 is the re-verification of spec Section 2 with drift. Sections 2 to 9 are the design. Section 10 maps tests to R-numbers. Section 11 records decisions. Section 12 is the phase plan. Section 13 lists the questions for the owner. Requirement citations use the spec's `R-nn` and `Dn` numbering.

---

## 1. Re-verification of spec Section 2 (2026-09-07)

Everything was checked on this machine with the commands listed in Appendix A. "Confirmed" means the spec statement holds. "Drift" means the spec statement is wrong or incomplete and the design relies on the corrected fact.

### 1.1 Runtime and tools

| Item | Verdict | Evidence |
|---|---|---|
| Python 3.14.0 | Confirmed | `C:\Python314\python.exe` |
| rich, prompt_toolkit, pyyaml, ruamel.yaml, Pillow, pytest, tkinter installed | Confirmed | rich 14.2.0, prompt_toolkit 3.0.52, PyYAML 6.0.3, ruamel.yaml 0.19.1, Pillow 12.0.0, pytest 9.0.2, Tk 8.6 |
| PyInstaller not installed; `alloy.spec` excludes `PIL` and `pytest` | Confirmed | `ModuleNotFoundError`; `excludes=[..., 'PIL', ..., 'pytest', ...]` |
| numpy | Drift (addition) | numpy 2.3.5 is installed but `alloy.spec` excludes it and D5 forbids new dependencies. The builder does not use numpy. |
| Blender 5.2.1 LTS at `C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`, not on PATH | Confirmed | `-b --version` prints `Blender 5.2.1 LTS (hash 9e2066aef7ef built 2026-08-25)`; `which blender` finds nothing |
| Bundled Python 3.13.13 | Confirmed | `sys.version` inside `--python-expr` |
| `--python-exit-code <code>` | Confirmed (verify item resolved) | Listed in `--help`; a raised exception exits 3 with `--python-exit-code 3` and **exits 0 without the flag**. The flag is mandatory on every Blender invocation. |
| Render engine identifiers | Confirmed (verify item resolved) | `blender -b -E help` lists `BLENDER_EEVEE`, `BLENDER_WORKBENCH`, `CYCLES`. The RNA enum in background mode lists only `BLENDER_EEVEE`, so runtime checks must use `-E help` or a set-and-read test, never the enum. |
| Headless renders | New fact | Workbench and EEVEE both render headless here (320x240 probe: 1.44 s and 2.67 s). Custom property `alloy_id` on objects and materials survives save and reopen. A `.blend` path containing spaces and non-Latin characters saves and reopens. |
| `Material.use_nodes` | New fact | Deprecated in 5.2 ("expected to be removed in Blender 6.0"). Scripts must not set it. |
| argv passthrough | New fact | `subprocess.run([...], shell=False)` delivers `C:\tmp dir\ü nï 名.json` and `a&b|c^d%e "q" 'sq'` to Blender's `sys.argv` unchanged after `--`. |
| Console encoding | New fact | Alloy's Python under this shell has `sys.stdout.encoding == 'cp1252'` (filesystem encoding UTF-8). The builder never relies on console encoding: files are UTF-8, stdout is reconfigured to UTF-8 with `errors="replace"` at CLI entry. |
| claude 2.1.233, gemini 0.55.1, codex 0.147.0 on PATH; copilot and ollama absent | Confirmed | `claude --version`, `gemini --version`, `codex --version`; `which copilot` and `which ollama` find nothing |
| Appendix A flags | Confirmed, with additions | See 1.3 |
| Bearer assets exist with stated sizes | Confirmed by `stat` only, never opened | PNG 2,661,938 bytes, mtime 2026-08-28 21:56:25 -0500; `.blend` 1,716,114 bytes, mtime 2026-09-07 11:18:24 -0500 |
| sqlite3 | New fact | SQLite 3.50.4 with JSON functions; WAL available |
| Process control | New fact | `taskkill`, `tasklist`, `powershell` present; `kernel32.CreateJobObjectW`, `AssignProcessToJobObject`, `TerminateJobObject` callable through ctypes; `subprocess.CREATE_NO_WINDOW` and `CREATE_NEW_PROCESS_GROUP` available |

### 1.2 Alloy architecture

| Spec statement | Verdict | Evidence |
|---|---|---|
| Providers are `shell=True` subprocesses with `{message}` substitution | Confirmed | `orchestrator.py:327` `command = ai_config.command.replace("{message}", escaped_message)`; `orchestrator.py:354`, `:238-240` |
| `_escape_message` quotes and escapes only `"` on Windows | Confirmed | `orchestrator.py:419-420` |
| `_execute` treats non-zero exit as success when there is output | Confirmed | `orchestrator.py:367-373` |
| All four query methods fall back and retry through `_execute_with_retry` | **Drift** | All four fall back (`:70`, `:100`, `:137`, `:204`), but only the two non-streaming methods retry (`:67`, `:134`). The streaming methods call `_execute_streaming` directly with no retry. |
| Single 300 s timeout; streaming applies it after stdout closes | Confirmed, plus drift | `orchestrator.py:32`, `:251-258`. Additionally `AIConfig.timeout` (`config.py:87`) is never read by the orchestrator. |
| `--continue` inserted before `-p`, tracked per AI name | Confirmed | `orchestrator.py:338-342`; `_ai_has_session` at `:38` |
| `sessions.py` unused | Confirmed | no `import sessions` anywhere |
| `main.py` line numbers and `main()` accepts only `--gui` and `--setup` | Confirmed | `query_single_ai` at 612, `_process_invocations` at 719, `run_roles` at 1096, argparse at 1499-1513 |
| Tools read-only under `ContextManager.root`; `[[RUN]]` is `shell=True`, 60 s, denylist; advertised only when `supports_tools` | Confirmed | `tools.py:243-250`, `context.py:155`, `prompts.py:107-108`, `config.py:40` |
| GUI `_run_mode` stub; no `--continue`; worker threads and `response_queue` polled by `after` | Confirmed | `gui/app.py:776-783`, `:72`, `:785-835` |
| `gui/settings.py` rewrites through `_collect_config` / `_merged_config` / `deep_merge` | Confirmed, plus detail | Unknown top-level keys are **preserved** on save (`deep_merge` iterates updates only; ruamel round trip at `:1112-1136`). `Config.save` (`config.py:503`) **drops** unknown keys but is reached only from the settings editor's "Reset to defaults" (`gui/settings.py:759`). |
| All standard prompts advertise `[[ASK:...]]` | **Drift** | Only the three environment templates do (`prompts.py:22`, `:54`, `:71`). `ModePrompts` templates contain no `[[ASK]]` or tool text. Irrelevant to the builder (D2), recorded for accuracy. |
| `modes.py` `ModeType`; `router.py` parses `@mode[opts] --flags`; handlers in `main.py` | Confirmed | `modes.py:8`, `router.py:36-45`, `main.py:857-867` |
| No tests; `.gitignore` has `.pytest_cache/` | Confirmed | no `tests/` directory, no `test_*.py` |
| Working tree: 12 modified, 5 untracked modules, `nul` | Confirmed | `git status --porcelain` matches; `wrappers/` package (ollama, groq) also exists and is tracked |
| `config.yaml` providers | Drift (addition) | Besides claude, gemini, codex, the file enables a fourth provider `ox` (`opencode run --agent plan ...`) and carries disabled entries (ollama, groq, pickle, nemotron, mimo, hy3, muse). Harmless to the builder. |
| Line endings | New fact | All tracked files are LF in the working copy while `core.autocrlf=true`; git warns "LF will be replaced by CRLF the next time Git touches it". Rule for this work: no git write operations, and every edit to an existing file keeps LF (checked with `file` after editing). |
| `invocation.py`, `context.py` | Confirmed | `invocation.py` parses `[[ASK]]` and is imported by `main.py:62` and `actions.py:22`; `context.py` holds `ContextManager` |

Reusable pieces found: `deep_merge` / `overwrite` in `gui/settings.py:36-68` (generic, dependency-free); the queue-to-Tk pattern in `gui/app.py:72, 785-835`; the encoding convention `text=True, encoding="utf-8", errors="replace"` with `NO_COLOR=1`, `PYTHONUNBUFFERED=1` in `orchestrator.py:243-247`. There is no reusable Windows process-tree kill code; `orchestrator.py:268-270` kills only `cmd.exe` and orphans the CLI.

### 1.3 CLI capabilities (Appendix A re-verified from `--help` and official docs)

**claude 2.1.233.** All Appendix A flags present. `--permission-mode` choices are `acceptEdits, auto, bypassPermissions, manual, dontAsk, plan`. Additional relevant flags: `--tools <list>` (restrict built-in tools, `""` disables all), `--strict-mcp-config`, `--setting-sources`, `--disable-slash-commands`, `--system-prompt`, `--input-format`. `--bare` exists but reads auth only from `ANTHROPIC_API_KEY`, never OAuth, so it is not used by default. Official headless docs (code.claude.com/docs/en/headless) state: stdin is read as prompt input (10 MB cap); `--output-format json` returns `result`, `session_id`, `total_cost_usd` (client-side estimate) and usage metadata; with `--json-schema` the structured result is in `structured_output`; `--resume <session_id>` works from any directory; a `-p` run without `--bare` loads project hooks and MCP servers from the working directory, so the agent working directory must be a directory Alloy controls. Exact `usage` sub-field names are not documented verbatim and are read at runtime (R-19, R-25).

**gemini 0.55.1.** All Appendix A flags present. `--resume` accepts `latest` or an index number per `--help` and the CLI reference; UUID acceptance is undocumented and must be probed in Phase 2. `--session-id <uuid>` starts a new session. `--session-file <json>` loads a session from a file (undocumented beyond `--help`). `-p` text is "appended to input on stdin (if any)", so the packet can travel on stdin. `-o json` returns `response`, `stats`, `error` (headless reference); `stats` field names are not documented and are read at runtime. `--approval-mode plan` is documented as read-only mode. `--policy` files exist (Policy Engine) and `--allowed-tools` is deprecated. No reasoning-effort flag.

**codex 0.147.0.** All Appendix A flags present. Additional: `--skip-git-repo-check` (required, because packet and scratch directories are not git repositories), `--ignore-user-config`, `--ignore-rules`, `--add-dir` (writable directories, therefore never used). Official docs (learn.chatgpt.com config reference and non-interactive mode): reasoning effort is config key `model_reasoning_effort` with values `minimal | low | medium | high | xhigh` ("xhigh is model-dependent"); `sandbox_mode` is `read-only | workspace-write | danger-full-access`; `approval_policy` is `untrusted | on-request | never | {granular...}`. `--json` emits JSONL events `thread.started` (field `thread_id`), `turn.started`, `turn.completed` (field `usage` with `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`), `turn.failed`, `item.started`, `item.completed`, `error`. Stdin is the full prompt when the positional is `-`. `codex exec resume <SESSION_ID>` accepts a UUID. The owner's `~/.codex/config.toml` sets `model = "gpt-6-astra"` and `model_reasoning_effort = "high"` (read 2026-09-07); the adapter still passes explicit `-m` and `-c model_reasoning_effort=...` on every invocation rather than inheriting, and preflight reports the effective values from the CLI's own output.

**Models (owner guidance, 2026-09-07).** The owner named GPT 6 and Fable 5.1 as the models suited to this work, GPT especially. Sourced identifiers: `claude --model fable` (alias documented in `claude --help`; the resolved full model name is read from the CLI's JSON output at preflight) and codex `gpt-6-astra` (the identifier configured locally). No other identifier is assumed; a rejected identifier is a preflight failure quoting the CLI's error.

Not verified (needs live runs, Phase 2 with approval): image delivery for each CLI, write-fail enforcement, gemini resume by UUID, exact usage field names, acceptance of `--effort max` and `model_reasoning_effort=xhigh` for the chosen models.

---

## 2. Module layout (D1, D2)

New package `builder/` plus `tests/`. Existing modules are touched only for the hooks listed in 9.4.

```
builder/
  __init__.py          version string, public API re-exports
  __main__.py          `python -m builder` -> cli.main
  cli.py               argparse subcommands (R-90); thin, calls Engine
  config.py            BuilderConfig: lenient parse of config.yaml['builder'] (D9), Blender autodetect
  ids.py               time-sortable IDs, sha256 helpers, canonical JSON hashing
  records.py           dataclass record types, validation, JSON (de)serialization, enums
  store.py             SQLite store (WAL), schema_version, records + history + journal + files tables,
                       append-only journal triggers, rebuild-from-journal
  state.py             the four state machines (R-4): transition tables, guards, journaled transition()
  project.py           project directory layout, create/open, presets (R-93), file registry
  references.py        reference intake, hashing, dimensions, regions, versions (R-26, R-27); image probe (R-16)
  inventory.py         parts, instances, assemblies, construction relations, dependencies, identity diff,
                       impact analysis (R-33 to R-39)
  ownership.py         ownership tokens, base-revision checks, handoff, stale rejection (R-41, R-44, R-45)
  operations.py        operation lifecycle: create, stage, run, validate, promote, reconcile (R-23, R-41, R-47)
  render.py            views, cameras, render manifests, cache keys, stale detection (R-60 to R-65, R-81)
  evidence.py          packet builder: task/review/intake packets, crops, "changed since" deltas (R-11, R-65, R-77, R-79)
  schemas.py           JSON Schemas for agent outputs + Python validators (R-5, R-24, R-73)
  prompts.py           packet text templates (framing per R-13, R-14); independent from Alloy's prompts.py
  engine.py            Engine: scheduler, improvement loop, gates, user actions, limits, recovery, checkpoints
  limits.py            consumption accounting, enforceable vs estimated, unknown handling (R-25, R-85 to R-87)
  fixture.py           disposable fixture project with controlled defects (Section 5)
  harness.py           efficiency comparison harness (R-84)            [Phase 4]
  blender/
    __init__.py
    runner.py          BlenderRunner: discovery, version, smoke, run script with deadline, staging paths
    scripts/           executed INSIDE Blender (bpy, Python 3.13); no Alloy imports; JSON args in, JSON result out
      _alloy_common.py   argv/JSON protocol, identity helpers, result writer
      smoke.py           version, python version, engine set/read test
      validate.py        reopen, identity map, NaN transforms, expected parts, deletions, duplicates, orphans
      apply_operation.py load base, exec screened agent script in controlled namespace, post-checks, save
      render.py          view -> PNG with recorded settings
      measure.py         bboxes, distances, ratios, overlap candidates (R-68)
      identities.py      enumerate alloy_id datablocks
      fixture_build.py   build truth model, defective model, and fixture references
  providers/
    __init__.py
    base.py            ProviderAdapter ABC, InvocationRequest/Result, Usage (Measured|Unknown), capability tiers
    process.py         argv subprocess runner: concurrent drains, three timers, Job Object tree kill + confirmation
    structured.py      schema validation, JSON extraction from text, one bounded repair round
    mock.py            scripted mock adapters ("screenplays") for D tests
    claude.py, gemini.py, codex.py                                     [Phase 2]
gui/
  builder_view.py      Tk view connected through worker thread + queue (R-91, R-92)   [Phase 3]
tests/
  conftest.py          tmp projects, mock adapters, `blender` marker with skip reason
  test_*.py            deterministic (D) tests
  test_blender_*.py    real-Blender (B) tests, marked `blender`
```

Everything that decides state lives in `engine.py`, `state.py`, `operations.py`, `ownership.py`, and `limits.py`. `cli.py` and `gui/builder_view.py` only call the engine API and render its events.

---

## 3. Records, store, and journal (D5, R-2, R-3, R-6)

### 3.1 Storage

`<workflow_dir>/builder.sqlite3`, `PRAGMA journal_mode=WAL`, `schema_version` in a `meta` table (starts at 1; migrations are explicit functions). Tables:

- `meta(key PRIMARY KEY, value)`: `schema_version`, `project_id`, `created_at`.
- `records(kind, id, version, state, parent_id, data JSON, created_at, updated_at, PRIMARY KEY(kind,id))`, indexes on `(kind,state)` and `(kind,parent_id)`. Current version of every record.
- `record_history(kind, id, version, data, changed_at, journal_seq)`: every superseded version (R-2 versioning, R-6 superseded acceptances remain visible).
- `journal(seq AUTOINCREMENT, ts, run_id, actor, actor_kind, event, record_kind, record_id, from_state, to_state, inputs JSON, evidence JSON, outcome, op_id, after JSON)`: append-only. `BEFORE UPDATE` and `BEFORE DELETE` triggers raise. `after` holds the full record snapshot written by the transition, which makes the journal sufficient to rebuild `records` (R-3). `store.rebuild_from_journal()` exists and is tested to reproduce the `records` table exactly.
- `files(path PRIMARY KEY, sha256, size, kind, record_id, registered_at)`: registry of workflow-owned files for hash-mismatch detection (R-45, R-63, R-64).
- `control_requests(seq, ts, kind, payload, consumed_at)`: pause/resume/cancel/feedback from another process (CLI `pause` while `start` runs in the foreground), consumed at safe boundaries (R-59).

Every transition is one SQLite transaction: journal insert, `records` upsert, `record_history` insert. File promotions happen before the transaction that records them, with a preceding journal entry announcing the intended file and hash, so recovery can classify a crash between the two (Section 8.5).

### 3.2 Record kinds

Each kind is a dataclass in `records.py` with `validate()`; the store refuses invalid records. IDs are `<prefix>_<time-sortable base32>`; prefixes: `run, ag, ps, inv, ref, reg, brief, obs, p, grp, rel, dep, t, own, op, rev, ck, cam, view, rnd, f, ca, acc, fb, wv, pf, cov, pk`.

| Kind | Key fields |
|---|---|
| project | name, asset_name, source_project_dir (recorded, never accessed until an authorized run), preset_id, settings snapshot |
| run | project_id, execution_state, stop_reason, started_at, ended_at, attended, limits snapshot, consumption (measured and estimated kept separate), current_task_id, active_op_ids, active_processes (pids) |
| agent | label (A or B), provider, model, reasoning, executable path, settings, preflight_report_id, scratch_dir, context_contains (record ids the persistent session has seen; drives R-11 isolation) |
| provider_session | agent_id, session_uuid (chosen by Alloy), kind (persistent, isolated_review), resume_supported, reconstruct_context, invocation_count, last_verified_revision_id |
| invocation | agent_id, session_id, purpose, packet_id, argv, cwd, started_at, ended_at, exit_code, stdout_log, stderr_log, outcome (ok, malformed, repaired, repair_failed, timeout, inactive, cancelled, crashed), usage (per field Measured or Unknown), cost (Measured, Estimated, or Unknown), reported_session_id, structured_output_path |
| reference | file, sha256, width, height, labels, kind (target, previous_attempt, rejected, hypothesis), version, replaces_id, notes |
| reference_region | reference_id, name, bbox in original pixel space, purpose (target_region, detail_crop), created_by |
| brief | R-29 fields, version, editable text plus structured sections |
| observation | agent_id, session_id, subject (intake or component id), evidence_versions, dependency_versions, content per R-28, valid flag with invalidation reason |
| part, part_group | R-35 fields, blender_ids (alloy_id list), instances, owner_agent_id, impl_state, review_coverage summary |
| relation | from_part, to_part, type (covers, sits_under, supports, passes_behind, recessed_in, attached_to, contacts, clearance), evidence, intended_contact flag (feeds R-68) |
| dependency | from (task or part), to_part, pinned_version |
| task | kind (blockout, observe, plan, build, revise, review, correct, verify, integrate, reassess), part_ids, owner_agent_id, rationale, expected_outcome, state, base_revision_id, dependency_versions, packet_id, op_ids, attempt counters |
| ownership | resource (assembly or component id), holder (agent id or engine), token, granted_at, base_revision_id, released_at, release_reason; partial unique index on open grants per resource |
| operation | task_id, agent_id, ownership_token, kind (apply_script, render, measure, validate, checkpoint, restore, integrate, fixture), expected_base_revision_id, intent, target_part_ids, expected_outcome, declared_effects, script_path, args_path, state, result_revision_id, validation_report, deadline_s, pid, timestamps |
| revision | parent_revision_id, file, sha256, size, created_by_op_id, identity_map, asset_dependencies (path, sha256), immutable |
| checkpoint | revision_id, name, reason, files (path, sha256), created_by, restorable |
| camera | params (location, rotation, lens, sensor, clip, type), alignment_assumptions, calibration_status (measured, hypothesis), matches_reference_id |
| view | name, camera_id, visibility (isolate, hide), lighting preset, mode (clay, material), engine, resolution, evidence_label (matched, inferred_construction) |
| render | manifest per R-63, file, sha256, status, cache_key, stale (computed), stale_reason |
| finding | R-73 fields, state (open, correction_planned, correcting, verify_pending, closed, waived, evidence_gap, reassess), attempts (correction_attempt ids), owner |
| correction_attempt | finding_id, task_id, op_ids, before_render_ids, after_render_ids, outcome (improved, unchanged, regressed, uncertain), rationale |
| acceptance | component_id, state, revision_id, evidence_versions, dependency_versions, user, at, superseded_by, supersede_reason |
| feedback | text, target, received_at, applied_at, boundary |
| waiver | finding_id, rationale, user, at |
| preflight_report | agent_id, cli_path, cli_version, help_hash, tiers per capability (declared, local, live) with evidence, cache_key (R-18) |
| coverage | part_id, view_id, region, revision_id, inspected_by (agent, invocation), instances_inspected, sampling_strategy (R-72) |
| packet | kind, agent_id, task_id, files (path, sha256, bytes, role), text_bytes, included record versions, withheld (what, why), token_estimate (Unknown unless measured) |

`Measured`, `Estimated`, and `Unknown` are distinct value wrappers in `records.py`; arithmetic on `Unknown` is refused, so an unknown cost can never become zero (R-25, R-85).

---

## 4. The four state machines (R-4)

Implemented in `state.py` as explicit tables `{(from, to): guard}`; `transition()` evaluates the guard, writes the journal entry, and updates the record atomically. An illegal transition raises `IllegalTransition` and writes nothing. No transition is triggered by agent text (R-1).

**Execution (per run):** `idle -> running` (start; preflight passed). `running -> waiting_for_provider` (invocation dispatched) `-> running` (result recorded, any outcome). `running -> rendering` (Blender op) `-> running`. `running -> waiting_for_user` (attended gate or material question; stop_reason `ready_for_user_review` or `missing_evidence`) `-> running` (user action). Any active state `-> paused` (pause requested, applied at the next safe boundary; stop_reason set). `paused -> running` (resume; limits re-checked). Any state `-> cancelled` (after in-flight operations are reconciled; stop_reason `user_cancel`). Any state `-> failed` (stop_reason `execution_failure`). On open: any non-idle persisted state `-> recovering -> running | paused | failed` (Section 8.5). Guard on every dispatch: no pause or cancel request pending, no limit reached (R-86).

**Review (per component):** `unreviewed -> findings_open` (reviewer's initial findings recorded, at least one open). `unreviewed -> ready_for_user_review` (review recorded with zero open findings, coverage requirements met, renders fresh). `findings_open -> changes_required` (engine decides to correct). `changes_required -> findings_open` (a verification closed some findings, others remain). `changes_required -> ready_for_user_review` (all findings closed or waived, renders fresh, coverage met, no pending operations). Any `-> unreviewed` on invalidation (R-39), keeping finding history linked.

**Acceptance (per component):** `unaccepted -> accepted_at_revision` (user action; binds component revision, evidence versions, dependency versions; R-6). `accepted_at_revision -> superseded` (new revision touching the component or a bound dependency changes; or user reopen with reason). `superseded` records stay; a new acceptance is a new record. `unaccepted` is the default; nothing is accepted by default (R-89).

**Stop reason (per run, set on stops, cleared on resume):** `user_pause, user_cancel, missing_evidence, stalled, attempt_limit, budget_limit, time_limit, execution_failure, ready_for_user_review`. Every stop writes the remaining open findings and uncertainties into the status record (R-89). `ready_for_user_review` and `accepted` are different machines and never conflated (R-76).

Sub-machines documented with the same mechanism: task (`pending, assigned, in_progress, awaiting_validation, done, cancelled, blocked`), operation (`created, staged, running, validating, promoting, committed, rejected, failed, uncertain, cancelled`), finding (Section 3.2).

---

## 5. Blender runner contract (Section 5, D4, D6, R-22, R-37, R-41, R-46, R-48)

**Discovery and preflight.** `builder.blender.executable` if set; otherwise the highest `C:\Program Files\Blender Foundation\Blender *\blender.exe`. Preflight records `-b --version`, runs `smoke.py` (prints Blender version, Python version, sets and reads back `BLENDER_WORKBENCH` and `BLENDER_EEVEE`), and parses `-E help`.

**Invocation.** Always an argv list, never a shell:

```
blender.exe -b --factory-startup -noaudio --python-exit-code 3 [<staged.blend>] --python <script.py> -- <args.json>
```

The `.blend` precedes `--python` so it is loaded before the script runs. Environment adds `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`, `NO_COLOR=1`. `creationflags=CREATE_NO_WINDOW`. The process is placed in a Windows Job Object at spawn. stdout and stderr are drained by threads into `logs/<op_id>.stdout.log` and `.stderr.log`. Each operation kind has its own deadline (defaults: validate 120 s, apply 600 s, render 900 s, measure 120 s, fixture 300 s; configurable). On deadline: `TerminateJobObject`, then `taskkill /T /F` fallback, then confirmation through `tasklist` until the PID and known descendants are gone; the operation becomes `failed(timeout)` or `uncertain` if a staged output exists.

**Result protocol.** The script writes `<staging>/<op_id>/result.json`: `{op_id, stage, ok, data, errors[], warnings[], blender_version, elapsed_s}`. Exit 3 means a Python exception (traceback in stderr log) and is `failed`. Exit 0 without `result.json` is `uncertain`, never success.

**Staging and promotion.** `staging/<op_id>/base.blend` is copied from `revisions/<base_rev>.blend` after its hash is re-verified against the `files` registry (R-45). `apply_operation.py` loads base, runs the agent script (below), saves `staging/<op_id>/out.blend` with `relative_remap=True` and records external asset dependencies (linked libraries, image file paths) with hashes; a missing external asset is a validation error (R-48). Validation runs in a **separate** Blender process on `out.blend` (this proves the file reopens): identity map, NaN or infinite transforms, expected parts present, unexpected deletions relative to the base identity map, duplicate `alloy_id`, unmapped mesh objects (allowed only when the operation declared `creates`), objects unlinked from every collection. A passing validation promotes: sha256, journal `op.promoting` with the intended revision id and hash, `os.replace` into `revisions/<rev_id>.blend`, Windows read-only attribute, then the `op.committed` transaction. Revision files are never modified afterwards.

**Identity.** Custom properties `alloy_id` (string) and `alloy_kind` (`part, instance, material, collection, feature`) on objects, collections, and materials. Names are ignored for identity (R-37). `identities.py` enumerates them; `inventory.diff()` reports deleted, duplicated, unmapped, and orphaned items after every operation.

**Agent scripts.** An agent's operation request carries the bpy source. Alloy writes it to `ops/<op_id>/script.py` and `apply_operation.py` executes it with `exec` in a namespace that offers `bpy` and an `ALLOY` helper (`get(alloy_id)`, `tag(datablock, part_id, kind)`, `ids()`), catching exceptions into `result.json`. Before dispatch, `operations.screen_script()` rejects scripts that import `os`, `subprocess`, `shutil`, `pathlib`, `sys`, `ctypes`, `socket`, or `urllib`, call `open(`, `exec(`, `eval(`, `__import__`, or use `bpy.ops.wm.save*`, `bpy.ops.wm.open*`, `bpy.ops.wm.link`, `bpy.ops.wm.append`, `bpy.data.libraries`. This screening is a guardrail, not a sandbox: the script runs with Blender's full Python. What enforces D4 is that the script only ever sees a staged copy, only Alloy saves, the authoritative revisions are immutable, and validation gates promotion. The limitation is stated in the docs and in preflight output.

**Renders.** `render.py` takes a view record (camera params, visibility by `alloy_id`, isolate or hide lists, lighting preset, mode), the revision file, resolution, color management (`Standard` view transform for evidence), sampling and seed, and writes `renders/<rnd_id>.png`. Clay uses `BLENDER_WORKBENCH` with studio lighting and a single flat color; materials use `BLENDER_EEVEE`. The script reports the settings actually applied (engine, version, samples, resolution, view transform), so the manifest records reality, not intent. Alloy computes the output hash and assembles the manifest (R-63). `cache_key = sha256(canonical_json(revision sha256, asset dependency hashes, view record version, camera, visibility, lighting, engine and Blender version, resolution, color management, seed, render script hash))`. A render is reused only when the key matches and the file hash still matches its manifest (R-81, R-64). Crops for detail inspection are produced by Pillow from the full-resolution render and carry their source render id and pixel rectangle (R-65).

**Measurements.** `measure.py` reports world-space bounding boxes of evaluated meshes, distances between tagged features (empties or objects with `alloy_kind=feature`), part dimensions and ratios, and overlap candidates. Bounding-box intersection is reported as a candidate only; a BVH overlap test (`mathutils.bvhtree`) is reported as "mesh overlap detected" with the caveat that declared `intended_contact` relations are excluded (R-68). Each result carries a `limitations` list.

**Fixture (Section 5).** `fixture_build.py` builds a small multi-part object (base plate, post, housing, bracket fitting, lens cap, four bolts as instances) twice: a truth model whose renders become the fixture's reference images (front, side, three-quarter, and a detail crop region), and a defective model saved as revision 0 with controlled defects: (1) housing depth scaled to 0.6 of truth, (2) bracket fitting floating 0.15 units above its mount, (3) lens material base color red instead of the reference blue, (4) a stale render whose manifest revision hash does not match revision 0. The fixture is generated into a temporary directory and never touches the Bearer files. The reference images are synthetic by construction and are labeled as such in the fixture project.

---

## 6. Provider adapter contract and preflight tiers (D3, R-15 to R-25)

### 6.1 Contract (`providers/base.py`)

`InvocationRequest`: agent_id, session (`new(uuid)`, `resume(uuid)`, `isolated(uuid)`), purpose, packet_dir (read-only evidence), cwd (the agent's scratch directory, which Alloy controls), prompt_text (stdin), framing_text (system-prompt append where supported), images (paths inside the packet), output_schema (dict), model, reasoning, timeouts (`response_s`, `inactivity_s`), budget (`max_cost_usd`, enforceable only where the CLI enforces it), env overrides.

`InvocationResult`: exit_code, stdout_log, stderr_log, structured (dict or None), raw_text, reported_session_id, usage (each of input, output, cached_input, image, reasoning, cost is `Measured` or `Unknown`), timings, outcome, cancelled, kill_confirmed, warnings.

Adapter methods: `build_argv(req)` (pure, unit-tested: no shell, no `{message}`, no fallback flags, no `--continue`, `--last`, `-c`, `--ephemeral`, `--no-session-persistence`), `stdin_payload(req)`, `parse_result(...)`, `declared_capabilities()`, `preflight_local()`, `preflight_live(probes)`, `cancel(handle)`. Adapters never choose a different provider or model (R-20): a rejected model or reasoning value is a preflight failure with the CLI's own error text.

### 6.2 Process layer (`providers/process.py`, shared with the Blender runner)

`run_process(argv, stdin_bytes, cwd, env, response_s, inactivity_s, cancel_event, on_output)`: `Popen(list, shell=False, stdin=PIPE, stdout=PIPE, stderr=PIPE, creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)`, Job Object assignment (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`), reader threads per pipe writing to log files and updating `last_output_at`, a monitor loop with three independent timers: total response deadline, inactivity (no bytes for N seconds while alive), and the caller's per-operation deadline for Blender. A quiet Blender render is not treated as a stalled chat because the Blender path uses only its operation deadline (R-22). Kill = `TerminateJobObject`, then `taskkill /T /F /PID` fallback, then confirmation by polling `tasklist` for the PID and descendants enumerated through `Get-CimInstance Win32_Process` at kill time; `kill_confirmed` is recorded either way. The window between `CreateProcess` and job assignment is documented as a residual risk.

### 6.3 Per-provider argv (Phase 2 implements; documented here from `--help` and official docs)

**claude.** `claude -p --output-format json --json-schema <schema> --session-id <uuid>` or `--resume <uuid>`, `--model <m>`, `--effort max` (highest listed; the effective value comes from the CLI's own output when reported, otherwise the flag value with a "not confirmed by provider" label), `--tools Read,Glob,Grep --permission-mode plan --strict-mcp-config --disable-slash-commands --add-dir <packet_dir> --max-budget-usd <cap> --append-system-prompt <framing>`; prompt on stdin; cwd = agent scratch directory. Never `--continue`, `--fallback-model`, `--no-session-persistence`, `--bare`. Read at runtime: `result`, `structured_output`, `session_id`, `total_cost_usd` (labeled estimate per docs), `usage` fields as present, `is_error`.

**gemini.** `gemini -p "<short instruction>" -o json -m <model> --approval-mode plan --include-directories <packet_dir> --session-id <uuid>`; packet on stdin. Resume: probe `--resume <uuid>` and `--session-file` in Phase 2; if neither resumes by explicit id, the adapter declares `session_resume=unsupported` and the engine reconstructs context from records for every invocation (R-10, R-79). No reasoning flag: shown as "not exposed by CLI". Structured output: no native schema flag, so JSON is extracted from `response` and validated (R-24). Images: `@path` references are undocumented in the fetched pages; the R-16 probe decides.

**codex.** `codex exec --json --skip-git-repo-check -s read-only -C <scratch> -m <model> -c model_reasoning_effort=<level> --output-schema <file> -o <last_message_file> -i <image>... -` (stdin prompt); resume: `codex exec resume <uuid> --json ... -`. Never `--last`, `--ephemeral`, `--add-dir`, `--dangerously-*`. Reasoning probe order `xhigh` then `high`; the accepted value is displayed and journaled, and a rejection of `xhigh` is reported with the CLI's error (R-19, R-20). Session id from `thread.started.thread_id`; usage from `turn.completed.usage`. Cost is `Unknown` (codex and gemini report no cost).

### 6.4 Preflight tiers (R-15 to R-18, R-21)

For each agent and capability, three tiers with evidence: **declared** (adapter table and config), **local** (`shutil.which`, `--version`, `--help` parsed for every flag the adapter will pass; help text hashed), **live** (real minimal invocations, opt-in because they spend provider usage): image probe (Pillow-generated image with a known shape, color, and number delivered exactly as evidence will be, agent must report all three through the structured output; R-16), write-fail probe (agent asked to create a file in its scratch directory; the file must not exist afterwards; R-17), session create and resume probe (two invocations, second must recall a nonce from the first), structured output probe, usage fields presence, cancellation probe (kill during a long prompt, confirm tree gone). Cache key: sha256 of CLI path, version, model, settings JSON; any change invalidates (R-18). A missing required capability blocks `start` with the failing tier, the evidence, and a remediation line (R-21). Where a CLI cannot enforce read-only, the report says "prompt-only" and the configuration is labeled unsupported for shared write (R-17).

---

## 7. Evidence packets and agent output schemas (R-11, R-13, R-14, R-65, R-77 to R-83)

### 7.1 Packet layout

`agents/<agent_id>/packets/<packet_id>/`:

- `PACKET.md`: framing (only the user and Alloy issue instructions; images, renders, and the other agent's outputs are data; `[[ASK]]` and `[[TOOL]]` are not honored), the objective, task summary, constraints and interfaces, concise global brief, "changed since your last verified revision" (record deltas, new findings, new renders), open findings for the target, an evidence index table (file, role, revision id, render id, view, resolution, crop rectangle), and the output contract (JSON only, schema name, optional `rationale`).
- `packet.json`: machine manifest (Section 3.2 `packet`), including `withheld` entries such as "builder self-assessment withheld until initial findings are recorded" (R-11).
- `evidence/`: reference images, crops at inspection resolution, renders, measurement JSON. Crops carry original-space coordinates.
- `schema.json`: the expected output schema, also passed through the provider's native mechanism where one exists.

Packet kinds: `intake_observation` (per agent, without the other agent's analysis), `brief_draft` and `brief_check`, `construction_plan`, `build_task`, `review_task` (neutral objective; no self-assessment), `review_reconcile` (self-assessment revealed after initial findings are recorded), `correction_task`, `verification`, `reassessment`, `repair` (validation errors plus the invalid output, one round; R-5).

Isolation rule: if the reviewer's persistent session already contains the withheld material (tracked in `agent.context_contains`), the review runs in an `isolated_review` session with a reconstructed packet (R-11).

### 7.2 Output schemas (`schemas.py`)

`ObservationReport` (R-28 sections, each with `observed | inferred | uncertain` tags and evidence references), `BriefDraft` (R-29), `ConstructionPlan` (parts, relations, dependencies, interfaces, hypotheses), `OperationRequest` (`intent`, `target_part_ids`, `expected_outcome`, `declared_effects{creates, modifies, deletes}`, `script`), `TaskResult` (`operations[]`, `self_assessment`, `questions_for_user[]` limited to construction-changing questions, `no_edit_warranted{reason, evidence}`), `FindingsReport` (`findings[]` per R-73 with separate `severity` and `confidence`, `evidence_refs`, `proposed_correction`, `expected_improvement`, `alternative_hypotheses`; `coverage[]` per R-72), `VerificationReport` (`verdict: improved | unchanged | regressed | uncertain`, evidence), `RepairResponse`. Validation is a hand-written validator over these schemas (no new dependency); the same schema dicts are handed to `--json-schema` and `--output-schema`. Any parse or validation failure is `malformed`, one repair round is attempted, and a failed repair fails the invocation; none of these outcomes changes review, acceptance, or task state (R-5).

---

## 8. Engine (R-7 to R-9, R-12, R-31, R-39, R-41 to R-47, R-56 to R-59, R-76, R-85 to R-89)

### 8.1 Scheduling and assignment (R-8, R-12)

Code, not agents. Candidates for a task are agents whose required capabilities are verified (for example image reading for review tasks). Rules, in order: a user override wins; the builder of a component keeps its own corrections until two failed attempts on the same finding (then reassign, R-88); review tasks go to the non-owner; independent observation tasks go to both; otherwise alternate, starting with A. Every assignment records a one-line rationale. Contribution counts are reported, never balanced (R-9).

### 8.2 Improvement loop (R-56)

Per component: assemble evidence and open findings; independent observations only for new or materially changed evidence (validated observations are reused while their evidence and dependency versions are unchanged, R-57); reconcile; plan; assign a bounded task with expected outcomes; acquire ownership and checkpoint the base revision; build (agent returns operation requests; each is staged, validated, promoted, journaled); render affected views (cache by key); reviewer inspects with a neutral packet; initial findings recorded before the self-assessment is revealed; engine decides among correct, gather evidence, reassign, or request user review; corrections by the owner or after handoff; verify on new renders; integrate and inspect within the full assembly at milestones. "No useful edit warranted" is a recorded, evidence-linked result (R-58).

### 8.3 Ownership and handoff (R-41, R-44, R-45)

Ownership is a record with a random token bound to a base revision. Every operation carries the token and the expected base revision; the execution boundary rejects a missing, stale, or released token and a base mismatch before any Blender process starts. Handoff: cancel or finish pending operations, validate the owned revision, write the handoff record (source revision, changed parts, assumptions, findings, pending issues), release, grant the next holder a new token bound to the verified revision. Hash mismatches on workflow-owned files (a revision edited outside Alloy) block the operation and require explicit reconciliation (adopt as a new revision with provenance "external", or discard the external change).

### 8.4 Limits and stalls (R-85 to R-89)

`limits.py` tracks wall clock, request count, render count, attempts per finding, and cost. Cost is enforceable only for claude through `--max-budget-usd` per invocation and is labeled as such; gemini and codex costs are `Unknown` and a monetary cap over them is labeled "not enforceable, unknown consumption". In-flight work is counted at dispatch. After a limit is reached nothing new is dispatched and the run stops with the matching reason. Two failed corrections of one finding move it to `reassess`: cause classification (geometry, camera, material, lighting, evidence), optional checkpoint restore when ownership and dependencies allow, then a materially different approach, fresh analysis, reassignment, or an evidence-gap report. Transport retries apply only to side-effect-free operations and are counted separately from correction attempts (R-23, R-87).

### 8.5 Recovery on open (R-23, R-47)

On open, `operations.reconcile_all()` classifies every non-terminal operation from the journal, staging directory, revisions, ownership, and live process table: `never_started` (no staging), `failed` (result with `ok=false`, or exit code recorded non-zero), `committed` (journal has `op.promoting` and the revision file exists with the announced hash but no `op.committed`: the commit transaction is completed), `uncertain` (staged output without result, hash mismatch, or a process that was alive when the engine died). Uncertain outputs move to `staging/_quarantine/<op_id>/` and are never deleted automatically. Live orphan processes matching recorded PIDs are killed and confirmed. No mutation is replayed. A task whose operation was uncertain returns to `pending` with a note in the next packet describing the uncertain outcome. The run resumes `paused` with stop reason `execution_failure` when anything was uncertain, otherwise where it left off.

### 8.6 Checkpoints and restore (R-46)

A checkpoint is a manifest under `checkpoints/<ck_id>/` naming the immutable revision file, copies of owned linked assets and textures, the operation scripts that produced the revision, and the render manifests, with hashes. Restore creates a new revision whose parent is the checkpoint's revision (history is never rewritten), requires free or requester-held ownership, touches only workflow-owned files, and journals the restore with the reason.

### 8.7 User actions and feedback (R-59, R-75, R-76)

`request_correction`, `accept_component`, `reopen_component`, `mark_ambiguity`, `waive_finding(rationale)`, `accept_model`, `feedback(text)`, `pause`, `resume`, `cancel`, `restore_checkpoint`, `assign(task, agent)`. Feedback arriving mid-operation is stored immediately and applied at the next safe boundary. Attended mode gates before advancing past a detailed component: execution `waiting_for_user`, review `ready_for_user_review`, acceptance `unaccepted` until the user accepts. Unattended mode continues within limits and leaves components unaccepted.

---

## 9. CLI, GUI, configuration, preset (D8, D9, R-90 to R-93)

### 9.1 CLI (`builder/cli.py`, R-90)

`python -m builder <subcommand>` and `python main.py --build <subcommand ...>`. The workflow is general: `new` creates a project for any asset from any reference images, and a preset is only saved intake data (a named shortcut) that `new --preset <id>` can apply. Nothing in the engine, records, runner, adapters, or packets is specific to one asset. Subcommands: `new`, `open`, `preset list|create|show`, `preflight [--live]`, `start [--unattended] [--limit-*]`, `pause`, `resume`, `cancel`, `status`, `findings`, `accept`, `reopen`, `waive`, `assign`, `checkpoint list|create|restore`, `feedback`, `journal`, `fixture create`. `start` runs the engine in the foreground and prints progress; Ctrl+C requests a pause at the next safe boundary and a second Ctrl+C requests cancel. `pause`, `resume`, `cancel`, and `feedback` from another process write `control_requests`, which the running engine consumes at boundaries. Stdout is reconfigured to UTF-8.

### 9.2 GUI (Phase 3, R-91, R-92)

`gui/builder_view.py` `BuilderWindow(tk.Toplevel)` opened from a new menu entry. The engine runs in a worker thread and publishes events on a `queue.Queue` polled with `after(50)`, the same pattern as `gui/app.py`. Panels: intake, agents and capability status with effective reasoning setting, stage and ownership, part tree with relations, reference and render comparison (Pillow to `PhotoImage`, aspect preserved, zoom, labeled with revision and render ids), before and after, measurements toggle, findings, coverage, consumption and limits, controls. The main chat stays responsive because the view never blocks the Tk thread.

### 9.3 Configuration key (D9)

```yaml
builder:
  workflow_root: ""            # empty = <project_dir>/alloy-builder/<preset or project slug>; see Section 13
  blender:
    executable: ""             # empty = auto-detect
    deadlines: {validate: 120, apply: 600, render: 900, measure: 120}
  agents:
    A: {provider: claude, model: "fable",       reasoning: "max",   executable: ""}   # alias from `claude --help`; full name read at preflight
    B: {provider: codex,  model: "gpt-6-astra", reasoning: "xhigh", executable: ""}   # from ~/.codex/config.toml; xhigh probed, falls to high with a visible report
  provider_timeouts: {response: 900, inactivity: 180}
  limits: {wall_clock_minutes: 0, max_cost_usd: 0, max_requests: 0, max_renders: 0, attempts_per_finding: 2}
  attended: true
  presets:
    e10-bearer-head:
      name: "E10 Bearer - Head"
      project_dir: "C:\\Death Factory"
      asset: "E10 Bearer, the humanoid enemy"
      target_reference: "C:\\Death Factory\\docs\\art\\locked-theme-concept-v1.png"
      target_region: {name: "lower-left humanoid", bbox: null}   # chosen by the user in an authorized run, never guessed
      existing_source: "C:\\Death Factory\\art-source\\blender\\E10\\E10_Bearer_v4_proportions.blend"
      existing_source_trust: "unfinished; proportions not assumed correct"
      first_component: "Head"
      rejected_references: []
```

`builder/config.py` parses this leniently (missing key means defaults; unknown sub-keys are kept and ignored). Hooks in existing files: `config.py` gains `Config.builder: dict` read from `data.get("builder", {})` and mirrored in `Config.save()` when non-empty (so the only `Config.save` caller, "Reset to defaults", is the only path that discards it, which is its purpose). The settings editor already preserves the key (Section 1.2); a D test proves a save through `deep_merge` and the ruamel round trip keeps `builder` intact. Presets are data; creating, listing, or showing one never reads the PNG or opens the `.blend` (R-93), proven by a test that asserts the source paths are never opened.

### 9.4 Minimal hooks in existing files (all LF-preserving, verified with `file` after edit)

- `main.py`: `parser.add_argument("--build", nargs=argparse.REMAINDER)`; when present, `from builder.cli import main as build_main; sys.exit(build_main(args.build))` before any other handling. Phase 1.
- `config.py`: `builder` field, parse, save mirror. Phase 1.
- `config.yaml`: the `builder` key above with the preset. Phase 1.
- `requirements.txt`: `Pillow>=10`. Phase 1 (used by references and crops).
- `alloy.spec`: remove `PIL` from excludes; add `builder` package and `builder/blender/scripts/*.py` as data. Phase 4.
- `gui/app.py`: one `add_command` for the builder view. Phase 3.
- `gui/settings.py`: a Builder tab. Phase 3.

---

## 10. Test plan by requirement (Section 6 matrix)

Layers: **D** deterministic (mock adapters, no Blender, no network), **B** real Blender (marker `blender`, skipped with the reason "Blender executable not found at ..." when absent), **U** CLI and GUI interaction, **L** live providers (opt-in, approved scope and budget).

| Test module | Layer | Requirements | What it proves |
|---|---|---|---|
| `test_store_journal.py` | D | R-2, R-3, D5 | records versioned with history; journal append-only (update and delete raise); `rebuild_from_journal()` equals `records`; schema_version present; invalid records refused |
| `test_state_machines.py` | D | R-1, R-4, R-5, R-76 | every legal transition journaled; illegal transitions raise and write nothing; agent text with "done"/"we agree" changes nothing; malformed output never advances review or acceptance; attended gate stops at `waiting_for_user` while unattended continues and leaves `unaccepted` |
| `test_ownership.py` | D | R-41, R-44, R-45 | single holder per resource; stale token rejected after handoff; base-revision mismatch rejected; conflicting commits from two holders impossible; external hash mismatch blocks and requires reconciliation |
| `test_adapter_argv.py` | D | D3, R-10, R-20, R-22 | argv lists only; no `shell=True` in `builder/` (source scan); no `{message}`; no `--continue`, `--last`, `--fallback-model`, `fallback_ai`; two agents on one provider get distinct session UUIDs; Windows paths with spaces, Unicode, and metacharacters survive `subprocess.list2cmdline` round trip |
| `test_process.py` | D | R-22, R-23 | with a Python child script: response deadline, inactivity detection, and per-operation deadline fire independently; cancellation kills a child that spawned a grandchild and confirms both gone; stdout and stderr drained concurrently without deadlock; transport retry only for side-effect-free operations |
| `test_structured_output.py` | D | R-5, R-24, R-73 | schema validation of every output kind; JSON extraction from text; one repair round then failure; a finding record requires separate severity and confidence |
| `test_references.py` | D | R-16, R-26, R-27 | originals unmodified (hash); dimensions recorded; regions in original space; replacement creates a new version; composite sheets require a target region; probe image generation |
| `test_render_manifest.py` | D | R-63, R-64, R-81 | cache key includes every input; stale, missing, unreadable, mismatched renders detected and block `ready_for_user_review` |
| `test_invalidation.py` | D | R-6, R-39 | acceptance binds revision, evidence, and dependency versions; upstream change supersedes visibly and invalidates affected renders and observations only |
| `test_preflight.py` | D | R-15, R-18, R-21 | three tiers reported per capability with mock adapters; cache invalidated by version or model change; missing required capability blocks with remediation; prompt-only read-only labeled unsupported for shared write |
| `test_engine_flow.py` | D | R-7, R-9, R-11, R-56, R-58, R-74 | mock A builds, mock B finds and corrects a scripted defect, handoff, both contribute; reviewer packet withholds self-assessment until initial findings are recorded; finding closes only after a verified verification report on a new render id, never on script exit; "no useful edit" recorded |
| `test_recovery.py` | D | R-23, R-47 | fixtures for each in-flight class (never started, failed, committed-uncommitted, uncertain) classified correctly; uncertain outputs quarantined; no duplicate operation dispatched; no stage advancement |
| `test_limits.py` | D | R-25, R-85 to R-87 | unknown cost never zero and never satisfies a cap; in-flight counted; nothing dispatched after a limit; correction attempts separate from transport retries |
| `test_reassessment.py` | D | R-88 | two failed corrections move the finding to reassess with a cause classification and a different approach or reassignment |
| `test_config_hooks.py` | D | D9, Section 2 | `builder` key survives `deep_merge` and the ruamel save path; `Config.load` unchanged for existing keys; importing `builder` writes nothing; preset creation never opens the source files (patched `open` asserts) |
| `test_existing_unchanged.py` | D | D2 | `Router` parsing and `Orchestrator` command building behave as before for representative inputs; `builder` imports nothing from `orchestrator`, `prompts`, `tools`, `actions` |
| `test_blender_runner.py` | B | Section 5, R-22, R-48 | smoke, exit code 3 on exception, result protocol, Unicode staging paths, deadline kill during a long script with confirmation, missing external asset surfaced |
| `test_blender_operations.py` | B | R-37, R-41, R-45 | validate detects deletion, duplicate, unmapped, orphan; promotion is atomic; revision read-only; external edit detected by hash |
| `test_blender_render.py` | B | R-60 to R-65, R-81 | Workbench clay and EEVEE material renders with manifests; settings reported match request; cache reuse on identical inputs; recompute on any input change |
| `test_blender_fixture_e2e.py` | B | Section 5, R-43, R-46, R-74 | fixture with four defects; mock A builds the missing part; mock B reports the floating fitting; correction operation; measurement shows the gap closed; re-render; finding closed by verification; handoff; checkpoint restore creates a child revision; reopen |
| `test_blender_recovery.py` | B | R-47 | Blender killed mid-save and mid-promotion; reopen classifies and recovers without duplicate geometry |
| `test_cli.py` | D, U | R-90 | subcommands run through the engine; status prints stop reasons; UTF-8 output |
| `test_efficiency_harness.py` | D | R-84 | Phase 4: same fixture under transcript replay and packet delivery; reports delivered bytes and preserved evidence and coverage; no percentages |

Every mock adapter states in its docstring exactly what it scripts; the D flow tests prove routing and state handling, not visual judgment.

---

## 11. Decisions and tradeoffs

1. **Package name `builder`** (D1). No existing module conflicts.
2. **Document-style records in SQLite with typed history and journal snapshots.** Simple schema, validated in Python, rebuildable from the journal (R-3). Tradeoff: some queries scan JSON; project sizes are hundreds of records, so this is acceptable.
3. **Job Objects for process-tree kill** through ctypes, `taskkill /T` as fallback, `tasklist` confirmation. No new dependency (psutil is not installed and D5 forbids it).
4. **Agent bpy scripts are screened, not sandboxed.** The spec (D4) requires free-form bpy scripts. Enforcement comes from staging, immutable revisions, validation, and read-only agent CLIs; screening is a guardrail and is documented as such.
5. **claude without `--bare`.** Bare mode disables OAuth; the adapter instead controls the working directory, restricts tools, and disables MCP and slash commands.
6. **gemini resume is capability-gated.** Context reconstruction is a first-class path because R-11 isolated reviews need it anyway.
7. **codex `--skip-git-repo-check` always; `--ignore-user-config` not used by default** (auth is preserved either way; explicit `-m` and `-c model_reasoning_effort` override the user file). Reasoning probe order `xhigh`, then `high`, with the accepted value displayed.
8. **Cost handling.** Only claude reports a cost (an estimate per its docs) and only claude can enforce a per-invocation cap; the UI labels every other monetary figure "unknown".
9. **Fixture references are renders of a truth model.** Deterministic and independent of the defective model, labeled synthetic.
10. **Workflow state lives outside the `.blend`** in a workflow directory with SQLite, revisions, renders, and logs. Only revisions are user-facing deliverables. Default location is the open question in Section 13.
11. **`main.py --build` uses `argparse.REMAINDER`** so the builder's own parser owns everything after the flag; `python -m builder` works without touching `main.py`.
12. **No numpy, no jsonschema, no psutil.** Pillow is the only addition (D5).
13. **All new files LF and UTF-8; console output UTF-8 with replacement.**
14. **Reasoning setting policy** (R-20): claude `--effort max`; codex `xhigh` if accepted; gemini none, shown as "not exposed". Effective values are shown in CLI status and in the GUI.
15. **Presets are config data** under `builder.presets`, created without touching sources (R-93).
16. **Deadlines** default to validate 120 s, apply 600 s, render 900 s, measure 120 s; provider response 900 s, inactivity 180 s. All configurable, all separate.
17. **Default agent bindings: A = claude `fable`, B = codex `gpt-6-astra`.** Per the owner's guidance that GPT 6 and Fable 5.1 are the models suited to this work. Both CLIs offer native structured output (`--json-schema`, `--output-schema`) and an explicit session id, and codex attaches images explicitly with `-i`, which makes the R-16 delivery contract testable. gemini remains a configurable alternative with extracted-JSON parsing. Assignment still alternates and reassigns per Section 8.1; there is no permanent builder/reviewer split (R-8).

---

## 12. Phase plan

**Phase 1 (this session, after approval), test-first in this order:**
1. `tests/conftest.py`; `ids.py`, `records.py`, `store.py` with journal rebuild (D).
2. `state.py` machines and guards (D).
3. `config.py` (builder), `project.py` layout and presets; `config.py` and `config.yaml` hooks; D9 tests.
4. `providers/process.py` with the three timers and Job Object kill (D, Python child scripts).
5. `blender/runner.py` and scripts `smoke`, `validate`, `identities`, `apply_operation`, `render`, `measure` (B).
6. `render.py` manifests, cache keys, stale detection (D and B).
7. `ownership.py`, `operations.py` lifecycle, promotion, recovery classification (D and B).
8. `references.py`, `evidence.py`, `schemas.py`, `providers/structured.py` (D).
9. `providers/base.py`, `providers/mock.py` (D).
10. `engine.py` with mocks: A builds, B finds and corrects, handoff, checkpoint restore, reopen, attended gate, limits (D); end-to-end on real Blender with `fixture.py` (B).
11. `cli.py`, `__main__.py`, `main.py --build` hook (D, U).
12. Report in the required format, including the Bearer asset check by `stat`.

**Phase 2:** claude, gemini, codex adapters; three-tier preflight; image, write-fail, session, structured-output, usage, and cancellation probes; bounded live smoke test only after owner approval of scope and budget.
**Phase 3:** `gui/builder_view.py`, menu entry, settings tab, screenshots.
**Phase 4:** recovery hardening, limits UI, efficiency harness (R-84), documentation (Section 8 of the spec), `alloy.spec` and launch scripts.

---

## 12a. Phase 1 construction notes (2026-09-07, after owner approval)

Owner decisions: workflow location is selectable (`new --workflow-dir`, preset `workflow_dir`, `builder.workflow_root`) with the default `<project_dir>\alloy-builder\<slug>`; reviewer isolation is `builder.isolated_reviews: when_contaminated | always`, default `when_contaminated`, no `never`.

Decisions made while building, recorded here because they refine Sections 8 and 10:

1. **Correction ownership (refines 8.1).** A correction task goes to the agent that proposed it (the reviewer), by handoff from the current holder, because R-56 and R-7 want the finder to implement what it can, and the Phase 1 scenario requires both agents to build. After `attempts_per_finding` unsuccessful attempts the finding enters `reassess`; the other agent performs the reassessment and becomes the reassigned corrector; the run stops with `attempt_limit` until the user raises the limit, so no oscillation is possible (R-88).
2. **Coverage rule for the gate (refines 8.2, R-72, R-81).** A part is covered when a coverage record exists for each required clay view at a revision no older than the part's last modification. Verification of a corrected part records coverage for that part at the after revision. Unchanged parts keep their review coverage.
3. **Withdrawn findings.** A finding the reviewer withdraws after seeing the builder's self-assessment closes with resolution `withdrawn_by_reviewer`; the journal keeps the initial finding (R-11, R-73).
4. **Mock convention.** `ScriptedAdapter` fills whole-string `{key}` placeholders from request metadata Alloy attaches (`finding_id`, `finding_ids`, `nonce`, `attempted_path`) so screenplays never guess ids. The session probe is two invocations: create, then resume with the same UUID.
5. **Standard views are framed from measurements, never fixed presets.** Six views at 640x480 with `Standard` view transform: front, side, three-quarter clay and three-quarter material framed on the component's measured bounding box, plus whole-model front and three-quarter framed on the assembly. A first attempt with fixed preset cameras clipped the fixture's head out of frame, which the evidence sheet exposed; cameras now come from `render.frame_camera` (bounding-sphere fit inside the vertical field of view with a 15 percent margin), are recorded per component with `hypothesis` calibration, stay frozen so before/after renders compare, and are re-framed as a new view version with a journal entry only when parts leave the frame. Fixture references are rendered from the truth model with the same helper. Matching real references per R-60 remains Phase 2 work.
6. **Execution sub-states.** The run moves through `waiting_for_provider` and `rendering` around provider and Blender work, so the journal and GUI can show them (R-4).
7. **CLI injection.** `builder.cli.main(argv, runner_factory=..., adapters_factory=..., stdout=...)` lets deterministic tests drive every verb with a fake runner; the default path discovers Blender and, in Phase 1, accepts only `--mock <screenplay.json>` agents, saying so explicitly.
8. **Consumption persistence.** The run record stores the limit tracker snapshot at every stage change and stop, so `status`, `resume`, and a restarted engine continue counting (R-85, R-86).
9. **Agent script screening list** (Section 5): modules os, subprocess, shutil, pathlib, sys, ctypes, socket, urllib, http, importlib, builtins, io, tempfile, glob, multiprocessing, threading, pickle, marshal, code, runpy; calls open, exec, eval, compile, `__import__`; `bpy.ops.wm.save*/open*/link/append/read*/recover*/revert*/quit*`; `bpy.data.libraries`; `bpy.app.handlers/timers`.
10. **Orphans.** Blender drops zero-user datablocks on save, so `apply_operation.py` reports objects linked to no collection before saving and the operation is rejected (R-37).
11. **Component membership.** A per-part `component` field in the construction plan is authoritative; without it the membership the project or preset defined is kept, and only a component with no membership takes every planned part. The first version dissolved the fixture's head component into the whole lamp, which mis-framed the review renders against the references; the evidence sheet caught it.

Phase 1 delivered: `builder/` (engine, records, store, state machines, config, project, references, inventory-lite via parts and relations, ownership, operations, render manifests, evidence packets, schemas, structured parsing, provider base and mock, limits, fixture, CLI), `builder/blender/` (runner and seven in-Blender scripts), `tests/` (150 tests: deterministic and real Blender), hooks in `main.py`, `config.py`, `config.yaml`, `requirements.txt`. Not in Phase 1: real provider adapters (Phase 2), the Tk view (Phase 3), efficiency harness, `alloy.spec`, and documentation (Phase 4).

## 12b. Phase 2a construction notes (2026-09-07, real adapters, preflight, close-ups, roles)

Re-verified at implementation time (design Section 1.3 held): `claude` 2.1.233 is a native `claude.EXE`; `gemini` 0.55.1 and `codex` 0.147.0 are npm `.cmd` shims that re-enter `cmd.exe`. Official pages read again: code.claude.com/docs/en/headless (stdin is the prompt, 10 MB cap; `--output-format json` carries `result`, `session_id`, `structured_output`, `total_cost_usd` as a client-side estimate; `--resume <id>` works from any directory; SIGTERM exits 143), geminicli.com headless and cli-reference (`response`/`stats`/`error`; exit codes 0, 1, 42, 53; `--resume` documented for `latest` or an index only; `--skip-trust` documented), learn.chatgpt.com non-interactive mode and config reference (`--json` events `thread.started.thread_id`, `turn.completed.usage.{input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens}`, `turn.failed`, `error`; `-` reads stdin; `model_reasoning_effort` is `minimal|low|medium|high|xhigh`, "xhigh is model-dependent"; `sandbox_mode`, `approval_policy` keys). The codex CLI's own model cache (`~/.codex/models_cache.json`, written by client 0.153.1) lists `gpt-6-astra` reasoning levels low, medium, high, xhigh, max, ultra; the adapter uses the owner-approved `xhigh` with the documented `high` as the one probed fallback and does not assume `max` or `ultra`.

Decisions made while building:

1. **npm shims are unwrapped** (`providers/cli_common.resolve_cli`). The `.cmd` shim is parsed for the bundle it names and the CLI runs as `node.exe <bundle.js>` so every argument reaches the CLI through `CreateProcess` unchanged; no `cmd.exe` quoting layer, no `%`/`^`/`&` mangling. The resolution (`via=npm_shim`, script path) is shown in preflight and is part of the local report. A configured executable path that does not exist is a preflight failure, never replaced by a PATH lookup.
2. **Local tier reads `--help`** (R-15, R-19). Each adapter declares, per capability, the help tokens it relies on (`--flag`, `--flag=choice` meaning the choice appears in that flag's help segment, `word:resume` for a subcommand). `preflight_local` runs `--version` and the help commands (free), hashes the help text, and reports every missing token; the engine's local tier is `missing` with the list when any is absent. Verified against the installed CLIs: no token is missing for claude, gemini, or codex (`tests/test_preflight_local.py`, marker `cli`).
3. **Session ids** (R-10). claude and gemini take the UUID Alloy chose (`--session-id`) and resume with it. codex assigns its own thread id (`thread.started.thread_id`); the registry records the reported id and `ref_for` resumes with the reported id when the provider reported one, else Alloy's UUID. Resume is capability-gated: gemini declares `session_resume=probe` (resume by UUID is undocumented); when the live nonce probe fails, every later invocation of that agent starts a fresh session and the invocation record carries `context_reconstructed=true` (the packet's brief, deltas, and findings are the context, R-79).
4. **Reasoning probe** (R-20). `CliAdapter.invoke` retries once with the documented next level when the CLI's own error mentions the reasoning setting, keeps the rejection text, reports `reasoning_requested`/`reasoning_effective`/`reasoning_rejection` in every result, journals the downgrade in the preflight report (`reasoning_fallbacks`) and prints it as `REASONING DOWNGRADE (reported, not silent)`. The accepted level is reused; nothing is probed twice. claude has no fallback table: a rejected `--effort max` is a preflight failure quoting the CLI. gemini shows `not exposed by CLI`.
5. **Usage** (R-19, R-25). Only field names that are documented and present in the CLI's own output are mapped: claude `usage.input_tokens/output_tokens/cache_read_input_tokens` (Anthropic Messages API names) and `total_cost_usd` as `Estimated`; codex the four `turn.completed.usage` names; gemini none (the `stats` object is kept raw in `effective_settings.stats_raw`, usage stays `UNKNOWN`, and the preflight tier says `not_verified` with the raw field names seen). Unmapped raw fields are listed, never guessed.
6. **Transport failures never trigger a repair round.** A timeout, cancel, spawn failure, non-zero exit, provider-reported error (`is_error`, gemini `error`, codex `turn.failed`/`error`), or unparsable output ends the invocation as failed. Only a parsable reply that fails schema validation gets the one repair round (R-5, R-23).
7. **Read-only modes** (R-17). claude `--tools Read,Glob,Grep --permission-mode plan --strict-mcp-config --disable-slash-commands --add-dir <packet>`; gemini `--approval-mode plan --include-directories <packet> --skip-trust` (the scratch directory Alloy controls is the trusted workspace); codex `-s read-only` plus `-c sandbox_mode="read-only"` and `-c approval_policy="never"` on every invocation, because `codex exec resume --help` offers no `-s`/`-C`. The write-fail probe verifies each live. codex is not given `--add-dir` (writable) and claude is not given `--bare` (it reads no OAuth login).
8. **Framing delivery.** The working agreement (`prompts.FRAMING`) is in every `PACKET.md` and is also passed as `--append-system-prompt` to claude and as a stdin preamble to gemini and codex, so it reaches the model even when the packet is skimmed. Cost: about 450 tokens per invocation for the stdin providers; accepted for Phase 2a and listed for the Phase 4 efficiency harness.
9. **Cancellation probe** (R-22). `preflight --live` starts a slow-running prompt and sets the invocation's cancel event after `provider_timeouts.cancel_probe_after` seconds (default 8); verified means the process tree was confirmed gone. A CLI that answers before the timer fires yields `not_verified` with the reason; the process-tree kill itself is proven by `tests/test_process.py` and `tests/test_adapters.py::test_cancellation_kills_the_cli_process_and_confirms` against a real child process.
10. **Cache key** (R-18) now includes each adapter's `settings_signature()` (tools, permission or approval mode, sandbox, output format, prompt channel) besides CLI path, version, model, and reasoning, so a change to any argv setting invalidates the stored report. `start` and `resume` with real adapters never run a live preflight implicitly: a stale or missing report is an error that names `preflight --live`; only scripted mocks are probed automatically.
11. **Monetary cap.** `--max-budget-usd` is passed to claude as the remaining budget (cap minus measured and estimated totals). The tracker stops dispatch when the estimated total reaches the cap and says so as an estimate. codex and gemini report no cost: unknown, never zero, and the cap is labelled not enforceable over them.
12. **Per-part close-ups and crops** (R-61, R-65). `_views` adds one clay view `closeup:<part_id>` per part of the component that has a measured bounding box, framed with `frame_camera(..., margin=1.35)` on that part alone (three-quarter direction); framing is frozen and re-framed with a journal entry only when the part leaves the frame, like the other views. `render.project_bbox` projects a part's world box through the recorded perspective camera into render pixels (Blender XYZ Euler, AUTO sensor fit); `_crop_items` cuts that rectangle (15 percent padding, clamped, skipped when the part already fills more than 60 percent of the frame) from the front, side, and three-quarter overview renders at native pixels with `space=render_pixels`, the source render and revision ids, and the part id. Review packets carry close-ups and crops for every part of the component; correction packets carry them for the finding's part; verification packets carry BEFORE and AFTER close-ups and crops for the finding's part. Render manifests of close-ups name the part. Coverage and the gate still require the four review views; close-ups are additional evidence, never a substitute.
13. **Roles** (`builder/roles.py`, Section 14, R-107, R-108). The table is observer, planner, builder, corrector, reviewer, verifier, reassessor, art_director, image_generator. `assign()` returns the seat and a one-line rationale; the engine calls it for the brief and plan (planner), builds (alternating), corrections (proposer or the reassigned seat), reviews, verifications, and reassessments (never the author, even under a user override, which raises `RoleConflict` and stops the run with the reason). The art director defaults to seat A and rotates after two rejected rounds; the image seat holds only image_generator. Engine behaviour for the existing loop is unchanged; the Phase 1 tests pass untouched except for the added cancellation probe entries in the screenplays.
14. **Test safety net.** `tests/conftest.py` wraps `subprocess.Popen` for every test not marked `live`: an argv whose executable or bundle is claude, gemini, or codex is refused unless it only asks `--help` or `--version`. `tests/_fake_cli.py` emulates the three CLIs' documented output shapes (and a schema-driven minimal reply, a refused write, a nonce memory keyed by session id) so the adapters' argv, stdin, files, parsing, downgrade, cancellation, and the CLI's preflight-then-start path run as real processes at no cost.

15. **Live findings (owner-approved smoke test, 2026-09-07).** Recorded from the CLIs' own output; each changed the code.
    - *codex on PATH is too old.* `codex-cli 0.147.0` (npm) is refused by the API for `gpt-6-astra` ("requires a newer version of Codex"). The Codex desktop app ships `codex-cli 0.153.1` at `~/.codex/plugins/.plugin-appserver/codex.exe` (with its `codex-code-mode-host.exe` beside it); the copy at `~/.codex/.sandbox-bin/codex.exe` has no tool host next to it and cannot read files ("failed to spawn code-mode host"), which showed up as an empty nonce. `builder.agents.B.executable` points at the plugin-appserver copy for live runs; npm's latest is 0.153.4 (not installed: a global upgrade is the owner's call).
    - *OpenAI structured output is strict.* codex 0.153.1 rejects a schema unless every object carries `additionalProperties: false` and lists every property in `required` (`invalid_json_schema`). `cli_common.strict_schema` produces that form for the provider copy (optional properties become nullable and required, `minItems` dropped); `strip_optional_nulls` removes the nulls before Alloy validates against its own schema. Two Alloy schemas were made expressible in that dialect: `dimensions` is a list of named entries with a status (R-35) instead of a free-form object, and `instances_inspected` is text.
    - *codex reads `-i` images, refuses writes, resumes by thread id, reports usage, and accepted `xhigh`.* Verified: probe `{'shape': 'triangle', 'color': 'red', 'number': 7}`; write refused with "writing is blocked by read-only sandbox; rejected by user approval settings"; nonce recalled through `codex exec resume <thread_id>`; usage fields `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`; no reasoning downgrade was needed; the model name is not reported in the JSONL events (recorded as such). An `item.completed` item of type `error` ("Model metadata ... not found", from 0.147.0) is kept as a warning.
    - *claude verified end to end.* Alias `fable` resolved to `claude-fable-5` (from `modelUsage`); `--effort max` accepted; write probe answered `no_write_tool_available` ("only Read, Glob, Grep, and StructuredOutput"); nonce recalled via `--resume <uuid>`; `total_cost_usd` present (estimate); cancellation after 8 s with the process tree confirmed gone. `--output-format json` prints nothing until the reply is complete, so the inactivity timer must not be shorter than the response timeout for claude (the live config sets both to 900 s); a `stream-json` activity signal is a Phase 4 candidate.
    - *gemini is unusable on this machine.* `gemini` 0.55.1 aborts before any request under the owner's account: "IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals ... migrate to the Antigravity suite". Nothing about the gemini adapter is live-verified. Observed on a resume of a never-created session: "Error resuming session: No previous sessions found for this project."
    - *Two engine fixes from the runs.* A failed `new` invocation no longer advances the provider session (the next call would otherwise try to resume a session that was never created; journal event `provider_session.not_advanced`). The write probe reports an outcome enum (`written`, `attempted_and_refused`, `no_write_tool_available`, `could_not_attempt`): the first two are enforcement evidence, `could_not_attempt` is `not_verified`, never `verified`. `preflight --agent <label>` probes one agent and keeps the others' stored reports.

16. **Live fixture run (2026-09-07, caps: 16 requests, 60 renders, 5 USD estimated, 60 min, 1 attempt per finding).** Intake completed for both agents (claude: 41 evidence-tagged statements and three construction questions in 321 s; codex: an independent 20-statement observation in 184 s, 813k input tokens of which 598k cached, 9k output, 5k reasoning), the brief completed (claude, 13 observed-vs-inferred statements, four unresolved decisions, 211 s). The plan call stopped: Alloy passed claude the *remaining* budget as `--max-budget-usd` (5.00 minus 2.26 minus 2.67 = 0.06 USD), claude spent 2.07 USD before its cap ended the turn (`is_error`, `result` null, `stop_reason` tool_use), and Alloy's estimated total (4.94) had not reached the cap at dispatch. Both caps act after the spend, so the 5 USD ceiling was overrun by about 2 USD. Consequences in code: the tracker refuses to dispatch when the remaining budget is below the largest single-invocation cost that agent has reported (`budget_limit`, labelled as an estimate); a claude result stopped by its cap is the outcome `budget_exhausted` and pauses the run with `budget_limit`, never `execution_failure`. Cost per call at `--effort max` on the fixture: intake 2.26 USD, brief 2.67 USD, each preflight probe 0.5 to 0.8 USD (the probes read the packet and think); two full claude preflights cost about 5.3 USD. Total claude spend for the session: about 12.3 USD in the CLI's own estimates plus two cancelled probes that reported no cost; codex: unknown. The live run stopped at the plan stage by decision (the approved budget was spent); revision 0 is unchanged, no render was produced, the journal rebuilds. `--output-format json` gave no activity signal during the 5-minute intake call, which confirms the inactivity-timer note in item 15.

Phase 2a delivered: `builder/providers/cli_common.py`, `claude.py`, `gemini.py`, `codex.py`, `providers/__init__.py` (factory), `builder/roles.py`, projection and close-up helpers in `render.py`, engine preflight and packet changes, CLI wiring, `config.yaml` key `provider_timeouts.cancel_probe_after`. Tests: 181 deterministic (3 of them read the installed CLIs locally), 20 real-Blender, 1 live (skipped unless `ALLOY_LIVE=1`). Live: preflight verified for claude and codex, fixture run to the plan stage (items 15 and 16).

**Live smoke test proposal (awaiting approval).** `python -m builder fixture create <dir>` then `python -m builder preflight <dir>\workflow --live` (per agent: image probe 1, write probe 1, session probe 2, cancellation probe 1, plus at most one repair round per probe: 5 to 10 calls per agent) and `python -m builder start <dir>\workflow --max-steps 40` with `builder.limits` set to `max_requests: 16`, `max_renders: 60`, `max_cost_usd: 5`, `wall_clock_minutes: 60`, `attempts_per_finding: 1` (minimum path intake 2, brief 1, plan 1, build 1, review 1, reconcile 1, correct 1, verify 1 = 9 calls; bounded by `max_requests`). Enforceable by Alloy: request count, render count, wall clock, attempts. Enforceable by the provider: claude `--max-budget-usd` (remaining budget per invocation, an estimate per its docs). Not enforceable: codex cost (unknown). The same is scripted as `tests/test_live_smoke.py` (`ALLOY_LIVE=1`, caps via `ALLOY_LIVE_MAX_*`).

## 12c. Phase 2b construction notes (2026-09-07, concept stage per Addendum A)

Built against Section 14 and Addendum A; the base spec's numbering is cited as before. Decisions made while building:

1. **The loop is command-driven, not a run stage.** The concept stage precedes `start` and waits on the owner at every generation (the manual seat, D12), so it does not live in `run_until_stop`. `builder/concept.py` (`ConceptStage`) holds canon, requests, import, approval, verdicts, conflicts, coverage, studies, and limits; `advance()` performs the automatic work the owner's last command unlocked (art-director prompts, canon description, both seats' verdicts, mode decisions, bounded regenerations, escalations, the coverage check) and every `concept` verb that can spend usage calls it. All LLM calls go through `Engine._invoke`, so packets, journal, limits, sessions, and repair rounds are the Phase 2a ones. Without a run the tracker snapshot lives on the `concept_plan` record, so caps hold across CLI processes (R-86).
2. **Canon machine** (fifth machine in `state.py`, kind `concept_plan`): `no_canon -> anchor_pending -> anchor_approved -> turnaround_pending -> turnaround_approved -> complete`, `no_canon -> anchor_approved` for seed images, backward edges on rejection (`anchor_approved|turnaround_pending -> anchor_pending`, `turnaround_approved|complete -> turnaround_pending`). Guards: an approved anchor, a recorded canon description, coverage of the needed set, and no open `evidence_conflict`. `turnaround_pending -> complete` requires a user actor with `proceed=True`; the transition writes `proceeded_partial` (by, at, missing) onto the plan (R-103). `_stage_intake` refuses while a plan exists and is not `complete`, and builds its packets from `References.approved()` only (R-94).
3. **Reference fields.** `add()` marks owner-supplied references `canon_state=approved`, `precedence=0 (owner_target)`; `add_generated()` copies unmodified under `refs/generated/<request>/`, marks `candidate`, `kind=generated`, `evidence_of_original=False`, and records the owner's vendor/model/attached as `declared` with a note that Alloy cannot verify them (R-96a, R-97). A reference without a canon state predates the concept stage and counts as approved. `canon_rank()` sorts by precedence and never merges (R-95).
4. **Generation records are the manifests** (R-96): target, prompt (text and the art director's structured fields), attachments with hashes, seat, declarations, outputs with hashes, `usage` and `cost` as `not_applicable` for the manual seat (a new quantity kind next to unknown, never zero, R-105), timestamps, success/error, round, `revises`. `concept/requests/<id>/manifest.json` mirrors the record on every state change, including `abandoned` and `failed` (owner verbs `concept abandon` and `concept failed`, added beyond R-110 so those manifests can exist).
5. **Manual seat contract** (`providers/imagegen/base.py`, `manual.py`): `publish_request` writes `PROMPT.md` (prompt verbatim in a fenced block, attachment copies under `attach/` with hashes, the exact import command, the declaration reminder), `generate()` returns `None` (the owner generates), `preflight()` reports declared (manual, intended vendor), local (import directory writable; flow confirmed once, recorded as `flow_confirmed_at` on the plan after the first successful import), live `not_applicable` with the R-109 sentence. `watch_folder()` implements `--watch`: new image files are handed over once their size is stable for one poll. No credentials exist anywhere in the seat modules; a test greps for it, and `ImageSeat.SECRETS_POLICY` states the environment-only rule for any future API seat.
6. **Verdicts** (R-102): `roles.consistency_seats()` returns both LLM seats with the rationale; each verdict packet carries the anchor (labelled approved), the candidate (labelled candidate, "a hypothesis, not canon"), the canon description as the brief, and `withheld` naming the other seat's verdict; the first verdict is journaled (`reference.verdict_recorded`) before the second packet is built, which the test checks by journal sequence. A verdict naming a different `reference_id` is downgraded to `uncertain`; a malformed reply is recorded as `malformed` and never approves anything (R-5). The summary over both seats is `inconsistent` if either says so, else `uncertain` if either does, else `consistent`.
7. **Mode decisions** (D11, R-98): `each` leaves every candidate to the owner and prints the verdicts; `anchor_only` and `auto` auto-approve when both verdicts are `consistent` and no conflict is open (criteria text recorded, `auto=true`, actor `engine`), reject `inconsistent` candidates with the verdict reasons, and never touch `uncertain` ones (escalated to the owner). `auto` picks the anchor through the art director's `anchor_pick` schema (criteria and rejected alternatives recorded); the owner reverses a pick with `concept reject`, which returns canon to `anchor_pending` and opens a new round. In every mode an `inconsistent` view is regenerated while `max_regenerations_per_view` allows and escalated afterwards (R-102, R-105); in `each` the inconsistent candidate stays a candidate so the owner may still approve it.
8. **Conflicts never averaged** (R-95): when the owner approves a candidate whose verdicts named inconsistencies with the anchor, one `evidence_conflict` per inconsistency is recorded (both reference ids, region, what differs, reporting seat). Open conflicts block `turnaround_approved`, `complete`, `proceed`, and auto-approval. Rejecting one of the images resolves the conflict `resolved_by_user`; approving a regenerated image that supersedes one resolves it `resolved_by_regeneration`.
9. **Art director** (R-107, R-108): assigned per prompt through `roles.assign("art_director", rejected_rounds=...)` where a rejected round is a generation record in state `rejected` or `failed`; the seat and one-line rationale are recorded on the plan and on every request. The canon description is versioned per anchor and written by the art director in force.
10. **Studies** (R-104): `concept study` and the reassessment schema's optional `study_requests` create `study_request` records; with an approved anchor the art director writes the prompt at once (anchor plus the approved view of the same name as conditioning), otherwise the request is recorded and the stop note says how to generate it. Approved studies attach to the part (`part_id`, precedence 3) and appear in modeling packets below the turnaround.
11. **Limits** (R-105): `max_images` counts every reference with a `generation_id` (imported and generated alike) and is checked before a request is opened and before an import; `max_regenerations_per_view` bounds rounds per view or study and holds for the owner's `concept regenerate` too. The image seat's cost is `not_applicable`; the LLM calls count in the normal tracker.
12. **CLI** (R-110): `concept start|prompts|import|list|show|approve|reject|regenerate|study|proceed|abandon|failed`; every verb prints `approval mode <mode>: <meaning>` first. Verbs that may spend usage (`start`, `import`, `approve`, `reject`, `regenerate`, `study`) need a current live preflight report with real adapters, exactly like `start`; `--mock` probes scripted seats. `import --as-anchor|--as-view` creates the owner's request first (R-96a); `--watch <folder>` imports new files for one request (`--watch-timeout`, `--watch-max`); `--no-check` defers the verdicts. `preflight` prints the image seat's tiers as `image seat I`.
13. **Carry-overs done.** (a) Verification packets now carry BEFORE and AFTER close-ups framed in the finding's own view (`closeup:<part>@<direction>`, `render.closeup_view_def(part, direction)`), rendered for that packet only (not part of the gate set); the BEFORE close-up is rendered from the attempt's base revision, which is immutable, so it is a fresh render of the old file. (b) Preflight probe spend counts toward the monetary cap: `LimitTracker.note_external_cost("preflight_probe", usage)` enters measured or estimated totals (unknown stays unknown), per-agent `probe_cost` is stored in the preflight report, and `start()` and `load_preflight()` seed the run tracker from the stored reports so a later process still counts it; `status` shows it under `consumption.external`.
14. **Carry-overs not done.** (c) claude activity signal: `claude --help` (2.1.233) lists `--output-format stream-json` and `--include-partial-messages`, and states that stream-json in print mode requires `--verbose`; the change touches the live-verified adapter and can only be verified by a paid probe, so it stays a Phase 4 candidate with those facts recorded. (d) Upgrading the global codex package (npm 0.147.0 on PATH; the desktop app's 0.153.1 is used through `builder.agents.B.executable`) is the owner's decision.

Phase 2b delivered: `builder/concept.py`, `builder/providers/imagegen/{__init__,base,manual}.py`, record kinds and the canon machine, schemas `art_director_prompt`, `canon_description`, `consistency_verdict`, `anchor_pick` (strict-form compatible, checked by test), config keys `builder.concept` and `builder.image_generation`, engine hooks (approved-only packets, intake guard, image-seat preflight, status summary, study hook, aligned close-ups, probe spend), CLI `concept` verbs. Tests: 46 new deterministic tests (43 across `tests/test_concept_records.py`, `test_imagegen.py`, `test_concept.py`, `test_concept_engine.py`, `test_concept_cli.py`, plus two in `test_closeups.py` and one in `test_engine.py`); no new Blender or live test. The owner-driven end-to-end pass with real images awaits approval (proposal below).

**Owner-driven end-to-end proposal (awaiting approval).** Config: `builder.limits: {max_requests: 12, max_cost_usd: 12, wall_clock_minutes: 90}`, `builder.concept: {approval: each, views: [front, side], anchor_candidates: 2, max_images: 8, max_regenerations_per_view: 1}`, `builder.agents.B.executable` at the desktop app's codex 0.153.1. Steps: `new` a project in a scratch directory; `preflight <wf> --live` only if the stored reports are stale (5 to 10 calls per agent, about 0.5 to 0.8 USD per claude probe); `concept start <wf> --from-text "..."` (1 claude call, the anchor prompt); the owner generates 2 candidates in ChatGPT or Gemini and runs `concept import` (0 calls); `concept approve <ref>` (1 claude call for the canon description, 2 for the two view prompts); the owner generates and imports each view (2 claude verdicts + 2 codex verdicts); `concept approve` both (0 calls) or, if a view is inconsistent, one regeneration (1 claude prompt, then 2 more verdicts); `start <wf> --max-steps 2` to see intake run on the approved set (2 calls) or stop before it. Expected: 9 to 12 LLM calls, of which 6 to 8 claude at 2 to 3 USD each by the CLI's own estimate (intake-sized calls; verdict packets are smaller, so likely less) and 3 to 4 codex at unknown cost. Enforceable by Alloy: `max_requests`, `max_images`, `max_regenerations_per_view`, wall clock; by claude: `--max-budget-usd` per invocation (an estimate, and it stops a call only after the spend, so the tracker also refuses to dispatch when the remaining budget is below the agent's largest reported call). Not enforceable: codex cost. The image seat costs nothing to Alloy and whatever the owner's app subscription charges, recorded as not applicable.

## 14. Concept stage, seats, and roles (design for Addendum A, 2026-09-07)

Spec: `docs/specs/collaborative-model-builder-addendum-concept-stage.md`. Owner decisions: image generation by an OpenAI or Google image model as a third seat; roles assigned per task with one seat able to hold several; approval modes `each` (default), `anchor_only`, `auto`; modeling adapters first (Phase 2a), then the concept stage (Phase 2b).

**Modules.** `builder/concept.py` (canon, generation requests, coverage of the needed set, approval, conflicts, studies), `builder/providers/imagegen/manual.py` (the default seat: writes `concept/requests/<id>/PROMPT.md` with the prompt and the attachment paths, imports files with manifests), `builder/providers/imagegen/base.py` (seat contract so an optional API wrapper can be added later without touching the concept loop). `builder/roles.py` holds the role table and the assignment rules that `engine._assign` uses today.

**Records.** `reference` gains `canon_state` (candidate, approved, superseded, rejected), `derived_from` (anchor id), `generation_id`, `precedence` (owner_target 0, anchor 1, turnaround 2, study 3). New kinds: `generation` (R-96 manifest), `canon_description` (R-101, versioned), `evidence_conflict` (R-95; states open, resolved_by_user, resolved_by_regeneration), `study_request` (R-104; states requested, generated, checked, approved, rejected, withdrawn), `concept_plan` (needed views and parts with coverage).

**State.** A fifth machine, canon: `no_canon -> anchor_pending -> anchor_approved -> turnaround_pending -> turnaround_approved -> complete`, with `complete` also reachable from `turnaround_pending` by the owner's explicit `proceed` (recorded). Intake's guard requires canon `complete`, or a project whose references were all owner-supplied.

**Roles.** Table: observer, builder, reviewer, verifier, reassessor (existing duties), art_director, image_generator. Rules: the art director is seat A unless overridden or after two rejected rounds; the reviewer and verifier are never the seat that made the operation; consistency verdicts on generated images come from both LLM seats independently; the image seat holds only image_generator. Every assignment records a one-line rationale (R-107).

**Concept loop.** From text: art-director prompt -> N anchor candidates -> pick (owner or auto with criteria) -> canon description -> per-view generation conditioned on the anchor -> independent consistency verdicts -> approve, regenerate (bounded), or escalate -> coverage check -> intake. From images: seeds are approved targets; the anchor is the first; the same loop fills missing views. During modeling, `gather evidence` may raise a study request that follows the same generate, check, approve path and attaches to the part.

**Manual seat contract (default).** A generation request record holds the prompt the art director wrote, the attachment list (image ids with hashes and the paths the owner should upload), the target (anchor candidate, view name, or part study), and its state (open, imported, checked, approved, rejected, abandoned). `concept prompts` prints open requests; the owner generates in ChatGPT or Gemini and runs `concept import <request_id> <files>`, optionally declaring vendor and model. Import copies originals unmodified under `refs/generated/`, hashes them, writes the manifest with the declarations marked as declared, and marks the images `candidate`. Nothing about the vendor's behavior is assumed: Alloy checks consistency with both LLM seats and the owner approves per mode. An API seat, if ever added, implements the same contract behind `process.py` with keys from the environment only.

**Approval in the CLI.** `concept` verbs per R-110; in `each` mode the CLI prints candidates as file paths with the seats' verdicts and waits for `concept approve`; the GUI panel arrives in Phase 3.

**Tests.** The matrix in Addendum A.4; the mock image seat returns fixture PNGs (for example the truth renders of the fixture lamp) with manifests, so the D tests can prove precedence, conflicts, approval modes, coverage, and key hygiene without spending.

## 13. Questions for the owner (answers change construction)

1. **Default workflow directory for any project.** Every workflow project's state (SQLite, revisions, renders, staging, checkpoints, logs) needs a home. Options: (a) `<project_dir>\alloy-builder\<project-slug>\`, next to the user's art source, created only when the project is created; (b) `%LOCALAPPDATA%\Alloy\builder\<slug>\`, keeping the user's project untouched until a revision is exported; (c) a path given per project (`new --workflow-dir`, or `workflow_dir` in a preset), with `builder.workflow_root` as the fallback. My default is (a) because the editable `.blend` deliverable belongs near the art source; it stays out of the user's git only if they ignore it there. For E10 Bearer this would be `C:\Death Factory\alloy-builder\e10-bearer-head\`, created only during an authorized run. Phase 1 fixtures use temporary directories either way.
2. **Reviewer isolation cost.** When the reviewer's persistent session has already seen the builder's self-assessment, R-11 calls for an isolated fresh session with a reconstructed packet. That doubles context delivery for those reviews. Confirm that fidelity wins over token cost here (my default), or allow a config switch `builder.isolated_reviews: always | when_contaminated | never` with `when_contaminated` as default.

Routine decisions made without asking are listed in Section 11.

---

## Appendix A. Verification commands and results (2026-09-07)

```
python --version                                  -> Python 3.14.0 (C:\Python314\python.exe)
python -m pip list | grep ...                      -> rich 14.2.0, prompt_toolkit 3.0.52, PyYAML 6.0.3, ruamel.yaml 0.19.1,
                                                     pillow 12.0.0, pytest 9.0.2, numpy 2.3.5; PyInstaller: ModuleNotFoundError
python -c "import tkinter; print(tkinter.TkVersion)" -> 8.6
git rev-parse HEAD                                 -> 593df76b8efdf2676c3fb80f1e13e15da3d67efd
git status --porcelain                             -> 12 modified, untracked: actions.py context.py docs/ invocation.py nul sessions.py tools.py
git config --get core.autocrlf                     -> true ; file <tracked files> -> no CRLF (LF working copies)
blender.exe -b --version                           -> Blender 5.2.1 LTS (hash 9e2066aef7ef built 2026-08-25 02:38:20)
blender.exe --help | grep python-exit-code         -> "--python-exit-code <code>  Set the exit-code in [0..255] to exit if a Python exception is raised"
blender.exe -b -E help                             -> BLENDER_EEVEE, BLENDER_WORKBENCH, CYCLES
blender.exe -b --factory-startup --python-exit-code 3 --python-expr "raise RuntimeError('boom')"   -> exit 3
blender.exe -b --factory-startup --python-expr "raise RuntimeError('boom')"                        -> exit 0
python blender_probe_outer.py (argv list, PYTHONUTF8=1)  -> ARGV ['C:\\tmp dir\\ü nï 名.json', 'a&b|c^d%e "q" \'sq\''] ; rc 0
render probe (cube, camera, sun; 320x240)          -> BLENDER_WORKBENCH ok 1.44 s ; BLENDER_EEVEE ok 2.67 s ; alloy_id on object and
                                                     material read back after save+reopen ; saved "...\probe ü 名.blend" ;
                                                     DeprecationWarning: 'Material.use_nodes' is expected to be removed in Blender 6.0
which claude gemini codex copilot ollama           -> claude, gemini, codex found ; copilot, ollama NOT FOUND
claude --version / gemini --version / codex --version -> 2.1.233 / 0.55.1 / codex-cli 0.147.0
claude --help, gemini --help, codex exec --help, codex exec resume --help -> flags as summarized in Section 1.3
python -c "import sqlite3; ..."                    -> sqlite 3.50.4, json_extract ok
python -c "import ctypes; ..."                     -> CreateJobObjectW, AssignProcessToJobObject, TerminateJobObject available
which taskkill tasklist powershell                 -> all present
stat "C:\Death Factory\docs\art\locked-theme-concept-v1.png"                       -> 2661938 bytes, mtime 2026-08-28 21:56:25.540282000 -0500
stat "C:\Death Factory\art-source\blender\E10\E10_Bearer_v4_proportions.blend"    -> 1716114 bytes, mtime 2026-09-07 11:18:24.049604600 -0500
```

Online sources read: code.claude.com/docs/en/headless (Claude Code print mode), learn.chatgpt.com/docs/config-file/config-reference and learn.chatgpt.com/docs/non-interactive-mode (Codex, reached by redirect from developers.openai.com), geminicli.com/docs/cli/headless and geminicli.com/docs/cli/cli-reference (Gemini CLI). No provider was invoked; nothing was spent.
