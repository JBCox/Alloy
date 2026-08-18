#!/usr/bin/env python3
"""AI Chat desktop app: pywebview shell around the relay engine (relay.py).

Runs a native window (WebView2) hosting ui/index.html. The conversation loop
mirrors relay.py's round-robin engine and reuses its Agent adapters verbatim.
"""

import base64
import datetime
import json
import os
import queue
import re
import shutil
import subprocess
import threading

import webview

from relay import (AGENT_TYPES, PROVIDERS, SESSIONS_DIR, HELP_TEXT,
                   MODES, DEFAULT_MODE, IMPLEMENTED_MODES, DEFAULT_CEILING,
                   assign_labels, compact_agent, resolve_cmd, clean_env,
                   logout_gemini,
                   LoopIO, run_rounds, dispatch_command,
                   SessionStore, make_log, read_meta, read_messages,
                   session_summary,
                   list_sessions as stored_sessions, session_path, rehydrate)

AGENT_ORDER = ["claude", "gpt", "gemini"]


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


class _AppIO(LoopIO):
    """LoopIO for the desktop app. A module-level class on purpose: public
    attributes on the js_api object are walked by the pywebview bridge at page
    load (deadlock), so this wrapper lives outside Api and holds it privately.
    """

    def __init__(self, api):
        self._api = api

    def emit(self, event, payload=None):
        self._api.emit(event, payload)

    def drain_human(self):
        out = []
        while not self._api._human_q.empty():
            out.append(self._api._human_q.get_nowait())
        return out

    def should_stop(self):
        return self._api._stop_flag.is_set()

    def on_turn_boundary(self, state):
        # a staged role lands here, so the seat about to speak gets its fresh
        # preamble with the new role rather than switching identity mid-turn
        if self._api._staged_roles:
            self._api._commit_roles(state)


