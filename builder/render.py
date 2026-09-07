"""Views, render manifests, cache keys, and staleness (spec R-60 to R-65, R-81).

Only the Blender backend produces renders; this module decides what a render depends on and whether
an existing render is still evidence for the current revision.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ids import hash_json, sha256_file, utc_now
from .records import Record

ENGINES = {"clay": "BLENDER_WORKBENCH", "material": "BLENDER_EEVEE"}
MANIFEST_VERSION = 1
SENSOR_WIDTH_MM = 36.0

# Standard view set (R-61): component-framed views for review plus whole-model context views.
# Cameras are never fixed presets; they are framed from the measured bounding box (R-60, R-65).
STANDARD_VIEW_DEFS: list[dict[str, str]] = [
    {"name": "front", "mode": "clay", "direction": "front", "subject": "component"},
    {"name": "side", "mode": "clay", "direction": "side", "subject": "component"},
    {"name": "three_quarter", "mode": "clay", "direction": "three_quarter", "subject": "component"},
    {"name": "three_quarter_material", "mode": "material", "direction": "three_quarter", "subject": "component"},
    {"name": "whole_front", "mode": "clay", "direction": "front", "subject": "assembly"},
    {"name": "whole_three_quarter", "mode": "clay", "direction": "three_quarter", "subject": "assembly"},
]
# Per-part close-ups (R-61 component close-ups, R-65 adequate resolution): one clay view per part of the
# component, framed tight on that part's measured bounding box so small elements fill the frame.
CLOSEUP_PREFIX = "closeup:"
CLOSEUP_DIRECTION = "three_quarter"
CLOSEUP_MARGIN = 1.35          # a little wider than the component frame so neighbours stay visible for context
OVERVIEW_VIEWS_FOR_CROPS = ("front", "side", "three_quarter")
CROP_PAD = 0.15                # crop padding as a fraction of the projected rectangle
CROP_MAX_FRACTION = 0.6        # no crop when the part already fills most of the overview render


def closeup_view_def(part_id: str) -> dict[str, str]:
    return {"name": f"{CLOSEUP_PREFIX}{part_id}", "mode": "clay", "direction": CLOSEUP_DIRECTION, "subject": "part",
            "part_id": part_id}


def part_of_closeup(view_name: str) -> str | None:
    return view_name[len(CLOSEUP_PREFIX):] if view_name.startswith(CLOSEUP_PREFIX) else None


DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "front": (0.0, -1.0, 0.0), "rear": (0.0, 1.0, 0.0), "side": (1.0, 0.0, 0.0), "side_left": (-1.0, 0.0, 0.0),
    "top": (0.0, 0.0, 1.0), "underside": (0.0, 0.0, -1.0),
    "three_quarter": (0.62, -0.62, 0.48), "three_quarter_rear": (-0.62, 0.62, 0.48),
}


def bbox_union(bboxes: list[dict[str, Any]]) -> tuple[list[float], list[float]]:
    """Center and size of the axis-aligned union of ``{"min": [...], "max": [...]}`` boxes."""
    boxes = [b for b in bboxes if b and "min" in b and "max" in b]
    if not boxes:
        raise ValueError("no bounding boxes to frame")
    mins = [min(float(b["min"][i]) for b in boxes) for i in range(3)]
    maxs = [max(float(b["max"][i]) for b in boxes) for i in range(3)]
    return [(mins[i] + maxs[i]) / 2 for i in range(3)], [maxs[i] - mins[i] for i in range(3)]


def look_at_euler(cam_pos: list[float], target: list[float]) -> list[float]:
    """XYZ Euler (ry = 0) that points a Blender camera at ``target``."""
    import math

    v = [target[i] - cam_pos[i] for i in range(3)]
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    v = [c / n for c in v]
    rx = math.acos(max(-1.0, min(1.0, -v[2])))
    s = math.sin(rx)
    rz = 0.0 if s < 1e-9 else math.atan2(-v[0], v[1])
    return [rx, 0.0, rz]


def frame_camera(center: list[float], size: list[float], direction: str, *, lens: float = 50.0,
                 aspect: tuple[int, int] = (4, 3), margin: float = 1.15, min_distance: float = 0.5) -> dict[str, Any]:
    """Perspective camera along ``direction`` whose vertical field of view contains the bounding sphere."""
    import math

    if direction not in DIRECTIONS:
        raise ValueError(f"unknown view direction {direction!r}; expected one of {sorted(DIRECTIONS)}")
    d = DIRECTIONS[direction]
    n = math.sqrt(sum(c * c for c in d))
    d = [c / n for c in d]
    radius = 0.5 * math.sqrt(sum(float(s) * float(s) for s in size))
    if radius < 1e-6:
        radius = 0.5
    tan_half_v = (SENSOR_WIDTH_MM / 2.0) / lens * min(1.0, aspect[1] / aspect[0])
    half_v = math.atan(tan_half_v)
    distance = max(min_distance, radius / tan_half_v * margin)
    location = [float(center[i]) + d[i] * distance for i in range(3)]
    return {
        "location": location, "rotation_euler": look_at_euler(location, [float(c) for c in center]), "lens": lens,
        "type": "PERSP", "clip_start": 0.01, "clip_end": max(1000.0, distance * 10.0),
        "framing": {"center": [float(c) for c in center], "size": [float(s) for s in size], "fit_radius": radius,
                    "distance": distance, "direction": direction, "margin": margin, "half_vertical_fov_rad": half_v,
                    "aspect": list(aspect)},
    }


def _rotation_xyz(rx: float, ry: float, rz: float) -> list[list[float]]:
    """Blender XYZ Euler: R = Rz @ Ry @ Rx (X applied first)."""
    import math

    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    rxm = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    rym = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    rzm = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]

    def mul(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    return mul(rzm, mul(rym, rxm))


def _half_tangents(camera: dict[str, Any], resolution: tuple[int, int]) -> tuple[float, float]:
    """Half field-of-view tangents; Blender's AUTO sensor fit assigns the sensor width to the larger image side."""
    w, h = int(resolution[0]), int(resolution[1])
    lens = float(camera.get("lens", 50.0))
    sensor = float(camera.get("sensor_width", SENSOR_WIDTH_MM))
    tan_major = (sensor / 2.0) / lens
    if w >= h:
        return tan_major, tan_major * h / w
    return tan_major * w / h, tan_major


