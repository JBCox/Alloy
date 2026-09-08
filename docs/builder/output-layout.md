# Output layout and reopening the result

Purpose: what lives in a workflow directory, which files are immutable, how to open a revision in Blender, and where the harness reports go.

## The workflow directory

Created by `new`, `preset create`, `fixture create`, or `harness run` (`builder/project.py::LAYOUT`, plus directories the engine and concept stage add on demand):

| path | contents |
|---|---|
| `project.json` | small manifest (project id, name, asset, schema and builder versions) so the directory is recognised without opening the database |
| `builder.sqlite3` | the authoritative records (`records`, `record_history`), the append-only `journal`, the `files` registry (path, sha256, size, kind), `control_requests`, and `meta` with `schema_version` (currently `1`); WAL mode (D5) |
| `refs/` | reference originals copied byte-for-byte as `<ref_id>_v<version><ext>`; `refs/generated/<gen_id>/` for concept-stage imports |
| `revisions/` | immutable committed `.blend` revisions, `rev_<id>.blend` |
| `renders/` | backend renders, `rnd_<id>.png`; their manifests are in the render records |
| `staging/` | per-operation working copies (`<op_id>/base.blend`, `out.blend`), removed after a commit; `staging/_quarantine/<op_id>/` keeps uncertain or interrupted outputs for diagnosis and is never cleaned automatically |
| `checkpoints/<ck_id>/` | `model.blend`, `assets/`, `script.py` when applicable, `manifest.json` |
| `logs/` | stdout and stderr of every subprocess (`<op_id>/`, `<rnd_id>/`, `meas_<id>/measurement.json`, `smoke/`, `version.*.log`, `engines.*.log`) |
| `ops/<op_id>/script.py` | the agent's operation script exactly as received (hashed on the operation record) |
| `agents/<label>/work/` | each seat's scratch directory (the CLI's working directory); `agents/<label>/probes/` holds preflight probe inputs |
| `packets/<agent_id>/<packet_id>/` | evidence packets: `PACKET.md`, `packet.json`, `schema.json`, `evidence/`, and `invocation/` with the per-attempt stdout/stderr logs and, for codex, `output_schema.json` and `last_message.txt` |
| `concept/requests/<gen_id>/` | `PROMPT.md`, `attach/` (copies of the conditioning images), `manifest.json` |
| `concept/imports/` | the default import directory checked by the image-seat preflight (`builder.concept.import_dir`) |
| `harness-report.json`, `harness-report.txt` | written by `harness run` and `harness report` |

The design lists `agents/<agent_id>/packets/` (Section 7.1); the code writes packets under `packets/<agent_id>/`.

## Revision 0: `source register` and `source empty`

A project created with `new` has no revision until one is registered explicitly (creating or opening the project never opens a source, R-93). Both verbs need Blender and refuse once a revision exists (revisions are immutable, R-40):

```text
python -m builder source register "<wf>" "C:\art\model.blend" [--note "owner's v4"]
python -m builder source empty "<wf>"
```

- `source register`: the file is validated in two separate Blender processes (`identities.py` enumerates every `alloy_id`; `validate.py` reopens the file and checks duplicates, orphans, non-finite transforms, and missing external assets; neither saves), then copied byte for byte to `revisions/rev_<id>.blend`, hashed, set read-only, registered in the `files` table, and journaled (`source.validated`, `source.registered`, `revision.created`; a refusal journals `source.rejected`). The revision record carries `identity_map`, `asset_dependencies` (hashes of external images and libraries), and `source` (path, size, modification time, sha256, `unmapped_geometry`, object count). Geometry without an `alloy_id` is recorded, never tagged in place: the source file's bytes and modification time are unchanged and no `.blend1` is written beside it. Tagged objects and instances become `part` records; collections and materials do not.
- `source empty`: `build_scene.py` builds a scene with one tagged collection (`col_model`, named after the asset) and no objects under `staging/`, validates it the same way, and moves it into `revisions/`; the staging directory is removed. The part inventory stays empty until the construction plan fills it.

The window offers both on the Project tab ("Register source .blend…", "Register empty scene"); `status` prints `revision_id` for the latest revision. The engine's build stage renders and measures revision 0 as the whole-model blockout base (R-33); an empty scene measures as empty and is framed with the default camera until parts exist.

## What is immutable

- `revisions/*.blend`: written once by `Operations._finalize_revision`, then set read-only (`os.chmod(dest, stat.S_IREAD)`), hashed, and registered in the `files` table. Every later operation re-verifies the base revision's hash before staging; a mismatch is journaled as `revision.external_modification` and the operation is rejected until the change is reconciled explicitly (R-45). Revisions are never modified afterwards (R-40).
- `refs/*`: originals copied unmodified and hashed (R-27).
- The `journal` table: `BEFORE UPDATE` and `BEFORE DELETE` triggers refuse changes (design Section 3.1); `store.rebuild_from_journal()` reproduces the records table from it (R-3).
- Superseded record versions stay in `record_history`; superseded acceptances stay visible (R-6).

Renders are registered files too; a render whose file no longer matches its manifest is `STALE (hash_mismatch)`.

## Reopening a revision in Blender

Every revision file is read-only. To inspect or continue outside Alloy, copy it first; do not edit it in place:

```text
python -m builder status "<wf>"
copy "<wf>\revisions\rev_<id>.blend" "C:\work\lamp-rev.blend"
"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe" "C:\work\lamp-rev.blend"
```

`status` prints the current revision id per component (`revision=rev_...`); `checkpoint list` prints each checkpoint's revision. Objects, collections, and materials carry the custom properties `alloy_id` and `alloy_kind` (`part`, `instance`, `material`, `collection`, `feature`), which are the identities the inventory uses; object names are not identity (R-37). A revision saved with `relative_remap` records its external asset dependencies with hashes on the revision record; a checkpoint copies those assets under `checkpoints/<ck_id>/assets/`.

Editing a revision file in place (after clearing the read-only attribute) is detected as an external modification and blocks further operations on that base; adopting the change means registering it as a new revision, which this build exposes only through the engine API, not as a CLI verb (`source register` registers revision 0 only).

## Harness report files

`harness-report.json` holds the full comparison (`packet_delivery`, `transcript_replay`, `preserved`, `caveats`, per-turn rows); `harness-report.txt` is the printed table. Both are overwritten by the next `harness report` on the same workflow. See [token-efficiency-and-usage.md](token-efficiency-and-usage.md).

## Reading the records directly

`journal --tail N` prints the last entries (sequence, time, actor, event, record kind and id, state transition). `status --json` dumps the run, components, consumption, agents, findings, uncertainties, questions for the user, notes, and checkpoints. `concept show <id>` prints one concept record. The SQLite file can be opened read-only with any SQLite tool while no engine is running; the schema is documented in design Section 3.1.
