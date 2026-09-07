"""R-110 concept verbs through the CLI with a JSON screenplay for the LLM seats and prepared PNGs for the image seat
(layer D, CLI interaction): prompts printed with request ids and attachment paths, imports with declarations,
approval mode shown on every command, list/show, regenerate writes a new request, proceed, watch-import, and the
hand-off to intake with approved references only."""
from __future__ import annotations

import io
import json
import re
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from builder import cli  # noqa: E402
from builder.project import Project  # noqa: E402

from _fake_runner import FakeRunner  # noqa: E402
from _screenplay import PARTS, screenplay as modeling_screenplay  # noqa: E402
from test_cli import _seed_revision  # noqa: E402
from test_concept import AD_PROMPT, CANON, PICK, VERDICT_OK, png  # noqa: E402


def run_cli(*argv, runner=None):
    out = io.StringIO()
    code = cli.main([str(a) for a in argv], runner_factory=(lambda cfg, project: runner or FakeRunner()), stdout=out)
    return code, out.getvalue()


@pytest.fixture
def play(workdir):
    base = modeling_screenplay()
    for label in ("A", "B"):
        base[label].update({"art_director_prompt": [AD_PROMPT] * 6, "canon_description": [CANON] * 2,
                            "consistency_verdict": [VERDICT_OK] * 6, "anchor_pick": [PICK] * 2})
    p = workdir / "play ü.json"
    p.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def cfg(workdir):
    p = workdir / "config.yaml"
    p.write_text("default_ai: claude\nais: {}\nbuilder:\n  concept: {approval: each, views: [front, side], max_images: 20}\n"
                 "  image_generation: {seat: manual, vendor: chatgpt}\n", encoding="utf-8")
    return p


def _ids(text, prefix):
    return list(dict.fromkeys(re.findall(rf"\b{prefix}_[0-9A-Z]+\b", text)))


def test_concept_verbs_end_to_end_with_mocks(workdir, play, cfg):
    wf = workdir / "wf ü"
    runner = FakeRunner()
    runner.identity_map = [{"alloy_id": p, "type": "OBJECT"} for p in PARTS]
    assert run_cli("new", "--workflow-dir", wf, "--name", "Drone ü", "--first-component", "Hull")[0] == 0
    _seed_revision(wf, runner)

    code, out = run_cli("concept", "start", wf, "--from-text", "a hexapod maintenance drone", "--mock", play, "--config", cfg, runner=runner)
    assert code == 0, out
    assert "approval mode each" in out and "owner approves every generated image" in out
    assert "art director A" in out and _ids(out, "gen"), out
    gen = _ids(out, "gen")[0]
    assert "PROMPT.md" in out

    code, out = run_cli("concept", "prompts", wf, "--config", cfg)
    assert code == 0 and gen in out and AD_PROMPT["prompt"] in out and "attach" in out.lower() and "concept import" in out
    assert "approval mode each" in out

    a = png(workdir / "gen" / "anchor 1 ü.png", (1, 2, 3))
    b = png(workdir / "gen" / "anchor 2.png", (4, 5, 6))
    code, out = run_cli("concept", "import", wf, gen, a, b, "--vendor", "chatgpt", "--model", "GPT Image (as shown)", "--mock", play,
                        "--config", cfg, runner=runner)
    assert code == 0, out
    cands = _ids(out, "ref")
    assert len(cands) == 2 and "candidate" in out and "declared" in out and "approval mode each" in out

    code, out = run_cli("concept", "list", wf, "--config", cfg)
    assert code == 0 and "anchor_pending" in out and cands[0] in out and "front" in out and "side" in out

    code, out = run_cli("concept", "approve", wf, cands[0], "--user", "josh", "--mock", play, "--config", cfg, runner=runner)
    assert code == 0, out
    assert "approved" in out and "canon description" in out and "front" in out and "side" in out
    assert "anchor_approved" in out or "turnaround_pending" in out

    code, out = run_cli("concept", "prompts", wf, "--config", cfg)
    reqs = _ids(out, "gen")
    assert len(reqs) == 2 and "attach" in out.lower() and cands[0] in out   # views are conditioned on the anchor (R-101)
    with Project.open(wf) as prj:
        by_view = {g.data["target"]["view"]: g.id for g in prj.store.list("generation") if g.state == "open"}
    front, side = by_view["front"], by_view["side"]

    f = png(workdir / "gen" / "front.png", (7, 7, 7))
    code, out = run_cli("concept", "import", wf, front, f, "--mock", play, "--config", cfg, runner=runner)
    assert code == 0, out
    fref = _ids(out, "ref")[0]
    assert "A=consistent" in out and "B=consistent" in out and "awaiting the owner" in out

    code, out = run_cli("concept", "show", wf, fref, "--config", cfg)
    assert code == 0 and '"verdicts"' in out and '"candidate"' in out and "approval mode each" in out

    code, out = run_cli("concept", "approve", wf, fref, "--user", "josh", "--mock", play, "--config", cfg, runner=runner)
    assert code == 0 and "waiting for the owner" in out and "side" in out

    code, out = run_cli("concept", "regenerate", wf, side, "--note", "longer legs", "--mock", play, "--config", cfg, runner=runner)
    assert code == 0, out
    new_side = [g for g in _ids(out, "gen") if g != side]
    assert new_side and "round 2" in out

    code, out = run_cli("concept", "reject", wf, fref, "--config", cfg)
    assert code == 2   # --reason is required

    code, out = run_cli("concept", "proceed", wf, "--user", "josh", "--config", cfg)
    assert code == 0 and "complete" in out and "side" in out and "partial" in out

    code, out = run_cli("status", wf, "--json", "--config", cfg)
    status = json.loads(out)
    assert status["concept"]["canon_state"] == "complete" and status["concept"]["proceeded_partial"]["missing"] == ["side"]
    assert status["concept"]["mode"] == "each"

    code, out = run_cli("preflight", wf, "--mock", play, "--config", cfg, runner=runner)
    assert code == 0 and "image seat I" in out and "not_applicable" in out and "flow" in out

    code, out = run_cli("start", wf, "--mock", play, "--max-steps", "2", "--config", cfg, runner=runner)
    assert "[stage] intake" in out and "[stage] brief" in out, out
    with Project.open(wf) as prj:
        packets = [p for p in prj.store.list("packet") if p.data["kind"] == "intake_observation"]
        ids = {f["meta"].get("reference_id") for p in packets for f in p.data["files"] if f["role"] == "reference"}
        assert ids == {cands[0], fref}      # approved canon only (R-94)