def project_point(camera: dict[str, Any], point: list[float], resolution: tuple[int, int]) -> tuple[float, float, float] | None:
    """Pixel position (x right, y down) and depth of a world point through a perspective camera record
    (``location``, ``rotation_euler``, ``lens``). None when the point is not in front of the camera."""
    loc = [float(c) for c in camera.get("location", (0.0, 0.0, 0.0))]
    rot = [float(c) for c in camera.get("rotation_euler", (0.0, 0.0, 0.0))]
    r = _rotation_xyz(*rot)
    v = [float(point[i]) - loc[i] for i in range(3)]
    d = [sum(r[k][i] * v[k] for k in range(3)) for i in range(3)]     # R^T v: world -> camera space
    if d[2] >= -1e-9:
        return None
    depth = -d[2]
    tan_h, tan_v = _half_tangents(camera, resolution)
    ndc_x = (d[0] / depth) / tan_h
    ndc_y = (d[1] / depth) / tan_v
    w, h = int(resolution[0]), int(resolution[1])
    return (ndc_x + 1.0) / 2.0 * w, (1.0 - ndc_y) / 2.0 * h, depth


def project_bbox(camera: dict[str, Any], bbox: dict[str, Any] | None, resolution: tuple[int, int], *,
                 pad: float = CROP_PAD) -> dict[str, Any] | None:
    """Padded, clamped pixel rectangle covering a world bounding box in a render of ``resolution``
    (``{"x","y","w","h","clipped","fraction"}``), or None when the box is empty or not in front of the camera."""
    if not bbox or bbox.get("empty") or "min" not in bbox or "max" not in bbox:
        return None
    mn, mx = bbox["min"], bbox["max"]
    corners = [[mn[0] if i & 1 == 0 else mx[0], mn[1] if i & 2 == 0 else mx[1], mn[2] if i & 4 == 0 else mx[2]]
               for i in range(8)]
    pts = [project_point(camera, c, resolution) for c in corners]
    if any(p is None for p in pts):
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    px, py = (x1 - x0) * pad, (y1 - y0) * pad
    x0, x1, y0, y1 = x0 - px, x1 + px, y0 - py, y1 + py
    w, h = int(resolution[0]), int(resolution[1])
    clipped = x0 < 0 or y0 < 0 or x1 > w or y1 > h
    cx0, cy0 = max(0, int(x0)), max(0, int(y0))
    cx1, cy1 = min(w, int(x1) + 1), min(h, int(y1) + 1)
    if cx1 - cx0 <= 0 or cy1 - cy0 <= 0:
        return None
    return {"x": cx0, "y": cy0, "w": cx1 - cx0, "h": cy1 - cy0, "clipped": clipped,
            "fraction": ((cx1 - cx0) * (cy1 - cy0)) / float(w * h)}


