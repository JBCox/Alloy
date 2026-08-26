# RESTART_DESIGN — the self-improvement loop

How Alloy edits itself while a conversation is running, proves the edit is
safe, stops, relaunches on the new code, and drops the team back into the
same conversation mid-flight.

Owner: this doc (RESTART_DESIGN.md). Wave-2 implementation touches `app.py`,
`relay.py` and `ui/index.html` — see §8 for the exact worklist per file.

---

## 1. The loop

One improvement wave:

```
seats propose + edit code  ->  GATE (tests/run_all.py, token-free)
                           ->  green checkpoint (git commit)
                           ->  controlled stop at a turn boundary
                           ->  handoff marker written atomically
                           ->  child process relaunches (detached)
                           ->  auto-reopen newest resumable session
                           ->  system note in transcript; conversation resumes
```

The critical insight: **the hard part already exists and works.** A new
process can continue any conversation today — `Api.open_session` rehydrates
agents, restores CLI session ids, queues, scheduler state and project context.
What is missing is only the *automatic* part: nobody calls `open_session`
at process start, nothing performs a controlled self-stop, and there is no
handoff between two instances of the app. This design adds exactly those
three pieces and nothing more.

## 2. What already exists (verified in code — build on this, don't rebuild)

| Piece | Where | Guarantee |
|---|---|---|
| Resume validation | `relay.continue_block(meta)` | Refuses legacy/foreign-version/orphaned metas with a human-readable reason |
| Agent rebuild | `relay.rehydrate(meta)` | Object construction only, safe anywhere; restores provider/model/effort/label/role/connectors, per-seat `session_id`, pending queues by slot id, cursor, floor state, workstreams, supervisor waves, usage |
| Reopen path | `app.Api.open_session(session_id)` | Validates path via registry first, rehydrates under a `can_continue` guard, patches `state["brief"] = read_project_context(...)` (replays RECORDED text verbatim) and `state["_run"]`; refuses double-loading a live session id |
| Continue path | `app.Api._continue(cfg)` | Revives chat from a fresh message; clears stale `closing`/`next_speaker`/`deferred_wrap`; extends rounds or until-done ceiling; reports `brief_drift` without regenerating |
| Durable state | `SessionStore.save(state)` after every committed turn | meta.json v2, `_atomic_write` with `os.replace` retry against concurrent readers; commit-consume queue invariant means the file is valid at every instant |
| Crash-tolerant replay | `read_messages` | Truncated final JSONL line skipped, never fatal |
| Test gate | `tests/run_all.py` | Every suite in a fresh subprocess; exit 0 = all green; prints reproducible suite/test totals; zero CLI calls, zero tokens |
| Outcome record | `outcome.json` via `finally` in `run_rounds` | Written on every exit path including crashes |

Two facts that make live editing safe:
- CPython does not reload imported `.py` files, so seats can edit `app.py` /
  `relay.py` while the running process keeps executing the old code until
  relaunch.
- WebView2 loads `ui/index.html` once; edits do not hot-apply to the open
  window.

So "edit while running" is inherently safe; the danger is only *relaunching
with unproven code*, which the gate and git checkpoint address.

## 3. The gate

Run **before** stopping, never after (a failed wave must not have taken the
app down):

1. `python tests/run_all.py` (fresh subprocess, worker thread — never the
   bridge thread; capture output tail).
2. Cold-import smoke: `python -c "import app"` — run_all exercises modules
   but not app.py's module-level import graph in every environment.
3. Optional targeted check when UI files changed: node must exist for
   `test_ui_boot`; if node is absent the suite skips itself, so record that
   fact in the gate result rather than assuming coverage.

Gate result recorded as `{ok, suites, tests, failed_suites, output_tail}`.
Failure aborts the wave: no stop, no commit; the transcript gets a system
note naming the failed suites, and the seats get the failure text in their
queues next round. Rollback of a failed-but-committed edit is §7.

Runtime expectation: ~35 suites; tens of seconds to a couple of minutes.
Acceptable for one side call per wave; emit progress (`status` events) so the
UI doesn't look frozen.

## 4. Controlled stop

