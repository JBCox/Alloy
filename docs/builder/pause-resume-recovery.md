# Pause, resume, restart recovery, and checkpoints

Purpose: the control requests (pause, resume, cancel, feedback) and where they take effect; what the engine does on restart after a crash or kill; how checkpoints are created and restored.

## Control requests and safe boundaries (R-59)

`start` and `resume` run the engine in the foreground of one process. From another console, these verbs write a `control_requests` row in `builder.sqlite3` that the running engine consumes at its next safe boundary (between loop steps, before any dispatch):

```text
python -m builder pause "<wf>"
python -m builder cancel "<wf>"
python -m builder feedback "<wf>" "the hood is too shallow; compare the side view"
python -m builder limits "<wf>" --set max_requests=30
```

Each prints `<verb> request #<seq> recorded; a running engine applies it at the next safe boundary`. Conflicting instructions are never injected into an active write: a provider call or Blender operation that is in flight finishes (or is cancelled as a whole, below) before the request applies.

- Pause: the loop stops before the next step with execution state `paused` and stop reason `user_pause` ("pause requested by the user; stopped at a safe boundary"). Consumption is persisted on the run record.
- Cancel: the engine's cancel event is set, the in-flight provider or Blender process is killed as a process tree (Windows Job Object, then `taskkill /T /F`, then confirmation), and the run becomes `cancelled` with stop reason `user_cancel`. A cancelled operation's staged output is quarantined, never promoted.
- Feedback: recorded immediately as a `feedback` record (`received`), marked `applied` at the next boundary with the stage it landed on, and added to the run's `user_feedback` list, which the next build packet lists under constraints as `User feedback: ...`.
- Limits: see [unattended-and-limits.md](unattended-and-limits.md).

In the GUI: Pause (a thread-safe flag the engine reads at its next step boundary), Cancel (confirmed; kills the in-flight process and cancels after reconciliation), Feedback, and the Limits card; each goes through the same store rows or the engine's own flags.

Resume:

```text
python -m builder resume "<wf>"
```

`resume` re-checks the limits (a reached limit refuses the `paused -> running` transition with "a limit is reached; raise the limit before resuming"), clears the stop reason, and runs until the next stop. `start` on a project that already has a run creates a new run record; use `resume` to continue.

## What happens on restart (R-23, R-47)

There is no separate recovery verb: `resume` (and the GUI's Resume when the project was opened with a non-terminal run) calls `Engine.recover()` before continuing and prints the report as `recovery: {...}` when any operation was reconciled.

### Reconciliation of in-flight operations

`builder/operations.py::reconcile_all` examines every operation still in `created`, `staged`, `running`, `validating`, or `promoting` against the journal, the staging directory, the revisions directory, and the live process table, and classifies each:

| classification | when | effect |
|---|---|---|
| `committed` | state `promoting`, and the announced revision file exists with the announced hash | the commit transaction is completed (the crash fell between the file move and the record) |
| `failed` (promotion) | state `promoting` but the file is missing or its hash differs | the file, if present, is moved to `staging/_quarantine/<op_id>/`; the operation fails with "promotion did not complete" |
| `uncertain` | a staged `out.blend` exists without a verified result | the whole staging directory is moved to `staging/_quarantine/<op_id>/` ("quarantined, never replayed") |
| `failed` (interrupted) | a PID was recorded, or the state was `running`/`validating`, and no output exists | the staging directory is removed; `recovery: interrupted`; "nothing was promoted; the base revision is unchanged" |
| `never_started` | nothing was staged | the operation is `cancelled` with "never started (no staged output); safe to recreate" |

No mutation is replayed. Quarantined outputs are never deleted automatically; they are kept for diagnosis and named in the next packet.

### Recorded PIDs and orphan processes

`Operations.execute` records the Blender PID on the operation (`op.spawned`) the moment the process starts. During reconciliation `_reap_orphan` checks whether that PID is still alive and, because PIDs are reused, kills it only when its command line still names this operation id; a live PID whose command line does not name the operation is left alone and journaled as `op.orphan_not_killed`. A kill is confirmed (`op.orphan_killed` with `confirmed` and any survivors).

### Tasks and findings

`Engine._reset_interrupted` then returns the loop to a resumable state: tasks still `in_progress` become `blocked` with `interrupted: true` and a note that nothing from them is assumed to have applied; the interrupted holder's ownership grant is released so the retry gets a fresh token; findings still `correcting` return to `open` with a `recovery_notes` entry ("correction attempt interrupted or uncertain ...; the base revision is unchanged and the next attempt starts from it"), and their attempt is marked `uncertain`.

### The run state after recovery

`recover()` moves the run through `recovering` to `paused`: with stop reason `execution_failure` when anything was `uncertain` ("N uncertain operation(s) quarantined"), otherwise `user_pause` ("recovered; resume to continue"). The CLI's `resume` then continues at once; to inspect first, read `status` and the journal before resuming, keeping in mind that recovery itself only runs inside `resume`.

### The next packet

The retried build or correction packet contains the section "Interrupted operations against the current base revision (never applied)": for each operation that ended `uncertain`, `cancelled`, or interrupted against the current base, its intent, error, quarantine path, and the instruction "Do not assume any of its edits exist; start from the evidence in this packet." See [assignments-and-handoffs.md](assignments-and-handoffs.md).

## Checkpoints and restore (R-46)

The engine writes checkpoints itself before every build (`base before t_...`) and at the gate (`review-ready cmp_...`). You can add one at any time:

```text
python -m builder checkpoint list "<wf>"
python -m builder checkpoint create "<wf>" --name "before hood rework"
python -m builder checkpoint restore "<wf>" <checkpoint_id>
```

A checkpoint lives under `checkpoints/<ck_id>/`: `model.blend` (a copy of the immutable revision, hash-verified against the revision record; a mismatch raises "checkpoint copy hash mismatch (R-48)"), copies of the revision's external asset dependencies under `assets/`, the operation `script.py` that produced the revision when one exists, and `manifest.json` with the render manifests of that revision and every file's hash.

Restore never rewrites history: it registers a new revision whose parent is the checkpoint's revision (note "restored from checkpoint ck_... (name)"), supersedes every acceptance bound to another revision (`supersede_reason: checkpoint_restore`), returns every component to `unreviewed` ("checkpoint restore invalidates review (R-39)"), journals `checkpoint.restored`, and sets the stage to `build` for the next `resume`. It refuses while ownership is held ("release or hand off before restoring") and when the checkpoint's revision file is missing or corrupted. Only workflow-owned files are touched. The GUI's Restore button confirms with the same explanation.
