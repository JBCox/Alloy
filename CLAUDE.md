# AI Chat — multi-AI conversation relay

Claude (Claude Code CLI, Max), GPT (OpenAI Codex CLI, ChatGPT Pro), and Gemini
(Google Antigravity CLI `agy`, free Google login) hold autonomous conversations.
**No API keys anywhere** — every agent authenticates through its official CLI's
account login. Built 2026-08-16. Owner: Josh.

## Components

| File | Role |
|------|------|
| `outcome.py` | Standalone session-outcome recorder. `run_rounds` is a thin wrapper over `_run_rounds` and writes `outcome.json` in `finally`, once for every mode/front end and exit path. The record keeps `hard_facts`, optional `human_feedback`, and reserved `model_eval` separate; `write_outcome` preserves feedback across rebuilds and `set_feedback` is the app's only validated write path. |
| `workstreams.py` | Pure concurrent-work scheduler used by the shared loop: slot-id-keyed task records, strict in-flight context isolation, dependency scheduling, overlap auto-serialization, literal-file ownership, filesystem verification, and settlement summaries. `relay.assign_workstreams` seeds private queues; `commit_reply` settles/verifies an active task and only its summary crosses streams. Workstreams persist additively in meta and emit `workstreams` UI events. |
| `retro.py` | Deterministic outcome aggregator behind `/retro`. It scans `sessions/*/outcome.json`, keeps hard facts separate from explicit feedback, derives only provenance-backed rules (human reason tags immediately; inferred patterns after recurrence), and atomically refreshes the human-editable `sessions/playbook.json`. Pinned/dismissed choices survive merges; unpinned inactive rules decay after 30 days. |
| `relay.py` | Engine + CLI. Agent adapters (`ClaudeAgent`, `CodexAgent`, `GeminiAgent`), THE shared loop (`run_rounds(state, io)` + `LoopIO` seam + `CLIIO` terminal front end + `dispatch_command`/`seat_command`), say.txt/stdin interjection, and the durable session layer (`SessionStore`, `make_log`, readers/listing, continuation validation, rehydration). Every visible message is appended to both `transcript.md` and `messages.jsonl`; `meta.json` atomically snapshots CLI session ids, queues, seat config, and round state after fan-out. Interject commands: `/stop`, `/turns N`, `/clear [seat]`, `/compact [seat]` (compact = seat self-summarizes via `COMPACT_PROMPT`, then its session restarts seeded with the summary; seat arg = label or provider, omitted = all; helpers `match_seats`/`compact_agent` shared with app.py). Also owns `PROVIDERS`, the single provider registry (adapter class, color, auth probe, login/logout argv, install hint) — `AGENT_TYPES` is derived from it; grok is registered with `agent=None` (Accounts-panel-only) until its adapter lands, and adding a provider = one entry. Auth probes (no tokens spent): claude → `claude auth status --json`; codex → `codex login status` with `~/.codex/auth.json` fallback on timeout; gemini → file check of `~/.gemini/oauth_creds.json` + `google_accounts.json` (agy has NO auth subcommand; can't detect revoked tokens). Probes return `unknown` on garbage/timeout — NEVER guess signed_out. `logout_gemini` moves creds to `~/.gemini/aichat-logout-backup-<stamp>\` (restore = move the files back). `resolve_cmd`/`clean_env` are the extracted shim-resolution + env-strip helpers used by turns, probes, and app.py. **Skills + MCP management** (see the gotchas): `PROVIDERS` entries carry `skills_dir` + an `mcp` descriptor (`kind="cli"` with `argv`/`fmt` for claude+codex, `kind="file"` with `path` for agy's `mcp_config.json`); `manageable_providers()` is the seatable-with-a-CLI subset (grok excluded). Skills: `valid_skill_name` (reject, never sanitize), `skill_file_in` (case-insensitive `skill.md`), `parse_skill`/`render_skill` (BOM-tolerant frontmatter), `list_skills` (normalized-sha divergence + `extras` count), `read_skill`, `write_skill(..., source=)` (whole-tree copy, atomic swap), `delete_skill` (refuses a folder with no SKILL.md). MCP: `list_mcp` (`_parse_mcp_line_list` for claude, `_parse_mcp_json_list` for `codex mcp list --json`, JSON file for agy), `add_mcp`/`remove_mcp` (claude gets `-s user`), `invalidate_mcp_cache`. **Streaming turns + live activity** (2026-08-17): `Agent.turn(message, on_activity=None)` runs the CLI via `Agent._run_streaming` (Popen; stdout read line-by-line on the seat's own thread, stderr drained by a helper thread, `threading.Timer` watchdog enforcing the effort-scaled `self.turn_timeout` = `TURN_TIMEOUT × TIMEOUT_SCALE[effort]`); each stdout line goes through the per-adapter `activity(line)` hook (claude: stream-json assistant events — thinking blocks + tool_use described per tool; codex: the `--json` item.started/item.completed vocabulary, verified live 2026-08-16; gemini: none — but see the image harvest below) and the resulting `{kind, text[, path_raw]}` dicts flow through `make_activity_sink` (per-turn: consecutive-dedupe, 160-char texts, `ACTIVITY_MAX` cap, and **edit-path confinement** — `path_raw` from a CLI stream is untrusted, `confine_to_workspace` (lives HERE, app re-exports) either relativizes it or drops the whole event silently) out as `activity` events, and persist (last `ACTIVITY_KEEP`) as the message row's optional `activity` list. ClaudeAgent runs `--output-format stream-json --verbose` (--verbose is REQUIRED in -p mode; final line is the same result object, `_result_object` reverse-scans for `"result"` as hardening). `describe_failure(stdout, stderr)` is the per-adapter error sentence used by BOTH raise paths (claude: subtype + HTTP status + the CLI's `result` text; codex: `error`/`message` events; base: the stderr/stdout tail). Every row also carries `ts` (ISO seconds) — stamped in `SessionStore.record`, echoed as HH:MM in transcript.md headers. `note_retry` persists first-failure retry notices as system rows; `TurnTimeout` errors are `no_retry` and name minutes, `error_excerpt` keeps head+tail of long errors. `ai-chat` on PATH (`~\.local\bin\ai-chat.cmd`) calls it. Per-seat **roles**: `Agent.role` (public name) + `Agent.role_instructions` (private text) live on the agent like `name`, so `preamble()` reads them without either loop passing new args; `preamble(..., roster=agents)` prints the roster in turn order. `apply_role_flags` applies `--role`/`--role-instructions` through `match_seats` after `assign_labels`. `SessionStore.record(role=)` stamps the role into each row. **Orchestration** (ORCHESTRATION_DESIGN.md): `MODES`/`IMPLEMENTED_MODES` + the per-turn scheduler (`choose_next_seat`, `compose_prompt`/`commit_reply`/`commit_skip`), `run_parallel` and `run_free` (the two threaded modes), `peel_directives` (the one trailing-directive grammar; `wrap_called` is a one-liner over it), `set_next_speaker`, `moderator_pick`/`build_moderator`, and `SpawnManager` + `handle_spawn_directives`/`parse_spawn`/`parse_team` for the three spawning tiers. **Asking Josh** (ORCHESTRATION_DESIGN.md § Asking Josh): `parse_ask` + `handle_ask_directive` (runs after `commit_reply` in all three loops, blocks on the `LoopIO.ask_human(payload, abort=None)` seam — headless default answers `None` instantly so nothing ever hangs) + `announce_lost_ask` (run start, next to `announce_lost_helpers`); gate `state["ask"]` (CLI `--no-ask`, app always True, child teams False) toggles the preamble's "Asking Josh" block + softened header; `ask`/`ask_pending` persist additively in meta; `CLIIO.ask_human` prompts on the console with an `_asking` flag so concurrent coordinator drains can't steal the typed answer. **Shared project context** (PROJECT_CONTEXT_DESIGN.md; see the gotcha below): `BRIEF_DOCS`/`project_doc_names()` (the FIXED scan set) + `Agent.project_docs` per adapter (what each CLI already auto-loads — drives only the per-seat "you already load this" line; `test_brief` guards the two against drifting), `find_context_docs`/`quote_docs` (verbatim path), `brief_status`/`brief_fingerprints`/`write_brief`/`read_brief` (sha256-keyed staleness for the synthesized path), `BRIEF_PROMPT`/`synthesize_brief` (a throwaway stateless adapter exactly like `build_moderator`), `project_brief` (the one orchestrator both front ends call), `brief_preamble_block`, `brief_record`/`write_project_context`/`read_project_context`/`brief_drift`. Gated on `session_project(...)` being truthy, so default in-session workspaces are untouched. |
| `app.py` | Desktop app: pywebview/WebView2 window hosting `ui/index.html`. Imports relay's adapters/session helpers and runs the SHARED loop: `_rounds(state)` is now a thin wrapper that calls `relay.run_rounds(state, _AppIO(self))` then runs the app epilogue (paused footer + `done` emit). `_AppIO` (module-level class) adapts `emit`/`_human_q`/`_stop_flag`/staged-role commit to the LoopIO seam. `list_sessions`/`open_session`/`rename_session`/`delete_session` are bridge-thread-safe file operations; `open_session` rebuilds live agents and assigns their saved CLI ids, so a new process can continue the conversation. `command(text)` handles the same slash commands as relay: queued to the loop when running, executed on a worker thread when idle. Accounts: `precompute_auth` (startup thread, thread-per-provider, progressive `auth_status` emits), `get_auth_status` (cache snapshot ONLY — must stay subprocess-free/non-blocking, it runs on the bridge thread), `recheck_auth`/`sign_in`/`sign_out` (bridge → worker thread). Pre-flight `_auth_blockers` gates `_conversation`/`_continue` on cached signed_out/not_installed only — unknown/pending NEVER blocks. Roles: `apply_role(seat_id, role, instructions)` only STAGES (bridge thread — never subprocess there); `_commit_roles` drains the staging area at a turn boundary inside `_rounds`, or on a worker thread (`_commit_roles_idle`, guarded by `_roles_lock`/`_roles_busy`) when the chat is paused. `emit` is now a pure enqueue onto `_emit_q`; ONE daemon thread (`_drain_emits`) owns `evaluate_js` — required once parallel/free modes emit from seat threads, and it makes event order FIFO across producers. `_conversation` validates cfg `mode` against `IMPLEMENTED_MODES` and builds the `moderator`/`until_done`/`turn_ceiling`/`spawn` state; `_continue` extends `turn_ceiling` for until-done chats (round cap otherwise) and clears stale `closing`/`next_speaker`. `precompute_config` warms `relay.codex_multi_agent_enabled()` off the bridge thread. [[ASK]] plumbing: `_AppIO.ask_human` emits a `question` event then blocks the CONVERSATION thread on a per-qid queue (polling `_stop_flag` + `abort`), under `Api._ask_lock` so simultaneous parallel-mode questions become consecutive modals; `Api.answer_question(qid, text)` is a pure bridge-thread enqueue (empty text = skip); `question_done` always follows, win or lose. Shared project context: `_conversation` calls `relay.project_brief` AFTER the `started` emit (so a slow read shows a status line, not a frozen window) and BEFORE the opener (compose_prompt prepends the preamble to the first prompt), on the worker thread `start` spawned — cfg key `brief`; `_continue` calls `brief_drift` and REPORTS changes without regenerating (regenerating there would hand a later `/clear`'d seat different context than its peers got); `open_session` patches `state["brief"] = read_project_context(path, meta)` beside the `store`/`log` patches, because `rehydrate` has no session_dir. **File/image viewing bridge**: `read_image(path, full)` / `list_workspace_files()` serve the UI's inline previews + Files rail — `confine_to_workspace` canonicalizes (realpath BEFORE the containment check, so junction/symlink escapes fail) and serves ONLY files beneath the LIVE workspace (`_conv["workspace"]`, or `_view_workspace` for reopened view-only chats — never a path rebuilt from the session id); image-extension allowlist + 15 MB cap, data URIs (file:// doesn't load in WebView2), 320px thumbnail bytes first, full res only for the lightbox; forbidden and missing paths return the IDENTICAL quiet error (no existence disclosure); `tests/test_bridge_files.py` guards all of it. **Skills & Connections bridge**: `get_skills`/`read_skill`/`save_skill`/`remove_skill` are SYNCHRONOUS bridge-thread methods (bounded file I/O, like `list_sessions`) and merge the per-provider rows into one row per skill name with `providers`/`missing`/`diverged`/`extras`; `get_mcp`/`add_mcp`/`remove_mcp` shell out, so they follow the `recheck_auth` shape — return `{"ok": True}` at once, do the work on a worker thread, answer with an `mcp_status` event. `read_text(path)` is the live-code-viewer sibling of `read_image`: same `_active_workspace` → `confine_to_workspace` → identical quiet "not available", `TEXT_MAX_BYTES` cap, NUL-sniff binary refusal (tests/test_activity.py). The opener emits in `_conversation`/`_continue` emit the ROW returned by `log(...)` (never a hand-built dict), so live and replayed Josh messages carry identical keys (`ts` etc.). Desktop "AI Chat" shortcut → `pythonw app.py` (window titles itself Alloy; the shortcut keeps its on-disk name). **Window/taskbar icon**: `main()` sets `SetCurrentProcessExplicitAppUserModelID("Alloy.AIChat")` BEFORE the window exists (else the taskbar groups under pythonw with Python's icon), and `_apply_window_icon` (events.shown) sends `WM_SETICON` small+big from `ai-chat.ico` — best-effort ctypes, never raises; the desktop AND pinned-taskbar shortcuts point at `alloy.ico,0` — a byte-identical copy `make_icon.py` writes beside `ai-chat.ico` — because Windows' icon cache keys on PATH and the old chat-bubbles icon stayed cached under the `ai-chat.ico` path through every refresh (`ie4uinit`, .lnk re-save); a fresh filename was the only reliable fix. If the icon ever goes stale again, point the .lnks at a new copy name rather than fighting the cache. |
| `ui/index.html` | Single-file UI (inline CSS/JS, local fonts only). A 224px chat-history rail lists saved sessions Claude-app-style: single-line rows (provider dots + ellipsized title; time/seats/view-only live in the tooltip) grouped under collapsible per-project headers — `session_summary` computes `project` via `relay.session_project` (basename of a CUSTOM working folder; the default in-session workspace ⇒ "" ⇒ the "No project" group), groups rank by their newest chat, collapse state persists best-effort in localStorage. Rows keep replay, active selection, dblclick-rename, two-step delete, and view-only legacy chats. The seat rail supports dynamic/duplicate seats with model + thinking pickers, rounds, working-folder picker, yolo toggle, and live thinking state; the seat-name heading is an editable input — the auto name ("Claude 2") is its *placeholder*, typed text becomes the seat's explicit label (`cfgFor` sends it as `label`, engine-side `assign_labels` takes it as-is and rejects duplicates), and `restoreSeats` writes a saved name into the box only when it differs from the auto placeholder so reopened auto-named seats keep renumbering; reopening restores original seat ids/models so events and captions remain truthful. No topic box: the first message typed into the chat bar starts the conversation (cfg key `opener`). After a run ends (`done` carries `can_continue`), the next non-`/` message calls `continue_chat`; messages starting `/` route to `api.command()`. Accounts live in a modal (`#acctModal`), opened by the sidebar-bottom `#acctBtn` button whose red badge counts seatable providers that are signed_out/not_installed; `renderAccounts` is registry-driven from `auth_status`. Roles are edited in a shared modal (`#roleModal`), opened from a slim per-card `.role-btn` showing the current role; role name + instructions live on the seat JS object (`seat.role`/`seat.roleInst` — `cfgFor`/`restoreSeats`/`roleApplied` all go through it, never through card inputs). Closing the modal commits only while un-seated; once a conversation exists (`setSeated`) the modal shows **Apply role change** instead — role edits cost a CLI turn, so they are never autosaved. `#seatList.locked` re-enables pointer events for `.role-btn` only. Message captions show the role from the stored row, never live seat config. The **Conversation** group holds the conversation-level orchestration controls, all locked once seated (`setSeated`) and restored truthfully when a chat is reopened: `#modeSel` (turn order — round_robin/speaker/moderator/parallel/free → cfg `mode`; picking moderator reveals `#modCtl`, a provider+model+thinking picker for the moderator itself → cfg `moderator`, defaults claude-haiku-4-5:low to match `build_moderator`, restored on reopen via `session_summary`'s `moderator` field with gemini slugs split back into family+level), the rounds stepper paired with `#untilDone` (checked ⇒ the same stepper becomes the safety-ceiling stepper via `syncRoundsCtl`, cfg `until_done`/`ceiling`), `#spawnSel` (helper budget) and `#teamSel` (team budget) → cfg `spawn: {tier1, max_helpers, max_teams}`. Typing indicators are a `Map` keyed by SEAT ID (`typingEls`, `showTyping(speakerId, provider, name)`/`hideTyping(seatId)`/`hideAllTyping()`) so parallel and free modes can show several seats thinking at once and duplicate-provider seats stay distinguishable; `addMsg` re-appends live indicators so they stay below new messages, and the round badge switches to `turn N/ceiling` for until-done runs. Message captions prefer the row's own `meta` (so helper rows read "helper for Claude"). Rail rows for spawned children show a ↳ prefix + "spawned by X" tooltip from `session_summary`'s `parent`. Composer extras: the box defaults to 96px min-height and `#sayGrip` (the grab bar above it) drags it taller — pointer-capture drag sets `sayMinH`, which `autoGrow` treats as the floor (the native corner grip also still works via the pointerup fallback); a 📎 button + paste handler queue attachments as base64 chips (`pendingAtt`) that ride with start/continue/interject and land in `<workspace>\attachments\` via `app.save_attachments` — message text gains `[Josh attached a file: <path>]` lines (`with_attachments`), so agents/transcript/replay all see them. Rails collapse from a `«` button ON each rail (`bindRail`); a collapsed rail is a 22px `.rail-reopen` strip whose click re-expands it — the `«` handler must stopPropagation or the same click bubbles to the strip listener and reopens instantly. Seat renaming has a ✎ button (`.rn`) that focuses the name input. Under the working-folder picker, `#projBrief` ("Share the folder's AI docs with every seat", cfg key `brief`) is locked by `setSeated` like the other conversation-level controls and restored from `session_summary`'s `brief` record, so a reopened chat truthfully shows whether its seats were ever given project context. **Skimmability**: `md()` peels TRAILING directives via `peelDirs` (a JS mirror of `peel_directives` — last-`[[`-anchored, end-only, ≤4) and renders them as `.dir-chip` pills ("asks Josh: …", "next: GPT", "wrap"); mid-reply mentions stay as raw text because they didn't fire; `==text==` renders as a seat-tinted `<mark>` (the preamble tells seats to mark at most one key line per reply). **Ask modal** (`#askModal` + `#askPill`): a `question` event opens a seat-colored modal — option chips answer instantly, `#askOther` is the free-text "Other" box (Enter sends), Skip answers empty; ✕/backdrop/Escape only HIDE it (the wait is engine-side) leaving the composer pill to reopen; `question_done` clears both unconditionally. **File & image viewing**: `addMsg` scans bodies (`findImageRefs` → `[{path, strict}]`: markdown image links + `[Josh attached a file: …]` + paths with ≥1 separator are STRICT (a load failure shows the quiet placeholder), a bare `name.png` in prose is LOOSE (a load failure removes the cell silently — a seat musing about "logo.png" must not leave a broken thumbnail), loose refs dedupe by basename so "saved as [x.png](C:\…\x.png)" yields ONE thumbnail. The drive letter must consume its own slash in that path regex — `(?:[A-Za-z]:)?` before `(?:[\w.\-]+[\\/])+` silently matched `C:\Users\x.png` starting at "Users", handing the bridge a relative path that resolved nowhere, i.e. every absolute path a seat reported — which is exactly how GPT reports the images it generates — rendered as "image no longer available") and renders `.img-thumb` strips via `fetchImage` (per-chat `imgCache`, cleared whenever the workspace boundary changes) — failures render a quiet `.img-missing` placeholder, never a broken tag; click opens `#lightbox` (full-res fetch). `#fileRail` (right side, same `bindRail` pattern) lists the working folder newest-first with thumbnails/type icons, click-to-preview, Open-in-OS; refreshes debounced on message events. Every `.msg` is `user-select: text` and carries a `.copy-btn` (clipboard API with execCommand fallback). **Skills & Connections** (`#skillModal`, `.modal.wide`, opened by `#skillBtn` above `#acctBtn`; add any new modal id to BOTH the `display:none` and `.show` selectors and to the one shared Escape listener): the Skills tab lists one row per skill NAME with a provider dot per CLI that has it (hollow = missing) and ⚠ when the copies diverge, an editor (name/description/body + per-provider checkboxes) whose **Save reconciles the ticked set** — ticking installs, unticking removes — and an `#skSyncBtn` "Install to GPT and Gemini" that just ticks the missing boxes and calls the same `saveSkill()`, so there is no second code path. `#skExtras` states the sidecar count BEFORE the click, since those files travel with it. The Connections tab is per-provider on purpose (the backends differ, and a merged list would invite a "sync everywhere" action that is actively wrong); the stdio Add button is a two-step arm showing the exact command that will run locally, disarmed whenever the command changes, and skipped entirely for http/sse where nothing executes. **Timestamps**: `addMsg(..., ts)` renders the row's `ts` as a right-aligned HH:MM (`.mtime`, full stamp in the tooltip); old rows without `ts` show nothing. **Live activity**: `activity` events append `.act-line`s (last ~6) to the seat's typing indicator (`typingActivity`); when the message lands, the ROW's persisted `activity` renders as a collapsed `.think-block` `<details>` ("X worked through N steps") above the body — replay included; everything escaped (command lines are arbitrary CLI text). **Live code viewer** (`#codePane` inside `#fileRail`): `kind:"edit"` activity marks the rail row `.editing` (provider-color border + ✎, ~6s expiry in the `editing` map) and auto-opens/refreshes the pane (`openCode`/`fetchCode` via `api.read_text`) — prev snapshot line-diffed (common prefix/suffix), changed band highlighted in the editing seat's color + scrolled into view, ~700ms poll only while a seat is mid-turn (catches Gemini, which has no stream) — the re-render guard compares CONTENT, not mtime, because the poll and the edit event race for the same write and a second render of identical text erases the highlight the first one just drew (caught only by driving the real UI, 2026-08-17), "follow" checkbox pins a file, non-image rail rows open here (OS-open fallback for binary). `resetEditing()` runs beside every `imgCache.clear()` — workspace boundary changes clear the map and close the pane. `scheduleFilesRefresh(ms)` takes a delay; edit activity uses 300ms. **Alloy branding** (BRANDING.md): title/h1 "Alloy", wordmark = fixed trefoil (data-URI PNG from `branding/trefoil_v2.py`, never encodes seat count), empty-state h2 "Different metals. One alloy." with a DYNAMIC roster cluster (`renderEmptyRoster` — one dot per enabled seat, real provider colors), `--alloy` #F4B942 = app chrome (wordmark h1, Send, round badge, focus rings, checkboxes), `--josh` #C9B896 warm bone; provider colors stay participants-only. |
| `launcher.ps1` | Console launcher (prompts for topic). ASCII-only on purpose. |
| `sessions/` | One folder per conversation: `transcript.md` (human log), `messages.jsonl` (UI replay), `meta.json` (resumable state, **v2**), optional default `workspace/`, `project-context.md` (the exact shared-context text the seats were given, when the chat used a custom working folder), and `say.txt`. Old transcript-only folders remain listable as legacy/view-only; v1 metas stay continuable. Spawned teams (tier 3) are ordinary sessions in here too — child meta carries `parent: {id, seat, label}`, parent meta lists `children` (hints only: a child can be deleted). |
| `tests/` | Token-free test suites, each a runnable script (`python tests/test_loop.py`): `test_loop` (shared loop), `test_scheduler` (meta v2 + resume), `test_modes` (directives, speaker, moderator), `test_until_done`, `test_parallel`, `test_free`, `test_spawn_tier1/_helpers/_teams`, `test_brief` (shared project context), `test_ask` ([[ASK]] questions to Josh), `test_bridge_files` (file/image viewing bridge: workspace confinement incl. `..`/absolute/junction escapes, MIME + size cap, thumbnails), `test_capabilities` (who-can-do-what routing), `test_skills` (skill authoring/sync + MCP management), `test_activity` (streaming runner via `python -c` children, adapter activity mapping, sink dedupe/cap/confinement, loop + persistence, read_text bridge), `test_app_headless` (real `app.Api` + fake window). 434 tests, no CLI calls, no tokens — `test_loop.py` exports the shared `FakeAgent`/`RecordingIO`/`build_state` helpers the others import. |
| `make_icon.py` / `ai-chat.ico` | Icon generator (Pillow) and the generated icon: the **Alloy trefoil** (BRANDING.md, approved 2026-08-16) — geometry lives in `branding/trefoil_v2.py` (parametric curve, crossings solved numerically, depth-sorted over-arcs; the over-arc redraw must outrun its erase band by more than the erase cap radius or a notch punches the glow), every .ico size regenerates from that one function, <32px uses `small_mark`'s heavier weights. `branding/trefoil-v2-comparison.png` is the approved proof sheet. Brand rename is UI-surface-only: repo/CLI/skills stay `ai-chat` on disk. |

