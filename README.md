# Alloy — AI-to-AI conversation relay

*Different metals. One alloy.* Many models, one conversation.
(The repo, CLI and skills keep the `ai-chat` name on disk — only the brand
changed. See `BRANDING.md`.)

Kick off a conversation between **Claude** (Claude Code CLI, your Max account),
**GPT** (OpenAI Codex CLI, your ChatGPT Pro account), **Gemini** (Google
Antigravity CLI, free Google-account login) and **OpenCode** (its Zen gateway's
free models — Ox Alpha and friends, no account at all). No API keys — each agent
authenticates through its official CLI's account login. You start it, they talk;
you can jump in anytime.

## Getting started

Open the app, pick how the room should work, optionally choose where it works,
and start typing — everything else has a sensible default.

- **Pick a mode from the pill beside the chat box.** Five recipes cover most
  rooms: **Discuss in Turns** (the default — orderly round-robin conversation),
  **Talk Live** (everyone replies whenever ready — faster, less orderly),
  **Compare & Decide** (separate answers and critiques, then one final
  recommendation), **Build Together** (work is split into parallel tasks whose
  files are verified on disk), and **Keep Improving** (works on your project
  non-stop, choosing its own next objective, until you stop it — it asks
  before starting because it never ends by itself). Fine-tuning lives in the
  Conversation controls in the seat rail; the pill locks once a chat begins.
- **Choose a working folder** (optional). By default seats share a private
  scratch folder created per conversation. Point them at one of your real
  project folders instead (**Working folder → Choose**) and they read its
  files, share its AI docs as common context, and (in Build Together / Keep
  Improving) create and edit real files there. A permission picker right below
  decides how much they may touch: Read only, Ask first, Workspace, or Full
  access. Chats sharing a folder group together in the left rail.
- **Learn the slash commands** (type them in the chat bar anytime):
  `/stop` ends gracefully; `/turns N` changes the round cap mid-run
  (`/ceiling N` in until-done chats); `/clear [seat]` wipes a seat's memory;
  `/compact [seat]` has a seat summarize-and-restart to shrink context;
  `/retro` aggregates past sessions into the editable playbook; and Keep
  Improving adds `/limits`, `/checkin` and `/objective <text>`. `/help` lists
  them. Anything that doesn't start with `/` joins the conversation itself.

That's the whole loop: preset → (optional) folder → first message. The rest of
this file is depth, not prerequisites.

## Three ways to start a conversation

