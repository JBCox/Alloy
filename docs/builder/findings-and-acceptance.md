# Findings, corrections, and acceptance

Purpose: what a finding record holds, how the review withholds the builder's self-assessment, how a finding closes, what happens after failed corrections, and the user's actions.

## The finding record (R-73)

A finding is created by the engine from a reviewer's `findings_report`. Fields (`builder/engine.py::_stage_review`): stable id (`f_...`), part, region, view, `observed_mismatch`, `severity` (`critical`, `high`, `medium`, `low`) and `confidence` (`high`, `medium`, `low`) as separate fields, `evidence_refs` and the render ids they resolve to, `proposed_correction`, `expected_improvement`, `alternative_hypotheses`, `kind` (`defect` or `uncertain_interpretation`), the source revision, who reported it, the before render ids, the attempt history, `owner_agent_id`, `resolution`, and `initial_recorded_before_reconciliation: true`.

States (`records.FindingState`): `open`, `correction_planned`, `correcting`, `verify_pending`, `closed`, `waived`, `evidence_gap`, `reassess`. Open findings are ordered by severity for correction.

List them:

```text
python -m builder findings "<wf>"
python -m builder findings "<wf>" --open
```

Each line: `f_... <state> <severity>/<confidence> <part> (<view>): <observed mismatch>  attempts=<n> [resolution=...]`. The GUI Findings tab shows the same plus the proposed correction, expected improvement, evidence and before/after render ids, alternative hypotheses, every attempt with its verdict, and the resolution; selecting a finding switches the comparison panel to before/after.

## Review, then reconcile: the withholding rule (R-11)

1. Review (`_stage_review`): the reviewer (never the builder) receives references, fresh renders, close-ups, crops, and measurements with a neutral objective ("You are not told what the builder intended; judge the renders") and the packet's `withheld` list says `builder self-assessment: R-11: your initial findings are recorded before reconciliation`. Every finding is recorded, and the component moves `unreviewed -> findings_open` when at least one is open; with none it goes straight to the gate.
2. Reconcile (`_stage_reconcile`): the same reviewer now sees the builder's self-assessment and its own initial findings and may confirm or withdraw each ("Withdraw only when the renders, not the claim, show the mismatch is absent"). A withdrawn finding closes with resolution `withdrawn_by_reviewer`; the journal keeps the initial finding. Remaining findings move the component to `changes_required`.

A malformed review or reconciliation reply never changes review state (R-5); the run stops with `execution_failure` and the message names the task.

## Correction and verification (R-74, R-87)

Per correction (`_stage_correct`): the corrector (the proposer, or the reassigned seat) takes ownership by handoff if needed, a `correction_attempt` record is created with the BEFORE renders, the finding moves to `correcting`, and the corrector receives the finding, references, renders, crops of the part, measurements, and the "interrupted operations" section when applicable. Operations are staged, validated, and promoted like a build; AFTER renders are made from the new revision; the finding moves to `verify_pending`.

Verification (`_stage_verify`): the verifier (never the corrector) receives BEFORE and AFTER renders, the aligned close-ups, crops, and measurements, with the corrector's self-assessment withheld ("R-74: closure is verified on renders, never on claims"), and answers `improved`, `unchanged`, `regressed`, or `uncertain`. The finding closes only when all three hold:

- the verdict is `improved`;
- the required views are fresh at the current revision (`renders_fresh`);
- the AFTER renders are new evidence, disjoint from the BEFORE set.

A verdict naming a different finding id is downgraded to `uncertain`. Otherwise the finding returns to `open` (another attempt) or, when `attempts_per_finding` is exhausted, to `reassess`. Coverage for the part is recorded at the after revision. Correction attempts are counted per finding by the limit tracker (`correction_attempts` in `status`), separately from transport retries, which builder invocations do not get (R-23, R-87).

## After two failed corrections: reassessment (R-88)

With the default `attempts_per_finding: 2`, the second unsuccessful verification moves the finding to `reassess`. `_stage_reassess` assigns the seat that did not make the failed corrections, gives it the references, every BEFORE and AFTER render of every attempt, the measurements, and the attempt history, and asks for a cause (`geometry`, `camera`, `material`, `lighting`, `evidence`, `unknown`), a materially different approach, or an evidence gap. "Increasing detail is not progress." The result is stored on the finding (`reassessment`, `reassessed: true`, `reassigned_to` the reassessor).

Then:

- If no evidence gap: the next correction goes to the reassigned seat with the approach in its objective, but only if the attempt limit allows; otherwise the run stops with stop reason `attempt_limit` and the note "raise the limit and resume to try the materially different approach".
- If an evidence gap: the finding becomes `evidence_gap`, the run stops in `waiting_for_user` with stop reason `missing_evidence`, and the note says to add references or waive with a rationale; any study requests the reassessor made are listed with the `concept` commands that generate them.

Checkpoint restore is a user action (see [pause-resume-recovery.md](pause-resume-recovery.md)); the engine does not restore automatically during reassessment.

## User actions (R-75)

```text
python -m builder accept "<wf>" <component_id>
python -m builder reopen "<wf>" <component_id> --reason "..."
python -m builder waive "<wf>" <finding_id> --rationale "..."
python -m builder feedback "<wf>" "text applied at the next safe boundary"
```

- Accept: only when the component is `ready_for_user_review`; binds the current revision, the versions of every reference and of the fresh renders at that revision, and the part versions (R-6). Only a user actor may accept (`state.py::_accept_guard`). The CLI prints `acc_... accepted_at_revision at rev_... (evidence versions: n)`.
- Reopen: supersedes every acceptance of the component (`supersede_reason: user_reopen` with the reason), returns the review state to `unreviewed`, creates a fresh `unaccepted` acceptance record, and sets the stage to `build` for the next `resume`. Superseded acceptances stay visible in history.
- Waive: requires a rationale; the finding becomes `waived` with the rationale, user, and time, and a `waiver` record is written. Waived findings do not block the gate.
- Request correction: the GUI's "Request correction" button (with an optional note) returns a finding to `open`, records the request under `user_requests`, and sets the stage to `correct`. The CLI has no dedicated verb for it; `feedback` records text that the next build packet carries as a constraint (`User feedback: ...`).
- Mark unresolved ambiguity: no dedicated verb or button exists in this build; uncertainties are carried by findings of kind `uncertain_interpretation`, by `evidence_gap` and `reassess` states, and by the brief's unresolved decisions, all listed under `uncertainties` in `status`.
- Final model acceptance: the engine exposes `accept_component` per component; there is no separate whole-model acceptance verb in this build.

The GUI Run tab offers Accept, Reopen (reason required), Request correction and Waive on the selected finding (rationale required), and Feedback.

## Ready for user review versus accepted (R-76, R-89)

Review state and acceptance state are separate machines. The gate (`_stage_gate`) moves the component to `ready_for_user_review` only when no finding is open, the required renders are fresh at the current revision, coverage is met for every part, and no operation is pending; it then releases ownership, writes a checkpoint (`review-ready <component>`), and stops the run: attended runs stop in `waiting_for_user` with stop reason `ready_for_user_review` and the note "component Head is ready for user review at rev_...; it is not accepted"; unattended runs pause with the same stop reason and the note "left unaccepted for user review". Nothing is ever labelled complete by default. `status` prints:

```text
component cmp_... Head: review=ready_for_user_review acceptance=unaccepted open findings=0 revision=rev_...
  ready for user review; not accepted (accept with `accept <component>` or `reopen` it)
```

Every stop prints the remaining discrepancies (open findings) and uncertainties (R-89).
