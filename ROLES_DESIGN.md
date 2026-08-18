# Per-seat roles — design (v1)

Drafted by Claude (round 2), redlined by Claude 2 and GPT (round 3), settled
round 4. **Status: consensus of all three seats — ready to implement.**
All file:line references verified against the working tree 2026-08-16.

## Scope

Specialization **by instruction, not capability**. A role changes what a seat
is told, never what it can do. Per-seat permissions (yolo is conversation-wide
today) and turn-taking changes are explicitly out of scope.

## Seat config

Each seat gains two fields:

- `role` — short public name ("Researcher", "Code reviewer"). Shown in the
  roster line and optionally in UI captions.
- `role_instructions` — freeform private text ("You review code only, never
  write it. Be specific: file, line, problem."). Keep short and imperative —
  a preamble instruction competes with a growing conversation, so verbosity
  erodes fastest. UI: text field per seat, preset chips that fill it.

Persisted in `meta.json` seat entries; restored by `open_session`/rehydration.

## Injection — the one hard rule

Role text goes through `preamble()` ONLY. Never via `pending[i]`/opener.

Why: `introduced[i]` resets to False on both /clear and /compact
(app.py:827, app.py:839), so the preamble is the only text re-injected when a
seat's session resets. Preamble-carried roles survive compaction for free; a
queued "you are the coder" evaporates at the first /compact with no error.

`preamble()` gains the seat's own full role text plus a one-line roster:
"Roles: Claude = researcher, GPT = coder, Claude 2 = reviewer." Public names
for everyone, full instructions only for the owning seat (keeps preambles
small; avoids seats litigating each other's instructions).

Both call sites must pass the new args: `relay.py` loop (~line 1204) and
`app.Api._rounds` (~line 677). The function itself is shared.

## Editing a role mid-conversation

Don't lock; don't hard-reset. Ride the existing /compact path:

1. Stage the requested role change; don't expose it as committed yet.
2. Run the compact flow for that seat at a turn boundary (`compact_agent` → summary,
   `introduced[i] = False`, summary inserted into `pending[i]` — app.py:821-831).
3. If compaction succeeds, commit the new role in state/config. If it fails,
   retain the old role so the UI and live model cannot disagree.
4. If the public role name changed, queue a concise roster-update notice to
   every other seat. They otherwise retain the roster from their original
   preambles. This use of `pending` is safe because it is only the immediate
   notification; persisted config remains authoritative and later preambles
   reconstruct the updated roster.
5. Next turn = fresh preamble (new role) + the seat's own summary of the
   conversation so far. Role change without amnesia.

Tweak the inserted note when the trigger was a role edit: "(Josh changed your
role from X to Y. Your summary of the conversation so far, written in your
previous role:)" — so the model doesn't fight its own summary's old self-image.

## Deferred (own feature, own tests)

- **[[PASS]] token** — a deliberate non-message so idle roles don't fill the
  transcript with "nothing for me this round." Semantics SETTLED (round 4,
  all three seats): fire if the reply ENDS with the token — the WRAP history
  showed stricter forms silently never fire because seats close a sentence
  first. The full reply is still recorded in transcript.md/messages.jsonl
  marked `pass: true` (nothing silently destroyed; Josh can audit); it just
  isn't queued to other seats. A CLI empty reply still raises — PASS is an
  affirmative act, distinct from failure, which strengthens never-forge-a-turn.
  WRAP and PASS must share ONE helper (generalize `wrap_called`,
  relay.py:888, → `ends_with_token(reply, token)`): two tokens with two
  grammars means the preamble teaches both, and the one explained less
  carefully silently never fires. No token savings (the call still happens);
  what it buys is transcript signal-to-noise. Consecutive all-pass rounds
  worth surfacing in the UI; auto-ending on them is scope creep. Still ships
  AFTER roles, with its own tests (failure, empty output, consecutive passes).
- **Per-seat capabilities** (reviewer read-only, researcher web) — structural:
  per-provider sandbox flags + per-seat yolo. Different feature, different day.

## Open questions — answered (Claude 2, round 3)

1. **CLI syntax.** Separate repeatable flags; don't extend the seat token. The
   schema has two independent values, so the CLI needs both:
   `--role "<seat>=<public name>"` and
   `--role-instructions "<seat>=<private text>"`. Using `--role` for the
   instructions alone would leave the public roster/caption name unspecified.
   Resolve `<seat>` for both flags through the EXISTING
   `match_seats(agents, arg)` (relay.py:473), the same resolver `/clear` and
   `/compact` use: it already accepts a label ("claude 2") or a provider name
   ("claude" → all its seats), so one grammar covers the CLI and the slash
   commands and they can't drift. Two hard requirements: (a) `--role` is applied
   AFTER `assign_labels` (relay.py:504), since auto labels don't exist before
   that; (b) a `<seat>` that matches nothing is a **hard error**, not a no-op —
   a typo'd `--role "clade=..."` that silently starts an unroled conversation is
   the same failure shape as the queued-role-evaporates bug, and both are
   invisible until round 8. Ship CLI and UI together; the CLI is our only cheap
   end-to-end test path (`--turns 1 --claude-model haiku`), so UI-only v1 means
   the feature has no test.
2. **Roster wording.** Names alone; no third field. The gap is real ("Graphic"
   is uninformative) but the fix is naming the presets properly — ship chips as
   "Code reviewer", "Graphic designer", "Researcher (finds and cites)", not
   one-word stubs. Adding a public-blurb field to fix bad preset names is
   schema paying for copywriting.
3. **Role in captions.** Yes — with one constraint: stamp the role name into
   the message row in `SessionStore.record` (relay.py:596), and have the caption
   read the row, not live seat config. That row IS the UI payload and the
   docstring already says one call site on purpose so replay can't drift from
   what Josh watched. If captions read current config instead, a role edit in
   round 6 silently relabels rounds 1-5 in the replay — the transcript would
   claim the reviewer wrote messages the coder wrote.

## Settled round 4

4. **Role edits are an explicit "Apply role change" action, never an
   autosaving textbox.** All three seats agree. Compaction-as-role-edit costs
   one full CLI turn per edit (fine for a deliberate switch, wrong for
   keystroke autosave) — and an autosaved edit wouldn't just burn a turn, it
   would also broadcast a roster-update notice to every other seat per edit
   (step 4 above), so three casual tweaks = three compacts plus three
   broadcasts. The button batches intent; its label/tooltip states the cost.
   Apply at a turn boundary; commit only after compaction succeeds.
