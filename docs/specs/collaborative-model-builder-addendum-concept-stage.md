# Collaborative Model Builder - Addendum A: Concept Stage, Seats, and Roles

**Status:** Draft for owner approval, 2026-09-07. Extends `collaborative-model-builder.md` (Draft v1). Requirement numbers continue from R-93. Everything in the base spec still applies; where this addendum narrows a base requirement, it says so.

---

## A.1 Purpose

Projects may start from a text idea, or from one or a few images, instead of a finished concept sheet. A concept stage produces the reference set the modeling workflow consumes: an approved canon anchor, a consistent turnaround, and, on demand, close-up studies of individual pieces. The owner decides how much to approve by hand.

Principle: a generated image is never evidence of a pre-existing design (base R-32). When the design itself is being generated, the images the owner approves *become* the design. Unapproved generations are hypotheses. Approved images are ranked, and contradictions between them are resolved explicitly, never averaged.

---

## A.2 Settled decisions (owner, 2026-09-07)

- **D10. Seats and roles.** A *seat* is a provider binding: `A` (claude, `fable`), `B` (codex, `gpt-6-astra`), and `I`, an image-generation model from OpenAI or Google, chosen at configuration time. A *role* is a duty assigned per task by the scheduler: observer, builder, reviewer, verifier, reassessor, art director, image generator. Any LLM seat may hold any LLM role over the run; roles are never permanent (base R-8). The image seat is a tool: it generates from prompts the art director writes and it judges nothing.
- **D11. Approval modes.** `concept.approval` is `each` (default: the owner approves every generated image before it is used), `anchor_only` (the owner approves the anchor; turnaround and studies are auto-approved when both LLM seats independently judge them consistent with the anchor and no conflict is open), or `auto` (the art director picks the anchor by stated criteria and everything proceeds; every pick is journaled and reversible). The mode is shown in the CLI and GUI at all times.
- **D12. Image generation is manual by default.** The image seat `I` is the owner operating the ChatGPT or Gemini app: Alloy's art director writes the prompt and names the images to attach, the owner generates and saves the files, and `concept import` brings them in with a manifest whose vendor and model are recorded as declared by the owner (Alloy cannot verify them and says so). No API key, no automated call, no vendor cost accounting. An argv API wrapper (`builder/providers/imagegen/<vendor>`) may be added later as an alternative seat; if it is, keys come from environment variables only and never appear in argv, records, logs, or packets, and model identifiers come from the vendor's model list or official documentation (base R-19).
- **D13. Ordering.** Phase 2a delivers the real LLM adapters (base Phase 2). Phase 2b delivers the concept stage on top of them, because the art director is an LLM role.

---

## A.3 Requirements

### A.3.1 Canon and provenance

- **R-94** A reference has a `canon_state`: `candidate`, `approved`, `superseded`, or `rejected`. Only `approved` references enter intake and evidence packets as references; candidates may appear in packets only inside concept-stage tasks, labeled as candidates.
- **R-95** Canon precedence, highest first: owner-supplied target images; the approved anchor; approved turnaround views; approved studies. When two approved images contradict each other, the engine records an `evidence_conflict` (images, region, what differs, reported by which seat) and the run asks the owner or regenerates; it never picks silently and never averages.
- **R-96** Every generated image carries a generation manifest: the generation request id, the prompt Alloy wrote, the images Alloy asked to be attached (with hashes), vendor and model (declared by the owner for manual generation, reported by the vendor for an API seat), seed and size where known, output hash, usage and cost as measured, unknown, or not applicable (manual), timestamps, success or failure. A manifest is written for every request, including requests the owner abandons or marks failed.
- **R-96a** Manual import (`concept import`): files are copied unmodified under `refs/generated/`, hashed, linked to their request, and marked `candidate`. The owner may declare vendor, model, and which images were actually attached; Alloy records these as declarations, never as verified facts. An import with no matching open request is refused unless the owner creates a request for it (`concept import --as-anchor` or `--as-view <view>`).
- **R-97** Generated images are stored unmodified under `refs/generated/`. Crops for evidence are derived files with original-space coordinates, as for any reference (base R-27).
- **R-98** Approving, rejecting, or superseding a generated image is a user action (or an auto-approval under D11) recorded with the mode in force, the seats' consistency verdicts, and the criteria used.

### A.3.2 Concept workflow

