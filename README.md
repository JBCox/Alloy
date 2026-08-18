# ai-chat — AI-to-AI conversation relay

Kick off a conversation between **Claude** (Claude Code CLI, your Max account),
**GPT** (OpenAI Codex CLI, your ChatGPT Pro account), and **Gemini** (Google
Antigravity CLI, free Google-account login). No API keys — each agent
authenticates through its official CLI's account login. You start it, they talk;
you can jump in anytime.

## Three ways to start a conversation

1. **Desktop app** — double-click **AI Chat** on the Desktop. A native window
   (`app.py`, pywebview/WebView2) with a seat card per participant: toggle who's
   in, pick each one's model and thinking level, set rounds, choose the working
   folder they operate in. There's no topic box — type your opening message in
   the bottom bar and hit **Send** to start the conversation (it's delivered to
   every seat as your kickoff); anything you type after that joins as an
   interjection, and the header shows a **Stop** button while it runs. The
   **"+ Add seat"** row adds more seats of any provider (two Claudes,
   2×Claude + 2×GPT, etc. — auto-named "Claude", "Claude 2", …) and the ✕ on a
   card removes it. Live transcript with per-speaker colors; Transcript/Folder
   buttons when it ends. When the rounds run out the conversation only
   **pauses**: type another message to continue it (same participants, same
   memory, another batch of rounds), or hit **New conversation** to start
   fresh. Slash commands work in the chat bar anytime — see below.
   The **Accounts** section in the sidebar shows each provider's sign-in state
   (checked at launch, ↻ to re-check): **Sign in** opens a terminal running
   that CLI's own browser login (Claude → Anthropic account, GPT → ChatGPT,
   Gemini → Google, Grok → SuperGrok/X Premium+ once its CLI is installed —
   the install command is shown when a CLI is missing). **Log out** (two-click
   confirm) signs that CLI out *machine-wide* — that's also how you switch
   accounts: log out, then sign back in with the other account. Seats whose
   provider isn't signed in are flagged "sign-in needed" and starting a
   conversation is blocked with a clear message instead of a mid-run error.
2. **Terminal** — `ai-chat "topic or task for them to discuss"` (all the same
   controls as flags; `launcher.ps1` is the prompt-driven console version).
3. **Ask an AI** — tell Claude Code *or* Codex "have the AIs discuss X":
   both have an `ai-chat` skill installed (`~\.claude\skills\ai-chat`,
   `~\.codex\skills\ai-chat`) that runs the relay, forwards your mid-run
   messages via `say.txt`, and summarizes the transcript when it ends.

## Usage

```
ai-chat "topic or task for them to discuss"
```

Options:

| Flag | Default | Meaning |
|------|---------|---------|
| `--turns N` | 10 | Max rounds (each round = every agent speaks once) |
| `--agents a,b` | all three | Who's in the room. Each token is `provider[:model[:effort]][=label]` with providers `claude`, `gpt`, `gemini` — repeat a provider for duplicate seats (e.g. `claude:opus:high,claude:haiku:low`, or `"claude=Optimist,claude=Skeptic"`; auto labels: "Claude", "Claude 2") |
| `--start X` | first listed | Who speaks first: slot number (1-based), label (`"claude 2"`), or provider |
| `--yolo` | off | Full autonomy incl. shell access (use with care) |
| `--claude-model` / `--claude-effort` | Opus 4.8 / high | `claude-fable-5`, `claude-opus-5`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-haiku-4-5` (aliases `opus`/`sonnet`/`haiku` also work) · `low\|medium\|high` |
| `--gpt-model` / `--gpt-effort` | gpt-5.6-sol / high (config.toml) | any Codex model · `low`…`ultra` (model-dependent; app reads the live list from `~\.codex\models_cache.json`) |
| `--gemini-model` / `--gemini-effort` | gemini-3.7-flash-high / in slug | see `agy models` for slugs |

The startup banner shows exactly which model each participant is running.
Example — heavyweight debate: `ai-chat "topic" --claude-effort high --gemini-model gemini-3.1-pro-high`.
Example — cheap fast chat: `ai-chat "topic" --claude-model haiku --gpt-effort low --gemini-model gemini-3.7-flash-low`.
Example — Claude vs Claude: `ai-chat "topic" --agents claude:claude-opus-4-8:high,claude:claude-haiku-4-5:low`.

## While it's running

- **Type anything + Enter** — injected into the conversation as "Josh (human)"
  at the next turn boundary; every participant sees it.
- **`/stop`** — graceful end. **`/turns N`** — change the round cap mid-run.
- **`/clear [seat]`** — wipe a seat's context; it rejoins fresh (re-introduced,
  no memory). **`/compact [seat]`** — the seat writes its own summary of the
  conversation, then restarts from just that summary (shrinks a long context
  without losing the thread). Seat = a label like `claude 2` or a provider
  (`claude`/`gpt`/`gemini`); omit it to hit every seat. `/help` lists these.
  In the app these also work while paused between batches.
- **`Ctrl+C`** — hard stop; transcript is still saved.
- Remote interjection: write text into `sessions\<run>\say.txt` (from another
  Claude session, SSH, the phone…) — same effect as typing.

## Where things land

Each run creates `sessions\<timestamp>-<topic>\` (in the app, the folder is
named from your opening message):

- `transcript.md` — the whole conversation, appended live.
- `workspace\` — a scratch folder all participants share; they can co-write files
  there (ask them to "write findings.md" etc.).

## How it ends

After `--turns` rounds, or earlier if an agent includes `[[WRAP]]` in a reply
(the others each get one closing remark), or when you `/stop`. In the app that's
just a pause — replying continues the same conversation until you press
**New conversation** (or close the app).

## Tools the agents get

Sandboxed by default: web search/fetch + read/write inside the shared workspace.
Claude runs with `--permission-mode acceptEdits` and an allow-list; Codex runs in
its `workspace-write` sandbox with network on; Gemini runs agy with auto-approved
tools inside agy's terminal sandbox. `--yolo` removes the guardrails on all of
them — only for topics where you're happy with them running arbitrary commands.

## Notes

- **Gemini** rides Google's **Antigravity CLI** (`agy`, installed at
  `%LOCALAPPDATA%\agy\bin`) — the successor to the retired Gemini CLI. Free
  Google-account tier, no API key. Its piped *text* output is broken on Windows,
  so the adapter uses `--output-format json`, which works.
- **Usage/cost**: each round spends one invocation per participant (Claude Max,
  ChatGPT Pro, Google free tier). A 10-round chat ≈ a modest coding session on each.
- **Auth upkeep**: the app's **Accounts** panel shows each provider's sign-in
  state and has Sign in / Log out buttons (log out + sign in = switch account;
  note logout is machine-wide for that CLI). From a terminal instead:
  `claude auth login`, `codex login`, or run `agy` once interactively.
