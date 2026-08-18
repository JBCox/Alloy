#!/usr/bin/env python3
"""AI Chat desktop app: pywebview shell around the relay engine (relay.py).

Runs a native window (WebView2) hosting ui/index.html. The conversation loop
mirrors relay.py's round-robin engine and reuses its Agent adapters verbatim.
"""

import datetime
import json
import os
import queue
import re
import subprocess
import threading

import webview

from relay import (AGENT_TYPES, PROVIDERS, SESSIONS_DIR,
                   CLEAR_NOTE, preamble, wrap_called, assign_labels, match_seats,
                   compact_agent, resolve_cmd, clean_env, logout_gemini)

HELP_TEXT = ("Commands: /clear [seat] · /compact [seat] · /turns N · /stop · "
             "/help — seat is a name ('claude 2') or a provider "
             "(claude/gpt/gemini); no seat means every seat.")

AGENT_ORDER = ["claude", "gpt", "gemini"]


def agy_path():
    import shutil
    return shutil.which("agy") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe")


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

    # ---------------------------------------------------------- to the UI --
    def emit(self, event, payload=None):
        data = json.dumps({"event": event, "payload": payload or {}})
        try:
            self._window.evaluate_js(f"uiEvent({data})")
        except Exception:
            pass

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
                with open(self._conv["transcript"], "a", encoding="utf-8") as f:
                    f.write("\n*conversation ended*\n")
            except OSError:
                pass
        self._conv = None
        return {"ok": True}

    def interject(self, text):
        text = (text or "").strip()
        if text:
            self._human_q.put(text)
        return {"ok": True}

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
        title_src = topic or opener
        slug = re.sub(r"[^a-z0-9]+", "-", title_src.lower())[:40].strip("-") or "chat"
        self._session_dir = os.path.join(SESSIONS_DIR, f"{stamp}-{slug}")
        workspace = cfg.get("workspace") or os.path.join(self._session_dir, "workspace")
        os.makedirs(self._session_dir, exist_ok=True)
        os.makedirs(workspace, exist_ok=True)
        transcript = os.path.join(self._session_dir, "transcript.md")

        agents = []
        for s, label in zip(picked, labels):
            agents.append(AGENT_TYPES[s["provider"]](
                workspace, yolo=yolo,
                model=s.get("model") or None, effort=s.get("effort") or None,
                name=label))
        slot_ids = [s.get("id", i) for i, s in enumerate(picked)]
        providers = [s["provider"] for s in picked]

        title = title_src if len(title_src) <= 80 else title_src[:77] + "…"
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(f"# AI Chat — {title}\n\n"
                    f"*{datetime.datetime.now():%Y-%m-%d %H:%M} · "
                    f"{' ↔ '.join(a.name for a in agents)} · "
                    f"max {turns} rounds*\n")

        def log(speaker, text, meta=""):
            with open(transcript, "a", encoding="utf-8") as f:
                f.write(f"\n## {speaker}{f'  · {meta}' if meta else ''}\n\n{text}\n")

        self.emit("started", {
            "session_dir": self._session_dir, "workspace": workspace,
            "transcript": transcript,
            "participants": [
                {"id": slot_ids[i], "provider": providers[i],
                 "name": agents[i].name,
                 "model": picked[i].get("model") or "default",
                 "effort": picked[i].get("effort") or ""}
                for i in range(len(picked))],
        })

        state = {
            "agents": agents, "slot_ids": slot_ids, "providers": providers,
            "transcript": transcript, "workspace": workspace, "topic": topic,
            "turns": turns, "log": log,
            "pending": {i: [] for i in range(len(agents))},
            "introduced": [False] * len(agents),
            "rnd": 0, "max": turns,
        }
        self._conv = state
        if opener:
            log("Josh (human)", opener)
            self.emit("message", {"speaker": "josh", "name": "Josh",
                                  "text": opener, "round": 0})
            for j in state["pending"]:
                state["pending"][j].append(
                    f"Josh (human) opens the conversation: {opener}")
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
        turns = max(1, int(cfg.get("turns", state["turns"])))
        state["max"] = state["rnd"] + turns
        if opener:
            state["log"]("Josh (human)", opener)
            self.emit("message", {"speaker": "josh", "name": "Josh",
                                  "text": opener, "round": state["rnd"]})
            for j in state["pending"]:
                state["pending"][j].append(f"Josh (human) says: {opener}")
        self._rounds(state)

    def _rounds(self, state):
        """Run rounds until state['max'], a wrap, or a stop. Leaves state
        intact so the conversation can be continued later."""
        agents, log = state["agents"], state["log"]
        slot_ids, providers = state["slot_ids"], state["providers"]
        pending, introduced = state["pending"], state["introduced"]
        closing_left = None
        stopping = False
        while (state["rnd"] < state["max"] and not stopping
               and not self._stop_flag.is_set()):
            state["rnd"] += 1
            rnd = state["rnd"]
            for i, agent in enumerate(agents):
                if self._stop_flag.is_set():
                    stopping = True
                    break
                while not self._human_q.empty():
                    h = self._human_q.get_nowait()
                    if h.startswith("/"):
                        if self._do_command(state, h):
                            stopping = True
                        continue
                    log("Josh (human)", h)
                    self.emit("message", {"speaker": "josh", "name": "Josh",
                                          "text": h, "round": rnd})
                    for j in range(len(agents)):
                        pending[j].append(f"Josh (human) interjects: {h}")
                if stopping:
                    break

                parts = []
                first_turn = not introduced[i]
                if first_turn:
                    parts.append(preamble(agent,
                                          [a for a in agents if a is not agent],
                                          state["topic"], state["turns"],
                                          state["workspace"]))
                    if i == 0 and rnd == 1 and not pending[i]:
                        parts.append("You open the conversation. Go.")
                queued = pending[i]
                pending[i] = []
                if queued:
                    parts.append("\n\n".join(queued))
                message = "\n\n".join(parts)

                key = slot_ids[i]
                self.emit("thinking", {"speaker": key, "provider": providers[i],
                                       "name": agent.name, "round": rnd,
                                       "turns": state["max"]})
                try:
                    reply = agent.turn(message)
                except Exception:
                    try:
                        reply = agent.turn(message)
                    except Exception as e2:
                        self.emit("agent_error", {
                            "speaker": key, "provider": providers[i],
                            "message": f"{agent.name} failed twice; skipping "
                                       f"this round. ({str(e2)[:200]})"})
                        # Restore only consumed queue entries. The preamble and
                        # opener are generated prompt scaffolding, not messages.
                        pending[i] = queued + pending[i]
                        continue
                finally:
                    self.emit("thinking_done", {"speaker": key})

                # Never forge a turn — see the matching backstop in relay._rounds.
                if not (reply or "").strip():
                    self.emit("agent_error", {
                        "speaker": key, "provider": providers[i],
                        "message": f"{agent.name} returned an empty reply; "
                                   f"skipping this round (nothing sent to the "
                                   f"others)."})
                    pending[i] = queued + pending[i]
                    continue

                if first_turn:
                    introduced[i] = True
                log(agent.name, reply, meta=f"round {rnd}")
                self.emit("message", {"speaker": key, "provider": providers[i],
                                      "name": agent.name,
                                      "text": reply, "round": rnd})
                for j, other in enumerate(agents):
                    if other is not agent:
                        pending[j].append(f"{agent.name} said:\n{reply}")

                if closing_left is None and wrap_called(reply):
                    closing_left = len(agents) - 1
                    self.emit("status", {"text": f"{agent.name} called it — "
                                                 f"closing remarks…"})
                elif closing_left is not None:
                    closing_left -= 1
                    if closing_left <= 0:
                        stopping = True
                        break

        with open(state["transcript"], "a", encoding="utf-8") as f:
            f.write("\n---\n*paused — reply in the app to continue*\n")
        self.emit("done", {"transcript": state["transcript"],
                           "session_dir": self._session_dir,
                           "can_continue": True})

    # --------------------------------------------------- slash commands --
    def _do_command(self, state, text):
        """Handle a /command. Returns True if the conversation should stop."""
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.lower().lstrip("/"), arg.strip()
        if cmd == "stop":
            return True
        if cmd == "turns":
            if arg.isdigit():
                state["max"] = max(state["rnd"], int(arg))
                self.emit("status", {"text": f"Round cap is now {state['max']}."})
            else:
                self.emit("status", {"text": "Usage: /turns N"})
        elif cmd in ("clear", "compact"):
            self._seat_command(state, cmd, arg)
        elif cmd == "help":
            self.emit("status", {"text": HELP_TEXT})
        else:
            self.emit("status", {"text": f"Unknown command /{cmd}. {HELP_TEXT}"})
        return False

    def _seat_command(self, state, cmd, arg):
        idxs = match_seats(state["agents"], arg)
        if not idxs:
            self.emit("status", {"text": f"No seat matches '{arg}'. {HELP_TEXT}"})
            return
        for i in idxs:
            agent = state["agents"][i]
            if cmd == "compact":
                self.emit("status", {"text": f"Compacting {agent.name}'s "
                                             f"context…"})
                try:
                    summary = compact_agent(agent)
                except Exception as e:
                    self.emit("status", {"text": f"{agent.name} compact failed: "
                                                 f"{str(e)[:200]}"})
                    continue
                state["introduced"][i] = False
                state["pending"][i].insert(0, "(Josh compacted your context. "
                                              "Your own summary of the "
                                              "conversation so far:)\n\n"
                                              + summary)
                state["log"](agent.name, summary,
                             meta="context compacted — self-summary")
                self.emit("status", {"text": f"{agent.name}'s context "
                                             f"compacted."})
            else:
                agent.session_id = None
                state["introduced"][i] = False
                state["pending"][i].insert(0, CLEAR_NOTE)
                self.emit("status", {"text": f"{agent.name}'s context "
                                             f"cleared."})


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
