# Collaborative Model Builder - Specification

**Status:** Draft v1, 2026-09-07. Restructured from a ChatGPT draft and grounded against the Alloy working tree (HEAD `593df76` plus uncommitted changes) and the tools installed on this machine.
**Audience:** the implementing agent and the owner.
**How to read:** Requirements are numbered `R-nn` and use MUST / SHOULD. Design docs, tests, and status reports cite R-numbers. Items marked **(verify)** were observed on 2026-09-07 and must be re-checked at implementation time because CLIs change.

---

## 1. Purpose and priorities

A workflow inside Alloy in which two AI agents collaboratively reconstruct a supplied concept design as an editable Blender model. Alloy holds the authoritative state, executes every modeling and render operation, and gates progress on evidence rather than on agent agreement.

Priorities, in order:

1. Model quality and reference fidelity, reliable execution, editability, honest verification.
2. Token efficiency: avoid redundant work, redundant context delivery, redundant tool use, and redundant rendering. Efficiency never reduces inspection coverage, omits evidence, weakens acceptance criteria, or silently selects a less capable model.

Fidelity means careful reconstruction of the supplied design: proportions, depth, construction, details, materials, appearance. Agreement between agents, scripts that executed, object counts, and polygon counts are not evidence of fidelity. The workflow never promises exact dimensions, hidden construction, or material properties that the images cannot establish. Uncertainty is recorded, evidence-linked, and revisable.

When a user-configured limit prevents further improvement, the run stops with an accurate status and preserved progress. The standard is never lowered to call a result successful.

---

## 2. Verified starting point (Alloy on 2026-09-07)

Facts to build on. They are not instructions to change unrelated code. Re-verify anything you depend on.

### Runtime and tools

- Python 3.14.0. Installed: rich, prompt_toolkit, pyyaml, ruamel.yaml, Pillow, pytest, tkinter. PyInstaller is not installed; `alloy.spec` exists and explicitly excludes `PIL` and `pytest`.
- Blender 5.2.1 LTS at `C:\Program Files\Blender Foundation\Blender 5.2\blender.exe` (not on PATH). Headless `-b --python-expr` works. Its bundled Python is 3.13.13, separate from Alloy's interpreter: scripts running inside Blender cannot import Alloy's packages, so the bridge is argv + JSON files.
- AI CLIs on PATH: `claude` 2.1.233, `gemini` 0.55.1, `codex` 0.147.0. Not present: copilot, ollama. Verified flags are in Appendix A.
- The real Death Factory assets exist and are off-limits for reads, renders, copies, and writes during development: `C:\Death Factory\docs\art\locked-theme-concept-v1.png` (2.66 MB) and `C:\Death Factory\art-source\blender\E10\E10_Bearer_v4_proportions.blend` (1.72 MB).

### Alloy architecture

- Providers are CLIs launched as subprocesses. There is no API client anywhere. `config.yaml` maps to `Config` / `AIConfig` in `config.py`: a `command` template containing `{message}`, plus `continue_flag`, `timeout`, `retry_count`, `weight`, `fallback_ai`, and `profile.supports_tools`.
- `orchestrator.py` (`Orchestrator`): every invocation is `subprocess` with `shell=True` and the prompt string-substituted into the command. `_escape_message` wraps the prompt in double quotes on Windows and escapes only `"`; `%`, `&`, `|`, `^`, and newlines are not handled. `_execute` treats a non-zero exit as success whenever anything was printed to stdout or stderr. `query`, `query_with_session`, `query_streaming`, and `query_streaming_with_session` all fall back to `fallback_ai` (config.yaml: claude to gemini, gemini to claude, codex to claude) and retry through `_execute_with_retry`. A single `timeout` (300 s) covers everything, and the streaming path applies it only after stdout closes. Session continuation inserts `--continue` before `-p`, tracked per AI name in `_ai_has_session`, which means "resume the most recent conversation in this cwd": provider-global, not per agent.
- `sessions.py` (`PersistentSession`, `SessionManager`: stdin/stdout pipes, `^>\s*$` prompt regex, silence-based completion) is not imported anywhere. Treat it as unused.
- `main.py` (`AICollab`): the interactive CLI. `run_roles` (about line 1096) runs each role once and concatenates prior contributions as text. `query_single_ai` (about line 612) builds context and uses `--continue`. `_process_invocations` (about line 719) executes `[[ASK:ai:msg]]` recursively to depth 3. `main()` accepts only `--gui` and `--setup`; a first run without a config launches the wizard.
- `tools.py` / `actions.py`: `[[READ]]`, `[[LIST]]`, `[[SEARCH]]`, `[[GIT]]`, `[[CONTEXT]]` are read-only inside `ContextManager.root` (the cwd). `[[RUN:cmd]]` is `shell=True` with a 60 s timeout and a string denylist. Tools are advertised in prompts only when `profile.supports_tools` is true (default: claude only).
- `gui/app.py` (`AlloyGUI`, Tk): `_run_mode` (about line 776) is a stub that prints "coming soon" and issues a direct query. The GUI never uses `--continue`; it rebuilds context from its own message list (`_build_conversation_context`) and calls `orchestrator.query_streaming` from worker threads, delivering results through `response_queue`, which `_process_queue` polls with Tk `after`. CLI and GUI therefore already have divergent context strategies.
- `gui/settings.py` rewrites `config.yaml` through `_collect_config` / `_merged_config` / `deep_merge`. Any new top-level config key must survive a save from this editor.
- `prompts.py`: all standard prompts advertise the `[[ASK:...]]` collaboration syntax and tools.
- Modes: `modes.py` (`ModeType`), `router.py` (parses `@mode[opts] --flags`); handlers live in `main.py`.
- Tests: none exist. `.gitignore` already lists `.pytest_cache/`.
- Working tree: 12 modified files (+1,411 / -152) and 5 untracked new modules (`actions.py`, `context.py`, `invocation.py`, `sessions.py`, `tools.py`), an in-progress feature. A junk file named `nul` (a bash artifact) is also untracked. Preserve all of it. Do not reformat, do not change line endings, do not stash, do not commit unless asked.

