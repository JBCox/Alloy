"""codex adapter (design Section 6.3; spec Appendix A, D3, R-10, R-16, R-17, R-19, R-20, R-24, R-25).

argv, from ``codex exec --help`` / ``codex exec resume --help`` 0.147.0 and the Codex docs
(learn.chatgpt.com non-interactive mode and config reference):

    codex exec --json --skip-git-repo-check -s read-only -c sandbox_mode="read-only" -c approval_policy="never"
               -C <scratch> -m <model> -c model_reasoning_effort="<level>" --output-schema <file>
               -o <last_message_file> [-i <image>]... -
    codex exec resume <uuid> --json --skip-git-repo-check -c sandbox_mode="read-only" -c approval_policy="never"
               -m <model> -c model_reasoning_effort="<level>" --output-schema <file> -o <file> [-i <image>]... -

``-`` reads the whole prompt from stdin. ``resume`` takes the UUID Alloy recorded from ``thread.started``;
``--last`` is never used. ``codex exec resume`` offers no ``-s``/``-C``, so the sandbox policy also travels as
the documented config key ``sandbox_mode`` on every invocation. Reasoning levels documented for
``model_reasoning_effort``: minimal, low, medium, high, xhigh ("xhigh is model-dependent"); the adapter
probes ``xhigh`` and falls to ``high`` once, visibly (R-20). Read at runtime from the JSONL events:
``thread.started.thread_id``, ``turn.completed.usage`` (``input_tokens``, ``cached_input_tokens``,
``output_tokens``, ``reasoning_output_tokens``), ``turn.failed``, ``error``, ``item.completed`` agent messages.
Cost is never reported by codex: UNKNOWN.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import CAPABILITIES, InvocationRequest, InvocationResult
from .cli_common import (
    OUTCOME_PROVIDER_ERROR,
    OUTCOME_UNPARSABLE,
    CliAdapter,
    jsonl_events,
    read_text,
    strict_schema,
    strip_optional_nulls,
    usage_from,
)
from .process import ProcessResult

SANDBOX = "read-only"
USAGE_FIELDS = {"input": "input_tokens", "cached_input": "cached_input_tokens", "output": "output_tokens",
                "reasoning": "reasoning_output_tokens"}


class CodexAdapter(CliAdapter):
    name = "codex"
    executable = "codex"
    supports_native_schema = True
    HELP_COMMANDS = [["exec", "--help"], ["exec", "resume", "--help"]]
    REASONING_FALLBACKS = {"xhigh": "high"}

    def declared_capabilities(self) -> dict[str, str]:
        caps = {cap: "yes" for cap in CAPABILITIES}
        caps["cost_cap"] = "no"
        return caps

    def local_requirements(self) -> dict[str, list[str]]:
        return {
            "image_reading": ["-i"],
            "evidence_dir_access": ["-i", "-C"],
            "read_only_enforcement": ["-s=read-only", "-c"],
            "session_create": ["--json"],
            "session_resume": ["word:resume"],
            "structured_output": ["--output-schema", "-o"],
            "model_settings_applied": ["-m", "-c"],
            "usage_reporting": ["--json"],
            "cancellation": [],
            "framing": ["--skip-git-repo-check"],
        }

    def settings_signature(self) -> dict[str, Any]:
        return {"sandbox": SANDBOX, "approval_policy": "never", "json_events": True, "prompt_via": "stdin",
                "reasoning_fallbacks": dict(self.reasoning_fallbacks)}

    @staticmethod
    def _files(req: InvocationRequest) -> tuple[Path, Path]:
        """Paths only; ``build_argv`` stays pure. ``prepare`` creates them before the process starts."""
        d = Path(req.packet_dir) / "invocation"
        return d / "output_schema.json", d / "last_message.txt"

    def prepare(self, req: InvocationRequest) -> None:
        self._invocation_dir(req)
        schema_file, last = self._files(req)
        with open(schema_file, "w", encoding="utf-8") as f:
            # the provider gets the strict form (observed requirement, codex 0.153.1: additionalProperties false and
            # every property required); Alloy validates the reply against its own schema after nulls are stripped
            json.dump(strict_schema(req.output_schema), f, ensure_ascii=False, indent=1)
        if last.exists():
            last.unlink()     # a previous attempt's message must never be read as this attempt's result

    def build_argv(self, req: InvocationRequest) -> list[str]:
        schema_file, last = self._files(req)
        common = ["--json", "--skip-git-repo-check", "-c", f'sandbox_mode="{SANDBOX}"', "-c", 'approval_policy="never"']
        if req.session.kind == "resume":
            argv = [*self.head(), "exec", "resume", req.session.uuid, *common]
        else:
            argv = [*self.head(), "exec", *common, "-s", SANDBOX, "-C", str(req.cwd)]
        argv += ["-m", req.model]
        if req.reasoning:
            argv += ["-c", f'model_reasoning_effort="{req.reasoning}"']
        argv += ["--output-schema", str(schema_file), "-o", str(last)]
        for img in req.images:
            argv += ["-i", str(img)]
        argv.append("-")
        return argv

    def parse_result(self, req: InvocationRequest, proc: ProcessResult) -> InvocationResult:
        stdout = read_text(proc.stdout_path)
        events = jsonl_events(stdout)
        effective: dict[str, Any] = {"sandbox": SANDBOX, "approval_policy": "never",
                                     "reasoning_reported": "read from stderr header when present",
                                     "stderr_head": read_text(proc.stderr_path)[:600]}
        if not events:
            err = (proc.stderr_tail or stdout).strip()[-800:] or "no output"
            return self._failure(req, proc, OUTCOME_UNPARSABLE, f"codex produced no JSONL events: {err}", **effective)
        thread_id: str | None = None
        errors: list[str] = []
        messages: list[str] = []
        item_errors: list[str] = []
        usage_raw: dict[str, Any] | None = None
        turn_failed = False
        for ev in events:
            kind = ev.get("type")
            if kind == "thread.started" and isinstance(ev.get("thread_id"), str):
                thread_id = ev["thread_id"]
            elif kind == "turn.completed" and isinstance(ev.get("usage"), dict):
                usage_raw = ev["usage"]
            elif kind == "turn.failed":
                e = ev.get("error")
                errors.append(str(e.get("message") if isinstance(e, dict) else e))
                turn_failed = True
            elif kind == "error":
                errors.append(str(ev.get("message") or ev))
            elif kind == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    messages.append(item["text"])
                elif item.get("type") == "error" and item.get("message"):
                    item_errors.append(str(item["message"]))     # e.g. "Model metadata ... not found"; kept as a warning
        effective["event_types"] = sorted({str(e.get("type")) for e in events})
        stream_notices: list[str] = []
        if errors and usage_raw is not None and not turn_failed:
            # Observed live (2026-09-08): the CLI's stream dropped and it printed `error` events ("Reconnecting... n/5
            # ... websocket closed by server") before completing the turn with a full reply and usage. A completed
            # turn is the reply; the notices are reported as warnings, never as a failure that discards the reply.
            stream_notices, errors = list(dict.fromkeys(errors)), []
        if errors:
            unique = list(dict.fromkeys(errors))    # the CLI repeats an `error` event; report each text once
            res = self._failure(req, proc, OUTCOME_PROVIDER_ERROR, "; ".join(unique), **effective)
            res.reported_session_id = thread_id
            return res
        if proc.outcome != "ok":
            return self._failure(req, proc, "nonzero_exit", f"codex exited {proc.exit_code}: {proc.stderr_tail.strip()[-500:]}",
                                 **effective)
        _, last = self._files(req)
        text = read_text(last) if last.is_file() else (messages[-1] if messages else "")
        structured: dict[str, Any] | None = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                structured = strip_optional_nulls(parsed, req.output_schema)
        except ValueError:
            structured = None
        usage, unmapped = usage_from(usage_raw, USAGE_FIELDS, "codex:turn.completed.usage")
        effective.update({"model_reported": "not reported in JSONL events", "usage_raw": usage_raw,
                          "usage_unmapped_fields": unmapped, "last_message_file": str(last) if last.is_file() else None})
        for line in effective["stderr_head"].splitlines():
            if line.lower().startswith("reasoning effort:"):
                effective["reasoning_reported"] = line.split(":", 1)[1].strip()
            if line.lower().startswith("model:"):
                effective["model_reported"] = line.split(":", 1)[1].strip()
        res = InvocationResult(outcome="ok", exit_code=proc.exit_code, structured=structured, raw_text=text,
                               reported_session_id=thread_id, usage=usage, argv=list(proc.argv), effective_settings=effective)
        if req.session.kind == "resume" and thread_id and thread_id != req.session.uuid:
            res.warnings.append(f"codex resumed thread {thread_id}, not the requested {req.session.uuid}")
        res.warnings.extend(f"codex stream notice (turn completed afterwards): {m}" for m in stream_notices)
        res.warnings.extend(f"codex item error: {m}" for m in item_errors)
        return res
