"""The engine: scheduling, the collaborative improvement loop, gates, user actions, limits, checkpoints,
and recovery (spec D1, R-7 to R-14, R-31, R-33, R-39, R-41 to R-47, R-56 to R-59, R-74 to R-76, R-85 to R-89).

Everything that decides state lives here or in the modules it calls (state, operations, ownership,
limits). Agents only ever return structured outputs that the engine validates and records.

Stages of a run (per component): intake -> brief -> plan -> build -> review -> reconcile -> correct ->
verify (loop) -> reassess (when needed) -> gate -> done.
"""
from __future__ import annotations

import json
import secrets
import shutil
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .blender.runner import script_sha256
from .config import BuilderConfig
from .evidence import EvidenceItem, PacketBuilder, changed_since
from .prompts import FRAMING
from .ids import new_id, sha256_file, utc_now
from .limits import LimitTracker
from .operations import IN_FLIGHT_STATES, OperationRequest, Operations
from .ownership import OwnershipError, OwnershipManager
from .project import Project
from .providers.base import (
    CAPABILITIES,
    InvocationRequest,
    ProviderAdapter,
    SessionRef,
    SessionRegistry,
    preflight_cache_key,
    preflight_verdict,
    report_json,
)
from .providers.structured import ParseOutcome, run_with_repair
from .records import Record
from .references import References, make_probe_image
from .render import (
    CLOSEUP_MARGIN,
    CROP_MAX_FRACTION,
    OVERVIEW_VIEWS_FOR_CROPS,
    STANDARD_VIEW_DEFS,
    ViewSpec,
    applied_settings_match,
    bbox_union,
    build_manifest,
    closeup_view_def,
    frame_camera,
    framing_contains,
    project_bbox,
    render_cache_key,
    render_is_stale,
    renders_fresh,
)
from .roles import RoleConflict, assign as assign_role
from .schemas import SCHEMAS
from .state import IllegalTransition, transition

REQUIRED_CAPABILITIES = ["image_reading", "evidence_dir_access", "read_only_enforcement", "session_create",
                         "structured_output"]
COVERAGE_VIEWS = ("front", "side", "three_quarter")
REVIEW_VIEWS = ("front", "side", "three_quarter", "three_quarter_material")
OPEN_FINDING_STATES = ("open", "correction_planned", "correcting", "verify_pending", "reassess", "evidence_gap")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
DEFAULT_FRAMING = ([0.0, 0.0, 1.0], [2.0, 2.0, 2.0])   # used only when no measurement is available
INTAKE_OBJECTIVE = ("Study the reference images independently and report what you observe about silhouette and "
                    "proportions, main masses and depth, separate pieces and construction, armour versus frame, overlaps, "
                    "gaps, joints and attachments, material differences, camera and pose ambiguities, and contradictions, "
                    "occlusion, missing views, and uncertainty. Mark every statement observed, inferred, or uncertain.")
REVIEW_OBJECTIVE = ("Inspect the current model renders against the references. Report specific mismatches per part, "
                    "region, and view, with severity and confidence as separate judgements, the evidence you used, a "
                    "proposed correction, and the observable improvement you expect. Record which parts and views you "
                    "covered. You are not told what the builder intended; judge the renders.")


class EngineFailure(Exception):
    pass


class _TransportFailure(Exception):
    """The provider process did not deliver a parsable reply (timeout, cancel, crash, provider error).
    Never answered with a repair round: a repair is for malformed content, and re-dispatching a cancelled
    or failed call is a transport retry, which builder invocations do not get (R-5, R-23)."""

    def __init__(self, result: Any):
        super().__init__(result.error)
        self.result = result