---

## 3. Settled architecture decisions

Decided. Do not reopen without a recorded reason in the design doc.

- **D1. Own package.** A new package (for example `builder/`) contains the engine, records, scheduler, Blender runner, provider adapters, and prompt-packet builders. The CLI entry and the GUI view are thin clients. Scheduling, state transitions, acceptance, ownership, and recovery live only in the engine.
- **D2. Not a mode.** The builder does not route through `execute_mode` / `run_roles`, `Orchestrator`, `prompts.py`, `[[ASK]]`, or `[[TOOL]]`. Existing modes keep their fallback, retry, and continue behavior untouched. It may reuse `Config` to read provider definitions.
- **D3. Argv-only provider execution.** Builder invocations use `subprocess.Popen(list, shell=False)`. Prompts travel via stdin or a file, never `{message}` interpolation. No `fallback_ai`, no fallback model, no cross-provider or cross-model substitution.
- **D4. Agents never write the model.** Agent CLIs run in their read-only tool modes with filesystem access limited to the evidence packet and their own scratch directory. Agents emit structured operation requests: a bpy script plus declared intent, target part IDs, and expected observable outcome. Alloy runs each operation in Blender against a staged copy, validates it, and promotes it to an immutable revision. This is enforced by process permissions and the staging path, not by prompt text.
- **D5. SQLite records plus files.** Authoritative records live in SQLite (stdlib `sqlite3`, WAL mode) inside the workflow project directory, with a `schema_version`, alongside `refs/`, `revisions/`, `renders/`, `staging/`, `checkpoints/`, `logs/`. An append-only `journal` table records every transition and operation with actor, before/after state, inputs, and evidence references. No new runtime dependency beyond Pillow (already installed; must be added to `requirements.txt` and un-excluded in `alloy.spec`).
- **D6. Blender only.** The backend interface has exactly the operations this workflow uses (open/validate, apply operation, save revision, render view, measure, list identities). No speculative abstraction for other backends.
- **D7. Independence is a procedure, not a guarantee.** Isolated provider sessions plus deliberately constructed evidence packets. Described to users as an "independent evidence pass".
- **D8. Attended by default.** Unattended mode is an explicit opt-in.
- **D9. Configuration.** Builder settings (Blender path, agent bindings, limits, presets) live under a new top-level key in `config.yaml`, parsed leniently, exposed in the settings editor, and proven to survive a settings-editor save.

---

## 4. Requirements

### 4.1 Authoritative state and workflow contracts

- **R-1** Alloy owns workflow state. No agent text, provider transcript, or heuristic on phrases such as "done", "looks good", or "both agents agree" changes state.
- **R-2** Versioned, validated records with stable identifiers for: projects and runs; agents and provider sessions; references and reference regions; parts, instances, assemblies, and construction relations; tasks and dependency versions; editing ownership; operations and source revisions; checkpoints and external asset dependencies; render manifests; observations, findings, and correction attempts; user feedback and acceptance; limits, consumption, and execution status.
- **R-3** Every transition and operation is journaled durably (actor, timestamps, from and to state, inputs, evidence references, outcome). The journal is sufficient to reconstruct state on restart.
- **R-4** Explicit state machines with guarded transitions, kept separate: execution state (idle, running, waiting_for_user, waiting_for_provider, rendering, paused, recovering, failed, cancelled); review state (unreviewed, findings_open, changes_required, ready_for_user_review); acceptance state (unaccepted, accepted_at_revision, superseded); stop reason (user_pause, user_cancel, missing_evidence, stalled, attempt_limit, budget_limit, time_limit, execution_failure, ready_for_user_review).
- **R-5** Agent outputs are validated against Alloy's schemas. Malformed or contradictory outputs are rejected with actionable validation errors. One bounded repair round is permitted. A failed repair fails the operation. A parsing failure never becomes approval, a closed finding, or an advanced stage.
- **R-6** Acceptance binds to an exact component revision plus the versions of its evidence and dependencies. Superseded acceptances remain visible in history.

