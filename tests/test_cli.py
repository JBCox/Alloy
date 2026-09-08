"""R-90 CLI entry: create/open, intake, presets, preflight, start, pause/resume/cancel, findings, accept/reopen,
checkpoints, status, all through the engine (layer D with a fake runner and mock adapters)."""
from __future__ import annotations

import builtins
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from builder import cli
from builder.project import Project
from builder.store import Store

from _fake_runner import FakeRunner
from _screenplay import PARTS, write_screenplay


def run_cli(*argv, runner=None):
    out = io.StringIO()
    code = cli.main([str(a) for a in argv], runner_factory=(lambda cfg, project: runner or FakeRunner()), stdout=out)
    return code, out.getvalue()


@pytest.fixture
def refs(workdir):
    paths = []
    for label in ("front", "side", "three_quarter"):
        p = workdir / f"ref {label} ü.png"
        Image.new("RGB", (100, 80), (70, 70, 70)).save(p)
        paths.append(p)
    return paths


@pytest.fixture
def config_yaml(workdir):
    p = workdir / "config.yaml"
    p.write_text(
        "default_ai: claude\nais: {}\nbuilder:\n  presets:\n    e10-bearer-head:\n      name: E10 Bearer - Head\n"
        f"      project_dir: '{(workdir / 'art proj').as_posix()}'\n      asset: E10 Bearer\n"
        f"      target_reference: '{(workdir / 'art proj' / 'art' / 'concept.png').as_posix()}'\n"
        "      target_region: {name: lower-left humanoid, bbox: null}\n"
        f"      existing_source: '{(workdir / 'art proj' / 'art' / 'model.blend').as_posix()}'\n"
        "      first_component: Head\n", encoding="utf-8")
    return p


def _seed_revision(wf: Path, runner: FakeRunner):
    """Phase 1 fixture stand-in: register revision 0 and the part inventory directly."""
    from builder.operations import Operations
    from builder.ownership import OwnershipManager
    from builder.records import Record

    with Project.open(wf) as prj:
        seed = wf / "staging" / "seed.blend"
        seed.write_bytes(b"BLENDER-fake-seed")
        ops = Operations(prj, runner, OwnershipManager(prj.store))
        ops.register_revision(seed, parent_revision_id=None, created_by_op_id=None, actor="engine",
                              identity_map=[{"alloy_id": p, "type": "OBJECT"} for p in PARTS], note="seed")
        for p in PARTS:
            prj.store.upsert(Record.new("part", {"name": p, "blender_ids": [p]}, id=p), actor="engine", event="part.created")


def test_new_intake_status(workdir, refs):
    wf = workdir / "wf ü"
    code, out = run_cli("new", "--workflow-dir", wf, "--name", "Lamp ü", "--asset", "Lamp", "--first-component", "Head")
    assert code == 0 and "created" in out and (wf / "project.json").is_file()
    code, out = run_cli("intake", wf, "--ref", refs[0], "--labels", "front", "--ref", refs[1], "--labels", "side")
    assert code == 0 and "2 reference" in out
    code, out = run_cli("status", wf, "--json")
    status = json.loads(out)
    assert status["execution"] == "idle" and status["references"] == 2 and status["project"]["name"] == "Lamp ü"
    code, out = run_cli("status", workdir / "nope")
    assert code == 2 and "no workflow project" in out


def test_preset_list_show_create_never_open_sources(workdir, config_yaml, monkeypatch):
    code, out = run_cli("preset", "list", "--config", config_yaml)
    assert code == 0 and "e10-bearer-head" in out
    code, out = run_cli("preset", "show", "e10-bearer-head", "--config", config_yaml)
    assert code == 0 and "concept.png" in out
    real_open = builtins.open

    def guarded(file, *a, **kw):
        if "art" in str(file).replace("\\", "/").split("/"):
            raise AssertionError(f"opened a source file: {file}")
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", guarded)
    wf = workdir / "wf preset"
    code, out = run_cli("preset", "create", "e10-bearer-head", "--config", config_yaml, "--workflow-dir", wf)
    assert code == 0 and (wf / "project.json").is_file()
    assert not (workdir / "art proj").exists()
    with Project.open(wf) as prj:
        assert prj.record.data["preset_id"] == "e10-bearer-head"
        assert prj.record.data["target_reference"]["region"]["bbox"] is None


