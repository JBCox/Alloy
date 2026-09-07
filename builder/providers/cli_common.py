"""Shared machinery for the real CLI adapters (spec D3, R-15, R-18, R-19, R-20, R-22, R-25).

* ``resolve_cli`` finds the executable. npm installs (gemini, codex) put a ``.cmd`` shim on PATH that
  re-enters ``cmd.exe``; the shim is parsed and the CLI is run as ``node.exe <bundle.js>`` directly, so
  every argument reaches the CLI through CreateProcess unchanged (no shell quoting layer).
* ``preflight_local`` runs ``--version`` and ``--help`` (free), hashes the help text, and checks that every
  flag the adapter will pass is listed (R-15 local tier, R-19: flags come from ``--help``).
* ``CliAdapter.invoke`` adds the one reasoning-level probe the design allows (R-20): a level the CLI
  rejects is retried once with the documented next level, the rejection text is kept, the effective value
  is reported in every result, and the accepted value is reused so nothing is probed twice.
* ``usage_from`` maps only fields that are actually present in the CLI's own output to ``Measured``;
  everything else stays ``UNKNOWN`` (R-25).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..records import UNKNOWN, Measured
from .base import InvocationRequest, InvocationResult, ProviderAdapter, Usage
from .process import ProcessResult, run_process

OUTCOME_PROVIDER_ERROR = "provider_error"       # the CLI ran and reported a failure of its own
OUTCOME_UNPARSABLE = "unparsable_output"        # the CLI produced nothing the adapter can read
OUTCOME_BUDGET = "budget_exhausted"             # the provider's own per-invocation cap stopped the call (after the spend)
TRANSPORT_FAILURES = ("timeout", "inactive", "cancelled", "spawn_failed", "nonzero_exit", "crashed",
                      OUTCOME_PROVIDER_ERROR, OUTCOME_UNPARSABLE, OUTCOME_BUDGET)

_NPM_SHIM_RE = re.compile(r'"%dp0%\\([^"]+\.js)"')
_FLAG_RE = re.compile(r"(?<![\w-])(-{1,2}[A-Za-z][\w-]*)")


@dataclass
class ResolvedCli:
    name: str
    path: str | None
    argv_head: list[str]
    via: str                 # executable | npm_shim | injected | missing
    script: str | None = None
    node: str | None = None

    @property
    def found(self) -> bool:
        return bool(self.argv_head)


def resolve_cli(name: str, configured: str = "") -> ResolvedCli:
    """Configured path first (must exist; PATH is not searched when a configured path is missing), else PATH.
    A npm ``.cmd`` shim is resolved to ``node.exe`` plus the bundle it names."""
    if configured:
        path = configured if Path(configured).is_file() else None
    else:
        path = shutil.which(name)
    if path is None:
        return ResolvedCli(name, None, [], "missing")
    p = Path(path)
    if p.suffix.lower() in (".cmd", ".bat"):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = _NPM_SHIM_RE.search(text)
        if m:
            script = p.parent / m.group(1)
            node = p.parent / "node.exe"
            node_path = str(node) if node.is_file() else shutil.which("node")
            if script.is_file() and node_path:
                return ResolvedCli(name, str(p), [node_path, str(script)], "npm_shim", script=str(script), node=node_path)
    return ResolvedCli(name, str(p), [str(p)], "executable")


def parse_help_flags(text: str) -> set[str]:
    return set(_FLAG_RE.findall(text or ""))


def help_segment(text: str, flag: str) -> str:
    """The help text from ``flag``'s definition up to the next flag definition line."""
    lines = (text or "").splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(r"^\s*(-\w,\s*)?" + re.escape(flag) + r"(\b|[ ,=<])", ln)), None)
    if start is None:
        return ""
    out = [lines[start]]
    for ln in lines[start + 1:]:
        if re.match(r"^\s{0,8}-{1,2}[A-Za-z]", ln):
            break
        out.append(ln)
    return "\n".join(out)


def requirement_met(text: str, token: str) -> bool:
    """``--flag`` must be listed; ``--flag=choice`` also needs ``choice`` in that flag's help segment;
    ``word:name`` needs ``name`` as a word (a subcommand) anywhere in the help."""
    if token.startswith("word:"):
        return re.search(r"\b" + re.escape(token[5:]) + r"\b", text or "") is not None
    flag, _, choice = token.partition("=")
    if flag not in parse_help_flags(text):
        return False
    if not choice:
        return True
    return re.search(r"\b" + re.escape(choice) + r"\b", help_segment(text, flag) or (text or "")) is not None