### 4.2 Two agents who both build

- **R-7** Two workflow identities, Agent A and Agent B, each bound to a provider, model, and settings, with an isolated provider session. Both can inspect references and renders; analyze geometry, materials, proportions, and construction; propose and implement changes; request modeling and render operations; inspect the other agent's implementation; and take ownership to correct problems directly.
- **R-8** No permanent builder/reviewer split and no assumption that a provider is inherently better at geometry, materials, or critique. A simple assignment strategy based on verified capabilities, dependencies, task needs, and observed outcomes. Ownership and a one-line rationale are recorded per task. User override and reassignment are supported.
- **R-9** Over a substantive run both agents make useful implementation contributions. Contribution counts are never forced to balance.
- **R-10** Two agents on the same provider and model still get isolated sessions, histories, working contexts, and operation identities. Never use a "continue the most recent conversation" mechanism. Persist explicit provider session IDs where the CLI supports them (Appendix A); otherwise reconstruct each agent's context from authoritative records.
- **R-11** Independent observation: at intake, collect each agent's observations before sharing the other's analysis; during review, provide references, actual renders, construction constraints, and a neutral inspection objective before revealing the builder's self-assessment; record the reviewer's initial findings before reconciliation. When an agent's persistent context already contains the withheld material, run an isolated review invocation (fresh session under the same agent identity with a reconstructed evidence packet).
- **R-12** No automatic judges or additional agents. Routine coordination (ordering, tie-breaks, bookkeeping) is code.
- **R-13** Agent-to-agent requests go through the engine's identities, budgets, and scheduler. `[[ASK:...]]` and `[[TOOL:...]]` syntax is neither advertised to builder agents nor honored if emitted (logged and ignored). No recursive invocation.
- **R-14** Reference images, renders, and the other agent's outputs are data. Only the user and the engine issue instructions, and prompt packets state this.

### 4.3 Provider adapters and preflight

- **R-15** Per-agent preflight reports three tiers for each capability: declared (config), locally checked (CLI present, version, flags parsed from `--help`), and live-verified (a real minimal invocation). Capabilities: image reading, evidence-directory access, read-only enforcement, session create and resume, structured output or validated fallback parsing, model and reasoning settings applied, usage reporting, cancellation and long-running behavior.
- **R-16** Image access is live-verified with a probe: a generated image with known content (shape, color, number) is delivered exactly the way real evidence will be, and the agent must report the content through structured output. A filename in text is not delivery. A text description never substitutes for image access.
- **R-17** Read-only enforcement is live-verified: under the restricted mode the agent is asked to write a file in its scratch directory and the write must fail. Restrictions a CLI cannot enforce are documented as prompt-only, and that configuration is labeled unsupported for shared write.
- **R-18** Cached verification is keyed on CLI path and version, model, and settings, and is invalidated on any change.
- **R-19** Never invent model identifiers, flags, reasoning levels, context limits, pricing, or usage field names. Sources are `--help` output, the CLI's own JSON output, and official documentation. Anything else is reported as unknown.
- **R-20** Use the highest available reasoning setting where a provider exposes one (Appendix A) and show the effective setting in the CLI and GUI. Never silently substitute a provider or model. Builder invocations never pass provider-level fallback options.
- **R-21** A missing required capability blocks the run with a specific explanation and remediation.
- **R-22** Separate timers: provider response timeout, provider inactivity detection (no output for N seconds while the process is alive), and per-operation modeling and render deadlines. A quiet, active Blender render is not a stalled chat response. Drain stdout and stderr concurrently, expose progress, and on cancellation kill the whole process tree (Windows: `taskkill /T` or equivalent) and confirm it is gone.
- **R-23** Retries respect side effects. Transport retries apply only to operations with no side effects. After an uncertain outcome (timeout, crash, lost result) the engine reconciles operation state from the journal, staging area, and revisions before anything that could edit geometry or commit runs again.
- **R-24** Structured output uses the provider's native mechanism where one exists (Appendix A); otherwise JSON is extracted from text. Both paths go through R-5.
- **R-25** Usage capture per invocation: input, output, cached, image, and reasoning tokens plus cost where the provider reports them. Unreported fields are recorded as unknown, never zero. Measured and estimated values are separate fields. Operation counts and render time are diagnostics, never quality metrics.