Also installed elsewhere: `ai-chat` skills in `~\.claude\skills\ai-chat\` and
`~\.codex\skills\ai-chat\` (so either AI can run conversations on request).
If paths here change, update those skills + the desktop shortcut + `~\.local\bin\ai-chat.cmd`.

## CLI knobs (relay.py)

`ai-chat "topic" --turns N --agents claude,gpt,gemini --start X
--permission read_only|ask|auto|full (`--yolo` is an alias for `full`)
--claude-model <id> --claude-effort low|medium|high|xhigh|max
--gpt-model <id> --gpt-effort low|…|ultra --gemini-model <agy slug>
--gemini-effort low|medium|high (normally baked into the slug)`
Claude ids (all verified on this account): claude-fable-5, claude-opus-5,
claude-opus-4-8, claude-sonnet-5, claude-haiku-4-5 (aliases opus/sonnet/haiku ok).
Defaults: all three agents, 10 rounds, Opus 5 / gpt-5.6-sol(high) / gemini-3.7-flash-high.
The app reads live model lists: GPT from `~\.codex\models_cache.json` (+ defaults
from `config.toml`), Gemini from `agy models`; Claude list is pinned in app.py.

**Working folder + shared project context**: `--workspace PATH` runs the seats
in an existing project instead of a fresh scratch dir (the app has had a folder
picker all along; the CLI had no equivalent, which meant the feature had no
cheap end-to-end test path). A custom folder turns on the shared project
context; `--no-brief` skips it (app: the "Share the folder's AI docs with every
seat" checkbox, cfg key `brief`). Cheap e2e:
`python relay.py "what is this project?" --turns 1 --workspace C:\ai-chat
--agents claude:claude-haiku-4-5:low,gpt --gpt-effort low`.

**Orchestration** (ORCHESTRATION_DESIGN.md — all conversation-level, mirrored
in the app's Conversation controls): `--mode round-robin|speaker|moderator|
supervisor|parallel|free` (speaker = seats end replies with `[[NEXT: seat]]`; moderator =
a stateless cheap side call picks each turn, `--moderator provider[:model
[:effort]]`, default claude:claude-haiku-4-5:low, can answer DONE; parallel =
simultaneous barrier rounds; free = seats reply whenever messages arrive,
FREE_MAX_LEAD throttle). `--until-done --ceiling N` = no round cap, wrap-driven
end with a hard turn ceiling (default 60, `/ceiling N` mid-run). Spawning:
tier 1 native CLI subagents on by default (`--no-native-subagents` to hide);
`--spawn-helpers N` = seats may play `[[SPAWN: provider[:model[:effort]] |
task]]` for one-shot helpers (requester-only results); `--spawn-teams N` =
`[[TEAM: seats | rounds=N mode=m | task]]` spawns a whole child session
(depth 1, ≤ CHILD_ROUNDS rounds, reports back, replayable from the rail).
Asking Josh: on by default (`--no-ask` to disable) — a seat may END a reply
with `[[ASK: question | option A | option B]]`; the conversation PAUSES on
`LoopIO.ask_human` until Josh answers (app: modal with option buttons + an
Other box + Skip; CLI: console prompt, number picks an option), the answer
fans out to every seat as a real Josh row, and an unanswered/aborted
question becomes a relay note (never a forged answer). Child teams have
ask off. In dynamic/parallel/free modes the rounds knob is a budget of
turns × seats.

Supervisor is a distinct mode, not a moderator alias. `build_supervisor` makes
one stateless planning call; `supervisor_roster_block` derives the roster from
the live adapters' `capability_note()` plus real write ability;
`plan_workstreams` parses stacked TASK directives, then runs capability gate →
overlap serialization → private-queue dispatch through the parallel loop.
Planner failure or an empty/unparseable plan degrades visibly to ordinary
parallel conversation and never invents tasks. Active workers are radio-silent
until settlement; a worker's trailing `[[WRAP]]` closes only its task, never the
global conversation (caught by the first real-CLI smoke test on 2026-08-17).
At the parallel-round barrier, `replan_failed_workstreams` gives each
filesystem-failed task exactly one stateless repair attempt. The replacement
must reuse the task id (so downstream DAG edges remain valid), keeps the
original dependencies, and is never called while a worker thread is active.

**The Supervisor is a rolling manager, not a one-shot planner** (added
2026-08-18 — Josh asked twice to see it "react and keep up ... a real manager
and decision maker"). At the same barrier, AFTER repair, `supervise_next_wave`
fires whenever `plan_drained(state)` — every task settled, nothing pending/
active/blocked. It re-reads the objective record (`wave_report`: verified
filesystem results FIRST, then each worker's own closing report, explicitly
labelled a claim — `settle_workstream` keeps a `WORKSTREAM_REPORT_MAX`
excerpt on the task, which for a no-files research task is the only account
that exists) and answers with either `[[DONE: verdict]]` (the run ends
`wrapped`, on the manager's word rather than the round cap) or the next wave
of `[[TASK: …]]` directives, appended to the same plan and dispatched through
`assign_workstreams`. `parse_supervisor_verdict` opts DONE in exactly the way
TASK is opted in — it is NOT in `KNOWN_DIRECTIVES`, so an ordinary seat
playing it stays visibly unknown instead of quietly gaining authority to close
the conversation. Bounded by `SUPERVISOR_MAX_WAVES` (6) and announced once on
exhaustion; every failure path (dead side call, unparseable reply, prose with
neither verdict nor tasks) returns `idle`, leaves the plan untouched, and
degrades to ordinary parallel conversation. A wave that reuses an existing
task id has that task DROPPED and said so out loud — settled history keeps
its ids so downstream deps stay valid. The wave trace carries the WHOLE task
list (the UI's task map renders whatever it carries). Every trace entry carries the WAVE it belongs to (`supervisor_wave_index`, engine-stamped): the UI must not infer waves by cutting on `plan_created`, because `SUPERVISOR_TRACE_MAX` truncation would renumber them exactly on the long runs that need numbers. Two terminal states, never one: `goal_accepted` (the manager closed it) vs `goal_unresolved` (waves spent, or `note_unfinished_supervision` at a round-cap exit) - conflating them is how a supervised run that merely timed out reads as finished. Public trace types:
`work_reviewed` / `plan_created` / `goal_accepted`. The control log renders
them as WAVES (`supervisorWaves` cuts on `plan_created`, each wave a
collapsible container) so the plan->work->review->plan cycle is legible;
`goal_accepted` is lifted out of the stream into a verdict card, because
"the manager decided this is finished" must never read like "the round
cap ran out"; a `work_reviewed` entry that is the NEWEST entry pulses,
since the trace is written when the side call STARTS and that is exactly
what "still deliberating" means. Live-verified 2026-08-18 (two haiku
seats, real CLIs): plan_started -> plan_created -> task_assigned x2 ->
verification_review x2 -> work_reviewed -> goal_accepted, both files on
disk, run ended in round 1 of 3 on the manager's verdict. `relay.supervisor_status(meta)` derives the compact rail badge (accepted / working / unresolved / settled / planning) ENGINE-SIDE, and its precedence is deliberate: open work outranks a past `goal_unresolved`, because a chat that hit the turn limit mid-job is resumable and a row reading "No verdict" would say the opposite of what continuing does. `supervisor_goal` + `supervisor_waves` persist in meta, so a
reopened chat resumes mid-job instead of re-planning from scratch.

**Duplicate seats** (same provider more than once, e.g. Claude vs Claude): each
`--agents` token is `provider[:model[:effort]][=label]` — e.g.
`--agents claude:claude-opus-4-8:high,claude:claude-haiku-4-5:low` or
`--agents "claude=Optimist,claude=Skeptic" --claude-model sonnet`. Omitted
model/effort falls back to the provider-wide flags (old syntax unchanged).
Auto labels ordinal-suffix ("Claude", "Claude 2"); `--start` takes a 1-based
slot number, a label ("claude 2"), or a provider (its first seat). The preamble
adds a same-model note so instances don't mistake each other for echoes; each
codex seat writes its own `.codex-last-message-<uid>.txt`. The app UI has an
"+ Add seat" row (engine config = `seats` list; legacy `agents` dict still
accepted by `app.Api._conversation`).

**Per-seat roles** (`ROLES_DESIGN.md` — specialization by INSTRUCTION, never
capability; a role changes what a seat is told, not what it can do):
`--role "<seat>=<public name>"` and `--role-instructions "<seat>=<private text>"`,
both repeatable, e.g.
`--agents claude,gpt --role "claude=Researcher" --role-instructions "claude=Cite every claim."`
`<seat>` resolves through `match_seats` — the SAME resolver `/clear` and
`/compact` use, so it is label-first ("claude 2"), falling back to a provider
name only when no seat carries that label. Unmatched targets are a hard error.
Every seat sees the one-line roster of public names; only the owner sees its own
instructions. In the app, each seat card's role button opens the shared role
modal: edits there are free before a chat starts; once seated, only the modal's
**Apply role change** button (one CLI turn) changes anything.

## Hard-won gotchas (do not relearn these)

- **Workstreams are strict radio silence**: while a seat owns an active task,
  its draft reply neither reaches nor receives the main rail or another worker.
  Only the filesystem-verified settlement summary crosses that boundary. Task
  file claims are literal workspace-relative paths in v1 (no globs, absolute
  paths, or `..`); overlaps auto-serialize into dependencies. `TASK` is parsed
  only through the Supervisor planner's `parse_task_directives`, not global
  `KNOWN_DIRECTIVES`; an ordinary seat playing TASK remains visibly unknown
  rather than gaining orchestration authority or disappearing as a no-op.

- **npm shims**: `codex`/`gemini` are `.cmd` shims — CreateProcess can't run them
  and `cmd /c` TRUNCATES MULTI-LINE ARGS at the first newline. relay resolves the
  shim to `node <script>.js` and runs node directly.
- **claude CLI**: in print mode, bind the prompt directly to `-p` (the
  documented `claude -p "prompt" --resume <id>` shape);
  use `--allowedTools=a,b` equals-form (variadic flag swallows the prompt);
  strip `CLAUDE*`/`ANTHROPIC*` env vars when nesting under a Claude session;
  `-p --resume` returns a NEW session_id each call — always re-capture.
- **codex**: `codex exec resume <id>` takes no `--sandbox`/`--cd` — use `-c`
  overrides + process cwd. Reply read via `-o <file>`; session id from `--json`
  events (thread_id). Sandbox: `-c sandbox_mode="workspace-write"` + network flag.
- **agy (Gemini)**: piped TEXT output is broken on Windows (TTY-only renderer);
  `--output-format json` works. `--conversation <id>` = memory. Thinking level is
  baked into model slugs (`gemini-3.7-flash-high`); UI splits family+level.
  Installed at `%LOCALAPPDATA%\agy\bin\agy.exe` (may be off PATH in old shells).
- **pywebview**: (1) NEVER store the Window/Queue/Event as PUBLIC attrs on the
  js_api object — the bridge recursively walks public attrs at page load and
  deadlocks; underscore-prefix internal state. (2) `subprocess.run` directly on a
  js-bridge call thread deadlocks — precompute on a normal thread (threads
  *spawned* by bridge calls are fine). (3) `.ps1` files must stay ASCII or have a
  BOM (PowerShell 5.1 reads BOM-less files as ANSI).
- All agent subprocesses need `stdin=subprocess.DEVNULL`.
- **Sign-in consoles are the OPPOSITE of agent subprocesses**: visible
  `CREATE_NEW_CONSOLE`, no DEVNULL stdin, no capture (the console owns the TTY
  for the browser-OAuth flow) — `["cmd","/c"]+argv` → `wait()` → re-probe →
  emit. Everything else (probes, logout, agents) stays hidden
  (`CREATE_NO_WINDOW` + DEVNULL). `get_auth_status` runs on the js-bridge
  thread → it must stay a pure cache read (subprocess there deadlocks).
- **Gemini auth + ToS (researched + live-verified 2026-08-17)**: agy ≥1.1.13
  authenticates via the WINDOWS KEYRING ("ChainedAuth: authenticated via
  keyring" in `~/.gemini/antigravity-cli/cli.log`) — `oauth_creds.json` can
  carry a stale/expired `expiry_date` while login still works, so never
  conclude signed-out from that file's contents; `agy models` is a free live
  auth check. True sign-in flow (only when actually signed out): run `agy`
  interactively → "1. Google OAuth" → browser → paste the code back. ToS:
  scripted/headless use of the OFFICIAL CLI is documented-supported
  (antigravity.google/docs/cli/headless); what got accounts banned (Feb 2026
  wave) was third-party tools piggybacking Antigravity OAuth to hit Google's
  backend directly — the terms name OpenClaw as the example breach. Keep
  seats on the real agy binary, gemini-* slugs, human-scale volume; never
  let any other tool touch `~/.gemini` creds. **Installing agy: the official
  docs' one-liner cannot work.** `install.cmd` greps its OWN `CMDCMDLINE` for
  `& | ; < > ^` and aborts with "Fatal: Illegal shell characters detected in
  command line arguments", so the documented
  `curl … -o install.cmd && install.cmd && del install.cmd` fails before
  doing anything. Download and run as two steps (PowerShell's `;` is fine —
  it never reaches the cmd child); that two-step form is what the Accounts
  panel's Gemini `install_hint` now shows. The panel distinguishes
  `not_installed` (no CLI — nothing to sign into, no Sign in button) from
  `signed_out`; a seat card must not label the first one "sign-in needed".
- Testing auth states without touching real logins: stub `claude.cmd`/
  `codex.cmd` scripts in a temp dir prepended to PATH (keep System32 for
  cmd.exe/ping), gemini/codex-fallback via the probes' injectable `home=` param
  (see scratchpad test_auth_probes.py pattern from 2026-08-16).
- **The workspace contract**: `workspace` is EITHER `sessions/<name>/workspace/`
  (default) OR a folder Josh picked in the UI (`cfg["workspace"]`, app.py). It is
  the only writable dir an adapter may assume. Never navigate out of it — a `..`
  hop in `CodexAgent._lastmsg` assumed the default layout, so a picked folder of
  `C:\ai-chat` sent codex's `-o` file to `C:\` (denied), codex exited 0 writing
  nothing, and EVERY GPT turn silently became "(no reply)" for a whole
  conversation. Fixed 2026-08-16; the tell was a session dir with no `workspace/`.
- **Never forge a turn.** A content-free reply must NOT be relayed to the other
  seats. `Agent.turn` raises when a CLI exits 0 with an empty parse (source), and
  both `_rounds` copies skip the round + restore `queued` instead of inventing
  "(no reply)" (sink). A fake turn hid the bug above from three agents for six
  rounds.
- **Wrap token**: `wrap_called(reply)` fires only if the reply ENDS with
  `[[WRAP]]` (`rstrip().endswith`). A bare substring check let a seat end the
  conversation by merely discussing the token; requiring the token to be the
  entire last line overcorrected — seats play it by closing a sentence
  ("Good place to stop. [[WRAP]]"), so that form silently never fired and the
  mechanic looked implemented while every conversation ran to the round cap.
  Ending-on-the-token accepts both real forms; mentions have text after them,
  and quoted/code-span mentions end on the closing mark. The preamble wording
  must stay in sync.
- **Dead session ids fail loudly — but must not fail repeatedly.** Verified
  2026-08-16 by resuming a bogus uuid: claude exits 1 "No conversation found
  with session ID: …", codex exits 1 "thread/resume failed: no rollout found
  for thread id …". Good news — neither silently starts a fresh session, so
  nothing forges continuity. But that failure is *permanent*, and the loops
  treated it as transient: retry + hit the same wall next round = 2N calls and
  N identical errors for one problem. `fatal_seat_error(agent, exc)` now
  classifies it, both loops skip the retry, emit one `agent_error`
  (`fatal: True`), persist it, and stop. Recovery is OFFERED, never performed:
  `/clear <seat>` gives that seat a fresh session with its still-pending queue
  and subsequent messages, not its lost earlier context. Never auto-reseed —
  that would claim memory the agent doesn't have. (agy/gemini's behaviour here
  is still untested; agy was not installed on this machine.)
- **One loop now** (was: two duplicated loops, extracted 2026-08-16):
  `relay.run_rounds(state, io)` is THE loop; `LoopIO` is the front-end seam
  (`emit`/`drain_human`/`should_stop`/`on_turn_boundary`). The CLI uses `CLIIO`
  (stdin + say.txt in, ANSI status out; message rows print via make_log's
  echo), the app uses `_AppIO` (module-level on purpose — public attrs on the
  js_api object deadlock the pywebview bridge walk). Slash commands live in
  `relay.dispatch_command`/`seat_command`, shared by the loop and the app's
  idle-path worker. Anything loop-shaped goes in `run_rounds` ONCE; front ends
  own only setup and epilogue (CLI: ended footer; app: paused footer + `done`).
  Unified in the extraction: `/turns` clamps to the current round everywhere,
  `/help` exists in the CLI, the opener nudge is `rnd == 1`-guarded, and the
  failed-twice/empty-reply skip paths save state in the app too (they didn't).
  `run_rounds(state, LoopIO())` with scripted fake agents = token-free loop
  tests — see `tests/` (191 tests, all suites runnable as plain scripts).
- **Each CLI reads only its OWN project doc, so context must ride the
  preamble.** Every seat runs with `cwd = workspace`, and there each CLI
  auto-loads a different file: claude → `CLAUDE.md`, codex → `AGENTS.md`
  (config keys `project_doc_max_bytes`, `project_doc_fallback_filenames`),
  agy → `AGENTS.md`/`GEMINI.md` (all three verified by grepping the shipped
  binaries, 2026-08-16). Point a chat at a repo holding only `CLAUDE.md` and
  the Claude seat arrives with the whole project in context while the others
  arrive blind — invisible in the transcript, and the worst failure mode
  available for a multi-AI debate (one seat authoritative, two guessing).
  `AI-CHAT.md` is loaded by NO CLI, so a file alone fixes nothing; the context
  has to be injected as text, and `preamble()` is the only durable channel
  (`introduced[i]` resets on `/clear` and `/compact`, so `pending[]` would
  evaporate — same rule as ROLES_DESIGN.md:28). Fixing it does NOT require
  paraphrasing: the missing seats are missing exactly the bytes the Claude
  seat already gets, so docs under `BRIEF_MAX` are quoted VERBATIM (free,
  lossless, deterministic, token-free to test) and only an oversized doc set
  gets a synthesized `AI-CHAT.md`, cached by source sha256 so it costs one
  call per doc change rather than one per chat. Corollaries that are easy to
  get wrong: fingerprint on sha256 ONLY (mtime moves on `git checkout` with
  identical bytes → churn); never re-scan on resume (`read_project_context`
  replays the recorded text, `brief_drift` reports changes — regenerating
  would hand a later `/clear`'d seat different context than its peers got);
  spawned teams INHERIT the parent's record; and the preamble's old "you share
  a scratch workspace ... read/write files there if useful" line is a lie for
  a real repo, where non-yolo claude holds `Write`/`Edit` and codex holds
  `workspace-write`.
- **Windows caps a command line at ~32,767 chars and every adapter passes the
  prompt as ONE argv element** (claude/codex: last positional; agy: `-p`), with
  npm shims expanded to `node <long path>.js` eating more. Preamble growth is
  therefore genuinely bounded — a fat context block plus a parallel/free-mode
  backlog is how a seat starts failing every round with an unexplained
  `OSError` from `subprocess.run` (transient path → retry → "failed twice;
  skipping"). `BRIEF_MAX` is small on purpose; keep it that way.
- **Commit-consume is the queue invariant** (every mode): `compose_prompt`
  snapshots a backlog WITHOUT clearing it; `commit_reply` deletes exactly the
  consumed prefix + fans out + saves. Failures restore nothing because nothing
  was removed; `store.save` is therefore valid at every instant. Writing
  `pending[i] = queued + pending[i]` again = re-introducing the old bug class.
- **Threading contract** (parallel/free): lock order `state["lock"]` →
  `store._lock` → print lock, never reversed. ONE thread per Agent object,
  ever (session_id capture + codex `-o` files are single-owner) — which is
  why /clear//compact defer to the round boundary (parallel) or run on the
  owning seat's thread via its inbox (free), and why free mode doesn't drain
  staged roles mid-run. The app calls `evaluate_js` from exactly ONE emitter
  thread (`Api._emit_q`); `Api.emit` only enqueues; tests flush with
  `_emit_q.join()`.
- **Directive parsing**: one grammar (`peel_directives`) for [[WRAP]]/
  [[NEXT:]]/[[SPAWN:]]/[[TEAM:]] — end-anchored per the wrap-token bug
  history, and each peel anchors at the LAST `[[` via rfind: a leftmost
  `re.search` with a lazy dot collapses a stacked tail ("… [[NEXT: A]]
  [[WRAP]]") into ONE directive with a garbage argument. Directives are
  relayed/recorded verbatim, never stripped. `wrap_called` is a one-liner
  over the shared parser so the grammars can't drift.
- **`_atomic_write` retries `os.replace`**: on Windows a concurrent READER
  without FILE_SHARE_DELETE (an editor with meta.json open, the indexer, a
  test polling the file) blocks the rename with a transient PermissionError.
  Without the retry, a mid-conversation save can crash a commit.
- **A skill is a FOLDER, and its sidecars are load-bearing.** All three CLIs
  read the same `SKILL.md` (YAML frontmatter `name` + `description`, then
  markdown) from `<skills_dir>/<name>/`, which is why one file installs
  verbatim everywhere — but 5 of the 6 real skills on this machine carry
  `scripts/`, `references/` or `assets/` next to it AND link to them, so a
  SKILL.md-only copy installs a skill whose every reference dangles.
  `write_skill(..., source=<provider>)` therefore copies the whole tree into
  `<name>.alloy-tmp<pid>` and swaps it in (old dir moved aside, restored on
  failure) — never a partially-copied folder visible to a CLI, symlinks
  refused, capped by `SKILL_TREE_MAX_FILES/BYTES`.
  More measured facts, all easy to get wrong: **5 of 6 use lowercase
  `skill.md`** (hence `skill_file_in()`, a case-insensitive lookup — Windows
  hides this bug, another OS would not); one carries a **UTF-8 BOM** that
  makes its description parse as `---` unless stripped; skill dirs also hold
  dotted folders (codex's `.system`) that must be skipped. Divergence is
  detected by **sha256 of NORMALIZED text** (BOM stripped, CRLF folded) —
  the two `ai-chat` copies really are different, and their mtimes are 1.1 ms
  apart, so mtime can neither detect nor arbitrate it (same rule as the
  project-context fingerprint).
- **`claude mcp add` defaults to `-s local` — i.e. the CURRENT DIRECTORY.**
  Without `-s user` a server added from the app exists only for CLIs run in
  the app's cwd, so seats would never see it and the panel would look like
  it silently did nothing. `codex mcp list --json` is the parseable form (its
  plain output is a column table, and splitting it on `:` eats the `C:\`
  drive letter); the target lives under `transport`, not at top level.
  codex has **no SSE** transport — the picker hides it rather than failing at
  exec. Mutating anything MUST call `invalidate_mcp_cache()`, or the
  `connectors` switch keeps granting the stale prefix set until restart.
- **The seats have DIFFERENT capabilities, and only the preamble can say so.**
  Verified live 2026-08-17 (do not re-derive from what these products
  "are" — I twice reasoned they were all just coding agents and was twice
  wrong): **codex generates images** (feature flag `image_generation`,
  stable/true; `codex exec` in the relay's own NON-yolo sandbox produced a
  photorealistic PNG straight into the workspace); **agy generates images**
  too (`generate_image`) but IGNORES the process cwd for file writes: it
  drops them in `GEMINI_BRAIN`
  (`~/.gemini/antigravity-cli/brain/<conversation-id>/`) and then reports
  them as being "in the current working directory" (tested sandboxed AND
  yolo), so `GeminiAgent.harvest_images()` copies each turn's new images
  into the workspace from `parse` — after `session_id` is set, because a
  first turn only learns the id there; `before_run` snapshots so a resumed
  conversation never re-copies old ones; it never overwrites and never
  raises. The RELAY does the copy, which is why it works in sandbox mode
  where agy's own `run_command` cannot;
  **claude has no image tool at all** (checked its full
  tool list from a live `system/init` event). Hence `Agent.capability_note()`
  — same hard contract as `native_spawn_note()`: it states what build_cmd
  actually grants on THIS install (`codex_features()` caches
  `codex features list`; any probe failure ⇒ {} ⇒ claim nothing), and Gemini
  claims images only because the harvest above makes the claim true.
  `preamble` renders the
  notes as a "What each participant can actually do" block plus the
  hand-it-over rule, and only when ≥2 seats declare something (a solo chat
  and FakeAgent-based tests keep the old preamble byte-for-byte). Without
  it, seats assume they must attempt everything themselves: asked for an
  image in a Claude+GPT chat, Claude drew one in code while GPT — holding a
  real image tool — waited its turn, because names are not a capability map
  and brand knowledge guesses wrong.
- **`--allowedTools` is an AUTO-APPROVE list, not a whitelist** (claude;
  verified live 2026-08-17). A non-yolo seat ran `Bash`, loaded a `Skill`
  and used `ToolSearch` with none of them listed; only MCP calls came back
  "you haven't granted it yet" and appeared in the result's
  `permission_denials`. So the ONE capability that list actually gates is
  MCP — hence `Agent.connectors` (CLI `--connectors`, app checkbox, meta
  key, off by default): it appends `mcp__<server>` prefixes from
  `claude_mcp_prefixes()` (cached `claude mcp list`; name → prefix turns
  `.`/`:`/space into `_` and KEEPS hyphens: "plugin:superpowers-chrome:chrome"
  → `mcp__plugin_superpowers-chrome_chrome`). Naming the server grants all
  its tools — also verified. It is deliberately NOT tied to yolo: yolo is
  about the workspace, connectors reach Josh's real Gmail/Drive/Calendar/
  M365/ERP with seats running unattended for many turns.
- **Both GPT and Claude already build real Office/PDF documents** in
  non-yolo mode (verified 2026-08-17: valid OOXML + %PDF on disk). codex
  uses its bundled `documents`/`pdf`/`spreadsheets`/`presentations` plugins;
  claude uses the `document-skills` plugin (docx/pdf/pptx/xlsx) plus pip.
  Nothing in the relay needed changing for this — but the capability notes
  must SAY so, or seats won't route document work to anyone.
- **A failed `result` object is not a reply.** claude's stream-json puts the
  CLI's own error sentence in the SAME `result` field a successful turn uses
  ("API Error: 529 overloaded"), so a parse that only reads `result` will
  relay an error to every other seat as if Claude had said it — the
  never-forge-a-turn rule, one level deeper than "(no reply)".
  `ClaudeAgent.parse` returns "" when `is_error` or `subtype != success`, so
  it takes the retry-then-skip path instead.
- **Structured-output CLIs need `describe_failure`, not a raw tail.** The
  generic `(stderr or stdout)[-500:]` is JSON soup for these adapters: a real
  failure reached the app as `Claude exited 1: n":{"ephemeral_1h_input_tokens":0,…`
  — every legible word truncated away, and it read as a path bug for the
  second time in this repo's history (cf. the codex timeout below). Adapters
  extract the sentence; keep it short, the loops excerpt again.
- **Opus streams thinking VOLUME, not thinking CONTENT** (verified 2026-08-17:
  `claude-opus-4-8 --effort high` emitted `system/thinking_tokens` events and
  ZERO `thinking` blocks; haiku emits both). So Claude seats narrate tools
  plus a `kind:"progress"` counter, not reasoning prose. Progress acts are
  live-only by contract: the sink emits them, never persists them, and never
  spends `ACTIVITY_MAX` budget on them (a finished reply's activity list must
  read as what the seat DID, not a stopwatch), and the UI updates ONE line in
  place instead of appending.
- **Activity narration is best-effort and must NEVER fail a turn.** The
  `activity(line)` hooks and the `on_activity` callback are wrapped in
  swallow-everything try/except on purpose; unknown JSON shapes return `()`
  (both CLIs' vocabularies drift between versions). The streaming runner's
  stderr-drain THREAD is mandatory — a child that fills the stderr pipe
  deadlocks on Windows if only stdout is read. And claude's `stream-json`
  print mode hard-requires `--verbose` — dropping the flag makes every
  claude turn exit 1 instantly.
- **A fake Agent that overrides `turn` must accept `on_activity=None`** —
  the loops always pass the kwarg, and a positional-only fake makes every
  turn TypeError → "failed twice; skipping" with nothing obviously wrong.
  Same trap for test wrappers around `.turn` (test_parallel's jitter,
  test_app_headless's spy lambda). tests/test_loop.py's shared FakeAgent
  scripts activity as `(reply, [acts])` tuples.
- **Spawn rules**: helpers/teams deliver results ONLY through
  `SpawnManager.drain_into_pending` at loop boundaries (helper threads never
  touch pending); every refusal/failure becomes a note in the requester's
  queue (never silent, never forged, never auto-retried); in-flight side-work
  at a crash is declared lost on the next run, never silently re-run. Teams
  are normal sessions (child meta `parent`, parent meta `children` — hints,
  a child may be deleted); depth is hard 1 via the child's zeroed spawn
  policy. `native_spawn_note()` lives next to build_cmd so the preamble can
  never promise a capability the flags don't grant.

## Testing

**Token-free first**: `tests/` holds 449 tests across 26 suites, each suite a plain script
(`python tests/test_loop.py` etc.) — FakeAgents drive the REAL loop via
`run_rounds(state, LoopIO())`; `test_app_headless.py` runs the real `app.Api`
against a fake window (flush the async emitter with `api._emit_q.join()`
before reading captured events); parallel/free suites use gated/sleeping
fakes for deterministic concurrency. Run the loop suites after ANY loop or
scheduler change — they cost nothing.

Cheap end-to-end: `python relay.py "test" --turns 1 --claude-model haiku
--claude-effort low --gemini-model gemini-3.7-flash-low --gpt-effort low`.
Duplicate seats cheap: `python relay.py "test" --turns 1
--agents claude:claude-haiku-4-5:low,claude:claude-haiku-4-5:low`.
Cheap per-mode: add `--mode speaker|moderator|parallel|free` to either;
until-done: `--until-done --ceiling 6`; spawning: `--spawn-helpers 1` (or
`--spawn-teams 1`) plus an opener that asks a seat to play the directive;
asking: an opener telling a seat to END its reply with the ASK directive —
answer at the console prompt, or drop the answer into the session's say.txt
(verified live 2026-08-16: haiku played it, say.txt "1" resolved to the
first option, GPT's turn carried the answer).
App engine headless: instantiate `app.Api` with a fake `_window` whose
`evaluate_js` captures events, call `_conversation(cfg)` directly — cfg uses
`"seats": [{id, provider, enabled, model, effort}, …]` (legacy `"agents"` dict
also still works). Events carry `speaker`=seat id + `provider` + `name`.
Cfg takes optional `"opener"` (seed message: logged as a Josh round-0 message
and queued to all seats as "Josh (human) opens the conversation: …") and/or
optional `"topic"` (legacy; `preamble` omits its `Topic:` line when empty).
UI without spending tokens: stub `get_config` and probe with `evaluate_js`.
Auth checks (no tokens): `python -c "import json,relay; print(json.dumps(relay.probe_all(), indent=1))"`
— or per-CLI: `claude auth status --json` · `codex login status` · gemini =
file check (`~\.gemini\oauth_creds.json`). Re-auth: the app's Accounts panel
(Sign in button), or `claude auth login`, `codex login`, run `agy` once
interactively.
