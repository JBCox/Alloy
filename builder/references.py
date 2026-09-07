"""Reference intake (spec R-16, R-26, R-27, R-32).

Originals are copied byte-for-byte into ``refs/`` and never modified. Regions live in original pixel
space. Replacing a reference creates a new version and keeps the old record. Composite sheets need an
explicit target region before intake is ready. Generated studies are hypotheses, never evidence.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .ids import sha256_file, utc_now
from .project import Project
from .records import PRECEDENCE, Record

# ``generated`` is reserved for concept-stage imports (addendum R-96a); ``add`` refuses it, ``add_generated`` sets it.
KINDS = ("target", "previous_attempt", "rejected", "hypothesis", "generated")
KNOWN_LABELS = ("front", "side", "rear", "top", "underside", "three-quarter", "detail", "other")
REGION_PURPOSES = ("target_region", "detail_crop")


class References:
    def __init__(self, project: Project):
        self.project = project
        self.store = project.store
        self.dir = project.path("refs")

    # --- intake --------------------------------------------------------------------

    def add(self, source_path: str | Path, *, labels: list[str], kind: str = "target", notes: str = "",
            composite: bool = False, replaces_id: str | None = None, known_dimensions: dict[str, Any] | None = None,
            scale: dict[str, Any] | None = None, pose_notes: str = "", evidence_preference: int | None = None,
            actor: str = "user") -> Record:
        if kind not in KINDS or kind == "generated":
            raise ValueError(f"reference kind {kind!r} must be one of {tuple(k for k in KINDS if k != 'generated')}")
        _check_labels(labels)
        src, width, height, fmt = _open_image(source_path)
        version = 1
        previous: Record | None = None
        if replaces_id:
            previous = self.store.require("reference", replaces_id)
            version = int(previous.data.get("version", 1)) + 1
        rec = Record.new("reference", {})
        dest = self.dir / f"{rec.id}_v{version}{src.suffix.lower()}"
        digest = _copy_unmodified(src, dest)
        rec.data.update({
            "file": str(dest), "sha256": digest, "width": width, "height": height, "format": fmt,
            "labels": list(labels), "kind": kind, "notes": notes, "composite": bool(composite), "version": version,
            "replaces_id": replaces_id, "replaced_by": None, "original_path": str(src),
            "evidence_of_original": kind == "target", "known_dimensions": known_dimensions or {},
            "scale": scale or {}, "pose_notes": pose_notes, "evidence_preference": evidence_preference,
            "added_at": utc_now(),
            # addendum R-94, R-95: owner-supplied references are canon on entry with the highest precedence
            "canon_state": "approved", "precedence": PRECEDENCE["owner_target"], "precedence_label": "owner_target",
            "derived_from": None, "generation_id": None,
        })
        rec = self.store.upsert(rec, actor=actor, event="reference.added", inputs={"source": str(src), "labels": labels})
        self.store.register_file(dest, kind="reference", record_id=rec.id)
        if previous is not None:
            previous.data["replaced_by"] = rec.id
            self.store.upsert(previous, actor=actor, event="reference.replaced", inputs={"by": rec.id})
        return rec

    def add_generated(self, source_path: str | Path, *, generation_id: str, labels: list[str], precedence_label: str,
                      derived_from: str | None, declared: dict[str, Any], part_id: str | None = None, notes: str = "",
                      actor: str = "user") -> Record:
        """Concept-stage import (addendum R-96a, R-97): the file is copied unmodified under ``refs/generated/<request>/``,
        hashed, linked to its generation request, and marked ``candidate``. Vendor and model are declarations."""
        _check_labels(labels)
        if precedence_label not in PRECEDENCE or precedence_label == "owner_target":
            raise ValueError(f"generated references take precedence anchor, turnaround, or study, not {precedence_label!r}")
        src, width, height, fmt = _open_image(source_path)
        rec = Record.new("reference", {})
        dest = self.dir / "generated" / generation_id / f"{rec.id}{src.suffix.lower()}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        digest = _copy_unmodified(src, dest)
        rec.data.update({
            "file": str(dest), "sha256": digest, "width": width, "height": height, "format": fmt,
            "labels": list(labels), "kind": "generated", "notes": notes, "composite": False, "version": 1,
            "replaces_id": None, "replaced_by": None, "original_path": str(src),
            "evidence_of_original": False,          # R-32: a generated image is never evidence of a pre-existing design
            "known_dimensions": {}, "scale": {}, "pose_notes": "", "evidence_preference": None, "added_at": utc_now(),
            "canon_state": "candidate", "precedence": PRECEDENCE[precedence_label], "precedence_label": precedence_label,
            "derived_from": derived_from, "generation_id": generation_id, "part_id": part_id,
            "declared": dict(declared), "verdicts": {}, "approval": None, "rejection": None,
        })
        rec = self.store.upsert(rec, actor=actor, event="reference.imported",
                                inputs={"source": str(src), "generation_id": generation_id, "labels": labels,
                                        "declared": dict(declared)})
        self.store.register_file(dest, kind="reference", record_id=rec.id)
        return rec

    def get(self, reference_id: str) -> Record:
        return self.store.require("reference", reference_id)

    def current(self) -> list[Record]:
        return [r for r in self.store.list("reference") if not r.data.get("replaced_by")]

    def approved(self) -> list[Record]:
        """Canon references (addendum R-94): only these enter intake and evidence packets as references. A record
        without a canon state predates the concept stage and was owner-supplied, so it counts as approved."""
        return [r for r in self.current() if r.data.get("canon_state", "approved") == "approved"]

    def candidates(self) -> list[Record]:
        return [r for r in self.current() if r.data.get("canon_state") == "candidate"]

    # --- regions -------------------------------------------------------------------

    def add_region(self, reference_id: str, name: str, bbox: list[int], *, purpose: str = "target_region",
                   actor: str = "user") -> Record:
        ref = self.get(reference_id)
        if purpose not in REGION_PURPOSES:
            raise ValueError(f"region purpose {purpose!r} must be one of {REGION_PURPOSES}")
        x, y, w, h = _validate_bbox(bbox, ref.data["width"], ref.data["height"])
        rec = Record.new("reference_region", {"reference_id": reference_id, "name": name, "bbox": [x, y, w, h],
                                              "purpose": purpose, "space": "original_pixels", "created_at": utc_now()},
                         parent_id=reference_id)
        return self.store.upsert(rec, actor=actor, event="reference_region.added")

    def regions(self, reference_id: str) -> list[Record]:
        return self.store.list("reference_region", parent_id=reference_id)

    def crop(self, reference_id: str, bbox: list[int], out_path: str | Path) -> dict[str, Any]:
        ref = self.get(reference_id)
        x, y, w, h = _validate_bbox(bbox, ref.data["width"], ref.data["height"])
        src = Path(ref.data["file"])
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(src) as im:
            im.crop((x, y, x + w, y + h)).save(out, format="PNG")
        return {"path": str(out), "width": w, "height": h, "bbox": [x, y, w, h], "reference_id": reference_id,
                "source_sha256": ref.data["sha256"], "space": "original_pixels"}

    def intake_ready(self) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        # owner targets, or approved generated canon (addendum A.1: approved images become the design)
        targets = [r for r in self.approved() if r.data.get("kind") in ("target", "generated")]
        if not targets:
            reasons.append("no approved target reference has been added")
        for t in targets:
            if t.data.get("composite") and not any(r.data.get("purpose") == "target_region" for r in self.regions(t.id)):
                reasons.append(f"composite reference {t.id} needs an explicit target region so unrelated subjects never "
                               "enter the evidence (R-27)")
        return (not reasons), reasons


def _check_labels(labels: Any) -> None:
    if not isinstance(labels, list) or not all(isinstance(x, str) and x for x in labels):
        raise ValueError("labels must be a list of non-empty strings")


def _open_image(source_path: str | Path) -> tuple[Path, int, int, str | None]:
    src = Path(source_path)
    if not src.is_file():
        raise FileNotFoundError(src)
    with Image.open(src) as im:
        return src, im.size[0], im.size[1], im.format


def _copy_unmodified(src: Path, dest: Path) -> str:
    shutil.copyfile(src, dest)
    digest = sha256_file(dest)
    if digest != sha256_file(src):
        dest.unlink(missing_ok=True)
        raise OSError("reference copy hash differs from the original (R-48)")
    return digest


def _validate_bbox(bbox: Any, width: int, height: int) -> tuple[int, int, int, int]:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("bbox must be [x, y, w, h] in original pixels")
    try:
        x, y, w, h = (int(v) for v in bbox)
    except (TypeError, ValueError):
        raise ValueError("bbox values must be integers") from None
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        raise ValueError(f"bbox {bbox} is outside the {width}x{height} image or empty")
    return x, y, w, h


PROBE_COLORS = {"red": (220, 30, 30), "green": (30, 180, 60), "blue": (40, 80, 220), "yellow": (230, 200, 30)}


def make_probe_image(path: str | Path, *, shape: str = "triangle", color: str = "red", number: int = 7,
                     size: tuple[int, int] = (320, 240)) -> dict[str, Any]:
    """R-16 probe: a shape of a known color plus a printed number. Returns the expected report."""
    if shape not in ("triangle", "square", "circle"):
        raise ValueError("shape must be triangle, square, or circle")
    rgb = PROBE_COLORS[color]
    w, h = size
    img = Image.new("RGB", (w, h), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    cx, cy, r = w // 2, h // 2, min(w, h) // 3
    if shape == "triangle":
        draw.polygon([(cx, cy - r), (cx + r, cy + r), (cx - r, cy + r)], fill=rgb)
    elif shape == "square":
        draw.rectangle([cx - r, cy - r, cx + r, cy + r], fill=rgb)
    else:
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=rgb)
    draw.text((w - 60, 8), str(number), fill=(0, 0, 0))
    draw.text((8, h - 20), f"{shape} {color} {number}", fill=(0, 0, 0))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return {"shape": shape, "color": color, "number": int(number)}