### 4.4 Reference intake and reconstruction brief

- **R-26** Accept one or more images plus optional labels (front, side, rear, top, underside, three-quarter), detail crops and target regions, known dimensions with units, desired overall scale, evidence preference when references disagree, pose and articulation notes, and kind labels (target, previous attempt, rejected result, exploratory hypothesis).
- **R-27** Originals are preserved unmodified. Store hash, pixel dimensions, labels, and crop coordinates in original-image space. Replacing a reference creates a new evidence version. Composite concept sheets require an explicit target region so unrelated subjects never enter the evidence.
- **R-28** Before detailed modeling each agent independently reports: silhouette and overall proportions; main masses and depth; separate pieces and possible construction; armour versus supporting frame; overlap, gaps, joints, attachment points; material differences; camera and pose ambiguities; contradictions, occlusion, missing views, uncertainty.
- **R-29** Observations reconcile into a concise, editable reconstruction brief: intended asset and authoritative references; modeling scope and desired editable deliverables; scale and coordinate conventions; symmetry and pose assumptions; fidelity priorities; observed versus inferred construction; missing evidence and unresolved decisions. Provisional scale and relative measurements are used when real scale is absent. No false precision.
- **R-30** No assumptions of orthographic references, perfect symmetry, consistent perspective, or physically realizable construction. Where an interpretation affects the model, the chosen interpretation and competing hypotheses are recorded.
- **R-31** Targeted user questions are asked only when the answer would materially change construction. Independent work that does not depend on the answer continues.
- **R-32** Generated turnarounds or studies are labeled hypotheses and never count as evidence of the original design.

### 4.5 Global blockout, part inventory, dependencies

- **R-33** Before detailed work on the starting component: a coarse whole-model blockout, coordinate system, provisional camera alignment, and relevant attachment interfaces. "Start with Head" means the head is the first detailed component; it does not permit ignoring body proportions or neck placement.
- **R-34** A persistent, editable part inventory is created before detailed construction and refined as evidence reveals more parts. No speculative exhaustive enumeration before blockout.
- **R-35** Per part or repeated-part group: stable ID, name, parent assembly; supporting references and regions; function or construction interpretation; approximate dimensions, ratios, uncertainty, position, orientation; attachments, depth, layering, thickness, gaps, clearances; materials; observed, inferred, or mixed evidence status; confidence and unresolved questions; dependencies, owner, implementation state, review coverage; corresponding Blender objects, instances, materials, or procedural controls.
- **R-36** Typed construction relations (covers, sits_under, supports, passes_behind, recessed_in, attached_to, and similar) are kept distinct from scheduling dependencies. Dependency validation never forces physical relations into a strictly acyclic hierarchy.
- **R-37** Inventory IDs are written into Blender metadata (custom properties on objects, collections, and materials). After every operation the engine detects deleted, duplicated, unmapped, and unintentionally orphaned items. Object names are not identity.
- **R-38** Units, axes, origins, transforms, attachment interfaces, and clearance expectations are defined for separately built components.
- **R-39** An upstream change (dimension, pose, reference, camera interpretation, material, interface) identifies affected downstream work. Unaffected evidence is preserved; affected renders, reviews, and acceptances are invalidated visibly, never kept silently.

### 4.6 Editing ownership, checkpoints, recovery

- **R-40** One authoritative assembly with immutable committed revisions.
- **R-41** Only one owner (an agent or an engine operation) may modify a shared source at a time, enforced at the execution boundary through operation IDs, expected base revisions, ownership tokens that stale holders cannot reuse after handoff, staged working outputs, validation before commit, and durable commit records.
- **R-42** Isolated workspaces per agent or operation and a controlled promotion path. A lock file or working-directory setting is not treated as enforcement. Shared-write configurations that cannot be enforced are not offered as supported.
- **R-43** Parallel component work only when ownership and interfaces are clear and files are isolated. Integration loads the actual components into the assembly and checks transforms, attachments, and relevant clearances.
- **R-44** Handoff: finish or cancel pending writes; save and validate the owned checkpoint; record source revision, changed parts, assumptions, findings, pending issues; release ownership; grant the next owner control over the verified revision.
- **R-45** Stale operations are rejected. External or manual changes to workflow-owned files are detected by hash mismatch and reconciled explicitly, never overwritten.
- **R-46** Checkpoints include everything needed to reopen a usable artifact: the .blend revision, owned linked assets and textures, scripts, manifests. Restore touches only workflow-owned files. Never a broad project rollback.
- **R-47** Restart: inspect the journal, staging area, committed revisions, locks, and live processes; classify each in-flight operation as never started, failed, committed, or uncertain; never replay mutations blindly; preserve unknown partial outputs for diagnosis; recover to a verified state with no duplicate geometry and no silent stage advancement.
- **R-48** Disk failures, interrupted saves, corrupted files, and missing external assets are surfaced, never hidden.

