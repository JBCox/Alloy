"""R-15 local tier and R-19 sourcing: executables resolved without a shell layer, flags read from ``--help``
(layer D with synthetic help text and shims; layer ``cli`` against the installed CLIs, free, skipped when absent)."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from builder.config import AgentBinding
from builder.providers import make_adapter
from builder.providers.cli_common import help_segment, parse_help_flags, requirement_met, resolve_cli

CLAUDE_HELP_SNIPPET = """Usage: claude [options] [command] [prompt]

Options:
  --add-dir <directories...>            Additional directories to allow tool
                                        access to
  -c, --continue                        Continue the most recent conversation in
                                        the current directory
  --json-schema <schema>                JSON Schema for structured output
                                        validation.
  --output-format <format>              Output format (only works with --print):
                                        "text" (default), "json" (single
                                        result), or "stream-json" (choices:
                                        "text", "json", "stream-json")
  --permission-mode <mode>              Permission mode to use for the session
                                        (choices: "acceptEdits", "auto",
                                        "bypassPermissions", "manual",
                                        "dontAsk", "plan")
  -p, --print                           Print response and exit
  -r, --resume [value]                  Resume a conversation by session ID
  --session-id <uuid>                   Use a specific session ID
"""


def test_parse_help_flags_and_choice_requirements():
    flags = parse_help_flags(CLAUDE_HELP_SNIPPET)
    assert {"--add-dir", "-c", "--continue", "--json-schema", "--output-format", "--permission-mode", "-p", "--print",
            "-r", "--resume", "--session-id"} <= flags
    assert requirement_met(CLAUDE_HELP_SNIPPET, "--permission-mode=plan")
    assert requirement_met(CLAUDE_HELP_SNIPPET, "--output-format=json")
    assert not requirement_met(CLAUDE_HELP_SNIPPET, "--permission-mode=yolo")
    assert not requirement_met(CLAUDE_HELP_SNIPPET, "--effort")
    assert "plan" in help_segment(CLAUDE_HELP_SNIPPET, "--permission-mode") and "--print" not in help_segment(CLAUDE_HELP_SNIPPET, "--permission-mode")
    assert requirement_met("Commands:\n  resume  Resume a previous session by id", "word:resume")
    assert not requirement_met("Commands:\n  review", "word:resume")


def test_resolve_cli_unwraps_npm_cmd_shims_into_node_plus_bundle(workdir):
    npm = workdir / "npm dir ü"
    (npm / "node_modules" / "@vendor" / "tool" / "bundle").mkdir(parents=True)
    script = npm / "node_modules" / "@vendor" / "tool" / "bundle" / "tool.js"
    script.write_text("console.log('x')", encoding="utf-8")
    shim = npm / "tool.cmd"
    shim.write_text('@ECHO off\r\nSETLOCAL\r\n"%_prog%"  "%dp0%\\node_modules\\@vendor\\tool\\bundle\\tool.js" %*\r\n',
                    encoding="utf-8")
    r = resolve_cli("tool", configured=str(shim))
    if shutil.which("node") is None:
        pytest.skip("node is not installed; shim resolution needs it")
    assert r.found and r.via == "npm_shim" and Path(r.argv_head[1]) == script and r.argv_head[0].lower().endswith("node.exe")
    assert r.path == str(shim) and r.script == str(script)
    plain = npm / "plain.cmd"
    plain.write_text("@echo hi\r\n", encoding="utf-8")
    p = resolve_cli("plain", configured=str(plain))
    assert p.via == "executable" and p.argv_head == [str(plain)]
    missing = resolve_cli("nope-xyz", configured=str(npm / "absent.exe"))
    assert not missing.found and missing.via == "missing" and missing.argv_head == []


def test_configured_path_is_not_replaced_by_a_path_lookup(workdir):
    r = resolve_cli("python", configured=str(workdir / "not here.exe"))   # python exists on PATH, but the config names a missing file
    assert not r.found


@pytest.mark.cli
@pytest.mark.parametrize("provider,model,reasoning,version_re", [
    ("claude", "fable", "max", r"^\d+\.\d+\.\d+"),
    ("gemini", "gemini-x", "", r"^\d+\.\d+\.\d+"),
    ("codex", "gpt-6-astra", "xhigh", r"^codex-cli \d+\.\d+\.\d+"),
])
def test_installed_cli_lists_every_flag_the_adapter_passes(provider, model, reasoning, version_re):
    """Free local checks only (``--version``, ``--help``): the argv each adapter builds is backed by the CLI's own help."""
    adapter = make_adapter(AgentBinding("X", provider, model, reasoning))
    resolved = adapter.resolved()
    if not resolved.found:
        pytest.skip(f"{provider} is not installed on PATH")
    local = adapter.preflight_local()
    assert local["found"] and re.match(version_re, local["version"] or ""), local
    assert local["errors"] == [], local["errors"]
    assert local["missing_flags"] == [], f"{provider} --help does not list: {local['missing_flags']}"
    assert local["help_hash"] and local["help_bytes"] > 1000
    if provider in ("gemini", "codex"):
        assert local["resolved_via"] == "npm_shim" and local["argv_head"][0].lower().endswith("node.exe")
    else:
        assert local["resolved_via"] == "executable" and local["cli_path"].lower().endswith(".exe")
