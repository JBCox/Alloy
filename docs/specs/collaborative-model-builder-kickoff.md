# Kickoff prompt: Collaborative Model Builder

Paste everything below the line into a fresh Claude Code session started in `C:\Alloy`.

---

Build the Collaborative Model Builder workflow in this repo (`C:\Alloy`).

The specification is `docs/specs/collaborative-model-builder.md`. Read it completely before doing anything else. It is the source of record: cite its R-numbers in your design doc, tests, and status reports. Its Section 2 lists what I already verified about this codebase and the installed tools on 2026-09-07. Re-verify anything you depend on rather than assuming it still holds.

## Authorization for this session

Allowed:
- Implementing and testing the workflow with disposable fixtures and mock providers.
- Read-only inspection of this repo, including the uncommitted working tree, and of the installed CLIs and Blender.
- Creating the E10 Bearer preset record.

Not allowed:
- Reading, rendering, copying, or modifying anything under `C:\Death Factory`. No remodeling of Bearer.
- Live provider runs that spend money, until I approve a scope and budget in this chat.
- `git commit`, `git stash`, `git checkout --`, `git clean`, reformatting, or changing line endings. Do not delete, rewrite, or "clean up" the uncommitted files (including the stray `nul` file). Add new files; touch existing ones only for the minimal hooks the spec requires (an argparse flag, a GUI menu entry, `requirements.txt`, `alloy.spec`, a config key).

## Priorities

1. Fidelity to the supplied design, reliable execution, editability, honest verification.
2. Token efficiency. It never reduces inspection coverage, evidence, or acceptance criteria, and it never silently selects a weaker model.

## Invariants (spec Sections 3 and 4 expand each)

1. Alloy owns state. No agent prose, transcript, or phrase like "done", "looks good", or "we agree" decides that an edit committed, a finding closed, or a component advanced.
2. Both agents build. No permanent builder/reviewer split; both take ownership and implement.
3. Two agents on the same provider still get isolated sessions through explicit session IDs. Never `--continue`, `--last`, or any "most recent conversation" mechanism.
4. Only Alloy writes the authoritative `.blend`. Agent CLIs run read-only. Every mutation is a bpy operation that Alloy runs in Blender against a staged copy, validates, and promotes to an immutable revision.
5. Builder provider calls are argv-based subprocesses: no `shell=True`, no `{message}` interpolation, no fallback provider or model, and separate provider timeouts, inactivity detection, and Blender deadlines.
6. Only backend renders from exact revisions, with manifests, count as evidence.
7. Every stop has a reason (ready for review, accepted, evidence-limited, stalled, attempt or budget or time limit, paused, cancelled, failed). Nothing is "complete" by default.
8. Existing modes, provider configs, GUI behavior, and the uncommitted working tree keep working unchanged.

## How this session runs

Phase 0, inspect and design, no production code:
1. Verify spec Section 2 against the code and tools. Note any drift.
2. Write `docs/plans/2026-09-07-collaborative-model-builder-design.md` covering: module layout; record schemas and the journal; the four state machines; the Blender runner contract (staging, validate, promote); the provider adapter contract and preflight tiers; evidence packet formats; the test plan mapped to R-numbers; decisions and tradeoffs; the phase plan.
3. Stop and show me the design. Ask only the few questions whose answers would change construction. Make routine decisions yourself and record them.

Phase 1, after I approve the design: the vertical slice from spec Section 7. Engine, records, state machines, Blender runner, render manifests, the fixture project with its controlled defects, mock adapters where Agent A builds and Agent B finds and corrects a defect, handoff, checkpoint restore, reopen, the CLI runner, and the deterministic and real-Blender tests. Then stop and report.

Phases 2 to 4 (real adapters and preflight, the GUI view, recovery and limits and docs and packaging) are separate sessions unless I say otherwise.

## Working rules

- Test first: write the failing deterministic test, then the code. Real-Blender tests are marked and skip with a stated reason when Blender is absent.
- Windows is the target. Paths with spaces and Unicode (`C:\Program Files\Blender Foundation\Blender 5.2\blender.exe`, `C:\Death Factory`), argv lists, UTF-8 everywhere, process-tree kills.
- Never invent flags, model IDs, reasoning levels, usage fields, or prices. Use `--help`, the CLIs' own JSON output, and official docs. Otherwise say "unknown".
- Stop and report when a phase is done, when a required capability is missing, when a limit is hit, or when your context is about 70% used. Never lower a standard to declare success.

## Report format at every stop

1. What was built: files and entry points.
2. Architecture summary and key tradeoffs, kept short.
3. Launch instructions.
4. Test results by layer (deterministic, real Blender, CLI/GUI interaction, live provider) with the actual command output, plus an explicit list of what was not verified and why.
5. Fixture artifacts and screenshots where they exist.
6. Remaining limitations and open questions.
7. Confirmation that nothing under `C:\Death Factory` was read or modified, shown as unchanged size and modification time, checked without opening the files.
