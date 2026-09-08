"""Builder configuration (spec D9): the top-level ``builder:`` key of config.yaml, parsed leniently.

A missing key means defaults. Unknown sub-keys are kept in ``raw`` and ignored. Values of the wrong
type fall back to the default and are reported in ``warnings`` so the CLI and GUI can show them.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DEADLINES: dict[str, int] = {"validate": 120, "apply": 600, "render": 900, "measure": 120, "fixture": 300}
DEFAULT_PROVIDER_TIMEOUTS: dict[str, int] = {"response": 900, "inactivity": 180, "cancel_probe_after": 8}
DEFAULT_LIMITS: dict[str, Any] = {"wall_clock_minutes": 0, "max_cost_usd": 0.0, "max_requests": 0,
                                  "max_renders": 0, "attempts_per_finding": 2,
                                  # consecutive loop steps without evidence-supported progress before the run stops
                                  # with stop reason ``stalled`` (R-85, R-88); zero disables the check
                                  "stall_steps": 12}
DEFAULT_AGENTS: dict[str, dict[str, str]] = {
    # Owner guidance 2026-09-07: GPT 6 and Fable 5.1. `fable` is an alias listed in `claude --help`;
    # `gpt-6-astra` is the model configured in the owner's ~/.codex/config.toml. Nothing else is assumed.
    "A": {"provider": "claude", "model": "fable", "reasoning": "max", "executable": ""},
    "B": {"provider": "codex", "model": "gpt-6-astra", "reasoning": "xhigh", "executable": ""},
}
ISOLATED_REVIEW_MODES = ("when_contaminated", "always")
# Concept stage (addendum A, D11, D12, R-100, R-102, R-105)
APPROVAL_MODES = ("each", "anchor_only", "auto")
DEFAULT_CONCEPT_VIEWS = ["front", "side", "rear", "top", "underside", "three-quarter"]
DEFAULT_CONCEPT: dict[str, Any] = {"approval": "each", "anchor_candidates": 4, "views": list(DEFAULT_CONCEPT_VIEWS),
                                   "max_images": 40, "max_regenerations_per_view": 3, "import_dir": ""}
# Roles a user may pre-assign to a seat for a run (R-8 user override; R-107 still bars a seat from reviewing,
# verifying, or reassessing its own operation). ``brief``, ``plan``, ``build`` are the task kinds the engine assigns
# through ``_assign``; the others are the judging and correcting roles assigned through ``_role_of``.
ASSIGNABLE_ROLES = ("brief", "plan", "build", "corrector", "reviewer", "verifier", "reassessor")
IMAGE_SEATS = ("manual",)       # an API seat may be added later (keys from the environment only, D12)
DEFAULT_IMAGE_GENERATION: dict[str, str] = {"seat": "manual", "vendor": "chatgpt", "model": ""}
BLENDER_SEARCH_ROOTS = (Path(r"C:\Program Files\Blender Foundation"),)


@dataclass
class AgentBinding:
    label: str
    provider: str
    model: str = ""
    reasoning: str = ""
    executable: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "model": self.model, "reasoning": self.reasoning,
                "executable": self.executable}


@dataclass
class BuilderConfig:
    workflow_root: str = ""
    blender_executable: str = ""
    deadlines: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_DEADLINES))
    agents: dict[str, AgentBinding] = field(default_factory=dict)
    provider_timeouts: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PROVIDER_TIMEOUTS))
    limits: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    attended: bool = True
    isolated_reviews: str = "when_contaminated"
    presets: dict[str, dict[str, Any]] = field(default_factory=dict)
    concept: dict[str, Any] = field(default_factory=lambda: _copy_concept(DEFAULT_CONCEPT))
    image_generation: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_IMAGE_GENERATION))
    assignments: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # --- construction -------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BuilderConfig":
        """``data`` is the whole config.yaml mapping (the ``builder`` key is read from it)."""
        data = data if isinstance(data, dict) else {}
        section = data.get("builder")
        section = section if isinstance(section, dict) else {}
        warnings: list[str] = []
        cfg = cls(raw=dict(section), warnings=warnings)

        cfg.workflow_root = _as_str(section.get("workflow_root"), "", "workflow_root", warnings)
        cfg.attended = _as_bool(section.get("attended"), True, "attended", warnings)
        iso = _as_str(section.get("isolated_reviews"), "when_contaminated", "isolated_reviews", warnings)
        if iso not in ISOLATED_REVIEW_MODES:
            warnings.append(f"isolated_reviews={iso!r} is not one of {ISOLATED_REVIEW_MODES}; using when_contaminated")
            iso = "when_contaminated"
        cfg.isolated_reviews = iso

        blender = section.get("blender")
        blender = blender if isinstance(blender, dict) else {}
        cfg.blender_executable = _as_str(blender.get("executable"), "", "blender.executable", warnings)
        cfg.deadlines = _merge_numbers(DEFAULT_DEADLINES, blender.get("deadlines"), "blender.deadlines", warnings)
        cfg.provider_timeouts = _merge_numbers(DEFAULT_PROVIDER_TIMEOUTS, section.get("provider_timeouts"),
                                               "provider_timeouts", warnings)
        cfg.limits = _merge_numbers(DEFAULT_LIMITS, section.get("limits"), "limits", warnings)

        agents_raw = section.get("agents")
        agents_raw = agents_raw if isinstance(agents_raw, dict) else {}
        cfg.agents = {}
        for label, defaults in DEFAULT_AGENTS.items():
            given = agents_raw.get(label)
            if isinstance(given, dict):
                cfg.agents[label] = AgentBinding(
                    label=label,
                    provider=_as_str(given.get("provider"), defaults["provider"], f"agents.{label}.provider", warnings),
                    model=_as_str(given.get("model"), "", f"agents.{label}.model", warnings),
                    reasoning=_as_str(given.get("reasoning"), "", f"agents.{label}.reasoning", warnings),
                    executable=_as_str(given.get("executable"), "", f"agents.{label}.executable", warnings),
                )
            else:
                cfg.agents[label] = AgentBinding(label=label, **defaults)
        for label in agents_raw:
            if label not in DEFAULT_AGENTS:
                warnings.append(f"agents.{label}: only agents A and B exist; ignored")

        presets_raw = section.get("presets")
        cfg.presets = {str(k): dict(v) for k, v in (presets_raw or {}).items() if isinstance(v, dict)} \
            if isinstance(presets_raw, dict) else {}
        cfg.concept = _parse_concept(section.get("concept"), warnings)
        cfg.image_generation = _parse_image_generation(section.get("image_generation"), warnings)
        cfg.assignments = parse_assignments(section.get("assignments"), list(cfg.agents), warnings)
        return cfg

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "BuilderConfig":
        path: Path | None
        if config_path is None:
            from config import Config  # Alloy's loader; discovery order is unchanged (spec D2)

            path = Config.get_config_path()
        else:
            path = Path(config_path)
        if path is None or not path.exists():
            return cls.from_dict({})
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(yaml.safe_load(f) or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_root": self.workflow_root, "attended": self.attended,
            "isolated_reviews": self.isolated_reviews,
            "blender": {"executable": self.blender_executable, "deadlines": dict(self.deadlines)},
            "agents": {k: v.to_dict() for k, v in self.agents.items()},
            "provider_timeouts": dict(self.provider_timeouts), "limits": dict(self.limits),
            "presets": {k: dict(v) for k, v in self.presets.items()},
            "concept": _copy_concept(self.concept), "image_generation": dict(self.image_generation),
            "assignments": dict(self.assignments),
        }


def parse_assignments(given: Any, seats: list[str], warnings: list[str], *, source: str = "assignments") -> dict[str, str]:
    """``{role: seat}`` with unknown roles and seats dropped and reported (never guessed)."""
    out: dict[str, str] = {}
    if given is None:
        return out
    if not isinstance(given, dict):
        warnings.append(f"{source}: expected a mapping of role to seat (one of {ASSIGNABLE_ROLES}); got {given!r}; ignored")
        return out
    for role, seat in given.items():
        role_s, seat_s = str(role), str(seat)
        if role_s not in ASSIGNABLE_ROLES:
            warnings.append(f"{source}.{role_s}: unknown role; expected one of {ASSIGNABLE_ROLES}; ignored")
            continue
        if seat_s not in seats:
            warnings.append(f"{source}.{role_s}={seat_s!r}: not a configured seat ({', '.join(seats)}); ignored")
            continue
        out[role_s] = seat_s
    return out


def _copy_concept(d: dict[str, Any]) -> dict[str, Any]:
    out = dict(d)
    out["views"] = list(d.get("views") or [])
    return out


def _parse_concept(given: Any, warnings: list[str]) -> dict[str, Any]:
    out = _copy_concept(DEFAULT_CONCEPT)
    if given is None:
        return out
    if not isinstance(given, dict):
        warnings.append("concept: expected a mapping; using defaults")
        return out
    for name, value in given.items():
        if name == "approval":
            mode = _as_str(value, "each", "concept.approval", warnings)
            if mode not in APPROVAL_MODES:
                warnings.append(f"concept.approval={mode!r} is not one of {APPROVAL_MODES}; using each")
                mode = "each"
            out["approval"] = mode
        elif name == "views":
            if isinstance(value, list) and all(isinstance(v, str) and v for v in value):
                out["views"] = list(value)
            elif isinstance(value, str) and value.strip():
                out["views"] = [v.strip() for v in value.split(",") if v.strip()]
            else:
                warnings.append(f"concept.views: expected a list of view names, got {value!r}; using defaults")
        elif name == "import_dir":
            out["import_dir"] = _as_str(value, "", "concept.import_dir", warnings)
        elif name in ("anchor_candidates", "max_images", "max_regenerations_per_view"):
            try:
                if isinstance(value, bool):
                    raise ValueError
                out[name] = int(value)
            except (TypeError, ValueError):
                warnings.append(f"concept.{name}: expected a number, got {value!r}; using {DEFAULT_CONCEPT[name]}")
        else:
            warnings.append(f"concept.{name}: unknown setting; ignored")
    return out


def _parse_image_generation(given: Any, warnings: list[str]) -> dict[str, str]:
    out = dict(DEFAULT_IMAGE_GENERATION)
    if given is None:
        return out
    if not isinstance(given, dict):
        warnings.append("image_generation: expected a mapping; using defaults")
        return out
    seat = _as_str(given.get("seat"), "manual", "image_generation.seat", warnings)
    if seat not in IMAGE_SEATS:
        warnings.append(f"image_generation.seat={seat!r} is not available in this build (only {IMAGE_SEATS}); using manual. "
                        "An API seat, if added later, takes keys from the environment only (D12)")
        seat = "manual"
    out["seat"] = seat
    out["vendor"] = _as_str(given.get("vendor"), "chatgpt", "image_generation.vendor", warnings)
    out["model"] = _as_str(given.get("model"), "", "image_generation.model", warnings)
    for name in given:
        if name not in ("seat", "vendor", "model"):
            warnings.append(f"image_generation.{name}: unknown setting; ignored")
    return out


# --- lenient coercion -------------------------------------------------------------

def _as_str(value: Any, default: str, key: str, warnings: list[str]) -> str:
    if value is None:
        return default
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    warnings.append(f"{key}: expected text, got {type(value).__name__}; using {default!r}")
    return default


def _as_bool(value: Any, default: bool, key: str, warnings: list[str]) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "yes", "no", "on", "off", "1", "0"):
        return value.strip().lower() in ("true", "yes", "on", "1")
    warnings.append(f"{key}: expected true/false, got {value!r}; using {default}")
    return default


def _merge_numbers(defaults: dict[str, Any], given: Any, key: str, warnings: list[str]) -> dict[str, Any]:
    out = dict(defaults)
    if given is None:
        return out
    if not isinstance(given, dict):
        warnings.append(f"{key}: expected a mapping; using defaults")
        return out
    for name, value in given.items():
        if name not in defaults:
            warnings.append(f"{key}.{name}: unknown setting; ignored")
            continue
        default = defaults[name]
        try:
            if isinstance(value, bool):
                raise ValueError
            out[name] = float(value) if isinstance(default, float) else int(value)
        except (TypeError, ValueError):
            warnings.append(f"{key}.{name}: expected a number, got {value!r}; using {default}")
    return out


# --- discovery and paths ----------------------------------------------------------

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?")


def _dir_version(path: Path) -> tuple[int, int]:
    m = _VERSION_RE.search(path.name)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)


def discover_blender(configured: str = "", search_roots: list[Path] | tuple[Path, ...] | None = None) -> Path | None:
    """Configured path first (must exist), else the highest ``Blender */blender.exe`` under the roots."""
    if configured:
        p = Path(configured)
        return p if p.is_file() else None
    roots = list(search_roots) if search_roots is not None else list(BLENDER_SEARCH_ROOTS)
    candidates: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for d in root.glob("Blender *"):
            exe = d / "blender.exe"
            if exe.is_file():
                candidates.append(exe)
    if not candidates:
        return None
    candidates.sort(key=lambda p: _dir_version(p.parent), reverse=True)
    return candidates[0]


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "project"


def default_workflow_dir(project_dir: str | Path | None, slug: str, cfg: BuilderConfig,
                         cwd: Path | None = None) -> Path:
    """``builder.workflow_root/<slug>`` when configured, else ``<project_dir or cwd>/alloy-builder/<slug>``."""
    if cfg.workflow_root:
        return Path(cfg.workflow_root) / slug
    base = Path(project_dir) if project_dir else (cwd or Path.cwd())
    return base / "alloy-builder" / slug
