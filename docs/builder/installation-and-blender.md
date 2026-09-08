# Installation, Blender discovery, and configuration

Purpose: what the builder needs installed, how it finds and checks Blender, where a project's workflow state lives, and every key of the `builder:` section of `config.yaml` with its default.

## Python and packages

- Python 3.14.0 is the interpreter the design was verified against (`C:\Python314\python.exe`, design Section 1.1). The builder uses only the standard library plus the packages in `requirements.txt`.
- `requirements.txt` lists: `rich>=13.0.0`, `prompt_toolkit>=3.0.0`, `pyyaml>=6.0`, `ruamel.yaml>=0.17` (optional; preserves `config.yaml` comments when the GUI settings editor saves), and `Pillow>=10.0` (reference intake, evidence crops, render manifests, the GUI previews).
- Install them into the interpreter you will run Alloy with:

```text
python -m pip install -r requirements.txt
```

- No API client, no numpy, no jsonschema, no psutil (design Section 11, item 12). Every provider is a CLI launched as an argv list (D3).
- PyInstaller is not installed on the verification machine and a packaged build has not been verified; see [known-limitations.md](known-limitations.md).

## Blender

### Discovery

`builder/config.py::discover_blender` decides which Blender runs:

1. If `builder.blender.executable` is set, that path is used and must exist. A configured path that does not exist yields "not found"; PATH is not searched as a fallback.
2. Otherwise the highest-versioned `C:\Program Files\Blender Foundation\Blender *\blender.exe` is chosen (the directory name's version number decides the order).

Blender 5.2.1 LTS at `C:\Program Files\Blender Foundation\Blender 5.2\blender.exe` is the version recorded in the design (Section 1.1). Its bundled Python is separate from Alloy's interpreter, so the in-Blender scripts under `builder/blender/scripts/` import nothing from Alloy; the bridge is argv plus JSON files (spec Section 5).

When Blender is not found, verbs that need it (`start`, `resume`, `source register`, `source empty`, `fixture create`, `harness run`) stop with:

```text
error: Blender was not found; set builder.blender.executable in config.yaml or install Blender under C:\Program Files\Blender Foundation
```

`preflight` and the `concept` verbs open the runner as optional and continue without Blender.

For the test suite, `ALLOY_BLENDER_EXE` overrides discovery (`tests/conftest.py`); it is not read by the CLI.

### Invocation

Every Blender call is the argv list below, never a shell string (`builder/blender/runner.py::BlenderRunner.argv`):

```text
blender.exe -b --factory-startup -noaudio --python-exit-code 3 [<file.blend>] --python <script.py> -- <args.json>
```

`--python-exit-code 3` is mandatory: without it a Python exception inside Blender exits 0 (design Section 1.1). The runner classifies a script run as `ok` (exit 0 and a `result.json` with `ok: true` for this operation id), `failed` (exit 3, any other non-zero exit, or `ok: false`), `uncertain` (exit 0 without a readable result, never treated as success), or `timeout`/`cancelled`/`spawn_failed` from the process layer.

### The smoke test

`preflight` (with or without `--live`) runs `BlenderRunner.smoke()` when Blender is available:

- `builder/blender/scripts/smoke.py` reports the Blender version, its Python version, and a set-and-read test of the render engines.
- `blender.exe -b -E help` lists the engine identifiers (`engines_listed`).
- The result is stored in the preflight report under `blender` and printed as:

```text
blender: <version> (python <version>); engines ok: BLENDER_WORKBENCH, BLENDER_EEVEE
```

A failing smoke test raises `BlenderError("Blender smoke test failed ...")` and the verb stops.

### Deadlines

Each operation kind has its own deadline in seconds (`builder.blender.deadlines`; defaults from `builder/config.py::DEFAULT_DEADLINES`):

| key | default | used by |
|---|---|---|
| `validate` | 120 | identity enumeration, validation, the smoke test |
| `apply` | 600 | an agent's operation script against the staged copy |
| `render` | 900 | one view render |
| `measure` | 120 | bounding boxes, distances, ratios, overlaps |
| `fixture` | 300 | building the fixture scenes |

A Blender process gets only its operation deadline; the provider inactivity timer is not applied to it, so a quiet render is never mistaken for a stalled chat (R-22). On deadline or cancel the process tree is killed (Windows Job Object, then `taskkill /T /F`, then confirmation) and the operation becomes `failed` or `uncertain` with its staged output quarantined (see [pause-resume-recovery.md](pause-resume-recovery.md)).

## Where workflow state lives

A workflow project is a directory (`workflow_dir`) containing `project.json`, `builder.sqlite3`, and the subdirectories listed in [output-layout.md](output-layout.md). It is chosen, in order:

1. `new --workflow-dir <dir>` or `preset create --workflow-dir <dir>`.
2. A preset's own `workflow_dir` key (`preset create` only).
3. `builder.workflow_root` in `config.yaml`, when not empty: `<workflow_root>\<slug>`.
4. Otherwise `<project_dir>\alloy-builder\<slug>`, where `project_dir` is `new --project-dir` (or the preset's `project_dir`) and, when neither is given, the current working directory.

`<slug>` is `slugify(--name)` for `new` and `slugify(<preset_id>)` for `preset create` (ASCII lower case, non-alphanumerics collapsed to `-`). Creating a project never reads a reference image or opens a source `.blend` (R-93); intake is a separate step.

## The `builder:` section of `config.yaml`

Parsed leniently by `builder/config.py::BuilderConfig.from_dict` (D9): a missing key means the default; an unknown sub-key is kept in `raw`, ignored, and reported in `warnings`; a value of the wrong type falls back to the default with a warning. Every verb accepts `--config <path>` to read a different `config.yaml`; without it Alloy's normal discovery (`Config.get_config_path()`) is used.

| key | default | meaning |
|---|---|---|
| `workflow_root` | `""` | Root for workflow directories when `--workflow-dir` is not given (see above). |
| `attended` | `true` | Attended by default; `start --unattended` opts out (D8, R-76). |
| `isolated_reviews` | `when_contaminated` | `when_contaminated` or `always`: when a review runs in a fresh isolated session (R-11). |
| `blender.executable` | `""` | Blender path; empty means auto-detect. |
| `blender.deadlines` | `{validate: 120, apply: 600, render: 900, measure: 120, fixture: 300}` | Per-operation deadlines in seconds. |
| `agents.A` | `{provider: claude, model: fable, reasoning: max, executable: ""}` | Seat A binding. |
| `agents.B` | `{provider: codex, model: gpt-6-astra, reasoning: xhigh, executable: ""}` | Seat B binding. Only `A` and `B` exist; other labels are ignored with a warning. |
| `provider_timeouts` | `{response: 900, inactivity: 180, cancel_probe_after: 8}` | Seconds: total response deadline, no-output detection, and when the live cancellation probe fires. |
| `limits` | `{wall_clock_minutes: 0, max_cost_usd: 0, max_requests: 0, max_renders: 0, attempts_per_finding: 2, stall_steps: 12}` | Zero means unlimited (except `attempts_per_finding` and `stall_steps`, where zero disables the check). See [unattended-and-limits.md](unattended-and-limits.md). |
| `concept.approval` | `each` | `each`, `anchor_only`, or `auto` (D11). |
| `concept.anchor_candidates` | `4` | Anchor candidates requested per round (R-100). |
| `concept.views` | `[front, side, rear, top, underside, three-quarter]` | The needed turnaround views (R-103). A comma-separated string is also accepted. |
| `concept.max_images` | `40` | Imported and generated images counted alike (R-105). |
| `concept.max_regenerations_per_view` | `3` | Rounds beyond the first per view or study (R-105). |
| `concept.import_dir` | `""` | Empty means `<workflow_dir>\concept\imports`; used by the image-seat preflight. |
| `image_generation` | `{seat: manual, vendor: chatgpt, model: ""}` | The image seat I. Only `manual` exists in this build; any other seat value falls back to `manual` with a warning (D12). |
| `assignments` | `{}` | Per-run role overrides, `{role: seat}` with roles `brief`, `plan`, `build`, `corrector`, `reviewer`, `verifier`, `reassessor` and seats `A`/`B`; unknown roles or seats are dropped with a warning. `start --assign role=SEAT` wins over this key. A seat never reviews, verifies, or reassesses its own operation, even under an override (R-107). See [assignments-and-handoffs.md](assignments-and-handoffs.md). |
| `presets.<id>` | the `e10-bearer-head` preset shipped in `config.yaml` | Saved intake data for `preset create` (R-93). |

The shipped `config.yaml` carries the same defaults plus the `e10-bearer-head` preset (`project_dir`, `asset`, `target_reference`, `target_region` with `bbox: null`, `existing_source`, `existing_source_trust`, `first_component`, `rejected_references`). The preset's target region is chosen by the user during an authorized run and is never guessed (R-93).

### The settings editor

Alloy's Tk settings editor (`gui/settings.py`, Builder tab) exposes: `attended`, `isolated_reviews`, `blender.executable`, both seats' `provider`, `model`, `reasoning`, `executable`, the limits `wall_clock_minutes`, `max_cost_usd`, `max_requests`, `max_renders`, `attempts_per_finding`, the `concept` keys, and `image_generation`. What it collects is deep-merged onto the existing `builder` mapping, so `presets`, `blender.deadlines`, `provider_timeouts`, `workflow_root`, `assignments`, and `limits.stall_steps` survive a save even though the tab does not show them (`tests/test_settings_builder_tab.py`). `stall_steps` is edited in `config.yaml`, with `limits --set stall_steps=<n>`, or in the GUI Limits card.

Alloy's own `config.py` keeps the `builder` key as a raw dict and writes it back in `Config.save()`; the only caller of `save()` is "Reset to defaults", which discards it on purpose (design Section 9.3).
