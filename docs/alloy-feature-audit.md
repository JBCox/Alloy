# Alloy feature audit — multi-AI collaboration inventory

2026-08-25 · Written from the code itself (`relay.py`, `app.py`, `ui/index.html`,
`workstreams.py`, `outcome.py`, plus the supporting modules), not from memory or
marketing. Purpose: one structured inventory so a later feature-diff step can
compare an external product (e.g. Traycer) against what Alloy already ships
**without re-reading the codebase**. Companion to `docs/competitor-feature-gaps.md`
(gaps vs. other frameworks) and `docs/feature-ideas.md` (Alloy-native leverage).

---

## 1. Architecture in one paragraph

Alloy is a local desktop app + CLI that runs autonomous conversations between
multiple AI coding agents, each powered by its OWN official CLI logged into its
OWN account (no API keys anywhere): Claude Code CLI, OpenAI Codex CLI, Google
Antigravity CLI (`agy`), and OpenCode CLI (free Zen gateway models). One shared
engine loop (`relay.run_rounds(state, io)`) serves every front end through a
`LoopIO` seam (CLI console or pywebview window). Seats share ONE workspace
folder; every visible word is persisted to append-only logs; the human owns
start/stop and is pulled in through structured question/approval channels.

## 2. Providers and seats

- Provider registry `PROVIDERS` (relay.py:2334): claude, gpt (codex), gemini
  (agy), ox (opencode), grok (registered, Accounts-panel only — adapter not
  built). One entry = adapter class, color, auth probe, login/logout argv,
  skills dir, MCP descriptor, install hint.
- **Duplicate seats**: same provider multiple times (`provider:model:effort=label`
  syntax); auto labels ordinal ("Claude", "Claude 2"); same-model note in the
  preamble so instances don't mistake each other for echoes.
- Per-seat model + effort/thinking pickers; thinking levels are PER MODEL (read
  from models.dev cache — levels that do nothing disappear rather than being
  offered).
- Auth probes never spend tokens; zero-credential Ox counts as signed_in (free
  tier needs no account). Probes return `unknown` on garbage/timeout, never a
  guess. Accounts modal: status, sign in (visible OAuth console), sign out,
  install hints; red badge counting seatable providers that are out.
- Capability honesty: each adapter declares what its CLI actually grants on
  THIS install (GPT+Gemini generate images, Claude does documents via skills;
  Gemini image writes are harvested from its brain dir by the relay). Rendered
  in a preamble "What each participant can actually do" block only when ≥2
  seats declare something.

## 3. Orchestration engines and modes

Eight modes, four real dispatch engines (sequential / parallel-barrier /
free-reactive / panel), consolidated by an audit that also fixed four honesty
bugs. Mode is validated against `IMPLEMENTED_MODES` at start — no silent
fall-through.

| Mode | Engine | What happens |
|---|---|---|
| round_robin | sequential | cyclic floor |
| speaker | sequential | seats nominate the next speaker with `[[NEXT]]` |
| moderator | sequential | cheap stateless side call picks each turn, may end run |
| parallel | barrier | all seats answer simultaneously per round |
| free | reactive | seats reply whenever messages arrive, throttled lead |
| supervisor | barrier | planner decomposes goal into workstream tasks (see §6) |
| panel | barrier | isolated drafts → critiques → synthesis fan-out |
| battle | barrier | exactly two seats answer unseen; human votes; Elo |

- Six-axis policy recipe behind the labels (`ORCHESTRATION_VALUES`):
  concurrency, floor, workflow, routing (broadcast/addressed/isolated),
  completion, budget_unit — legacy mode strings remain the compatibility
  surface.
- Presets in the UI: Discuss in Turns / Talk Live / Compare & Decide / Build
  Together / Keep Improving (composer-bar pill; hand-edited axes read Custom).
- Round budget OR until-done mode with a safety ceiling; `/turns`, `/ceiling`.

## 4. How communication happens (seat ↔ seat)

This is the section Traycer's communication design should be diffed against.

