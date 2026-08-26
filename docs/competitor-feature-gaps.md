# Competitor feature-gap analysis

2026-08-25 · Everything CrewAI, LangGraph, AutoGen / Microsoft Agent Framework,
MetaGPT, CAMEL, Poe, LMArena, MultipleChat, Claude Code Agent Teams and
n8n-style automation tools have that Alloy doesn't — filtered for what is
actually addable in this CLI-based, no-API-key architecture. Companion to
docs/feature-ideas.md (which lists Alloy-native leverage; this file lists what
everyone else already ships).

## Orchestration patterns

1. **Agent-to-agent handoffs** (OpenAI Swarm style) — seat A programmatically transfers an open task + context to seat B mid-turn, not just `[[NEXT]]` turn-taking
2. **Hierarchical manager-delegates** — CrewAI's mode where the manager LLM *dynamically* assigns tasks to workers as they free up (ours is wave-batched)
3. **Nested chats** (AutoGen) — a side two-seat conversation spawned inside one turn to resolve a sub-question before the main thread continues
4. **Sequential chat pipelines** — chained 1:1 conversations where output A feeds prompt B, C, D deterministically
5. **Structured output contracts** — JSON-schema-enforced replies between seats (LangGraph-style typed state), e.g. `{verdict, score, reasons}`
6. **Guardrails between hops** — validation/filter functions on every message before it reaches the next seat (reject PII, enforce format, block scope creep)
7. **Best-of-N / consensus voting** — N seats answer the same subtask independently, then a judge picks or merges (we have panel→synthesizer, but not per-subtask fan-out voting)
8. **Debate adjudication with scores** — formal rubric scoring of positions rather than free-form disagreement digests
9. **Role-playing inception** (CAMEL) — two seats co-specify and refine their own task before starting
10. **MetaGPT SOP assembly lines** — standardized artifact chain (PRD → design doc → code → test report) where each role's output is a file contract the next role consumes

## Memory & knowledge (the biggest real gap)

11. **Long-term persistent memory** across sessions — vector store of decisions/facts agents can query ("what did we decide last week?"); retro playbook only covers derived rules
12. **Entity memory** — structured facts about people/projects/tools that persist and update
13. **RAG over workspace documents** — embeddings retrieval so seats can search a big repo without quoting everything into the preamble (removes the BRIEF_MAX ceiling)
14. **Per-conversation knowledge uploads** — drop a folder/PDF set that becomes searchable context for all seats
15. **Teachable agents** (AutoGen) — Josh corrects a seat once; the correction persists into future conversations automatically
16. **Cross-session dedupe** — don't re-explain the same context to returning chats

## Execution environment & tools

17. **Sandboxed Docker/container exec** as a first-class shared tool all seats can use (isolation beyond the workspace contract)
18. **Browser automation tool** — a shared headless browser any seat can drive (computer-use style)
19. **Shared tool registry** — prebuilt tool library (search, scrape, translate, chart) exposed uniformly to every CLI via MCP, managed from the app
20. **In-chat code runner** — "Run" button on code blocks executing in the sandbox with output shown
21. **Artifact preview panes** — render generated HTML/SVG/charts inline like Claude Artifacts (images and code text are previewed today; rendered output is not)
22. **Mermaid/diagram rendering** in messages — architecture discussions become pictures
23. **LaTeX/math rendering**

## Evaluation & comparison (LMArena's whole game)

24. **Blind A/B battle mode** — two anonymous seats answer the same prompt; Josh votes, then identities reveal
25. **Elo leaderboard** accumulated across battles for your own seats/models
26. **LLM-as-judge scoring** — automated grading of replies on rubrics (helpfulness, accuracy) logged to outcome.json
27. **Regression evals** — replay a fixed prompt suite against new models/settings and diff results
28. **Prompt A/B testing** — run the same conversation twice with different preambles/roles, compare transcripts side-by-side
29. **Fork comparison view** — two forks of the same conversation side-by-side with divergence highlighted (forks exist; no diff view)

## Observability & tracing

30. **Full trace viewer** (LangSmith-style) — every turn expandable into the raw CLI stream: tool calls, args, results, denials, timings
31. **Seat health dashboard** — per-seat success rate, retry count, latency histogram, error classes over time
32. **Historical spend analytics** — charts across all past sessions, by provider/model/mode (budget bar is per-run only)
33. **Context-window fill meters** per seat — visualize how full each seat's context is, warn before degradation
34. **OpenTelemetry export** of runs

## Human-in-the-loop upgrades

35. **Inline edit of agent replies** — fix a seat's message before it fans out to peers
36. **Message reactions** (👍/👎) feeding outcome.json human_feedback from the transcript, not just at end
37. **Approval queues** — batch-review pending asks/edits from a single panel instead of sequential modals
38. **Per-edit approval mode** — yolo is global; add "propose edit → Josh approves each diff" granularity (Claude Code permission modes)
39. **Scheduled send / undo send** in composer
40. **Priority interjection lane** — urgent Josh message jumps ahead of queued backlog

