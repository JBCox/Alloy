"""Provider adapter contract, argv policy, session registry, usage, and preflight verdicts
(spec D3, R-10, R-15, R-17, R-18, R-19, R-20, R-21, R-25).
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ids import hash_json, utc_now
from ..records import UNKNOWN, Quantity, Record, quantity_from_json, quantity_to_json
from ..store import Store
from .process import ProcessResult, run_process

CAPABILITIES = ("image_reading", "evidence_dir_access", "read_only_enforcement", "session_create", "session_resume",
                "structured_output", "model_settings_applied", "usage_reporting", "cancellation")

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_FORBIDDEN_ANYWHERE = {"--continue", "--last", "--fallback-model", "--ephemeral", "--no-session-persistence", "--yolo",
                       "--bg", "--background"}
_FORBIDDEN_PREFIXES = ("--dangerously-", "--allow-dangerously-")


class PolicyViolation(Exception):
    pass


def assert_argv_policy(argv: Any) -> None:
    """Refuse shell strings and every 'most recent conversation', fallback, or bypass mechanism (D3, R-10, R-20)."""
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise PolicyViolation("argv must be a list of strings (never a shell command string)")
    exe = Path(argv[0]).name.lower()
    for i, tok in enumerate(argv):
        if tok in _FORBIDDEN_ANYWHERE or any(tok.startswith(p) for p in _FORBIDDEN_PREFIXES):
            raise PolicyViolation(f"forbidden argument {tok!r}")
        if tok in ("-c", "-r") and exe.startswith(("claude", "gemini")):
            if tok == "-c":
                raise PolicyViolation("forbidden short flag -c (continue most recent conversation)")
        if tok in ("--resume", "-r"):
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if not _UUID.match(nxt):
                raise PolicyViolation(f"--resume must name an explicit session UUID, got {nxt!r}")
        if tok == "-y" and exe.startswith("gemini"):
            raise PolicyViolation("forbidden gemini -y (yolo)")
    if "resume" in argv and exe.startswith("codex"):
        idx = argv.index("resume")
        nxt = argv[idx + 1] if idx + 1 < len(argv) else ""
        if not _UUID.match(nxt):
            raise PolicyViolation(f"codex exec resume must name an explicit session UUID, got {nxt!r}")


@dataclass(frozen=True)
class SessionRef:
    kind: str          # new | resume | isolated
    uuid: str


@dataclass
class InvocationRequest:
    agent_id: str
    session: SessionRef
    purpose: str
    packet_dir: Path
    cwd: Path
    prompt_text: str
    schema_name: str
    output_schema: dict[str, Any]
    model: str
    reasoning: str
    framing_text: str = ""
    images: list[Path] = field(default_factory=list)
    response_s: float | None = None
    inactivity_s: float | None = None
    max_cost_usd: float | None = None
    env: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    input: Quantity = UNKNOWN
    output: Quantity = UNKNOWN
    cached_input: Quantity = UNKNOWN
    image: Quantity = UNKNOWN
    reasoning: Quantity = UNKNOWN
    cost_usd: Quantity = UNKNOWN

    FIELDS = ("input", "output", "cached_input", "image", "reasoning", "cost_usd")

    def to_json(self) -> dict[str, Any]:
        return {name: quantity_to_json(getattr(self, name)) for name in self.FIELDS}

    @classmethod
    def from_json(cls, d: dict[str, Any] | None) -> "Usage":
        d = d or {}
        return cls(**{name: quantity_from_json(d.get(name)) for name in cls.FIELDS})


@dataclass
class InvocationResult:
    outcome: str                      # ok | malformed | timeout | inactive | cancelled | nonzero_exit | spawn_failed | crashed
                                      # | provider_error (the CLI reported its own failure) | unparsable_output
    exit_code: int | None = None
    structured: dict[str, Any] | None = None
    raw_text: str = ""
    reported_session_id: str | None = None
    usage: Usage = field(default_factory=Usage)
    elapsed_s: float = 0.0
    stdout_path: str = ""
    stderr_path: str = ""
    argv: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    kill_confirmed: bool | None = None
    effective_settings: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


class ProviderAdapter(ABC):
    name: str = "provider"
    supports_native_schema: bool = False
    executable: str = ""

    def declared_capabilities(self) -> dict[str, str]:
        return {cap: "no" for cap in CAPABILITIES}

    @abstractmethod
    def build_argv(self, req: InvocationRequest) -> list[str]: ...

    def stdin_payload(self, req: InvocationRequest) -> bytes:
        text = (req.framing_text + "\n\n" if req.framing_text else "") + req.prompt_text
        return text.encode("utf-8")

    @abstractmethod
    def parse_result(self, req: InvocationRequest, proc: ProcessResult) -> InvocationResult: ...

    def preflight_local(self) -> dict[str, Any]:
        path = shutil.which(self.executable) if self.executable else None
        return {"cli_path": path, "found": path is not None, "version": None, "help_hash": None, "flags": {},
                "missing_flags": []}

    def local_requirements(self) -> dict[str, list[str]]:
        """Per capability, the ``--help`` tokens the adapter relies on (R-15 local tier). Empty for mocks."""
        return {}

    def settings_signature(self) -> dict[str, Any]:
        """Fixed argv settings that must invalidate the preflight cache when they change (R-18)."""
        return {}

    def prepare(self, req: InvocationRequest) -> None:
        """Write any files the argv refers to (schema files, output paths) before the process starts."""

    def invoke(self, req: InvocationRequest, cancel_event: threading.Event | None = None) -> InvocationResult:
        self.prepare(req)
        argv = self.build_argv(req)
        assert_argv_policy(argv)
        logs = Path(req.packet_dir) / "invocation"
        logs.mkdir(parents=True, exist_ok=True)
        attempt = len(list(logs.glob("stdout.*.log"))) + 1
        proc = run_process(argv, cwd=req.cwd, stdout_path=logs / f"stdout.{attempt}.log",
                           stderr_path=logs / f"stderr.{attempt}.log", stdin_bytes=self.stdin_payload(req),
                           env_extra=req.env or None, response_s=req.response_s, inactivity_s=req.inactivity_s,
                           cancel_event=cancel_event)
        if proc.outcome in ("timeout", "inactive", "cancelled", "spawn_failed"):
            return InvocationResult(outcome=proc.outcome, exit_code=proc.exit_code, elapsed_s=proc.elapsed_s,
                                    stdout_path=proc.stdout_path, stderr_path=proc.stderr_path, argv=argv,
                                    error=proc.error or proc.stderr_tail[-500:], kill_confirmed=proc.kill_confirmed)
        result = self.parse_result(req, proc)
        result.argv = argv
        result.elapsed_s = proc.elapsed_s
        result.stdout_path, result.stderr_path = proc.stdout_path, proc.stderr_path
        result.exit_code = proc.exit_code
        return result


class SessionRegistry:
    """Explicit provider sessions per agent (R-10): never 'most recent', always a UUID Alloy chose."""

    def __init__(self, store: Store):
        self.store = store

    def persistent(self, agent_id: str, *, provider: str, actor: str = "engine") -> Record:
        for s in self.store.list("provider_session", parent_id=agent_id):
            if s.data.get("kind") == "persistent" and s.data.get("provider") == provider:
                return s
        return self._create(agent_id, provider, "persistent", actor, reason="persistent agent session")

    def isolated(self, agent_id: str, *, provider: str, reason: str, actor: str = "engine") -> Record:
        return self._create(agent_id, provider, "isolated_review", actor, reason=reason)

    def _create(self, agent_id: str, provider: str, kind: str, actor: str, *, reason: str) -> Record:
        existing = {s.data.get("session_uuid") for s in self.store.list("provider_session")}
        session_uuid = str(uuid.uuid4())
        while session_uuid in existing:  # pragma: no cover - astronomically unlikely
            session_uuid = str(uuid.uuid4())
        rec = Record.new("provider_session", {
            "agent_id": agent_id, "session_uuid": session_uuid, "kind": kind, "provider": provider,
            "invocation_count": 0, "last_journal_seq": None, "last_used_at": None, "reported_session_id": None,
            "reason": reason, "created_at": utc_now(),
        }, parent_id=agent_id)
        return self.store.upsert(rec, actor=actor, event="provider_session.created", inputs={"kind": kind, "reason": reason})

    @staticmethod
    def ref_for(session: Record, *, first_use: bool) -> SessionRef:
        """New sessions use the UUID Alloy chose. Resumes use the id the provider reported for that session when
        it reported one (codex assigns its own thread id; claude echoes ours), else Alloy's UUID."""
        if first_use or int(session.data.get("invocation_count") or 0) == 0:
            return SessionRef(kind="new", uuid=session.data["session_uuid"])
        reported = session.data.get("reported_session_id")
        return SessionRef(kind="resume", uuid=reported if isinstance(reported, str) and _UUID.match(reported)
                          else session.data["session_uuid"])

    def mark_used(self, session: Record, *, journal_seq: int | None = None, reported_session_id: str | None = None,
                  actor: str = "engine") -> Record:
        session.data["invocation_count"] = int(session.data.get("invocation_count") or 0) + 1
        session.data["last_used_at"] = utc_now()
        if journal_seq is not None:
            session.data["last_journal_seq"] = journal_seq
        if reported_session_id is not None:
            session.data["reported_session_id"] = reported_session_id
        return self.store.upsert(session, actor=actor, event="provider_session.used")


