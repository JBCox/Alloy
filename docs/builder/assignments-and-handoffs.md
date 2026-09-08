# Assignments, ownership, and handoffs

Purpose: how tasks are assigned to seats, how editing ownership is enforced at the execution boundary, what a handoff records, and what happens when an interrupted build is retried.

## Roles and the scheduler (R-8, R-12, R-107)

Assignment is code, not agents: `builder/roles.py::assign` returns a seat and a one-line rationale that is recorded on every task (`task.rationale`, shown by `status`, the Run tab, and the journal). Roles: `observer`, `planner`, `builder`, `corrector`, `reviewer`, `verifier`, `reassessor`, `art_director`, `image_generator`. Rules, in order:

1. A user override wins, except that an override can never make a seat review, verify, or reassess its own operation: that raises `RoleConflict` and the run stops with stop reason `execution_failure` and the R-107 reason in its note. See "User overrides" below.
2. `observer`: every LLM seat independently; nothing to assign. Intake observations are collected from both seats before either sees the other's (R-11).
3. `planner` (the brief and the construction plan): seat A ("first agent by configuration order; both agents' independent observations are included").
4. `builder`: alternates by build count, starting with A ("alternating build ownership (build task #n)"). Contribution counts are reported, never balanced (R-9).
5. `corrector`: the seat that proposed the finding implements it ("the seat that proposed the correction implements it (R-7, R-56)"), or the seat a reassessment reassigned it to (R-88).
6. `reviewer`, `verifier`, `reassessor`: the seat that did not make the operation ("reviewer is the non-owner; independent evidence pass"; "the non-corrector verifies on renders"; "fresh analysis by the agent that did not make the failed corrections"). Never the author.
7. `art_director`: seat A, rotating after two rejected generation rounds (R-108).
8. `image_generator`: only the image seat; an LLM seat never holds it (D10).

`status` prints `contributions (committed operations): {'A': n, 'B': m}`.

### User overrides (R-8, R-107)

Roles can be pre-assigned to a seat for one run:

```text
python -m builder start "<wf>" --assign build=B --assign plan=A
```

or for every run through the settings file's `assignments` key:

```yaml
builder:
  assignments: {build: B}
```

`--assign` (repeatable, `role=SEAT`) wins over the config key. Roles: `brief`, `plan`, `build`, `corrector`, `reviewer`, `verifier`, `reassessor`; seats: the configured `A` and `B`. An unknown role or seat on `--assign` is an error before any run record is created; in the config key it is dropped with a warning. The engine stores the merged result on the run record as `assignment_overrides` with `assignment_override_sources` (`cli` or `config`) and journals it with `run.created`. Every task the override decides carries the rationale `user override (<source>: <role>=<seat>) (R-8); R-107 still applies: <seat> never reviews, verifies, or reassesses its own operation`, and `status` prints one line per override:

```text
assignment override build=B (cli): user override (cli: build=B) (R-8); R-107 still applies: B never reviews, ...
```

The window shows the same lines in the Run tab's "Stage, task, ownership" card; the Controls card has an "assignments for this run" entry (`build=B plan=A`) that Start passes through. `builder.assignments` from `config.yaml` pre-fills that entry.

The independence rule is not overridable: with `--assign build=B --assign reviewer=B` the build goes to B and the review stage stops the run (`execution_failure`, note "seat B made the operation under reviewer and may not hold that role for it (R-107); assignment override reviewer=B cannot be honoured for this task"). Overrides never touch the concept stage's art director (that rotation is R-108).

## Ownership tokens and base revisions (R-41, R-45)

`builder/ownership.py::OwnershipManager` keeps one holder per resource (the resource is `assembly`). A grant is a record with a random token (`secrets.token_hex(16)`) bound to a base revision. Every operation carries its token and the base revision it expects, and `Operations.execute` checks, before any Blender process starts:

