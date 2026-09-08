"""Command-line runner for the Collaborative Model Builder (spec R-90). Thin: every verb calls the engine.

    python -m builder <verb> ...        or        python main.py --build <verb> ...

Agents are the real claude, gemini, and codex CLI adapters named in ``config.yaml`` (``builder.agents``), or
scripted mocks with ``--mock <screenplay.json>``. Live preflight and runs with real adapters spend provider
usage and are never started implicitly: ``preflight --live`` is an explicit step. Blender is real whenever it
is installed.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .blender.runner import BlenderRunner
from .concept import ConceptError, ConceptStage
from .config import BuilderConfig, default_workflow_dir, discover_blender, slugify
from .engine import Engine, EngineFailure
from .project import Project
from .providers import make_adapter
from .providers.mock import ScriptedAdapter
from .references import References
from .source import SourceError, register_empty, register_source

RunnerFactory = Callable[[BuilderConfig, Project], Any]
AdaptersFactory = Callable[[BuilderConfig, argparse.Namespace], dict[str, Any]]


class CliError(Exception):
    pass


class Ctx:
    def __init__(self, out: Any, runner_factory: RunnerFactory | None, adapters_factory: AdaptersFactory | None):
        self.out = out
        self.runner_factory = runner_factory
        self.adapters_factory = adapters_factory

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def config(self, args: argparse.Namespace) -> BuilderConfig:
        path = getattr(args, "config", None)
        return BuilderConfig.load(Path(path)) if path else BuilderConfig.load()

    def project(self, args: argparse.Namespace) -> Project:
        wf = Path(args.workflow_dir)
        if not Project.exists(wf):
            raise CliError(f"no workflow project at {wf} (expected project.json); create one with `new` or `preset create`")
        return Project.open(wf)

    def runner(self, cfg: BuilderConfig, project: Project, *, required: bool = True) -> Any:
        if self.runner_factory is not None:
            return self.runner_factory(cfg, project)
        exe = discover_blender(cfg.blender_executable)
        if exe is None:
            if required:
                raise CliError("Blender was not found; set builder.blender.executable in config.yaml or install Blender under "
                               r"C:\Program Files\Blender Foundation")
            return None
        return BlenderRunner(exe, logs_dir=project.path("logs"), deadlines=cfg.deadlines)

    def adapters(self, cfg: BuilderConfig, args: argparse.Namespace) -> dict[str, Any]:
        mock = getattr(args, "mock", None)
        if mock:
            with open(mock, "r", encoding="utf-8") as f:
                play = json.load(f)
            return {label: ScriptedAdapter(label, play.get(label, {})) for label in cfg.agents}
        if self.adapters_factory is not None:
            return self.adapters_factory(cfg, args)
        adapters: dict[str, Any] = {}
        for label, binding in cfg.agents.items():
            try:
                adapters[label] = make_adapter(binding)
            except ValueError as exc:
                raise CliError(str(exc)) from None
        return adapters

    def engine(self, cfg: BuilderConfig, project: Project, adapters: dict[str, Any], runner: Any, *, verbose: bool = False) -> Engine:
        observer = (lambda ev: self.say(_format_event(ev))) if verbose else None
        return Engine(project, cfg, adapters=adapters, runner=runner, observer=observer)


def _format_event(ev: dict[str, Any]) -> str:
    kind = ev.get("event")
    if kind == "stage":
        return f"[stage] {ev.get('stage')}"
    if kind == "invocation":
        return f"[agent {ev.get('label')}] {ev.get('purpose')}: {ev.get('outcome')}"
    if kind == "operation":
        return f"[operation] {ev.get('op_id')} by {ev.get('agent')}: {ev.get('state')}"
    if kind == "run.stopped":
        return f"[stop] {ev.get('stop_reason')}: {ev.get('note')}"
    return f"[{kind}] " + ", ".join(f"{k}={v}" for k, v in ev.items() if k not in ("event", "at"))


# ----------------------------------------------------------------------------- verbs

def cmd_new(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.config(args)
    wf = Path(args.workflow_dir) if args.workflow_dir else default_workflow_dir(args.project_dir, slugify(args.name), cfg)
    extra = {"first_component": args.first_component} if args.first_component else {}
    prj = Project.create(wf, name=args.name, asset_name=args.asset or args.name, source_project_dir=args.project_dir, extra=extra)
    try:
        ctx.say(f"created workflow project {prj.record.id} at {wf}")
        if args.ref:
            _intake(prj, args, ctx)
    finally:
        prj.close()
    return 0


def _intake(prj: Project, args: argparse.Namespace, ctx: Ctx) -> None:
    labels = args.labels or []
    if len(labels) not in (0, len(args.ref)):
        raise CliError("give one --labels value per --ref (comma-separated labels)")
    refs = References(prj)
    for i, path in enumerate(args.ref):
        lab = [x.strip() for x in (labels[i] if labels else "other").split(",") if x.strip()]
        rec = refs.add(path, labels=lab, kind=getattr(args, "kind", "target") or "target",
                       composite=bool(getattr(args, "composite", False)), notes=getattr(args, "notes", "") or "")
        ctx.say(f"added reference {rec.id} v{rec.data['version']} {rec.data['width']}x{rec.data['height']} labels={lab}")
    for region in getattr(args, "region", None) or []:
        ref_id, name, coords = region.split(":", 2)
        bbox = [int(v) for v in coords.split(",")]
        reg = refs.add_region(ref_id, name, bbox, purpose="target_region")
        ctx.say(f"added target region {reg.id} on {ref_id}: {name} {bbox}")
    ready, reasons = refs.intake_ready()
    ctx.say(f"{len(refs.current())} reference(s); intake ready: {ready}" + ("" if ready else " (" + "; ".join(reasons) + ")"))


def cmd_intake(args: argparse.Namespace, ctx: Ctx) -> int:
    prj = ctx.project(args)
    try:
        _intake(prj, args, ctx)
    finally:
        prj.close()
    return 0


def cmd_open(args: argparse.Namespace, ctx: Ctx) -> int:
    return cmd_status(args, ctx)


def cmd_preset(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.config(args)
    if args.preset_cmd == "list":
        if not cfg.presets:
            ctx.say("no presets under builder.presets in config.yaml")
        for pid, p in cfg.presets.items():
            ctx.say(f"{pid}: {p.get('name', pid)} (asset {p.get('asset', '?')}; first component {p.get('first_component', '?')})")
        return 0
    preset = cfg.presets.get(args.preset_id)
    if preset is None:
        raise CliError(f"unknown preset {args.preset_id!r}; known: {', '.join(cfg.presets) or 'none'}")
    if args.preset_cmd == "show":
        ctx.say(json.dumps(preset, ensure_ascii=False, indent=1))
        return 0
    prj = Project.create_from_preset(args.preset_id, preset, workflow_dir=args.workflow_dir, config=cfg)
    try:
        ctx.say(f"created workflow project {prj.record.id} at {prj.workflow_dir} from preset {args.preset_id}")
        ctx.say(prj.record.data.get("status_note", ""))
    finally:
        prj.close()
    return 0


def _print_preflight(report: dict[str, Any], ctx: Ctx) -> None:
    if "blender" in report:
        b = report["blender"]
        ctx.say(f"blender: {b.get('blender_version')} (python {b.get('python_version')}); engines ok: {', '.join(b.get('engines_ok') or [])}")
    for label, entry in report.items():
        if label == "blender":
            continue
        if label == "I":
            _print_seat_preflight(entry, ctx)
            continue
        local = entry.get("local") or {}
        eff = entry.get("effective_settings") or {}
        ctx.say(f"agent {label}: provider={entry['provider']} model={entry['model']} reasoning={entry['reasoning'] or 'not exposed'}"
                f" -> {'OK' if entry['ok'] else 'BLOCKED'}")
        ctx.say(f"  cli: {local.get('cli_path')} version={local.get('version')} via={local.get('resolved_via')}"
                + (f" script={local.get('script')}" if local.get("script") else ""))
        if local.get("missing_flags"):
            ctx.say(f"  missing in --help: {', '.join(local['missing_flags'])}")
        for err in local.get("errors") or []:
            ctx.say(f"  local check error: {err}")
        if eff:
            ctx.say(f"  effective: model_reported={eff.get('model_reported')} reasoning_requested={eff.get('reasoning_requested')} "
                    f"reasoning_effective={eff.get('reasoning_effective')}")
        for fb in entry.get("reasoning_fallbacks") or []:
            ctx.say(f"  REASONING DOWNGRADE (reported, not silent): requested {fb.get('requested')} -> accepted {fb.get('accepted')}: "
                    f"{str(fb.get('error'))[:200]}")
        for cap, tiers in entry["capabilities"].items():
            ctx.say(f"  {cap:24s} declared={tiers.get('declared'):11s} local={tiers.get('local'):8s} live={tiers.get('live')}"
                    + (f"  ({tiers['evidence']})" if tiers.get("evidence") else "")
                    + (f"  [local: {tiers['local_evidence']}]" if tiers.get("local") != "ok" and tiers.get("local_evidence") else ""))
        for b in entry["blockers"]:
            ctx.say(f"  BLOCKER: {b}")


def _print_seat_preflight(entry: dict[str, Any], ctx: Ctx) -> None:
    """Addendum R-109: the image seat's three tiers, live reported as not applicable for the manual seat."""
    d = entry.get("declared") or {}
    tiers = entry.get("tiers") or {}
    ctx.say(f"image seat I: seat={d.get('seat')} vendor={d.get('vendor')} model={d.get('model')} -> "
            f"{'OK' if entry.get('ok') else 'BLOCKED'}")
    ctx.say(f"  declared={tiers.get('declared')}  local={tiers.get('local')}  live={tiers.get('live')}")
    ctx.say(f"  local: {(entry.get('local') or {}).get('evidence')}")
    ctx.say(f"  live: {(entry.get('live') or {}).get('evidence')}")
    for b in entry.get("blockers") or []:
        ctx.say(f"  BLOCKER: {b}")


