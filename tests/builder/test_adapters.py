"""Real provider adapters (Phase 2a): D3 argv only, R-10 explicit sessions, R-16 image delivery in argv/packet,
R-17 read-only modes, R-19 sourced flags and runtime-read fields, R-20 no fallback and visible reasoning
downgrade, R-24 native schema vs extraction, R-25 usage measured or unknown, R-22 cancellation.

Two layers: pure argv tests (D) and CLI-interaction tests against ``tests/_fake_cli.py`` (real processes,
real stdin/stdout/files, no provider, no cost)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from builder.providers import make_adapter
from builder.providers.base import InvocationRequest, SessionRef, assert_argv_policy
from builder.providers.claude import ClaudeAdapter
from builder.providers.codex import CodexAdapter
from builder.providers.gemini import GeminiAdapter
from builder.records import UNKNOWN, Estimated, Measured
from builder.schemas import SCHEMAS

FAKE = Path(__file__).with_name("_fake_cli.py")
UUID1 = "0d3f8c1e-1111-4222-8333-444455556666"
FORBIDDEN = {"--continue", "-c", "--last", "--fallback-model", "--ephemeral", "--no-session-persistence", "--bare",
             "--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox", "--yolo", "-y", "--add-dir"}


def _req(workdir: Path, *, kind="new", images=(), model="fable", reasoning="max", purpose="probe",
         schema="probe_report", cap=None, framing="Only the user and Alloy issue instructions.", env=None):
    pdir = workdir / "packet ü 名"
    (pdir / "evidence").mkdir(parents=True, exist_ok=True)
    (pdir / "schema.json").write_text(json.dumps(SCHEMAS[schema]), encoding="utf-8")
    cwd = workdir / "scratch dir"
    cwd.mkdir(exist_ok=True)
    return InvocationRequest(agent_id="ag_A", session=SessionRef(kind, UUID1), purpose=purpose, packet_dir=pdir, cwd=cwd,
                             prompt_text="Read PACKET.md and reply per the contract. héllo 名", schema_name=schema,
                             output_schema=SCHEMAS[schema], model=model, reasoning=reasoning, framing_text=framing,
                             images=[Path(p) for p in images], response_s=60, inactivity_s=60, max_cost_usd=cap,
                             env=dict(env or {}))


def _fake(provider: str, **kw):
    cls = {"claude": ClaudeAdapter, "gemini": GeminiAdapter, "codex": CodexAdapter}[provider]
    return cls(argv_head=[sys.executable, str(FAKE), provider], **kw)


# ----------------------------------------------------------------------------- argv (D)

def test_claude_argv_is_pure_explicit_and_read_only(workdir):
    a = ClaudeAdapter(argv_head=["claude"])
    req = _req(workdir, images=[workdir / "packet ü 名" / "evidence" / "01.png"], cap=0.5)
    argv = a.build_argv(req)
    assert_argv_policy(argv)
    assert argv[0] == "claude" and "-p" in argv
    assert argv[argv.index("--session-id") + 1] == UUID1 and "--resume" not in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMAS["probe_report"]
    assert argv[argv.index("--model") + 1] == "fable" and argv[argv.index("--effort") + 1] == "max"
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert "--strict-mcp-config" in argv and "--disable-slash-commands" in argv
    assert argv[argv.index("--add-dir") + 1] == str(req.packet_dir)
    assert argv[argv.index("--max-budget-usd") + 1] == "0.5"
    assert argv[argv.index("--append-system-prompt") + 1].startswith("Only the user")
    assert not (FORBIDDEN - {"--add-dir"}) & set(argv)
    assert a.stdin_payload(req).decode("utf-8") == req.prompt_text  # framing travels as system prompt, not stdin
    resumed = a.build_argv(_req(workdir, kind="resume"))
    assert resumed[resumed.index("--resume") + 1] == UUID1 and "--session-id" not in resumed
    assert "--max-budget-usd" not in resumed  # no cap configured -> no flag
    assert "--effort" not in a.build_argv(_req(workdir, reasoning=""))  # no invented reasoning level


def test_gemini_argv_uses_plan_mode_and_explicit_session(workdir):
    a = GeminiAdapter(argv_head=["gemini"])
    req = _req(workdir, model="gemini-x", reasoning="")
    argv = a.build_argv(req)
    assert_argv_policy(argv)
    assert argv[argv.index("-o") + 1] == "json" and argv[argv.index("-m") + 1] == "gemini-x"
    assert argv[argv.index("--approval-mode") + 1] == "plan"
    assert argv[argv.index("--include-directories") + 1] == str(req.packet_dir)
    assert argv[argv.index("--session-id") + 1] == UUID1
    assert "-p" in argv and "--skip-trust" in argv
    assert not FORBIDDEN & set(argv)
    payload = a.stdin_payload(req).decode("utf-8")
    assert payload.startswith("Only the user") and req.prompt_text in payload
    resumed = a.build_argv(_req(workdir, kind="resume"))
    assert resumed[resumed.index("--resume") + 1] == UUID1 and "--session-id" not in resumed
    assert a.declared_capabilities()["session_resume"] == "probe"     # resume by UUID is undocumented: probed live
    assert a.declared_capabilities()["cost_cap"] == "no"


def test_codex_argv_attaches_images_and_reads_prompt_from_stdin(workdir):
    a = CodexAdapter(argv_head=["codex"])
    img = workdir / "packet ü 名" / "evidence" / "01 ref.png"
    req = _req(workdir, images=[img], model="gpt-6-astra", reasoning="xhigh")
    a.prepare(req)
    argv = a.build_argv(req)
    assert_argv_policy(argv)
    assert argv[:2] == ["codex", "exec"] and argv[-1] == "-"
    assert "--json" in argv and "--skip-git-repo-check" in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == str(req.cwd)
    assert argv[argv.index("-m") + 1] == "gpt-6-astra"
    cfg = [argv[i + 1] for i, t in enumerate(argv) if t == "-c"]
    assert 'model_reasoning_effort="xhigh"' in cfg and 'sandbox_mode="read-only"' in cfg and 'approval_policy="never"' in cfg
    assert argv[argv.index("-i") + 1] == str(img)
    schema_file = Path(argv[argv.index("--output-schema") + 1])
    written = json.loads(schema_file.read_text(encoding="utf-8"))
    assert schema_file.is_file() and written["additionalProperties"] is False and set(written["required"]) == {"shape", "color", "number"}
    assert Path(argv[argv.index("-o") + 1]).parent == schema_file.parent
    assert "resume" not in argv and not (FORBIDDEN - {"-c"}) & set(argv)   # codex -c is a config override, not "continue"
    resumed = a.build_argv(_req(workdir, kind="resume", model="gpt-6-astra", reasoning="xhigh"))
    assert resumed[:4] == ["codex", "exec", "resume", UUID1] and resumed[-1] == "-"
    assert "-s" not in resumed and "-C" not in resumed  # not offered by `codex exec resume --help`; config keys carry the policy
    assert 'sandbox_mode="read-only"' in [resumed[i + 1] for i, t in enumerate(resumed) if t == "-c"]


def test_no_adapter_passes_a_shell_string_or_message_template():
    import ast

    for cls in (ClaudeAdapter, GeminiAdapter, CodexAdapter):
        src = Path(sys.modules[cls.__module__].__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        first_code = tree.body[1] if ast.get_docstring(tree) else tree.body[0]   # skip the module docstring
        code = "\n".join(ln for ln in src.splitlines()[first_code.lineno - 1:] if not ln.strip().startswith("#"))
        assert not re.search(r"shell\s*=\s*True", code) and "{message}" not in code
        assert "--fallback-model" not in code and "fallback_ai" not in code and "--continue" not in code and "--last" not in code


def test_strict_schema_form_for_openai_and_null_stripping():
    """Observed live (codex 0.153.1): the API rejects a schema unless every object has additionalProperties false and
    lists every property as required. The provider copy is strict; Alloy validates against its own schema."""
    from builder.providers.cli_common import strict_schema, strip_optional_nulls
    from builder.schemas import validate_against

    def walk(s, path="$"):
        if isinstance(s, dict):
            if "properties" in s:
                assert s.get("additionalProperties") is False, path
                assert set(s["required"]) == set(s["properties"]), path
                for k, v in s["properties"].items():
                    walk(v, f"{path}.{k}")
            if isinstance(s.get("items"), dict):
                walk(s["items"], f"{path}[]")
            assert "minItems" not in s, path

    for name, schema in SCHEMAS.items():
        strict = strict_schema(schema)
        walk(strict, name)
        assert json.dumps(schema) == json.dumps(SCHEMAS[name])   # the original is untouched
    strict = strict_schema(SCHEMAS["findings_report"])
    finding = strict["properties"]["findings"]["items"]["properties"]
    assert finding["region"]["type"] == ["string", "null"] and finding["kind"]["enum"][-1] is None
    assert finding["severity"]["type"] == "string"          # required keys keep their type
    reply = {"findings": [{"part_id": "p", "region": None, "view": None, "observed_mismatch": "gap", "severity": "high",
                           "confidence": "high", "evidence_refs": ["r"], "proposed_correction": "x", "expected_improvement": "y",
                           "alternative_hypotheses": None, "kind": None, "rationale": None}],
             "coverage": [], "rationale": None}
    assert validate_against(SCHEMAS["findings_report"], reply)            # nulls fail Alloy's own schema
    cleaned = strip_optional_nulls(reply, SCHEMAS["findings_report"])
    assert validate_against(SCHEMAS["findings_report"], cleaned) == []
    assert "region" not in cleaned["findings"][0] and "rationale" not in cleaned
    bad = strip_optional_nulls({"findings": None, "coverage": []}, SCHEMAS["findings_report"])
    assert bad["findings"] is None                                         # required keys are never silently dropped


def test_schemas_fit_comfortably_in_a_windows_command_line():
    for name, schema in SCHEMAS.items():
        assert len(json.dumps(schema)) < 8000, name


def test_awkward_paths_survive_argv_round_trip_to_a_native_child(workdir):
    a = ClaudeAdapter(argv_head=[sys.executable, str(Path(__file__).with_name("_child.py")), "echo"])
    req = _req(workdir)
    argv = a.build_argv(req)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}   # as the process layer sets them
    out = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", cwd=str(req.cwd), input="x",
                         timeout=60, env=env).stdout
    got = json.loads(out.splitlines()[0])["args"]
    assert str(req.packet_dir) in got and json.loads(got[got.index("--json-schema") + 1]) == SCHEMAS["probe_report"]


def test_factory_builds_adapters_from_bindings_and_refuses_unknown_providers():
    from builder.config import AgentBinding

    assert isinstance(make_adapter(AgentBinding("A", "claude", "fable", "max")), ClaudeAdapter)
    assert isinstance(make_adapter(AgentBinding("B", "codex", "gpt-6-astra", "xhigh")), CodexAdapter)
    assert isinstance(make_adapter(AgentBinding("B", "gemini", "g", "")), GeminiAdapter)
    with pytest.raises(ValueError, match="copilot"):
        make_adapter(AgentBinding("B", "copilot"))


# ------------------------------------------------------------ CLI interaction with the fake CLI

def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_claude_invoke_parses_structured_output_session_usage_and_estimated_cost(workdir, monkeypatch):
    rec = workdir / "calls.jsonl"
    monkeypatch.setenv("ALLOY_FAKE_CLI_RECORD", str(rec))
    monkeypatch.setenv("ALLOY_FAKE_CLI_REPLY", json.dumps({"shape": "triangle", "color": "red", "number": 7}))
    a = _fake("claude")
    req = _req(workdir)
    res = a.invoke(req)
    assert res.outcome == "ok", res.error
    assert res.structured == {"shape": "triangle", "color": "red", "number": 7}
    assert res.reported_session_id == UUID1
    assert res.usage.input == Measured(120, "claude:usage.input_tokens")
    assert res.usage.output == Measured(30, "claude:usage.output_tokens")
    assert res.usage.cached_input == Measured(50, "claude:usage.cache_read_input_tokens")
    assert res.usage.reasoning is UNKNOWN and res.usage.image is UNKNOWN
    assert res.usage.cost_usd == Estimated(0.0123, "claude:total_cost_usd (client-side estimate per docs)")
    assert res.effective_settings["model_reported"] == ["claude-fable-5-1-fake"]
    assert "not confirmed" in res.effective_settings["reasoning_reported"]
    call = _records(rec)[0]
    assert call["stdin"] == req.prompt_text and Path(call["cwd"]).resolve() == req.cwd.resolve()
    assert Path(res.stdout_path).is_file() and Path(res.stderr_path).is_file()


def test_claude_text_only_reply_is_returned_raw_for_extraction(workdir, monkeypatch):
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "text_only")
    res = _fake("claude").invoke(_req(workdir))
    assert res.outcome == "ok" and res.structured is None and '"shape"' in res.raw_text   # JSON embedded in prose


def test_claude_is_error_result_is_a_provider_error_not_a_retry(workdir, monkeypatch):
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "error")
    res = _fake("claude").invoke(_req(workdir, model="nope-1"))
    assert res.outcome == "provider_error" and "Invalid model name" in res.error and res.exit_code == 1


def test_claude_budget_stop_is_reported_as_budget_exhausted_with_the_spend(workdir, monkeypatch):
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "budget")
    res = _fake("claude").invoke(_req(workdir, cap=0.06))
    assert res.outcome == "budget_exhausted" and "0.06" in res.error and "2.06" in res.error
    assert res.usage.cost_usd == Estimated(2.06, "claude:total_cost_usd (client-side estimate per docs)")


def test_claude_no_usage_fields_are_unknown_never_zero(workdir, monkeypatch):
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "no_usage")
    res = _fake("claude").invoke(_req(workdir))
    assert res.outcome == "ok" and res.usage.input is UNKNOWN and res.usage.output is UNKNOWN
    assert res.effective_settings["model_reported"] == "not reported"


def test_gemini_invoke_extracts_json_from_response_and_keeps_stats_raw(workdir, monkeypatch):
    rec = workdir / "calls.jsonl"
    monkeypatch.setenv("ALLOY_FAKE_CLI_RECORD", str(rec))
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "text_only")
    a = _fake("gemini")
    req = _req(workdir, model="gemini-x", reasoning="")
    res = a.invoke(req)
    assert res.outcome == "ok" and res.structured is None and '"shape"' in res.raw_text   # extracted later (R-24)
    # No usage field name is sourced for gemini (R-19): everything stays unknown and the raw stats are kept.
    assert all(getattr(res.usage, f) is UNKNOWN for f in res.usage.FIELDS)
    assert res.effective_settings["stats_raw"]["tools"] == {"totalCalls": 1}
    assert res.effective_settings["reasoning_reported"] == "not exposed by CLI"
    call = _records(rec)[0]
    assert "Only the user" in call["stdin"] and req.prompt_text in call["stdin"]


def test_gemini_error_object_and_exit_code_are_reported(workdir, monkeypatch):
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "error")
    res = _fake("gemini").invoke(_req(workdir, model="nope-1", reasoning=""))
    assert res.outcome == "provider_error" and "invalid model" in res.error and res.exit_code == 42


def test_codex_invoke_reads_jsonl_events_last_message_and_usage(workdir, monkeypatch):
    rec = workdir / "calls.jsonl"
    monkeypatch.setenv("ALLOY_FAKE_CLI_RECORD", str(rec))
    monkeypatch.setenv("ALLOY_FAKE_CLI_REPLY", json.dumps({"nonce": "zz"}))
    a = _fake("codex")
    req = _req(workdir, model="gpt-6-astra", reasoning="high", schema="session_probe_report")
    res = a.invoke(req)
    assert res.outcome == "ok", res.error
    # codex chooses the thread id itself (no --session-id flag): Alloy records the reported id and resumes with it
    assert res.structured == {"nonce": "zz"} and re.fullmatch(r"[0-9a-f-]{36}", res.reported_session_id)
    assert res.reported_session_id != UUID1
    resumed = a.invoke(_req(workdir, kind="resume", model="gpt-6-astra", reasoning="high", schema="session_probe_report"))
    assert resumed.outcome == "ok" and resumed.reported_session_id == UUID1
    assert res.usage.input == Measured(200, "codex:turn.completed.usage.input_tokens")
    assert res.usage.cached_input == Measured(80, "codex:turn.completed.usage.cached_input_tokens")
    assert res.usage.output == Measured(40, "codex:turn.completed.usage.output_tokens")
    assert res.usage.reasoning == Measured(15, "codex:turn.completed.usage.reasoning_output_tokens")
    assert res.usage.cost_usd is UNKNOWN
    assert res.effective_settings["reasoning_effective"] == "high"
    call = _records(rec)[0]
    assert call["stdin"].endswith(req.prompt_text) and call["argv"][-1] == "-"


def test_codex_reasoning_downgrade_is_visible_and_reported_once(workdir, monkeypatch):
    rec = workdir / "calls.jsonl"
    monkeypatch.setenv("ALLOY_FAKE_CLI_RECORD", str(rec))
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "reject_xhigh")
    a = _fake("codex")
    req = _req(workdir, model="gpt-6-astra", reasoning="xhigh", schema="session_probe_report")
    res = a.invoke(req)
    assert res.outcome == "ok"
    assert res.effective_settings["reasoning_requested"] == "xhigh"
    assert res.effective_settings["reasoning_effective"] == "high"
    assert "xhigh" in res.effective_settings["reasoning_rejection"] and "unsupported" in res.effective_settings["reasoning_rejection"]
    assert any("xhigh" in w and "high" in w for w in res.warnings)
    calls = _records(rec)
    assert len(calls) == 2 and 'model_reasoning_effort="xhigh"' in calls[0]["argv"] and 'model_reasoning_effort="high"' in calls[1]["argv"]
    res2 = a.invoke(_req(workdir, model="gpt-6-astra", reasoning="xhigh", schema="session_probe_report"))
    assert res2.outcome == "ok" and len(_records(rec)) == 3  # the accepted value is reused: no second probe
    assert res2.effective_settings["reasoning_effective"] == "high"


def test_codex_turn_failed_is_a_provider_error(workdir, monkeypatch):
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "error")
    res = _fake("codex").invoke(_req(workdir, model="nope-1", reasoning="high"))
    assert res.outcome == "provider_error" and "not found" in res.error


def test_codex_stream_reconnect_notices_before_a_completed_turn_are_warnings_not_a_failure(workdir, monkeypatch):
    """Live 2026-09-08 (E11 build, 18 min): the CLI reported `error` events ("Reconnecting... n/5 ... websocket closed
    by server") and then completed the turn with a full reply and usage. A completed turn is the reply; the notices
    are kept as warnings and the reply is never discarded."""
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "reconnect_then_ok")
    monkeypatch.setenv("ALLOY_FAKE_CLI_REPLY", json.dumps({"nonce": "rc"}))
    res = _fake("codex").invoke(_req(workdir, model="gpt-6-astra", reasoning="high", schema="session_probe_report"))
    assert res.outcome == "ok", res.error
    assert res.structured == {"nonce": "rc"}
    assert res.usage.output == Measured(40, "codex:turn.completed.usage.output_tokens")
    assert sum("Reconnecting" in w for w in res.warnings) == 2 and any("HTTPS" in w for w in res.warnings)


def test_codex_error_event_without_a_completed_turn_is_still_a_provider_error(workdir, monkeypatch):
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "error_event_only")
    res = _fake("codex").invoke(_req(workdir, model="gpt-6-astra", reasoning="high", schema="session_probe_report"))
    assert res.outcome == "provider_error" and "Reconnecting" in res.error


def test_codex_never_downgrades_a_level_without_a_documented_fallback(workdir, monkeypatch):
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "reject_xhigh")
    a = CodexAdapter(argv_head=[sys.executable, str(FAKE), "codex"], reasoning_fallbacks={})
    res = a.invoke(_req(workdir, model="gpt-6-astra", reasoning="xhigh"))
    assert res.outcome == "provider_error" and "xhigh" in res.error


def test_empty_output_is_not_success(workdir, monkeypatch):
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "empty")
    for p in ("claude", "gemini", "codex"):
        res = _fake(p).invoke(_req(workdir, reasoning=""))
        assert res.outcome == "unparsable_output", p


def test_cancellation_kills_the_cli_process_and_confirms(workdir, monkeypatch):
    monkeypatch.setenv("ALLOY_FAKE_CLI_MODE", "slow")
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    t0 = time.time()
    res = _fake("claude").invoke(_req(workdir), cancel_event=cancel)
    assert res.outcome == "cancelled" and res.kill_confirmed is True and time.time() - t0 < 30


def test_local_preflight_runs_version_and_help_and_reports_flags(workdir):
    a = _fake("claude")
    local = a.preflight_local()
    assert local["found"] and local["version"] == "9.9.9 (Claude Code)" and local["help_hash"]
    assert local["flags"]["--help"] is True  # the fake echoes its argv as its help text
    assert "--session-id" in a.local_requirements()["session_create"]
