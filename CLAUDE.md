# AI Chat — multi-AI conversation relay

Claude (Claude Code CLI, Max), GPT (OpenAI Codex CLI, ChatGPT Pro), and Gemini
(Google Antigravity CLI `agy`, free Google login) hold autonomous conversations.
**No API keys anywhere** — every agent authenticates through its official CLI's
account login. Built 2026-08-16. Owner: Josh.

## Components

| File | Role |
|------|------|
| `relay.py` | Engine + CLI. Agent adapters (`ClaudeAgent`, `CodexAgent`, `GeminiAgent`), round-robin loop, transcript writer, say.txt/stdin interjection. Interject commands: `/stop`, `/turns N`, `/clear [seat]`, `/compact [seat]` (compact = seat self-summarizes via `COMPACT_PROMPT`, then its session restarts seeded with the summary; seat arg = label or provider, omitted = all; helpers `match_seats`/`compact_agent` shared with app.py). Also owns `PROVIDERS`, the single provider registry (adapter class, color, auth probe, login/logout argv, install hint) — `AGENT_TYPES` is derived from it; grok is registered with `agent=None` (Accounts-panel-only) until its adapter lands, and adding a provider = one entry. Auth probes (no tokens spent): claude → `claude auth status --json`; codex → `codex login status` with `~/.codex/auth.json` fallback on timeout; gemini → file check of `~/.gemini/oauth_creds.json` + `google_accounts.json` (agy has NO auth subcommand; can't detect revoked tokens). Probes return `unknown` on garbage/timeout — NEVER guess signed_out. `logout_gemini` moves creds to `~/.gemini/aichat-logout-backup-<stamp>\` (restore = move the files back). `resolve_cmd`/`clean_env` are the extracted shim-resolution + env-strip helpers used by turns, probes, and app.py. `ai-chat` on PATH (`~\.local\bin\ai-chat.cmd`) calls it. |
| `app.py` | Desktop app: pywebview/WebView2 window hosting `ui/index.html`. Imports relay's adapters; its own copy of the loop emits UI events. Loop lives in `_rounds(state)`; the state dict (agents/pending/introduced/rnd/max) is kept in `self._conv` after a run ends, so `continue_chat` resumes the same CLI sessions with more rounds (`start` = fresh, `reset_conversation` = discard + mark transcript ended). `command(text)` handles the same slash commands as relay: queued to the loop when running, executed on a worker thread when idle. Accounts: `precompute_auth` (startup thread, thread-per-provider, progressive `auth_status` emits), `get_auth_status` (cache snapshot ONLY — must stay subprocess-free/non-blocking, it runs on the bridge thread), `recheck_auth`/`sign_in`/`sign_out` (bridge → worker thread). Pre-flight `_auth_blockers` gates `_conversation`/`_continue` on cached signed_out/not_installed only — unknown/pending NEVER blocks. Desktop "AI Chat" shortcut → `pythonw app.py`. |
| `ui/index.html` | Single-file UI (inline CSS/JS, local fonts only). Dynamic seat list (add/remove seats, duplicate providers allowed, "+ Add seat" row, ✕ per card) with per-seat model + thinking pickers, rounds, working-folder picker, yolo toggle, live transcript, interject bar. No topic box: the first message typed into the chat bar starts the conversation (cfg key `opener`); the header button is Stop-only, hidden while idle. After a run ends (`done` carries `can_continue`), the next non-`/` message calls `continue_chat` — same agents/sessions, +rounds, feed NOT cleared; the "New conversation" button in the after-row resets. Messages starting `/` always route to `api.command()`. Accounts live in a modal (`#acctModal`), opened by the sidebar-bottom `#acctBtn` button whose red badge counts seatable providers that are signed_out/not_installed; `renderAccounts` is fully registry-driven from the `auth_status` payload (grok shows up with install hint; seatable providers feed `PROVIDER_NAMES` + the add-seat dropdown) and each re-render clears `#acctNote` (the in-modal status/warning line used by sign-in, the logout arm, and auth blocks); seats of signed-out providers get `.noauth` dashed cue + "sign-in needed"; `sendSay` pre-flights auth via `authBlocked()` (fresh via seat cards, continue via `convProviders` captured from the `started` event), which posts the reason to the feed AND auto-opens the modal with the reason as a danger note; Log out is a two-step arm with a machine-wide warning shown in `#acctNote`. Seat DOM ids/colors key off seat id + `data-provider`. |
| `launcher.ps1` | Console launcher (prompts for topic). ASCII-only on purpose. |
| `sessions/` | One folder per conversation: `transcript.md`, `workspace/`, `say.txt`. |
| `make_icon.py` / `ai-chat.ico` | Icon generator (Pillow) and the generated icon. |

Also installed elsewhere: `ai-chat` skills in `~\.claude\skills\ai-chat\` and
`~\.codex\skills\ai-chat\` (so either AI can run conversations on request).
If paths here change, update those skills + the desktop shortcut + `~\.local\bin\ai-chat.cmd`.

## CLI knobs (relay.py)

`ai-chat "topic" --turns N --agents claude,gpt,gemini --start X --yolo
--claude-model <id> --claude-effort low|medium|high|xhigh|max
--gpt-model <id> --gpt-effort low|…|ultra --gemini-model <agy slug>`
Claude ids (all verified on this account): claude-fable-5, claude-opus-5,
claude-opus-4-8, claude-sonnet-5, claude-haiku-4-5 (aliases opus/sonnet/haiku ok).
Defaults: all three agents, 10 rounds, Opus 4.8 / gpt-5.6-sol(high) / gemini-3.7-flash-high.
The app reads live model lists: GPT from `~\.codex\models_cache.json` (+ defaults
from `config.toml`), Gemini from `agy models`; Claude list is pinned in app.py.

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

## Hard-won gotchas (do not relearn these)

- **npm shims**: `codex`/`gemini` are `.cmd` shims — CreateProcess can't run them
  and `cmd /c` TRUNCATES MULTI-LINE ARGS at the first newline. relay resolves the
  shim to `node <script>.js` and runs node directly.
- **claude CLI**: prompt must be the LAST positional (required with `--resume`);
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
- **Wrap token**: `wrap_called(reply)` fires only if `[[WRAP]]` is in the LAST
  non-empty line. A bare `WRAP_TOKEN in reply` let a seat end the conversation by
  merely discussing the token. The preamble wording must stay in sync.
- **Two loops**: `relay._rounds`-equivalent and `app.Api._rounds` are duplicated
  round-robin implementations. Adapter fixes propagate for free; anything
  loop-shaped must be written TWICE or it only works in the terminal. Extracting
  one shared loop (emit callback per front end) is the standing next refactor.

## Testing

Cheap end-to-end: `python relay.py "test" --turns 1 --claude-model haiku
--claude-effort low --gemini-model gemini-3.7-flash-low --gpt-effort low`.
Duplicate seats cheap: `python relay.py "test" --turns 1
--agents claude:claude-haiku-4-5:low,claude:claude-haiku-4-5:low`.
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
