"""Scripted mock adapters for deterministic tests (spec Section 6: mocks state exactly what they script).

A ``ScriptedAdapter`` returns pre-written structured outputs per purpose, in order, and records every
request it receives. It never improvises: an exhausted screenplay raises. It proves routing, packet
construction, and state handling, not visual intelligence.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from ..records import Measured
from .base import CAPABILITIES, InvocationRequest, InvocationResult, ProviderAdapter, Usage, assert_argv_policy
from .process import ProcessResult


class ScriptedAdapter(ProviderAdapter):
    supports_native_schema = True

    def __init__(self, label: str, screenplay: dict[str, list[Any]], *, capabilities: dict[str, str] | None = None,
                 provider_name: str = "mock"):
        self.label = label
        self.name = provider_name
        self.executable = "mock-cli"
        self.screenplay = {k: list(v) for k, v in screenplay.items()}
        self.invocations: list[InvocationRequest] = []
        self.results: list[InvocationResult] = []
        self._capabilities = {cap: "yes" for cap in CAPABILITIES}
        if capabilities:
            self._capabilities.update(capabilities)

    def declared_capabilities(self) -> dict[str, str]:
        return dict(self._capabilities)

    def build_argv(self, req: InvocationRequest) -> list[str]:
        session_flag = ["--resume", req.session.uuid] if req.session.kind == "resume" else ["--session-id", req.session.uuid]
        return ["mock-cli", "--label", self.label, *session_flag, "--purpose", req.purpose, "--model", req.model,
                "--reasoning", req.reasoning, "--schema", req.schema_name]

    def parse_result(self, req: InvocationRequest, proc: ProcessResult) -> InvocationResult:  # pragma: no cover
        raise NotImplementedError("ScriptedAdapter never runs a process")

    def preflight_local(self) -> dict[str, Any]:
        return {"cli_path": "mock-cli", "found": True, "version": "mock-1.0", "help_hash": "mock", "flags": {}}

    def invoke(self, req: InvocationRequest, cancel_event: threading.Event | None = None) -> InvocationResult:
        argv = self.build_argv(req)
        assert_argv_policy(argv)
        self.invocations.append(req)
        queue = self.screenplay.get(req.purpose)
        if not queue:
            raise RuntimeError(f"ScriptedAdapter {self.label!r}: no scripted response left for purpose {req.purpose!r} "
                               "(mocks never improvise)")
        item = queue.pop(0)
        if callable(item):
            item = item(req)
        item = _fill_placeholders(item, req.extra)
        result = InvocationResult(outcome="ok", reported_session_id=req.session.uuid, argv=argv, exit_code=0)
        if isinstance(item, dict) and "__outcome__" in item:
            result.outcome = item["__outcome__"]
            result.error = item.get("__error__", f"scripted {item['__outcome__']}")
            result.exit_code = item.get("__exit_code__")
            if "__kill_confirmed__" in item:
                result.kill_confirmed = bool(item["__kill_confirmed__"])
        elif isinstance(item, dict) and "__raw__" in item:
            result.raw_text = item["__raw__"]
        elif isinstance(item, dict) and "__structured__" in item:
            result.structured = item["__structured__"]
            result.raw_text = json.dumps(item["__structured__"], ensure_ascii=False)
        elif isinstance(item, str):
            result.raw_text = item
        else:
            result.structured = item
            result.raw_text = json.dumps(item, ensure_ascii=False)
        if isinstance(item, dict) and item.get("__usage__"):
            usage = Usage()
            for key, value in item["__usage__"].items():
                if key in Usage.FIELDS:
                    setattr(usage, key, Measured(value, "mock"))
            result.usage = usage
        result.effective_settings = {"model": req.model, "reasoning": req.reasoning, "provider": self.name}
        self.results.append(result)
        return result


def _fill_placeholders(item: Any, extra: dict[str, Any]) -> Any:
    """Replace whole-string ``{key}`` values with ``req.extra[key]`` so a screenplay can refer to ids Alloy
    generated (finding ids, nonces) without guessing them. Anything else is returned unchanged."""
    if isinstance(item, str) and item.startswith("{") and item.endswith("}") and item[1:-1] in extra:
        return extra[item[1:-1]]
    if isinstance(item, list):
        return [_fill_placeholders(x, extra) for x in item]
    if isinstance(item, dict):
        return {k: _fill_placeholders(v, extra) for k, v in item.items()}
    return item
