# Comms design — three-gate seat-to-seat communication for Alloy

2026-08-25 · Task t4. Analysis only — no code touched. Inputs: `docs/traycer-research.md`
§3 (Traycer's agent-to-agent model) × `docs/alloy-feature-audit.md` §4–§5 (Alloy's
communication inventory, written from the code). Companion to `docs/traycer-alloy-gap.md`
row 9, which this document expands into an implementable design.

## 0. The honest starting point

Traycer splits agent-to-agent contact into three separately-gated capabilities because its
agents live across machines, users, and runtimes with different inbox support: **Reference**
(always), **Transcript-read** (same user; interface- and Host-dependent), **Message-delivery**
(same user + same Host + A2A-capable runtime on both ends). Failures are REJECTED visibly,
never queued into a void.

Alloy does not have Traycer's problem — every seat is a local CLI subprocess owned by one
process — so copying the matrix wholesale would add gates that can never fire. But Traycer's
underlying discipline is exactly right and Alloy only half-has it today: *naming someone,
reading their history, and putting words into their queue are different powers with
different failure modes.* Alloy currently has one implicit channel (@-mention queues verbatim
text), one implicit read (spawn results return only to requesters), and no way for a seat to
read a peer's transcript at all. The design below adapts the three gates to what Alloy
actually has: per-seat pending queues, the commit-consume invariant, and the never-forge rule.

## 1. The three gates in Alloy terms

### Gate 1 — Reference (status: EXISTS, needs naming)

Naming a seat as context without claiming any effect: prose mentions, `[[NEXT: seat]]`
nomination, spawn/teammate references in a brief, the preamble roster.

- **Requirement**: the seat exists in the roster. Nothing else. Works under every routing
  mode and while the target is paused/parked/dead.
- **Failure mode**: none — a reference carries no delivery promise, so it cannot fail.
  This is precisely why Traycer made it the broadest capability, and why Alloy should say so
  out loud instead of letting seats believe a name-drop did something.
- **Change**: documentation only — one sentence in the preamble's communication block
  distinguishing "mentioning a peer" from "sending to a peer."

### Gate 2 — Transcript-read (status: MISSING — the real gap)

A seat may READ another seat's prior messages from the durable log. Today this power exists
nowhere outside the engine itself: replay reads messages.jsonl, but no seat ever sees a
peer's words except through fan-out at the time they were spoken. A seat added later, a
cleared (`/clear`) seat, or a spawned helper inherits NOTHING of the conversation it just
joined.

**Proposed mechanic — `[[READ: <seat> [N]]]` directive:**

- Ends-of-reply grammar, parsed through the existing `peel_directives` machinery, opted in
  explicitly (added to the directive set alongside ASK/TASK/OBJECTIVE, NOT to
  `KNOWN_DIRECTIVES` — same rule as every orchestration verb: an ordinary seat playing READ
  stays visibly unknown rather than gaining authority). End-anchored, last-`[[` anchor —
  the stacked-tail bug class is already solved there.
- The LOOP performs the read, not the seat: at the drain site after `commit_reply`, the
  engine extracts the last N (default small, cap hard) rows for the named seat from
  messages.jsonl — readers/listeners for that file already exist — wraps them in a system
  row carrying provenance ("transcript of GPT, rows X–Y, read at Claude's request"), and
  enqueues it ONLY to the requester's pending queue.
- This respects commit-consume by construction: the read result is a normal queued item;
  the requester consumes it with its next prompt snapshot like any other message. Nothing
  is injected into a live turn.
- **Gates**: (a) target seat has rows in THIS session's log (cross-session reads are out of
  scope — that is fork/provenance territory); (b) target not currently RADIO-SILENT under an
  active workstream task — reading a worker's draft would breach the isolation invariant
  (audit §6); the gate returns the worker's last SETTLED summary instead, which crosses
  streams legitimately; (c) N clamped to a cap consistent with the error_excerpt /
  WORKSTREAM_REPORT_MAX philosophy — a transcript read must never become a context-length
  bomb given the Windows ~32k argv ceiling that BRIEF_MAX already guards.
- **Failures surface visibly**: unknown/ambiguous seat label → relay note in the requester's
  queue ("no seat labeled X; roster is …"), matching the match_seats resolver behavior used
  by `/clear`; zero rows → note saying so, never an empty forgery; silent-worker read → the
  settled-summary substitution stated in the note itself.

### Gate 3 — Message-delivery (status: EXISTS, needs explicit gating + receipts)

Putting text into another seat's queue for consumption at its next turn. Alloy already has
four delivery paths — broadcast fan-out in `commit_reply`, addressed @-mention via
`parse_mention`, spawn results through `SpawnManager.drain_into_pending`, and Josh's own
rows via `enqueue_josh_message`. What it lacks is Traycer's discipline of CHECKING
deliverability before accepting a send and SAYING what happened either way.

**Proposed mechanic — a single `deliver()` chokepoint:**