def test_full_cli_flow_with_mocks(workdir, refs):
    wf = workdir / "wf"
    runner = FakeRunner()
    runner.identity_map = [{"alloy_id": p, "type": "OBJECT"} for p in PARTS]
    assert run_cli("new", "--workflow-dir", wf, "--name", "Lamp", "--asset", "Lamp", "--first-component", "Head")[0] == 0
    assert run_cli("intake", wf, "--ref", refs[0], "--labels", "front", "--ref", refs[1], "--labels", "side")[0] == 0
    _seed_revision(wf, runner)
    play = write_screenplay(workdir / "play.json")

    code, out = run_cli("preflight", wf, "--live", "--mock", play, runner=runner)
    assert code == 0 and "image_reading" in out and "verified" in out

    code, out = run_cli("start", wf, "--mock", play, "--max-steps", "60", runner=runner)
    assert code == 0, out
    assert "ready_for_user_review" in out and "not accepted" in out

    code, out = run_cli("status", wf, "--json")
    status = json.loads(out)
    assert status["execution"] == "waiting_for_user" and status["stop_reason"] == "ready_for_user_review"
    comp_id = status["components"][0]["id"]
    assert status["components"][0]["acceptance_state"] == "unaccepted"
    assert status["contributions"]["A"] >= 1 and status["contributions"]["B"] >= 1

    code, out = run_cli("findings", wf)
    assert code == 0 and "p_bracket" in out and "closed" in out
    code, out = run_cli("findings", wf, "--open")
    assert code == 0 and "p_bracket" not in out

    code, out = run_cli("accept", wf, comp_id, "--user", "josh")
    assert code == 0 and "accepted_at_revision" in out
    code, out = run_cli("reopen", wf, comp_id, "--reason", "rear seam", "--user", "josh")
    assert code == 0 and "unreviewed" in out

    code, out = run_cli("checkpoint", "list", wf)
    assert code == 0 and "base before" in out
    ck_id = [line.split()[0] for line in out.splitlines() if line.startswith("ck_")][0]
    code, out = run_cli("checkpoint", "restore", wf, ck_id, "--user", "josh")
    assert code == 0 and "rev_" in out

    code, out = run_cli("journal", wf, "--tail", "5")
    assert code == 0 and "checkpoint.restored" in out

    code, out = run_cli("pause", wf)
    assert code == 0
    code, out = run_cli("feedback", wf, "make the cap rounder", "--user", "josh")
    assert code == 0
    with Store(wf / "builder.sqlite3") as store:
        kinds = [c.kind for c in store.pop_controls()]
    assert kinds == ["pause", "feedback"]


def test_start_with_real_agents_never_runs_live_preflight_implicitly(workdir, refs):
    """Real adapters spend money: `start` refuses to probe them on its own and points at `preflight --live` (R-18)."""
    wf = workdir / "wf"
    cfg = workdir / "config.yaml"
    cfg.write_text("default_ai: claude\nais: {}\nbuilder:\n  agents:\n"
                   "    A: {provider: claude, model: fable, reasoning: max}\n"
                   "    B: {provider: codex, model: gpt-6-astra, reasoning: xhigh}\n", encoding="utf-8")
    assert run_cli("new", "--workflow-dir", wf, "--name", "Lamp", "--asset", "Lamp")[0] == 0
    code, out = run_cli("start", wf, "--config", cfg)
    assert code == 2 and "preflight" in out and "--live" in out and "spends" in out
    # no run was created and no provider process was started (the conftest guard would have raised)
    with Project.open(wf) as prj:
        assert prj.store.list("run") == [] and prj.store.list("invocation") == []


