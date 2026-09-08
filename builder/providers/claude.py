"""claude adapter (design Section 6.3; spec Appendix A, D3, R-10, R-16, R-17, R-19, R-20, R-24, R-25).

argv, from ``claude --help`` 2.1.233 and the headless docs (code.claude.com/docs/en/headless):

    claude -p --output-format json --json-schema <schema> (--session-id <uuid> | --resume <uuid>)
           --model <m> [--effort <level>] --tools Read,Glob,Grep --permission-mode plan
           --strict-mcp-config --disable-slash-commands --add-dir <packet_dir>
           [--max-budget-usd <cap>] [--append-system-prompt <framing>]

The prompt travels on stdin (documented: piped stdin is the prompt, 10 MB cap). Never ``--continue``,
``-c``, ``--fallback-model``, ``--no-session-persistence``, ``--bare`` (bare mode reads no OAuth login).
Read at runtime from the JSON result: ``result``, ``structured_output``, ``session_id``, ``is_error``,
``total_cost_usd`` (a client-side estimate per the docs), ``usage`` and ``modelUsage`` as present.
"""
from __future__ import annotations

import json
from typing import Any

from ..records import Estimated
from .base import CAPABILITIES, InvocationRequest, InvocationResult
from .cli_common import (
    OUTCOME_BUDGET,
    OUTCOME_PROVIDER_ERROR,
    OUTCOME_UNPARSABLE,
    CliAdapter,
    last_json_object,
    read_text,
    usage_from,
)
from .process import ProcessResult

TOOLS = "Read,Glob,Grep"
PERMISSION_MODE = "plan"
# Anthropic Messages API usage field names (official API reference); applied only when the CLI's JSON carries them.
USAGE_FIELDS = {"input": "input_tokens", "output": "output_tokens", "cached_input": "cache_read_input_tokens"}


class ClaudeAdapter(CliAdapter):
    name = "claude"
    executable = "claude"
    supports_native_schema = True
    HELP_COMMANDS = [["--help"]]
    REASONING_FALLBACKS: dict[str, str] = {}     # no documented next level for --effort; a rejection is a preflight failure

    def declared_capabilities(self) -> dict[str, str]:
        caps = {cap: "yes" for cap in CAPABILITIES}
        caps["cost_cap"] = "yes"                 # --max-budget-usd is an enforceable per-invocation cap
        return caps

    def local_requirements(self) -> dict[str, list[str]]:
        return {
            "image_reading": ["--add-dir", "--tools", "-p"],
            "evidence_dir_access": ["--add-dir"],
            "read_only_enforcement": ["--permission-mode=plan", "--tools"],
            "session_create": ["--session-id"],
            "session_resume": ["--resume"],
            "structured_output": ["--json-schema", "--output-format=json"],
            "model_settings_applied": ["--model", "--effort"],
            "usage_reporting": ["--output-format=json"],
            "cancellation": [],
            "cost_cap": ["--max-budget-usd"],
            "framing": ["--append-system-prompt", "--strict-mcp-config", "--disable-slash-commands"],
        }

    def settings_signature(self) -> dict[str, Any]:
        return {"tools": TOOLS, "permission_mode": PERMISSION_MODE, "output_format": "json", "strict_mcp_config": True,
                "disable_slash_commands": True, "prompt_via": "stdin", "framing_via": "--append-system-prompt"}

    def build_argv(self, req: InvocationRequest) -> list[str]:
        argv = [*self.head(), "-p", "--output-format", "json", "--json-schema", json.dumps(req.output_schema, ensure_ascii=False)]
        if req.session.kind == "resume":
            argv += ["--resume", req.session.uuid]
        else:
            argv += ["--session-id", req.session.uuid]
        argv += ["--model", req.model]
        if req.reasoning:
            argv += ["--effort", req.reasoning]
        argv += ["--tools", TOOLS, "--permission-mode", PERMISSION_MODE, "--strict-mcp-config", "--disable-slash-commands",
                 "--add-dir", str(req.packet_dir)]
        if req.max_cost_usd is not None and req.max_cost_usd > 0:
            argv += ["--max-budget-usd", f"{req.max_cost_usd:g}"]
        if req.framing_text:
            argv += ["--append-system-prompt", req.framing_text]
        return argv

    def stdin_payload(self, req: InvocationRequest) -> bytes:
        return req.prompt_text.encode("utf-8")

    def parse_result(self, req: InvocationRequest, proc: ProcessResult) -> InvocationResult:
        stdout = read_text(proc.stdout_path)
        obj = last_json_object(stdout)
        effective = {"permission_mode": PERMISSION_MODE, "tools": TOOLS}
        if obj is None:
            err = (proc.stderr_tail or stdout).strip()[-800:] or "no output"
            return self._failure(req, proc, OUTCOME_UNPARSABLE, f"claude produced no JSON result: {err}", **effective)
        session = obj.get("session_id") if isinstance(obj.get("session_id"), str) else None
        cost = obj.get("total_cost_usd")
        if obj.get("is_error"):
            # Observed live (2.1.233): a run stopped by --max-budget-usd reports is_error with result null and a
            # total_cost_usd at or above the cap it was given; the spend has already happened.
            if (req.max_cost_usd is not None and isinstance(cost, (int, float)) and not isinstance(cost, bool)
                    and float(cost) >= float(req.max_cost_usd) and obj.get("result") in (None, "")):
                res = self._failure(req, proc, OUTCOME_BUDGET,
                                    f"claude stopped at its per-invocation budget cap (--max-budget-usd {req.max_cost_usd:g}; "
                                    f"reported total_cost_usd {cost}); stop_reason={obj.get('stop_reason')}", **effective)
                res.usage.cost_usd = Estimated(cost, "claude:total_cost_usd (client-side estimate per docs)")
            else:
                res = self._failure(req, proc, OUTCOME_PROVIDER_ERROR, str(obj.get("result") or obj.get("error") or obj), **effective)
            res.reported_session_id = session
            res.raw_text = str(obj.get("result") or "")
            return res
        if proc.outcome != "ok":
            return self._failure(req, proc, "nonzero_exit", f"claude exited {proc.exit_code}: {proc.stderr_tail.strip()[-500:]}",
                                 **effective)
        usage, unmapped = usage_from(obj.get("usage"), USAGE_FIELDS, "claude:usage")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            usage.cost_usd = Estimated(cost, "claude:total_cost_usd (client-side estimate per docs)")
        structured = obj.get("structured_output")
        model_usage = obj.get("modelUsage")
        effective.update({
            "model_reported": sorted(model_usage) if isinstance(model_usage, dict) and model_usage else "not reported",
            "reasoning_reported": (f"not reported by the CLI; flag value --effort {req.reasoning} not confirmed by provider"
                                   if req.reasoning else "not exposed"),
            "usage_raw": obj.get("usage") if isinstance(obj.get("usage"), dict) else None,
            "usage_unmapped_fields": unmapped, "num_turns": obj.get("num_turns"), "duration_ms": obj.get("duration_ms"),
            "subtype": obj.get("subtype"),
        })
        res = InvocationResult(outcome="ok", exit_code=proc.exit_code, structured=structured if isinstance(structured, dict) else None,
                               raw_text=str(obj.get("result") or ""), reported_session_id=session, usage=usage,
                               argv=list(proc.argv), effective_settings=effective)
        if session and session != req.session.uuid:
            res.warnings.append(f"claude reported session_id {session} but Alloy requested {req.session.uuid}")
        return res
