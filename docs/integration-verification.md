# Integration verification — post-t12 full-tree run

2026-08-25 · Task t13. Report only — nothing was fixed or modified. Command:
`python tests/run_all.py` against the tree containing t5 (step profiles +
handoff note), t7 (execution records + severity-graded re-verify), t6/t9
(spawn-lineage UI), t11 (refusalPill UI half), and t12 (delivery_gate engine
half).

## 1. Full suite result

**57 suites, 1417 tests, 0 failed (68.26s).** Every suite PASSed. There are
no failures to report verbatim — the failure log is empty.

Notable per-suite counts (all PASS): test_workstreams 206, test_continuous
128, test_activity 73, test_ui_boot 73, test_ox 69, test_outcome 83,
test_loop 33, test_modes 27, test_free 13, test_scheduler 10,
test_app_headless 46, test_webhook 43, test_skills 40, test_brief 34.

## 2. Per-feature coexistence smoke checks

Beyond the suites, each feature was exercised directly (imports, symbol
existence, functional probes) to prove the four waves coexist rather than
merely not colliding:

| Feature | Check | Result |
|---|---|---|
| **t5 — step profiles** | `relay` imports clean; `STEP_MODEL_KEYS == ('planner','moderator','title')`; `normalize_step_models({'planner':'ox','junk':'x'})` keeps only the valid entry; `helper_spec(['claude'], None, step='title', step_models={'title':'ox'})` → `{'provider': 'ox'}`; `HANDOFF_NOTE_MAX == 600` present | PASS |
| **t5 — handoff note** | `normalize_handoff_note` callable; brief threading covered by test_loop's HandoffNoteTests (passing in full tree) | PASS |
| **t7 — execution records** | `settle_workstream` source stamps `executed_by` and clears `findings`; `gate_commit.__doc__` documents `last_sha` binding via wave_gate | PASS |
| **t7 — severity re-verify** | `grade_findings({'verified': {'missing': ['x']}})` → `[{'severity': 'critical', 'finding': 'never created: x'}]` | PASS |
| **t11/t12 — refusal payload contract** | Engine: `SessionStore.record` whitelist contains BOTH `rejected_to` and `narrowing_failed`. UI: `refusalPill` reads exactly those keys; malformed shapes render nothing (test_ui_boot refusal probes green). The two halves agree on field names — the contract holds end-to-end | PASS |
| **t12 — delivery_gate** | `delivery_gate({'slot_ids':[0,1], '_floor_unavailable':{1}}, 0, 1)` → `'benched after repeated failures'`; workstream radio-silence + human-kind exemptions covered by test_loop DeliveryGateTests (passing) | PASS |
| **t6/t9 — lineage UI intact after t11/t12** | `ui/index.html` still carries `lineageKids`, `.lineage-pop`, ⌥ chip; test_ui_boot 73/73 including lineage + refusal-pill probes together in ONE page boot | PASS |

No import errors anywhere (`import relay` and the ui_boot node harness both
clean). No cross-feature regressions: the suites most likely to collide —
test_loop (t5+t12 tests cohabiting), test_envelopes (envelope rendering),
test_envelopes/test_ui_boot (payload shape), test_continuous (t7 settlement
paths), test_free/test_parallel/test_modes (t12 fan-out changes) — all pass.

## 3. Behavioral notes (not failures)

Two intentional behavior changes landed with this wave and are pinned by
tests; recorded here so nobody mistakes them for regressions later:

1. **Benched seats stop absorbing broadcasts.** A seat parked for the run
   (double-failure, fatal, timeout, or empty-reply skip) now receives a
   `rejected_to` receipt on the sender's row instead of silently
   accumulating an undrained queue. `test_empty_reply_backstop` and
   `test_failed_twice...` expectations were updated by their owner (t12) to
   pin this.
2. **Free-mode parking routes through the shared set**, and both park paths
   now reset `busy[i]` — fixing a pre-existing latent hang where the
   budget-cap stop could never fire after a park (surfaced by t12's
   three-seat test, diagnosed via faulthandler).

## 4. Verdict

All four features coexist. Nothing routed back to any feature owner: zero
failures, zero fixes applied by this task.
