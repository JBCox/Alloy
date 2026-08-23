# Communication and orchestration v2

Status: **APPROVED FOR IMPLEMENTATION** by Josh on 2026-08-21 after a live
Claude/GPT/Gemini design review. This document supersedes the product choices
in `ORCHESTRATION_DESIGN.md` where they conflict. The v1 document remains the
record of the shipped implementation and its invariants.

Implementation record: Phase 1 was completed 2026-08-21. The persisted
opening circuit, two-turn starvation ceiling, deferred early wrap, run-local
failure parking, `/next <seat>` command, and app "Speak" floor action are
implemented with scheduler, resume, app-headless, and UI boot regressions.
Phase 2 was completed the same day: legacy modes persist normalized policy
recipes, topology and budget dispatch read those policies, and cyclic,
nomination, moderation, fallback, completion proposals, and fairness pass
through one sequential floor-policy boundary. Phase 3 is also complete:
persisted rows now carry stable IDs, truthful origins and intended/actual
delivery; explicit trailing `[[TO: ...]]` addressing is live with lossless
broadcast fallback; edit activity produces confined, existence-checked
artifact descriptors; and the app exposes delivery badges plus per-seat
history lenses. The engine portions of Phases 4 and 5 are complete: Panel is
a resumable `2N + 1` phase machine, completion facts distinguish termination,
verdict, and lifecycle, CLI presets expose call estimates, addressed rows
synchronize through bounded relay-authored digests with verbatim fallback,
and reactive delivery is debounced. Phases 4 and 5 were completed on
2026-08-22: the app now launches from four goal-first presets, exposes only
validated policy combinations in an Advanced drawer, previews Panel's exact
`2N + 1` base calls, persists a stable synthesizer slot through resume, and
renders mechanical termination, semantic verdict, and lifecycle as separate
facts in both the header and chat rail. Live Room remains labelled
experimental while its new coordination metrics accumulate real-session data.
The canonical direct-process runner, `python tests/run_all.py`, is green at
34 standalone suites and 744 tests. This is the release count: unlike pytest
collection, it executes the complete custom-runner suites too.

## Why v2 exists

The shipped `mode` value currently chooses several unrelated behaviors at
once:

1. concurrency: sequential, barrier-parallel, or reactive;
2. floor selection: cyclic, speaker nomination, or model moderation;
3. workflow: open conversation or supervised workstreams;
4. budget accounting: laps, turns, waves, or a safety ceiling;
5. indirectly, completion authority and message visibility.

That coupling makes new workflows look like new turn loops, gives the UI six
engine-oriented choices, and prevents useful combinations. Ordinary
conversations also use an unconditional full-text broadcast bus, while
Supervisor workstreams use strict isolation. There is no addressed middle
ground.

The live design session exposed a concrete scheduler failure: Moderator mode
gave Claude two turns, GPT five turns, and Gemini zero turns, then accepted
`DONE`. Gemini was never invoked and was excluded from closing remarks because
only introduced seats were placed in the closing list. The moderator prompt
contained the full roster and per-seat counts, so prompting alone cannot
enforce fairness.

## Non-negotiable invariants

The existing safety invariants remain in force:

- **Commit-consume:** prompt composition snapshots a pending prefix; only a
  successful commit deletes exactly that prefix.
- **Never forge a turn:** model text is attributed only to the model that
  produced it. Relay summaries are visible relay-authored rows.
- **One owner thread per Agent:** no concurrent call may touch the same Agent
  object.
- **Truthful replay:** persisted rows and the UI expose who actually received
  each message.
- **Workspace confinement:** artifact paths are workspace-relative, verified
  to exist, and checked by the existing confinement logic before publication.
- **Resume safety:** every scheduling decision that can affect the next turn is
  persisted by slot ID, never by roster index.

V2 adds two hard floor invariants:

1. **Opening circuit.** Every available seat takes one opening turn before a
   non-deterministic floor policy may choose a seat or a completion request may
   end the run. Sequential workflows open in cursor order beginning at the
   configured start seat. Barrier and reactive workflows naturally open all
   seats. A seat that fatally fails or is explicitly removed is unavailable;
   an ordinary skipped turn does not silently remove it.
2. **Starvation ceiling.** After opening, no active seat may become more than
   two committed turns ahead of the least-heard active seat. When the ceiling
   would be crossed, the floor policy is restricted to the least-heard
   eligible seats. This is enforced by the scheduler, not by a model prompt.

Moderator `DONE` and participant `[[WRAP]]` requests made during the opening
circuit are deferred. Every remaining opener receives the wrap request; those
opening responses count as their closing remarks, so the run ends after the
last available seat responds rather than charging a duplicate closing lap.
They inherit the normal closing-turn budget exemption. A human `/stop` and
fatal safety failures always retain immediate authority.

Josh can override ordinary floor selection with `/next <seat>` or the app's
"Give floor" action. The override is persisted as a slot ID, is consumed by
the next attempt, and cannot interrupt an in-flight turn or a closing
sequence.

## Internal policy model

`mode` remains readable for legacy sessions and CLI compatibility, but new
sessions persist a normalized `orchestration` object:

```json
{
  "preset": "open_discussion",
  "concurrency": "sequential",
  "floor": "cyclic",
  "workflow": "conversation",
  "routing": "broadcast",
  "budget": {"unit": "laps", "limit": 4},
  "completion": "participants",
  "fairness": {"opening_circuit": true, "max_lead": 2}
}
```

The axes and initial values are deliberately small:

- `concurrency`: `sequential`, `barrier`, `reactive`;
- `floor`: `cyclic`, `nomination`, `moderated`, `all`, `fair`, `manager`;
- `workflow`: `conversation`, `panel`, `supervisor`;
- `routing`: `broadcast`, `addressed`, `isolated`;
- budget unit: `laps`, `turns`, `phases`, `waves`, `ceiling`;
- completion authority: `participants`, `moderator`, `synthesizer`,
  `supervisor`.

The engine validates combinations. The UI does not expose an arbitrary
Cartesian product. Policy implementations may initially remain functions in
`relay.py`; this design does not require class hierarchies merely for naming.

### Legacy mapping

| Existing mode | Concurrency | Floor | Workflow | Routing | Budget |
| --- | --- | --- | --- | --- | --- |
| `round_robin` | sequential | cyclic | conversation | broadcast | laps |
| `speaker` | sequential | nomination | conversation | broadcast | turns |
| `moderator` | sequential | moderated | conversation | broadcast | turns |
| `parallel` | barrier | all | conversation | broadcast | laps |
| `free` | reactive | fair | conversation | broadcast initially | turns |
| `supervisor` | barrier | manager | supervisor | isolated | waves/laps |

Missing `orchestration` data is derived from this table at load time. Existing
`--mode` flags keep working. New preset/advanced flags compose the normalized
object and also save the nearest legacy `mode` value during the migration
window. Resuming an old session must not silently change routing or workflow.

## Goal-first presets

The normal composer asks what the user wants to accomplish. It offers four
recipes and describes both the workflow and estimated model calls before the
run starts:

1. **Open Discussion** — sequential conversation with a cyclic floor by
   default. Nomination or moderation is available in Advanced. Suitable for
   quick questions and informal discussion.
2. **Panel Review** — all seats draft independently, all seats critique the
   collected drafts, then one designated synthesizer produces the result.
   With `N` seats the base recipe costs `2N + 1` seat calls.
3. **Build & Execute** — the existing rolling Supervisor workflow: decompose,
   execute isolated workstreams, verify artifacts, repair once, and review the
   next wave.
4. **Live Room** — reactive replies with addressed routing, debounce, and the
   starvation ceiling. It remains marked experimental until addressed routing
   and delivery visibility ship.

