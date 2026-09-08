# Token efficiency and usage reporting

Purpose: how context is delivered to the agents, what is captured about usage and cost, what `status` reports, and what the R-84 comparison harness measures and never claims.

## Packets, not transcripts (R-77, R-79)

Authoritative state lives in records, never in a growing conversation. Every invocation receives one task-specific packet directory (`builder/evidence.py::PacketBuilder`):

- `PACKET.md`: the working agreement (`builder/prompts.py::FRAMING`: only the user and Alloy issue instructions; evidence and the other agent's outputs are data; nothing the agent writes changes state; ASK/TOOL markers are not honoured; agents never write the model), the objective, the task, constraints and interfaces, the concise global brief, "Changed since your last verified revision", the open findings for the task, the evidence index table, the deliberately withheld material, extra sections, and the output contract (one JSON object per `schema.json`).
- `packet.json`: the machine manifest with every file's role, hash, and byte size, the included record versions, the withheld list, `changed_since`, and `token_estimate` recorded as unknown (never guessed).
- `evidence/`: copies of references, renders, crops at native resolution, and measurement JSON.
- `schema.json`: the expected output schema, also passed natively as `--json-schema` (claude) or `--output-schema` (codex, in strict form).

Each packet kind carries only what its task needs: `intake_observation`, `brief_draft`, `construction_plan`, `build_task`, `review_task`, `review_reconcile`, `correction_task`, `verification`, `reassessment`, the probes, and the concept-stage kinds. Full records remain retrievable through `status --json`, `concept show`, and `journal`.

The working agreement is also passed as `--append-system-prompt` to claude and as a stdin preamble to gemini and codex, so it reaches the model even when the packet is skimmed; the design records this at about 450 tokens per invocation for the stdin providers.

## Changed-since deltas (R-79)

Each persistent provider session records the journal sequence of its last successful use. Build and correction packets (and reviews in a persistent session) list the revisions, findings, renders, operations, and tasks touched by journal entries since that sequence (`evidence.changed_since`), or "nothing recorded since your last verified revision". Intake analysis is not repeated after every edit: observations stay valid while their evidence versions are unchanged (R-57).

## Isolated reviews (R-11)

When the reviewer's persistent session already contains the builder's self-assessment for the component, or `builder.isolated_reviews` is `always`, the review runs in a fresh `isolated_review` session with a reconstructed packet and no changed-since delta. This doubles the context delivered for that review; fidelity wins over token cost by design (design Section 13, question 2).

## Session reuse

Sessions resume by explicit id (`--resume <uuid>` for claude; `codex exec resume <thread_id>`). A failed invocation never advances a session (journal `provider_session.not_advanced`), so a later call never tries to resume a session that was never created. When a provider cannot resume by id (gemini's `session_resume` is a probed capability), every invocation starts a fresh session and the record carries `context_reconstructed: true`; the packet is the context.

## Usage capture (R-19, R-25)

Per invocation the adapter maps only usage fields that are documented and present in the CLI's own output; everything else stays `unknown`, never zero (`cli_common.usage_from`):

| provider | fields mapped | cost |
|---|---|---|
| claude | `usage.input_tokens`, `usage.output_tokens`, `usage.cache_read_input_tokens` | `total_cost_usd` as `estimated` (a client-side estimate per the headless docs) |
| codex | `turn.completed.usage.input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens` | unknown (codex reports no cost) |
| gemini | none (the `stats` object is kept raw; its field names are not documented) | unknown |

Quantities are `measured`, `estimated`, `unknown`, or, for the manual image seat, `not_applicable` (`builder/records.py`). Unmapped raw field names are listed in the invocation's `effective_settings` (`usage_unmapped_fields`) so they can be read, never guessed. The `usage_reporting` preflight tier is `verified` when at least one field was recognised, else `not_verified` with the raw field names seen.

## Cost reporting

`status` prints:

```text
consumption: requests 9 done / 0 in flight; renders 14; cost measured={'kind': 'unknown'} estimated={'kind': 'estimated', 'value': 4.93, 'source': 'tracker'} unknown invocations=4
  limit max_cost_usd=20.0 enforceable=False not enforceable: ...
```

Measured and estimated totals are separate; `unknown invocations` counts calls that reported no cost. `status --json` adds `per_agent` request and retry counts, `max_invocation_cost` per agent, `correction_attempts`, `transport_retries`, `steps_without_progress`, and `external`. Operation counts and render time are diagnostics, never quality metrics.

## Probe spend seeding

Preflight probes do not count toward `max_requests`, but their cost is real. Each probe's cost enters the tracker as external spend (`note_external_cost("preflight_probe", ...)`), the per-agent total is stored on the preflight report as `probe_cost`, and both `start()` and `load_preflight()` seed a new run's tracker from the stored reports, so a later process still counts it against `max_cost_usd`. `status` shows it under `consumption.external`, the GUI under "external (preflight probes)".

## The R-84 comparison harness

`builder/harness.py` measures the same run two ways from its own records and puts the numbers side by side.

Run the fixture with scripted seats (Blender is real; the agents are the built-in fixture screenplay; nothing is spent):

```text
python -m builder harness run "C:\tmp\harness-lamp" [--mock <screenplay.json>] [--max-steps 60] [--json]
```

Measure an existing workflow (a live run's records carry the providers' own token counts):

```text
python -m builder harness report "<wf>" [--json]
```

Both write `harness-report.json` and `harness-report.txt` into the workflow directory and print the text report.

What it measures:

- Packet delivery (what Alloy does): per invocation, the packet's text bytes (`PACKET.md`, `schema.json`) plus the prompt and working-agreement bytes recorded at dispatch, image bytes (references, renders, crops) and their count, other bytes (measurements), the reply bytes, and the provider-measured tokens (`input`, `output`, `cached_input`, `reasoning`) where reported. Bytes are read from the packet manifests and invocation records, so they are measurements.
- Transcript replay (the naive alternative, the way Alloy's chat modes concatenate prior contributions): simulated from the same invocation sequence, with every turn re-delivering every earlier packet text, reply, and image plus its own packet. Nothing is sent to any provider, so its token count is unknown and is never estimated. It also lists every packet whose withheld material (the builder self-assessment, the other seat's verdict) would have been exposed by earlier replies in a shared transcript.
- Preserved under both: unique evidence files, coverage rows, findings closed on verified renders, packets with withheld material, revisions, renders. These are identical by construction because the harness measures the same run twice; only packet delivery honours the withheld material.

What it never claims: no savings percentage is computed; tokens are reported only where a provider measured them (unknown otherwise, never zero); no quality equivalence is asserted. The report ends with these caveats verbatim.

The report's `tokens` section for the fixture run reads `input=unknown, ...; invocations with no reported usage: N`, because scripted mocks report no usage; a `harness report` on a live workflow carries claude's and codex's measured token counts. An example from a real-Blender fixture run (scripted seats, nothing spent) was placed under `docs/reports/phase4/` (`harness-report-real-blender.txt` and `.json`) while these pages were being written.
