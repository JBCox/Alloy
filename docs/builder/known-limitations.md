# Known limitations and unverified integrations

Purpose: what the builder does not enforce, what has not been verified live, and gaps found while writing these pages. Each item names its source: the design's construction notes (12a to 12d), the code as read on 2026-09-07, or both.

## Enforcement boundaries

- Script screening is a guardrail, not a sandbox (design Section 5 and 11 item 4; `builder/operations.py::screen_script`). Agent bpy scripts are rejected before they run when they import `os`, `subprocess`, `shutil`, `pathlib`, `sys`, `ctypes`, `socket`, `urllib`, `http`, `importlib`, `builtins`, `io`, `tempfile`, `glob`, `multiprocessing`, `threading`, `pickle`, `marshal`, `code`, or `runpy`; call `open(`, `exec(`, `eval(`, `compile(`, or `__import__`; use `bpy.ops.wm.save*`, `open*`, `link`, `append`, `read*`, `recover*`, `revert*`, `quit*`; access `bpy.data.libraries`; or use `bpy.app.handlers` or `bpy.app.timers`. The screening is line-based pattern matching; a script that passes it still runs with Blender's full Python. What enforces D4 is that the script only ever sees a staged copy, only Alloy saves, revisions are immutable, and validation gates promotion.
- The window between `CreateProcess` and Job Object assignment is a residual risk for process-tree cleanup (design Section 6.2).
- Read-only enforcement for agents relies on the CLIs' own modes (claude tools and permission mode, codex sandbox, gemini approval mode) and the write probe; a CLI that could only restrict by prompt would be labelled `prompt_only` and unsupported for shared write, but none of the shipped adapters is verified in that state.

## Provider status

- gemini 0.55.1 is unusable under the owner's account (`IneligibleTierError`, design 12b item 15). Nothing about the gemini adapter is live-verified: image delivery, resume by UUID, usage field names, and read-only enforcement are all unknown; the adapter declares them as `probe`.
- codex: on a long call (the E11 build, 15 to 18 minutes at `xhigh`) the CLI's stream to OpenAI dropped repeatedly ("Reconnecting... n/5 (stream disconnected before completion: websocket closed by server before response.completed)", then "Falling back from WebSockets to HTTPS transport") and the turn still completed. The adapter now treats `error` events as warnings (`codex stream notice (turn completed afterwards): ...`) when a `turn.completed` event follows; an `error` with no completed turn, and `turn.failed`, remain `provider_error` (fixed 2026-09-08 after one completed reply had been discarded). In the read-only sandbox the agent can still read any file on disk: during that build it read Alloy's own source (`operations.py`, `validate.py`, `apply_operation.py`) to learn the validation rules, which the packet already states; reads outside the packet are not prevented, only writes (R-17).
- codex: cost is unknown (the CLI reports no cost) and is never treated as zero. `gpt-6-astra` at `xhigh` is verified only with the Codex desktop app's `codex-cli 0.153.1` binary configured through `builder.agents.B.executable`; the npm `codex` 0.147.0 on PATH is refused by the API for that model. The codex model name is not reported in its JSONL events and is recorded as such. Upgrading the global npm package is the owner's decision (12c item 14d).
- claude: cost is the CLI's own `total_cost_usd`, a client-side estimate, tracked separately from measured values; there is no activity signal during a call (`--output-format json` prints nothing until the reply is complete), so the inactivity timer must not be shorter than the response timeout for claude and a long call cannot be told apart from a hung one until the response deadline (design 12b item 15, 12c item 14c). A `stream-json` activity signal is a recorded candidate, not built.

## Caps act after the spend

`--max-budget-usd` stops a claude call only after the money is spent; the recorded live run overran a 5 USD cap by about 2 USD (design 12b item 16). The tracker's largest-reported-call guard and the `budget_exhausted` outcome reduce, but do not eliminate, overruns; see [unattended-and-limits.md](unattended-and-limits.md). `max_cost_usd` is labelled not enforceable whenever any configured provider reports no cost.

## The manual image seat

Vendor, model, and which images were attached are declarations by the owner; Alloy cannot verify them and records them as declarations (D12, R-96a). The image seat's live tier is `not_applicable`. Cost for the image seat is `not_applicable`, never zero; whatever the owner's app subscription charges is outside Alloy. An API image seat does not exist in this build; `image_generation.seat` accepts only `manual`.

## Packaging and launch scripts

- PyInstaller is not installed and a packaged build has not been verified (design Section 1.1). While these pages were being written, `alloy.spec` gained the `builder` package and its in-Blender scripts as data, the builder and Pillow modules as hidden imports, and no longer excludes `PIL`; `tests/test_packaging.py` checks that spec as text only. Whether the bundled executable runs, finds Blender, launches the provider CLIs, or executes the in-Blender scripts from the bundle's files has not been tried.
- `scripts/builder.bat` and `scripts/model-builder.bat` appeared in the working tree at the same time; they wrap `python -m builder` and `python -m gui.builder_view` and were not run here.

## Gaps found while reading the code

- A project created with `new` has no starting revision until `source register` or `source empty` registers one (added after Phase 4; see [output-layout.md](output-layout.md)). The preset's `existing_source` is still only recorded: registering it is the explicit `source register` step during an authorized run. Without a revision the build stage pauses with `execution_failure` ("no revision exists; the fixture or intake must register revision 0").
- A local-only `preflight` report (live tiers `not_run`) never satisfies `start`, `resume`, or a spending `concept` verb with real adapters: the engine, the CLI, and the GUI session all refuse with the `preflight --live` remediation (R-18, R-21; fixed in Phase 4 after this gap was found, `tests/test_cli.py::test_local_only_preflight_report_never_satisfies_start_with_real_adapters`).
- Ctrl+C during `start` or `resume` requests a pause at the next safe boundary; a second Ctrl+C requests cancel (design Section 9.1; `builder/cli.py::interrupt_handler`, added in Phase 4). The handler is installed only on the main thread; elsewhere the default KeyboardInterrupt applies and the next `resume` runs recovery.
- There is no separate recovery verb: reconciliation runs only inside `resume` (CLI) or Resume (GUI), which then continues the run at once.
- Reference replacement (`replaces_id`), marking an unresolved ambiguity, and whole-model acceptance exist in the engine or records but have no CLI verb or GUI control. Assignment overrides have `start --assign` and the Run tab entry (added after Phase 4); the concept stage's art director is not overridable from either.
- Reference-region drawing exists in the GUI (`ImagePane.canvas_to_original`, "Draw region on reference"); design 12d item 13 still lists it as not built, so the design note is stale relative to the code.
- The design lists packets under `agents/<agent_id>/packets/` (Section 7.1); the code writes them under `packets/<agent_id>/`.

## What was and was not verified live (design 12b, 12c, 12d)

Verified live (owner-approved runs on the fixture, 2026-09-07): claude and codex preflight probes; intake for both agents; the brief; a plan call stopped by the budget cap. Not verified live: any build, review, correction, or verification with real agents (the fixture run stopped at the plan stage by decision); the concept stage with real images (the owner-driven end-to-end pass is proposed, not run); gemini in any form; the GUI with real adapters (the Phase 3 screenshots used scripted mocks and the fixture). Real-Blender behaviour (staging, validation, promotion, renders, measurements, recovery, cancellation) is covered by the `blender`-marked tests; the deterministic suite (297 tests collected on 2026-09-07) covers routing, state, limits, and recovery with mocks and fake CLIs. Mocks prove routing and state handling, never visual intelligence.