- Every seat→seat enqueue currently happens inline at fan-out sites. Route them through one
  function (natural home: beside `enqueue_josh_message`, which all four drain sites already
  funnel through per audit §4) that answers deliverability FIRST:
  1. **Target enabled and unparked** — a seat failed-twice/fatal-parked (audit §15) cannot
     receive; the sender gets a visible note ("GPT is benched; your message was NOT
     delivered"), never a silent drop into a queue nobody will drain.
  2. **Runtime supports it** — in Traycer this is the per-runtime inbox matrix; in Alloy it
     collapses honestly to ONE question: does the target hold a live provider session id?
     A seat whose CLI died fatally has nowhere for the words to go when its turn comes, so
     delivery refuses rather than queueing text that would be consumed by a fresh amnesiac
     session — that would forge continuity, the exact failure the dead-session-id gotcha
     forbids.
  3. **Mode allows it** — panel drafts/critiques are isolated by design
     (`commit_reply(fan_out=False)`); free mode throttles lead. A delivery refused by mode
     policy is a NOTE ("panel mode isolates drafts until synthesis"), not an error.
- On success, the existing audience/delivered_to envelope (already carried by mentions)
  becomes the universal receipt; on refusal, the note goes to the SENDER's queue — the
  mirror of the spawn-refusal rule (never silent, never forged, never auto-retried).

## 2. How routing changes (and doesn't)

The six-axis recipe's routing axis — broadcast / addressed / isolated — stays the single
source of truth for WHO receives a committed reply; the gates layer ON TOP of it:

| Routing | Reference | Transcript-read | Delivery |
|---|---|---|---|
| broadcast | always | unaffected (reads are pull, not push) | fan-out IS delivery; `deliver()` still checks park/session gates per recipient and reports skips |
| addressed (@-mention, `[[NEXT]]`-adjacent) | always | unaffected | the main beneficiary — every mention becomes a checked, receipted delivery |
| isolated (panel drafts, workstream workers) | always (names appear in briefs) | workers may read SETTLED artifacts only | inbound blocked during active task (radio silence holds); settlement summary remains the sole outbound |

Key principle preserved: reads are PULL and ride the requester's own queue; deliveries are
PUSH and touch the target's queue. Commit-consume is untouched by both — nothing clears a
backlog it didn't consume, nothing injects into a mid-turn prompt.

## 3. Exact integration points in relay.py

- **`peel_directives` / directive grammar**: add `[[READ: …]]` to the opted-in set; keep it
  OUT of `KNOWN_DIRECTIVES`. `wrap_called` derives from the shared parser automatically —
  the grammars cannot drift.
- **`compose_prompt`**: no structural change — read results and delivery refusals are ordinary
  queue items it already snapshots. Optional one-line addition: when a queued item carries a
  transcript-read envelope, prefix it with its provenance header (same pattern as the
  "Josh (human) opens…" framing).
- **`commit_reply`**: after consuming and before fan-out, route each recipient append through
  `deliver()`. The existing `fan_out=False` branch becomes a mode-policy answer inside
  `deliver()` rather than a bypass around checking — isolated replies still produce notes if
  a seat ATTEMPTED an end-run delivery, which today disappears silently.
- **`enqueue_josh_message` / `parse_mention`**: `deliver()` lives beside them; mention
  resolution (longest-match ≤3 words, ambiguity → literal broadcast) stays, but the resolved
  target now passes the park/session gates BEFORE the queue append, with refusal notes
  replacing quiet drops.
- **Drain sites** (all four): unchanged call shape — they already funnel through
  `enqueue_josh_message`, so the chokepoint covers sequential, parallel-barrier, free, and
  panel dispatch at once.
- **Workstream boundary**: the settled-summary substitution for reads of active workers
  hooks where `settle_workstream` already caps report excerpts — reuse
  `WORKSTREAM_REPORT_MAX` rather than a second constant.
- **meta.json persistence**: nothing new to persist — envelopes and notes are rows; gates
  derive from state (parked set, session ids, mode) at delivery time, so a resumed chat
  cannot inherit stale permissions. Same anti-forgery instinct as continuation validation.

## 4. UI affordances (ui/index.html, minimal)

- Render the existing delivered_to/audience envelope as a small chip on addressed rows
  (the data already rides the row; today only spawn captions surface lineage).
- Relay notes from delivery refusals already render as system rows — no work beyond making
  sure the note wording names the gate that fired (parked / dead-session / mode-policy).
- No new panels, modals, or settings in phase 1–2; the gates are engine honesty, not controls.

## 5. Phased rollout order

1. **Phase 0 — say what exists (doc + preamble only).** Add the three-gate vocabulary to
   the preamble's communication block: references always work; sends are checked; reads are
   coming. Zero risk, immediate seat-behavior clarity.
2. **Phase 1 — `deliver()` chokepoint.** Refactor the fan-out/mention enqueues through one
   checked function with visible refusal notes. Pure honesty upgrade, no new user-facing
   verbs; covered by extending the FakeAgent loop suites (token-free) plus the four drain
   sites' existing tests.
3. **Phase 2 — `[[READ]]` transcript gate.** Directive grammar + loop-performed capped read
   + settled-summary substitution for silent workers. Tests pin: cap enforcement, radio-silence
   substitution, unknown-label refusal, commit-consume integrity (read results consumed
   normally).
4. **Phase 3 — receipts in paint.** Envelope chips on addressed rows; refusal-note wording
   polish. UI-only, rides test_ui_boot's stub DOM harness.
5. **Phase 4 (optional, only if wanted)** — per-conversation delivery POLICY as a seventh
   recipe axis (e.g. broadcast-only rooms, DM-free rooms), validated through the same
   normalization path as the existing six axes. Defer unless a real conversation shape asks
   for it; a policy nobody requested is a control that lies about needing to exist.

Each phase lands independently; nothing downstream depends on an upstream phase being more
than its honest minimum.