def run_capture(argv: list[str], *, cwd: str | Path | None = None, timeout_s: float = 60.0) -> tuple[str, int | None, str, str]:
    """Run a free, read-only CLI command (``--version``, ``--help``) and return (outcome, rc, stdout, stderr)."""
    tmp = Path(tempfile.mkdtemp(prefix="alloy-cli-"))
    try:
        pr = run_process(argv, cwd=cwd or tmp, stdout_path=tmp / "out.log", stderr_path=tmp / "err.log",
                         response_s=timeout_s, inactivity_s=None)
        out = Path(pr.stdout_path).read_text(encoding="utf-8", errors="replace")
        err = Path(pr.stderr_path).read_text(encoding="utf-8", errors="replace")
        return pr.outcome, pr.exit_code, out, err
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def usage_from(raw: Any, mapping: dict[str, str], source: str) -> tuple[Usage, list[str]]:
    """``mapping``: Usage field -> key in ``raw``. Only present numeric keys become Measured; the rest stay
    UNKNOWN. Returns the usage and the raw keys that were not mapped (kept visible, never invented)."""
    usage = Usage()
    unmapped: list[str] = []
    if not isinstance(raw, dict):
        return usage, unmapped
    used = set()
    for field, key in mapping.items():
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            setattr(usage, field, Measured(value, f"{source}.{key}"))
            used.add(key)
    unmapped = [k for k in raw if k not in used]
    return usage, unmapped