- **R-99** Start modes: from text, from one or more seed images, or both. Seed images supplied by the owner are `approved` on entry with kind `target`. The first generated or supplied approved image is the *anchor*.
- **R-100** From text, the engine generates a small set of anchor candidates (default 4) from an art-director prompt that states subject, silhouette, construction, materials, palette, style, and framing. The owner picks one (`each`, `anchor_only`) or the art director picks with recorded criteria (`auto`).
- **R-101** The art director writes a *canon description* from the anchor: silhouette and proportions, main masses, parts and construction, materials and colors, distinguishing details, and what is unknown. All later generation prompts derive from this description plus the anchor as conditioning input, so views stay consistent.
- **R-102** The turnaround is generated view by view (front, side, rear, top, underside, three-quarter, or a subset the owner chooses). Each generated view is checked independently by both LLM seats against the anchor and the canon description before either sees the other's verdict (base R-11). Verdicts name specific inconsistencies (part, region, what differs). A view with an inconsistency is regenerated (bounded attempts) or escalated to the owner.
- **R-103** Coverage of the needed reference set is tracked like review coverage: which views and which parts have approved references, candidates, or nothing. Intake starts when the owner-chosen minimum set is approved, or when the owner explicitly proceeds with a partial set (recorded).
- **R-104** Per-piece studies on demand: during blockout or the improvement loop, an agent may request a study (`part_id`, view, region, purpose, draft prompt). The engine generates it conditioned on the canon, both seats check it, and it is approved under the mode in force. Approved studies attach to the part as references with precedence below the turnaround (R-95).
- **R-105** Concept-stage limits: `max_images` and `max_regenerations_per_view` count imported and generated images alike. A monetary cap applies only to an API seat and is labeled unenforceable when the vendor does not report cost (base R-25, R-85); for the manual seat cost is recorded as not applicable, never as zero.
- **R-106** Contradictions between generated views and the modeling evidence are handled by the existing intake observation and reconstruction brief (base R-28 to R-30): the brief records the chosen interpretation and competing hypotheses. Generated views never override the anchor.

### A.3.3 Seats, roles, and scheduling

- **R-107** Configuration binds seats to providers and models (`builder.agents.A`, `builder.agents.B`, `builder.image_generation`). Role assignment per task is decided by the scheduler with a recorded one-line rationale; the owner may override any assignment. A seat may hold several roles across a run, never two conflicting roles on one task (a seat never reviews or verifies its own operation).
- **R-108** The art director role defaults to seat A and rotates only on owner override or after two rejected generation rounds, so the canon description keeps one voice.
- **R-109** Preflight covers the image seat with the three tiers. Manual seat: declared (`manual`, with the vendor the owner intends to use), local (the import directory is writable and the owner has confirmed the flow once), live (not applicable; the report states that provenance is as declared by the owner and that consistency is checked by both LLM seats). API seat, if configured: declared (vendor and model), local (wrapper importable, key present as a boolean, model listed by the vendor's model endpoint where one exists), live (one minimal generation, opt-in because it costs money).

### A.3.4 Interface

- **R-110** CLI verbs: `concept start <wf> --from-text "..." | --from-image <path> ... [--approval each|anchor_only|auto]`, `concept prompts <wf>` (prints every open generation request: the prompt to paste, the images to attach with their paths, and the request id), `concept import <wf> <request_id> <file>... [--vendor chatgpt|gemini|other] [--model "<as shown in the app>"]`, `concept list`, `concept show <id>`, `concept approve <ids>`, `concept reject <ids> --reason`, `concept regenerate <id> [--note]` (writes a revised prompt as a new request), `concept study <wf> --part <id> --view <v> [--region ...]`, `concept proceed <wf>` (accept a partial set). Optional `concept import --watch <folder>` imports new files from a folder as they appear, each assigned to the request the owner names. The GUI (base R-91) gains an approval panel: open requests with copyable prompts, candidates side by side with the anchor, verdicts from both seats, approve, reject, regenerate.

---

## A.4 Test matrix additions

| Test area | Layer | Requirements |
|---|---|---|
| Canon states and precedence; conflicts never averaged | D | R-94, R-95, R-106 |
| Generation manifests written for success and failure; originals unmodified | D | R-96, R-97 |
| Approval modes: each, anchor_only, auto, with recorded verdicts and criteria | D | D11, R-98, R-100 |
| Independent consistency verdicts before sharing | D | R-102 |
| Coverage of the needed set; proceed with partial set recorded | D | R-103 |
| On-demand studies attach to parts with lower precedence | D | R-104 |
| Image limits and unknown cost | D | R-105 |
| Role assignment never lets a seat verify its own work | D | R-107 |
| Manual import: originals unmodified, hashed, linked to requests, declarations recorded as declarations, unmatched imports refused | D | D12, R-96, R-96a |
| Prompts printed with request ids and attachment paths; regenerate writes a new request | D | R-110 |
| Image seat preflight tiers (manual and, if configured, API) | D | R-109 |
| Owner-driven end-to-end: prompts out, files in, verdicts, approval, intake | U | R-99 to R-103 |

Mocks for the image seat are prepared PNGs imported through the same `concept import` path; they prove routing, provenance, and approval handling, not image quality. No live image generation is needed for any test.

---

## A.5 Phase plan changes

- **Phase 2a (next session).** Real claude, gemini, codex adapters; three-tier preflight with live probes; session isolation; structured output and repair; usage capture; cancellation; per-part close-up views and crops in the loop (base R-61, R-65); bounded live smoke test on the fixture after owner approval of scope and budget.
- **Phase 2b.** Concept stage per this addendum: manual image seat (prompts out, imports in), art director role, consistency checks, approval modes, coverage, studies, CLI verbs, D tests, and one owner-driven end-to-end pass with real images the owner generates in ChatGPT or Gemini. Consistency verdicts use the real LLM seats, so they spend LLM usage only, after owner approval.
- Phases 3 and 4 as in the base spec, with the approval panel added to Phase 3 and concept-stage documentation added to Phase 4.
