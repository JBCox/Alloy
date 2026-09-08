"""Model Builder host: one live BuilderSession for the app, its event pump, and the settings file.

Standalone-style like outcome.py and plan_limits.py: imports builder.* only (never relay or app), no public
method raises (every failure is a {"error": sentence} dict, every refusal {"ok": False, "reason": sentence}),
and paths are joined at call time. The session's lifetime is the process's, not the view's: hiding the Model
Builder view never cancels a run. Only `shutdown()` (app exit) closes the session, and closing cancels.

Threads: the app's bridge threads call the public methods, which only enqueue jobs on the session's worker
(or flip its thread-safe flags); the worker publishes tuples on `session.events`; ONE pump thread per session
turns those into `emit("builder", payload)` calls. The bridge thread never computes a snapshot: the last one
the worker published is the read model, cached here.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import math
import os
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

try:
    from builder.config import (ASSIGNABLE_ROLES, BuilderConfig, default_workflow_dir, discover_blender,
                                read_settings_file, slugify)
    from builder.project import Project
    from builder.providers import SUPPORTED_PROVIDERS
    from builder.viewmodel import BuilderSession, overlay_rects as _overlay_rects
    IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - Pillow or the package missing: the chat app must still boot
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

SETTINGS_FILE = "builder.json"
EVENT = "builder"                 # the one uiEvent name; payload["kind"] discriminates
HUMAN = "Josh"                    # journal actors read user:Josh, the app's name for the human
RECENT_MAX = 12
RUN_JOBS = ("start_run", "resume_run")
PROJECT_JOBS = ("open", "new_project", "new_fixture")
PROJECTS_SUBDIR = "builder-projects"   # under the settings directory when nothing else says where a project goes
_KEEP = object()


class Refusal(Exception):
    """A verb that cannot run right now; reported as {"ok": False, "reason": sentence}."""


# --- pure helpers ------------------------------------------------------------------------------------------------

def sentence(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def json_safe(obj: Any) -> Any:
    """Plain JSON types only: the app's emitter does a bare json.dumps and a TypeError there would kill the pump."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json_safe(dataclasses.asdict(obj))
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")
    return str(obj)


