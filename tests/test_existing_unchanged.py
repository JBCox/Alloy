"""D2 and Section 2 preservation: existing modes, the router, the orchestrator's command building, and Alloy's
config loading behave as before; the builder package touches none of them."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_router_parsing_unchanged():
    from router import CommandType, Router

    r = Router(["claude", "gemini"], "claude")
    p = r.parse("@gemini hello there")
    assert p.command_type == CommandType.DIRECT and p.target_ai == "gemini" and p.content == "hello there"
    p = r.parse("@roundtable[rounds=2] Should we use SQLite?")
    assert p.command_type == CommandType.MODE and p.mode_config is not None and p.content == "Should we use SQLite?"
    p = r.parse("/help")
    assert p.command_type == CommandType.SYSTEM and p.system_command == "help"
    p = r.parse("plain question")
    assert p.command_type == CommandType.DEFAULT and p.content == "plain question"


def test_orchestrator_legacy_command_template_untouched():
    from config import Config
    from orchestrator import Orchestrator

    cfg = Config._default_config()
    orch = Orchestrator(cfg)
    assert "{message}" in cfg.ais["claude"].command          # chat modes keep their template mechanism (D2)
    assert cfg.ais["claude"].fallback_ai == ""                # and their own fallback field, untouched by the builder
    assert getattr(cfg.ais["claude"], "continue_flag", "--continue") == "--continue"   # session continuation (field added by the in-progress chat work)
    if os.name == "nt":
        assert orch._escape_message('say "hi"') == '"say \\"hi\\""'


def test_real_config_yaml_loads_with_existing_ais_and_builder_key():
    from config import Config

    cfg = Config.load(ROOT / "config.yaml")
    assert {"claude", "gemini", "codex"} <= set(cfg.get_enabled_ais())
    assert cfg.default_ai == "claude" and cfg.timeout == 300
    assert cfg.builder["presets"]["e10-bearer-head"]["first_component"] == "Head"
    assert cfg.builder["presets"]["e10-bearer-head"]["target_region"]["bbox"] is None


def test_builder_imports_none_of_the_chat_machinery_in_a_fresh_interpreter():
    code = ("import builder.engine, builder.cli, builder.fixture, sys; "
            "print(sorted(m for m in sys.modules if m in {'orchestrator','prompts','tools','actions','invocation','sessions','main'}))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT), timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_main_build_flag_dispatches_to_builder_cli():
    out = subprocess.run([sys.executable, "main.py", "--build", "--help"], capture_output=True, text=True,
                         cwd=str(ROOT), timeout=120, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr
    assert "Collaborative Model Builder" in out.stdout and "preflight" in out.stdout
    out = subprocess.run([sys.executable, "main.py", "--help"], capture_output=True, text=True, cwd=str(ROOT), timeout=120,
                         encoding="utf-8", errors="replace")
    assert out.returncode == 0 and "--gui" in out.stdout and "--setup" in out.stdout and "--build" in out.stdout