1. **Desktop app (Alloy)** — double-click **AI Chat** on the Desktop (the
   shortcut keeps its old on-disk name). A native window
   (`app.py`, pywebview/WebView2) with a seat card per participant: toggle who's
   in, pick each one's model and thinking level, click the seat's name to give
   it a custom one ("Optimist" instead of "Claude 2"), and the **Role** button
   opens an editor for an optional role (a public role name plus private
   instructions only that seat sees — with presets); set rounds and choose the
   working folder they operate in. The **Conversation** controls below the
   seats set how the conversation runs: **Turn order** (round-robin, speaker
   picks next, moderator decides, parallel, free-running — picking "moderator
   decides" reveals a picker for which AI moderates: provider, model, and
   thinking level, defaulting to a cheap Claude), **Until done** —
   which turns the rounds stepper into a safety ceiling and lets the agents
   decide when the task is finished — and how many **Helpers** (one-shot AIs
   a seat can spawn) and **Teams** (whole sub-conversations that report back)
   they're allowed. These are locked once a conversation starts and restored
   when you reopen a chat. In **Supervisor** mode, a collapsible control log
   above the transcript shows the planner's public rationale, exact task briefs,
   routing changes, filesystem verification, and bounded repair attempts live;
   the log is saved with the session and explicitly excludes private model
   reasoning. There's no topic box — type your opening
   message in the bottom bar and hit **Send** to start the conversation (it's
   delivered to every seat as your kickoff); anything you type after that joins
   as an interjection, and the header shows a **Stop** button while it runs.
   The message box starts several lines tall, grows as you type, and the grab
   bar above it drags it to any height; the 📎 button (or pasting a screenshot
   straight into the box) attaches files, which are saved into the working
   folder and pointed out to every seat. Each sidebar collapses with the **«**
   button at its top — a collapsed bar becomes a slim strip; click it to bring
   the bar back. The ✎ next to a seat's name (or just clicking the name)
   renames that seat. The
   **"+ Add seat"** row adds more seats of any provider (two Claudes,
   2×Claude + 2×GPT, etc. — auto-named "Claude", "Claude 2", …) and the ✕ on a
   card removes it. Live transcript with per-speaker colors; Transcript/Folder
   buttons when it ends. Images the agents mention or you attach render as
   inline thumbnails right in the chat (click one for a full-size lightbox
   view), a **Files** rail on the right lists everything in the working folder
   newest-first with previews and an open-in-Explorer button, and every
   message has a copy button and selectable text. When the rounds run out the conversation only
   **pauses**: type another message to continue it (same participants, same
   memory, another batch of rounds), or hit **New conversation** to start
   fresh. The left chat rail lists every saved conversation on a single line
   each, grouped under collapsible project headers (chats sharing a picked
   working folder group together; the rest sit under "No project"); click one
   to replay it, then reply to resume the agents' saved CLI sessions even after
   closing and reopening the app. Titles can be renamed in place (double-click)
   and deletion uses a two-click confirmation. Older transcript-only runs
   remain available to read but are marked **view only**. Slash commands work
   in the chat bar anytime — see below.
   The **Accounts** section in the sidebar shows each provider's sign-in state
   (checked at launch, ↻ to re-check): **Sign in** opens a terminal running
   that CLI's own browser login (Claude → Anthropic account, GPT → ChatGPT,
   Gemini → Google, Grok → SuperGrok/X Premium+ once its CLI is installed —
   the install command is shown when a CLI is missing). **Ox** is the
   exception: its free Zen models need no sign-in, so it reports ready as soon
   as the CLI is installed, and signing in only adds the paid Zen catalog. **Log out** (two-click
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
| `--agents a,b` | all three | Who's in the room. Each token is `provider[:model[:effort]][=label]` with providers `claude`, `gpt`, `gemini`, `ox` — repeat a provider for duplicate seats (e.g. `claude:opus:high,claude:haiku:low`, or `"claude=Optimist,claude=Skeptic"`; auto labels: "Claude", "Claude 2") |
| `--start X` | first listed | Who speaks first: slot number (1-based), label (`"claude 2"`), or provider |
| `--role "SEAT=NAME"` | none | Public role name for a seat, shown to every seat in the roster line; repeatable. `SEAT` is the same label-or-provider grammar as `/clear`; a typo is a hard error, not a silent no-op |
| `--role-instructions "SEAT=TEXT"` | none | Private role instructions only that seat sees; repeatable, same `SEAT` grammar |
| `--permission LEVEL` | auto | Permission profile: `read_only`, `ask`, `auto` (sandboxed workspace), or `full` (no sandbox/approvals) |
| `--yolo` | off | Backward-compatible alias for `--permission full` |
| `--workspace PATH` | fresh scratch dir | Run the conversation inside an existing project folder instead of a private scratch dir. The folder's AI docs then become shared context for every seat (see below) |
| `--no-brief` | on | Skip that shared context and leave each seat with whatever its own CLI happens to load |
| `--preset P` | none | Goal-first recipe, overriding `--mode`: `open-discussion`, `panel-review`, `build-execute`, `live-room`, or `keep-improving` (Build Together with the brakes off — pair it with `--continuous`) |
| `--mode M` | round-robin | Orchestration: `round-robin` (fixed), `speaker` (each reply ends with `[[NEXT: seat]]` naming who goes next), `moderator` (a cheap side call picks each turn), `supervisor` (a stateless planner decomposes the goal into isolated concurrent workstreams with capability gating and filesystem verification), `parallel` (everyone answers at once in simultaneous rounds), `free` (seats reply whenever messages arrive, interleaved live) |
| `--moderator P` | claude:claude-haiku-4-5:low | Who moderates in `--mode moderator` (`provider[:model[:effort]]`); not a seat — one cheap stateless call per turn, and it can call the conversation DONE |
| `--until-done` / `--ceiling N` | off / 60 | No round cap: run until a seat wraps (or the moderator says DONE), hard-stopped at N total turns as the spend backstop; `/ceiling N` adjusts mid-run |
| `--continuous` | off | **Keep Improving.** No round cap and no turn ceiling: when the manager judges the current objective met it chooses the NEXT one itself and keeps going. Only your Stop button and the limits below end it |
| `--checkin-minutes N` | 30 | How often a cheap watchdog call checks the run is still committing turns and nothing is wedged, and repairs it if not (5–1440) |
| `--checkin-action A` | notify | What the watchdog may do: `auto` (fix and log), `notify` (fix, log and raise attention), `permission` (change nothing until you approve — **the run waits**) |
| `--spend-cap USD` | none | Pause once the run has provably cost this much. Only the CLIs that report cost are counted, so Gemini and OpenCode seats never appear in the figure |
| `--time-cap HOURS` | none | Pause after this many hours of run time, accumulated across resumes |
| `--no-watchdog-stop` | on | The watchdog may repair the run but never end it |
| `--gate CMD` / `--no-gate` | detected | Verification command run in the working folder at the end of each round of work, before the manager reviews it, so the manager reads a result rather than a claim. Detected from the folder (`tests/run_all.py`, then pytest, then `npm test`); a folder with none records a SKIP, never a pass |
| `--gate-commit` | off | `git commit` the working folder after each round whose verification passed. Refused if the tree already had your own uncommitted changes when the run started |
| `--spawn-helpers N` | 0 | Let seats spawn up to N one-shot helper AIs: a reply ending `[[SPAWN: provider[:model[:effort]] \| task]]` runs a helper in the shared workspace while the conversation continues; the result returns only to the requester |
| `--spawn-teams N` | 0 | Let seats spawn up to N whole sub-conversations: `[[TEAM: seats \| rounds=N mode=m \| task]]` runs a child chat (its own transcript, reopenable from the app's rail, marked ↳) that reports its outcome back to the requester |
| `--no-native-subagents` | on | Stop telling seats they may use their CLI's built-in subagent tools (Claude's Task tool, Codex multi-agent) |
| `--no-ask` | on | Stop telling seats they may put a question to you: a reply ending `[[ASK: question \| option A \| option B]]` PAUSES the conversation until you answer (app: a popup with option buttons, an "Other" box and Skip; CLI: a console prompt where a number picks an option). The answer is shared with every seat; an unanswered question resumes the chat with a note |
| `--claude-model` / `--claude-effort` | Opus 5 / high | `claude-fable-5`, `claude-opus-5`, `claude-opus-4-8`, `claude-sonnet-5`, `claude-haiku-4-5` (aliases `opus`/`sonnet`/`haiku` also work) · `low\|medium\|high\|xhigh\|max` |
| `--gpt-model` / `--gpt-effort` | gpt-5.6-sol / high (config.toml) | any Codex model · `low`…`ultra` (model-dependent; app reads the live list from `~\.codex\models_cache.json`) |
| `--gemini-model` / `--gemini-effort` | gemini-3.7-flash-high / in slug | see `agy models` for slugs |

The startup banner shows exactly which model each participant is running.
Example — heavyweight debate: `ai-chat "topic" --claude-effort high --gemini-model gemini-3.1-pro-high`.
Example — cheap fast chat: `ai-chat "topic" --claude-model haiku --gpt-effort low --gemini-model gemini-3.7-flash-low`.
Example — Claude vs Claude: `ai-chat "topic" --agents claude:claude-opus-4-8:high,claude:claude-haiku-4-5:low`.
Example — role team: `ai-chat "build it" --agents "claude=Researcher,gpt=Coder,claude=Reviewer" --role-instructions "Researcher=Find and verify facts; cite them; don't write code" --role-instructions "Coder=Implement the agreed design in the workspace" --role-instructions "Reviewer=Review only: file, line, problem, fix"` (here the seat labels already read as roles; add `--role` as well when you want the explicit "Roles:" roster line in every preamble).

## Talking about a real project

Point a conversation at a project folder — `--workspace C:\my\project`, or the
**Working folder → Choose** button in the app — and the agents run inside it,
reading its files.

There's a catch worth knowing, because it used to bite silently. Each CLI only
auto-loads *its own* doc from that folder: Claude reads `CLAUDE.md`, GPT reads
`AGENTS.md`, Gemini reads `AGENTS.md`/`GEMINI.md`. Most repos have one of those,
not three — so in a repo with only a `CLAUDE.md`, the Claude seat would arrive
having read your whole project and the other two would arrive knowing nothing,
with no sign of it in the transcript. One seat sounds authoritative, the others
guess.

So ai-chat now gives all of them the same context:

- **Small doc sets are quoted verbatim** to every seat. Free, nothing is
  written anywhere, and each seat is told which docs it already had.
- **Doc sets too large to quote** get summarized once into `AI-CHAT.md` in the
  project folder, tagged with the source files' hashes. Later chats reuse it for
  free and only rebuild it when you actually edit those docs — and the chat says
  so when it does. It's a generated cache (worth adding to `.gitignore`); the
  header inside it says as much.

If the summary can't be built, the seats are told that plainly and pointed at
the docs — nothing is invented. Reopening an old chat replays the context it was
originally given; if the docs have moved on since, you get a notice rather than a
silent swap. Untick **Share the folder's AI docs with every seat** (or pass
`--no-brief`) to turn the whole thing off.