class LimitReached(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class Engine:
    def __init__(self, project: Project, config: BuilderConfig, adapters: dict[str, ProviderAdapter], runner: Any,
                 *, observer: Callable[[dict[str, Any]], None] | None = None):
        self.project = project
        self.store = project.store
        self.config = config
        self.adapters = dict(adapters)
        self.runner = runner
        self.observer = observer
        self.ownership = OwnershipManager(self.store)
        self.operations = Operations(project, runner, self.ownership)
        self.sessions = SessionRegistry(self.store)
        self.packets = PacketBuilder(project)
        self.references = References(project)
        self.limits = LimitTracker(config.limits, cost_enforced_by_provider={})
        self.run: Record | None = None
        self.agents: dict[str, Record] = {}
        self.blender_info: dict[str, Any] = {}
        self.blender_version = "unknown"
        self.preflight_reports: dict[str, dict[str, Any]] = {}
        self._probe_spend: dict[str, dict[str, Any]] = {}
        self.cancel_event = threading.Event()
        self._pause_requested = False
        self._render_script_sha = script_sha256("render.py")
        self._bbox_cache: dict[str, dict[str, Any]] = {}
        self._ensure_agents()

    # ------------------------------------------------------------------ agents

    def _labels(self) -> list[str]:
        return [label for label in self.config.agents if label in self.adapters]

    def _ensure_agents(self) -> None:
        for rec in self.store.list("agent"):
            self.agents[rec.data.get("label", rec.id)] = rec
        for label in self._labels():
            binding = self.config.agents[label]
            aid = f"ag_{label}"
            scratch = self.project.path("agents", label, "work")
            scratch.mkdir(parents=True, exist_ok=True)
            rec = self.store.get("agent", aid)
            if rec is None:
                rec = Record.new("agent", {
                    "label": label, "provider": binding.provider, "model": binding.model, "reasoning": binding.reasoning,
                    "executable": binding.executable, "scratch_dir": str(scratch), "context_contains": [],
                    "preflight_report_id": None,
                }, id=aid)
                rec = self.store.upsert(rec, actor="engine", event="agent.created")
            self.agents[label] = rec

    def _label_of(self, agent_id: str) -> str:
        for label, rec in self.agents.items():
            if rec.id == agent_id:
                return label
        raise EngineFailure(f"unknown agent id {agent_id}")

    def _other(self, label: str) -> str:
        labels = self._labels()
        if len(labels) < 2:
            raise EngineFailure("two agents are required (R-7)")
        return next(x for x in labels if x != label)

    def _tag(self, label: str, tag: str) -> None:
        agent = self.agents[label]
        tags = list(agent.data.get("context_contains") or [])
        if tag not in tags:
            tags.append(tag)
            agent.data["context_contains"] = tags
            self.agents[label] = self.store.upsert(agent, actor="engine", event="agent.context_tagged", inputs={"tag": tag})

    def _emit(self, event: str, **data: Any) -> None:
        if self.observer is not None:
            try:
                self.observer({"event": event, "at": utc_now(), **data})
            except Exception:  # noqa: BLE001 - observers never break the engine
                pass

    # ---------------------------------------------------------------- preflight

    def preflight(self, live: bool = False, labels: list[str] | None = None) -> dict[str, Any]:
        """Three tiers per agent (R-15). ``labels`` limits the probes to some agents; the others keep their stored
        report (``load_preflight`` still decides whether that report is current, R-18)."""
        report: dict[str, Any] = {}
        if self.runner is not None:
            self.blender_info = self.runner.smoke()
            self.blender_version = str(self.blender_info.get("blender_version", "unknown"))
            report["blender"] = dict(self.blender_info)
        unknown = [x for x in (labels or []) if x not in self._labels()]
        if unknown:
            raise EngineFailure(f"unknown agent label(s) {unknown}; configured: {self._labels()}")
        for label in self._labels():
            if labels and label not in labels:
                continue
            adapter = self.adapters[label]
            agent = self.agents[label]
            self._probe_spend[label] = {"measured": 0.0, "estimated": 0.0, "unknown_invocations": 0}
            declared = adapter.declared_capabilities()
            local = adapter.preflight_local()
            caps = {cap: {"declared": declared.get(cap, "no"), **self._local_tier(adapter, local, cap), "live": "not_run"}
                    for cap in CAPABILITIES}
            if live:
                caps["image_reading"].update(self._probe_image(label))
                caps["evidence_dir_access"].update(caps["image_reading"])
                caps["model_settings_applied"].update(self._settings_evidence(label))
                caps["read_only_enforcement"].update(self._probe_write(label))
                session = self._probe_session(label)
                caps["session_create"].update(session["create"])
                caps["session_resume"].update(session["resume"])
                # the live tier of session_resume decides whether later invocations resume or reconstruct context
                self.preflight_reports[label] = {"capabilities": {"session_resume": caps["session_resume"]}}
                caps["structured_output"].update({"live": "verified" if caps["image_reading"]["live"] == "verified"
                                                  else "failed", "evidence": "probe outputs validated against schema"})
                caps["usage_reporting"].update(self._usage_evidence(label))
                caps["cancellation"].update(self._probe_cancel(label))
            ok, blockers = preflight_verdict(caps, REQUIRED_CAPABILITIES, require_live=live, shared_write=False)
            cache_key = preflight_cache_key(cli_path=str(local.get("cli_path")), version=str(local.get("version")),
                                            model=agent.data["model"],
                                            settings={"reasoning": agent.data["reasoning"], **adapter.settings_signature()})
            entry = {"agent_id": agent.id, "label": label, "provider": agent.data["provider"], "model": agent.data["model"],
                     "reasoning": agent.data["reasoning"], "capabilities": caps, "local": local, "ok": ok,
                     "blockers": blockers, "cache_key": cache_key, "live": live, "at": utc_now(),
                     "effective_settings": self._last_effective(label),
                     "reasoning_fallbacks": list(getattr(adapter, "fallbacks_reported", []) or []),
                     "probe_cost": dict(self._probe_spend.get(label) or {"measured": 0.0, "estimated": 0.0, "unknown_invocations": 0})}
            rec = self.store.upsert(Record.new("preflight_report", entry), actor="engine", event="preflight.recorded")
            agent.data["preflight_report_id"] = rec.id
            self.agents[label] = self.store.upsert(agent, actor="engine", event="agent.preflight")
            report[label] = entry
            self.preflight_reports[label] = entry
        if not labels or "I" in labels:
            report["I"] = self._preflight_image_seat()
            self.preflight_reports["I"] = report["I"]
        self._emit("preflight", report=report)
        return report

    def _preflight_image_seat(self) -> dict[str, Any]:
        """Addendum R-109: the image seat's three tiers. Manual seat: declared (manual, intended vendor), local (import
        directory writable, flow confirmed once), live (not applicable; provenance is as declared by the owner and
        consistency is checked by both LLM seats)."""
        from .providers.imagegen import make_image_seat

        seat = make_image_seat(self.config.image_generation)
        plans = self.store.list("concept_plan")
        confirmed = bool(plans and plans[-1].data.get("flow_confirmed_at"))
        import_dir = Path(self.config.concept.get("import_dir") or self.project.path("concept", "imports"))
        entry = seat.preflight(import_dir=import_dir, flow_confirmed=confirmed)
        entry.update({"agent_id": "I", "label": "I", "seat": "I", "provider": seat.name, "model": entry["declared"].get("model"),
                      "reasoning": "", "capabilities": {}, "blockers": entry.get("blockers") or [], "live_probes": False})
        rec = self.store.upsert(Record.new("preflight_report", entry), actor="engine", event="preflight.recorded",
                                inputs={"seat": "I"})
        entry["report_id"] = rec.id
        return entry

    def _note_probe_spend(self, label: str, usage: Any) -> None:
        from .records import Estimated, Measured

        entry = self._probe_spend.setdefault(label, {"measured": 0.0, "estimated": 0.0, "unknown_invocations": 0})
        cost = usage.cost_usd
        if isinstance(cost, Measured):
            entry["measured"] = round(entry["measured"] + float(cost.value), 6)
        elif isinstance(cost, Estimated):
            entry["estimated"] = round(entry["estimated"] + float(cost.value), 6)
        else:
            entry["unknown_invocations"] += 1

    def _seed_probe_spend(self, tracker: LimitTracker) -> None:
        """Carry the stored preflight probe spend of every agent into ``tracker`` (design 12b item 16 follow-up)."""
        for label, entry in self.preflight_reports.items():
            pc = entry.get("probe_cost") if isinstance(entry, dict) else None
            if pc:
                tracker.add_external("preflight_probe", measured=float(pc.get("measured") or 0.0),
                                     estimated=float(pc.get("estimated") or 0.0),
                                     unknown_invocations=int(pc.get("unknown_invocations") or 0))

    def _last_usage_known(self, label: str) -> bool:
        invs = [i for i in self.store.list("invocation") if i.data.get("agent_id") == self.agents[label].id]
        return bool(invs) and any(v.get("kind") != "unknown" for v in (invs[-1].data.get("usage") or {}).values())

    def _last_invocation(self, label: str) -> Record | None:
        invs = [i for i in self.store.list("invocation") if i.data.get("agent_id") == self.agents[label].id]
        return invs[-1] if invs else None

    def _last_effective(self, label: str) -> dict[str, Any]:
        """Effective settings from the most recent invocation that produced a parsed reply (a cancelled or failed
        probe carries none), else from the most recent invocation of any outcome."""
        invs = [i for i in self.store.list("invocation") if i.data.get("agent_id") == self.agents[label].id]
        for inv in reversed(invs):
            eff = inv.data.get("effective_settings") or {}
            if inv.state == "completed" and eff.get("model_reported") is not None:
                return dict(eff)
        return dict(invs[-1].data.get("effective_settings") or {}) if invs else {}

    @staticmethod
    def _local_tier(adapter: ProviderAdapter, local: dict[str, Any], cap: str) -> dict[str, Any]:
        """R-15 local tier: the executable is present and every ``--help`` token the adapter relies on for this
        capability is listed (R-19: flags come from ``--help``, never from memory)."""
        needed = (adapter.local_requirements() or {}).get(cap, [])
        flags = local.get("flags") or {}
        missing = [t for t in needed if not flags.get(t)]
        if not local.get("found"):
            return {"local": "missing", "local_evidence": f"{adapter.name} executable not found"}
        if missing:
            return {"local": "missing", "local_evidence": f"--help does not list: {', '.join(missing)}"}
        return {"local": "ok", "local_evidence": (f"listed in --help: {', '.join(needed)}" if needed
                                                  else "no CLI flag involved (process layer or adapter logic)")}

    def _settings_evidence(self, label: str) -> dict[str, Any]:
        """R-20: the effective model and reasoning setting as the adapter reported them after the first live call."""
        eff = self._last_effective(label)
        inv = self._last_invocation(label)
        if inv is None or inv.state != "completed":
            return {"live": "failed", "evidence": f"first live invocation did not complete: {inv.data.get('error') if inv else 'none'}"}
        requested, effective = eff.get("reasoning_requested"), eff.get("reasoning_effective")
        text = (f"model requested {eff.get('model_requested')!r}, reported {eff.get('model_reported', 'unknown')!r}; "
                f"reasoning requested {requested!r}, effective {effective!r} ({eff.get('reasoning_reported', 'unknown')})")
        if eff.get("reasoning_rejection"):
            text += f"; the CLI rejected {requested!r}: {str(eff['reasoning_rejection'])[:200]}"
        return {"live": "verified", "evidence": text, "effective": {"model_reported": eff.get("model_reported"),
                                                                     "reasoning_effective": effective}}

    def _usage_evidence(self, label: str) -> dict[str, Any]:
        """R-25: usage fields are read from the CLI's own output; nothing recognised means unknown, never zero."""
        inv = self._last_invocation(label)
        usage = (inv.data.get("usage") or {}) if inv else {}
        known = sorted(k for k, v in usage.items() if v.get("kind") != "unknown")
        eff = self._last_effective(label)
        raw = eff.get("usage_raw") or eff.get("stats_raw")
        if known:
            return {"live": "verified", "evidence": f"fields read from the CLI output: {', '.join(known)}"}
        return {"live": "not_verified", "evidence": "no usage field recognised in the CLI output; consumption recorded as unknown"
                                                   + (f"; raw fields seen: {sorted(raw)[:12]}" if isinstance(raw, dict) else "")}

    def _probe_dir(self, label: str, name: str) -> Path:
        d = self.project.path("agents", label, "probes", name)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _probe_image(self, label: str) -> dict[str, Any]:
        pdir = self._probe_dir(label, "image")
        expected = make_probe_image(pdir / "probe.png", shape="triangle", color="red", number=7)
        packet, packet_dir = self.packets.build(
            kind="probe", agent_id=self.agents[label].id, task=None,
            objective="Look at the probe image in evidence/ and report its shape, its color, and the printed number.",
            brief_text="(preflight probe; no model yet)", evidence=[EvidenceItem(pdir / "probe.png", "reference", {"label": "probe"})],
            open_findings=[], constraints=[], withheld=[], schema_name="probe_report", schema=SCHEMAS["probe_report"], changed=None)
        outcome, inv = self._invoke(label, "probe", packet=packet, pdir=packet_dir, schema_name="probe_report",
                                    images=[packet_dir / f["path"] for f in packet.data["files"] if f["role"] == "reference"],
                                    extra={"expected": expected}, count_toward_limits=False)
        if not outcome.ok:
            return {"live": "failed", "evidence": f"probe output invalid: {outcome.errors}"}
        got = outcome.value
        if all(got.get(k) == v for k, v in expected.items()):
            return {"live": "verified", "evidence": f"probe reported {got}"}
        return {"live": "failed", "evidence": f"probe reported {got}, expected {expected}"}

    def _probe_write(self, label: str) -> dict[str, Any]:
        agent = self.agents[label]
        target = Path(agent.data["scratch_dir"]) / f"write-probe-{secrets.token_hex(4)}.txt"
        packet, packet_dir = self.packets.build(
            kind="write_probe", agent_id=agent.id, task=None,
            objective=f"Try to create the file {target} containing the text 'probe'. Actually attempt the write with a "
                      "tool if any tool could write. Then report exactly what happened as `outcome`: `written` (the file "
                      "now exists), `attempted_and_refused` (you tried and the tool or sandbox refused), "
                      "`no_write_tool_available` (no tool in this session can write files, so no attempt was possible), or "
                      "`could_not_attempt` (anything else, for example you could not read this packet). Put the error text "
                      "in `error_text`.",
            brief_text="(preflight probe)", evidence=[], open_findings=[], constraints=[], withheld=[],
            schema_name="write_probe_report", schema=SCHEMAS["write_probe_report"], changed=None)
        outcome, inv = self._invoke(label, "write_probe", packet=packet, pdir=packet_dir, schema_name="write_probe_report",
                                    extra={"attempted_path": str(target)}, count_toward_limits=False)
        exists = target.exists()
        if exists:
            target.unlink(missing_ok=True)
        if not outcome.ok:
            return {"live": "failed", "evidence": f"probe output invalid: {outcome.errors}; file exists={exists}"}
        if exists or outcome.value.get("write_succeeded"):
            return {"live": "failed", "evidence": f"agent wrote a file under restricted mode (exists={exists}, reported={outcome.value})"}
        how = outcome.value.get("outcome")
        if how == "written":
            return {"live": "failed", "evidence": f"agent reports the file was written but it is absent: {outcome.value}"}
        if how == "could_not_attempt":
            return {"live": "not_verified", "evidence": ("the agent could not attempt the write "
                                                         f"({outcome.value.get('error_text', '')!r}); enforcement not exercised (R-17)")}
        return {"live": "verified", "evidence": f"{how}: {outcome.value.get('error_text', '')!r}; file absent"}

    def _probe_cancel(self, label: str) -> dict[str, Any]:
        """R-22: cancel a real invocation part-way and confirm the whole process tree is gone."""
        agent = self.agents[label]
        packet, pdir = self.packets.build(
            kind="cancel_probe", agent_id=agent.id, task=None,
            objective="Preflight cancellation probe. Work slowly: list the integers from 1 to 400 one per line, "
                      "reflecting on each, and only then reply per the output contract with nonce set to 'cancel'.",
            brief_text="(preflight probe)", evidence=[], open_findings=[], constraints=[], withheld=[],
            schema_name="session_probe_report", schema=SCHEMAS["session_probe_report"], changed=None)
        delay = float(self.config.provider_timeouts.get("cancel_probe_after") or 8)
        event = threading.Event()
        timer = threading.Timer(delay, event.set)
        timer.start()
        try:
            outcome, inv = self._invoke(label, "cancel_probe", packet=packet, pdir=pdir, schema_name="session_probe_report",
                                        extra={"nonce": "cancel"}, count_toward_limits=False, cancel_event=event)
        finally:
            timer.cancel()
        last = (inv.data.get("results") or [{}])[-1]
        if last.get("outcome") == "cancelled":
            if last.get("kill_confirmed"):
                return {"live": "verified", "evidence": f"cancelled after {delay:g}s; process tree confirmed gone"}
            return {"live": "failed", "evidence": "cancelled but the process tree was not confirmed gone (R-22)"}
        if last.get("outcome") == "ok":
            return {"live": "not_verified", "evidence": (f"the CLI answered in {last.get('elapsed_s', 0):.1f}s, before the cancel "
                                                         f"fired at {delay:g}s; tree kill is proven by the process-layer tests")}
        return {"live": "failed", "evidence": f"probe ended with {last.get('outcome')}: {last.get('error')}"}

    def _probe_session(self, label: str) -> dict[str, dict[str, Any]]:
        agent = self.agents[label]
        nonce = secrets.token_hex(6)
        packet, packet_dir = self.packets.build(
            kind="session_probe", agent_id=agent.id, task=None,
            objective=f"Remember this nonce: {nonce}. Reply with it in the nonce field.", brief_text="(preflight probe)",
            evidence=[], open_findings=[], constraints=[], withheld=[], schema_name="session_probe_report",
            schema=SCHEMAS["session_probe_report"], changed=None)
        first, _ = self._invoke(label, "session_probe", packet=packet, pdir=packet_dir, schema_name="session_probe_report",
                                extra={"nonce": nonce}, count_toward_limits=False)
        create = {"live": "verified" if first.ok and first.value.get("nonce") == nonce else "failed",
                  "evidence": f"first reply {first.value}"}
        packet2, packet_dir2 = self.packets.build(
            kind="session_probe", agent_id=agent.id, task=None,
            objective="Reply with the nonce you were given earlier in this session, in the nonce field.",
            brief_text="(preflight probe)", evidence=[], open_findings=[], constraints=[], withheld=[],
            schema_name="session_probe_report", schema=SCHEMAS["session_probe_report"], changed=None)
        second, inv2 = self._invoke(label, "session_probe", packet=packet2, pdir=packet_dir2,
                                    schema_name="session_probe_report", extra={"nonce": nonce}, count_toward_limits=False)
        resumed = inv2.data.get("session_ref_kind") == "resume"
        resume = {"live": "verified" if (second.ok and second.value.get("nonce") == nonce and resumed) else "failed",
                  "evidence": f"second reply {second.value} via {inv2.data.get('session_ref_kind')} session"}
        return {"create": create, "resume": resume}

    # -------------------------------------------------------------- invocation

    def _resume_allowed(self, label: str) -> bool:
        """Resume only when the adapter declares it and the live probe did not fail; otherwise every invocation
        starts a fresh session and the packet carries the reconstructed context (R-10, R-79)."""
        declared = self.adapters[label].declared_capabilities().get("session_resume", "no")
        live = (((self.preflight_reports.get(label) or {}).get("capabilities") or {}).get("session_resume") or {}).get("live")
        return declared != "no" and live != "failed"

    def _invoke(self, label: str, purpose: str, *, packet: Record | None, pdir: Path, schema_name: str,
                images: list[Path] | tuple[Path, ...] = (), extra: dict[str, Any] | None = None,
                session_kind: str = "persistent", isolated_reason: str = "", prompt: str | None = None,
                count_toward_limits: bool = True, cancel_event: threading.Event | None = None) -> tuple[Any, Record]:
        agent = self.agents[label]
        adapter = self.adapters[label]
        if session_kind == "isolated":
            session = self.sessions.isolated(agent.id, provider=agent.data["provider"], reason=isolated_reason)
        else:
            session = self.sessions.persistent(agent.id, provider=agent.data["provider"])
        resume_allowed = self._resume_allowed(label)
        inv = Record.new("invocation", {
            "agent_id": agent.id, "purpose": purpose, "packet_id": packet.id if packet else None,
            "session_id": session.id, "session_uuid": session.data["session_uuid"], "session_kind": session.data["kind"],
            "schema_name": schema_name, "started_at": utc_now(), "outcome": None, "usage": {}, "repair_rounds": 0,
            "argv": [], "error": "", "effective_settings": {}, "results": [], "session_ref_kind": None,
        }, state="running")
        inv = self.store.upsert(inv, actor="engine", event="invocation.started", run_id=self._run_id())
        results: list[Any] = []
        default_prompt = (f"Read PACKET.md in the packet directory ({pdir}) and follow its output contract. "
                          f"Purpose: {purpose}.")
        self._execution("waiting_for_provider")

        def call(repair_prompt: str | None) -> tuple[dict[str, Any] | None, str | None]:
            if count_toward_limits:
                ok, reason, msg = self.limits.can_dispatch("request", agent_id=agent.id)
                if not ok:
                    raise LimitReached(reason or "budget_limit", msg)
            current = self.store.require("provider_session", session.id)
            ref = self.sessions.ref_for(current, first_use=(not results and int(current.data.get("invocation_count") or 0) == 0))
            if ref.kind == "resume" and not resume_allowed:
                ref = SessionRef(kind="new", uuid=str(uuid.uuid4()))
                inv.data["context_reconstructed"] = True     # provider cannot resume by explicit id: packet carries context
            if not results:
                inv.data["session_ref_kind"] = ref.kind
            inv.data.setdefault("session_refs", []).append({"kind": ref.kind, "uuid": ref.uuid})
            req = InvocationRequest(
                agent_id=agent.id, session=ref, purpose=purpose, packet_dir=pdir, cwd=Path(agent.data["scratch_dir"]),
                prompt_text=repair_prompt or prompt or default_prompt, schema_name=schema_name,
                output_schema=SCHEMAS[schema_name], model=agent.data["model"], reasoning=agent.data["reasoning"],
                framing_text=FRAMING, images=[Path(p) for p in images],
                response_s=float(self.config.provider_timeouts["response"]),
                inactivity_s=float(self.config.provider_timeouts["inactivity"]),
                max_cost_usd=self.limits.remaining_budget_usd(), extra=dict(extra or {}))
            if count_toward_limits:
                self.limits.note_dispatch("request", agent_id=agent.id)
            # measured at dispatch for the efficiency harness (R-84): what this call delivered besides the packet files
            delivered = inv.data.setdefault("delivered", {"prompt_bytes": 0, "framing_bytes": 0, "images": 0, "reply_bytes": 0, "rounds": 0})
            delivered["prompt_bytes"] += len(req.prompt_text.encode("utf-8"))
            delivered["framing_bytes"] += len((req.framing_text or "").encode("utf-8"))
            delivered["images"] += len(req.images)
            delivered["rounds"] += 1
            res = adapter.invoke(req, cancel_event=cancel_event or self.cancel_event)
            delivered["reply_bytes"] += len((res.raw_text or "").encode("utf-8"))
            if count_toward_limits:
                self.limits.note_complete("request", agent_id=agent.id, usage=res.usage)
            else:
                # a preflight probe is not a modeling request but its cost is real: it counts toward the monetary cap
                self.limits.note_external_cost("preflight_probe", res.usage)
                self._note_probe_spend(label, res.usage)
            results.append(res)
            if res.outcome == "ok":
                # only a successful call advances the session: a failed `new` never becomes a `resume` (R-10)
                self.sessions.mark_used(current, journal_seq=self.store.last_seq(), reported_session_id=res.reported_session_id)
            else:
                self.store.append_journal(actor="engine", event="provider_session.not_advanced", run_id=self._run_id(),
                                          inputs={"session_id": current.id, "invocation_id": inv.id, "outcome": res.outcome})
            if res.raw_text and ("[[ASK:" in res.raw_text or "[[TOOL:" in res.raw_text):
                self.store.append_journal(actor="engine", event="invocation.directive_ignored",
                                          inputs={"invocation_id": inv.id, "note": "ASK/TOOL syntax logged and ignored (R-13)"})
            if res.outcome != "ok":
                raise _TransportFailure(res)
            return res.structured, res.raw_text

        try:
            try:
                outcome, rounds = run_with_repair(call, SCHEMAS[schema_name], schema_name)
            except _TransportFailure as exc:
                outcome = ParseOutcome(ok=False, value=None, source="none",
                                       errors=[f"provider invocation {exc.result.outcome}: {exc.result.error}"])
                rounds = 0
                budget_stop = exc.result.outcome == "budget_exhausted"
            else:
                budget_stop = False
        finally:
            self._execution("running")
        last = results[-1] if results else None
        if last is None or last.outcome != "ok":
            inv_outcome = last.outcome if last else "spawn_failed"
            inv.state = "failed"
        elif outcome.ok:
            inv_outcome = "ok" if rounds == 0 else "repaired"
            inv.state = "completed"
        else:
            inv_outcome = "malformed" if rounds == 0 else "repair_failed"
            inv.state = "failed"
        inv.data.update({
            "outcome": inv_outcome, "repair_rounds": rounds, "usage": (last.usage.to_json() if last else {}),
            "argv": last.argv if last else [], "error": (last.error if last and last.error else "; ".join(outcome.errors)),
            "effective_settings": last.effective_settings if last else {}, "ended_at": utc_now(),
            "elapsed_s": sum(r.elapsed_s for r in results), "parse_errors": outcome.errors, "parse_source": outcome.source,
            "structured_output": outcome.value, "results": [report_json(r) for r in results],
        })
        inv = self.store.upsert(inv, actor="engine", event=f"invocation.{inv.state}", run_id=self._run_id())
        self._emit("invocation", label=label, purpose=purpose, outcome=inv_outcome, invocation_id=inv.id)
        if budget_stop:
            # the provider's cap stopped the call after the spend (R-85): stop the run as a budget limit, not a failure
            raise LimitReached("budget_limit", f"agent {label} ({purpose}): {last.error if last else 'provider budget cap'}")
        return outcome, inv

    # ------------------------------------------------------------- run control

    def _run_id(self) -> str | None:
        return self.run.id if self.run else None

    def _execution(self, state: str) -> None:
        """Best-effort execution sub-state changes around provider and Blender work (R-4)."""
        if self.run is None or self.run.state == state:
            return
        try:
            self.run = transition(self.store, self.run, state, actor="engine", reason=f"engine {state}")
        except IllegalTransition:
            pass

    def resolve_assignments(self, overrides: dict[str, str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
        """Merge the config key ``builder.assignments`` with explicit overrides (which win); validate roles and seats
        strictly (an explicit override that names an unknown role or seat is an error, never guessed)."""
        from .config import ASSIGNABLE_ROLES

        merged = dict(self.config.assignments)
        sources = {role: "config" for role in merged}
        for role, seat in (overrides or {}).items():
            if role not in ASSIGNABLE_ROLES:
                raise EngineFailure(f"unknown assignment role {role!r}; expected one of {', '.join(ASSIGNABLE_ROLES)}")
            if seat not in self._labels():
                raise EngineFailure(f"assignment {role}={seat!r}: not a configured seat ({', '.join(self._labels())})")
            merged[role] = seat
            sources[role] = "cli"
        return merged, sources

    @staticmethod
    def override_rationale(role: str, seat: str, source: str) -> str:
        return (f"user override ({source}: {role}={seat}) (R-8); R-107 still applies: {seat} never reviews, verifies, "
                f"or reassesses its own operation")

    def start(self, *, attended: bool | None = None, component_name: str | None = None,
              assignment_overrides: dict[str, str] | None = None) -> Record:
        if not self.preflight_reports:
            raise RuntimeError("preflight has not been run; run preflight before start (R-21)")
        blockers = {label: r["blockers"] for label, r in self.preflight_reports.items() if not r["ok"] and label != "I"}
        if blockers:
            raise RuntimeError("preflight blocks the run: " + json.dumps(blockers, ensure_ascii=False))
        local_only = [label for label in self._labels()
                      if not (self.preflight_reports.get(label) or {}).get("live") and getattr(self.adapters[label], "name", "") != "mock"]
        if local_only:
            raise EngineFailure(f"agent(s) {', '.join(local_only)} have only a local (free) preflight report: the live tiers were "
                                "never run, so image reading, read-only enforcement, and sessions are unverified (R-21). "
                                "Run `preflight --live` first; it spends provider usage and is never started implicitly.")
        attended = self.config.attended if attended is None else attended
        overrides, override_sources = self.resolve_assignments(assignment_overrides)
        self.limits = LimitTracker(self.config.limits, cost_enforced_by_provider={
            label: self.adapters[label].declared_capabilities().get("cost_cap") == "yes" for label in self._labels()})
        self._seed_probe_spend(self.limits)
        run = Record.new("run", {
            "project_id": self.project.record.id, "attended": attended, "stage": "intake", "component_id": None,
            "stop_reason": None, "notes": [], "started_at": utc_now(), "limits": dict(self.config.limits),
            "blender_version": self.blender_version, "agents": {label: a.id for label, a in self.agents.items()},
            "assignment_overrides": overrides, "assignment_override_sources": override_sources,
        })
        run = self.store.upsert(run, actor="user", event="run.created",
                                inputs={"assignment_overrides": overrides, "sources": override_sources} if overrides else None)
        comp = self._component(create=True, name=component_name)
        run.data["component_id"] = comp.id
        self.run = transition(self.store, run, "running", actor="engine", reason="start", context={"preflight_ok": True})
        self._emit("run.started", run_id=self.run.id)
        return self.run

    def attach(self) -> Record | None:
        runs = self.store.list("run")
        self.run = runs[-1] if runs else None
        if self.run is not None:
            self.limits = LimitTracker(self.run.data.get("limits") or self.config.limits, cost_enforced_by_provider={
                label: self.adapters[label].declared_capabilities().get("cost_cap") == "yes" for label in self._labels()})
            self.limits.restore(self.run.data.get("consumption"))
            self.blender_version = self.run.data.get("blender_version", self.blender_version)
        return self.run

    def load_preflight(self) -> dict[str, dict[str, Any]]:
        """Reuse stored preflight reports whose cache key still matches (R-18); returns the labels loaded."""
        loaded: dict[str, dict[str, Any]] = {}
        for label in self._labels():
            agent = self.agents[label]
            local = self.adapters[label].preflight_local()
            key = preflight_cache_key(cli_path=str(local.get("cli_path")), version=str(local.get("version")),
                                      model=agent.data["model"],
                                      settings={"reasoning": agent.data["reasoning"], **self.adapters[label].settings_signature()})
            reports = [r for r in self.store.list("preflight_report") if r.data.get("agent_id") == agent.id]
            if reports and reports[-1].data.get("cache_key") == key:
                loaded[label] = dict(reports[-1].data)
                self.preflight_reports[label] = loaded[label]
        if self.run is None and not self.limits.external_costs:
            self._seed_probe_spend(self.limits)
        if self.runner is not None and not self.blender_info:
            self.blender_info = self.runner.smoke()
            self.blender_version = str(self.blender_info.get("blender_version", "unknown"))
        return loaded

    def recover(self) -> dict[str, Any]:
        """R-47: classify in-flight operations from records, files, and live processes; never replay; return tasks and
        findings the interruption left mid-flight to a state the loop can resume from; stop if anything is uncertain."""
        report = self.operations.reconcile_all(actor="engine:recovery")
        tasks_reset, findings_reset = self._reset_interrupted(actor="engine:recovery", reason="restart")
        uncertain = [r for r in report if r["classification"] == "uncertain"]
        if self.run is not None and self.run.state in ("running", "waiting_for_provider", "rendering", "waiting_for_user", "paused"):
            if self.run.state != "paused":
                self.run = transition(self.store, self.run, "recovering", actor="engine:recovery", reason="restart")
            else:
                self.run = transition(self.store, self.run, "recovering", actor="engine:recovery", reason="restart")
            if uncertain:
                self.run = transition(self.store, self.run, "paused", actor="engine:recovery",
                                      reason=f"{len(uncertain)} uncertain operation(s) quarantined", stop_reason="execution_failure")
            else:
                self.run = transition(self.store, self.run, "paused", actor="engine:recovery",
                                      reason="recovered; resume to continue", stop_reason="user_pause")
        return {"operations": report, "uncertain": len(uncertain), "tasks_reset": tasks_reset, "findings_reset": findings_reset}

    def _reset_interrupted(self, *, actor: str, reason: str) -> tuple[list[str], list[str]]:
        """Tasks still ``in_progress`` and findings still ``correcting`` after an interruption (a crash, a failed or
        uncertain operation) return to a resumable state with a recovery note, so the loop retries the work as a new
        attempt against the unchanged base revision instead of assuming it happened (R-23, R-47)."""
        tasks_reset: list[str] = []
        for task in self.store.list("task", state="in_progress"):
            ops = [self.store.get("operation", oid) for oid in task.data.get("op_ids") or []]
            states = {o.state for o in ops if o is not None}
            note = (f"interrupted ({reason}) while in progress; operations {sorted(states) or 'none'}; nothing from this task "
                    "is assumed to have applied")
            self._finish_task(task, "blocked", error=note, interrupted=True)
            tasks_reset.append(task.id)
        # R-44: an interrupted holder's pending writes are cancelled (quarantined above), so its grant is released and
        # the retry acquires a fresh token bound to the unchanged base revision
        holder = self.ownership.current("assembly")
        if holder is not None:
            owner_tasks = [t for t in self.store.list("task") if t.data.get("owner_agent_id") == holder.data["holder"]]
            inflight = any(o.state in IN_FLIGHT_STATES for o in self.store.list("operation"))
            if owner_tasks and owner_tasks[-1].state == "blocked" and not inflight:
                self.ownership.release(holder.data["token"], actor=actor,
                                       reason=f"holder's task {owner_tasks[-1].id} was interrupted ({reason}); released for the retry")
        findings_reset: list[str] = []
        for f in self.store.list("finding", state="correcting"):
            attempt_id = (f.data.get("attempts") or [None])[-1]
            attempt = self.store.get("correction_attempt", attempt_id) if attempt_id else None
            op_ids: list[str] = []
            if attempt is not None:
                task = self.store.get("task", attempt.data.get("task_id", ""))
                op_ids = list((task.data.get("op_ids") if task else None) or attempt.data.get("op_ids") or [])
                if attempt.state in ("started", "uncertain") or attempt.state not in ("improved", "unchanged", "regressed"):
                    attempt.state = "uncertain"
                    attempt.data["op_ids"] = op_ids
                    attempt.data["interrupted"] = reason
                    self.store.upsert(attempt, actor=actor, event="correction_attempt.uncertain", run_id=self._run_id())
                if task is not None and task.state == "in_progress":
                    self._finish_task(task, "blocked", error=f"interrupted ({reason}) during correction of {f.id}", interrupted=True)
                    tasks_reset.append(task.id)
            ops = [self.store.get("operation", oid) for oid in op_ids]
            outcomes = ", ".join(f"{o.id} {o.state}" for o in ops if o is not None) or "no operation was recorded"
            note = {"at": utc_now(), "reason": reason, "task_id": attempt.data.get("task_id") if attempt else None,
                    "attempt_id": attempt_id, "op_ids": op_ids,
                    "note": f"correction attempt interrupted or uncertain ({outcomes}); the base revision is unchanged and "
                            "the next attempt starts from it; nothing from the interrupted attempt is assumed to exist"}
            f.state = "open"
            f.data.setdefault("recovery_notes", []).append(note)
            self.store.upsert(f, actor=actor, event="finding.reset_after_interruption", run_id=self._run_id(),
                              inputs={"attempt_id": attempt_id, "op_ids": op_ids, "reason": reason})
            findings_reset.append(f.id)
        return tasks_reset, findings_reset

    def _interrupted_ops_section(self, base_rev: Record) -> dict[str, str]:
        """Packet section naming every operation attempted against ``base_rev`` that ended uncertain, cancelled, or
        interrupted: never promoted, so the agent must not assume its edits exist (R-47)."""
        lines: list[str] = []
        for o in self.store.list("operation"):
            if o.data.get("expected_base_revision_id") != base_rev.id:
                continue
            interrupted = o.state in ("uncertain", "cancelled") or (o.state == "failed" and o.data.get("recovery") == "interrupted")
            if not interrupted:
                continue
            agent = o.data.get("agent_id")
            label = next((lab for lab, a in self.agents.items() if a.id == agent), agent)
            where = o.data.get("quarantine_dir")
            lines.append(f"- {o.id} by agent {label}, intent: {o.data.get('intent')}; ended {o.state}: {o.data.get('error')}. "
                         f"Staged output {'quarantined at ' + str(where) if where else 'none'}; never promoted; the base revision "
                         f"{base_rev.id} is unchanged. Do not assume any of its edits exist; start from the evidence in this packet.")
        if not lines:
            return {}
        return {"Interrupted operations against the current base revision (never applied)": "\n".join(lines)}

    def pause(self, user: str = "user") -> None:
        self._pause_requested = True

    def resume(self, user: str = "user") -> Record:
        if self.run is None:
            raise EngineFailure("no run to resume")
        ok, reason, msg = self.limits.can_dispatch()
        self.run = transition(self.store, self.run, "running", actor=user, reason="resume", context={"limit_reached": not ok})
        self._pause_requested = False
        return self.run

    def cancel(self, user: str = "user") -> Record:
        if self.run is None:
            raise EngineFailure("no run to cancel")
        self.cancel_event.set()
        self.run = transition(self.store, self.run, "cancelled", actor=user, reason="cancel", stop_reason="user_cancel",
                              context={"inflight_reconciled": not any(o.state in IN_FLIGHT_STATES
                                                                       for o in self.store.list("operation"))})
        return self.run

    def feedback(self, text: str, user: str = "user") -> Record:
        rec = Record.new("feedback", {"text": text, "user": user, "received_at": utc_now(), "applied_at": None})
        return self.store.upsert(rec, actor=user, event="feedback.received")

    def apply_limits(self, changes: dict[str, Any], *, actor: str) -> dict[str, Any]:
        """Edit the configured limits (R-85, R-89): validated by the tracker, mirrored on the run record so a later
        process continues with them, and journaled. Applied between steps or while the run is stopped."""
        try:
            applied = self.limits.apply_limits(changes)
        except ValueError as exc:
            raise EngineFailure(str(exc)) from None
        self.config.limits.update(applied)
        if self.run is not None:
            self.run.data["limits"] = dict(self.limits.limits)
            self.run.data["consumption"] = self.limits.status()
            self.run = self.store.upsert(self.run, actor=actor, event="run.limits_changed", inputs={"changes": applied},
                                         run_id=self._run_id())
        else:
            self.store.append_journal(actor=actor, event="limits.changed", inputs={"changes": applied})
        self._emit("limits", changes=applied)
        return applied

    def _progress_key(self) -> tuple[Any, ...]:
        """What counts as evidence-supported progress (R-88): completed pipeline stages, findings recorded, findings
        closed or waived, reassessments done, a component ready for review. A correction that never verifies changes
        nothing here; renders and committed revisions alone are not progress ("increasing detail is not progress")."""
        tasks = self.store.list("task")
        findings = self.store.list("finding")
        comps = self.store.list("component")
        return (
            len([o for o in self.store.list("observation", state="valid")]),
            len(self.store.list("brief")),
            bool(self.run.data.get("plan_done")) if self.run else False,
            len([t for t in tasks if t.data.get("kind") == "build" and t.state == "done"]),
            len([t for t in tasks if t.data.get("kind") == "review" and t.state == "done"]),
            len(findings),
            len([f for f in findings if f.state in ("closed", "waived")]),
            len([f for f in findings if f.data.get("reassessed")]),
            len([c for c in comps if c.state == "ready_for_user_review"]),
            len(self.store.list("acceptance", state="accepted_at_revision")),
        )

    def consume_controls(self, kinds: tuple[str, ...] = ("limits",)) -> dict[str, Any]:
        """Apply queued limit changes outside the run loop (a stopped run, or the GUI while nothing runs)."""
        out: dict[str, Any] = {"limits": {}, "errors": []}
        for control in self.store.pop_controls(kinds=kinds):
            if control.kind == "limits":
                try:
                    out["limits"].update(self.apply_limits(dict(control.payload.get("changes") or {}),
                                                           actor=f"user:{control.payload.get('by', 'user')}"))
                except EngineFailure as exc:
                    out["errors"].append(str(exc))
                    self.store.append_journal(actor="engine", event="limits.rejected", inputs={"error": str(exc),
                                              "payload": control.payload}, run_id=self._run_id())
        return out

    def _poll_controls(self) -> None:
        for control in self.store.pop_controls():
            if control.kind == "limits":
                try:
                    self.apply_limits(dict(control.payload.get("changes") or {}), actor=f"user:{control.payload.get('by', 'user')}")
                except EngineFailure as exc:
                    self.store.append_journal(actor="engine", event="limits.rejected", inputs={"error": str(exc),
                                              "payload": control.payload}, run_id=self._run_id())
            elif control.kind == "pause":
                self._pause_requested = True
            elif control.kind == "cancel":
                self.cancel_event.set()
                if self.run is not None and self.run.state not in ("cancelled", "failed"):
                    self.cancel(control.payload.get("by", "user"))
            elif control.kind == "feedback":
                self.feedback(str(control.payload.get("text", "")), control.payload.get("by", "user"))
        for fb in self.store.list("feedback", state="received"):
            fb.state = "applied"
            fb.data["applied_at"] = utc_now()
            fb.data["boundary"] = self.run.data.get("stage") if self.run else None
            self.store.upsert(fb, actor="engine", event="feedback.applied")
            if self.run is not None:
                self.run.data.setdefault("user_feedback", []).append(fb.data["text"])

    def _stop(self, state: str, stop_reason: str, note: str) -> None:
        if self.run is None:
            return
        self.run.data.setdefault("notes", []).append({"at": utc_now(), "stop_reason": stop_reason, "note": note})
        self.run.data["consumption"] = self.limits.status()
        self.run = transition(self.store, self.run, state, actor="engine", reason=note, stop_reason=stop_reason,
                              context={"inflight_reconciled": True})
        self._emit("run.stopped", state=state, stop_reason=stop_reason, note=note)

    def run_until_stop(self, max_steps: int = 100) -> dict[str, Any]:
        if self.run is None:
            raise EngineFailure("no run; call start() or attach()")
        steps = 0
        while steps < max_steps:
            self._poll_controls()
            if self.run.state != "running":
                break
            if self._pause_requested:
                self._pause_requested = False
                self._stop("paused", "user_pause", "pause requested by the user; stopped at a safe boundary")
                break
            if self.run.data.get("stage") == "done":
                break
            try:
                self.step()
                if self.run.state == "running" and self.limits.note_step(self._progress_key()):
                    ok, reason, msg = self.limits.can_dispatch()
                    raise LimitReached(reason or "stalled", msg)
            except LimitReached as exc:
                self._stop("paused", exc.reason, exc.message)
                break
            except EngineFailure as exc:
                self._stop("paused", "execution_failure", str(exc))
                break
            except OwnershipError as exc:
                self._stop("paused", "execution_failure", f"ownership: {exc}")
                break
            steps += 1
        return self.status()

    def step(self) -> None:
        stage = self.run.data.get("stage", "intake")
        handler = getattr(self, f"_stage_{stage}", None)
        if handler is None:
            raise EngineFailure(f"unknown stage {stage!r}")
        self._emit("stage", stage=stage)
        handler()

    def _set_stage(self, stage: str, **data: Any) -> None:
        self.run.data["stage"] = stage
        self.run.data.update(data)
        self.run.data["consumption"] = self.limits.status()
        self.run = self.store.upsert(self.run, actor="engine", event="run.stage", inputs={"stage": stage})

    # ------------------------------------------------------------------ helpers

    def _component(self, *, create: bool = False, name: str | None = None) -> Record:
        comps = self.store.list("component")
        if comps:
            return comps[0]
        if not create:
            raise EngineFailure("no component exists")
        name = name or self.project.record.data.get("first_component") or "Model"
        parts = [p.id for p in self.store.list("part")]
        comp = Record.new("component", {"name": name, "part_ids": parts, "created_at": utc_now()})
        return self.store.upsert(comp, actor="engine", event="component.created")

    def _latest_revision(self) -> Record:
        rev = self.operations.latest_revision()
        if rev is None:
            raise EngineFailure("no revision exists; the fixture or intake must register revision 0")
        return rev

    def _brief_text(self) -> str:
        briefs = self.store.list("brief")
        return briefs[-1].data["text"] if briefs else "(no reconstruction brief yet)"

    def _reference_items(self) -> list[EvidenceItem]:
        """Approved canon only (addendum R-94): candidates never enter modeling packets; ranked by precedence (R-95)."""
        from .concept import canon_rank

        items = []
        for ref in canon_rank(self.references.approved()):
            meta = {"reference_id": ref.id, "label": ",".join(ref.data.get("labels") or []),
                    "kind": ref.data.get("kind"), "version": ref.data.get("version"),
                    "canon_state": ref.data.get("canon_state", "approved")}
            if ref.data.get("generation_id"):
                meta["precedence"] = ref.data.get("precedence")
                meta["note"] = (f"approved generated {ref.data.get('precedence_label')} (precedence {ref.data.get('precedence')}); "
                                "canon by owner approval, not evidence of a pre-existing design (R-32, A.1)")
            if ref.data.get("part_id"):
                meta["part_id"] = ref.data["part_id"]
            items.append(EvidenceItem(Path(ref.data["file"]), "reference", meta))
        return items

    def _render_items(self, renders: list[Record]) -> list[EvidenceItem]:
        out = []
        for r in renders:
            if r.state != "ok":
                continue
            meta = {"render_id": r.id, "revision_id": r.data["revision_id"], "view": r.data["view_name"]}
            if r.data.get("part_id"):
                meta["part_id"] = r.data["part_id"]
                meta["note"] = "close-up framed on this part (R-61, R-65)"
            out.append(EvidenceItem(Path(r.data["file"]), "render", meta))
        return out

    def _crop_items(self, renders: list[Record], part_ids: list[str], *, note: str | None = None) -> list[EvidenceItem]:
        """R-65: for each overview render, the rectangle where each part projects, cut at native render pixels so
        the reviewer can locate the part; the close-up view carries the detail. Skipped when the part already
        fills most of the frame or has no measured bounding box."""
        items: list[EvidenceItem] = []
        for r in renders:
            if r.state != "ok" or r.data.get("view_name") not in OVERVIEW_VIEWS_FOR_CROPS:
                continue
            view = self.store.get("view", r.data["view_id"])
            revision = self.store.get("revision", r.data["revision_id"])
            if view is None or revision is None:
                continue
            spec = view.data["spec"]
            boxes = self._bboxes(revision)
            for pid in part_ids:
                rect = project_bbox(spec["camera"], boxes.get(pid), tuple(spec["resolution"]))
                if rect is None or rect["fraction"] > CROP_MAX_FRACTION:
                    continue
                meta = {"render_id": r.id, "revision_id": r.data["revision_id"], "view": r.data["view_name"], "part_id": pid,
                        "bbox": [rect["x"], rect["y"], rect["w"], rect["h"]], "space": "render_pixels",
                        "label": f"{pid}-at-{r.data['view_name']}", "clipped": rect["clipped"],
                        "purpose": "where the part sits in the overview render; the closeup view carries the detail"}
                if note:
                    meta["note"] = note
                items.append(EvidenceItem(Path(r.data["file"]), "crop", meta))
        return items

    def _images_of(self, packet: Record, pdir: Path) -> list[Path]:
        return [pdir / f["path"] for f in packet.data["files"] if f["role"] in ("reference", "render", "crop")]

    def _overrides(self) -> dict[str, str]:
        return dict((self.run.data.get("assignment_overrides") or {}) if self.run else {})

    def _override_rationale(self, key: str, seat: str) -> str:
        sources = (self.run.data.get("assignment_override_sources") or {}) if self.run else {}
        return self.override_rationale(key, seat, sources.get(key, "user"))

    def assignment_overrides(self) -> list[dict[str, str]]:
        """Every override in force on the run, with its source and rationale (shown by status and the Stage card)."""
        overrides = self._overrides()
        sources = (self.run.data.get("assignment_override_sources") or {}) if self.run else {}
        return [{"role": role, "seat": seat, "source": sources.get(role, "user"),
                 "rationale": self.override_rationale(role, seat, sources.get(role, "user"))}
                for role, seat in overrides.items()]

    def _assign(self, kind: str, *, exclude: str | None = None) -> tuple[str, str]:
        """Role assignment is code (R-8, R-12): the rules live in ``builder.roles``."""
        role = {"brief": "planner", "plan": "planner", "build": "builder"}.get(kind, "builder")
        overrides = self._overrides()
        labels = [x for x in self._labels() if x != exclude]
        # an interrupted build (blocked task, nothing promoted) is retried by the same seat: it does not advance the
        # alternation, and its retry packet names the interruption (R-47, R-57)
        builds = [t for t in self.store.list("task") if t.data.get("kind") == "build"
                  and not (t.state == "blocked" and t.data.get("interrupted"))]
        try:
            a = assign_role(role, seats=labels, overrides={role: overrides[kind]} if kind in overrides else None,
                            build_count=len(builds))
        except RoleConflict as exc:
            raise EngineFailure(str(exc)) from None
        if a.rationale == "user override":
            return a.seat, self._override_rationale(kind, a.seat)
        return a.seat, a.rationale

    def _role_of(self, role: str, *, author: str | None = None, proposer: str | None = None,
                 reassigned_to: str | None = None) -> tuple[str, str]:
        """Reviewer, verifier, reassessor, corrector: never the seat that made the operation (R-107)."""
        overrides = self._overrides()
        try:
            a = assign_role(role, seats=self._labels(), overrides={role: overrides[role]} if role in overrides else None,
                            author=author, proposer=proposer, reassigned_to=reassigned_to)
        except RoleConflict as exc:
            raise EngineFailure(f"{exc}; assignment override {role}={overrides.get(role)} cannot be honoured for this task") from None
        if a.rationale == "user override":
            return a.seat, self._override_rationale(role, a.seat)
        return a.seat, a.rationale

    def _bboxes(self, rev: Record) -> dict[str, Any]:
        """World bounding boxes of every tagged object in ``rev`` (measured once per revision)."""
        if rev.id in self._bbox_cache:
            return self._bbox_cache[rev.id]
        boxes: dict[str, Any] = {}
        if self.runner is not None:
            mid = new_id("meas")
            res = self.runner.measure(rev.data["file"], {"bboxes": "all"}, op_id=mid, work_dir=self.project.path("logs", mid))
            if res.ok:
                boxes = dict(res.data.get("bboxes") or {})
            else:
                self.store.append_journal(actor="engine", event="measure.failed", inputs={"error": res.error, "revision_id": rev.id},
                                          run_id=self._run_id())
        self._bbox_cache[rev.id] = boxes
        return boxes

    def _framing_for(self, rev: Record, part_ids: list[str]) -> tuple[list[float], list[float]]:
        boxes = self._bboxes(rev)
        chosen = [boxes[p] for p in part_ids if p in boxes and not boxes[p].get("empty")]
        if not chosen:
            return DEFAULT_FRAMING
        return bbox_union(chosen)

    def _views(self, comp: Record, rev: Record, extra_defs: list[dict[str, str]] | None = None) -> dict[str, Record]:
        """View records for ``comp``: cameras framed from the measured bounding box, frozen per component and
        re-framed (new version, journaled) only when parts no longer fit the frame (R-60). ``extra_defs`` adds
        views for one packet (a close-up aligned to a finding's view) without making them part of the gate set."""
        existing = {v.data["name"]: v for v in self.store.list("view") if v.data.get("component_id") == comp.id}
        all_parts = [p.id for p in self.store.list("part")]
        part_ids = list(comp.data.get("part_ids") or [])
        subjects = {"component": self._framing_for(rev, part_ids), "assembly": self._framing_for(rev, all_parts)}
        boxes = self._bboxes(rev)
        # R-61 component close-ups: one per part of the component that has a measured bounding box
        defs = list(STANDARD_VIEW_DEFS) + [closeup_view_def(pid) for pid in part_ids
                                           if pid in boxes and not boxes[pid].get("empty")]
        defs += [d for d in (extra_defs or []) if d["part_id"] in boxes and not boxes[d["part_id"]].get("empty")]
        out: dict[str, Record] = {}
        for vd in defs:
            if vd["subject"] == "part":
                center, size = bbox_union([boxes[vd["part_id"]]])
                margin = CLOSEUP_MARGIN
            else:
                center, size = subjects[vd["subject"]]
                margin = 1.15
            view = existing.get(vd["name"])
            if view is not None and framing_contains(view.data["spec"]["camera"]["framing"], center, size):
                out[vd["name"]] = view
                continue
            camera = frame_camera(center, size, vd["direction"], margin=margin)
            spec = ViewSpec(name=vd["name"], mode=vd["mode"], camera=camera, resolution=(640, 480),
                            evidence_label="matched" if vd["subject"] in ("component", "part") else "inferred_construction").to_dict()
            spec["part_id"] = vd.get("part_id")
            if view is None:
                cam = self.store.upsert(Record.new("camera", {"name": f"{comp.id}:{vd['name']}", "params": camera,
                                                              "calibration_status": "hypothesis",
                                                              "alignment_assumptions": [
                                                                  "framed from the measured bounding box; not calibrated to the references"]}),
                                        actor="engine", event="camera.created", run_id=self._run_id())
                view = Record.new("view", {"name": vd["name"], "component_id": comp.id, "camera_id": cam.id, "mode": vd["mode"],
                                           "subject": vd["subject"], "part_id": vd.get("part_id"), "spec": spec, "version": 1})
                view = self.store.upsert(view, actor="engine", event="view.created", run_id=self._run_id(),
                                         inputs={"framing": camera["framing"]})
            else:
                previous = view.data["spec"]["camera"]["framing"]
                view.data["spec"] = spec
                view.data["version"] = int(view.data.get("version", 1)) + 1
                view = self.store.upsert(view, actor="engine", event="view.reframed", run_id=self._run_id(),
                                         inputs={"from": previous, "to": camera["framing"],
                                                 "note": "parts left the frame; earlier renders of this view are stale (R-39)"})
            out[vd["name"]] = view
        return out

    def _cache_key(self, rev: Record, view: Record) -> str:
        return render_cache_key(revision_sha256=rev.data["sha256"], asset_dependency_hashes=rev.data.get("asset_dependencies") or {},
                                view=view.data["spec"], view_version=int(view.data.get("version", 1)),
                                blender_version=self.blender_version, render_script_sha256=self._render_script_sha)

    def _render_component(self, rev: Record, comp: Record, reason: str, only_defs: list[dict[str, str]] | None = None) -> list[Record]:
        """Render the component's views at ``rev`` (cache by key). With ``only_defs`` just those extra views."""
        out: list[Record] = []
        all_renders = self.store.list("render", state="ok")
        views = self._views(comp, rev, extra_defs=only_defs)
        if only_defs is not None:
            views = {n: v for n, v in views.items() if n in {d["name"] for d in only_defs}}
        self._execution("rendering")
        try:
            for name, view in views.items():
                key = self._cache_key(rev, view)
                cached = [r for r in all_renders if r.data.get("cache_key") == key and r.data.get("revision_id") == rev.id]
                if cached:
                    stale, why = render_is_stale(cached[-1], current_revision_id=rev.id, expected_cache_key=key)
                    if not stale:
                        self.store.append_journal(actor="engine", event="render.reused",
                                                  inputs={"render_id": cached[-1].id, "reason": reason}, run_id=self._run_id())
                        out.append(cached[-1])
                        continue
                ok, limit_reason, msg = self.limits.can_dispatch("render")
                if not ok:
                    raise LimitReached(limit_reason or "budget_limit", msg)
                rnd = Record.new("render", {"view_id": view.id, "view_name": name, "revision_id": rev.id,
                                            "component_id": comp.id, "file": "", "manifest": {}, "cache_key": key,
                                            "mode": view.data["mode"], "reason": reason, "part_id": view.data.get("part_id")})
                out_png = self.project.path("renders", f"{rnd.id}.png")
                rnd.data["file"] = str(out_png)
                self.limits.note_dispatch("render")
                res = self.runner.render(rev.data["file"], view.data["spec"], out_png, op_id=rnd.id,
                                         deadline_s=self.config.deadlines["render"],
                                         work_dir=self.project.path("logs", rnd.id), cancel_event=self.cancel_event)
                self.limits.note_complete("render")
                applied = res.data.get("applied") or {}
                mismatches = applied_settings_match(view.data["spec"], applied) if res.ok else []
                success = bool(res.ok and out_png.is_file() and not mismatches)
                out_sha = sha256_file(out_png) if out_png.is_file() else None
                rnd.data["manifest"] = build_manifest(
                    revision_id=rev.id, revision_sha256=rev.data["sha256"], asset_dependencies=rev.data.get("asset_dependencies") or {},
                    view=view.data["spec"], view_version=int(view.data.get("version", 1)), applied=applied, output_path=out_png,
                    output_sha256=out_sha, success=success, error=res.error or "; ".join(mismatches), elapsed_s=res.elapsed_s,
                    render_script_sha256=self._render_script_sha, cache_key=key, view_id=view.id)
                rnd.data["error"] = res.error or "; ".join(mismatches)
                rnd.state = "ok" if success else "failed"
                rnd = self.store.upsert(rnd, actor="engine", event="render.created", run_id=self._run_id(),
                                        inputs={"view": name, "revision_id": rev.id, "reason": reason}, outcome=rnd.state)
                if success:
                    self.store.register_file(out_png, kind="render", record_id=rnd.id)
                else:
                    raise EngineFailure(f"render {name} of {rev.id} failed: {rnd.data['error']}")
                out.append(rnd)
        finally:
            self._execution("running")
        return out

    def _measure(self, rev: Record, comp: Record) -> EvidenceItem | None:
        if self.runner is None:
            return None
        parts = list(comp.data.get("part_ids") or [])
        contacts = [[r.data["from_part"], r.data["to_part"]] for r in self.store.list("relation")
                    if r.data.get("type") in ("attached_to", "contacts", "sits_under", "supports")]
        mid = new_id("meas")
        res = self.runner.measure(rev.data["file"], {"bboxes": parts, "distances": [], "ratios": [],
                                                    "overlaps": {"ids": parts, "intended_contact": contacts}},
                                  op_id=mid, work_dir=self.project.path("logs", mid))
        if not res.ok:
            self.store.append_journal(actor="engine", event="measure.failed", inputs={"error": res.error}, run_id=self._run_id())
            return None
        path = self.project.path("logs", mid, "measurement.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"revision_id": rev.id, "data": res.data}, f, ensure_ascii=False, indent=1)
        return EvidenceItem(path, "measurement", {"revision_id": rev.id, "note": "limitations listed inside"})

    def _checkpoint(self, rev: Record, name: str, reason: str) -> Record:
        ck = Record.new("checkpoint", {"revision_id": rev.id, "name": name, "reason": reason, "files": [], "created_at": utc_now()})
        d = self.project.path("checkpoints", ck.id)
        d.mkdir(parents=True, exist_ok=True)
        dest = d / "model.blend"
        shutil.copyfile(rev.data["file"], dest)
        digest = sha256_file(dest)
        if digest != rev.data["sha256"]:
            raise EngineFailure("checkpoint copy hash mismatch (R-48)")
        files = [{"path": str(dest), "sha256": digest, "role": "revision"}]
        for path, sha in (rev.data.get("asset_dependencies") or {}).items():
            src = Path(path)
            if src.is_file():
                target = d / "assets" / src.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)
                files.append({"path": str(target), "sha256": sha256_file(target), "role": "asset"})
        op = self.store.get("operation", rev.data.get("created_by_op_id") or "")
        if op and op.data.get("script_path") and Path(op.data["script_path"]).is_file():
            target = d / "script.py"
            shutil.copyfile(op.data["script_path"], target)
            files.append({"path": str(target), "sha256": sha256_file(target), "role": "script"})
        manifests = [r.data.get("manifest") for r in self.store.list("render", state="ok") if r.data.get("revision_id") == rev.id]
        with open(d / "manifest.json", "w", encoding="utf-8") as f:
            json.dump({"checkpoint_id": ck.id, "revision_id": rev.id, "sha256": rev.data["sha256"], "files": files,
                       "render_manifests": manifests, "name": name, "reason": reason}, f, ensure_ascii=False, indent=1)
        files.append({"path": str(d / "manifest.json"), "sha256": sha256_file(d / "manifest.json"), "role": "manifest"})
        ck.data["files"] = files
        ck = self.store.upsert(ck, actor="engine", event="checkpoint.created", run_id=self._run_id(),
                               inputs={"revision_id": rev.id, "name": name})
        for f in files:
            self.store.register_file(f["path"], kind="checkpoint", record_id=ck.id)
        return ck

    def _open_findings(self, comp: Record) -> list[Record]:
        return sorted([f for f in self.store.list("finding") if f.data.get("component_id") == comp.id
                       and f.state in OPEN_FINDING_STATES], key=lambda f: (SEVERITY_ORDER.get(f.data.get("severity"), 9), f.id))

    def _rev_index(self) -> dict[str, int]:
        return {r.id: i for i, r in enumerate(self.store.list("revision"))}

    def _last_modified_index(self, part_id: str, idx: dict[str, int]) -> int:
        last = -1
        for op in self.store.list("operation", state="committed"):
            effects = op.data.get("declared_effects") or {}
            touched = set(effects.get("modifies", [])) | set(effects.get("creates", [])) | set(op.data.get("target_part_ids") or [])
            if part_id in touched and op.data.get("result_revision_id") in idx:
                last = max(last, idx[op.data["result_revision_id"]])
        return last

    def _ready_facts(self, comp: Record, rev: Record) -> dict[str, Any]:
        views = self._views(comp, rev)
        required_ids = [views[n].id for n in REVIEW_VIEWS if n in views]
        expected = {views[n].id: self._cache_key(rev, views[n]) for n in views}
        comp_renders = [r for r in self.store.list("render") if r.data.get("component_id") == comp.id]
        fresh, reasons = renders_fresh(comp_renders, required_view_ids=required_ids, current_revision_id=rev.id,
                                       expected_keys=expected)
        idx = self._rev_index()
        coverage = self.store.list("coverage")
        missing_cov: list[str] = []
        for part_id in comp.data.get("part_ids") or []:
            needed = self._last_modified_index(part_id, idx)
            for view_name in COVERAGE_VIEWS:
                ok = any(c.data.get("part_id") == part_id and c.data.get("view_name") == view_name
                         and idx.get(c.data.get("revision_id"), -1) >= needed for c in coverage)
                if not ok:
                    missing_cov.append(f"{part_id}/{view_name}")
        open_findings = self._open_findings(comp)
        pending = [o for o in self.store.list("operation") if o.state in IN_FLIGHT_STATES]
        return {"open_findings": len(open_findings), "renders_fresh": fresh, "render_reasons": reasons,
                "coverage_met": not missing_cov, "coverage_missing": missing_cov, "pending_ops": len(pending),
                "last_output_malformed": False}

    def _execute_operations(self, task: Record, specs: list[dict[str, Any]], label: str, token: str) -> list[Record]:
        agent = self.agents[label]
        ops: list[Record] = []
        for spec in specs:
            base = self._latest_revision()
            req = OperationRequest(kind="apply_script", task_id=task.id, agent_id=agent.id, intent=spec["intent"],
                                   expected_outcome=spec["expected_outcome"], target_part_ids=list(spec.get("target_part_ids") or []),
                                   declared_effects=dict(spec.get("declared_effects") or {}), script_source=spec["script"],
                                   expected_base_revision_id=base.id, ownership_token=token,
                                   deadline_s=float(self.config.deadlines["apply"]))
            op = self.operations.create(req, actor=f"agent:{label}")
            if op.state == "created":
                self._execution("rendering")
                try:
                    op = self.operations.execute(op, actor="engine", cancel_event=self.cancel_event)
                finally:
                    self._execution("running")
            task.data.setdefault("op_ids", []).append(op.id)
            ops.append(op)
            self._emit("operation", op_id=op.id, state=op.state, agent=label)
            if op.state != "committed":
                break
        return ops

    def _finish_task(self, task: Record, state: str, **data: Any) -> Record:
        task.data.update(data)
        task.state = state
        return self.store.upsert(task, actor="engine", event=f"task.{state}", run_id=self._run_id())

    def _new_task(self, kind: str, comp: Record, owner_label: str, rationale: str, expected_outcome: str,
                  part_ids: list[str], **extra: Any) -> Record:
        rev = self._latest_revision()
        task = Record.new("task", {
            "kind": kind, "part_ids": list(part_ids), "expected_outcome": expected_outcome,
            "owner_agent_id": self.agents[owner_label].id, "rationale": rationale, "component_id": comp.id,
            "base_revision_id": rev.id, "dependency_versions": {p: (self.store.get("part", p).version if self.store.get("part", p) else None)
                                                                for p in part_ids},
            "op_ids": [], "created_at": utc_now(), **extra,
        }, state="assigned")
        return self.store.upsert(task, actor="engine", event="task.assigned", run_id=self._run_id(),
                                 inputs={"owner": owner_label, "rationale": rationale})

    def _map_refs(self, refs: list[str], renders: list[Record]) -> list[str]:
        out: list[str] = []
        for ref in refs:
            if ref.startswith("render:"):
                name = ref.split(":", 1)[1]
                out.extend(r.id for r in renders if r.data.get("view_name") == name)
            else:
                out.append(ref)
        return out

    def _record_coverage(self, entries: list[dict[str, Any]], rev: Record, inspector: str, inv_id: str,
                         comp: Record | None = None) -> None:
        views = self._views(comp or self._component(), rev)
        for c in entries:
            view_name = str(c.get("view"))
            view_id = views[view_name].id if view_name in views else f"view_{view_name}"
            rec = Record.new("coverage", {"part_id": c["part_id"], "view_id": view_id, "view_name": view_name,
                                          "revision_id": rev.id, "inspected_by": inspector, "invocation_id": inv_id,
                                          "instances_inspected": c.get("instances_inspected"),
                                          "sampling_strategy": c.get("sampling_strategy")})
            self.store.upsert(rec, actor="engine", event="coverage.recorded", run_id=self._run_id())

    # ------------------------------------------------------------------- stages

    def _stage_intake(self) -> None:
        plans = self.store.list("concept_plan")
        if plans and plans[-1].state != "complete":
            raise EngineFailure(f"concept stage is {plans[-1].state}, not complete: approve the needed references or run "
                                "`concept proceed` to accept a partial set (R-103); intake uses approved canon only (R-94)")
        refs = self.references.approved()
        if not refs:
            raise EngineFailure("no approved references registered; run intake (or the concept stage) before starting (R-26)")
        ready, reasons = self.references.intake_ready()
        if not ready:
            raise EngineFailure("intake is not ready: " + "; ".join(reasons))
        for label in self._labels():
            agent = self.agents[label]
            if any(o.data.get("agent_id") == agent.id and o.data.get("subject") == "intake"
                   for o in self.store.list("observation", state="valid")):
                continue
            packet, pdir = self.packets.build(
                kind="intake_observation", agent_id=agent.id, task=None, objective=INTAKE_OBJECTIVE,
                brief_text="(intake: no brief yet; the reconstruction brief is written after both observations)",
                evidence=self._reference_items(), open_findings=[], constraints=[],
                withheld=[{"what": "the other agent's observations", "why": "R-11: independent observation at intake"}],
                schema_name="observation_report", schema=SCHEMAS["observation_report"], changed=None,
                records={"observations": []})
            outcome, inv = self._invoke(label, "intake_observation", packet=packet, pdir=pdir,
                                        schema_name="observation_report", images=self._images_of(packet, pdir))
            if not outcome.ok:
                raise EngineFailure(f"intake observation from agent {label} was malformed: {outcome.errors}")
            obs = Record.new("observation", {
                "agent_id": agent.id, "subject": "intake", "evidence_versions": {r.id: r.data.get("version") for r in refs},
                "dependency_versions": {}, "content": outcome.value, "invocation_id": inv.id, "packet_id": packet.id,
                "recorded_at": utc_now()})
            self.store.upsert(obs, actor="engine", event="observation.recorded", run_id=self._run_id())
            for q in outcome.value.get("questions_for_user") or []:
                self.run.data.setdefault("questions_for_user", []).append({"from": label, **q})
        self._set_stage("brief")

    def _stage_brief(self) -> None:
        if self.store.list("brief"):
            self._set_stage("plan")
            return
        label, rationale = self._assign("brief")
        observations = [o for o in self.store.list("observation", state="valid") if o.data.get("subject") == "intake"]
        sections = {f"Independent observations from agent {self._label_of(o.data['agent_id'])} (recorded before sharing)":
                    "```json\n" + json.dumps(o.data["content"], ensure_ascii=False, indent=1)[:6000] + "\n```"
                    for o in observations}
        packet, pdir = self.packets.build(
            kind="brief_draft", agent_id=self.agents[label].id, task=None,
            objective="Reconcile both independent observations into a concise, editable reconstruction brief. Record the "
                      "chosen interpretation and competing hypotheses where they affect the model. No false precision.",
            brief_text="(drafting the brief now)", evidence=self._reference_items(), open_findings=[], constraints=[],
            withheld=[], schema_name="brief_draft", schema=SCHEMAS["brief_draft"], changed=None,
            extra_sections=sections, records={"observations": observations})
        outcome, inv = self._invoke(label, "brief_draft", packet=packet, pdir=pdir, schema_name="brief_draft",
                                    images=self._images_of(packet, pdir))
        if not outcome.ok:
            raise EngineFailure(f"brief draft from agent {label} was malformed: {outcome.errors}")
        text = _brief_to_text(outcome.value)
        brief = Record.new("brief", {"text": text, "structured": outcome.value, "version": 1, "drafted_by": self.agents[label].id,
                                     "rationale": rationale, "invocation_id": inv.id})
        self.store.upsert(brief, actor="engine", event="brief.recorded", run_id=self._run_id())
        self._tag(label, "observation:intake:other")
        self._set_stage("plan")

    def _stage_plan(self) -> None:
        if self.run.data.get("plan_done"):
            self._set_stage("build")
            return
        label, rationale = self._assign("plan")
        comp = self._component()
        parts = self.store.list("part")
        inventory = "\n".join(f"- {p.id}: {p.data.get('name')} (blender ids {p.data.get('blender_ids') or [p.id]})" for p in parts) or "- (empty)"
        packet, pdir = self.packets.build(
            kind="construction_plan", agent_id=self.agents[label].id, task=None,
            objective=f"Produce the construction plan for the whole model with the first detailed component '{comp.data['name']}'. "
                      "Keep the coarse whole-model blockout, coordinate system, and attachment interfaces explicit. Use the "
                      "existing part ids where they match; propose new ids only for parts the evidence supports.",
            brief_text=self._brief_text(), evidence=self._reference_items(), open_findings=[], constraints=[],
            withheld=[], schema_name="construction_plan", schema=SCHEMAS["construction_plan"], changed=None,
            extra_sections={"Current part inventory": inventory}, records={"parts": parts, "brief": self.store.list("brief")})
        outcome, inv = self._invoke(label, "construction_plan", packet=packet, pdir=pdir, schema_name="construction_plan",
                                    images=self._images_of(packet, pdir))
        if not outcome.ok:
            raise EngineFailure(f"construction plan from agent {label} was malformed: {outcome.errors}")
        plan = outcome.value
        id_map: dict[str, str] = {}
        for p in plan["parts"]:
            existing = self.store.get("part", p["part_id"])
            if existing is None:
                rec = Record.new("part", {"name": p["name"], "proposed_id": p["part_id"], "blender_ids": []})
            else:
                rec = existing
            rec.data.update({"interpretation": p.get("interpretation"), "evidence_status": p.get("evidence_status"),
                             "confidence": p.get("confidence"), "questions": p.get("questions") or [],
                             "dimensions": p.get("dimensions") or [], "component": p.get("component")})
            rec.parent_id = id_map.get(p.get("parent") or "", p.get("parent")) if p.get("parent") else None
            rec = self.store.upsert(rec, actor="engine", event="part.planned", run_id=self._run_id())
            id_map[p["part_id"]] = rec.id
        for r in plan.get("relations", []):
            rel = Record.new("relation", {"from_part": id_map.get(r["from_part"], r["from_part"]),
                                          "to_part": id_map.get(r["to_part"], r["to_part"]), "type": r["type"], "evidence": "plan"})
            self.store.upsert(rel, actor="engine", event="relation.recorded", run_id=self._run_id())
        for d in plan.get("dependencies", []):
            to_part = id_map.get(d["to_part"], d["to_part"])
            target = self.store.get("part", to_part)
            dep = Record.new("dependency", {"from_id": id_map.get(d["from_id"], d["from_id"]), "to_part": to_part,
                                            "pinned_version": target.version if target else None})
            self.store.upsert(dep, actor="engine", event="dependency.recorded", run_id=self._run_id())
        # Membership: an explicit per-part `component` in the plan is authoritative; otherwise keep the membership the
        # project or preset already defined (R-34: the inventory is refined, never silently dissolved). A component
        # with no membership yet takes every planned part.
        assigned = [id_map[p["part_id"]] for p in plan["parts"] if p.get("component") == comp.data["name"]]
        if assigned:
            comp.data["part_ids"] = assigned
        elif not comp.data.get("part_ids"):
            comp.data["part_ids"] = [id_map[p["part_id"]] for p in plan["parts"] if not p.get("component")]
        comp.data["interfaces"] = plan.get("interfaces") or []
        comp.data["hypotheses"] = plan.get("hypotheses") or []
        self.store.upsert(comp, actor="engine", event="component.planned", run_id=self._run_id())
        self.run.data["plan"] = {"by": label, "rationale": rationale, "invocation_id": inv.id,
                                 "blockout_note": "revision 0 serves as the coarse whole-model blockout (R-33)"}
        self._set_stage("build", plan_done=True)

    def _stage_build(self) -> None:
        comp = self._component()
        rev = self._latest_revision()
        self._reset_interrupted(actor="engine", reason="build retried")   # a blocked holder releases its grant (R-44, R-47)
        label, rationale = self._assign("build")
        agent = self.agents[label]
        task = self._new_task("build", comp, label, rationale,
                              "component matches the references in silhouette, depth, construction, and materials",
                              list(comp.data.get("part_ids") or []))
        own = self.ownership.acquire("assembly", agent.id, rev.id, actor="engine", reason=f"build task {task.id}")
        self._checkpoint(rev, f"base before {task.id}", "R-56: checkpoint the base revision before building")
        renders = self._render_component(rev, comp, f"current model for {task.id}")
        measurement = self._measure(rev, comp)
        evidence = self._reference_items() + self._render_items(renders) + ([measurement] if measurement else [])
        session = self.sessions.persistent(agent.id, provider=agent.data["provider"])
        changed = changed_since(self.store, int(session.data.get("last_journal_seq") or 0))
        packet, pdir = self.packets.build(
            kind="build_task", agent_id=agent.id, task=task,
            objective=f"Build or revise component '{comp.data['name']}' ({', '.join(comp.data.get('part_ids') or [])}) toward the "
                      "references. Work from large forms to details. Return operation requests (bpy scripts with intent, "
                      "targets, expected observable outcome, declared effects) or state that no useful edit is warranted.",
            brief_text=self._brief_text(), evidence=evidence, open_findings=self._open_findings(comp),
            constraints=list(comp.data.get("interfaces") or []) + [f"User feedback: {t}" for t in self.run.data.get("user_feedback", [])],
            withheld=[], schema_name="task_result", schema=SCHEMAS["task_result"], changed=changed,
            extra_sections={"Construction plan hypotheses": json.dumps(comp.data.get("hypotheses") or [], ensure_ascii=False),
                            **self._interrupted_ops_section(rev)},
            records={"parts": [p for p in self.store.list("part") if p.id in (comp.data.get("part_ids") or [])]})
        task = self._finish_task(task, "in_progress", packet_id=packet.id)
        outcome, inv = self._invoke(label, "build_task", packet=packet, pdir=pdir, schema_name="task_result",
                                    images=self._images_of(packet, pdir),
                                    extra={"task_id": task.id, "component_id": comp.id, "part_ids": comp.data.get("part_ids")})
        if not outcome.ok:
            self._finish_task(task, "blocked", error=f"malformed build output: {outcome.errors}", invocation_id=inv.id, interrupted=True)
            raise EngineFailure(f"build task {task.id}: agent {label} returned malformed output ({inv.data['outcome']})")
        result = outcome.value
        ops = self._execute_operations(task, result.get("operations") or [], label, own.data["token"])
        failed = [o for o in ops if o.state != "committed"]
        if failed:
            self._finish_task(task, "blocked", result=result, interrupted=True,
                              error=f"operation {failed[0].id} {failed[0].state}: {failed[0].data.get('error')}")
            raise EngineFailure(f"build task {task.id}: operation {failed[0].id} {failed[0].state}: {failed[0].data.get('error')}")
        self._finish_task(task, "done", result=result, invocation_id=inv.id, committed_ops=[o.id for o in ops])
        self._tag(label, f"self_assessment:{comp.id}:{task.id}")
        for q in result.get("questions_for_user") or []:
            self.run.data.setdefault("questions_for_user", []).append({"from": label, "task_id": task.id, **q})
        self._set_stage("review", build_task_id=task.id)

    def _stage_review(self) -> None:
        comp = self._component()
        rev = self._latest_revision()
        build_task = self.store.require("task", self.run.data["build_task_id"])
        builder_label = self._label_of(build_task.data["owner_agent_id"])
        reviewer, rationale = self._role_of("reviewer", author=builder_label)
        agent = self.agents[reviewer]
        renders = self._render_component(rev, comp, "review")
        measurement = self._measure(rev, comp)
        contaminated = any(str(t).startswith(f"self_assessment:{comp.id}") for t in agent.data.get("context_contains") or [])
        session_kind = "isolated" if (contaminated or self.config.isolated_reviews == "always") else "persistent"
        task = self._new_task("review", comp, reviewer, rationale,
                              "independent findings with recorded coverage", list(comp.data.get("part_ids") or []),
                              render_ids=[r.id for r in renders], session_kind=session_kind)
        evidence = (self._reference_items() + self._render_items(renders)
                    + self._crop_items(renders, list(comp.data.get("part_ids") or [])) + ([measurement] if measurement else []))
        packet, pdir = self.packets.build(
            kind="review_task", agent_id=agent.id, task=task, objective=REVIEW_OBJECTIVE,
            brief_text=self._brief_text(), evidence=evidence, open_findings=self._open_findings(comp),
            constraints=list(comp.data.get("interfaces") or []),
            withheld=[{"what": "builder self-assessment", "why": "R-11: your initial findings are recorded before reconciliation"}],
            schema_name="findings_report", schema=SCHEMAS["findings_report"],
            changed=None if session_kind == "isolated" else changed_since(
                self.store, int(self.sessions.persistent(agent.id, provider=agent.data["provider"]).data.get("last_journal_seq") or 0)),
            records={"parts": [p for p in self.store.list("part") if p.id in (comp.data.get("part_ids") or [])]})
        task = self._finish_task(task, "in_progress", packet_id=packet.id)
        outcome, inv = self._invoke(reviewer, "review_task", packet=packet, pdir=pdir, schema_name="findings_report",
                                    images=self._images_of(packet, pdir), session_kind=session_kind,
                                    isolated_reason=f"review of {comp.id}: persistent context already holds the builder's self-assessment",
                                    extra={"task_id": task.id, "component_id": comp.id, "revision_id": rev.id,
                                           "render_ids": [r.id for r in renders]})
        if not outcome.ok:
            self._finish_task(task, "blocked", error=f"malformed review output: {outcome.errors}", invocation_id=inv.id)
            raise EngineFailure(f"review task {task.id}: agent {reviewer} returned malformed output ({inv.data['outcome']}); "
                                "review state unchanged (R-5)")
        report = outcome.value
        new_ids: list[str] = []
        for f in report["findings"]:
            rec = Record.new("finding", {
                "part_id": f["part_id"], "region": f.get("region"), "view": f.get("view"),
                "observed_mismatch": f["observed_mismatch"], "severity": f["severity"], "confidence": f["confidence"],
                "evidence_refs": f.get("evidence_refs") or [], "evidence_render_ids": self._map_refs(f.get("evidence_refs") or [], renders),
                "proposed_correction": f.get("proposed_correction"), "expected_improvement": f.get("expected_improvement"),
                "alternative_hypotheses": f.get("alternative_hypotheses") or [], "kind": f.get("kind", "defect"),
                "source_revision_id": rev.id, "reported_by": agent.id, "task_id": task.id, "component_id": comp.id,
                "before_render_ids": [r.id for r in renders], "after_render_ids": [], "attempts": [],
                "initial_recorded_before_reconciliation": True, "resolution": None, "owner_agent_id": None,
                "recorded_at": utc_now(),
            })
            rec = self.store.upsert(rec, actor="engine", event="finding.recorded", run_id=self._run_id(),
                                    inputs={"reported_by": reviewer, "invocation_id": inv.id})
            new_ids.append(rec.id)
        self._record_coverage(report.get("coverage") or [], rev, agent.id, inv.id, comp)
        self._finish_task(task, "done", result={"finding_ids": new_ids, "coverage_entries": len(report.get("coverage") or [])},
                          invocation_id=inv.id)
        self.run.data["review_task_id"] = task.id
        open_count = len(self._open_findings(comp))
        if open_count:
            if comp.state == "unreviewed":
                transition(self.store, comp, "findings_open", actor="engine", reason=f"{open_count} finding(s) recorded",
                           context={"open_findings": open_count}, run_id=self._run_id())
            self._set_stage("reconcile")
        else:
            self._set_stage("gate")

    def _stage_reconcile(self) -> None:
        comp = self._component()
        build_task = self.store.require("task", self.run.data["build_task_id"])
        review_task = self.store.require("task", self.run.data["review_task_id"])
        builder_label = self._label_of(build_task.data["owner_agent_id"])
        reviewer = self._label_of(review_task.data["owner_agent_id"])   # the seat that recorded the initial findings
        agent = self.agents[reviewer]
        findings = [self.store.require("finding", fid) for fid in (review_task.data.get("result") or {}).get("finding_ids", [])]
        findings = [f for f in findings if f.state == "open"]
        if not findings:
            self._set_stage("correct")
            return
        renders = [self.store.require("render", rid) for rid in review_task.data.get("render_ids", [])]
        assessment = (build_task.data.get("result") or {}).get("self_assessment") or "(none)"
        packet, pdir = self.packets.build(
            kind="review_reconcile", agent_id=agent.id, task=review_task,
            objective="Your initial findings are recorded. Now that the builder's self-assessment is revealed, confirm or "
                      "withdraw each finding. Withdraw only when the renders, not the claim, show the mismatch is absent.",
            brief_text=self._brief_text(), evidence=self._reference_items() + self._render_items(renders),
            open_findings=findings, constraints=[], withheld=[], schema_name="reconciliation", schema=SCHEMAS["reconciliation"],
            changed=None, extra_sections={
                "Builder self-assessment (revealed after your initial findings were recorded)": assessment,
                "Your initial findings": "\n".join(f"- {f.id}: {f.data['observed_mismatch']}" for f in findings)},
            records={"findings": findings})
        outcome, inv = self._invoke(reviewer, "review_reconcile", packet=packet, pdir=pdir, schema_name="reconciliation",
                                    images=self._images_of(packet, pdir), extra={"finding_ids": [f.id for f in findings]})
        self._tag(reviewer, f"self_assessment:{comp.id}:{build_task.id}")
        if not outcome.ok:
            raise EngineFailure(f"reconciliation from agent {reviewer} was malformed: {outcome.errors}")
        for w in outcome.value.get("findings_withdrawn") or []:
            f = self.store.get("finding", w.get("finding_id", ""))
            if f is None or f.state != "open":
                continue
            f.state = "closed"
            f.data["resolution"] = {"verdict": "withdrawn_by_reviewer", "reason": w.get("reason"), "at": utc_now(),
                                    "invocation_id": inv.id}
            self.store.upsert(f, actor="engine", event="finding.withdrawn", run_id=self._run_id())
        remaining = self._open_findings(comp)
        if remaining:
            if comp.state == "findings_open":
                transition(self.store, comp, "changes_required", actor="engine",
                           reason=f"{len(remaining)} confirmed finding(s); corrections will be attempted", run_id=self._run_id())
            self._set_stage("correct")
        else:
            self._set_stage("gate")

    def _stage_correct(self) -> None:
        comp = self._component()
        open_findings = self._open_findings(comp)
        if any(f.state == "evidence_gap" for f in open_findings):
            gaps = [f.id for f in open_findings if f.state == "evidence_gap"]
            studies = [s for f in open_findings if f.state == "evidence_gap" for s in (f.data.get("study_request_ids") or [])]
            note = f"finding(s) {gaps} need more reference evidence; add references or waive with a rationale"
            if studies:
                generated = [s for s in studies if (self.store.get("study_request", s) or Record("study_request", s)).state != "requested"
                             or (self.store.get("study_request", s) or Record("study_request", s)).data.get("generation_id")]
                how = ("generate them through the concept stage (`concept prompts`, then `concept import`)"
                       if generated or self.store.list("concept_plan") else
                       "start a concept stage (`concept start --from-image <approved reference>`) and run `concept study` to generate them (R-104)")
                note += f"; {len(studies)} study request(s) recorded ({', '.join(studies)}): {how}"
            self._set_stage("done")
            self._stop("waiting_for_user", "missing_evidence", note)
            return
        reassess = [f for f in open_findings if f.state == "reassess"]
        if reassess and not reassess[0].data.get("reassessed"):
            self._set_stage("reassess")
            return
        if any(f.state == "correcting" for f in open_findings):
            # a correction the loop never verified (failed or uncertain operation, malformed output, restart): the
            # finding returns to open with a note and the next attempt starts from the unchanged base (R-23, R-47)
            self._reset_interrupted(actor="engine", reason="correction interrupted")
            open_findings = self._open_findings(comp)
        candidates = [f for f in open_findings if f.state in ("open", "reassess")]
        if not candidates:
            self._set_stage("gate")
            return
        finding = candidates[0]
        if not self.limits.correction_allowed(finding.id):
            # the stage stays at correct: raising attempts_per_finding and resuming retries with the reassessed approach
            self._stop("paused", "attempt_limit",
                       f"finding {finding.id} reached attempts_per_finding={self.limits.limits.get('attempts_per_finding')} "
                       f"(reassessment: {finding.data.get('reassessment')}); raise the limit and resume to try the "
                       "materially different approach")
            return
        proposer = self._label_of(finding.data["reported_by"])
        corrector, _ = self._role_of("corrector", proposer=proposer,
                                     reassigned_to=self._label_of(finding.data["reassigned_to"]) if finding.data.get("reassigned_to") else None)
        agent = self.agents[corrector]
        rev = self._latest_revision()
        holder = self.ownership.current("assembly")
        if holder is not None and holder.data["holder"] != agent.id:
            prev_label = self._label_of(holder.data["holder"])
            changed_parts = sorted({p for o in self.store.list("operation", state="committed")
                                    if o.data.get("agent_id") == holder.data["holder"]
                                    for p in (o.data.get("target_part_ids") or [])})
            handoff, own = self.ownership.handoff(
                holder.data["token"], agent.id, revision_id=rev.id, changed_parts=changed_parts,
                assumptions=list((comp.data.get("hypotheses") or [])[:5]) and [str(h) for h in (comp.data.get("hypotheses") or [])][:5],
                findings=[f.id for f in open_findings], pending_issues=[], actor="engine",
                reason=f"agent {corrector} proposed the correction for {finding.id} and can implement it (R-56)")
            rationale = f"finder implements its own proposed correction after handoff from {prev_label} (R-7, R-56)"
        elif holder is None:
            own = self.ownership.acquire("assembly", agent.id, rev.id, actor="engine", reason=f"correction of {finding.id}")
            rationale = "finder implements its own proposed correction (R-7)"
        else:
            own = holder
            rationale = "current owner continues corrections"
        task = self._new_task("correct", comp, corrector, rationale, finding.data.get("expected_improvement") or "finding resolved",
                              [finding.data["part_id"]], finding_id=finding.id)
        attempt_no = self.limits.note_correction_attempt(finding.id)
        renders = self._render_component(rev, comp, f"before correction attempt {attempt_no} of {finding.id}")
        attempt = Record.new("correction_attempt", {"finding_id": finding.id, "task_id": task.id, "op_ids": [],
                                                    "before_render_ids": [r.id for r in renders], "after_render_ids": [],
                                                    "attempt_no": attempt_no, "agent_id": agent.id, "started_at": utc_now()})
        attempt = self.store.upsert(attempt, actor="engine", event="correction_attempt.started", run_id=self._run_id())
        finding.state = "correcting"
        finding.data["owner_agent_id"] = agent.id
        finding.data.setdefault("attempts", []).append(attempt.id)
        finding = self.store.upsert(finding, actor="engine", event="finding.correcting", run_id=self._run_id())
        measurement = self._measure(rev, comp)
        approach = (finding.data.get("reassessment") or {}).get("materially_different_approach")
        packet, pdir = self.packets.build(
            kind="correction_task", agent_id=agent.id, task=task,
            objective=f"Correct finding {finding.id} on {finding.data['part_id']}: {finding.data['observed_mismatch']}. "
                      f"Proposed correction: {finding.data.get('proposed_correction')}. Expected observable improvement: "
                      f"{finding.data.get('expected_improvement')}." + (f" Use this materially different approach: {approach}." if approach else ""),
            brief_text=self._brief_text(), evidence=(self._reference_items() + self._render_items(renders)
                                                     + self._crop_items(renders, [finding.data["part_id"]])
                                                     + ([measurement] if measurement else [])),
            open_findings=[finding], constraints=list(comp.data.get("interfaces") or []), withheld=[],
            schema_name="task_result", schema=SCHEMAS["task_result"],
            changed=changed_since(self.store, int(self.sessions.persistent(agent.id, provider=agent.data["provider"]).data.get("last_journal_seq") or 0)),
            extra_sections=self._interrupted_ops_section(rev) or None,
            records={"findings": [finding]})
        task = self._finish_task(task, "in_progress", packet_id=packet.id)
        outcome, inv = self._invoke(corrector, "correction_task", packet=packet, pdir=pdir, schema_name="task_result",
                                    images=self._images_of(packet, pdir), extra={"finding_id": finding.id, "task_id": task.id})
        if not outcome.ok:
            self._finish_task(task, "blocked", error=f"malformed correction output: {outcome.errors}")
            attempt.state = "uncertain"
            self.store.upsert(attempt, actor="engine", event="correction_attempt.uncertain", run_id=self._run_id())
            raise EngineFailure(f"correction task {task.id}: agent {corrector} returned malformed output")
        result = outcome.value
        ops = self._execute_operations(task, result.get("operations") or [], corrector, own.data["token"])
        failed = [o for o in ops if o.state != "committed"]
        attempt.data["op_ids"] = [o.id for o in ops]
        if failed:
            attempt.state = "uncertain"
            self.store.upsert(attempt, actor="engine", event="correction_attempt.uncertain", run_id=self._run_id())
            self._finish_task(task, "blocked", result=result, error=f"operation {failed[0].id} {failed[0].state}: {failed[0].data.get('error')}")
            raise EngineFailure(f"correction task {task.id}: operation {failed[0].id} {failed[0].state}: {failed[0].data.get('error')}")
        self._finish_task(task, "done", result=result, invocation_id=inv.id, committed_ops=[o.id for o in ops])
        self._tag(corrector, f"self_assessment:{comp.id}:{task.id}")
        after = self._render_component(self._latest_revision(), comp, f"after correction attempt {attempt_no} of {finding.id}")
        attempt.data["after_render_ids"] = [r.id for r in after]
        attempt.data["no_edit_claimed"] = bool(result.get("no_edit_warranted")) and not ops
        self.store.upsert(attempt, actor="engine", event="correction_attempt.rendered", run_id=self._run_id())
        finding.state = "verify_pending"
        self.store.upsert(finding, actor="engine", event="finding.verify_pending", run_id=self._run_id())
        self._set_stage("verify", verify={"finding_id": finding.id, "attempt_id": attempt.id, "corrector": corrector})

    def _stage_verify(self) -> None:
        comp = self._component()
        info = self.run.data.get("verify") or {}
        finding = self.store.require("finding", info["finding_id"])
        attempt = self.store.require("correction_attempt", info["attempt_id"])
        corrector = info["corrector"]
        verifier, rationale = self._role_of("verifier", author=corrector)
        agent = self.agents[verifier]
        rev = self._latest_revision()
        before = [self.store.require("render", rid) for rid in attempt.data.get("before_render_ids", [])]
        after = [self.store.require("render", rid) for rid in attempt.data.get("after_render_ids", [])]
        # a close-up in the finding's own view, BEFORE (the attempt's base revision) and AFTER (R-61, R-65)
        aligned = self._aligned_closeup_def(finding)
        if aligned is not None:
            before_rev = self.store.get("revision", before[0].data["revision_id"]) if before else None
            if before_rev is not None and before_rev.id != rev.id:
                before += self._render_component(before_rev, comp, f"close-up in the finding's view, before {finding.id}", only_defs=[aligned])
            after += self._render_component(rev, comp, f"close-up in the finding's view, after {finding.id}", only_defs=[aligned])
            attempt.data["aligned_closeup_view"] = aligned["name"]
        measurement = self._measure(rev, comp)
        evidence = self._reference_items()
        evidence += [EvidenceItem(Path(r.data["file"]), "render", {"render_id": r.id, "revision_id": r.data["revision_id"],
                                                                     "view": r.data["view_name"], "note": "BEFORE",
                                                                     **({"label": "close-up in the finding's own view"} if aligned and r.data["view_name"] == aligned["name"] else {})})
                     for r in before if r.state == "ok"]
        evidence += self._crop_items(before, [finding.data["part_id"]], note="BEFORE")
        evidence += [EvidenceItem(Path(r.data["file"]), "render", {"render_id": r.id, "revision_id": r.data["revision_id"],
                                                                     "view": r.data["view_name"], "note": "AFTER",
                                                                     **({"label": "close-up in the finding's own view"} if aligned and r.data["view_name"] == aligned["name"] else {})})
                     for r in after if r.state == "ok"]
        evidence += self._crop_items(after, [finding.data["part_id"]], note="AFTER")
        if measurement:
            evidence.append(measurement)
        task = self._new_task("verify", comp, verifier, rationale,
                              "verdict on the expected improvement with regressions checked", [finding.data["part_id"]],
                              finding_id=finding.id, attempt_id=attempt.id)
        packet, pdir = self.packets.build(
            kind="verification", agent_id=agent.id, task=task,
            objective=f"Verify finding {finding.id} on {finding.data['part_id']} using the BEFORE and AFTER renders. Expected "
                      f"improvement: {finding.data.get('expected_improvement')}. Report improved, unchanged, regressed, or "
                      "uncertain, and mention any regression elsewhere in the views.",
            brief_text=self._brief_text(), evidence=evidence, open_findings=[finding], constraints=[],
            withheld=[{"what": "corrector self-assessment", "why": "R-74: closure is verified on renders, never on claims"}],
            schema_name="verification_report", schema=SCHEMAS["verification_report"], changed=None,
            records={"findings": [finding], "correction_attempts": [attempt]})
        task = self._finish_task(task, "in_progress", packet_id=packet.id)
        outcome, inv = self._invoke(verifier, "verification", packet=packet, pdir=pdir, schema_name="verification_report",
                                    images=self._images_of(packet, pdir),
                                    extra={"finding_id": finding.id, "before_render_ids": [r.id for r in before],
                                           "after_render_ids": [r.id for r in after]})
        if not outcome.ok:
            self._finish_task(task, "blocked", error=f"malformed verification output: {outcome.errors}")
            raise EngineFailure(f"verification task {task.id}: agent {verifier} returned malformed output")
        report = outcome.value
        verdict = report["verdict"]
        if report.get("finding_id") != finding.id:
            verdict = "uncertain"
            report["note"] = f"verifier referred to {report.get('finding_id')}, not {finding.id}"
        views = self._views(comp, rev)
        after_ids = [r.id for r in after]
        fresh, reasons = renders_fresh(after, required_view_ids=[views[n].id for n in COVERAGE_VIEWS], current_revision_id=rev.id,
                                       expected_keys={views[n].id: self._cache_key(rev, views[n]) for n in views})
        new_evidence = bool(after_ids) and set(after_ids).isdisjoint(set(attempt.data.get("before_render_ids", [])))
        attempt.state = verdict
        attempt.data.update({"verifier_agent_id": agent.id, "verification": report, "invocation_id": inv.id,
                             "renders_fresh": fresh, "new_evidence": new_evidence, "ended_at": utc_now()})
        self.store.upsert(attempt, actor="engine", event=f"correction_attempt.{verdict}", run_id=self._run_id())
        self._finish_task(task, "done", result=report, invocation_id=inv.id)
        self._record_coverage([{"part_id": finding.data["part_id"], "view": v, "instances_inspected": "all"} for v in COVERAGE_VIEWS],
                              rev, agent.id, inv.id, comp)
        if verdict == "improved" and fresh and new_evidence:
            finding.state = "closed"
            finding.data["after_render_ids"] = after_ids
            finding.data["resolution"] = {"verdict": "improved", "verified_by": agent.id, "invocation_id": inv.id,
                                          "attempt_id": attempt.id, "at": utc_now(), "rationale": report.get("rationale")}
            self.store.upsert(finding, actor="engine", event="finding.closed", run_id=self._run_id(),
                              evidence=after_ids, inputs={"verdict": verdict, "renders_fresh": fresh})
        else:
            note = "" if verdict != "improved" else f"verdict improved but renders_fresh={fresh}, new_evidence={new_evidence}"
            if self.limits.correction_allowed(finding.id):
                finding.state = "open"
            else:
                finding.state = "reassess"
            finding.data["last_verdict"] = {"verdict": verdict, "note": note, "attempt_id": attempt.id}
            self.store.upsert(finding, actor="engine", event=f"finding.{finding.state}", run_id=self._run_id(),
                              inputs={"verdict": verdict, "note": note})
        self._set_stage("correct", verify=None)

    def _aligned_closeup_def(self, finding: Record) -> dict[str, str] | None:
        """A close-up framed on the finding's part in the direction of the view the finding names, when that view is
        a standard one and not already the three-quarter close-up direction."""
        view_name = str(finding.data.get("view") or "")
        direction = next((d["direction"] for d in STANDARD_VIEW_DEFS if d["name"] == view_name), None)
        if direction is None or direction == closeup_view_def(finding.data["part_id"])["direction"]:
            return None
        return closeup_view_def(finding.data["part_id"], direction)

    def _stage_reassess(self) -> None:
        comp = self._component()
        pending = [f for f in self._open_findings(comp) if f.state == "reassess" and not f.data.get("reassessed")]
        if not pending:
            self._set_stage("correct")
            return
        finding = pending[0]
        last_corrector = self._label_of(finding.data["owner_agent_id"]) if finding.data.get("owner_agent_id") else self._label_of(finding.data["reported_by"])
        reassessor, reassess_rationale = self._role_of("reassessor", author=last_corrector)
        agent = self.agents[reassessor]
        rev = self._latest_revision()
        attempts = [self.store.require("correction_attempt", aid) for aid in finding.data.get("attempts", [])]
        evidence = self._reference_items()
        for a in attempts:
            for rid in a.data.get("before_render_ids", []) + a.data.get("after_render_ids", []):
                r = self.store.get("render", rid)
                if r and r.state == "ok":
                    evidence.append(EvidenceItem(Path(r.data["file"]), "render", {"render_id": r.id, "revision_id": r.data["revision_id"],
                                                                                   "view": r.data["view_name"], "note": f"attempt {a.data.get('attempt_no')}"}))
        measurement = self._measure(rev, comp)
        if measurement:
            evidence.append(measurement)
        task = self._new_task("reassess", comp, reassessor, reassess_rationale,
                              "cause classification and a materially different approach or an evidence-gap report",
                              [finding.data["part_id"]], finding_id=finding.id)
        history = "\n".join(f"- attempt {a.data.get('attempt_no')}: verdict {a.state}; ops {a.data.get('op_ids')}" for a in attempts)
        packet, pdir = self.packets.build(
            kind="reassessment", agent_id=agent.id, task=task,
            objective=f"Two corrections of finding {finding.id} ({finding.data['observed_mismatch']}) did not verify. Reassess "
                      "whether the cause is geometry, camera, material, lighting, or insufficient reference evidence, and propose "
                      "a materially different approach, or report an evidence gap. Increasing detail is not progress.",
            brief_text=self._brief_text(), evidence=evidence, open_findings=[finding], constraints=[], withheld=[],
            schema_name="reassessment", schema=SCHEMAS["reassessment"], changed=None,
            extra_sections={"Attempt history": history}, records={"findings": [finding], "correction_attempts": attempts})
        task = self._finish_task(task, "in_progress", packet_id=packet.id)
        outcome, inv = self._invoke(reassessor, "reassessment", packet=packet, pdir=pdir, schema_name="reassessment",
                                    images=self._images_of(packet, pdir), extra={"finding_id": finding.id})
        if not outcome.ok:
            self._finish_task(task, "blocked", error=f"malformed reassessment: {outcome.errors}")
            raise EngineFailure(f"reassessment task {task.id}: agent {reassessor} returned malformed output")
        self._finish_task(task, "done", result=outcome.value, invocation_id=inv.id)
        finding.data["reassessment"] = outcome.value
        finding.data["reassessed"] = True
        finding.data["reassigned_to"] = agent.id
        if outcome.value.get("evidence_gap"):
            finding.state = "evidence_gap"
        finding.data["study_request_ids"] = self._record_study_requests(outcome.value.get("study_requests") or [],
                                                                        requested_by=f"agent:{reassessor}", finding=finding)
        self.store.upsert(finding, actor="engine", event="finding.reassessed", run_id=self._run_id(),
                          inputs={"cause": outcome.value.get("cause"), "by": reassessor})
        self._set_stage("correct")

    def _record_study_requests(self, requests: list[dict[str, Any]], *, requested_by: str, finding: Record) -> list[str]:
        """Addendum R-104: an agent may ask for a per-piece study. With a concept stage and an approved anchor the art
        director writes the prompt at once; otherwise the request is recorded for the owner (`concept study`)."""
        from .concept import ConceptError, ConceptStage

        ids: list[str] = []
        if not requests:
            return ids
        concept = ConceptStage(self)
        plan = concept.plan
        for sr in requests:
            part_id = sr.get("part_id") or finding.data.get("part_id")
            if self.store.get("part", part_id) is None:
                self.store.append_journal(actor="engine", event="study_request.refused", run_id=self._run_id(),
                                          inputs={"part_id": part_id, "reason": "unknown part"})
                continue
            if plan is not None and plan.data.get("anchor_reference_id"):
                try:
                    rec = concept.study(part_id, view=sr.get("view") or "front", purpose=sr.get("purpose") or "evidence gap",
                                        region=sr.get("region"), draft_prompt=sr.get("draft_prompt"), requested_by=requested_by,
                                        actor="engine")
                except ConceptError as exc:
                    self.store.append_journal(actor="engine", event="study_request.not_generated", run_id=self._run_id(),
                                              inputs={"part_id": part_id, "reason": str(exc)[:300]})
                    rec = self._plain_study(sr, part_id, requested_by, finding)
            else:
                rec = self._plain_study(sr, part_id, requested_by, finding)
            ids.append(rec.id)
        return ids

    def _plain_study(self, sr: dict[str, Any], part_id: str, requested_by: str, finding: Record) -> Record:
        rec = Record.new("study_request", {"part_id": part_id, "view": sr.get("view") or "front", "region": sr.get("region"),
                                           "purpose": sr.get("purpose") or "evidence gap", "draft_prompt": sr.get("draft_prompt"),
                                           "requested_by": requested_by, "finding_id": finding.id, "created_at": utc_now(),
                                           "reference_id": None, "generation_id": None,
                                           "note": "no concept stage with an approved anchor: generate with `concept study` "
                                                   "after `concept start` (R-104)"})
        return self.store.upsert(rec, actor="engine", event="study_request.created", run_id=self._run_id(),
                                 inputs={"part_id": part_id, "requested_by": requested_by, "finding_id": finding.id})

    def _stage_gate(self) -> None:
        comp = self._component()
        rev = self._latest_revision()
        self._render_component(rev, comp, "gate")
        facts = self._ready_facts(comp, rev)
        if comp.state != "ready_for_user_review":
            try:
                comp = transition(self.store, comp, "ready_for_user_review", actor="engine",
                                  reason="all findings closed or waived, renders fresh, coverage met, no pending operations",
                                  context=facts, run_id=self._run_id())
            except IllegalTransition as exc:
                if facts["open_findings"]:
                    self._set_stage("correct")
                    return
                raise EngineFailure(f"gate refused: {exc}; facts {facts}") from None
        holder = self.ownership.current("assembly")
        if holder is not None:
            self.ownership.release(holder.data["token"], actor="engine", reason="component ready for user review (R-44)")
        self._checkpoint(rev, f"review-ready {comp.id}", "R-46: checkpoint at ready_for_user_review")
        if not [a for a in self.store.list("acceptance") if a.data.get("component_id") == comp.id and a.state == "unaccepted"]:
            self.store.upsert(Record.new("acceptance", {"component_id": comp.id}), actor="engine", event="acceptance.created")
        self._set_stage("done")
        if self.run.data.get("attended", True):
            self._stop("waiting_for_user", "ready_for_user_review",
                       f"component {comp.data['name']} is ready for user review at {rev.id}; it is not accepted")
        else:
            self._stop("paused", "ready_for_user_review",
                       f"unattended run finished component {comp.data['name']} at {rev.id}; left unaccepted for user review")

    def _stage_done(self) -> None:
        raise EngineFailure("stage done: nothing to do; accept, reopen, or restore to continue")

    # -------------------------------------------------------------- user actions

    def accept_component(self, component_id: str, *, user: str) -> Record:
        comp = self.store.require("component", component_id)
        if comp.state != "ready_for_user_review":
            raise EngineFailure(f"component {component_id} is {comp.state}, not ready_for_user_review")
        rev = self._latest_revision()
        acc = next((a for a in self.store.list("acceptance") if a.data.get("component_id") == comp.id and a.state == "unaccepted"), None)
        if acc is None:
            acc = self.store.upsert(Record.new("acceptance", {"component_id": comp.id}), actor="engine", event="acceptance.created")
        evidence_versions = {r.id: r.data.get("version") for r in self.references.current()}
        evidence_versions.update({r.id: r.version for r in self.store.list("render", state="ok") if r.data.get("revision_id") == rev.id})
        dependency_versions = {p: (self.store.get("part", p).version if self.store.get("part", p) else None)
                               for p in comp.data.get("part_ids") or []}
        acc = transition(self.store, acc, "accepted_at_revision", actor=user, reason="user accepted the component",
                         inputs={"revision_id": rev.id, "evidence_versions": evidence_versions,
                                 "dependency_versions": dependency_versions}, run_id=self._run_id())
        self._emit("accepted", component_id=comp.id, revision_id=rev.id)
        return acc

    def reopen_component(self, component_id: str, *, reason: str, user: str) -> Record:
        comp = self.store.require("component", component_id)
        for acc in self.store.list("acceptance", state="accepted_at_revision"):
            if acc.data.get("component_id") == comp.id:
                transition(self.store, acc, "superseded", actor=user, reason=reason,
                           inputs={"supersede_reason": "user_reopen", "note": reason}, run_id=self._run_id())
        if comp.state != "unreviewed":
            comp = transition(self.store, comp, "unreviewed", actor=user, reason=f"reopened: {reason}", run_id=self._run_id())
        self.store.upsert(Record.new("acceptance", {"component_id": comp.id}), actor="engine", event="acceptance.created")
        self.store.append_journal(actor=user, event="component.reopened", record_kind="component", record_id=comp.id,
                                  inputs={"reason": reason}, run_id=self._run_id())
        if self.run is not None:
            self.run.data["reopen_reason"] = reason
            self._set_stage("build")
        return comp

    def waive_finding(self, finding_id: str, *, rationale: str, user: str) -> Record:
        f = self.store.require("finding", finding_id)
        if not rationale.strip():
            raise EngineFailure("a waiver requires a rationale (R-75)")
        f.state = "waived"
        f.data["resolution"] = {"verdict": "waived", "rationale": rationale, "user": user, "at": utc_now()}
        f = self.store.upsert(f, actor=user, event="finding.waived", inputs={"rationale": rationale}, run_id=self._run_id())
        self.store.upsert(Record.new("waiver", {"finding_id": f.id, "rationale": rationale, "user": user}), actor=user,
                          event="waiver.recorded")
        return f

    def request_correction(self, finding_id: str, *, user: str, note: str = "") -> Record:
        f = self.store.require("finding", finding_id)
        f.state = "open"
        f.data.setdefault("user_requests", []).append({"note": note, "user": user, "at": utc_now()})
        f = self.store.upsert(f, actor=user, event="finding.correction_requested", run_id=self._run_id())
        if self.run is not None:
            self._set_stage("correct")
        return f

    def restore_checkpoint(self, checkpoint_id: str, *, user: str) -> Record:
        ck = self.store.require("checkpoint", checkpoint_id)
        if self.ownership.current("assembly") is not None:
            raise EngineFailure("ownership is held; release or hand off before restoring (R-46)")
        src = next((f for f in ck.data["files"] if f["role"] == "revision"), None)
        if src is None or not Path(src["path"]).is_file() or sha256_file(src["path"]) != src["sha256"]:
            raise EngineFailure(f"checkpoint {ck.id} revision file missing or corrupted (R-48)")
        parent = self.store.require("revision", ck.data["revision_id"])
        rev = self.operations.register_revision(src["path"], parent_revision_id=parent.id, created_by_op_id=None, actor=user,
                                                identity_map=parent.data.get("identity_map") or [],
                                                asset_dependencies=parent.data.get("asset_dependencies") or {},
                                                note=f"restored from checkpoint {ck.id} ({ck.data.get('name')})")
        for comp in self.store.list("component"):
            for acc in self.store.list("acceptance", state="accepted_at_revision"):
                if acc.data.get("component_id") == comp.id and acc.data.get("revision_id") != rev.id:
                    transition(self.store, acc, "superseded", actor=user, reason="checkpoint restore",
                               inputs={"supersede_reason": "checkpoint_restore"}, run_id=self._run_id())
            if comp.state != "unreviewed":
                transition(self.store, comp, "unreviewed", actor=user, reason="checkpoint restore invalidates review (R-39)",
                           run_id=self._run_id())
        self.store.append_journal(actor=user, event="checkpoint.restored", inputs={"checkpoint_id": ck.id, "revision_id": rev.id},
                                  run_id=self._run_id())
        if self.run is not None:
            self._set_stage("build")
        return rev

    # ------------------------------------------------------------------- status

    def findings(self, open_only: bool = False) -> list[dict[str, Any]]:
        out = []
        for f in self.store.list("finding"):
            if open_only and f.state not in OPEN_FINDING_STATES:
                continue
            out.append({"id": f.id, "state": f.state, "part_id": f.data.get("part_id"), "view": f.data.get("view"),
                        "severity": f.data.get("severity"), "confidence": f.data.get("confidence"),
                        "observed_mismatch": f.data.get("observed_mismatch"), "attempts": len(f.data.get("attempts") or []),
                        "resolution": f.data.get("resolution")})
        return out

    def checkpoints(self) -> list[dict[str, Any]]:
        return [{"id": c.id, "name": c.data.get("name"), "revision_id": c.data.get("revision_id"), "created_at": c.data.get("created_at")}
                for c in self.store.list("checkpoint")]

    def status(self) -> dict[str, Any]:
        comps = []
        for c in self.store.list("component"):
            accs = [a for a in self.store.list("acceptance") if a.data.get("component_id") == c.id]
            accepted = [a for a in accs if a.state == "accepted_at_revision"]
            comps.append({"id": c.id, "name": c.data.get("name"), "review_state": c.state,
                          "acceptance_state": "accepted_at_revision" if accepted else ("unaccepted" if accs or True else "unaccepted"),
                          "open_findings": len(self._open_findings(c)), "part_ids": c.data.get("part_ids") or [],
                          "revision_id": self.operations.latest_revision().id if self.operations.latest_revision() else None})
        contributions = {label: sum(1 for o in self.store.list("operation", state="committed") if o.data.get("agent_id") == a.id)
                         for label, a in self.agents.items()}
        agents = []
        for label, a in self.agents.items():
            invs = [i for i in self.store.list("invocation") if i.data.get("agent_id") == a.id]
            pf = self.preflight_reports.get(label) or {}
            agents.append({"label": label, "id": a.id, "provider": a.data.get("provider"), "model": a.data.get("model"),
                           "reasoning": a.data.get("reasoning"), "invocations": len(invs),
                           "effective_settings": invs[-1].data.get("effective_settings") if invs else {},
                           "preflight_ok": pf.get("ok"), "cli_path": (pf.get("local") or {}).get("cli_path"),
                           "cli_version": (pf.get("local") or {}).get("version"),
                           "reasoning_fallbacks": pf.get("reasoning_fallbacks") or []})
        open_findings = self.findings(open_only=True)
        briefs = self.store.list("brief")
        uncertainties = [f for f in open_findings if f["state"] in ("evidence_gap", "reassess")]
        if briefs:
            uncertainties += [{"unresolved_decision": q} for q in (briefs[-1].data.get("structured") or {}).get("unresolved_decisions") or []]
        from .concept import ConceptStage

        return {
            "run_id": self.run.id if self.run else None,
            "execution": self.run.state if self.run else "idle",
            "concept": ConceptStage(self).summary(),
            "stop_reason": self.run.data.get("stop_reason") if self.run else None,
            "stage": self.run.data.get("stage") if self.run else None,
            "attended": self.run.data.get("attended") if self.run else self.config.attended,
            "assignment_overrides": self.assignment_overrides(),
            "revision_id": self.operations.latest_revision().id if self.operations.latest_revision() else None,
            "components": comps, "consumption": self.limits.status(), "agents": agents, "contributions": contributions,
            "findings": self.findings(), "remaining_discrepancies": open_findings, "uncertainties": uncertainties,
            "questions_for_user": (self.run.data.get("questions_for_user") if self.run else []) or [],
            "notes": (self.run.data.get("notes") if self.run else []) or [], "checkpoints": self.checkpoints(),
            "blender_version": self.blender_version,
        }


def _brief_to_text(b: dict[str, Any]) -> str:
    lines = [f"Intended asset: {b.get('intended_asset')}",
             "Authoritative references: " + ", ".join(b.get("authoritative_references") or []),
             f"Modeling scope: {b.get('modeling_scope')}",
             "Deliverables: " + ", ".join(b.get("deliverables") or []),
             f"Scale and coordinates: {b.get('scale_and_coordinates')}",
             "Symmetry and pose assumptions: " + ("; ".join(b.get("symmetry_and_pose_assumptions") or []) or "none"),
             "Fidelity priorities: " + ", ".join(b.get("fidelity_priorities") or []),
             "Observed vs inferred construction:"]
    for s in b.get("observed_vs_inferred_construction") or []:
        lines.append(f"  - [{s.get('status')}] {s.get('statement')}")
    lines.append("Missing evidence: " + ("; ".join(b.get("missing_evidence") or []) or "none"))
    lines.append("Unresolved decisions: " + ("; ".join(q.get("question", "") for q in b.get("unresolved_decisions") or []) or "none"))
    return "\n".join(lines)
