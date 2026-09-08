# Unattended operation and resource limits

Purpose: the difference between attended and unattended runs, every configurable limit and what enforces it, stall detection, the stop reasons, how to edit limits while a run exists, and the caveat that monetary caps act after the spend.

## Attended versus unattended (D8, R-76)

```text
python -m builder start "<wf>"                 # attended (default, or builder.attended)
python -m builder start "<wf>" --unattended    # advance within limits; components stay unaccepted
```

Both modes do whole-model planning and the blockout before the first detailed component (intake, brief, plan, then build). At the gate an attended run stops in `waiting_for_user` with stop reason `ready_for_user_review` and waits for `accept` or `reopen`; an unattended run pauses with the same stop reason and the note "left unaccepted for user review". "Ready for user review" is never "accepted" in either mode. The GUI's "attended" checkbox on the Run tab carries the same rule and its help text.

Independent of the mode, a run also stops at `missing_evidence` (an evidence gap after reassessment) and at every limit below.

## The limits (R-85, R-86)

Configured under `builder.limits` (defaults in `builder/config.py::DEFAULT_LIMITS`); zero means unlimited.

| limit | default | what it counts | enforced by |
|---|---|---|---|
| `wall_clock_minutes` | 0 | elapsed time since the run started, restored across processes | Alloy, before every dispatch |
| `max_cost_usd` | 0 | measured plus estimated provider cost, including seeded preflight probe spend | Alloy stops dispatch when the total reaches the cap (labelled an estimate when the total is estimated); claude also receives the remaining budget as `--max-budget-usd` per invocation |
| `max_requests` | 0 | LLM invocations completed plus in flight (probes excluded) | Alloy |
| `max_renders` | 0 | renders completed plus in flight | Alloy |
| `attempts_per_finding` | 2 | correction attempts per finding | Alloy; zero disables the check |
| `stall_steps` | 12 | consecutive loop steps without evidence-supported progress | Alloy; zero disables the check |

In-flight work is counted at dispatch, so nothing new is dispatched once a limit is reached (R-86). Limits are checked in `LimitTracker.can_dispatch` before every provider call and every render, in this order: wall clock, stall, request count, render count, cost totals, then the largest-call check described below.

### Enforceable versus estimated

`status` and `limits` print every limit with `enforceable=`. Every limit except `max_cost_usd` is marked enforceable. `max_cost_usd` is enforceable only when every configured provider both reports a cost and enforces a per-invocation cap: claude declares `cost_cap: yes` (`--max-budget-usd`), codex and gemini declare `no`. With the default seats (claude and codex) the line therefore reads:

```text
  limit max_cost_usd=20.0 enforceable=False not enforceable: at least one provider reports no cost and enforces no per-invocation cap; N invocation(s) so far reported unknown cost
```

Unknown cost is recorded as unknown, never as zero, and never satisfies a cap (`records.UNKNOWN` refuses arithmetic). Claude's `total_cost_usd` is a client-side estimate per its documentation and is tracked as `estimated`, separate from `measured`.

### Provider timeouts on long calls

`builder.provider_timeouts.response` and `inactivity` (default 900 and 180 s) are read when the engine process starts and apply to every provider call. A codex build task at `xhigh` reasoning on a 26-part inventory ran past 900 s on 2026-09-08 (killed at 902 s while still composing its operation; the retry completed in 18 minutes). Set both values with headroom for the longest call you expect: claude's `--output-format json` prints nothing until the reply is complete, so `inactivity` must never be shorter than `response` for claude. A timed-out call is journaled as `invocation.failed`, its task is blocked as interrupted, the run pauses with `execution_failure`, and `resume` retries the task with the same seat and the interruption named in the packet; the new timeouts apply from that `resume` (an in-flight call keeps the values its process started with). The wall-clock limit still bounds the whole run.

### The budget-after-spend caveat

A cap applied by the provider stops a call only after the spend. Recorded live (design 12b item 16): with 0.06 USD of a 5 USD cap remaining, claude spent 2.07 USD before its cap ended the turn, and the estimated total overran the ceiling by about 2 USD. Two consequences in code:

