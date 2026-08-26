# Alloy improvement backlog — ranked

2026-08-23 · Jackson (T1). Ranked by value-per-risk for this codebase
(single-file UI, one inline script — every item below is scoped to respect
the TDZ/boot-order rules documented at ui/index.html:6161-6175). "Borrowed"
items name the app the pattern is proven in. Companion to docs/ui-audit.md.

## Tier 1 — do next

1. **Command palette (Ctrl+K)** · borrowed from Linear/Raycast/VS Code.
   Fuzzy-jump to any saved session, run any slash command, toggle a seat,
   switch preset — one input, Escape closes like every other modal. The
   shortcuts it surfaces already exist (Ctrl+T, Ctrl+1-9, Ctrl+Tab) and are
   currently undiscoverable (ui-audit #3). Hooks cleanly beside the
   keyboard-reachability block (~6200). Highest intuition win available.
2. **Light theme + toggle** · borrowed from every mainstream chat app.
   Palette is already variable-driven (`:root` block), so this is a second
   variable set under `data-theme="light"`, honoring `prefers-color-scheme`
   on first run, persisted in localStorage, restored on boot (audit #2).
3. **Labeling pass** · own/a11y. One sweep giving the 36 unlabeled buttons
   `aria-label` + consistent tooltips (audit #1). Hours of work, permanent
   accessibility and tooltip-consistency payoff.
4. **Export conversation as standalone HTML** · borrowed from ChatGPT share
   / Claude export. A rail-row action rendering transcript.md (or
   messages.jsonl) into a self-contained styled HTML file in the session
   folder — no workspace files exposed, matches the confinement rules.
   Makes chats shareable outside the app.

## Tier 2 — high value, more surface

5. **Usage & retro dashboard modal** · own. The data exists on disk today:
   per-message usage pills in messages.jsonl and sessions/playbook.json
   from retro.py — neither has an in-app surface. One modal: spend-over-
   -time by seat/provider, plus active playbook rules with pin/dismiss.
   Closes the loop on the app's own learning system with zero engine work.
6. **Message-level actions** · borrowed from Discord/Slack/ChatGPT. Beyond
   the existing copy button (2350+): quote-into-composer (replies with a
   "> cited line"), and regenerate-last-turn (re-runs the final seat turn).
7. **Session management depth** · borrowed from Claude desktop projects /
   ChatGPT folders. Pin favorite chats above their group; archive (hide,
   not delete) as a middle state; drag-to-reorder open tabs with the order
   persisted beside titles.
8. **Slash-command autocomplete in the composer** · own. A hint menu fed
   from the real command list as "/" is typed, mirroring the @-mention
   spec from this project's first planning round. Removes tribal
   knowledge of `/clear`, `/compact`, `/ceiling` etc.
9. **Read-aloud (TTS) for replies** · borrowed from Claude desktop voice.
   Rides the existing local dictation stack's shape (probe → engine →
   bridge event); per-message speaker icon, OS voices, no new deps.

## Tier 3 — polish, take in slack moments

10. **Find-in-conversation** · Ctrl+F style highlight bar over `#feed`,
    browser-grade but in-app (WebView2's find doesn't reach shadowed
    scroll containers reliably).
11. **Notification options** · sound cue + optional OS toast when a turn
    or check-in lands while unfocused (taskbar flash exists; this builds
    on `_flash_taskbar`'s event path).
12. **Display settings** · font-size/density control persisted to
    localStorage; reduced-motion already handled.
13. **First-run overlay** · three-card tour (preset cards → working folder
    → composer) shown once; the empty-state roster cluster is the natural
    anchor (renderEmptyRoster, ~3746).
14. **Thinking-order unification** · derive Gemini level order from its
    family data exactly as GPT/Ox already do (ui-audit #4).

## Explicitly not backlog

- Splitting ui/index.html into modules — the one-inline-script constraint
  is load-bearing (boot-order TDZ history); the cost outweighs the tidiness
  until tooling exists to enforce the boot contract automatically.