An Advanced drawer exposes concurrency, floor, routing, completion authority,
budget, moderator/synthesizer choice, and fairness values. Invalid
combinations are disabled with a reason. The cost preview includes seat calls
and expected side calls (moderator, supervisor, and digest) separately; it is
an estimate, never fabricated telemetry.

## Panel Review workflow

Panel is a persisted phase machine, not another conversation loop:

1. `draft`: barrier prompt composition gives every seat the same source
   backlog and asks for an independent answer.
2. `critique`: every seat receives all draft rows and critiques the arguments,
   risks, and omissions rather than merely restating its own draft.
3. `synthesis`: a configured seat (default: the start seat) receives the
   drafts and critiques and produces one final response.
4. `closing`: optional only when Josh or the synthesizer asks for it.

Persist `panel.phase`, `panel.cycle`, `panel.synthesizer`, and the source row
IDs for each phase. Resume continues the incomplete phase without replaying
successful calls. A failed draft/critique is visibly absent; it is never
invented. If the synthesizer fails twice, the run ends `fatal` or pauses for a
human choice rather than silently selecting a different author.

## Message envelopes and addressed routing

Existing message fields remain valid. V2 rows add fields additively:

```json
{
  "message_id": "stable-row-id",
  "origin": "seat",
  "audience": [0, 2],
  "delivered_to": [0, 2],
  "thread_id": "optional-stable-thread",
  "intent": "critique",
  "artifacts": [],
  "digest_of": []
}
```

- `origin`: `seat`, `human`, or `relay`; the existing `speaker` remains the
  authoritative author identity.
- `audience`: intended slot IDs, or `"*"` for broadcast.
- `delivered_to`: the slot IDs whose pending queues actually received the
  row. It is recorded in the same locked commit as fan-out.
- `thread_id`: optional stable discussion/workflow thread.
- `intent`: optional controlled value such as `answer`, `question`,
  `challenge`, `critique`, `synthesis`, `status`, or `pass`.
- `digest_of`: source message IDs for relay-authored summaries.

The first public addressing grammar is a trailing `[[TO: seat, seat]]`
directive using the existing `peel_directives` rules. Invalid, ambiguous, or
self-only targets produce a visible status note and fall back to broadcast;
they never drop a message silently. `intent` and `thread_id` are initially set
by workflows and UI controls rather than requiring models to emit several
brittle directives.

The canonical transcript records every row. The UI displays delivery badges
and offers an "All messages" view plus a "What <seat> saw" lens. A seat lens
shows delivered rows and relay digests in prompt order. This visibility ships
before selective delivery is enabled by default.

## Artifact descriptors

An envelope may reference files without copying their contents into every
prompt:

```json
{
  "artifact_id": "stable-id",
  "path": "relative/path.png",
  "kind": "image/png",
  "operation": "created",
  "producer": 2,
  "source_message_id": "stable-row-id",
  "size": 12345
}
```

Descriptors are derived from verified tool activity or an explicit artifact
declaration, then validated by the relay. Absolute paths, traversal, missing
files, and paths outside the workspace are rejected. A descriptor says that
an artifact exists; it does not claim the file is correct. Content hashes may
be added where verification or change detection needs them.

## Relay-authored digests

Selective delivery creates different seat histories, so synchronization must
be explicit and observable:

- sequential/addressed: consolidate hidden routed rows at the lap boundary;
- barrier workflows: consolidate at the phase barrier before composing the
  next phase;
- reactive mode: consolidate after a bounded hidden-message count or before a
  starved seat's next turn.

A digest is a `relay` row with `origin: relay`, `intent: status`, explicit
`audience`, `delivered_to`, `digest_of`, and its own `by_kind: digest` usage.
It is never attributed to a participant. If digest generation fails or is
empty, the safe fallback is to deliver the original hidden rows at the next
synchronization point. The relay never invents a summary.

Digest generation must obey the Windows command-line budget before invoking a
CLI. Source rows are bounded deterministically, and artifact descriptors are
preferred over verbose file descriptions.