### 4.7 Modeling strategy and editability

- **R-49** Build from large forms to small details: overall proportions, silhouette, pose, main masses; supporting frame and major joints; armour and outer housings; brackets, actuators, mechanisms, fittings; bolts, washers, hoses, wires, vents, seams, lenses; materials and surface refinement; full assembly and cross-view inspection. Material studies may begin early. The full assembly is checked throughout.
- **R-50** Earlier stages reopen when new evidence exposes a proportion or construction problem. Incorrect primary shapes are never covered with extra hardware.
- **R-51** Distinct editable parts where the reference supports them, preserving thickness, layering, gaps, and plausible attachment. No invisible hardware invented to increase complexity.
- **R-52** Prefer stable parameterized construction, reusable operations, and localized changes. Preserve meaningful modifier stacks, material controls, and component organization. Repeated hardware may use instances; instance identities and their actual placement and variation are inspected.
- **R-53** Geometry versus texture is chosen by visibility, silhouette, depth, lighting response, and editability. Visible structural form is never replaced by a flat visual trick.
- **R-54** Local edits are preferred over scene regeneration. Any regeneration must preserve IDs and unrelated manual work, verified by validation.
- **R-55** No game polygon limits by default. Retopology, baking, LODs, rigging, and runtime export are separate, explicitly selected stages.

### 4.8 Collaborative improvement loop

- **R-56** Per component or coherent group: assemble evidence, constraints, dependency versions, open findings; obtain independent observations for new or materially changed evidence; reconcile into a concrete construction plan; assign a bounded task with expected outcomes; acquire ownership and checkpoint the base revision; build or revise; validate artifact integrity and render the actual geometry; have the other agent inspect references and renders independently; record specific findings before sharing self-assessment; decide whether to correct, gather evidence, reassign, or request user review; hand off ownership when the other agent can usefully implement corrections; render and review affected areas again; integrate and inspect within the full assembly.
- **R-57** Validated observations are reused when their evidence and dependencies are unchanged. Intake analysis is not repeated after every small edit.
- **R-58** Every edit has a stated purpose and expected outcome. "No useful edit warranted" is a valid, evidence-backed result. Cosmetic changes are never forced to demonstrate activity.
- **R-59** User feedback that arrives during an operation is recorded immediately and applied at the next safe boundary. Conflicting instructions are never injected into an active write.

### 4.9 Cameras, renders, evidence provenance

- **R-60** Reference and model comparisons use matched perspective, pose, framing, and scale. Camera parameters and alignment assumptions are stored. Uncertain calibration is a labeled hypothesis. Camera changes never conceal geometry errors.
- **R-61** View set as needed: whole model; front, side, rear, three-quarter; top and underside; component close-ups; armour-hidden; exploded; cross-sections and measurement overlays. Views without matching source evidence are labeled "inferred construction".
- **R-62** All renders are produced by the backend from exact source revisions. Agent-supplied, invented, or retouched images are never evidence.
- **R-63** Render manifest: source revision and asset dependency hashes; camera and pose; visibility and isolated parts; materials and lighting; renderer and version with relevant settings; resolution, color management, seed; output hash; success or failure. Render cache keys include every input that affects the image. File existence or timestamps are insufficient.
- **R-64** Stale, failed, missing, unreadable, or mismatched renders are detected and block review completion.
- **R-65** Evidence resolution matches the claimed detail: overview images for navigation, adequate-resolution crops for detail inspection. A whole-body thumbnail cannot support a bolt, seam, or wire finding.

### 4.10 Geometry and appearance review

- **R-66** Separate tracked dimensions: global proportions and silhouette; depth, construction, and layering; component and instance coverage; materials and appearance; artifact integrity and editability. A major failure in one dimension is never averaged away by strengths elsewhere.
- **R-67** Geometry review covers shape, dimensions, placement, orientation; depth and cross-view consistency; thickness, recesses, protrusions, bevels, edge profiles; spacing, alignment, gaps, clearances; attachment plausibility; cable and hose routing; floating parts and unintended intersections; consistency with relevant references.
- **R-68** Measurements and collision checks are supported with their limitations stated. Intended contact or overlap is distinguished from defects. A bounding-box intersection is not conclusive collision evidence.
- **R-69** Material review covers base color; metallic versus non-metallic response; roughness, reflectivity, highlights; cast, machined, painted, rubber, glass surfaces; texture scale and direction; reference-supported wear; lens depth, tint, emission, reflections; lighting, shadow depth, exposure, color management.
- **R-70** Stable neutral inspection lighting, plus separate reference-matching lighting where useful. Clay and material renders are compared to separate geometry, material, and lighting errors. A dark reference region is not assumed to be dark paint. No arbitrary grime, excessive glow, exaggerated bevels, uniformly shiny metal, or post-processing that conceals defects.
- **R-71** Quantitative comparisons only where their assumptions hold (camera alignment, segmentation, occlusion, stylization limit pixel and silhouette metrics). Agent-generated numerical scores are judgments, not calibrated measurements.
- **R-72** Coverage is tracked by part, view, region, and revision. For repeated details, inspected instances and any sampling strategy are recorded. Sampling is never presented as individual inspection of every instance.

