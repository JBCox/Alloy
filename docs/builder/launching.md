# Launching the builder

Purpose: the entry points (CLI and GUI), what each accepts, and what each refuses to do on its own.

## The CLI

Two equivalent spellings; both run `builder/cli.py::main` (R-90). `main.py` hands everything after `--build` to the builder's own parser (`argparse.REMAINDER`), so the two lines below are the same command:

```text
python -m builder <verb> [options]
python main.py --build <verb> [options]
```

Verbs (from `python -m builder --help`):

```text
new, open, intake, source, preset, preflight, start, resume, pause, cancel, feedback, status, findings,
accept, reopen, waive, checkpoint, limits, journal, concept, harness, fixture
```

`source register "<wf>" "<file.blend>" [--note ...]` and `source empty "<wf>"` register revision 0 for a project created with `new` (see [output-layout.md](output-layout.md)); `start --assign role=SEAT` (repeatable) pre-assigns roles for one run (see [assignments-and-handoffs.md](assignments-and-handoffs.md)).

Every verb except `new`, `preset`, `fixture`, and `harness run` takes the workflow directory as its first positional argument and `--config <config.yaml>` to read builder settings from a file other than the one Alloy discovers. `--help` works on every verb and sub-verb:

```text
python -m builder --help
python -m builder start --help
python -m builder concept import --help
python main.py --build --help
```

Stdout is reconfigured to UTF-8 with replacement so Windows console encodings never break output.

Exit codes: `0` on success; `1` when `preflight` reports a blocked agent, or when `start`/`resume` stop with a reason other than `ready_for_user_review` or `user_pause`; `2` for a usage or engine error (printed as `error: ...`).

Agents: real adapters are built from `builder.agents` in `config.yaml` (`claude`, `codex`, or `gemini`; anything else is refused with the supported list). `--mock <screenplay.json>` on `preflight`, `start`, `resume`, `harness run`, and the spending `concept` verbs binds scripted mock agents instead; mocks state exactly what they script and never spend anything.

## The Tk window

Three ways to open `gui/builder_view.py::BuilderWindow`:

1. From the chat app:

```text
python main.py --gui
```

then View > Model Builder... (the menu entry imports `gui.builder_view` lazily, so the chat app's startup is unchanged). The window opens with no project; type or browse the workflow directory in the top bar and click Open.

2. Standalone, without the chat app:

```text
python -m gui.builder_view "<workflow_dir>"
python -m gui.builder_view "<workflow_dir>" "<screenplay.json>"
```

The optional second argument binds scripted mock agents, exactly as `--mock` does in the CLI.

3. Launch scripts (`scripts/`). Both `pushd` into Alloy's own folder so `config.yaml` and the packages are found from any working directory, forward every argument, and return the Python exit code:

```text
scripts\builder.bat <verb> ...                          rem python -m builder %*
scripts\builder.bat --help
scripts\model-builder.bat [workflow_dir] [screenplay.json]   rem python -m gui.builder_view %*
```

The second argument of `model-builder.bat` binds scripted mock seats, as above. The scripts appeared in the working tree while these pages were being written (`tests/test_packaging.py` checks that `builder.bat --help` prints the verb list); they were not run here.

The window runs the engine on a worker thread (`builder/viewmodel.py::BuilderSession`) and drains its event queue every 50 ms; provider and Blender work never runs on the Tk thread, so the window stays responsive and shows `working: <job>` in the status bar (R-92). Its tabs: Project (references, region drawing), Agents (preflight tiers, effective reasoning), Run (stage, controls, Limits card, consumption) on the left; the comparison panel in the centre; Parts, Findings, Coverage, Concept, Activity on the right.

## What each entry point refuses to do implicitly

- No live probes on their own. `start`, `resume`, and the spending `concept` verbs (`start`, `import`, `approve`, `reject`, `regenerate`, `study`) call `load_preflight()` and require a stored preflight report for every configured agent whose cache key (CLI path, version, model, reasoning, adapter settings) still matches (R-18). When one is missing or stale, the CLI stops with:

```text
error: no current live preflight report for agent(s) A, B (never run, or the CLI path, version, model, or settings changed since; R-18). Run `preflight <wf> --live` first: it spends provider usage and is never started implicitly.
```

  The GUI raises the same message in its Start/Resume/concept jobs. Only scripted mocks are probed on the spot (they are free).

- The live preflight is an explicit action: `preflight --live` in the CLI, or the "Preflight (live)..." button, which first shows a confirmation dialog stating what it invokes and the measured probe costs. "Preflight (local, free)" never spends.

- No fallback between providers or models (D3, R-20): `builder/providers/base.py::assert_argv_policy` refuses `--continue`, `--last`, `--fallback-model`, `--ephemeral`, `--no-session-persistence`, `-c` for claude/gemini, `-y` for gemini, and any `--dangerously-*` flag, and requires an explicit UUID after `--resume` or `codex exec resume`.

- No modeling without Blender: `start`, `resume`, `source register`, `source empty`, `fixture create`, and `harness run` refuse when Blender is not found (see [installation-and-blender.md](installation-and-blender.md)).

- `new`, `open`, `preset create`, and `status` never read a reference image or open a source `.blend` (R-93). Opening a source is the explicit `source register` step, and it only ever reads the file (it is validated in separate Blender processes and copied; the original's bytes and modification time are unchanged).

- A caveat found while reading `builder/engine.py::load_preflight`: the stored report is matched by cache key only; the check does not look at whether that report was produced with `--live`. A local-only `preflight` records a report with `ok: true` when the declared and local tiers pass (its live tiers stay `not_run`), so a later `start` is not refused by this check even though its message names `preflight --live`. The live tiers are then shown as `not_run` in `status` and in the Agents tab. This is what the code does; it has not been exercised live.

## Attended by default

`start` runs attended unless `--unattended` is given (D8). The GUI has an "attended" checkbox on the Run tab with the same meaning. See [unattended-and-limits.md](unattended-and-limits.md).

## Ctrl+C

`start` and `resume` run the engine in the foreground. Design Section 9.1 states that Ctrl+C requests a pause at the next safe boundary and a second Ctrl+C requests cancel; `builder/cli.py` as read contains no `KeyboardInterrupt` handler on that path (only `concept import --watch` catches it), so a Ctrl+C during a run ends the process with Python's default behaviour and the next `resume` runs recovery (see [pause-resume-recovery.md](pause-resume-recovery.md)). Use `pause` from a second console for a clean stop.
