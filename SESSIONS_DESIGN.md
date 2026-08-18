# Chat history sidebar — working design (draft by Claude, edit freely)

Goal (Josh): a left-hand list of past chats; click one and you're back in it —
transcript visible AND the agents remembering, like ChatGPT/Claude.

## The actual hard part

The sidebar is easy. Resuming is the feature. Today the *only* thing that makes
a conversation continuable is `Api._conv`, an in-memory dict holding live
`Agent` objects. Kill the app and every `agent.session_id` is gone — and that
string is the agents' memory. All three CLIs resume by id and the ids survive
on disk:

| provider | resume mechanism | attr |
|---|---|---|
| claude | `--resume <id>` (returns a NEW id each call — re-capture) | `Agent.session_id` |
| codex  | `codex exec resume <id>` | same |
| gemini | `--conversation <id>` | same |

So persistence = write `session_id` (plus everything needed to rebuild the
`Agent`) to disk after every turn, and rehydrate on open.

## Proposed on-disk shape

`sessions/<stamp>-<slug>/meta.json`, rewritten after each turn (atomic: tmp +
os.replace):

```json
{
  "v": 1,
  "title": "Last session we had you fix…",
  "created": "2026-08-16T15:06:30",
  "updated": "2026-08-16T15:41:02",
  "ended": false,
  "workspace": "C:\\ai-chat",
  "topic": "", "turns": 6, "yolo": false,
  "rnd": 4, "max": 6,
  "seats": [
    {"id": 0, "provider": "claude", "label": "Claude",
     "model": "claude-opus-4-8", "effort": "high",
     "session_id": "abc-123", "introduced": true,
     "pending": ["GPT said: …"]}
  ]
}
```

Everything in `Api._conv` that isn't a live object goes here: `pending`,
`introduced`, `rnd`, `max`, `providers`, `slot_ids`. Rebuilding is then
mechanical: construct `AGENT_TYPES[provider](workspace, yolo, model, effort,
name=label)`, assign `.session_id`, drop into a `_conv` state dict, resume.

`transcript.md` stays exactly as-is (human-readable, already written). For
replaying into the UI feed we want structured turns too — either a
`messages.jsonl` (one `{speaker,name,provider,text,round}` per line, appended
alongside each `log()` call) or parse the markdown back. **jsonl is the right
call** — parsing our own markdown back is a bug farm the first time an agent
writes a `## heading`.

## API surface (app.py)

- `list_sessions()` → `[{id,title,created,updated,ended,participants:[{provider,name}],rounds}]`, newest first. `id` is the session directory basename, never an arbitrary path.
- `open_session(id)` → `{session, messages}`; refuses while a run is active, validates that `id` resolves to a direct child of `SESSIONS_DIR`, rehydrates `self._conv`, and makes that session active. `session` has the same summary shape returned by `list_sessions`, plus `workspace`, `transcript`, and `can_continue`.
- `rename_session(dir, title)` / `delete_session(dir)`
- `new_conversation()` → what the existing reset button does

`messages.jsonl` rows use a UI-ready stable shape:

```json
{"speaker":"josh","provider":null,"name":"Josh","text":"...","round":0,"meta":""}
```

`speaker` is the persisted seat id for an agent (not its label), matching the
current `message` event. Replay is therefore just the ordered JSONL rows passed
through the existing renderer; `open_session` should not emit one JS event per
message from its bridge call. Corrupt trailing JSONL rows are skipped so a crash
during append does not make the entire chat unopenable.

Rename changes only `meta.json:title`, not the directory name. Delete is refused
for the active/running session and removes exactly one validated session folder.
Opening a different session first clears stale queued human input. An old folder
without `meta.json` may be listed as a legacy/read-only transcript, but must not
be presented as resumable.

Both loops (`relay._rounds`, `app.Api._rounds`) need the save hook. Per
CLAUDE.md that's the standing duplication tax — write it once as a
`save_state(state)` helper in relay.py and call it from both.

## Open questions

1. Does a *stale* claude/codex session id still resume days later, or does the
   CLI GC it? Needs an actual test — if it fails we must degrade gracefully
   (reopen read-only, offer "continue with a summary" via COMPACT_PROMPT).
2. Projects (Josh mentioned "every different project and chat"). Suggest we
   ship flat chat history first, group-by-workspace second.
3. ~~Sidebar currently holds the seat config UI. Where does the chat list live?~~
   RESOLVED (Claude 2): second rail, no tabs. See "UI contract" below.

## Suggested split

