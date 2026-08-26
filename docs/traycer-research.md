# Traycer Research — Feature Catalog, Communication Design, and Architecture

Researched 2026-08-25 by Ox Alpha (task t1). Sources: github.com/traycerai/traycer (MIT,
repo `AGENTS.md`, README), docs.traycer.ai (index, concepts/agent-to-agent, panels/agents,
concepts/worktrees, extension/tasks/{epic,verification,yolo-mode,models}), traycer.ai homepage.
Every claim below cites its source. Note up front: **what is open source in the Traycer repo is
the clients + CLI + wire protocol only — the Host process and cloud backends are closed**
(repo AGENTS.md: "Open-source **clients, CLI, and protocol**. The Traycer Host and cloud
backends are **not** here"). So the deepest mechanics (their orchestration engine) are not
readable; what follows is from their docs and repo structure.

## 1. What Traycer is

A desktop app (Electron shell + GUI renderer + CLI) that orchestrates multiple coding agents
(Claude Code, Codex, Cursor, OpenCode, plus a native inference subscription) inside shared
**Tasks**. BYOA: it drives the agent CLIs/subscriptions you already pay for rather than proxying
a single API. (README; traycer.ai)

Repo map (AGENTS.md): `protocol/` = client⇄host wire contract (`@traycer/protocol`),
`clients/traycer-cli/` = CLI, `clients/shared/` = transport/auth/formatting,
`clients/gui-app/` = GUI renderer, `clients/desktop/` = Electron shell. Bun workspaces + Nx.

## 2. Mental model: Task → Agents → Artifacts

- A **Task** is the top-level container for agents, panels, terminals, artifacts, and its own
  shared filesystem/history/working context ("Every Task has its own shared filesystem,
  artifacts, history, and working context" — traycer.ai).
- An **agent** is a *durable session* inside the Task, worked through one of two interfaces:
  - **Chat interface**: in-app conversation; model, permissions, thinking effort, fast mode,
    attachments can change turn-by-turn.
  - **Terminal interface**: terminal-style session with launch choices (workspace/worktree,
    coding agent, model, effort, CLI args) FIXED at launch; Traycer stores the upstream CLI's
    session id so it can resume that same session on its original Host.
  (docs.traycer.ai/panels/agents)
- Plain Terminals are just shells and do NOT participate in agent-to-agent anything
  (concepts/agent-to-agent).

**Alloy mapping:** Alloy's session ≈ Task; seats ≈ agents; Alloy already stores upstream CLI
session ids per seat and resumes them — same design. Traycer's Chat/Terminal split maps to
Alloy's turn-steered seats vs fixed-config spawned helpers.

## 3. Agent-to-agent communication (the part Josh asked about most)

Source: docs.traycer.ai/concepts/agent-to-agent. Their key insight: communication is split into
THREE separately-gated capabilities, not one:

| Capability | Requirement |
|---|---|
| **Reference** (mention an agent as context) | Nothing beyond being in the Task. Always available. |
| **Read transcript** | Same user; Chat-interface history is Task-backed (cross-Host readable); Terminal-interface transcripts only on the owning Host. |
| **Deliver message** | Same user AND both sender+receiver local to the same Host AND both runtimes support A2A messaging. |

Mechanics:
- An agent can create a **child agent**, send another agent instructions, ask-for-reply or
  fire-and-forget, or read another agent's transcript.
- **Hierarchy is provenance only**: children appear in a tree under their parent (Agents panel),
  but ANY agent may reference ANY other agent — parent/child restricts nothing. "Dashed edges are
  references… whether a message can actually be *delivered* depends on the capability gates."
- Delivery failures are REJECTED, not queued ("a message to it is rejected rather than queued").
- Per-runtime support matrix (their docs): Claude Code supports messages on both interfaces;
  Codex/OpenCode support messages on Chat only (Terminal = reference+transcript); Cursor Chat-only.
  Claude Code Terminal has an actual **inbox** for A2A messages.
- Durability ≠ availability: Terminal transcripts are read from the coding agent's own session
  history (survives app close), but reading requires the owning Host reachable; missing history
  FAILS the read rather than returning partial results.
- Delegated work gets **agent-selection instructions**: global/per-workspace settings telling
  Traycer which coding agent, model, and reasoning effort to pick for delegation
  (settings/agents page referenced by concepts/agent-to-agent).

**Lessons for Alloy:** (1) our @-mention routing is closest to "reference"; we lack the explicit
transcript-read vs message-deliver distinction; (2) our spawn lineage (`parent`/`children` in
meta) matches their tree-as-provenance; (3) our rule that refusals become visible notes ≈ their
"rejected rather than queued" honesty.

## 4. Planning-first workflows (token-efficiency core)

The IDE-extension lineage gives four task modes (docs.traycer.ai/extension/tasks):

- **Plan Mode** — analyze codebase → produce a detailed file-level plan with symbol references →
  hand the PLAN (not the whole conversation) to a coding agent to execute → verify implementation
  against the original plan.
- **Phases Mode** — break complex features into ordered phases with validation between steps;
  context carried forward: "Traycer carries forward file mappings, decisions, and rationale so
  later steps reference earlier work accurately, avoiding re-analysis" (FAQ). Phases can be
  inserted, reordered (drag-drop), merged by AI, expanded from Plan.
- **Review Mode** — agentic code review producing categorized findings.
- **Epic Mode** — specs + tickets system (below).

**Why this is token-efficient:** the expensive codebase analysis happens ONCE in the planning
model, and what crosses into the coding agent's expensive context is a compact plan document —
not the full analysis transcript. This mirrors but sharpens Alloy's supervisor pattern: our
workers get only their task brief under radio-silence isolation; Traycer formalizes the brief as
a generated artifact with symbol-level file references.

## 5. Verification loop

(docs.traycer.ai/extension/tasks/verification) After execution, Traycer compares the diff
against the ORIGINAL plan and generates review comments categorized **Critical / Major / Minor /
Outdated**, then hands selected categories back to the coding agent for fixes. Options: fix one
comment, fix selected, fix all; **Re-verify** (focused, cheap — only previously-found issues) vs
**Fresh verification** (full re-analysis). Outdated comments auto-retire when implementation
changes make them moot.

**Alloy mapping:** this is a graded version of our wave_gate + filesystem verification — theirs
is semantic (plan-vs-diff) rather than command-based, with severity routing deciding what's worth
another paid iteration.

## 6. Epic Mode: artifacts as durable intent

(docs.traycer.ai/extension/tasks/epic) Specs (mini-specs: PRD, tech doc, design spec, API spec)
capture WHY; tickets break them into Todo→In Progress→Done items with acceptance criteria and
assignees. Key mechanics:

- **Full-context awareness**: all specs/tickets in an epic are automatically in the LLM's
  context during discussion — no manual re-pasting of decisions.
- **Executions**: every handoff to a coding agent is tracked as an Execution recording plans,
  verification comments, commit, status — a complete audit trail of who did what when.
- **Smart YOLO**: an intelligent orchestrator that executes entire epics end-to-end, and unlike
  fixed automation it EVOLVES the epic at runtime — updates specs/tickets, splits/merges tickets,
  reorders work, propagates learnings between executions, parallelizes independent tickets, runs
  verification loops, pauses on failure for human resolution.
- Human collaboration: shared boards, invite by email/GitHub handle, Editor/Viewer access,
  ticket assignment, real-time co-editing.

## 7. YOLO / automation configuration

(docs.traycer.ai/extension/tasks/yolo-mode) Two flavors sharing one config surface:
Smart YOLO (adaptive, above) vs Phases-YOLO (fixed config you set upfront). Handoff types per
phase: **user-query handoff** (skip planning, send query straight to agent — cheaper), **plan
handoff** (generate plan first — better structure), **verification handoff** (send back chosen
severity levels only), **review handoff**. Each step can use a different agent and a different
Handlebars template wrapping the prompt. Config changes allowed only for phases not yet started.
Rate-limit behavior: pause until slots recharge, then MANUAL resume — never auto-bill onward.

## 8. Model profiles & cost control

(docs.traycer.ai/extension/tasks/models) Different models for different STEPS (planning, review,
verification, iteration, orchestration) via profiles: **Balanced** (default mix) and **Frontier**
(premium), plus custom overrides per step or single-model-everywhere mode. An interactive cost
calculator estimates credits per feature-step using model pricing × reasoning multipliers ×
cache modifiers. This is deliberate cheap-model/expensive-model routing per job type — the same
instinct as Alloy's `helper_spec` fallback chain (moderator → first seat → default) but applied
to every internal operation type.

## 9. Worktrees and workspace isolation

(docs.traycer.ai/concepts/worktrees) Per-workspace-folder run location choice: **Local**
(in place), **New worktree** (fresh git worktree; branch sources: current branch / working tree
carrying uncommitted changes / other local branch / remote branch), or **Existing worktree**.
Setup/teardown scripts run on worktree creation with visible states (Creating → Setting up →
Ready / Failed / Cancelled). Git Diff panel inspects any worktree's changed files. A Task can hold
multiple workspace folders, each with its own location.

**Alloy gap:** our workspace is one folder per conversation; git-worktree-per-seat would give real
filesystem isolation between parallel workers instead of convention-based path ownership.

## 10. Cross-host architecture

(concepts/hosts + AGENTS.md): The open-source CLI provisions a **signed Host** binary from GitHub
Releases; the Host owns all workspace/terminal/file/agent operations for its machine. Wire
contract uses **per-method `{major, minor}` RPC versions negotiated at handshake** (not npm
semver), CLI inlines protocol at build time. Tabs bind a hostId for life; cross-host moves are
**clone-not-migrate**. This is why A2A delivery requires both agents on the same Host — the Host
is the trust and capability boundary. Cloud adds sync/sharing/collaboration (closed source).

## 11. Features Alloy already has (no action needed)

BYOA multi-provider via account logins; per-seat model/effort pickers; permission ladders
(their Supervised/Auto-accept edits/Full access ≈ our read_only/ask/auto/full); thinking effort;
attachments; voice input/output (dictation/speaker); session resume via stored CLI ids; child
spawning (helpers ≈ child agents, teams ≈ deeper); supervisor-style planning + rolling waves +
filesystem verification; auto-commit after green gates (--gate-commit); spend/time caps with
pause semantics; human Q&A mid-run ([[ASK]] ≈ their dialogue/elicitation); audit trails
(transcript/messages.jsonl/outcome.json ≈ their Executions view); Elo/battle for comparing
agents; fork/export/reactions.

## 12. Features Alloy does NOT have — candidates to adopt

1. **Three-capability A2A gating** — separate reference/transcript-read/deliver permissions
   instead of one mention mechanism (§3).
2. **Agent lineage TREE in the UI** — visible hierarchy of who spawned whom, grouped also under
   artifacts (§3, §2). We have meta provenance but render it only as tooltips/prefixes.
3. **Plan-artifact handoff** — generate a symbol-referencing plan doc once, hand the DOC to the
   executor instead of relaying full replies (§4). Biggest token win available to us.
4. **Severity-graded verification comments** (Critical/Major/Minor/Outdated) routed back to the
   fixing agent selectively, with cheap focused re-verify vs full fresh verify (§5).
5. **Specs/tickets as durable artifacts** with acceptance criteria, status, assignment, and
   automatic inclusion in seat context (§6) — stronger than our ephemeral workstream tasks.
6. **Execution records** — one row per handoff capturing plan used, verification result, commit,
   status (§6).
7. **Adaptive orchestrator** that may EDIT the plan mid-run (split/merge/reorder tickets) based
   on discoveries, not just replan on failure (§7) — our watchdog remedies are a closed set; a
   bounded "evolve the board" remedy is the natural extension.
8. **Per-step model profiles** — pin cheap models to planning/verification side calls and
   expensive ones to execution, as a saved reusable profile (§8).
9. **Git worktree isolation per worker** + setup/teardown hooks + branch-source options (§9).
10. **Handoff templates** (Handlebars) letting the human wrap every plan/verify/fix prompt with
    project standards (§7).
11. **Pause-on-rate-limit with manual resume** semantics for long automation (§7).
12. **Cross-machine hosts with clone-not-migrate** and per-method versioned RPC (§10) — relevant
    if Alloy ever spans machines (cf. Josh's PC-compute link).
