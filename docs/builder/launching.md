# Launching the builder

Purpose: the entry points (CLI and the app's Model Builder view), what each accepts, and what each refuses to do on its own.

## The CLI

One entry point, `builder/cli.py::main` (R-90), from the app's folder or through the `builder.cmd` launcher at the repo root (it `pushd`s into the app's folder, points `ALLOY_BUILDER_CONFIG` at `sessions\builder.json`, and returns the Python exit code):

```text
python -m builder <verb> [options]
builder.cmd <verb> [options]
```

Verbs (from `python -m builder --help`):

```text
new, open, intake, source, preset, preflight, start, resume, pause, cancel, feedback, status, findings,
accept, reopen, waive, checkpoint, limits, journal, concept, harness, fixture
```

`source register "<wf>" "<file.blend>" [--note ...]` and `source empty "<wf>"` register revision 0 for a project created with `new` (see [output-layout.md](output-layout.md)); `start --assign role=SEAT` (repeatable) pre-assigns roles for one run (see [assignments-and-handoffs.md](assignments-and-handoffs.md)).

Every verb except `new`, `preset`, `fixture`, and `harness run` takes the workflow directory as its first positional argument and `--config <file.json|file.yaml>` to read builder settings from another file (default: `$ALLOY_BUILDER_CONFIG`, then `.\sessions\builder.json` when it exists, else the built-in defaults; see [installation-and-blender.md](installation-and-blender.md)).

```text
python -m builder --help
python -m builder start --help
python -m builder concept import --help
builder.cmd --help
```

Stdout is reconfigured to UTF-8 with replacement so Windows console encodings never break output.

Exit codes: `0` on success; `1` when `preflight` reports a blocked agent, or when `start`/`resume` stop with a reason other than `ready_for_user_review` or `user_pause`; `2` for a usage or engine error (printed as `error: ...`).

Agents: real adapters are built from `agents` in the builder settings file (`sessions\builder.json`, key `builder`) (`claude`, `codex`, or `gemini`; anything else is refused with the supported list). `--mock <screenplay.json>` on `preflight`, `start`, `resume`, `harness run`, and the spending `concept` verbs binds scripted mock agents instead; mocks state exactly what they script and never spend anything.

## The Model Builder view

The builder is a view of the Alloy window: the cube button at the top of the app nav swaps the chat surface for it (`python app.py`, then the cube). The chat keeps running underneath; switching back never touches a build, and a run started in the view keeps going while it is hidden (the nav button's dot pulses while a job runs and stays lit when a decision waits for you).

- **New…** creates a workflow folder like the CLI's `new` (or `preset create` when a preset is chosen), then opens it; nothing is read from the references or sources (R-93). **Demo fixture…** in the same dialog builds the fixture lamp on real Blender with scripted seats and spends nothing. **Open…** and **Recent…** open an existing workflow folder (the one holding `project.json`); **Close** closes it (a run in progress must be paused or cancelled first).
- Left column: **Project** (project facts, Blender availability, references with their regions, Add image…, revision 0), **Agents** (both seats with the declared / local / live capability tiers, Preflight (local, free) and Preflight (live)… behind a confirmation that states what it spends), **Run** (stage, task and ownership, `QUESTION for you:` lines, the controls, the Limits card, consumption).
- Centre: the comparison panes (reference / render, or before / after of the selected finding), zoom, and the measured-boxes overlay (drawn only from a measurement, R-68). Every pane states its render or reference ids and the presentation scale; a stale render says `STALE (<reason>): not current evidence (R-64)`.
- Right column: **Parts** (hierarchy with construction relations as child rows), **Findings** (selecting one flips the comparison to before / after and selects its part), **Coverage**, **Activity** (the engine's events as they happen).

The view never computes anything itself: `builder_host.py` owns one `BuilderSession` per app process (the same worker thread the Tk window used), and every button only submits a job; results arrive as `builder` events (`busy`, `done`, `error`, `event`, `snapshot`, `closed`). Pause, Cancel and Feedback are never disabled by a running job: they are the way out of one (R-59). Closing the app cancels a run in flight (its provider and Blender processes are in kill-on-close Job Objects); reopening the project afterwards shows a recovery notice, and Resume runs recovery first (R-47).

Milestone 2 of the port adds the concept-approval panel, target regions by dragging on a reference, and the settings form; until then those go through the CLI and `sessions\builder.json`.

## What each entry point refuses to do implicitly

- No live probes on their own. `start`, `resume`, and the spending `concept` verbs (`start`, `import`, `approve`, `reject`, `regenerate`, `study`) call `load_preflight()` and require a stored preflight report for every configured agent whose cache key (CLI path, version, model, reasoning, adapter settings) still matches (R-18). When one is missing or stale, the CLI stops with:

```text
error: no current live preflight report for agent(s) A, B (never run, or the CLI path, version, model, or settings changed since; R-18). Run `preflight <wf> --live` first: it spends provider usage and is never started implicitly.
```

  The view shows the same message as the job's error. Only scripted mocks are probed on the spot (they are free).

- The live preflight is an explicit action: `preflight --live` in the CLI, or the "Preflight (live)..." button, which first shows a confirmation dialog stating what it invokes and the measured probe costs. "Preflight (local, free)" never spends.

- No fallback between providers or models (D3, R-20): `builder/providers/base.py::assert_argv_policy` refuses `--continue`, `--last`, `--fallback-model`, `--ephemeral`, `--no-session-persistence`, `-c` for claude/gemini, `-y` for gemini, and any `--dangerously-*` flag, and requires an explicit UUID after `--resume` or `codex exec resume`.

- No modeling without Blender: `start`, `resume`, `source register`, `source empty`, `fixture create`, and `harness run` refuse when Blender is not found (see [installation-and-blender.md](installation-and-blender.md)).

- `new`, `open`, `preset create`, and `status` never read a reference image or open a source `.blend` (R-93). Opening a source is the explicit `source register` step, and it only ever reads the file (it is validated in separate Blender processes and copied; the original's bytes and modification time are unchanged).

- A caveat found while reading `builder/engine.py::load_preflight`: the stored report is matched by cache key only; the check does not look at whether that report was produced with `--live`. A local-only `preflight` records a report with `ok: true` when the declared and local tiers pass (its live tiers stay `not_run`), so a later `start` is not refused by this check even though its message names `preflight --live`. The live tiers are then shown as `not_run` in `status` and in the Agents tab. This is what the code does; it has not been exercised live.

## Attended by default

`start` runs attended unless `--unattended` is given (D8). The view has an "attended" checkbox on the Run tab with the same meaning. See [unattended-and-limits.md](unattended-and-limits.md).

## Ctrl+C

`start` and `resume` run the engine in the foreground. Design Section 9.1 states that Ctrl+C requests a pause at the next safe boundary and a second Ctrl+C requests cancel; `builder/cli.py` as read contains no `KeyboardInterrupt` handler on that path (only `concept import --watch` catches it), so a Ctrl+C during a run ends the process with Python's default behaviour and the next `resume` runs recovery (see [pause-resume-recovery.md](pause-resume-recovery.md)). Use `pause` from a second console for a clean stop.
