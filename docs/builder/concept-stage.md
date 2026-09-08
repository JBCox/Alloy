# The concept stage

Purpose: generating the canon reference set before modeling when the project starts from a text idea or from a few seed images (addendum A). The image seat is manual: Alloy's art director writes prompts, the owner generates in ChatGPT or Gemini, and the files come back through `concept import`.

Principle (A.1, R-32): a generated image is never evidence of a pre-existing design. Unapproved generations are hypotheses; approved images become the design, ranked by precedence; contradictions are recorded as conflicts and never averaged (R-95).

Every `concept` verb prints the approval mode first (`approval mode each: the owner approves every generated image before it is used`), and the GUI shows it in the window's top bar and at the top of the Concept tab (D11).

Verbs that may spend LLM usage (`start`, `import`, `approve`, `reject`, `regenerate`, `study`) need a current live preflight report exactly like `start` does; `--mock <screenplay.json>` binds scripted seats. `prompts`, `list`, `show`, `proceed`, `abandon`, and `failed` never invoke an agent.

## Start modes (R-99)

From text (the art director writes the anchor prompt: one LLM call):

```text
python -m builder concept start "<wf>" --from-text "a compact desk lamp with a cast base, a steel post, and a hinged hood" [--approval each|anchor_only|auto] [--views front,side]
```

From seed images (approved on entry with kind `target`; the first is the anchor; the canon description and view prompts are written at once):

```text
python -m builder concept start "<wf>" --from-image "C:\refs\hero.png" --labels front --from-image "C:\refs\side.png" --labels side
```

Both may be combined. `--approval` defaults to `builder.concept.approval`; `--views` (comma-separated) defaults to `builder.concept.views`. One concept stage per project. In the GUI: "Start from text..." and "Start from seed images..." on the Concept tab.

The canon state machine (`state.py`, kind `concept_plan`) moves `no_canon -> anchor_pending -> anchor_approved -> turnaround_pending -> turnaround_approved -> complete`; seed images jump straight to `anchor_approved`; rejections move it backwards. `start` (the modeling run) refuses while a concept plan exists that is not `complete` (R-103) and builds its intake packets from approved references only (R-94).

## Approval modes (D11)

- `each` (default): the owner approves every generated image before it is used. Verdicts are printed; nothing is approved or rejected automatically. In `each`, an inconsistent candidate stays a candidate so the owner may still approve it (recorded with the verdicts).
- `anchor_only`: the owner approves the anchor; a view or study is auto-approved when both LLM seats independently judge it `consistent` and no conflict is open (actor `engine`, `auto: true`, criteria text recorded); an `inconsistent` one is rejected with the verdict reasons and regenerated while the cap allows.
- `auto`: the art director also picks the anchor among the imported candidates through the `anchor_pick` schema (criteria and rejected alternatives recorded on the reference), and views and studies follow the `anchor_only` rules. Every pick is journaled and reversible with `concept reject`, which returns canon to `anchor_pending` and opens a new anchor round.

In every mode an `uncertain` or `malformed` verdict is escalated to the owner and never auto-approved or silently regenerated.

## The request, prompt, import loop (D12, R-96, R-96a, R-110)

1. Print the open generation requests:

```text
python -m builder concept prompts "<wf>"
```

Each request shows its id (`gen_...`), target (anchor candidates, a view, or a study), round, the art director seat, the number of images expected, the prompt file (`concept/requests/<gen_id>/PROMPT.md`), the prompt text to paste as-is, the images to attach in order (copies under `concept/requests/<gen_id>/attach/` with reference ids and hashes), and the exact import command.

2. Generate in the app with that prompt and those attachments; save the images unmodified (PNG preferred).

3. Import:

```text
python -m builder concept import "<wf>" <gen_id> "C:\out\a.png" "C:\out\b.png" --vendor chatgpt --model "<as shown in the app>" [--attached ref_x,ref_y] [--no-check]
```

Files are copied unmodified under `refs/generated/<gen_id>/`, hashed, linked to the request, and marked `candidate` (R-96a, R-97). `--vendor` (`chatgpt`, `gemini`, `other`), `--model`, and `--attached` are recorded as declarations by the owner; Alloy prints `(declarations by the owner, not verified)`. An import with no matching open request is refused; create the request at import with `--as-anchor` (files as anchor candidates) or `--as-view <view>` (one of the needed views; an anchor must exist), in which case the first positional is a file, not a request id. The image limit is checked before the copy.

Unless `--no-check` is given, the import then advances the stage: both LLM seats give their verdicts (spends usage), the mode's decisions apply, and the coverage is printed.

Watching a folder instead of naming files (all files go to one request; a file is taken once its size has been stable for one poll):

```text
python -m builder concept import "<wf>" <gen_id> --watch "C:\out" [--watch-timeout 600] [--watch-max 4] [--watch-existing]
```

4. A request the app refused or that produced nothing usable is closed with a manifest either way (R-96):

```text
python -m builder concept abandon "<wf>" <gen_id> --reason "..."
python -m builder concept failed "<wf>" <gen_id> --reason "..."
```

The generation record is the manifest (`concept/requests/<gen_id>/manifest.json`, rewritten on every state change): target, prompt and its structured fields, attachments with hashes, seat, declarations, outputs with hashes, `usage` and `cost` as `not_applicable` (never zero), timestamps, success or error, round, and `revises`.

In the GUI (Concept tab): the open requests table, the prompt with a "Copy prompt" button, the attachment list and the CLI equivalent, declared vendor and model fields, "Import generated images..." (file dialog), "Abandon", and "Mark failed".

## Verdicts from both seats (R-102)

