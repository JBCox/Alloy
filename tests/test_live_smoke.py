"""Layer L: bounded live smoke test on the fixture with the real agents (spec Section 6, R-10, R-16, R-17, R-25).

Opt-in only. Skipped unless ``ALLOY_LIVE=1`` is set, which the owner does after approving scope and budget in
chat. Spends provider usage. Needs Blender (fixture) and the configured CLIs, authenticated.

Caps enforced by Alloy: request count, render count, wall clock, attempts per finding. A monetary cap is
enforceable only for claude (``--max-budget-usd`` per invocation, remaining budget); codex reports no cost, so
its consumption is recorded as unknown and never counted as zero.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from builder.config import BuilderConfig
from builder.engine import Engine
from builder.fixture import create_fixture
from builder.providers import make_adapter

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("ALLOY_LIVE") != "1",
                                 reason="live provider test: opt-in with ALLOY_LIVE=1 after owner approval of scope and budget (spends money)")]

MAX_REQUESTS = int(os.environ.get("ALLOY_LIVE_MAX_REQUESTS", "16"))
MAX_RENDERS = int(os.environ.get("ALLOY_LIVE_MAX_RENDERS", "60"))
MAX_COST_USD = float(os.environ.get("ALLOY_LIVE_MAX_COST_USD", "5"))
WALL_MINUTES = int(os.environ.get("ALLOY_LIVE_WALL_MINUTES", "60"))
REPORT_DIR = Path(os.environ.get("ALLOY_LIVE_REPORT_DIR", "")) if os.environ.get("ALLOY_LIVE_REPORT_DIR") else None


def _config() -> BuilderConfig:
    cfg = BuilderConfig.load()
    cfg.limits.update({"max_requests": MAX_REQUESTS, "max_renders": MAX_RENDERS, "max_cost_usd": MAX_COST_USD,
                       "wall_clock_minutes": WALL_MINUTES, "attempts_per_finding": 1})
    return cfg


def _write(report_dir: Path | None, name: str, payload: dict) -> None:
    if report_dir is None:
        return
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


@pytest.mark.blender
def test_live_preflight_and_bounded_fixture_run(blender_exe, workdir):
    from builder.blender.runner import BlenderRunner

    cfg = _config()
    runner = BlenderRunner(blender_exe, logs_dir=workdir / "logs", deadlines=cfg.deadlines)
    project, info = create_fixture(workdir / "fixture", runner)
    try:
        adapters = {label: make_adapter(binding) for label, binding in cfg.agents.items()}
        eng = Engine(project, cfg, adapters=adapters, runner=runner)
        report = eng.preflight(live=True)
        _write(REPORT_DIR, "preflight.json", report)
        for label in cfg.agents:
            assert report[label]["ok"], report[label]["blockers"]
        eng.start(attended=True)
        status = eng.run_until_stop(max_steps=40)
        _write(REPORT_DIR, "status.json", status)
        assert status["consumption"]["requests"]["completed"] <= MAX_REQUESTS
        assert status["stop_reason"] in ("ready_for_user_review", "budget_limit", "attempt_limit", "time_limit"), status["notes"]
        assert status["contributions"]["A"] + status["contributions"]["B"] >= 1
        assert project.store.verify_rebuild()
    finally:
        project.close()