def test_live_preflight_report_is_reused_by_start_and_downgrade_is_printed(workdir, refs, monkeypatch):
    """Real-adapter path through the fake CLI: `preflight --live` records a report keyed on CLI path, version, model
    and settings (R-18); `start` reuses it without probing again; a codex reasoning downgrade is printed, never silent."""
    import sys

    from builder.providers.claude import ClaudeAdapter
    from builder.providers.codex import CodexAdapter

    fake = Path(__file__).with_name("_fake_cli.py")
    record = workdir / "calls.jsonl"
    monkeypatch.setenv("ALLOY_FAKE_CLI_RECORD", str(record))
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "reject_xhigh")   # the fake answers every packet from its schema.json
    made = {}

    def factory(cfg, args):
        if not made:
            made["A"] = ClaudeAdapter(argv_head=[sys.executable, str(fake), "claude"])
            made["B"] = CodexAdapter(argv_head=[sys.executable, str(fake), "codex"])
        return dict(made)

    def run(*argv):
        out = io.StringIO()
        code = cli.main([str(a) for a in argv], runner_factory=lambda cfg, project: runner, adapters_factory=factory, stdout=out)
        return code, out.getvalue()

    wf = workdir / "wf"
    runner = FakeRunner()
    runner.identity_map = [{"alloy_id": p, "type": "OBJECT"} for p in PARTS]
    cfg = workdir / "config.yaml"
    cfg.write_text("default_ai: claude\nais: {}\nbuilder:\n  provider_timeouts: {cancel_probe_after: 1}\n  agents:\n"
                   "    A: {provider: claude, model: fable, reasoning: max}\n"
                   "    B: {provider: codex, model: gpt-6-astra, reasoning: xhigh}\n", encoding="utf-8")
    assert run("new", "--workflow-dir", wf, "--name", "Lamp", "--asset", "Lamp")[0] == 0
    assert run("intake", wf, "--ref", refs[0], "--labels", "front")[0] == 0
    _seed_revision(wf, runner)
    code, out = run("preflight", wf, "--live", "--config", cfg)
    assert "session_resume" in out and "cancellation" in out, out
    assert "REASONING DOWNGRADE" in out and "xhigh" in out and "reasoning_effective=high" in out, out
    # the session probe made a nonce round trip; the fake's session ids were the UUIDs Alloy chose (claude) or reported (codex)
    calls = [json.loads(l) for l in record.read_text(encoding="utf-8").splitlines() if l.strip()]
    n_preflight = len(calls)
    assert n_preflight >= 8
    assert any("--resume" in c["argv"] for c in calls) and any("resume" in c["argv"] for c in calls)
    # start reuses the stored report: no new probe calls before the first intake invocation
    code, out = run("start", wf, "--config", cfg, "--max-steps", "3")
    assert "running preflight" not in out and "no current live preflight" not in out, out
    calls_after = [json.loads(l) for l in record.read_text(encoding="utf-8").splitlines() if l.strip()]
    new_calls = calls_after[n_preflight:]
    assert new_calls and all("probe" not in (c["stdin"] or "") for c in new_calls)
    assert any('model_reasoning_effort="high"' in c["argv"] for c in new_calls)   # the accepted level is reused at run time
    assert not any('model_reasoning_effort="xhigh"' in c["argv"] for c in new_calls)


def test_unknown_provider_is_refused_not_substituted(workdir, refs):
    wf = workdir / "wf"
    cfg = workdir / "config.yaml"
    cfg.write_text("default_ai: claude\nais: {}\nbuilder:\n  agents:\n    A: {provider: copilot}\n", encoding="utf-8")
    assert run_cli("new", "--workflow-dir", wf, "--name", "Lamp", "--asset", "Lamp")[0] == 0
    code, out = run_cli("start", wf, "--config", cfg)
    assert code == 2 and "copilot" in out and "claude, gemini, codex" in out


def test_help_lists_every_r90_verb():
    out = io.StringIO()
    code = cli.main(["--help"], stdout=out)
    text = out.getvalue()
    for verb in ("new", "open", "preflight", "start", "pause", "resume", "cancel", "findings", "accept", "reopen",
                 "checkpoint", "status", "intake", "preset", "journal"):
        assert verb in text, verb