def strict_schema(schema: Any) -> Any:
    """OpenAI structured-output ("strict") form of an Alloy schema, observed live from codex 0.153.1:
    ``additionalProperties`` must be supplied and false on every object, and every property must be listed in
    ``required``. Optional properties therefore become nullable (``null`` added to their type or enum) and
    required; ``strip_optional_nulls`` removes those nulls again before Alloy validates against its own
    schema. ``minItems`` is dropped for the provider copy (Alloy still enforces it locally)."""
    if isinstance(schema, list):
        return [strict_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "minItems":
            continue
        out[key] = value
    props = schema.get("properties")
    if isinstance(props, dict) and (schema.get("type") == "object" or "properties" in schema):
        required = set(schema.get("required") or [])
        new_props: dict[str, Any] = {}
        for name, sub in props.items():
            sub = strict_schema(sub)
            if name not in required:
                sub = _nullable(sub)
            new_props[name] = sub
        out["properties"] = new_props
        out["required"] = list(props.keys())
        out["additionalProperties"] = False
    if isinstance(schema.get("items"), dict):
        out["items"] = strict_schema(schema["items"])
    return out


def _nullable(sub: Any) -> Any:
    if not isinstance(sub, dict):
        return sub
    sub = dict(sub)
    if "enum" in sub and None not in sub["enum"]:
        sub["enum"] = list(sub["enum"]) + [None]
    t = sub.get("type")
    if t is None:
        sub["type"] = ["object", "null"] if "properties" in sub else ["string", "null"]
    elif isinstance(t, list):
        if "null" not in t:
            sub["type"] = list(t) + ["null"]
    elif t != "null":
        sub["type"] = [t, "null"]
    return sub


def strip_optional_nulls(value: Any, schema: Any) -> Any:
    """Remove ``null`` values for keys the *original* schema does not require (they were nullable only for
    the provider's strict form). Required keys are left as they are so validation reports them."""
    if isinstance(schema, dict) and isinstance(value, dict):
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        out = {}
        for k, v in value.items():
            if v is None and k in props and k not in required:
                continue
            out[k] = strip_optional_nulls(v, props.get(k, {}))
        return out
    if isinstance(schema, dict) and isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [strip_optional_nulls(v, schema["items"]) for v in value]
    return value


def read_text(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def last_json_object(text: str) -> dict[str, Any] | None:
    """The whole text as a JSON object, else the last line that is one."""
    stripped = (text or "").strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def jsonl_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


class CliAdapter(ProviderAdapter):
    """Base for claude, gemini, codex. Subclasses set ``name``/``executable``, ``HELP_COMMANDS``,
    ``REASONING_FALLBACKS`` and implement ``build_argv``/``parse_result``/``local_requirements``."""

    HELP_COMMANDS: list[list[str]] = [["--help"]]
    VERSION_ARGS: list[str] = ["--version"]
    REASONING_FALLBACKS: dict[str, str] = {}
    REASONING_REJECTION_RE = re.compile(r"reasoning|effort", re.IGNORECASE)

    def __init__(self, *, executable: str = "", argv_head: list[str] | None = None, settings: dict[str, Any] | None = None,
                 reasoning_fallbacks: dict[str, str] | None = None):
        self.executable_configured = executable
        self._argv_head = [str(a) for a in argv_head] if argv_head else None
        self._resolved: ResolvedCli | None = None
        self._local: dict[str, Any] | None = None
        self.settings = dict(settings or {})
        self.reasoning_fallbacks = dict(self.REASONING_FALLBACKS if reasoning_fallbacks is None else reasoning_fallbacks)
        self.reasoning_accepted: dict[tuple[str, str], str] = {}     # (model, requested) -> accepted level
        self.fallbacks_reported: list[dict[str, str]] = []
        self._lock = threading.Lock()

    # --- resolution and local preflight ------------------------------------------------

    def resolved(self) -> ResolvedCli:
        if self._resolved is None:
            if self._argv_head:
                self._resolved = ResolvedCli(self.name, self._argv_head[0], list(self._argv_head), "injected")
            else:
                self._resolved = resolve_cli(self.executable, self.executable_configured)
        return self._resolved

    def head(self) -> list[str]:
        r = self.resolved()
        if not r.found:
            raise FileNotFoundError(f"{self.name}: executable not found (configured={self.executable_configured!r}, "
                                    f"searched PATH for {self.executable!r})")
        return list(r.argv_head)

    def local_requirements(self) -> dict[str, list[str]]:
        return {}

    def required_tokens(self) -> list[str]:
        seen: list[str] = []
        for toks in self.local_requirements().values():
            for t in toks:
                if t not in seen:
                    seen.append(t)
        return seen

    def preflight_local(self) -> dict[str, Any]:
        if self._local is not None:
            return dict(self._local)
        r = self.resolved()
        report: dict[str, Any] = {"cli_path": r.path, "found": r.found, "version": None, "help_hash": None, "flags": {},
                                  "missing_flags": [], "resolved_via": r.via, "argv_head": list(r.argv_head),
                                  "script": r.script, "errors": []}
        if not r.found:
            report["errors"].append(f"{self.name} not found")
            self._local = report
            return dict(report)
        outcome, rc, out, err = run_capture([*r.argv_head, *self.VERSION_ARGS])
        if outcome == "ok":
            report["version"] = (out.strip() or err.strip()).splitlines()[0] if (out.strip() or err.strip()) else ""
        else:
            report["errors"].append(f"--version: {outcome} rc={rc} {err.strip()[:200]}")
        help_texts: list[str] = []
        for cmd in self.HELP_COMMANDS:
            outcome, rc, out, err = run_capture([*r.argv_head, *cmd])
            if outcome != "ok":
                report["errors"].append(f"{' '.join(cmd)}: {outcome} rc={rc} {err.strip()[:200]}")
            help_texts.append(out + "\n" + err)
        combined = "\n".join(help_texts)
        report["help_hash"] = hashlib.sha256(combined.encode("utf-8")).hexdigest()
        report["help_bytes"] = len(combined.encode("utf-8"))
        flags: dict[str, bool] = {f: True for f in parse_help_flags(combined)}
        for tok in self.required_tokens():
            flags[tok] = requirement_met(combined, tok)
        report["flags"] = flags
        report["missing_flags"] = [t for t in self.required_tokens() if not flags.get(t)]
        self._local = report
        return dict(report)

    # --- invocation with the reasoning probe -------------------------------------------

    def effective_reasoning(self, model: str, requested: str) -> str:
        return self.reasoning_accepted.get((model, requested), requested)

    def _reasoning_rejected(self, res: InvocationResult) -> bool:
        return res.outcome in (OUTCOME_PROVIDER_ERROR, "nonzero_exit") and bool(self.REASONING_REJECTION_RE.search(res.error or ""))

    def invoke(self, req: InvocationRequest, cancel_event: threading.Event | None = None) -> InvocationResult:
        requested = req.reasoning
        level = self.effective_reasoning(req.model, requested)
        run_req = replace(req, reasoning=level) if level != req.reasoning else req
        res = super().invoke(run_req, cancel_event)
        rejection = ""
        if (not res.ok and level == requested and level in self.reasoning_fallbacks and self._reasoning_rejected(res)):
            fallback = self.reasoning_fallbacks[level]
            rejection = res.error
            note = (f"{self.name}: the CLI rejected reasoning level {level!r} for model {req.model!r} ({res.error.strip()[:300]}); "
                    f"retrying once with the documented next level {fallback!r} (R-20: reported, never silent)")
            with self._lock:
                self.reasoning_accepted[(req.model, requested)] = fallback
                self.fallbacks_reported.append({"model": req.model, "requested": level, "accepted": fallback, "error": rejection})
            level = fallback
            res = super().invoke(replace(req, reasoning=fallback), cancel_event)
            res.warnings.append(note)
        res.effective_settings.setdefault("provider", self.name)
        res.effective_settings["model_requested"] = req.model
        res.effective_settings["reasoning_requested"] = requested
        res.effective_settings["reasoning_effective"] = level if level else "not exposed by CLI"
        if rejection:
            res.effective_settings["reasoning_rejection"] = rejection
        elif (req.model, requested) in self.reasoning_accepted:
            res.effective_settings["reasoning_rejection"] = next(
                (f["error"] for f in self.fallbacks_reported if f["model"] == req.model and f["requested"] == requested), "")
        return res

    # --- helpers for subclasses ------------------------------------------------------------

    @staticmethod
    def _invocation_dir(req: InvocationRequest) -> Path:
        d = Path(req.packet_dir) / "invocation"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _failure(req: InvocationRequest, proc: ProcessResult, outcome: str, error: str, **effective: Any) -> InvocationResult:
        res = InvocationResult(outcome=outcome, exit_code=proc.exit_code, error=error.strip(), argv=list(proc.argv))
        res.effective_settings.update(effective)
        return res