Restart requests are honored **only between turns** — the same boundary the
stop flag already uses. Never kill a turn mid-flight: an orphaned CLI child
may still finish server-side, but its reply and its freshly minted session id
would never be captured, silently amputating that seat's memory. Between-turn
stopping makes the worst case "one fewer message", which the never-forge rule
already treats as correct.

Flow inside `app.py`:

1. Trigger: Josh clicks Restart (new bridge method), or a seat's request is
   confirmed by Josh through the existing [[ASK]] modal (recommended for the
   autonomous loop — see §9). Both funnel into `Api.request_restart()`.
2. If the focused chat is running: set `_restart_pending` and trip the same
   stop machinery `/stop` uses; `run_rounds` returns at the next boundary;
   the `_rounds` epilogue sees `_restart_pending` and proceeds to handoff
   instead of only emitting `done`.
3. If idle: proceed directly.
4. Drain: flush `_emit_q`, one final `store.save(state)`, wait briefly for
   spawned helper/team bookkeeping to settle (helpers mid-flight are declared
   lost on the next run — existing spawn rule, unchanged).

## 5. Handoff marker + single-instance mutex

**Marker** — repo root, NOT sessions/ (it is app-level state, and a stray
file in sessions/ would appear in the rail):

```
.alloy-restart.json
{ "v": 1,
  "session_id": "<basename of session dir to reopen>",
  "wave": 3,
  "reason": "seat-requested|josh-requested",
  "gate": {"suites": 35, "tests": 780, "failed": []},
  "git_before": "<sha before wave>", "git_after": "<sha after commit>",
  "pid": <writer pid>, "ts": "<iso>" }
```

Written with `_atomic_write` semantics as the LAST act before exit — after
the final meta save, before window destroy.

**Relaunch**: the old process spawns the child itself just before exiting:
`subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")],
creationflags=DETACHED_PROCESS|CREATE_NO_WINDOW, close_fds=True, cwd=ROOT,
stdin=DEVNULL)`. Under pythonw, `sys.executable` is already pythonw.exe — no
console flash.

**Mutex handshake**: `main()` creates a named Windows mutex
(`Local\Alloy.AIChat.Instance`). The child loops up to ~15 s trying to acquire
it (parent releases on exit), then consumes the marker. Why not an external
supervisor script: one more moving part, console/PATH hazards, no added
robustness — the mutex makes parent/child overlap benign, and Popen-before-
exit cannot strand the user at a dead desktop unless the child dies too, in
which case the manual-launch fallback below still recovers everything.

**Crash-safety matrix** — every failure point has a defined recovery:

| Killed/crashed… | Result | Recovery |
|---|---|---|
| during seat edits | old code still running | none needed |
| during gate | no stop happened | wave aborted, note queued |
| after commit, before marker | app keeps running old code | next wave retries |
| after marker, child never started | app down, marker present | **next manual launch finds marker and resumes it** — the marker IS the crash-recovery record |
| child crashed before consuming marker | same | same |
| marker present but session gone/corrupt | child renames it `.alloy-restart.failed.json`, starts normally, surfaces why in a status line | never writes into a refused session |
| power loss mid-turn | last committed turn survives; in-flight turn lost | reopen shows conversation minus that turn; CLI-side memories intact server-side |

Rule: the marker is consumed (deleted) only on successful open or definitive
refusal — never left half-interpreted.

## 6. Auto-reopen

At startup, `get_config()` includes `"reopen": {"session": id}` exactly once,
read from the marker (or `--reopen <id>` argv, which wins). The UI's
`pywebviewready` handler, after `restoreTabs()`, routes that id through the
SAME function a rail-row click uses (`openSession(id)` → `api.open_session`).
One code path: reopened-after-restart chats are pixel-identical to
reopened-by-click chats, including truthful seat/model/role restoration.

On success the UI clears the flag (`clear_reopen()`), deletes the marker, and
the app writes one system row into the transcript so the gap explains itself:

> App restarted for improvement wave 3 (gate: 35 suites / 780 tests passed,
> code moved `<before>` → `<after>`). Conversation resumed.

That line is load-bearing: without it the transcript shows a timestamp jump
and nothing else, and every future reader has to guess.