def preflight_cache_key(*, cli_path: str, version: str, model: str, settings: dict[str, Any]) -> str:
    return hash_json({"cli_path": cli_path, "version": version, "model": model, "settings": settings})


def preflight_verdict(report: dict[str, dict[str, Any]], required: list[str], *, require_live: bool = True,
                      shared_write: bool = False) -> tuple[bool, list[str]]:
    """Block with a specific explanation and remediation for each missing required capability (R-21)."""
    blockers: list[str] = []
    for cap in required:
        entry = report.get(cap) or {"declared": "no", "local": "unknown", "live": "not_run"}
        declared, local, live = entry.get("declared", "no"), entry.get("local", "unknown"), entry.get("live", "not_run")
        evidence = entry.get("evidence")
        if cap == "read_only_enforcement" and declared == "prompt_only" and shared_write:
            blockers.append(f"{cap}: the CLI can only enforce read-only by prompt; this configuration is unsupported "
                            "for shared write (R-17). Remediation: use a provider mode that enforces read-only "
                            "(claude --tools Read,Glob,Grep; codex -s read-only) or run single-owner only.")
            continue
        if declared == "no":
            blockers.append(f"{cap}: not declared for this provider. Remediation: choose a provider or CLI mode that "
                            f"supports {cap}, or update the adapter capability table with evidence.")
            continue
        if local != "ok":
            blockers.append(f"{cap}: local check {local}. Remediation: install or upgrade the CLI so the required flags "
                            "appear in --help, then re-run preflight.")
            continue
        if live == "failed":
            blockers.append(f"{cap}: live probe failed ({evidence or 'no evidence recorded'}). Remediation: fix the "
                            "provider configuration and re-run `preflight --live`.")
            continue
        if require_live and live != "verified":
            blockers.append(f"{cap}: live verification not run (status {live}). Remediation: run `preflight --live` "
                            "(spends provider usage) before starting.")
    return (not blockers), blockers


def report_json(result: InvocationResult) -> dict[str, Any]:
    return {"outcome": result.outcome, "exit_code": result.exit_code, "structured": result.structured,
            "raw_text": result.raw_text[:2000], "reported_session_id": result.reported_session_id,
            "usage": result.usage.to_json(), "elapsed_s": result.elapsed_s, "argv": result.argv,
            "warnings": result.warnings, "error": result.error, "kill_confirmed": result.kill_confirmed,
            "effective_settings": result.effective_settings, "stdout_path": result.stdout_path,
            "stderr_path": result.stderr_path}


def dump_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)