### 4.11 Findings, regressions, user acceptance

- **R-73** Finding record: stable ID; part, region, view; specific observed mismatch; severity and confidence as separate fields; supporting reference and model evidence; source revision; proposed correction and observable expected improvement; owner, status, attempt history; before and after evidence and resolution rationale. Observed defects are distinguished from uncertain interpretations; competing hypotheses are retained where useful.
- **R-74** A finding closes only when the expected visual or structural improvement is verified on a new render and relevant regressions are checked. Never because a script executed.
- **R-75** User actions: request correction; accept component; reopen component; mark unresolved ambiguity; explicit waiver with rationale; final model acceptance.
- **R-76** Attended default: continuous previews and user acceptance before advancing beyond a detailed component. Whole-model planning and blockout precede that component, and the UI explains this. Unattended mode advances within configured limits and leaves components unaccepted until user review. "Ready for user review" is always distinct from "accepted by user".

### 4.12 Token efficiency (never at the expense of 4.9 to 4.11)

- **R-77** Authoritative state lives in records, not in growing conversations. Each task receives a task-specific packet (relevant evidence, part records, interfaces, open findings, revision deltas) plus a concise global brief so whole-model constraints are retained.
- **R-78** Stable instructions and provider caching are used only where the provider actually supports them. No claim that local caching reduces provider billing.
- **R-79** Independent sessions and deliberate context reconstruction instead of transcript replay. Each agent receives a "changed since your last verified revision" summary. Full records remain retrievable on request.
- **R-80** Context compaction preserves unresolved questions, user requirements, rejected approaches, and evidence links. Summaries never replace required image access, and image detail is never reduced below what the inspection needs.
- **R-81** Render artifacts are reused only when their complete inputs are unchanged. Changed parts and affected dependencies are re-inspected, with whole-model checks at integration milestones.
- **R-82** Independent read-only inspections and coherent repeated-part tasks are batched. No conversational acknowledgments, repeated planning, full rewrites, or agent calls for deterministic bookkeeping. Measurement, hashing, validation, scheduling, and budget accounting run in code.
- **R-83** Agent outputs are concise and structured, with expandable rationale when needed.
- **R-84** A repeatable efficiency comparison harness runs the same fixture under naive transcript replay and under packet delivery, reporting delivered bytes and tokens alongside preserved evidence and review coverage. No invented savings percentages and no quality-equivalence claims without evidence. Compaction and cache-invalidation behavior are covered by tests.

### 4.13 Limits, stall detection, stopping

- **R-85** The run continues while meaningful evidence-supported improvements remain and limits allow. Explicit limits: wall clock; usage or cost where measurable; request count; render count; attempts per issue. Pause, resume, cancel, and user feedback are supported. Enforceable limits are labeled separately from estimates. Unknown provider cost is never treated as zero and never as proof that a monetary cap is enforced.
- **R-86** In-flight work is accounted for. No new work is dispatched after a limit is reached.
- **R-87** Modeling correction attempts are counted separately from transport retries.
- **R-88** After two unsuccessful corrections of the same finding: preserve attempts and evidence; reassess whether the cause is geometry, camera, material, lighting, or insufficient reference evidence; restore an appropriate checkpoint only when ownership and dependencies make it safe; then try a materially different approach, perform fresh analysis, reassign, or report the evidence gap. No endless oscillation. Increasing detail is not progress.
- **R-89** Terminal statuses use the stop reasons in R-4 and always show remaining discrepancies and uncertainties. Nothing is labeled "complete" by default.

### 4.14 CLI, GUI, and the Blender preset