Agents are also told a chosen folder is your real project and not to change
files there unless you ask. Choose **Read only**, **Ask first**, **Workspace**,
or **Full access** in the app. `--permission full` (and its older `--yolo`
alias) removes the guardrails entirely, so point it at a git repo you can
`git diff`.

## When a provider wobbles

Free and preview endpoints drop requests. Alloy treats that as a different
thing from a bug of its own: a failure that reads as the provider (`network_error`,
`Endpoint is unavailable`, 429/503/…) gets a short pause and then a **2-minute**
window for its one retry, instead of an instant re-hit followed by the full
effort-scaled watchdog — which, on `--effort max`, is fifteen minutes of a
conversation looking dead. Failures that will never heal on their own (a dead
CLI session id, a missing CLI, an auth problem) are excluded and still fail
immediately. While a seat is mid-turn its indicator shows `11:23 of 15:00`, so
a slow turn is visibly a slow turn; reopening the chat keeps that clock rather
than resetting it.

## When the app closes unexpectedly

A chat whose PROCESS died mid-run — a force quit, a power cut, the seats
restarting the app on themselves — is reopened *and* picked up again the next
time you launch. Every other ending (a wrap, the round cap, your Stop, a spend
limit) was a decision, so those chats are reopened and left alone. Two
automatic resumes that produce no turns block the third, so a chat that
crashes on resume cannot loop.

