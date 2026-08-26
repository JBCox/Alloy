#!/usr/bin/env python3
"""AI Chat desktop app: pywebview shell around the relay engine (relay.py).

Runs a native window (WebView2) hosting ui/index.html. The conversation loop
mirrors relay.py's round-robin engine and reuses its Agent adapters verbatim.
"""

import base64
import ctypes
import datetime
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

import webview

import dictation
import export as export_mod
import fork as fork_mod
import outcome
import relay
from relay import (AGENT_TYPES, PROVIDERS, SESSIONS_DIR, HELP_TEXT,
                   MODES, DEFAULT_MODE, IMPLEMENTED_MODES, DEFAULT_CEILING,
                   OX_FREE_MODELS, OX_DEFAULT_MODEL, helper_spec,
                   read_tabs, write_tabs, TAB_COLORS,
                   ox_model_details, ox_default_level,
                   assign_labels, compact_agent, resolve_cmd, clean_env,
                   logout_gemini,
                   error_excerpt,
                   LoopIO, run_rounds, dispatch_command,
                   SessionStore, make_log, read_meta, read_messages,
                   session_summary,
                   project_brief, write_project_context, read_project_context,
                   brief_drift,
                   list_sessions as stored_sessions, session_path, rehydrate)

AGENT_ORDER = ["claude", "gpt", "gemini"]

# Goal-first composer recipes still persist the nearest legacy mode while the
# migration window is open.  The normalized orchestration object is the
# execution truth; this fallback only lets app clients omit the redundant
# legacy key without turning a Panel or Live Room request into round-robin.
APP_PRESET_MODES = {
    "open_discussion": "round_robin",
    "panel_review": "panel",
    "build_execute": "supervisor",
    "live_room": "free",
}


def _app_orchestration_config(cfg, turns, until_done=False, ceiling=None):
    """Resolve one app launch into a legacy mode plus normalized recipe.

    Explicit ``mode`` remains authoritative for old clients.  New clients may
    send only the additive orchestration dictionary; in that case derive the
    nearest legacy mode so saved sessions still open in older app versions.
    The normalized budget limit also drives the corresponding engine cap,
    preventing ``SessionStore.save`` from immediately overwriting an
    Advanced-drawer value with a different top-level value.
    """
    raw = cfg.get("orchestration")
    mode_value = cfg.get("mode")
    if mode_value:
        mode = str(mode_value).replace("-", "_")
    else:
        mode = None
        if isinstance(raw, dict):
            legacy = raw.get("legacy_mode")
            if legacy in MODES:
                mode = legacy
            preset = str(raw.get("preset") or "").replace("-", "_")
            mode = mode or APP_PRESET_MODES.get(preset)
            if mode is None:
                workflow = raw.get("workflow")
                concurrency = raw.get("concurrency")
                floor = raw.get("floor")
                if workflow == "panel":
                    mode = "panel"
                elif workflow == "supervisor":
                    mode = "supervisor"
                elif concurrency == "reactive":
                    mode = "free"
                elif floor == "nomination":
                    mode = "speaker"
                elif floor == "moderated":
                    mode = "moderator"
                elif concurrency == "barrier":
                    mode = "parallel"
        mode = mode or DEFAULT_MODE

    limit = ceiling if until_done else turns
    # The reporting form: same policy, plus whatever the backend had to
    # override.  A correction the UI never hears about is a silent one.
    recipe, adjustments = relay.normalize_orchestration_report(
        raw, mode, limit, bool(until_done))
    return mode, recipe, adjustments


def _panel_synthesizer(cfg, slot_ids):
    """Return the configured Panel author as a stable slot id.

    HTML ``select`` values arrive as strings even when seat ids are integers,
    so a unique string-equivalent match is safe and keeps duplicate-provider
    rosters working.  Missing selection means the start seat; an invalid or
    ambiguous selection is rejected rather than silently changing authors.
    """
    panel_cfg = cfg.get("panel")
    value = panel_cfg.get("synthesizer") \
        if isinstance(panel_cfg, dict) else None
    if value is None:
        value = cfg.get("synthesizer")
    if value is None:
        return slot_ids[0]
    exact = [slot for slot in slot_ids if slot == value]
    matches = exact or [slot for slot in slot_ids if str(slot) == str(value)]
    if len(matches) != 1:
        raise ValueError("Panel synthesizer must be one enabled participant.")
    return matches[0]

# ------------------------------------------------------- file/image viewing --
# The bridge serves ONLY files beneath the ACTIVE session's workspace (the
# same contract the adapters live under). Allowed image types + a byte cap,
# returned as data URIs — file:// does not reliably load in WebView2 from the
# app's own origin, so this mirrors save_attachments' base64 path in reverse.
IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}
IMAGE_MAX_BYTES = 15 * 1024 * 1024   # matches the composer's attachment cap
THUMB_EDGE = 320                     # thumbnail bytes first; full res on click
FILE_LIST_MAX = 200                  # rows returned to the Files rail
FILE_SCAN_MAX = 4000                 # walk budget for huge picked folders
TEXT_MAX_BYTES = 256 * 1024          # live code viewer read cap

# The keyboard-shortcut cheat sheet (? toggles the overlay). Single source of
# truth: app.py owns it, the UI fetches it via get_shortcuts() and renders it
# verbatim, so a binding that changes here cannot drift from the sheet. Every
# entry must describe a binding the UI REALLY has — "?" itself is the toggle,
# which is why it heads the list.
KEYBOARD_SHORTCUTS = [
    {"keys": "?", "action": "Show or hide this cheat sheet"},
    {"keys": "Enter", "action": "Send the message in the composer"},
    {"keys": "/ …", "action": "A message starting with / runs a command instead of chatting"},
    {"keys": "Ctrl+T", "action": "New conversation tab"},
    {"keys": "Ctrl+1–9", "action": "Jump to tab N"},
    {"keys": "Ctrl+Tab", "action": "Cycle open tabs (Shift goes backwards)"},
    {"keys": "Ctrl+Shift+Space", "action": "Start / stop dictation (hold or tap to latch)"},
    {"keys": "Escape", "action": "Cancel dictation, close a modal, or clear search"},
    {"keys": "↑ / ↓", "action": "Resize the composer from its focused grab bar"},
    {"keys": "Double-click", "action": "Rename a chat in the rail (or a seat's name)"},
]


# Moved to relay.py (the activity sink confines CLI-quoted paths engine-side);
# re-exported here so the bridge methods and tests keep their import.
from relay import confine_to_workspace


# ------------------------------------------------------- session rail data --
# The rail paints about ten fields per row, but session_summary returns the
# WHOLE record — supervisor control trace, workstream task records, the
# orchestration recipe — none of which the rail reads. Those matter only once
# a chat is OPEN, and open_session keeps serving the full summary that
# restoreOrchestration / restoreSeats / the control log consume. Measured on
# the real history (2026-08-23): 42 chats carried 390 KB per refreshChats(),
# 76% of it supervisor_trace + tasks, and one long Keep Improving chat alone
# carried 140 KB of trace growing with every wave — hauled across the bridge
# on boot and at EVERY run end just to paint titles and dots. This is an
# explicit allowlist rather than a trim-list on purpose: a new summary field
# stays out of rail rows until the rail actually needs it, so the payload
# cannot quietly regrow; a genuinely new rail feature edits this tuple and
# the omission is visible here, not discovered in a profiler.
RAIL_SUMMARY_FIELDS = (
    # identity + ordering
    "id", "title", "created", "updated",
    # view-only markers and resumability (tooltip)
    "ended", "legacy", "can_continue", "can_continue_reason",
    # lineage ("↳ spawned by …", "branched from …") and project grouping headers
    "parent", "fork_of", "project",
    # outcome pill + compact manager badge (NOT the trace behind it)
    "completion", "supervisor_status",
)


def _rail_row(summary):
    """One sidebar row: exactly what the rail paints, nothing more.

    Participants shrink to what a row shows too — dots paint provider, the
    tooltip paints name; model/effort/role strings are seat-card material
    and ride open_session's full summary instead."""
    row = {k: summary[k] for k in RAIL_SUMMARY_FIELDS if k in summary}
    row["participants"] = [
        {k: p[k] for k in ("id", "provider", "name") if k in p}
        for p in summary.get("participants") or []]
    return row


def _thumb_bytes(path):
    """Downscaled bytes for a thumbnail, or (None, None) to fall back to the
    original file (tiny images, corrupt files, Pillow missing)."""
    try:
        from PIL import Image
    except ImportError:
        return None, None
    import io
    try:
        with Image.open(path) as im:
            if max(im.size) <= THUMB_EDGE:
                return None, None            # already small — original wins
            im.thumbnail((THUMB_EDGE, THUMB_EDGE))
            buf = io.BytesIO()
            if im.mode in ("RGBA", "LA", "P"):
                im.save(buf, "PNG")
                return buf.getvalue(), "image/png"
            im.convert("RGB").save(buf, "JPEG", quality=82)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        return None, None


def save_attachments(files, workspace):
    """Decode UI attachments ([{name, data-b64}]) into <workspace>/attachments.

    The workspace is the one folder every seat's CLI can already read, so a
    saved path is all an agent needs to open the file. Returns absolute paths.
    """
    if not files:
        return []
    att_dir = os.path.join(workspace, "attachments")
    os.makedirs(att_dir, exist_ok=True)
    paths = []
    for f in files:
        name = re.sub(r"[^\w .()\-]+", "_",
                      os.path.basename(f.get("name") or "file")) or "file"
        dest = os.path.join(att_dir, name)
        root, ext = os.path.splitext(dest)
        n = 1
        while os.path.exists(dest):
            n += 1
            dest = f"{root}-{n}{ext}"
        with open(dest, "wb") as fh:
            fh.write(base64.b64decode(f["data"]))
        paths.append(dest)
    return paths


def with_attachments(text, paths):
    """Append '[Josh attached a file: …]' lines so agents know to open them."""
    if not paths:
        return text
    lines = "\n".join(f"[Josh attached a file: {p}]" for p in paths)
    return f"{text}\n\n{lines}" if text else lines


def agy_path():
    import shutil
    return shutil.which("agy") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe")


class Run:
    """One conversation this window owns — live, paused, or just being drafted.

    Everything here USED to be a singular attribute on Api (`_conv`,
    `_thread`, `_stop_flag`, …), which is precisely why a second chat could
    not start without ending the first. One Run per chat; Api keeps a map.

    `id` is the session dir basename and is None until the chat is actually
    started (a draft has no identity yet — that is what `adopt` is for).
    """

    def __init__(self, chat_id=None):
        self.id = chat_id
        self.state = None            # relay state dict (the old Api._conv)
        self.thread = None
        self.stop_flag = threading.Event()
        self.human_q = queue.Queue()
        self.session_dir = None
        self.view_workspace = None   # reopened view-only chat's workspace
        self.staged_roles = {}       # seat index -> staged role change
        self.roles_busy = False      # idle role worker belongs to this run
        # Per-run on purpose: a global one made a question in chat B queue
        # invisibly behind an unanswered question in chat A.
        self.ask_lock = threading.Lock()
        self.status = "idle"         # the RunState vocabulary (see set_status)
        self.pending_ask = None
        self.unread = 0
        # Seats currently inside a turn: slot id -> {name, provider, started,
        # limit}. Typing indicators are LIVE-only in the UI, so reopening a
        # chat mid-turn used to wipe them and never bring them back — a room
        # with three seats 14 minutes into a 15-minute window looked exactly
        # like a dead one (2026-08-23). open_session replays this instead.
        self.thinking = {}

    def is_running(self):
        return bool(self.thread and self.thread.is_alive())


