"""Efficiency comparison harness (spec R-84).

The same fixture run, measured two ways from its own records:

* **packet delivery** (what Alloy does): every invocation receives one task-specific packet (text, schema, the
  evidence files it needs, crops at native resolution) plus the prompt and the working agreement. Bytes are read
  from the packet manifests and the invocation records, so they are measurements, not estimates.
* **transcript replay** (the naive alternative, the way Alloy's chat modes concatenate prior contributions): one
  shared conversation in which every turn re-delivers every earlier packet text and reply and every image
  delivered before, plus the current packet. This is *simulated* from the same invocation sequence; nothing is
  sent to any provider, so its token count is never known and is never estimated.

Rules: no invented savings percentage (absolute numbers side by side), tokens only where a provider measured
them (unknown otherwise, never zero), and no claim of quality equivalence: the harness reports which evidence,
coverage, and withheld material each delivery preserves, and nothing more.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import BuilderConfig
from .engine import Engine
from .project import Project
from .records import UNKNOWN, Measured, Record, quantity_from_json, quantity_to_json
from .schemas import OBSERVATION_SECTIONS

TEXT_ROLES = ("packet_text", "schema")
IMAGE_ROLES = ("reference", "render", "crop")
TOKEN_FIELDS = ("input", "output", "cached_input", "reasoning")
# preflight probes run in their own sessions and would never be part of a modeling conversation: reported apart
PROBE_PURPOSES = ("probe", "write_probe", "session_probe", "cancel_probe")

# --- the fixture screenplay (scripted seats: A builds, B finds and corrects the floating bracket, A verifies) ---------

FIXTURE_PARTS = ["p_base", "p_post", "p_housing", "p_bracket", "p_lens", "p_bolt", "p_bolt.1", "p_bolt.2", "p_bolt.3"]


def _statement(text: str, status: str = "observed") -> dict[str, Any]:
    return {"statement": text, "status": status, "evidence_refs": ["ref"]}


def _observation(agent: str) -> dict[str, Any]:
    return {**{s: [_statement(f"{agent}: {s}")] for s in OBSERVATION_SECTIONS}, "questions_for_user": []}


_BRIEF = {"intended_asset": "Fixture lamp", "authoritative_references": ["front", "side", "three_quarter"],
          "modeling_scope": "head first", "deliverables": ["editable parts"], "scale_and_coordinates": "provisional, Z up",
          "symmetry_and_pose_assumptions": [], "fidelity_priorities": ["silhouette", "depth", "attachment"],
          "observed_vs_inferred_construction": [_statement("housing is a box on a post")],
          "missing_evidence": ["no rear view"], "unresolved_decisions": []}

_PLAN = {"parts": [{"part_id": p, "name": p, "parent": None, "interpretation": "fixture part", "evidence_status": "observed",
                    "confidence": "medium", "questions": []} for p in FIXTURE_PARTS],
         "relations": [{"from_part": "p_bracket", "to_part": "p_housing", "type": "attached_to"},
                       {"from_part": "p_post", "to_part": "p_base", "type": "attached_to"}],
         "dependencies": [], "interfaces": ["bracket mounts on housing top face"], "hypotheses": []}

_FLOATING = {"part_id": "p_bracket", "region": "mount", "view": "side", "observed_mismatch": "bracket floats above the housing",
             "severity": "high", "confidence": "high", "evidence_refs": ["render:side"],
             "proposed_correction": "lower the bracket until it sits on the housing top face",
             "expected_improvement": "no gap between bracket and housing in the side view"}


def _task_result(script: str, part: str, assessment: str) -> dict[str, Any]:
    return {"operations": [{"intent": f"edit {part}", "target_part_ids": [part], "expected_outcome": "matches the reference",
                            "declared_effects": {"creates": [], "modifies": [part], "deletes": []}, "script": script}],
            "self_assessment": assessment, "questions_for_user": []}


def _coverage() -> list[dict[str, Any]]:
    return [{"part_id": p, "view": v, "instances_inspected": "all"} for p in FIXTURE_PARTS for v in ("front", "side", "three_quarter")]


def fixture_screenplay(*, housing_script: str = "ALLOY.get('p_housing').scale.y = 0.6\n",
                       bracket_script: str = "ALLOY.get('p_bracket').location.z -= 0.15\n") -> dict[str, dict[str, list[Any]]]:
    """The scripted seats for the fixture (mocks state exactly what they script; they prove routing and state
    handling, never visual judgement). ``{finding_id}``-style placeholders are filled by the mock from the request."""
    probes = {
        "probe": [{"shape": "triangle", "color": "red", "number": 7}],
        "write_probe": [{"attempted_path": "{attempted_path}", "outcome": "attempted_and_refused", "write_succeeded": False,
                         "error_text": "denied"}],
        "session_probe": [{"nonce": "{nonce}"}, {"nonce": "{nonce}"}],
        "cancel_probe": [{"__outcome__": "cancelled", "__kill_confirmed__": True}],
    }
    return {
        "A": {**probes, "intake_observation": [_observation("A")], "brief_draft": [_BRIEF], "construction_plan": [_PLAN],
              "build_task": [_task_result(housing_script, "p_housing", "housing depth restored to the side-view proportion")],
              "verification": [{"finding_id": "{finding_id}", "verdict": "improved", "evidence_refs": ["render:side"],
                                "rationale": "scripted"}]},
        "B": {**probes, "intake_observation": [_observation("B")],
              "review_task": [{"findings": [_FLOATING], "coverage": _coverage()}],
              "review_reconcile": [{"findings_confirmed": "{finding_ids}", "findings_withdrawn": [], "notes": "confirmed"}],
              "correction_task": [_task_result(bracket_script, "p_bracket", "bracket lowered onto the housing")]},
    }


# --- measurements ------------------------------------------------------------------------------------------------

def prompt_bytes(inv: Record) -> int:
    """Prompt and working-agreement bytes the engine recorded at dispatch (all rounds of the invocation)."""
    d = inv.data.get("delivered") or {}
    return int(d.get("prompt_bytes") or 0) + int(d.get("framing_bytes") or 0)


def _reply_bytes(inv: Record) -> int:
    d = inv.data.get("delivered") or {}
    if "reply_bytes" in d:
        return int(d["reply_bytes"] or 0)
    results = inv.data.get("results") or []
    return len((results[-1].get("raw_text") or "").encode("utf-8")) if results else 0


def _turns(store: Any, *, probes: bool = False) -> list[dict[str, Any]]:
    """One measured turn per invocation that received a packet, in dispatch order; modeling turns by default,
    preflight probes with ``probes=True``."""
    packets = {p.id: p for p in store.list("packet")}
    turns: list[dict[str, Any]] = []
    for inv in sorted(store.list("invocation"), key=lambda r: (r.data.get("started_at") or "", r.id)):
        pid = inv.data.get("packet_id")
        if not pid or pid not in packets:
            continue
        if (inv.data.get("purpose") in PROBE_PURPOSES) != probes:
            continue
        packet = packets[pid]
        text = images = other = 0
        image_count = 0
        image_hashes: list[str] = []
        for f in packet.data.get("files") or []:
            n = int(f.get("bytes") or 0)
            if f.get("role") in IMAGE_ROLES:
                images += n
                image_count += 1
                image_hashes.append(f.get("sha256"))
            elif f.get("role") in TEXT_ROLES:
                text += n
            else:
                other += n
        text += prompt_bytes(inv)
        turns.append({"invocation_id": inv.id, "packet_id": pid, "agent_id": inv.data.get("agent_id"),
                      "purpose": inv.data.get("purpose"), "kind": packet.data.get("kind"), "outcome": inv.data.get("outcome"),
                      "bytes_text": text, "bytes_images": images, "bytes_other": other, "bytes": text + images + other,
                      "images": image_count, "image_hashes": image_hashes, "reply_bytes": _reply_bytes(inv),
                      "withheld": list(packet.data.get("withheld") or []), "usage": inv.data.get("usage") or {}})
    return turns


def _tokens(turns: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, float] = {}
    unknown = 0
    for t in turns:
        any_known = False
        for field in TOKEN_FIELDS:
            q = quantity_from_json((t.get("usage") or {}).get(field))
            if isinstance(q, Measured):
                totals[field] = totals.get(field, 0) + float(q.value)
                any_known = True
        if not any_known:
            unknown += 1
    out: dict[str, Any] = {field: quantity_to_json(Measured(totals[field], "provider")) if field in totals else quantity_to_json(UNKNOWN)
                           for field in TOKEN_FIELDS}
    out["unknown_invocations"] = unknown
    out["note"] = ("token counts are the providers' own measurements where reported; invocations that reported none are "
                   "counted as unknown, never as zero")
    return out


def measure_packet_delivery(store: Any) -> dict[str, Any]:
    turns = _turns(store)
    return {
        "strategy": "packet delivery",
        "invocations": len(turns),
        "bytes_total": sum(t["bytes"] for t in turns),
        "bytes_text": sum(t["bytes_text"] for t in turns),
        "bytes_images": sum(t["bytes_images"] for t in turns),
        "bytes_other": sum(t["bytes_other"] for t in turns),
        "images_delivered": sum(t["images"] for t in turns),
        "reply_bytes": sum(t["reply_bytes"] for t in turns),
        "tokens": _tokens(turns),
        "turns": turns,
        "method": "measured from the packet manifests (file bytes) and the invocation records (prompt, working agreement, "
                  "reply bytes recorded at dispatch); preflight probes are reported apart",
        "probes": {"invocations": len(_turns(store, probes=True)), "bytes_total": sum(t["bytes"] for t in _turns(store, probes=True)),
                   "note": "preflight probes run in their own sessions; excluded from both deliveries above"},
    }


def simulate_transcript_replay(store: Any) -> dict[str, Any]:
    """Every turn re-delivers the whole shared transcript so far (every earlier packet text and reply, and every image
    delivered before) plus its own packet. Computed from the same turns; nothing is sent."""
    base = _turns(store)
    turns: list[dict[str, Any]] = []
    running_text = 0
    running_images = 0
    replayed_images = 0
    exposed: list[dict[str, Any]] = []
    for n, t in enumerate(base):
        replay_bytes = t["bytes"] + running_text + running_images
        turns.append({"invocation_id": t["invocation_id"], "purpose": t["purpose"], "agent_id": t["agent_id"],
                      "bytes": replay_bytes, "bytes_own_packet": t["bytes"], "bytes_replayed_text": running_text,
                      "bytes_replayed_images": running_images, "images_replayed": replayed_images})
        if t["withheld"]:
            earlier_other = [b["invocation_id"] for b in base[:n] if b["agent_id"] != t["agent_id"] and b["reply_bytes"] > 0]
            if earlier_other:
                for w in t["withheld"]:
                    exposed.append({"packet_id": t["packet_id"], "invocation_id": t["invocation_id"], "purpose": t["purpose"],
                                    "what": w.get("what"), "why": w.get("why"), "exposed_by": earlier_other})
        running_text += t["bytes_text"] + t["bytes_other"] + t["reply_bytes"]
        running_images += t["bytes_images"]
        replayed_images += t["images"]
    return {
        "strategy": "transcript replay",
        "simulated": True,
        "invocations": len(turns),
        "bytes_total": sum(t["bytes"] for t in turns),
        "images_delivered": sum(t["images_replayed"] for t in turns) + sum(t["images"] for t in base),
        "tokens": {**{f: quantity_to_json(UNKNOWN) for f in TOKEN_FIELDS}, "unknown_invocations": len(turns),
                   "note": "never estimated: the simulated transcript was not sent to any provider, so no token count exists"},
        "withheld_exposed": exposed,
        "turns": turns,
        "method": ("simulated from the same invocation sequence: each turn re-delivers every earlier packet text and reply "
                   "and every image delivered before, plus its own packet, the way a single shared chat transcript grows; "
                   "nothing was sent to a provider"),
    }


def preserved(store: Any) -> dict[str, Any]:
    hashes: set[str] = set()
    withheld_packets = 0
    for p in store.list("packet"):
        if p.data.get("withheld"):
            withheld_packets += 1
        for f in p.data.get("files") or []:
            if f.get("role") in IMAGE_ROLES and f.get("sha256"):
                hashes.add(f["sha256"])
    findings = store.list("finding")
    closed = [f for f in findings if f.state == "closed" and f.data.get("after_render_ids")]
    return {
        "unique_evidence_files": len(hashes),
        "coverage_rows": len(store.list("coverage")),
        "findings_total": len(findings),
        "findings_closed_on_verified_renders": len(closed),
        "packets_with_withheld_material": withheld_packets,
        "revisions": len(store.list("revision")),
        "renders_ok": len(store.list("render", state="ok")),
        "note": "identical under both deliveries by construction: the harness measures the same run twice; packet delivery "
                "honours the withheld material, the replayed transcript would not",
    }


def compare(project: Project) -> dict[str, Any]:
    store = project.store
    runs = store.list("run")
    return {
        "fixture": project.record.data.get("name"),
        "workflow_dir": str(project.workflow_dir),
        "run_id": runs[-1].id if runs else None,
        "stop_reason": (runs[-1].data.get("stop_reason") if runs else None),
        "agents": {a.data.get("label"): {"provider": a.data.get("provider"), "model": a.data.get("model")} for a in store.list("agent")},
        "packet_delivery": measure_packet_delivery(store),
        "transcript_replay": simulate_transcript_replay(store),
        "preserved": preserved(store),
        "caveats": [
            "bytes are measured; tokens are reported only where the provider measured them and are unknown otherwise",
            "transcript replay is simulated from the same turns; nothing was sent, so it has no token count",
            "no reduction percentage is computed and no quality equivalence is claimed: the two deliveries carry the same "
            "evidence files and coverage rows, and only packet delivery honours the withheld material (R-11)",
        ],
    }


def render_report(cmp: dict[str, Any]) -> str:
    pd, tr, pres = cmp["packet_delivery"], cmp["transcript_replay"], cmp["preserved"]

    def tok(t: dict[str, Any]) -> str:
        parts = []
        for f in TOKEN_FIELDS:
            q = t.get(f) or {}
            parts.append(f"{f}={q.get('value')} (measured)" if q.get("kind") == "measured" else f"{f}=unknown")
        return ", ".join(parts) + f"; invocations with no reported usage: {t.get('unknown_invocations')}"

    lines = [
        f"Efficiency comparison (R-84): {cmp.get('fixture')}  run {cmp.get('run_id')}  stop reason {cmp.get('stop_reason')}",
        f"agents: {cmp.get('agents')}",
        "",
        "strategy            invocations   delivered bytes   images delivered",
        f"packet delivery     {pd['invocations']:>11}   {pd['bytes_total']:>15}   {pd['images_delivered']:>16}",
        f"transcript replay   {tr['invocations']:>11}   {tr['bytes_total']:>15}   {tr['images_delivered']:>16}   (simulated; nothing was sent)",
        "",
        f"packet delivery bytes: text {pd['bytes_text']}, images {pd['bytes_images']}, other {pd['bytes_other']}; replies {pd['reply_bytes']}",
        f"preflight probes (apart from both): {pd['probes']['invocations']} invocations, {pd['probes']['bytes_total']} bytes",
        f"packet delivery tokens: {tok(pd['tokens'])}",
        f"transcript replay tokens: {tok(tr['tokens'])} ({tr['tokens'].get('note')})",
        "",
        f"preserved under both: {pres['unique_evidence_files']} unique evidence files, {pres['coverage_rows']} coverage rows, "
        f"{pres['findings_closed_on_verified_renders']} of {pres['findings_total']} findings closed on verified renders, "
        f"{pres['revisions']} revisions, {pres['renders_ok']} renders",
        f"withheld material: {pres['packets_with_withheld_material']} packet(s) withhold material under packet delivery; "
        f"the replayed transcript would expose it in {len(tr['withheld_exposed'])} turn(s):",
    ]
    for e in tr["withheld_exposed"]:
        lines.append(f"  - {e['purpose']} ({e['invocation_id']}): {e['what']} ({e['why']}); carried by earlier replies {e['exposed_by']}")
    lines += ["", "per turn (packet delivery bytes -> replayed bytes):"]
    for p, t in zip(pd["turns"], tr["turns"]):
        lines.append(f"  {p['purpose']:<22} {p['bytes']:>10} -> {t['bytes']:>10}   images {p['images']} -> {p['images'] + t['images_replayed']}")
    lines += ["", "caveats:"] + [f"  - {c}" for c in cmp["caveats"]]
    return "\n".join(lines)


def write_report(project: Project, cmp: dict[str, Any]) -> tuple[Path, Path]:
    j = project.workflow_dir / "harness-report.json"
    t = project.workflow_dir / "harness-report.txt"
    with open(j, "w", encoding="utf-8") as f:
        json.dump(cmp, f, ensure_ascii=False, indent=1, default=str)
    with open(t, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_report(cmp) + "\n")
    return j, t


def run_fixture(directory: str | Path, runner: Any, *, screenplay: dict[str, Any] | None = None,
                config: BuilderConfig | None = None, max_steps: int = 60) -> tuple[Project, dict[str, Any]]:
    """Create the fixture, run it with the scripted seats, and measure. The caller closes the project."""
    from .fixture import create_fixture
    from .providers.mock import ScriptedAdapter

    cfg = config or BuilderConfig.from_dict({"builder": {"limits": {"attempts_per_finding": 2}}})
    for label in cfg.agents:
        cfg.agents[label].provider = "mock"
    play = screenplay or fixture_screenplay()
    project, _info = create_fixture(directory, runner)
    adapters = {label: ScriptedAdapter(label, play.get(label, {})) for label in cfg.agents}
    eng = Engine(project, cfg, adapters=adapters, runner=runner)
    eng.preflight(live=True)      # scripted seats: free
    eng.start()
    status = eng.run_until_stop(max_steps=max_steps)
    cmp = compare(project)
    cmp["status"] = {"stop_reason": status.get("stop_reason"), "execution": status.get("execution")}
    write_report(project, cmp)
    return project, cmp