- **Per-seat queues, commit-consume invariant**: every seat has its own pending
  backlog; prompts snapshot WITHOUT clearing; only successful commits consume
  exactly the consumed prefix and fan out. Failures restore nothing because
  nothing was removed.
- **Broadcast fan-out** is the default: each reply is appended to every other
  seat's queue. Routing axis supports broadcast/addressed/isolated (panel
  drafts are isolated to kill duplicate prompt tokens).
- **Turn-taking directives**, one grammar (`peel_directives`, end-anchored,
  last-`[[` anchor): `[[WRAP]]` ends the conversation, `[[NEXT: seat]]`
  nominates, `[[TO]]`/`[[PASS]]` legacy aliases. Directives relay verbatim as
  text, rendered as chips in the UI.
- **Moderator floor**: a stateless cheap-model side call decides who speaks
  and can answer DONE. Moderator/supervisor is ONE role under two labels; any
  seatable provider can hold it (picker rebuilt from the live registry); the
  role is nameable (`room_helper_name`) and both builders read state, not the
  agent object.
- **Supervisor tasking**: workstreams carry briefs + literal file ownership +
  dependency edges; workers are radio-silent until settlement; only a
  filesystem-VERIFIED summary crosses streams (see §6).
- **Human interjection channels**: typed composer mid-run (queued to the loop),
  session `say.txt` file drop (CLI), slash commands (queued when running,
  executed when idle).
- **@-mentions**: `@Claude 2 …` queues to ONE seat (longest-match ≤3 words;
  no match/ambiguous/bare address = literal broadcast), delivered with an
  audience/delivered_to envelope, deliberately outside digest/fan-out sync.
