# Alloy UI audit — static walk of every surface

2026-08-23 · Jackson (T1). Method: full read of `ui/index.html`@6261 (the
entire UI is one file — inline CSS, inline script), cross-checked against
documented behavior. Line numbers refer to that revision; the file moves,
so treat them as anchors, not addresses. No live window was driven; every
finding below is visible in markup/JS on disk.

Scope walked: header + tab strip, chat-history rail (+ search), seat rail,
Conversation controls, composer (attachments, dictation, grip), feed and
message rows, Files rail + live code viewer, Accounts modal, Skills &
Connections modal, Roles modal, Ask modal, Keep Improving modal, lightbox,
empty state, boot sequence.

## What already holds up (so the priorities read honestly)

- Keyboard work landed and is real: global `:focus-visible` ring
  (~line 99), a Tab trap that keeps focus inside whichever `.show` modal is
  open with correct first/last cycling (6211-6228), Enter/Space activation
  for collapsed-rail strips (6203-6208), and arrow-key resizing for the
  composer grip mirroring the drag math (6229-6240).
- `prefers-reduced-motion` is honored in six places (animations off for
  messages, typing dots, thinking seats, scroll behavior).
- The Keep Improving warning modal is a model: real `<label for=>`
  bindings, an aria-labelled radiogroup (1796), honest hint text ("it does
  not review the quality of the work", 1792), and an acknowledgement
  checkbox that gates OK (6191).
- Seat-card controls carry both `title` and `aria-label` (3768-3779).
- Per-model honesty in pickers: Ox thinking levels hide entirely when a
  model has none rather than offering dead options (6141-6154).

## Findings (prioritized)

**P1 — Accessibility/intuition gaps with cheap fixes**

1. **36 of 64 `<button>` elements carry neither `title=` nor
   `aria-label=`.** Many are text buttons (Stop, "+ Add seat", Choose,
   Helpful) where the text is the label, but the tail of that list is
   icon-only controls whose meaning is invisible to screen readers and to
   anyone who doesn't hover-detect tooltips. Fix is mechanical: one pass
   adding `aria-label` (and tooltips where the icon isn't self-evident).
2. **The app has exactly one theme, hardcoded dark** (`--bg: #17151C` at
   the `:root` block; zero hits for `prefers-color-scheme`, `data-theme`,
   or any theme toggle anywhere in the file). All color already flows
   through CSS variables, so a light palette + toggle is mostly additive.
   Proposed in this project's first planning round and still absent.
3. **Keyboard shortcuts exist but are undiscoverable.** Ctrl+T, Ctrl+1-9,
   Ctrl+Tab and friends work (see tab strip, ~5756+), but there is no
   command palette, no `?` shortcut sheet, and no in-product list — grep
   finds no "palette" or "Ctrl+K" anywhere. New users cannot discover what
   power users use daily.

**P2 — Organization/clarity**

4. **Two sources of truth for thinking-level ordering**: Gemini levels are
   hardcoded `["high","medium","low"]` (6135) while GPT/Ox derive order
   from each model's own catalog entry (6155-6159). Harmless today;
   inconsistent by construction.
5. **Keep Improving limit inputs are live before their enable-checkboxes**
   (1807-1826): spend/hours inputs render editable inside labels whose
   checkbox starts unticked. Whether a typed-but-unticked value counts is
   answered only by reading `readContModal` — visually ambiguous.
6. **Empty-state roster silently no-ops if markup drifts**:
   `renderEmptyRoster` bails on `querySelector(".empty .dots")` missing
   (3747-3748) with no console signal — a refactor that renames the class
   loses the roster cluster invisibly.
7. **Seat name input allows 24 chars with the rule only in a tooltip**
   (3765-3767); overflow is invisible until it happens.

**P3 — Polish**

8. **Tooltip style is mixed**: some buttons explain policy ("blank =
   automatic"), others just name themselves. A consistent tooltip voice
   would make the dense seat rail calmer.
9. **Backdrop-click-to-close exists on some modals only** (e.g. contModal
   6188); verify each modal's dismiss model matches user expectation
   (Ask-modal's hide-not-close is deliberate and documented — keep it).

## Verdict

The bones are good — keyboard reachability and reduced-motion support are
above average for a single-file app, and the dangerous surfaces (Keep
Improving) warn precisely. The gaps are concentrated in three places:
one-theme-only, undiscoverable keyboard power, and a labeling tail.
Feature-level opportunities are ranked separately in improvement-backlog.md.