- **R-90** A CLI entry (for example `python main.py --build ...` or `python -m builder ...`) that creates or opens a project, runs preflight, starts, pauses, resumes, and cancels runs, lists findings, accepts or reopens components, restores checkpoints, and prints status, all through the engine.
- **R-91** A view in the existing Tk app connected to the same engine through a worker thread and queue, exposing: project and reference intake; agent, model, and reasoning selection with capability status; current stage, task, ownership, and active operation; part tree with construction relations; reference and render comparison with zoom and component or view selection; before and after revisions; toggleable measurements; observed and inferred labels; findings linked to parts and images; review coverage; consumption and configured limits; correction, acceptance, pause and resume, and checkpoint controls.
- **R-92** Previews are actual artifacts labeled with revision and render IDs. Aspect ratios are preserved. Presentation alignment is distinguished from altered evidence. The GUI stays responsive during provider and Blender work and shows progress and useful errors instead of a frozen chat.
- **R-93** Editable preset "E10 Bearer - Head": project `C:\Death Factory`; asset "E10 Bearer, the humanoid enemy"; target `C:\Death Factory\docs\art\locked-theme-concept-v1.png`; target region "lower-left humanoid", with coordinates chosen by the user during an authorized real run and never guessed; existing unfinished source `C:\Death Factory\art-source\blender\E10\E10_Bearer_v4_proportions.blend`, not assumed to have correct proportions; first detailed component: Head. The target concept is distinguished from rejected screenshots. No unseen reference views are assumed. Creating or opening the preset never launches modeling, reads the PNG, or opens the .blend.

---

## 5. Blender backend contract

- **Discovery.** `blender.executable` in config; auto-detect `C:\Program Files\Blender Foundation\Blender *\blender.exe`. Preflight records the version from `-b --version` and runs a `--python-expr` smoke test.
- **Execution.** `blender.exe -b <staged.blend> --python <op.py> -- <json-args-path>` as an argv list. Use `--python-exit-code <n>` **(verify in `blender --help`)** so Python exceptions fail the process. Scripts write a JSON result file; stdout and stderr go to `logs/`. Every operation has its own deadline and process-tree cleanup.
- **Staging and promotion.** Copy the authoritative revision to `staging/<op_id>/`; run; validate (file reopens; identity map intact; no NaN transforms; expected parts present; no unexpected deletions); save with relative or packed assets; hash; promote atomically to `revisions/<rev_id>.blend`; journal.
- **Identity.** A custom property (for example `alloy_id`) on objects, collections, and materials. A validation script enumerates them and diffs against the inventory.
- **Renders.** Workbench for neutral clay, EEVEE for materials **(verify engine identifiers in Blender 5.2)**. Camera, lighting, and visibility come from records. Output PNG plus manifest per R-63.
- **Measurements.** Bounding boxes, distances between named features, part dimensions, ratio checks, overlap candidates, each reported with limitations per R-68.
- **Fixture.** A generated, disposable project: primitives assembled into a small multi-part object with controlled defects (incorrect depth, a floating fitting, a material mismatch, stale evidence). Used by real-Blender tests. Never the Bearer files.

---

## 6. Validation layers and test matrix

Layers: **D** deterministic (mock providers, no Blender, no network); **B** real Blender (skipped with a stated reason when Blender is absent); **U** actual CLI and GUI interaction with screenshots where the environment allows; **L** live multimodal providers (opt-in, bounded, user-approved).

| Test area | Layer | Requirements |
|---|---|---|
| Both agents make implementation contributions | D | R-7, R-9 |
| Same-provider session isolation | D, L | R-10 |
| Independent review-context construction | D | R-11 |
| Ownership, handoffs, stale operations, conflicting commits | D, B | R-41, R-44, R-45 |
| Parallel components and integration | D, B | R-43 |
| Reference and image delivery contract | D, L | R-16, R-27 |
| Missing, failed, stale, mismatched renders | D, B | R-63, R-64 |
| Dependency and acceptance invalidation | D | R-6, R-39 |
| Unsupported provider configuration | D | R-15, R-21 |
| Structured findings and malformed output | D | R-5, R-24, R-73 |
| Provider, model, subprocess, render failures | D, B | R-22, R-23, R-48 |
| No fallback on any builder path | D | R-20, D3 |
| Cancellation and process cleanup | B | R-22 |
| Restart recovery at edit, save, commit boundaries | D, B | R-47 |
| Duplicate-operation prevention after uncertain outcomes | D | R-23, R-47 |
| Two failed corrections then reassessment | D | R-88 |
| Limits and unknown consumption | D | R-25, R-85, R-86 |
| Attended versus unattended advancement | D | R-76 |
| Windows paths with spaces, Unicode, shell metacharacters | D, B | D3, R-22 |
| New config keys survive a settings-editor save | D | D9 |
| Preservation of unrelated files and configuration | D | Section 2 |
| Existing modes and GUI behavior unchanged | D, U | D2 |
| End-to-end fixture: build, correct, render, close-up, materials, checkpoint restore, reopen | B | Section 5, R-46, R-74 |
| Efficiency comparison harness | D | R-84 |

Mocks state exactly what they script. A fixture adapter that emits the expected finding proves routing and state handling, not visual intelligence.

---

## 7. Phases and deliverables