## While it's running

- **Type anything + Enter** — injected into the conversation as "Josh (human)"
  at the next turn boundary; every participant sees it.
- **`/stop`** — graceful end. **`/turns N`** — change the round cap mid-run
  (**`/ceiling N`** instead, in an until-done conversation).
- In a Keep Improving run: **`/limits`** prints what will actually stop it,
  **`/checkin`** runs a health check at the next turn boundary, and
  **`/objective <text>`** steers what it works on next.
- **`/clear [seat]`** — wipe a seat's context; it rejoins fresh (re-introduced,
  no memory). **`/compact [seat]`** — the seat writes its own summary of the
  conversation, then restarts from just that summary (shrinks a long context
  without losing the thread). Seat = a label like `claude 2` or a provider
  (`claude`/`gpt`/`gemini`); omit it to hit every seat. `/help` lists these.
  In the app these also work while paused between batches.
- **`/retro`** — aggregate finished sessions' `outcome.json` records into the
  human-editable `sessions/playbook.json` and print the active,
  provenance-backed coordination heuristics. Explicit feedback reasons count
  immediately; inferred patterns require recurrence. Unpinned rules expire
  after 30 days, while pinned and dismissed choices survive refreshes.
- **`Ctrl+C`** — hard stop; transcript is still saved.
- Remote interjection: write text into `sessions\<run>\say.txt` (from another
  Claude session, SSH, the phone…) — same effect as typing.