def _ensure_preflight(eng: Engine, adapters: dict[str, Any], args: argparse.Namespace, ctx: Ctx) -> None:
    """Real adapters spend money: a current live preflight report is required and never produced implicitly (R-18,
    R-21); scripted mocks are probed on the spot."""
    loaded = eng.load_preflight()
    # a local-only report (live tiers never run) is not a live preflight for a real adapter (R-18, R-21)
    local_only = {label for label, r in loaded.items() if not r.get("live") and not isinstance(adapters.get(label), ScriptedAdapter)}
    if len(loaded) < len(adapters) or local_only:
        missing = ", ".join(sorted((set(adapters) - set(loaded)) | local_only))
        if all(isinstance(a, ScriptedAdapter) for a in adapters.values()):
            ctx.say(f"preflight: no current report for {missing}; running preflight (live) on the scripted agents")
            report = eng.preflight(live=True)
            _print_preflight(report, ctx)
        else:
            raise CliError(f"no current live preflight report for agent(s) {missing} (never run, only a local report, or the CLI "
                           f"path, version, model, or settings changed since; R-18). Run `preflight {args.workflow_dir} --live` "
                           "first: it spends provider usage and is never started implicitly.")


def interrupt_handler(eng: Any, say: Callable[[str], None]) -> Callable[[int, Any], None]:
    """Design 9.1: the first Ctrl+C requests a pause at the next safe boundary; the second requests cancel (the
    in-flight provider or Blender process is killed and confirmed, the run reconciled)."""
    state = {"count": 0}

    def handler(signum: int, frame: Any) -> None:
        state["count"] += 1
        if state["count"] == 1:
            eng.pause()
            say("Ctrl+C: pause requested; the engine stops at the next safe boundary (Ctrl+C again to cancel)")
        else:
            eng.cancel_event.set()
            say("Ctrl+C again: cancel requested; killing the in-flight process and reconciling")
    return handler