**Picking the session when no id is recorded** (manual launch finding a bare
marker-less crash): new relay helper `newest_resumable_session()` — walk
`list_sessions()`, keep `can_continue` entries WITHOUT `meta.parent`
(a spawned team child must never outrank its living parent as "the newest
chat"), return the first. Heuristic used only when the marker is absent or
lacks an id.

## 7. Green checkpoint / rollback

Before stopping, after the gate passes:

- `git add -A && git commit -m "wave N: <one-line summary>"` — the relaunch
  always runs committed code; `git_after` goes in the marker.
- Rollback = `git reset --hard <git_before>` (+ delete untracked stragglers),
  then relaunch; the marker's `git_before` makes this mechanical.
- If the working tree was already dirty before the wave, require Josh's
  explicit go-ahead ([[ASK]]) — committing his unrelated changes under a wave
  label corrupts the rollback story.

Guard rails during the edit phase (conversation-level, stated in the task
text): never edit anything under `sessions/`; never delete the live session
dir; do not regenerate AI-CHAT.md expecting it to change THIS chat (its brief
was snapshotted at start and replays verbatim regardless).

## 8. Wave-2 integration worklist

`relay.py` (small):
- [ ] `newest_resumable_session()` (§6).
- [ ] Nothing else. Rehydration/resume primitives are done; do not refactor them.

`app.py` (most of the work):
- [ ] Single-instance mutex create/release in `main()` (best-effort try/except —
      non-Windows or API failure must degrade to today's behavior, not block startup).
- [ ] `--reopen <id>` argv scan + marker read; expose once via `get_config()["reopen"]`;
      `clear_reopen()` bridge method.
- [ ] `Api.request_restart()` bridge method (worker thread): idle → straight to
      handoff; running → set `_restart_pending` + trip stop; epilogue branches on it.
- [ ] Handoff sequence as a worker-thread function: gate → commit → marker write →
      spawn child → destroy window → exit(0). All emits via `self.emit` (enqueue-only).
- [ ] System-note-on-resume (§6) — in the reopen path, only when arriving via restart.

`ui/index.html`:
- [ ] In `pywebviewready`: after `restoreTabs()`, `if (uiCfg.reopen?.session) await openSession(uiCfg.reopen.session);` then clear.
- [ ] A small "Restart & apply updates" control (Accounts panel or composer overflow)
      calling `request_restart`.

Explicitly OUT of scope for wave 2: CLI-side self-restart (`relay.py` has no
resume flag today; headless waves can simply shell out to `ai-chat` again —
note it here, don't build it yet).

## 9. Autonomy vs. approval

The loop has one genuinely irreversible step: replacing the running program.
Everything else is reversible (git) or lossless-by-design (session state).
Recommended default: a seat may PROPOSE a wave (edits + gate results in its
reply), but the actual restart fires only after Josh confirms — via the
existing [[ASK]] modal ("Apply wave N and restart?"). Fully autonomous mode
(restart without asking) is a deliberate, separate decision; if wanted later
it should be a config key gated per-conversation, defaulting off.

## 10. Testing (token-free)

New suites extend the existing pattern (`FakeAgent` drives real code, plain
scripts):

DELIVERED (r4): `tests/test_restart.py` — the continuity proof. A session
written by the REAL loop is reopened by a FRESH `app.Api` against a fake
window and asserted faithful off disk alone: roster/models/efforts/roles,
per-seat CLI session ids, owed queues (including the cap-debt case), cursor/
turn/rnd, floor state, until-done ceiling extension semantics, spawn budget
counters — then it CONTINUES through `_continue` without re-preambling an
introduced seat and persists the new debt. Both tests green as of this doc.

Still to build alongside wave 2:
- marker round-trip: write/consume/refusal-rename; atomicity under concurrent reader.
- `newest_resumable_session`: temp sessions dir with a fresh continuable chat,
  a spawned-team child newer than its parent (child must LOSE), a legacy
  view-only dir (skipped), a wrapped-but-continuable chat (eligible).
- `get_config().reopen` appears once and `clear_reopen()` retires it.
- headless restart flow in `test_app_headless.py` style: fake window, drive
  `request_restart` on an idle Api, assert marker content + child-spawn called
  (spawn point injected/mocked — never actually spawn in tests).
- gate runner wrapper: inject a failing fake `run_all.py` on PATH-style seam,
  assert wave aborted pre-commit pre-marker.

Nothing above spends tokens or touches real CLIs.
