"""Revision 0 for a project created with ``new`` (spec R-40, R-45, R-48, R-93; design 12e item 15c).

Creating a project never opens a source (R-93). Registering one is the explicit step here:

* ``register_source``: the owner's ``.blend`` is validated in separate Blender processes (identities enumerated,
  then reopened and checked for duplicates, orphans, non-finite transforms, missing external assets), copied
  unmodified into ``revisions/`` as an immutable revision with its identity map and asset dependencies, and
  journaled. Geometry without ``alloy_id`` is recorded on the revision, never tagged in place: the source file
  is only ever read.
* ``register_empty``: an empty scene (one tagged collection, no objects) is built through the runner, validated
  the same way, and registered as revision 0.

Both refuse when a revision already exists: revisions are immutable and revision 0 is registered once.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .ids import new_id, sha256_file, utc_now
from .operations import Operations, _asset_hashes
from .ownership import OwnershipManager
from .project import Project
from .records import Record

EMPTY_COLLECTION_ID = "col_model"


class SourceError(Exception):
    pass


def _existing_revision(project: Project) -> Record | None:
    revs = project.store.list("revision")
    return revs[-1] if revs else None


def _validate(project: Project, runner: Any, blend: Path, *, op_id: str, actor: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Two separate Blender processes: enumerate identities, then reopen and validate. Never a save."""
    logs = project.path("logs", op_id)
    ids = runner.identities(blend, op_id=f"{op_id}_identities", work_dir=logs / "identities")
    if not ids.ok:
        project.store.append_journal(actor=actor, event="source.rejected",
                                     inputs={"file": str(blend), "stage": "identities", "error": ids.error})
        raise SourceError(f"identity enumeration failed for {blend}: {ids.error}")
    identity_map = list(ids.data.get("identity_map") or [])
    vres = runner.validate(blend, {"base_identity_map": identity_map, "expect_present": [], "expect_absent": [],
                                   "allow_unmapped": True}, op_id=f"{op_id}_validate", work_dir=logs / "validate")
    if not vres.ok:
        project.store.append_journal(actor=actor, event="source.rejected",
                                     inputs={"file": str(blend), "stage": "validate", "error": vres.error,
                                             "validation": dict(vres.data)})
        raise SourceError(f"validation failed for {blend}: {vres.error}")
    validation = dict(vres.data)
    project.store.append_journal(actor=actor, event="source.validated",
                                 inputs={"file": str(blend), "identities": len(identity_map),
                                         "unmapped": list(validation.get("unmapped") or []),
                                         "object_count": validation.get("object_count"),
                                         "external_assets": len(validation.get("external_assets") or [])})
    return identity_map, validation


def _register(project: Project, runner: Any, blend: Path, *, actor: str, identity_map: list[dict[str, Any]],
              validation: dict[str, Any], note: str, source: dict[str, Any], move: bool) -> Record:
    ops = Operations(project, runner, OwnershipManager(project.store))
    expected = sha256_file(blend)
    rev = ops.register_revision(blend, parent_revision_id=None, created_by_op_id=None, actor=actor,
                                identity_map=identity_map, asset_dependencies=_asset_hashes(validation.get("external_assets") or []),
                                note=note, move=move, extra={"source": {**source, "sha256": expected}})
    if rev.data["sha256"] != expected:
        raise SourceError(f"the copy in revisions/ ({rev.data['sha256']}) differs from the source ({expected}); disk problem (R-48)")
    for e in identity_map:
        if e.get("type") == "OBJECT" and e.get("alloy_kind") in ("part", "instance") and project.store.get("part", e["alloy_id"]) is None:
            rec = Record.new("part", {"name": e.get("name"), "blender_ids": [e["alloy_id"]], "alloy_kind": e.get("alloy_kind"),
                                      "instance_of": e.get("instance_of")}, id=e["alloy_id"], parent_id=e.get("parent_alloy_id"))
            project.store.upsert(rec, actor=actor, event="part.created", inputs={"from": "source identity map"})
    project.store.append_journal(actor=actor, event="source.registered",
                                 inputs={"revision_id": rev.id, "kind": source.get("kind"), "path": source.get("path"),
                                         "sha256": expected, "identities": len(identity_map),
                                         "unmapped": list(source.get("unmapped_geometry") or [])})
    return rev


def register_source(project: Project, runner: Any, source_file: str | Path, *, actor: str = "user", note: str = "") -> Record:
    """Validate the owner's ``.blend`` in separate Blender processes and register it, unmodified, as revision 0."""
    src = Path(source_file)
    existing = _existing_revision(project)
    if existing is not None:
        raise SourceError(f"revision 0 already exists ({existing.id}); revisions are immutable and a source is registered once (R-40)")
    if not src.is_file():
        raise SourceError(f"source file not found: {src}")
    if src.suffix.lower() != ".blend":
        raise SourceError(f"source must be a .blend file, got {src.name}")
    st = src.stat()
    op_id = new_id("src")
    identity_map, validation = _validate(project, runner, src, op_id=op_id, actor=actor)
    unmapped = list(validation.get("unmapped") or [])
    source = {"kind": "owner_source", "path": str(src), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
              "registered_at": utc_now(), "unmapped_geometry": unmapped, "object_count": validation.get("object_count"),
              "reopened": validation.get("reopened"), "external_assets": validation.get("external_assets") or []}
    text = f"registered owner source {src.name} as revision 0 (validated: identities, reopen)"
    if unmapped:
        text += f"; {len(unmapped)} object(s) without alloy_id recorded, not tagged"
    if note:
        text += f"; {note}"
    return _register(project, runner, src, actor=actor, identity_map=identity_map, validation=validation, note=text,
                     source=source, move=False)


def empty_scene_spec(name: str = "Model") -> dict[str, Any]:
    return {"collections": [{"alloy_id": EMPTY_COLLECTION_ID, "name": name}], "objects": [], "features": []}


def register_empty(project: Project, runner: Any, *, actor: str = "user", note: str = "") -> Record:
    """Build an empty scene through the runner, validate it, and register it as revision 0."""
    existing = _existing_revision(project)
    if existing is not None:
        raise SourceError(f"revision 0 already exists ({existing.id}); revisions are immutable and a source is registered once (R-40)")
    op_id = new_id("src")
    stage_dir = project.path("staging", op_id)
    stage_dir.mkdir(parents=True, exist_ok=True)
    blend = stage_dir / "empty.blend"
    name = str(project.record.data.get("asset_name") or project.record.data.get("name") or "Model")
    try:
        res = runner.build_scene(blend, empty_scene_spec(name), op_id=f"{op_id}_build", work_dir=project.path("logs", op_id, "build"))
        if not res.ok or not blend.is_file():
            project.store.append_journal(actor=actor, event="source.rejected",
                                         inputs={"stage": "build_scene", "error": res.error, "kind": "empty_scene"})
            raise SourceError(f"building the empty scene failed: {res.error}")
        identity_map, validation = _validate(project, runner, blend, op_id=op_id, actor=actor)
        source = {"kind": "empty_scene", "path": None, "registered_at": utc_now(), "unmapped_geometry": [],
                  "object_count": validation.get("object_count"), "reopened": validation.get("reopened"),
                  "collection_id": EMPTY_COLLECTION_ID, "external_assets": []}
        text = f"empty scene built through the runner as revision 0 (one tagged collection {EMPTY_COLLECTION_ID}, no objects)"
        if note:
            text += f"; {note}"
        return _register(project, runner, blend, actor=actor, identity_map=identity_map, validation=validation, note=text,
                         source=source, move=True)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)