After an import of a view or study, `ConceptStage._check_candidate` asks each LLM seat, one after the other, to judge the candidate against the approved anchor and the canon description. Each packet labels the anchor `approved canon` and the candidate `CANDIDATE: a hypothesis, not canon`, withholds the other seat's verdict, and the first verdict is journaled before the second packet is built. A verdict names each inconsistency by part, region, what differs, and severity. A verdict that refers to a different reference id is downgraded to `uncertain`; a malformed reply is recorded as `malformed` and approves nothing (R-5).

The summary over both seats is `inconsistent` if either says so, else `uncertain` if either does, else `consistent`; `pending` while a seat has not answered. The CLI prints `verdicts for ref_... (front): A=consistent, B=inconsistent`; the GUI shows both verdicts with rationale and every inconsistency beside the anchor.

Anchor candidates are never checked by verdict: the owner (or the art director in `auto`) picks among them.

## Approving, rejecting, regenerating

```text
python -m builder concept approve "<wf>" ref_... [ref_...]
python -m builder concept reject "<wf>" ref_... --reason "..."
python -m builder concept regenerate "<wf>" <gen_id or ref_id> [--note "what to change"]
```

- Approving an anchor candidate makes it the anchor (labels become `anchor`), rejects its siblings from the same request, and triggers the canon description (R-101, one LLM call by the art director) and the view requests (one LLM call per needed view). Approving a view supersedes an earlier approved image for the same view; approving a study attaches it to its part.
- Every approval records the mode in force, the actor, the seats' verdicts, and the criteria (R-98). A manual approval must come from a user actor.
- Rejecting the anchor returns canon to `anchor_pending`; rejecting an approved view after `turnaround_approved` returns to `turnaround_pending`. Rejecting every output of a request marks the request `rejected`.
- `regenerate` abandons the open request and writes a revised prompt as a new request (round `n+1`), carrying the owner's note and the verdict problems; bounded by `max_regenerations_per_view`.

## Conflicts are never averaged (R-95)

When the owner approves a candidate whose verdicts named inconsistencies with the anchor, one `evidence_conflict` record is created per inconsistency (both reference ids, region, what differs, reporting seat). Open conflicts block `turnaround_approved`, `complete`, `proceed`, and every auto-approval. A conflict resolves when the owner rejects one of its images (`resolved_by_user`) or a regenerated image supersedes one (`resolved_by_regeneration`). `concept list` prints `open conflicts: ... (reject one image of each, or regenerate; never averaged)`.

## Coverage and proceed (R-103)

```text
python -m builder concept list "<wf>"
```

prints the canon state, the anchor, the image count against `max_images`, open conflicts, and one line per needed view with its status: `approved`, `candidates`, `requested`, or `nothing`; studies per part; missing approved views; escalations to the owner; and a `proceeded with a partial set` line when that happened. `concept show <id>` prints one record (reference, generation, study, conflict, canon description, or plan) as JSON.

When every needed view is approved and no conflict is open, the stage advances itself to `complete` and prints `canon complete: the approved set is ready for intake`. To hand off with views still missing:

```text
python -m builder concept proceed "<wf>"
```

`proceed` requires an approved anchor and canon description, refuses while conflicts are open, and records `proceeded_partial` (by whom, when, which views were missing) on the plan. In the GUI: "Proceed with partial set" (confirmed).

## Studies (R-104)

A per-piece study of a part in the inventory, conditioned on the anchor and the approved view of the same name:

```text
python -m builder concept study "<wf>" --part p_bracket --view side [--region "mount"] [--purpose "how the bracket meets the housing"]
```

The part must exist, an anchor must be approved, and the art director writes the prompt at once (one LLM call). Approved studies attach to the part with precedence `study` (3), below the turnaround, and appear in modeling packets. A reassessment may also request studies (`study_requests` in its schema); with an approved anchor the prompt is written immediately, otherwise a `study_request` record is kept for the owner and the stop note says how to generate it. In the GUI: select the part in the part tree, then "Request study...".

## Limits (R-105)

- `max_images` (default 40) counts every reference that came from a generation request, imported and generated alike; it is checked before a request is opened and before an import. Exceeding it stops with `concept limit max_images=... would be exceeded`.
- `max_regenerations_per_view` (default 3) bounds the rounds beyond the first per view or study, including the owner's own `concept regenerate`. When reached, the view is escalated: `ESCALATED to the owner (front): max_regenerations_per_view=3 reached ...; the owner decides: approve a candidate, `concept regenerate` after raising the cap, or `concept proceed``.
- The image seat's cost is `not_applicable` (never zero); the LLM calls the stage makes count in the normal request and cost tracker, whose snapshot lives on the concept plan while no run exists, so caps hold across CLI processes.

## The art director (R-107, R-108)

The art director is seat A by default and rotates to the other seat after two rejected generation rounds (rejected or failed requests); the seat and one-line rationale are recorded on the plan and on every request. The canon description is versioned per anchor and written by the art director in force.

## The Concept tab (R-110)

Top: the approval mode, then a coverage line (canon state, seat and cost kind, art director, image count, per-view status, missing views, open conflicts, escalations, partial-proceed note). Middle: the open requests table with the prompt, Copy prompt, attachments and CLI equivalent, declared vendor and model, Import, Abandon, Mark failed. Bottom: the candidates table (declared vendor/model, both verdicts, summary), the anchor beside the selected candidate (labelled hypothesis), the verdict text with every inconsistency and each seat's rationale, and Approve, Reject (reason required), Regenerate (note optional). Escalations and conflicts are shown; the panel never resolves them on its own. Selecting the Concept tab widens the right column.