- **Claude**: persistence layer in relay.py (`save_state`/`load_state`, meta.json,
  messages.jsonl, atomic writes) + wiring into both loops.
- **GPT**: `Api` methods (list/open/rename/delete) + rehydration in app.py,
  incl. the `started`/`message` event replay so the UI feed refills.
- **Claude 2**: the UI — sidebar chat list, active-row highlight, replay
  rendering, new-chat button. Talks to the API above.

Contract between us is the `meta.json` schema + the four `Api` methods. Argue
with those *now*, then we can work in parallel without stepping on each other.

## UI contract (Claude 2 — what the rail needs from the API)

**Layout: a second rail, not tabs.** `main` becomes
`#chatRail (220px) | aside (270px) | .stage`. Window is 1220 wide, so the feed
keeps ~700px — fine. Tabs lose: the seat cards carry the live `thinking…`
state (`seatState()`), so they cannot be hidden behind a tab during a run.
`#chatRail` = "＋ New chat" button, then session rows newest-first, each
`title` / `updated` relative + participant dots in seat colors. Active row
highlighted. ✕ on hover → delete (confirm inline), double-click title → rename
in place. Collapsible via the rail-label if it ever feels cramped.

**Replay reuses `addMsg()` verbatim.** So `messages.jsonl` rows must be exactly
the `message` event payload plus `round`. Two additions to that shape:

1. **Persist system lines too** (`{"speaker":"system","provider":null,
   "name":"relay",…}`) — `/clear`, `/compact`, round-cap notices, agent errors.
   `addMsg("system", …)` already renders them. Without this a reopened chat
   silently differs from the one Josh watched: "Claude's memory was cleared"
   vanishes and the transcript stops explaining itself.
2. `meta` stays the display string (`"round 3"` / `"opening"`), computed by the
   writer, not re-derived in JS.

**Fields the rail needs that aren't in the schema yet:**

- `title`: send the **full** opener line; the rail ellipsizes in CSS and uses it
  as the `title=` tooltip. Don't pre-truncate to the 50-char dir slug.
- `can_continue_reason`: string, empty when continuable. The composer
  placeholder and the after-row have to say something true — "legacy chat, view
  only" vs "ended" vs "agent memory expired" are three different sentences and I
  can't honestly infer them from booleans.
- `participants` needs `provider` **and** the seat `label` (for the dots +
  tooltip) — `list_sessions` already promises both, just keep label ≠ provider
  for duplicate seats ("Claude", "Claude 2").

**Events:** `started` should carry the session summary dict (`session`) so the
rail can insert + select the new row immediately instead of polling. I re-fetch
`list_sessions()` on `done`. No per-turn event needed — per-turn sidebar churn
buys nothing.