def cmd_preflight(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.config(args)
    prj = ctx.project(args)
    try:
        runner = ctx.runner(cfg, prj, required=False)
        adapters = ctx.adapters(cfg, args)
        eng = ctx.engine(cfg, prj, adapters, runner)
        try:
            report = eng.preflight(live=args.live, labels=getattr(args, "agent", None) or None)
        except EngineFailure as exc:
            raise CliError(str(exc)) from exc
        _print_preflight(report, ctx)
        return 0 if all(v.get("ok", True) for k, v in report.items() if k != "blender") else 1
    finally:
        prj.close()


def _print_status(status: dict[str, Any], ctx: Ctx) -> None:
    ctx.say(f"execution: {status['execution']}  stop reason: {status.get('stop_reason') or '-'}  stage: {status.get('stage') or '-'}")
    if status.get("revision_id"):
        ctx.say(f"revision: {status['revision_id']} (latest immutable revision)")
    for c in status["components"]:
        ctx.say(f"component {c['id']} {c['name']}: review={c['review_state']} acceptance={c['acceptance_state']} "
                f"open findings={c['open_findings']} revision={c.get('revision_id')}")
        if c["review_state"] == "ready_for_user_review" and c["acceptance_state"] != "accepted_at_revision":
            ctx.say("  ready for user review; not accepted (accept with `accept <component>` or `reopen` it)")
    cons = status["consumption"]
    ctx.say(f"consumption: requests {cons['requests']['completed']} done / {cons['requests']['inflight']} in flight; "
            f"renders {cons['renders']['completed']}; cost measured={cons['cost']['measured']} estimated={cons['cost']['estimated']} "
            f"unknown invocations={cons['cost']['unknown_invocations']}")
    for name, lim in cons["limits"].items():
        if not lim.get("unlimited"):
            ctx.say(f"  limit {name}={lim['value']} enforceable={lim.get('enforceable')} {lim.get('note', '')}")
    for o in status.get("assignment_overrides") or []:
        ctx.say(f"assignment override {o['role']}={o['seat']} ({o['source']}): {o['rationale']}")
    ctx.say(f"contributions (committed operations): {status['contributions']}")
    for a in status["agents"]:
        ctx.say(f"agent {a['label']}: {a['provider']} model={a['model']} reasoning={a['reasoning'] or 'not exposed'} "
                f"invocations={a['invocations']} effective={a.get('effective_settings')}")
    if status["remaining_discrepancies"]:
        ctx.say("remaining discrepancies:")
        for f in status["remaining_discrepancies"]:
            ctx.say(f"  {f['id']} [{f['state']}] {f['part_id']}: {f['observed_mismatch']}")
    if status["uncertainties"]:
        ctx.say(f"uncertainties: {json.dumps(status['uncertainties'], ensure_ascii=False)}")
    if status["notes"]:
        last = status["notes"][-1]
        ctx.say(f"last note: {last.get('stop_reason')}: {last.get('note')}")


def _status_dict(prj: Project, cfg: BuilderConfig) -> dict[str, Any]:
    eng = Engine(prj, cfg, adapters={}, runner=None)
    eng.attach()
    status = eng.status()
    status["project"] = {"id": prj.record.id, "name": prj.record.data.get("name"), "asset_name": prj.record.data.get("asset_name"),
                         "preset_id": prj.record.data.get("preset_id"), "first_component": prj.record.data.get("first_component"),
                         "workflow_dir": str(prj.workflow_dir)}
    status["references"] = len(References(prj).current())
    return status


def cmd_status(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.config(args)
    prj = ctx.project(args)
    try:
        status = _status_dict(prj, cfg)
        if getattr(args, "json", False):
            ctx.say(json.dumps(status, ensure_ascii=False, indent=1, default=str))
        else:
            p = status["project"]
            ctx.say(f"project {p['id']} {p['name']} (asset {p['asset_name']}; preset {p['preset_id'] or '-'}; "
                    f"first component {p['first_component'] or '-'}); references: {status['references']}")
            _print_status(status, ctx)
        return 0
    finally:
        prj.close()


def _run_engine(args: argparse.Namespace, ctx: Ctx, *, resume: bool) -> int:
    cfg = ctx.config(args)
    prj = ctx.project(args)
    try:
        runner = ctx.runner(cfg, prj)
        adapters = ctx.adapters(cfg, args)
        eng = ctx.engine(cfg, prj, adapters, runner, verbose=not getattr(args, "quiet", False))
        _ensure_preflight(eng, adapters, args, ctx)
        if resume:
            run = eng.attach()
            if run is None:
                raise CliError("no run to resume; use `start`")
            recovery = eng.recover()
            if recovery["operations"]:
                ctx.say(f"recovery: {json.dumps(recovery, ensure_ascii=False)}")
            eng.resume(user=f"user:{args.user}")
        else:
            attended = not getattr(args, "unattended", False)
            overrides = _parse_assignments(getattr(args, "assign", None))
            eng.start(attended=attended, component_name=getattr(args, "component", None), assignment_overrides=overrides)
            ctx.say(f"run {eng.run.id} started ({'attended' if attended else 'unattended'})")
            for o in eng.assignment_overrides():
                ctx.say(f"assignment override {o['role']}={o['seat']} ({o['source']}): {o['rationale']}")
        import signal

        previous = signal.getsignal(signal.SIGINT)
        try:
            signal.signal(signal.SIGINT, interrupt_handler(eng, ctx.say))
        except (ValueError, OSError):      # not the main thread: no handler, the default KeyboardInterrupt stays
            previous = None
        try:
            status = eng.run_until_stop(max_steps=args.max_steps)
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
        _print_status(status, ctx)
        return 0 if status["stop_reason"] in ("ready_for_user_review", "user_pause", None) else 1
    except EngineFailure as exc:
        raise CliError(str(exc)) from exc
    finally:
        prj.close()


def _parse_assignments(items: list[str] | None) -> dict[str, str]:
    """``--assign role=SEAT`` (repeatable). Roles and seats are validated by the engine against the configured seats."""
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise CliError(f"--assign expects role=SEAT (for example build=B), got {item!r}")
        role, seat = item.split("=", 1)
        out[role.strip()] = seat.strip()
    return out


def cmd_start(args: argparse.Namespace, ctx: Ctx) -> int:
    return _run_engine(args, ctx, resume=False)


def cmd_source(args: argparse.Namespace, ctx: Ctx) -> int:
    """Revision 0 for a project created with `new` (R-93: creating the project never opened the source)."""
    cfg = ctx.config(args)
    prj = ctx.project(args)
    try:
        runner = ctx.runner(cfg, prj)
        user = f"user:{args.user}"
        if args.source_cmd == "register":
            rev = register_source(prj, runner, Path(args.file), actor=user, note=args.note or "")
            src = rev.data.get("source") or {}
            ctx.say(f"registered {rev.id} from {src.get('path')} (sha256 {rev.data['sha256'][:12]}..., validated in separate "
                    f"Blender processes: identities, reopen; the source file is unmodified)")
            unmapped = src.get("unmapped_geometry") or []
            ctx.say(f"  identities: {len(rev.data.get('identity_map') or [])} tagged datablock(s); "
                    f"{len(unmapped)} object(s) without alloy_id" + (f": {', '.join(unmapped[:12])}" if unmapped else ""))
        else:
            rev = register_empty(prj, runner, actor=user, note=args.note or "")
            ctx.say(f"registered {rev.id}: empty scene built through the runner (validated: identities, reopen)")
        ctx.say(f"  revision file: {rev.data['file']} (immutable, R-40)")
        return 0
    except SourceError as exc:
        raise CliError(str(exc)) from exc
    finally:
        prj.close()


def cmd_resume(args: argparse.Namespace, ctx: Ctx) -> int:
    return _run_engine(args, ctx, resume=True)


def cmd_control(args: argparse.Namespace, ctx: Ctx) -> int:
    prj = ctx.project(args)
    try:
        payload: dict[str, Any] = {"by": f"user:{getattr(args, 'user', 'cli')}"}
        if args.verb == "feedback":
            payload["text"] = args.text
        seq = prj.store.push_control(args.verb, payload)
        ctx.say(f"{args.verb} request #{seq} recorded; a running engine applies it at the next safe boundary")
        return 0
    finally:
        prj.close()


def cmd_findings(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.config(args)
    prj = ctx.project(args)
    try:
        eng = Engine(prj, cfg, adapters={}, runner=None)
        eng.attach()
        rows = eng.findings(open_only=args.open)
        if not rows:
            ctx.say("no findings" if not args.open else "no open findings")
        for f in rows:
            ctx.say(f"{f['id']} {f['state']} {f['severity']}/{f['confidence']} {f['part_id']} ({f.get('view') or '-'}): "
                    f"{f['observed_mismatch']}  attempts={f['attempts']}" + (f" resolution={f['resolution']}" if f.get("resolution") else ""))
        return 0
    finally:
        prj.close()


def cmd_accept(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.config(args)
    prj = ctx.project(args)
    try:
        eng = Engine(prj, cfg, adapters={}, runner=None)
        eng.attach()
        acc = eng.accept_component(args.component_id, user=f"user:{args.user}")
        ctx.say(f"{acc.id} {acc.state} at {acc.data['revision_id']} (evidence versions: {len(acc.data['evidence_versions'])})")
        return 0
    except EngineFailure as exc:
        raise CliError(str(exc)) from exc
    finally:
        prj.close()


def cmd_reopen(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.config(args)
    prj = ctx.project(args)
    try:
        eng = Engine(prj, cfg, adapters={}, runner=None)
        eng.attach()
        comp = eng.reopen_component(args.component_id, reason=args.reason, user=f"user:{args.user}")
        ctx.say(f"component {comp.id} {comp.state}; previous acceptances superseded; resume to rebuild")
        return 0
    except EngineFailure as exc:
        raise CliError(str(exc)) from exc
    finally:
        prj.close()


def cmd_waive(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.config(args)
    prj = ctx.project(args)
    try:
        eng = Engine(prj, cfg, adapters={}, runner=None)
        eng.attach()
        f = eng.waive_finding(args.finding_id, rationale=args.rationale, user=f"user:{args.user}")
        ctx.say(f"finding {f.id} waived: {args.rationale}")
        return 0
    except EngineFailure as exc:
        raise CliError(str(exc)) from exc
    finally:
        prj.close()


def cmd_checkpoint(args: argparse.Namespace, ctx: Ctx) -> int:
    cfg = ctx.config(args)
    prj = ctx.project(args)
    try:
        eng = Engine(prj, cfg, adapters={}, runner=None)
        eng.attach()
        if args.ck_cmd == "list":
            rows = eng.checkpoints()
            if not rows:
                ctx.say("no checkpoints")
            for c in rows:
                ctx.say(f"{c['id']}  {c['name']}  revision={c['revision_id']}  {c['created_at']}")
            return 0
        if args.ck_cmd == "create":
            rev = eng._latest_revision()
            ck = eng._checkpoint(rev, args.name, "user requested")
            ctx.say(f"{ck.id} created at {rev.id}")
            return 0
        rev = eng.restore_checkpoint(args.checkpoint_id, user=f"user:{args.user}")
        ctx.say(f"restored: {rev.id} (parent {rev.data['parent_revision_id']}) from checkpoint {args.checkpoint_id}")
        return 0
    except EngineFailure as exc:
        raise CliError(str(exc)) from exc
    finally:
        prj.close()


ACTIVE_RUN_STATES = ("running", "waiting_for_provider", "rendering", "recovering")


def _parse_limit_settings(items: list[str] | None) -> dict[str, str]:
    changes: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise CliError(f"--set expects name=value, got {item!r}")
        name, value = item.split("=", 1)
        changes[name.strip()] = value.strip()
    return changes


def cmd_limits(args: argparse.Namespace, ctx: Ctx) -> int:
    """Show the limits in force and, with ``--set``, change them (R-85, R-89). A stopped run takes the change at once;
    a run that is active in another process takes it at its next safe boundary through a control request."""
    cfg = ctx.config(args)
    prj = ctx.project(args)
    try:
        changes = _parse_limit_settings(getattr(args, "set", None))
        eng = Engine(prj, cfg, adapters={}, runner=None)
        run = eng.attach()
        if changes:
            if run is not None and run.state in ACTIVE_RUN_STATES:
                prj.store.push_control("limits", {"changes": changes, "by": args.user})
                ctx.say(f"run {run.id} is {run.state}: limit change {changes} queued; the engine applies it at its next safe boundary")
            else:
                try:
                    applied = eng.apply_limits(changes, actor=f"user:{args.user}")
                except EngineFailure as exc:
                    raise CliError(str(exc)) from exc
                ctx.say(f"applied {applied}" + (f" to run {run.id} ({run.state})" if run else " (no run yet; the next start uses them)"))
        cons = eng.limits.status()
        ctx.say("limits in force (zero means unlimited):")
        for name, lim in cons["limits"].items():
            ctx.say(f"  {name} = {lim['value']}" + (" (unlimited)" if lim.get("unlimited") else "") + f"  enforceable={lim.get('enforceable')}"
                    + (f"  {lim['note']}" if lim.get("note") else ""))
        ctx.say(f"consumption: requests {cons['requests']['completed']} completed, renders {cons['renders']['completed']} completed, "
                f"elapsed {cons['elapsed_s']} s, steps_without_progress {cons['steps_without_progress']}, "
                f"correction attempts {cons['correction_attempts']}")
        if getattr(args, "json", False):
            ctx.say(json.dumps(cons, ensure_ascii=False, indent=1, default=str))
        return 0
    finally:
        prj.close()


def cmd_journal(args: argparse.Namespace, ctx: Ctx) -> int:
    prj = ctx.project(args)
    try:
        entries = prj.store.journal()
        for e in entries[-args.tail:]:
            ctx.say(f"{e.seq:6d} {e.ts} {e.actor:14s} {e.event:32s} {e.record_kind or '':12s} {e.record_id or e.op_id or ''} "
                    f"{(e.from_state or '') + '->' + (e.to_state or '') if e.to_state else ''}")
        return 0
    finally:
        prj.close()


def cmd_fixture(args: argparse.Namespace, ctx: Ctx) -> int:
    from .fixture import create_fixture

    cfg = ctx.config(args)
    scratch = Project.__new__(Project)  # runner factory needs a project-like logs dir; use the target directory
    scratch.workflow_dir = Path(args.directory)
    scratch.path = lambda *parts: Path(args.directory).joinpath(*parts)  # type: ignore[method-assign]
    runner = ctx.runner(cfg, scratch)
    project, info = create_fixture(args.directory, runner)
    try:
        ctx.say(f"fixture project {project.record.id} at {info['workflow_dir']}; defects: {', '.join(info['defects'])}")
        ctx.say(f"revision 0: {info['revision_id']}; references: {', '.join(info['references'])}")
    finally:
        project.close()
    return 0


def cmd_harness(args: argparse.Namespace, ctx: Ctx) -> int:
    """R-84: ``harness run <dir>`` creates the fixture, runs it with scripted seats (Blender real, agents scripted,
    nothing spent), and measures packet delivery against a simulated transcript replay; ``harness report <wf>``
    measures any existing workflow (a live run's records carry the providers' own token counts)."""
    from . import harness

    cfg = ctx.config(args)
    if args.harness_cmd == "run":
        scratch = Project.__new__(Project)
        scratch.workflow_dir = Path(args.directory)
        scratch.path = lambda *parts: Path(args.directory).joinpath(*parts)  # type: ignore[method-assign]
        runner = ctx.runner(cfg, scratch)
        play = None
        if getattr(args, "mock", None):
            with open(args.mock, "r", encoding="utf-8") as f:
                play = json.load(f)
        project, cmp = harness.run_fixture(args.directory, runner, screenplay=play, config=cfg, max_steps=args.max_steps)
    else:
        project = ctx.project(args)
        cmp = harness.compare(project)
        harness.write_report(project, cmp)
    try:
        if getattr(args, "json", False):
            ctx.say(json.dumps(cmp, ensure_ascii=False, indent=1, default=str))
        else:
            ctx.say(harness.render_report(cmp))
            ctx.say(f"written: {project.workflow_dir / 'harness-report.json'} and harness-report.txt")
    finally:
        project.close()
    return 0


# ----------------------------------------------------------------------- concept (R-110)

CONCEPT_NEEDS_AGENTS = ("start", "import", "approve", "reject", "regenerate", "study")


def _concept_open(args: argparse.Namespace, ctx: Ctx) -> tuple[Project, Engine, ConceptStage]:
    cfg = ctx.config(args)
    prj = ctx.project(args)
    try:
        if args.concept_cmd in CONCEPT_NEEDS_AGENTS and not getattr(args, "no_agents", False):
            runner = ctx.runner(cfg, prj, required=False)
            adapters = ctx.adapters(cfg, args)
            eng = ctx.engine(cfg, prj, adapters, runner, verbose=not getattr(args, "quiet", False))
            _ensure_preflight(eng, adapters, args, ctx)
        else:
            eng = Engine(prj, cfg, adapters={}, runner=None)
        concept = ConceptStage(eng)
    except Exception:
        prj.close()
        raise
    return prj, eng, concept


def _say_events(events: list[str], ctx: Ctx) -> None:
    for e in events:
        ctx.say(f"  - {e}")


def _print_coverage(concept: ConceptStage, ctx: Ctx) -> None:
    cov = concept.coverage()
    ctx.say(f"canon: {cov['canon_state']}  anchor: {cov['anchor'] or '-'}  images: {cov['images']['count']}/{cov['images']['max'] or 'unlimited'}"
            f"  conflicts open: {cov['conflicts_open']}")
    for view, e in cov["views"].items():
        detail = e["approved"] or e["candidates"] or e["requests_open"] or []
        ctx.say(f"  view {view:14s} {e['status']:10s} {', '.join(detail)}")
    for part, e in cov["parts"].items():
        for st in e["studies"]:
            ctx.say(f"  study {part:13s} {st['state']:10s} {st['id']} ({st['view']}) -> {st.get('reference_id') or '-'}")
    if cov["missing"]:
        ctx.say(f"  missing approved views: {', '.join(cov['missing'])}")
    for esc in cov["escalations"]:
        if esc.get("open"):
            ctx.say(f"  ESCALATED to the owner ({esc.get('view') or 'anchor'}): {esc.get('reason')}")
    if cov.get("proceeded_partial"):
        pp = cov["proceeded_partial"]
        ctx.say(f"  proceeded with a partial set by {pp.get('by')} at {pp.get('at')} (missing: {', '.join(pp.get('missing') or []) or 'none'})")


def _print_prompts(concept: ConceptStage, ctx: Ctx) -> None:
    rows = concept.prompts()
    if not rows:
        ctx.say("no open generation requests")
        return
    for r in rows:
        ctx.say(f"request {r['request_id']}  target: {r['target']}  round {r['round']}  images expected: {r['expected_count']}"
                f"  art director: {r['art_director'] or 'owner'}")
        ctx.say(f"  prompt file: {r['prompt_file']}")
        ctx.say("  prompt (paste as-is):")
        for line in (r["prompt"] or "").splitlines() or [""]:
            ctx.say(f"    {line}")
        if r["attachments"]:
            ctx.say("  attach these images, in this order:")
            for a in r["attachments"]:
                ctx.say(f"    {a['path']}  ({a['role']}, {a['reference_id']}, sha256 {str(a['sha256'])[:12]}...)")
        else:
            ctx.say("  attachments: none")
        ctx.say(f"  then: {r['import_command']}")


def cmd_concept(args: argparse.Namespace, ctx: Ctx) -> int:
    prj, eng, concept = _concept_open(args, ctx)
    try:
        ctx.say(concept.mode_text())
        handler = globals()[f"_concept_{args.concept_cmd}"]
        return int(handler(args, ctx, prj, eng, concept))
    except ConceptError as exc:
        raise CliError(str(exc)) from exc
    except EngineFailure as exc:
        raise CliError(str(exc)) from exc
    finally:
        prj.close()


def _concept_start(args, ctx, prj, eng, concept) -> int:
    labels = None
    if args.labels:
        if len(args.labels) != len(args.from_image or []):
            raise CliError("give one --labels value per --from-image (comma-separated labels)")
        labels = [[x.strip() for x in lab.split(",") if x.strip()] for lab in args.labels]
    views = [v.strip() for v in args.views.split(",") if v.strip()] if args.views else None
    plan = concept.start(from_text=args.from_text, from_images=args.from_image or [], labels=labels, approval=args.approval,
                         views=views, actor=f"user:{args.user}")
    ctx.say(f"concept stage {plan.id} started ({plan.data['start_mode']}); canon: {plan.state}; needed views: "
            f"{', '.join(plan.data['needed_views'])}")
    ad = plan.data.get("art_director") or {}
    if ad:
        ctx.say(f"art director {ad.get('seat')}: {ad.get('rationale')}")
    if plan.state == "anchor_approved":
        ctx.say("seed image(s) approved as canon; the first is the anchor (R-99). Writing the canon description and view requests:")
        _say_events(concept.advance(), ctx)
    _print_prompts(concept, ctx)
    _print_coverage(concept, ctx)
    return 0


def _concept_prompts(args, ctx, prj, eng, concept) -> int:
    _print_prompts(concept, ctx)
    return 0


def _concept_import(args, ctx, prj, eng, concept) -> int:
    from .providers.imagegen.base import watch_folder

    actor = f"user:{args.user}"
    files = list(args.files or [])
    request_id = args.request_id
    if args.as_anchor or args.as_view:
        files = [request_id] + files          # with --as-anchor/--as-view every positional is a file (R-96a)
        request_id = None
    imported: list[Any] = []
    if args.watch:
        if request_id is None:
            raise CliError("--watch needs the request id the new files belong to")
        ctx.say(f"watching {args.watch} for new images for request {request_id} (Ctrl+C to stop"
                + (f"; timeout {args.watch_timeout:g}s" if args.watch_timeout else "") + ")")
        count = {"n": 0}

        def on_file(path: Path) -> None:
            refs = concept.import_files(request_id, [path], vendor=args.vendor, model=args.model, attached=args.attached, actor=actor)
            imported.extend(refs)
            count["n"] += 1
            for r in refs:
                ctx.say(f"  imported {path.name} -> {r.id} (candidate, {r.data['sha256'][:12]}...)")
            if args.watch_max and count["n"] >= args.watch_max:
                raise StopIteration
        try:
            result = watch_folder(args.watch, on_file, poll_s=0.5, timeout_s=args.watch_timeout, ignore_existing=not args.watch_existing)
        except KeyboardInterrupt:
            result = {"imported": count["n"], "stopped_by": "keyboard", "errors": []}
        ctx.say(f"watch ended ({result['stopped_by']}): {result['imported']} file(s) imported")
        for err in result.get("errors") or []:
            ctx.say(f"  refused {err['file']}: {err['error']}")
    else:
        if not files:
            raise CliError("give at least one file to import")
        if args.as_anchor:
            imported = concept.import_as("anchor", files, vendor=args.vendor, model=args.model, actor=actor)
            ctx.say(f"request {imported[0].data['generation_id']} created by the owner at import (--as-anchor, R-96a)")
        elif args.as_view:
            imported = concept.import_as("view", files, view=args.as_view, vendor=args.vendor, model=args.model, actor=actor)
            ctx.say(f"request {imported[0].data['generation_id']} created by the owner at import (--as-view {args.as_view}, R-96a)")
        else:
            imported = concept.import_files(request_id, files, vendor=args.vendor, model=args.model, attached=args.attached, actor=actor)
    for r in imported:
        d = r.data.get("declared") or {}
        ctx.say(f"candidate {r.id}: {Path(r.data['file']).name} {r.data['width']}x{r.data['height']} sha256 {r.data['sha256'][:12]}... "
                f"labels={r.data['labels']} declared vendor={d.get('vendor') or '-'} model={d.get('model') or '-'} "
                "(declarations by the owner, not verified)")
    if not imported:
        return 0
    if args.no_check:
        ctx.say("checks skipped (--no-check); run `concept approve`/`reject` or import again without --no-check to run the verdicts")
        return 0
    if not eng.adapters:
        return 0
    ctx.say("advancing: consistency verdicts from both LLM seats where due (spends provider usage), then the mode's decisions")
    _say_events(concept.advance(), ctx)
    _print_coverage(concept, ctx)
    return 0


def _concept_list(args, ctx, prj, eng, concept) -> int:
    st = concept.status()
    ctx.say(f"plan {st['plan_id'] or '-'}  seat: {st['seat']['seat']} ({st['seat']['vendor']})  cost: {st['cost']['kind']}"
            f"  art director: {(st['art_director'] or {}).get('seat') or '-'}  rejected rounds: {st['rejected_rounds']}")
    _print_coverage(concept, ctx)
    ctx.say(f"requests: {st['requests']['by_state']}")
    for c in st["candidates"]:
        ctx.say(f"  candidate {c['id']} {c['labels']} verdicts={c['verdicts']} summary={c['summary']}")
    if st["conflicts_open"]:
        ctx.say(f"  open conflicts: {', '.join(st['conflicts_open'])} (reject one image of each, or regenerate; never averaged)")
    if st["requests"]["open"]:
        ctx.say(f"open requests: {', '.join(st['requests']['open'])} (see `concept prompts`)")
    return 0


def _concept_show(args, ctx, prj, eng, concept) -> int:
    ctx.say(json.dumps(concept.show(args.record_id), ensure_ascii=False, indent=1, default=str))
    return 0


def _concept_approve(args, ctx, prj, eng, concept) -> int:
    refs = concept.approve(args.ids, actor=f"user:{args.user}")
    for r in refs:
        ctx.say(f"approved {r.id} ({', '.join(r.data.get('labels') or [])}) as {r.data.get('precedence_label')} "
                f"(mode {r.data['approval']['mode']}, verdicts {{{', '.join(f'{k}={v.get('verdict')}' for k, v in r.data['approval']['verdicts'].items())}}})")
    _say_events(concept.advance(), ctx)
    _print_coverage(concept, ctx)
    if concept.open_requests():
        ctx.say("open requests: " + ", ".join(g.id for g in concept.open_requests()) + " (see `concept prompts`)")
    return 0


def _concept_reject(args, ctx, prj, eng, concept) -> int:
    refs = concept.reject(args.ids, reason=args.reason, actor=f"user:{args.user}")
    for r in refs:
        ctx.say(f"rejected {r.id}: {args.reason}")
    _say_events(concept.advance(), ctx)
    _print_coverage(concept, ctx)
    return 0


def _concept_regenerate(args, ctx, prj, eng, concept) -> int:
    gen = concept.regenerate(args.record_id, note=args.note or "", actor=f"user:{args.user}")
    ctx.say(f"new request {gen.id} (round {gen.data['round']}, revises {gen.data.get('revises')}) written by art director {gen.data['art_director']}")
    _print_prompts(concept, ctx)
    return 0


def _concept_study(args, ctx, prj, eng, concept) -> int:
    study = concept.study(args.part, view=args.view, region=args.region, purpose=args.purpose or f"study of {args.part}",
                          requested_by=f"user:{args.user}", actor=f"user:{args.user}")
    ctx.say(f"study {study.id} requested for part {args.part} ({args.view}); prompt written")
    _print_prompts(concept, ctx)
    return 0


def _concept_proceed(args, ctx, prj, eng, concept) -> int:
    before = concept.coverage()["missing"]
    plan = concept.proceed(actor=f"user:{args.user}")
    if plan.data.get("proceeded_partial"):
        ctx.say(f"canon {plan.state}: proceeding with a partial set; missing views recorded: {', '.join(before) or 'none'}")
    else:
        ctx.say(f"canon {plan.state}: the needed set is approved")
    _print_coverage(concept, ctx)
    return 0


def _concept_abandon(args, ctx, prj, eng, concept) -> int:
    gen = concept.abandon(args.request_id, reason=args.reason, actor=f"user:{args.user}")
    ctx.say(f"request {gen.id} abandoned; manifest written: {gen.data['manifest_file']}")
    return 0


def _concept_failed(args, ctx, prj, eng, concept) -> int:
    gen = concept.mark_failed(args.request_id, reason=args.reason, actor=f"user:{args.user}")
    ctx.say(f"request {gen.id} marked failed; manifest written: {gen.data['manifest_file']}")
    return 0


# ---------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m builder", description=(
        "Collaborative Model Builder (two AI agents reconstruct a concept design as an editable Blender model; Alloy owns "
        "the state). Verbs: new, open, intake, source, preset, preflight, start, resume, pause, cancel, feedback, status, "
        "findings, accept, reopen, waive, checkpoint, limits, journal, concept, fixture, harness."))
    sub = p.add_subparsers(dest="verb", required=True)

    def wf(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("workflow_dir", help="workflow project directory")
        sp.add_argument("--config", help="config.yaml to read builder settings from (default: Alloy's discovery)")

    sp = sub.add_parser("new", help="create a workflow project (optionally with references)")
    sp.add_argument("--workflow-dir", help="where the workflow state lives (default: <project-dir>\\alloy-builder\\<slug>)")
    sp.add_argument("--name", required=True)
    sp.add_argument("--asset")
    sp.add_argument("--project-dir", help="the user's project directory (recorded, never modified)")
    sp.add_argument("--first-component", help="name of the first detailed component (for example Head)")
    sp.add_argument("--ref", action="append", help="reference image (repeatable)")
    sp.add_argument("--labels", action="append", help="comma-separated labels for the matching --ref")
    sp.add_argument("--kind", default="target", choices=["target", "previous_attempt", "rejected", "hypothesis"])
    sp.add_argument("--composite", action="store_true")
    sp.add_argument("--config")
    sp.set_defaults(func=cmd_new)

    sp = sub.add_parser("open", help="open a workflow project and print its status")
    wf(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_open)

    sp = sub.add_parser("intake", help="register reference images and target regions")
    wf(sp)
    sp.add_argument("--ref", action="append", required=True)
    sp.add_argument("--labels", action="append")
    sp.add_argument("--kind", default="target", choices=["target", "previous_attempt", "rejected", "hypothesis"])
    sp.add_argument("--composite", action="store_true")
    sp.add_argument("--notes", default="")
    sp.add_argument("--region", action="append", help="<reference_id>:<name>:x,y,w,h in original pixels")
    sp.set_defaults(func=cmd_intake)

    sp = sub.add_parser("source", help="register revision 0: the owner's .blend validated in Blender and copied unmodified, "
                                       "or an empty scene built through the runner (never done by `new`, R-93)")
    ssub = sp.add_subparsers(dest="source_cmd", required=True)
    q = ssub.add_parser("register", help="validate <file.blend> in separate Blender processes (identities, reopen) and copy it "
                                         "unmodified into revisions/ as immutable revision 0")
    wf(q)
    q.add_argument("file", help="the .blend to register; read only, never modified")
    q.add_argument("--note", default="")
    q.add_argument("--user", default="cli")
    q.set_defaults(func=cmd_source)
    q = ssub.add_parser("empty", help="build an empty scene (one tagged collection) through the runner as revision 0")
    wf(q)
    q.add_argument("--note", default="")
    q.add_argument("--user", default="cli")
    q.set_defaults(func=cmd_source)

    sp = sub.add_parser("preset", help="list, show, or create a project from a builder preset")
    psub = sp.add_subparsers(dest="preset_cmd", required=True)
    for name in ("list", "show", "create"):
        q = psub.add_parser(name)
        if name != "list":
            q.add_argument("preset_id")
        if name == "create":
            q.add_argument("--workflow-dir")
        q.add_argument("--config")
    sp.set_defaults(func=cmd_preset)

    sp = sub.add_parser("preflight", help="report declared, local, and live capability tiers per agent and check Blender")
    wf(sp)
    sp.add_argument("--live", action="store_true",
                    help="run the live probes (image, write-fail, session, cancellation): spends provider usage with real agents")
    sp.add_argument("--mock", help="screenplay JSON for scripted mock agents")
    sp.add_argument("--agent", action="append", help="probe only this agent label (repeatable); others keep their stored report")
    sp.set_defaults(func=cmd_preflight)

    for verb, func in (("start", cmd_start), ("resume", cmd_resume)):
        sp = sub.add_parser(verb, help=f"{verb} a run and work until a stop reason")
        wf(sp)
        sp.add_argument("--mock", help="screenplay JSON for scripted mock agents")
        sp.add_argument("--max-steps", type=int, default=200)
        sp.add_argument("--quiet", action="store_true")
        sp.add_argument("--user", default="cli")
        if verb == "start":
            sp.add_argument("--unattended", action="store_true", help="advance within limits; components stay unaccepted")
            sp.add_argument("--component", help="name of the first detailed component")
            sp.add_argument("--assign", action="append", metavar="ROLE=SEAT",
                            help="pre-assign a role to a seat for this run (repeatable; roles: brief, plan, build, corrector, "
                                 "reviewer, verifier, reassessor; overrides builder.assignments). A seat never reviews, "
                                 "verifies, or reassesses its own operation, even under an override (R-107)")
        sp.set_defaults(func=func)

    for verb in ("pause", "cancel", "feedback"):
        sp = sub.add_parser(verb, help=f"record a {verb} request for the running engine")
        wf(sp)
        if verb == "feedback":
            sp.add_argument("text")
        sp.add_argument("--user", default="cli")
        sp.set_defaults(func=cmd_control)

    sp = sub.add_parser("status", help="print run, component, findings, consumption, and limits")
    wf(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("findings", help="list findings")
    wf(sp)
    sp.add_argument("--open", action="store_true")
    sp.set_defaults(func=cmd_findings)

    sp = sub.add_parser("accept", help="accept a component at its current revision")
    wf(sp)
    sp.add_argument("component_id")
    sp.add_argument("--user", default="cli")
    sp.set_defaults(func=cmd_accept)

    sp = sub.add_parser("reopen", help="reopen a component; supersedes its acceptance")
    wf(sp)
    sp.add_argument("component_id")
    sp.add_argument("--reason", required=True)
    sp.add_argument("--user", default="cli")
    sp.set_defaults(func=cmd_reopen)

    sp = sub.add_parser("waive", help="waive a finding with a rationale")
    wf(sp)
    sp.add_argument("finding_id")
    sp.add_argument("--rationale", required=True)
    sp.add_argument("--user", default="cli")
    sp.set_defaults(func=cmd_waive)

    sp = sub.add_parser("checkpoint", help="list, create, or restore checkpoints")
    csub = sp.add_subparsers(dest="ck_cmd", required=True)
    for name in ("list", "create", "restore"):
        q = csub.add_parser(name)
        wf(q)
        if name == "create":
            q.add_argument("--name", required=True)
        if name == "restore":
            q.add_argument("checkpoint_id")
            q.add_argument("--user", default="cli")
    sp.set_defaults(func=cmd_checkpoint)

    sp = sub.add_parser("limits", help="show the limits in force; --set name=value changes them (zero means unlimited)")
    wf(sp)
    sp.add_argument("--set", action="append", help="name=value, repeatable: wall_clock_minutes, max_cost_usd, max_requests, "
                                                  "max_renders, attempts_per_finding, stall_steps")
    sp.add_argument("--user", default="cli")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_limits)

    sp = sub.add_parser("journal", help="print the tail of the journal")
    wf(sp)
    sp.add_argument("--tail", type=int, default=30)
    sp.set_defaults(func=cmd_journal)

    sp = sub.add_parser("concept", help="concept stage: generate the canon reference set with the manual image seat (addendum A)")
    csub = sp.add_subparsers(dest="concept_cmd", required=True)

    def cwf(q: argparse.ArgumentParser, agents: bool = False) -> None:
        wf(q)
        q.add_argument("--user", default="cli")
        if agents:
            q.add_argument("--mock", help="screenplay JSON for scripted mock agents")
            q.add_argument("--quiet", action="store_true")

    q = csub.add_parser("start", help="start from text and/or seed images; opens the anchor request or writes the canon description")
    cwf(q, agents=True)
    q.add_argument("--from-text", help="the asset idea in words")
    q.add_argument("--from-image", action="append", help="seed image supplied by the owner (repeatable); approved on entry, the first is the anchor")
    q.add_argument("--labels", action="append", help="comma-separated view labels for the matching --from-image")
    q.add_argument("--approval", choices=["each", "anchor_only", "auto"], help="approval mode (default: builder.concept.approval)")
    q.add_argument("--views", help="comma-separated needed views (default: builder.concept.views)")
    q = csub.add_parser("prompts", help="print every open generation request: prompt to paste, images to attach, request id")
    cwf(q)
    q = csub.add_parser("import", help="import generated images for a request (copied unmodified, hashed, marked candidate)")
    cwf(q, agents=True)
    q.add_argument("request_id", help="the request id (with --as-anchor/--as-view: the first file)")
    q.add_argument("files", nargs="*")
    q.add_argument("--vendor", choices=["chatgpt", "gemini", "other"], help="declared by the owner; recorded as a declaration")
    q.add_argument("--model", help="model name as shown in the app; recorded as a declaration")
    q.add_argument("--attached", help="comma-separated reference ids actually attached (declaration)")
    q.add_argument("--as-anchor", action="store_true", help="no matching request: create an anchor request for these files (R-96a)")
    q.add_argument("--as-view", metavar="VIEW", help="no matching request: create a request for this needed view (R-96a)")
    q.add_argument("--watch", metavar="FOLDER", help="import new image files from FOLDER as they appear, all for request_id")
    q.add_argument("--watch-timeout", type=float, help="stop watching after this many seconds")
    q.add_argument("--watch-max", type=int, help="stop after this many files")
    q.add_argument("--watch-existing", action="store_true", help="also import files already in the folder")
    q.add_argument("--no-check", action="store_true", help="import only; do not run the seats' consistency verdicts now")
    q = csub.add_parser("list", help="canon state, coverage of the needed set, requests, candidates, conflicts")
    cwf(q)
    q = csub.add_parser("show", help="print one concept record (reference, generation, study, conflict, canon, plan)")
    cwf(q)
    q.add_argument("record_id")
    q = csub.add_parser("approve", help="approve candidate image(s); recorded with the mode and the seats' verdicts")
    cwf(q, agents=True)
    q.add_argument("ids", nargs="+")
    q = csub.add_parser("reject", help="reject image(s) with a reason")
    cwf(q, agents=True)
    q.add_argument("ids", nargs="+")
    q.add_argument("--reason", required=True)
    q = csub.add_parser("regenerate", help="write a revised prompt as a new request (bounded by max_regenerations_per_view)")
    cwf(q, agents=True)
    q.add_argument("record_id", help="a generation request id or a generated reference id")
    q.add_argument("--note", help="what to change")
    q = csub.add_parser("study", help="request a per-piece study conditioned on the canon (R-104)")
    cwf(q, agents=True)
    q.add_argument("--part", required=True)
    q.add_argument("--view", required=True)
    q.add_argument("--region")
    q.add_argument("--purpose")
    q = csub.add_parser("proceed", help="accept a partial reference set explicitly (recorded) and hand off to intake")
    cwf(q)
    q = csub.add_parser("abandon", help="abandon an open request (its manifest records the abandonment, R-96)")
    cwf(q)
    q.add_argument("request_id")
    q.add_argument("--reason", required=True)
    q = csub.add_parser("failed", help="mark an open request failed (the app refused or produced nothing usable)")
    cwf(q)
    q.add_argument("request_id")
    q.add_argument("--reason", required=True)
    sp.set_defaults(func=cmd_concept)

    sp = sub.add_parser("harness", help="R-84 efficiency comparison: run the fixture with scripted seats, or report an existing workflow")
    hsub = sp.add_subparsers(dest="harness_cmd", required=True)
    q = hsub.add_parser("run", help="create the fixture (needs Blender), run it with scripted seats (nothing spent), measure")
    q.add_argument("directory")
    q.add_argument("--mock", help="screenplay JSON (default: the built-in fixture screenplay)")
    q.add_argument("--max-steps", type=int, default=60)
    q.add_argument("--config")
    q.add_argument("--json", action="store_true")
    q = hsub.add_parser("report", help="measure an existing workflow's invocations (live runs carry measured tokens)")
    wf(q)
    q.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_harness)

    sp = sub.add_parser("fixture", help="create the disposable fixture project (needs Blender)")
    fsub = sp.add_subparsers(dest="fixture_cmd", required=True)
    q = fsub.add_parser("create")
    q.add_argument("directory")
    q.add_argument("--config")
    sp.set_defaults(func=cmd_fixture)
    return p


def main(argv: list[str] | None = None, *, runner_factory: RunnerFactory | None = None,
         adapters_factory: AdaptersFactory | None = None, stdout: Any = None) -> int:
    if stdout is None and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # console encoding is never trusted (Section 1.1)
        except (AttributeError, ValueError):
            pass
    out = stdout or sys.stdout
    ctx = Ctx(out, runner_factory, adapters_factory)
    parser = build_parser()
    try:
        with contextlib.redirect_stdout(out):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        return int(args.func(args, ctx))
    except CliError as exc:
        ctx.say(f"error: {exc}")
        return 2
    except FileExistsError as exc:
        ctx.say(f"error: {exc}")
        return 2