class RunManager:
    """Every chat this window is holding, keyed by chat id, plus the draft.

    Deliberately dumb: a dict, a focus pointer and a lock. The rule it does
    enforce is the one that corrupts data if broken — a session may be live
    in AT MOST ONE run. Two Agent objects sharing a CLI session id shred
    continuity, because `claude -p --resume` mints a NEW id on every call and
    whichever thread writes last wins.
    """

    def __init__(self):
        self._runs = {}              # chat_id -> Run
        self._draft = Run()          # the unstarted new-chat stage
        self._focus = None           # chat_id, or None meaning the draft
        self._lock = threading.RLock()

    def focused(self):
        with self._lock:
            if self._focus is None:
                return self._draft
            return self._runs.get(self._focus) or self._draft

    def get(self, chat_id):
        with self._lock:
            return self._runs.get(chat_id)

    def all(self):
        with self._lock:
            return list(self._runs.values())

    def live(self):
        """Runs with a loop thread still going — what a new chat must not
        disturb, and what a window close has to stop."""
        with self._lock:
            return [r for r in self._runs.values() if r.is_running()]

    def adopt(self, run, chat_id):
        """Give a run its identity the moment its session dir exists."""
        with self._lock:
            run.id = chat_id
            self._runs[chat_id] = run
            if run is self._draft:
                self._draft = Run()          # a fresh stage for the next chat
            self._focus = chat_id
            return run

    def focus(self, chat_id):
        """Switch which chat the window is SHOWING. Never touches threads:
        looking at another conversation must not stop this one."""
        with self._lock:
            if chat_id is None:
                self._focus = None
                return self._draft
            run = self._runs.get(chat_id)
            if run is None:
                run = self._runs[chat_id] = Run(chat_id)
            self._focus = chat_id
            return run

    def new_draft(self):
        """Start composing another chat, leaving live runs untouched."""
        with self._lock:
            self._focus = None
            return self._draft

    def forget(self, chat_id):
        with self._lock:
            run = self._runs.pop(chat_id, None)
            if self._focus == chat_id:
                self._focus = None
            return run


class _AppIO(LoopIO):
    """LoopIO for the desktop app. A module-level class on purpose: public
    attributes on the js_api object are walked by the pywebview bridge at page
    load (deadlock), so this wrapper lives outside Api and holds it privately.
    """

    def __init__(self, api, run=None):
        self._api = api
        # Captured ONCE, never re-read from the focus pointer: this loop keeps
        # answering to its own chat after Josh switches to another one. Reading
        # api._stop_flag here instead would make Stop in the visible chat kill
        # a background run — the exact bug the registry exists to prevent.
        self._run = run if run is not None else api._runs.focused()

    def emit(self, event, payload=None):
        payload = dict(payload or {})
        payload.setdefault("chat_id", self._run.id)
        # Track in-flight seats here rather than in Api.emit, which must stay
        # a pure enqueue, and rather than in the loop, which would have to
        # learn about front-end state it has no business knowing.
        if event == "thinking":
            self._run.thinking[str(payload.get("speaker"))] = {
                "speaker": payload.get("speaker"),
                "provider": payload.get("provider"),
                "name": payload.get("name"),
                "limit": payload.get("limit"),
                "idle": payload.get("idle"),
                "started": time.time()}
        elif event == "thinking_done":
            self._run.thinking.pop(str(payload.get("speaker")), None)
        self._api.emit(event, payload)

    def drain_human(self):
        out = []
        while not self._run.human_q.empty():
            out.append(self._run.human_q.get_nowait())
        return out

    def should_stop(self):
        return self._run.stop_flag.is_set()

    def on_turn_boundary(self, state):
        # a staged role lands here, so the seat about to speak gets its fresh
        # preamble with the new role rather than switching identity mid-turn
        if self._run.staged_roles:
            self._api._commit_roles(state, self._run)

    def ask_human(self, payload, abort=None):
        # Runs on the conversation worker / a seat thread, NEVER the bridge
        # thread. emit is a pure enqueue, so blocking here never stalls the
        # UI. _ask_lock serializes simultaneous questions (parallel mode) into
        # consecutive modals instead of stacked ones; it is app-level only, so
        # no lock-order interaction with state["lock"].
        api, run = self._api, self._run
        q = queue.Queue()
        with run.ask_lock:               # per-RUN: chat B must not queue behind A
            api._ask_waiters[payload["qid"]] = q
            run.pending_ask = dict(payload)
            self.emit("question", payload)
            try:
                while True:
                    if run.stop_flag.is_set() or (abort and abort()):
                        return None
                    try:
                        return q.get(timeout=0.5)
                    except queue.Empty:
                        pass
            finally:
                api._ask_waiters.pop(payload["qid"], None)
                run.pending_ask = None
                self.emit("question_done", {"qid": payload["qid"]})


