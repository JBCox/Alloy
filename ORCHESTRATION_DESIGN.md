# Orchestration — turn-order modes, until-done, and agent spawning (v1)

Planned with Josh 2026-08-16 (mode/scope decisions his), designed via three
parallel plan agents, reconciled and implemented by Claude the same day.
**Status: SHIPPED — all nine phases landed and verified** (111 token-free tests
across this doc's ten suites as of 2026-08-16, plus one cheap real run per
feature). This is the sequel
ROLES_DESIGN.md:11 explicitly deferred ("turn-taking changes are out of
scope"). All file references verified against the working tree 2026-08-16.

## Scope

Five conversation modes, one end condition, three spawning tiers — every one
of them a CONVERSATION-level setting (CLI flag + cfg key + meta field + a
control in the app's Conversation group), never per-seat capability:

- `--mode round-robin` (default) · `speaker` · `moderator` · `parallel` · `free`
- `--until-done --ceiling N` — orthogonal to mode
- tier 1 `--no-native-subagents` (on by default) · tier 2 `--spawn-helpers N`
  (off by default) · tier 3 `--spawn-teams N` (off by default)

Out of scope, deliberately: per-seat permissions, live UI for child-team
feeds (children replay from the rail), recursion beyond depth 1. (A moderator
picker in the UI was originally deferred here; it shipped 2026-08-16 —
`#modCtl` in ui/index.html feeds cfg `moderator: {provider, model, effort}`,
default claude:claude-haiku-4-5:low.)

## The one hard rule: commit-consume

`compose_prompt` SNAPSHOTS a seat's backlog without clearing it;
`commit_reply` deletes exactly the consumed prefix, fans out, logs, counts,
and saves — one implementation of the queue invariant for every mode
(relay.py). A failed turn "restores" the queue by construction, because
nothing was removed. Consequences that must not be broken:

- `store.save(state)` is valid at EVERY instant: each seat's pending is
  exactly what it is still owed. A crash loses at most in-flight turns.
- Entries appended mid-turn (interjections, other seats' parallel commits)
  land after the consumed prefix and survive untouched.
- The failure paths never touch pending. If you find yourself writing
  `pending[i] = queued + pending[i]` again, you are re-introducing the bug
  class this replaced.

## The scheduler (sequential modes)

`run_rounds` is a per-turn scheduler, not a nested round loop.
`choose_next_seat` peeks with this authority order: **closing list** (a wrap
in progress — persisted, survives pause/resume) → **[[NEXT:]] pick**
(speaker mode) → **moderator** → **round-robin cursor**. Consumption happens
AFTER the lap/cap check so a cap-stop can't eat a seat's closing turn.

- Round-robin: list position 0 is the lap boundary (cap check + `rnd += 1`)
  — byte-equivalent behavior to the old nested loop.
- Dynamic modes (speaker/moderator/free): budget = `turns × seats` total
  turns; `rnd` is the lap counter `1 + turn // seats` so captions,
  `/turns`, and continue-math stay uniform.
- `meta.json` v2 persists `mode/turn/cursor/next_speaker/closing/moderator/
  until_done/turn_ceiling/spawn/parent/children` — all seat references by
  SLOT ID, never index. v1 metas remain fully continuable
  (`META_VERSIONS_OK`); the version is always WRITTEN as 2 so older code
  refuses newer sessions instead of mis-resuming them.

Decided against, with reasons:
- **Mechanical anti-starvation in speaker mode** — an override would fight
  the mode's purpose; the preamble asks seats to share the floor and the
  missing-directive fallback rotates naturally. Josh can interject.
- **Moderator as a seat** — it would eat roster/fan-out semantics and a
  dead-session id could kill the run. It is a STATELESS side call (session
  id wiped after every pick), picks are status-only (not transcript rows),
  any failure falls back to listed order, three consecutive failures disable
  it for the run. Only DONE is persisted (it explains a discontinuity).
- **Repurposing `rnd` as a turn counter in free mode** (the original plan) —
  dropped for uniformity: laps keep every knob and caption consistent
  across modes at the cost of "round" being approximate in free mode.

## Directives: one trailing-token grammar

`peel_directives` (relay.py) owns `[[WRAP]]`, `[[NEXT: seat]]`,
`[[SPAWN: …]]`, `[[TEAM: …]]`, `[[ASK: …]]` (+ reserved `PASS`). `wrap_called` is a
one-liner over it, so the grammars cannot drift. Rules inherited from the
wrap token's two documented bugs: a directive fires only when it TERMINATES
the reply (sentence-close form fires; mid-reply mentions have text after
them; quoted/backticked forms end on the closing mark). Directives may stack
in any order at the end. **Each peel is anchored at the LAST `[[`
(`rfind`)** — a leftmost `search` with a lazy dot collapses a stacked tail
into one directive with a garbage argument (found by test, kept as a test).
Unknown `[[NAMES]]` are surfaced to the seat, never ignored. Replies are
relayed and recorded VERBATIM — directives are never stripped (the [[WRAP]]
precedent: the relay's ethos is verbatim relaying; the loop acts on the
parse separately).

## Parallel modes and the threading contract

- Lock order: `state["lock"]` → `store._lock`, never the reverse. The
  print lock (CLI) nests innermost.
- ONE thread per Agent object, ever (session_id capture and codex `-o`
  files are single-owner). Barrier mode: one daemon thread per seat per
  round. Free mode: one long-lived thread per seat; /clear and /compact run
  on the OWNING thread via its inbox (compact is a CLI turn).
- The app calls `evaluate_js` from exactly ONE emitter thread (`Api._emit_q`
  drains on a daemon); `Api.emit` just enqueues — thread-safe, FIFO across
  all producers. Tests flush with `_emit_q.join()`.
- Barrier rounds: compose ALL prompts, then run; commits happen per-arrival
  (record/emit/fan-out/save in ARRIVAL order — live UI == transcript ==
  replay). A reply landing mid-round is invisible to already-composed
  prompts: seats see it next round, which is exactly what the mode means.
- Wrap in parallel: all wrapped → stop; otherwise every non-wrapper gets one
  more simultaneous round, persisted via `closing` and consumed per-commit
  (a crash mid-closing-round resumes with only the seats still owed).
- Free mode: `FREE_MAX_LEAD = 2` (a seat may not start a turn ≥2 ahead of
  the slowest live seat); a seat parks after 3 consecutive double-failures;
  <2 live seats pauses the run; role staging does NOT drain mid-run (it
  applies when the run pauses).
- `_atomic_write` retries `os.replace` briefly: on Windows a concurrent
  READER without FILE_SHARE_DELETE (editor, indexer, a test polling
  meta.json) blocks the rename with PermissionError. Found the hard way by
  a polling test; it was always a latent hazard for saves.

## Asking Josh — [[ASK: question | option | …]]

A seat may END a reply with `[[ASK: question | option A | option B]]`
(options optional, ≤6; the pipe grammar means the question cannot contain
`|`). `handle_ask_directive` runs after `commit_reply` in every loop and
BLOCKS on the new `LoopIO.ask_human(payload, abort=None) -> str|None` seam:

- **Headless default returns `None` immediately** — tests and child-team
  runs never hang; the requester gets a "(Relay: Josh was unavailable…)"
  note instead. Never forge: a missing answer is a note, an answer is a real
  Josh row (`meta="answer to <name>"`) fanned out to EVERY seat like an
  interjection.
- **The wait happens OUTSIDE `state["lock"]`** (it can be minutes; the
  handler takes the lock only around mutations). Sequential: the loop simply
  pauses. Parallel: the round barrier waits; the coordinator's drain keeps
  /stop live. Free: the asking seat blocks with `busy[i]` held (cap-stop
  can't fire mid-question) while the others keep talking until FREE_MAX_LEAD
  throttles; `abort=flow-stop` unblocks it on a fatal elsewhere.
- **Gate `state["ask"]`**: True from the app and the CLI (`--no-ask` to
  disable), False in child teams / bare states, persisted additively in meta
  (`ask`, `ask_pending`) — the preamble's "Asking Josh" block and the
  softened header sentence toggle with it, so seats are never promised a
  channel the front end doesn't provide.
- **Crash safety**: `ask_pending` is saved BEFORE the wait; `announce_lost_ask`
  (run start, next to `announce_lost_helpers`) turns a leftover marker into
  one system note + a requester note — a lost question is never re-popped
  (the conversation state it came from is gone).
- **App front end**: `_AppIO.ask_human` emits a `question` event, blocks on a
  per-qid queue polling `_stop_flag`; `Api.answer_question(qid, text)` is a
  pure bridge-thread enqueue (empty text = explicit skip); `question_done`
  always follows. The UI modal can be hidden ("answer later") — the wait is
  engine-side and a composer pill reopens it. CLI: `CLIIO.ask_human` prompts
  on the console (number picks an option; any text answers; a `/command` is
  re-queued to the loop and resolves the question unanswered); its
  `_asking` flag stops the concurrent coordinator drain from stealing the
  typed answer in parallel/free.

## Until-done

An end condition, orthogonal to mode: no round cap; ends via [[WRAP]],
moderator DONE, /stop, or the hard `turn_ceiling` (default 60 total turns,
`/ceiling N` mid-run, "Until done" checkbox swaps the Rounds stepper for a
ceiling stepper). Closing turns are exempt (bounded by seat count). Continue
extends the ceiling (`turn + ceiling`) instead of the round cap. The
preamble replaces the cap line: "…Do not pad: wrap as soon as the goal is
met."

## Spawning — three tiers, all instruction-plus-budget, never trust

- **Tier 1 (native, on by default):** `Task` added to claude's non-yolo
  allowlist (a Task subagent inherits the seat's permission mode/cwd — no
  new effective capability, only intra-turn parallelism; verified live).
  codex gets the note only when `codex features list` shows `multi_agent`
  enabled (cached token-free probe, warmed in precompute_config — NEVER on
  the bridge thread). agy has no spawn capability → Gemini gets no note.
  `Agent.native_spawn_note()` lives NEXT TO build_cmd so the note and the
  capability cannot drift — capability-honest is the feature.
- **Tier 2 (helpers, off by default):** `[[SPAWN: provider[:model[:effort]]
  | task]]` → a fresh ONE-SHOT Agent in the shared workspace (the only dir
  the workspace contract allows), on a daemon thread while the conversation
  continues. Results are delivered ONLY to the requester, at loop
  boundaries, by `SpawnManager.drain_into_pending` (single-threaded or
  under the conversation lock — helper threads never touch pending).
  Failures are noted verbatim, never retried, never forged. Josh sees
  everything: request = system row, result = a provider-colored row
  captioned "helper for X". In-flight helpers at a crash are LOST by
  design — the next run tells the requester; nothing silently re-spends.
- **Tier 3 (teams, off by default):** `[[TEAM: <agents-spec> | rounds=N
  mode=<m> | task]]` → a whole child conversation as a NORMAL session
  (own folder, driven by the same `run_rounds`, silent LoopIO — replay it
  from the rail, "↳" marks it). It inherits the parent's workspace, runs
  until-done with rounds ≤ CHILD_ROUNDS (6), then ONE closing-report call
  to its first seat; the report goes to the requester with the child id.
  Depth is HARD 1: children get `max_helpers: 0, max_teams: 0`, so their
  preambles never mention SPAWN/TEAM and a directive anyway gets the
  standard refusal. Parent meta lists `children` (hints — a child may be
  deleted); child meta carries `parent`.
- One SPAWN **or** TEAM per reply; both → neither runs. Every refusal is a
  note in the requester's queue.

## Landed — verification record (2026-08-16)

Token-free: 111 tests (as of 2026-08-16) across `tests/test_loop.py`, `test_scheduler.py`,
`test_modes.py`, `test_until_done.py`, `test_parallel.py`, `test_free.py`,
`test_spawn_tier1.py`, `test_spawn_helpers.py`, `test_spawn_teams.py`,
`test_app_headless.py` — the Phase-1 loop extraction is what made the loop
drivable by FakeAgents (`run_rounds(state, LoopIO())`) without spending a
turn. Real cheap runs, one per feature, all in `sessions/`: 3-provider
round-robin + duplicate seats (post-refactor equivalence), speaker mode
(all seats played real [[NEXT:]] tokens), moderator mode (double-turns
observed; a failed turn skipped without burning budget), until-done (wrapped
at turn 3/6), parallel (concurrent thinking, wrap → closing round referencing
everyone's replies), free (simultaneous thinking, budget cap), tier-1 (a
haiku seat ran a real Task subagent under the non-yolo allowlist), tier-2
(Claude spawned a Gemini helper mid-conversation; PONG returned to Claude
only), tier-3 (Claude spawned a claude+gemini team that ran 2 rounds and
reported back while the parent kept talking). Known model-quality caveat:
cheap seats sometimes NARRATE file writes without invoking tools — a seat
behavior, not a relay one (the relay never forges anything on their behalf).