**Stale-id failure must be a question, not a silent fallback.** GPT proved
same-day resume; nobody has proved day-old. When a resume fails, the honest
path is `agent_error` for that seat + a one-time prompt ("Claude no longer
remembers this chat — start a fresh session seeded with a summary?") with a
button. Auto-re-seeding from `COMPACT_PROMPT` behind Josh's back is the same
class of bug as the forged `(no reply)` turn in CLAUDE.md: the UI would claim
continuity the agents don't have.

### Landed (Claude 2) — `ui/index.html` only, no .py touched

`#chatRail` + `renderChats`/`openChat`/`startRename`/`armDelete`/`newChat`/
`restoreSeats`. Verified in Chrome against a stubbed bridge
(`ui/_harness.html`, gitignored, regenerated from `index.html` + a fake
`pywebview.api`): 3 rows render with correct provider dots, click replays 5
messages incl. a system row, seats rebuild with their **original ids** so
`seatState()` still resolves, rename commits on Enter/blur, delete is a
two-step arm, New chat resets the stage. No console errors.

Two things found by running it rather than reading it: `renderAccounts` sets
`--<provider>` on `documentElement` from the auth payload's `color`, so the rail
dots inherit the live registry — a payload without `color` writes the literal
string `undefined` and every dot goes transparent. And a saved model missing
from the picker used to silently display the *default* model; it now appends the
saved value as an option instead of captioning the chat with the wrong model.

~~**Still needed from the API:** `transcript` key~~ — landed; both after-row
buttons show on a reopened chat.

**Final check against real persisted data** (post-integration, zero tokens):
`ui/_harness.html` regenerated from actual `relay.list_sessions()` +
`read_messages()` output. 13 rows — 12 legacy view-only, 1 resumable — and
opening the real one replays its 2 stored messages, rebuilds `seat-0`/`seat-1`
as "Claude"/"Claude 2" (duplicate seats, `claude-haiku-4-5`/`low`, straight from
`meta.json`), locks the pickers, shows both after-row buttons, and sets the
continue placeholder. No console errors.

**Fatal-seat display (Claude 2, after the stale-id fix).** `agent_error` with
`fatal: True` marked the seat "error" — and then `finish()` and
`applyAuthToSeats()`, which both hardcoded `"ready"`, repainted it two lines
later. The seat that had just permanently lost its CLI session ended the run
captioned *ready*, next to a feed telling Josh to `/clear` it. Fixed with a
`fatalSeats` set: the card reads "session lost" with the dashed `.noauth`-style
border and survives `done`. It is cleared only by **evidence** — that seat
emitting `thinking` again (i.e. actually taking a turn after a `/clear`) — or by
the chat changing. Deliberately NOT cleared by seeing a `/clear` command go by:
that would mean re-implementing `match_seats` in JS, and a duplicated matcher
that drifts would mark a healthy seat dead or a dead seat healthy. Verified:
during-run → `thinking…`; fatal → `session lost`; after `done` → still
`session lost`; after that seat's next turn → `ready`. Non-fatal `agent_error`
still shows a transient `error` that clears at `done`, unchanged.

**Harness recipe** (regenerate it whenever `index.html` changes — it's a stale
copy otherwise, and it's gitignored): read `index.html`, inject a
`<script>` before `</head>` defining `window.pywebview.api` with the real
`list_sessions`/`read_messages`/`_fallback_config` JSON, dispatch
`pywebviewready` on load. Gotcha: **`node --check` the injected stub before
trusting a red result** — an escaping slip in the generator produced a stub that
silently never executed, which looks exactly like "the rail is broken."

**Bridge-thread rule applies:** `list_sessions`/`open_session` run on the
js-bridge thread → pure file reads only (one `meta.json` + one `messages.jsonl`
per call). No subprocess, no workspace walking, no transcript reads — same trap
`get_auth_status` already fell into.

### Landed (Claude) — loop wiring, both loops

`relay.main()` and `app.Api._rounds` both persist now. `store.save(state)` fires
after the fan-out on every completed turn (not before — the saved queues must
match what each seat still *owes*, or a resume replays a turn or drops one),
plus after Josh's opener, after every interjection, after `/clear`/`/compact`,
and at `done`.

- **`continue_block` invariant fixed** per GPT: blocks when an `introduced`
  seat has no `session_id`; a pre-first-turn crash (no ids, nothing introduced)
  stays resumable, so Josh's lone opener survives.
- **`transcript` added to `session_summary`** (both the meta and legacy
  branches) per Claude 2.
- **`started` carries `session`** — the row appears the instant a run starts.
- **`done` reads `can_continue`/`can_continue_reason` back off disk** instead of
  hardcoding `True`. If a seat's id didn't persist, the composer says so.
- **One logger, `relay.make_log(state, store, echo=None)`** — maps display name
  → stable seat id once. It replaced GPT's equivalent closure in
  `open_session` and the two hand-rolled `log` closures. `record()` writes the
  transcript line and the JSONL row in a single call, so the two can't drift.
- `reset_conversation` saves `ended=True` — display state only; the chat still
  reopens and still takes a message.

Verified with a stub `Agent` subclass (zero tokens, real session-id churn):
2 seats × 2 rounds → `_conversation`, then a **brand-new `Api` object** (a real
restart, nothing in memory) → `open_session` → `_continue`. Session ids, pending
queues, `introduced`, `rnd`/`max` all survive; the resumed seat gets the pending
queue and *not* a fresh preamble; rounds advance 2 → 3. Also checked: `/clear`
nulls the id, persists a system row and leaves the chat continuable; an orphaned
introduced seat blocks; a truncated trailing JSONL row is skipped; `../..`
rejected; all 12 legacy folders list as view-only.

**Stale-id failure is tested; day-old retention is not.** Bogus saved ids were
tried against the real Claude and Codex CLIs: both fail rather than silently
starting fresh. `fatal_seat_error` classifies those permanent failures, skips
the pointless retry, persists one fatal system row, preserves the seat's queue,
and stops the run. Explicit `/clear <seat>` was verified to null the dead id,
un-introduce the seat, and let it rejoin with its still-queued messages plus the
next human message. Gemini remains untested because `agy` is not installed.
Whether otherwise-valid ids are retained for days is still calendar-dependent.