- **Attachments**: paperclip button, paste, and drag-drop queue base64 chips
  that land in `<workspace>\attachments\`; message text gains
  `[Josh attached a file: <path>]` lines so ALL seats, transcript, and replay
  see them identically.
- **Never forge a turn**: empty/error replies are never relayed as if spoken;
  failed seats take retry→backoff→skip paths and are announced; all-seats-dead
  ends the run visibly as `starved`.
- **Streaming activity between turns**: stdout/stderr line hooks translate each
  CLI's stream vocabulary into live "working on X" lines shown under typing
  indicators (per-seat, deduped, capped); finished rows keep a collapsed
  activity log ("X worked through N steps").

## 5. Spawning (three tiers)

1. **Native CLI subagents** — each CLI's own subagent feature surfaced and
   announced in the preamble; hideable (`--no-native-subagents`).
2. **Helpers** `[[SPAWN: provider[:model[:effort]] | task]]` — one-shot side
   agent sharing the workspace; result returns ONLY to the requester; capped
   per conversation; refusals become notes in the requester's queue (never
   silent, never forged, never auto-retried).
3. **Child teams** `[[TEAM: seats | rounds=N mode=m | task]]` — a whole child
   session (depth 1, ≤6 rounds, spawn policy zeroed in children); child meta
   carries `parent`, parent meta lists `children`; replayable from the rail;
   ask disabled in children.
- `SpawnManager` delivers results only at loop boundaries; in-flight side-work
  at a crash is declared lost on next run.

## 6. Supervisor / workstreams (planned parallel work)

- Stateless planner call emits stacked `[[TASK: …]]` directives → parsed into
  slot-id-keyed task records with deps, file lists, statuses
  (`workstreams.py`). Prompt explicitly forbids clarifying questions (a skill
  leak once starved runs).
- Scheduling: capability gate → overlap auto-serialization (declared file
  ownership instead of git worktrees) → dependency-respecting dispatch through
  the parallel loop. Strict radio silence while active.
- **Filesystem verification before credit**: claimed artifacts must exist on
  disk; failures get exactly one repair attempt reusing the task id (DAG stays
  valid); settlement summaries capped.
- **Rolling manager**: when the board drains, the supervisor reviews evidence
  FIRST (filesystem results, then worker reports labelled as claims) and either
  accepts the goal (`[[DONE: verdict]]` → ended `wrapped` on the manager's
  word) or plans the next wave; bounded waves, every trace entry wave-stamped.
  Terminal states separated: `goal_accepted` vs `goal_unresolved` (waves spent
  or cap hit mid-job). Planner failure degrades VISIBLY to ordinary parallel
  conversation and latches so resume doesn't re-plan forever.
- Control-log UI renders WAVES (collapsible plan→work→review containers), a
  verdict card for acceptance, a pulsing "still deliberating" entry, and a
  compact rail badge derived engine-side.

## 7. Keep Improving (continuous mode)

Build Together with the brakes off; identical on all six policy axes except
`continuous.on`. No round cap, no turn ceiling; manager chooses the NEXT
objective itself when one is met (`[[OBJECTIVE]]`/`[[IDLE]]`), resetting the
wave cap while the wave index keeps climbing.

- Human-set limits only: spend cap, hours cap (accumulated clock survives
  resume — mark resets, total doesn't), and whether the check-in may stop the
  run; all three off is legal and the warning modal says so in those words.
- Scheduled health-check watchdog (`run_checkin`): measured snapshot (committed
  turns, stuck tasks, dead seats, last gate) → verdict `[[HEALTHY]]` /
  `[[FIX: remedy | why]]` / `[[STOP]]`; remedy set CLOSED (requeue/replan/
  next_objective/clear_seat/nudge) — unrecognized remedies decline visibly.
  Check-in actions `auto|notify|permission` (permission makes the run WAIT,
  including overnight); unanswered check-in = SKIP, never approval.
- Verification gate: project test command detected/run BEFORE manager review;
  green waves optionally committed to git (`gate_commit`); `gate` events fire
  for both colors.
- Crash revival: runs ended by anything except Josh's Stop or his limits are
  restarted, bounded barren-revival counter; `was_interrupted` lifecycle stamp
  is the only honest auto-resume signal; two fruitless resumes block a third.
- A seat's `[[ASK]]` cannot wedge an unattended run: deadline composes with the
  check-in interval; expiry takes the documented unanswered path.
- Mid-run steering: `/checkin`, `/objective <text>`, `/limits`.

## 8. Human-in-the-loop

- **[[ASK]] directive**: seat ENDS a reply with a question + up to 6 options;
  conversation pauses on the `ask_human` seam; UI shows a seat-colored modal
  (chips instant-answer, Other box, Skip = empty) plus a reopen pill; CLI
  prompts on console (number picks); headless default answers None instantly.
  Answer fans out as a REAL Josh row. Unanswered/aborted becomes a relay note,
  never a forged answer.
- **Permission ladder** (per chat): read_only / ask / auto (workspace) / full
  (yolo alias). "Ask" wires a PreToolUse approval bridge (`approval_hook.py`)
  INTO the Claude seat: every write/exec tool pauses for the human; fails
  CLOSED; answers Allow once / Allow rest of turn / Deny (+ deny rest, +
  session-scoped always-allow).
- **Workspace boundary enforcement**: adapters confined to the working folder
  (realpath-before-containment, junction escapes fail, quiet identical errors,
  no existence disclosure).
- **Connectors opt-in**: MCP access for the Claude seat is a separate explicit
  checkbox (server-name prefixes auto-approved) — deliberately NOT tied to
  yolo, because connectors reach real personal accounts.

## 9. Shared context engineering

- Each CLI auto-loads only ITS OWN project doc (CLAUDE.md / AGENTS.md /
  GEMINI.md), so cross-seat context rides the preamble: docs ≤ BRIEF_MAX
  (4000 chars) quoted VERBATIM; oversized sets get a synthesized brief cached
  by source sha256 (one call per change, not per chat).
- Recorded context is REPLAYED on resume (`project-context.md`), drift is
  reported, never regenerated mid-run (later-cleared seats must see what peers
  saw). Spawned teams inherit the parent record.
- **Per-seat roles**: public name + PRIVATE instructions on each seat
  (specialization by instruction, never capability); roster block in preamble;
  edits cost a CLI turn once seated (staged, applied at turn boundary or idle).
- Same-model note, capability notes, native-spawn notes: the preamble states
  what build flags actually grant — never brand assumptions.
- Working folder: default per-session scratch OR any folder Josh picks
  (custom folder turns on shared project context; `--workspace`, `--no-brief`).

## 10. Persistence and session lifecycle

- Per session: `transcript.md` (human log), append-only `messages.jsonl`
  (UI replay; every row carries ISO timestamp + optional activity + usage),
  `meta.json` v2 (CLI session ids, queues, orchestration recipe, round state —
  atomically written with replace-retry), `outcome.json`, optional
  `project-context.md`, `say.txt`, `workspace/`.
- Continue/resume: continuation validation; dead session ids fail LOUDLY once
  then park the seat (never auto-reseeded — no forged memory); reopened chats
  rebuild live agents with saved ids; mid-turn reopen replays thinking state
  with true start times.
- **Fork/branch** at any message (regenerated transcript, sanitized meta,
  cleared provider threads — no forged continuity), provenance shown in
  tooltips; **HTML export** self-contained deterministic single file; archive,
  pin, rename, two-step delete; cross-chat full-text search; tabs persistence;
  saved room templates (capture exact send config, start from template);
  auto-titling (one stateless side call after round 1, forks inherit).
- Battle verdicts persist to `sessions/leaderboard.json` (Elo, K=32, tie=half).
- Self-restart machinery: standalone script + in-app request-at-turn-boundary,
  gated on the full test suite passing first, host/ownership guards before any
  kill.
- Legacy transcript-only folders remain view-only listable; v1 metas stay
  continuable.

## 11. Telemetry, feedback, learning

- Truth-over-estimation usage telemetry: streamed cost/token extraction per
  turn (Claude/GPT; seats reporting nothing stay honestly blank), central
  accumulator by-seat/by-kind (seat, supervisor, moderator, helper, team,
  brief, retry, failed), per-message pills, LIVE budget bar with burn clock
  and ≈ projection against caps.
- Outcome records (`outcome.py`): hard_facts (structural only — turns, usage,
  asks answered, interventions, artifacts, termination_reason, goal verdict,
  lifecycle) strictly separated from human_feedback (end card, per-message
  reactions 👍/👎) and reserved model_eval; rebuildable additively for ANY old
  session.
- Retro aggregator (`retro.py`): derives provenance-backed rules (human reason
  tags immediate; inferred patterns after recurrence) into a human-editable
  pinned/dismiss-aware playbook with 30-day decay.
- Termination taxonomy incl. wrapped/starved/limit; supervisor badge states.

## 12. Desktop UI surface (ui/index.html)

- Chat-history rail: provider dots, project-grouped collapsible headers ranked
  by newest chat, "Needs input" ranking above pins, tooltips carrying
  time/seats/view-only/fork provenance, replay, dblclick-rename, two-step
  delete, ⤓ export, ⑂ fork, ★ pin; multi-tab support.
- Seat rail: add/duplicate seats, per-seat model + thinking pickers, editable
  names (typed label vs auto placeholder), role buttons + shared modal,
  permission pill, yolo toggle, rounds stepper (typable, clamped visibly),
  until-done ceiling morph, helper/team budgets, moderator picker + nameable
  moderator, working-folder picker, brief checkbox, connectors checkbox — all
  locked once seated and restored truthfully on reopen.
- Messages: markdown with trailing-directive chips, ==highlight== marks,
  timestamps, copy buttons, per-row usage pills, reactions, collapsible
  activity blocks, image thumbnails with strict/loose resolution + quiet
  missing placeholders + full-res lightbox, spawned-row ↳ captions.
- Live: per-seat typing indicators WITH streaming activity lines and watchdog
  clocks (silence surfaced honestly), live code viewer with prev-snapshot
  line diffs colored by editing seat, Files rail (newest-first, previews,
  open-in-OS), supervisor wave/task control log, continuous strip + banner,
  budget strip, battle blinding/reveal/vote flow.
- Modals: accounts, skills/MCP, roles, rooms, event hooks, continuous warning
  (acknowledgement-gated OK whose wording changes when nothing can stop the
  run), ask/question. Keyboard shortcuts with ? cheat sheet rendered verbatim
  from the engine's constant. Sound cues on question/checkin/done
  (toggleable, remembered). Empty state renders the live roster cluster.
- Branding: Alloy trefoil icon/wordmark; repo/CLI stay `ai-chat` on disk.

## 13. Voice I/O and triggers

- **Dictation**: local faster-whisper transcription, sounddevice capture,
  hold-to-talk + tap-latch + Ctrl+Shift+Space, caret insert (never auto-send),
  probe() names WHICH piece is missing; a documented Wispr Flow seam refuses
  by name (paid API would break the no-keys rule). Empty/failed transcriptions
  never produce words.
- **Read-aloud**: Windows SAPI via hidden PowerShell child, base64 stdin
  transport (injection/codepage-proof), latest-wins stop, per-row 🔊 toggle,
  same probe() honesty.
- **Webhook trigger** (`webhook.py`): loopback-only POST /start starts a
  conversation from outside (strict payload whitelist, unknown keys rejected,
  token via compare_digest, refuses while a chat is live); UI toggle with
  copy-curl.
- **Event hooks**: user shell commands on question/checkin/done/gate_red
  (unknown names rejected; env carries event/session/title/detail; timeouts
  swallowed).

## 14. Skills and MCP management (cross-provider)

- Skills are FOLDERS; whole-tree atomic install/swap (sidecars travel),
  case-insensitive SKILL.md lookup, BOM-tolerant frontmatter, divergence
  detection by normalized-text sha256, extras count, refusal semantics on bad
  deletes. One editor reconciles the ticked provider set (tick=install,
  untick=remove) + one-click "install to GPT and Gemini".
- MCP servers managed per provider through each CLI's real mechanism (claude
  `-s user`, codex JSON list, agy config file); cache invalidation after every
  mutation so granted tool prefixes stay fresh.

## 15. Integrity invariants (the load-bearing rules)

- Never forge a turn (empty/error ≠ speech; failed `result` objects parse as
  errors, not replies; unanswered questions ≠ answers; skipped checks ≠
  approvals; absent telemetry stays blank).
- Commit-consume queue invariant everywhere.
- Threading contract: one thread per Agent ever; fixed lock order; ONE emitter
  thread owns all JS evaluation and window interaction; subprocesses never on
  bridge threads; stderr drain threads mandatory.
- Watchdog measures SILENCE, not duration (effort-scaled idle window restarted
  by every line; absolute ceiling only if Josh sets one); provider-wobble
  backoff + probation windows at all four retry ladders; fatal errors (dead
  session id, auth, missing CLI) never retried.
- Atomic writes with os.replace retry; ASCII/BOM discipline for .ps1; npm-shim
  resolution to node; command-line length caps bounded by small BRIEF_MAX.
- Tests: ~1255 token-free tests across 56 suites (FakeAgents drive the REAL
  loop; a node harness executes the UI's inline script against a stub DOM;
  custom-runner suites judged by exit code); the restart gate runs them all.

## 16. Known gaps already catalogued elsewhere

Do NOT re-derive here: `docs/competitor-feature-gaps.md` lists what CrewAI/
LangGraph/AutoGen/MetaGPT/etc. ship that Alloy lacks (agent-to-agent handoffs,
hierarchical dynamic delegation, nested chats, structured output contracts,
persistent long-term/entity memory, RAG over workspace, shared sandbox/browser
tools, artifact preview panes, mermaid/LaTeX…); `docs/feature-ideas.md` lists
Alloy-native leverage ideas (diff lane, checkpoint rewind, gate scoreboard,
blind panel, disagreement map…). Any Traycer-vs-Alloy diff should fold its
findings into those two documents rather than starting a third list.