class Api:
    def __init__(self):
        self._window = None
        # Every per-chat thing now lives on a Run; these underscore properties
        # below are views onto the FOCUSED run, so the whole existing body of
        # Api (and its tests) keeps working while chat-scoped call sites move
        # over one at a time. Half-migrating is how confinement bugs appear,
        # so the views stay until every caller passes a chat id.
        self._runs = RunManager()
        self._config_cache = None
        self._config_ready = threading.Event()
        self._auth_cache = {}          # provider id -> status dict (relay probe)
        self._auth_lock = threading.Lock()
        self._login_procs = {}         # provider id -> Popen of open login console
        # qids are globally unique, so ONE map is correct here even with many
        # runs; the LOCK is what had to become per-run (see Run.ask_lock).
        self._ask_waiters = {}         # qid -> Queue awaiting answer_question
        self._roles_lock = threading.Lock()
        # Dictation: one microphone and one composer, so this is app-wide
        # rather than per-run. Underscore-prefixed like everything else here —
        # public attrs on the js_api object deadlock the pywebview bridge walk.
        self._dict_lock = threading.Lock()
        self._dict_rec = None          # the live dictation.Recorder, if any
        self._dict_engine = None       # lazily built Transcriber, then cached
        self._dict_probe = None        # dictation.probe() result, set at startup
        # Serialized emitter: evaluate_js is only ever called from ONE thread
        # (pywebview/WebView2 marshalling isn't documented thread-safe, and
        # parallel modes emit from several seat threads). A single queue also
        # guarantees FIFO ordering across all producers.
        self._emit_q = queue.Queue()
        # Sound cues for events that wait on Josh (question/checkin/done).
        # UI toggles it via set_sound and remembers the choice in localStorage;
        # ON by default because the events that chime are the ones that block.
        self._sound = True
        threading.Thread(target=self._drain_emits, daemon=True).start()

    # ---- focused-run views (the old singular attributes) -----------------
    # Underscore-prefixed on purpose: pywebview walks PUBLIC attrs at page
    # load and deadlocks, so none of this may be public.
    @property
    def _conv(self):
        return self._runs.focused().state

    @_conv.setter
    def _conv(self, value):
        self._runs.focused().state = value

    @property
    def _thread(self):
        return self._runs.focused().thread

    @_thread.setter
    def _thread(self, value):
        self._runs.focused().thread = value

    @property
    def _stop_flag(self):
        return self._runs.focused().stop_flag

    @property
    def _human_q(self):
        return self._runs.focused().human_q

    @property
    def _staged_roles(self):
        return self._runs.focused().staged_roles

    @property
    def _ask_lock(self):
        return self._runs.focused().ask_lock

    @property
    def _session_dir(self):
        return self._runs.focused().session_dir

    @_session_dir.setter
    def _session_dir(self, value):
        # The moment a chat has a directory it has an identity, so this is the
        # one hook that registers it. Doing it here rather than at each call
        # site means no start path can forget and leave a run untracked.
        run = self._runs.focused()
        run.session_dir = value
        if value:
            self._runs.adopt(run, os.path.basename(os.path.normpath(value)))

    @property
    def _view_workspace(self):
        return self._runs.focused().view_workspace

    @_view_workspace.setter
    def _view_workspace(self, value):
        self._runs.focused().view_workspace = value

    # ---- lifecycle status ------------------------------------------------
    # The canonical RunState wire vocabulary. Lifecycle answers "what is this
    # run doing"; attention metadata (unread, pending_ask) answers "does Josh
    # need to look" — kept separate so neither multiplies the other.
    RUN_STATES = ("idle", "running", "thinking", "waiting",
                  "stopping", "stopped", "failed", "done")

    def _set_status(self, run, status, **extra):
        """Record a run's lifecycle state and tell the UI. Never raises: a
        status emit failing must not take a conversation down with it."""
        if run is None:
            return
        if status not in self.RUN_STATES:      # a typo'd state would silently
            status = "running"                 # freeze a rail row forever
        run.status = status
        payload = {"chat_id": run.id, "status": status,
                   "unread": run.unread,
                   "pending_ask": run.pending_ask}
        payload.update(extra)
        self.emit("run_status", payload)

    def run_status(self, chat_id=None):
        """Snapshot for the rail — pure cache read, safe on the bridge thread.

        `chat_id=None` returns every chat this window is holding, which is what
        the UI needs after a restart or a focus switch to repaint truthfully
        instead of guessing from the last event it happened to see.
        """
        runs = self._runs.all() if not chat_id else             [r for r in [self._runs.get(chat_id)] if r]
        return {"runs": [{"chat_id": r.id, "status": r.status,
                          "running": r.is_running(),
                          "unread": r.unread,
                          "pending_ask": r.pending_ask} for r in runs]}

    # ---------------------------------------------------------- to the UI --
    def emit(self, event, payload=None):
        # non-blocking and thread-safe: callers just enqueue
        self._emit_q.put((event,
                          json.dumps({"event": event,
                                      "payload": payload or {}})))

    def _drain_emits(self):
        while True:
            event, data = self._emit_q.get()
            try:
                self._window.evaluate_js(f"uiEvent({data})")
                # An unattended Keep Improving run that repaired itself at 3am
                # is exactly what Josh wants to notice when he comes back. Done
                # HERE rather than in emit() so the one thread that owns window
                # interaction keeps owning all of it.
                if event == "checkin":
                    _flash_taskbar(self._window)
                if event in SOUND_CUES and self._sound:
                    threading.Thread(target=_play_cue, args=(event,),
                                     daemon=True).start()
            except Exception:
                pass
            finally:
                self._emit_q.task_done()   # lets tests flush with q.join()

    # ------------------------------------------------------- config for UI --
    def get_config(self):
        # Called from the JS bridge. subprocess.run DEADLOCKS on pywebview's
        # bridge thread (verified on winforms/WebView2), so the config is
        # precomputed by a normal thread at startup; this only waits for it.
        self._config_ready.wait(timeout=45)
        return self._config_cache or self._fallback_config()

    @staticmethod
    def _seatable_providers():
        """Every provider with a real adapter, for BOTH provider pickers.

        The UI used to hardcode claude/gpt/gemini in two places, so Ox was
        seatable but could not be chosen as moderator or supervisor - the room
        could run entirely on one provider except for the one call that routes
        it. Deriving both lists from the registry means the next provider is
        choosable everywhere the moment its adapter lands.
        """
        return [{"id": pid, "label": meta["label"]}
                for pid, meta in PROVIDERS.items() if meta.get("agent")]

    @staticmethod
    def _fallback_config():
        return {
            "claude_models": [{"id": "claude-opus-5", "label": "Opus 5"}],
            "claude_default_model": "claude-opus-5",
            "claude_default_effort": "high",
            "gpt_models": [{"id": "gpt-5.6-sol", "label": "GPT-5.6-Sol",
                            "levels": ["low", "medium", "high"],
                            "default_level": "medium"}],
            "gpt_default_model": "gpt-5.6-sol",
            "gpt_default_effort": "high",
            "gemini_families": [
                {"base": "gemini-3.7-flash", "label": "Gemini 3.7 Flash",
                 "levels": ["high", "medium", "low"]}],
            "gemini_default_family": "gemini-3.7-flash",
            "gemini_default_level": "high",
            "ox_models": [dict(m, levels=[], default_level="")
                          for m in OX_FREE_MODELS],
            "ox_default_model": OX_DEFAULT_MODEL,
            "ox_default_effort": "",
            "providers": Api._seatable_providers(),
            # The probe never finished, so claim nothing rather than showing a
            # mic that cannot work — same posture as an `unknown` auth probe.
            "dictation": {"available": False,
                          "reason": "Still checking the microphone."},
        }

    def precompute_config(self):
        # warm the codex feature probe off the bridge thread, so the first
        # preamble build never blocks on a subprocess
        try:
            import relay as _relay
            _relay.codex_multi_agent_enabled()
        except Exception:
            pass
        # Dictation availability: cheap (import checks + a device enumeration),
        # but it touches PortAudio, so it belongs on this startup thread rather
        # than in get_config, which runs on the js bridge.
        try:
            self._dict_probe = dictation.probe()
        except Exception as exc:
            self._dict_probe = {"available": False, "reason": relay.error_excerpt(exc)}
        gemini_models = []
        try:
            out = subprocess.run(
                [agy_path(), "models"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for line in out.splitlines():
                if "\t" in line:
                    slug, label = line.split("\t", 1)
                    if slug.strip().startswith("gemini"):
                        gemini_models.append(
                            {"id": slug.strip(), "label": label.strip()})
        except Exception:
            pass
        if not gemini_models:
            gemini_models = [{"id": "gemini-3.7-flash-high",
                              "label": "Gemini 3.7 Flash (High)"}]
        # agy publishes each thinking level as its own model id
        # (gemini-3.7-flash-high / -medium / -low). Split into family + levels
        # so the UI can offer a model picker and a thinking picker like the rest.
        families = {}
        for m in gemini_models:
            match = re.match(r"^(.*)-(high|medium|low)$", m["id"])
            if not match:
                continue
            base, level = match.groups()
            fam = families.setdefault(base, {
                "base": base,
                "label": re.sub(r"\s*\((High|Medium|Low)\)\s*$", "", m["label"]),
                "levels": []})
            fam["levels"].append(level)
        gemini_families = list(families.values()) or [
            {"base": "gemini-3.7-flash", "label": "Gemini 3.7 Flash",
             "levels": ["high", "medium", "low"]}]

        # GPT models: the Codex CLI keeps its account's catalog (with each
        # model's supported reasoning levels) in ~/.codex/models_cache.json.
        gpt_models = []
        try:
            with open(os.path.join(os.path.expanduser("~"), ".codex",
                                   "models_cache.json"), encoding="utf-8") as f:
                cache = json.load(f)
            for m in cache.get("models", []):
                if m.get("visibility") != "list":
                    continue
                gpt_models.append({
                    "id": m["slug"], "label": m.get("display_name", m["slug"]),
                    "levels": [lv["effort"] for lv in
                               m.get("supported_reasoning_levels", [])],
                    "default_level": m.get("default_reasoning_level", ""),
                })
        except Exception:
            pass
        if not gpt_models:
            gpt_models = [{"id": "gpt-5.6-sol", "label": "GPT-5.6-Sol",
                           "levels": ["low", "medium", "high"],
                           "default_level": "medium"}]

        # Ox models: `opencode models` prints one id per line and lists the
        # FREE Zen models even with zero credentials (verified 2026-08-22).
        # Intersected with OX_FREE_MODELS rather than shown raw, for two
        # reasons: the CLI prints ids with no labels, and once Josh signs in
        # for the paid catalog the raw list becomes ~50 keyed models that this
        # keyless seat would offer and then fail on. Order follows
        # OX_FREE_MODELS, so Ox Alpha stays first while it exists.
        ox_models = []
        try:
            out = subprocess.run(
                # resolve_cmd, not a bare name: opencode installs as a .cmd
                # shim and CreateProcess cannot find it without the extension.
                resolve_cmd(["opencode", "models"]),
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                stdin=subprocess.DEVNULL, env=clean_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            have = {ln.strip() for ln in (out or "").splitlines() if ln.strip()}
            ox_models = [dict(m) for m in OX_FREE_MODELS if m["id"] in have]
        except Exception:
            pass
        if not ox_models:
            # CLI missing or the probe failed: show the known catalog rather
            # than an empty picker. The Accounts panel is what reports a
            # missing CLI; an empty dropdown would just look broken.
            ox_models = [dict(m) for m in OX_FREE_MODELS]
        # Each model carries ITS OWN thinking levels, straight from models.dev
        # via opencode's cache. These genuinely differ - Ox Alpha has
        # low/high/max, Muse Spark has five, Nemotron and MiMo have none at
        # all - so one shared list would offer levels that do nothing on most
        # of them (and opencode does not validate --variant, so a wrong level
        # fails silently rather than loudly).
        details = ox_model_details()
        for m in ox_models:
            info = details.get(m["id"].split("/", 1)[-1], {})
            m["levels"] = info.get("levels", [])
            m["default_level"] = ox_default_level(m["levels"])
            if info.get("context"):
                m["context"] = info["context"]
        ox_default_model = (OX_DEFAULT_MODEL
                            if any(m["id"] == OX_DEFAULT_MODEL for m in ox_models)
                            else ox_models[0]["id"])

        # The GPT seat's real defaults live in ~/.codex/config.toml.
        gpt_default_model = gpt_models[0]["id"]
        gpt_default_effort = ""
        try:
            with open(os.path.join(os.path.expanduser("~"), ".codex",
                                   "config.toml"), encoding="utf-8") as f:
                toml_text = f.read()
            m = re.search(r'^model\s*=\s*"([^"]+)"', toml_text, re.M)
            if m and any(g["id"] == m.group(1) for g in gpt_models):
                gpt_default_model = m.group(1)
            m = re.search(r'^model_reasoning_effort\s*=\s*"([^"]+)"',
                          toml_text, re.M)
            if m:
                gpt_default_effort = m.group(1)
        except Exception:
            pass

        self._config_cache = {
            "gpt_default_model": gpt_default_model,
            "gpt_default_effort": gpt_default_effort,
            # newest Opus. Pinned rather than inherited: the claude CLI
            # would otherwise fall back to ~/.claude/settings.json, which
            # is Josh's own global default and drifts independently.
            "claude_default_model": "claude-opus-5",
            "claude_default_effort": "high",
            "gemini_families": gemini_families,
            "gemini_default_family": "gemini-3.7-flash",
            "gemini_default_level": "high",
            "ox_models": ox_models,
            "ox_default_model": ox_default_model,
            "ox_default_effort": next(
                (m["default_level"] for m in ox_models
                 if m["id"] == ox_default_model), ""),
            "providers": self._seatable_providers(),
            # explicit IDs — all verified working on this Max account
            "claude_models": [
                {"id": "claude-fable-5", "label": "Fable 5"},
                {"id": "claude-opus-5", "label": "Opus 5"},
                {"id": "claude-opus-4-8", "label": "Opus 4.8"},
                {"id": "claude-sonnet-5", "label": "Sonnet 5"},
                {"id": "claude-haiku-4-5", "label": "Haiku 4.5"},
            ],
            "gpt_models": gpt_models,
            "gemini_models": gemini_models,
            "gemini_default": "gemini-3.7-flash-high",
            "dictation": self._dict_probe or {"available": False, "reason": ""},
            "docs": os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "README.md"),
        }
        self._config_ready.set()

    def pick_folder(self):
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if result:
            return result[0]
        return None

    def folder_exists(self, path):
        """Is this still a real folder? Bridge-thread safe (one stat call, no
        subprocess). Used to validate the remembered working folder at
        startup: silently restoring a path that has since been moved or
        deleted would hand the next conversation a workspace that isn't
        there, which surfaces much later as confusing seat failures."""
        try:
            return bool(path) and os.path.isdir(path)
        except (OSError, ValueError):
            return False

    def open_path(self, path):
        if path and os.path.exists(path):
            os.startfile(path)

    # ------------------------------------------------------------ dictation --
    # A microphone for the composer, on a LOCAL engine (dictation.py explains
    # why not Wispr Flow's own). Bridge-thread rules apply hard: opening
    # PortAudio and loading the speech model both block for seconds, so every
    # method here returns at once and the work happens on a worker thread that
    # answers with a `dictation` event — the same shape as recheck_auth.
    #
    # States, in order: recording -> transcribing -> text | empty | error.
    # `empty` and `error` carry NO text, ever. Dictation feeds a prompt three
    # CLIs will act on, so a bad recording has to stay visibly empty rather
    # than become plausible words (the never-forge rule, one field over).

    def _dict_emit(self, state, text="", note=""):
        self.emit("dictation", {"state": state, "text": text, "note": note})

    @staticmethod
    def _dict_reason(exc):
        return relay.error_excerpt(str(exc) or exc.__class__.__name__)

    def _dictation_engine(self):
        with self._dict_lock:
            if self._dict_engine is None:
                self._dict_engine = dictation.make_transcriber()
            return self._dict_engine

    def _dict_warm(self):
        """Load the speech model while Josh is still talking.

        The model load is the slow part (~5 s cold for base.en) and it is
        independent of the audio, so overlapping the two means the first
        dictation of a session costs about as much as every later one.
        """
        try:
            self._dictation_engine().warm()
        except Exception:
            pass          # the real failure will be reported by transcribe()

    def dictation_start(self):
        """Begin capturing. Returns immediately; progress arrives as events."""
        with self._dict_lock:
            live = self._dict_rec
            if live is not None and live.recording:
                return {"ok": True, "note": "already recording"}
            rec = self._dict_rec = dictation.Recorder()

        def work():
            try:
                if not rec.start():
                    return           # stopped while the device was opening
            except Exception as exc:
                with self._dict_lock:
                    if self._dict_rec is rec:
                        self._dict_rec = None
                self._dict_emit("error", note=self._dict_reason(exc))
                return
            self._dict_emit("recording")
            self._dict_warm()

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def dictation_stop(self):
        """Stop capturing and transcribe. The text arrives as an event."""
        with self._dict_lock:
            rec, self._dict_rec = self._dict_rec, None
        if rec is None:
            return {"ok": True, "note": "not recording"}

        def work():
            try:
                pcm = rec.stop()
            except Exception as exc:
                self._dict_emit("error", note=self._dict_reason(exc))
                return
            if dictation.pcm_seconds(pcm) < dictation.MIN_SECONDS:
                self._dict_emit("empty",
                                note="Too short — hold the mic while you speak.")
                return
            self._dict_emit("transcribing")
            try:
                text = self._dictation_engine().transcribe(pcm)
            except Exception as exc:
                self._dict_emit("error", note=self._dict_reason(exc))
                return
            if not text:
                self._dict_emit("empty", note="Nothing was heard.")
                return
            self._dict_emit("text", text=text,
                            note=("Recording hit the length cap."
                                  if getattr(rec, "truncated", False) else ""))

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def dictation_cancel(self):
        """Drop the recording without transcribing (Escape while holding)."""
        with self._dict_lock:
            rec, self._dict_rec = self._dict_rec, None
        if rec is None:
            return {"ok": True, "note": "not recording"}
        threading.Thread(target=rec.cancel, daemon=True).start()
        self._dict_emit("idle")
        return {"ok": True}

    # ------------------------------------------------- file/image viewing --
    # Bridge-thread rules apply: bounded file I/O and Pillow only — no
    # subprocess, no unbounded walks. Errors come back as {"error": …} so the
    # UI can render a quiet placeholder instead of a broken tag.

    def _active_workspace(self, chat_id=None):
        """The LIVE workspace value for ONE chat: its running/continuable
        conversation's, else its reopened (view-only) one. Never rebuilt from
        a session id — a path derived from an id would let a renamed or
        deleted chat resolve somewhere it no longer owns.

        With several chats live, "the workspace" stopped having a referent.
        Answering an UNKNOWN chat_id from the focused run is how chat A's
        thumbnails start resolving inside chat B's folder, so an unknown id
        resolves to nothing rather than to a default.
        """
        run = self._runs.get(chat_id) if chat_id else self._runs.focused()
        if run is None:
            return None
        return (run.state or {}).get("workspace") or run.view_workspace

    def read_image(self, path, full=False, chat_id=None):
        ws = self._active_workspace(chat_id)
        if not ws:
            return {"error": "No active conversation workspace."}
        real = confine_to_workspace(ws, path)
        if not real or not os.path.isfile(real):
            # out-of-bounds and missing look identical on purpose: the reply
            # must not disclose whether a forbidden path exists
            return {"error": "not available"}
        mime = IMAGE_MIME.get(os.path.splitext(real)[1].lower())
        if not mime:
            return {"error": "not an image"}
        try:
            if os.path.getsize(real) > IMAGE_MAX_BYTES:
                return {"error": "image too large to preview"}
            data = None
            if not full:
                data, tmime = _thumb_bytes(real)
                if data is not None:
                    mime = tmime
            if data is None:
                with open(real, "rb") as f:
                    data = f.read()
        except OSError:
            return {"error": "not available"}
        return {"ok": True, "name": os.path.basename(real),
                "data_uri": f"data:{mime};base64,"
                            f"{base64.b64encode(data).decode('ascii')}"}

    # chat_id comes SECOND: the UI calls read_text(path, activeId), and with
    # max_bytes in that slot a chat id was silently used as the byte cap —
    # no error, just a wrong limit. Argument order is part of the contract.
    def read_text(self, path, chat_id=None, max_bytes=None):
        """Text of a workspace file for the live code viewer. Mirrors
        read_image's posture exactly: confined path, and forbidden/missing
        are the IDENTICAL quiet error (no existence disclosure)."""
        ws = self._active_workspace(chat_id)
        if not ws:
            return {"error": "No active conversation workspace."}
        real = confine_to_workspace(ws, path)
        if not real or not os.path.isfile(real):
            return {"error": "not available"}
        cap = int(max_bytes or TEXT_MAX_BYTES)
        cap = max(1, min(cap, TEXT_MAX_BYTES))
        try:
            st = os.stat(real)
            with open(real, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(cap)
        except OSError:
            return {"error": "not available"}
        if "\x00" in text[:4096]:
            return {"error": "not a text file"}
        return {"ok": True, "name": os.path.basename(real),
                "path": os.path.relpath(real, os.path.realpath(ws)),
                "text": text, "truncated": st.st_size > cap,
                "mtime": st.st_mtime}

    def list_workspace_files(self, chat_id=None):
        ws = self._active_workspace(chat_id)
        if not ws or not os.path.isdir(ws):
            return {"workspace": None, "files": []}
        root = os.path.realpath(ws)
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv"}
        rows, scanned = [], 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in skip and not d.startswith(".")]
            for fn in filenames:
                scanned += 1
                if scanned > FILE_SCAN_MAX:
                    dirnames[:] = []
                    break
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rows.append({
                    "name": fn,
                    "path": os.path.relpath(full, root),
                    "abs": full,
                    "size": st.st_size, "mtime": st.st_mtime,
                    "is_image": os.path.splitext(fn)[1].lower() in IMAGE_MIME})
            if scanned > FILE_SCAN_MAX:
                break
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        return {"workspace": root, "files": rows[:FILE_LIST_MAX],
                "truncated": scanned > FILE_SCAN_MAX or len(rows) > FILE_LIST_MAX}

    def restart_resume(self):
        """What, if anything, this launch should pick back up.

        Read on startup. Reopening a chat is free, so the last active tab is
        always offered; RESUMING it costs real calls, so that is offered only
        for a chat whose process died mid-run (`was_interrupted`) — every
        other ending was somebody's decision.

        The barren guard mirrors Keep Improving's: two auto-resumes that
        committed no turn means resuming is not working, and a third would
        just be a crash loop with a bill attached.
        """
        try:
            active = (read_tabs() or {}).get("active")
        except Exception:
            return {}
        if not active:
            return {}
        path = session_path(active)
        if not path:
            return {}
        meta = read_meta(path)
        summary = session_summary(path, meta)
        out = {"session_id": active, "resume": False, "reason": ""}
        if not (summary.get("interrupted") and summary.get("can_continue")):
            return out
        seen = meta.get("auto_resume") or {}
        if (int(seen.get("turn", -1)) == int(meta.get("turn") or 0)
                and int(seen.get("count") or 0) >= 2):
            out["reason"] = ("This chat was interrupted again, but the last "
                             "two automatic resumes produced no turns — "
                             "reply to try it yourself.")
            return out
        out["resume"] = True
        out["reason"] = ("This conversation was still running when the app "
                         "closed, so it is being picked up where it left off. "
                         "Press Stop if you would rather it did not.")
        return out

    def note_auto_resume(self, session_id):
        """Record the attempt so a crash loop cannot bill itself forever."""
        path = session_path(session_id)
        if not path:
            return {"ok": False}
        meta = read_meta(path)
        if not meta:
            return {"ok": False}
        turn = int(meta.get("turn") or 0)
        seen = meta.get("auto_resume") or {}
        count = int(seen.get("count") or 0) + 1             if int(seen.get("turn", -1)) == turn else 1
        meta["auto_resume"] = {"turn": turn, "count": count}
        try:
            relay._atomic_write(os.path.join(path, "meta.json"),
                                json.dumps(meta, ensure_ascii=False, indent=1))
        except Exception:
            pass                      # bookkeeping must never block a resume
        return {"ok": True, "count": count}

    # -------------------------------------------------- keep improving --
    def continuous_probe(self, path=None):
        """What the Keep Improving warning modal needs to tell the truth.

        `recheck_auth` shape, and for the documented reason: `git status` is a
        subprocess, and subprocess.run DEADLOCKS on the pywebview bridge
        thread. Answering with an event costs the modal one repaint and buys
        it a real answer instead of a guess.
        """
        folder = (path or "").strip() or None

        def work():
            payload = {"workspace": folder, "command": "", "dirty": False,
                       "git": False}
            try:
                if folder and os.path.isdir(folder):
                    payload["command"] = relay.detect_test_command(folder)
                    dirty = relay.git_dirty(folder)
                    payload["git"] = dirty is not None
                    payload["dirty"] = bool(dirty)
            except Exception:
                pass            # a probe that fails must not block the modal
            self.emit("continuous_probe", payload)
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------ accounts --
    # get_auth_status is called on the js-bridge thread: it must stay
    # subprocess-free and non-blocking (cache snapshot only). All probing
    # happens on normal/worker threads (pywebview bridge-thread deadlock).

    def _auth_payload(self):
        with self._auth_lock:
            cache = dict(self._auth_cache)
        provs = []
        for pid, meta in PROVIDERS.items():
            st = dict(cache.get(pid) or {
                "provider": pid, "label": meta["label"], "state": "unknown",
                "email": None, "detail": "checking…",
                "install_hint": meta["install_hint"]})
            st["color"] = meta["color"]
            st["seatable"] = meta["agent"] is not None
            st["can_logout"] = bool(meta["logout_argv"]) or pid == "gemini"
            provs.append(st)
        return {"providers": provs,
                "ready": all(p in cache for p in PROVIDERS)}

    def _probe_into_cache(self, pid):
        try:
            st = PROVIDERS[pid]["probe"]()
        except Exception as e:
            st = {"provider": pid, "label": PROVIDERS[pid]["label"],
                  "state": "unknown", "email": None,
                  "detail": f"probe error: {str(e)[:100]}",
                  "install_hint": PROVIDERS[pid]["install_hint"]}
        with self._auth_lock:
            self._auth_cache[pid] = st
        return st

    def precompute_auth(self):
        # Runs on a normal startup thread; one thread per provider so the
        # panel fills in progressively as each probe finishes.
        def one(pid):
            self._probe_into_cache(pid)
            self.emit("auth_status", self._auth_payload())
        for pid in PROVIDERS:
            threading.Thread(target=one, args=(pid,), daemon=True).start()

    def get_auth_status(self):
        return self._auth_payload()

    def recheck_auth(self, provider=None):
        pids = [provider] if provider in PROVIDERS else list(PROVIDERS)

        def work():
            for pid in pids:
                self._probe_into_cache(pid)
            self.emit("auth_status", self._auth_payload())
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def sign_in(self, provider):
        meta = PROVIDERS.get(provider)
        if not meta:
            return {"error": f"Unknown provider {provider!r}."}
        proc = self._login_procs.get(provider)
        if proc and proc.poll() is None:
            return {"error": f"{meta['label']} sign-in is already in progress "
                             f"— finish it in the terminal window."}

        def work():
            argv = list(meta["login_argv"])
            if argv[0] == "agy":  # may be off PATH in shells opened pre-install
                argv[0] = agy_path()
            env = clean_env() if meta.get("login_strip_env") else None
            try:
                # A VISIBLE console that owns the TTY for the OAuth flow —
                # deliberately the opposite of agent subprocesses: new console,
                # no DEVNULL stdin, no capture, no CREATE_NO_WINDOW.
                p = subprocess.Popen(
                    ["cmd", "/c"] + argv, env=env,
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
            except OSError as e:
                self.emit("status",
                          {"text": f"Could not open a sign-in terminal: {e}"})
                return
            self._login_procs[provider] = p
            try:
                p.wait(timeout=600)
            except subprocess.TimeoutExpired:
                pass
            st = self._probe_into_cache(provider)
            self.emit("auth_status", self._auth_payload())
            if st["state"] == "signed_in":
                who = st.get("email") or st.get("detail") or ""
                self.emit("status", {"text": f"{meta['label']} signed in"
                                             + (f" as {who}" if who else "")
                                             + "."})
            else:
                self.emit("status", {"text": f"{meta['label']} still not "
                                             f"signed in — finish the flow, "
                                             f"then hit ↻ in Accounts."})
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def sign_out(self, provider):
        meta = PROVIDERS.get(provider)
        if not meta:
            return {"error": f"Unknown provider {provider!r}."}
        if not (meta["logout_argv"] or provider == "gemini"):
            return {"error": f"{meta['label']} logout isn't wired up yet."}

        def work():
            try:
                if meta["logout_argv"]:
                    subprocess.run(
                        resolve_cmd(meta["logout_argv"]), capture_output=True,
                        text=True, encoding="utf-8", errors="replace",
                        timeout=60, stdin=subprocess.DEVNULL, env=clean_env(),
                        creationflags=getattr(subprocess,
                                              "CREATE_NO_WINDOW", 0))
                else:  # gemini: no CLI logout — creds moved to a backup dir
                    backup = logout_gemini()
                    if backup:
                        self.emit("status",
                                  {"text": f"Gemini credentials backed up to "
                                           f"{backup} (move them back to "
                                           f"restore)."})
            except Exception as e:
                self.emit("status", {"text": f"{meta['label']} logout failed: "
                                             f"{str(e)[:150]}"})
            st = self._probe_into_cache(provider)
            self.emit("auth_status", self._auth_payload())
            self.emit("status", {"text": f"{meta['label']}: "
                                 + ("signed out." if st["state"] != "signed_in"
                                    else "still signed in.")})
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def _auth_blockers(self, providers):
        """Friendly pre-flight messages for seats whose provider is known to
        be unusable. Cached statuses only — unknown/pending NEVER blocks."""
        with self._auth_lock:
            cache = dict(self._auth_cache)
        msgs = []
        for pid in sorted(set(providers)):
            st = cache.get(pid)
            if not st:
                continue
            label = PROVIDERS[pid]["label"]
            if st["state"] == "signed_out":
                msgs.append(f"{label} isn't signed in — open Accounts in the "
                            f"sidebar and click Sign in (or disable that "
                            f"seat).")
            elif st["state"] == "not_installed":
                msgs.append(f"The {label} CLI isn't installed — Accounts in "
                            f"the sidebar has the install command.")
        return msgs

    # ------------------------------------------------------------- skills --
    # Skills are plain files, so these stay on the bridge thread like the
    # history methods below. MCP is different — it shells out to the CLIs, so
    # those methods hand off to a worker and answer with an `mcp_status`
    # event (the recheck_auth shape).

    def get_skills(self):
        """One row per skill NAME across providers: which have it, which are
        missing it, and whether the installed copies have drifted apart."""
        per = relay.list_skills()
        managed = relay.manageable_providers()
        merged = {}
        for pid, rows in per.items():
            for r in rows:
                m = merged.setdefault(r["name"], {
                    "name": r["name"], "description": "",
                    "providers": [], "missing": [], "shas": [],
                    "extras": 0, "diverged": False, "error": None})
                m["providers"].append(pid)
                m["shas"].append(r["sha"])
                m["extras"] = max(m["extras"], r.get("extras") or 0)
                m["description"] = m["description"] or r["description"]
                m["error"] = m["error"] or r["error"]
        for m in merged.values():
            m["missing"] = [p for p in managed if p not in m["providers"]]
            # same name, different bytes: report it, never silently pick one
            m["diverged"] = len(set(m["shas"])) > 1
            m.pop("shas")
        return {"providers": [{"id": p, "label": PROVIDERS[p]["label"],
                               "color": PROVIDERS[p]["color"]}
                              for p in managed],
                "skills": sorted(merged.values(), key=lambda m: m["name"])}

    def read_skill(self, provider, name):
        got = relay.read_skill(provider, name)
        if got is None:
            return {"error": "not installed"}
        return {"ok": True, "description": got[0], "body": got[1]}

    def save_skill(self, name, description, body, providers, source=None):
        """`source` = a provider that already has this skill; its whole folder
        (scripts/, references/, assets/) is copied to the others. Without it a
        synced skill arrives with every link in its markdown dangling."""
        try:
            res = relay.write_skill(name, description, body,
                                    list(providers or []), source=source)
        except ValueError as e:
            return {"error": str(e)}
        failed = {p: e for p, e in res.items() if e}
        if failed:
            return {"error": "; ".join(f"{p}: {e}" for p, e in failed.items())}
        return {"ok": True}

    def remove_skill(self, name, providers):
        try:
            res = relay.delete_skill(name, list(providers or []))
        except ValueError as e:
            return {"error": str(e)}
        failed = {p: e for p, e in res.items()
                  if e and e != "not installed"}
        if failed:
            return {"error": "; ".join(f"{p}: {e}" for p, e in failed.items())}
        return {"ok": True}

    # --------------------------------------------------------------- MCP --
    # Every one of these spends a subprocess for claude/codex, so the bridge
    # thread only ever starts a worker and returns immediately.

    def get_mcp(self):
        def work():
            self.emit("mcp_status", {"providers": relay.list_mcp()})
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def add_mcp(self, provider, name, command, args=None, env=None,
                transport="stdio", url=None):
        if provider not in relay.manageable_providers():
            return {"error": "Unknown provider."}

        def work():
            err = relay.add_mcp(provider, name, command, args=args, env=env,
                                transport=transport, url=url)
            self.emit("status", {"text": err
                                 or f"Added MCP server '{name}' to "
                                    f"{PROVIDERS[provider]['label']}."})
            self.emit("mcp_status", {"providers": relay.list_mcp()})
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def remove_mcp(self, provider, name):
        if provider not in relay.manageable_providers():
            return {"error": "Unknown provider."}

        def work():
            err = relay.remove_mcp(provider, name)
            self.emit("status", {"text": err
                                 or f"Removed MCP server '{name}' from "
                                    f"{PROVIDERS[provider]['label']}."})
            self.emit("mcp_status", {"providers": relay.list_mcp()})
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    # ------------------------------------------------------------ history --
    # These methods are called on pywebview's bridge thread. Keep them to
    # bounded file I/O and object construction: never probe a CLI here.

    def list_sessions(self):
        """Sidebar rows under the rail contract (RAIL_SUMMARY_FIELDS) — the
        full summary stays on open_session, the one call whose consumer
        actually reads the deep fields."""
        return [_rail_row(s) for s in stored_sessions()]

    def search_sessions(self, query):
        """Cross-chat full-text search. Same bridge-thread rules as
        list_sessions — bounded file reads only, never a subprocess."""
        try:
            return relay.search_sessions(query)
        except Exception as exc:
            return {"error": error_excerpt(exc)}

    def get_shortcuts(self):
        """The keyboard-shortcut cheat sheet's data (? toggles the overlay).

        Pure constant return — the safest kind of bridge call there is: no
        file I/O, no subprocess, cannot raise, safe on the js-bridge thread.
        The UI renders KEYBOARD_SHORTCUTS verbatim rather than keeping its own
        hand-written copy, so the sheet can never drift from what the app
        actually binds.
        """
        return {"shortcuts": [dict(s) for s in KEYBOARD_SHORTCUTS]}

    def open_session(self, session_id):
        """Show another chat. Opening one no longer stops the running one —
        it is a FOCUS switch, which is the whole point of the run registry.

        The one thing it must refuse is loading a session that is already
        live in this window a SECOND time: two Agent objects carrying the
        same CLI session id shred continuity, because `claude -p --resume`
        mints a new id on every call and whichever thread writes last wins.
        So an already-open chat is focused, never rebuilt.
        """
        # The registry is consulted FIRST, and an already-open chat is served
        # from its own run's validated session_dir rather than a path rebuilt
        # from the id — the same rule read_image and friends follow.
        existing = self._runs.get(session_id)
        if existing is not None and existing.state is not None \
                and existing.session_dir:
            self._runs.focus(session_id)
            return {"ok": True,
                    "session": session_summary(existing.session_dir),
                    "messages": read_messages(existing.session_dir),
                    # so the UI can put the typing indicators back rather than
                    # showing a grinding chat as an idle one
                    "thinking": list(existing.thinking.values()),
                    "live": existing.is_running()}
        path = session_path(session_id)
        if not path:
            return {"error": "That chat no longer exists."}
        # a chat this window has not loaded yet gets its own run; the focused
        # one keeps its thread, queue and stop flag untouched
        self._runs.focus(os.path.basename(os.path.normpath(path)))

        meta = read_meta(path)
        summary = session_summary(path, meta)
        messages = read_messages(path)
        state = None
        if summary["can_continue"]:
            try:
                state = rehydrate(meta)
            except (KeyError, TypeError, ValueError) as e:
                summary["can_continue"] = False
                summary["can_continue_reason"] = str(e) or \
                    "Saved chat state is incomplete — view only"

        self._session_dir = path
        # view-only chats have no live state; the Files rail and inline
        # previews still need THIS chat's recorded workspace (may be gone —
        # confine/read handle that with placeholders, never a broken tag)
        self._view_workspace = summary.get("workspace") or None
        while not self._human_q.empty():
            self._human_q.get_nowait()

        if state is not None:
            store = SessionStore(path)
            state["store"] = store
            state["transcript"] = store.transcript
            # same logger the live loops use — a second copy here is how a
            # reopened chat's rows start drifting from a fresh one's
            state["log"] = make_log(state, store)
            # Patched here rather than inside rehydrate(): rehydrate takes no
            # session_dir and deliberately refuses to trust meta["id"] as a
            # path, while open_session already holds the validated one.
            state["brief"] = read_project_context(path, meta)
            # INSIDE the guard: a legacy view-only chat rehydrates to None,
            # and pinning the run on None crashed every one of them.
            state["_run"] = self._runs.focused()
        self._conv = state
        # a chat this window has not loaded cannot have a turn in flight
        return {"ok": True, "session": summary, "messages": messages,
                "thinking": [], "live": False}

    def get_tabs(self):
        """The open-tab strip, filtered to chats that still exist."""
        return read_tabs()

    def save_tabs(self, tabs):
        """Persist the strip. Called on every open/close/reorder/recolour, so
        it is deliberately cheap and never fails loudly: losing the tab layout
        must not interrupt a conversation."""
        try:
            return write_tabs(tabs or {})
        except Exception as exc:                       # pragma: no cover
            return {"error": error_excerpt(exc)}

    def tab_colors(self):
        return list(TAB_COLORS)

    def rename_session(self, session_id, title):
        path = session_path(session_id)
        if not path:
            return {"error": "That chat no longer exists."}
        # Resolve by id, not by the focused-run compatibility properties. A
        # background chat is still live even while Josh is viewing another
        # one, and renaming its files underneath the loop is unsafe.
        run = self._runs.get(session_id)
        if run is not None and run.is_running():
            return {"error": "Wait until this conversation pauses before "
                             "renaming it."}
        title = " ".join((title or "").split()).strip()
        if not title:
            return {"error": "A chat title cannot be empty."}
        meta = read_meta(path)
        if not meta:
            return {"error": "Legacy chats can be viewed but not renamed."}
        meta["title"] = title
        target = os.path.join(path, "meta.json")
        tmp = f"{target}.rename-{os.getpid()}-{threading.get_ident()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return {"error": f"Could not rename chat: {e}"}
        if run is not None and run.state:
            run.state["title"] = title
        return {"ok": True, "session": session_summary(path, meta)}

    def delete_session(self, session_id):
        path = session_path(session_id)
        if not path:
            return {"error": "That chat no longer exists."}
        # The target may be running in the background. Never derive this from
        # focus: deleting that folder while its loop persists would strand the
        # store and CLI session beneath a live worker.
        run = self._runs.get(session_id)
        if run is not None and run.is_running():
            return {"error": "Stop this conversation before deleting it."}
        try:
            shutil.rmtree(path)
        except OSError as e:
            return {"error": f"Could not delete chat: {e}"}
        self._runs.forget(session_id)
        return {"ok": True, "id": session_id}

    def export_session(self, session_id):
        """Render one chat as a self-contained HTML file and open it.

        Bridge-thread safe: export.py is bounded local file I/O — no
        subprocess, no workspace walking. Exporting a RUNNING chat is
        deliberately allowed: it reads messages.jsonl once and writes a
        separate file, so the worst case is an export missing a turn that
        landed mid-render.
        """
        path = session_path(session_id)
        if not path:
            return {"error": "That chat no longer exists."}
        res = export_mod.export_session(path)
        if res.get("error"):
            return res
        return {"ok": True, "path": res["path"], "messages": res["messages"]}

    def fork_session(self, session_id, message_id=None):
        """Branch a NEW conversation from this one, up to `message_id`.

        Refuses while the source is running (the copy would race its writes,
        exactly like rename/delete). The fork carries fresh AI memory by
        design — fork.py clears every seat's CLI session id — so opening it
        and continuing starts new provider threads at the branch point.
        """
        path = session_path(session_id)
        if not path:
            return {"error": "That chat no longer exists."}
        run = self._runs.get(session_id)
        if run is not None and run.is_running():
            return {"error": "Stop this conversation before branching it."}
        # Resolve through relay's SESSIONS_DIR, never fork.py's own default:
        # relay owns where sessions live (tests redirect it; the day it moves,
        # this stays correct instead of silently splitting in two).
        res = fork_mod.fork_session(session_id, message_id,
                                    sessions_dir=SESSIONS_DIR)
        if res.get("error"):
            return res
        # A fork can be large (it copies the workspace), but this still runs
        # on the bridge thread like delete_session's rmtree: bounded local
        # I/O, and the UI shows nothing until it answers.
        return {"ok": True, "id": res["id"],
                "session": session_summary(res["path"])}

    def set_sound(self, enabled):
        self._sound = bool(enabled)
        return {"ok": True, "sound": self._sound}

    def new_conversation(self):
        return self.reset_conversation()

    def submit_feedback(self, rating, reasons=None, note="", chat_id=None):
        """Persist the optional end-card response for ONE chat.

        Chat-scoped: rating the chat you are looking at must not write into
        whichever run happens to be focused when several are open.

        Bridge-thread safe: this is one bounded, atomic JSON update.  The UI
        never writes outcome.json itself, so outcome.set_feedback remains the
        single validator and owner of the v1 vocabulary.
        """
        run = self._runs.get(chat_id) if chat_id else self._runs.focused()
        session_dir = run.session_dir if run else None
        if not session_dir or not os.path.isdir(session_dir):
            return {"error": "There is no active conversation to rate."}
        try:
            rec = outcome.set_feedback(session_dir, rating,
                                       reasons or [], note or "")
        except ValueError as e:
            return {"error": str(e)}
        if rec is None:
            return {"error": "Could not save feedback for this conversation."}
        return {"ok": True, "feedback": rec.get("human_feedback") or {}}

    # ------------------------------------------------------- conversation --
    def start(self, cfg):
        if self._thread and self._thread.is_alive():
            return {"error": "A conversation is already running."}
        self._stop_flag.clear()
        while not self._human_q.empty():
            self._human_q.get_nowait()
        self._thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        self._thread.start()
        return {"ok": True}

    def continue_chat(self, cfg):
        """Resume ONE paused chat. `cfg["session_id"]` picks it; without one
        it is the focused chat. Another chat running elsewhere is irrelevant —
        only THIS chat already running would be a double-start."""
        chat_id = (cfg or {}).get("session_id")
        run = self._runs.get(chat_id) if chat_id else self._runs.focused()
        if run is None:
            return {"error": "No such chat in this window."}
        if run.is_running():
            return {"error": "That conversation is already running."}
        if not run.state:
            return {"error": "No conversation to continue."}
        self._runs.focus(run.id) if run.id else None
        run.stop_flag.clear()
        run.thread = threading.Thread(target=self._run_continue, args=(cfg,),
                                      daemon=True)
        run.thread.start()
        return {"ok": True}

    def reset_conversation(self):
        """Clear the stage for a new chat.

        A RUNNING chat is left alone and simply loses focus — Josh asked to
        start a new conversation without stopping the old one, and refusing
        here is what made the run registry unreachable from the UI. Only an
        idle chat gets closed out, because only an idle chat is finished.
        """
        run = self._runs.focused()
        if run.is_running():
            self._runs.new_draft()          # it keeps running in the background
            return {"ok": True, "backgrounded": run.id}
        if self._conv:
            try:
                # `ended` is display state only — continue_block never gates on
                # it, so a closed chat reopens and can still take a message
                self._conv["store"].save(self._conv, ended=True)
                with open(self._conv["transcript"], "a", encoding="utf-8") as f:
                    f.write("\n*conversation ended*\n")
            except (OSError, KeyError):
                pass
        self._conv = None
        self._view_workspace = None
        # A fresh stage rather than reusing the closed chat's Run: that Run is
        # still registered under its old id, and starting a new conversation
        # on it would leave the map pointing two ids at one object.
        self._runs.new_draft()
        return {"ok": True}

    def interject(self, text, files=None, chat_id=None):
        """Send Josh's message into ONE chat's queue.

        Chat-scoped: with several chats live, an interjection typed into the
        visible one must not land in whichever run was focused last, and an
        attachment must be saved into THAT chat's workspace.
        """
        run, err = self._resolve_chat(chat_id)
        if err:
            return {"error": err}
        text = (text or "").strip()
        if files:
            ws = (run.state or {}).get("workspace")
            if not ws:
                return {"error": "No conversation workspace to attach files to."}
            try:  # plain file IO — safe on the bridge thread (no subprocess)
                text = with_attachments(text, save_attachments(files, ws))
            except (OSError, ValueError) as e:
                return {"error": f"Could not save attachment: {e}"}
        if text:
            run.human_q.put(text)
        return {"ok": True, "text": text}

    def answer_question(self, qid, text, chat_id=None):
        """Answer (or skip, with empty text) a seat's [[ASK]] question.

        `chat_id` is accepted and ignored on purpose: qids are globally
        unique, so the waiter map alone routes the answer correctly. The UI
        passes it for symmetry with every other chat-scoped call, and a
        signature that silently rejected it would break the ask modal.

        Bridge-thread safe: a pure queue put, like interject."""
        q = self._ask_waiters.get(qid)
        if not q:
            return {"error": "That question is no longer waiting."}
        q.put((text or "").strip())
        return {"ok": True}

    def command(self, text, chat_id=None):
        """Run a slash command against ONE chat.

        Scoped for the same reason as interject: /clear or /compact must hit
        the chat Josh is looking at, never whichever run was focused last.
        """
        text = (text or "").strip()
        if not text.startswith("/"):
            return {"error": "Commands start with /."}
        run = self._runs.get(chat_id) if chat_id else self._runs.focused()
        if run is None:
            return {"error": "No such chat in this window."}
        if run.is_running():
            run.human_q.put(text)
            return {"ok": True, "note": "Queued — runs before the next turn."}
        if not run.state:
            return {"error": "No conversation yet — start one first. " + HELP_TEXT}
        head = text[1:].partition(" ")[0].lower()
        if head in ("stop", "turns"):
            return {"error": f"/{head} only applies while a conversation "
                             f"is running."}
        # idle: run directly on a worker thread (threads *spawned* by a bridge
        # call are safe for subprocess.run; the bridge thread itself is not)
        threading.Thread(target=self._do_command, args=(run.state, text),
                         daemon=True).start()
        return {"ok": True}

    def apply_role(self, seat_id, role, instructions, chat_id=None):
        """Stage one seat's role change; it lands at the next turn boundary.

        Deliberately NOT an autosaving field. Applying costs that seat a full
        CLI turn (it compacts, so the new role arrives with the seat's memory
        intact instead of amnesia) and queues a roster note to every other
        seat, so each apply has to be an explicit act — an autosaving textbox
        would spend a turn and broadcast a notice per keystroke pause.

        Bridge-thread safe: this only stages and returns. The work happens on
        the loop thread (running) or on a spawned worker (idle) — never here,
        where a subprocess would deadlock pywebview.
        """
        run = self._runs.get(chat_id) if chat_id else self._runs.focused()
        state = run.state if run else None
        if not state:
            return {"error": "No conversation yet — set roles on the seat "
                             "cards, then send your opening message."}
        try:
            idx = list(state["slot_ids"]).index(seat_id)
        except ValueError:
            return {"error": "That seat isn't part of this conversation."}
        role = " ".join((role or "").split())
        instructions = (instructions or "").strip()
        agent = state["agents"][idx]
        if (role == (agent.role or "")
                and instructions == (agent.role_instructions or "")):
            return {"error": f"That's already {agent.name}'s role."}
        with self._roles_lock:
            run.staged_roles[idx] = (role or None, instructions or None)
            running = run.is_running()
            spawn = not running and not run.roles_busy
            if spawn:
                run.roles_busy = True
        if spawn:
            threading.Thread(target=self._commit_roles_idle, args=(state, run),
                             daemon=True).start()
        return {"ok": True,
                "note": (f"Queued — {agent.name} switches at the next turn "
                         f"boundary." if running
                         else f"Applying {agent.name}'s new role…")}

    def _commit_roles_idle(self, state, run):
        try:
            # A bridge call can stage another edit after _commit_roles sees an
            # empty dict but before this worker clears run.roles_busy: the stager
            # reads busy=True, doesn't spawn, and the edit would sit unapplied
            # while the UI says "Applying…". Recheck under the same lock the
            # stager uses so that edit either spawns its own worker (it saw
            # busy=False) or is drained by this loop — never stranded.
            while True:
                self._commit_roles(state, run)
                with self._roles_lock:
                    if not run.staged_roles:
                        run.roles_busy = False
                        return
        except Exception as e:                       # never strand the flag
            self.emit("status", {"text": f"Role change failed: {error_excerpt(e)}"})
            with self._roles_lock:
                run.roles_busy = False

    def _commit_roles(self, state, run):
        """Apply staged role changes by riding the /compact path.

        The seat summarizes itself, its session resets, `introduced` flips
        False — so its next turn opens with a fresh preamble carrying the NEW
        role plus its own summary. Role change without amnesia.

        The new role is committed ONLY after compaction succeeds: a half-
        applied change would leave the seat card claiming one role while the
        live model still believes another.
        """
        while True:
            with self._roles_lock:
                if not run.staged_roles:
                    return
                i = sorted(run.staged_roles)[0]
                role, instructions = run.staged_roles.pop(i)
            agent = state["agents"][i]
            key = state["slot_ids"][i]
            old, new = agent.role or "no role", role or "no role"
            # An instructions-only edit keeps the public name: the seat itself
            # must know, but broadcasting "X is now <same role>" to the others
            # would be noise AND a tell that private instructions changed.
            public_changed = (agent.role or "") != (role or "")
            note = f"Applying {agent.name}'s role change ({old} → {new})…"
            self.emit("status", {"text": note})
            state["store"].system(note, round=state["rnd"])
            try:
                summary = compact_agent(agent)
            except Exception as e:
                note = (f"{agent.name}'s role change failed "
                        f"({str(e)[:160]}) — it stays {old}.")
                self.emit("status", {"text": note})
                state["store"].system(note, round=state["rnd"])
                self._emit_role(key, agent, ok=False)
                continue
            # log BEFORE committing: the summary was written in the old role,
            # and make_log stamps rows with the role the seat had when it spoke
            state["log"](agent.name, summary,
                         meta=f"role change: {old} → {new} — self-summary")
            agent.role, agent.role_instructions = role, instructions
            state["introduced"][i] = False
            change_note = (
                f"Josh changed your role from {old} to {new}"
                if public_changed else
                f"Josh updated the private instructions for your {new} role")
            state["pending"][i].insert(
                0, f"({change_note}. Your summary of the conversation so far, "
                   f"written before that change:)\n\n{summary}")
            # Every other seat still holds the roster from its own preamble, so
            # it would keep addressing this seat by the old role. Telling them
            # goes through pending on purpose, and that's safe: it is only the
            # immediate courtesy. Config stays authoritative and any later
            # preamble rebuilds the roster from it (ROLES_DESIGN.md).
            if public_changed:
                for j in range(len(state["agents"])):
                    if j != i:
                        state["pending"][j].append(
                            f"(Roster update from Josh: {agent.name} is now "
                            f"{new}.)")
            note = (f"{agent.name} is now {new}." if public_changed else
                    f"{agent.name}'s {new} instructions were updated.")
            self.emit("status", {"text": note})
            state["store"].system(note, round=state["rnd"])
            self._emit_role(key, agent, ok=True)
            state["store"].save(state)

    def _emit_role(self, key, agent, ok):
        """Tell the UI what the seat's role ACTUALLY is now — on failure too,
        so a card can never keep showing a change that didn't land."""
        self.emit("role_applied", {
            "speaker": key, "ok": bool(ok), "name": agent.name,
            "role": agent.role or "",
            "role_instructions": agent.role_instructions or ""})

    def _chat_id(self):
        """The id of the run this Api currently owns (session dir basename).

        Until the run registry lands there is exactly one, so this is the
        single point that has to change when there are many."""
        d = self._session_dir
        return os.path.basename(os.path.normpath(d)) if d else None

    def _resolve_chat(self, chat_id):
        """(run, error) for a chat-scoped call.

        An unknown chat_id is REFUSED, never silently applied to whichever run
        happens to be focused — stopping the wrong conversation is worse than
        not stopping at all. `None` still means "the chat I'm showing".
        """
        run = self._runs.get(chat_id) if chat_id else self._runs.focused()
        if run is None:
            return None, "No such chat in this window."
        if not run.state:
            return None, "No conversation is running."
        return run, None

    def stop(self, chat_id=None):
        """ONE press stops the whole conversation — Josh should never have to
        stop each seat (2026-08-18).

        Two halves, both required. The flag ends the LOOP at its next
        boundary; `cancel_all` kills the CLI children that would otherwise
        keep the loop from reaching that boundary for minutes, with replies
        still landing the whole time. The flag alone is what made Stop feel
        like it did nothing.
        """
        run, err = self._resolve_chat(chat_id)
        if err:
            self._stop_flag.set()      # nothing live: still honour the press
            return {"ok": True, "stopped": 0, "note": err}
        run.stop_flag.set()            # THAT chat's flag, not the focused one
        run.status = "stopping"
        killed = relay.cancel_all(run.state)
        if killed:
            self.emit("status", {"text": f"Stopping — interrupted {killed} "
                                         f"seat{'s' if killed != 1 else ''} "
                                         f"mid-turn."})
        return {"ok": True, "stopped": killed}

    def approve_plan(self, chat_id=None, plan_id=None, payload=None):
        """Josh approved (or rejected) the plan card.

        This ANSWERS the question the loop is blocked on — it does not flip
        capability flags itself. Doing that directly was a real deadlock: the
        flags changed, the card said "Executing", and the conversation thread
        stayed asleep in ask_human forever with nobody left to wake it. The
        gate in relay.plan_gate owns the unlock, and it only runs when this
        answer arrives (caught by GPT's audit, 2026-08-18).

        Bridge-thread safe: a pure queue put, exactly like answer_question.
        """
        run, err = self._resolve_chat(chat_id)
        if err:
            return {"ok": False, "error": err}
        plan = (run.state or {}).get("plan") or {}
        qid = plan.get("qid")
        if not qid or qid not in self._ask_waiters:
            return {"ok": False, "error": "No plan is waiting for approval."}
        # Stale-card guard: an old window must not approve over a newer draft.
        data = dict(payload or {})
        if plan_id and plan.get("id") and plan_id != plan["id"]:
            return {"ok": False, "error": "That plan card is out of date."}
        rev = data.get("revision")
        if rev is not None and rev != plan.get("revision"):
            return {"ok": False, "error": "That plan card is out of date."}
        self._ask_waiters[qid].put({
            "approved": data.get("approved", True),
            "goal": data.get("goal"),
            "tasks": data.get("tasks"),
        })
        return {"ok": True}

    def stop_seat(self, chat_id, seat_id):
        """Stop ONE seat without ending the conversation.

        Deliberately does NOT set the stop flag: the other seats keep going
        and this one takes the ordinary never-forge-a-turn skip path, so
        nothing it half-said is relayed to anybody.
        """
        run, err = self._resolve_chat(chat_id)
        if err:
            return {"ok": False, "error": err}
        state = run.state
        agents = state.get("agents") or []
        # the UI sends the slot id it saw on the `thinking` event; fall back to
        # the label resolver so "claude 2" works from /commands too
        slots = state.get("slot_ids") or []
        if seat_id in slots:
            i = slots.index(seat_id)
            hit = [i] if agents[i].cancel() else []
        else:
            hit = relay.cancel_seat(state, str(seat_id or ""))
        if not hit:
            return {"ok": True, "stopped": 0,
                    "note": "That seat is not mid-turn."}
        names = ", ".join(agents[i].name for i in hit)
        self.emit("status", {"text": f"Stopped {names} mid-turn; the rest of "
                                     f"the conversation continues."})
        return {"ok": True, "stopped": len(hit),
                "seats": [slots[i] if i < len(slots) else i for i in hit]}

    def _run(self, cfg):
        try:
            self._conversation(cfg)
        except Exception as e:
            self.emit("error", {"message": str(e)})
            self.emit("done", {"transcript": None,
                               "can_continue": bool(self._conv)})

    def _run_continue(self, cfg):
        try:
            self._continue(cfg)
        except Exception as e:
            self.emit("error", {"message": str(e)})
            self.emit("done", {"transcript": None,
                               "can_continue": bool(self._conv)})

    def _conversation(self, cfg):
        topic = (cfg.get("topic") or "").strip()
        opener = (cfg.get("opener") or "").strip()
        turns = max(1, int(cfg.get("turns", 10)))
        until_done = bool(cfg.get("until_done"))
        ceiling = max(1, int(cfg.get("ceiling") or DEFAULT_CEILING)) \
            if until_done else None
        yolo = bool(cfg.get("yolo"))
        # Connected apps (MCP) — Josh's real Gmail/Drive/Calendar/M365/ERP.
        # Explicit per-conversation opt-in; never inferred from yolo.
        connectors = bool(cfg.get("connectors"))
        mode, recipe, orchestration_adjustments = _app_orchestration_config(
            cfg, turns, until_done=until_done, ceiling=ceiling)
        if mode not in MODES:
            self.emit("error", {"message": f"Unknown mode {mode!r}."})
            self.emit("done", {"transcript": None})
            return
        if mode not in IMPLEMENTED_MODES:
            self.emit("error", {"message": f"Mode '{mode}' isn't available "
                                           f"yet."})
            self.emit("done", {"transcript": None})
            return
        # The normalized recipe owns the Advanced drawer's budget.  Keep the
        # engine's mechanical cap in lockstep so its lazy orchestration(state)
        # normalization does not rewrite the persisted value on first save.
        if until_done:
            ceiling = recipe["budget"]["limit"]
        else:
            turns = recipe["budget"]["limit"]
        moderator_spec = None
        if recipe["floor"] == "moderated":
            m = cfg.get("moderator") or {}
            provider = (m.get("provider") or "claude").lower()
            if provider not in AGENT_TYPES:
                self.emit("error", {"message": f"Unknown moderator provider "
                                               f"{provider!r}."})
                self.emit("done", {"transcript": None})
                return
            moderator_spec = {"provider": provider,
                              "model": m.get("model") or None,
                              "effort": m.get("effort") or None}
        supervisor_spec = (cfg.get("supervisor")
                           if recipe["workflow"] == "supervisor" else None)
        # Keep Improving. Only ever paired with the supervisor workflow from
        # the UI; validated through the engine's own normalizer so a hand-made
        # cfg cannot smuggle a limit shape the loop would misread.
        continuous_cfg = None
        raw_continuous = cfg.get("continuous")
        if isinstance(raw_continuous, dict) and raw_continuous.get("on"):
            continuous_cfg = relay.continuous_policy(raw_continuous)
        seats_cfg = cfg.get("seats")
        if seats_cfg is None:  # legacy shape: {"agents": {provider: {...}}}
            seats_cfg = [dict(id=i, provider=k, **cfg["agents"][k])
                         for i, k in enumerate(AGENT_ORDER)
                         if k in cfg.get("agents", {})]
        picked = [s for s in seats_cfg
                  if s.get("provider") in AGENT_TYPES and s.get("enabled")]
        if len(picked) < 2:
            self.emit("error", {"message": "Pick at least two participants."})
            self.emit("done", {"transcript": None})
            return
        slot_ids = [s.get("id", i) for i, s in enumerate(picked)]
        panel_state = None
        if recipe["workflow"] == "panel":
            try:
                panel_state = {"synthesizer":
                               _panel_synthesizer(cfg, slot_ids)}
            except ValueError as e:
                self.emit("error", {"message": str(e)})
                self.emit("done", {"transcript": None})
                return
        try:
            labels = assign_labels([(s["provider"], s.get("label"),
                                     s.get("model")) for s in picked])
        except ValueError as e:
            self.emit("error", {"message": str(e)})
            self.emit("done", {"transcript": None})
            return
        blockers = self._auth_blockers(s["provider"] for s in picked)
        if blockers:
            self.emit("error", {"message": " ".join(blockers)})
            self.emit("done", {"transcript": None,
                               "can_continue": bool(self._conv)})
            return

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        attachments = cfg.get("attachments") or []
        title_src = topic or opener or \
            (attachments[0].get("name", "") if attachments else "")
        slug = re.sub(r"[^a-z0-9]+", "-", title_src.lower())[:40].strip("-") or "chat"
        self._session_dir = os.path.join(SESSIONS_DIR, f"{stamp}-{slug}")
        self._view_workspace = None      # the live _conv is authoritative now
        workspace = cfg.get("workspace") or os.path.join(self._session_dir, "workspace")
        os.makedirs(self._session_dir, exist_ok=True)
        os.makedirs(workspace, exist_ok=True)
        if continuous_cfg:
            # Both of these need the real folder, which only exists now. The
            # dirty flag is snapshotted at the START on purpose: it is what
            # decides whether a green wave may commit, and asking later would
            # be asking about the seats' own edits.
            gate = continuous_cfg["gate"]
            if not gate["command"]:
                gate["command"] = relay.detect_test_command(workspace)
            gate["dirty_at_start"] = bool(relay.git_dirty(workspace))
        # attachment lines join the opener AFTER the title is set — the rail
        # title should stay the words Josh typed, not a wall of file paths
        opener = with_attachments(opener, save_attachments(attachments, workspace))
        transcript = os.path.join(self._session_dir, "transcript.md")

        agents = []
        for s, label in zip(picked, labels):
            agents.append(AGENT_TYPES[s["provider"]](
                workspace, yolo=yolo,
                model=s.get("model") or None, effort=s.get("effort") or None,
                name=label,
                role=s.get("role") or None,
                role_instructions=s.get("role_instructions") or None,
                connectors=connectors))
        providers = [s["provider"] for s in picked]

        # Full opener text is the title — the rail ellipsizes in CSS and uses
        # the rest as a tooltip, so truncating here would throw it away.
        store = SessionStore(self._session_dir)
        store.open_transcript(title_src, agents, turns)

        state = {
            "agents": agents, "slot_ids": slot_ids, "providers": providers,
            "transcript": store.transcript, "workspace": workspace,
            "topic": topic or opener, "title": title_src, "created": store.created,
            "yolo": yolo, "connectors": connectors,
            "turns": turns, "store": store, "ended": False,
            "pending": {i: [] for i in range(len(agents))},
            "introduced": [False] * len(agents),
            "floor_opened": {}, "floor_turns": {},
            "forced_next": None, "deferred_wrap": None,
            "rnd": 0, "max": turns, "mode": mode,
            "orchestration": recipe,
            "moderator": moderator_spec,
            "supervisor": supervisor_spec,
            "supervisor_trace": [],
            "supervisor_goal": None,
            "supervisor_waves": 0,
            "supervisor_wave_index": 1,
            "workstreams": None,
            "panel": panel_state,
            "continuous": continuous_cfg,
            # Keep Improving has no round cap and no ceiling of its own — the
            # limits Josh acknowledged in the warning modal are the brakes.
            "until_done": until_done or bool(continuous_cfg),
            "turn_ceiling": None if continuous_cfg else ceiling,
            "spawn": {"tier1": bool((cfg.get("spawn") or {})
                                    .get("tier1", True)),
                      "max_helpers": max(0, int((cfg.get("spawn") or {})
                                                .get("max_helpers") or 0)),
                      "helpers_used": 0,
                      "max_teams": max(0, int((cfg.get("spawn") or {})
                                              .get("max_teams") or 0)),
                      "teams_used": 0},
            # the app always has a human watching — seats may [[ASK]] Josh
            "ask": True,
        }
        state["log"] = log = make_log(state, store)
        # _session_dir was set above, so the focused run is this chat's — pin
        # it to the state before any thread can move the focus pointer.
        state["_run"] = self._runs.focused()
        if (cfg.get("plan") or {}).get("enabled"):
            # Read-only from the FIRST turn, before any seat has spoken:
            # starting in execution and downgrading later would leave a window
            # in which a seat could already have written something.
            relay.start_plan(state, cfg.get("opener") or cfg.get("topic") or "")
        self._conv = state
        # Persist before the first turn: if the app dies here, Josh's opener is
        # the only content that exists and it must still be resumable.
        store.save(state)

        self.emit("started", {
            "session_dir": self._session_dir, "workspace": workspace,
            "transcript": store.transcript, "mode": mode,
            "session": session_summary(self._session_dir),
            # Anything the engine had to correct in the requested recipe, so
            # the UI can show it in the same badges as its own adjustments.
            "orchestration_adjustments": orchestration_adjustments,
            "participants": [
                {"id": slot_ids[i], "provider": providers[i],
                 "name": agents[i].name,
                 "model": picked[i].get("model") or "default",
                 "effort": picked[i].get("effort") or "",
                 "role": agents[i].role or "",
                 "role_instructions": agents[i].role_instructions or ""}
                for i in range(len(picked))],
        })

        # After `started` so a slow read shows a status line instead of a
        # frozen window, and BEFORE the opener because compose_prompt prepends
        # the preamble to the very first prompt — the block has to be in state
        # by then. Safe to spend a subprocess here: this is the worker thread
        # `start` spawned, not the js-bridge thread.
        def brief_status_row(text):
            store.system(text, round=0)
            self.emit("status", {"text": text})

        brief = project_brief(workspace, self._session_dir,
                              spec=helper_spec([s.get("provider")
                                                for s in picked],
                                               moderator_spec,
                                               supervisor_spec),
                              enabled=cfg.get("brief", True),
                              on_status=brief_status_row)
        if brief.get("status") != "off":
            state["brief"] = brief
            if brief.get("usage"):
                relay.record_usage(state, brief["usage"], kind="brief")
            write_project_context(self._session_dir, brief)
            store.save(state)

        if opener:
            # emit the recorded row so live and replayed chats carry the
            # same keys (ts, meta, …) for this message
            self.emit("message", log("Josh (human)", opener))
            for j in state["pending"]:
                state["pending"][j].append(
                    f"Josh (human) opens the conversation: {opener}")
            store.save(state)
        self._rounds(state)

    def _continue(self, cfg):
        """Resume a finished conversation: same agents, same sessions."""
        state = self._conv
        blockers = self._auth_blockers(state["providers"])
        if blockers:
            self.emit("error", {"message": " ".join(blockers)})
            self.emit("done", {"transcript": state["transcript"],
                               "can_continue": True})
            return
        opener = (cfg.get("opener") or "").strip()
        opener = with_attachments(
            opener, save_attachments(cfg.get("attachments"), state["workspace"]))
        turns = max(1, int(cfg.get("turns", state["turns"])))
        if state.get("until_done"):
            # extend the safety ceiling instead of the round cap
            extra = max(1, int(cfg.get("ceiling") or DEFAULT_CEILING))
            state["turn_ceiling"] = state.get("turn", 0) + extra
        else:
            state["max"] = state["rnd"] + turns
        # Josh's fresh message revives the chat: a wrap-in-progress or a stale
        # [[NEXT:]] pick from the previous run is off. (Matches the old loop-
        # local closing_left, which never survived a process anyway.)
        state["closing"] = None
        state["next_speaker"] = None
        state["deferred_wrap"] = None
        if relay.continuous_on(state):
            # Continuing IS the answer to "a limit stopped it": clear the
            # announcement so the same limit can be reported again if the new
            # one is also reached, re-arm the clock, and forgive the barren
            # restart count — Josh looking at it is new information.
            pol = state["continuous"]
            pol.pop("announced_limit", None)
            pol["barren_revivals"] = 0
            state.pop("_cont_mark", None)
            limits = pol.get("limits") or {}
            for key in ("spend_usd", "hours"):
                raised = ((cfg.get("continuous") or {}).get("limits")
                          or {}).get(key)
                if raised is not None:
                    limits[key] = relay._opt_number(raised)
            still = relay.continuous_backstop(state)
            if still:
                note = (still + " Raise or clear that limit in the Keep "
                        "Improving settings before continuing, or this run "
                        "will stop again immediately.")
                state["store"].system(note, round=state["rnd"])
                self.emit("status", {"text": note})
        # Project docs may have moved since this chat started. REPORT it, never
        # swap it: the seats already hold the original text, so regenerating
        # here would give a later /clear'd seat different context than its
        # peers were given, with nothing in the transcript saying so. Same
        # posture as dead session ids — recovery is offered, not performed.
        drift = brief_drift(state.get("brief"), state["workspace"])
        if drift:
            note = (f"Project docs changed since this chat started "
                    f"({', '.join(drift)}). The seats still have the original "
                    f"text; start a new chat to pick up the new version.")
            state["store"].system(note, round=state["rnd"])
            self.emit("status", {"text": note})
        if opener:
            self.emit("message", state["log"]("Josh (human)", opener))
            for j in state["pending"]:
                state["pending"][j].append(f"Josh (human) says: {opener}")
            state["store"].save(state)
        self._rounds(state)

    def _rounds(self, state):
        """Run the shared loop, then the app's epilogue: paused footer + done.

        The loop itself lives in relay.run_rounds — anything loop-shaped goes
        there, once, for both front ends."""
        # Bound to THIS chat's run, not the focused one: Josh switching to
        # another chat mid-run must not redirect this loop's stop flag,
        # human queue or staged roles to the chat he happens to be viewing.
        run = state.get("_run")
        if run is not None:
            run.thinking.clear()     # a new run starts with nobody mid-turn
        self._set_status(run, "running")
        try:
            outcome_kind = run_rounds(state, _AppIO(self, run))
        except BaseException:
            # `failed` is a distinct terminal state from `done` and `stopped`:
            # a run Josh killed did not finish, and one that finished did not
            # break. Collapsing the three is how a rail lies to him.
            self._set_status(run, "failed")
            raise
        store = state["store"]
        store.save(state)
        with open(state["transcript"], "a", encoding="utf-8") as f:
            f.write("\n---\n*paused — reply in the app to continue*\n")
        summary = session_summary(self._session_dir)
        rec = outcome.read_outcome(self._session_dir) or {}
        self._set_status(run, "stopped" if outcome_kind == "stopped"
                         else "failed" if outcome_kind == "fatal" else "done",
                         outcome=outcome_kind)
        self.emit("done", {"transcript": state["transcript"],
                           "session_dir": self._session_dir,
                           "session": summary,
                           "feedback": rec.get("human_feedback") or {},
                           # read back from what was actually persisted rather
                           # than asserted — if a seat's id didn't save, the
                           # composer must say so instead of promising a resume
                           "can_continue": summary["can_continue"],
                           "can_continue_reason": summary["can_continue_reason"]})

    # --------------------------------------------------- slash commands --
    def _do_command(self, state, text):
        """Idle-path shim: the shared dispatcher does the work (relay.py)."""
        return dispatch_command(state, text, _AppIO(self, state.get("_run")))


ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "ai-chat.ico")


def _flash_taskbar(window):
    """Raise attention without stealing focus. Windows-only, best-effort.

    An unattended run that quietly fixed itself is exactly the thing Josh
    wants to notice when he comes back, and a real toast needs `winrt`, which
    this project does not have. Same posture as `_apply_window_icon`: an
    attention-getter must never be able to crash the app.
    """
    if sys.platform != "win32" or window is None:
        return
    try:
        u32 = ctypes.windll.user32
        try:
            hwnd = int(window.native.Handle.ToInt64())
        except Exception:
            hwnd = u32.FindWindowW(None, window.title)
        if not hwnd:
            return

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                        ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                        ("dwTimeout", ctypes.c_uint)]
        FLASHW_TRAY, FLASHW_TIMERNOFG = 0x2, 0xC
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), ctypes.c_void_p(hwnd),
                          FLASHW_TRAY | FLASHW_TIMERNOFG, 0, 0)
        u32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def _play_cue(kind):
    """A short local chime for one event kind, on its own thread, best-effort.

    Sound is the one channel that reaches Josh when the window is minimized or
    he is in another room — the same reason _flash_taskbar exists. winsound
    only; non-Windows and every failure stay silent. NEVER blocking: Beep
    holds its thread for the tone's duration, so the emitter thread must not
    call this inline.
    """
    if sys.platform != "win32":
        return
    try:
        import winsound
        for freq, ms in SOUND_CUES.get(kind, ()):
            winsound.Beep(int(freq), int(ms))
    except Exception:
        pass


