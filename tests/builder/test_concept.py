"""Concept stage (addendum A.4, layer D): canon states and precedence with conflicts never averaged (R-94, R-95,
R-106); manifests for success and failure with originals unmodified (R-96, R-97); approval modes with recorded
verdicts and criteria (D11, R-98, R-100); independent verdicts before sharing (R-102); coverage and recorded partial
proceed (R-103); studies attached to parts with lower precedence (R-104); image limits and not-applicable cost
(R-105); art-director rotation (R-108); manual import provenance (D12, R-96a); prompts and regeneration (R-110).

The image seat is mocked by prepared PNGs imported through the real ``concept import`` path; the LLM seats are
scripted mocks. They prove routing, provenance, and approval handling, never image quality."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from builder.concept import MODE_TEXT, ConceptError, ConceptStage, canon_rank
from builder.config import BuilderConfig
from builder.engine import Engine, EngineFailure
from builder.ids import sha256_file
from builder.project import Project
from builder.providers.mock import ScriptedAdapter
from builder.records import Record
from builder.roles import consistency_seats

from _fake_runner import FakeRunner

AD_PROMPT = {"subject": "hexapod maintenance drone", "silhouette": "low and wide", "construction": "six jointed legs on a flat hull",
             "materials": "matte steel, brass fittings", "palette": "olive and brass", "style": "concept sheet", "framing": "three-quarter",
             "prompt": "PROMPT-TEXT: hexapod drone, olive hull, brass fittings, neutral grey background", "avoid": ["text", "watermark"],
             "attachments": []}
CANON = {"silhouette_and_proportions": "low, wide, 2:1", "main_masses": "hull, six legs", "parts_and_construction": [
    {"name": "hull", "description": "flat box"}, {"name": "leg", "description": "three segments"}],
    "materials_and_colors": "olive paint, brass", "distinguishing_details": ["brass knee caps"], "unknowns": ["underside"]}
VERDICT_OK = {"reference_id": "{reference_id}", "verdict": "consistent", "inconsistencies": [], "rationale": "matches the anchor"}
VERDICT_BAD = {"reference_id": "{reference_id}", "verdict": "inconsistent",
               "inconsistencies": [{"part": "leg", "region": "left front knee", "what_differs": "three toes instead of two", "severity": "high"}],
               "rationale": "toe count differs"}
VERDICT_UNSURE = {"reference_id": "{reference_id}", "verdict": "uncertain", "inconsistencies": [], "rationale": "the view is too dark"}
PICK = {"chosen_reference_id": "{first_candidate_id}", "criteria": ["clearest silhouette", "construction readable"],
        "rejected": [{"reference_id": "{second_candidate_id}", "reason": "cluttered"}], "rationale": "scripted pick"}


def png(path: Path, color=(30, 120, 200), size=(96, 64)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def screenplay(verdicts_a=None, verdicts_b=None, prompts=8, picks=2):
    va = list(verdicts_a or [VERDICT_OK] * 8)
    vb = list(verdicts_b or [VERDICT_OK] * 8)
    return {
        "A": {"art_director_prompt": [AD_PROMPT] * prompts, "canon_description": [CANON] * 2,
              "consistency_verdict": va, "anchor_pick": [PICK] * picks},
        "B": {"art_director_prompt": [{**AD_PROMPT, "prompt": "PROMPT-TEXT-FROM-B"}] * prompts, "canon_description": [CANON] * 2,
              "consistency_verdict": vb, "anchor_pick": [PICK] * picks},
    }


def make(workdir, *, play=None, approval="each", views=("front", "side"), max_images=40, max_regen=3, name="Drone ü"):
    prj = Project.create(workdir / "wf ü", name=name, asset_name=name, extra={"first_component": "Hull"})
    cfg = BuilderConfig.from_dict({"builder": {"concept": {"approval": approval, "views": list(views), "max_images": max_images,
                                                            "max_regenerations_per_view": max_regen, "anchor_candidates": 2},
                                               "image_generation": {"seat": "manual", "vendor": "chatgpt"}}})
    play = play or screenplay()
    adapters = {"A": ScriptedAdapter("A", play["A"]), "B": ScriptedAdapter("B", play["B"])}
    eng = Engine(prj, cfg, adapters=adapters, runner=FakeRunner())
    return prj, eng, ConceptStage(eng)


def start_text(concept, **kw):
    return concept.start(from_text="a hexapod maintenance drone", actor="user:josh", **kw)


def open_requests(concept, kind=None):
    return [g for g in concept.open_requests() if kind is None or g.data["target"]["kind"] == kind]


# --- start, prompts, requests ------------------------------------------------------------------------------

def test_start_from_text_opens_anchor_request_with_prompt_file_and_rationale(workdir):
    prj, eng, concept = make(workdir)
    plan = start_text(concept)
    assert plan.state == "anchor_pending" and plan.data["approval_mode"] == "each" and plan.data["start_mode"] == "text"
    assert plan.data["art_director"]["seat"] == "A" and "R-108" in plan.data["art_director"]["rationale"]   # R-107, R-108
    reqs = open_requests(concept, "anchor_candidate")
    assert len(reqs) == 1 and reqs[0].data["target"]["count"] == 2 and reqs[0].data["seat"] == "I"
    gen = reqs[0]
    assert gen.data["prompt"] == AD_PROMPT["prompt"] and gen.data["prompt_structured"]["silhouette"] == "low and wide"   # R-100
    prompt_file = Path(gen.data["prompt_file"])
    assert prompt_file.is_file() and prompt_file.parent == prj.path("concept", "requests", gen.id)
    assert AD_PROMPT["prompt"] in prompt_file.read_text(encoding="utf-8")
    manifest = json.loads(Path(gen.data["manifest_file"]).read_text(encoding="utf-8"))
    assert manifest["id"] == gen.id and manifest["state"] == "open" and manifest["cost"]["kind"] == "not_applicable"   # R-96
    printed = concept.prompts()
    assert printed[0]["request_id"] == gen.id and printed[0]["prompt"] == AD_PROMPT["prompt"] and printed[0]["attachments"] == []
    assert "concept import" in printed[0]["import_command"] and gen.id in printed[0]["import_command"]
    assert MODE_TEXT["each"] in concept.mode_text()
    with pytest.raises(ConceptError):
        start_text(concept)   # one concept stage per project
    prj.close()


def test_start_needs_text_or_images_and_a_known_mode(workdir):
    prj, eng, concept = make(workdir)
    with pytest.raises(ConceptError):
        concept.start(actor="user")
    with pytest.raises(ConceptError):
        concept.start(from_text="x", approval="sometimes", actor="user")
    prj.close()


# --- manual import (D12, R-96, R-96a, R-97) --------------------------------------------------------------------

def test_import_copies_unmodified_hashes_links_and_records_declarations(workdir):
    prj, eng, concept = make(workdir)
    start_text(concept)
    gen = open_requests(concept)[0]
    f1 = png(workdir / "out" / "cand ü 1.png", (10, 10, 10))
    f2 = png(workdir / "out" / "cand 2.png", (200, 10, 10))
    before = {p: sha256_file(p) for p in (f1, f2)}
    refs = concept.import_files(gen.id, [f1, f2], vendor="chatgpt", model="GPT Image (as shown)", attached=[], actor="user:josh")
    assert len(refs) == 2
    for ref, src in zip(refs, (f1, f2)):
        assert ref.data["canon_state"] == "candidate" and ref.data["kind"] == "generated" and ref.data["generation_id"] == gen.id
        assert ref.data["evidence_of_original"] is False and ref.data["precedence_label"] == "anchor"
        copy = Path(ref.data["file"])
        assert copy.parent == prj.path("refs", "generated", gen.id) and sha256_file(copy) == before[src] == ref.data["sha256"]
        assert sha256_file(src) == before[src]                     # original untouched (R-97)
        decl = ref.data["declared"]
        assert decl["vendor"] == "chatgpt" and decl["model"] == "GPT Image (as shown)" and "declar" in decl["note"]
        assert "verified" not in decl["note"].replace("not verified", "")
    gen = prj.store.require("generation", gen.id)
    assert gen.state == "imported" and [o["reference_id"] for o in gen.data["outputs"]] == [r.id for r in refs]
    assert gen.data["vendor_declared"] == "chatgpt" and gen.data["success"] is True and gen.data["imported_at"]
    manifest = json.loads(Path(gen.data["manifest_file"]).read_text(encoding="utf-8"))
    assert manifest["state"] == "imported" and manifest["outputs"][1]["sha256"] == before[f2]
    assert manifest["cost"]["kind"] == "not_applicable" and manifest["usage"]["kind"] == "not_applicable"   # R-105
    assert manifest["vendor_declared"] == "chatgpt" and manifest["provenance"].startswith("declared")
    assert concept.plan.data["flow_confirmed_at"]                    # R-109 local tier: the flow was confirmed once
    with pytest.raises(ConceptError):
        concept.import_files(gen.id, [f1], vendor="midjourney", actor="user")   # unknown vendor label refused
    prj.close()


def test_import_without_matching_open_request_is_refused_unless_as_anchor_or_as_view(workdir):
    prj, eng, concept = make(workdir)
    start_text(concept)
    f = png(workdir / "x.png")
    with pytest.raises(ConceptError):
        concept.import_files("gen_NOPE", [f], actor="user")
    gen = open_requests(concept)[0]
    concept.abandon(gen.id, reason="changed my mind", actor="user:josh")
    with pytest.raises(ConceptError):
        concept.import_files(gen.id, [f], actor="user")             # abandoned is not open
    refs = concept.import_as("anchor", [f], vendor="gemini", actor="user:josh")   # R-96a: owner creates the request
    owner_gen = prj.store.require("generation", refs[0].data["generation_id"])
    assert owner_gen.data["target"]["kind"] == "anchor_candidate" and owner_gen.data["art_director"] is None
    assert "not recorded" in owner_gen.data["prompt"] and owner_gen.state == "imported"
    concept.approve([refs[0].id], actor="user:josh")
    concept.advance()
    side = concept.import_as("view", [png(workdir / "s.png", (1, 2, 3))], view="side", actor="user:josh")
    assert side[0].data["labels"] == ["side"] and side[0].data["derived_from"] == refs[0].id
    with pytest.raises(ConceptError):
        concept.import_as("view", [f], view="underside-left", actor="user")   # not a needed view
    prj.close()


def test_manifest_is_written_for_abandoned_and_failed_requests(workdir):
    prj, eng, concept = make(workdir)
    start_text(concept)
    gen = open_requests(concept)[0]
    concept.mark_failed(gen.id, reason="the app refused the prompt", actor="user:josh")
    m = json.loads(Path(gen.data["manifest_file"]).read_text(encoding="utf-8"))
    assert m["state"] == "failed" and m["success"] is False and "refused" in m["error"] and m["outputs"] == []
    concept.advance()   # a failed anchor round: the art director writes a new request
    gen2 = open_requests(concept, "anchor_candidate")[0]
    assert gen2.id != gen.id and gen2.data["round"] == 2
    concept.abandon(gen2.id, reason="skip", actor="user:josh")
    m2 = json.loads(Path(gen2.data["manifest_file"]).read_text(encoding="utf-8"))
    assert m2["state"] == "abandoned" and m2["success"] is False and m2["error"] == "abandoned by the owner: skip"
    prj.close()


# --- approval modes (D11, R-98, R-100, R-101) -----------------------------------------------------------------

def test_each_mode_anchor_approval_then_canon_description_and_view_requests(workdir):
    prj, eng, concept = make(workdir)
    start_text(concept)
    gen = open_requests(concept)[0]
    c1, c2 = concept.import_files(gen.id, [png(workdir / "a.png"), png(workdir / "b.png", (9, 9, 9))], actor="user:josh")
    with pytest.raises(ConceptError):
        concept.approve([c1.id], actor="engine")   # each: only the owner approves
    approved = concept.approve([c1.id], actor="user:josh")[0]
    assert approved.data["canon_state"] == "approved" and approved.data["approval"]["mode"] == "each"
    assert approved.data["approval"]["by"] == "user:josh" and approved.data["approval"]["auto"] is False
    assert prj.store.require("reference", c2.id).data["canon_state"] == "rejected"
    assert concept.plan.state == "anchor_approved" and concept.plan.data["anchor_reference_id"] == c1.id
    assert prj.store.require("generation", gen.id).state == "approved"
    events = concept.advance()
    canon = prj.store.list("canon_description")[-1]
    assert canon.data["anchor_reference_id"] == c1.id and canon.data["version"] == 1 and "brass knee caps" in canon.data["text"]   # R-101
    assert canon.data["written_by"] == "A"
    assert concept.plan.state == "turnaround_pending"
    views = {g.data["target"]["view"]: g for g in open_requests(concept, "view")}
    assert set(views) == {"front", "side"}
    att = views["front"].data["attachments"]
    assert att and att[0]["reference_id"] == c1.id and att[0]["sha256"] == c1.data["sha256"] and att[0]["role"] == "anchor"   # R-96
    text = Path(views["front"].data["prompt_file"]).read_text(encoding="utf-8")
    assert "attach" in text.lower() and c1.data["sha256"][:12] in text
    assert any("canon" in e for e in events) and any("front" in e for e in events)
    # the art director's packet carried the canon description and the anchor (labelled approved), never a candidate
    ad_packets = [p for p in prj.store.list("packet") if p.data["kind"] == "art_director_view"]
    assert ad_packets and all(any(f["meta"].get("canon_state") == "approved" for f in p.data["files"]) for p in ad_packets)
    prj.close()


def _to_turnaround(workdir, concept, extra=""):
    start_text(concept)
    gen = open_requests(concept)[0]
    c1, = concept.import_files(gen.id, [png(workdir / f"anchor{extra}.png")], actor="user:josh")
    concept.approve([c1.id], actor="user:josh")
    concept.advance()
    return c1


def test_view_verdicts_are_independent_and_recorded_before_sharing(workdir):
    prj, eng, concept = make(workdir, play=screenplay(verdicts_a=[VERDICT_OK] * 4, verdicts_b=[VERDICT_BAD] + [VERDICT_OK] * 3))
    anchor = _to_turnaround(workdir, concept)
    front = next(g for g in open_requests(concept, "view") if g.data["target"]["view"] == "front")
    cand, = concept.import_files(front.id, [png(workdir / "front.png", (50, 50, 50))], vendor="gemini", actor="user:josh")
    events = concept.advance()
    cand = prj.store.require("reference", cand.id)
    verdicts = cand.data["verdicts"]
    assert verdicts["A"]["verdict"] == "consistent" and verdicts["B"]["verdict"] == "inconsistent"
    assert verdicts["B"]["inconsistencies"][0]["region"] == "left front knee"
    assert cand.data["canon_state"] == "candidate"      # each: the owner decides
    # R-102: B's packet was built without A's verdict and says so; A's verdict was journaled before B's packet
    packets = [p for p in prj.store.list("packet") if p.data["kind"] == "consistency_check"]
    assert len(packets) == 2
    for p in packets:
        text = Path(p.data["dir"], "PACKET.md").read_text(encoding="utf-8")
        assert "matches the anchor" not in text and "toe count" not in text
        assert any("verdict" in w["what"] for w in p.data["withheld"])
        assert any(f["meta"].get("canon_state") == "candidate" and "candidate" in f["meta"].get("label", "") for f in p.data["files"])
    seqs = {e.event: e.seq for e in prj.store.journal() if e.event in ("reference.verdict_recorded",) and e.inputs.get("seat") == "A"}
    b_packet_seq = [e.seq for e in prj.store.journal() if e.event == "packet.built" and e.record_id == packets[1].id][0]
    assert seqs["reference.verdict_recorded"] < b_packet_seq
    assert prj.store.require("generation", front.id).state == "checked"
    # an inconsistency triggers a bounded regeneration request with the inconsistencies handed to the art director
    regen = [g for g in open_requests(concept, "view") if g.data["target"]["view"] == "front"]
    assert len(regen) == 1 and regen[0].data["round"] == 2 and regen[0].data["revises"] == front.id
    ad = [p for p in prj.store.list("packet") if p.data["kind"] == "art_director_revision"][-1]
    assert "three toes" in Path(ad.data["dir"], "PACKET.md").read_text(encoding="utf-8")
    assert any("regenerat" in e for e in events)
    prj.close()


def test_owner_approval_records_verdicts_and_conflict_is_recorded_never_averaged(workdir):
    prj, eng, concept = make(workdir, play=screenplay(verdicts_a=[VERDICT_BAD] * 4, verdicts_b=[VERDICT_OK] * 4))
    anchor = _to_turnaround(workdir, concept)
    front = next(g for g in open_requests(concept, "view") if g.data["target"]["view"] == "front")
    cand, = concept.import_files(front.id, [png(workdir / "front.png", (50, 50, 50))], actor="user:josh")
    concept.advance()
    approved = concept.approve([cand.id], actor="user:josh")[0]
    assert approved.data["approval"]["verdicts"]["A"]["verdict"] == "inconsistent"    # R-98 verdicts recorded with the approval
    conflicts = concept.open_conflicts()
    assert len(conflicts) == 1
    c = conflicts[0]
    assert set(c.data["reference_ids"]) == {cand.id, anchor.id} and c.data["region"] == "left front knee"
    assert c.data["what_differs"] == "three toes instead of two" and c.data["reported_by"] == "A" and c.state == "open"
    cov = concept.coverage()
    assert cov["conflicts_open"] == 1 and cov["views"]["front"]["status"] == "approved"
    # the run asks the owner: neither completion nor proceed is possible while the conflict is open (R-95)
    with pytest.raises(ConceptError):
        concept.proceed(actor="user:josh")
    order = canon_rank([prj.store.require("reference", cand.id), anchor])
    assert [r.id for r in order] == [anchor.id, cand.id]          # precedence, never a blend (R-95)
    concept.reject([cand.id], reason="the anchor wins; regenerate", actor="user:josh")
    c = prj.store.require("evidence_conflict", c.id)
    assert c.state == "resolved_by_user" and "anchor wins" in c.data["resolution"]["reason"]
    assert concept.coverage()["conflicts_open"] == 0
    prj.close()


def test_anchor_only_auto_approves_consistent_views_with_criteria_and_completes(workdir):
    prj, eng, concept = make(workdir, approval="anchor_only")
    anchor = _to_turnaround(workdir, concept)
    assert concept.plan.data["approval_mode"] == "anchor_only"
    for g in open_requests(concept, "view"):
        concept.import_files(g.id, [png(workdir / f"{g.data['target']['view']}.png", (7, 7, 7))], actor="user:josh")
    events = concept.advance()
    approved = [r for r in prj.store.list("reference") if r.data.get("canon_state") == "approved" and r.data.get("kind") == "generated"]
    views = {r.data["labels"][0]: r for r in approved if r.data["precedence_label"] == "turnaround"}
    assert set(views) == {"front", "side"}
    for r in views.values():
        a = r.data["approval"]
        assert a["auto"] is True and a["mode"] == "anchor_only" and a["by"] == "engine"
        assert set(a["verdicts"]) == {"A", "B"} and all(v["verdict"] == "consistent" for v in a["verdicts"].values())
        assert "both LLM seats" in a["criteria"] and "no open conflict" in a["criteria"]
        assert r.data["precedence"] == 2 and r.data["derived_from"] == anchor.id
    assert concept.plan.state == "complete" and concept.coverage()["complete"] is True
    assert any("complete" in e for e in events)
    prj.close()


def test_anchor_only_never_auto_approves_uncertain_or_inconsistent_views(workdir):
    play = screenplay(verdicts_a=[VERDICT_OK, VERDICT_UNSURE] + [VERDICT_OK] * 4, verdicts_b=[VERDICT_BAD, VERDICT_OK] + [VERDICT_OK] * 4)
    prj, eng, concept = make(workdir, approval="anchor_only", max_regen=1)
    eng.adapters["A"].screenplay = {k: list(v) for k, v in play["A"].items()}
    eng.adapters["B"].screenplay = {k: list(v) for k, v in play["B"].items()}
    _to_turnaround(workdir, concept)
    reqs = {g.data["target"]["view"]: g for g in open_requests(concept, "view")}
    concept.import_files(reqs["front"].id, [png(workdir / "f.png", (1, 1, 1))], actor="user:josh")   # A ok, B inconsistent
    concept.import_files(reqs["side"].id, [png(workdir / "s.png", (2, 2, 2))], actor="user:josh")    # A uncertain, B ok
    concept.advance()
    refs = {r.data["labels"][0]: r for r in prj.store.list("reference") if r.data.get("kind") == "generated" and r.data.get("precedence_label") == "turnaround"}
    assert refs["front"].data["canon_state"] == "rejected" and "inconsistent" in refs["front"].data["rejection"]["reason"]
    assert refs["side"].data["canon_state"] == "candidate" and refs["side"].data["verdict_summary"] == "uncertain"
    cov = concept.coverage()
    assert cov["views"]["front"]["status"] == "requested" and cov["views"]["side"]["status"] == "candidates"
    assert any(e["view"] == "side" and "uncertain" in e["reason"] for e in cov["escalations"])   # escalated to the owner
    prj.close()


def test_auto_mode_art_director_picks_anchor_with_criteria_journaled_and_reversible(workdir):
    prj, eng, concept = make(workdir, approval="auto")
    start_text(concept)
    gen = open_requests(concept)[0]
    c1, c2 = concept.import_files(gen.id, [png(workdir / "a.png"), png(workdir / "b.png", (9, 9, 9))], actor="user:josh")
    events = concept.advance()
    c1 = prj.store.require("reference", c1.id)
    assert c1.data["canon_state"] == "approved" and c1.data["approval"]["auto"] is True and c1.data["approval"]["mode"] == "auto"
    assert c1.data["approval"]["criteria"] == ["clearest silhouette", "construction readable"] and c1.data["approval"]["by"] == "engine:art_director:A"
    assert prj.store.require("reference", c2.id).data["canon_state"] == "rejected"
    assert any(e.event == "reference.approved" and e.inputs.get("auto") for e in prj.store.journal())
    assert concept.plan.state == "turnaround_pending"    # canon description and view requests followed in the same advance
    assert any("anchor" in e and "auto" in e for e in events)
    # reversible: the owner rejects the pick; canon returns to anchor_pending and a new anchor round is requested
    concept.reject([c1.id], reason="not the design I want", actor="user:josh")
    assert concept.plan.state == "anchor_pending" and concept.plan.data["anchor_reference_id"] is None
    concept.advance()
    new = open_requests(concept, "anchor_candidate")
    assert len(new) == 1 and new[0].data["round"] == 2
    prj.close()


# --- coverage, proceed, intake guard (R-94, R-103) --------------------------------------------------------------

def test_coverage_and_partial_proceed_are_recorded_and_gate_intake(workdir):
    prj, eng, concept = make(workdir)
    anchor = _to_turnaround(workdir, concept)
    front = next(g for g in open_requests(concept, "view") if g.data["target"]["view"] == "front")
    cand, = concept.import_files(front.id, [png(workdir / "f.png", (3, 3, 3))], actor="user:josh")
    concept.advance()
    concept.approve([cand.id], actor="user:josh")
    cov = concept.coverage()
    assert cov["views"]["front"]["status"] == "approved" and cov["views"]["side"]["status"] == "requested"
    assert cov["missing"] == ["side"] and cov["complete"] is False and concept.plan.state == "turnaround_pending"
    eng.preflight_reports = {"A": {"ok": True, "blockers": []}, "B": {"ok": True, "blockers": []}}
    eng.start(attended=True)
    with pytest.raises(EngineFailure) as exc:
        eng.step()
    assert "concept" in str(exc.value) and "proceed" in str(exc.value)
    plan = concept.proceed(actor="user:josh")
    assert plan.state == "complete" and plan.data["proceeded_partial"]["missing"] == ["side"]
    assert plan.data["proceeded_partial"]["by"] == "user:josh"
    # intake now runs on approved references only (R-94): the anchor and the front view, no candidate, no rejected image
    eng.adapters["A"].screenplay["intake_observation"] = [_observation("A")]
    eng.adapters["B"].screenplay["intake_observation"] = [_observation("B")]
    eng.step()
    packets = [p for p in prj.store.list("packet") if p.data["kind"] == "intake_observation"]
    assert len(packets) == 2
    ids = {f["meta"].get("reference_id") for p in packets for f in p.data["files"] if f["role"] == "reference"}
    assert ids == {anchor.id, cand.id}
    prj.close()


def _observation(agent):
    from builder.schemas import OBSERVATION_SECTIONS

    return {**{s: [{"statement": f"{agent} {s}", "status": "observed", "evidence_refs": ["ref"]}] for s in OBSERVATION_SECTIONS},
            "questions_for_user": []}


def test_from_images_seeds_are_approved_targets_and_first_is_the_anchor(workdir):
    prj, eng, concept = make(workdir, views=("front", "side"))
    a = png(workdir / "seed front ü.png", (5, 5, 5))
    b = png(workdir / "seed side.png", (6, 6, 6))
    plan = concept.start(from_images=[a, b], labels=[["front"], ["side"]], actor="user:josh")
    seeds = [prj.store.require("reference", i) for i in plan.data["seed_reference_ids"]]
    assert all(r.data["canon_state"] == "approved" and r.data["kind"] == "target" and r.data["precedence"] == 0 for r in seeds)
    assert plan.state == "anchor_approved" and plan.data["anchor_reference_id"] == seeds[0].id and plan.data["start_mode"] == "images"
    events = concept.advance()
    assert prj.store.list("canon_description") and concept.plan.state == "complete"    # both needed views are owner-supplied
    assert open_requests(concept) == [] and concept.coverage()["complete"]
    prj.close()


def test_from_images_fills_missing_views_from_the_anchor(workdir):
    prj, eng, concept = make(workdir, views=("front", "rear"))
    a = png(workdir / "seed.png")
    plan = concept.start(from_images=[a], labels=[["front"]], from_text="steampunk variant", actor="user:josh")
    assert plan.data["start_mode"] == "both"
    concept.advance()
    reqs = open_requests(concept, "view")
    assert [g.data["target"]["view"] for g in reqs] == ["rear"]
    assert reqs[0].data["attachments"][0]["reference_id"] == plan.data["anchor_reference_id"]
    prj.close()


# --- studies (R-104) --------------------------------------------------------------------------------------------

def test_study_attaches_to_part_with_lower_precedence_after_both_verdicts(workdir):
    prj, eng, concept = make(workdir, approval="anchor_only")
    anchor = _to_turnaround(workdir, concept)
    prj.store.upsert(Record.new("part", {"name": "leg", "blender_ids": ["p_leg"]}, id="p_leg"), actor="engine", event="part.created")
    study = concept.study("p_leg", view="front", region="knee", purpose="knee joint construction", requested_by="agent:B", actor="engine")
    assert study.state == "requested" and study.data["part_id"] == "p_leg"
    gen = [g for g in open_requests(concept, "study")][0]
    assert gen.data["target"]["study_request_id"] == study.id and gen.data["attachments"][0]["reference_id"] == anchor.id
    cand, = concept.import_files(gen.id, [png(workdir / "knee.png", (8, 8, 8))], actor="user:josh")
    assert prj.store.require("study_request", study.id).state == "generated"
    concept.advance()
    cand = prj.store.require("reference", cand.id)
    assert cand.data["canon_state"] == "approved" and cand.data["part_id"] == "p_leg" and cand.data["precedence"] == 3
    assert cand.data["precedence_label"] == "study" and cand.data["precedence"] > 2   # below the turnaround (R-95)
    assert prj.store.require("study_request", study.id).state == "approved"
    assert set(cand.data["approval"]["verdicts"]) == {"A", "B"}
    with pytest.raises(ConceptError):
        concept.study("p_nope", view="front", purpose="x", requested_by="user", actor="user")
    prj.close()


# --- limits (R-105) ---------------------------------------------------------------------------------------------

def test_image_limits_count_imported_and_generated_alike_and_cost_is_not_applicable(workdir):
    prj, eng, concept = make(workdir, max_images=3, max_regen=1, play=screenplay(verdicts_a=[VERDICT_BAD] * 6))
    start_text(concept)
    gen = open_requests(concept)[0]
    concept.import_files(gen.id, [png(workdir / "1.png"), png(workdir / "2.png")], actor="user:josh")
    st = concept.status()
    assert st["images"] == {"count": 2, "max": 3} and st["cost"]["kind"] == "not_applicable"
    with pytest.raises(ConceptError) as exc:
        concept.import_files(gen.id, [png(workdir / "3.png"), png(workdir / "4.png")], actor="user:josh")
    assert "max_images" in str(exc.value)
    concept.import_files(gen.id, [png(workdir / "3.png", (2, 2, 2))], actor="user:josh")
    assert concept.status()["images"]["count"] == 3
    refs = [r for r in prj.store.list("reference") if r.data.get("generation_id") == gen.id]
    concept.approve([refs[0].id], actor="user:josh")
    with pytest.raises(ConceptError) as exc:
        concept.advance()          # the view requests would exceed max_images: refused, nothing dispatched
    assert "max_images" in str(exc.value)
    assert open_requests(concept, "view") == []
    prj.close()


def test_regenerations_per_view_are_bounded_then_escalated(workdir):
    prj, eng, concept = make(workdir, max_regen=1, play=screenplay(verdicts_a=[VERDICT_BAD] * 6))
    _to_turnaround(workdir, concept)
    front = next(g for g in open_requests(concept, "view") if g.data["target"]["view"] == "front")
    concept.import_files(front.id, [png(workdir / "f1.png")], actor="user:josh")
    concept.advance()
    regen = [g for g in open_requests(concept, "view") if g.data["target"]["view"] == "front"]
    assert len(regen) == 1 and regen[0].data["round"] == 2
    concept.import_files(regen[0].id, [png(workdir / "f2.png", (4, 4, 4))], actor="user:josh")
    concept.advance()
    assert [g for g in open_requests(concept, "view") if g.data["target"]["view"] == "front"] == []
    esc = concept.coverage()["escalations"]
    assert any(e["view"] == "front" and "max_regenerations_per_view" in e["reason"] for e in esc)
    with pytest.raises(ConceptError):
        concept.regenerate(regen[0].id, note="try again", actor="user:josh")   # the cap holds for the owner too
    prj.close()


def test_regenerate_writes_a_new_request_with_the_note(workdir):
    prj, eng, concept = make(workdir)
    _to_turnaround(workdir, concept)
    front = next(g for g in open_requests(concept, "view") if g.data["target"]["view"] == "front")
    new = concept.regenerate(front.id, note="make the legs longer", actor="user:josh")
    assert new.id != front.id and new.data["round"] == 2 and new.data["revises"] == front.id and new.state == "open"
    assert prj.store.require("generation", front.id).state == "abandoned"
    ad = [p for p in prj.store.list("packet") if p.data["kind"] == "art_director_revision"][-1]
    assert "legs longer" in Path(ad.data["dir"], "PACKET.md").read_text(encoding="utf-8")
    assert Path(new.data["prompt_file"]).is_file()
    prj.close()


# --- roles (R-107, R-108) ---------------------------------------------------------------------------------------

def test_art_director_rotates_after_two_rejected_rounds(workdir):
    prj, eng, concept = make(workdir)
    start_text(concept)
    for n in range(2):
        gen = open_requests(concept, "anchor_candidate")[0]
        ref, = concept.import_files(gen.id, [png(workdir / f"r{n}.png", (n, n, n))], actor="user:josh")
        concept.reject([ref.id], reason="wrong direction", actor="user:josh")
        assert prj.store.require("generation", gen.id).state == "rejected"
        concept.advance()
    assert concept.rejected_rounds() == 2
    gen3 = open_requests(concept, "anchor_candidate")[0]
    assert gen3.data["art_director"] == "B" and "two rejected" in gen3.data["art_director_rationale"]
    assert gen3.data["prompt"] == "PROMPT-TEXT-FROM-B"
    prj.close()


def test_consistency_seats_are_both_llm_seats_never_the_image_seat():
    seats = consistency_seats(["A", "B"], image_seat="I")
    assert [s.seat for s in seats] == ["A", "B"] and all("R-102" in s.rationale for s in seats)
    with pytest.raises(ValueError):
        consistency_seats(["A"], image_seat="I")