- the tracker refuses to dispatch when the remaining budget is below the largest single-invocation cost that agent has reported so far (`max_invocation_cost` in `status`): "remaining budget X USD is below the largest cost one invocation of this agent has reported (Y USD); a cap applied by the provider only stops a call after the spend, so nothing more is dispatched";
- a claude result stopped by its own cap (`is_error`, null result, reported cost at or above the cap) is the outcome `budget_exhausted` and pauses the run with stop reason `budget_limit`, never `execution_failure`.

Neither mechanism can prevent the first oversized call; set `max_cost_usd` with headroom for one full call per agent (intake and brief calls were 2 to 3 USD each by claude's estimate on the fixture).

## What counts as progress (stall detection; R-85, R-88)

After every loop step the engine computes a progress key (`Engine._progress_key`) and hands it to the tracker. The key changes only when one of these changes: the number of valid observations, briefs, whether the plan is done, build tasks done, review tasks done, findings recorded, findings closed or waived, findings reassessed, components ready for user review, accepted components. Renders and committed revisions alone are not in the key: a correction that never verifies changes nothing, and "increasing detail is not progress". After `stall_steps` consecutive steps with an unchanged key the run pauses with stop reason `stalled`:

```text
N consecutive step(s) without evidence-supported progress (no finding closed or waived, no stage completed) reached stall_steps=12; increasing detail is not progress (R-88)
```

`steps_without_progress` and `last_progress_key` are persisted on the run record so a restart continues the count.

## Stop reasons (R-4, R-89)

`user_pause`, `user_cancel`, `missing_evidence`, `stalled`, `attempt_limit`, `budget_limit` (request, render, or cost caps), `time_limit`, `execution_failure`, `ready_for_user_review`. Every stop appends a note (`notes` in `status`, `last note:` in the printed status) and persists consumption; `status` always lists remaining discrepancies and uncertainties. A limit stop leaves the run `paused`; raise the limit and `resume`.

## Editing limits

Show the limits in force and consumption:

```text
python -m builder limits "<wf>"
python -m builder limits "<wf>" --json
```

Change them (`--set` is repeatable; names must be one of the six above; values are numbers, never negative; a rejected batch changes nothing):

```text
python -m builder limits "<wf>" --set max_requests=30 --set max_cost_usd=20 --set stall_steps=0
```

- When no run exists, or the run is stopped, the change applies at once (`applied {...}`), is mirrored on the run record so a later process continues with it, and is journaled (`run.limits_changed` or `limits.changed`).
- When a run is active in another process (`running`, `waiting_for_provider`, `rendering`, `recovering`), the change is queued as a `limits` control request and applied at the engine's next safe boundary; a rejected change is journaled as `limits.rejected`.

In the GUI, the Limits card on the Run tab shows one entry per limit (including `stall_steps`); "Apply limits" submits only the values you changed as a control request, which the worker applies immediately when nothing is consuming controls or which the running engine takes at its boundary. The "Consumption and limits" card below it shows elapsed time, requests and renders (completed and in flight), measured and estimated cost, invocations with unknown cost, each limit with its enforceability note, external (probe) spend, per-agent counts, findings at the attempt limit, contributions, transport retries, and correction attempts.

Changing `limits` in the settings file affects the next `start`; an existing run keeps the limits stored on its run record.

## Live smoke test caps (for reference)

`tests/test_live_smoke.py` is opt-in with `ALLOY_LIVE=1` and reads `ALLOY_LIVE_MAX_REQUESTS` (16), `ALLOY_LIVE_MAX_RENDERS` (60), `ALLOY_LIVE_MAX_COST_USD` (5), `ALLOY_LIVE_WALL_MINUTES` (60), and `ALLOY_LIVE_REPORT_DIR`. Every other test refuses to spawn a real provider CLI unless the argv is only `--help` or `--version` (`tests/conftest.py`).
