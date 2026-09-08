"""Concept stage (addendum A; design Section 14): canon, generation requests, manual import, approval modes,
independent consistency verdicts, evidence conflicts, coverage of the needed set, studies, and limits.

Principle (A.1): a generated image is never evidence of a pre-existing design (base R-32); the images the owner
approves *become* the design. Unapproved generations are hypotheses. Approved images are ranked by precedence
(R-95) and contradictions are recorded as ``evidence_conflict`` records, never averaged and never resolved silently.

The loop is driven by owner commands (``concept start | import | approve | reject | regenerate | study | proceed``);
``advance()`` performs the automatic work those commands unlock: the art director's prompts, the canon description,
both seats' verdicts, auto-approvals under the mode in force, bounded regenerations, escalations, and the coverage
check that hands off to intake. Every LLM call goes through ``Engine._invoke`` (limits, journal, packets); the image
seat only publishes prompts and, for the manual seat, never generates anything itself (D12).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .evidence import EvidenceItem
from .ids import utc_now
from .records import NOT_APPLICABLE, PRECEDENCE, Record, quantity_to_json
from .roles import RoleConflict, assign as assign_role, consistency_seats
from .schemas import SCHEMAS
from .state import IllegalTransition, transition
from .providers.imagegen import make_image_seat
from .providers.imagegen.base import ImageSeat

if TYPE_CHECKING:  # pragma: no cover
    from .engine import Engine

MODE_TEXT = {
    "each": "each: the owner approves every generated image before it is used",
    "anchor_only": "anchor_only: the owner approves the anchor; views and studies are auto-approved when both LLM seats "
                   "independently judge them consistent and no conflict is open",
    "auto": "auto: the art director picks the anchor by stated criteria and everything proceeds; every pick is journaled "
            "and reversible",
}
VENDORS = ("chatgpt", "gemini", "other")
TARGET_PRECEDENCE = {"anchor_candidate": "anchor", "view": "turnaround", "study": "study"}
OPEN_REQUEST_STATES = ("open", "imported", "checked")
PROVENANCE_NOTE = "declared by the owner; Alloy cannot verify the vendor or model and records them as declarations (R-96a)"

ANCHOR_OBJECTIVE = ("You are the art director. Write ONE image-generation prompt for the anchor concept image of the asset "
                    "described below. State the subject, silhouette, construction, materials, palette, style, and framing "
                    "explicitly in the prompt text (R-100). The owner will generate {count} candidate images from this "
                    "prompt in an image app and pick one. No text, labels, or watermarks in the image.")
CANON_OBJECTIVE = ("You are the art director. From the approved anchor image (evidence/), write the canon description "
                   "that every later generation prompt derives from (R-101): silhouette and proportions, main masses, "
                   "parts and construction, materials and colors, distinguishing details, and what the anchor does not "
                   "establish (unknowns). Describe only what the anchor shows; mark the rest as unknown.")
VIEW_OBJECTIVE = ("You are the art director. Write ONE image-generation prompt for the '{view}' view of the SAME design as "
                  "the approved anchor image (attached as conditioning input). Derive every statement from the canon "
                  "description in the brief; keep silhouette, construction, materials, palette, and style identical; "
                  "state the camera view explicitly. Set `view` to '{view}' and list the anchor in `attachments`.")
STUDY_OBJECTIVE = ("You are the art director. Write ONE image-generation prompt for a close-up study of part '{part}' "
                   "({view} view{region}) for this purpose: {purpose}. Condition on the approved anchor (attached) and the "
                   "canon description; the study must not change the design. List the anchor in `attachments`.")
REVISION_OBJECTIVE = ("You are the art director. The previous generation for '{target}' (request {request}) was not "
                      "acceptable; write a revised prompt for the same target. Address the listed problems explicitly "
                      "while keeping everything else identical to the canon description. List the anchor in `attachments`.")
VERDICT_OBJECTIVE = ("Judge whether the CANDIDATE image (labelled candidate in evidence/) shows the same design as the "
                     "approved ANCHOR image and the canon description in the brief. It is a '{view}' view. Name every "
                     "specific inconsistency by part, region, and what differs (R-102). Answer consistent only when you "
                     "find none; uncertain when the candidate does not let you tell. You are not told what any other "
                     "seat concluded. Set reference_id to '{reference_id}'.")
PICK_OBJECTIVE = ("Approval mode is auto. Pick the anchor among the candidate images (labelled candidate in evidence/) for "
                  "the asset described below, by stated criteria: readability of silhouette and construction, fit to the "
                  "description, freedom from artifacts. List the criteria you used and why each other candidate was not "
                  "chosen. Candidate reference ids: {ids}.")


class ConceptError(Exception):
    pass


def canon_rank(references: list[Record]) -> list[Record]:
    """Precedence order, highest first (R-95): owner targets, the anchor, turnaround views, studies. Never merges."""
    return sorted(references, key=lambda r: (int(r.data.get("precedence", PRECEDENCE["owner_target"])),
                                             str(r.data.get("added_at") or ""), r.id))


class ConceptStage:
    def __init__(self, engine: "Engine", seat: ImageSeat | None = None):
        self.engine = engine
        self.store = engine.store
        self.project = engine.project
        self.config = engine.config
        self.references = engine.references
        self.seat = seat or make_image_seat(self.config.image_generation)
        self.dir = self.project.path("concept")
        if self.engine.run is None:
            plan = self.plan
            if plan is not None:
                self.engine.limits.restore(plan.data.get("consumption"))

    # ------------------------------------------------------------------ plan and mode

    @property
    def plan(self) -> Record | None:
        plans = self.store.list("concept_plan")
        return plans[-1] if plans else None

    def _require_plan(self) -> Record:
        plan = self.plan
        if plan is None:
            raise ConceptError("no concept stage exists for this project; run `concept start` first (R-99)")
        return plan

    @property
    def mode(self) -> str:
        plan = self.plan
        return plan.data["approval_mode"] if plan is not None else self.config.concept["approval"]

    def mode_text(self) -> str:
        return "approval mode " + MODE_TEXT[self.mode]

    def _save_plan(self, plan: Record, event: str, **inputs: Any) -> Record:
        plan.data["consumption"] = self.engine.limits.status()
        return self.store.upsert(plan, actor="engine", event=event, inputs=inputs or None, run_id=self.engine._run_id())

    def _canon(self, plan: Record, to_state: str, *, actor: str, reason: str, inputs: dict[str, Any] | None = None) -> Record:
        plan = self.store.require("concept_plan", plan.id)     # always the stored version: never overwrite newer fields
        cov = self.coverage(plan)
        ctx = {"anchor_requests_open": bool(self._requests(kind="anchor_candidate", states=OPEN_REQUEST_STATES)),
               "anchor_approved": plan.data.get("anchor_reference_id") is not None,
               "canon_description": self._canon_description(plan) is not None,
               "coverage_met": not cov["missing"], "open_conflicts": cov["conflicts_open"]}
        try:
            plan = transition(self.store, plan, to_state, actor=actor, reason=reason, context=ctx, inputs=inputs,
                              run_id=self.engine._run_id())
        except IllegalTransition as exc:
            raise ConceptError(str(exc)) from None
        plan.data["consumption"] = self.engine.limits.status()
        return self.store.upsert(plan, actor="engine", event="concept_plan.updated")

    # ------------------------------------------------------------------------ start

    def start(self, *, from_text: str | None = None, from_images: list[str | Path] | tuple[str | Path, ...] = (),
              labels: list[list[str]] | None = None, approval: str | None = None, views: list[str] | None = None,
              actor: str = "user") -> Record:
        if self.plan is not None:
            raise ConceptError(f"a concept stage already exists ({self.plan.id}, canon {self.plan.state}); one per project")
        if not from_text and not from_images:
            raise ConceptError("give --from-text and/or --from-image (R-99)")
        mode = approval or self.config.concept["approval"]
        if mode not in MODE_TEXT:
            raise ConceptError(f"approval mode {mode!r} must be one of {tuple(MODE_TEXT)} (D11)")
        needed = list(views or self.config.concept["views"])
        if not needed:
            raise ConceptError("at least one needed view is required (R-103)")
        if labels is not None and len(labels) != len(from_images):
            raise ConceptError("give one labels list per seed image")
        start_mode = "both" if (from_text and from_images) else ("text" if from_text else "images")
        plan = Record.new("concept_plan", {
            "needed_views": needed, "approval_mode": mode, "start_mode": start_mode, "text": from_text or "",
            "anchor_reference_id": None, "seed_reference_ids": [], "needed_parts": [], "escalations": [],
            "art_director": None, "flow_confirmed_at": None, "seat": self.seat.declared(),
            "image_limits": {"max_images": int(self.config.concept["max_images"]),
                             "max_regenerations_per_view": int(self.config.concept["max_regenerations_per_view"])},
            "anchor_candidates": int(self.config.concept["anchor_candidates"]), "created_at": utc_now(), "created_by": actor,
        })
        plan = self.store.upsert(plan, actor=actor, event="concept_plan.created", inputs={"mode": mode, "start_mode": start_mode})
        for i, path in enumerate(from_images):
            lab = list(labels[i]) if labels else ["other"]
            ref = self.references.add(path, labels=lab, kind="target",
                                      notes="seed image supplied by the owner; approved on entry (R-99)", actor=actor)
            plan.data["seed_reference_ids"].append(ref.id)
            if plan.data["anchor_reference_id"] is None:
                plan.data["anchor_reference_id"] = ref.id
                ref.data["is_anchor"] = True
                self.store.upsert(ref, actor=actor, event="reference.anchor", inputs={"note": "first seed image is the anchor (R-99)"})
        plan = self._save_plan(plan, "concept_plan.seeded")
        if from_images:
            plan = self._canon(plan, "anchor_approved", actor="engine", reason="the first seed image is the anchor (R-99)")
        else:
            self._request_anchor_round(plan, actor=actor)
            plan = self._canon(self.plan, "anchor_pending", actor="engine", reason="anchor candidates requested (R-100)")
        return plan

    # --------------------------------------------------------------- art director

    def _art_director(self, plan: Record) -> tuple[str, str]:
        plan = self.store.require("concept_plan", plan.id)
        overrides = plan.data.get("assignment_overrides") or {}
        try:
            a = assign_role("art_director", seats=self.engine._labels(), overrides=overrides or None,
                            rejected_rounds=self.rejected_rounds(), image_seat=self.seat.label)
        except RoleConflict as exc:
            raise ConceptError(str(exc)) from None
        plan.data["art_director"] = {"seat": a.seat, "rationale": a.rationale, "rejected_rounds": self.rejected_rounds(),
                                     "at": utc_now()}
        self._save_plan(plan, "concept_plan.art_director", seat=a.seat, rationale=a.rationale)
        return a.seat, a.rationale

    def rejected_rounds(self) -> int:
        """Generation rounds the owner or the verdicts rejected or the app failed (R-108 rotation counter)."""
        return sum(1 for g in self.store.list("generation") if g.state in ("rejected", "failed"))

    def _ask(self, label: str, purpose: str, *, kind: str, objective: str, evidence: list[EvidenceItem],
             schema_name: str, extra_sections: dict[str, str] | None = None, withheld: list[dict[str, str]] | None = None,
             extra: dict[str, Any] | None = None) -> tuple[dict[str, Any], Record]:
        plan = self._require_plan()
        canon = self._canon_description(plan)
        brief = canon.data["text"] if canon else f"(no canon description yet) Asset idea from the owner: {plan.data.get('text') or '(images only)'}"
        packet, pdir = self.engine.packets.build(
            kind=kind, agent_id=self.engine.agents[label].id, task=None, objective=objective, brief_text=brief,
            evidence=evidence, open_findings=[], constraints=[], withheld=withheld or [], schema_name=schema_name,
            schema=SCHEMAS[schema_name], changed=None, extra_sections=extra_sections)
        outcome, inv = self.engine._invoke(label, purpose, packet=packet, pdir=pdir, schema_name=schema_name,
                                           images=self.engine._images_of(packet, pdir), extra=extra)
        if not outcome.ok:
            raise ConceptError(f"{purpose} from seat {label} was malformed or failed ({inv.data.get('outcome')}): "
                               f"{'; '.join(outcome.errors)[:300]}; nothing was approved or advanced (R-5)")
        return outcome.value, inv

    def _anchor(self, plan: Record | None = None) -> Record | None:
        plan = plan or self.plan
        aid = plan.data.get("anchor_reference_id") if plan else None
        return self.store.get("reference", aid) if aid else None

    def _anchor_item(self, anchor: Record) -> EvidenceItem:
        return EvidenceItem(Path(anchor.data["file"]), "reference",
                            {"reference_id": anchor.id, "label": "anchor (approved canon)", "canon_state": "approved",
                             "precedence": anchor.data.get("precedence")})

    def _candidate_item(self, ref: Record) -> EvidenceItem:
        return EvidenceItem(Path(ref.data["file"]), "reference",
                            {"reference_id": ref.id, "label": f"candidate ({','.join(ref.data.get('labels') or [])})",
                             "canon_state": "candidate", "note": "CANDIDATE: a hypothesis, not canon; judge it against the anchor"})

    def _canon_description(self, plan: Record) -> Record | None:
        for c in reversed(self.store.list("canon_description")):
            if c.data.get("anchor_reference_id") == plan.data.get("anchor_reference_id"):
                return c
        return None

    # ---------------------------------------------------------------- requests

    def _requests(self, *, kind: str | None = None, states: tuple[str, ...] | None = None, view: str | None = None,
                  study_id: str | None = None) -> list[Record]:
        out = []
        for g in self.store.list("generation"):
            t = g.data.get("target") or {}
            if kind and t.get("kind") != kind:
                continue
            if states and g.state not in states:
                continue
            if view and t.get("view") != view:
                continue
            if study_id and t.get("study_request_id") != study_id:
                continue
            out.append(g)
        return out

    def open_requests(self) -> list[Record]:
        return self._requests(states=("open",))

    def images_count(self) -> int:
        """Imported and generated images alike (R-105): every reference that came from a generation request."""
        return sum(1 for r in self.store.list("reference") if r.data.get("generation_id"))

    def _check_image_limit(self, adding: int) -> None:
        cap = int(self.config.concept["max_images"])
        if cap > 0 and self.images_count() + adding > cap:
            raise ConceptError(f"concept limit max_images={cap} would be exceeded ({self.images_count()} imported or generated "
                               f"so far, {adding} more requested); raise builder.concept.max_images to continue (R-105)")

    def _count_requests(self, target: dict[str, Any]) -> int:
        if target.get("kind") == "view":
            return len(self._requests(kind="view", view=target["view"]))
        if target.get("kind") == "study":
            return len(self._requests(kind="study", study_id=target.get("study_request_id")))
        return len(self._requests(kind="anchor_candidate"))

    def _regenerations(self, target: dict[str, Any]) -> int:
        """Rounds beyond the first for this target (R-105 ``max_regenerations_per_view`` counts them)."""
        return max(0, self._count_requests(target) - 1)

    def _regeneration_allowed(self, target: dict[str, Any]) -> tuple[bool, str]:
        cap = int(self.config.concept["max_regenerations_per_view"])
        done = self._regenerations(target)
        if cap > 0 and done >= cap:
            return False, f"max_regenerations_per_view={cap} reached for {_target_name(target)} ({done} regeneration(s))"
        return True, ""

    def _request(self, plan: Record, *, target: dict[str, Any], prompt: str, prompt_structured: dict[str, Any] | None,
                 attachments: list[Record], art_director: str | None, rationale: str | None, expected_count: int,
                 revises: str | None = None, note: str = "", actor: str = "engine") -> Record:
        self._check_image_limit(expected_count)
        round_no = self._count_requests(target) + 1
        att = [{"reference_id": r.id, "path": r.data["file"], "sha256": r.data["sha256"],
                "role": "anchor" if r.id == plan.data.get("anchor_reference_id") else "reference"} for r in attachments]
        gen = Record.new("generation", {
            "target": dict(target), "prompt": prompt, "prompt_structured": prompt_structured, "attachments": att,
            "seat": self.seat.label, "seat_declared": self.seat.declared(), "expected_count": int(expected_count),
            "round": round_no, "revises": revises, "note": note, "art_director": art_director,
            "art_director_rationale": rationale, "vendor_declared": None, "model_declared": None, "attached_declared": None,
            "provenance": PROVENANCE_NOTE, "outputs": [], "seed": "unknown", "size": "unknown",
            "usage": quantity_to_json(NOT_APPLICABLE), "cost": quantity_to_json(self.seat.cost_quantity()),
            "created_at": utc_now(), "imported_at": None, "success": None, "error": None,
            "prompt_file": None, "manifest_file": None, "request_dir": None,
        })
        rdir = self.dir / "requests" / gen.id
        published = self.seat.publish_request(rdir, request_id=gen.id, target=target, prompt=prompt, attachments=att,
                                              expected_count=expected_count,
                                              import_command=self._import_command(gen.id))
        gen.data.update({"prompt_file": published["prompt_file"], "request_dir": str(rdir),
                         "manifest_file": str(rdir / "manifest.json"), "attachment_copies": published["attachments"]})
        gen = self.store.upsert(gen, actor=actor, event="generation.requested", run_id=self.engine._run_id(),
                                inputs={"target": target, "round": round_no, "art_director": art_director, "rationale": rationale})
        self._write_manifest(gen)
        return gen

    def _import_command(self, request_id: str) -> str:
        return (f"python -m builder concept import \"{self.project.workflow_dir}\" {request_id} <file>... "
                "[--vendor chatgpt|gemini|other] [--model \"<as shown in the app>\"]")

    def _write_manifest(self, gen: Record) -> None:
        path = Path(gen.data["manifest_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"id": gen.id, "state": gen.state, "version": gen.version, **gen.data}, f, ensure_ascii=False, indent=1)

    def _update_request(self, gen: Record, *, state: str | None = None, actor: str, event: str, **changes: Any) -> Record:
        gen.data.update(changes)
        if state:
            gen.state = state
        gen = self.store.upsert(gen, actor=actor, event=event, run_id=self.engine._run_id(),
                                inputs={k: v for k, v in changes.items() if k in ("reason", "vendor_declared", "model_declared")} or None)
        self._write_manifest(gen)
        return gen

    def _request_anchor_round(self, plan: Record, *, actor: str, problems: list[str] | None = None) -> Record:
        label, rationale = self._art_director(plan)
        count = int(plan.data.get("anchor_candidates") or 4)
        sections = {"Asset idea from the owner": plan.data.get("text") or "(none)"}
        if problems:
            sections["Problems with the previous round (from the owner or the verdicts)"] = "\n".join(f"- {p}" for p in problems)
        value, inv = self._ask(label, "art_director_prompt", kind="art_director_anchor",
                               objective=ANCHOR_OBJECTIVE.format(count=count), evidence=[], schema_name="art_director_prompt",
                               extra_sections=sections)
        gen = self._request(plan, target={"kind": "anchor_candidate", "count": count}, prompt=value["prompt"],
                            prompt_structured=value, attachments=[], art_director=label, rationale=rationale,
                            expected_count=count, actor=actor)
        gen.data["invocation_id"] = inv.id
        return self._update_request(gen, actor="engine", event="generation.prompt_recorded")

    def _request_view(self, plan: Record, view: str, *, revises: Record | None = None, problems: list[str] | None = None,
                      actor: str = "engine") -> Record:
        anchor = self._anchor(plan)
        if anchor is None:
            raise ConceptError("no approved anchor to condition on (R-101)")
        label, rationale = self._art_director(plan)
        kind, purpose = "art_director_view", "art_director_prompt"
        objective = VIEW_OBJECTIVE.format(view=view)
        sections: dict[str, str] = {}
        if revises is not None:
            kind = "art_director_revision"
            objective = REVISION_OBJECTIVE.format(target=f"view {view}", request=revises.id)
            sections["Previous prompt"] = revises.data.get("prompt") or ""
            sections["Problems to address"] = "\n".join(f"- {p}" for p in (problems or ["(none stated)"]))
        value, inv = self._ask(label, purpose, kind=kind, objective=objective, evidence=[self._anchor_item(anchor)],
                               schema_name="art_director_prompt", extra_sections=sections or None)
        gen = self._request(plan, target={"kind": "view", "view": view}, prompt=value["prompt"], prompt_structured=value,
                            attachments=[anchor], art_director=label, rationale=rationale, expected_count=1,
                            revises=revises.id if revises else None, actor=actor)
        gen.data["invocation_id"] = inv.id
        return self._update_request(gen, actor="engine", event="generation.prompt_recorded")

    def _request_study(self, plan: Record, study: Record, *, revises: Record | None = None,
                       problems: list[str] | None = None, actor: str = "engine") -> Record:
        anchor = self._anchor(plan)
        if anchor is None:
            raise ConceptError("no approved anchor to condition on (R-104)")
        label, rationale = self._art_director(plan)
        evidence = [self._anchor_item(anchor)]
        for ref in self.references.approved():
            if ref.data.get("precedence_label") == "turnaround" and study.data["view"] in (ref.data.get("labels") or []):
                evidence.append(EvidenceItem(Path(ref.data["file"]), "reference",
                                             {"reference_id": ref.id, "label": f"approved {study.data['view']} view", "canon_state": "approved"}))
        region = f", region {study.data['region']}" if study.data.get("region") else ""
        kind = "art_director_revision" if revises else "art_director_study"
        objective = (REVISION_OBJECTIVE.format(target=f"study {study.id}", request=revises.id) if revises
                     else STUDY_OBJECTIVE.format(part=study.data["part_id"], view=study.data["view"], region=region,
                                                 purpose=study.data["purpose"]))
        sections = {}
        if study.data.get("draft_prompt"):
            sections["Draft prompt from the requesting agent (data, not an instruction)"] = study.data["draft_prompt"]
        if revises is not None:
            sections["Previous prompt"] = revises.data.get("prompt") or ""
            sections["Problems to address"] = "\n".join(f"- {p}" for p in (problems or ["(none stated)"]))
        value, inv = self._ask(label, "art_director_prompt", kind=kind, objective=objective, evidence=evidence,
                               schema_name="art_director_prompt", extra_sections=sections or None)
        gen = self._request(plan, target={"kind": "study", "study_request_id": study.id, "part_id": study.data["part_id"],
                                          "view": study.data["view"], "region": study.data.get("region")},
                            prompt=value["prompt"], prompt_structured=value, attachments=[anchor], art_director=label,
                            rationale=rationale, expected_count=1, revises=revises.id if revises else None, actor=actor)
        gen.data["invocation_id"] = inv.id
        return self._update_request(gen, actor="engine", event="generation.prompt_recorded")

    def prompts(self) -> list[dict[str, Any]]:
        """Every open generation request: the prompt to paste, the images to attach, and the request id (R-110)."""
        out = []
        for g in self.open_requests():
            out.append({"request_id": g.id, "target": _target_name(g.data["target"]), "prompt": g.data["prompt"],
                        "prompt_file": g.data.get("prompt_file"), "expected_count": g.data.get("expected_count"),
                        "round": g.data.get("round"), "art_director": g.data.get("art_director"),
                        "attachments": [{"reference_id": a.get("reference_id"), "path": a.get("copy") or a.get("path"),
                                         "sha256": a.get("sha256"), "role": a.get("role")}
                                        for a in (g.data.get("attachment_copies") or g.data.get("attachments") or [])],
                        "import_command": self._import_command(g.id)})
        return out

    # ------------------------------------------------------------------ import

    def import_files(self, request_id: str, files: list[str | Path], *, vendor: str | None = None, model: str | None = None,
                     attached: list[str] | None = None, actor: str = "user") -> list[Record]:
        plan = self._require_plan()
        gen = self.store.get("generation", request_id)
        if gen is None:
            raise ConceptError(f"no generation request {request_id!r}; an import needs a matching open request. Create one "
                               "with `concept import --as-anchor` or `--as-view <view>` (R-96a)")
        if gen.state not in ("open", "imported"):
            raise ConceptError(f"request {gen.id} is {gen.state}, not open; imports are refused (R-96a)")
        if vendor is not None and vendor not in VENDORS:
            raise ConceptError(f"--vendor must be one of {VENDORS} (a label the owner declares; 'other' for anything else)")
        paths = [Path(f) for f in files]
        if not paths:
            raise ConceptError("no files given")
        for p in paths:
            if not p.is_file():
                raise ConceptError(f"file not found: {p}")
        self._check_image_limit(len(paths))
        target = gen.data["target"]
        label = TARGET_PRECEDENCE[target["kind"]]
        labels = {"anchor_candidate": ["anchor-candidate"], "view": [target.get("view")],
                  "study": [target.get("view") or "detail"]}[target["kind"]]
        declared = {"vendor": vendor, "model": model, "attached": list(attached) if attached is not None else None,
                    "declared_by": actor, "at": utc_now(), "note": PROVENANCE_NOTE}
        refs: list[Record] = []
        for p in paths:
            ref = self.references.add_generated(
                p, generation_id=gen.id, labels=labels, precedence_label=label,
                derived_from=None if target["kind"] == "anchor_candidate" else plan.data.get("anchor_reference_id"),
                declared=declared, part_id=target.get("part_id"), actor=actor,
                notes=f"generated image imported for request {gen.id} ({_target_name(target)}); a hypothesis until approved (R-32)")
            refs.append(ref)
            gen.data["outputs"].append({"reference_id": ref.id, "file": ref.data["file"], "sha256": ref.data["sha256"],
                                        "original_path": str(p), "width": ref.data["width"], "height": ref.data["height"]})
        gen = self._update_request(gen, state="imported", actor=actor, event="generation.imported",
                                   vendor_declared=vendor, model_declared=model, attached_declared=declared["attached"],
                                   imported_at=utc_now(), success=True, error=None)
        if target["kind"] == "study" and target.get("study_request_id"):
            study = self.store.get("study_request", target["study_request_id"])
            if study is not None and study.state == "requested":
                study.state = "generated"
                self.store.upsert(study, actor=actor, event="study_request.generated", run_id=self.engine._run_id())
        if not plan.data.get("flow_confirmed_at"):
            plan.data["flow_confirmed_at"] = utc_now()
        self._save_plan(plan, "concept_plan.import", request_id=gen.id, files=len(paths))
        return refs

    def import_as(self, kind: str, files: list[str | Path], *, view: str | None = None, vendor: str | None = None,
                  model: str | None = None, actor: str = "user") -> list[Record]:
        """R-96a: an import with no matching request needs an owner-created request (`--as-anchor`, `--as-view`)."""
        plan = self._require_plan()
        if kind == "anchor":
            target: dict[str, Any] = {"kind": "anchor_candidate", "count": len(files)}
            attachments: list[Record] = []
        elif kind == "view":
            if not view or view not in plan.data["needed_views"]:
                raise ConceptError(f"--as-view needs one of the needed views {plan.data['needed_views']}, got {view!r}")
            if self._anchor(plan) is None:
                raise ConceptError("approve an anchor before importing views (R-99)")
            target = {"kind": "view", "view": view}
            attachments = [self._anchor(plan)]
        else:
            raise ConceptError("kind must be anchor or view")
        gen = self._request(plan, target=target, prompt="(owner-generated outside a request; the prompt is not recorded)",
                            prompt_structured=None, attachments=attachments, art_director=None, rationale=None,
                            expected_count=len(files), note="request created by the owner at import (R-96a)", actor=actor)
        return self.import_files(gen.id, files, vendor=vendor, model=model, actor=actor)

    def abandon(self, request_id: str, *, reason: str, actor: str = "user") -> Record:
        gen = self._open_request(request_id)
        return self._update_request(gen, state="abandoned", actor=actor, event="generation.abandoned", success=False,
                                    error=f"abandoned by the owner: {reason}", reason=reason)

    def mark_failed(self, request_id: str, *, reason: str, actor: str = "user") -> Record:
        gen = self._open_request(request_id)
        return self._update_request(gen, state="failed", actor=actor, event="generation.failed", success=False,
                                    error=f"marked failed by the owner: {reason}", reason=reason)

    def _open_request(self, request_id: str) -> Record:
        gen = self.store.get("generation", request_id)
        if gen is None:
            raise ConceptError(f"no generation request {request_id!r}")
        if gen.state not in OPEN_REQUEST_STATES:
            raise ConceptError(f"request {gen.id} is {gen.state}; only open, imported, or checked requests can change")
        return gen

    # ---------------------------------------------------------------- verdicts

    def _check_candidate(self, plan: Record, ref: Record) -> Record:
        """R-102: both LLM seats judge the candidate against the anchor and the canon description, each without the
        other's verdict. The first verdict is journaled before the second packet is built."""
        anchor = self._anchor(plan)
        if anchor is None:
            raise ConceptError("no approved anchor to check against")
        seats = consistency_seats(self.engine._labels(), image_seat=self.seat.label)
        view = (ref.data.get("labels") or ["?"])[0]
        for a in seats:
            others = [s.seat for s in seats if s.seat != a.seat]
            withheld = [{"what": f"seat {o}'s verdict on this candidate", "why": "R-102: independent verdicts before sharing"}
                        for o in others]
            ref = self.store.require("reference", ref.id)
            try:
                value, inv = self._ask(a.seat, "consistency_verdict", kind="consistency_check",
                                       objective=VERDICT_OBJECTIVE.format(view=view, reference_id=ref.id),
                                       evidence=[self._anchor_item(anchor), self._candidate_item(ref)],
                                       schema_name="consistency_verdict", withheld=withheld, extra={"reference_id": ref.id})
                verdict = {"verdict": value["verdict"], "inconsistencies": value.get("inconsistencies") or [],
                           "rationale": value.get("rationale"), "invocation_id": inv.id, "seat_rationale": a.rationale,
                           "at": utc_now(), "reference_id_reported": value.get("reference_id")}
                if value.get("reference_id") != ref.id:
                    verdict["verdict"] = "uncertain"
                    verdict["note"] = f"the seat referred to {value.get('reference_id')!r}, not {ref.id}"
            except ConceptError as exc:
                verdict = {"verdict": "malformed", "inconsistencies": [], "rationale": None, "error": str(exc)[:300],
                           "at": utc_now(), "seat_rationale": a.rationale}
            ref.data.setdefault("verdicts", {})[a.seat] = verdict
            ref.data["verdict_summary"] = _summarize(ref.data["verdicts"], expected=[s.seat for s in seats])
            ref = self.store.upsert(ref, actor="engine", event="reference.verdict_recorded", run_id=self.engine._run_id(),
                                    inputs={"seat": a.seat, "verdict": verdict["verdict"], "inconsistencies": len(verdict["inconsistencies"])})
        return ref

    # ---------------------------------------------------------------- approval

    def approve(self, reference_ids: list[str], *, actor: str = "user", criteria: str | None = None,
                auto: bool = False) -> list[Record]:
        plan = self._require_plan()
        mode = plan.data["approval_mode"]
        if not auto and not actor.startswith("user"):
            raise ConceptError(f"under approval mode {mode!r} a manual approval is a user action (R-98)")
        out = []
        for rid in reference_ids:
            ref = self.store.get("reference", rid)
            if ref is None:
                raise ConceptError(f"no reference {rid!r}")
            if ref.data.get("canon_state") == "approved":
                out.append(ref)
                continue
            if ref.data.get("canon_state") != "candidate":
                raise ConceptError(f"{rid} is {ref.data.get('canon_state')}; only candidates can be approved (R-94)")
            gen = self.store.require("generation", ref.data["generation_id"])
            target = gen.data["target"]
            if target["kind"] != "anchor_candidate" and self._anchor(plan) is None:
                raise ConceptError("approve an anchor first (R-99)")
            ref.data["canon_state"] = "approved"
            ref.data["approval"] = {"mode": mode, "by": actor, "at": utc_now(), "auto": bool(auto),
                                    "verdicts": dict(ref.data.get("verdicts") or {}), "criteria": criteria or
                                    ("owner's decision" if not auto else "")}
            if target["kind"] == "anchor_candidate":
                ref.data["is_anchor"] = True
                ref.data["labels"] = ["anchor"]        # the approved candidate is the anchor; packets label it so
            ref = self.store.upsert(ref, actor=actor, event="reference.approved", run_id=self.engine._run_id(),
                                    inputs={"mode": mode, "auto": bool(auto), "criteria": criteria,
                                            "verdicts": {k: v.get("verdict") for k, v in (ref.data.get("verdicts") or {}).items()}})
            out.append(ref)
            # siblings of the same request are not chosen; an earlier approved image for the same target is superseded
            for other in self.store.list("reference"):
                if other.id == ref.id:
                    continue
                if other.data.get("generation_id") == gen.id and other.data.get("canon_state") == "candidate":
                    self._set_state(other, "rejected", actor=actor, reason=f"another candidate of request {gen.id} was approved ({ref.id})")
                elif (other.data.get("canon_state") == "approved" and other.data.get("kind") == "generated"
                      and _same_target(other, self.store, target) and other.data.get("generation_id") != gen.id):
                    other.data["replaced_by"] = ref.id
                    self._set_state(other, "superseded", actor=actor, reason=f"superseded by the newly approved {ref.id}")
                    self._resolve_conflicts_with(other.id, "resolved_by_regeneration", actor=actor,
                                                 reason=f"{other.id} superseded by {ref.id}")
            self._update_request(gen, state="approved", actor=actor, event="generation.approved", approved_reference_id=ref.id)
            if target["kind"] == "anchor_candidate":
                plan.data["anchor_reference_id"] = ref.id
                plan = self._save_plan(plan, "concept_plan.anchor", anchor=ref.id)
                if plan.state in ("no_canon", "anchor_pending"):
                    plan = self._canon(plan, "anchor_approved", actor=actor, reason=f"anchor {ref.id} approved")
            elif target["kind"] == "study" and target.get("study_request_id"):
                study = self.store.get("study_request", target["study_request_id"])
                if study is not None:
                    study.state = "approved"
                    study.data["reference_id"] = ref.id
                    self.store.upsert(study, actor=actor, event="study_request.approved", run_id=self.engine._run_id())
            self._record_conflicts(plan, ref, actor=actor)
        return out

    def reject(self, reference_ids: list[str], *, reason: str, actor: str = "user") -> list[Record]:
        plan = self._require_plan()
        if not reason.strip():
            raise ConceptError("a rejection needs --reason (R-98)")
        out = []
        for rid in reference_ids:
            ref = self.store.get("reference", rid)
            if ref is None:
                raise ConceptError(f"no reference {rid!r}")
            if ref.data.get("canon_state") not in ("candidate", "approved"):
                raise ConceptError(f"{rid} is {ref.data.get('canon_state')}; only candidates or approved images can be rejected")
            was_anchor = plan.data.get("anchor_reference_id") == ref.id
            ref = self._set_state(ref, "rejected", actor=actor, reason=reason)
            out.append(ref)
            self._resolve_conflicts_with(ref.id, "resolved_by_user", actor=actor, reason=reason)
            gen = self.store.get("generation", ref.data.get("generation_id") or "")
            if gen is not None:
                self._settle_request(gen, actor=actor)
            if ref.data.get("part_id") and gen is not None and gen.data["target"].get("study_request_id"):
                study = self.store.get("study_request", gen.data["target"]["study_request_id"])
                if study is not None and study.state in ("generated", "checked", "approved"):
                    study.state = "rejected"
                    self.store.upsert(study, actor=actor, event="study_request.rejected", run_id=self.engine._run_id())
            if was_anchor:
                plan.data["anchor_reference_id"] = None
                plan = self._save_plan(plan, "concept_plan.anchor_rejected", reference_id=ref.id, reason=reason)
                if plan.state in ("anchor_approved", "turnaround_pending", "turnaround_approved", "complete"):
                    if plan.state in ("turnaround_approved", "complete"):
                        plan = self._canon(plan, "turnaround_pending", actor=actor, reason="anchor rejected")
                    plan = self._canon(plan, "anchor_pending", actor=actor, reason=f"anchor {ref.id} rejected: {reason}")
            elif ref.data.get("precedence_label") == "turnaround" and plan.state in ("turnaround_approved", "complete"):
                plan = self._canon(plan, "turnaround_pending", actor=actor, reason=f"approved view {ref.id} rejected: {reason}")
        return out

    def _set_state(self, ref: Record, state: str, *, actor: str, reason: str) -> Record:
        ref.data["canon_state"] = state
        if state == "rejected":
            ref.data["rejection"] = {"by": actor, "at": utc_now(), "reason": reason, "verdicts": dict(ref.data.get("verdicts") or {})}
        return self.store.upsert(ref, actor=actor, event=f"reference.{state}", run_id=self.engine._run_id(), inputs={"reason": reason})

    def _settle_request(self, gen: Record, *, actor: str) -> Record:
        outputs = [self.store.get("reference", o["reference_id"]) for o in gen.data.get("outputs") or []]
        states = {r.data.get("canon_state") for r in outputs if r is not None}
        if outputs and states <= {"rejected", "superseded"} and gen.state in ("imported", "checked", "approved"):
            return self._update_request(gen, state="rejected", actor=actor, event="generation.rejected",
                                        reason="every imported image was rejected")
        return gen

    # --------------------------------------------------------------- conflicts

    def _record_conflicts(self, plan: Record, ref: Record, *, actor: str) -> None:
        """R-95: an approved image whose verdicts named inconsistencies with the anchor contradicts another approved
        image. Recorded, never averaged; the owner resolves it (reject one) or a regeneration supersedes one."""
        anchor_id = plan.data.get("anchor_reference_id")
        if not anchor_id or anchor_id == ref.id:
            return
        for seat, v in (ref.data.get("verdicts") or {}).items():
            for inc in v.get("inconsistencies") or []:
                rec = Record.new("evidence_conflict", {
                    "reference_ids": [ref.id, anchor_id], "region": inc.get("region"), "part": inc.get("part"),
                    "what_differs": inc.get("what_differs"), "severity": inc.get("severity"), "reported_by": seat,
                    "invocation_id": v.get("invocation_id"), "recorded_at": utc_now(), "resolution": None,
                    "note": "approved by the owner despite the verdict; contradiction kept explicit (R-95)"})
                self.store.upsert(rec, actor="engine", event="evidence_conflict.recorded", run_id=self.engine._run_id(),
                                  inputs={"approved_by": actor, "reported_by": seat})

    def open_conflicts(self) -> list[Record]:
        return self.store.list("evidence_conflict", state="open")

    def _resolve_conflicts_with(self, reference_id: str, state: str, *, actor: str, reason: str) -> None:
        for c in self.open_conflicts():
            if reference_id in (c.data.get("reference_ids") or []):
                c.state = state
                c.data["resolution"] = {"by": actor, "at": utc_now(), "reason": reason, "rejected_or_superseded": reference_id}
                self.store.upsert(c, actor=actor, event=f"evidence_conflict.{state}", run_id=self.engine._run_id(),
                                  inputs={"reason": reason})

    def record_conflict(self, reference_ids: list[str], *, region: str, what_differs: str, reported_by: str,
                        actor: str = "engine") -> Record:
        rec = Record.new("evidence_conflict", {"reference_ids": list(reference_ids), "region": region, "what_differs": what_differs,
                                               "reported_by": reported_by, "recorded_at": utc_now(), "resolution": None})
        return self.store.upsert(rec, actor=actor, event="evidence_conflict.recorded", run_id=self.engine._run_id())

    # ---------------------------------------------------------------- coverage

    def coverage(self, plan: Record | None = None) -> dict[str, Any]:
        """R-103: which views and parts have approved references, candidates, open requests, or nothing."""
        plan = plan or self.plan
        if plan is None:
            return {"canon_state": "no_canon", "mode": self.mode, "views": {}, "parts": {}, "missing": [], "complete": False,
                    "conflicts_open": 0, "escalations": [], "anchor": None, "images": {"count": 0, "max": int(self.config.concept["max_images"])}}
        refs = self.store.list("reference")
        views: dict[str, dict[str, Any]] = {}
        for v in plan.data["needed_views"]:
            approved = [r for r in refs if r.data.get("canon_state") == "approved" and v in (r.data.get("labels") or [])
                        and r.data.get("precedence_label") in ("owner_target", "turnaround")]
            cands = [r for r in refs if r.data.get("canon_state") == "candidate" and v in (r.data.get("labels") or [])
                     and r.data.get("precedence_label") == "turnaround"]
            reqs = self._requests(kind="view", view=v, states=("open",))
            status = "approved" if approved else ("candidates" if cands else ("requested" if reqs else "nothing"))
            views[v] = {"status": status, "approved": [r.id for r in approved], "candidates": [r.id for r in cands],
                        "requests_open": [g.id for g in reqs]}
        parts: dict[str, dict[str, Any]] = {}
        for s in self.store.list("study_request"):
            entry = parts.setdefault(s.data["part_id"], {"studies": []})
            entry["studies"].append({"id": s.id, "state": s.state, "view": s.data.get("view"), "reference_id": s.data.get("reference_id")})
        anchor = plan.data.get("anchor_reference_id")
        missing = [v for v, e in views.items() if e["status"] != "approved"]
        conflicts = len(self.open_conflicts())
        return {"canon_state": plan.state, "mode": plan.data["approval_mode"], "anchor": anchor, "views": views, "parts": parts,
                "missing": missing, "complete": plan.state == "complete", "conflicts_open": conflicts,
                "escalations": list(plan.data.get("escalations") or []),
                "images": {"count": self.images_count(), "max": int(self.config.concept["max_images"])},
                "proceeded_partial": plan.data.get("proceeded_partial")}

    def proceed(self, *, actor: str = "user") -> Record:
        """R-103: the owner accepts a partial set explicitly; recorded with what is missing."""
        plan = self._require_plan()
        if plan.state == "complete":
            return plan
        cov = self.coverage(plan)
        if cov["conflicts_open"]:
            raise ConceptError(f"{cov['conflicts_open']} evidence conflict(s) are open; reject one image of each conflict or "
                               "regenerate before proceeding (R-95)")
        if plan.state not in ("turnaround_pending", "turnaround_approved"):
            raise ConceptError(f"canon is {plan.state}; an approved anchor and canon description are required before proceeding")
        if plan.state == "turnaround_approved":
            return self._canon(plan, "complete", actor=actor, reason="needed set approved")
        return self._canon(plan, "complete", actor=actor, reason="owner proceeds with a partial reference set (R-103)",
                           inputs={"proceed": True, "missing": cov["missing"]})

    # ----------------------------------------------------------------- studies

    def study(self, part_id: str, *, view: str, purpose: str, region: str | None = None, draft_prompt: str | None = None,
              requested_by: str = "user", actor: str = "user") -> Record:
        plan = self._require_plan()
        if self.store.get("part", part_id) is None:
            raise ConceptError(f"no part {part_id!r} in the inventory; studies attach to parts (R-104)")
        if self._anchor(plan) is None:
            raise ConceptError("approve an anchor before requesting studies (R-104)")
        study = Record.new("study_request", {"part_id": part_id, "view": view, "region": region, "purpose": purpose,
                                             "draft_prompt": draft_prompt, "requested_by": requested_by, "created_at": utc_now(),
                                             "reference_id": None})
        study = self.store.upsert(study, actor=actor, event="study_request.created", run_id=self.engine._run_id(),
                                  inputs={"part_id": part_id, "view": view, "requested_by": requested_by})
        if part_id not in plan.data.get("needed_parts", []):
            plan.data.setdefault("needed_parts", []).append(part_id)
            self._save_plan(plan, "concept_plan.study", study_id=study.id)
        self._request_study(self.plan, study, actor=actor)
        return self.store.require("study_request", study.id)

    # -------------------------------------------------------------- regenerate

    def regenerate(self, request_or_reference_id: str, *, note: str = "", actor: str = "user") -> Record:
        """R-110: writes a revised prompt as a new request; the old request is abandoned."""
        plan = self._require_plan()
        gen = self.store.get("generation", request_or_reference_id)
        if gen is None:
            ref = self.store.get("reference", request_or_reference_id)
            if ref is None or not ref.data.get("generation_id"):
                raise ConceptError(f"{request_or_reference_id!r} is neither a generation request nor a generated reference")
            gen = self.store.require("generation", ref.data["generation_id"])
        target = gen.data["target"]
        ok, why = self._regeneration_allowed(target)
        if not ok:
            raise ConceptError(f"{why}; raise builder.concept.max_regenerations_per_view or approve/reject what exists (R-105)")
        problems = [f"owner note: {note}"] if note else []
        problems += _verdict_problems(self.store, gen)
        if gen.state in OPEN_REQUEST_STATES:
            self._update_request(gen, state="abandoned", actor=actor, event="generation.abandoned", success=False,
                                 error=f"superseded by a regeneration requested by {actor}", reason=note or "regenerate")
        if target["kind"] == "view":
            return self._request_view(plan, target["view"], revises=gen, problems=problems, actor=actor)
        if target["kind"] == "study":
            study = self.store.require("study_request", target["study_request_id"])
            return self._request_study(plan, study, revises=gen, problems=problems, actor=actor)
        return self._request_anchor_round(plan, actor=actor, problems=problems)

    # ----------------------------------------------------------------- advance

    def advance(self, actor: str = "engine") -> list[str]:
        """Perform the automatic work the owner's last action unlocked. Idempotent; every step is journaled."""
        plan = self._require_plan()
        events: list[str] = []
        mode = plan.data["approval_mode"]
        labels = self.engine._labels()

        # 1. anchor: auto pick, or a new anchor round after a rejection
        if plan.state == "anchor_pending":
            cands = [r for r in self.references.candidates() if r.data.get("precedence_label") == "anchor"]
            if cands and mode == "auto":
                events.append(self._auto_pick_anchor(plan, cands))
                plan = self.plan
            elif not cands and not self._requests(kind="anchor_candidate", states=("open", "imported")):
                gen = self._request_anchor_round(plan, actor=actor, problems=self._last_rejection_reasons("anchor"))
                events.append(f"anchor round {gen.data['round']} requested from art director {gen.data['art_director']}: {gen.id}")
                plan = self.plan
        if plan.state in ("no_canon", "anchor_pending"):
            self._save_plan(plan, "concept_plan.advanced", events=events)
            return events

        # 2. canon description (R-101), versioned per anchor
        if self._canon_description(plan) is None:
            events.append(self._write_canon_description(plan))
            plan = self.plan
        if plan.state == "anchor_approved":
            cov = self.coverage(plan)
            if not cov["missing"] and not cov["conflicts_open"]:
                plan = self._canon(plan, "turnaround_approved", actor="engine", reason="every needed view is owner-supplied")
            else:
                plan = self._canon(plan, "turnaround_pending", actor="engine", reason="canon description recorded; views requested")

        # 3. verdicts for unchecked candidates (views and studies), both seats independently (R-102)
        for ref in self.references.candidates():
            if ref.data.get("precedence_label") == "anchor":
                continue
            if set(ref.data.get("verdicts") or {}) >= set(labels):
                continue
            ref = self._check_candidate(plan, ref)
            events.append(f"verdicts for {ref.id} ({','.join(ref.data.get('labels') or [])}): "
                          + ", ".join(f"{s}={v['verdict']}" for s, v in ref.data["verdicts"].items()))
            gen = self.store.require("generation", ref.data["generation_id"])
            outs = [self.store.require("reference", o["reference_id"]) for o in gen.data["outputs"]]
            if all(set(o.data.get("verdicts") or {}) >= set(labels) for o in outs) and gen.state == "imported":
                self._update_request(gen, state="checked", actor="engine", event="generation.checked")
                if gen.data["target"].get("study_request_id"):
                    study = self.store.get("study_request", gen.data["target"]["study_request_id"])
                    if study is not None and study.state == "generated":
                        study.state = "checked"
                        self.store.upsert(study, actor="engine", event="study_request.checked", run_id=self.engine._run_id())

        # 4. decide per checked candidate: auto-approve, regenerate (bounded), or escalate to the owner
        for ref in self.references.candidates():
            if ref.data.get("precedence_label") == "anchor" or set(ref.data.get("verdicts") or {}) < set(labels):
                continue
            events.extend(self._decide(plan, ref, mode, actor=actor))
            plan = self.plan

        # 5. open requests for needed views without an approved image, a candidate, or an open request
        if plan.state in ("turnaround_pending",):
            for v in plan.data["needed_views"]:
                entry = self.coverage(plan)["views"][v]
                if entry["status"] != "nothing":
                    continue
                prior = self._requests(kind="view", view=v)
                if prior:
                    ok, why = self._regeneration_allowed({"kind": "view", "view": v})
                    if not ok:
                        self._escalate(plan, view=v, reason=f"{why}; the owner decides: approve a candidate, `concept regenerate` "
                                                            "after raising the cap, or `concept proceed`")
                        continue
                    gen = self._request_view(plan, v, revises=prior[-1], problems=self._last_rejection_reasons(v), actor=actor)
                else:
                    gen = self._request_view(plan, v, actor=actor)
                events.append(f"view '{v}' round {gen.data['round']} requested from art director {gen.data['art_director']}: {gen.id}")
                plan = self.plan

        # 6. coverage check and hand-off (R-103)
        cov = self.coverage(plan)
        if plan.state == "turnaround_pending" and not cov["missing"] and not cov["conflicts_open"]:
            plan = self._canon(plan, "turnaround_approved", actor="engine", reason="every needed view approved")
        if plan.state == "turnaround_approved" and not cov["conflicts_open"]:
            plan = self._canon(plan, "complete", actor="engine", reason="needed reference set approved; hand-off to intake")
            events.append("canon complete: the approved set is ready for intake")
        elif cov["missing"]:
            events.append(f"waiting for the owner: views without an approved image: {', '.join(cov['missing'])}")
        self._save_plan(self.plan, "concept_plan.advanced", events=events)
        return events

    def _decide(self, plan: Record, ref: Record, mode: str, *, actor: str) -> list[str]:
        events: list[str] = []
        gen = self.store.require("generation", ref.data["generation_id"])
        target = gen.data["target"]
        summary = ref.data.get("verdict_summary")
        name = _target_name(target)
        if summary == "consistent":
            if mode in ("anchor_only", "auto"):
                self.approve([ref.id], actor="engine", auto=True,
                             criteria=f"both LLM seats independently judged {ref.id} consistent with the anchor and canon; "
                                      f"no open conflict; mode {mode} (D11)")
                events.append(f"auto-approved {ref.id} for {name} (mode {mode})")
            else:
                events.append(f"{ref.id} ({name}) judged consistent by both seats; awaiting the owner's approval (mode each)")
            return events
        problems = _problems_of(ref)
        if summary == "inconsistent":
            ok, why = self._regeneration_allowed(target)
            if mode in ("anchor_only", "auto"):
                self.reject([ref.id], reason=f"inconsistent per verdicts: {'; '.join(problems)[:400]}", actor="engine")
                events.append(f"rejected {ref.id} ({name}): inconsistent per verdicts")
            else:
                events.append(f"{ref.id} ({name}) has inconsistencies ({len(problems)}); the owner may approve it (recorded with the "
                              "verdicts) or reject it")
            if ok:
                if gen.state in OPEN_REQUEST_STATES and mode != "each":
                    self._update_request(gen, state="rejected", actor="engine", event="generation.rejected",
                                         reason="rejected per verdicts; regenerated")
                new = self._request_for(plan, target, revises=gen, problems=problems, actor=actor)
                events.append(f"regeneration round {new.data['round']} requested for {name}: {new.id}")
            else:
                self._escalate(plan, view=target.get("view"), reason=f"{why}; inconsistencies remain: {'; '.join(problems)[:300]}")
                events.append(f"escalated {name} to the owner: {why}")
            return events
        # uncertain or malformed: never auto-approved, never silently regenerated; the owner decides
        self._escalate(plan, view=target.get("view"), reason=f"verdicts on {ref.id} are {summary} "
                                                              f"({', '.join(f'{s}={v.get('verdict')}' for s, v in ref.data['verdicts'].items())}); "
                                                              "the owner decides: approve, reject, or regenerate")
        events.append(f"escalated {name} to the owner: verdicts {summary}")
        return events

    def _request_for(self, plan: Record, target: dict[str, Any], *, revises: Record, problems: list[str], actor: str) -> Record:
        if target["kind"] == "view":
            return self._request_view(plan, target["view"], revises=revises, problems=problems, actor=actor)
        if target["kind"] == "study":
            return self._request_study(plan, self.store.require("study_request", target["study_request_id"]), revises=revises,
                                       problems=problems, actor=actor)
        return self._request_anchor_round(plan, actor=actor, problems=problems)

    def _escalate(self, plan: Record, *, view: str | None, reason: str) -> None:
        plan = self.store.require("concept_plan", plan.id)
        entries = plan.data.setdefault("escalations", [])
        if any(e.get("view") == view and e.get("reason") == reason and e.get("open") for e in entries):
            return
        entries.append({"view": view, "reason": reason, "at": utc_now(), "open": True})
        self._save_plan(plan, "concept_plan.escalated", view=view, reason=reason)

    def _auto_pick_anchor(self, plan: Record, cands: list[Record]) -> str:
        label, rationale = self._art_director(plan)
        ids = [c.id for c in cands]
        value, inv = self._ask(label, "anchor_pick", kind="anchor_pick", objective=PICK_OBJECTIVE.format(ids=", ".join(ids)),
                               evidence=[self._candidate_item(c) for c in cands], schema_name="anchor_pick",
                               extra_sections={"Asset idea from the owner": plan.data.get("text") or "(none)"},
                               extra={"first_candidate_id": ids[0], "second_candidate_id": ids[1] if len(ids) > 1 else ids[0],
                                      "candidate_ids": ids})
        chosen = value["chosen_reference_id"]
        if chosen not in ids:
            raise ConceptError(f"the art director chose {chosen!r}, not one of the candidates {ids}; nothing approved (R-5)")
        self.approve([chosen], actor=f"engine:art_director:{label}", auto=True, criteria=list(value.get("criteria") or []))
        ref = self.store.require("reference", chosen)
        ref.data["approval"]["criteria"] = list(value.get("criteria") or [])
        ref.data["approval"]["rejected_alternatives"] = value.get("rejected") or []
        ref.data["approval"]["invocation_id"] = inv.id
        self.store.upsert(ref, actor="engine", event="reference.auto_pick_recorded", run_id=self.engine._run_id(),
                          inputs={"criteria": value.get("criteria"), "art_director": label})
        return f"anchor auto-picked by art director {label}: {chosen} (criteria: {', '.join(value.get('criteria') or [])}); reversible with `concept reject`"

    def _write_canon_description(self, plan: Record) -> str:
        anchor = self._anchor(plan)
        if anchor is None:
            raise ConceptError("no approved anchor; cannot write the canon description (R-101)")
        label, rationale = self._art_director(plan)
        evidence = [self._anchor_item(anchor)]
        for ref in self.references.approved():
            if ref.id != anchor.id and ref.data.get("precedence_label") == "owner_target":
                evidence.append(EvidenceItem(Path(ref.data["file"]), "reference",
                                             {"reference_id": ref.id, "label": "owner seed " + ",".join(ref.data.get("labels") or []),
                                              "canon_state": "approved"}))
        sections = {"Asset idea from the owner": plan.data.get("text") or "(none)"}
        value, inv = self._ask(label, "canon_description", kind="canon_description", objective=CANON_OBJECTIVE,
                               evidence=evidence, schema_name="canon_description", extra_sections=sections)
        version = len(self.store.list("canon_description")) + 1
        rec = Record.new("canon_description", {"text": _canon_text(value), "structured": value, "version": version,
                                               "anchor_reference_id": anchor.id, "written_by": label, "rationale": rationale,
                                               "invocation_id": inv.id, "created_at": utc_now()})
        self.store.upsert(rec, actor="engine", event="canon_description.recorded", run_id=self.engine._run_id(),
                          inputs={"anchor": anchor.id, "version": version, "by": label})
        return f"canon description v{version} written by art director {label} from anchor {anchor.id}"

    def _last_rejection_reasons(self, view_or_anchor: str) -> list[str]:
        out: list[str] = []
        for ref in reversed(self.store.list("reference")):
            if ref.data.get("canon_state") != "rejected":
                continue
            lab = ref.data.get("precedence_label")
            if (view_or_anchor == "anchor" and lab == "anchor") or (view_or_anchor in (ref.data.get("labels") or []) and lab == "turnaround"):
                rej = ref.data.get("rejection") or {}
                out.append(f"{ref.id}: {rej.get('reason')}")
                out.extend(_problems_of(ref))
                break
        return out

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        plan = self.plan
        cov = self.coverage(plan)
        gens = self.store.list("generation")
        return {"plan_id": plan.id if plan else None, "canon_state": cov["canon_state"], "mode": self.mode, "mode_text": self.mode_text(),
                "anchor": cov["anchor"], "coverage": cov, "images": cov["images"],
                "cost": quantity_to_json(self.seat.cost_quantity()), "seat": self.seat.declared(),
                "requests": {"open": [g.id for g in gens if g.state == "open"],
                             "by_state": {s: sum(1 for g in gens if g.state == s) for s in KIND_STATES}},
                "candidates": [{"id": r.id, "labels": r.data.get("labels"), "verdicts": {k: v.get("verdict") for k, v in (r.data.get("verdicts") or {}).items()},
                                "summary": r.data.get("verdict_summary")} for r in self.references.candidates()],
                "conflicts_open": [c.id for c in self.open_conflicts()], "escalations": cov["escalations"],
                "art_director": (plan.data.get("art_director") if plan else None), "rejected_rounds": self.rejected_rounds(),
                "flow_confirmed_at": plan.data.get("flow_confirmed_at") if plan else None,
                "consumption": self.engine.limits.status()}

    def summary(self) -> dict[str, Any]:
        """Small view for ``status`` (mode always shown, D11)."""
        plan = self.plan
        cov = self.coverage(plan)
        return {"plan_id": plan.id if plan else None, "canon_state": cov["canon_state"], "mode": self.mode, "mode_text": self.mode_text(),
                "anchor": cov["anchor"], "missing": cov["missing"], "complete": cov["complete"], "conflicts_open": cov["conflicts_open"],
                "open_requests": [g.id for g in self.open_requests()], "images": cov["images"], "seat": self.seat.declared(),
                "escalations": cov["escalations"], "proceeded_partial": cov.get("proceeded_partial")}

    def show(self, record_id: str) -> dict[str, Any]:
        prefix = record_id.split("_", 1)[0]
        kind = {"ref": "reference", "gen": "generation", "study": "study_request", "conf": "evidence_conflict",
                "canon": "canon_description", "cplan": "concept_plan"}.get(prefix)
        rec = self.store.get(kind, record_id) if kind else None
        if rec is None:
            raise ConceptError(f"no concept record {record_id!r} (reference, generation, study, conflict, canon, or plan id)")
        return {"kind": rec.kind, "id": rec.id, "state": rec.state, "version": rec.version, **rec.data}


