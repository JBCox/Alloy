# Alloy feature ideas — round 2

2026-08-25 · Companion to docs/improvement-backlog.md (which stays valid; its
tier-1 palette / light-theme / a11y items are still the right next UI work, and
its #4 export, #7 pinning and #11 sound cue shipped on 2026-08-25).

This list deliberately skips generic chat-app polish and asks a different
question: **what can Alloy do that no single-model chat app can?** It owns four
real agent CLIs, a verified workspace, a planning supervisor, git, and a
structured on-disk record of every turn. Most ideas below are leverage on
assets that already exist and currently have no surface.

---

## 1. See the work, not just the talk

The biggest gap today: after an overnight Keep Improving run, Josh has commits
and a transcript, and **no in-app way to see what actually changed.**

1. **Diff lane** — a right-rail tab beside Files showing `git diff` per wave
   (and per commit `gate_commit` made), file tree plus hunks, provider-colored
   by which seat's workstream claimed the file. Hooks: `_git` / `_gate_run`
   already shell git, `continuous_probe` is the exact bridge shape to copy (git
   is a subprocess, so worker thread, never the bridge thread), and confinement
   reuses `confine_to_workspace`.
2. **Checkpoint and rewind** — every green wave is already a commit. Expose
   "restore workspace to wave N" (refuse on dirty, show what would be lost).
   Pairs with fork: branch the *conversation* and the *files* together.
3. **Gate scoreboard** — gate output is prose today. Render pass/fail counts per
   wave with the failing tail collapsible, plus a trend across waves
   ("1079 → 1084 tests, 0 failing"). Two red gates in a row is the single most
   actionable signal an unattended run produces.
4. **Blast-radius preview** — workstream tasks already declare literal file
   paths. Before dispatching a wave under `auto` / `full`, show "this wave will
   touch 6 files" with one-click narrowing. Cheap, and it makes the permission
   ladder legible instead of a dropdown nobody re-reads.
5. **Ship it** — one button at the end of a Build Together run: branch, commit,
   `gh pr create` with the wave report as the body. The summary already exists
   (`wave_report`); it currently dies in the transcript.

## 2. Get more truth out of having four models

6. **Blind panel** — in panel / compare mode, run the first pass with `isolated`
   routing *and* hide seat identity from Josh until reveal. Anchoring on "Opus
   said it" is the failure mode of a multi-model room, and the routing axis to
   do it already exists in `ORCHESTRATION_VALUES`.
7. **Disagreement map** — one stateless side call after a panel round (same
   shape as `build_moderator` / `synthesize_brief`) extracting each seat's
   claims into an agree/split matrix. Where four models agree is probably true;
   where they split is exactly where Josh should look. Render as a compact grid
   above the synthesis.
8. **Cross-examination phase** — a built-in panel step where each seat must
   attack the strongest point of one *other* seat's answer before the
   synthesizer runs. Adversarial verification, but across genuinely different
   models rather than N copies of one.
9. **Citation checking** — when a seat claims something about the repo, require
   `file:line`; the UI verifies the path exists (workspace-confined read) and
   marks unverifiable citations. The never-forge-a-turn rule extended from "did
   the seat speak" to "did the seat look".
10. **Column view for panel mode** — four answers to the same question read
    badly as one vertical feed. Side-by-side columns with a "differences only"
    toggle.
11. **Postmortem card** — when a run ends `goal_unresolved`, a cheap side call
    reads the supervisor trace and says *why* (waves spent, gate red, seat
    benched, plan never parsed). Today those endings all look identical.

## 3. Rooms you can re-open, not re-assemble

12. **Saved rooms (templates)** — name a lineup: seats, models, efforts, roles,
    preset, working folder, limits. "Code review room", "Cheap grunt work",
    "Two-Opus argument". One click to start. It is a config blob — `cfgFor`
    already builds exactly this shape; persist it beside `tabs.json` (through
    `relay.SESSIONS_DIR`, never a second module constant).
13. **Scheduled rooms** — start a room at a time, with caps, against a repo.
    "Every night at 1am, Keep Improving on ai-chat, $2 and 3 hours, stop on red
    gate." The watchdog, spend cap and time cap already exist; only the trigger
    is missing.
14. **Prompt library** — reusable openers with `{placeholders}`, per project.
15. **Auto-title after round 1** — chats are titled from the opener text. One
    cheap side call (routed through `helper_spec`, so an all-Ox room does not
    silently spend a Claude call) gives the rail readable titles.

## 4. Make unattended actually unattended

16. **Event hooks** — a user-configured shell command run on `question`,
    `checkin`, `gate_red`, `done`. Best-effort, swallow-everything, never fails
    a turn — the same contract activity narration has. Hook point: the one
    emitter thread that already owns `_play_cue` and `_flash_taskbar`.
    Immediately useful here: the phone link can turn a stuck run into a
    `termux-notification` or an SMS without Alloy knowing anything about phones.
17. **Wake-me rules** — the policy layer on top: notify only on (a) a question,
    (b) two red gates, (c) spend past X%, (d) no committed turn in N minutes.
    Everything else stays silent.
18. **Morning report** — reopening a chat that ran overnight opens with a card:
    waves, commits, spend by seat, tests delta, unresolved questions, and what
    the run is working on now. `current_objective`, `wave_report` and the usage
    accumulator already hold every number.
19. **Global attention count** — one badge (taskbar plus title) for "chats
    waiting on you" across all sessions, not just the rail group.

## 5. Money

20. **Live budget bar** — burn rate and projection against the run's caps
    ("$0.42 in 18 min; you hit the $2.00 cap around 09:40"). Seats that report
    nothing (Gemini, Ox) must stay honestly blank, never estimated.
21. **Auto-downshift for side calls** — moderator, brief, title, check-in and
    planner calls all pick their own model already, so a "cheap side work"
    policy under budget pressure is free and safe. Seat models are *not* free to
    change mid-conversation (a new model means a new CLI session, i.e. amnesia),
    so downshifting seats may only happen at an objective rollover, and must say
    so out loud.
22. **Cost per outcome** — outcome.json already separates hard facts from
    feedback. "This room costs about $1.10 and 22 minutes for a review this
    size" is derivable from history, and it is the number that decides which
    room to open.

## 6. Extending the roster

23. **Bring-your-own seat** — a JSON-described provider: argv template, resume
    flag, reply parse rule, optional activity mapping. `PROVIDERS` is already a
    one-entry-per-provider registry (grok sits there with `agent=None` waiting
    for an adapter), so a user-defined entry is the natural extension, and it
    makes Alloy extensible without code.
24. **Per-seat permission** — permission is conversation-wide today. A reviewer
    seat that physically cannot write, next to a builder that can, is a real
    capability split. Note the tension honestly: ROLES_DESIGN's rule is that a
    role changes what a seat is *told*, never what it *can do* — so this ships
    as its own axis with its own UI, not folded into roles.
25. **Remote seats** — run a seat's CLI on the 16-core peer over the existing
    SSH link. Only honest for a seat that does not share the workspace (a reader
    or reviewer), because session ids and files are host-local. Speculative;
    listed because the hardware is already there.

## 7. Input and reading

26. **@-mention in the composer** — the `TO` directive and
    `_addressed_recipients` already exist for seat-to-seat addressing; the
    *human* has no way to use it. "@Ox check this" should queue to one seat.
27. **Drag-and-drop onto the composer** — files attach, a folder sets the
    working folder. Paste and 📎 exist; drop is the missing third path.
28. **Image gallery** — Gemini and GPT both generate images and Alloy harvests
    them into the workspace. A per-chat gallery beats scrolling for thumbnails.
29. **Replay scrubber** — play an overnight run back at speed with its activity
    stream, to review eight hours in two minutes.
30. **Run timeline** — a gantt of who was thinking when and where the wall clock
    went. Every row already carries `ts` and a duration.

---

## If only five

1. **Diff lane plus checkpoint rewind** (#1, #2) — closes the loop on unattended
   work; today the app writes code Josh cannot review inside it.
2. **Saved rooms plus scheduling** (#12, #13) — the highest everyday leverage
   per line of code in this list, and almost no engine risk.
3. **Blind panel plus disagreement map** (#6, #7) — the thing only this app can
   do, and the reason to keep four seats rather than one.
4. **Event hooks** (#16) — twenty lines in the emitter thread; turns every
   future notification idea into configuration instead of a feature.
5. **Bring-your-own seat** (#23) — stops the roster being a code change.

One-afternoon wins worth grabbing regardless: auto-title (#15), @-mention (#26),
drag-and-drop (#27).

## Deliberately not proposed

- Anything that answers on Josh's behalf, auto-retries a fatal error, or carries
  CLI memory across a fork — the house rules against forging continuity exist
  because each one was learned the hard way.
- Splitting ui/index.html into modules (see the backlog's own note).
- Estimating usage for providers that report none.