- **Phase 0. Inspect and design (no production code).** Verify Section 2 against the code and tools. Write `docs/plans/<date>-collaborative-model-builder-design.md` covering module layout, record schemas and journal, the four state machines, the Blender runner contract, the adapter contract and preflight tiers, evidence packet formats, the test plan by R-number, decisions and tradeoffs, and the phase plan. Stop for owner review.
- **Phase 1. Vertical slice.** Engine, records and journal, state machines, Blender runner with staging, validation, and promotion, render manifests, fixture project with controlled defects, mock adapters (Agent A builds; Agent B finds and corrects a defect; handoff; checkpoint restore; reopen), CLI runner, and the D and B tests for every matrix row they cover. Stop and report.
- **Phase 2. Real adapters.** claude, gemini, and codex argv adapters; preflight with declared, local, and live tiers; session isolation; image probe; structured output and repair; usage capture; cancellation. Opt-in bounded live smoke test, run only after explicit owner approval of scope and budget. Stop and report.
- **Phase 3. GUI.** The view in R-91 and R-92 with screenshots; attended and unattended controls. Stop and report.
- **Phase 4. Hardening and delivery.** Recovery and limits (R-47, R-85 to R-89), the efficiency harness (R-84), documentation (Section 8), packaging (`requirements.txt`, `alloy.spec`, launch scripts). Final report.

Every phase ends with: tests run and their actual output by layer; an explicit list of what was not verified and why; remaining limitations; confirmation that the Bearer assets were untouched (unchanged size and modification time, checked without opening them).

---

## 8. Documentation deliverables

Installation and Blender discovery and configuration; launching from CLI and GUI; selecting and verifying both agents; reference labeling and target-region selection; assignments, ownership, handoffs; geometry, material, and layering inspection; findings, corrections, acceptance; pause and resume, restart recovery, checkpoint restoration; unattended operation and resource limits; token-efficiency behavior and usage reporting; editable output layout and reopening the result; known limitations and unverified integrations.

---

## 9. Out of scope and prohibited

- Reading, rendering, copying, modifying, or "just inspecting" the real Bearer assets during development and testing.
- Remodeling Bearer in an implementation session.
- Live paid provider runs without explicit owner approval of scope and budget.
- Migrating the GUI framework; refactoring unrelated modes; committing; stashing; changing line endings; deleting or rewriting the untracked working-tree files.
- Speculative backends beyond Blender.

---

## Appendix A. Verified CLI capabilities (2026-09-07, re-verify at implementation time)

**claude 2.1.233** (`claude -p`): `--session-id <uuid>` creates a session with a chosen UUID; `--resume <id>` continues it; `--fork-session`; `--no-session-persistence` (do not use for agents); `--output-format json|stream-json`; `--json-schema <schema>` for structured output; `--effort low|medium|high|xhigh|max`; `--model <alias|full>`; `--allowedTools` and `--disallowedTools`; `--permission-mode <mode>`; `--add-dir <dirs>`; `--max-budget-usd <amount>` (an enforceable per-invocation cap); `--append-system-prompt`. `--fallback-model` exists and must never be passed. Images: read through the Read tool given a path under an allowed directory **(live-verify with the probe)**. Usage: from the JSON result **(read the actual field names at runtime; report unknown if absent)**.

**gemini 0.55.1** (`gemini -p`): `--session-id <uuid>` starts a new session; `--resume <latest|index>` **(verify whether a UUID is accepted; if not, reconstruct context from records per R-10)**; `--session-file`; `--list-sessions`; `-o json|stream-json`; `--approval-mode plan` (read-only), `auto_edit`, `yolo`; `--include-directories`; `-m <model>`. No reasoning-effort flag observed. Images: multimodal file reading **(live-verify)**. Usage: from JSON output **(verify)**.

**codex 0.147.0** (`codex exec`): `-i/--image <file>...` attaches images explicitly; `codex exec resume <uuid> [prompt]` continues a session (`--last` must never be used); `-m <model>`; `-c key=value` config overrides (reasoning effort is a config key: **verify the exact name in the Codex documentation**); `-s read-only|workspace-write|danger-full-access`; `-C <dir>`; `--add-dir`; `--ephemeral` (do not use for agents); `--output-schema <file>`; `--json` (JSONL events: the source of the session ID and usage, **verify event names**); `-o/--output-last-message <file>`.

**Blender 5.2.1**: `-b`, `--python <file>`, `--python-expr`, `--` argument separator, `--version`; `--python-exit-code` **(verify)**.

## Appendix B. Mapping from the original draft

Original section 1 to Sections 2 and 3; 2 to 4.1; 3 to 4.2; 4 to 4.3 and Appendix A; 5 to 4.4; 6 to 4.5; 7 to 4.6; 8 to 4.7; 9 to 4.8; 10 to 4.9; 11 to 4.10; 12 to 4.11; 13 to 4.12; 14 to 4.13; 15 to 4.14; 16 to Sections 6, 7, 8, and 9.