- **Questions to you**: when a seat needs your decision it can end a reply
  with `[[ASK: …]]` — the app pops a question box (option buttons + an
  "Other" box + Skip) and the conversation waits for you; closing the box
  leaves a "waiting for your answer" pill above the composer to reopen it.
  In the terminal it's a console prompt (a number picks an option; say.txt
  works too). Seats are also told to mark the one key line of a reply with
  `==double equals==` — the app renders it highlighted, and trailing
  directives show as small chips instead of raw brackets.

## Searching your chats

The box at the top of the left chat rail (**Search all chats…**) does
full-text search across every saved conversation — not just titles:

- **What it searches** — chat titles and message text: the structured
  `messages.jsonl` rows for app-era chats, falling back to `transcript.md`
  for legacy transcript-only chats (those open view-only, but they stay
  findable). A chat whose log was damaged by a crash — intact rows beside
  lines that no longer parse — is read from its transcript instead, so a
  half-eaten chat stays findable. A currently-running chat is searchable as
  far as its files have been written, since both are appended live. A title
  match lists the chat even when no message body contains the words.
- **Syntax** — plain case-insensitive substring, nothing fancier: no regex,
  no quotes, no fuzzy matching. At least two characters (a one-letter query
  matches half the history), and pasted multi-word text works because
  surrounding whitespace is collapsed.
- **Results** — the rail swaps to a results page: title matches first, then
  most hits, newest among ties. Each row shows provider dots, the chat's
  project, up to three snippet excerpts, and the hit count; a match on the
  title alone says "title match" rather than a number, and a spawned team
  keeps its **↳** marker in results just as it has in the rail. Clicking a
  row opens that conversation scrolled to and flashing the first matching
  message (it just opens normally if the match sits in text the renderer
  skips). **✕ or Escape** brings the normal rail back.
- **Limits** — hit counting stops at 999 occurrences per chat and at most
  40 chats are listed (when more than 40 match, the header says so:
  "40 chats shown (more match)"); only conversation text is searched, never
  workspace files or attachments.

## Where things land

