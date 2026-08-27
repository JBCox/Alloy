# AI Chat — multi-AI conversation relay

Claude (Claude Code CLI, Max), GPT (OpenAI Codex CLI, ChatGPT Pro), Gemini
(Google Antigravity CLI `agy`, free Google login) and Ox (OpenCode CLI on
OpenCode Zen's free models, no account at all) hold autonomous conversations
— **or one of them works alone, with Alloy as its harness** (§ Solo seat).
**No API keys anywhere** — every agent authenticates through its official CLI's
account login. Built 2026-08-16. Owner: Josh.

## Components

| File | Role |
|------|------|
| `outcome.py` | Standalone session-outcome recorder. `run_rounds` is a thin wrapper over `_run_rounds` and writes `outcome.json` in `finally`, once for every mode/front end and exit path. The record keeps `hard_facts`, optional `human_feedback`, and reserved `model_eval` separate; `write_outcome` preserves feedback across rebuilds and `set_feedback` is the app's only validated write path. |
| `webhook.py` | Local webhook trigger (2026-08-25) — outside scripts can START a conversation: `WebhookServer(on_start, token=None)` serves `POST /start` on LOOPBACK ONLY (non-loopback hosts raise at construction, checked via getaddrinfo, not string match). Strict payload whitelist in the event-hooks culture — required `topic` (≤500 chars), optional `seats`/`turns`/`workspace`, UNKNOWN KEYS REJECT (a typo'd key would look accepted and do nothing); 64 KB body cap; `X-Alloy-Token` via `hmac.compare_digest`; GET `/health` open, everything else 404. `start()` returns False on a busy port rather than retrying into someone else's socket; `allow_reuse_address=False` because Windows SO_REUSEADDR permits port hijack; `serving` = socket open NOW vs `started` = "any /start ever succeeded" (survives stop). TLS deliberately absent: loopback traffic never leaves the machine. App bridge: config in `sessions/webhook.json` (derived from relay.SESSIONS_DIR at call time), `get_webhook`/`set_webhook` follow the recheck_auth shape (`{"ok": True}` at once, bind/unbind on a worker thread, truth arrives as a `webhook_status` event); `_webhook_on_start` refuses while ANY chat is live by RAISING (HTTP 500) so a script sees failure, never ok-with-error; UI toggle lives in the Event-hooks modal with a copy-curl button. |
| `workstreams.py` | Pure concurrent-work scheduler used by the shared loop: slot-id-keyed task records, strict in-flight context isolation, dependency scheduling, overlap auto-serialization, literal-file ownership, filesystem verification, and settlement summaries. `relay.assign_workstreams` seeds private queues; `commit_reply` settles/verifies an active task and only its summary crosses streams. Workstreams persist additively in meta and emit `workstreams` UI events. |
| `stats.py` | Cross-session statistics over `sessions/*/outcome.json` (2026-08-27). Standalone like export/fork — stdlib plus outcome's reader, no writes. `collect` groups by PROVIDER and by `provider:model` (never by seat id, which names a different agent in every chat) into chats/turns/spend/prompt/output/cache-hit/wall-time rows. Two rules carry the whole module: **the cache convention is per provider and measured** (`CACHE_CONVENTION` — claude's cached tokens are DISJOINT from its input, codex's are a SUBSET; one formula for both is wrong by ~44% or ~1000x) and **a number nobody reported is None, never 0**. `MIN_TRUSTED_BASIS` excludes GPT token counters written before the W1.3 fix — believing them makes the real sessions folder read 559,310,306 input tokens — while keeping their turns and costs, and the count of what was excluded is published so a front end can say so. |
| `retro.py` | Deterministic outcome aggregator behind `/retro`. It scans `sessions/*/outcome.json`, keeps hard facts separate from explicit feedback, derives only provenance-backed rules (human reason tags immediately; inferred patterns after recurrence), and atomically refreshes the human-editable `sessions/playbook.json`. Pinned/dismissed choices survive merges; unpinned inactive rules decay after 30 days. |
| `export.py` | Standalone HTML transcript export (2026-08-25). `export_session(session_dir, out_path=None)` reads messages.jsonl (legacy transcript-only folders ship the raw markdown in a `<pre>`) + meta.json and renders ONE self-contained file — inline CSS, no external resources, no JS, provider-colored escaped cards with activity `<details>` and usage pills. Deterministic on purpose: NO export timestamp is embedded, so identical input is byte-identical (a test pins it). Stdlib only, imports nothing from relay/app; errors are `{"error": sentence}` dicts, never raises. App bridge: `Api.export_session` (bridge-thread safe bounded I/O; exporting a RUNNING chat is allowed — it reads the log once into a separate file). UI: ⤓ button on every rail row, opens the result in the default browser via `open_path`. |
| `fork.py` | Fork (branch) a conversation at a chosen message (2026-08-25). `fork_session(session_id, upto_message_id=None, sessions_dir=None)` copytrees the session folder to a unique `-fork-<HHMMSS>` sibling, keeps rows up to AND INCLUDING the chosen message, REGENERATES transcript.md from those rows (keeping the source's longer transcript would read as if later turns happened), deletes outcome.json/say.txt from the copy, sanitizes meta (`id`, title +" (fork)", `ended: False`, `fork_of` provenance, parent/children reset), and **clears every seat's CLI session id** — the provider threads hold the ORIGINAL conversation's memory, so resuming them from a diverged timeline would forge continuity (house rule). The source folder is never touched; any post-copy failure removes the partial copy. Standalone stdlib-only. App bridge: `Api.fork_session` refuses while the source run is running and resolves through `relay.SESSIONS_DIR`, never fork.py's own module constant (same rule as tabs: tests redirect relay's globals; a second default would silently split where sessions live). UI: ⑂ button on any persisted message row (two-step arm like rail delete) → forks and opens the new chat immediately. Rail tooltip shows "branched from …" via session_summary's `fork_of` field. |
| `memory.py` | **Notes that outlive one conversation** (Wave 3, 2026-08-27). Standalone like export/fork/stats -- stdlib only, imports nothing from relay/app, never raises out of a public function, and it keeps NO module-level root: relay owns `MEMORY_DIR` and passes it in, because fork.py's gotcha is that a second default is how two halves of the app disagree about where the data lives. `BASE_DIR/memory/` is a **sibling** of `sessions/` (a child would ship a phantom rail row the two-step delete would `rmtree` -- there is a test that builds one and watches it appear). Markdown is the ONLY source of truth: no persisted index, because at a few hundred short entries the rollup costs under a millisecond to recompute and an index would buy nothing while adding a whole staleness class. `project_key` carries a sha1 of the **normcased** path AND normcases the basename (normcasing only the digest gives two spellings of one folder one hash and two filenames -- the exact collision the sha1 was added to prevent, arriving through the other half of the key). **A chat reads and writes exactly ONE scope** -- its project, or `global` in a scratch chat -- and the single crossing is by KIND, not scope: a note **Josh** wrote globally reaches every chat, a note a SEAT wrote globally does not, which is the cross-project poisoning path closed by construction. Every write is read-modify-write on a file several chats share, so it takes a real cross-process lock (stale locks are BROKEN, not waited out, or a killed process disables memory forever) -- last-rename-wins would silently lose a note, and Alloy runs several chats at once by design. Eviction never touches Josh's own notes and says out loud when it dropped a seat's; truncation announces itself in all three places it can happen (an oversized file, a trimmed search, a trimmed preamble block), and a file too large to read in full REFUSES to be rewritten rather than deleting everything past the cut. |
| `dictation.py` | A microphone for the composer, on a LOCAL engine — no key, no account, offline. `Recorder` (16 kHz mono int16 via sounddevice, with a `stream_factory` test seam), a `Transcriber` registry (`WhisperTranscriber` over faster-whisper; `WisprFlowTranscriber` is a documented SEAM that refuses by name — Wispr's own API is approval-gated and billed, see the gotcha), and `probe()`, which reports WHICH piece is missing rather than a dead button. Imports nothing from relay/app/webview. |
| `speaker.py` | Read-aloud — the OUTPUT twin of dictation (2026-08-25), same discipline: local engine, no key/account, `probe()` honesty, injectable `runner` seam, never raises. Windows SAPI via a hidden PowerShell child (`System.Speech`); the text crosses ONLY as base64 UTF-8 on stdin so neither injection nor the ANSI-codepage gotcha can reach it; the `-Command` script is <500 chars of pure ASCII decode machinery. Latest-wins: speaking again stops the old utterance first (each swap reaps only the proc it captured, under one lock); `stop()` when idle returns False like dictation's stop-with-no-start; blocking work (stdin feed, terminate→kill reaping) rides throwaway daemon threads. Accepted trade: ~200 ms PowerShell spawn per utterance instead of a persistent SAPI host process. App bridge: `speak_text`/`stop_speech`/`speaker_state`; UI: per-row 🔊 toggle button (hidden when the probe says unavailable), availability rides `get_config`'s `speaker` key probed in `precompute_config`. |
| `desktop.py` | Windows desktop control (computer use), library only — no seat exposure yet, that is Track B. Same discipline as dictation/speaker: injectable `backend` seam, honest `probe()`, imports nothing from relay/app/webview, cheap import that never raises off-Windows. Ported from `PerryLink/dsh-click` (Apache-2.0, a DeepSeek Harness plugin) but **in-process** via pythonnet → `System.Windows.Automation`, which removes dsh-click's worst flaw (it spawns a PowerShell child and recompiles its `Add-Type` C# on EVERY call, ~200 ms each). Zero new pip dependencies: pythonnet is already a hard pywebview requirement, and pywin32/Pillow/psutil/mcp were all installed. The contract is **observe → cite → verify → act**: `screen_read`/`screen_shot`/`app_list` are free; every mutator (`click`/`type_text`/`scroll`/`key`) REQUIRES `based_on={observation_id, window_id}`, takes a full re-snapshot, and answers one of five named refusals (`UNKNOWN_OBSERVATION`, `EXPIRED` 30 s, `STALE_IDENTITY`, `STALE_TREE`, `STALE_PIXELS`) each ending "run screen_read again before acting" so the refusal is model-actionable. `element_id` is the UIA RuntimeId, **re-resolved by FindFirst at action time**, never an index. Input NEVER steals the foreground: UIA patterns first, `PostMessage` second, cursor never moves, `delivered: uia|posted|none` reported; a test greps the source for `SendInput`/`mouse_event`/`SetCursorPos`/`SetForegroundWindow` and fails on a hit. `strict_pixels` defaults **off** (a raw-pixel hash refuses on a blinking caret — a check that fires on healthy behaviour teaches people to route around it, the silence-vs-duration lesson again). Three dsh-click bugs fixed rather than ported: its scroll direction disagrees between the UIA and wheel paths (one shared table here, with a test that flips the entry and asserts BOTH outputs flip), its process-identity check is vacuous (both reads happen after the action; here it brackets, proven by asserting the call log reads identity/invoke/identity), and its flat DFS-truncate-at-500 hands a Chromium window 500 nodes of scaffolding and no buttons (ranked pruning here: visible-and-intersecting, has-pattern, interactive, named; truncation announces `kept`/`seen`). **`self_pids` is injectable and that is load-bearing** — the refusal of Alloy's own process tree is structural, not a preference (a seat that can click Alloy's approval modal makes every approval forgeable), and it applies to the OBSERVERS too, with no override parameter; when the MCP server ships as a separate process it must be TOLD Alloy's pid rather than inferring it from its own ancestry. 108 token-free tests, no hardware. |
| `desktop_mcp.py` | The DELIVERY half of computer use: a stdio MCP server (official `mcp` SDK, already installed) that a seat's own CLI talks to. Everything comes from the ENVIRONMENT, never from a tool argument — `ALLOY_DESKTOP_RUNG`, `ALLOY_DESKTOP_APPROVAL_DIR`, `ALLOY_DESKTOP_SEAT`, `ALLOY_DESKTOP_ALLOWLIST`, `ALLOY_APP_PID` — because the model writes the arguments. Four rungs: `off` (default; refuses everything and does no desktop work at all, not even a window enumeration), `ask` (observers free, every mutator waits for Josh), `allowlist` (windows matching Josh's up-front regexes go straight through, everything else asks), `full` (no prompt, unattended). `ALLOWED_ARGS` is derived FROM the tool schemas and is deliberately narrower than the Python signatures: `allow_password` and `strict_pixels` appear in neither, which is what makes them settings rather than arguments a seat can flip. Approval reuses `approval_hook.py`'s proven wire protocol (`<id>.req` in, `<id>.ans` out, fail closed on timeout/junk/no-channel) but on `Agent.desktop_dir()`, a SEPARATE directory from `approval_dir()` — see the gotcha. `gate()` takes a `describe()` CALLABLE rather than a value so a refusal costs nothing. Live-verified end to end 2026-08-26: a real haiku seat connected (`mcp_servers [('alloy_desktop','connected')]`), listed 12 windows, read a Notepad tree, tried a click, and the relay answered deny — the model reported the refusal accurately. 38 token-free tests in `tests/test_desktop_mcp.py`. |
| `browser_mcp.py` | The DELIVERY half of **web use** (Track C, 2026-08-26), and Alloy's third capability axis. Alloy writes no browser library and forks nothing: this is a stdio MCP server that spawns Google's **`chrome-devtools-mcp` 1.7.0** (Apache-2.0, zero runtime deps, pinned on disk in the npx cache -- never `npx @latest`, since the installed copy already advertises 1.8.0 on every startup) as a child, forwards MCP JSON-RPC to it, and applies Alloy's own ladder before anything that changes a page. **The load-bearing control is NOT the ladder -- it is the Chrome flag.** `--allowedUrlPattern` is enforced inside Chrome's network stack: it blocks navigations AND subresources and survives `evaluate_script`, the one tool that walks straight through any allowlist applied at the tool layer. Four rungs (`ALLOY_BROWSER_RUNG`): `off` (default, not even registered), `read` (navigate/snapshot/screenshot/console/network -- no clicking, typing or scripting), `ask` (as read, plus interaction, each waiting for Josh), `full` (unattended). `read` exists because a browser has no equivalent of "look at a window": reaching a page IS a network request, so navigation is free at every live rung and the ladder gates what CHANGES a page. Everything comes from the ENVIRONMENT (`ALLOY_BROWSER_RUNG/_SITES/_APPROVAL_DIR/_SEAT/_VENDOR/_NODE/_WORKSPACE/_WEBHOOK_PORT`), never a tool argument. `PUBLISH` is the curated republish -- 24 of the vendor's 29 tools, argument keys from Alloy's own table, schema bodies copied from the vendor's LIVE schema with `additionalProperties` forced **false** (the vendor ships it true), so the prose stays Google's while the fence stays ours and a version bump cannot widen it. `curate()` is a reconciliation GATE, not a filter: a tool the installed vendor lacks, or one whose `required` list names an argument outside our keep-set, is DROPPED with a named reason rather than published with a mutilated schema -- which is what catches a *renamed* required argument, something a key whitelist cannot see. `WITHHELD`/`DROPPED_ARGS` are stated in the server's `instructions` rather than left as absences (a seat that knows a capability was withheld asks Josh; one that finds a tool missing invents a workaround). Structural withholdings that survive `full`: `initScript` always (script injection under another name), `extraHttpHeaders` always (credential injection), every write-path argument always (the vendor already confines writes to the OS temp dir when the client does not negotiate MCP roots -- so we deliberately do NOT negotiate them, and never pass `--allowUnrestrictedPaths`), and `evaluate_script` whenever a configured site can reach loopback. `upload_file` keeps its required `filePath` and gets Alloy's own workspace confinement instead -- it READS. That confinement (`_confine`) is a deliberate SECOND COPY of `relay.confine_to_workspace` — this module imports nothing from relay — kept in step by `tests/test_confinement_parity.py` rather than by the docstring that used to claim parity while four rules differed (see the gotcha). Approval reuses `approval_hook.py`'s wire protocol on `Agent.browser_dir()`, a THIRD directory beside `approval_dir()` and `desktop_dir()`. `decide()` answers the rung without touching the browser, so a refusal never starts Chrome. `probe()` mirrors `dictation.probe()`. 111 token-free tests in `tests/test_browser_mcp.py` — including a BRIDGE section (real `app.Api._conversation`/`open_session` with adapter subclasses whose only fake is `turn`), because W0.1's whole lesson is that the engine can be perfect while the bridge drops the key. TWENTY-THREE of its rules RED-verified (removing each one fails the suite): the flag spelling, empty-means-deny-all, the self-test, the argument fence, upload confinement, the non-blocking approval wait, the prove-fence-once lock, and the four bridge plumbing points (cfg key, clamp, meta persistence, session_summary), and the six defects an adversarial review found (rehydrate returning the axes to state, the no-vendor capability claim, look-only publishing tools it always refuses, an approval note riding a fence refusal, handleBeforeUnload, the advisory that carved the fence out of its own admission, the page-controlled approval-card URL, the values hidden from the card by fill_form/handle_dialog, and the two rungs at which the servers registered but could not be called). |
| `relay.py` | Engine + CLI. Agent adapters (`ClaudeAgent`, `CodexAgent`, `GeminiAgent`, `OpenCodeAgent`), THE shared loop (`run_rounds(state, io)` + `LoopIO` seam + `CLIIO` terminal front end + `dispatch_command`/`seat_command`), say.txt/stdin interjection, and the durable session layer (`SessionStore`, `make_log`, readers/listing, continuation validation, rehydration). Every visible message is appended to both `transcript.md` and `messages.jsonl`; `meta.json` atomically snapshots CLI session ids, queues, seat config, and round state after fan-out. Interject commands: `/stop`, `/turns N`, `/clear [seat]`, `/compact [seat]` (compact = seat self-summarizes via `COMPACT_PROMPT`, then its session restarts seeded with the summary; seat arg = label or provider, omitted = all; helpers `match_seats`/`compact_agent` shared with app.py). Also owns `PROVIDERS`, the single provider registry (adapter class, color, auth probe, login/logout argv, install hint) — **Moderation is a primary control**: `#modToggleRow`'s checkbox (above the Advanced drawer, not inside it) mirrors `floorSel` and re-runs `normalizePolicyControls("floorSel")` rather than keeping its own state. Promoting it exposed a one-way coupling in that normalizer: `completion="moderator"` implies a moderated floor, so the fallback branch dragged the floor back and moderation could be switched ON but never OFF (from the drawer either) - the anchored axis now wins and the end-condition follows to `participants`. **The moderator/supervisor is nameable**: `#modName` (placeholder = the role word, like a seat's auto name) rides in the same spec, and `room_helper_name(state, role)` is the ONE place that answers "what is this room's helper called" - both builders and every user-visible sentence read it FROM STATE rather than off the agent object (which is a stub in tests). **Moderator and supervisor are one role under two labels** - one picker, relabelled by `syncModCtl`, and they never coexist (a moderated room has no supervisor and vice versa); `build_digest_agent` and `helper_spec` therefore accept either, which is what stopped a Build Together room with an Ox supervisor from handing every digest to claude. **Any seatable provider can also run the room**: the moderator/supervisor picker is rebuilt from the same `seatable` registry payload as the Add-seat picker (a hand-kept `<option>` list is exactly how Ox shipped seatable but unable to moderate); `syncProviderPicker` compares option values **and labels**, never the count - the static markup ships one `<option>` per provider, so a count guard matched on the first paint and the hand-written label stuck, leaving the picker offering "Ox" long after the provider was renamed "OpenCode" and making a seven-model gateway read as one model, and `helper_spec(seat_providers, moderator_spec)` decides who does the relay's OWN side work - moderator, then first seat, then the historical claude default - so an all-Ox room never quietly spends a Claude call for its brief. `AGENT_TYPES` is derived from it; grok is registered with `agent=None` (Accounts-panel-only) until its adapter lands, and adding a provider = one entry. `Agent.extra_env()` is the hook for a CLI whose sandbox is a config rather than a flag (opencode); `OX_FREE_MODELS`/`OX_DEFAULT_MODEL` are the one catalog app.py intersects with `opencode models` so the seat never offers a model this install lacks. Auth probes (no tokens spent): claude → `claude auth status --json`; codex → `codex login status` with `~/.codex/auth.json` fallback on timeout; gemini → file check of `~/.gemini/oauth_creds.json` + `google_accounts.json` (agy has NO auth subcommand; can't detect revoked tokens); ox → `opencode auth list`, where **zero credentials is `signed_in`, not `signed_out`** (Zen's free models need no account, so greying that seat out would be a lie; sign-in only adds the paid catalog). **Ox specifics** (all verified 2026-08-22 against opencode 1.18.21): `run --format json` is JSONL, one event per line, every line carrying `sessionID`; resume is `--session <id>` (NOT `--continue`, which means "the last session" and, placed before the subcommand as the flag-insertion would put it, just prints help and exits 0); the model is ALWAYS pinned because an unpinned run can pick a paid Zen model on a keyless machine; the permission ladder is half flags and half config — `--agent plan` for read-only/ask (verified: it refused to create a file and explained why, while the default `build` agent wrote one with no prompt and no stall in a pipe) and `OPENCODE_CONFIG_CONTENT` via the new `Agent.extra_env()` hook for the rest, where `external_directory: deny` is the workspace boundary and survives `--auto` (verified). Thinking levels are **per model**, from models.dev via opencode's `~/.cache/opencode/models.json` (`ox_model_details`): Ox Alpha has low/high/max, Muse Spark five, Hy3 three, Nemotron/MiMo/Big Pickle NONE - so the box disappears rather than offering levels that do nothing. This was nearly shipped disabled: a first probe passed `--variant bogus-level`, saw no error and 0 reasoning tokens, and concluded there was no control. The prompt was the problem - it needed no thinking. On a river-crossing puzzle: `low` -> 0 reasoning tokens, `max` -> 42. opencode does NOT validate the flag, so a level must come from the model's own `reasoning_options` and never from a shared list. **The provider is `OpenCode`, not `Ox`** - a gateway is not an identity, so `Agent.seat_name(model)` names each seat after the MODEL it runs ("Ox Alpha", "Nemotron 3 Ultra") and `assign_labels` takes an optional third tuple element; the UI's `seatBaseName` mirrors it, because only TYPED labels reach the engine and an auto name that differed would put a different roster in the transcript. Probes return `unknown` on garbage/timeout — NEVER guess signed_out. `logout_gemini` moves creds to `~/.gemini/aichat-logout-backup-<stamp>\` (restore = move the files back). `resolve_cmd`/`clean_env` are the extracted shim-resolution + env-strip helpers used by turns, probes, and app.py. **Skills + MCP management** (see the gotchas): `PROVIDERS` entries carry `skills_dir` + an `mcp` descriptor (`kind="cli"` with `argv`/`fmt` for claude+codex, `kind="file"` with `path` for agy's `mcp_config.json`); `manageable_providers()` is the seatable-with-a-CLI subset (grok excluded). Skills: `valid_skill_name` (reject, never sanitize), `skill_file_in` (case-insensitive `skill.md`), `parse_skill`/`render_skill` (BOM-tolerant frontmatter), `list_skills` (normalized-sha divergence + `extras` count), `read_skill`, `write_skill(..., source=)` (whole-tree copy, atomic swap), `delete_skill` (refuses a folder with no SKILL.md). MCP: `list_mcp` (`_parse_mcp_line_list` for claude, `_parse_mcp_json_list` for `codex mcp list --json`, JSON file for agy), `add_mcp`/`remove_mcp` (claude gets `-s user`), `invalidate_mcp_cache`. **Showing that the RELAY is working** (2026-08-25): `working(io, phase, detail="", label="")` is a context manager that emits a `working` event on entry and, in a `finally`, on exit - one row for every stretch of NON-seat work, which is the entire answer to "nothing is happening yet". Wired at every site that used to run silent: the Supervisor's plan/replan/review/next-objective/check-in calls, `moderator_pick`, `synthesize_brief` (via `project_brief(io=)`), `maybe_auto_title`, the relay digest, `/compact`, `wave_gate`'s test subprocess (not a CLI call at all, and often the longest silence in the app), spawned helpers, a whole spawned team, and app.py's pre-flight `setup`. `WORK_PHASES` is the wording table - a bare key like "replan" on screen answers "is it stuck?" no better than an empty window - and `label=` lets a room's own manager name appear instead of the word Supervisor. Ids are unique per call so concurrent openers (parallel/free seat threads, helper threads) cannot close each other's rows, `io=None` is a legal no-op, and every emit is swallow-everything: this is decoration, and it must never fail the work it wraps (same contract as the activity hooks). **Streaming turns + live activity** (2026-08-17): `Agent.turn(message, on_activity=None)` runs the CLI via `Agent._run_streaming` (Popen; stdout read line-by-line on the seat's own thread, stderr drained by a helper thread, a polling watchdog enforcing SILENCE rather than duration — `self.idle_timeout` = `IDLE_TIMEOUT × TIMEOUT_SCALE[effort]`, restarted by every line on either pipe, plus the normally-`None` absolute `self.turn_timeout`; `armed_window(agent)` names whichever one is live); each stdout line goes through the per-adapter `activity(line)` hook (claude: stream-json assistant events — thinking blocks + tool_use described per tool; codex: the `--json` item.started/item.completed vocabulary, verified live 2026-08-16; gemini: none — but see the image harvest below) and the resulting `{kind, text[, path_raw]}` dicts flow through `make_activity_sink` (per-turn: consecutive-dedupe, 160-char texts, `ACTIVITY_MAX` cap, and **edit-path confinement** — `path_raw` from a CLI stream is untrusted, `confine_to_workspace` (lives HERE, app re-exports) either relativizes it or drops the whole event silently) out as `activity` events, and persist (last `ACTIVITY_KEEP`) as the message row's optional `activity` list. ClaudeAgent runs `--output-format stream-json --verbose` (--verbose is REQUIRED in -p mode; final line is the same result object, `_result_object` reverse-scans for `"result"` as hardening). **Narration got the two things it was missing** (2026-08-26, measured against live streams from all three CLIs, not inferred): every CLI streams the model's OWN running commentary and every tool's OUTPUT, and we dropped both - which is why the log read as a list of intentions with no story and no results. Two new kinds: `say` (claude's `assistant`/`text` blocks, codex's `agent_message` items, opencode's `text` parts) and `result` (claude's `user`/`tool_result` blocks paired back to their call through a per-turn `_tool_names` map reset in `before_run`, codex's `aggregated_output`, opencode's `state.output`). `_result_note(tool, text, is_error)` summarizes in the terms of the tool that produced it - "found 3" for a grep, "read 40 lines" for a read, "saved" for an edit, the first line otherwise - because a shared "3 lines" would make a grep look like it returned a file. Calls carry more of what was already in their arguments too: `_edit_size` renders "(+14/-3)" from Edit's own old/new strings (blank for Write, which has no old text - a made-up number is worse than none), Grep says WHERE it is looking, Read says which lines. Ox additionally gained a live token counter from `step_finish` (it had NO progress signal at all, so a long silent step was indistinguishable from a hung one). `Agent.last_context` is the sibling of `last_usage` for the seat's context LEVEL (see § Wave 1's W1.4 note) - never summed, never folded into usage, stamped on the row as its own `context` key. `ACTIVITY_MAX`/`ACTIVITY_KEEP` rose to 400/80 because a call and its outcome are two entries now. A fourth kind, `todo`, carries the seat's own checklist (see § Wave 1's W1.2 note): `TODO_STATES`/`_todo_state`/`_todo_act` are the one canonical shape three very different CLI vocabularies are normalized into, the sink keeps exactly ONE of them and re-parks it at the END of `acts` on every append (ACTIVITY_KEEP trims from the front), and it is never counted as a step. **The sink holds the newest `say` back**: the LAST piece of a turn's commentary IS the reply, so emitting each one on arrival would print the whole answer into the log a moment before the message row does. It is released only when something else follows it; the final one never is, and no turn-end hook is needed because the turn ending is exactly the thing that does not follow. `describe_failure(stdout, stderr)` is the per-adapter error sentence used by BOTH raise paths (claude: subtype + HTTP status + the CLI's `result` text; codex: `error`/`message` events; base: the stderr/stdout tail). Every row also carries `ts` (ISO seconds) — stamped in `SessionStore.record`, echoed as HH:MM in transcript.md headers. `note_retry` persists first-failure retry notices as system rows; `TurnTimeout` errors are `no_retry` and name minutes, `error_excerpt` keeps head+tail of long errors. `ai-chat` on PATH (`~\.local\bin\ai-chat.cmd`) calls it. Per-seat **roles**: `Agent.role` (public name) + `Agent.role_instructions` (private text) live on the agent like `name`, so `preamble()` reads them without either loop passing new args; `preamble(..., roster=agents)` prints the roster in turn order. `apply_role_flags` applies `--role`/`--role-instructions` through `match_seats` after `assign_labels`. `SessionStore.record(role=)` stamps the role into each row. **Orchestration** (ORCHESTRATION_DESIGN.md): `MODES`/`IMPLEMENTED_MODES` + the per-turn scheduler (`choose_next_seat`, `compose_prompt`/`commit_reply`/`commit_skip`), `run_parallel` and `run_free` (the two threaded modes), `peel_directives` (the one trailing-directive grammar; `wrap_called` is a one-liner over it), `set_next_speaker`, `moderator_pick`/`build_moderator`, and `SpawnManager` + `handle_spawn_directives`/`parse_spawn`/`parse_team` for the three spawning tiers. **Asking Josh** (ORCHESTRATION_DESIGN.md § Asking Josh): `parse_ask` + `handle_ask_directive` (runs after `commit_reply` in all three loops, blocks on the `LoopIO.ask_human(payload, abort=None)` seam — headless default answers `None` instantly so nothing ever hangs) + `announce_lost_ask` (run start, next to `announce_lost_helpers`); gate `state["ask"]` (CLI `--no-ask`, app always True, child teams False) toggles the preamble's "Asking Josh" block + softened header; `ask`/`ask_pending` persist additively in meta; `CLIIO.ask_human` prompts on the console with an `_asking` flag so concurrent coordinator drains can't steal the typed answer. **Shared project context** (PROJECT_CONTEXT_DESIGN.md; see the gotcha below): `BRIEF_DOCS`/`project_doc_names()` (the FIXED scan set) + `Agent.project_docs` per adapter (what each CLI already auto-loads — drives only the per-seat "you already load this" line; `test_brief` guards the two against drifting), `find_context_docs`/`quote_docs` (verbatim path), `brief_status`/`brief_fingerprints`/`write_brief`/`read_brief` (sha256-keyed staleness for the synthesized path), `BRIEF_PROMPT`/`synthesize_brief` (a throwaway stateless adapter exactly like `build_moderator`), `project_brief` (the one orchestrator both front ends call), `brief_preamble_block`, `brief_record`/`write_project_context`/`read_project_context`/`brief_drift`. Gated on `session_project(...)` being truthy, so default in-session workspaces are untouched. **Keep Improving** (the continuous mode, README § flags): one block of code beside the Supervisor it extends, because it reuses `wave_report`, `plan_workstreams`, `assign_workstreams`, `build_supervisor` and `supervisor_trace` wholesale. `continuous_policy` normalizes/validates the conversation-level recipe (nullable limits — all three null is legal and means *only the Stop button*; `_opt_number` turns garbage into `None`, never `0`). `effective_ceiling` is now the ONLY reader of `turn_ceiling` (see the gotcha). `continuous_tick` accumulates run seconds onto a persisted clock so a resumed chat keeps yesterday's hours; `continuous_backstop`/`announce_backstop` pause once on a spend or time limit with `termination_reason = "limit"`. Three brakes come off: `[[DONE]]` calls `next_objective` (a stateless side call ending `[[OBJECTIVE: …]]` or `[[IDLE: …]]`, both opted in like TASK/DONE and absent from `KNOWN_DIRECTIVES`) which archives the settled board via `archive_objective`, resets `supervisor_waves` (the cap measures ONE goal) while the wave INDEX keeps climbing (the UI cuts its wave boxes on it), and re-plans; `SUPERVISOR_MAX_WAVES` becomes per objective; and the ceiling is absent. `run_checkin` is the scheduled watchdog — `continuous_health` builds a measured snapshot (committed turns since the last check, tasks stuck across checks via `_track_stuck`, seats with no session id or marked unavailable, the last gate), `parse_checkin_verdict` reads one of `[[HEALTHY]]`/`[[FIX: remedy | why]]`/`[[STOP]]`, `split_remedy` + `apply_remedy` perform the CLOSED remedy set, and the `auto|notify|permission` action decides whether Josh is told, flashed at, or asked through the existing `io.ask_human` seam. `wave_gate` runs the project's own test command (`detect_test_command`, `_gate_run`/`_git`/`gate_commit` — all three are the test seams) BEFORE the manager reviews, so the wave report carries proof rather than claims, and only green code is committed. `continuous_revive` + the revival loop inside `run_rounds` restart a run that ended on anything except Josh's Stop or a limit he set, bounded by `MAX_BARREN_REVIVALS`. `current_objective` is the one answer to "what is this run working on". `describe_limits` backs `/limits`; `/checkin` and `/objective <text>` are its siblings. **Browser control** is the third capability axis, built as an exact sibling of the desktop one (`BROWSER_RUNGS`/`BROWSER_ORDER`/`BROWSER_SERVER`/`normalize_browser`/`browser_enabled`/`browser_capability_clause`, `Agent.browser`/`browser_sites`/`on_browser_approval`, `browser_dir()`/`browser_server_spec()`/`_watch_browser()`, the `ask_browser` callback in `run_rounds`, `--browser`/`--browser-site`, meta keys `browser`/`browser_sites`). Two things it does NOT share: `browser_site_report`/`clamp_browser_rung` delegate pattern classification to `browser_mcp.classify_sites` so the fence Josh sees judged and the fence Chrome receives cannot disagree, and the watcher starts only at rung `ask` (`read` refuses without asking and `full` asks nobody). `advisory_rung_note()` is shared by both axes and states the honest ceiling — see the gotcha. `ClaudeAgent.build_cmd` now builds `extra` as a dict of BOTH servers and appends every registered name to `--allowedTools`, not just the first. |
| `app.py` | Desktop app: pywebview/WebView2 window hosting `ui/index.html`. **Stats & playbook** (2026-08-27): `get_stats`/`get_playbook` follow the `recheck_auth` shape (ok at once, worker thread, truth as a `stats`/`playbook` event) because both scan every session folder uncapped; `get_playbook` runs the FULL retro pass rather than a read, so the tab cannot display rules the Supervisor is not actually using; `set_playbook_rule` is bounded file I/O and answers on the bridge thread like `save_skill`. Imports relay's adapters/session helpers and runs the SHARED loop: `_rounds(state)` is now a thin wrapper that calls `relay.run_rounds(state, _AppIO(self))` then runs the app epilogue (paused footer + `done` emit). `_AppIO` (module-level class) adapts `emit`/`_human_q`/`_stop_flag`/staged-role commit to the LoopIO seam. `list_sessions`/`open_session`/`rename_session`/`delete_session` are bridge-thread-safe file operations; `open_session` rebuilds live agents and assigns their saved CLI ids, so a new process can continue the conversation. `command(text)` handles the same slash commands as relay: queued to the loop when running, executed on a worker thread when idle. Accounts: `precompute_auth` (startup thread, thread-per-provider, progressive `auth_status` emits), `get_auth_status` (cache snapshot ONLY — must stay subprocess-free/non-blocking, it runs on the bridge thread), `recheck_auth`/`sign_in`/`sign_out` (bridge → worker thread). Pre-flight `_auth_blockers` gates `_conversation`/`_continue` on cached signed_out/not_installed only — unknown/pending NEVER blocks. Roles: `apply_role(seat_id, role, instructions)` only STAGES (bridge thread — never subprocess there); `_commit_roles` drains the staging area at a turn boundary inside `_rounds`, or on a worker thread (`_commit_roles_idle`, guarded by `_roles_lock`/`_roles_busy`) when the chat is paused. `emit` is now a pure enqueue onto `_emit_q`; ONE daemon thread (`_drain_emits`) owns `evaluate_js` — required once parallel/free modes emit from seat threads, and it makes event order FIFO across producers. `_conversation` validates cfg `mode` against `IMPLEMENTED_MODES` and builds the `moderator`/`until_done`/`turn_ceiling`/`spawn` state; `_continue` extends `turn_ceiling` for until-done chats (round cap otherwise) and clears stale `closing`/`next_speaker`. `precompute_config` warms `relay.codex_multi_agent_enabled()` off the bridge thread. [[ASK]] plumbing: `_AppIO.ask_human` emits a `question` event then blocks the CONVERSATION thread on a per-qid queue (polling `_stop_flag` + `abort`), under `Api._ask_lock` so simultaneous parallel-mode questions become consecutive modals; `Api.answer_question(qid, text)` is a pure bridge-thread enqueue (empty text = skip); `question_done` always follows, win or lose. Shared project context: `_conversation` calls `relay.project_brief` AFTER the `started` emit (so a slow read shows a status line, not a frozen window) and BEFORE the opener (compose_prompt prepends the preamble to the first prompt), on the worker thread `start` spawned — cfg key `brief`; `_continue` calls `brief_drift` and REPORTS changes without regenerating (regenerating there would hand a later `/clear`'d seat different context than its peers got); `open_session` patches `state["brief"] = read_project_context(path, meta)` beside the `store`/`log` patches, because `rehydrate` has no session_dir. **File/image viewing bridge**: `read_image(path, full)` / `list_workspace_files()` serve the UI's inline previews + Files rail — `confine_to_workspace` canonicalizes (realpath BEFORE the containment check, so junction/symlink escapes fail) and serves ONLY files beneath the LIVE workspace (`_conv["workspace"]`, or `_view_workspace` for reopened view-only chats — never a path rebuilt from the session id); image-extension allowlist + 15 MB cap, data URIs (file:// doesn't load in WebView2), 320px thumbnail bytes first, full res only for the lightbox; forbidden and missing paths return the IDENTICAL quiet error (no existence disclosure); `tests/test_bridge_files.py` guards all of it. **Skills & Connections bridge**: `get_skills`/`read_skill`/`save_skill`/`remove_skill` are SYNCHRONOUS bridge-thread methods (bounded file I/O, like `list_sessions`) and merge the per-provider rows into one row per skill name with `providers`/`missing`/`diverged`/`extras`; `get_mcp`/`add_mcp`/`remove_mcp` shell out, so they follow the `recheck_auth` shape — return `{"ok": True}` at once, do the work on a worker thread, answer with an `mcp_status` event. `read_text(path)` is the live-code-viewer sibling of `read_image`: same `_active_workspace` → `confine_to_workspace` → identical quiet "not available", `TEXT_MAX_BYTES` cap, NUL-sniff binary refusal (tests/test_activity.py). The opener emits in `_conversation`/`_continue` emit the ROW returned by `log(...)` (never a hand-built dict), so live and replayed Josh messages carry identical keys (`ts` etc.). Desktop "AI Chat" shortcut → `pythonw app.py` (window titles itself Alloy; the shortcut keeps its on-disk name). **Dictation bridge**: `dictation_start`/`dictation_stop`/`dictation_cancel` follow the `recheck_auth` shape exactly — return `{"ok": True}` at once, work on a worker thread, answer with a `dictation` event (`recording` → `transcribing` → `text` | `empty` | `error`; the last two carry no text, ever). All state is `_dict_*` (public attrs deadlock the bridge walk); `precompute_config` runs `dictation.probe()` off the bridge thread and puts it in the config as `dictation`, and `_fallback_config` claims unavailable so an unfinished probe never shows a dead mic. **Window/taskbar icon**: `main()` sets `SetCurrentProcessExplicitAppUserModelID("Alloy.AIChat")` BEFORE the window exists (else the taskbar groups under pythonw with Python's icon), and `_apply_window_icon` (events.shown) sends `WM_SETICON` small+big from `ai-chat.ico` — best-effort ctypes, never raises; the desktop AND pinned-taskbar shortcuts point at `alloy.ico,0` — a byte-identical copy `make_icon.py` writes beside `ai-chat.ico` — because Windows' icon cache keys on PATH and the old chat-bubbles icon stayed cached under the `ai-chat.ico` path through every refresh (`ie4uinit`, .lnk re-save); a fresh filename was the only reliable fix. If the icon ever goes stale again, point the .lnks at a new copy name rather than fighting the cache. **Keep Improving**: `_conversation` validates `cfg["continuous"]` through `relay.continuous_policy` and fills the gate command + `dirty_at_start` AFTER the workspace exists (both need the real folder; the dirty snapshot is taken at the start because it decides whether a green wave may commit). `until_done` is forced on and `turn_ceiling` to `None` — the limits Josh acknowledged are the brakes. `_continue` clears the announced limit, re-arms the clock, forgives the barren-restart count, applies raised limits, and warns when the SAME limit is still tripped. No new thread and no `threading.Timer`: the watchdog rides the existing barrier, the only place the one-thread-per-Agent rule permits a side call. `continuous_probe(path)` follows the `recheck_auth` shape (git status is a subprocess, which deadlocks on the bridge thread) and answers the warning modal with a `continuous_probe` event. `_flash_taskbar` is a best-effort `FlashWindowEx`, called from the ONE emitter thread on a `checkin` event — which is why `_emit_q` now carries `(event, json)`. **Relay-busy tracking**: `Run.working` is the exact sibling of `Run.thinking` - `_AppIO.emit` opens/closes rows on the `working` event, `_rounds` clears it beside `run.thinking.clear()`, and `open_session` returns `working` alongside `thinking` so a chat reopened 90 seconds into a supervisor plan is not drawn as an idle one. `_conversation`'s whole pre-`started` stretch (attachments, the gate's git probe, opening the transcript) runs inside one `relay.working(..., "setup")`, and it passes an `_AppIO` into `project_brief(io=)`. **Sound cues** (`SOUND_CUES` + `_play_cue`, 2026-08-25): the same emitter thread spawns a daemon winsound thread on `question`/`checkin`/`done` — never inline (Beep holds its calling thread for the tone's duration); `set_sound(enabled)` toggles it, the UI remembers the choice in localStorage and mirrors it on boot, ON by default because every chimed event is one that waits on Josh or tells him work ended. |
| `ui/index.html` | Single-file UI (inline CSS/JS, local fonts only). A 224px chat-history rail lists saved sessions Claude-app-style: single-line rows (provider dots + ellipsized title; time/seats/view-only live in the tooltip) grouped under collapsible per-project headers — `session_summary` computes `project` via `relay.session_project` (basename of a CUSTOM working folder; the default in-session workspace ⇒ "" ⇒ the "No project" group), groups rank by their newest chat, collapse state persists best-effort in localStorage. Rows keep replay, active selection, dblclick-rename, two-step delete, and view-only legacy chats. The seat rail supports dynamic/duplicate seats with model + thinking pickers, rounds, working-folder picker, yolo toggle, and live thinking state; the seat-name heading is an editable input — the auto name ("Claude 2") is its *placeholder*, typed text becomes the seat's explicit label (`cfgFor` sends it as `label`, engine-side `assign_labels` takes it as-is and rejects duplicates), and `restoreSeats` writes a saved name into the box only when it differs from the auto placeholder so reopened auto-named seats keep renumbering; reopening restores original seat ids/models so events and captions remain truthful. No topic box: the first message typed into the chat bar starts the conversation (cfg key `opener`). After a run ends (`done` carries `can_continue`), the next non-`/` message calls `continue_chat`; messages starting `/` route to `api.command()`. Accounts live in a modal (`#acctModal`), opened by the sidebar-bottom `#acctBtn` button whose red badge counts seatable providers that are signed_out/not_installed; `renderAccounts` is registry-driven from `auth_status`. Roles are edited in a shared modal (`#roleModal`), opened from a slim per-card `.role-btn` showing the current role; role name + instructions live on the seat JS object (`seat.role`/`seat.roleInst` — `cfgFor`/`restoreSeats`/`roleApplied` all go through it, never through card inputs). Closing the modal commits only while un-seated; once a conversation exists (`setSeated`) the modal shows **Apply role change** instead — role edits cost a CLI turn, so they are never autosaved. `#seatList.locked` re-enables pointer events for `.role-btn` only. Message captions show the role from the stored row, never live seat config. The **mode picker is a composer-bar pill** (`#modePickBtn`/`#modePickMenu`, mirroring the permission pill; 2026-08-25 redesign): five modes — Discuss in Turns / Talk Live / Compare & Decide / Build Together / Keep Improving — one row each with icon + one-line description; a row click calls `applyPreset`, hand-edited axes read "Custom" with no row highlighted, and the pill locks once seated. The old five-card `#presetGrid` is GONE from the rail (`preset-card`/`preset-grid` must not reappear — test_orchestration_ui pins their absence); `#presetSel` stays the hidden state holder and all preset logic (`PRESET_RECIPES`/`presetForCurrentRecipe` incl. the `contOn` comparison/`applyPreset`'s contPrevPreset capture) is unchanged, only its visuals target the pill. The rail's **Conversation** group keeps only contextual controls, all locked once seated (`setSeated`) and restored truthfully when a chat is reopened: the hidden `#modeSel` (legacy compatibility recipe → cfg `mode`), `#modCtl` (moderator/supervisor provider+model+thinking picker → cfg `moderator`, defaults claude-haiku-4-5:low to match `build_moderator`, restored on reopen via `session_summary`'s `moderator` field with gemini slugs split back into family+level), the rounds stepper paired with `#untilDone` (checked ⇒ the same stepper becomes the safety-ceiling stepper via `syncRoundsCtl`, cfg `until_done`/`ceiling`), `#spawnSel` (helper budget) and `#teamSel` (team budget) → cfg `spawn: {tier1, max_helpers, max_teams}`. Typing indicators are a `Map` keyed by SEAT ID (`typingEls`, `showTyping(speakerId, provider, name)`/`hideTyping(seatId)`/`hideAllTyping()`) so parallel and free modes can show several seats thinking at once and duplicate-provider seats stay distinguishable; `addMsg` re-appends live indicators so they stay below new messages, and the round badge switches to `turn N/ceiling` for until-done runs. Message captions prefer the row's own `meta` (so helper rows read "helper for Claude"). Rail rows for spawned children show a ↳ prefix + "spawned by X" tooltip from `session_summary`'s `parent`. Composer extras: the box defaults to 96px min-height and `#sayGrip` (the grab bar above it) drags it taller — pointer-capture drag sets `sayMinH`, which `autoGrow` treats as the floor (the native corner grip also still works via the pointerup fallback); a 📎 button + paste handler queue attachments as base64 chips (`pendingAtt`) that ride with start/continue/interject and land in `<workspace>\attachments\` via `app.save_attachments` — message text gains `[Josh attached a file: <path>]` lines (`with_attachments`), so agents/transcript/replay all see them. Rails collapse from a `«` button ON each rail (`bindRail`); a collapsed rail is a 22px `.rail-reopen` strip whose click re-expands it — the `«` handler must stopPropagation or the same click bubbles to the strip listener and reopens instantly. Seat renaming has a ✎ button (`.rn`) that focuses the name input. Under the working-folder picker, `#projBrief` ("Share the folder's AI docs with every seat", cfg key `brief`) is locked by `setSeated` like the other conversation-level controls and restored from `session_summary`'s `brief` record, so a reopened chat truthfully shows whether its seats were ever given project context. **Skimmability**: `md()` peels TRAILING directives via `peelDirs` (a JS mirror of `peel_directives` — last-`[[`-anchored, end-only, ≤4) and renders them as `.dir-chip` pills ("asks Josh: …", "next: GPT", "wrap"); mid-reply mentions stay as raw text because they didn't fire; `==text==` renders as a seat-tinted `<mark>` (the preamble tells seats to mark at most one key line per reply). **Ask modal** (`#askModal` + `#askPill`): a `question` event opens a seat-colored modal — option chips answer instantly, `#askOther` is the free-text "Other" box (Enter sends), Skip answers empty; ✕/backdrop/Escape only HIDE it (the wait is engine-side) leaving the composer pill to reopen; `question_done` clears both unconditionally. **File & image viewing**: `addMsg` scans bodies (`findImageRefs` → `[{path, strict}]`: markdown image links + `[Josh attached a file: …]` + paths with ≥1 separator are STRICT (a load failure shows the quiet placeholder), a bare `name.png` in prose is LOOSE (a load failure removes the cell silently — a seat musing about "logo.png" must not leave a broken thumbnail), loose refs dedupe by basename so "saved as [x.png](C:\…\x.png)" yields ONE thumbnail. The drive letter must consume its own slash in that path regex — `(?:[A-Za-z]:)?` before `(?:[\w.\-]+[\\/])+` silently matched `C:\Users\x.png` starting at "Users", handing the bridge a relative path that resolved nowhere, i.e. every absolute path a seat reported — which is exactly how GPT reports the images it generates — rendered as "image no longer available") and renders `.img-thumb` strips via `fetchImage` (per-chat `imgCache`, cleared whenever the workspace boundary changes) — failures render a quiet `.img-missing` placeholder, never a broken tag; click opens `#lightbox` (full-res fetch). `#fileRail` (right side, same `bindRail` pattern) lists the working folder newest-first with thumbnails/type icons, click-to-preview, Open-in-OS; refreshes debounced on message events. Every `.msg` is `user-select: text` and carries a `.copy-btn` (clipboard API with execCommand fallback). **Skills & Connections** (`#skillModal`, `.modal.wide`, opened by `#skillBtn` above `#acctBtn`; add any new modal id to BOTH the `display:none` and `.show` selectors and to the one shared Escape listener): the Skills tab lists one row per skill NAME with a provider dot per CLI that has it (hollow = missing) and ⚠ when the copies diverge, an editor (name/description/body + per-provider checkboxes) whose **Save reconciles the ticked set** — ticking installs, unticking removes — and an `#skSyncBtn` "Install to GPT and Gemini" that just ticks the missing boxes and calls the same `saveSkill()`, so there is no second code path. `#skExtras` states the sidecar count BEFORE the click, since those files travel with it. The Connections tab is per-provider on purpose (the backends differ, and a merged list would invite a "sync everywhere" action that is actively wrong); the stdio Add button is a two-step arm showing the exact command that will run locally, disarmed whenever the command changes, and skipped entirely for http/sse where nothing executes. **Timestamps**: `addMsg(..., ts)` renders the row's `ts` as a right-aligned HH:MM (`.mtime`, full stamp in the tooltip); old rows without `ts` show nothing. **Live activity**: `activity` events append `.act-line`s (last ~6) to the seat's typing indicator (`typingActivity`); when the message lands, the ROW's persisted `activity` renders as a collapsed `.think-block` `<details>` ("X worked through N steps") above the body — replay included; everything escaped (command lines are arbitrary CLI text). **Live code viewer** (`#codePane` inside `#fileRail`): `kind:"edit"` activity marks the rail row `.editing` (provider-color border + ✎, ~6s expiry in the `editing` map) and auto-opens/refreshes the pane (`openCode`/`fetchCode` via `api.read_text`) — prev snapshot line-diffed (common prefix/suffix), changed band highlighted in the editing seat's color + scrolled into view, ~700ms poll only while a seat is mid-turn (catches Gemini, which has no stream) — the re-render guard compares CONTENT, not mtime, because the poll and the edit event race for the same write and a second render of identical text erases the highlight the first one just drew (caught only by driving the real UI, 2026-08-17), "follow" checkbox pins a file, non-image rail rows open here (OS-open fallback for binary). `resetEditing()` runs beside every `imgCache.clear()` — workspace boundary changes clear the map and close the pane. `scheduleFilesRefresh(ms)` takes a delay; edit activity uses 300ms. **Dictation**: `#micBtn` beside 📎 — hold to talk, a tap under 350 ms latches (click again to finish), Ctrl+Shift+Space toggles, Escape cancels; `onDictation` paints the button and `insertDictation` puts the text at the CARET in `#say` and never auto-sends. A soft outcome ("too short", "nothing heard") lives on the button for 4 s rather than littering the transcript with rows; only `error` becomes a system row. `applyDictationConfig` runs from the `pywebviewready` handler (TDZ rule) and leaves the button visible-but-disabled with the reason in its tooltip when the probe says no. **Alloy branding** (BRANDING.md): title/h1 "Alloy", wordmark = fixed trefoil (data-URI PNG from `branding/trefoil_v2.py`, never encodes seat count), empty-state h2 "Different metals. One alloy." with a DYNAMIC roster cluster (`renderEmptyRoster` — one dot per enabled seat, real provider colors), `--alloy` #F4B942 = app chrome (wordmark h1, Send, round badge, focus rings, checkboxes), `--josh` #C9B896 warm bone; provider colors stay participants-only. **Rounds are typable**: `#rVal` is a real `<input>` (not the old `<b>` written with `.textContent`) — `type="text"`+`inputmode`, never `type="number"` (WebView2 draws duplicate spinners and reports `""` for partial input, which makes clamp-on-blur lie). `commitRounds` clamps on commit and REPAINTS the clamped value so a refused number is visible; `syncRoundsCtl` skips the write while the box has focus, and the boot paint lives at the END of the script with `addSeat(…)`. `openChat` no longer pre-paints before `restoreOrchestration` restores the numbers. **Keep Improving** is a fifth preset card whose selection opens `#contModal`, the warning modal: check-in interval + the Automatic/Notify/Ask-permission radio, three independent limit rows (spend, hours, may-the-check-in-stop-it), the verification command and commit checkboxes, and an acknowledgement checkbox that is the ONLY thing enabling OK — whose wording changes when every limit is off, because a run nothing can stop is a different promise. Cancel/Escape/backdrop revert the preset rather than leaving it selected. `contOn`/`contCfg` are the single source of truth (`continuousCfg()` builds the payload, nulls meaning no limit); `#contSummary` states the configuration under the cards, `#contStrip` shows objective/wave/spend/next-check-in live, and a `notify` check-in adds a dismissible `#contBanner`. `restoreContinuous` puts a reopened chat back truthfully and `resetStage` clears it. **Narration rendering** (2026-08-26): `actLineHtml(a)` is the ONE renderer for a step - live indicator and finished row both - so the two cannot drift apart from the adapters. `ACT_ICONS` gives each kind a glyph (command deliberately has NONE, because its text already starts `$ `; the lookup uses `kind in ACT_ICONS`, not `||`, or an intentionally empty icon falls back to the generic dot). `say`/`reasoning` render as italic prose rather than monospace - they are sentences, not command lines - and a `result` is dimmer and indented under its call so "searching ... / found 3" reads as one step; a `result` starting "failed" turns red. The live log keeps `ACT_LOG_MAX` (14) lines and SCROLLS instead of dropping at 6, and the typing header gained `N steps - on this M:SS` (`dataset.steps`/`stepat`, incremented only for real steps - a progress tick is a stopwatch, the same rule the engine's sink follows), because total age alone cannot tell a seat grinding through 40 tool calls from one wedged on its first. **Relay-busy rows** (2026-08-25): `workingEls` (keyed by the engine's token id, so an open emitted before a chat had an id still pairs with a close emitted after) renders `.working` rows in the feed beside the typing indicators - alloy chrome, never a provider colour, because this is the APP working and not a participant - sharing the typing ticker for their age clocks and re-appended below each new message like `typingEls`. `WORK_GRACE_MS` (450) is the whole reason it is not noise: a row is not painted until it has been open that long, so a 40 ms moderator pick shows nothing and the 90-second plan stands out. `hideAllTyping` clears them (a spinner that outlives its run is worse than none), and `openChat` replays `r.working` the way it replays `r.thinking`. **Branching + rail extras** (2026-08-25): every persisted message row carries a ⑂ button (two-step arm) that forks the conversation up to and including that row via `api.fork_session` and opens the fork; every rail row gains ★ pin (`pinnedChats()` in localStorage, a "Pinned" group ranked just under "Needs input" — a question Josh must answer still outranks a chat he merely likes) and ⤓ export (`api.export_session` → `open_path` into the browser); rail tooltips show "branched from …" from `session_summary.fork_of` (additively allowlisted in RAIL_SUMMARY_FIELDS); `#soundBtn` toggles the engine's sound cues with the choice in localStorage. **Browser control** (2026-08-26) mirrors the desktop block one group down — `#browserMode` picker, `#browserSites`/`#browserSiteList`, `#browserNote`, the `#brwsModal` acknowledgement gate for the unattended rung (registered in all THREE places: the `display:none` list, the `.show` list, and the one Escape listener, with the same `.contains("show")` guard because closing REVERTS the picker), `browserPrev`/`restoreBrowser`/`browserSiteList`/`syncBrowserNote`, the two `.disabled = seated` lines, and the `browser`/`browser_sites` keys in BOTH cfg builders plus both restore call sites. Its own `browserPrev` on purpose — a shared revert target makes cancelling one modal move the other picker. Unlike the desktop allowlist, the site field shows at EVERY live rung, because with no sites Chrome reaches nothing. `#rungAdvisory` + `syncRungAdvisory()` state the honest ceiling under both pickers whenever the permission mode is Workspace or Full access. |
| `launcher.ps1` | Console launcher (prompts for topic). ASCII-only on purpose. |
| `sessions/` | One folder per conversation: `transcript.md` (human log), `messages.jsonl` (UI replay), `meta.json` (resumable state, **v2**), optional default `workspace/`, `project-context.md` (the exact shared-context text the seats were given, when the chat used a custom working folder), and `say.txt`. Old transcript-only folders remain listable as legacy/view-only; v1 metas stay continuable. Spawned teams (tier 3) are ordinary sessions in here too — child meta carries `parent: {id, seat, label}`, parent meta lists `children` (hints only: a child can be deleted). |
| `tests/` | Token-free test suites, each a runnable script (`python tests/test_loop.py`): `test_loop` (shared loop), `test_scheduler` (meta v2 + resume), `test_modes` (directives, speaker, moderator), `test_until_done`, `test_parallel`, `test_free`, `test_spawn_tier1/_helpers/_teams`, `test_brief` (shared project context), `test_ask` ([[ASK]] questions to Josh), `test_bridge_files` (file/image viewing bridge: workspace confinement incl. `..`/absolute/junction escapes, MIME + size cap, thumbnails), `test_capabilities` (who-can-do-what routing), `test_skills` (skill authoring/sync + MCP management), `test_activity` (streaming runner via `python -c` children, adapter activity mapping for all three CLIs incl. the 2026-08-26 commentary and tool-result kinds, sink dedupe/cap/confinement AND the one-slot `say` hold that keeps a turn's final prose out of its own log, loop + persistence, read_text bridge; the checklist half lives in `test_todo`), `test_app_headless` (real `app.Api` + fake window), `test_ui_boot` (EXECUTES `ui/index.html`'s inline script in node against a stub DOM — the only suite that can see a top-level JS throw, and the suite that drives a `dictation` event into the real composer; skips where node is absent), `test_dictation` (recorder state machine incl. the stop-during-open race, transcriber seam, probe honesty, bridge round trip), `test_continuous` (Keep Improving: policy normalization incl. all-limits-off, the unbounded ceiling, objective rollover, the closed remedy set, the three check-in actions incl. an unanswered one meaning SKIP, gate red/green/dirty/skip with `_gate_run`/`_git` stubbed, and the revival loop driven through a `_run_rounds` seam — a real continuous loop has no cap and would never return, which is exactly the property the revival layer exploits)), `test_watchdog` (the turn watchdog: a talking child outliving a window that would have killed it, the silence clock restarting on every line, stderr counting as liveness, the optional hard cap, per-adapter defaults, and probation reaching the ARMED window), `test_resilience` (what happens when the PROVIDER wobbles rather than Alloy breaking: `transient_error` classification, the backoff + probation window driven through the real loop, and `was_interrupted` — the killed-mid-run signal auto-resume keys on). `test_branching` (bridge level: real `Api.export_session`/`Api.fork_session` incl. the running-chat refusal, session_summary's `fork_of`, and the sound-cue path through the one emitter thread with `_play_cue` stubbed), `test_rooms` (saved room templates: store round trip/overwrite/trim-tie-break + bridge + UI markup guards), `test_hooks` (event hooks: config round trip, unknown-name rejection, env contract, timeout/swallow, emitter-thread non-blocking, wave_gate's new `gate` event), `test_auto_title` (the once-per-session title side call through the `LoopIO.auto_title` seam), `test_mention` (@-mention routing through `enqueue_josh_message` across all drain sites), `test_budget_bar` (usage event payload honesty, projection math edges, strip rendering), `test_working` (the relay's own busy indicator: the context manager's pairing/uniqueness/never-break contract, the CLI wording, and the real wired sites - moderator, planner, gate, auto-title, compact - each driven with a stubbed side agent), `test_desktop` (computer use: the five staleness refusals, LRU/expiry, RuntimeId re-resolution, both scroll paths agreeing via one shared table, type rollback incl. the failed-restore wording, coordinate-outside-rect, the self-approval refusal covering OBSERVERS too, password refusal + override, redaction, process-identity bracketing, truncation announcing itself, and a source grep proving no SendInput/SetForegroundWindow — all behind a FakeBackend, zero hardware), `test_desktop_mcp` (delivery: the four rungs, the allowlist, the approval channel's fail-closed paths, the argument fence proving the model cannot flip allow_password/strict_pixels, the meta round-trip, and a RED guard that a standing TURN verdict never answers a desktop request), `test_permissions` (the rung ladder AND — since 2026-08-26 — the app bridge: real `app.Api._conversation`/`_continue`/`open_session` with adapter subclasses whose only fake is `turn`, so the assertions read the SHIPPING `build_cmd`), `test_browser_mcp` (web use, behind a FakeVendor so no node and no Chrome: the fence argv incl. never-a-blocklist / empty-means-deny-all / the exact flag spelling, the site classifier's refusals and its resolution-based loopback detection, the self-test latching a session dead when the fence is absent, the reconciliation gate dropping a tool whose REQUIRED argument was renamed, the four rungs, the approval round trip, the argument fence, upload confinement, and the relay axis incl. the clamp and the advisory ceiling — twenty-three of its rules RED-verified by removing them and watching the suite fail, and a BRIDGE section driving the real app.Api). `test_solo` (**the solo seat**: the real loop at n=1 proving no turn is ever handed an empty prompt, the preamble's solo voice with a RED guard that the multi-AI sentence never returns, the one seat-count table shared by the CLI/bridge/loops, free and battle refusing by name in the ENGINE, resumption through continue_block/rehydrate/session_summary, the solo Supervisor wave, and a BRIDGE section driving the real `app.Api` — 39 rules RED-verified across three passes). `test_artifacts` (**produced-file chips**: the descriptor gate incl. the DIRECTORY case that `getsize` alone lets through, `extra_paths` confined/verified/deduped like any streamed path, the commit_reply wiring, export.py as the second renderer, and a `test_ui_boot` probe driving the REAL `message` event AND the replay call), `test_memory` (**Wave 3's store, its injection, and the three commands**: the scope key's two normcase halves, the positional header parse driven by an ORDER assertion, the cross-process lock under real threads, an oversized file refusing to be rewritten, Josh's notes surviving eviction, the global-SEAT note that must not reach a project chat, the three budget constants' identity, every brief status charged by its content, a note reaching a real seat's first prompt through the REAL loop, /forget's STATELESS arm proved by asserting nothing was deleted, and a BRIDGE section where forget_memory refuses a scope the chat cannot see; 42 rules RED-verified), `test_confinement_parity` (**the two copies of the workspace-confinement rule**: one table of escape cases — `..` hops, absolute-elsewhere, junction/symlink escapes, case-differing roots, drive roots, empty, non-str — fed to BOTH `relay.confine_to_workspace` and `browser_mcp._confine`, asserting identical verdicts AND identical resolved paths, plus a line-anchored guard that browser_mcp.py stays standalone and a check that each docstring names its twin), `test_telemetry_truth` (**the cumulative-token lie**: the real `CodexAgent.parse` driven with the exact bytes a live `codex exec --json` returned, the baseline surviving save→rehydrate, `forget_thread` pairing the baseline to the session id, `basis_versions`, `wall_ms` riding an existing usage dict and never creating one, and the usage pill through the real script — 22 rules RED-verified), `test_folding` (**W1.6 folding + W1.7 reaction notes**: the three-state note through outcome/bridge/export, the export dropping the stored ts so the byte-identical test holds, the directive refusal and its stated reason, alt-click scoped to one speaker, find unfolding the row it landed in, and a BRIDGE section driving the real `app.Api`; 25 rules RED-verified), `test_stats` (**cross-session stats + the playbook UI**: the two measured cache conventions and what each would cost if the other were used, the 559-million-token exclusion and its per-session counting, outcome.py carrying cached_tokens/wall_ms/basis_versions, absence staying None through every layer, retro's pin/dismiss/restore incl. the unpinned-wording refusal and a dismissed rule leaving `playbook_block`, a BRIDGE section driving the real `app.Api`, and a `test_ui_boot` probe over the real stats and playbook events; 27 rules RED-verified), `test_context` (**the per-seat context readout**: `_context_used` summing the cache fields the plan's spec would have missed, the subagent filter driven with a real non-None `parent_tool_use_id`, the window read from the CLI's own modelUsage and withheld when several models ran, OpenCode's cached half, codex and gemini staying honestly blank, the reset driven through the REAL `Agent.turn` against a `python -c` child, the row/export/UI renderers, and two source guards for defects only a browser could see; 23 rules RED-verified), `test_todo` (**the per-seat todo strip**: the shapes MEASURED from real captured stdout on 2026-08-27 — claude's incremental TaskCreate/TaskUpdate/TaskList incl. the resume that makes the state per-thread, codex's todo_list snapshot incl. the `item.updated` events the gate was dropping, opencode's `todowrite` and its echoed output, gemini's honest blank — plus the no-complete-list-no-strip rule, the sink's one-slot always-last parking, export.py as the second renderer, and a `test_ui_boot` probe driving the REAL activity and message events through the trim the plan warned about; 28 rules RED-verified). 2221 tests, no CLI calls, no tokens — `test_loop.py` exports the shared `FakeAgent`/`RecordingIO`/`build_state` helpers the others import. |
| `make_icon.py` / `ai-chat.ico` | Icon generator (Pillow) and the generated icon: the **Alloy trefoil** (BRANDING.md, approved 2026-08-16) — geometry lives in `branding/trefoil_v2.py` (parametric curve, crossings solved numerically, depth-sorted over-arcs; the over-arc redraw must outrun its erase band by more than the erase cap radius or a notch punches the glow), every .ico size regenerates from that one function, <32px uses `small_mark`'s heavier weights. `branding/trefoil-v2-comparison.png` is the approved proof sheet. Brand rename is UI-surface-only: repo/CLI/skills stay `ai-chat` on disk. |

Also installed elsewhere: `ai-chat` skills in `~\.claude\skills\ai-chat\` and
`~\.codex\skills\ai-chat\` (so either AI can run conversations on request).
If paths here change, update those skills + the desktop shortcut + `~\.local\bin\ai-chat.cmd`.

## CLI knobs (relay.py)

`ai-chat "topic" --turns N --agents claude,gpt,gemini --start X
--permission read_only|ask|auto|full (`--yolo` is an alias for `full`)
--turn-cap MINUTES (absolute ceiling on ONE turn; default none — a turn
runs until the work is done and is cut off only if the CLI goes silent)
--claude-model <id> --claude-effort low|medium|high|xhigh|max
--gpt-model <id> --gpt-effort low|…|ultra --gemini-model <agy slug>
--gemini-effort low|medium|high (normally baked into the slug)`
Claude ids (all verified on this account): claude-fable-5, claude-opus-5,
claude-opus-4-8, claude-sonnet-5, claude-haiku-4-5 (aliases opus/sonnet/haiku ok).
Defaults: all three agents, 10 rounds, Opus 5 / gpt-5.6-sol(high) / gemini-3.7-flash-high.
The app reads live model lists: GPT from `~\.codex\models_cache.json` (+ defaults
from `config.toml`), Gemini from `agy models`; Claude list is pinned in app.py.

**ONE `--agents` token runs Alloy as a harness for that single agent** (see
§ Solo seat): `--agents claude`, `--agents ox:opencode/x-preview-f-free`. With
one seat a "round" is simply a turn, `--start` is ignored, `--mode free` and
`--mode battle` refuse by name, and the app hides Talk Live / Compare & Decide
/ Arena Duel behind a stated reason. Everything else — permissions, computer
use, web use, workstreams, the files rail, fork, export, usage, resume — is
inherited unchanged.

**Computer use and web use** are two further axes, each independent of
`--permission` (that one bounds the WORKING FOLDER; these bound Josh's screen
and the open web). `--desktop off|ask|allowlist|full` with repeatable
`--desktop-app REGEX`; `--browser off|read|ask|full` with repeatable
`--browser-site URLPATTERN`. Both default off and read anything unrecognised
as off. Browser specifics: the site list is an ALLOWLIST handed straight to
Chrome, so **an empty list reaches nothing** and anything unlisted (including
`file://` and this machine's own ports) is blocked inside Chrome's network
stack; a pattern naming `file:`/`chrome:`/`devtools:`/`data:`/`javascript:` or
Alloy's own webhook port is REFUSED out loud rather than narrowed, and a
refused pattern caps the rung at `ask` while an unusable list caps it at
`read` (`clamp_browser_rung`). Cheap live check:

```
python relay.py "open https://example.com and report its heading" --turns 1   --agents claude:claude-haiku-4-5:low,claude:claude-haiku-4-5:low   --browser read --browser-site "https://example.com/*"
```

Verified end to end 2026-08-26 with real seats: the seat connected to
`alloy_browser`, navigated, snapshotted, found the link's uid, attempted a
click and reported Alloy's look-only refusal accurately. Only **claude** has a
proven per-invocation MCP route (`--mcp-config` inline JSON), so browser and
desktop control are claude-seat capabilities today, not four-seat ones -
`capability_note()` must never imply otherwise.

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
supervisor|parallel|free|panel|battle` (speaker = seats end replies with `[[NEXT: seat]]`; moderator =
a stateless cheap side call picks each turn, `--moderator provider[:model
[:effort]]`, default claude:claude-haiku-4-5:low, can answer DONE; parallel =
simultaneous barrier rounds; free = seats reply whenever messages arrive,
FREE_MAX_LEAD throttle). `--until-done --ceiling N` = no round cap, wrap-driven
end with a hard turn ceiling (default 60, `/ceiling N` mid-run). Spawning:
tier 1 native CLI subagents on by default (`--no-native-subagents` to hide);
`--spawn-helpers N` = seats may play `[[SPAWN: provider[:model[:effort]] |`
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

**Keep Improving** (`--preset keep-improving --continuous`, app: the fifth
preset pill + its warning modal) is Build Together with the three brakes off.
It has no round cap and no turn ceiling; when the manager judges an objective
met it chooses the NEXT one itself (`[[OBJECTIVE: ...]]`) and keeps working,
and a run that falls over is restarted. What ends it: Josh's Stop, and
whichever limits he set — `--spend-cap USD`, `--time-cap HOURS`,
`--no-watchdog-stop`. All three off is a legal, deliberate choice and the
modal says so in those words. `--checkin-minutes N` (5-1440) schedules a
watchdog that checks the run is still committing turns and repairs it when it
is not; `--checkin-action auto|notify|permission` decides whether Josh is
told, flashed at, or asked first (permission makes the run WAIT). `--gate CMD`
/ `--no-gate` / `--gate-commit` verify each round of work in the working
folder BEFORE the manager reviews it, and checkpoint green ones to git.
Mid-run: `/limits`, `/checkin`, `/objective <text>`. Cheap live check:

```
python relay.py "make this small project better" --preset keep-improving   --continuous --agents claude:claude-haiku-4-5:low,claude:claude-haiku-4-5:low   --spend-cap 0.40 --time-cap 0.1 --gate-commit --workspace <a throwaway repo>
```

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

**Battle (Arena Duel, added 2026-08-25):** mode `battle`, exactly two seats
(app refuses otherwise), one barrier round answered UNSEEN — isolation is
`commit_reply(fan_out=False)` plus rows stamped `intent:"battle"`, the same
mechanism as panel drafts. The run then ends on purpose
(`termination_reason: "battle_vote"`); `Api.vote_battle` records the verdict
in meta AND moves Elo in `sessions/leaderboard.json` (model-level keys
`provider:model`, K=32, tie=half, "both bad" counts without moving ratings),
emits `battle_revealed`, and every later continue rides ordinary
run_parallel. Blindness lives in the UI on purpose (`addMsg`/`showTyping`
mask via `battleCtx`, seat cards blur; dataset.real* restores on reveal) —
messages.jsonl always held the truth; this is honesty paint, not security.
Registration is THREE tables or normalize_orchestration silently rewrites
the mode back to round_robin on first save: MODES+IMPLEMENTED_MODES,
LEGACY_ORCHESTRATION, and a forcing branch keyed on workflow=="battle".
Known v1 edge: a crash MID-blind-round resumes through run_parallel, so one
seat may answer twice.

**Reactions (added 2026-08-25):** per-message 👍/👎 land in outcome.json's
`human_feedback.reactions` keyed by message_id through `outcome.set_reaction`
(the single validator). The end card and reactions preserve each other in
BOTH directions on purpose — set_feedback carries `reactions` forward,
set_reaction merges into the existing feedback dict — because they are
different questions about different scopes, and write_outcome's
any-non-empty-value rule keeps both across rebuilds with no special case.
UI: instant-toggle buttons on persisted seat rows (no arm; reversible, like
archive), repainted on reopen from `Api.get_reactions`.

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

## Solo seat — Alloy as a harness for ONE agent (2026-08-27)

Josh: *"make it so that we can use a single agent of whoever we want. Pretty
much just our own harness for whatever agent we want. Just like deepseek
harness or traycer."* **No new mode**: the seat floor drops to one and the
existing recipes adapt. Every `mode` value on disk is unchanged, so meta,
replay, forks and saved rooms stay valid.

```
python relay.py "make this better" --turns 3 --agents claude:claude-haiku-4-5:low
```

- **The measurement that scoped it was WRONG, and a FakeAgent is why.** An
  earlier probe drove the real `run_rounds` with one FakeAgent across six
  modes and concluded five of them worked. They did not. A multi-seat backlog
  is refilled by `commit_reply`'s fan-out to PEERS; with one seat nothing ever
  fans in, so from turn 2 `compose_prompt` returned **the empty string** —
  measured `[1106, 0, 0]` chars for a 3-turn run. Against a real CLI that is
  `claude -p ""`, measured exit 1, *"Input must be provided either through
  stdin or as a prompt argument when using --print"*. The loop reads that as a
  provider failure, retries into the identical wall, parks the only seat and
  prints "Every seat has failed twice this run" — so a solo chat took ONE
  useful turn and died looking like a broken CLI. A FakeAgent answers happily
  whatever it is handed, which is exactly why it saw nothing. `SOLO_CONTINUE`
  (and `IDLE_CONTINUE`, its n>1 twin for a room whose peers all went quiet) is
  the fix, and `parts` is now never empty on any path.
- **Four seat floors, not two.** `relay.main()` and `app.Api._conversation`
  were the obvious ones. `ui/index.html`'s `sendSay` is an INDEPENDENT copy
  that fires before the bridge is ever called, and `relay.continue_block` is
  the silent one — it feeds `session_summary`'s `can_continue`, which drives
  `setSeated`, the composer's continue branch and the rail tooltip, and
  `rehydrate` RAISES on it. Left at two, every solo chat would start fine and
  then be permanently view-only, with typing into it silently starting a brand
  new conversation. `parse_team` was a fifth (a solo seat spawning a solo
  sub-session IS the sub-agent shape).
- **`MODE_SEAT_LIMITS` + `seat_count_refusal(mode, n)` is the ONE table.** Only
  two modes cannot mean anything at n=1: `battle` (a blind A/B vote over one
  answer) and `free` (its coordinator pauses on its FIRST pass at fewer than
  two live seats, so the run ends before the seat ever speaks). Both now refuse
  UP FRONT and by name, in `main()`, in the bridge, and in `run_free` /
  `run_battle` themselves — `run_battle` had NO engine-side guard at all, only
  app.py's, so a CLI battle at n=1 ran the blind round and stopped for a vote
  claiming two answers existed. Their reason is `termination_reason:
  "seat_count"`, deliberately distinct from `starved`: starved means seats
  died, seat_count means the run was never runnable and nobody was ever asked
  for a turn.
- **`panel` is a FRONT-END policy, not an engine invariant.** It runs solo
  (draft → self-critique → synthesis is a real technique) so the engine allows
  it; the app declines to sell "Compare & Decide" to a roster of one. The CLI
  and the Advanced drawer still reach it. `moderator` is the same shape: hidden
  at n=1 because it spends a real side call per turn to choose among one, but
  reachable, and `MODERATOR_PROMPT_SOLO` tells it the truth (the pick is
  forced; the only judgement left is whether the work is finished).
- **The solo preamble is its own voice, not a patched group one.** The group
  version was not merely padded — it opened *"You are Claude, in a live
  multi-AI conversation with ."* (a sentence naming nobody), promised relayed
  peer messages that never arrive, and then instructed the only participant to
  *"talk to the other AI(s), not to him"*. `solo = not list(others)` swaps the
  header, the human line, the workspace and privacy lines, the cap line
  ("turns", and wrap when the WORK is done rather than when the topic is
  exhausted), the ask block (asking Josh is the normal loop, not a rare
  exception), the role block, and the reply-shape rule — "a few paragraphs at
  most, no markdown headers" keeps a three-way transcript readable but fights a
  harness whose deliverable is a plan or a file walkthrough. `order_line` is
  simply ABSENT from the solo return, which is also how `[[NEXT]]` stops being
  offered to a seat that could only ever nominate itself.
- **`capability_note`'s block was suppressed at n=1 — and that silently
  dropped `advisory_rung_note()`**, the honest admission that at `auto` or
  `full` access the desktop and browser ladders are a guardrail rather than a
  boundary. A solo harness is exactly the configuration that admission was
  written for. The block is restored under its own header, without the
  hand-it-over-to-a-peer rule, which has no referent.
- **The Supervisor at n=1 is the flagship** (planner + one executor +
  `wave_gate` + `gate_commit` — the Traycer shape). `SUPERVISOR_VOICE` is a
  two-entry table filling `{intro}` and `{teamwork}` in ONE prompt template, so
  rules 1/2/5/6 — the grammar and the do-not-ask-clarifying-questions lesson —
  cannot drift between the shapes. The measured reason it matters: rule 4
  ("one task per seat to start with") caps every solo wave at a SINGLE task,
  degenerating the rolling manager into plan → one task → review → plan at one
  billed supervisor call per task, exhausting `SUPERVISOR_MAX_WAVES` after ~6.
  One owner does not mean one task: `workstreams.next_assignments` starts at
  most one task per owner and `settle_workstream` immediately starts the next,
  so a wave of several runs through in order under ONE review.
- **Front-end wording follows the roster, not just the dots.** `rosterChanged()`
  (called from `refreshSeatNames` and the Include checkbox) repaints the mode
  pill's label, the Advanced drawer's recipe sentence and the rounds/turns
  label, because a stage can go solo while the composer still offers "Discuss
  in Turns" and describes what "every AI" will do. Unavailable modes are
  **disabled with a stated reason**, never hidden — the browser work's rule
  that a withholding is stated rather than left as an absence.
- Live-verified 2026-08-27 end to end, three times: a 3-turn CLI run where a
  real haiku seat created a.txt, b.txt and c.txt one per turn (turns 2 and 3
  riding `SOLO_CONTINUE`) and wrapped on its own word; the same shape through
  the APP BRIDGE with a real seat, reaching `started`, writing its file, and
  reopening as continuable; and a re-run after the review fixes below.
- **Then four adversarial lenses were pointed at it** (over-claim / the-control-
  that-does-nothing / the-UI-is-one-inline-script / engine-correctness) and
  they were right about a lot. **Two of the defects were mine and REGRESSED
  SHIPPING MULTI-SEAT BEHAVIOUR**, which is the part worth remembering:
  1. **A general guard met a caller for whom "empty" was correct.** The
     never-hand-a-CLI-an-empty-prompt fix fired on every Panel critique and
     synthesis turn, because panel commits with `fan_out=False` and
     `_panel_prompt` supplies the whole prompt itself. So the flagship
     multi-seat "Compare & Decide" preset began every critique with "the other
     participants produced nothing" directly above those participants' drafts,
     and offered `[[WRAP]]` one line above "Do not use [[WRAP]]". `filler=`
     now says who owns the body. tests/test_panel.py could not see it: nothing
     asserted what a phase prompt STARTS with.
  2. **A fallback keyed on "has no rule" instead of "has no recipe".** The UI's
     `presetSeatRefusal` gained a fallback so a hand-tuned "custom" recipe is
     judged by its live mode — and because the three solo presets carry no
     rule of their own, they inherited whichever rule the live mode had. The
     ordinary gesture of removing a seat while Talk Live was selected greyed
     out ALL SIX rows of the mode pill, each labelled with Talk Live's reason,
     leaving no clickable way out.
  The rest: a moderated solo room kept running with its OFF switch hidden and
  a sentence denying the moderator existed; the empty-state headline claimed
  "every tool Alloy has" on a stage keyed only on seat COUNT, which is false
  for the three providers `MCP_DELIVERING_PROVIDERS` excludes; `seat_count`
  never reached outcome.json because the loop's generic `starved` return
  overwrote it; `[[TEAM: claude | mode=free | …]]` became parseable and the
  refused child was still asked for a REPORT, handing the requester a forged
  account of work that never happened; one refusal sentence explained an n=1
  problem to someone who brought three seats; the solo workspace line said
  "write files freely" at `read_only`; a roster change wiped the badges
  explaining an adjustment the user had just been shown; and `panel_review`
  was refused by the UI while the engine, the CLI and the drawer's own
  sentence all allowed it — so it is now OFFERED under a solo name that says
  what it does ("Draft, Critique, Finalise"). Every fix is RED-verified:
  **39 rules across three passes.**
- Not done on purpose: the Advanced drawer's static `<option>` labels ("Laps —
  everyone speaks once per lap", "The AIs themselves") still read for a crowd.
  That drawer is the expert surface and `#policyReason` above it now carries
  the solo explanation.

## Wave 1 — seeing the run (2026-08-27)

- **Produced-file chips (W1.1).** `artifact_descriptors` had been stamping a
  verified `artifacts` list on every seat row since it shipped and
  `ui/index.html` had never read the field — the engine computed the answer
  and threw it away. `artifactChips(container, arts, provider)` renders one
  clickable chip per file under the reply, below the image strip; paths are
  workspace-RELATIVE, which is exactly what `read_text`/`read_image` take, so
  a chip click reuses `openCode`/`openLightbox` and no absolute path ever
  reaches the UI. `provider` is PASSED IN rather than read back off the row,
  because a masked battle row has had its provider deliberately cleared and
  the chips must not restore the colour the mask just removed. Absence stays
  silent. `export.py` renders them too (text, never links — an export travels
  away from the machine that holds the workspace): it is the SECOND renderer
  over these rows and it already read the sibling `activity`/`usage` keys
  while dropping this one. `artifact_descriptors` gained `extra_paths`,
  through the SAME gate (confine → isfile → normcase-dedupe) rather than
  appended raw, because **a Gemini seat structurally could not produce a
  chip**: agy has no activity stream at all, and `harvest_images` copies the
  turn's images into the workspace itself, so a file that really existed had
  no route into the list. The isfile gate is the only thing standing between
  a seat naming a DIRECTORY and a chip offering to open one — `getsize`
  answers happily for a folder on Windows, so a missing path is caught twice
  over and a directory is not.
- **Telemetry truth (W1.3): GPT's token counters are thread-CUMULATIVE and
  were being summed.** Measured, not inferred — a live `codex exec --json`
  run of three one-word turns returned `input_tokens` 16194 → 34244 → 52313
  and `output_tokens` 5 → 11 → 17. Every counter restates the whole thread,
  so `record_usage` counted turn 1 once per turn thereafter: 102,751 input
  tokens for three replies that cost 52,313, and a real chat on disk reached
  40,428,770 on one row and 534,655,991 summed. `usage_delta(baseline,
  totals, session_id)` differences them; `Agent.usage_baseline` persists in
  the seat's meta beside `session_id` and rehydrates with it, because Alloy
  resumes constantly and a memory-only baseline would re-count the whole
  thread once per reopen. Live-verified end to end: a 2-turn solo GPT run
  recorded 33,348 + 33,886 = 67,234, exactly the thread's own total (the old
  code would have summed 100,582).
  - **The plan named only input tokens; OUTPUT is cumulative too**, and the
    same event exposed a third bug: the field is spelled
    **`cached_input_tokens`** and `_extract_usage` looked only for
    `cached_tokens`/`cache_read_input_tokens`, so every GPT turn ever taken
    recorded 0 cached tokens while 11,008+ were arriving.
  - **A baseline is the only route**, and that is measured too: the codex
    binary does define `struct TokenUsageInfo with 3 elements:
    total_token_usage, last_token_usage, model_context_window`, but that
    struct belongs to the app-server protocol and never reaches
    `exec --json`, which emits exactly ONE usage event per run
    (`turn.completed`) carrying no `total_tokens` at all.
  - **`Agent.forget_thread()`** is the pairing made structural:
    `session_id` and `usage_baseline` are two halves of one fact ("where this
    seat's provider thread had got to") and the three sites that discard a
    live thread (`/clear` sequential, `/clear` free-mode, `compact_agent`)
    call it. `fork.py` drops the baseline beside the session id and the
    `introduced` flag. The surviving bare `agent.session_id = None` lines are
    the throwaway side-call adapters and all say "stateless by design" — a
    test asserts that, so a new bare clear is a live thread being
    half-forgotten.
  - **`wall_ms`** is Alloy's own clock on the whole child process, kept apart
    from the CLI's self-reported `duration_ms` (claude sends one, codex sends
    none — so every GPT row showed no time at all until `formatUsage` gained
    the fallback). It rides an EXISTING usage dict and never creates one:
    manufacturing a dict to carry a wall time would give Gemini and OpenCode
    a `by_seat` entry reading "0 tokens" exactly where the budget bar renders
    an honest blank. TTFT is deliberately NOT shipped — the first line out of
    these CLIs is the process booting, not the model answering.
  - **`USAGE_BASIS`/`basis_versions`** label how a number was arrived at, and
    it is a SET rather than a stamp because a chat continued across the fix
    genuinely mixes bases: totals already present when the label arrived are
    seeded as `1`, and a chat that had spent nothing is not called mixed.
    History is never rewritten.
  - **Only claude and codex write `last_usage` at all** (a test greps for it).
    OpenCode's `step_finish` token count feeds the live activity progress
    line and nothing else, so the plan's "gate the ox half on its own
    measurement" resolves to: there is no ox telemetry to fix, and adding it
    would be new scope.
- **Per-seat todo strip (W1.2): three CLIs, three different checklists, and
  the one the plan named does not exist here.** The plan said to add a
  `TodoWrite` branch to `ClaudeAgent._describe_tool`. Captured stdout says
  claude 2.1.233 exposes **no `TodoWrite` at all** to this account — its
  `system/init` tool list carries `TaskCreate`/`TaskGet`/`TaskList`/
  `TaskUpdate` instead (the `TodoWrite` string IS in the binary, so the
  branch is kept as a documented, defensive, UNMEASURED read). What ships is
  one canonical shape — `TODO_STATES` (`pending`/`active`/`done`),
  `_todo_state` to normalize each CLI's vocabulary into it, and `_todo_act`
  to build the one act — fed by three very different adapters:
  - **claude is INCREMENTAL**: no event carries the whole list, and the
    number identifying a task appears for the first time in the RESULT text
    of the `TaskCreate` that made it (`"Task #1 created successfully: …"`),
    so the state is accumulated across the turn. It also **survives a
    resume** (measured: a second turn's `TaskList` returned both tasks the
    first turn created), which puts it in the same family as `session_id`
    and `usage_baseline` — so it is per THREAD, not per turn, and
    `forget_thread()` drops it. `TaskList` results (`#2 [in_progress] beta`)
    are parsed as a full reconciliation, which is the only route back to a
    complete view.
  - **codex is a SNAPSHOT**, on `item.started` → `item.updated` ×N →
    `item.completed`, each carrying `items: [{text, completed}]`.
    `item.updated` is a type the activity hook's gate never accepted, so
    **every checklist change after the first was being dropped on the
    floor** — the gate is opened for `todo_list` only, and a test proves it
    is not opened for everything (with a payload that would actually
    produce an act if it were; the first version of that test could not
    tell the two apart).
  - **opencode's `todowrite`** carries `{todos:[{content,status,priority}]}`
    and echoes the same list back as pretty-printed JSON in its output —
    which the generic result note rendered as `16 lines: [` directly under
    the strip, so that one tool's note is suppressed.
  - **gemini gets nothing**, honestly: agy has no stream at all.
  **No complete list, no strip.** A task first seen through an *update* was
  created before this process was watching — every reopened chat — and
  drawing a checklist from that view invents its denominator: one update on
  a five-item plan reads `plan 1/1 · all done`. The item is still tracked
  (a later `TaskList` names it), the strip is withheld, and the event is
  said as the one line it actually is. Same rule as W1.4's *no measured
  denominator, no ring*.
  **The sink keeps exactly ONE checklist and parks it LAST**, re-parking on
  every append. Both halves are load-bearing and the second was found by a
  live run: a plan that settles early in a 400-step turn is otherwise among
  the first things `[-ACTIVITY_KEEP:]` trims off the front — from exactly
  the turns the strip exists for. The consecutive-repeat check therefore
  compares the previous STEP rather than `acts[-1]`, which stops being the
  previous step the moment a checklist is parked behind it.
  **The UI pins the strip OUTSIDE `.act-log`** (the plan's named trap:
  `ACT_LOG_MAX` removes `log.firstChild`), updates it in place by setting
  the inner half on one element it keeps, and does not count it as a step —
  a checklist is a state, the same rule the engine's sink follows for the
  token stopwatch. The finished row says `worked through N steps · plan
  1/3` without being expanded, and a reply that only planned reads `planned
  3 steps · 1 done` rather than claiming work it never did. `export.py`
  renders it as its own list. 28 rules RED-verified.
- **Honest per-seat context readout (W1.4).** The plan said to publish the
  last top-level assistant event's INPUT-TOKEN COUNT, called
  `parent_tool_use_id` unconfirmed, and assumed there was no measured
  denominator. Captured stdout moved all three:
  - **`input_tokens` is not the context.** A real turn whose context was
    41,616 tokens reported `input_tokens: 8`; everything else arrives as
    `cache_creation_input_tokens` + `cache_read_input_tokens` — cached, but
    still in the prompt. `_context_used` sums all three. Publishing
    input_tokens alone would have said a seat had used 8 tokens of a
    200,000 window: the W1.3 bug one field over.
  - **`parent_tool_use_id` is confirmed, and the filter is load-bearing.** A
    native subagent's assistant events ride the same stream carrying a
    non-None id, and in the measured run the subagent sat at 21,184 while
    the seat itself was at 39,613 — so an unfiltered read understates by
    45% at exactly the moments a seat is delegating hardest.
  - **There IS a measured denominator**: the result object's
    `modelUsage[<model>].contextWindow` (200,000). Read per turn, not
    cached, because the result carries it every time. When several models
    ran and none matches the one that answered, there is no window — a
    guess BETWEEN two real windows is still a guess.
  - **OpenCode can answer too, with the identical trap.** Its
    `step_finish.tokens.input` counts only what was NOT served from cache,
    so the context is `input + cache.read + cache.write`; the measured
    series ran 13,717 / 13,943 / 5,445(+8,640 cached) / … and reading
    `input` alone would have shown the context SHRINKING by 60% the moment
    caching started working. Its denominator is models.dev's own `context`,
    which `ox_model_details` already caches. **codex cannot** (one usage
    event per run, summed over internal calls) and **gemini cannot** (no
    stream) — both stay honestly blank.
  - **`Agent.last_context` is deliberately NOT part of `last_usage`**: a
    level is not a spend, `record_usage` sums everything it is handed, and
    a seat whose CLI reports no tokens must be able to report a context
    without manufacturing a usage dict that would read "0 tokens" exactly
    where the budget bar draws an honest blank (the `wall_ms` rule one field
    over). It rides the ROW as its own `context` key, beside `artifacts`.
  - **NO MEASURED DENOMINATOR, NO RING** is enforced in both renderers: the
    seat card draws its bar only when a window came back, and says
    "14.7k in context" otherwise; the row pill reads `ctx 41.6k/200k` or
    `ctx 14.7k`; `export.py` writes "(no window reported)" rather than a
    percentage. A seat past `CONTEXT_TIGHT` (0.8 of a MEASURED window) turns
    red. `turn()` resets it, so a turn that measures nothing cannot
    republish the last one's number — the one path where only that reset
    stands between the reader and a stale figure, and a RED pass proved a
    hand-cleared test could not see it.
  - Two defects only a REAL BROWSER could see, both now pinned by source
    guards: `.msg-head` was `flex-wrap: nowrap`, so the extra pill pushed a
    row to 559px inside a 490px box and the transcript grew a horizontal
    scrollbar; and `.msg-usage.ctx-tight` written up with the seat CSS has
    EQUAL specificity to `.msg-head .msg-usage` and loses on source order,
    so the red pill silently never turned red. Same family as the `font:`
    shorthand: valid CSS, no error anywhere, no effect.
  - Live-verified across two real turns: 86,656 → 87,118 of 200,000.
    23 rules RED-verified.
- **Stats + the playbook's first UI (W1.5).** `stats.py` is a new standalone
  module (stdlib + `outcome`'s reader, like `export.py` and `fork.py`) that
  aggregates every `sessions/*/outcome.json` into per-provider and
  per-model rows: chats, turns, spend, prompt tokens, output, cache hit,
  wall time. Rows group by PROVIDER and by `provider:model`, never by seat
  id — a seat id means a different agent in every chat.
  - **The cache convention is PER PROVIDER, and that is measured.** claude's
    `cached_tokens` are DISJOINT from its `input_tokens` (a turn reported
    `input 10 / cached 86,646` and its context, derived independently from
    its assistant events, was exactly 86,656), so its prompt is
    `input + cached`. codex's are a SUBSET (turn 2 of a two-turn job
    reported `input 33,886 / cached 27,136`; under a disjoint reading that
    turn had 33,886 tokens of *fresh* material for one short reply), so its
    prompt is `input`. Using one formula for both is wrong by ~44% one way
    and ~1000x the other. Anyone else is unknown ⇒ no prompt size, no rate.
    `totals` therefore carries NO combined hit rate: it would average two
    different quantities.
  - **The old records still hold the pre-W1.3 lie, and a footnote under it
    is a caption, not a correction.** The real sessions folder aggregates to
    **559,310,306** GPT input tokens if you believe them. History is not
    rewritten, so `MIN_TRUSTED_BASIS = {"gpt": 2}` excludes those token
    counters (turns and costs still count, since neither was affected), a
    mixed chat is judged by its OLDEST basis, and the UI STATES how many
    chats were left out and why.
  - **outcome.py was dropping the two fields this needed**: it re-derives
    seat usage from rows and never carried `cached_tokens` or `wall_ms`,
    though every row has had them since W1.3 — the W1.1 shape again. It now
    also records `basis_versions` per seat, a SET for the same reason
    relay's is.
  - **A number nobody reported is a BLANK, never a zero**, from the row
    dicts (`None`, not 0) through `statCell` to a dimmed em dash. Gemini and
    OpenCode report no cost at all and a 0 in a spend column reads as "this
    was free". A measured zero still renders as `0%`.
  - **The playbook gets its first UI** — it had been steering every
    Supervisor plan through `playbook_block` with no way to see it.
    `retro.rules_for_display` is uncapped and carries `status` (so a
    dismissal can be undone); `retro.set_rule` is the one validated write
    path and applies ONLY the arguments passed, so dismissing cannot
    silently unpin. Editing a rule's wording is refused unless it is
    PINNED, because `merge_heuristics` overwrites an unpinned directive on
    the next refresh — an edit that looked accepted and was gone by morning.
  - `Api.get_stats` / `Api.get_playbook` follow the `recheck_auth` shape
    (ok at once, truth as an event) because both scan every session folder
    uncapped; `set_playbook_rule` is bounded file I/O and answers on the
    bridge thread like `save_skill`. `get_playbook` runs the FULL retro pass
    rather than a read, so the tab cannot show rules the planner is not
    actually using. 27 rules RED-verified.
- **Turn folding (W1.6) and a note on a reaction (W1.7).** The class is
  **`.msg-folded`**, never `.folded` — that one already means something on
  `.sup-wave` in the Supervisor control log, and two meanings for one class
  name is how a fold in one panel collapses a box in another. A folded row
  keeps one line of itself (`.msg-peek`), built from the row's OWN raw text
  stashed as `dataset.peek` rather than scraped back out of rendered
  markdown. Alt-click folds every row from that speaker (`dataset.foldKey`
  = the seat id). **A row carrying a `.dir-chip` refuses to fold** — a
  trailing directive is the line the conversation acted on, and the plan's
  two named cases collapse into that one check because `md()` peels an
  unanswered `[[ASK]]` into a `.dir-chip.dir-ask` on the row that asked; the
  refusal is SAID on the button rather than left as a dead control. And
  `scrollFindCurrent` unfolds the host first: a `display:none` body has no
  box, so both scroll paths would land nowhere.
  A **note** is Josh's own words about one reply, and it is THREE-STATE all
  the way down — `None` leaves an existing note alone, `""` clears it, text
  sets it — so the thumb buttons, which pass no note at all, can never
  delete what he typed. It belongs to its reaction: removing the thumb
  removes the note, and the row says so at the same moment. The editor is
  inline (`openNoteEditor`) rather than `window.prompt`, which this app uses
  nowhere. `export.py` gained `_read_reactions` and renders the thumb and
  the note but NEVER the stored `ts` — the export is byte-identical for
  identical input by design and a timestamp that moves whenever a thumb is
  re-clicked would break that for a fact nobody reads. Also fixed in
  passing: `.copy-btn` is `opacity: 0` until its row is hovered, so a reply
  Josh had already marked showed nothing at all until he happened to hover
  it (`.msg .copy-btn.on { opacity: 1 }`). 25 rules RED-verified.

## Hard-won gotchas (do not relearn these)

- **A sidebar button left out of the shared id-list selector renders as a raw
  browser default, and nothing but a real browser can see it** (found
  2026-08-27 while adding `#memBtn`). All the sidebar's bottom buttons are
  styled by ONE rule listing their ids, and `#statsBtn` was never added to it
  when W1.5 shipped the day before -- so "Stats & playbook" had been drawing
  as Arial 13.3px on white with a square 2px black border, between four
  correct siblings. `tests/test_ui_boot.py` executes the page but stubs the
  DOM, so it has no cascade at all; every other suite reads the file as text.
  `getComputedStyle` on the real page showed it in one call. The general test
  now reads the button ids straight out of the `<aside>`'s bottom group and
  asserts each appears in the rule, so the next one is caught for free -- and
  the rule carries a comment saying every button belongs in it.

- **A placeholder is a POSITION, not noise** (Wave 3, 2026-08-27). memory.py
  renders a note's header as `## <id> | <kind> | <who> | <when>` and writes
  `?` for a field it never recorded. The first parser filtered those
  placeholders out *before* reading the fields positionally, which shifted
  every later field one slot left: a note with no recorded author came back
  with its DATE as the author and no date at all, so it rendered as
  `- [2026-01-01, undated]` and sorted as though it had never been stamped.
  Nothing raised, the round trip "worked", and the only reason it was caught
  is that a test asserted the ORDER of three notes with known dates rather
  than that a write followed by a read returned three notes. The parser now
  reads by index, and a first field that is not one of our own kinds means
  "this line is not our rendering" -- unknown attribution beats attribution
  mis-assigned to whatever happens to sit in slot 2.

- **A status whitelist over `brief` silently missed four of its five spending
  statuses.** `brief_content_len` charges the project brief against the
  shared `PREAMBLE_CONTEXT_MAX`, and its first version gated on
  `status in ("quoted", "digest", "ok")` -- a vocabulary inferred rather than
  measured. `project_brief` actually sets `quoted` for the verbatim path and
  **`fresh` / `written` / `updated` / `readonly`** for the synthesized one, so
  every digest brief would have been charged ZERO and memory would have taken
  the full 4,000 on top of it: 7,500 chars injected where 4,000 was the
  promise. The gate was also redundant -- `off`/`none`/`failed` leave both
  content fields empty by construction, on the fresh path AND the resumed one
  (a failed brief writes no sidecar, so `read_project_context` finds none) --
  so it was DELETED rather than corrected, and the test that pins it asks the
  general question ("an unrecognised status is still charged its content"),
  not the five specific names.

- **A placeholder is a POSITION, not noise** (Wave 3, 2026-08-27). memory.py
  renders a note's header as `## <id> | <kind> | <who> | <when>` and writes
  `?` for a field it never recorded. The first parser filtered those
  placeholders out *before* reading the fields positionally, which shifted
  every later field one slot left: a note with no recorded author came back
  with its DATE as the author and no date at all, so it rendered as
  `- [2026-01-01, undated]` and sorted as though it had never been stamped.
  Nothing raised, the round trip "worked", and the only reason it was caught
  is that a test asserted the ORDER of three notes with known dates rather
  than that a write followed by a read returned three notes. The parser now
  reads by index and treats a first field that is not one of our own kinds as
  "this line is not our rendering" -- unknown attribution beats attribution
  mis-assigned to whatever happens to be in slot 2.

- **A status whitelist over `brief` silently missed four of its five spending
  statuses.** `brief_content_len` is what charges the project brief against
  the shared `PREAMBLE_CONTEXT_MAX`, and its first version gated on
  `status in ("quoted", "digest", "ok")` -- a vocabulary inferred rather than
  measured. `project_brief` actually sets `quoted` for the verbatim path and
  **`fresh` / `written` / `updated` / `readonly`** for the synthesized one, so
  every digest brief was charged ZERO and memory would have taken the full
  4,000 on top of it: 7,500 chars injected where 4,000 was the promise. The
  gate was also redundant -- `off`/`none`/`failed` leave both content fields
  empty by construction, on the fresh path AND the resumed one (a failed
  brief writes no sidecar, so `read_project_context` finds none) -- so it was
  DELETED rather than corrected, and the test that pins it asks the general
  question ("an unrecognised status is still charged its content"), not the
  five specific names.

- **`real.startswith(root + os.sep)` is not a containment check, and a
  docstring that CLAIMS parity is not parity** (found in the Wave 1 survey,
  fixed 2026-08-27). There are two copies of the workspace-confinement rule
  ON PURPOSE — `relay.confine_to_workspace` and `browser_mcp._confine`,
  because browser_mcp.py is a standalone stdio server spawned as its own
  process by a seat's CLI and its top-level imports are stdlib only, so
  importing relay would put an ImportError (a failure that says nothing to
  anybody) in front of a fail-closed security component. They had drifted
  FOUR ways while the second one's docstring said it followed "the same rule
  `confine_to_workspace` follows in the app", and `_confine` had no test of
  its own at all. Two of the four were that one `startswith` line:
  `os.path.realpath("C:\")` ALREADY ends in a separator, so `root + os.sep`
  is `C:\` and a workspace of a drive root refused every path on the
  machine; and realpath canonicalizes case only for components that EXIST, so
  a workspace folder that does not exist yet keeps whatever the caller typed
  and two spellings of one root stopped matching. `os.path.commonpath`
  answers both. The other two: no truthiness guard on the path (an empty
  `filePath` joined to the root and came back as the WORKSPACE DIRECTORY, so
  Alloy's own refusal never fired and the approval card would have named a
  folder), and no type guards (`os.path.join` raises TypeError, which is not
  in the `except (OSError, ValueError)`, so the copy could raise out of an MCP
  handler and reach the seat as a transport error — relay had that hole too,
  for a non-str ROOT, directly under a docstring promising it never raises).
  **Every divergence failed CLOSED or crashed; not one let a path out** — the
  only caller is `upload_file`, which READS, so this was a robustness and
  honesty fix and saying otherwise would have been an over-claim. And
  **measure which line is load-bearing before writing it down**: `normcase`
  looks like what saved relay from the case bug and it is not
  (`ntpath.commonpath` lowercases internally), and the `isabs` branch was
  equivalent to a plain `os.path.join` too (`ntpath.isabs("oo")` has been
  False since 3.13) — both are kept ONLY so a reader diffing the two functions
  finds nothing to reconcile, and the docstrings now say exactly that instead
  of promoting them to rules. `tests/test_confinement_parity.py` feeds ONE
  table of escape cases to both and asserts identical verdicts; that suite,
  not the prose, is what stops the next drift (10 of its rules RED-verified,
  plus two mutations confirmed genuinely equivalent). Its standalone guard had
  to be LINE-ANCHORED: `"from relay" in src` matched the module docstring's
  own promise that it imports nothing from relay — the wrap-token bug's
  family, a substring match that cannot tell a statement from a mention.

- **A number that only ever goes UP is a restated total, not a measurement**
  (found 2026-08-27). GPT's per-turn token counts had been summed since
  telemetry shipped, through 18 passing usage tests, because nothing ever
  asked what the SHAPE of the number was. The tell was free and sitting in
  the session logs: a real chat's GPT rows read 171,279 → 213,161 → … →
  40,428,770, monotone across 20 rows, while claude's read [6, 4, 26] —
  non-monotone, so claude's cannot be cumulative and GPT's obviously is.
  Whenever an adapter starts reporting a counter, check monotonicity across
  a real multi-turn chat before summing it. And note where the answer had to
  come from: `codex --help` does not document the event, model self-report is
  a hallucination surface, and the binary's own `TokenUsageInfo` struct names
  a per-turn field that `exec --json` never emits — only capturing the raw
  stdout of a real 3-turn run settled it.

- **A fork was never resumable, at any seat count** (found 2026-08-27 while
  verifying a solo fork; reproduced at one, two and three seats). `fork.py`
  clears every seat's `session_id` by design — the provider threads hold the
  ORIGINAL conversation and resuming them from a diverged timeline would forge
  continuity — but it left `introduced: True`. `continue_block` reads
  introduced-without-a-session-id as an orphaned seat ("X's memory wasn't
  saved — view only") and `rehydrate` RAISES on it, so every fork of a chat
  that had taken a turn was permanently view-only and typing into it silently
  started a brand new conversation. Clearing `introduced` is also required for
  its own sake: it is what makes the next turn send the preamble, and a seat
  with a brand-new CLI session has not had one. The lesson is the pairing —
  `session_id` and `introduced` are two halves of "this seat has a live
  thread", and anything that drops one must drop the other.

- **A guard that is right everywhere can still be wrong at one caller.** The
  never-hand-a-CLI-an-empty-prompt fix was correct for every dispatch loop and
  wrong for `_panel_prompt`, which supplies the whole prompt body itself and
  joins with `if p` — so an empty `compose_prompt` was LOAD-BEARING there, and
  filling it regressed the shipping multi-seat panel with a sentence its own
  payload contradicted. Before making a shared helper "never return empty",
  enumerate its callers and ask which of them was RELYING on empty. And note
  what let it ship: `tests/test_panel.py` asserted what its prompts CONTAIN,
  never what they START with, so six passing tests saw nothing.

- **A fake that tolerates the broken input is why the bug survived a
  measurement.** Six modes were driven through the REAL `run_rounds` with one
  FakeAgent and five reported "works". Every one of them was handing the seat
  `""` from turn 2 (nothing fans in with no peers), which a real CLI rejects
  outright — `claude -p ""` exits 1. The FakeAgent answered happily because it
  never looks at its prompt, so the probe measured the loop's control flow and
  called it a measurement of the product. Same family as the abort-seam stubs
  that took `abort` and ignored it, and the fake vendor whose `call_tool` had
  no `await` so every "concurrency" test was secretly sequential. The rule:
  when a probe's verdict is "it works", ask what the fake would have done if
  it did not — and assert on the ARTEFACT the real component consumes (here,
  the prompt string), never only on the fact that a turn happened.

- **A cfg key the UI sends and the app never reads is a control that does
  nothing, and nothing anywhere says so** (fixed 2026-08-26, had been
  shipping). `ui/index.html` built the start cfg with BOTH `permission:
  permissionMode` and a compatibility `yolo: permissionMode === "full"` —
  and `Api._conversation` read ONLY `yolo`, so "Read only — no changes" and
  "Ask first — supervised" both arrived as `False`, `Agent.__init__` fell
  back to `DEFAULT_PERMISSION`, and every app seat ran at `auto` (claude
  `--permission-mode acceptEdits`, codex `workspace-write`, opencode
  `--auto`). Only "Full access" ever differed, and only through the legacy
  key. Nothing looked wrong from any angle: the pill painted its own colour,
  the HUD read the rung back out of `session_summary`, and meta.json agreed —
  because `SessionStore.save` derives `permission` from the same absent state
  key, so a reopened chat repeated the app's own mistake back to it. Two
  rules follow. The **compatibility spelling must be derived from the real
  one, in the reader, not trusted from the writer** — `permission =
  normalize_permission(cfg["permission"], "full" if cfg["yolo"] else
  DEFAULT)` then `yolo = permission == "full"`, exactly as relay's own
  `main()` has always done — because the moment two keys carry one fact, the
  one the reader ignores is silently authoritative. And a **safety control
  needs a test at the bridge, not just at the engine**: relay's permission
  ladder had 18 passing tests and every one of them was right; the app had
  none, and `app.py` contained zero occurrences of the string "perm".
  `_continue` and `open_session` needed no fix and never did — `_continue`
  never rebuilds agents, and `rehydrate` already resolved the rung — they
  were simply being handed a meta that said `auto`.

- **THE DESKTOP AND BROWSER RUNGS ONLY ENFORCE AT `permission: read_only`
  AND `ask`.** Verified 2026-08-26 with a working proof of concept: a seat
  holding a shell forged **5 of 5** desktop approvals and Josh was asked **0**
  times. The mechanism needs no secret to find - `desktop_dir()` /
  `browser_dir()` are `%TEMP%\alloy-*\<uid>`, the seat's CLI runs as the same
  Windows user as the relay, a `%TEMP%\alloy-*\*` glob finds the channel, and
  the uid is printed verbatim on the seat CLI's own argv inside the
  `--mcp-config` JSON. **No same-user file channel can fix this**: a shared
  secret rides on argv (readable by the same user) and an ACL does not help
  because the owner holds `WRITE_DAC`. And tracing it one step further makes
  it simpler than "the channel is forgeable" - `permission: auto` and `full`
  both grant Bash with no prompt, so such a seat never needs to forge
  anything: it can drive UIA from its own PowerShell, or launch its own Chrome
  with no fence, and never touch Alloy's MCP servers at all. The forgery is
  merely the tidiest of several routes. So this is **not a bug to patch; it is
  a true statement about the threat model**, and the only honest response is
  to say it: `advisory_rung_note()` puts it in `capability_note()` at auto and
  full, and `#rungAdvisory` puts it under the pickers. Note the shape of the
  trap - the combination a careful person actually picks ("let it work freely
  in the workspace, but ask me before it clicks anything") is exactly the
  combination where the asking is worth least. What DOES stay enforcing at
  every permission level is the browser's **site fence**, because it lives
  inside Chrome, in a process the seat cannot write to.

- **Four rules for driving `chrome-devtools-mcp`, every one MEASURED against
  a live 1.7.0 (2026-08-26) and every one re-verified before shipping.**
  (1) **Allowlist-only, always.** `--allowedUrlPattern` and
  `--blockedUrlPattern` are MUTUALLY EXCLUSIVE - pass both and the server
  prints "Arguments allowedUrlPattern and blockedUrlPattern are mutually
  exclusive" and **never handshakes**, so the capability silently vanishes.
  The allowlist is also strictly stronger: against `https://example.com/*` it
  refused `file:///C:/Windows/win.ini`, `http://127.0.0.1:8765/start`,
  `chrome://version` AND `http://lvh.me:8765/start` - a public, permanent DNS
  alias for 127.0.0.1 that no blacklist could enumerate. It refuses `lvh.me`
  because it is *not listed*, which is the whole point. Never emit
  `--blockedUrlPattern`. (2) **Empty means deny-all.** Never `if patterns:` -
  an omitted flag is no fence at all, and a reviewer read `~/.codex/auth.json`
  through exactly that gap. With nothing configured, emit the sentinel
  `https://alloy.invalid/__never__` (measured: parses, blocks everything).
  (3) **The fence must PROVE ITSELF, because the vendor silently accepts
  unknown flags.** Passing `--allowedUrlPatterns` - one character, the plural -
  produced no warning, no error, no non-zero exit, and `file://` then
  navigated **successfully**. So on the first tool call the proxy navigates a
  scratch page to a URL that must be refused and latches dead if it is not.
  Re-proven end to end against real Chrome after the code was written: with
  the typo, zero approval cards and zero unfenced browsing. (4) **A URL-policy
  refusal comes back with `isError` FALSE**, reason in the text body
  ("...is blocked by blocklist/allowlist rules."), so nothing may decide from
  `isError` alone - in particular an approval note must never be stamped onto
  a call the fence actually refused. Two more the vendor's source explains:
  the same refusal has a DIFFERENT shape from `new_page` (which does not catch,
  so it arrives `isError: true`), and `goBack`/`goForward`/`reload` never
  consult the URL check at all - safe only because history can contain nothing
  the fence did not already allow.

- **Transport details that cost a hang or a false "unavailable" if guessed.**
  Speak to the vendor over the MCP SDK's own stdio client, not hand-rolled
  JSON-RPC: on Windows `create_windows_process()` puts the child in a Job
  Object with `KILL_ON_JOB_CLOSE`, which is the entire Chrome-reaping
  guarantee and ~100 lines of pywin32 we would otherwise own. Open the vendor
  session in `serve()`, **never lazily inside a request handler** - that is an
  anyio cancel-scope constraint, not style: the prototype died with
  "Attempted to exit a cancel scope that isn't the current task's". `errlog`
  must be a **real file object** - a bounded ring-buffer writer raises
  `io.UnsupportedOperation: fileno` inside `anyio.open_process` on a perfectly
  healthy vendor, which would report the capability permanently unavailable.
  Eager node, lazy Chrome: the handshake is ~0.6 s with zero new chrome
  processes, and the first tool call launches Chrome in ~0.4 s - so a turn
  whose seat never browses pays only the node handshake. And `node` is passed
  by ABSOLUTE path, because an MCP client may hand a stdio server only the env
  its config names.

- **The approval wait must not run on the event loop that drains the vendor.**
  `ask_josh` polls a directory for up to three minutes; awaited inline it
  would stall Chrome's entire session while Josh reads the card, and a full
  stdout pipe would wedge the child outright. It rides `asyncio.to_thread`.
  The fence self-test needs an `asyncio.Lock` for the same reason -
  overlapping requests would each navigate the probe. **Both were found by
  reading, and the first attempt to test them PASSED against the broken
  code**, twice over: the fake vendor's `call_tool` had no `await` inside, so
  nothing ever interleaved and every "concurrency" test was secretly
  sequential; and the blocking version still SUCCEEDED, just late, so only a
  TIME assertion could see it. Same lesson as the abort-seam Event - a test
  that does not exercise the thing under test is not a test.

- **`rehydrate` must return every access axis to STATE, not only to the
  agents** (fixed 2026-08-26; `connectors` and `desktop` had been losing
  themselves this way since they shipped). It fed the saved values into the
  `Agent` constructors and then returned a state dict without them, while
  `SessionStore.save` reads `state.get("connectors"/"desktop"/"browser")` —
  so a resumed chat wrote `false`/`"off"`/`"off"` over the real values on its
  very next save. The rail, fed from that meta, then showed no access for a
  chat that still had it, and the NEXT reopen built agents that genuinely had
  none. Silent, one-way, and indistinguishable from "the setting was
  forgotten". The general rule: **anything `SessionStore.save` reads off state
  must be put back on state by `rehydrate`**, and the two lists should be
  compared whenever either grows.

- **The PUBLISHED tool list is the strongest capability claim a model reads.**
  A look-only browser conversation used to publish `click`/`fill`/`type_text`/
  `evaluate_script` and then refuse every call against them — which is the
  exact inverse of the WITHHELD rule (state it, don't leave it as an absence)
  and costs the seat one tool call per attempt. `curate()` now takes the rung.
  The corollary that makes it safe: a tool that is withheld must still be
  refused with its REASON (`Proxy.call` looks it up in `self.dropped`), never
  as "unknown tool" — which would send a seat hunting for a spelling mistake
  that is not there.

- **A capability can be over-claimed at the ROOM level, not just the seat.**
  Only `ClaudeAgent.build_cmd` registers the desktop and browser servers, so a
  GPT+Gemini room could set Browser control to Unattended, type a site list,
  tick the acknowledgement modal — and produce a run byte-identical to
  `off`, with every Josh-facing string still saying "Seats".
  `axis_unreachable_note()` says so, in both front ends, and
  `MCP_DELIVERING_PROVIDERS` is spelled once with a test that greps each
  adapter's `build_cmd` — a hand-kept list would drift the moment another
  adapter gains a route.

- **An admission that carves out an exception gives itself back.** The first
  draft of `advisory_rung_note` said the rungs are only a guardrail at
  auto/full "…Chrome's site list is enforced inside the browser and still
  holds" — and a seat reads that last clause as "the site list is the one
  thing I cannot get around". It bounds the Chrome ALLOY spawned; a seat with
  a shell reaches any site with curl. The wording is now "the site list still
  bounds the browser Alloy runs, but it does not bound a shell."

- **The vendor's path check covers READS too, so Alloy negotiates MCP roots**
  (fixed 2026-08-26 after a live repro). The first design read the vendor's
  "file-writing tools are restricted to the OS temp directory" notice as
  covering writes only, dropped every write-path key, and kept `upload_file`
  behind Alloy's own workspace confinement. Measured: `input.js` declares
  `verifyFilesSchema: ['filePath']` for `upload_file` and `ToolHandler`
  calls `context.validatePath` on it BEFORE it even resolves the page — so
  with no roots negotiated the only root is `os.tmpdir()` and every workspace
  path failed with "Access denied: … not within any of the configured
  workspace roots". A published tool that passes Alloy's check, asks Josh, and
  then always fails. The fix is to negotiate ONE root, the conversation's
  working folder, via `ClientSession(list_roots_callback=…)`: the vendor's
  boundary and Alloy's `_confine` become the same boundary, enforced in the
  process that does the I/O. No workspace ⇒ no roots ⇒ the vendor denies every
  path, which is the direction to fail.

- **A URLPattern port can be a wildcard, and `int()` does not notice.** The
  webhook-port refusal compared a literal port, so `http://localhost:*/*` —
  the natural way to write "my dev server, whatever port it took" — sailed
  past it AND, because nothing was rejected, left `clamp_browser_rung` with no
  reason to lower an Unattended run. A loopback pattern must now name one
  literal port while the webhook is listening. Same class as
  `x or DEFAULT_CEILING`: a value the parser cannot express reads as "no
  problem here".

- **A documented gate that nothing calls is worse than no gate.** `gate()`
  spelled out the callable-`describe` contract while `Proxy.call` did its own
  `decide` → `prove_fence` → `ask_josh` sequence, so neutering `gate` entirely
  changed no behaviour and passed the suite. Whoever hardened "the gate" would
  have edited the copy that never runs. It is deleted; `decide()` is the one
  interpreter, and a test asserts there is exactly one.

- **REGISTERED IS NOT CALLABLE: `--allowedTools` is the only thing that
  gates MCP, and only the `auto` branch emitted it** (fixed 2026-08-26, both
  halves measured with a real seat). The desktop server had shipped with this.
  Per rung: **`auto`** works (the names are appended). **`full`** works
  (`--dangerously-skip-permissions` bypasses every check, so no allowlist is
  needed). **`ask`** was denied on every call — *"Claude requested permissions
  to use mcp__alloy_browser__new_page, but you haven't granted it yet"* —
  because the append loop patched an existing `--allowedTools=` and no-opped
  when there was none; it now ADDS one naming exactly the two server prefixes,
  which leaves the ask rung intact (Write/Edit/Bash still route through the
  approval hook) and is correct because those two axes have their own watcher
  and their own answer from Josh. **`read_only`** cannot be fixed at all:
  it emits `--permission-mode plan`, and claude refuses every MCP call in it —
  *"Cannot call mcp__alloy_browser__new_page while in plan mode."* So both
  `*_server_spec()` return None there, which also silences `capability_note`
  for free (both clauses gate on a spec existing). The shape to remember: a
  capability can register, list its tools, promise itself in the preamble and
  still be uncallable — the init event proves the SERVER connected, not that a
  call will be permitted.

- **The approval card's page name was scraped out of page-controlled text.**
  The vendor renders each page as `<id>: <title> (<url>) [selected]`, and the
  title is written by the page — so a leftmost `\((\S+?)\)\s*\[selected\]`
  match on a title of `(https://safe.test) [selected]` put a URL the PAGE
  chose on the card Josh approves, while the click landed elsewhere. The parse
  is end-anchored now (the real `(url) [selected]` is always last on the line,
  and a non-selected page's line ends with `(url)` and no marker), and a
  capture that is not http(s) names nothing. Exactly the wrap-token bug's
  family: a substring match on text somebody else controls.

- **The card must name the VALUES, not a count.** `fill` showed its text while
  `fill_form` said "fill 2 fields" and `handle_dialog` never showed
  `promptText` — so the way to keep a secret off the line Josh reads was to
  send it through the plural tool. His decision is only ever as good as that
  sentence, which is the same rule `desktop_mcp._detail` was written to.

- **`webhook.py` requires `application/json` on POST /start, and that is a
  security check.** A cross-origin page can POST with no preflight only when
  the request is a CORS *simple request*, and the three qualifying media types
  are `x-www-form-urlencoded`, `multipart/form-data` and `text/plain`.
  Demanding JSON forces a preflight that nothing here answers. It matters
  because loopback is reachable from any browser on this machine - including
  one an Alloy seat is driving - which is the self-approval class one surface
  over. A MISSING header is refused too: `fetch()` with no explicit header
  sends `text/plain`, so accepting "absent" would hand the simple-request path
  straight back.

- **Desktop control is its own axis, with its own watcher, its own request
  directory and its own callback — and that separation IS the safety.** Two
  holes close by construction. (1) `run_rounds` nulls `on_approval` for every
  seat whose permission is not `ask`, so hanging clicks off that callback
  would deny them at read_only, auto AND full — everywhere real work happens —
  and read as broken hardware rather than as policy. (2) `_watch_approvals`
  short-circuits on `_turn_verdict`, so a "rest of this turn" Josh said to an
  unrelated Bash prompt would otherwise pre-approve every click and keystroke
  after it; `_watch_desktop` consults no standing verdict and the desktop
  modal offers only Allow once / Deny — no "rest of turn", no "always allow",
  because the way to stop being asked is the allowlist set up front, not a
  button offered while a run is waiting on him. A RED guard pins hole 2.
- **`--mcp-config` accepts an inline JSON STRING, which is how desktop control
  is delivered without touching `~/.claude.json`.** `--strict-mcp-config` plus
  a config naming exactly one server is a whitelist of one, so the definition
  travels per invocation: the grant is scoped to that conversation for free
  and Josh's own terminal `claude` sessions never see it. That also removes
  any need for `claude mcp add` here. Measured 2026-08-26 from the
  `system/init` event: no fence = 8 servers / 59 `mcp__` tools; empty fence =
  0 and 0; one-server fence = `[('alloy_desktop','connected')]` and exactly
  its 7 tools. When connectors are ON the config is passed WITHOUT
  `--strict-mcp-config`, so it ADDS the desktop server to Josh's real ones
  instead of silently deleting them.
- **Never verify a capability by asking the model what tools it has.** Asked
  to list its `mcp__` tools, a haiku seat once named a plugin server that the
  authoritative `system/init` event showed was not loaded at all. Model
  self-report is a hallucination surface; the init event's `tools` and
  `mcp_servers` arrays are ground truth, and every capability claim in this
  repo should be checked against them (or against the CLI's own `--help`).
- **An abort seam is CALLED, so never hand it an Event** (fixed 2026-08-26,
  had been shipping). `Agent._watch_approvals` passed its `stop`
  `threading.Event` straight into `on_approval(req, abort)`. Every consumer
  does `abort and abort()` (`_AppIO.ask_human`, `CLIIO.ask_human`) or
  `abort()` (`ask_abort`) — and an Event is TRUTHY BUT NOT CALLABLE, so every
  mid-turn approval raised TypeError into `_watch_approvals`' blanket except
  and was answered **deny**, reading "Alloy approval failed ('Event' object is
  not callable)". The whole "Ask first" rung was silently auto-denying while
  the modal flashed open and shut. Pass `stop.is_set`. The reason six passing
  tests proved nothing: every stub took `abort` and ignored it. A stub that
  never exercises the argument under test is not a test — `test_permissions`
  now uses it exactly as the real front ends do, and was verified RED against
  the old line.
- **MCP reachability is not a property of the permission rung, so the
  connectors fence lives OUTSIDE the rung branch** (fixed 2026-08-26).
  `--allowedTools` gates MCP only in the `auto` branch; `full` emits
  `--dangerously-skip-permissions`, which bypasses every permission check
  including MCP. So a Full-access seat held every connected server no matter
  what the Connected-apps checkbox said — **verified live**: a haiku seat at
  `full` listed `mcp__claude_ai_Corvaer_Epicor__*` tools (Josh's real ERP),
  and with the fence, NONE. The fence is `--strict-mcp-config --mcp-config
  '{"mcpServers":{}}'` whenever `connectors` is off, and it must be a
  WHITELIST: the tempting `--disallowedTools=<claude_mcp_prefixes()>` fails
  OPEN, because that helper returns `[]` on any probe failure and an empty
  blacklist grants everything — the gate would vanish exactly when the probe
  broke.
- **The three MCP backends speak three different grammars** (fixed
  2026-08-26; all verified against each CLI's own `--help` and, for opencode,
  its installed SDK types). Env flag: claude documents `-e, --env`, codex
  documents `--env` ONLY — the shipped code emitted `-e` for both, so every
  env-carrying server registered with GPT was rejected at the flag. `--env` is
  the one spelling both accept, hence the default. **opencode cannot be
  written through its CLI at all**: `opencode mcp add` takes no command
  positional (only `--url/--env/--header`) and there is no `mcp remove`
  subcommand. Its servers live in `opencode.json` under **`mcp`** (not
  `mcpServers`), as `{type:"local", command:[argv...], environment:{}}` or
  `{type:"remote", url, headers:{}}` — per `@opencode-ai/sdk`'s own
  `McpLocalConfig`/`McpRemoteConfig`. Hence the `write` descriptor key: read
  over the CLI, write to the file. Listing still works over its CLI.
- **`codex features list` says `browser_use` and `computer_use` are `stable
  true`, and `codex exec` exposes NEITHER** (measured 2026-08-26 by asking
  codex exec to enumerate its own tools inside Alloy's sandbox). The feature
  flags describe the product, not the print-mode surface. `codex exec`'s real
  tool list is: `functions.exec/wait/request_user_input`, `collaboration.*`,
  `apply_patch`, `shell_command`, `create_goal`/`get_goal`/`update_goal`,
  `update_plan`, `view_image`, `image_gen__imagegen`, `web__run`, and the MCP
  resource readers. So a GPT seat has web search and image generation but
  **cannot drive a browser**, and no capability_note may say otherwise — the
  obvious "read the feature flags" shortcut gets this exactly wrong.
- **A single global file needs a UNIQUE temp name, not just a replace retry**
  (fixed 2026-08-26). `retro.write_playbook` used a fixed `<path>.tmp`, so two
  writers truncated each other's scratch file and both renamed it into place.
  Reproduced: 6 threads × 12 writes raised 5 PermissionErrors. outcome.py's
  `_atomic_write` gets away with a fixed name only because there is one
  outcome.json per session; the playbook is one file for the whole app, and
  `playbook_block()` is interpolated straight into `SUPERVISOR_PROMPT`, so a
  spliced write degrades the planner of an unattended run.
- **`Api._session_dir` resolves to the FOCUSED run**, so anything inside
  `_rounds` must read `state["store"].dir` instead (fixed 2026-08-26). The
  function already bound `run = state["_run"]` with a comment about not using
  the focused one; three later reads still leaked, so a Josh who switched tabs
  mid-run got this chat's `done` event carrying the other chat's directory,
  summary and feedback.
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
  tests — see `tests/` (2221 tests across 69 suites, all runnable as plain scripts, or `python tests/run_all.py` for the lot).
- **Cost and token telemetry (truth over estimation).** `ClaudeAgent` and `CodexAgent`
  stream parsers extract `total_cost_usd`, input/output/cached tokens, and duration
  per turn (`last_usage`), resetting at the start of `Agent.turn`. On failed CLI error
  returns, usage is extracted from the result object before returning `""`, counting spend
  while preserving the never-forge rule. A central accumulator `record_usage(state, usage, seat_key=None, kind=...)`
  rolls spend into `state["usage"]` with additive totals, `by_seat`, and `by_kind` (`seat`,
  `supervisor`, `moderator`, `helper`, `team`, `brief`, `retry`, `failed`), persisted atomically
  in `meta.json` and aggregated into `outcome.py` `hard_facts["usage"]`. Seats that report
  nothing (Gemini CLI) remain honestly blank — never inferred or estimated. The UI renders
  `.msg-usage` pills per turn from the stored row across both live streaming and session replay.
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
- **The UI is ONE inline `<script>`, so any top-level throw silently deletes
  the rest of the app.** Verified 2026-08-21: the permission-hardening commit
  put `syncPermissionNote();` a few lines ABOVE `let sessions = [], activeId =
  null;`, and that function reads `activeId`. `let` is not `var` — reading it
  before its declaration line executes is a TDZ `ReferenceError`, not
  `undefined`. Everything after that line stopped running: no `addSeat` calls,
  no `pywebviewready` listener, therefore no `get_config()`, therefore EVERY
  model and thinking menu blank — while the window, its CSS, and the Python
  side all looked perfectly healthy (`get_config` returned all 5/7/4 models on
  demand). The tell is that the failure is total but silent: WebView2 logs the
  error to a console nobody opens. Rules that follow: initializers that read
  top-level state belong at the END of the script next to the `addSeat(…)`
  line, never beside the function they call; and `tests/test_ui_boot.py` now
  actually EXECUTES the script in node against a stub DOM, because all 27
  other suites read the UI as text and not one of them could see this. That
  suite carries a deliberate RED guard that re-injects this exact bug — a boot
  harness that quietly stops executing anything is worse than none.
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
- **Dictation is local because Wispr Flow cannot be embedded** (researched
  2026-08-22, do not re-derive). Wispr ships exactly two surfaces: the desktop
  app (system-wide keystroke injection — nothing an app "adds"; it was not even
  installed on this machine) and the **Flow API**, which is real and documented
  (`wss://platform-api.wisprflow.ai/api/v1/dash/ws`, base64 16 kHz mono 16-bit
  PCM WAV in equal ~1 s chunks, `auth`/`append`/`commit`, `{"status":"text",
  "final":…}` back, and an auth-frame `conversation` context object) but gated:
  the org must be approved by Wispr, then the key is billed. That is a paid API
  key in a project whose first line is "No API keys anywhere", so `dictation.py`
  ships local faster-whisper plus a `WisprFlowTranscriber` SEAM that records the
  contract and refuses by name. Corollaries: **capture must happen in Python,
  not the WebView** — pywebview 6.2.1's edgechromium backend has NO
  `PermissionRequested` handling at all (only the Qt backend mentions
  permissions), so `getUserMedia` in the window is a coin flip, while a
  Python-side stream hands us exactly the buffer both engines want; the
  **start/stop race is real** (a tap can finish before PortAudio finishes
  opening, and the loser must close the stream it never owned — `Recorder.start`
  returns False for that, guarded by a test); a **stop with no start is not an
  error**, because hold-to-talk loses its pointerup to a dragged-away cursor;
  and **an empty or failed transcription may never produce words** — same rule
  as never-forge-a-turn, one field over, since dictated text becomes a prompt
  three CLIs act on. `base.en` (already cached) loads in ~5 s cold, so
  `_dict_warm` overlaps the load with the speaking rather than the release.

- **`x or DEFAULT_CEILING` cannot express "no ceiling".** Four call sites read
  `state.get("turn_ceiling") or DEFAULT_CEILING` (two loop caps, free mode's
  `budget_left`, and its ceiling note). `0` is falsy, so that idiom silently
  turns an unbounded run into a 60-turn one — the single bug a Keep Improving
  run cannot survive. `effective_ceiling(state)` is now the only reader:
  `None` means unbounded (continuous mode), and `0`/`None`/garbage still mean
  `DEFAULT_CEILING` for everyone else. Every caller must test
  `ceiling is not None and turn >= ceiling`, never truthiness.

- **Keep Improving and Build Together are identical on all six policy axes.**
  Both are `supervisor` workflow → barrier / manager / isolated / supervisor
  completion / waves. The ONLY thing that separates them is
  `continuous.on`, so `presetForCurrentRecipe()` compares `!!recipe.continuous
  === contOn` as a seventh term. Without it the Advanced drawer silently
  relabels a running Keep Improving room "Build Together" — the same class of
  quiet relabelling the moderator picker already taught us about.

- **`os.path.abspath("")` is the CWD, and this process runs in the Alloy
  repo.** `detect_test_command(None)` therefore "detected" Alloy's own
  `tests/run_all.py` and would have offered to run it against somebody else's
  project folder. Guard falsy input BEFORE abspath, not after. Same trap waits
  for any other path helper that normalizes first and validates second.

- **A supervisor side call inherits Josh's global skills.** Live 2026-08-22:
  the stateless planner (`claude-haiku-4-5`, running with `cwd = workspace`)
  loaded the `superpowers:brainstorming` skill, decided it must ask clarifying
  questions first, and returned prose with no `[[TASK:]]` directives at all.
  The engine degraded correctly (no plan, no forged tasks, an ordinary
  parallel conversation) — but a Keep Improving run then has nothing to do
  until the watchdog notices. Two consequences already handled:
  `current_objective(state)` falls back to the topic, because
  `plan_workstreams` sets `supervisor_goal` only on SUCCESS and both watchdog
  remedies would otherwise have nothing to anchor on; and "Task board: EMPTY"
  is an explicit line in the health report rather than an absence. The
  underlying skill leak is NOT fixed and is not specific to this mode.

- **The watchdog's remedy set is CLOSED, and that is the whole design.**
  `requeue` / `replan` / `next_objective` / `clear_seat:<seat>` /
  `nudge:<text>` are the only things `apply_remedy` can perform. A free-text
  instruction the engine cannot execute would read like a repair in the
  transcript while changing nothing, which is strictly worse than declining
  out loud — so an unrecognized remedy becomes a visible note and no action.
  `HEALTHY`/`FIX`/`STOP`, like `OBJECTIVE`/`IDLE`, `TASK` and `DONE`, are
  opted into by the parser and are NOT in `KNOWN_DIRECTIVES`: an ordinary seat
  playing one stays visibly unknown instead of acquiring watchdog authority.

- **An unanswered check-in is a SKIP, never an approval.** The `permission`
  action blocks on `io.ask_human`, whose documented headless answer is `None`
  (CLI without a human, child teams, tests). That must mean "changed nothing,
  and said so", exactly like never-forge-a-turn one field over. It is also why
  the warning modal states in plain words that `permission` makes the run wait
  — including overnight.

- **Continuous mode has no cap, so a real loop never returns.** That is
  deliberate, and it means `tests/test_continuous.py` cannot drive the
  revival layer through the real loop: FakeAgents would answer forever. The
  revival tests stub `relay._run_rounds` instead, which is the honest unit
  anyway (the layer under test lives in `run_rounds` AROUND that call). The
  same property bites in the other direction: a supervisor-workflow test state
  with no `workstreams` makes `_run_rounds` call `plan_workstreams`, which
  builds a REAL `ClaudeAgent` and shells out — two tests here did that and
  cost 23 seconds before `sup_state` started stubbing `build_supervisor`
  unconditionally. A suite that claims token-free has to be *structurally*
  token-free.

- **The check-in clock is accumulated, not wall-clock.** `continuous_tick`
  adds `monotonic` deltas onto a persisted `elapsed_s` at each barrier: the
  MARK resets on resume, the TOTAL does not, so a chat continued tomorrow
  keeps yesterday's hours instead of being handed a fresh eight. Consequences:
  `/checkin` needs its own `checkin_now` flag (a run whose clock is still near
  zero would otherwise ignore it, and "I asked and nothing happened" is the
  worst answer a watchdog can give), and `run_checkin` marks the check as
  taken BEFORE the side call so a dead call cannot re-fire at every barrier.

- **`tests/test_ui_boot.py`'s stub selector engine used to match
  `input[name=x]` as bare `input`.** It ignored the bracket entirely, so
  `querySelectorAll("input[name=contAction]")` returned EVERY input on the
  page and the UI's own binding loop overwrote `onchange` on the rounds box
  and the moderator toggle. The suite then failed in two unrelated tests. It
  now matches `[attr]`, `[attr=value]`, `:checked` and `:disabled` for real.
  A stub that silently over-matches is worse than one that refuses.

- **`Api._emit_q` carries `(event, json)`, not just json.** The drain thread
  needs the event name to flash the taskbar on `checkin` — done there rather
  than in `emit()` so the one thread that owns window interaction keeps owning
  all of it, and `emit` stays a pure enqueue. Anything reading the queue
  directly must unpack the tuple.

- **A seat's `[[ASK]]` can wedge an unattended run, and every brake is
  downstream of the barrier it blocks.** Live 2026-08-22: two haiku seats
  ended round 4 with a clarifying question, `handle_ask_directive` blocked the
  parallel barrier waiting for a console answer that was never coming, and the
  accumulated clock, the spend cap and the scheduled watchdog are ALL checked
  at that barrier — so nothing could fire and the run sat there. `ask_abort`
  now composes the caller's abort with a deadline (`min(ASK_WAIT_MAX,
  check-in interval)`) in continuous mode only, and both front ends already
  poll `abort` and return `None` from it, so no front-end change was needed.
  The expiry takes the documented unanswered path — a relay note in the
  requester's queue, never a forged answer. A `permission` check-in is NOT
  subject to this: Josh explicitly configured that one to wait.

- **The Supervisor planner used to reply with clarifying questions.** Same
  live run: `claude-haiku-4-5` loaded a brainstorming skill and asked what
  "better" meant instead of emitting `[[TASK:]]` directives, so the engine
  correctly recorded "no executable tasks" — and a Keep Improving run with no
  plan has nothing to do at all. `SUPERVISOR_PROMPT` rule 6 now says, in the
  prompt, that nobody will answer, that this is one stateless call, and that a
  reply with no directives is a wasted one. Verified: the very next run
  planned two waves, delivered files, passed the gate and committed.

- **Backing out of the Keep Improving warning is a REFUSAL.** `applyPreset`
  captures the outgoing preset BEFORE `setSelectedPreset` moves the cards —
  reading it inside `openContinuous` reads "keep_improving" (already
  selected), so Cancel re-applied Keep Improving and re-opened the modal
  forever. `tests/test_ui_boot.py` carries the RED guard.

- **`tests/test_ui_boot.py`'s stub `classList` was a no-op in BOTH
  directions**: `.add("show")` did nothing and `.contains()` always returned
  false, so a suite could drive a modal open, "pass", and never have opened
  anything. It is now a real implementation over `className`. Along with the
  attribute-selector fix above: a stub that silently lies is worse than one
  that refuses.

- **The CSS `font` shorthand rejects `inherit` in its family slot**, and an invalid shorthand drops the WHOLE declaration. `font: italic 12px/1.55 var(--ui, inherit)` therefore silently applied nothing - no italics, no size - while reading perfectly sensibly in the source, and the only way it surfaced was reading `getComputedStyle` off the real page. Use longhands whenever a value might be a keyword or a custom property with a keyword fallback.

- **A `working` CLOSE must bypass the UI's chat routing.** The app's
  pre-flight row opens before the chat has an id (`Run.id` is None for a
  draft) and closes after `self._session_dir = ...` adopted one - so the
  close carries a chat_id the visible stage does not match yet, and
  `uiEvent`'s not-my-chat gate dropped exactly that close, stranding the
  spinner forever. Closes are now handled at the very top of `uiEvent`,
  before any routing; closing an id that was never painted is a no-op, which
  is what makes that safe. The OPEN still respects the gate - a background
  chat's work is not this transcript's business.

- **The same stub's `appendChild` did not MOVE an already-parented node**, it
  just pushed a second reference. The UI re-appends its live indicators below
  every new message (`typingEls.forEach(el => feed.appendChild(el))`, and now
  `workingEls` too), so in the harness one row became two entries - and since
  `remove()` is a filter, closing ONE row then appeared to delete two. Real
  DOM semantics now: detach from the old parent first. This had been silently
  wrong for the typing indicators the whole time; only a test that counted
  rows AFTER a message landed could see it.

- **An instant retry into a provider outage is worth nothing, and the full
  watchdog on top of it is worth less than nothing.** Diagnosed from a real
  2026-08-23 session: four `ox` seats at effort `max`, whose free endpoint kept
  answering `finish_reason: network_error` / `Endpoint is unavailable`. The
  automatic second attempt fired immediately, hit the identical wall ~90 s
  later, and the seat was benched. Worse, three seats then spent the FULL
  `TURN_TIMEOUT × TIMEOUT_SCALE["max"]` = 900 s each discovering what the
  provider had already said — the timeout landed at exactly 08:51:24, 900 s
  after the 08:36:24 dispatch. (That 900 s window is gone; see the
  silence-not-duration gotcha below. The backoff still matters — a dead
  endpoint is SILENT, so it now costs the idle window, not the full one.) `transient_error` / `retry_plan` /
  `backoff_wait` / `retry_window` now give a provider-class failure a
  `RETRY_BACKOFF` pause and a `PROBATION_TIMEOUT` window, at all FOUR retry
  ladders (sequential, parallel, panel, free). Deliberately excluded:
  `TurnTimeout` (it has its own no-retry path, and matching it here would hand
  a genuinely hung seat a second window), dead session ids, auth failures and
  a missing CLI — a backoff only delays a failure that will never heal.
  Probation is per-retry and restored in a `finally`; it never LENGTHENS a
  window that was already short.

- **The diagnosis was nearly wrong twice, and only measurement saved it.**
  First guess: too many concurrent calls to a free tier. Measured — four
  simultaneous 15 KB prompts at effort `max` came back 4/4 in 16 s, so
  concurrency was innocent and a "fix" there would have been pure damage.
  Second guess: prompt size. Measured — the real prompts were **~200 chars**,
  because a workstream worker gets only its task brief under the isolation
  rule. What was actually left was the long RESUMED session (13+ turns of
  heavy tool use on a free preview model), which Alloy cannot fix and should
  not pretend to. Josh then hit the identical error inside the opencode TUI
  with Alloy nowhere near it, which confirmed it outright.

- **"The conversation won't restart" was a conversation that WAS running.**
  Every element of the report was true and the conclusion was still wrong, so
  check these before believing a restart bug: the transcript's own `ts` fields
  against the app process's start time (his continue at 08:32 demonstrably
  worked — a new Supervisor plan landed at 08:36), and whether CLI children
  are alive (three `opencode.exe` from 08:36:24 still running at 08:50). What
  actually made a live run look dead: typing indicators are LIVE-only, so
  reopening a chat mid-turn wiped them and nothing brought them back. The
  `thinking` payload now carries the seat's watchdog, `Run.thinking` tracks
  who is mid-turn, and `open_session` replays them with their TRUE start time
  so a 14-minute-old turn does not restart at 0:00.

- **`was_interrupted` is the only honest "resume this automatically" signal.**
  `run_rounds` stamps `lifecycle: "active"` on entry and `paused` plus a
  `termination_reason` in its `finally` on every exit path — cap, wrap, stop,
  fatal, even an exception on the way out. So "active with no reason" can only
  mean the process itself went away. Every other ending was somebody's
  decision: `restart_resume` reopens those but never resumes them. Two
  auto-resumes that commit no turn block the third (`note_auto_resume`),
  because a chat that crashes on resume would otherwise bill itself in a loop
  — the same shape as Keep Improving's barren-revival guard.

- **`relay.write_tabs()` and `session_path()` read relay's OWN module
  globals**, not `app.SESSIONS_DIR`. A test that redirects only the app
  constant still writes the REAL `sessions/tabs.json` and throws away
  whatever Josh had open. (It did, once; restored by hand.) Any test touching
  tabs or resolving a session id must patch `relay.SESSIONS_DIR` AND
  `relay.TABS_FILE` — see `_sandbox_relay_paths` in `test_app_headless.py`.

- **`tests/test_ui_boot.py` used to hang for 60 s the moment a test drove a
  `thinking` event.** The page legitimately schedules 1-second tickers (the
  typing clock, `setSeatTelemetry`), and node will not exit while one is
  pending — so the harness reported nothing and failed on its own timeout.
  `report()` now exits from the stdout write callback, which both flushes and
  terminates. Two more stub limitations worth knowing: markup is parsed into a
  FLAT list by a tag regex, so `querySelector` INTO static markup finds
  nothing (code walking static children needs the same absence guard it would
  want in a browser — `renderJump` now has one), and a parent's `textContent`
  is not derived from its children, so a probe reading rendered text must walk
  `children` and `_html` itself.

- **The turn watchdog measures SILENCE, not duration** (2026-08-23, Josh: "a
  hard time limit is silly"). No CLI here caps a turn: `claude --help` lists no
  turn timeout and no `--max-turns` (its only budget knob is
  `--max-budget-usd`, API-mode only), and neither does `codex exec` — a turn is
  an agentic loop that runs until the work is done. So every limit a turn ever
  hit was OURS, and the first one was a single `threading.Timer` over the whole
  child: at effort `max` that killed a seat 15 minutes in whether it had
  streamed 400 tool calls or hung on a dead socket at 0:30 — while `on_line`
  fired for every one of those calls and reset nothing. We held the liveness
  signal and threw it away, and the visible result was a working seat that
  "failed for some reason" mid-edit, leaving half-written files AND an amnesiac
  session (`session_id` is captured in `parse()`, which the timeout path never
  reaches, so the next turn resumes from BEFORE the killed work).
  Now: `idle_timeout` (`IDLE_TIMEOUT × TIMEOUT_SCALE[effort]`) is restarted by
  every line on EITHER pipe — stderr counts, because a CLI that narrates to
  stderr is alive and reading only stdout would bench it — and `turn_timeout`
  is an absolute ceiling that is `None` unless Josh sets one (`--turn-cap
  MINUTES`). Corollaries that are easy to get wrong: the watchdog POLLS at
  0.25 s instead of rescheduling a Timer per line (a chatty turn emits
  thousands); `armed_window(agent)` is the ONE answer to "which window is
  live", because probation that shrinks the other one is a no-op that reads
  like a fix; `GeminiAgent.streams_progress = False` (agy prints its JSON at
  the end, which is also why it has no `activity()` hook) so silence proves
  nothing about it and it keeps a duration bound — it is the one seat that can
  still die on the clock; and the preamble must never again promise a fixed
  per-turn limit, because a seat told it has 15 minutes abandons work it had
  time to finish. The UI clock follows the same rule: with no duration bound it
  shows AGE only and surfaces `· quiet M:SS of M:SS` once silence passes half
  the idle window — "0:00 of 15:00" on a turn nothing will cut off is the
  precise lie that made a healthy run read as a hung app.

- **`tests/run_all.py` used to die on the first failing suite.** It prints a
  failure's output verbatim, and one non-ASCII byte in it raised
  UnicodeEncodeError on a cp1252 Windows console — killing the RUNNER, so
  every suite after the first failure never ran and no total was printed,
  exactly when you most need both. `main()` now reconfigures stdout/stderr
  with `errors="replace"`.

- **Spawn rules**: helpers/teams deliver results ONLY through
  `SpawnManager.drain_into_pending` at loop boundaries (helper threads never
  touch pending); every refusal/failure becomes a note in the requester's
  queue (never silent, never forged, never auto-retried); in-flight side-work
  at a crash is declared lost on the next run, never silently re-run. Teams
  are normal sessions (child meta `parent`, parent meta `children` — hints,
  a child may be deleted); depth is hard 1 via the child's zeroed spawn
  policy. `native_spawn_note()` lives next to build_cmd so the preamble can
  never promise a capability the flags don't grant.

- **The 7 "modes" are 4 engines wearing 7 labels** (audit + fixes, 2026-08-25):
  sequential / parallel-barrier / free / panel are the only dispatch targets;
  speaker = round-robin + the `[[NEXT]]` nomination floor, supervisor =
  parallel + planning hooks. Four honesty bugs fixed the same day: (1) the
  "park" for failed sequential seats (`mark_floor_unavailable`) was consulted
  by fairness/wrap but NOT the main cursor, so a dead provider was retried
  every lap — the cursor now skips unavailable seats AFTER lap accounting
  (skipping inside `choose_next_seat` broke the seat-0 lap boundary and spun
  uncapped runs forever), and all-seats-parked ends the run visibly as
  `starved`, never a forged turn; (2) panel fanned every draft/critique into
  every peer's backlog AND carried the collected-source packet — ~2x prompt
  tokens — fixed with `commit_reply(..., fan_out=False)` during draft/critique
  (synthesis still fans out); rows stay logged, only prompts deduplicated;
  (3) free-mode's benign "fewer than two live seats" pause was recorded
  `fatal` — it is now `starved` (outcome.py ENDED_FROM_LOOP/TERMINATION_REASONS
  carry it); (4) a supervisor plan that produced no tasks re-ran the planner on
  every resume — `supervisor_plan_attempted` latches in meta, cleared only by
  the watchdog `replan` remedy or a fresh `/objective`.

## Round-2 features (2026-08-25, docs/feature-ideas.md)

Landed as four parallel agent-team branches, merged by hand (the merge
conflicts were all "both sides appended at the same anchor" — resolve by
keeping BOTH sides; and extract git blobs for manual merging with PYTHON
subprocess, never a PowerShell pipeline, which decodes UTF-8 as cp1252 and
mojibakes every em-dash in the file).

- **Saved rooms** (#12): `relay.save_room/list_rooms/delete_room` over
  `rooms.json` beside tabs.json (derived from `SESSIONS_DIR` like `TABS_FILE`;
  overwrite semantics, `ROOMS_MAX` trim AFTER newest-first ordering because
  same-second stamps tie). Bridge: `Api.get_rooms/save_room/delete_room`
  (get_skills shape). UI: `#roomsModal` — save captures the exact Send cfg via
  `roomCfgFromStage()`, Start rebuilds through the restore machinery then
  `setSeated(false)` (a template stage is pre-conversation).
- **Event hooks** (#16): user shell commands on `question`/`checkin`/`done`/
  `gate_red`. Config beside tabs.json (`read_event_hooks`/`write_event_hooks`,
  unknown names REJECT — a typo'd hook would look configured and do nothing).
  Fired from the ONE emitter thread but on a throwaway daemon thread (10 s
  timeout, everything swallowed); env carries `AICHAT_EVENT/_SESSION/_TITLE/
  _DETAIL`. Gate results had NO event before — `wave_gate` now emits
  `gate: {ok}` for both colours; app maps only red. UI: `#hooksModal`.
- **Auto-title** (#15): one stateless side call after round 1 through
  `helper_spec` (all-Ox rooms never spend a Claude call), routed via the new
  `LoopIO.auto_title` seam whose headless default is a no-op;
  `_side_calls_enabled`/`CLIIO(title_side_calls=True)` are flipped only in
  production `main()`s, so test-instantiated engines can never spend tokens.
  `auto_titled` is stamped BEFORE the call (run_checkin rule) — once per
  session, forks inherit it. UI learns via a `session_title` event.
- **@-mention + drag-drop** (#26/#27): `relay.enqueue_josh_message` +
  `parse_mention` (longest-match ≤3 words; no match / ambiguous provider /
  bare address = literal broadcast) funnel ALL FOUR loop drain sites, so
  "@Claude 2 …" queues to one seat with verbatim text and an
  audience/delivered_to envelope, deliberately outside digest/hidden sync.
  Drop onto the composer reuses the pendingAtt chips; a dropped FOLDER cannot
  set the working folder because WebView2 never exposes absolute drop paths
  (`File.path` is Electron-only) — it refuses with a cue instead of lying.
- **Live budget bar** (#20): `record_usage` now emits a `usage` event through
  LoopIO (seam stashed as `state["_usage_io"]`, private key, never persisted);
  payload = additive totals + per-seat nullable cost, absence IS the blank for
  seats that report nothing. UI `#budgetStrip`: burn clock mirrors
  continuous_tick (accrue while running, mark resets, total doesn't),
  projection labelled ≈, none without a cap or burn.

## Testing

**Token-free first**: `tests/` holds 2221 tests across 69 suites (plus three
custom-runner suites — `test_outcome`, `test_retro`, `test_workstreams` — which print
their own `N passed` line instead of unittest's `OK`; judge those by exit code), each suite a plain script
(`python tests/test_loop.py` etc.) — FakeAgents drive the REAL loop via
`run_rounds(state, LoopIO())`; `test_app_headless.py` runs the real `app.Api`
against a fake window (flush the async emitter with `api._emit_q.join()`
before reading captured events); parallel/free suites use gated/sleeping
fakes for deterministic concurrency. Run the loop suites after ANY loop or
scheduler change — they cost nothing.

Keep Improving (no tokens): `python tests/test_continuous.py`. Live, bounded,
and cheap: the command in the CLI-knobs section above — point it at a
THROWAWAY git repo, never this one, and give it both a spend cap and a time
cap so it definitely ends.

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
Dictation (no tokens, no mic): `python tests/test_dictation.py`. The real
path is checkable without speaking — synthesize a 16 kHz clip with Windows SAPI
(`System.Speech.Synthesis.SpeechSynthesizer` + `SetOutputToWaveFile`), read its
frames, and feed them to `dictation.WhisperTranscriber().transcribe(pcm)`; on
2026-08-22 that came back verbatim. Dictation needs `sounddevice` (the one new
dependency) plus `faster-whisper`; `python -c "import dictation; print(dictation.probe())"`
says which piece is missing.

Auth checks (no tokens): `python -c "import json,relay; print(json.dumps(relay.probe_all(), indent=1))"`
— or per-CLI: `claude auth status --json` · `codex login status` · gemini =
file check (`~\.gemini\oauth_creds.json`). Re-auth: the app's Accounts panel
(Sign in button), or `claude auth login`, `codex login`, run `agy` once
interactively.