def deep_merge(base: dict, patch: dict, replace_keys: tuple[str, ...] = ("presets",)) -> dict:
    """A new mapping: dict-in-dict merges, lists and scalars replace, and the sections in ``replace_keys`` replace
    as a whole (a deleted preset stays deleted)."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    for k, v in patch.items():
        if k in replace_keys:
            out[k] = json.loads(json.dumps(v)) if isinstance(v, dict) else v
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v, ())
        else:
            out[k] = v
    return out


def empty_settings() -> dict:
    return {"version": 1, "builder": {}, "recent": []}


def read_settings(path: str | os.PathLike) -> dict:
    """Never raises: a missing file is the empty document; a malformed one is the empty document plus ``_error``."""
    doc = empty_settings()
    try:
        if not os.path.isfile(path):
            return doc
        data = read_settings_file(path) if not IMPORT_ERROR else json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        doc["_error"] = f"{path}: could not be read ({sentence(exc)}); using defaults"
        return doc
    if not isinstance(data, dict):
        doc["_error"] = f"{path}: expected a mapping at the top level; using defaults"
        return doc
    section = data.get("builder")
    doc["builder"] = section if isinstance(section, dict) else {}
    recent = data.get("recent")
    doc["recent"] = [r for r in recent if isinstance(r, dict) and r.get("workflow_dir")] if isinstance(recent, list) else []
    doc["version"] = data.get("version", 1)
    for k, v in data.items():
        if k not in doc:
            doc[k] = v                       # a host may keep other keys beside ours; they survive a save
    return doc


def write_settings(path: str | os.PathLike, doc: dict) -> None:
    """tmp + os.replace, retried briefly: on Windows a concurrent reader without FILE_SHARE_DELETE blocks the
    rename for a moment (the same rule as relay's atomic writes)."""
    path = os.fspath(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {k: v for k, v in doc.items() if not k.startswith("_")}
    tmp = f"{path}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    for delay in (0.0, 0.05, 0.15, 0.3):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(tmp, path)


def touch_recent(doc: dict, workflow_dir: str, name: str | None, now: str | None = None) -> dict:
    """Most recent first, de-duplicated by normalised path, capped at RECENT_MAX."""
    key = os.path.normcase(os.path.normpath(workflow_dir))
    kept = [r for r in doc.get("recent", []) if os.path.normcase(os.path.normpath(str(r.get("workflow_dir", "")))) != key]
    entry = {"workflow_dir": workflow_dir, "name": name,
             "opened_at": now or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    doc["recent"] = [entry, *kept][:RECENT_MAX]
    return doc


def _confine(root: str, path: str) -> str | None:
    """Fallback containment (the app injects relay.confine_to_workspace instead): the real path of ``path``,
    resolved against ``root``, only when it stays inside ``root``."""
    try:
        root_r = os.path.realpath(root)
        cand = os.path.realpath(os.path.join(root_r, path))
        if os.path.commonpath([os.path.normcase(root_r), os.path.normcase(cand)]) != os.path.normcase(root_r):
            return None
        return cand
    except (ValueError, OSError):
        return None


# --- the host ---------------------------------------------------------------------------------------------------

class BuilderHost:
    def __init__(self, *, settings_path: Callable[[], str], emit: Callable[[str, dict], Any],
                 confine: Callable[[str, str], str | None] | None = None,
                 session_factory: Callable[[Any, Any], Any] | None = None):
        self._settings_path = settings_path
        self._emit = emit
        self._confine = confine or _confine
        self._session_factory = session_factory or (lambda cfg, mock: BuilderSession(cfg, mock=mock))
        self._lock = threading.RLock()
        self._session: Any = None
        self._pump: threading.Thread | None = None
        self._snapshot: dict | None = None
        self._workflow_dir: str | None = None
        self._busy_job: str | None = None
        self._last_error: str | None = None
        self._needs_recovery = False
        self._seq = 0

    # -- settings ----------------------------------------------------------------------------------------------
    def settings_path(self) -> str:
        return os.fspath(self._settings_path())

    def _doc(self) -> dict:
        return read_settings(self.settings_path())

    def config(self) -> Any:
        return BuilderConfig.from_dict({"builder": self._doc().get("builder", {})})

    def get_settings(self) -> dict:
        if IMPORT_ERROR:
            return self._unavailable()
        try:
            doc = self._doc()
            cfg = BuilderConfig.from_dict({"builder": doc.get("builder", {})})
            warnings = list(cfg.warnings)
            if doc.get("_error"):
                warnings.insert(0, doc["_error"])
            found = discover_blender(cfg.blender_executable)
            return {"ok": True, "path": self.settings_path(), "builder": doc.get("builder", {}),
                    "config": json_safe(cfg.to_dict()), "warnings": warnings,
                    "blender_found": str(found) if found else None, "seats": sorted(cfg.agents),
                    "providers": list(SUPPORTED_PROVIDERS), "assignable_roles": list(ASSIGNABLE_ROLES),
                    "presets": sorted(cfg.presets)}
        except Exception as exc:  # noqa: BLE001
            return {"error": sentence(exc)}

    def save_settings(self, patch: dict | None) -> dict:
        if IMPORT_ERROR:
            return self._unavailable()
        try:
            if not isinstance(patch, dict):
                return {"ok": False, "reason": "settings must be a mapping of builder keys"}
            doc = self._doc()
            doc.pop("_error", None)
            doc["builder"] = deep_merge(doc.get("builder", {}), patch)
            cfg = BuilderConfig.from_dict({"builder": doc["builder"]})     # validates; warnings come back
            write_settings(self.settings_path(), doc)
            with self._lock:
                if self._session is not None:
                    self._session.config = cfg          # the next open uses it; live limits go through set_limits
            return {"ok": True, "warnings": list(cfg.warnings), "path": self.settings_path()}
        except Exception as exc:  # noqa: BLE001
            return {"error": sentence(exc)}

    def recent(self) -> dict:
        try:
            out = []
            for r in self._doc().get("recent", []):
                wf = str(r.get("workflow_dir", ""))
                out.append({"workflow_dir": wf, "name": r.get("name"), "opened_at": r.get("opened_at"),
                            "exists": bool(wf) and (Project.exists(wf) if not IMPORT_ERROR else os.path.isdir(wf))})
            return {"ok": True, "recent": out}
        except Exception as exc:  # noqa: BLE001
            return {"error": sentence(exc)}

    def _remember(self, workflow_dir: str | None, name: str | None) -> None:
        if not workflow_dir:
            return
        try:
            doc = self._doc()
            doc.pop("_error", None)
            write_settings(self.settings_path(), touch_recent(doc, workflow_dir, name))
        except Exception:  # noqa: BLE001 - bookkeeping never breaks the pump
            pass

    # -- memory-only reads (bridge-thread safe) -----------------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            session = self._session
            return {"available": not IMPORT_ERROR, "open": self._workflow_dir is not None,
                    "workflow_dir": self._workflow_dir, "busy": self._busy_job, "last_error": self._last_error,
                    "has_snapshot": self._snapshot is not None, "needs_recovery": self._needs_recovery,
                    "mock": bool(session is not None and getattr(session, "mock", None) is not None),
                    "settings_path": self.settings_path()}

    def snapshot(self) -> dict:
        with self._lock:
            return {"ok": True, "state": self.state(), "snapshot": self._snapshot}

    def overlay_rects(self, render_id: str) -> dict:
        if IMPORT_ERROR:
            return self._unavailable()
        try:
            with self._lock:
                snap = self._snapshot
            if not snap or not render_id:
                return {"ok": True, "rects": []}
            return {"ok": True, "rects": json_safe(_overlay_rects(snap, str(render_id)))}
        except Exception as exc:  # noqa: BLE001
            return {"error": sentence(exc)}

    def resolve_image(self, path: str) -> dict:
        """The real path of an artifact inside the open workflow directory; forbidden and missing are the identical
        quiet answer (the reply must not disclose whether a path outside exists)."""
        with self._lock:
            root = self._workflow_dir
        if not root or not path:
            return {"error": "not available"}
        try:
            real = self._confine(root, str(path))
        except Exception:  # noqa: BLE001
            real = None
        if not real or not os.path.isfile(real):
            return {"error": "not available"}
        return {"ok": True, "path": real}

    # -- session plumbing ----------------------------------------------------------------------------------------
    def _unavailable(self) -> dict:
        return {"error": f"The Model Builder is unavailable: {IMPORT_ERROR}"}

    def _call(self, fn: Callable[[Any], dict], *, mock: Any = _KEEP) -> dict:
        if IMPORT_ERROR:
            return self._unavailable()
        try:
            with self._lock:
                session = self._ensure_session() if mock is _KEEP else self._session_for(mock)
                return fn(session)
        except Refusal as r:
            return {"ok": False, "reason": str(r)}
        except Exception as exc:  # noqa: BLE001 - never raises across the bridge
            return {"error": sentence(exc)}

    def _alive(self) -> bool:
        return self._session is not None and not getattr(self._session, "_closed", False)

    def _ensure_session(self) -> Any:
        if not self._alive():
            self._start_session(None)
        return self._session

    def _session_for(self, mock: Any) -> Any:
        """The session whose seats match ``mock`` (None = the configured providers). ``mock`` is fixed per
        BuilderSession, so a different one means a new session, which is only allowed while nothing is running."""
        if self._alive() and getattr(self._session, "mock", None) == mock:
            return self._session
        if self._alive():
            if self._busy_job is not None:
                raise Refusal("a job is in progress; pause or cancel it first")
            self._stop_session(2.0)
        self._start_session(mock)
        return self._session

    def _start_session(self, mock: Any) -> None:
        session = self._session_factory(self.config(), mock)
        self._session = session
        self._pump = threading.Thread(target=self._pump_loop, args=(session,), name="builder-host-pump", daemon=True)
        self._pump.start()

    def _stop_session(self, timeout: float) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close(timeout=timeout)
            except Exception:  # noqa: BLE001
                pass

    def _submit(self, session: Any, job: str, call: Callable[[], Any]) -> dict:
        call()
        return {"ok": True, "job": job}

    # -- the pump -----------------------------------------------------------------------------------------------
    def _pump_loop(self, session: Any) -> None:
        while True:
            try:
                first = session.events.get(timeout=0.5)
            except queue.Empty:
                if session is not self._session and not self._alive():
                    return
                continue
            burst = [first]
            while True:
                try:
                    burst.append(session.events.get_nowait())
                except queue.Empty:
                    break
            payloads: list[dict] = []
            last_snapshot: dict | None = None
            stop = False
            with self._lock:
                for ev in burst:
                    for payload in self._translate(ev):
                        if payload["kind"] == "snapshot":
                            last_snapshot = payload
                        else:
                            payloads.append(payload)
                    if ev and ev[0] == "closed":
                        stop = True
                        break
                if last_snapshot is not None:
                    payloads.append(last_snapshot)
            for payload in payloads:
                self._send(payload)
            if stop:
                return

    def _send(self, payload: dict) -> None:
        with self._lock:
            self._seq += 1
            payload["seq"] = self._seq
            payload["state"] = self.state()
        try:
            self._emit(EVENT, payload)
        except Exception:  # noqa: BLE001 - a broken window must not kill the pump
            pass

    def _translate(self, ev: tuple) -> list[dict]:
        kind = ev[0] if ev else ""
        if kind == "busy":
            self._busy_job = ev[1]
            return [{"kind": "busy", "job": ev[1]}]
        if kind == "done":
            name, result = ev[1], json_safe(ev[2])
            self._busy_job = None
            out = [{"kind": "done", "job": name, "result": result}]
            if name in PROJECT_JOBS and isinstance(result, dict):
                self._workflow_dir = result.get("workflow_dir")
                self._needs_recovery = bool(result.get("needs_recovery"))
                self._remember(result.get("workflow_dir"), result.get("name"))
            elif name == "close_project":
                self._workflow_dir, self._snapshot, self._needs_recovery = None, None, False
                out.append({"kind": "snapshot", "snapshot": {}})     # the worker has no engine left to describe
            return out
        if kind == "error":
            self._busy_job = None
            self._last_error = ev[2]
            if ev[1] in PROJECT_JOBS:                    # the worker closed the previous project before failing
                self._workflow_dir, self._snapshot, self._needs_recovery = None, None, False
            return [{"kind": "error", "job": ev[1], "message": ev[2]}]
        if kind == "event":
            return [{"kind": "event", "event": json_safe(ev[1])}]
        if kind == "snapshot":
            self._snapshot = json_safe(ev[1])
            return [{"kind": "snapshot", "snapshot": self._snapshot}]
        self._workflow_dir, self._snapshot, self._busy_job, self._needs_recovery = None, None, None, False
        return [{"kind": "closed"}]

    # -- verbs --------------------------------------------------------------------------------------------------
    def new_project(self, name=None, asset=None, project_dir=None, workflow_dir=None, first_component=None,
                    preset_id=None) -> dict:
        def go(session):
            cfg = session.config
            wf = workflow_dir
            if not wf and not project_dir and not cfg.workflow_root:
                preset = cfg.presets.get(preset_id) if preset_id else None
                if preset is None or not (preset.get("workflow_dir") or preset.get("project_dir")):
                    slug = slugify(str(name or preset_id or "project"))
                    wf = os.path.join(os.path.dirname(self.settings_path()), PROJECTS_SUBDIR, slug)
            session.create(name=name or None, asset_name=asset or None, project_dir=project_dir or None,
                           workflow_dir=wf or None, first_component=first_component or None,
                           preset_id=preset_id or None, user=HUMAN)
            return {"ok": True, "job": "new_project"}
        return self._call(go, mock=None)

    def new_fixture(self, directory) -> dict:
        from builder.harness import fixture_screenplay

        def go(session):
            if not directory:
                raise Refusal("choose a folder for the demo project")
            session.create_fixture(Path(directory))
            return {"ok": True, "job": "new_fixture"}
        return self._call(go, mock=fixture_screenplay())

    def open(self, workflow_dir, screenplay=None) -> dict:
        def go(session):
            if not workflow_dir:
                raise Refusal("choose a workflow folder")
            session.open(workflow_dir)
            return {"ok": True, "job": "open"}
        return self._call(go, mock=(str(screenplay) if screenplay else None))

    def close_project(self, force=False) -> dict:
        def go(session):
            if self._busy_job in RUN_JOBS:
                if not force:
                    raise Refusal("a run is in progress; pause or cancel it first")
                session.cancel(HUMAN)
            session.close_project()
            return {"ok": True, "job": "close_project"}
        return self._call(go)

    def preflight(self, live=False) -> dict:
        return self._call(lambda s: self._submit(s, "preflight_live" if live else "preflight",
                                                 lambda: s.preflight(live=bool(live))))

    def start(self, attended=True, assignments=None, component=None) -> dict:
        def go(session):
            session.start_run(attended=bool(attended), component_name=component or None,
                              assignments=dict(assignments) if assignments else None)
            return {"ok": True, "job": "start_run"}
        return self._call(go)

    def resume(self) -> dict:
        return self._call(lambda s: self._submit(s, "resume_run", lambda: s.resume_run(user=HUMAN)))

    def pause(self) -> dict:
        def go(session):
            session.pause()
            return {"ok": True}
        return self._call(go)

    def cancel(self) -> dict:
        def go(session):
            session.cancel(HUMAN)
            return {"ok": True}
        return self._call(go)

    def feedback(self, text) -> dict:
        def go(session):
            if not str(text or "").strip():
                raise Refusal("feedback needs some text")
            session.feedback(str(text).strip(), user=HUMAN)
            return {"ok": True}
        return self._call(go)

    def accept(self, component_id) -> dict:
        return self._call(lambda s: self._submit(s, "accept", lambda: s.accept_component(str(component_id), user=HUMAN)))

    def reopen(self, component_id, reason) -> dict:
        def go(session):
            if not str(reason or "").strip():
                raise Refusal("reopening needs a reason")
            session.reopen_component(str(component_id), reason=str(reason).strip(), user=HUMAN)
            return {"ok": True, "job": "reopen"}
        return self._call(go)

    def waive(self, finding_id, rationale) -> dict:
        def go(session):
            if not str(rationale or "").strip():
                raise Refusal("a waiver needs a rationale (R-75)")
            session.waive_finding(str(finding_id), rationale=str(rationale).strip(), user=HUMAN)
            return {"ok": True, "job": "waive"}
        return self._call(go)

    def request_correction(self, finding_id, note="") -> dict:
        return self._call(lambda s: self._submit(s, "request_correction",
                                                 lambda: s.request_correction(str(finding_id), note=str(note or ""), user=HUMAN)))

    def restore_checkpoint(self, checkpoint_id) -> dict:
        return self._call(lambda s: self._submit(s, "restore_checkpoint",
                                                 lambda: s.restore_checkpoint(str(checkpoint_id), user=HUMAN)))

    def set_limits(self, changes) -> dict:
        def go(session):
            if not isinstance(changes, dict) or not changes:
                raise Refusal("no limit changes given")
            session.set_limits(dict(changes), user=HUMAN)
            return {"ok": True, "job": "set_limits"}
        return self._call(go)

    def add_reference(self, paths, labels=None, kind="target", composite=False, notes="") -> dict:
        def go(session):
            files = [str(p) for p in (paths or []) if p]
            if not files:
                raise Refusal("no files were chosen")
            flat = isinstance(labels, list) and all(isinstance(x, str) for x in labels) and labels
            for i, p in enumerate(files):
                if flat:
                    lbl = list(labels)
                elif isinstance(labels, list) and i < len(labels) and isinstance(labels[i], list) and labels[i]:
                    lbl = [str(x) for x in labels[i]]
                else:
                    lbl = ["other"]
                session.add_reference(p, labels=lbl, kind=str(kind or "target"), notes=str(notes or ""),
                                      composite=bool(composite), user=HUMAN)
            return {"ok": True, "jobs": len(files)}
        return self._call(go)

    def register_source(self, path) -> dict:
        def go(session):
            if not path:
                raise Refusal("choose a .blend file")
            session.register_source(Path(path), user=HUMAN)
            return {"ok": True, "job": "register_source"}
        return self._call(go)

    def register_empty_source(self) -> dict:
        return self._call(lambda s: self._submit(s, "register_empty_source", lambda: s.register_empty_source(user=HUMAN)))

    def refresh(self) -> dict:
        return self._call(lambda s: self._submit(s, "refresh", s.refresh))

    def shutdown(self, timeout: float = 5.0) -> None:
        """Process exit only: closing the session cancels a run in flight (Job Objects reap its children)."""
        with self._lock:
            self._stop_session(timeout)
