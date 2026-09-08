"""D3 argv policy, R-10 session isolation, R-15/R-18/R-21 preflight tiers and cache keys, R-20 no fallback."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from builder.providers.base import (
    CAPABILITIES,
    InvocationRequest,
    PolicyViolation,
    SessionRef,
    SessionRegistry,
    Usage,
    assert_argv_policy,
    preflight_cache_key,
    preflight_verdict,
)
from builder.providers.mock import ScriptedAdapter
from builder.records import UNKNOWN, Measured
from builder.store import Store

ROOT = Path(__file__).resolve().parents[2]


def test_builder_package_never_uses_shell_or_message_templates():
    files = list((ROOT / "builder").rglob("*.py"))
    assert files, f"builder package not found at {ROOT / 'builder'}"
    offenders = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        if re.search(r"shell\s*=\s*True", text) or "{message}" in text:
            offenders.append(str(p))
    assert offenders == []


def test_builder_never_imports_alloy_chat_machinery():
    banned = ("orchestrator", "prompts", "tools", "actions", "invocation", "sessions")
    files = list((ROOT / "builder").rglob("*.py"))
    assert files, f"builder package not found at {ROOT / 'builder'}"
    offenders = []
    for p in files:
        for line in p.read_text(encoding="utf-8").splitlines():
            if re.match(r"\s*(from|import)\s+(%s)\b" % "|".join(banned), line):
                offenders.append(f"{p.name}: {line.strip()}")
    assert offenders == []


@pytest.mark.parametrize("argv", [
    ["claude", "-p", "--continue"], ["claude", "-p", "-c"], ["codex", "exec", "resume", "--last"],
    ["claude", "-p", "--fallback-model", "x"], ["codex", "exec", "--ephemeral"],
    ["claude", "-p", "--no-session-persistence"], ["gemini", "-p", "x", "--resume", "latest"],
    ["gemini", "-p", "x", "--resume", "3"], ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"],
])
def test_argv_policy_rejects_forbidden_mechanisms(argv):
    with pytest.raises(PolicyViolation):
        assert_argv_policy(argv)


def test_argv_policy_accepts_explicit_sessions():
    assert_argv_policy(["claude", "-p", "--session-id", "0d3f8c1e-1111-4222-8333-444455556666", "--output-format", "json"])
    assert_argv_policy(["codex", "exec", "resume", "0d3f8c1e-1111-4222-8333-444455556666", "--json", "-"])
    with pytest.raises(PolicyViolation):
        assert_argv_policy("claude -p hello")  # a string would be a shell command


@pytest.fixture
def store(workdir):
    s = Store(workdir / "b.sqlite3").open()
    yield s
    s.close()


def test_two_agents_on_one_provider_get_distinct_persistent_sessions(store):
    reg = SessionRegistry(store)
    a = reg.persistent("ag_A", provider="codex")
    b = reg.persistent("ag_B", provider="codex")
    assert a.data["session_uuid"] != b.data["session_uuid"]
    assert re.fullmatch(r"[0-9a-f-]{36}", a.data["session_uuid"])
    assert reg.persistent("ag_A", provider="codex").id == a.id  # stable per agent
    iso1 = reg.isolated("ag_B", provider="codex", reason="review of cmp_1 while contaminated")
    iso2 = reg.isolated("ag_B", provider="codex", reason="second review")
    assert len({a.data["session_uuid"], b.data["session_uuid"], iso1.data["session_uuid"], iso2.data["session_uuid"]}) == 4
    assert iso1.data["kind"] == "isolated_review" and b.data["kind"] == "persistent"
    ref = reg.ref_for(a, first_use=True)
    assert ref == SessionRef(kind="new", uuid=a.data["session_uuid"])
    reg.mark_used(a, journal_seq=5)
    assert reg.ref_for(store.get("provider_session", a.id), first_use=False).kind == "resume"


def test_scripted_adapter_returns_scripted_structured_output_and_records_requests(workdir):
    adapter = ScriptedAdapter("mock-claude", screenplay={
        "probe": [{"shape": "triangle", "color": "red", "number": 7}],
        "build": [{"operations": [], "self_assessment": "", "questions_for_user": [],
                   "no_edit_warranted": {"reason": "matches", "evidence": ["rnd_1"]}}],
    })
    req = InvocationRequest(agent_id="ag_A", session=SessionRef("new", "u-1"), purpose="probe", packet_dir=workdir,
                            cwd=workdir, prompt_text="report the image", schema_name="probe_report", output_schema={},
                            model="fable", reasoning="max", images=[workdir / "p.png"])
    res = adapter.invoke(req)
    assert res.outcome == "ok" and res.structured == {"shape": "triangle", "color": "red", "number": 7}
    assert res.usage.input is UNKNOWN and res.usage.cost_usd is UNKNOWN
    assert adapter.invocations[0].purpose == "probe" and adapter.invocations[0].images == [workdir / "p.png"]
    assert res.reported_session_id == "u-1"
    assert_argv_policy(adapter.build_argv(req))
    with pytest.raises(RuntimeError):
        adapter.invoke(req)  # screenplay exhausted: mocks never improvise


def test_scripted_adapter_can_script_failures_and_usage(workdir):
    adapter = ScriptedAdapter("mock", screenplay={
        "build": [{"__outcome__": "timeout"}, {"__raw__": "not json at all"},
                  {"__structured__": {"x": 1}, "__usage__": {"input": 120, "output": 30, "cost_usd": 0.01}}],
    })
    req = InvocationRequest(agent_id="ag_A", session=SessionRef("new", "u"), purpose="build", packet_dir=workdir,
                            cwd=workdir, prompt_text="x", schema_name="task_result", output_schema={}, model="m", reasoning="")
    assert adapter.invoke(req).outcome == "timeout"
    r2 = adapter.invoke(req)
    assert r2.outcome == "ok" and r2.structured is None and r2.raw_text == "not json at all"
    r3 = adapter.invoke(req)
    assert r3.usage.input == Measured(120, "mock") and r3.usage.cost_usd == Measured(0.01, "mock")
    assert r3.usage.cached_input is UNKNOWN


def test_usage_json_round_trip_keeps_unknown_distinct_from_zero():
    u = Usage(input=Measured(0, "p"))
    d = u.to_json()
    assert d["input"] == {"kind": "measured", "value": 0, "source": "p"} and d["output"] == {"kind": "unknown"}
    back = Usage.from_json(d)
    assert back.input == Measured(0, "p") and back.output is UNKNOWN


def test_preflight_verdict_blocks_on_missing_required_capability_with_remediation():
    report = {cap: {"declared": "yes", "local": "ok", "live": "verified"} for cap in CAPABILITIES}
    ok, blockers = preflight_verdict(report, required=["image_reading", "structured_output"])
    assert ok and blockers == []
    report["image_reading"] = {"declared": "no", "local": "unknown", "live": "not_run"}
    ok, blockers = preflight_verdict(report, required=["image_reading"])
    assert not ok and "image_reading" in blockers[0] and "remediation" in blockers[0].lower()
    report["image_reading"] = {"declared": "yes", "local": "ok", "live": "failed", "evidence": "probe reported 'square'"}
    ok, blockers = preflight_verdict(report, required=["image_reading"])
    assert not ok and "probe reported" in blockers[0]
    report["image_reading"] = {"declared": "yes", "local": "ok", "live": "not_run"}
    ok, blockers = preflight_verdict(report, required=["image_reading"], require_live=True)
    assert not ok and "live" in blockers[0]
    ok, _ = preflight_verdict(report, required=["image_reading"], require_live=False)
    assert ok


def test_prompt_only_read_only_is_labeled_unsupported_for_shared_write():
    report = {cap: {"declared": "yes", "local": "ok", "live": "verified"} for cap in CAPABILITIES}
    report["read_only_enforcement"] = {"declared": "prompt_only", "local": "ok", "live": "not_run"}
    ok, blockers = preflight_verdict(report, required=["read_only_enforcement"], shared_write=True)
    assert not ok and "unsupported for shared write" in blockers[0]
    ok, blockers = preflight_verdict(report, required=["read_only_enforcement"], shared_write=False, require_live=False)
    assert ok


def test_preflight_cache_key_invalidates_on_any_change():
    k = preflight_cache_key(cli_path="C:/x/claude", version="2.1.233", model="fable", settings={"effort": "max"})
    assert k == preflight_cache_key(cli_path="C:/x/claude", version="2.1.233", model="fable", settings={"effort": "max"})
    assert k != preflight_cache_key(cli_path="C:/y/claude", version="2.1.233", model="fable", settings={"effort": "max"})
    assert k != preflight_cache_key(cli_path="C:/x/claude", version="2.1.234", model="fable", settings={"effort": "max"})
    assert k != preflight_cache_key(cli_path="C:/x/claude", version="2.1.233", model="opus", settings={"effort": "max"})
    assert k != preflight_cache_key(cli_path="C:/x/claude", version="2.1.233", model="fable", settings={"effort": "high"})
