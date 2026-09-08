"""Read model and worker session for the Tk view (spec R-91, R-92; design Section 9.2).

Two things live here, both free of Tk so they are testable without a display:

* ``snapshot(engine)`` turns the authoritative records into one plain dict per panel of R-91: project and
  references, agents with capability tiers and the effective reasoning setting, stage and ownership, parts with
  construction relations, renders labelled with revision and render ids and their evidence label, findings linked
  to parts and images, coverage, consumption and limits, checkpoints, the concept stage (approval mode always
  present), and the measurement boxes a view can overlay. It only reads; nothing here changes state.
* ``BuilderSession`` runs every engine call on one worker thread and publishes ``("busy" | "event" | "done" |
  "error" | "snapshot" | "closed", ...)`` tuples on ``events`` (a ``queue.Queue``). The Tk thread submits jobs and
  drains the queue with ``after``; it never touches the store or waits on a provider or Blender.

A generated image is never evidence of the original design (R-32): the snapshot carries every reference's
``evidence_of_original`` flag and canon state so the view can label it.
"""
from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any, Callable

from .concept import ConceptStage
from .config import BuilderConfig, discover_blender
from .engine import Engine
from .operations import IN_FLIGHT_STATES
from .project import Project
from .providers import make_adapter
from .providers.mock import ScriptedAdapter
from .records import Record
from .references import References
from .render import project_bbox, render_is_stale

JOURNAL_TAIL = 60


# ============================================================================================ read model

def snapshot(engine: Engine) -> dict[str, Any]:
    """One plain dict for the whole window. Reads records only (never invokes a provider or Blender)."""
    store = engine.store
    prj = engine.project
    status = engine.status()
    latest = engine.operations.latest_revision()
    latest_id = latest.id if latest else None
    agents = _agents(engine, status)
    views = {v.id: v for v in store.list("view")}
    renders = [_render(r, views, latest_id) for r in store.list("render")]
    parts = _parts(engine)
    findings = [_finding(f, store) for f in store.list("finding")]
    return {
        "project": {"id": prj.record.id, "name": prj.record.data.get("name"), "asset_name": prj.record.data.get("asset_name"),
                    "workflow_dir": str(prj.workflow_dir), "first_component": prj.record.data.get("first_component"),
                    "preset_id": prj.record.data.get("preset_id"), "fixture": bool(prj.record.data.get("fixture")),
                    "target_reference": prj.record.data.get("target_reference"),
                    "existing_source": prj.record.data.get("existing_source"), "status_note": prj.record.data.get("status_note")},
        "blender": {"version": engine.blender_version, "executable": (engine.blender_info or {}).get("executable"),
                    "available": engine.runner is not None},
        "stage": _stage(engine, status),
        "agents": agents,
        "image_seat": _image_seat(engine, status),
        "components": status["components"],
        "references": [_reference(r, store) for r in References(prj).current()],
        "revisions": [{"id": r.id, "parent_revision_id": r.data.get("parent_revision_id"), "note": r.data.get("note"),
                       "created_at": r.created_at, "sha256": r.data.get("sha256"), "file": r.data.get("file"),
                       "created_by_op_id": r.data.get("created_by_op_id")} for r in store.list("revision")],
        "latest_revision_id": latest_id,
        "renders": renders,
        "parts": parts,
        "relations": [{"id": r.id, "from_part": r.data.get("from_part"), "to_part": r.data.get("to_part"),
                       "type": r.data.get("type"), "evidence": r.data.get("evidence")} for r in store.list("relation")],
        "findings": findings,
        "coverage": [{"id": c.id, "part_id": c.data.get("part_id"), "view_name": c.data.get("view_name"), "view_id": c.data.get("view_id"),
                      "revision_id": c.data.get("revision_id"), "inspected_by": _label_or_id(engine, c.data.get("inspected_by")),
                      "instances_inspected": c.data.get("instances_inspected"), "sampling_strategy": c.data.get("sampling_strategy")}
                     for c in store.list("coverage")],
        "measurements": _measurements(prj),
        "checkpoints": status["checkpoints"],
        "consumption": status["consumption"],
        "contributions": status["contributions"],
        "questions_for_user": status["questions_for_user"],
        "notes": status["notes"],
        "remaining_discrepancies": status["remaining_discrepancies"],
        "uncertainties": status["uncertainties"],
        "concept": _concept(engine),
        "journal_tail": _journal_tail(store),
    }