## Completion and lifecycle states

Three concepts remain separate throughout persistence and UI:

1. **Run termination reason:** `wrap`, `moderator_done`, `supervisor_done`,
   `cap`, `ceiling`, `stop`, or `fatal`.
2. **Goal verdict:** `resolved`, `partial`, `unresolved`, or `unknown`, plus
   `verdict_source` (`seat`, `moderator`, `synthesizer`, `supervisor`, or
   `human`). A wrap request alone is not proof of resolution.
3. **Session lifecycle:** `active`, `paused`, or `closed`. A paused or closed
   session may still be technically resumable under the existing rules.

The current `outcome.json` hard fact distinguishing wrap/cap/stop/fatal is
preserved. Additive fields carry the semantic verdict and source. The rail and
composer must not label a cap as successfully completed merely because both
cap and wrap currently map to the app's generic `done` run status.

## Implementation sequence

Each phase is independently releasable and keeps the full token-free suite
green.

### Phase 1 — fairness hotfix

- Add sequential opening-circuit scheduling before nomination/moderation.
- Defer early wrap/DONE until the circuit completes.
- Persist per-slot committed floor counts and a forced-next slot.
- Enforce the two-turn starvation ceiling in nomination and moderation.
- Add `/next <seat>` and an app "Give floor" action.
- Replace the v1 tests that deliberately permit a never-heard seat with
  regression tests reproducing the 2/5/0 live failure.

### Phase 2 — normalize policies without behavior drift

- Add validation and legacy-mode mapping for `orchestration`.
- Route sequential selection through one floor-policy function returning
  `seat`, `fallback`, or `done`.
- Move budget-unit decisions out of `mode in (...)` conditionals.
- Keep parallel, free, and supervisor threading/commit behavior unchanged.

### Phase 3 — envelopes and truthful delivery

- Add stable message IDs and additive envelope fields.
- Persist actual delivery audiences atomically with fan-out.
- Add artifact validation/descriptors.
- Add transcript delivery badges and per-seat history lenses.
- Enable explicit addressing only after the UI can reveal divergent delivery.

### Phase 4 — presets, Panel, completion display

- Add the goal-first composer and Advanced drawer.
- Implement Panel as persisted phases over the existing barrier machinery.
- Display estimated calls before launch.
- Surface termination, verdict, and lifecycle separately.
- Keep legacy mode names available when reopening and in CLI help.

### Phase 5 — digests and Live Room hardening

- Add bounded relay-authored digest side calls and safe full-text fallback.
- Add reactive debounce/causal wake-up rules.
- Change Live Room routing from compatibility broadcast to addressed.
- Measure resent input tokens, repeated-content rate, participation skew,
  latency, digest spend, and turns-to-resolution before promoting Live Room.

## Acceptance criteria

At minimum, automated tests must prove:

- a moderator cannot select any seat twice before every available seat opens;
- moderator `DONE` and participant wrap cannot silence an unopened seat;
- after opening, no active seat exceeds the configured lead over the quietest;
- a forced human floor choice survives save/resume and wins the next eligible
  sequential turn;
- legacy metas resume with byte-equivalent routing and workflow behavior;
- every routed row records intended and actual audiences;
- the seat-history lens reconstructs prompt delivery order;
- a digest is relay-authored, cites source IDs, and falls back losslessly;
- artifact descriptors cannot escape the workspace or name missing files;
- Panel resumes at each phase without replaying completed calls;
- cap, wrap, stop, fatal, semantic verdict, and lifecycle render distinctly;
- all prior commit-consume, parallel locking, permission, plan, spawning,
  outcome, retro, and app-headless tests continue to pass.

No phase is complete merely because its UI exists. Engine state, persistence,
resume, CLI behavior, app behavior, and regression tests ship together.
Cross-review must execute `python tests/run_all.py`, not only read the diff or
run pytest collection; every `tests/test_*.py` file must also remain runnable
directly from the repository root.