Each run creates `sessions\<timestamp>-<topic>\` (in the app, the folder is
named from your opening message):

- `transcript.md` — the whole conversation, appended live.
- `messages.jsonl` — structured message/system rows used for exact UI replay.
- `meta.json` — participant settings, pending queues, round state, and each
  CLI's session id; this is what makes restart-safe continuation possible.
- `workspace\` — a scratch folder all participants share; they can co-write files
  there (ask them to "write findings.md" etc.). Absent when you chose your own
  working folder — that folder is used directly.
- `project-context.md` — only when the chat used a chosen working folder: the
  exact shared-context text the seats were given, kept so reopening the chat
  replays what they actually saw.

Terminal-created chats use the same files and appear in the desktop app's chat
rail, so a terminal conversation can be reopened there too. A conversation
spawned by a seat (a **team**) is an ordinary session folder as well: it shows
up in the rail marked **↳** with a "spawned by …" tooltip, and it replays like
any other chat.

## How it ends

After `--turns` rounds (in `speaker`/`moderator`/`free` modes that's a budget
of turns × seats), or earlier if an agent ENDS a reply with `[[WRAP]]` (the
others each get one closing remark — in parallel mode, one closing round), or
when you `/stop`. With `--until-done` there's no round cap at all: the agents
decide when the task is finished, bounded by the `--ceiling` turn limit. In
the app all of these are just a pause — replying continues the same
conversation until you press **New conversation**. Closing the app is safe:
reopen the chat from the left rail and reply to continue it.

**Keep Improving does not end.** That is the point of it: when the manager
judges an objective met it picks the next one instead of stopping, and if the
conversation falls over anyway — a cap, a fatal seat, a wrap nobody asked for —
it is restarted (three restarts in a row that commit nothing stop it loudly
rather than spinning). What ends it is your Stop button, plus whichever of the
spend cap, the time cap and "the check-in may stop it" you turned on. Turning
all three off is allowed, and the warning modal says so in those words.

## Self-improvement / restart

The seats can improve Alloy itself: they edit code between turns, prove the
edit, and the app relaunches on it without losing the conversation.

What a seat runs is `python restart.py` (repo root; standalone, stdlib only).
It always gates first — `tests/run_all.py` plus an `import app` smoke, all
token-free — and aborts loudly on any red, so unproven code never takes the
running app down. It then finds `pythonw.exe <path>\app.py` instances, refuses to
touch the one hosting the calling session (host guard) or any candidate with
no visible Alloy window (ownership guard), stops targets gently (`taskkill`
posts WM_CLOSE), relaunches exactly one detached `pythonw app.py`, verifies
the new process stays up, and prints the newest `sessions\<id>` to reopen.
`--dry-run` prints the plan and touches nothing.

Between turns nothing is lost by design: every committed turn atomically
updates `meta.json`, and the transcript/message files are append-only, so the
worst case of a stop is "one fewer message", never a forged or half-saved
one. A stop *mid-turn* is still amputation — the gentle close is only gentle
while the room is idle; honoring a restart request at a turn boundary, plus
the handoff marker, single-instance mutex and auto-reopen of the newest
resumable chat, is the designed next step (see RESTART_DESIGN.md).

Reopening continues the same conversation: open the printed or rail-listed
session, and the app rebuilds every seat off disk alone — models, roles,
each CLI's session id, owed reply queues, round state, shared project
context — then your next message resumes through the normal continue path.
The continuity contract itself is pinned token-free by `tests/test_restart.py`.

Manual fallback: if the detached child died before coming up, run
`pythonw app.py` in the repo root and reopen the newest chat from the rail.
Sessions are crash-safe by construction (a truncated JSONL tail line is
skipped, never fatal), so even a hard kill costs at most the in-flight turn.

## Tools the agents get

Four permission profiles are available. **Read only** disables writes and shell
execution. **Ask first** routes Claude's individual write/command tools through
an approval card; because Codex and Gemini's print-mode CLIs have no equivalent
pre-tool hook, Alloy asks once before each potentially mutating turn and runs a
denied turn read-only. **Workspace** is the default: Claude uses accepted edits,
Codex uses its `workspace-write` sandbox with network on, Gemini stays in its
terminal sandbox, and Ox denies `external_directory` so writes land in the
working folder and nowhere else. **Full access** removes those guardrails on all providers and
is only for work where arbitrary commands are acceptable.

### Your screen and the web

Two further switches, each independent of the permission profile above — that
one bounds the *working folder*, these bound your *desktop* and the *open web*.
Both are off by default, and anything Alloy doesn't recognise reads as off.

**Desktop control** (`--desktop ask|allowlist|full`, or the sidebar picker) lets
seats read a window's controls and click, type and scroll in it. The pointer
never moves and windows are never brought to the front. Alloy always refuses its
own windows and password fields.

**Browser control** (`--browser read|ask|full`) gives a seat a real Chrome,
limited to the sites you list (`--browser-site "https://example.com/*"`, or the
box under the picker). The list is an **allowlist enforced inside Chrome**, not a
suggestion: anything you don't list — including your own files and this machine's
own ports — is blocked by the browser's network stack, so it holds even against a
page's own scripts. Leave the list empty and the browser opens but reaches
nothing. **Look only** reads pages and can't click, type or run scripts (though opening a page is still a real request); **Ask before acting**
stops for you on every click and keystroke; **Unattended** doesn't stop at all
and asks you to acknowledge that before it turns on. Chrome runs on a fresh,
empty profile, so nothing you're signed into elsewhere carries over.

One caveat Alloy states rather than hides: those two ladders are *enforcing*
controls only while the permission profile is **Read only** or **Ask first**. At
**Workspace** and **Full access** the seat already has a shell, and a shell can
go around them. The browser's site list is the exception — it lives inside
Chrome and holds at every profile. The app says so under the pickers.

Browser and desktop control reach **Claude seats** today; the other CLIs have no
equivalent per-conversation route, and the seats are told the truth about that.

Seats are also told they may use their own CLI's built-in subagents (Claude's
Task tool, Codex's multi-agent mode) for small side-tasks inside a turn — but
only when their configuration actually grants it, so the relay never promises
a capability a seat doesn't have. Turn that off with `--no-native-subagents`.
Relay-spawned **helpers** and **teams** are separate and off unless you enable
them (`--spawn-helpers N` / `--spawn-teams N`, or the sidebar controls), since
each one spends real account usage.

## Notes

- **Gemini** rides Google's **Antigravity CLI** (`agy`, installed at
  `%LOCALAPPDATA%\agy\bin`) — the successor to the retired Gemini CLI. Free
  Google-account tier, no API key. Its piped *text* output is broken on Windows,
  so the adapter uses `--output-format json`, which works.
- **OpenCode** is a gateway, not a single model: the provider rides the
  **OpenCode CLI** (`npm install -g opencode-ai`) against OpenCode Zen and
  ships with Zen's *free* models — Ox Alpha (a 1M-context stealth preview),
  Big Pickle, Nemotron ×2, MiMo, Hy3 and Muse Spark — none of which need an
  account, a key or a login. Each **seat is named for the model it runs**
  ("Ox Alpha", "Nemotron 3 Ultra"), so a room can hold several at once and the
  transcript says who spoke. **Thinking levels come from each model**: Ox
  Alpha has low/high/max, Muse Spark five, Hy3 three, and Nemotron, MiMo and
  Big Pickle none at all — where a model has none the Thinking box disappears
  instead of offering settings it would silently ignore. Ox Alpha is a preview
  and will eventually be withdrawn; when it goes, `opencode models` lists what
  is still there and the dropdown offers whatever survives.
- **Who moderates**: **Let an AI moderate** is a checkbox in the Conversation
  section (not in Advanced) — tick it and the picker appears right below, with
  a name box: leave it blank for "Moderator"/"Supervisor", or call it Referee
  and every status line, the Supervisor control log and its transcript row say
  Referee. It
  offers every provider you can seat, so a room can be run entirely by one model — e.g. Ox in every seat
  *and* as supervisor, which costs nothing. The relay's own side call (the
  project brief) follows the same rule: it uses the moderator, or failing that
  the first seat, rather than always reaching for Claude.
- **Usage/cost**: each round spends one invocation per participant (Claude Max,
  ChatGPT Pro, Google free tier, Ox free). A 10-round chat ≈ a modest coding session on each.
  Moderator mode adds one cheap call per turn; each spawned helper is one more
  call, and a spawned team is a whole extra conversation — that's why helpers
  and teams are off by default and capped, and why `--until-done` always has a
  turn ceiling.
- **Auth upkeep**: the app's **Accounts** panel shows each provider's sign-in
  state and has Sign in / Log out buttons (log out + sign in = switch account;
  note logout is machine-wide for that CLI). From a terminal instead:
  `claude auth login`, `codex login`, or run `agy` once interactively. Ox needs
  none of this unless you want Zen's paid models (`opencode auth login`).