1. the token is known, still `held`, for the right resource, and bound to the same base revision as the operation (`OwnershipError` otherwise: "stale holders cannot act after handoff", "base revision mismatch");
2. the base revision record exists;
3. the revision file's hash still matches the store's `files` registry. A mismatch journals `revision.external_modification` and rejects the operation: "external modification detected; reconcile it explicitly before continuing". Nothing is overwritten.

A rejected operation never stages anything. After a commit the holder's token advances to the new revision, so the next operation from the same holder expects the revision that was just promoted.

Ownership is visible in `status` (through the Run tab: `ownership: A holds assembly at rev_... since ...` or `free (no holder)`), and in the journal (`ownership.acquired`, `ownership.advanced`, `ownership.released`, `handoff.created`).

## The build, review, correct cycle and who holds the assembly

- Build (`_stage_build`): the builder seat acquires ownership at the current revision; the engine checkpoints the base revision first ("R-56: checkpoint the base revision before building"), renders the current views, measures, and sends the build packet. Each returned operation is staged, validated, promoted, and journaled in order; the first failure blocks the task.
- Review (`_stage_review`): the reviewer inspects renders it did not make; no ownership is needed.
- Correct (`_stage_correct`): the corrector is the proposer. If another seat holds the assembly, the engine performs a handoff to the corrector; if nobody holds it, the corrector acquires it; if the corrector already holds it, it continues.
- Gate (`_stage_gate`): when the component becomes `ready_for_user_review`, the holder releases ownership ("component ready for user review (R-44)") and a checkpoint is written.

## Handoff (R-44)

`OwnershipManager.handoff` does, in one step: release the old token (it can never be reused), write a `handoff` record (resource, from and to holder, the revision handed over, the parts the previous holder changed, up to five assumptions from the plan's hypotheses, the open finding ids, pending issues, the reason), and grant the new holder a fresh token bound to the same verified revision. The reason recorded in the engine is "agent B proposed the correction for f_... and can implement it (R-56)", and the correction task's rationale reads "finder implements its own proposed correction after handoff from A (R-7, R-56)". The Run tab shows the number of handoffs.

Pending writes are never carried across a handoff: a correction only starts after the previous operation committed or was reconciled, and an interrupted holder's grant is released by `_reset_interrupted` (below).

## What a retried interrupted build does (R-23, R-47, R-57)

An interrupted build is a `build` task left `in_progress` by a crash, a failed or uncertain operation, a malformed reply, or a restart. On the next pass through `_stage_build` (and during recovery on restart), `Engine._reset_interrupted`:

1. marks each `in_progress` task `blocked` with `interrupted: true` and a note ("interrupted (build retried) while in progress; operations [...]; nothing from this task is assumed to have applied");
2. releases the assembly grant of the holder whose latest task is `blocked` and has no operation in flight, so the retry acquires a fresh token bound to the unchanged base revision (R-44);
3. returns findings stuck in `correcting` to `open` with a `recovery_notes` entry and marks their correction attempt `uncertain`.

Then the retry is assigned. `Engine._assign` excludes blocked-and-interrupted build tasks from the alternation count, so the same seat that was interrupted builds again instead of the turn passing to the other seat. The retry packet carries a section headed "Interrupted operations against the current base revision (never applied)" listing every operation that ended `uncertain`, `cancelled`, or `failed` with `recovery: interrupted` against that base: its intent, the error, where its staged output was quarantined, and the instruction "Do not assume any of its edits exist; start from the evidence in this packet." No mutation is replayed; the base revision is the one before the interruption.

## Isolated reviews (R-11)

Each agent record tracks `context_contains` tags. A builder's persistent session is tagged with `self_assessment:<component>:<task>` after a build or correction. When the reviewer's persistent session already holds the builder's self-assessment for that component (or `builder.isolated_reviews` is `always`), the review runs in a fresh `isolated_review` session with a reconstructed packet; the task records `session_kind: isolated` and the packet's `withheld` list names the builder self-assessment. See [token-efficiency-and-usage.md](token-efficiency-and-usage.md) for the cost side.
