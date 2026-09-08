"""gemini adapter (design Section 6.3; spec Appendix A, D3, R-10, R-16, R-17, R-19, R-24, R-25).

argv, from ``gemini --help`` 0.55.1 and geminicli.com/docs/cli/headless + cli-reference:

    gemini -p "<short instruction>" -o json -m <model> --approval-mode plan
           --include-directories <packet_dir> --skip-trust (--session-id <uuid> | --resume <uuid>)

``-p`` text is "appended to input on stdin (if any)", so the packet prompt (framing plus task) travels on
stdin and ``-p`` carries only a short pointer. ``--approval-mode plan`` is the documented read-only mode.
``--skip-trust`` trusts the workspace (the scratch directory Alloy controls) for this session, so the
read-only tools are available headlessly. ``--resume`` is documented for ``latest`` or an index only;
resuming by the UUID Alloy chose is undocumented and is therefore a *probed* capability: when the live
session probe fails, the engine reconstructs context from records for every invocation (R-10, R-79).
No reasoning-effort flag exists ("not exposed by CLI"). No native schema flag: JSON is extracted from
``response`` (R-24). The ``stats`` object is kept raw; no field name inside it is documented, so no usage
value is mapped (R-19, R-25) until the field names have been read from real output.
"""
from __future__ import annotations

from typing import Any

from .base import CAPABILITIES, InvocationRequest, InvocationResult
from .cli_common import OUTCOME_PROVIDER_ERROR, OUTCOME_UNPARSABLE, CliAdapter, last_json_object, read_text, usage_from
from .process import ProcessResult

APPROVAL_MODE = "plan"
INSTRUCTION = ("The complete task (working agreement, objective, evidence index, output contract) is the text above, "
               "read from standard input. Follow it and reply with exactly one JSON object that satisfies the schema it "
               "names. No prose before or after the JSON.")
USAGE_FIELDS: dict[str, str] = {}     # nothing sourced: geminicli.com documents `stats` without naming its fields


class GeminiAdapter(CliAdapter):
    name = "gemini"
    executable = "gemini"
    supports_native_schema = False
    HELP_COMMANDS = [["--help"]]
    REASONING_FALLBACKS: dict[str, str] = {}

    def declared_capabilities(self) -> dict[str, str]:
        caps = {cap: "yes" for cap in CAPABILITIES}
        caps["image_reading"] = "probe"          # multimodal file reading: the R-16 probe decides
        caps["session_resume"] = "probe"         # --resume <uuid> undocumented: probed; else context reconstruction
        caps["usage_reporting"] = "probe"        # `stats` present per docs, field names unknown until observed
        caps["cost_cap"] = "no"
        return caps

    def local_requirements(self) -> dict[str, list[str]]:
        return {
            "image_reading": ["--include-directories", "-p"],
            "evidence_dir_access": ["--include-directories"],
            "read_only_enforcement": ["--approval-mode=plan"],
            "session_create": ["--session-id"],
            "session_resume": ["--resume"],
            "structured_output": ["--output-format=json"],
            "model_settings_applied": ["-m"],
            "usage_reporting": ["--output-format=json"],
            "cancellation": [],
            "framing": ["--skip-trust"],
        }

    def settings_signature(self) -> dict[str, Any]:
        return {"approval_mode": APPROVAL_MODE, "output_format": "json", "skip_trust": True, "prompt_via": "stdin",
                "reasoning": "not exposed by CLI"}

    def build_argv(self, req: InvocationRequest) -> list[str]:
        argv = [*self.head(), "-p", INSTRUCTION, "-o", "json"]
        if req.model:
            argv += ["-m", req.model]       # empty model = the CLI's own default; the resolved name is read from `stats`
        argv += ["--approval-mode", APPROVAL_MODE, "--include-directories", str(req.packet_dir), "--skip-trust"]
        if req.session.kind == "resume":
            argv += ["--resume", req.session.uuid]
        else:
            argv += ["--session-id", req.session.uuid]
        return argv

    def parse_result(self, req: InvocationRequest, proc: ProcessResult) -> InvocationResult:
        stdout = read_text(proc.stdout_path)
        obj = last_json_object(stdout)
        effective = {"approval_mode": APPROVAL_MODE, "reasoning_reported": "not exposed by CLI"}
        if obj is None:
            err = (proc.stderr_tail or stdout).strip()[-800:] or "no output"
            return self._failure(req, proc, OUTCOME_UNPARSABLE, f"gemini produced no JSON result: {err}", **effective)
        error = obj.get("error")
        if error:
            text = error.get("message") if isinstance(error, dict) else str(error)
            res = self._failure(req, proc, OUTCOME_PROVIDER_ERROR, f"{text} ({error})" if isinstance(error, dict) else str(error),
                                **effective)
            return res
        if proc.outcome != "ok":
            return self._failure(req, proc, "nonzero_exit", f"gemini exited {proc.exit_code}: {proc.stderr_tail.strip()[-500:]}",
                                 **effective)
        stats = obj.get("stats") if isinstance(obj.get("stats"), dict) else None
        usage, unmapped = usage_from(stats, USAGE_FIELDS, "gemini:stats")
        models = stats.get("models") if stats and isinstance(stats.get("models"), dict) else None
        effective.update({"model_reported": sorted(models) if models else "not reported", "stats_raw": stats,
                          "usage_unmapped_fields": unmapped,
                          "usage_note": "no gemini stats field is mapped: names are not documented (R-19); raw stats kept"})
        response = obj.get("response")
        return InvocationResult(outcome="ok", exit_code=proc.exit_code, structured=None,
                               raw_text=response if isinstance(response, str) else "", reported_session_id=None,
                               usage=usage, argv=list(proc.argv), effective_settings=effective)