## Triggers, scheduling & integrations (n8n territory)

41. **Webhook/API trigger** — start a conversation from outside (CI failure, email arrival) via local HTTP endpoint ✅ **SHIPPED 2026-08-25**: `webhook.py` (loopback-only ThreadingHTTPServer, optional `X-Alloy-Token`, strict payload whitelist, unknown keys reject); bridge `get_webhook`/`set_webhook` + toggle in the Event-hooks modal (`tests/test_bridge_tts_webhook.py`)
42. **Recurring scheduled chats** — cron-style "every morning summarize overnight activity"
43. **Push notifications** — Windows toast/email when a question or done fires while unfocused (sound cues are local-only)
44. **Slack/Telegram/Discord front ends** — drive and watch runs from chat apps
45. **Email digest** of what agents did today

## Remote access

46. **Web dashboard** — monitor running sessions from another device (pairs naturally with the PC/laptop link)
47. **Headless SDK** — programmatic seat turns (`relay.turn(seat, msg)` as a library API for scripts)
48. **Read-only share links** — publish an exported session at a URL

## Session management

49. **Archive sessions** — declutter rail without deleting ✅ **SHIPPED 2026-08-25**: `archived` flag in meta.json → `session_summary` → `RAIL_SUMMARY_FIELDS`; `Api.set_archived`; one collapsed-by-default "Archived" group at the bottom of the rail (`tests/test_archive.py`)
50. **Merge two conversations**
51. **Tags/annotations** — bookmark a message, attach notes that persist in meta
52. **Import foreign transcripts** — continue a ChatGPT/Claude-app conversation here
53. **Counterfactual re-run** — edit a past human message and re-branch from there (fork copies; doesn't let you modify the fork point)
54. **Git-backed session history** — sessions auto-committed/synced

## Model routing & resilience

55. **Fallback chains** — provider down ⇒ auto-route that turn to a backup provider (retry/backoff exists; cross-provider failover doesn't)
56. **Cost-aware router** — route easy turns to cheap seats, escalate on difficulty (the "model router" pattern)
57. **Mid-run model swap** — change a seated seat's model without ending the chat
58. **Local model seats** — Ollama/LM Studio as providers (fits the no-API-key philosophy perfectly; OpenCode could proxy it)

## Security & governance

59. **Secrets scanning** — redact API keys/tokens before they enter prompts or transcripts
60. **PII scrubbing option**
61. **Encrypted sessions at rest**
62. **Audit log viewer** — searchable record of every tool call and connector request (data partly exists in activity; no UI)

## Persona & content libraries

63. **Persona marketplace/library** — importable role presets (role system exists; no sharing/import format)
64. **Room template import/export** — share room configs as files
65. **Prompt/snippet library** for Josh's reusable instructions with variables (`{{project}}`)

## Composer & transcript polish

66. **Draft persistence** — composer contents survive app restart ✅ **SHIPPED 2026-08-25**: `sayDraft` in localStorage, saved per keystroke, restored last at boot into an empty box only, cleared by every clear path
67. **Multi-message queueing** — type several Josh messages, control order/priority
68. ~~**Within-chat jump/search bar**~~ — **ALREADY EXISTED**: Ctrl+F find bar (CSS Custom Highlight API), cross-chat rail search, and the per-seat history lens predate this list
69. **Thread summarization button** — TL;DR of any message range on demand
70. **Translate a message** action
71. **Post-run executive report** — auto-generated summary of decisions/artifacts/action items (digest exists mid-run; no closing report artifact)
72. **Action-item extraction** to Second Brain tasks automatically at wrap

## Accessibility & misc

73. **Command palette** (Ctrl+K) for all slash commands and actions
74. **Dark/light theme toggle**, font scaling, focus mode
75. **TTS read-aloud** of replies (dictation covers input; output is silent) ✅ **SHIPPED 2026-08-25**: `speaker.py` (Windows SAPI via hidden PowerShell child, text crosses stdin as base64 — no injection surface, latest-wins, `probe()` honesty); per-row 🔊 toggle button, availability from the startup probe (`tests/test_speaker.py`, `tests/test_bridge_tts_webhook.py`)
76. **Voice conversation mode** — the phone's walkie-talkie loop brought into the desktop app
77. **Screen capture as attachment** — screenshot region becomes image context

## Highest-leverage five

Given the architecture: #11–13 (memory/RAG — removes the BRIEF_MAX ceiling),
#24–25 (battle mode + leaderboard — LMArena proves demand, builds on parallel
mode), #30 (trace viewer — most of the data is already captured), #17 (shared
sandbox), #41 (webhooks). All are doable CLI-first without breaking the
no-API-key rule; memory/RAG is the only one needing a new dependency (an
embedder — could ride local ONNX or a seat itself).