# (frequency Hz, duration ms) per tone. Distinct shapes so a question can be
# told from a finished run without looking: question = rising pair ("come
# answer"), checkin = triple tap ("the watchdog spoke"), done = single low.
SOUND_CUES = {
    "question": ((740, 130), (988, 200)),
    "checkin": ((880, 90), (880, 90), (880, 90)),
    "done": ((587, 160),),
}


def _apply_window_icon(window):
    """Title-bar + taskbar icon for the RUNNING window. pythonw owns the
    process, so without WM_SETICON the window wears the stock Python icon no
    matter what the desktop shortcut shows. Windows-only, best-effort — an
    icon must never be able to crash the app."""
    if sys.platform != "win32" or not os.path.isfile(ICON_PATH):
        return
    try:
        u32 = ctypes.windll.user32
        try:                       # pywebview/WinForms exposes the native Form
            hwnd = int(window.native.Handle.ToInt64())
        except Exception:          # renderer changed shape — find by title
            hwnd = u32.FindWindowW(None, window.title)
        if not hwnd:
            return
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
        for size, which in ((16, 0), (32, 1)):   # ICON_SMALL, ICON_BIG
            h = u32.LoadImageW(None, ICON_PATH, IMAGE_ICON,
                               size, size, LR_LOADFROMFILE)
            if h:
                u32.SendMessageW(hwnd, WM_SETICON, which, h)
    except Exception:
        pass


def main():
    if sys.platform == "win32":
        # Own taskbar identity BEFORE the window exists — otherwise Windows
        # groups the app under pythonw.exe and shows Python's icon there.
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Alloy.AIChat")
        except Exception:
            pass
    api = Api()
    threading.Thread(target=api.precompute_config, daemon=True).start()
    threading.Thread(target=api.precompute_auth, daemon=True).start()
    ui = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "ui", "index.html")
    api._window = webview.create_window(
        "Alloy — many models, one conversation", ui, js_api=api,
        width=1220, height=820,
        min_size=(940, 620), background_color="#17151C")
    api._window.events.shown += lambda *a: _apply_window_icon(api._window)
    webview.start(debug=False)


if __name__ == "__main__":
    main()