def framing_contains(framing: dict[str, Any], center: list[float], size: list[float]) -> bool:
    """True when the bounding sphere of (center, size) fits inside the sphere this framing was fitted to."""
    import math

    radius = 0.5 * math.sqrt(sum(float(s) * float(s) for s in size))
    offset = math.sqrt(sum((float(center[i]) - float(framing["center"][i])) ** 2 for i in range(3)))
    return offset + radius <= float(framing["fit_radius"]) * float(framing.get("margin", 1.0)) + 1e-9


def engine_for(mode: str) -> str:
    try:
        return ENGINES[mode]
    except KeyError:
        raise ValueError(f"unknown render mode {mode!r}; expected one of {sorted(ENGINES)}") from None


@dataclass
class ViewSpec:
    name: str
    mode: str
    camera: dict[str, Any]
    resolution: tuple[int, int] = (1024, 768)
    visibility: dict[str, Any] = field(default_factory=dict)
    lighting: str = "neutral_studio"
    color_management: dict[str, str] = field(default_factory=lambda: {"view_transform": "Standard", "look": "None",
                                                                       "display_device": "sRGB"})
    samples: int = 16
    seed: int = 0
    film_transparent: bool = False
    evidence_label: str = "matched"   # matched | inferred_construction (R-61)
    pose: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "mode": self.mode, "engine": engine_for(self.mode), "camera": dict(self.camera),
            "resolution": [int(self.resolution[0]), int(self.resolution[1])], "visibility": dict(self.visibility),
            "lighting": self.lighting, "color_management": dict(self.color_management), "samples": int(self.samples),
            "seed": int(self.seed), "film_transparent": bool(self.film_transparent),
            "evidence_label": self.evidence_label, "pose": dict(self.pose),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ViewSpec":
        return cls(name=d["name"], mode=d["mode"], camera=dict(d.get("camera") or {}),
                   resolution=tuple(d.get("resolution") or (1024, 768)), visibility=dict(d.get("visibility") or {}),
                   lighting=d.get("lighting", "neutral_studio"),
                   color_management=dict(d.get("color_management") or {"view_transform": "Standard", "look": "None",
                                                                        "display_device": "sRGB"}),
                   samples=int(d.get("samples", 16)), seed=int(d.get("seed", 0)),
                   film_transparent=bool(d.get("film_transparent", False)),
                   evidence_label=d.get("evidence_label", "matched"), pose=dict(d.get("pose") or {}))


def applied_settings_match(view: dict[str, Any], applied: dict[str, Any]) -> list[str]:
    """Problems when the backend reports settings different from the requested view."""
    problems = []
    want_engine = view.get("engine") or engine_for(view.get("mode", "clay"))
    if applied.get("engine") != want_engine:
        problems.append(f"engine: requested {want_engine}, applied {applied.get('engine')}")
    want_res = [int(x) for x in view.get("resolution", [1024, 768])]
    if [int(x) for x in (applied.get("resolution") or [])] != want_res:
        problems.append(f"resolution: requested {want_res}, applied {applied.get('resolution')}")
    want_vt = (view.get("color_management") or {}).get("view_transform", "Standard")
    if applied.get("view_transform") != want_vt:
        problems.append(f"view_transform: requested {want_vt}, applied {applied.get('view_transform')}")
    return problems


