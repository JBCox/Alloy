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
from .config import BuilderConfig, default_workflow_dir, discover_blender, slugify
from .engine import Engine, EngineFailure
from .project import Project
from .providers import make_adapter
from .providers.mock import ScriptedAdapter
from .references import References

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
        loaded = eng.load_preflight()
        if len(loaded) < len(adapters):
            missing = ", ".join(sorted(set(adapters) - set(loaded)))
            if all(isinstance(a, ScriptedAdapter) for a in adapters.values()):
                ctx.say(f"preflight: no current report for {missing}; running preflight (live) on the scripted agents")
                report = eng.preflight(live=True)
                _print_preflight(report, ctx)
            else:
                raise CliError(f"no current live preflight report for agent(s) {missing} (never run, or the CLI path, version, "
                               f"model, or settings changed since; R-18). Run `preflight {args.workflow_dir} --live` first: it spends "
                               "provider usage and is never started implicitly.")
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
            eng.start(attended=attended, component_name=getattr(args, "component", None))
            ctx.say(f"run {eng.run.id} started ({'attended' if attended else 'unattended'})")
        status = eng.run_until_stop(max_steps=args.max_steps)
        _print_status(status, ctx)
        return 0 if status["stop_reason"] in ("ready_for_user_review", "user_pause", None) else 1
    except EngineFailure as exc:
        raise CliError(str(exc)) from exc
    finally:
        prj.close()


def cmd_start(args: argparse.Namespace, ctx: Ctx) -> int:
    return _run_engine(args, ctx, resume=False)


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


# ---------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m builder", description=(
        "Collaborative Model Builder (two AI agents reconstruct a concept design as an editable Blender model; Alloy owns "
        "the state). Verbs: new, open, intake, preset, preflight, start, resume, pause, cancel, feedback, status, findings, "
        "accept, reopen, waive, checkpoint, journal, fixture."))
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

    sp = sub.add_parser("journal", help="print the tail of the journal")
    wf(sp)
    sp.add_argument("--tail", type=int, default=30)
    sp.set_defaults(func=cmd_journal)

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