def test_import_as_anchor_and_watch_folder(workdir, play, cfg):
    wf = workdir / "wf"
    assert run_cli("new", "--workflow-dir", wf, "--name", "Drone")[0] == 0
    code, out = run_cli("concept", "start", wf, "--from-text", "drone", "--mock", play, "--config", cfg)
    assert code == 0
    with Project.open(wf) as prj:
        gen = [g for g in prj.store.list("generation") if g.state == "open"][0].id
    a = png(workdir / "own anchor.png", (9, 9, 9))
    code, out = run_cli("concept", "import", wf, a, "--as-anchor", "--vendor", "gemini", "--no-check", "--mock", play, "--config", cfg)
    assert code == 0, out
    assert "created by the owner" in out or "owner" in out
    ref = _ids(out, "ref")[0]
    code, out = run_cli("concept", "approve", wf, ref, "--user", "josh", "--mock", play, "--config", cfg)
    assert code == 0, out
    with Project.open(wf) as prj:
        front = [g for g in prj.store.list("generation") if g.state == "open" and g.data["target"].get("view") == "front"][0].id
    folder = workdir / "drop ü"
    folder.mkdir()
    result = {}

    def worker():
        result["ret"] = run_cli("concept", "import", wf, front, "--watch", folder, "--watch-timeout", "4", "--watch-max", "1",
                                "--no-check", "--mock", play, "--config", cfg)

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.5)
    png(folder / "front from app.png", (3, 3, 3))
    t.join(timeout=10)
    code, out = result["ret"]
    assert code == 0, out
    assert "watch" in out and "front from app.png" in out and _ids(out, "ref")
    with Project.open(wf) as prj:
        gen_rec = prj.store.require("generation", front)
        assert gen_rec.state == "imported" and gen_rec.data["outputs"][0]["original_path"].endswith("front from app.png")


def test_concept_help_lists_every_r110_verb():
    out = io.StringIO()
    cli.main(["concept", "--help"], stdout=out)
    text = out.getvalue()
    for verb in ("start", "prompts", "import", "list", "show", "approve", "reject", "regenerate", "study", "proceed"):
        assert verb in text, verb
