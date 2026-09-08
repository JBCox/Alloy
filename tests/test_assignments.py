"""Assignment overrides from the CLI (`start --assign build=B --assign plan=A`) and from the `builder.assignments`
config key (layer D, fake runner, scripted seats): stored on the run record as ``assignment_overrides`` with their
source, honoured by the scheduler with a "user override" rationale, shown with the rationale by ``status`` and in
the window's Stage read model, and never able to make a seat review, verify, or reassess its own operation
(R-8, R-107: ``roles.assign`` raises ``RoleConflict`` and the run stops with the reason)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from builder import cli
from builder.config import BuilderConfig
from builder.project import Project

from _fake_runner import FakeRunner
from _screenplay import PARTS, screenplay
from test_cli import _seed_revision


def swapped_screenplay(path: Path) -> Path:
    """The fixture screenplay with the seats' duties swapped: B builds and verifies, A reviews, reconciles, corrects.
    Mocks never improvise, so an override that makes B build needs B's build script."""
    play = screenplay()
    a, b = play["A"], play["B"]
    for key in ("build_task", "verification"):
        b[key] = a.pop(key)
    for key in ("review_task", "review_reconcile", "correction_task"):
        a[key] = b.pop(key)
    path.write_text(json.dumps(play, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def run_cli(*argv, runner=None):
    out = io.StringIO()
    code = cli.main([str(a) for a in argv], runner_factory=(lambda cfg, project: runner), stdout=out)
    return code, out.getvalue()


@pytest.fixture
def ready(workdir):
    """A project with references, revision 0, and a live (mock) preflight report."""
    wf = workdir / "wf ü"
    runner = FakeRunner()
    runner.identity_map = [{"alloy_id": p, "type": "OBJECT"} for p in PARTS]
    ref = workdir / "front ü.png"
    Image.new("RGB", (100, 80), (70, 70, 70)).save(ref)
    assert run_cli("new", "--workflow-dir", wf, "--name", "Lamp", "--asset", "Lamp", "--first-component", "Head", runner=runner)[0] == 0
    assert run_cli("intake", wf, "--ref", ref, "--labels", "front", runner=runner)[0] == 0
    _seed_revision(wf, runner)
    play = swapped_screenplay(workdir / "play.json")
    assert run_cli("preflight", wf, "--live", "--mock", play, runner=runner)[0] == 0
    return wf, runner, play


def _tasks(wf: Path):
    with Project.open(wf) as prj:
        agents = {a.id: a.data["label"] for a in prj.store.list("agent")}
        return [(t.data["kind"], agents.get(t.data["owner_agent_id"]), t.data.get("rationale")) for t in prj.store.list("task")]


def _run(wf: Path):
    with Project.open(wf) as prj:
        runs = prj.store.list("run")
        return dict(id=runs[-1].id, state=runs[-1].state, **runs[-1].data) if runs else None


# --- config key --------------------------------------------------------------------------------------------

def test_config_assignments_key_is_parsed_leniently_and_round_trips():
    cfg = BuilderConfig.from_dict({"builder": {"assignments": {"build": "B", "plan": "A", "bogus": "A", "reviewer": "Z"}}})
    assert cfg.assignments == {"build": "B", "plan": "A"}
    joined = "\n".join(cfg.warnings)
    assert "assignments.bogus" in joined and "assignments.reviewer" in joined and "Z" in joined
    assert cfg.to_dict()["assignments"] == {"build": "B", "plan": "A"}
    assert BuilderConfig.from_dict({"builder": {"assignments": "build=B"}}).assignments == {}
    assert BuilderConfig.from_dict({}).assignments == {}


# --- CLI ---------------------------------------------------------------------------------------------------

def test_start_assign_makes_b_build_and_a_review_with_the_override_and_rationale_in_status(ready):
    wf, runner, play = ready
    code, out = run_cli("start", wf, "--mock", play, "--max-steps", "60", "--assign", "build=B", "--assign", "plan=A", runner=runner)
    assert code == 0, out
    run = _run(wf)
    assert run["assignment_overrides"] == {"build": "B", "plan": "A"}
    assert run["assignment_override_sources"] == {"build": "cli", "plan": "cli"}
    tasks = _tasks(wf)
    builds = [t for t in tasks if t[0] == "build"]
    reviews = [t for t in tasks if t[0] == "review"]
    assert builds and all(owner == "B" for _, owner, _ in builds)
    assert all("user override" in rationale and "build=B" in rationale for _, _, rationale in builds)
    assert reviews and all(owner == "A" for _, owner, _ in reviews)              # R-107: B never reviews its own build
    assert run["plan"]["by"] == "A" and "user override" in run["plan"]["rationale"]
    code, out = run_cli("status", wf, runner=runner)
    assert code == 0
    assert "assignment override build=B (cli)" in out and "user override" in out and "R-107" in out
    code, out = run_cli("status", wf, "--json", runner=runner)
    st = json.loads(out)
    roles = {o["role"]: o for o in st["assignment_overrides"]}
    assert roles["build"]["seat"] == "B" and roles["build"]["source"] == "cli" and "R-107" in roles["build"]["rationale"]


def test_start_assign_that_would_make_a_seat_review_its_own_work_stops_with_the_r107_reason(ready):
    wf, runner, play = ready
    code, out = run_cli("start", wf, "--mock", play, "--max-steps", "60", "--assign", "build=B", "--assign", "reviewer=B", runner=runner)
    assert code == 1, out
    run = _run(wf)
    assert run["stop_reason"] == "execution_failure"
    note = run["notes"][-1]["note"]
    assert "R-107" in note and "reviewer" in note and "B" in note
    assert "R-107" in out
    tasks = _tasks(wf)
    assert not any(kind == "review" for kind, _, _ in tasks)                   # nothing reviewed by the author


def test_start_assign_rejects_unknown_roles_and_seats_before_creating_a_run(ready):
    wf, runner, play = ready
    code, out = run_cli("start", wf, "--mock", play, "--assign", "sculptor=B", runner=runner)
    assert code == 2 and "sculptor" in out and "build" in out and "reviewer" in out
    code, out = run_cli("start", wf, "--mock", play, "--assign", "build=C", runner=runner)
    assert code == 2 and "C" in out and "A" in out and "B" in out
    code, out = run_cli("start", wf, "--mock", play, "--assign", "build", runner=runner)
    assert code == 2 and "role=SEAT" in out
    assert _run(wf) is None


def test_config_assignments_apply_when_start_has_no_assign_and_cli_wins_over_config(ready, workdir):
    wf, runner, play = ready
    cfg = workdir / "config.yaml"
    cfg.write_text("default_ai: claude\nais: {}\nbuilder:\n  assignments: {build: B}\n", encoding="utf-8")
    code, out = run_cli("start", wf, "--mock", play, "--max-steps", "60", "--config", cfg, runner=runner)
    assert code == 0, out
    run = _run(wf)
    assert run["assignment_overrides"] == {"build": "B"} and run["assignment_override_sources"] == {"build": "config"}
    assert all(owner == "B" for kind, owner, _ in _tasks(wf) if kind == "build")
    code, out = run_cli("status", wf, runner=runner)
    assert "assignment override build=B (config)" in out
    # a second run with an explicit --assign overrides the config value and records the source
    code, out = run_cli("start", wf, "--mock", play, "--max-steps", "1", "--config", cfg, "--assign", "build=A", runner=runner)
    run = _run(wf)
    assert run["assignment_overrides"] == {"build": "A"} and run["assignment_override_sources"] == {"build": "cli"}


# --- read model for the window's Stage card -------------------------------------------------------------------

def test_snapshot_stage_lists_the_overrides_with_rationale(ready):
    from builder.engine import Engine
    from builder.viewmodel import snapshot

    wf, runner, play = ready
    assert run_cli("start", wf, "--mock", play, "--max-steps", "60", "--assign", "build=B", runner=runner)[0] == 0
    with Project.open(wf) as prj:
        eng = Engine(prj, BuilderConfig.from_dict({}), adapters={}, runner=None)
        eng.attach()
        st = snapshot(eng)["stage"]
    assert st["assignment_overrides"] == [{"role": "build", "seat": "B", "source": "cli",
                                           "rationale": st["assignment_overrides"][0]["rationale"]}]
    assert "user override" in st["assignment_overrides"][0]["rationale"] and "R-107" in st["assignment_overrides"][0]["rationale"]