KIND_STATES = ("open", "imported", "checked", "approved", "rejected", "abandoned", "failed")


def _target_name(target: dict[str, Any]) -> str:
    kind = target.get("kind")
    if kind == "anchor_candidate":
        return "anchor candidates"
    if kind == "view":
        return f"view {target.get('view')}"
    if kind == "study":
        return f"study of {target.get('part_id')} ({target.get('view')})"
    return str(target)


def _same_target(other: Record, store: Any, target: dict[str, Any]) -> bool:
    gen = store.get("generation", other.data.get("generation_id") or "")
    if gen is None:
        return False
    t = gen.data.get("target") or {}
    if t.get("kind") != target.get("kind"):
        return False
    if t.get("kind") == "view":
        return t.get("view") == target.get("view")
    if t.get("kind") == "study":
        return t.get("study_request_id") == target.get("study_request_id")
    return False    # a second anchor candidate approval is refused earlier (anchor already approved)


def _summarize(verdicts: dict[str, dict[str, Any]], *, expected: list[str]) -> str:
    if set(verdicts) < set(expected):
        return "pending"
    values = [v.get("verdict") for v in verdicts.values()]
    if any(v == "malformed" for v in values):
        return "malformed"
    if any(v == "inconsistent" for v in values):
        return "inconsistent"
    if any(v == "uncertain" for v in values):
        return "uncertain"
    return "consistent"