class Api:
    def __init__(self):
        self._window = None
        self._thread = None
        self._stop_flag = threading.Event()
        self._human_q = queue.Queue()
        self._session_dir = None
        self._config_cache = None
        self._config_ready = threading.Event()
        self._conv = None  # finished-conversation state, kept for continuation
        self._auth_cache = {}          # provider id -> status dict (relay probe)
        self._auth_lock = threading.Lock()
        self._login_procs = {}         # provider id -> Popen of open login console
        self._staged_roles = {}        # seat index -> staged role change (apply_role)
        self._roles_lock = threading.Lock()
        self._roles_busy = False       # an idle-path commit worker is running
        # Serialized emitter: evaluate_js is only ever called from ONE thread
        # (pywebview/WebView2 marshalling isn't documented thread-safe, and
        # parallel modes emit from several seat threads). A single queue also
        # guarantees FIFO ordering across all producers.
        self._emit_q = queue.Queue()
        threading.Thread(target=self._drain_emits, daemon=True).start()

    # ---------------------------------------------------------- to the UI --
    def emit(self, event, payload=None):
        # non-blocking and thread-safe: callers just enqueue
        self._emit_q.put(json.dumps({"event": event, "payload": payload or {}}))

    def _drain_emits(self):
        while True:
            data = self._emit_q.get()
            try:
                self._window.evaluate_js(f"uiEvent({data})")
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
    def _fallback_config():
        return {
            "claude_models": [{"id": "claude-opus-4-8", "label": "Opus 4.8"}],
            "claude_default_model": "claude-opus-4-8",
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
        }

    def precompute_config(self):
        # warm the codex feature probe off the bridge thread, so the first
        # preamble build never blocks on a subprocess
        try:
            import relay as _relay
            _relay.codex_multi_agent_enabled()
        except Exception:
            pass
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
            # matches the account default (interactive sessions run Opus 4.8 high)
            "claude_default_model": "claude-opus-4-8",
            "claude_default_effort": "high",
            "gemini_families": gemini_families,
            "gemini_default_family": "gemini-3.7-flash",
            "gemini_default_level": "high",
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
            "docs": os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "README.md"),
        }
        self._config_ready.set()

    def pick_folder(self):
        result = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if result:
            return result[0]
        return None

    def open_path(self, path):
        if path and os.path.exists(path):
            os.startfile(path)

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

    # ------------------------------------------------------------ history --
    # These methods are called on pywebview's bridge thread. Keep them to
    # bounded file I/O and object construction: never probe a CLI here.

    def list_sessions(self):
        return stored_sessions()

    def open_session(self, session_id):
        if self._thread and self._thread.is_alive():
            return {"error": "Stop the current conversation before opening "
                             "another chat."}
        path = session_path(session_id)
        if not path:
            return {"error": "That chat no longer exists."}

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
        while not self._human_q.empty():
            self._human_q.get_nowait()

        if state is not None:
            store = SessionStore(path)
            state["store"] = store
            state["transcript"] = store.transcript
            # same logger the live loops use — a second copy here is how a
            # reopened chat's rows start drifting from a fresh one's
            state["log"] = make_log(state, store)
        self._conv = state
        return {"ok": True, "session": summary, "messages": messages}

    def rename_session(self, session_id, title):
        path = session_path(session_id)
        if not path:
            return {"error": "That chat no longer exists."}
        if (self._thread and self._thread.is_alive()
                and os.path.abspath(path) == os.path.abspath(
                    self._session_dir or "")):
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
        if self._conv and os.path.abspath(path) == os.path.abspath(
                self._session_dir or ""):
            self._conv["title"] = title
        return {"ok": True, "session": session_summary(path, meta)}

    def delete_session(self, session_id):
        path = session_path(session_id)
        if not path:
            return {"error": "That chat no longer exists."}
        active = os.path.abspath(path) == os.path.abspath(
            self._session_dir or "")
        if active and self._thread and self._thread.is_alive():
            return {"error": "Stop this conversation before deleting it."}
        if active:
            self._conv = None
            self._session_dir = None
        try:
            shutil.rmtree(path)
        except OSError as e:
            return {"error": f"Could not delete chat: {e}"}
        return {"ok": True, "id": session_id}

    def new_conversation(self):
        return self.reset_conversation()

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
        if self._thread and self._thread.is_alive():
            return {"error": "A conversation is already running."}
        if not self._conv:
            return {"error": "No conversation to continue."}
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_continue, args=(cfg,),
                                        daemon=True)
        self._thread.start()
        return {"ok": True}

    def reset_conversation(self):
        if self._thread and self._thread.is_alive():
            return {"error": "Stop the conversation first."}
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
        return {"ok": True}

    def interject(self, text, files=None):
        text = (text or "").strip()
        if files:
            ws = (self._conv or {}).get("workspace")
            if not ws:
                return {"error": "No conversation workspace to attach files to."}
            try:  # plain file IO — safe on the bridge thread (no subprocess)
                text = with_attachments(text, save_attachments(files, ws))
            except (OSError, ValueError) as e:
                return {"error": f"Could not save attachment: {e}"}
        if text:
            self._human_q.put(text)
        return {"ok": True, "text": text}

    def command(self, text):
        text = (text or "").strip()
        if not text.startswith("/"):
            return {"error": "Commands start with /."}
        if self._thread and self._thread.is_alive():
            self._human_q.put(text)
            return {"ok": True, "note": "Queued — runs before the next turn."}
        if not self._conv:
            return {"error": "No conversation yet — start one first. " + HELP_TEXT}
        head = text[1:].partition(" ")[0].lower()
        if head in ("stop", "turns"):
            return {"error": f"/{head} only applies while a conversation "
                             f"is running."}
        # idle: run directly on a worker thread (threads *spawned* by a bridge
        # call are safe for subprocess.run; the bridge thread itself is not)
        threading.Thread(target=self._do_command, args=(self._conv, text),
                         daemon=True).start()
        return {"ok": True}

    def apply_role(self, seat_id, role, instructions):
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
        state = self._conv
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
            self._staged_roles[idx] = (role or None, instructions or None)
            running = bool(self._thread and self._thread.is_alive())
            spawn = not running and not self._roles_busy
            if spawn:
                self._roles_busy = True
        if spawn:
            threading.Thread(target=self._commit_roles_idle, args=(state,),
                             daemon=True).start()
        return {"ok": True,
                "note": (f"Queued — {agent.name} switches at the next turn "
                         f"boundary." if running
                         else f"Applying {agent.name}'s new role…")}

    def _commit_roles_idle(self, state):
        try:
            # A bridge call can stage another edit after _commit_roles sees an
            # empty dict but before this worker clears _roles_busy: the stager
            # reads busy=True, doesn't spawn, and the edit would sit unapplied
            # while the UI says "Applying…". Recheck under the same lock the
            # stager uses so that edit either spawns its own worker (it saw
            # busy=False) or is drained by this loop — never stranded.
            while True:
                self._commit_roles(state)
                with self._roles_lock:
                    if not self._staged_roles:
                        self._roles_busy = False
                        return
        except Exception as e:                       # never strand the flag
            self.emit("status", {"text": f"Role change failed: {str(e)[:200]}"})
            with self._roles_lock:
                self._roles_busy = False

    def _commit_roles(self, state):
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
                if not self._staged_roles:
                    return
                i = sorted(self._staged_roles)[0]
                role, instructions = self._staged_roles.pop(i)
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

    def stop(self):
        self._stop_flag.set()
        return {"ok": True}

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
        yolo = bool(cfg.get("yolo"))
        mode = (cfg.get("mode") or DEFAULT_MODE).replace("-", "_")
        if mode not in MODES:
            self.emit("error", {"message": f"Unknown mode {mode!r}."})
            self.emit("done", {"transcript": None})
            return
        if mode not in IMPLEMENTED_MODES:
            self.emit("error", {"message": f"Mode '{mode}' isn't available "
                                           f"yet."})
            self.emit("done", {"transcript": None})
            return
        moderator_spec = None
        if mode == "moderator":
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
        try:
            labels = assign_labels([(s["provider"], s.get("label"))
                                    for s in picked])
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
        workspace = cfg.get("workspace") or os.path.join(self._session_dir, "workspace")
        os.makedirs(self._session_dir, exist_ok=True)
        os.makedirs(workspace, exist_ok=True)
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
                role_instructions=s.get("role_instructions") or None))
        slot_ids = [s.get("id", i) for i, s in enumerate(picked)]
        providers = [s["provider"] for s in picked]

        # Full opener text is the title — the rail ellipsizes in CSS and uses
        # the rest as a tooltip, so truncating here would throw it away.
        store = SessionStore(self._session_dir)
        store.open_transcript(title_src, agents, turns)

        state = {
            "agents": agents, "slot_ids": slot_ids, "providers": providers,
            "transcript": store.transcript, "workspace": workspace,
            "topic": topic, "title": title_src, "created": store.created,
            "yolo": yolo, "turns": turns, "store": store, "ended": False,
            "pending": {i: [] for i in range(len(agents))},
            "introduced": [False] * len(agents),
            "rnd": 0, "max": turns, "mode": mode,
            "moderator": moderator_spec,
            "until_done": bool(cfg.get("until_done")),
            "turn_ceiling": (max(1, int(cfg.get("ceiling")
                                        or DEFAULT_CEILING))
                             if cfg.get("until_done") else None),
            "spawn": {"tier1": bool((cfg.get("spawn") or {})
                                    .get("tier1", True)),
                      "max_helpers": max(0, int((cfg.get("spawn") or {})
                                                .get("max_helpers") or 0)),
                      "helpers_used": 0,
                      "max_teams": max(0, int((cfg.get("spawn") or {})
                                              .get("max_teams") or 0)),
                      "teams_used": 0},
        }
        state["log"] = log = make_log(state, store)
        self._conv = state
        # Persist before the first turn: if the app dies here, Josh's opener is
        # the only content that exists and it must still be resumable.
        store.save(state)

        self.emit("started", {
            "session_dir": self._session_dir, "workspace": workspace,
            "transcript": store.transcript, "mode": mode,
            "session": session_summary(self._session_dir),
            "participants": [
                {"id": slot_ids[i], "provider": providers[i],
                 "name": agents[i].name,
                 "model": picked[i].get("model") or "default",
                 "effort": picked[i].get("effort") or "",
                 "role": agents[i].role or "",
                 "role_instructions": agents[i].role_instructions or ""}
                for i in range(len(picked))],
        })

        if opener:
            log("Josh (human)", opener)
            self.emit("message", {"speaker": "josh", "name": "Josh",
                                  "text": opener, "round": 0})
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
        if opener:
            state["log"]("Josh (human)", opener)
            self.emit("message", {"speaker": "josh", "name": "Josh",
                                  "text": opener, "round": state["rnd"]})
            for j in state["pending"]:
                state["pending"][j].append(f"Josh (human) says: {opener}")
            state["store"].save(state)
        self._rounds(state)

    def _rounds(self, state):
        """Run the shared loop, then the app's epilogue: paused footer + done.

        The loop itself lives in relay.run_rounds — anything loop-shaped goes
        there, once, for both front ends."""
        run_rounds(state, _AppIO(self))
        store = state["store"]
        store.save(state)
        with open(state["transcript"], "a", encoding="utf-8") as f:
            f.write("\n---\n*paused — reply in the app to continue*\n")
        summary = session_summary(self._session_dir)
        self.emit("done", {"transcript": state["transcript"],
                           "session_dir": self._session_dir,
                           "session": summary,
                           # read back from what was actually persisted rather
                           # than asserted — if a seat's id didn't save, the
                           # composer must say so instead of promising a resume
                           "can_continue": summary["can_continue"],
                           "can_continue_reason": summary["can_continue_reason"]})

    # --------------------------------------------------- slash commands --
    def _do_command(self, state, text):
        """Idle-path shim: the shared dispatcher does the work (relay.py)."""
        return dispatch_command(state, text, _AppIO(self))


def main():
    api = Api()
    threading.Thread(target=api.precompute_config, daemon=True).start()
    threading.Thread(target=api.precompute_auth, daemon=True).start()
    ui = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "ui", "index.html")
    api._window = webview.create_window(
        "AI Chat", ui, js_api=api, width=1220, height=820,
        min_size=(940, 620), background_color="#17151C")
    webview.start(debug=False)


if __name__ == "__main__":
    main()