def overlay_rects(snap: dict[str, Any], render_id: str) -> list[dict[str, Any]]:
    """Measured part boxes projected into a render's pixels (R-68: from a measurement of that revision, never
    invented). Empty when the render or a measurement of its revision is unknown."""
    render = next((r for r in snap.get("renders", []) if r["id"] == render_id), None)
    if render is None or not render.get("camera") or not render.get("resolution"):
        return []
    boxes = (snap.get("measurements") or {}).get(render["revision_id"]) or {}
    out: list[dict[str, Any]] = []
    for part_id, bbox in boxes.items():
        rect = project_bbox(render["camera"], bbox, tuple(render["resolution"]), pad=0.0)
        if rect is None:
            continue
        out.append({"part_id": part_id, "x": rect["x"], "y": rect["y"], "w": rect["w"], "h": rect["h"],
                    "clipped": rect.get("clipped"), "space": "render_pixels", "source": "measurement",
                    "revision_id": render["revision_id"]})
    return out


# --- pieces ---------------------------------------------------------------------------------------------------

def _label_or_id(engine: Engine, agent_id: Any) -> Any:
    for label, rec in engine.agents.items():
        if rec.id == agent_id:
            return label
    return agent_id


def _agents(engine: Engine, status: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for a in status["agents"]:
        label = a["label"]
        pf = engine.preflight_reports.get(label) or {}
        effective = a.get("effective_settings") or pf.get("effective_settings") or {}
        eff_reasoning = effective.get("reasoning") if isinstance(effective, dict) else None
        caps = {name: {"declared": t.get("declared"), "local": t.get("local"), "live": t.get("live"), "evidence": t.get("evidence")}
                for name, t in (pf.get("capabilities") or {}).items()}
        out.append({
            "label": label, "id": a["id"], "provider": a["provider"], "model": a["model"],
            "model_effective": effective.get("model") if isinstance(effective, dict) else None,
            "reasoning_requested": a["reasoning"] or None,
            "reasoning_effective": eff_reasoning,
            "reasoning_note": ("not exposed by CLI" if not a["reasoning"] else
                               ("confirmed by the provider's own output" if eff_reasoning else "not confirmed by provider yet")),
            "reasoning_fallbacks": a.get("reasoning_fallbacks") or [],
            "executable": engine.agents[label].data.get("executable") if label in engine.agents else None,
            "invocations": a["invocations"],
            "preflight": {"ok": pf.get("ok"), "live": pf.get("live"), "at": pf.get("at"), "blockers": pf.get("blockers") or [],
                          "cli_path": a.get("cli_path"), "cli_version": a.get("cli_version"), "capabilities": caps,
                          "probe_cost": pf.get("probe_cost")},
        })
    return out


def _image_seat(engine: Engine, status: dict[str, Any]) -> dict[str, Any]:
    pf = engine.preflight_reports.get("I")
    if pf is None:
        reports = [r for r in engine.store.list("preflight_report") if r.data.get("agent_id") == "I"]
        pf = dict(reports[-1].data) if reports else {}
    seat = (status.get("concept") or {}).get("seat") or {}
    return {"declared": seat, "preflight": {"declared": pf.get("declared"), "local": pf.get("local"), "live": pf.get("live"),
                                            "ok": pf.get("ok"), "blockers": pf.get("blockers") or [], "at": pf.get("at")}}


def _stage(engine: Engine, status: dict[str, Any]) -> dict[str, Any]:
    store = engine.store
    tasks = store.list("task")
    task = None
    if tasks:
        t = tasks[-1]
        task = {"id": t.id, "kind": t.data.get("kind"), "state": t.state, "owner": _label_or_id(engine, t.data.get("owner_agent_id")),
                "rationale": t.data.get("rationale"), "expected_outcome": t.data.get("expected_outcome"),
                "part_ids": t.data.get("part_ids") or [], "base_revision_id": t.data.get("base_revision_id"),
                "session_kind": t.data.get("session_kind")}
    active = [{"id": o.id, "kind": o.data.get("kind"), "state": o.state, "agent": _label_or_id(engine, o.data.get("agent_id")),
               "intent": o.data.get("intent"), "target_part_ids": o.data.get("target_part_ids") or []}
              for o in store.list("operation") if o.state in IN_FLIGHT_STATES]
    holder = engine.ownership.current("assembly")
    ownership = None
    if holder is not None:
        ownership = {"holder": _label_or_id(engine, holder.data.get("holder")), "base_revision_id": holder.data.get("base_revision_id"),
                     "granted_at": holder.data.get("granted_at"), "resource": holder.data.get("resource")}
    run = engine.run
    return {"run_id": status["run_id"], "execution": status["execution"], "stop_reason": status["stop_reason"],
            "stage": status["stage"], "attended": status["attended"], "task": task, "active_operations": active,
            "ownership": ownership, "last_note": (status["notes"][-1] if status["notes"] else None),
            "handoffs": len(store.list("handoff")), "plan": (run.data.get("plan") if run else None),
            "component_id": run.data.get("component_id") if run else None,
            "assignment_overrides": engine.assignment_overrides()}


def _reference(r: Record, store: Any | None = None) -> dict[str, Any]:
    d = r.data
    regions = [{"id": g.id, "name": g.data.get("name"), "bbox": list(g.data.get("bbox") or []), "purpose": g.data.get("purpose"),
                "space": g.data.get("space", "original_pixels")}
               for g in (store.list("reference_region", parent_id=r.id) if store is not None else [])]
    return {"regions": regions,"id": r.id, "file": d.get("file"), "labels": d.get("labels") or [], "kind": d.get("kind"), "notes": d.get("notes"),
            "width": d.get("width"), "height": d.get("height"), "version": d.get("version"),
            "canon_state": d.get("canon_state", "approved"), "precedence_label": d.get("precedence_label", "owner_target"),
            "evidence_of_original": bool(d.get("evidence_of_original", d.get("kind") == "target")),
            "composite": bool(d.get("composite")), "declared": d.get("declared"), "generation_id": d.get("generation_id"),
            "derived_from": d.get("derived_from"), "part_id": d.get("part_id"),
            "verdicts": d.get("verdicts") or {}, "verdict_summary": d.get("verdict_summary"),
            "approval": d.get("approval"), "rejection": d.get("rejection"), "original_path": d.get("original_path")}


def _render(r: Record, views: dict[str, Record], latest_id: str | None) -> dict[str, Any]:
    d = r.data
    view = views.get(d.get("view_id") or "")
    spec = (view.data.get("spec") if view else None) or d.get("manifest", {}).get("view") or {}
    stale, why = render_is_stale(r, current_revision_id=latest_id or "", expected_cache_key=None) if latest_id else (True, "no_revision")
    manifest = d.get("manifest") or {}
    applied = manifest.get("applied") or {}
    return {"id": r.id, "view_name": d.get("view_name"), "view_id": d.get("view_id"), "revision_id": d.get("revision_id"),
            "component_id": d.get("component_id"), "file": d.get("file"), "mode": d.get("mode"), "state": r.state,
            "reason": d.get("reason"), "part_id": d.get("part_id") or spec.get("part_id"),
            "evidence_label": spec.get("evidence_label") or ("inferred_construction" if spec.get("subject") == "assembly" else "matched"),
            "resolution": list(spec.get("resolution") or applied.get("resolution") or []),
            "camera": spec.get("camera"), "engine": applied.get("engine"), "stale": stale, "stale_reason": why,
            "created_at": r.created_at, "error": d.get("error")}


def _parts(engine: Engine) -> list[dict[str, Any]]:
    store = engine.store
    rels = store.list("relation")
    owner_of: dict[str, Any] = {}
    for t in store.list("task"):
        for pid in t.data.get("part_ids") or []:
            owner_of[pid] = _label_or_id(engine, t.data.get("owner_agent_id"))
    comps = {pid: c.data.get("name") for c in store.list("component") for pid in (c.data.get("part_ids") or [])}
    out = []
    for p in store.list("part"):
        d = p.data
        relations = [{"id": r.id, "type": r.data.get("type"), "from_part": r.data.get("from_part"), "to_part": r.data.get("to_part"),
                      "direction": "out" if r.data.get("from_part") == p.id else "in", "evidence": r.data.get("evidence")}
                     for r in rels if p.id in (r.data.get("from_part"), r.data.get("to_part"))]
        out.append({"id": p.id, "name": d.get("name"), "parent_id": p.parent_id, "state": p.state, "blender_ids": d.get("blender_ids") or [],
                    "evidence_status": d.get("evidence_status"), "confidence": d.get("confidence"),
                    "interpretation": d.get("interpretation"), "questions": d.get("questions") or [],
                    "dimensions": d.get("dimensions") or [], "component": d.get("component") or comps.get(p.id),
                    "owner": owner_of.get(p.id), "alloy_kind": d.get("alloy_kind"), "instance_of": d.get("instance_of"),
                    "relations": relations})
    return out


def _finding(f: Record, store: Any) -> dict[str, Any]:
    d = f.data
    attempts = []
    for aid in d.get("attempts") or []:
        a = store.get("correction_attempt", aid)
        if a is not None:
            attempts.append({"id": a.id, "state": a.state, "attempt_no": a.data.get("attempt_no"),
                             "before_render_ids": a.data.get("before_render_ids") or [], "after_render_ids": a.data.get("after_render_ids") or [],
                             "aligned_closeup_view": a.data.get("aligned_closeup_view")})
    return {"id": f.id, "state": f.state, "part_id": d.get("part_id"), "region": d.get("region"), "view": d.get("view"),
            "severity": d.get("severity"), "confidence": d.get("confidence"), "observed_mismatch": d.get("observed_mismatch"),
            "proposed_correction": d.get("proposed_correction"), "expected_improvement": d.get("expected_improvement"),
            "alternative_hypotheses": d.get("alternative_hypotheses") or [], "kind": d.get("kind"),
            "evidence_refs": d.get("evidence_refs") or [], "evidence_render_ids": d.get("evidence_render_ids") or [],
            "before_render_ids": d.get("before_render_ids") or [], "after_render_ids": d.get("after_render_ids") or [],
            "source_revision_id": d.get("source_revision_id"), "reported_by": d.get("reported_by"), "owner_agent_id": d.get("owner_agent_id"),
            "attempts": attempts, "resolution": d.get("resolution"), "last_verdict": d.get("last_verdict"),
            "user_requests": d.get("user_requests") or [], "component_id": d.get("component_id")}


def _measurements(prj: Project) -> dict[str, dict[str, Any]]:
    """Latest measured bounding boxes per revision, from the measurement files the review stage wrote."""
    logs = prj.path("logs")
    if not logs.is_dir():
        return {}
    latest: dict[str, tuple[float, dict[str, Any]]] = {}
    for path in logs.glob("meas_*/measurement.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                doc = json.load(f)
            rev = doc.get("revision_id")
            boxes = (doc.get("data") or {}).get("bboxes") or {}
            mtime = path.stat().st_mtime
        except (OSError, ValueError):
            continue
        if not rev or not isinstance(boxes, dict):
            continue
        if rev not in latest or mtime > latest[rev][0]:
            latest[rev] = (mtime, {k: v for k, v in boxes.items() if isinstance(v, dict) and not v.get("empty")})
    return {rev: boxes for rev, (_, boxes) in latest.items()}


def _concept(engine: Engine) -> dict[str, Any]:
    concept = ConceptStage(engine)
    st = concept.status()
    refs = {r.id: r for r in engine.store.list("reference")}
    anchor = refs.get(st["anchor"] or "")
    cands = []
    for c in st["candidates"]:
        r = refs.get(c["id"])
        if r is None:
            continue
        cands.append({**_reference(r, engine.store), "summary": c["summary"]})
    conflicts = [{"id": c.id, "state": c.state, "reference_ids": c.data.get("reference_ids"), "region": c.data.get("region"),
                  "what_differs": c.data.get("what_differs"), "reported_by": c.data.get("reported_by")} for c in concept.open_conflicts()]
    cov = st["coverage"]
    return {"plan_id": st["plan_id"], "mode": st["mode"], "mode_text": st["mode_text"], "canon_state": st["canon_state"],
            "anchor": _reference(anchor, engine.store) if anchor else None, "prompts": concept.prompts(), "candidates": cands,
            "coverage": cov.get("views") or {}, "parts": cov.get("parts") or {}, "missing": cov.get("missing") or [],
            "complete": cov.get("complete"), "conflicts": conflicts, "escalations": st["escalations"], "images": st["images"],
            "proceeded_partial": cov.get("proceeded_partial"), "seat": st["seat"], "cost": st["cost"],
            "art_director": st["art_director"], "rejected_rounds": st["rejected_rounds"], "flow_confirmed_at": st["flow_confirmed_at"],
            "requests": st["requests"]}


def _journal_tail(store: Any) -> list[dict[str, Any]]:
    last = store.last_seq()
    entries = store.journal(since_seq=max(0, last - JOURNAL_TAIL))
    return [{"seq": e.seq, "ts": e.ts, "actor": e.actor, "event": e.event, "record_kind": e.record_kind, "record_id": e.record_id,
             "from_state": e.from_state, "to_state": e.to_state, "outcome": e.outcome} for e in entries]


# ============================================================================================ session

RunnerFactory = Callable[[BuilderConfig, Project], Any]
AdaptersFactory = Callable[[BuilderConfig, Any], dict[str, Any]]


class BuilderSession:
    """Owns the project, engine, and concept stage on one worker thread. Every public method except ``pause``,
    ``cancel``, ``feedback``, and ``close`` submits a job and returns at once; results arrive on ``events``."""

    def __init__(self, config: BuilderConfig, *, runner_factory: RunnerFactory | None = None,
                 adapters_factory: AdaptersFactory | None = None, mock: str | Path | dict[str, Any] | None = None,
                 max_steps: int = 200):
        self.config = config
        self.runner_factory = runner_factory
        self.adapters_factory = adapters_factory
        self.mock = mock
        self.max_steps = max_steps
        self.events: queue.Queue = queue.Queue()
        self._jobs: queue.Queue = queue.Queue()
        self.project: Project | None = None
        self.engine: Engine | None = None
        self.concept: ConceptStage | None = None
        self.adapters: dict[str, Any] = {}
        self.runner: Any = None
        self._pause_requested = threading.Event()
        self._cancel_requested = threading.Event()
        self._needs_recovery = False
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name="builder-session", daemon=True)
        self._thread.start()

    @property
    def worker_thread_name(self) -> str:
        return self._thread.name

    @property
    def busy(self) -> bool:
        return not self._jobs.empty()

    # --- plumbing --------------------------------------------------------------------------------------------

    def submit(self, name: str, fn: Callable[[], Any]) -> None:
        if self._closed:
            raise RuntimeError("session is closed")
        self._jobs.put((name, fn))

    def _loop(self) -> None:
        while True:
            name, fn = self._jobs.get()
            if name == "__close__":
                self._close_project()
                self.events.put(("closed",))
                return
            self.events.put(("busy", name))
            try:
                result = fn()
                self.events.put(("done", name, result))
            except Exception as exc:  # noqa: BLE001 - every failure is shown, never hidden (R-48)
                self.events.put(("error", name, f"{type(exc).__name__}: {exc}"))
            if self.engine is not None:
                try:
                    self.events.put(("snapshot", snapshot(self.engine)))
                except Exception as exc:  # noqa: BLE001
                    self.events.put(("error", "snapshot", f"{type(exc).__name__}: {exc}"))

    def _observe(self, ev: dict[str, Any]) -> None:
        """Engine observer (worker thread): forward the event and, at stage boundaries, a fresh snapshot so the
        window shows progress during a long run (R-92)."""
        self.events.put(("event", ev))
        if self._pause_requested.is_set() and self.engine is not None:
            self.engine.pause()
        if ev.get("event") in ("stage", "run.stopped", "preflight") and self.engine is not None:
            try:
                self.events.put(("snapshot", snapshot(self.engine)))
            except Exception as exc:  # noqa: BLE001 - a snapshot failure is reported, never hidden
                self.events.put(("error", "snapshot", f"{type(exc).__name__}: {exc}"))

    def _require_engine(self) -> Engine:
        if self.engine is None:
            raise RuntimeError("no workflow project is open")
        return self.engine

    def _close_project(self) -> None:
        if self.project is not None:
            try:
                self.project.close()
            finally:
                self.project = self.engine = self.concept = None

    def close(self, timeout: float | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_requested.set()
        if self.engine is not None:
            self.engine.cancel_event.set()
        self._jobs.put(("__close__", None))
        self._thread.join(timeout)

    # --- project ---------------------------------------------------------------------------------------------

    def open(self, workflow_dir: str | Path) -> None:
        def job() -> dict[str, Any]:
            self._close_project()
            wf = Path(workflow_dir)
            if not Project.exists(wf):
                raise FileNotFoundError(f"no workflow project at {wf} (expected project.json)")
            prj = Project.open(wf)
            try:
                self.runner = self._make_runner(prj)
                self.adapters = self._make_adapters()
                eng = Engine(prj, self.config, adapters=self.adapters, runner=self.runner, observer=self._observe)
                run = eng.attach()
                self._needs_recovery = run is not None and run.state not in ("idle", "cancelled", "failed")
                eng.load_preflight()
            except Exception:
                prj.close()
                raise
            self.project, self.engine, self.concept = prj, eng, ConceptStage(eng)
            return {"workflow_dir": str(wf), "run_state": run.state if run else "idle", "needs_recovery": self._needs_recovery}
        self.submit("open", job)

    def _make_runner(self, prj: Project) -> Any:
        if self.runner_factory is not None:
            return self.runner_factory(self.config, prj)
        exe = discover_blender(self.config.blender_executable)
        if exe is None:
            return None
        from .blender.runner import BlenderRunner

        return BlenderRunner(exe, logs_dir=prj.path("logs"), deadlines=self.config.deadlines)

    def _make_adapters(self) -> dict[str, Any]:
        if self.adapters_factory is not None:
            return self.adapters_factory(self.config, self.mock)
        if self.mock is not None:
            play = self.mock
            if not isinstance(play, dict):
                with open(play, "r", encoding="utf-8") as f:
                    play = json.load(f)
            return {label: ScriptedAdapter(label, play.get(label, {})) for label in self.config.agents}
        return {label: make_adapter(binding) for label, binding in self.config.agents.items()}

    def add_reference(self, path: str | Path, *, labels: list[str], kind: str = "target", notes: str = "",
                      composite: bool = False, user: str = "user") -> None:
        def job() -> dict[str, Any]:
            eng = self._require_engine()
            rec = References(eng.project).add(path, labels=labels, kind=kind, notes=notes, composite=composite, actor=f"user:{user}")
            return {"reference_id": rec.id}
        self.submit("add_reference", job)

    def add_region(self, reference_id: str, name: str, bbox: list[int], *, purpose: str = "target_region", user: str = "user") -> None:
        """A target region or detail crop in ORIGINAL pixels (R-26, R-27); the view converts from canvas pixels first."""
        def job() -> dict[str, Any]:
            eng = self._require_engine()
            rec = References(eng.project).add_region(reference_id, name, [int(v) for v in bbox], purpose=purpose, actor=f"user:{user}")
            return {"region_id": rec.id, "bbox": rec.data["bbox"]}
        self.submit("add_region", job)

    def set_limits(self, changes: dict[str, Any], *, user: str = "user") -> None:
        """Edit limits (R-85, R-89). Recorded at once as a control request so a run in progress applies it at its next
        safe boundary; the worker job applies it directly when no run is consuming controls."""
        eng = self._require_engine()
        eng.store.push_control("limits", {"changes": dict(changes), "by": user})

        def job() -> dict[str, Any]:
            applied = self._require_engine().consume_controls(kinds=("limits",))
            if applied.get("errors"):
                raise RuntimeError("; ".join(applied["errors"]))
            return {"applied": applied.get("limits") or {}, "note": "applied by the running engine at its boundary" if not applied.get("limits") else ""}
        self.submit("set_limits", job)

    # --- preflight and runs ----------------------------------------------------------------------------------

    def preflight(self, *, live: bool = False, labels: list[str] | None = None) -> None:
        """``live`` spends provider usage; the view confirms with the user before calling this."""
        def job() -> dict[str, Any]:
            report = self._require_engine().preflight(live=live, labels=labels)
            return {k: {"ok": v.get("ok"), "blockers": v.get("blockers")} for k, v in report.items() if isinstance(v, dict)}
        self.submit("preflight_live" if live else "preflight", job)

    def _ensure_preflight(self, eng: Engine) -> None:
        loaded = eng.load_preflight()
        local_only = {label for label, r in loaded.items() if not r.get("live") and not isinstance(self.adapters.get(label), ScriptedAdapter)}
        if len(loaded) >= len(self.adapters) and not local_only:
            return
        missing = ", ".join(sorted((set(self.adapters) - set(loaded)) | local_only))
        if all(isinstance(a, ScriptedAdapter) for a in self.adapters.values()):
            eng.preflight(live=True)
            return
        raise RuntimeError(f"no current live preflight report for agent(s) {missing} (never run, or the CLI path, version, model, or "
                           "settings changed since; R-18). Run a live preflight first: it spends provider usage and is never "
                           "started implicitly.")

    def _run_loop(self, eng: Engine) -> dict[str, Any]:
        if self._pause_requested.is_set():
            eng.pause()
        try:
            status = eng.run_until_stop(max_steps=self.max_steps)
        finally:
            self._pause_requested.clear()
        if self._cancel_requested.is_set() and eng.run is not None and eng.run.state not in ("cancelled", "failed"):
            eng.cancel()
            status = eng.status()
        self._cancel_requested.clear()
        eng.cancel_event.clear()
        return {"execution": status["execution"], "stop_reason": status["stop_reason"], "stage": status["stage"]}

    def start_run(self, *, attended: bool | None = None, component_name: str | None = None,
                  assignments: dict[str, str] | None = None) -> None:
        """``assignments`` are per-run role overrides (``{"build": "B"}``); the engine validates them and R-107 still
        bars a seat from judging its own operation."""
        def job() -> dict[str, Any]:
            eng = self._require_engine()
            self._ensure_preflight(eng)
            eng.start(attended=attended, component_name=component_name, assignment_overrides=assignments)
            return self._run_loop(eng)
        self.submit("start_run", job)

    # --- revision 0 (R-93: never done by opening or creating the project) -----------------------------------------

    def register_source(self, path: Path, *, user: str = "user", note: str = "") -> None:
        """The owner's .blend validated in separate Blender processes and copied unmodified as revision 0."""
        def job() -> dict[str, Any]:
            from .source import register_source

            eng = self._require_engine()
            if self.runner is None:
                raise RuntimeError("Blender was not found; set builder.blender.executable to register a source")
            rev = register_source(eng.project, self.runner, path, actor=f"user:{user}", note=note)
            return {"revision_id": rev.id, "unmapped": (rev.data.get("source") or {}).get("unmapped_geometry") or []}
        self.submit("register_source", job)

    def register_empty_source(self, *, user: str = "user", note: str = "") -> None:
        """An empty scene built through the runner as revision 0."""
        def job() -> dict[str, Any]:
            from .source import register_empty

            eng = self._require_engine()
            if self.runner is None:
                raise RuntimeError("Blender was not found; set builder.blender.executable to build the empty scene")
            rev = register_empty(eng.project, self.runner, actor=f"user:{user}", note=note)
            return {"revision_id": rev.id}
        self.submit("register_empty_source", job)

    def resume_run(self, *, user: str = "user") -> None:
        def job() -> dict[str, Any]:
            eng = self._require_engine()
            self._ensure_preflight(eng)
            if eng.attach() is None:
                raise RuntimeError("no run to resume; start one")
            recovery = None
            if self._needs_recovery:
                recovery = eng.recover()
                self._needs_recovery = False
            if eng.run.state != "running":
                eng.resume(user=f"user:{user}")
            out = self._run_loop(eng)
            if recovery is not None:
                out["recovery"] = recovery
            return out
        self.submit("resume_run", job)

    def pause(self) -> None:
        """Thread-safe: applied at the next safe boundary (R-59)."""
        self._pause_requested.set()
        if self.engine is not None:
            self.engine.pause()

    def cancel(self, user: str = "user") -> None:
        """Thread-safe: the in-flight provider or Blender process is cancelled and the run is cancelled after
        reconciliation on the worker thread."""
        self._cancel_requested.set()
        if self.engine is not None:
            self.engine.cancel_event.set()
            self.engine.store.push_control("cancel", {"by": f"user:{user}"})

    def feedback(self, text: str, user: str = "user") -> None:
        """Thread-safe: recorded immediately as a control request, applied at the next safe boundary (R-59)."""
        if self.engine is None:
            raise RuntimeError("no workflow project is open")
        self.engine.store.push_control("feedback", {"text": text, "by": f"user:{user}"})
        self.submit("feedback", lambda: {"recorded": True})

    # --- user actions (R-75) -----------------------------------------------------------------------------------

    def accept_component(self, component_id: str, *, user: str) -> None:
        self.submit("accept", lambda: {"acceptance": self._require_engine().accept_component(component_id, user=f"user:{user}").state})

    def reopen_component(self, component_id: str, *, reason: str, user: str) -> None:
        self.submit("reopen", lambda: {"component": self._require_engine().reopen_component(component_id, reason=reason, user=f"user:{user}").state})

    def waive_finding(self, finding_id: str, *, rationale: str, user: str) -> None:
        self.submit("waive", lambda: {"finding": self._require_engine().waive_finding(finding_id, rationale=rationale, user=f"user:{user}").state})

    def request_correction(self, finding_id: str, *, note: str = "", user: str) -> None:
        self.submit("request_correction", lambda: {"finding": self._require_engine().request_correction(finding_id, user=f"user:{user}", note=note).state})

    def restore_checkpoint(self, checkpoint_id: str, *, user: str) -> None:
        self.submit("restore_checkpoint", lambda: {"revision": self._require_engine().restore_checkpoint(checkpoint_id, user=f"user:{user}").id})

    # --- concept stage (addendum R-110) ---------------------------------------------------------------------------

    def _require_concept(self) -> ConceptStage:
        self._require_engine()
        assert self.concept is not None
        return self.concept

    def _advance(self, concept: ConceptStage) -> list[str]:
        if not self.adapters:
            return []
        self._ensure_preflight(self._require_engine())
        return concept.advance()

    def concept_start(self, *, from_text: str | None = None, from_images: list[str | Path] = (), labels: list[list[str]] | None = None,
                      approval: str | None = None, views: list[str] | None = None, user: str = "user") -> None:
        def job() -> dict[str, Any]:
            concept = self._require_concept()
            self._ensure_preflight(self._require_engine())
            plan = concept.start(from_text=from_text, from_images=list(from_images), labels=labels, approval=approval, views=views,
                                 actor=f"user:{user}")
            events = self._advance(concept) if plan.state == "anchor_approved" else []
            return {"plan_id": plan.id, "canon_state": concept.plan.state if concept.plan else plan.state, "events": events}
        self.submit("concept_start", job)

    def concept_import(self, request_id: str | None, files: list[str | Path], *, vendor: str | None = None, model: str | None = None,
                       attached: list[str] | None = None, as_kind: str | None = None, view: str | None = None,
                       check: bool = True, user: str = "user") -> None:
        def job() -> dict[str, Any]:
            concept = self._require_concept()
            actor = f"user:{user}"
            if as_kind:
                refs = concept.import_as(as_kind, list(files), view=view, vendor=vendor, model=model, actor=actor)
            else:
                if not request_id:
                    raise ValueError("choose the open request the files belong to")
                refs = concept.import_files(request_id, list(files), vendor=vendor, model=model, attached=attached, actor=actor)
            events = self._advance(concept) if check else []
            return {"imported": [r.id for r in refs], "events": events}
        self.submit("concept_import", job)

    def concept_approve(self, reference_ids: list[str], *, user: str = "user") -> None:
        def job() -> dict[str, Any]:
            concept = self._require_concept()
            refs = concept.approve(list(reference_ids), actor=f"user:{user}")
            return {"approved": [r.id for r in refs], "events": self._advance(concept)}
        self.submit("concept_approve", job)

    def concept_reject(self, reference_ids: list[str], *, reason: str, user: str = "user") -> None:
        def job() -> dict[str, Any]:
            concept = self._require_concept()
            refs = concept.reject(list(reference_ids), reason=reason, actor=f"user:{user}")
            return {"rejected": [r.id for r in refs], "events": self._advance(concept)}
        self.submit("concept_reject", job)

    def concept_regenerate(self, record_id: str, *, note: str = "", user: str = "user") -> None:
        def job() -> dict[str, Any]:
            concept = self._require_concept()
            self._ensure_preflight(self._require_engine())
            gen = concept.regenerate(record_id, note=note, actor=f"user:{user}")
            return {"request_id": gen.id, "round": gen.data.get("round")}
        self.submit("concept_regenerate", job)

    def concept_proceed(self, *, user: str = "user") -> None:
        def job() -> dict[str, Any]:
            concept = self._require_concept()
            plan = concept.proceed(actor=f"user:{user}")
            return {"canon_state": plan.state, "proceeded_partial": plan.data.get("proceeded_partial")}
        self.submit("concept_proceed", job)

    def concept_abandon(self, request_id: str, *, reason: str, user: str = "user") -> None:
        self.submit("concept_abandon", lambda: {"request": self._require_concept().abandon(request_id, reason=reason, actor=f"user:{user}").state})

    def concept_failed(self, request_id: str, *, reason: str, user: str = "user") -> None:
        self.submit("concept_failed", lambda: {"request": self._require_concept().mark_failed(request_id, reason=reason, actor=f"user:{user}").state})

    def concept_study(self, part_id: str, *, view: str, purpose: str, region: str | None = None, user: str = "user") -> None:
        def job() -> dict[str, Any]:
            concept = self._require_concept()
            self._ensure_preflight(self._require_engine())
            study = concept.study(part_id, view=view, region=region, purpose=purpose, requested_by=f"user:{user}", actor=f"user:{user}")
            return {"study_id": study.id}
        self.submit("concept_study", job)

    def refresh(self) -> None:
        self.submit("refresh", lambda: {"ok": True})