def test_limits_verb_shows_and_edits_limits_on_a_stopped_run(workdir, refs):
    wf = workdir / "wf"
    runner = FakeRunner()
    runner.identity_map = [{"alloy_id": p, "type": "OBJECT"} for p in PARTS]
    assert run_cli("new", "--workflow-dir", wf, "--name", "Lamp", "--asset", "Lamp", "--first-component", "Head")[0] == 0
    assert run_cli("intake", wf, "--ref", refs[0], "--labels", "front")[0] == 0
    _seed_revision(wf, runner)
    play = write_screenplay(workdir / "play.json")
    cfg = workdir / "limits.yaml"
    cfg.write_text("builder:\n  limits: {max_requests: 2}\n", encoding="utf-8")
    assert run_cli("preflight", wf, "--live", "--mock", play, "--config", cfg, runner=runner)[0] == 0
    code, out = run_cli("start", wf, "--mock", play, "--max-steps", "60", "--config", cfg, runner=runner)
    assert code == 1 and "budget_limit" in out
    code, out = run_cli("limits", wf, "--config", cfg)
    assert code == 0 and "max_requests = 2" in out and "steps_without_progress" in out
    code, out = run_cli("limits", wf, "--set", "max_requests=0", "--set", "stall_steps=30", "--config", cfg)
    assert code == 0 and "max_requests = 0" in out and "applied" in out
    code, out = run_cli("limits", wf, "--set", "max_tokens=1", "--config", cfg)
    assert code == 2 and "unknown limit" in out
    code, out = run_cli("resume", wf, "--mock", play, "--max-steps", "60", "--config", cfg, runner=runner)
    assert code == 0 and "ready_for_user_review" in out, out


def test_local_only_preflight_report_never_satisfies_start_with_real_adapters(workdir, refs, monkeypatch):
    """R-18/R-21: a report whose live tiers were never run is not a live preflight; `start` with real adapters must
    still point at `preflight --live` (found while writing the docs: the cache-key match alone accepted it)."""
    import sys

    from builder.providers.claude import ClaudeAdapter
    from builder.providers.codex import CodexAdapter

    fake = Path(__file__).with_name("_fake_cli.py")
    monkeypatch.setenv("ALLOY_FAKE_CLI_RECORD", str(workdir / "calls.jsonl"))
    made = {}

    def factory(cfg, args):
        if not made:
            made["A"] = ClaudeAdapter(argv_head=[sys.executable, str(fake), "claude"])
            made["B"] = CodexAdapter(argv_head=[sys.executable, str(fake), "codex"])
        return dict(made)

    def run(*argv):
        out = io.StringIO()
        code = cli.main([str(a) for a in argv], runner_factory=lambda cfg, project: runner, adapters_factory=factory, stdout=out)
        return code, out.getvalue()

    wf = workdir / "wf"
    runner = FakeRunner()
    runner.identity_map = [{"alloy_id": p, "type": "OBJECT"} for p in PARTS]
    cfg = workdir / "config.yaml"
    cfg.write_text("default_ai: claude\nais: {}\nbuilder:\n  agents:\n"
                   "    A: {provider: claude, model: fable, reasoning: max}\n"
                   "    B: {provider: codex, model: gpt-6-astra, reasoning: xhigh}\n", encoding="utf-8")
    assert run("new", "--workflow-dir", wf, "--name", "Lamp", "--asset", "Lamp")[0] == 0
    assert run("intake", wf, "--ref", refs[0], "--labels", "front")[0] == 0
    _seed_revision(wf, runner)
    code, out = run("preflight", wf, "--config", cfg)                 # local tiers only: free
    assert code == 0 and "not_run" in out
    code, out = run("start", wf, "--config", cfg)
    assert code == 2 and "--live" in out and "spends" in out
    with Project.open(wf) as prj:
        assert prj.store.list("run") == []


def test_interrupt_handler_pauses_first_and_cancels_second():
    """Design 9.1: Ctrl+C requests a pause at the next safe boundary; a second Ctrl+C requests cancel."""
    from builder.cli import interrupt_handler

    class Eng:
        def __init__(self):
            self.paused = 0
            import threading
            self.cancel_event = threading.Event()

        def pause(self, user="user"):
            self.paused += 1

    said = []
    eng = Eng()
    handler = interrupt_handler(eng, said.append)
    handler(2, None)
    assert eng.paused == 1 and not eng.cancel_event.is_set() and "pause" in said[-1]
    handler(2, None)
    assert eng.cancel_event.is_set() and "cancel" in said[-1]