def _problems_of(ref: Record) -> list[str]:
    out = []
    for seat, v in (ref.data.get("verdicts") or {}).items():
        for inc in v.get("inconsistencies") or []:
            out.append(f"seat {seat}: {inc.get('part')} / {inc.get('region')}: {inc.get('what_differs')} ({inc.get('severity', '?')})")
    return out


def _verdict_problems(store: Any, gen: Record) -> list[str]:
    out: list[str] = []
    for o in gen.data.get("outputs") or []:
        ref = store.get("reference", o["reference_id"])
        if ref is not None:
            out.extend(_problems_of(ref))
    return out


def _canon_text(c: dict[str, Any]) -> str:
    lines = [f"Silhouette and proportions: {c.get('silhouette_and_proportions')}",
             f"Main masses: {c.get('main_masses')}", "Parts and construction:"]
    for p in c.get("parts_and_construction") or []:
        lines.append(f"  - {p.get('name')}: {p.get('description')}")
    lines.append(f"Materials and colors: {c.get('materials_and_colors')}")
    lines.append("Distinguishing details: " + ("; ".join(c.get("distinguishing_details") or []) or "none"))
    lines.append("Unknown (not established by the anchor): " + ("; ".join(c.get("unknowns") or []) or "none"))
    return "\n".join(lines)
