#!/usr/bin/env python3
"""AI Chat desktop app: pywebview shell around the relay engine (relay.py).

Runs a native window (WebView2) hosting ui/index.html. The conversation loop
mirrors relay.py's round-robin engine and reuses its Agent adapters verbatim.
"""

import base64
import collections
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
import speaker
import webhook as webhook_mod
import export as export_mod
import fork as fork_mod
import memory as memory_mod
import outcome
import relay
import retro as retro_mod
import schedule as schedule_mod
import stats as stats_mod
from relay import (AGENT_TYPES, PROVIDERS, SESSIONS_DIR, HELP_TEXT,
                   MODES, DEFAULT_MODE, IMPLEMENTED_MODES, DEFAULT_CEILING,
                   OX_FREE_MODELS, OX_DEFAULT_MODEL, helper_spec,
                   read_tabs, write_tabs, TAB_COLORS,
                   read_event_hooks, write_event_hooks, HOOK_EVENTS,
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
    # rail decluttering (the Archived group at the bottom of the rail)
    "archived",
    # blind-duel badge ("Awaiting your vote" / decided)
    "battle",
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


# ------------------------------------------------------------ webhook config --
# Persisted beside tabs.json (derived from relay.SESSIONS_DIR at CALL time —
# the same rule as write_tabs: a module-level constant captured at import
# would survive test redirects and throw away real state). Shape:
# {"enabled": bool, "token": "<hex>", "port": 0}. The token is generated once
# on first enable and then stable, so scripts can hardcode it.

def _webhook_cfg_path():
    return os.path.join(relay.SESSIONS_DIR, "webhook.json")


# --------------------------------------------------------- schedule config --
# Same rule, same reason: the path is JOINED at call time from
# relay.SESSIONS_DIR, never captured into a module constant at import. A
# constant here would survive a test's redirect and write schedules into
# Josh's real sessions/ folder — which is precisely what write_tabs taught,
# expensively, by throwing away the tabs he had open.
SCHEDULE_POLL_S = 30


def _schedule_path():
    return os.path.join(relay.SESSIONS_DIR, schedule_mod.SCHEDULE_FILE)


def read_webhook_config():
    try:
        with open(_webhook_cfg_path(), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def write_webhook_config(cfg):
    target = _webhook_cfg_path()
    tmp = f"{target}.tmp-{os.getpid()}-{threading.get_ident()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True


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


class HumanQueue:
    """Josh's undelivered lines for ONE chat, plus a race-free peek.

    A FIFO with the three methods `queue.Queue` was being used for, plus
    `qsize()` so a front end can say honestly how much is still waiting to be
    picked up (`Api.jobs` publishes it).

    Deliberately NOT edit or drop. The loops drain at moments no front end can
    predict — the SEQUENTIAL loop reads this once per TURN, which is minutes —
    so an edit or a delete against it would silently do nothing whenever the
    drain won the race, and look fine in parallel and free mode where drains
    are 250 ms apart. That is the repo's own recurring shape (N sites, one
    guard makes the edit meaningless), so the editable hold lives in the
    CLIENT, where a row is provably still Josh's until he presses send. This
    side only has to be truthful about what it is holding.
    """

    def __init__(self):
        self._items = collections.deque()
        self._lock = threading.Lock()

    def put(self, item):
        with self._lock:
            self._items.append(item)

    def get_nowait(self):
        with self._lock:
            if not self._items:
                raise queue.Empty
            return self._items.popleft()

    def empty(self):
        with self._lock:
            return not self._items

    def qsize(self):
        with self._lock:
            return len(self._items)

    def snapshot(self):
        """What is still waiting, oldest first. A COPY: handing out the deque
        would let a reader mutate the queue by accident.

        Not published to the UI. The dock shows what Josh is HOLDING, which is
        client-side; this is what he has already sent and cannot take back, so
        the honest thing to show of it is a count, not a list he might try to
        edit. Kept because the count comes from the same lock and a reader
        that wants the contents (a future jobs preview) should not reach into
        `_items`.
        """
        with self._lock:
            return list(self._items)


class Run:
    """One conversation this window owns — live, paused, or just being drafted.

    Everything here USED to be a singular attribute on Api (`_conv`,
    `_thread`, `_stop_flag`, …), which is precisely why a second chat could
    not start without ending the first. One Run per chat; Api keeps a map.

    `id` is the session dir basename and is None until the chat is actually
    started (a draft has no identity yet — that is what `adopt` is for).
    """

    def __init__(self, chat_id=None, background=False):
        self.id = chat_id
        self.state = None            # relay state dict (the old Api._conv)
        self.thread = None
        # Started by something OTHER than the visible stage (the webhook, and
        # every scheduled room after it). It still earns a rail row, a status
        # and a tab-less life of its own; what it must never do is take the
        # focus pointer, because that yanks the transcript Josh is reading out
        # from under him mid-sentence.
        self.background = bool(background)
        self.stop_flag = threading.Event()
        self.human_q = HumanQueue()
        self.session_dir = None
        self.view_workspace = None   # reopened view-only chat's workspace
        self.staged_roles = {}       # seat index -> staged role change
        self.roles_busy = False      # idle role worker belongs to this run
        # Per-run on purpose: a global one made a question in chat B queue
        # invisibly behind an unanswered question in chat A.
        self.ask_lock = threading.Lock()
        # `thinking` and `working` below are written from SEAT threads and read
        # from the bridge thread, and `list(d.values())` raises RuntimeError if
        # the dict changes size mid-iteration. open_session got away with an
        # unguarded read because it happens once, when Josh clicks; Api.jobs is
        # POLLED across every run at once, which turns a theoretical race into
        # a likely one. Held only around a dict copy — never around I/O.
        self.clock_lock = threading.Lock()
        self.status = "idle"         # the RunState vocabulary (see set_status)
        self.pending_ask = None
        # NO `unread` here on purpose. There used to be one; it was set to 0 in
        # this constructor and never incremented anywhere in Python, while the
        # UI read a key (`unread_count`) that Python never sent — two halves of
        # one dead path, publishing a permanent zero. Unread is a front-end
        # fact (which chat is visible, how far it is scrolled), the UI has
        # always maintained its own, and a number nobody measures is not a
        # number to publish.
        # Seats currently inside a turn: slot id -> {name, provider, started,
        # limit}. Typing indicators are LIVE-only in the UI, so reopening a
        # chat mid-turn used to wipe them and never bring them back — a room
        # with three seats 14 minutes into a 15-minute window looked exactly
        # like a dead one (2026-08-23). open_session replays this instead.
        self.thinking = {}
        # The relay's OWN work: token id -> {phase, what, detail, started}.
        # Same reason `thinking` exists — the indicator is live-only, so a
        # chat reopened while the supervisor is planning would otherwise
        # render as completely idle.
        self.working = {}

    def is_running(self):
        return bool(self.thread and self.thread.is_alive())

    def clocks(self):
        """(seats mid-turn, relay work) as copies, safe to read anywhere.

        The ONE read path for both dicts. Copying each entry as well as the
        list matters: the caller serializes these to the UI, and a shared dict
        handed out under a lock stops being protected the moment the lock is
        released.
        """
        with self.clock_lock:
            return ([dict(v) for v in self.thinking.values()],
                    [dict(v) for v in self.working.values()])


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
        # Runs that have a thread but not yet an id. A conversation spends its
        # first seconds here (the session dir, and therefore `adopt`, happens
        # well INTO _conversation), and `live()` reading only the adopted map
        # is what let a second start slip through exactly that window.
        self._pending = []
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
        disturb, and what a window close has to stop.

        Includes runs that have a thread but no id yet (`_pending`): a chat is
        live from the instant its worker starts, not from the moment it
        earns a directory, and the gap between the two is seconds of real CLI
        work.
        """
        with self._lock:
            out, seen = [], set()
            for r in list(self._runs.values()) + list(self._pending):
                if id(r) in seen or not r.is_running():
                    continue
                seen.add(id(r))
                out.append(r)
            return out

    def spawn(self, target, args=(), run=None):
        """Start a conversation worker ON a run, and record its thread.

        THE way a conversation thread comes into being. `Api.start` and
        `continue_chat` used to assign `run.thread` themselves and the webhook
        path did not — so a webhook-started chat ran with `is_running()` False
        forever and could be renamed, deleted, forked, re-continued or raced by
        a second webhook start while its seats were mid-turn.

        Precisely: the guards WORKED for chats this window started itself, and
        were blind to exactly the ones nobody was watching. (An earlier version
        of this docstring said they "refused nothing, ever" — an over-claim an
        adversarial pass caught, 2026-08-27.) `RunManager.live()` had no caller
        at all, which is what made the hole invisible.

        The thread is recorded BEFORE it starts: a guard that reads
        `is_running()` in between must see True, not a hole one instruction
        wide.
        """
        with self._lock:
            if run is None:
                run = self._draft
            # a finished pending run has either been adopted (so it is in the
            # map) or died; either way it stops being pending here rather than
            # leaking for the life of the window
            self._pending = [r for r in self._pending
                             if r is run or (r.id is None and r.is_running())]
            run.thread = threading.Thread(target=target, args=tuple(args),
                                          daemon=True)
            if run.id is None and not any(r is run for r in self._pending):
                self._pending.append(run)
        run.thread.start()
        return run

    def background(self):
        """A Run of its own for a chat nobody is watching.

        Never the draft: a webhook (and, later, a schedule) that borrowed the
        draft would adopt the very stage Josh is composing on, registering his
        half-typed room under someone else's chat id.
        """
        with self._lock:
            run = Run(background=True)
            self._pending.append(run)
            return run

    def adopt(self, run, chat_id, focus=False):
        """Give a run its identity the moment its session dir exists.

        `focus` is opt-IN because stealing the focus pointer is the dangerous
        half: a background run that took it would repaint Josh's window with a
        conversation he never opened.
        """
        with self._lock:
            # Re-adopting a run that already has an id must not leave the old
            # key pointing at it: `_runs[old]` and `_runs[new]` would be the
            # SAME object with `.id == new`, so open_session(old) served the
            # new chat's state under the old id. `fresh_stage` is what stops
            # this happening at all; this is the belt to its braces, and it is
            # what makes the failure loud instead of silent if a third start
            # path ever appears.
            if run.id and run.id != chat_id:
                if self._runs.get(run.id) is run:
                    del self._runs[run.id]
                if self._focus == run.id:
                    self._focus = None
            run.id = chat_id
            self._runs[chat_id] = run
            self._pending = [r for r in self._pending if r is not run]
            if run is self._draft:
                self._draft = Run()          # a fresh stage for the next chat
            if focus:
                self._focus = chat_id
            return run

    def fresh_stage(self):
        """The Run a NEW conversation starts on: the draft, never an adopted one.

        Josh typing into a reopened chat starts a new conversation — the UI
        clears `activeId` — but the PYTHON focus pointer still names the chat
        he reopened. So `focused()` handed `start` an already-adopted run and
        `_conversation` adopted it a second time under the new directory.
        """
        with self._lock:
            if self.focused().id is not None:
                self._focus = None           # back to the draft stage
            return self._draft

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
        # Before this run is adopted its chat_id is None, and the UI reads a
        # null chat_id as "belongs to the chat on screen". The stamp is what
        # lets it drop a background run's pre-identity events instead of
        # painting them into whatever Josh is reading.
        if self._run.background:
            payload["background"] = True
        # Track in-flight seats here rather than in Api.emit, which must stay
        # a pure enqueue, and rather than in the loop, which would have to
        # learn about front-end state it has no business knowing.
        if event == "thinking":
            with self._run.clock_lock:
                self._run.thinking[str(payload.get("speaker"))] = {
                    "speaker": payload.get("speaker"),
                    "provider": payload.get("provider"),
                    "name": payload.get("name"),
                    "limit": payload.get("limit"),
                    "idle": payload.get("idle"),
                    "started": time.time(),
                    # nothing has been heard from it YET, and silence since
                    # the turn began is exactly what the quiet clock measures
                    "lastact": time.time()}
        elif event == "activity":
            # WHEN this seat was last heard from. The engine's watchdog counts
            # SILENCE, not duration, so a jobs view with no last-activity
            # stamp has nothing to measure quiet against — and passing the
            # turn's start time instead makes "quiet" equal the whole age,
            # which is the "0:00 of 15:00" lie the shared clock rule exists to
            # prevent, in the other direction.
            with self._run.clock_lock:
                seat = self._run.thinking.get(str(payload.get("speaker")))
                if seat is not None:
                    seat["lastact"] = time.time()
        elif event == "thinking_done":
            with self._run.clock_lock:
                self._run.thinking.pop(str(payload.get("speaker")), None)
        elif event == "working":
            wid = str(payload.get("id") or "")
            with self._run.clock_lock:
                if payload.get("done"):
                    self._run.working.pop(wid, None)
                elif wid:
                    self._run.working[wid] = {
                        "id": wid, "phase": payload.get("phase"),
                        "what": payload.get("what"),
                        "detail": payload.get("detail"),
                        "started": payload.get("started") or time.time()}
        self._api.emit(event, payload)

    def drain_human(self):
        out = []
        while not self._run.human_q.empty():
            out.append(self._run.human_q.get_nowait())
        return out

    def should_stop(self):
        return self._run.stop_flag.is_set()

    def auto_title(self, state):
        # The engine's one-shot post-first-round retitle, run at this barrier
        # (no seat thread alive). emit carries the run's chat_id, so the rail
        # refreshes even for a background chat. Gated on the production flag:
        # a side call costs a real CLI invocation, and headless Api instances
        # in tests must stay token-free structurally, not by vigilance.
        if self._api._side_calls_enabled:
            relay.maybe_auto_title(state, self)

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
        # Read-aloud: the output twin of dictation. One app-wide Speaker (the
        # engine itself serializes latest-wins), probed on the same startup
        # thread for the same reason — a subprocess probe never runs here.
        self._speaker = speaker.Speaker()
        self._spk_probe = None        # speaker.probe() result, set at startup
        # Webhook trigger: the server object lives here, its config on disk
        # beside tabs.json. None until first enabled.
        self._webhook = None
        # Serialized emitter: evaluate_js is only ever called from ONE thread
        # (pywebview/WebView2 marshalling isn't documented thread-safe, and
        # parallel modes emit from several seat threads). A single queue also
        # guarantees FIFO ordering across all producers.
        self._emit_q = queue.Queue()
        # Sound cues for events that wait on Josh (question/checkin/done).
        # UI toggles it via set_sound and remembers the choice in localStorage;
        # ON by default because the events that chime are the ones that block.
        self._sound = True
        # Event hooks: user shell commands per conversation event. The cache
        # is loaded lazily off disk on first fire and refreshed by
        # set_event_hooks; underscore-prefixed like everything else here.
        self._hooks_lock = threading.Lock()
        self._hooks_cache = None
        # Production-only opt-in for side calls that cost real CLI turns
        # (currently the one-shot auto-title). main() flips this; tests
        # instantiating Api directly stay token-free by construction.
        self._side_calls_enabled = False
        # Scheduled rooms. The THREAD is deliberately absent here — see
        # start_scheduler: a poller in the constructor would run against
        # Josh's real sessions/ folder inside every suite that builds an
        # Api, and spend real CLI turns doing it.
        self._sched_thread = None
        self._sched_stop = threading.Event()
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

    # `_thread`, `_staged_roles`, `_ask_lock` and `_chat_id` used to live
    # here. They are gone rather than kept "for symmetry": every one had zero
    # callers left, and a focused-run view with no caller is a focus leak
    # waiting for its first one (Api.start read `_thread` and refused a second
    # chat on the strength of an unrelated one).
    @property
    def _stop_flag(self):
        return self._runs.focused().stop_flag

    @property
    def _human_q(self):
        return self._runs.focused().human_q

    @property
    def _session_dir(self):
        return self._runs.focused().session_dir

    @_session_dir.setter
    def _session_dir(self, value):
        # The moment a chat has a directory it has an identity, so this is the
        # one hook that registers it. Doing it here rather than at each call
        # site means no start path can forget and leave a run untracked.
        #
        # This property is BY DEFINITION a view onto the focused run, so it
        # focuses. A run that must not take the focus pointer (anything
        # background) calls `_adopt_run` directly instead — see _conversation.
        run = self._runs.focused()
        self._adopt_run(run, value)

    def _adopt_run(self, run, value):
        """Register ONE run under its session dir. Focus follows `background`.

        Split out of the `_session_dir` setter so a webhook- (or, later,
        schedule-) started conversation can earn its identity without
        repainting the window Josh is looking at.
        """
        run.session_dir = value
        if value:
            self._runs.adopt(run, os.path.basename(os.path.normpath(value)),
                             focus=not run.background)
        return run

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
        payload = {"status": status, "pending_ask": run.pending_ask}
        payload.update(extra)
        # through _emit_for so a background run's status rows carry the same
        # identity stamp as everything else it produces — a status row with a
        # null chat_id lands on whatever chat the window is showing
        self._emit_for(run, "run_status", payload)

    def run_status(self, chat_id=None):
        """Snapshot for the rail — pure cache read, safe on the bridge thread.

        `chat_id=None` returns every chat this window is holding, which is what
        the UI needs after a restart or a focus switch to repaint truthfully
        instead of guessing from the last event it happened to see.
        """
        runs = self._runs.all() if not chat_id else             [r for r in [self._runs.get(chat_id)] if r]
        return {"runs": [{"chat_id": r.id, "status": r.status,
                          "running": r.is_running(),
                          "background": r.background,
                          "pending_ask": r.pending_ask} for r in runs]}

    def jobs(self):
        """Every chat this window is holding, and what each is doing RIGHT NOW.

        Bridge-thread synchronous, like `run_status` and `list_sessions`: it
        is a bounded in-memory read with no file I/O and no subprocess. It
        takes NO conversation lock — not `state["lock"]`, not the store's —
        because it is polled while runs are mid-turn and a jobs view that can
        block behind a seat is worse than no jobs view. The only lock it takes
        is each Run's own `clock_lock`, held for a dict copy.

        No title here on purpose: a Run does not carry one (the rail gets it
        from the session summary), and inventing one from the session id would
        put a slug where Josh expects the name he gave the chat.

        `now` rides along so the UI's clocks are anchored to the same instant
        the numbers were read, rather than to whenever the reply happened to
        arrive.
        """
        out = []
        for r in self._runs.all():
            thinking, working = r.clocks()
            out.append({
                "chat_id": r.id,
                "status": r.status,
                "running": r.is_running(),
                "background": r.background,
                "pending_ask": r.pending_ask,
                # Josh's own lines still waiting to be picked up. The engine
                # queue, not the client-side dock — this is the half he cannot
                # take back (see HumanQueue).
                "queued": r.human_q.qsize(),
                "thinking": thinking,
                "working": working,
            })
        return {"jobs": out, "now": time.time()}

    # ---------------------------------------------------------- to the UI --
    def emit(self, event, payload=None):
        # non-blocking and thread-safe: callers just enqueue
        self._emit_q.put((event,
                          json.dumps({"event": event,
                                      "payload": payload or {}})))

    def _emit_for(self, run, event, payload=None):
        """Emit an event that belongs to ONE run, stamped with its identity.

        `_AppIO.emit` already does this for everything the LOOP produces;
        these are the app's own setup and failure notices, which happen before
        there is a loop and sometimes before there is an id at all. A
        background run that fails during setup has neither a chat id nor a
        rail row yet, and without the `background` stamp the UI would paint
        that failure onto whatever conversation Josh happens to be reading.
        """
        payload = dict(payload or {})
        if run is not None:
            payload.setdefault("chat_id", run.id)
            if run.background:
                payload["background"] = True
        self.emit(event, payload)

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
                # Event hooks ride the same one thread, but never ON it: the
                # command runs on its own daemon (see run_event_hook), so a
                # slow or hung hook cannot stall the emit queue.
                try:
                    self.run_event_hook(event,
                                        json.loads(data).get("payload"))
                except Exception:
                    pass
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
            "speaker": {"available": False,
                        "detail": "Still checking text-to-speech."},
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
        # Read-aloud probe: shutil.which only — never launches SAPI here
        try:
            self._spk_probe = speaker.probe()
        except Exception as exc:
            self._spk_probe = {"available": False, "detail": relay.error_excerpt(exc)}
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
            "speaker": self._spk_probe or {"available": False, "detail": ""},
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

    # ------------------------------------------------------- read-aloud --
    # The output twin of dictation (speaker.py explains the engine). SAPI is
    # local and free, so like dictation this needs no account and no key.

    def speak_text(self, text):
        """Speak one message aloud. Bridge-thread safe: Speaker.speak only
        spawns its PowerShell child on a daemon thread and returns at once,
        so nothing subprocess-shaped runs on this thread. Latest-wins —
        speaking another message replaces the current utterance."""
        if not isinstance(text, str) or not text.strip():
            return {"error": "Nothing to read aloud."}
        self._speaker.speak(text[:speaker.MAX_CHARS])
        return {"ok": True}

    def stop_speech(self):
        """Interrupt read-aloud. Safe when idle, exactly like dictation's
        stop-with-no-start: hold-to-talk loses its pointerup, and a stop
        click can race a finished utterance."""
        self._speaker.stop()
        return {"ok": True}

    def speaker_state(self):
        """Cheap poll for a play/stop button's face. Pure attribute reads."""
        return {"speaking": bool(self._speaker.speaking)}

    # ---------------------------------------------------- webhook trigger --
    # A loopback HTTP endpoint outside scripts can POST to start a chat
    # (webhook.py owns the protocol). Config rides webhook.json beside
    # tabs.json; start/stop follow the recheck_auth shape — the bridge call
    # returns at once, a worker thread binds/unbinds the socket, and the
    # truth comes back as a `webhook_status` event (a bind can fail on a
    # busy port, which the checkbox must not silently swallow).

    def get_webhook(self):
        """Current trigger state. Bridge-thread safe: one small config read
        plus in-memory server state."""
        cfg = read_webhook_config()
        return {"enabled": bool(cfg.get("enabled")),
                "running": self._webhook is not None and self._webhook.serving,
                "url": getattr(self._webhook, "url", None),
                "token": cfg.get("token") or ""}

    def set_webhook(self, enabled):
        if not isinstance(enabled, bool):
            return {"error": "Enabled must be true or false."}
        cfg = read_webhook_config()
        cfg["enabled"] = enabled
        if enabled and not cfg.get("token"):
            import secrets
            cfg["token"] = secrets.token_hex(16)
        write_webhook_config(cfg)
        threading.Thread(target=self._webhook_apply, args=(cfg,),
                         daemon=True).start()
        return {"ok": True}

    def _webhook_on_start(self, payload):
        """The webhook's on_start callback: turn a validated payload into a
        conversation. Refuses while ANY chat is live — this window runs one
        new conversation per call, and racing an active loop would fork its
        emit queue mid-turn. Runs on the webhook handler thread (a normal
        thread), so spawning the conversation worker here is safe."""
        # `live()`, not a walk of the adopted map: a chat is live from the
        # instant its worker starts, and before this refactor a WEBHOOK-started
        # chat was live with `is_running()` False — so this guard, which is the
        # one that can only ever see webhook runs, passed every single time.
        if self._runs.live():
            # raise, not return: the webhook module turns a raised
            # exception into an HTTP 500 {"error": …}, and a script must
            # see the refusal as a FAILURE, not as ok-with-error attached
            raise ValueError("A conversation is already running.")
        seatable = {p["id"] for p in Api._seatable_providers()}
        asked = [str(s) for s in payload.get("seats") or []]
        # `seats` is now honoured down to ONE, because a solo run is a
        # legitimate conversation. That also exposes the old fallback's
        # dishonesty: a payload whose providers are NONE of them seatable used
        # to become the default three-seat room and report started — the
        # strict-payload culture this module was written in ("UNKNOWN KEYS
        # REJECT", because a typo would look accepted and do nothing) applied
        # to keys but not to values. Raising surfaces it to the calling script
        # as HTTP 500, exactly like the already-running refusal above. A
        # MIXED list still drops the unknown names and runs the known ones
        # — that is a deliberate, tested contract, not the bug.
        known = [s for s in asked if s in seatable]
        if asked and not known:
            raise ValueError("No seatable provider in %r. Seatable: %s."
                             % (asked, ", ".join(sorted(seatable))))
        providers = known or [p["id"] for p in Api._seatable_providers()][:3]
        seats = [{"id": i, "provider": p, "enabled": True}
                 for i, p in enumerate(providers)]
        cfg = {"opener": payload["topic"], "turns": payload.get("turns", 10),
               "seats": seats}
        ws = payload.get("workspace")
        if ws and os.path.isdir(ws):
            cfg["workspace"] = ws
        # A Run OF ITS OWN, and spawned through the manager. Borrowing the
        # draft would adopt the stage Josh is composing on; a bare
        # threading.Thread would leave run.thread None, which is precisely how
        # every "refuse while a chat is live" guard in this app came to refuse
        # nothing. `_run`, not `_conversation`, so a failure is reported
        # instead of dying silently on a detached thread.
        run = self._runs.background()
        self._runs.spawn(self._run, (dict(cfg), run), run=run)
        return {"started": True}

    def _webhook_apply(self, cfg):
        """Worker thread: make the running server match cfg.enabled."""
        want = bool(cfg.get("enabled"))
        if want:
            token = cfg.get("token") or None
            port = int(cfg.get("port") or 0)
            try:
                srv = webhook_mod.WebhookServer(
                    self._webhook_on_start, token=token, port=port)
            except ValueError as e:      # non-loopback host refused
                self.emit("webhook_status", {"running": False, "error": str(e)})
                return
            if not srv.start():
                self.emit("webhook_status",
                          {"running": False, "error": f"Port {port} is in use."
                           if port else "Could not bind a local port."})
                return
            self._webhook = srv
            # Tell the browser fence which port is Alloy's own front door, so
            # a site pattern aimed at it is refused by NAME rather than only
            # by the blanket loopback rule. The live port, not the configured
            # one: `port: 0` means the OS picked it.
            relay.WEBHOOK_PORT = srv.port
            self.emit("webhook_status", {"running": True, "url": srv.url,
                                         "token": token or ""})
        else:
            srv, self._webhook = self._webhook, None
            relay.WEBHOOK_PORT = None
            if srv is not None:
                srv.stop()
            self.emit("webhook_status", {"running": False})

    def webhook_stop_all(self):
        """Shutdown path: release the socket so the process can exit."""
        srv, self._webhook = self._webhook, None
        relay.WEBHOOK_PORT = None
        if srv is not None:
            try:
                srv.stop()
            except Exception:
                pass

    # ---------------------------------------------------- scheduled rooms --
    # A saved room, an opening message, and a recurrence. schedule.py owns the
    # store and the time arithmetic; everything here is the bridge and the
    # poller — and the poller is started from main() ALONE (see
    # start_scheduler), never from __init__.
    #
    # Bridge-thread rules: get/save/delete/enable are bounded JSON I/O like
    # get_skills and save_tabs, so they answer synchronously. Nothing here
    # shells out.

    @staticmethod
    def _rooms_by_name():
        """One read of the rooms store, as {name: cfg}. `get_schedules` used
        to call `_room_cfg` per ROW, so listing 64 schedules meant 65 reads
        of rooms.json on the bridge thread."""
        return {r.get("name"): (r.get("cfg") or {})
                for r in relay.list_rooms().get("rooms") or ()}

    def _room_cfg(self, name):
        """One saved room's cfg by name, or None. The rooms store is the ONE
        source: a schedule keeps the room's NAME, never a copy of its config,
        so editing a room edits every schedule that starts it."""
        return Api._rooms_by_name().get(name)

    @staticmethod
    def _room_axes(cfg):
        """A room cfg reduced to the axes a standing grant is judged on.

        NORMALIZED HERE, with relay's own functions, because relay is where
        the rungs are defined — schedule.py deliberately owns the policy
        (which values are grants, what each one says) and none of the
        normalization. A second copy of `normalize_desktop` living next to
        the acknowledgement would be the browser_mcp._confine drift again,
        this time on the control that decides what runs unattended at 01:00.
        """
        cfg = cfg if isinstance(cfg, dict) else {}
        cont = relay.continuous_policy(cfg.get("continuous"))
        limits = cont.get("limits") or {}
        unbounded = bool(cont.get("on")) and (
            limits.get("spend_usd") is None and limits.get("hours") is None
            and not limits.get("watchdog_may_stop"))
        return {
            "permission": relay.normalize_permission(
                cfg.get("permission"),
                "full" if cfg.get("yolo") else relay.DEFAULT_PERMISSION),
            "connectors": bool(cfg.get("connectors")),
            "desktop": relay.normalize_desktop(cfg.get("desktop")),
            "browser": relay.normalize_browser(cfg.get("browser")),
            "continuous": bool(cont.get("on")),
            "checkin_action": (cont.get("checkin") or {}).get("action"),
            "continuous_unbounded": unbounded,
        }

    @staticmethod
    def _mark_watch(run, state=None):
        """Copy `Run.background` onto the ONE plain bool relay reads.

        `relay.unattended` must not learn an app type, so the fact crosses as
        a private key (never persisted — SessionStore.save whitelists). FOUR
        writers, one implementation: the two that BUILD a run's state
        (`_conversation`, `_continue`), `open_session`, and `interject`,
        which is Josh typing into a chat that is already running. (It said
        "three" and listed three until 2026-08-27: `open_session` — the door
        that hands a run the very focus `background` exists to withhold —
        was missing from both, and had been since the day this was written.
        A count is a claim.) That last one is not a nicety — `continue_chat` clears
        `background` because typing is proof he is watching, and typing into
        a LIVE background chat is the same proof arriving through a different
        door; without it a scheduled run he joined mid-flight would go on
        expiring his own questions. Writing one dict key from the bridge
        thread is safe for the same reason `Run.background` itself is: an
        item assignment is atomic, and `ask_abort` re-reads it per question.
        """
        state = state if state is not None else run.state
        if isinstance(state, dict):
            state["_unattended"] = bool(run.background)

    def _room_grants(self, name_or_cfg):
        """The standing grants a room hands out on EVERY scheduled run."""
        cfg = (name_or_cfg if isinstance(name_or_cfg, dict)
               else self._room_cfg(name_or_cfg))
        if cfg is None:
            return None                     # the room is gone; not "no grants"
        return schedule_mod.grants_for(Api._room_axes(cfg))

    def room_risk(self, name):
        """What the modal needs to draw the acknowledgement for ONE room:
        the grants (each with its sentence) and the controls that will do
        nothing unattended. A room that no longer exists says so rather than
        answering "no grants", which would read as "safe"."""
        cfg = self._room_cfg(name)
        if cfg is None:
            return {"error": "No saved room called %r." % name}
        axes = Api._room_axes(cfg)
        grants = schedule_mod.grants_for(axes)
        return {"ok": True, "grants": grants,
                "sentences": schedule_mod.grant_sentences(grants),
                # The WAIT crosses here, computed by relay's own
                # `ask_wait_limit` for THIS room, rather than being spelled a
                # second time in schedule.py (which imports nothing from
                # relay) — same rule as _room_axes doing the normalizing for
                # grants_for. A Keep Improving room's cap is its check-in
                # interval, so a flat ASK_WAIT_MAX here would overstate it.
                "notes": schedule_mod.unattended_notes(
                    axes,
                    relay.ask_wait_limit(
                        {"continuous": relay.continuous_policy(
                            cfg.get("continuous")),
                         "_unattended": True}))}

    def get_schedules(self):
        """Every schedule, each judged against its room AS IT IS NOW.

        `grants`/`ack_gap`/`missing_room` are computed on the way out rather
        than trusted from the record: a room can be overwritten (or deleted)
        long after a schedule was acknowledged, and a list that showed the
        stored ack would tell Josh a nightly Full-access run was still the
        one he agreed to.
        """
        rows = schedule_mod.read_schedules(_schedule_path())["schedules"]
        rooms = Api._rooms_by_name()
        out = []
        for rec in rows:
            row = dict(rec)
            cfg = rooms.get(rec["room"])
            grants = (None if cfg is None
                      else schedule_mod.grants_for(Api._room_axes(cfg)))
            row["missing_room"] = grants is None
            row["grants"] = grants or []
            row["ack_gap"] = ([] if grants is None
                              else schedule_mod.ack_gap(rec, grants))
            row["ack_sentences"] = schedule_mod.grant_sentences(row["ack_gap"])
            row["describe"] = schedule_mod.describe(rec)
            out.append(row)
        return {"ok": True, "schedules": out, "rooms": sorted(rooms),
                "poll_seconds": SCHEDULE_POLL_S}

    def save_schedule(self, spec):
        spec = spec if isinstance(spec, dict) else {}
        grants = self._room_grants(str(spec.get("room") or ""))
        if grants is None:
            return {"error": "Pick a saved room for this schedule to start."}
        try:
            rec = schedule_mod.save_schedule(_schedule_path(), spec, grants)
        except ValueError as e:
            return {"error": str(e)}
        except OSError as exc:
            return {"error": error_excerpt(exc)}
        return {"ok": True, "schedule": rec}

    def delete_schedule(self, sched_id):
        try:
            if schedule_mod.delete_schedule(_schedule_path(), sched_id):
                return {"ok": True}
        except OSError as exc:
            return {"error": error_excerpt(exc)}
        return {"error": "No such schedule."}

    def set_schedule_enabled(self, sched_id, on):
        if not isinstance(on, bool):
            return {"error": "Enabled must be true or false."}
        try:
            rec = schedule_mod.set_enabled(_schedule_path(), sched_id, on)
        except OSError as exc:
            return {"error": error_excerpt(exc)}
        if rec is None:
            return {"error": "No such schedule."}
        return {"ok": True, "schedule": rec}

    def run_schedule_now(self, sched_id):
        """Start one schedule's room this instant, without touching its clock.

        Deliberately not a `claim`: this is Josh pressing a button, so it must
        not consume the night's window or record a miss. It goes through the
        SAME `_launch_schedule`, which is what makes the acknowledgement, the
        missing-room refusal and the busy refusal identical on both paths — a
        second, friendlier code path here is exactly how a safety control ends
        up with a way around it.
        """
        for rec in schedule_mod.read_schedules(_schedule_path())["schedules"]:
            if rec["id"] == sched_id:
                try:
                    ok, text = self._launch_schedule(rec, manual=True)
                    schedule_mod.record_result(_schedule_path(), sched_id,
                                               text, ran=ok)
                except OSError as exc:
                    return {"error": error_excerpt(exc)}
                self._announce_schedule(rec, ok, text, manual=True)
                return {"ok": True, "started": ok, "text": text}
        return {"error": "No such schedule."}

    # ---- the poller ------------------------------------------------------
    # NOT started by __init__. Twenty-nine test suites construct app.Api()
    # directly (the plan said eighteen; it predates three waves), and a
    # scheduler spun up in the constructor would poll Josh's REAL sessions/
    # folder from inside every one of them and shell out to real CLIs. It
    # starts from main(), which is also where `_side_calls_enabled` is
    # flipped, for exactly the same reason.

    def start_scheduler(self):
        """Begin polling saved schedules. Called from main() and from tests
        that mean it — never from __init__."""
        if self._sched_thread is not None and self._sched_thread.is_alive():
            return False
        self._sched_stop.clear()
        self._sched_thread = threading.Thread(target=self._scheduler_loop,
                                              daemon=True)
        self._sched_thread.start()
        return True

    def stop_scheduler(self):
        """Shutdown path, and the way a test takes its scheduler back."""
        self._sched_stop.set()

    def _scheduler_loop(self):
        # `wait` rather than sleep: closing the window must not have to
        # outlast a poll interval.
        while not self._sched_stop.wait(SCHEDULE_POLL_S):
            try:
                self._scheduler_tick()
            except Exception:
                pass                        # a poller that dies is a feature
                                            # that silently stops existing

    def _scheduler_tick(self, now=None):
        """One pass: fire whatever is due. THE unit — the thread above adds
        nothing but a clock, so every test drives this directly."""
        now = now or datetime.datetime.now()
        path = _schedule_path()
        rows = schedule_mod.read_schedules(path)["schedules"]
        fired = []
        for rec in schedule_mod.due(rows, now):
            claimed, verdict, note = schedule_mod.claim(
                path, rec["id"], rec["next_run"], now)
            if claimed is None:
                continue
            if verdict == "missed":
                # never fired late: the window belonged to a time Alloy was
                # not running, and starting a nightly job at breakfast is a
                # surprise, not a service
                self._announce_schedule(claimed, False, note)
                continue
            try:
                ok, text = self._launch_schedule(claimed)
            except Exception as exc:
                # the window is already spent (claim advances FIRST), so a
                # launch that blew up must leave a sentence rather than a
                # record stuck on "starting…" until the next occurrence
                ok, text = False, "Could not start: %s" % error_excerpt(exc)
            schedule_mod.record_result(path, claimed["id"], text, ran=ok,
                                       now=now)
            self._announce_schedule(claimed, ok, text)
            fired.append((claimed["id"], ok, text))
        return fired

    def _launch_schedule(self, rec, manual=False):
        """Turn one schedule into a running conversation. (ok, sentence).

        Every refusal is a SENTENCE that lands on the record and in the
        `scheduled` event, because nobody is watching at 01:00 and a schedule
        that silently did nothing is indistinguishable from one that ran.
        """
        cfg = self._room_cfg(rec["room"])
        if cfg is None:
            return False, ("The room %r no longer exists." % rec["room"])
        grants = schedule_mod.grants_for(Api._room_axes(cfg))
        gap = schedule_mod.ack_gap(rec, grants)
        if gap:
            # The room GAINED access since this schedule was acknowledged.
            # Refusing is the whole reason the check happens here as well as
            # at save time: a room is saved by name and overwriting one is
            # documented behaviour.
            return False, ("%s now grants access this schedule was never "
                           "acknowledged for: %s."
                           % (rec["room"],
                              "; ".join(schedule_mod.grant_sentences(gap))))
        live = self._runs.live()
        if live:
            # The webhook RAISES here, because a script is waiting for an
            # answer. A schedule has nobody to answer to, so it SKIPS — and
            # says so on the record rather than queueing, which would start
            # an unattended room at an unpredictable hour.
            return False, ("Skipped — another conversation was still "
                           "running.")
        start = dict(cfg)
        start["opener"] = rec["prompt"]
        start["turns"] = rec["turns"]
        # The schedule's own identity, carried onto the run so the transcript,
        # the rail row and the `scheduled` hook all name the same thing.
        start["scheduled"] = {"id": rec["id"], "name": rec["name"],
                              "room": rec["room"],
                              "when": schedule_mod.describe(rec),
                              "manual": bool(manual)}
        run = self._runs.background()
        self._runs.spawn(self._run, (start, run), run=run)
        return True, ("Started %s%s." % (rec["room"],
                                         " (run now)" if manual else ""))

    def _announce_schedule(self, rec, started, text, manual=False):
        """One `scheduled` event per fire — started or not.

        A miss and a skip are exactly the events Josh wants a hook for, so
        the event fires for them too and carries `started: false`. It has no
        chat_id: the conversation, if there is one, emits its own `started`
        with its own identity a moment later, and stamping this with the
        FOCUSED chat would paint a background notice onto whatever Josh has
        open (the `_emit_for` lesson).
        """
        self.emit("scheduled", {
            "id": rec.get("id"), "name": rec.get("name"),
            "room": rec.get("room"), "started": bool(started),
            # a hook has to be able to tell a timer from a button press
            "manual": bool(manual),
            "text": "%s — %s" % (rec.get("name") or rec.get("room"), text),
        })

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

    # ------------------------------------------------- stats + playbook --
    # Both READ every session's outcome.json with no cap, so both follow the
    # recheck_auth shape: answer {"ok": True} at once, work on a worker
    # thread, and deliver the truth as an event. A bridge-thread scan of 300
    # session folders would freeze the window.

    # ------------------------------------------------------------ memory --
    # Bounded file I/O only -- a few hundred short notes -- so these answer on
    # the BRIDGE thread like get_skills/save_skill, not through the
    # recheck_auth worker shape that get_stats needs for its uncapped scan.

    def _memory_scope(self, chat_id=None):
        """This chat's memory scope, or the global one when no chat is open.

        The modal can be opened before any conversation exists, and a global
        scope is a true answer there -- Josh's own notes reach every chat. It
        is NOT _active_workspace's rule relaxed: an unknown chat_id still must
        never resolve to the FOCUSED chat's project, so this uses the same
        lookup and falls back to `global`, never to whatever is on screen.
        """
        run = self._runs.get(chat_id) if chat_id else self._runs.focused()
        if run is None:
            return memory_mod.GLOBAL_SCOPE, ""
        ws = (run.state or {}).get("workspace") or run.view_workspace
        return relay.memory_scope_for(run.session_dir or "", ws or "")

    def _memory_payload(self, scope, label):
        got = memory_mod.collect(relay.MEMORY_DIR, scope)
        return {"scope": scope, "label": label, "entries": got["entries"],
                "truncated": bool(got.get("truncated")),
                "error": got.get("error"),
                "global_scope": memory_mod.GLOBAL_SCOPE}

    def get_memory(self, chat_id=None):
        """Exactly what this chat's seats are shown, each row tagged with the
        file it came from."""
        try:
            scope, label = self._memory_scope(chat_id)
            return self._memory_payload(scope, label)
        except Exception as e:
            return {"error": relay.error_excerpt(e)}

    def save_memory(self, text, everywhere=False, chat_id=None):
        """Josh's own note. `everywhere` forces the global scope.

        The picker is the ONLY way a josh-global note can be written from a
        project chat, and without it the crossing rule that carries his notes
        into every project would be reachable from scratch chats alone.
        """
        try:
            scope, label = self._memory_scope(chat_id)
            target = memory_mod.GLOBAL_SCOPE if everywhere else scope
            got = memory_mod.remember(relay.MEMORY_DIR, target, text,
                                      kind=memory_mod.KIND_JOSH, who="Josh")
            if "error" in got:
                return {"error": got["error"]}
            out = self._memory_payload(scope, label)
            out.update(ok=True, id=got["id"], note=got.get("note") or "")
            return out
        except Exception as e:
            return {"error": relay.error_excerpt(e)}

    def forget_memory(self, entry_id, scope=None, chat_id=None):
        """Remove one note. The scope must be one this chat can actually see.

        Checked rather than trusted: the id and the scope both arrive from
        the page, and an unchecked scope would let a stray value reach any
        project's file -- the one operation here that cannot be undone.
        """
        try:
            own, label = self._memory_scope(chat_id)
            target = scope or own
            if target not in (own, memory_mod.GLOBAL_SCOPE):
                return {"error": "That note does not belong to this chat."}
            got = memory_mod.forget(relay.MEMORY_DIR, target, entry_id)
            if "error" in got:
                return {"error": got["error"]}
            out = self._memory_payload(own, label)
            out.update(ok=True, removed=got["removed"])
            return out
        except Exception as e:
            return {"error": relay.error_excerpt(e)}

    def get_stats(self):
        """Cross-session totals, per provider and per model."""
        def work():
            try:
                payload = stats_mod.gather(relay.SESSIONS_DIR)
            except Exception as e:                    # a stats page is
                payload = {"error": relay.error_excerpt(e)}   # decoration
            self.emit("stats", payload)
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def get_playbook(self):
        """The playbook, REFRESHED — scan, derive, merge, write.

        Deliberately the full `/retro` pass rather than a read: the playbook
        file is what `relay.playbook_block` interpolates into every
        Supervisor plan, so a tab that only displayed a stale copy would
        show Josh rules the planner is not actually using. `write_playbook`
        is atomic with a unique temp name, so this racing a running /retro
        is safe (last rename wins, which is the intended semantic).
        """
        def work():
            try:
                book, report, path = retro_mod.run_retro(relay.SESSIONS_DIR)
                records = retro_mod.scan_outcomes(relay.SESSIONS_DIR)
                payload = {"summary": retro_mod.summarize(records, book),
                           "rules": retro_mod.rules_for_display(book),
                           "report": report, "path": path,
                           "updated": book.get("updated")}
            except Exception as e:
                payload = {"error": relay.error_excerpt(e)}
            self.emit("playbook", payload)
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def set_playbook_rule(self, heuristic_id, pinned=None, dismissed=None,
                          directive=None):
        """Josh's editorial decision on ONE rule. Bounded file I/O, so it
        answers on the bridge thread like save_skill — no scan, no derive."""
        try:
            book = retro_mod.set_rule(relay.SESSIONS_DIR, heuristic_id,
                                      pinned=pinned, dismissed=dismissed,
                                      directive=directive)
        except Exception as e:
            return {"error": relay.error_excerpt(e)}
        if book is None:
            return {"error": "No rule with that id — refresh the playbook."}
        return {"ok": True, "rules": retro_mod.rules_for_display(book),
                "updated": book.get("updated")}

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
            # He is looking at it, and the focus `background` exists to
            # withhold has just been handed to this run by his own click — so
            # the flag is no longer true of it. Without this, `continue_chat`
            # was the only route that cleared it, and `continue_chat` REFUSES
            # while a run is live: a scheduled chat Josh opened mid-flight
            # went on expiring the questions he was sitting there to answer.
            existing.background = False
            Api._mark_watch(existing)
            thinking, working = existing.clocks()
            return {"ok": True,
                    "session": session_summary(existing.session_dir),
                    "messages": read_messages(existing.session_dir),
                    # so the UI can put the typing indicators back rather than
                    # showing a grinding chat as an idle one
                    "thinking": thinking,
                    # same rule for the relay's own work: a chat reopened
                    # mid-plan must not render as idle either
                    "working": working,
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

        # `focus` above already made this chat's run the focused one, so the
        # three writes below land on it. Named explicitly all the same: a
        # focused-run view read three lines from a focus switch is how the
        # next reader assumes the wrong run.
        run = self._runs.focused()
        self._adopt_run(run, path)
        # view-only chats have no live state; the Files rail and inline
        # previews still need THIS chat's recorded workspace (may be gone —
        # confine/read handle that with placeholders, never a broken tag)
        run.view_workspace = summary.get("workspace") or None
        while not run.human_q.empty():
            run.human_q.get_nowait()

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
                "thinking": [], "working": [], "live": False}

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

    # ------------------------------------------------------- saved rooms --
    # Room templates are one JSON file beside tabs.json: bounded file I/O,
    # so these stay SYNCHRONOUS on the bridge thread exactly like
    # get_skills/save_tabs — never a subprocess there.

    def get_rooms(self):
        """Saved room templates for the Rooms modal, newest first."""
        return relay.list_rooms()

    def save_room(self, name, cfg):
        """Persist the stage config under `name`; an existing name is
        overwritten (documented in relay.save_room)."""
        try:
            return relay.save_room(name, cfg or {})
        except ValueError as e:
            return {"error": str(e)}
        except OSError as exc:
            return {"error": error_excerpt(exc)}

    def delete_room(self, name):
        if relay.delete_room(name):
            return {"ok": True}
        return {"error": "No saved room with that name."}

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

    def set_archived(self, session_id, archived):
        """Rail decluttering: move a chat into/out of the Archived group.

        Bridge-thread safe (bounded file I/O — one meta read + atomic write,
        the rename_session shape). Archiving is the opposite of deleting on
        purpose: the folder, workspace and resumability are untouched. A
        RUNNING chat refuses for the same reason rename does — its loop
        rewrites meta.json at every commit and would race or un-archive the
        flag on the next save.
        """
        path = session_path(session_id)
        if not path:
            return {"error": "That chat no longer exists."}
        run = self._runs.get(session_id)
        if run is not None and run.is_running():
            return {"error": "Wait until this conversation pauses before "
                             "archiving it."}
        meta = read_meta(path)
        if not meta:
            return {"error": "Legacy chats can be viewed but not archived."}
        meta["archived"] = bool(archived)
        target = os.path.join(path, "meta.json")
        tmp = f"{target}.archive-{os.getpid()}-{threading.get_ident()}"
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
            return {"error": f"Could not update chat: {e}"}
        return {"ok": True, "session": session_summary(path, meta)}

    def vote_battle(self, choice, chat_id=None):
        """Record the human's verdict on a blind duel and move Elo.

        Bridge-thread safe (submit_feedback's class: bounded atomic JSON
        writes, no subprocess). The verdict lands twice on purpose — in the
        session's meta (so the rail badge and the reveal survive restarts)
        and in leaderboard.json (the cross-battle tally). A second vote is
        refused rather than re-scored: Elo already moved once.
        """
        choice = (choice or "").strip().lower()
        if choice not in relay.BATTLE_CHOICES:
            return {"error": "Vote must be one of: a, b, tie, bad."}
        run = self._runs.get(chat_id) if chat_id else self._runs.focused()
        session_dir = run.session_dir if run else None
        if not session_dir or not os.path.isdir(session_dir):
            return {"error": "There is no battle to vote on."}
        meta = read_meta(session_dir)
        if not meta:
            return {"error": "This chat has no readable record."}
        pair = relay.battle_seats(meta)
        if not pair:
            return {"error": "This conversation is not a battle."}
        b = meta.get("battle") or {}
        if b.get("phase") == relay.BATTLE_VOTED:
            return {"error": "You already voted on this battle."}
        board = relay.read_leaderboard()
        relay.apply_battle_result(board, pair[0]["key"], pair[1]["key"],
                                  choice)
        relay.write_leaderboard(board)
        b["phase"] = relay.BATTLE_VOTED
        b["verdict"] = choice
        b["voted_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        meta["battle"] = b
        target = os.path.join(session_dir, "meta.json")
        tmp = f"{target}.vote-{os.getpid()}-{threading.get_ident()}"
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
            return {"error": f"Could not record the vote: {e}"}
        if run is not None and run.state:
            run.state["battle"] = b     # continuation now rides run_parallel
        self.emit("battle_revealed", {
            "verdict": choice,
            "contestants": [
                {"slot": pair[0]["slot"], "letter": "A",
                 "provider": pair[0]["provider"], "model": pair[0]["model"],
                 "rating": board["ratings"].get(pair[0]["key"])},
                {"slot": pair[1]["slot"], "letter": "B",
                 "provider": pair[1]["provider"], "model": pair[1]["model"],
                 "rating": board["ratings"].get(pair[1]["key"])},
            ],
            "games": board["games"],
        })
        return {"ok": True}

    def get_leaderboard(self):
        """Cross-battle Elo tally. Bridge-thread safe (one small JSON read)."""
        return relay.read_leaderboard()

    def react_message(self, message_id, verdict, chat_id=None, note=None):
        """One per-message thumb feeding outcome.json. Bridge-thread safe
        (submit_feedback's class: bounded atomic JSON via outcome.py, whose
        set_reaction is the single validator of the vocabulary). verdict None
        toggles the reaction off.

        `note` is Josh's own words about this message. It is three-state on
        purpose — None leaves an existing note alone, "" clears it — so the
        thumb buttons, which never pass one, cannot delete what he typed."""
        run = self._runs.get(chat_id) if chat_id else self._runs.focused()
        session_dir = run.session_dir if run else None
        if not session_dir or not os.path.isdir(session_dir):
            return {"error": "There is no conversation to react to."}
        try:
            rec = outcome.set_reaction(session_dir, message_id, verdict,
                                       note=note)
        except ValueError as e:
            return {"error": str(e)}
        if rec is None:
            return {"error": "Could not save the reaction."}
        return {"ok": True,
                "reaction": (rec.get("human_feedback", {})
                             .get("reactions", {}).get(message_id))}

    def get_reactions(self, chat_id=None):
        """The chat's per-message thumbs, for repainting buttons on reopen.
        Bridge-thread safe: one small JSON read."""
        run = self._runs.get(chat_id) if chat_id else self._runs.focused()
        session_dir = run.session_dir if run else None
        if not session_dir:
            return {}
        rec = outcome.read_outcome(session_dir) or {}
        fb = rec.get("human_feedback")
        if not isinstance(fb, dict):
            return {}
        rx = fb.get("reactions")
        return rx if isinstance(rx, dict) else {}

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

    # -------------------------------------------------------- event hooks --
    def _hook_command(self, hook_name):
        """The configured command for one event, or None. Lazy-loads the file
        once; a corrupt read degrades to "no hooks" (never raises)."""
        with self._hooks_lock:
            if self._hooks_cache is None:
                try:
                    self._hooks_cache = read_event_hooks().get("hooks") or {}
                except Exception:
                    self._hooks_cache = {}
            return self._hooks_cache.get(hook_name)

    def run_event_hook(self, event, payload=None):
        """Fire the user's shell command for one conversation event.

        Called from the ONE emitter thread where _play_cue/_flash_taskbar
        fire — which is exactly why this must never block it: the command
        runs on a fresh daemon thread with a hard timeout, DEVNULL pipes and
        CREATE_NO_WINDOW, and every failure is swallowed (the same contract
        as activity narration). Zero overhead when nothing is configured.
        Returns the spawned Thread so tests can join it (None = nothing to do).
        """
        payload = payload if isinstance(payload, dict) else {}
        if event == "gate":
            # wave_gate emits both colours; only a RED gate is worth a buzz.
            if payload.get("ok") is not False:
                return None
            hook_name = "gate_red"
        elif event in ("question", "checkin", "done", "scheduled"):
            # `scheduled` fires for a SKIP and a MISS too, not only a
            # start: a nightly room that did not run is exactly the thing
            # a notification hook exists for. The payload's `started`
            # flag says which, and `text` is the sentence.
            hook_name = event
        else:
            return None
        command = self._hook_command(hook_name)
        if not command:
            return None
        detail = ""
        for key in ("text", "question", "message", "command"):
            if payload.get(key):
                detail = str(payload[key])
                break
        # THE FIRING RUN, not the focused one. Every payload that reaches
        # here carries its own identity — `_AppIO.emit` stamps `chat_id` on
        # everything the loop produces, and `done` carries the session summary
        # — so reading the focus pointer meant a background chat's hook fired
        # with the id of whatever conversation Josh happened to be reading,
        # which is worse than firing with none: a script that acts on
        # AICHAT_SESSION would act on the wrong conversation.
        session_id = (payload.get("chat_id")
                      or payload.get("session_id")
                      or (payload.get("session") or {}).get("id")
                      or "")
        title = ""
        if hook_name == "scheduled":
            # A schedule is not a conversation event, and the fallback two
            # lines down is the W2.0 bug in miniature: with no chat_id it
            # would hand the script whatever chat Josh happens to be
            # READING (measured: AICHAT_SESSION="some-other-chat"), and a
            # hook that acts on AICHAT_SESSION would act on that one. The
            # schedule names itself instead; if it started a chat, that
            # chat emits its own `started` with its own identity.
            env = hook_environment(hook_name, "",
                                   str(payload.get("name") or
                                       payload.get("room") or ""), detail)
            worker = threading.Thread(target=self._hook_worker,
                                      args=(hook_name, command, env),
                                      daemon=True)
            worker.start()
            return worker
        try:
            run = (self._runs.get(session_id) if session_id
                   else self._runs.focused())
            if run is not None:
                if not session_id and run.id:
                    session_id = run.id
                # in-memory only: the emitter thread must never do file I/O
                title = str((run.state or {}).get("title") or "")
        except Exception:
            pass
        env = hook_environment(hook_name, session_id, title, detail)
        worker = threading.Thread(target=self._hook_worker,
                                  args=(hook_name, command, env), daemon=True)
        worker.start()
        return worker

    def _hook_worker(self, hook_name, command, env):
        try:
            _execute_command(command, env)
        except Exception:
            pass

    def get_event_hooks(self):
        """Bridge-thread safe: ONE small bounded JSON read (like
        list_sessions), no subprocess anywhere near it."""
        with self._hooks_lock:
            try:
                data = read_event_hooks()
                self._hooks_cache = data.get("hooks") or {}
            except Exception:
                data = {"version": 1, "hooks": {}}
                self._hooks_cache = {}
        return {"ok": True, "events": list(HOOK_EVENTS),
                "hooks": data.get("hooks") or {}}

    def set_event_hooks(self, hooks):
        """Persist hook commands: the recheck_auth shape — answer {"ok": True}
        at once, do the file write on a worker thread, report with a
        hooks_status event. Unknown names reject HERE (cheap, no I/O) so the
        UI sees the refusal immediately."""
        hooks = hooks if isinstance(hooks, dict) else {}
        for name in hooks:
            if name not in HOOK_EVENTS:
                return {"error": "Unknown hook event %r — expected one of: %s."
                                 % (name, ", ".join(HOOK_EVENTS))}
        threading.Thread(target=self._set_hooks_worker, args=(dict(hooks),),
                         daemon=True).start()
        return {"ok": True}

    def _set_hooks_worker(self, hooks):
        try:
            data = write_event_hooks(hooks)
            with self._hooks_lock:
                self._hooks_cache = data.get("hooks") or {}
            self.emit("hooks_status", {"ok": True})
        except Exception as e:
            self.emit("hooks_status",
                      {"ok": False, "error": str(e)[:200]})

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
        # The DRAFT, not whatever is focused: see RunManager.fresh_stage.
        run = self._runs.fresh_stage()
        if run.is_running():
            return {"error": "A conversation is already running."}
        run.stop_flag.clear()
        while not run.human_q.empty():
            run.human_q.get_nowait()
        # spawn() records the thread BEFORE starting it, so the run is live to
        # every guard from the first instruction — see RunManager.spawn.
        self._runs.spawn(self._run, (cfg, run), run=run)
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
        # Josh reopened it and typed into it, so it is his chat now whatever
        # started it — a resumed background run stops being background.
        run.background = False
        run.stop_flag.clear()
        self._runs.spawn(self._run_continue, (cfg, run), run=run)
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
        # BEFORE the attachments are written. Refusing after saving them
        # leaves the files in the workspace with no message that names them —
        # `prepare_message`, its twin, has always checked first.
        if text.startswith("/"):
            return {"error": "That starts with / — send it as a command, or "
                             "put a space or a word in front of it."}
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
            # He is typing into it, so somebody is watching it — the same
            # proof `continue_chat` acts on, arriving through the live door.
            run.background = False
            Api._mark_watch(run)
        # How many of Josh's lines are still waiting to be picked up, counted
        # at the moment his own was added. True when it is said; deliberately
        # not polled (see HumanQueue on why this side does not pretend to be
        # editable) — Api.jobs publishes the live figure.
        return {"ok": True, "text": text, "waiting": run.human_q.qsize()}

    def prepare_message(self, text, files=None, chat_id=None):
        """Save a message's attachments WITHOUT delivering the message.

        The queue dock holds Josh's rows in the browser, where an edit or a
        delete is provably his until he presses send. Attachment BYTES cannot
        live there — they belong in the chat's working folder, where the seats
        can open them and the Files rail can list them — so they are written
        now and the returned text carries the `[Josh attached a file: …]`
        lines the row will send verbatim.

        The consequence is stated rather than hidden: deleting a held row
        cannot unwrite those files, so the dock's delete says how many it is
        leaving behind.

        Bridge-thread safe, exactly like interject: bounded file IO, no
        subprocess.
        """
        run, err = self._resolve_chat(chat_id)
        if err:
            return {"error": err}
        text = (text or "").strip()
        if text.startswith("/"):
            return {"error": "That starts with / — send it as a command, or "
                             "put a space or a word in front of it."}
        saved = []
        if files:
            ws = (run.state or {}).get("workspace")
            if not ws:
                return {"error": "No conversation workspace to attach files to."}
            try:
                saved = save_attachments(files, ws)
            except (OSError, ValueError) as e:
                return {"error": f"Could not save attachment: {e}"}
        if not text and not saved:
            return {"error": "Nothing to queue."}
        return {"ok": True, "text": with_attachments(text, saved),
                "attached": len(saved)}

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
                with relay.working(_AppIO(self, state.get("_run")), "compact",
                                   agent.name,
                                   label=f"Applying {agent.name}'s role change"):
                    summary = compact_agent(
                        agent, solo=len(state["agents"]) == 1)
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
            # Only when Josh did not NAME a chat. `_resolve_chat` exists so
            # that "an unknown chat_id is REFUSED, never silently applied to
            # whichever run happens to be focused — stopping the wrong
            # conversation is worse than not stopping at all", and this line
            # was doing precisely that for every id it did not recognise.
            if chat_id:
                return {"ok": False, "stopped": 0, "error": err}
            self._runs.focused().stop_flag.set()   # honour a bare press
            return {"ok": True, "stopped": 0, "note": err}
        run.stop_flag.set()            # THAT chat's flag, not the focused one
        # through _set_status, not a bare assignment: this was the one status
        # write that emitted nothing, so the rail and the jobs view could not
        # see a chat enter "stopping" until something else happened to emit
        self._set_status(run, "stopping")
        killed = relay.cancel_all(run.state)
        if killed:
            # _emit_for, like every other run-scoped notice: a bare emit lands
            # in whatever chat is on screen, which for a backgrounded run is
            # somebody else's transcript.
            self._emit_for(run, "status",
                           {"text": f"Stopping — interrupted {killed} "
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

    def approve_board(self, chat_id=None, board_id=None, payload=None):
        """Josh answered the Supervisor's board card.

        Like `approve_plan`, this ANSWERS the question the loop is blocked on
        and changes nothing itself: `relay.board_gate` owns the merge and the
        dispatch, and it only runs when this answer arrives. Flipping the
        board here instead would leave the conversation thread asleep in
        ask_human with the card saying "approved" — the deadlock the plan
        card's own audit found in 2026-08-18.

        A separate method and a separate state key from approve_plan on
        purpose: `state["plan"]` belongs to Plan Mode, is written with no mode
        check and rehydrates, so a supervisor chat can arrive already holding
        one.

        Bridge-thread safe: a pure queue put, exactly like answer_question.
        """
        run, err = self._resolve_chat(chat_id)
        if err:
            return {"ok": False, "error": err}
        board = (run.state or {}).get("board") or {}
        qid = board.get("qid")
        if not qid or qid not in self._ask_waiters:
            return {"ok": False, "error": "No board is waiting for review."}
        data = dict(payload or {})
        # Stale-card guard: an old window must not approve over a newer wave.
        if board_id and board.get("id") and board_id != board["id"]:
            return {"ok": False, "error": "That board is out of date."}
        rev = data.get("revision")
        if rev is not None and rev != board.get("revision"):
            return {"ok": False, "error": "That board is out of date."}
        self._ask_waiters[qid].put({
            "approved": bool(data.get("approved", True)),
            "tasks": data.get("tasks"),
            "feedback": data.get("feedback") or "",
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
        # "the rest of the conversation continues" is the opposite of what
        # happens with one seat: there is no rest, and the sequential floor
        # parks the only seat and ends the run.
        self.emit("status", {"text": (
            f"Stopped {names} mid-turn; this turn is skipped."
            if len(agents) == 1 else
            f"Stopped {names} mid-turn; the rest of the conversation "
            f"continues.")})
        return {"ok": True, "stopped": len(hit),
                "seats": [slots[i] if i < len(slots) else i for i in hit]}

    def _run(self, cfg, run=None):
        run = run if run is not None else self._runs.focused()
        try:
            self._conversation(cfg, run)
        except Exception as e:
            self._emit_for(run, "error", {"message": str(e)})
            self._emit_for(run, "done", {"transcript": None,
                                         "can_continue": bool(run.state)})

    def _run_continue(self, cfg, run=None):
        run = run if run is not None else self._runs.focused()
        try:
            self._continue(cfg, run)
        except Exception as e:
            self._emit_for(run, "error", {"message": str(e)})
            self._emit_for(run, "done", {"transcript": None,
                                         "can_continue": bool(run.state)})

    def _conversation(self, cfg, run=None):
        # PINNED, never re-read from the focus pointer. Everything below used
        # to go through the `self._conv` / `session_dir` views, which
        # resolve to whatever chat the WINDOW is showing — so a conversation
        # this window did not start from the visible stage (the webhook) wrote
        # its state, its directory and its identity onto Josh's draft, or onto
        # whichever chat he had open.
        run = run if run is not None else self._runs.focused()
        emit = lambda event, payload=None: self._emit_for(run, event, payload)
        topic = (cfg.get("topic") or "").strip()
        opener = (cfg.get("opener") or "").strip()
        turns = max(1, int(cfg.get("turns", 10)))
        until_done = bool(cfg.get("until_done"))
        ceiling = max(1, int(cfg.get("ceiling") or DEFAULT_CEILING)) \
            if until_done else None
        # PERMISSION RUNG — the same resolution relay's own CLI does
        # (`normalize_permission(args.permission, "full" if args.yolo else …)`):
        # the named rung wins, `yolo` survives ONLY as the legacy spelling of
        # "full", and anything unrecognised falls back to DEFAULT_PERMISSION
        # rather than granting more than was asked for. Until 2026-08-26 this
        # read `yolo` alone, so the composer's permission pill was decorative
        # in the app: "Read only" and "Ask first" both arrived here as False
        # and every seat ran at the "auto" default (claude --permission-mode
        # acceptEdits, codex workspace-write, opencode --auto). Only "Full
        # access" ever differed, and only through the legacy key.
        permission = relay.normalize_permission(
            cfg.get("permission"),
            "full" if cfg.get("yolo") else relay.DEFAULT_PERMISSION)
        yolo = permission == "full"
        # Connected apps (MCP) — Josh's real Gmail/Drive/Calendar/M365/ERP.
        # Explicit per-conversation opt-in; never inferred from yolo.
        connectors = bool(cfg.get("connectors"))
        # Desktop control — a separate axis from `permission`, because that
        # one bounds the workspace and this one bounds Josh's actual screen.
        # normalize_desktop reads anything it does not recognise as OFF, so a
        # typo or a stale cfg key cannot hand a seat the mouse.
        desktop = relay.normalize_desktop(cfg.get("desktop"))
        desktop_allowlist = [str(p) for p in (cfg.get("desktop_allowlist") or ())
                             if str(p).strip()]
        # Browser control — a THIRD axis, bounding the open web. The rung is
        # CLAMPED against the site list here rather than taken as typed: a
        # pattern Alloy refuses (file:, chrome:, Alloy's own webhook port)
        # must not leave an unattended run believing its boundary is wider
        # than it is, and a list with nothing usable must not offer clicking
        # on a browser that can reach nothing.
        browser = relay.normalize_browser(cfg.get("browser"))
        browser_sites = [str(p) for p in (cfg.get("browser_sites") or ())
                         if str(p).strip()]
        # Collected here (the rung is needed before `started`, to build the
        # agents) and SAID after it, as persisted system rows — so a reopened
        # chat still shows why its boundary is what it is.
        browser_notes = []
        if browser != "off":
            kept_sites, rejected_sites = relay.browser_site_report(browser_sites)
            asked = browser
            browser = relay.clamp_browser_rung(browser, browser_sites)
            for pattern, why in rejected_sites:
                browser_notes.append("Alloy refused the browser site %r: %s"
                                     % (pattern, why))
            if not kept_sites:
                browser_notes.append(
                    "No usable browser sites, so Chrome can reach nothing at "
                    "all. Add a site to make browsing possible.")
            if browser != asked:
                browser_notes.append(
                    "Browser control lowered from %r to %r for this chat."
                    % (relay.BROWSER_RUNGS[asked]["label"],
                       relay.BROWSER_RUNGS[browser]["label"]))
        mode, recipe, orchestration_adjustments = _app_orchestration_config(
            cfg, turns, until_done=until_done, ceiling=ceiling)
        if mode not in MODES:
            emit("error", {"message": f"Unknown mode {mode!r}."})
            emit("done", {"transcript": None})
            return
        if mode not in IMPLEMENTED_MODES:
            emit("error", {"message": f"Mode '{mode}' isn't available "
                 f"yet."})
            emit("done", {"transcript": None})
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
                emit("error", {"message": f"Unknown moderator provider "
                     f"{provider!r}."})
                emit("done", {"transcript": None})
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
        # ONE seat is a conversation: Alloy is a harness for a single agent as
        # well as a room for several. The seat-count rules that remain live in
        # relay.MODE_SEAT_LIMITS, so the bridge, the CLI and the loops all
        # refuse with the same sentence for the same reason — a battle over
        # one answer, a reactive room with nobody to react to. Zero always
        # refuses.
        refusal = relay.seat_count_refusal(mode, len(picked))
        if refusal:
            emit("error", {"message": refusal})
            emit("done", {"transcript": None})
            return
        slot_ids = [s.get("id", i) for i, s in enumerate(picked)]
        panel_state = None
        battle_state = None
        if recipe["workflow"] == "battle":
            # A duel is two-boxing by definition: one answer can't be ranked
            # and three makes the A/B vote a lie. seat_count_refusal above
            # owns BOTH bounds for mode "battle", so this is unreachable for
            # any cfg the UI builds - it survives only as the backstop for a
            # hand-made cfg whose legacy `mode` disagrees with its workflow,
            # which is the one way the refusal above can be sidestepped.
            if len(picked) != 2:
                emit("error", {"message": "A battle needs exactly two "
                     "participants."})
                emit("done", {"transcript": None})
                return
            battle_state = {"phase": "blind",
                            "slots": sorted(slot_ids)[:2]}
        if recipe["workflow"] == "panel":
            try:
                panel_state = {"synthesizer":
                               _panel_synthesizer(cfg, slot_ids)}
            except ValueError as e:
                emit("error", {"message": str(e)})
                emit("done", {"transcript": None})
                return
        try:
            labels = assign_labels([(s["provider"], s.get("label"),
                                     s.get("model")) for s in picked])
        except ValueError as e:
            emit("error", {"message": str(e)})
            emit("done", {"transcript": None})
            return
        blockers = self._auth_blockers(s["provider"] for s in picked)
        if blockers:
            emit("error", {"message": " ".join(blockers)})
            emit("done", {"transcript": None,
                 "can_continue": bool(run.state)})
            return

        # Everything from here to `started` is real work with no seat in
        # it yet - decoding attachments, probing git for the gate, opening
        # the transcript. Short on a small chat, not on a big attachment or
        # a large repo, and until now it was indistinguishable from a dead
        # window. The UI holds this row back for a beat, so a fast setup
        # still shows nothing.
        with relay.working(_AppIO(self, run), "setup"):
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            attachments = cfg.get("attachments") or []
            title_src = topic or opener or \
                (attachments[0].get("name", "") if attachments else "")
            slug = re.sub(r"[^a-z0-9]+", "-", title_src.lower())[:40].strip("-") or "chat"
            session_dir = os.path.join(SESSIONS_DIR, f"{stamp}-{slug}")
            self._adopt_run(run, session_dir)
            run.view_workspace = None    # the live state is authoritative now
            workspace = cfg.get("workspace") or os.path.join(session_dir, "workspace")
            os.makedirs(session_dir, exist_ok=True)
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
            transcript = os.path.join(session_dir, "transcript.md")

            agents = []
            for s, label in zip(picked, labels):
                agents.append(AGENT_TYPES[s["provider"]](
                    workspace, yolo=yolo, permission=permission,
                    model=s.get("model") or None, effort=s.get("effort") or None,
                    name=label,
                    role=s.get("role") or None,
                    role_instructions=s.get("role_instructions") or None,
                    connectors=connectors,
                    desktop=desktop, desktop_allowlist=desktop_allowlist,
                    browser=browser, browser_sites=browser_sites))
            providers = [s["provider"] for s in picked]
            # A room with no Claude seat accepts the desktop/browser pickers,
            # the site list and the unattended acknowledgement — and delivers
            # nothing, because only claude's build_cmd registers those servers.
            # Recorded as a row rather than left to be discovered.
            unreachable = relay.axis_unreachable_note(providers, desktop=desktop,
                                                      browser=browser)
            if unreachable:
                browser_notes.append(unreachable)
            # ...and the same for a permission rung that makes them
            # uncallable. The servers are correctly not registered; this is
            # what stops that being silent while the picker still shows the
            # rung Josh chose.
            blocked = relay.axis_blocked_by_permission_note(
                permission, desktop=desktop, browser=browser)
            if blocked:
                browser_notes.append(blocked)

            # Full opener text is the title — the rail ellipsizes in CSS and uses
            # the rest as a tooltip, so truncating here would throw it away.
            store = SessionStore(session_dir)
            store.open_transcript(title_src, agents, turns)

            state = {
                "agents": agents, "slot_ids": slot_ids, "providers": providers,
                "transcript": store.transcript, "workspace": workspace,
                "topic": topic or opener, "title": title_src, "created": store.created,
                "yolo": yolo, "permission": permission,
                # Per-conversation "always allow <tool>" grants Josh gives at
                # the ask-first modal; run_rounds appends to this and
                # SessionStore.save persists it.
                "permission_grants": [],
                "connectors": connectors,
                "desktop": desktop, "desktop_allowlist": desktop_allowlist,
                "browser": browser, "browser_sites": browser_sites,
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
                "battle": battle_state,
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
                # The app has a modal for it, so seats may [[ASK]] Josh —
                # unlike the headless default, which answers None at once.
                # Whether anyone is THERE is a separate question, answered a
                # few lines down by `_mark_watch`: a scheduled or webhook run
                # still offers the directive, and still expires it.
                "ask": True,
                # W2.2: pause before each Supervisor wave dispatches. Off
                # unless Josh ticked it; a no-op outside supervisor modes.
                "board_review": bool(cfg.get("board_review")),
            }
            state["log"] = log = make_log(state, store)
            # _session_dir was set above, so the focused run is this chat's — pin
            # it to the state before any thread can move the focus pointer.
            state["_run"] = run
            # ...and the ONE fact the engine needs off it, as a plain bool:
            # relay.ask_abort must not learn an app type, and a private key is
            # never persisted by SessionStore.save (the `_usage_io` shape). A
            # webhook or scheduled start has nobody to answer an [[ASK]], so
            # the question gets a deadline instead of holding the run open.
            Api._mark_watch(run, state)
            if (cfg.get("plan") or {}).get("enabled"):
                # Read-only from the FIRST turn, before any seat has spoken:
                # starting in execution and downgrading later would leave a window
                # in which a seat could already have written something.
                relay.start_plan(state, cfg.get("opener") or cfg.get("topic") or "")
            run.state = state
            # Persist before the first turn: if the app dies here, Josh's opener is
            # the only content that exists and it must still be resumable.
            store.save(state)

        emit("started", {
             "session_dir": session_dir, "workspace": workspace,
             "transcript": store.transcript, "mode": mode,
             "session": session_summary(session_dir),
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
            emit("status", {"text": text})

        # A scheduled run says so in its OWN transcript, as a persisted row.
        # Josh reads these hours later with no memory of arming them, and
        # the rail cannot tell him: `background` says nobody was watching,
        # not WHAT decided this should happen at 01:00.
        sched = cfg.get("scheduled")
        if isinstance(sched, dict) and sched.get("name"):
            brief_status_row(
                "Started by the schedule %r (%s, room %r)%s."
                % (sched.get("name"), sched.get("when") or "no recurrence",
                   sched.get("room"),
                   " — run now" if sched.get("manual") else ""))

        # Every browser-fence correction, as a row rather than a badge: the
        # site list is the one control here that is actually enforcing, so a
        # change Alloy made to it has to survive a reopen.
        for note in browser_notes:
            brief_status_row(note)

        brief = project_brief(workspace, session_dir,
                              spec=helper_spec([s.get("provider")
                                                for s in picked],
                                               moderator_spec,
                                               supervisor_spec),
                              enabled=cfg.get("brief", True),
                              solo=len(picked) == 1,
                              on_status=brief_status_row,
                              io=_AppIO(self, state.get("_run")))
        if brief.get("status") != "off":
            state["brief"] = brief
            if brief.get("usage"):
                relay.record_usage(state, brief["usage"], kind="brief")
            write_project_context(session_dir, brief)
            store.save(state)

        if opener:
            # emit the recorded row so live and replayed chats carry the
            # same keys (ts, meta, …) for this message
            target, rest = relay.parse_mention(opener, state["agents"])
            if target is None:
                emit("message", log("Josh (human)", opener))
                for j in state["pending"]:
                    state["pending"][j].append(
                        f"Josh (human) opens the conversation: {opener}")
            else:
                # "@Seat ..." opener: the named seat alone opens with it
                sid = state["slot_ids"][target]
                emit("message", log("Josh (human)", opener, envelope={
                     "audience": [sid], "delivered_to": [sid]}))
                state["pending"][target].append(
                    f"Josh (human) opens the conversation: {rest}")
            store.save(state)
        self._rounds(state)

    def _continue(self, cfg, run=None):
        """Resume a finished conversation: same agents, same sessions."""
        # Pinned like _conversation: `self._conv` is a view onto the FOCUSED
        # run, so continuing a chat while looking at another one resumed the
        # wrong conversation's state.
        run = run if run is not None else self._runs.focused()
        emit = lambda event, payload=None: self._emit_for(run, event, payload)
        state = run.state
        # Re-derived, never carried over: `continue_chat` clears `background`
        # the moment Josh types into a chat the schedule started, and the
        # state key has to follow or a run he is now watching would keep
        # expiring his own questions.
        Api._mark_watch(run, state)
        blockers = self._auth_blockers(state["providers"])
        if blockers:
            emit("error", {"message": " ".join(blockers)})
            emit("done", {"transcript": state["transcript"],
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
                emit("status", {"text": note})
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
            emit("status", {"text": note})
        if opener:
            target, rest = relay.parse_mention(opener, state["agents"])
            if target is None:
                emit("message", state["log"]("Josh (human)", opener))
                for j in state["pending"]:
                    state["pending"][j].append(f"Josh (human) says: {opener}")
            else:
                sid = state["slot_ids"][target]
                emit("message", state["log"](
                     "Josh (human)", opener,
                     envelope={"audience": [sid], "delivered_to": [sid]}))
                state["pending"][target].append(
                    f"Josh (human) says to you: {rest}")
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
            # UNDER the lock. `Run.clocks()` calls itself the one read path and
            # holds `clock_lock` while it iterates, which closes nothing if a
            # writer can `.clear()` from another thread — and a poll of
            # Api.jobs racing this line is exactly the RuntimeError the lock
            # was added for.
            with run.clock_lock:
                run.thinking.clear()  # a new run starts with nobody mid-turn
                run.working.clear()   # ...and no side call left from the last
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
        # store.dir, NOT self._session_dir: that property resolves to the
        # FOCUSED run, so a Josh who switched tabs mid-run would get this
        # chat's `done` event carrying the other chat's directory, summary and
        # feedback. The run was deliberately bound above for exactly this
        # reason; these three reads were the last ones still leaking.
        session_dir = store.dir
        summary = session_summary(session_dir)
        rec = outcome.read_outcome(session_dir) or {}
        self._set_status(run, "stopped" if outcome_kind == "stopped"
                         else "failed" if outcome_kind == "fatal" else "done",
                         outcome=outcome_kind)
        self._emit_for(run, "done", {
            "transcript": state["transcript"],
            "session_dir": session_dir,
            "session": summary,
            "feedback": rec.get("human_feedback") or {},
            # read back from what was actually persisted rather than asserted —
            # if a seat's id didn't save, the composer must say so instead of
            # promising a resume
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

# Event hooks (feature #16): the same best-effort attention channel as sound
# cues and the taskbar flash, but Josh's own commands instead of built-ins —
# e.g. a termux-notification on his phone when a run asks him something.
HOOK_TIMEOUT_S = 10       # a hook is a nudge, not a job; it never queues work
HOOK_DETAIL_MAX = 200     # AICHAT_DETAIL is an excerpt, not the whole payload


def hook_environment(hook_name, session_id="", title="", detail=""):
    """os.environ plus the four AICHAT_* variables a hook command may read."""
    env = dict(os.environ)
    env["AICHAT_EVENT"] = str(hook_name or "")
    env["AICHAT_SESSION"] = str(session_id or "")
    env["AICHAT_TITLE"] = str(title or "")
    env["AICHAT_DETAIL"] = str(detail or "")[:HOOK_DETAIL_MAX]
    return env


def _execute_command(command, env):
    """Run ONE user-configured shell command. Raises on timeout/failure by
    design — Api._hook_worker swallows everything; keeping this pure makes
    both halves testable without spawning anything."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(command, shell=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=HOOK_TIMEOUT_S, env=env, **kwargs)


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
    api._side_calls_enabled = True     # real window: side calls may spend
    api.start_scheduler()              # ONLY here — never in Api.__init__
    threading.Thread(target=api.precompute_config, daemon=True).start()
    threading.Thread(target=api.precompute_auth, daemon=True).start()
    ui = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "ui", "index.html")
    api._window = webview.create_window(
        "Alloy — many models, one conversation", ui, js_api=api,
        width=1220, height=820,
        min_size=(940, 620), background_color="#17151C")
    api._window.events.shown += lambda *a: _apply_window_icon(api._window)
    try:
        webview.start(debug=False)
    finally:
        # release the webhook socket so the process can exit promptly
        api.stop_scheduler()
        api.webhook_stop_all()


if __name__ == "__main__":
    main()