def render_cache_key(*, revision_sha256: str, asset_dependency_hashes: dict[str, str], view: dict[str, Any],
                     view_version: int, blender_version: str, render_script_sha256: str) -> str:
    """Every input that affects the image (R-63): revision, assets, the full view (camera, visibility,
    lighting, engine, resolution, color management, seed, samples), Blender version, render script."""
    return hash_json({
        "revision_sha256": revision_sha256, "assets": dict(sorted(asset_dependency_hashes.items())),
        "view": view, "view_version": view_version, "blender_version": blender_version,
        "render_script_sha256": render_script_sha256, "manifest_version": MANIFEST_VERSION,
    })


def build_manifest(*, revision_id: str, revision_sha256: str, asset_dependencies: dict[str, str], view: dict[str, Any],
                   view_version: int, applied: dict[str, Any], output_path: str | Path, output_sha256: str | None,
                   success: bool, error: str, elapsed_s: float, render_script_sha256: str, cache_key: str,
                   view_id: str | None = None) -> dict[str, Any]:
    cm = view.get("color_management") or {}
    return {
        "manifest_version": MANIFEST_VERSION,
        "source": {"revision_id": revision_id, "revision_sha256": revision_sha256,
                   "asset_dependencies": dict(asset_dependencies)},
        "view": {"id": view_id, "name": view.get("name"), "version": view_version,
                 "evidence_label": view.get("evidence_label", "matched"), "part_id": view.get("part_id")},
        "camera": applied.get("camera") or view.get("camera"),
        "pose": view.get("pose") or {},
        "visibility": {"isolate": (view.get("visibility") or {}).get("isolate"),
                       "hide": (view.get("visibility") or {}).get("hide") or [],
                       "hidden_ids": applied.get("hidden_ids")},
        "materials_lighting": {"mode": view.get("mode"), "lighting": view.get("lighting")},
        "renderer": {"engine": applied.get("engine"), "blender_version": applied.get("blender_version"),
                     "samples": applied.get("samples"), "settings": applied},
        "output": {"path": str(output_path), "sha256": output_sha256, "resolution": applied.get("resolution"),
                   "color_management": {"view_transform": applied.get("view_transform", cm.get("view_transform")),
                                        "look": applied.get("look", cm.get("look")),
                                        "display_device": applied.get("display_device", cm.get("display_device"))},
                   "seed": applied.get("seed")},
        "success": bool(success), "error": error, "elapsed_s": elapsed_s,
        "render_script_sha256": render_script_sha256, "cache_key": cache_key, "created_at": utc_now(),
    }


def render_is_stale(render: Record, *, current_revision_id: str, expected_cache_key: str | None) -> tuple[bool, str | None]:
    """File existence or timestamps are insufficient (R-63): check status, file hash, revision, and inputs."""
    manifest = render.data.get("manifest") or {}
    if render.state != "ok" or not manifest.get("success", False):
        return True, "render_failed"
    path = Path(render.data.get("file") or manifest.get("output", {}).get("path", ""))
    if not path.is_file():
        return True, "missing_file"
    expected_hash = (manifest.get("output") or {}).get("sha256")
    try:
        actual = sha256_file(path)
    except OSError:
        return True, "unreadable_file"
    if not expected_hash or actual != expected_hash:
        return True, "hash_mismatch"
    if (manifest.get("source") or {}).get("revision_id") != current_revision_id:
        return True, "revision_mismatch"
    if expected_cache_key is not None and render.data.get("cache_key") != expected_cache_key:
        return True, "inputs_changed"
    return False, None


def renders_fresh(renders: list[Record], *, required_view_ids: list[str], current_revision_id: str,
                  expected_keys: dict[str, str | None]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    by_view: dict[str, list[Record]] = {}
    for r in renders:
        by_view.setdefault(r.data.get("view_id", ""), []).append(r)
    for view_id in required_view_ids:
        candidates = sorted(by_view.get(view_id, []), key=lambda r: r.created_at)
        if not candidates:
            reasons.append(f"{view_id}: no render")
            continue
        stale, why = render_is_stale(candidates[-1], current_revision_id=current_revision_id,
                                     expected_cache_key=expected_keys.get(view_id))
        if stale:
            reasons.append(f"{view_id}: {why}")
    return (not reasons), reasons
