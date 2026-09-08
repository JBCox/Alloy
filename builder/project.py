"""Workflow project directory: layout, manifest, create/open, and presets (spec D5, R-93).

Layout under the workflow directory::

    builder.sqlite3   records, history, journal, file registry, control requests
    project.json      small manifest so a directory can be recognised without opening the database
    refs/             reference originals (never modified) and their crops
    revisions/        immutable committed .blend revisions
    renders/          backend renders with manifests
    staging/          per-operation working copies (and staging/_quarantine for uncertain outputs)
    checkpoints/      checkpoint manifests and owned assets
    logs/             stdout/stderr of every subprocess
    ops/              agent operation scripts and args
    agents/           per-agent scratch directories (the CLI working directory) and packets
    packets/          evidence packets

Creating or opening a project, with or without a preset, never reads a reference image or opens a
source .blend. Intake is a separate explicit step.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import __version__
from .config import BuilderConfig, default_workflow_dir, slugify
from .ids import utc_now
from .records import Record
from .store import SCHEMA_VERSION, Store

LAYOUT = ("refs", "revisions", "renders", "staging", "checkpoints", "logs", "ops", "agents", "packets")
MANIFEST = "project.json"
DB_NAME = "builder.sqlite3"


class Project:
    def __init__(self, workflow_dir: Path, store: Store, record: Record):
        self.workflow_dir = workflow_dir
        self.store = store
        self.record = record

    # --- lifecycle ----------------------------------------------------------------

    @staticmethod
    def exists(workflow_dir: str | Path) -> bool:
        return (Path(workflow_dir) / MANIFEST).is_file()

    @classmethod
    def create(cls, workflow_dir: str | Path, *, name: str, asset_name: str,
               source_project_dir: str | Path | None = None, preset_id: str | None = None,
               extra: dict[str, Any] | None = None, actor: str = "user") -> "Project":
        wf = Path(workflow_dir)
        if cls.exists(wf) or (wf / DB_NAME).exists():
            raise FileExistsError(f"a workflow project already exists at {wf}")
        wf.mkdir(parents=True, exist_ok=True)
        for sub in LAYOUT:
            (wf / sub).mkdir(exist_ok=True)
        store = Store(wf / DB_NAME).open()
        data: dict[str, Any] = {
            "name": name, "asset_name": asset_name,
            "source_project_dir": None if source_project_dir is None else str(source_project_dir),
            "preset_id": preset_id, "created_at": utc_now(), "builder_version": __version__,
        }
        data.update(extra or {})
        record = store.upsert(Record.new("project", data), actor=actor, event="project.created",
                              inputs={"workflow_dir": str(wf)})
        manifest = {"project_id": record.id, "name": name, "asset_name": asset_name,
                    "schema_version": SCHEMA_VERSION, "builder_version": __version__, "created_at": data["created_at"]}
        with open(wf / MANIFEST, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        return cls(wf, store, record)

    @classmethod
    def open(cls, workflow_dir: str | Path) -> "Project":
        wf = Path(workflow_dir)
        if not cls.exists(wf):
            raise FileNotFoundError(f"no workflow project at {wf} (missing {MANIFEST})")
        with open(wf / MANIFEST, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        store = Store(wf / DB_NAME).open()
        try:
            record = store.require("project", manifest["project_id"])
        except Exception:
            store.close()
            raise
        return cls(wf, store, record)

    @classmethod
    def create_from_preset(cls, preset_id: str, preset: dict[str, Any], *, workflow_dir: str | Path | None = None,
                           config: BuilderConfig | None = None) -> "Project":
        """Record a preset's intake data without reading any of the files it names (R-93)."""
        cfg = config or BuilderConfig.from_dict({})
        name = str(preset.get("name") or preset_id)
        asset = str(preset.get("asset") or name)
        project_dir = preset.get("project_dir")
        wf = Path(workflow_dir) if workflow_dir else (
            Path(preset["workflow_dir"]) if preset.get("workflow_dir")
            else default_workflow_dir(project_dir, slugify(preset_id), cfg))
        region = preset.get("target_region") if isinstance(preset.get("target_region"), dict) else {}
        extra: dict[str, Any] = {
            "target_reference": {
                "path": _opt_str(preset.get("target_reference")),
                "region": {"name": region.get("name"), "bbox": region.get("bbox")},
            },
            "existing_source": {
                "path": _opt_str(preset.get("existing_source")),
                "trust": _opt_str(preset.get("existing_source_trust")),
            },
            "first_component": _opt_str(preset.get("first_component")),
            "rejected_references": [str(p) for p in (preset.get("rejected_references") or [])],
            "pending_intake": True,
            "status_note": "preset created; no reference was read and no source file was opened (R-93). "
                           "Run intake to register references; the target region is chosen by the user, never guessed.",
        }
        return cls.create(wf, name=name, asset_name=asset, source_project_dir=project_dir, preset_id=preset_id,
                          extra=extra)

    # --- helpers ------------------------------------------------------------------

    def path(self, *parts: str) -> Path:
        return self.workflow_dir.joinpath(*parts)

    def update(self, *, actor: str, event: str, **changes: Any) -> Record:
        self.record.data.update(changes)
        self.record = self.store.upsert(self.record, actor=actor, event=event, inputs=dict(changes))
        return self.record

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Project":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)
