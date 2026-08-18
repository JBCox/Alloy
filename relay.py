#!/usr/bin/env python3
"""ai-chat: an AI-to-AI conversation relay.

Claude (Claude Code CLI, Max account) talks to GPT (OpenAI Codex CLI, ChatGPT
account) -- and optionally Gemini (Gemini CLI) -- with no API keys: every agent
authenticates through its official CLI's account login.

Josh kicks it off with a topic and can interject anytime by typing into the
terminal (or dropping text into the session's say.txt). Everything is saved to
a markdown transcript.

Usage:
    ai-chat "topic here" [--turns 10] [--agents claude,gpt] [--start gpt] [--yolo]

Each --agents token is provider[:model[:effort]][=label]; repeat a provider for
duplicate seats (e.g. claude:opus:high,claude:haiku:low -> "Claude" vs "Claude 2").
"""

import argparse
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
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
TURN_TIMEOUT = 300  # seconds per agent turn
WRAP_TOKEN = "[[WRAP]]"

# Conversation modes (ORCHESTRATION_DESIGN.md). One conversation-level value:
# cfg key `mode`, CLI --mode, meta field `mode`. `round_robin` is the classic
# fixed order; the others land phase by phase — IMPLEMENTED_MODES is the gate
# both front ends validate against, so an unbuilt mode is a clear error at
# start time, never a silent fall-through to round-robin.
MODES = ("round_robin", "speaker", "moderator", "parallel", "free")
DEFAULT_MODE = "round_robin"
IMPLEMENTED_MODES = ("round_robin", "speaker", "moderator", "parallel",
                     "free")

# "Until done": no round cap — the conversation ends via [[WRAP]], a moderator
# DONE, /stop, or this hard turn ceiling (the spend backstop; generous but
# bounded). Orthogonal to mode.
DEFAULT_CEILING = 60

# Free mode fairness: a seat may not START a turn while this many turns ahead
# of the slowest live seat — a fast cheap seat must not flood the budget.
FREE_MAX_LEAD = 2
# Free mode: after a failed (skipped) turn, wait for new queue content or this
# many seconds before retrying — a permanently failing seat must not hot-loop.
FREE_RETRY_BACKOFF = 5.0

# ---------------------------------------------------------------- colors ----

os.system("")  # enable ANSI escape processing on Windows consoles
RESET, DIM, BOLD = "\x1b[0m", "\x1b[2m", "\x1b[1m"
COLORS = {
    "Claude": "\x1b[38;5;208m",   # orange
    "GPT": "\x1b[38;5;42m",       # green
    "Gemini": "\x1b[38;5;69m",    # blue
    "Josh (human)": "\x1b[38;5;213m",  # pink
}


def banner(speaker, extra=""):
    color = COLORS.get(speaker, "")
    line = "─" * 62
    tag = f" {speaker} " + (f"{DIM}{extra}{RESET}{color}" if extra else "")
    print(f"\n{color}{BOLD}┌{line}┐{RESET}")
    print(f"{color}{BOLD}│{RESET}{color}{tag}{RESET}")
    print(f"{color}{BOLD}└{line}┘{RESET}")


# One acquisition per complete visual block: parallel modes print from several
# seat threads, and a banner interleaved with a status line is unreadable.
_PRINT_LOCK = threading.Lock()


def status(msg):
    with _PRINT_LOCK:
        print(f"{DIM}  {msg}{RESET}", flush=True)


# ---------------------------------------------------------------- agents ----

def resolve_cmd(cmd):
    """Resolve cmd[0] into something CreateProcess can run.

    Handles the agy off-PATH fallback, PATH lookup, and npm .cmd shims (bare
    names fail under CreateProcess, and `cmd /c` truncates multi-line args at
    the first newline — so shims are resolved to `node <script>.js` directly).
    Raises RuntimeError if the CLI is not found.
    """
    cmd = list(cmd)
    # agy installs to %LOCALAPPDATA%\agy\bin, which may not be on the PATH
    # of shells opened before it was installed.
    if cmd[0] == "agy" and not shutil.which("agy"):
        fallback = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                "agy", "bin", "agy.exe")
        if os.path.exists(fallback):
            cmd[0] = fallback
    exe = shutil.which(cmd[0])
    if not exe:
        raise RuntimeError(f"'{cmd[0]}' not found on PATH")
    cmd[0] = exe
    if exe.lower().endswith((".cmd", ".bat")):
        with open(exe, "r", encoding="utf-8", errors="replace") as f:
            m = re.search(r'"%dp0%\\([^"]+\.js)"', f.read())
        if m:
            script = os.path.join(os.path.dirname(exe), m.group(1))
            cmd = [shutil.which("node") or "node", script] + cmd[1:]
        else:  # unknown shim shape; cmd /c works only for single-line args
            cmd = ["cmd", "/c"] + cmd
    return cmd


def clean_env():
    """os.environ minus CLAUDE*/ANTHROPIC*: if the relay runs inside a Claude
    Code session, inherited vars make the nested `claude` CLI think it has
    host auth and fail with "Not logged in"."""
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith(("CLAUDE", "ANTHROPIC"))}


class Agent:
    """Base adapter: run one turn against a CLI, keeping session continuity."""

    name = "agent"
    cli = None

    def native_spawn_note(self):
        """One preamble sentence when THIS seat's config actually allows its
        CLI's built-in subagents; None otherwise. Capability-honest: never
        promise what build_cmd doesn't grant — the note and the capability
        must come from the same place or the preamble lies."""
        return None

    def __init__(self, workspace, yolo=False, model=None, effort=None, name=None,
                 role=None, role_instructions=None):
        self.workspace = workspace
        self.yolo = yolo
        self.model = model
        self.effort = effort
        self.session_id = None
        self.uid = uuid.uuid4().hex[:8]
        if name:  # instance attr shadows the class attr (duplicate-provider seats)
            self.name = name
        # Roles live on the agent (like `name`) so preamble() reads them without
        # either loop passing new args. Injection is preamble-ONLY: the preamble
        # is the one text re-injected when introduced[] resets on /clear and
        # /compact, so roles survive resets; anything pushed through pending[]
        # instead would silently evaporate at the first compact (ROLES_DESIGN.md).
        self.role = (role or "").strip() or None
        self.role_instructions = (role_instructions or "").strip() or None

    def turn(self, message):
        cmd = resolve_cmd(self.build_cmd(message))
        env = clean_env()
        self.before_run()
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=self.workspace, timeout=TURN_TIMEOUT,
            shell=False, stdin=subprocess.DEVNULL, env=env,
            # Console children spawn a visible console window when the parent
            # has none (pythonw app); output is piped, so suppress it.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{self.name} exited {result.returncode}: "
                f"{(result.stderr or result.stdout or '').strip()[-500:]}"
            )
        reply = self.parse(result.stdout)
        if not reply:
            # Exit 0 with nothing to say is a SOFT failure (dropped auth, quota,
            # a CLI that logged an error and still exited clean). Raise so it
            # takes the loop's retry-then-skip path instead of being relayed to
            # the other seats as "(no reply)" — a content-free turn poisons
            # every other agent's context and burns the round silently.
            raise RuntimeError(
                f"{self.name} exited 0 but produced no reply: "
                f"{(result.stderr or result.stdout or '').strip()[-300:] or 'no output'}"
            )
        return reply

    def before_run(self):
        """Hook: reset per-turn scratch state before the CLI runs."""

    def build_cmd(self, message):
        raise NotImplementedError

    def parse(self, stdout):
        raise NotImplementedError


class ClaudeAgent(Agent):
    name = "Claude"
    cli = "claude"

    def build_cmd(self, message):
        cmd = ["claude", "-p", "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        if self.yolo:
            cmd += ["--dangerously-skip-permissions"]
        else:
            cmd += [
                "--permission-mode", "acceptEdits",
                # equals form: --allowedTools is variadic and would otherwise
                # swallow the positional prompt that follows it.
                # Task = built-in subagents (tier-1 spawning): a Task subagent
                # inherits this same permission mode/allowlist/cwd, so it adds
                # parallelism within a turn, never new effective capability.
                "--allowedTools=WebSearch,WebFetch,Read,Write,Edit,Glob,Grep,Task",
            ]
        cmd.append(message)  # positional prompt goes last (required for --resume)
        return cmd

    def parse(self, stdout):
        data = json.loads(stdout.strip().splitlines()[-1])
        # -p --resume forks to a fresh session id each call; track the newest
        self.session_id = data.get("session_id", self.session_id)
        return (data.get("result") or "").strip()

    def native_spawn_note(self):
        # true for BOTH build_cmd branches: yolo allows everything, non-yolo
        # has Task in the allowlist
        return ("You may use your built-in Task/subagent tool for small, "
                "focused side-tasks within your turn. Subagents share your "
                "workspace and permissions.")


class CodexAgent(Agent):
    name = "GPT"
    cli = "codex"

    @property
    def _lastmsg(self):
        # per-instance filename: two GPT seats must not share one -o file
        # Inside the workspace, NOT "workspace/..": that parent-hop assumed the
        # workspace is always sessions/<name>/workspace/. With a user-picked
        # working folder (app.py: cfg["workspace"]) it escapes one level up —
        # pick C:\ai-chat and codex is told to write C:\.codex-last-message-*,
        # which the drive root denies, so codex exits 0 having written nothing
        # and every GPT turn silently becomes "(no reply)". The workspace is
        # writable by definition and inside codex's workspace-write sandbox.
        # (.gitignore's .codex-last-message-*.txt already matches at any depth.)
        return os.path.join(self.workspace, f".codex-last-message-{self.uid}.txt")

    def before_run(self):
        # codex only WRITES this file when it has an assistant message. If a run
        # exits 0 without producing one, a leftover file would make parse()
        # return the PREVIOUS round's text — the seat silently repeats itself
        # and nothing looks wrong. Delete it so a missing file means "no reply".
        try:
            os.remove(self._lastmsg)
        except FileNotFoundError:
            pass

    def build_cmd(self, message):
        # `codex exec resume` takes no --sandbox/--cd flags, so the sandbox is
        # set via -c overrides (valid on both forms) and cwd via the process.
        common = [
            "--json", "--skip-git-repo-check",
            "-o", self._lastmsg,
        ]
        if self.model:
            common += ["-m", self.model]
        if self.effort:
            common += ["-c", f'model_reasoning_effort="{self.effort}"']
        if self.yolo:
            common += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            common += [
                "-c", 'sandbox_mode="workspace-write"',
                "-c", "sandbox_workspace_write.network_access=true",
            ]
        if self.session_id:
            return ["codex", "exec", "resume", self.session_id] + common + [message]
        return ["codex", "exec"] + common + [message]

    def parse(self, stdout):
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            for key in ("session_id", "thread_id", "conversation_id"):
                found = self._find_key(evt, key)
                if found:
                    self.session_id = found
                    break
            if self.session_id:
                break
        try:
            with open(self._lastmsg, "r", encoding="utf-8", errors="replace") as f:
                return f.read().strip()
        except OSError:
            return ""

    @staticmethod
    def _find_key(obj, key):
        if isinstance(obj, dict):
            if isinstance(obj.get(key), str):
                return obj[key]
            for v in obj.values():
                found = CodexAgent._find_key(v, key)
                if found:
                    return found
        return None

    def native_spawn_note(self):
        if codex_multi_agent_enabled():
            return ("You may use your built-in multi-agent/collab tools for "
                    "small, focused side-tasks within your turn.")
        return None


_CODEX_MULTI_AGENT = None


def codex_multi_agent_enabled():
    """Is codex's native multi-agent feature enabled on this install?

    `codex features list` spends no tokens; cached for the process lifetime.
    Any failure -> False: never tell a seat it can spawn on a guess. Call from
    loop/worker threads only — never the pywebview bridge thread."""
    global _CODEX_MULTI_AGENT
    if _CODEX_MULTI_AGENT is None:
        enabled = False
        try:
            r = subprocess.run(
                resolve_cmd(["codex", "features", "list"]),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=20, shell=False,
                stdin=subprocess.DEVNULL, env=clean_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in (r.stdout or "").splitlines():
                toks = line.split()
                if toks and toks[0] == "multi_agent":
                    enabled = toks[-1].lower() == "true"
                    break
        except Exception:
            enabled = False
        _CODEX_MULTI_AGENT = enabled
    return _CODEX_MULTI_AGENT


class GeminiAgent(Agent):
    # Gemini rides Google's Antigravity CLI (agy) — the successor to the retired
    # Gemini CLI. Free Google-account login, JSON print mode, --conversation
    # for memory. JSON output works piped (the TTY-only-renderer bug that eats
    # piped *text* output does not affect --output-format json as of 1.1.13).
    name = "Gemini"
    cli = "agy"

    def build_cmd(self, message):
        cmd = ["agy", "-p", message, "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        if self.session_id:
            cmd += ["--conversation", self.session_id]
        if self.yolo:
            cmd += ["--dangerously-skip-permissions"]
        else:
            # auto-approve tools but keep terminal restrictions on: print mode
            # can't answer interactive permission prompts, it would just stall
            cmd += ["--dangerously-skip-permissions", "--sandbox"]
        return cmd

    def parse(self, stdout):
        data = json.loads(stdout[stdout.find("{"):])
        self.session_id = data.get("conversation_id", self.session_id)
        return (data.get("response") or "").strip()


# ------------------------------------------------------ auth + registry ----

def _run_probe(argv, timeout):
    return subprocess.run(
        resolve_cmd(argv), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, shell=False,
        stdin=subprocess.DEVNULL, env=clean_env(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _status(provider, state, email=None, detail=""):
    meta = PROVIDERS[provider]
    return {"provider": provider, "label": meta["label"], "state": state,
            "email": email, "detail": detail,
            "install_hint": meta["install_hint"], "checked_at": time.time()}


def probe_claude(timeout=15, home=None):
    """`claude auth status --json` — fast, spends no tokens."""
    try:
        r = _run_probe(["claude", "auth", "status", "--json"], timeout)
    except RuntimeError:
        return _status("claude", "not_installed")
    except Exception as e:
        return _status("claude", "unknown", detail=str(e)[:120])
    try:
        data = json.loads((r.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and "loggedIn" in data:
        if data["loggedIn"]:
            bits = [b for b in (data.get("authMethod"),
                                data.get("subscriptionType")) if b]
            return _status("claude", "signed_in", email=data.get("email"),
                           detail=" · ".join(bits))
        return _status("claude", "signed_out")
    # old CLI without `auth status`, or unparseable output: never guess
    # signed_out — the runtime error path remains the backstop.
    return _status("claude", "unknown",
                   detail=(r.stderr or r.stdout or "unparseable").strip()[:120])


def probe_codex(timeout=15, home=None):
    """`codex login status`; falls back to ~/.codex/auth.json on timeout."""
    home = home or os.path.expanduser("~")
    try:
        r = _run_probe(["codex", "login", "status"], timeout)
    except RuntimeError:
        return _status("gpt", "not_installed")
    except Exception:
        auth = os.path.join(home, ".codex", "auth.json")
        try:
            if os.path.getsize(auth) > 2:
                return _status("gpt", "signed_in", detail="(from auth.json)")
        except OSError:
            pass
        return _status("gpt", "unknown", detail="status check failed")
    out = ((r.stdout or "") + " " + (r.stderr or "")).strip()
    if re.search(r"not\s+logged", out, re.I) or r.returncode != 0:
        return _status("gpt", "signed_out", detail=out[:120])
    if re.search(r"logged\s+in", out, re.I):
        return _status("gpt", "signed_in", detail=out.splitlines()[0][:80])
    return _status("gpt", "unknown", detail=out[:120])


def probe_gemini(timeout=15, home=None):
    """File-only (agy has no auth subcommand): ~/.gemini/oauth_creds.json +
    google_accounts.json. Can't detect revoked/expired creds — the runtime
    error path is the backstop for that."""
    home = home or os.path.expanduser("~")
    agy_exe = shutil.which("agy") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe")
    if not (shutil.which("agy") or os.path.exists(agy_exe)):
        return _status("gemini", "not_installed")
    creds = os.path.join(home, ".gemini", "oauth_creds.json")
    accounts = os.path.join(home, ".gemini", "google_accounts.json")
    try:
        if not (os.path.exists(creds) and os.path.getsize(creds) > 2):
            return _status("gemini", "signed_out")
    except OSError as e:
        return _status("gemini", "unknown", detail=str(e)[:120])
    email = None
    try:
        with open(accounts, encoding="utf-8") as f:
            email = json.load(f).get("active") or None
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return _status("gemini", "signed_in", email=email,
                   detail="" if email else "credentials present")


def probe_grok(timeout=15, home=None):
    """Placeholder until the Grok Build adapter lands."""
    if not shutil.which("grok"):
        return _status("grok", "not_installed")
    return _status("grok", "unknown",
                   detail="installed — probe lands with the Grok adapter")


def logout_gemini(home=None):
    """agy has no logout command: move its Google creds into a timestamped
    backup dir (never deleted — restoring = moving the files back)."""
    home = home or os.path.expanduser("~")
    gdir = os.path.join(home, ".gemini")
    backup = os.path.join(gdir, "aichat-logout-backup-"
                          + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    moved = False
    for name in ("oauth_creds.json", "google_accounts.json"):
        src = os.path.join(gdir, name)
        if os.path.exists(src):
            os.makedirs(backup, exist_ok=True)
            shutil.move(src, os.path.join(backup, name))
            moved = True
    return backup if moved else None


# Single source of truth for providers: agent adapters, auth probes, login/
# logout flows, and UI metadata. Adding a provider = one entry here (agent
# class + probe); agent=None lists it in the Accounts panel but not as a seat.
PROVIDERS = {
    "claude": dict(label="Claude", cli="claude", agent=ClaudeAgent,
                   color="#E07B54", probe=probe_claude,
                   login_argv=["claude", "auth", "login"],
                   logout_argv=["claude", "auth", "logout"],
                   login_strip_env=True,
                   install_hint="npm install -g @anthropic-ai/claude-code"),
    "gpt": dict(label="GPT", cli="codex", agent=CodexAgent,
                color="#2EAE8B", probe=probe_codex,
                login_argv=["codex", "login"],
                logout_argv=["codex", "logout"],
                login_strip_env=False,
                install_hint="npm install -g @openai/codex"),
    "gemini": dict(label="Gemini", cli="agy", agent=GeminiAgent,
                   color="#5B7FE8", probe=probe_gemini,
                   login_argv=["agy"],  # interactive first run triggers OAuth
                   logout_argv=None,    # file-move: logout_gemini()
                   login_strip_env=False,
                   install_hint="Antigravity CLI installer "
                                "(installs to %LOCALAPPDATA%\\agy)"),
    "grok": dict(label="Grok", cli="grok", agent=None,  # adapter not built yet
                 color="#B8B8C8", probe=probe_grok,
                 login_argv=["grok", "login"],
                 logout_argv=None,  # hidden until the adapter task verifies it
                 login_strip_env=False,
                 install_hint="irm https://x.ai/cli/install.ps1 | iex"),
}

AGENT_TYPES = {k: v["agent"] for k, v in PROVIDERS.items() if v["agent"]}


def probe_all(home=None):
    return {pid: meta["probe"](home=home) for pid, meta in PROVIDERS.items()}


# ------------------------------------------------------- slash commands ----

COMPACT_PROMPT = (
    "Josh (the human relay operator) here -- pause the conversation for a "
    "moment. Your context is about to be reset. Write a compact, self-contained "
    "summary of this conversation so far for your own future reference: who is "
    "participating, the key points and agreements/disagreements so far, current "
    "open threads, and anything in the shared workspace worth remembering. "
    "Your next instance will see ONLY this summary plus new messages, so "
    "include everything you need. Reply with the summary only."
)

CLEAR_NOTE = ("(Josh cleared your context: you are rejoining the conversation "
              "fresh. Catch up from the messages that follow.)")

HELP_TEXT = ("Commands: /clear [seat] · /compact [seat] · /turns N · "
             "/ceiling N (until-done chats) · /stop · /help — seat is a name "
             "('claude 2') or a provider (claude/gpt/gemini); no seat means "
             "every seat. Roles are edited on the seat cards — Apply role "
             "change compacts that seat so it keeps its memory.")


def match_seats(agents, arg):
    """Resolve a /clear-/compact target. '' -> every seat; a label ('claude 2')
    -> that seat; a provider name -> all of that provider's seats."""
    if not (arg or "").strip():
        return list(range(len(agents)))
    low = arg.strip().lower()
    hits = [i for i, a in enumerate(agents) if a.name.lower() == low]
    if hits:
        return hits
    cls = AGENT_TYPES.get(low)
    return [i for i, a in enumerate(agents) if cls and type(a) is cls]


def compact_agent(agent):
    """Ask the agent to summarize the conversation for itself, then reset its
    CLI session. The caller seeds the fresh session with the summary."""
    summary = (agent.turn(COMPACT_PROMPT) or "").strip()
    agent.session_id = None
    return summary


def apply_role_flags(agents, specs, flagname):
    """Apply --role / --role-instructions "<seat>=<value>" specs to agents.

    <seat> goes through the SAME resolver as /clear and /compact
    (match_seats: a label like "claude 2", or a provider name for all of that
    provider's seats) so the CLI can't drift from the slash commands. Must run
    AFTER labels are assigned — auto labels ("Claude 2") don't exist earlier.
    An unmatched seat is a HARD error, never a no-op: a typo'd --role that
    silently starts an unroled conversation is invisible until deep into the
    run (same failure shape as the queued-role-evaporates bug). Raises
    ValueError; an empty value clears the field.
    """
    attr = "role" if flagname == "--role" else "role_instructions"
    for spec in specs or []:
        seat, sep, value = spec.partition("=")
        seat, value = seat.strip(), value.strip()
        if not sep or not seat:
            raise ValueError(
                f'{flagname} needs "<seat>=<text>" (got {spec!r})')
        idxs = match_seats(agents, seat)
        if not idxs:
            valid = ", ".join(a.name for a in agents)
            raise ValueError(
                f"{flagname}: no seat matches {seat!r} (seats: {valid})")
        for i in idxs:
            setattr(agents[i], attr, value or None)


def parse_agent_token(tok):
    """provider[:model[:effort]][=label] -> (provider, model, effort, label)."""
    head, _, label = tok.partition("=")
    parts = head.split(":", 2)
    provider = parts[0].strip().lower()
    model = parts[1].strip() if len(parts) > 1 else ""
    effort = parts[2].strip() if len(parts) > 2 else ""
    return provider, model or None, effort or None, label.strip() or None


def assign_labels(slots):
    """Unique display names for seats. `slots` is [(provider, label_or_None)].

    Auto-named seats get the provider's bare name ("Claude") for the first
    seat and ordinals ("Claude 2") after that; explicit labels win as-is.
    Raises ValueError on duplicate final labels.
    """
    labels = []
    auto_counts = {}
    for provider, explicit in slots:
        if explicit:
            labels.append(explicit)
            continue
        base = AGENT_TYPES[provider].name
        auto_counts[base] = auto_counts.get(base, 0) + 1
        n = auto_counts[base]
        labels.append(base if n == 1 else f"{base} {n}")
    seen = set()
    for lb in labels:
        if lb.lower() in seen:
            raise ValueError(
                f"duplicate agent label '{lb}' -- use =label to disambiguate")
        seen.add(lb.lower())
    return labels


# ---------------------------------------------------------- interjection ----

# ---------------------------------------------------------------- sessions --
# Durable state for one conversation directory. Three files, three readers:
#   transcript.md   human-readable log (unchanged — still the thing Josh opens)
#   messages.jsonl  one UI-ready row per message; the replay source
#   meta.json       everything needed to RESUME: seat config + CLI session ids
# The agents' memory lives inside their own CLIs, keyed by session_id. Losing
# that one string per seat is the entire reason a restart used to start over;
# everything else in here is bookkeeping around it.

SESSION_META = "meta.json"
SESSION_MSGS = "messages.jsonl"
# v2 added mode/turn/cursor/next_speaker/closing (all with v1-safe defaults in
# rehydrate, so v1 sessions stay fully openable and continuable). Always WRITE
# the newest version: an older copy of the code opening a v2 session refuses it
# as view-only, which is the right direction of protection — old code silently
# ignoring scheduler state would mis-resume a conversation.
META_VERSION = 2
META_VERSIONS_OK = (1, 2)


def _atomic_write(path, text):
    """Write via tmp + os.replace so a crash can't leave a half-written meta.

    The replace retries briefly: on Windows a concurrent READER that lacks
    FILE_SHARE_DELETE (an editor with meta.json open, the search indexer, a
    test polling the file) blocks the rename with PermissionError for a
    moment. Transient by nature — surrendering would crash a commit instead.
    """
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for _ in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def session_path(session_id):
    """Resolve a session id (directory BASENAME) to its folder, or None.

    The id arrives from the UI, so it is untrusted: anything that isn't a
    direct child of SESSIONS_DIR is rejected rather than normalised.
    """
    if (not session_id or not isinstance(session_id, str)
            or "/" in session_id or "\\" in session_id
            or session_id in (".", "..")):
        return None
    path = os.path.abspath(os.path.join(SESSIONS_DIR, session_id))
    if os.path.dirname(path) != os.path.abspath(SESSIONS_DIR):
        return None
    return path if os.path.isdir(path) else None


class SessionStore:
    """Owns the three files of one session directory.

    `record` writes the transcript line and the JSONL row in one call on
    purpose — two call sites is how a replayed chat drifts from the one Josh
    actually watched.
    """

    def __init__(self, session_dir):
        self.dir = session_dir
        self.id = os.path.basename(session_dir.rstrip("\\/"))
        self.transcript = os.path.join(session_dir, "transcript.md")
        self.messages = os.path.join(session_dir, SESSION_MSGS)
        self.meta_path = os.path.join(session_dir, SESSION_META)
        self.created = datetime.datetime.now().isoformat(timespec="seconds")
        self._lock = threading.Lock()

    def open_transcript(self, title, agents, turns):
        with open(self.transcript, "w", encoding="utf-8") as f:
            f.write(f"# AI Chat — {title}\n\n"
                    f"*{datetime.datetime.now():%Y-%m-%d %H:%M} · "
                    f"{' ↔ '.join(a.name for a in agents)} · "
                    f"max {turns} rounds*\n")

    def record(self, name, text, *, speaker=None, provider=None,
               round=0, meta="", role=None):
        """Append one message. Returns the row (== the UI `message` payload).

        `role` is stamped into the row AT RECORD TIME on purpose: captions and
        replay read the row, never live seat config, so editing a role in round
        6 cannot retroactively relabel rounds 1-5 (ROLES_DESIGN.md)."""
        row = {"speaker": name.lower() if speaker is None else speaker,
               "provider": provider, "name": name, "text": text,
               "round": round, "meta": meta, "role": role or None}
        with self._lock:
            with open(self.transcript, "a", encoding="utf-8") as f:
                if row["speaker"] == "system":
                    f.write(f"\n*{text}*\n")
                else:
                    f.write(f"\n## {name}{f' — {role}' if role else ''}"
                            f"{f'  · {meta}' if meta else ''}\n\n"
                            f"{text}\n")
            with open(self.messages, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def system(self, text, round=0):
        """/clear, /compact, round-cap and agent-error notices. These MUST be
        persisted: without them a reopened chat stops explaining its own
        discontinuities ("Claude's memory was cleared" silently vanishes)."""
        return self.record("relay", text, speaker="system", provider=None,
                           round=round)

    def save(self, state, ended=None):
        """Snapshot resumable state to meta.json. Call after every turn."""
        if ended is not None:
            state["ended"] = bool(ended)
        agents = state["agents"]
        meta = {
            "v": META_VERSION,
            "id": self.id,
            "title": state.get("title", ""),
            "created": state.get("created") or self.created,
            "updated": datetime.datetime.now().isoformat(timespec="seconds"),
            "ended": bool(state.get("ended")),
            "workspace": state["workspace"],
            "topic": state.get("topic", ""),
            "yolo": bool(state.get("yolo")),
            "turns": state["turns"],
            "rnd": state["rnd"],
            "max": state["max"],
            # scheduler state (meta v2) — all by seat SLOT ID, never index, so
            # a future seat-list edit can't silently reassign any of it
            "mode": state.get("mode", DEFAULT_MODE),
            "turn": state.get("turn", 0),
            "cursor": state.get("cursor"),
            "next_speaker": state.get("next_speaker"),
            "closing": state.get("closing"),
            "moderator": state.get("moderator"),
            "until_done": bool(state.get("until_done")),
            "turn_ceiling": state.get("turn_ceiling"),
            "spawn": state.get("spawn"),
            "parent": state.get("parent"),
            "children": state.get("children"),
            "seats": [{
                "id": state["slot_ids"][i],
                "provider": state["providers"][i],
                "label": a.name,
                "model": a.model,
                "effort": a.effort,
                "session_id": a.session_id,
                "role": a.role,
                "role_instructions": a.role_instructions,
                "introduced": bool(state["introduced"][i]),
                "pending": list(state["pending"][i]),
            } for i, a in enumerate(agents)],
        }
        with self._lock:
            _atomic_write(self.meta_path,
                          json.dumps(meta, ensure_ascii=False, indent=1))
        return meta


def make_log(state, store, echo=None):
    """The one logger both loops use.

    Call sites only know a display name, but a replay row needs the stable seat
    id — so the mapping lives here once instead of in each front end. Anything
    that isn't Josh or a seated agent is a relay note, not a speaker.
    """
    def log(name, text, meta=""):
        if name.startswith("Josh"):
            row = store.record("Josh", text, speaker="josh", provider=None,
                               round=state["rnd"], meta=meta)
        else:
            for i, a in enumerate(state["agents"]):
                if a.name == name:
                    # a.role is read at call time: rows carry the role the seat
                    # had WHEN IT SPOKE, not whatever it was later changed to
                    row = store.record(name, text,
                                       speaker=state["slot_ids"][i],
                                       provider=state["providers"][i],
                                       round=state["rnd"], meta=meta,
                                       role=a.role)
                    break
            else:
                row = store.system(text, round=state["rnd"])
        if echo:
            echo(row)
        return row
    return log


def read_meta(session_dir):
    try:
        with open(os.path.join(session_dir, SESSION_META), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def read_messages(session_dir):
    """Ordered UI rows. A truncated final line (crash mid-append) is skipped,
    never fatal — one bad row must not make a whole chat unopenable."""
    rows = []
    try:
        with open(os.path.join(session_dir, SESSION_MSGS),
                  encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def continue_block(meta):
    """Why this session can't be resumed — '' when it can.

    Derived from seat metadata, NOT from `ended`: hitting the round cap is a
    pause, and even a wrapped chat can take another message. A stale CLI id is
    deliberately not guessed at here — that only shows up when a resume is
    actually attempted, and it must surface then rather than be predicted now.
    """
    if not meta:
        return "Legacy chat — view only"
    if meta.get("v") not in META_VERSIONS_OK:
        return "Saved by a different version — view only"
    seats = meta.get("seats") or []
    if len(seats) < 2:
        return "Incomplete chat — view only"
    unknown = [s.get("provider") for s in seats
               if s.get("provider") not in AGENT_TYPES]
    if unknown:
        return f"Unknown participant ({unknown[0]}) — view only"
    # The invariant is per-seat, not global (GPT's catch): a seat that has
    # spoken MUST have its id, but a chat that crashed after saving Josh's
    # opener and before the first turn has no ids at all and is perfectly
    # resumable — that is exactly the case where the opener is the only
    # surviving content, so blocking it would be the worst possible failure.
    orphaned = [s.get("label") or s.get("provider") for s in seats
                if s.get("introduced") and not s.get("session_id")]
    if orphaned:
        return f"{orphaned[0]}'s memory wasn't saved — view only"
    return ""


def _mtime_iso(path):
    try:
        return datetime.datetime.fromtimestamp(
            os.path.getmtime(path)).isoformat(timespec="seconds")
    except OSError:
        return ""


def _pretty_slug(session_id):
    """Title for a legacy folder — from the NAME, not by reading transcript.md.
    These summaries run on the pywebview bridge thread; keep the I/O to one
    stat per folder."""
    body = re.sub(r"^\d{8}-\d{6}-", "", session_id).replace("-", " ").strip()
    return (body[:1].upper() + body[1:]) if body else session_id


def session_project(session_dir, workspace):
    """Sidebar group label: a CUSTOM working folder is a 'project'; a
    workspace inside ANY session folder (this one's default, or a parent's
    default for a spawned team) groups under '' (no project)."""
    if not workspace:
        return ""
    try:
        ws = os.path.abspath(workspace)
        roots = [os.path.abspath(session_dir), os.path.abspath(SESSIONS_DIR)]
        if any(os.path.commonpath([ws, r]) == r for r in roots):
            return ""
    except ValueError:          # different drives — definitely not inside
        pass
    return os.path.basename(ws.rstrip("\\/")) or ws


def session_summary(session_dir, meta=None):
    """One sidebar row. Pure file reads (one meta.json + one stat)."""
    sid = os.path.basename(session_dir.rstrip("\\/"))
    if meta is None:
        meta = read_meta(session_dir)
    if not meta:
        return {"id": sid, "title": _pretty_slug(sid), "created": "",
                "updated": _mtime_iso(session_dir), "ended": True,
                "participants": [], "rounds": 0, "max": 0, "legacy": True,
                "workspace": os.path.join(session_dir, "workspace"),
                "transcript": os.path.join(session_dir, "transcript.md"),
                "project": "",
                "can_continue": False,
                "can_continue_reason": "Legacy chat — view only"}
    reason = continue_block(meta)
    return {
        # The directory basename is the public identifier. Never trust a stale
        # or hand-edited value inside meta.json to become a UI-provided path.
        "id": sid,
        # full text — the rail ellipsizes in CSS and tooltips the rest
        "title": meta.get("title") or _pretty_slug(sid),
        "created": meta.get("created", ""),
        "updated": meta.get("updated", "") or _mtime_iso(session_dir),
        "ended": bool(meta.get("ended")),
        "participants": [{"id": s.get("id"), "provider": s.get("provider"),
                          "name": s.get("label") or s.get("provider"),
                          "model": s.get("model") or "default",
                          "effort": s.get("effort") or "",
                          "role": s.get("role") or "",
                          "role_instructions": s.get("role_instructions") or ""}
                         for s in meta.get("seats") or []],
        "rounds": meta.get("rnd", 0),
        "max": meta.get("max", 0),
        "mode": meta.get("mode", DEFAULT_MODE),
        "until_done": bool(meta.get("until_done")),
        "spawn": meta.get("spawn") or {},
        "parent": meta.get("parent"),
        "workspace": meta.get("workspace", ""),
        "project": session_project(session_dir, meta.get("workspace", "")),
        "transcript": os.path.join(session_dir, "transcript.md"),
        "legacy": False,
        "can_continue": not reason,
        "can_continue_reason": reason,
    }


def list_sessions():
    """Sidebar list, newest first. One meta.json read per folder — no
    subprocess, no workspace walking, no transcript parsing."""
    out = []
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return out
    for name in names:
        d = os.path.join(SESSIONS_DIR, name)
        if os.path.isdir(d):
            out.append(session_summary(d))
    out.sort(key=lambda s: (s.get("updated") or "", s.get("id") or ""),
             reverse=True)
    return out


def rehydrate(meta, workspace=None):
    """Rebuild live Agents from saved meta and return a loop-ready state dict.

    Object construction only — no subprocess runs — so this is safe anywhere.
    The caller supplies `store`/`log`/`transcript` and can then call the same
    _rounds() it uses for a fresh conversation.
    """
    reason = continue_block(meta)
    if reason:
        raise ValueError(reason)
    ws = workspace or meta.get("workspace")
    seats = meta["seats"]
    agents = []
    for s in seats:
        a = AGENT_TYPES[s["provider"]](
            ws, yolo=bool(meta.get("yolo")),
            model=s.get("model") or None, effort=s.get("effort") or None,
            name=s.get("label") or None,
            role=s.get("role") or None,
            role_instructions=s.get("role_instructions") or None)
        a.session_id = s.get("session_id") or None
        agents.append(a)
    return {
        "agents": agents,
        "slot_ids": [s.get("id", i) for i, s in enumerate(seats)],
        "providers": [s["provider"] for s in seats],
        "workspace": ws,
        "topic": meta.get("topic", ""),
        "title": meta.get("title", ""),
        "created": meta.get("created", ""),
        "yolo": bool(meta.get("yolo")),
        "turns": meta.get("turns", 10),
        "rnd": meta.get("rnd", 0),
        "max": meta.get("max", meta.get("rnd", 0)),
        "ended": bool(meta.get("ended")),
        "pending": {i: list(s.get("pending") or [])
                    for i, s in enumerate(seats)},
        "introduced": [bool(s.get("introduced")) for s in seats],
        # scheduler state (meta v2); v1 defaults reproduce the old resume
        # behavior exactly: restart the round at seat 0, queues intact.
        # `turn` is only budget arithmetic, so the v1 approximation is fine.
        "mode": meta.get("mode", DEFAULT_MODE),
        "turn": meta.get("turn", meta.get("rnd", 0) * max(1, len(seats))),
        "cursor": meta.get("cursor"),          # None -> loop starts at seat 0
        "next_speaker": meta.get("next_speaker"),
        "closing": meta.get("closing"),
        "moderator": meta.get("moderator"),
        "until_done": bool(meta.get("until_done")),
        "turn_ceiling": meta.get("turn_ceiling"),
        "spawn": meta.get("spawn"),
        "parent": meta.get("parent"),
        "children": meta.get("children"),   # hints — a child may be deleted
    }


def start_stdin_reader(q):
    def reader():
        while True:
            try:
                line = input()
            except (EOFError, OSError):
                return
            if line.strip():
                q.put(line.strip())
    threading.Thread(target=reader, daemon=True).start()


def drain_human_input(q, say_file):
    """Collect anything Josh typed or dropped into say.txt since last check."""
    lines = []
    while True:
        try:
            lines.append(q.get_nowait())
        except queue.Empty:
            break
    if os.path.exists(say_file):
        try:
            with open(say_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            os.remove(say_file)
            if content:
                lines.append(content)
        except OSError:
            pass
    return lines


# ------------------------------------------------------------------ main ----

# One grammar for every end-of-reply directive: [[WRAP]], [[NEXT: seat]], and
# the coming [[SPAWN:]]/[[TEAM:]]/[[PASS]]. wrap_called is reimplemented over
# peel_directives so the wrap rule and any new directive can never drift apart.
KNOWN_DIRECTIVES = ("WRAP", "NEXT", "PASS", "SPAWN", "TEAM")
# matched against body[rfind("[["):] — anchoring each peel at the LAST "[["
# keeps a stacked tail ("… [[NEXT: A]] [[WRAP]]") from collapsing into one
# directive with a garbage argument (the leftmost-match + lazy-dot trap)
_TRAILING_DIRECTIVE = re.compile(
    r"\[\[([A-Z][A-Z0-9_]*)(?:\s*:\s*(.*?))?\s*\]\]$", re.S)


def peel_directives(reply, known=KNOWN_DIRECTIVES, max_peel=4):
    """Peel [[NAME]] / [[NAME: arg]] blocks off the END of a reply.

    The rule every directive inherits from the wrap token's bug history: a
    directive fires only when it TERMINATES the reply. A bare substring check
    fired on mere mentions; requiring a standalone last line silently never
    fired (seats close a sentence with the token). Ending-on-the-token accepts
    both real forms, while mid-reply mentions have text after them and quoted/
    backticked mentions (`[[WRAP]]`, "[[WRAP]]") end on the closing mark, not
    the token — safe even in the last position.

    Returns (body, hits, unknown):
      body    — the reply with trailing directive blocks removed (rstripped)
      hits    — [(NAME, arg-or-None), …] in PEEL order, i.e. the last-written
                directive first
      unknown — directive-shaped trailing names not in `known`; callers should
                surface these to the seat, never ignore them
    """
    body = (reply or "").rstrip()
    hits, unknown = [], []
    for _ in range(max_peel):
        start = body.rfind("[[")
        if start == -1:
            break
        m = _TRAILING_DIRECTIVE.match(body[start:])
        if not m:
            break
        name, arg = m.group(1), m.group(2)
        arg = arg.strip() if arg else None
        if name in known:
            hits.append((name, arg))
        else:
            unknown.append(name)
        body = body[:start].rstrip()
    return body, hits, unknown


def wrap_called(reply):
    """True only when a seat PLAYS the wrap token to close its turn.

    The token must TERMINATE the reply (see peel_directives for the bug
    history this rule comes from). A reply may stack directives at the end —
    "Over to you. [[WRAP]] [[NEXT: GPT]]" wraps in either order.
    """
    _, hits, _ = peel_directives(reply)
    return any(name == "WRAP" for name, _ in hits)


# Signatures of a PERMANENT per-seat failure. Captured from the real CLIs on
# 2026-08-16 by resuming a bogus uuid:
#   claude: "No conversation found with session ID: <uuid>"        (exit 1)
#   codex : "thread/resume failed: no rollout found for thread id" (exit 1)
# Both raise, which is what we wanted — neither silently starts a fresh session
# and forges continuity. But the loop treated them as transient: it retried
# (a second CLI call on a guaranteed-identical error) and then hit the same
# wall every remaining round. A dead session id therefore produced N identical
# "failed twice" errors and burned 2N calls, with no way forward.
STALE_SESSION_SIGNS = (
    "no conversation found with session id",
    "no rollout found for thread id",
    "conversation not found",
    "session not found",
)


def stale_session(exc):
    """True when a turn failed because this seat's saved CLI session is gone."""
    return any(s in str(exc).lower() for s in STALE_SESSION_SIGNS)


def fatal_seat_error(agent, exc):
    """Reason string when a failure is permanent for this seat, else ''.

    Permanent failures must not be retried and must not be repeated once a
    round: a legible error printed N times reads as a broken app rather than
    as one actionable problem. Recovery is offered, never performed — silently
    reseeding a fresh session would claim continuity the agent doesn't have.
    """
    if stale_session(exc):
        return (f"{agent.name} no longer remembers this chat — its saved CLI "
                f"session expired or was deleted. The transcript is intact. "
                f"Run /clear {agent.name.lower()} to give it a fresh session "
                f"(it will receive messages that were still queued, plus "
                f"whatever you send next), then send a message to continue.")
    if "not found on path" in str(exc).lower():
        return (f"The {type(agent).cli} CLI isn't on PATH, so {agent.name} "
                f"can't take a turn. Install it before continuing, or start "
                f"a new chat without that seat.")
    return ""


def preamble(agent, others, topic, turns, workspace, roster=None,
             mode=DEFAULT_MODE, until_done=False, ceiling=None, spawn=None):
    """`roster` is the full seat list IN TURN ORDER. Without it the roster line
    would read agent-first and so come out in a different order for every
    recipient — for a role team the order is information ("researcher speaks,
    then coder, then reviewer"), so both loops pass their `agents` list.

    `mode` swaps the cap line and adds the turn-order rule; `until_done`
    replaces the cap line entirely. The defaults keep the round-robin preamble
    byte-identical to what it always was."""
    other_names = " and ".join(a.name for a in others)
    topic_line = f"Topic: {topic}\n\n" if (topic or "").strip() else ""
    # Per-seat roles (ROLES_DESIGN.md). Public role NAMES go to every seat as a
    # one-line roster (that's what makes handoffs possible); the full private
    # instructions go only to the owning seat (keeps preambles small and stops
    # seats litigating each other's instructions). Empty when no seat has a
    # role, so role-free conversations get the exact preamble they always did.
    seated = list(roster) if roster else [agent] + list(others)
    role_block = ""
    if any(a.role for a in seated):
        roster = ", ".join(f"{a.name} = {a.role}" for a in seated if a.role)
        unassigned = [a.name for a in seated if not a.role]
        role_block = f"Roles: {roster}"
        if unassigned:
            role_block += f" ({' and '.join(unassigned)}: no assigned role)"
        role_block += ".\n"
    if agent.role or agent.role_instructions:
        own = ([f"Your role is {agent.role}."] if agent.role else []) \
            + ([agent.role_instructions] if agent.role_instructions else [])
        role_block += (
            " ".join(own) + " The other participants see your role name, not "
            "these instructions. Stay in your role for the whole conversation; "
            "if a round has nothing for your role, say so briefly and hand "
            "back.\n")
    if role_block:
        role_block += "\n"
    # Tier-1 spawning (ORCHESTRATION_DESIGN.md): the note and the capability
    # toggle together — native_spawn_note() reflects what build_cmd actually
    # grants, and the policy gate hides it entirely when tier1 is off.
    spawn_lines = []
    if (spawn or {}).get("tier1", True):
        note = agent.native_spawn_note()
        if note:
            spawn_lines.append(
                f"- {note} Keep side-tasks rare and small: each spends real "
                f"account usage, and your whole turn must finish within "
                f"{TURN_TIMEOUT // 60} minutes.")
    if int((spawn or {}).get("max_helpers") or 0) > 0:
        spawn_lines.append(
            f"- You may spawn a one-shot helper AI: END a reply with "
            f"[[SPAWN: provider[:model[:effort]] | task text]] (providers: "
            f"{', '.join(sorted(AGENT_TYPES))}). It must be the very last "
            f"thing you write (stack with {WRAP_TOKEN} or [[NEXT: ...]] if "
            f"needed); mentioning it earlier, or in quotes/backticks, does "
            f"nothing. The helper shares the workspace, runs while the "
            f"conversation continues, and its result comes back only to "
            f"you. Up to {int(spawn.get('max_helpers'))} helper(s) total "
            f"this conversation — each is a real CLI call, so spawn only "
            f"when it genuinely helps.")
    if int((spawn or {}).get("max_teams") or 0) > 0:
        spawn_lines.append(
            f"- You may spawn a whole SUB-CONVERSATION: END a reply with "
            f"[[TEAM: <agents-spec> | rounds=N mode=<mode> | <task>]] where "
            f"<agents-spec> is a comma list like "
            f"claude:claude-haiku-4-5:low,gpt (the middle segment is "
            f"optional; at most {CHILD_ROUNDS} rounds). The team shares this "
            f"workspace, runs on the side, and reports its outcome back only "
            f"to you. Same trailing-token rules as {WRAP_TOKEN}. Up to "
            f"{int(spawn.get('max_teams'))} team(s) this conversation — a "
            f"team is MANY real CLI calls, so prefer cheap models and spawn "
            f"one only for genuinely separable work.")
    spawn_block = ("Delegation:\n" + "\n".join(spawn_lines) + "\n\n"
                   if spawn_lines else "")
    dup_note = ""
    if any(type(a) is type(agent) for a in others):
        dup_note = (
            f"Note: one or more of the other participants run on the same "
            f"underlying model as you. They are separate instances with their "
            f"own memory -- not echoes of you. Any relayed message prefixed with "
            f"another name and 'said:' was written by that other instance, never "
            f"by you. Always speak only as {agent.name}. "
        )
    n_seats = len(seated)
    if until_done:
        cap_line = (
            f"- This conversation runs until the task is genuinely done -- "
            f"there is no fixed round count (a safety limit of "
            f"{ceiling or DEFAULT_CEILING} total turns exists so it cannot "
            f"run away). When the work is complete, END a reply with the "
            f"token {WRAP_TOKEN} to wind down -- it must be the very last "
            f"thing you write. Mentioning it anywhere earlier, or in quotes/"
            f"backticks, does not trigger it. Do not pad: wrap as soon as "
            f"the goal is met.\n")
    elif mode in ("speaker", "moderator"):
        cap_line = (
            f"- The conversation has a budget of about {turns} rounds "
            f"({turns * n_seats} turns in total). If the topic feels "
            f"genuinely exhausted, END a reply with the token {WRAP_TOKEN} to "
            f"wind down -- it must be the very last thing you write. "
            f"Mentioning it anywhere earlier, or in quotes/backticks, does "
            f"not trigger it.\n")
    else:
        cap_line = (
            f"- The conversation runs at most {turns} rounds. If the topic feels "
            f"genuinely exhausted, END a reply with the token {WRAP_TOKEN} to wind "
            f"down -- it must be the very last thing you write. Mentioning it "
            f"anywhere earlier, or in quotes/backticks, does not trigger it.\n")
    order_line = ""
    if mode == "speaker":
        names = " / ".join(a.name for a in others)
        order_line = (
            f"- Turn order: YOU pick who speaks next. End your reply with the "
            f"token [[NEXT: <name>]] naming one of {names} -- it must be the "
            f"very last thing you write (if you are also wrapping, stack "
            f"{WRAP_TOKEN} and [[NEXT: ...]] at the very end, or just "
            f"{WRAP_TOKEN}). Mentioning it anywhere earlier, or in quotes/"
            f"backticks, does not pass the turn. If you leave it out, the "
            f"turn passes in listed order. Share the floor: prefer whoever "
            f"has been quiet longest unless the discussion clearly needs "
            f"someone specific.\n")
    elif mode == "moderator":
        order_line = (
            f"- Turn order: a moderator chooses who speaks after each reply, "
            f"so you may speak twice in a row or wait several turns. Do not "
            f"hand off explicitly -- just end your reply.\n")
    elif mode == "parallel":
        order_line = (
            f"- Turns run in simultaneous rounds: every participant answers "
            f"the same backlog at once, and all replies are shared as the "
            f"round completes -- replies to what you say now reach you next "
            f"round.\n")
    return (
        f"You are {agent.name}, in a live multi-AI conversation with {other_names}. "
        f"{dup_note}"
        f"Messages from the other participant(s) are relayed to you verbatim, prefixed "
        f"with the speaker's name. A human (Josh) set this up and may occasionally "
        f"interject; he is otherwise not involved -- talk to the other AI(s), not to him.\n\n"
        f"{role_block}"
        f"{topic_line}"
        f"{spawn_block}"
        f"Ground rules:\n"
        f"- Conversational replies, a few paragraphs at most. No markdown headers.\n"
        f"- You share a scratch workspace (your current directory) with the other "
        f"participant(s) -- you may read/write files there if useful, e.g. to "
        f"co-write a document.\n"
        f"{cap_line}"
        f"{order_line}"
        f"- Be yourself; disagree freely; build on each other's points.\n"
    )


# ----------------------------------------------------------- shared loop ----
# One loop, two front ends. The loop owns turn order, prompt composition,
# retries, fan-out, the wrap countdown, and every save; a LoopIO object is the
# only thing that differs between the terminal and the app. Anything
# loop-shaped goes HERE — the era of writing it twice is over.

class LoopIO:
    """Front-end seam for run_rounds. Every hook is a safe no-op so a headless
    test can drive the real loop with `run_rounds(state, LoopIO())` and fake
    agents — no console, no window, no tokens spent."""

    def emit(self, event, payload=None):
        """Semantic events: thinking / thinking_done / message / status /
        agent_error. `message` payloads are the persisted row from make_log."""

    def drain_human(self):
        """Return raw human input lines gathered since the last turn."""
        return []

    def should_stop(self):
        """External stop request (the app's Stop button)."""
        return False

    def on_turn_boundary(self, state):
        """Hook before each prompt is composed (app: staged role commit)."""


class CLIIO(LoopIO):
    """Terminal front end: stdin + say.txt in, ANSI status lines out.
    Message rows are NOT printed here — the CLI's make_log echo owns that."""

    def __init__(self, human_q, say_file):
        self._q = human_q
        self._say = say_file

    def drain_human(self):
        return drain_human_input(self._q, self._say)

    def emit(self, event, payload=None):
        p = payload or {}
        if event == "thinking":
            if p.get("until_done"):
                status(f"turn {p.get('turn')}/{p.get('ceiling')} · "
                       f"{p.get('name')} is thinking…")
            else:
                status(f"round {p.get('round')}/{p.get('turns')} · "
                       f"{p.get('name')} is thinking…")
        elif event == "status":
            status(p.get("text", ""))
        elif event == "agent_error":
            status(p.get("message", ""))


def dispatch_command(state, text, io):
    """Handle a /command from Josh. Returns True if the run should stop.

    Shared by both front ends: the loop routes drained slash input here, and
    the app also calls it from its idle-path worker thread."""
    # Front ends echo commands themselves (the UI immediately, the CLI via the
    # log's echo); persist the row without emitting a duplicate live message.
    state["log"]("Josh (human)", text, meta="command")
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.lower().lstrip("/"), arg.strip()
    if cmd == "stop":
        state["store"].save(state)
        return True
    if cmd == "turns":
        if state.get("until_done"):
            note = ("This chat runs until done — use /ceiling N to set the "
                    "safety ceiling.")
        elif arg.isdigit():
            state["max"] = max(state["rnd"], int(arg))
            note = f"Round cap is now {state['max']}."
        else:
            note = "Usage: /turns N"
        io.emit("status", {"text": note})
        state["store"].system(note, round=state["rnd"])
        state["store"].save(state)
    elif cmd == "ceiling":
        if not state.get("until_done"):
            note = ("/ceiling only applies to until-done chats — use "
                    "/turns N here.")
        elif arg.isdigit():
            state["turn_ceiling"] = max(state.get("turn", 0), int(arg))
            note = f"Safety ceiling is now {state['turn_ceiling']} turns."
        else:
            note = "Usage: /ceiling N"
        io.emit("status", {"text": note})
        state["store"].system(note, round=state["rnd"])
        state["store"].save(state)
    elif cmd in ("clear", "compact"):
        seat_command(state, cmd, arg, io)
    elif cmd == "help":
        io.emit("status", {"text": HELP_TEXT})
        state["store"].system(HELP_TEXT, round=state["rnd"])
        state["store"].save(state)
    else:
        note = f"Unknown command /{cmd}. {HELP_TEXT}"
        io.emit("status", {"text": note})
        state["store"].system(note, round=state["rnd"])
        state["store"].save(state)
    return False


def seat_command(state, cmd, arg, io):
    """/clear and /compact bodies, shared by both front ends."""
    idxs = match_seats(state["agents"], arg)
    if not idxs:
        note = f"No seat matches '{arg}'. {HELP_TEXT}"
        io.emit("status", {"text": note})
        state["store"].system(note, round=state["rnd"])
        state["store"].save(state)
        return
    for i in idxs:
        agent = state["agents"][i]
        if cmd == "compact":
            note = f"Compacting {agent.name}'s context…"
            io.emit("status", {"text": note})
            state["store"].system(note, round=state["rnd"])
            try:
                summary = compact_agent(agent)
            except Exception as e:
                note = f"{agent.name} compact failed: {str(e)[:200]}"
                io.emit("status", {"text": note})
                state["store"].system(note, round=state["rnd"])
                continue
            state["introduced"][i] = False
            state["pending"][i].insert(0, "(Josh compacted your context. "
                                          "Your own summary of the "
                                          "conversation so far:)\n\n"
                                          + summary)
            state["log"](agent.name, summary,
                         meta="context compacted — self-summary")
            note = f"{agent.name}'s context compacted."
            io.emit("status", {"text": note})
            state["store"].system(note, round=state["rnd"])
        else:
            agent.session_id = None
            state["introduced"][i] = False
            state["pending"][i].insert(0, CLEAR_NOTE)
            io.emit("status", {"text": f"{agent.name}'s context cleared."})
            state["store"].system(f"{agent.name}'s context was cleared.",
                                  round=state["rnd"])
    state["store"].save(state)


def slot_index(state, sid):
    """Seat-list index for a slot id, or None if that seat no longer exists."""
    if sid is None:
        return None
    try:
        return state["slot_ids"].index(sid)
    except ValueError:
        return None


def compose_prompt(state, i):
    """Build seat i's next prompt WITHOUT touching its queue.

    Commit-consume: the backlog is snapshotted here and deleted only by
    commit_reply, so a failed turn "restores" the queue by construction —
    nothing was removed. Returns (message, consumed, first_turn).
    """
    agents = state["agents"]
    agent = agents[i]
    backlog = list(state["pending"][i])
    parts = []
    first_turn = not state["introduced"][i]
    if first_turn:
        parts.append(preamble(agent, [a for a in agents if a is not agent],
                              state["topic"], state["turns"],
                              state["workspace"], roster=agents,
                              mode=state.get("mode", DEFAULT_MODE),
                              until_done=bool(state.get("until_done")),
                              ceiling=state.get("turn_ceiling"),
                              spawn=state.get("spawn")))
        # parallel/free round 1 with no opener: EVERY seat opens
        # simultaneously — the honest semantics of those modes (CLI-only;
        # the app always seeds an opener)
        if (i == 0 or state.get("mode") in ("parallel", "free")) \
                and state["rnd"] == 1 and not backlog:
            parts.append("You open the conversation. Go.")
    if backlog:
        parts.append("\n\n".join(backlog))
    return "\n\n".join(parts), len(backlog), first_turn


def commit_reply(state, i, reply, consumed, io):
    """Deliver a successful turn: consume exactly the composed backlog, flip
    introduced, log + emit the row, fan out to every other seat, count the
    turn, save. The one implementation of the queue invariant — the saved
    queues always match what each seat is still owed."""
    agents = state["agents"]
    agent = agents[i]
    if not state["introduced"][i]:
        state["introduced"][i] = True
    del state["pending"][i][:consumed]
    # captions read the persisted row (role stamped at record time), never
    # live seat config — a later role edit can't relabel this message
    row = state["log"](agent.name, reply, meta=f"round {state['rnd']}")
    io.emit("message", row)
    for j, other in enumerate(agents):
        if other is not agent:
            state["pending"][j].append(f"{agent.name} said:\n{reply}")
    state["turn"] = state.get("turn", 0) + 1
    state["store"].save(state)
    return row


def commit_skip(state, i, note, io, fatal=False):
    """A visible skip: nothing forged, nothing consumed (commit-consume means
    the backlog was never removed), the note persisted, state saved. A
    reopened chat that silently omits its failures stops explaining its gaps.
    """
    payload = {"speaker": state["slot_ids"][i],
               "provider": state["providers"][i], "message": note}
    if fatal:
        payload["fatal"] = True
    io.emit("agent_error", payload)
    state["store"].system(note, round=state["rnd"])
    state["store"].save(state)


def choose_next_seat(state):
    """Peek at (index, source) of the seat that takes the next turn.

    Authority order (ORCHESTRATION_DESIGN.md): the closing list — a wrap in
    progress — beats everything; then mode-specific picks (speaker's
    [[NEXT:]], the moderator — landing in their phases); then the round-robin
    cursor. Pure peek apart from dropping closing ids that no longer resolve:
    consumption happens in the loop AFTER the lap/cap check, so a cap-stop
    can't eat a seat's closing turn.

    Returns (None, 'wrapped') when a closing sequence has run out of seats.
    """
    closing = state.get("closing")
    if closing is not None:
        while closing and slot_index(state, closing[0]) is None:
            closing.pop(0)      # seat vanished; dropping the id is all we can do
        if not closing:
            return None, "wrapped"
        return slot_index(state, closing[0]), "closing"
    if state.get("mode") == "speaker":
        idx = slot_index(state, state.get("next_speaker"))
        if idx is not None:
            return idx, "next"
    idx = slot_index(state, state.get("cursor"))
    return (0 if idx is None else idx), "cursor"


def start_closing(state, i):
    """Begin the wrap countdown: every OTHER seat gets one last word, in list
    order starting after the wrapper — the same order the old closing_left
    countdown produced, but persisted, so a wrap survives pause/resume."""
    ids = state["slot_ids"]
    state["closing"] = list(ids[i + 1:]) + list(ids[:i])


# ------------------------------------------------- tier-2 spawned helpers ---

HELPER_PROMPT = (
    "You are a one-shot helper spawned by {requester} from a live multi-AI "
    "conversation. You share their workspace (your current directory). "
    "Complete the task below and reply with the result only -- you get no "
    "follow-up turn.\n\nTask from {requester}:\n{task}"
)
MAX_INFLIGHT_HELPERS = 2

# Tier-3 sub-conversations: a seat spawns a whole child conversation. The
# child is a NORMAL session (own folder/transcript/meta, replayable from the
# rail); it inherits the parent's workspace and runs until-done with this
# hard rounds ceiling. Depth is 1: children cannot spawn helpers or teams.
CHILD_ROUNDS = 6
TEAM_REPORT_PROMPT = (
    "This sub-conversation is ending. Report the outcome for {requester} "
    "(who spawned this team from another conversation): what was decided or "
    "produced, and the workspace paths of any artifacts. Reply with the "
    "report only."
)


def parse_team(arg):
    """'<agents-spec> | <opener>' or '<agents-spec> | rounds=N mode=<m> |
    <opener>' -> (slots, opts, opener). Raises ValueError seat-facing."""
    parts = [p.strip() for p in (arg or "").split("|")]
    if len(parts) == 2:
        spec, opts_s, opener = parts[0], "", parts[1]
    elif len(parts) == 3:
        spec, opts_s, opener = parts
    else:
        raise ValueError("a TEAM needs '<agents-spec> | <opener>' or "
                         "'<agents-spec> | rounds=N mode=<mode> | <opener>'")
    if not opener:
        raise ValueError("the TEAM opener (its task) is empty")
    slots = [parse_agent_token(t) for t in spec.split(",") if t.strip()]
    if len(slots) < 2:
        raise ValueError("a TEAM needs at least two seats")
    unknown = sorted({p for p, _, _, _ in slots if p not in AGENT_TYPES})
    if unknown:
        raise ValueError(f"unknown provider {unknown[0]!r} — valid: "
                         f"{', '.join(sorted(AGENT_TYPES))}")
    opts = {}
    for tok in opts_s.split():
        k, _, v = tok.partition("=")
        if k == "rounds" and v.isdigit():
            opts["rounds"] = int(v)
        elif k == "mode":
            m = v.replace("-", "_")
            if m not in IMPLEMENTED_MODES:
                raise ValueError(f"unknown team mode {v!r}")
            opts["mode"] = m
        else:
            raise ValueError(f"unknown TEAM option {tok!r}")
    return slots, opts, opener


def parse_spawn(arg):
    """'provider[:model[:effort]] | task text' -> (provider, model, effort,
    task). Raises ValueError with a SEAT-FACING message (it lands in the
    requester's queue — unknown providers are surfaced, never silent)."""
    spec, sep, task = (arg or "").partition("|")
    task = task.strip()
    if not sep or not task:
        raise ValueError("a SPAWN needs 'provider[:model[:effort]] | "
                         "task text'")
    provider, model, effort, label = parse_agent_token(spec.strip())
    if label:
        raise ValueError("SPAWN takes no '=label' part")
    if provider not in AGENT_TYPES:
        raise ValueError(f"unknown provider {provider!r} — valid: "
                         f"{', '.join(sorted(AGENT_TYPES))}")
    return provider, model, effort, task


class SpawnManager:
    """Side-work (tier-2 helpers) for one conversation.

    Helper threads complete into an internal queue; results are moved into
    the requester's pending ONLY by drain_into_pending, which every loop
    calls from its boundary (single-threaded, or under the conversation lock
    in parallel/free) — so pending/meta writes never race a helper thread.
    The manager itself takes no locks: callers own the synchronization.
    """

    def __init__(self, state, io):
        self._state = state
        self._io = io
        self._results = queue.Queue()
        self._inflight = []              # helper ids still running

    def _spawn_cfg(self):
        return self._state.setdefault("spawn", {})

    def inflight(self):
        return len(self._inflight)

    def announce_lost_helpers(self):
        """Run-start: anything in spawn.pending_helpers/_teams died with the
        last process. Tell the requester (never silently re-run spend)."""
        state = self._state
        cfg = state.get("spawn") or {}
        lost = cfg.get("pending_helpers") or []
        lost_teams = cfg.get("pending_teams") or []
        if not lost and not lost_teams:
            return
        for h in lost:
            idx = slot_index(state, h.get("requester"))
            if idx is not None:
                state["pending"][idx].append(
                    f"(Relay: a helper you spawned ({h.get('spec')}) was "
                    f"lost when the last run ended — re-spawn it if still "
                    f"needed.)")
            state["store"].system(
                f"A spawned helper ({h.get('spec')}) was lost with the "
                f"previous run.", round=state["rnd"])
        for t in lost_teams:
            idx = slot_index(state, t.get("requester"))
            if idx is not None:
                state["pending"][idx].append(
                    f"(Relay: the team you spawned was interrupted with the "
                    f"last run. Its partial transcript is saved as session "
                    f"'{t.get('child')}' and can be reopened from the "
                    f"sidebar.)")
            state["store"].system(
                f"A spawned team ({t.get('child')}) was interrupted with "
                f"the previous run.", round=state["rnd"])
        self._spawn_cfg()["pending_helpers"] = []
        self._spawn_cfg()["pending_teams"] = []
        state["store"].save(state)

    def request_helper(self, req_idx, provider, model, effort, task):
        """Start a helper for seat req_idx. Returns an error string (goes to
        the requester) or None on success. Caller holds the conversation
        lock where one exists."""
        state = self._state
        cfg = self._spawn_cfg()
        cap = int(cfg.get("max_helpers") or 0)
        used = int(cfg.get("helpers_used") or 0)
        if cap <= 0:
            return ("helpers are disabled for this conversation "
                    "(--spawn-helpers N in the CLI, or the Helpers setting "
                    "in the app)")
        if used >= cap:
            return f"the helper budget ({cap}) is exhausted"
        if len(self._inflight) >= MAX_INFLIGHT_HELPERS:
            return (f"{MAX_INFLIGHT_HELPERS} helpers are already running — "
                    f"try again next turn")
        hid = used + 1
        cfg["helpers_used"] = hid
        spec = provider + (f":{model}" if model else "") \
            + (f":{effort}" if effort else "")
        cfg.setdefault("pending_helpers", []).append(
            {"requester": state["slot_ids"][req_idx], "spec": spec,
             "task_head": task[:100]})
        self._inflight.append(hid)
        requester = state["agents"][req_idx].name
        label = PROVIDERS[provider]["label"]
        state["store"].system(
            f"{requester} spawned a {label} helper ({spec}): "
            f"{task[:100]}{'…' if len(task) > 100 else ''}",
            round=state["rnd"])
        self._io.emit("status", {"text": f"{requester} spawned a {label} "
                                         f"helper — running…"})
        state["store"].save(state)
        threading.Thread(
            target=self._run_helper,
            args=(hid, req_idx, provider, model, effort, task),
            daemon=True).start()
        return None

    def _run_helper(self, hid, req_idx, provider, model, effort, task):
        state = self._state
        agent = AGENT_TYPES[provider](
            state["workspace"], yolo=bool(state.get("yolo")),
            model=model, effort=effort, name=f"Helper {hid}")
        requester = state["agents"][req_idx].name
        prompt = HELPER_PROMPT.format(requester=requester, task=task)
        try:
            text = agent.turn(prompt)
            self._results.put(("helper", hid, req_idx, provider, model,
                               text, None))
        except Exception as e:
            self._results.put(("helper", hid, req_idx, provider, model,
                               None, str(e)[:200]))

    # ------------------------------------------------- tier-3 teams -------
    def request_team(self, req_idx, slots, opts, opener):
        """Spawn a child conversation. Returns an error string for the
        requester, or None. Caller holds the conversation lock if any."""
        state = self._state
        cfg = self._spawn_cfg()
        cap = int(cfg.get("max_teams") or 0)
        used = int(cfg.get("teams_used") or 0)
        if cap <= 0:
            return ("teams are disabled for this conversation "
                    "(--spawn-teams N in the CLI, or the Teams setting in "
                    "the app)")
        if used >= cap:
            return f"the team budget ({cap}) is exhausted"
        if cfg.get("pending_teams"):
            return "a team is already running — one at a time"
        try:
            labels = assign_labels([(p, lb) for p, _, _, lb in slots])
        except ValueError as e:
            return str(e)
        rounds = min(int(opts.get("rounds") or CHILD_ROUNDS), CHILD_ROUNDS)
        mode = opts.get("mode") or DEFAULT_MODE
        requester = state["agents"][req_idx].name

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        slug = re.sub(r"[^a-z0-9]+", "-", opener.lower())[:32].strip("-") \
            or "team"
        child_dir = os.path.join(SESSIONS_DIR, f"{stamp}-team-{slug}")
        workspace = state["workspace"]        # inherited: shared artifacts
        os.makedirs(child_dir, exist_ok=True)
        agents = [AGENT_TYPES[p](workspace, yolo=bool(state.get("yolo")),
                                 model=m, effort=e, name=lb)
                  for (p, m, e, _), lb in zip(slots, labels)]
        store = SessionStore(child_dir)
        title = f"Team: {opener[:60]}"
        store.open_transcript(title, agents, rounds)
        child = {
            "agents": agents, "slot_ids": list(range(len(agents))),
            "providers": [p for p, _, _, _ in slots],
            "workspace": workspace, "transcript": store.transcript,
            "topic": "", "title": title, "created": store.created,
            "yolo": bool(state.get("yolo")), "turns": rounds,
            "rnd": 0, "max": rounds, "ended": False, "mode": mode,
            "until_done": True,
            "turn_ceiling": rounds * len(agents),
            # depth 1: children may use native subagents, nothing else
            "spawn": {"tier1": bool(cfg.get("tier1", True)),
                      "max_helpers": 0, "max_teams": 0},
            "parent": {"id": state["store"].id,
                       "seat": state["slot_ids"][req_idx],
                       "label": requester},
            "pending": {i: [] for i in range(len(agents))},
            "introduced": [False] * len(agents), "store": store,
        }
        child["log"] = make_log(child, store)
        tid = used + 1
        cfg["teams_used"] = tid
        cfg.setdefault("pending_teams", []).append(
            {"requester": state["slot_ids"][req_idx], "child": store.id})
        state.setdefault("children", []).append(store.id)
        state["store"].system(
            f"{requester} spawned a team ({', '.join(labels)} · {mode} · "
            f"up to {rounds} rounds): {opener[:100]}"
            f"{'…' if len(opener) > 100 else ''}", round=state["rnd"])
        self._io.emit("status", {"text": f"{requester} spawned a team "
                                         f"({', '.join(labels)}) — running "
                                         f"as '{store.id}'…"})
        state["store"].save(state)
        self._inflight.append(f"team-{tid}")
        threading.Thread(target=self._run_team,
                         args=(tid, req_idx, child, opener, requester),
                         daemon=True).start()
        return None

    def _run_team(self, tid, req_idx, child, opener, requester):
        store = child["store"]
        try:
            row_text = (f"[relayed from {requester} in "
                        f"'{self._state.get('title', '')}'] {opener}")
            store.record("Josh", row_text, speaker="josh", round=0)
            for j in child["pending"]:
                child["pending"][j].append(
                    f"Josh (human) opens the conversation: {row_text}")
            store.save(child)
            outcome = run_rounds(child, LoopIO())   # silent: replay from rail
            report, note = None, None
            if outcome != "fatal":
                try:
                    report = child["agents"][0].turn(
                        TEAM_REPORT_PROMPT.format(requester=requester))
                except Exception as e:
                    note = f"its closing report failed ({str(e)[:120]})"
            else:
                note = "it stopped early on a fatal seat error"
            if not (report or "").strip():
                # degraded but real: the child's literal last message
                rows = [r for r in read_messages(store.dir)
                        if r.get("speaker") not in ("system", "josh")]
                report = rows[-1]["text"] if rows else "(no messages)"
                note = (note or "no report was produced") + \
                    " — this is the team's last message instead"
            store.save(child, ended=True)
            with open(child["transcript"], "a", encoding="utf-8") as f:
                f.write("\n---\n*team finished — reported back*\n")
            self._results.put(("team", tid, req_idx, store.id,
                               child["rnd"], report, note))
        except Exception as e:
            self._results.put(("team", tid, req_idx, store.id,
                               child.get("rnd", 0), None, str(e)[:200]))

    def drain_into_pending(self):
        """Deliver finished helpers/teams to their REQUESTER only. Loop-
        boundary only (single-threaded, or under the conversation lock)."""
        state = self._state
        delivered = False
        while True:
            try:
                item = self._results.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == "helper":
                _, hid, req_idx, provider, model, text, err = item
                if hid in self._inflight:
                    self._inflight.remove(hid)
                plist = self._spawn_cfg().get("pending_helpers") or []
                for k, h in enumerate(plist):
                    if h.get("requester") == state["slot_ids"][req_idx]:
                        plist.pop(k)
                        break
                requester = state["agents"][req_idx].name
                label = PROVIDERS[provider]["label"]
                if err is None:
                    head = (f"(Helper {hid} ({label}"
                            f"{', ' + model if model else ''}) returned for "
                            f"{requester}:)")
                    state["pending"][req_idx].append(f"{head}\n{text}")
                    row = state["store"].record(
                        f"Helper {hid} ({label})", text,
                        speaker=f"helper-{hid}", provider=provider,
                        round=state["rnd"], meta=f"helper for {requester}")
                    self._io.emit("message", row)
                else:
                    state["pending"][req_idx].append(
                        f"(Relay: your helper request ({label}) failed and "
                        f"was NOT retried: {err}. Nothing was produced.)")
                    state["store"].system(
                        f"{requester}'s {label} helper failed: {err}",
                        round=state["rnd"])
            else:
                _, tid, req_idx, child_id, child_rnd, report, note = item
                if f"team-{tid}" in self._inflight:
                    self._inflight.remove(f"team-{tid}")
                plist = self._spawn_cfg().get("pending_teams") or []
                for k, t in enumerate(plist):
                    if t.get("child") == child_id:
                        plist.pop(k)
                        break
                requester = state["agents"][req_idx].name
                if report is None:
                    state["pending"][req_idx].append(
                        f"(Relay: your team failed: {note}. Partial "
                        f"transcript: sessions/{child_id})")
                    state["store"].system(
                        f"{requester}'s team ({child_id}) failed: {note}",
                        round=state["rnd"])
                else:
                    head = (f"(Team '{child_id}' finished after {child_rnd} "
                            f"round(s)"
                            + (f" — {note}" if note else "")
                            + f". Report for {requester}:)")
                    state["pending"][req_idx].append(
                        f"{head}\n{report}\nFull transcript: "
                        f"sessions/{child_id}")
                    row = state["store"].record(
                        f"Team report", report, speaker=f"team-{tid}",
                        provider=None, round=state["rnd"],
                        meta=f"team report for {requester}")
                    self._io.emit("message", row)
            state["store"].save(state)
            delivered = True
        return delivered

    def finish(self, timeout=None):
        """Run end: give in-flight side-work a bounded window, then declare
        stragglers lost — persisted, never silent. Single-threaded caller.
        Teams get a longer grace than helpers (they run whole rounds)."""
        if not self._inflight:
            self.drain_into_pending()
            return
        if timeout is None:
            timeout = 300 if any(str(x).startswith("team-")
                                 for x in self._inflight) else 60
        self._io.emit("status", {"text": f"waiting up to {timeout}s for "
                                         f"{len(self._inflight)} spawned "
                                         f"task(s)…"})
        deadline = time.time() + timeout
        while self._inflight and time.time() < deadline:
            self.drain_into_pending()
            if self._inflight:
                time.sleep(0.25)
        self.drain_into_pending()
        state = self._state
        cfg = state.get("spawn") or {}
        for h in list(cfg.get("pending_helpers") or []):
            state["store"].system(
                f"A spawned helper ({h.get('spec')}) was still running when "
                f"the conversation ended — its result is lost.",
                round=state["rnd"])
        for t in list(cfg.get("pending_teams") or []):
            state["store"].system(
                f"A spawned team was still running when the conversation "
                f"ended — reopen '{t.get('child')}' from the sidebar to see "
                f"how far it got.", round=state["rnd"])
        # entries stay in place: announce_lost_helpers on the next run turns
        # each into a note for its requester


def handle_spawn_directives(state, i, reply, io, mgr):
    """Honor a trailing [[SPAWN: …]] or [[TEAM: …]] from the seat that just
    spoke. One delegation per reply; every failure becomes a note in the
    REQUESTER's queue — never silent, never forged. Caller holds the
    conversation lock if one exists."""
    if mgr is None:
        return
    _, hits, _ = peel_directives(reply)
    spawns = [arg for name, arg in hits if name == "SPAWN"]
    teams = [arg for name, arg in hits if name == "TEAM"]
    if not spawns and not teams:
        return
    name = state["agents"][i].name

    def reject(note):
        state["pending"][i].append(f"(Relay: {note})")
        io.emit("status", {"text": f"{name}: {note}"})
        state["store"].save(state)

    if len(spawns) + len(teams) > 1:
        reject("only one SPAWN or TEAM per reply — none were run.")
        return
    if spawns:
        try:
            provider, model, effort, task = parse_spawn(spawns[0])
        except ValueError as ve:
            reject(f"your SPAWN was not run: {ve}")
            return
        err = mgr.request_helper(i, provider, model, effort, task)
        if err:
            reject(f"your SPAWN was not run: {err}")
        return
    try:
        slots, opts, opener = parse_team(teams[0])
    except ValueError as ve:
        reject(f"your TEAM was not run: {ve}")
        return
    err = mgr.request_team(i, slots, opts, opener)
    if err:
        reject(f"your TEAM was not run: {err}")


def set_next_speaker(state, i, reply, io):
    """Speaker mode: honor a trailing [[NEXT: seat]] from the seat that just
    spoke. Resolution goes through match_seats — the SAME resolver /clear,
    /compact and --role use — so 'gpt', 'claude 2', or a custom label all
    work. A missing directive is the common case and falls back silently to
    listed order; an invalid or self pick falls back with one status note
    (deterministic fallback — nothing in the transcript needs explaining)."""
    agents = state["agents"]
    _, hits, unknown = peel_directives(reply)
    for name in unknown:
        io.emit("status", {"text": f"{agents[i].name} played [[{name}]] — "
                                   f"not a directive; ignored"})
    target = next((arg for name, arg in hits if name == "NEXT" and arg), None)
    if not target:
        return
    idxs = match_seats(agents, target)
    if not idxs:
        io.emit("status", {"text": f"{agents[i].name} passed to {target!r} — "
                                   f"no such seat; continuing in order"})
        return
    pick = next((k for k in idxs if k != i), None)
    if pick is None:
        io.emit("status", {"text": f"{agents[i].name} picked itself — "
                                   f"passing in order"})
        return
    state["next_speaker"] = state["slot_ids"][pick]


MODERATOR_PROMPT = (
    "You are the silent moderator of a live multi-AI conversation.\n"
    "Participants: {roster}\n"
    "{topic_line}"
    "Turns taken so far: {counts}\n"
    "Recent messages, oldest first:\n{tail}\n\n"
    "Pick who should speak next: favor whoever the discussion most needs, "
    "and avoid leaving anyone out for long. Picking the same speaker twice "
    "in a row is allowed only when clearly warranted.\n"
    "Reply with EXACTLY one participant name from: {names}.\n"
    "If the conversation has clearly reached its goal or is exhausted, reply "
    "with the single word DONE.\n"
    "One word only."
)


def build_moderator(state):
    """Fresh moderator adapter. NOT a seat: no roster entry, no queue, no
    fan-out, invisible to the seats — and stateless (its session id is reset
    after every call), which sidesteps the entire dead-session-id fatal class
    and makes resume trivially correct."""
    spec = state.get("moderator") or {}
    provider = spec.get("provider") or "claude"
    model = spec.get("model") or ("claude-haiku-4-5" if provider == "claude"
                                  else None)
    effort = spec.get("effort") or ("low" if provider == "claude" else None)
    return AGENT_TYPES[provider](state["workspace"], yolo=False,
                                 model=model, effort=effort, name="Moderator")


def moderator_pick(state, io, moderator):
    """(seat index or None, done). One cheap stateless CLI call.

    Any failure — CLI error, timeout, unparseable or ambiguous output —
    returns (None, False): the loop falls back to listed order. The moderator
    is auxiliary; a broken moderator must never kill the conversation, so
    nothing here raises and nothing is retried (the fallback is free)."""
    agents = state["agents"]
    rows = [r for r in read_messages(state["store"].dir)
            if r.get("speaker") != "system" and r.get("text")]
    counts = {a.name: 0 for a in agents}
    for r in rows:
        if r.get("name") in counts:
            counts[r["name"]] += 1
    tail = "\n".join(f"{r.get('name')}: {r['text'][:400]}"
                     for r in rows[-2 * len(agents):])
    roster = ", ".join(a.name + (f" (role: {a.role})" if a.role else "")
                       for a in agents)
    topic = (state.get("topic") or "").strip()
    prompt = MODERATOR_PROMPT.format(
        roster=roster,
        topic_line=f"Topic: {topic}\n" if topic else "",
        counts=", ".join(f"{k} {v}" for k, v in counts.items()),
        tail=tail or "(no messages yet)",
        names=", ".join(a.name for a in agents))
    try:
        reply = moderator.turn(prompt)
    except Exception as e:
        io.emit("status", {"text": f"Moderator error ({str(e)[:120]}) — "
                                   f"continuing in order"})
        return None, False
    finally:
        moderator.session_id = None     # stateless by design
    pick = (reply or "").strip().strip('."\'`*: ')
    if pick.upper() == "DONE":
        return None, True
    idxs = match_seats(agents, pick)
    if len(idxs) == 1:
        io.emit("status", {"text": f"Moderator: {agents[idxs[0]].name} "
                                   f"speaks next"})
        return idxs[0], False
    # lenient pass: exactly one seat label appearing in the reply
    low = (reply or "").lower()
    found = [k for k, a in enumerate(agents) if a.name.lower() in low]
    if len(found) == 1:
        io.emit("status", {"text": f"Moderator: {agents[found[0]].name} "
                                   f"speaks next"})
        return found[0], False
    io.emit("status", {"text": f"Moderator reply unusable "
                               f"({(reply or '')[:80]!r}) — continuing "
                               f"in order"})
    return None, False


def run_rounds(state, io):
    """The one conversation loop both front ends run.

    Per-turn scheduler: each iteration picks ONE seat (closing list, then
    mode-specific picks, then the round-robin cursor), composes its prompt
    commit-consume style, runs the turn with the fatal/retry/empty ladder,
    and commits. Lap accounting reproduces the old nested round loop exactly
    for round_robin: attempting the seat at list position 0 is the lap
    boundary where the round cap is checked and `rnd` increments.

    `io` is the front-end seam (LoopIO). No epilogue happens in here — the
    CLI's ended footer and the app's paused footer + `done` event stay with
    their owners. KeyboardInterrupt propagates (the CLI catches it as before).

    Returns how the run ended: 'cap' | 'wrapped' | 'stopped' | 'fatal'.
    """
    if state.get("mode") == "parallel":
        return run_parallel(state, io)
    if state.get("mode") == "free":
        return run_free(state, io)
    agents, log, store = state["agents"], state["log"], state["store"]
    slot_ids, providers = state["slot_ids"], state["providers"]
    pending = state["pending"]
    state.setdefault("mode", DEFAULT_MODE)
    state.setdefault("turn", 0)
    state.setdefault("next_speaker", None)
    state.setdefault("closing", None)
    moderator = None                 # built lazily on the first pick
    mgr = SpawnManager(state, io)
    mgr.announce_lost_helpers()
    outcome = "cap"
    while True:
        if io.should_stop():
            outcome = "stopped"
            break
        stopped = False
        for h in io.drain_human():
            if h.startswith("/"):
                if dispatch_command(state, h, io):
                    stopped = True
                continue
            row = log("Josh (human)", h)
            io.emit("message", row)
            for j in range(len(agents)):
                pending[j].append(f"Josh (human) interjects: {h}")
            store.save(state)
        mgr.drain_into_pending()
        if stopped:
            outcome = "stopped"
            break
        # Turn boundary: app-staged role changes land here, so the seat about
        # to speak gets a fresh preamble with the new role rather than
        # switching identity halfway through a turn.
        io.on_turn_boundary(state)

        # Until-done: no round cap; the hard turn ceiling is the spend
        # backstop. Closing turns are exempt (a wrap in flight finishes;
        # bounded by seat count anyway).
        if state.get("until_done") and state["closing"] is None:
            ceiling = state.get("turn_ceiling") or DEFAULT_CEILING
            if state["turn"] >= ceiling:
                note = (f"Safety ceiling reached ({ceiling} turns) without a "
                        f"wrap — pausing. Continue the chat to extend the "
                        f"ceiling, or /stop for good.")
                io.emit("status", {"text": note})
                store.system(note, round=state["rnd"])
                store.save(state)
                break                       # outcome stays "cap"

        i, source = choose_next_seat(state)
        if i is None:
            io.emit("status", {"text": "Conversation wrapped."})
            outcome = "wrapped"
            break
        dynamic = state["mode"] in ("speaker", "moderator")
        if dynamic:
            # per-turn budget: the rounds knob means ≈ conversation length,
            # enforced as turns × seats. Closing turns are exempt — once a
            # wrap is in flight, the last words get to finish (bounded by
            # seat count anyway). rnd becomes the lap counter for captions.
            if state["closing"] is None and not state.get("until_done") and \
                    state["turn"] >= state["max"] * len(agents):
                break                       # outcome stays "cap"
            state["rnd"] = 1 + state["turn"] // len(agents)
            if (source == "cursor" and state["mode"] == "moderator"
                    and state["closing"] is None and state["turn"] > 0
                    and not state.get("_mod_disabled")):
                if moderator is None:
                    moderator = build_moderator(state)
                m_idx, done = moderator_pick(state, io, moderator)
                if done:
                    state["closing"] = [slot_ids[k] for k in range(len(agents))
                                        if state["introduced"][k]]
                    note = ("The moderator called the conversation done — "
                            "closing remarks…")
                    io.emit("status", {"text": note})
                    store.system(note, round=state["rnd"])
                    store.save(state)
                    continue
                if m_idx is not None:
                    i, source = m_idx, "moderator"
                    state["_mod_failures"] = 0
                else:
                    fails = state.get("_mod_failures", 0) + 1
                    state["_mod_failures"] = fails
                    if fails >= 3:
                        state["_mod_disabled"] = True
                        store.system("Moderator is failing — continuing in "
                                     "round-robin order.", round=state["rnd"])
        else:
            if i == 0:
                # lap boundary: seats[0] (--start seat) beginning a new pass
                if not state.get("until_done") and \
                        state["rnd"] >= state["max"]:
                    break                   # outcome stays "cap"
                state["rnd"] += 1
        if source == "closing":
            # consumed AFTER the cap check — popped before the attempt, so a
            # closing seat that fails its turn loses its slot (deliberately:
            # the old countdown gave it a whole second lap instead)
            state["closing"].pop(0)
        elif source == "next":
            state["next_speaker"] = None    # consumed by this attempt
        rnd = state["rnd"]
        agent = agents[i]

        message, consumed, first_turn = compose_prompt(state, i)
        key = slot_ids[i]
        io.emit("thinking", {"speaker": key, "provider": providers[i],
                             "name": agent.name, "round": rnd,
                             "turns": state["max"],
                             "turn": state["turn"] + 1,
                             "until_done": bool(state.get("until_done")),
                             "ceiling": state.get("turn_ceiling")})
        try:
            reply = agent.turn(message)
        except Exception as e1:
            fatal = fatal_seat_error(agent, e1)
            if fatal:
                # no retry, don't hit the same wall every round — and the
                # cursor stays ON this seat so a resume retries it (after a
                # /clear it gets a fresh session and its still-owed queue)
                commit_skip(state, i, fatal, io, fatal=True)
                outcome = "fatal"
                break
            io.emit("status", {"text": f"{agent.name} error "
                                       f"({str(e1)[:200]}) — retrying once…"})
            try:
                reply = agent.turn(message)
            except Exception as e2:
                if source != "closing":
                    state["cursor"] = slot_ids[(i + 1) % len(agents)]
                commit_skip(state, i,
                            f"{agent.name} failed twice; skipping this "
                            f"round. ({str(e2)[:200]})", io)
                continue
        finally:
            io.emit("thinking_done", {"speaker": key})

        # Never forge a turn. "(no reply)" used to be relayed to the other
        # seats as if the agent had said it, which hid a hard failure for a
        # whole conversation. Adapters raise on empty; this is the backstop.
        if not (reply or "").strip():
            if source != "closing":
                state["cursor"] = slot_ids[(i + 1) % len(agents)]
            commit_skip(state, i,
                        f"{agent.name} returned an empty reply; skipping "
                        f"this round (nothing sent to the others).", io)
            continue

        wrapped_now = state["closing"] is None and wrap_called(reply)
        if wrapped_now:
            start_closing(state, i)
        elif state["mode"] == "speaker" and state["closing"] is None:
            set_next_speaker(state, i, reply, io)
        if source != "closing":
            # the cursor is the fallback order in every mode: after seat i,
            # listed order resumes from i+1 whenever nothing overrides it
            state["cursor"] = slot_ids[(i + 1) % len(agents)]
        commit_reply(state, i, reply, consumed, io)
        handle_spawn_directives(state, i, reply, io, mgr)
        if wrapped_now:
            io.emit("status", {"text": f"{agent.name} called it — "
                                       f"closing remarks…"})
    mgr.finish()
    if outcome == "cap" and io.should_stop():
        outcome = "stopped"
    return outcome


def run_parallel(state, io):
    """Simultaneous rounds: every seat answers the same backlog at once, and
    all replies are shared as the round completes — replies to what a seat
    says now reach it next round.

    The parallel contract (ORCHESTRATION_DESIGN.md):
    - `state["lock"]` guards pending/introduced/turn and every store.save
      while seat threads are alive. Lock order: state["lock"] -> store._lock,
      never the reverse.
    - One daemon thread per seat per round — exactly one thread ever touches
      a given Agent (session_id capture and codex -o files stay single-owner).
    - Commit-consume: every prompt is composed BEFORE any thread starts and
      nothing is removed from a queue until that seat's own commit, so
      store.save is valid at every instant and a crash loses at most the
      in-flight turns. Messages record/emit in ARRIVAL order — live UI,
      transcript, and replay all agree.
    - /clear and /compact are deferred to the next round boundary: they run a
      CLI turn of their own and the target seat may be mid-flight right now.
    - Wrap: if every seat wraps in the same round, stop; otherwise everyone
      who didn't wrap gets one more simultaneous round (persisted via
      `closing`, consumed per-commit so a crash mid-closing-round resumes
      with only the seats still owed their last word).
    """
    agents, log, store = state["agents"], state["log"], state["store"]
    slot_ids, providers = state["slot_ids"], state["providers"]
    pending = state["pending"]
    state.setdefault("turn", 0)
    state.setdefault("next_speaker", None)
    state.setdefault("closing", None)
    lock = state.setdefault("lock", threading.RLock())
    deferred = []                    # /clear//compact queued mid-round
    mgr = SpawnManager(state, io)
    mgr.announce_lost_helpers()
    outcome = "cap"

    def drain(during_round):
        """Handle human input; True = /stop. Mid-round, seat commands defer."""
        stop = False
        for h in io.drain_human():
            if h.startswith("/"):
                head = h[1:].split()[0].lower() if len(h) > 1 else ""
                if during_round and head in ("clear", "compact"):
                    deferred.append(h)
                    io.emit("status", {"text": f"{h} queued — runs after "
                                               f"this round"})
                    continue
                with lock:
                    if dispatch_command(state, h, io):
                        stop = True
                continue
            with lock:
                row = log("Josh (human)", h)
                io.emit("message", row)
                for j in range(len(agents)):
                    pending[j].append(f"Josh (human) interjects: {h}")
                store.save(state)
        return stop

    while True:
        if io.should_stop():
            outcome = "stopped"
            break
        stopped = False
        while deferred and not stopped:
            with lock:
                if dispatch_command(state, deferred.pop(0), io):
                    stopped = True
        if not stopped:
            stopped = drain(during_round=False)
        with lock:
            mgr.drain_into_pending()
        if stopped:
            outcome = "stopped"
            break
        io.on_turn_boundary(state)

        closing_round = state["closing"] is not None
        if closing_round and not state["closing"]:
            io.emit("status", {"text": "Conversation wrapped."})
            outcome = "wrapped"
            break
        if not closing_round:
            if state.get("until_done"):
                ceiling = state.get("turn_ceiling") or DEFAULT_CEILING
                if state["turn"] >= ceiling:
                    note = (f"Safety ceiling reached ({ceiling} turns) "
                            f"without a wrap — pausing. Continue the chat to "
                            f"extend the ceiling, or /stop for good.")
                    io.emit("status", {"text": note})
                    store.system(note, round=state["rnd"])
                    store.save(state)
                    break               # outcome stays "cap"
            elif state["rnd"] >= state["max"]:
                break                   # outcome stays "cap"
        state["rnd"] += 1
        rnd = state["rnd"]

        if closing_round:
            roster = [k for k in (slot_index(state, sid)
                                  for sid in list(state["closing"]))
                      if k is not None]
        else:
            roster = list(range(len(agents)))

        # compose everything BEFORE any thread runs
        prompts = {i: compose_prompt(state, i) for i in roster}
        for i in roster:
            io.emit("thinking", {"speaker": slot_ids[i],
                                 "provider": providers[i],
                                 "name": agents[i].name, "round": rnd,
                                 "turns": state["max"],
                                 "turn": state["turn"] + 1,
                                 "until_done": bool(state.get("until_done")),
                                 "ceiling": state.get("turn_ceiling")})

        results = {}

        def seat_task(i):
            agent = agents[i]
            message, consumed, _first = prompts[i]
            key = slot_ids[i]

            def consume_closing_slot():
                if closing_round and key in (state["closing"] or []):
                    state["closing"].remove(key)

            try:
                try:
                    reply = agent.turn(message)
                except Exception as e1:
                    fatal = fatal_seat_error(agent, e1)
                    if fatal:
                        with lock:
                            consume_closing_slot()
                            commit_skip(state, i, fatal, io, fatal=True)
                        results[i] = "fatal"
                        return
                    io.emit("status", {"text": f"{agent.name} error "
                                               f"({str(e1)[:200]}) — "
                                               f"retrying once…"})
                    try:
                        reply = agent.turn(message)
                    except Exception as e2:
                        with lock:
                            consume_closing_slot()
                            commit_skip(state, i,
                                        f"{agent.name} failed twice; "
                                        f"skipping this round. "
                                        f"({str(e2)[:200]})", io)
                        results[i] = "skip"
                        return
                if not (reply or "").strip():
                    with lock:
                        consume_closing_slot()
                        commit_skip(state, i,
                                    f"{agent.name} returned an empty reply; "
                                    f"skipping this round (nothing sent to "
                                    f"the others).", io)
                    results[i] = "skip"
                    return
                try:
                    with lock:
                        consume_closing_slot()
                        commit_reply(state, i, reply, consumed, io)
                        handle_spawn_directives(state, i, reply, io, mgr)
                except Exception as e3:
                    # a commit failure (disk full, meta unwritable even after
                    # the atomic-write retries) must stop the run VISIBLY —
                    # a silently dead thread would just look like a hang
                    results[i] = "fatal"
                    io.emit("agent_error", {
                        "speaker": key, "provider": providers[i],
                        "fatal": True,
                        "message": f"{agent.name}: failed to record its "
                                   f"reply ({str(e3)[:200]}) — stopping."})
                    return
                results[i] = "wrap" if wrap_called(reply) else "ok"
            finally:
                io.emit("thinking_done", {"speaker": key})

        threads = [threading.Thread(target=seat_task, args=(i,), daemon=True)
                   for i in roster]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            if drain(during_round=True):
                stopped = True          # takes effect at the barrier
            for t in threads:
                t.join(timeout=0.25)

        if closing_round:
            io.emit("status", {"text": "Conversation wrapped."})
            outcome = "wrapped"
            break
        if any(r == "fatal" for r in results.values()):
            outcome = "fatal"
            break
        if stopped or io.should_stop():
            outcome = "stopped"
            break
        wrappers = [i for i, r in results.items() if r == "wrap"]
        if wrappers:
            others = [slot_ids[i] for i in roster if results.get(i) != "wrap"]
            if not others:
                io.emit("status", {"text": "Conversation wrapped."})
                outcome = "wrapped"
                break
            with lock:
                state["closing"] = others
                store.save(state)
            names = ", ".join(agents[i].name for i in wrappers)
            io.emit("status", {"text": f"{names} called it — one closing "
                                       f"round…"})
    mgr.finish()
    return outcome


def run_free(state, io):
    """Free-running mode: each seat replies whenever new messages arrive for
    it, independently — turns interleave in real time.

    Structure: one long-lived daemon thread per seat plus the calling thread
    as coordinator (human input at 0.25s, stop conditions). The parallel
    contract applies (state["lock"]/cond guard everything shared; one thread
    per Agent for the whole run; commit-consume). Extra rules:
    - Budget = turns × seats total turns, same as speaker/moderator; `rnd` is
      the lap counter 1 + turn//seats, so captions/continue-math stay uniform
      (laps are approximate here by nature).
    - Fairness: FREE_MAX_LEAD — a seat may not start a turn while ≥2 turns
      ahead of the slowest live seat.
    - Wrap: the wrapper stops; every other live seat gets exactly ONE more
      turn (its backlog contains the wrap by construction), then done. A seat
      mid-turn when the wrap lands commits normally and still gets its word.
    - A seat is PARKED after 3 consecutive double-failures (its queue keeps
      accumulating for a /clear revival on a later continue); fewer than two
      live seats stops the run.
    - /clear and /compact run on the OWNING seat's thread via its inbox
      (compact is a CLI turn — no other thread may touch the Agent). Role
      staging is NOT drained mid-run here; it applies when the run pauses.
    """
    agents, log, store = state["agents"], state["log"], state["store"]
    slot_ids, providers = state["slot_ids"], state["providers"]
    pending = state["pending"]
    state.setdefault("turn", 0)
    state.setdefault("next_speaker", None)
    state.setdefault("closing", None)
    lock = state.setdefault("lock", threading.RLock())
    cond = threading.Condition(lock)
    n = len(agents)
    if state["turn"] == 0 and state["rnd"] == 0:
        state["rnd"] = 1         # lap 1 from the first beat (opener nudges)
    taken = [0] * n              # per-seat commits (this process; fairness)
    busy = [False] * n           # seat currently composing/turning
    parked = [False] * n
    inbox = {i: [] for i in range(n)}    # deferred /clear//compact jobs
    flow = {"stop": False, "outcome": None}
    mgr = SpawnManager(state, io)
    with cond:
        mgr.announce_lost_helpers()

    def budget_left():
        if state.get("until_done"):
            return state["turn"] < (state.get("turn_ceiling")
                                    or DEFAULT_CEILING)
        return state["turn"] < state["max"] * n

    def throttled(i):
        floor = min((taken[k] for k in range(n)
                     if not parked[k]), default=taken[i])
        return taken[i] - floor >= FREE_MAX_LEAD

    def stop_all(outcome):
        with cond:
            flow["stop"] = True
            if flow["outcome"] is None:
                flow["outcome"] = outcome
            cond.notify_all()

    def seat_loop(i):
        agent = agents[i]
        key = slot_ids[i]
        fails = 0
        while True:
            job = None
            with cond:
                busy[i] = False
                while job is None:
                    if flow["stop"]:
                        return
                    if inbox[i]:
                        job = inbox[i].pop(0)
                        break
                    closing = state["closing"]
                    if closing is not None:
                        if key in closing:
                            closing.remove(key)   # consume BEFORE the attempt
                            job = "turn"
                            break
                        return                    # said my piece — done
                    # an un-introduced seat with an empty queue is the
                    # no-opener opening beat: every seat opens at once
                    if (pending[i] or not state["introduced"][i]) \
                            and budget_left() and not throttled(i):
                        job = "turn"
                        break
                    cond.wait(timeout=0.5)
                busy[i] = True
                if job == "turn":
                    message, consumed, _first = compose_prompt(state, i)
                    turn_no = state["turn"] + 1
                    lap = 1 + state["turn"] // n

            if job == "clear":
                with cond:
                    agent.session_id = None
                    state["introduced"][i] = False
                    pending[i].insert(0, CLEAR_NOTE)
                    io.emit("status", {"text": f"{agent.name}'s context "
                                               f"cleared."})
                    store.system(f"{agent.name}'s context was cleared.",
                                 round=state["rnd"])
                    store.save(state)
                    cond.notify_all()
                continue
            if job == "compact":
                io.emit("status", {"text": f"Compacting {agent.name}'s "
                                           f"context…"})
                try:
                    summary = compact_agent(agent)   # my thread owns the Agent
                except Exception as e:
                    with cond:
                        note = (f"{agent.name} compact failed: "
                                f"{str(e)[:200]}")
                        io.emit("status", {"text": note})
                        store.system(note, round=state["rnd"])
                    continue
                with cond:
                    state["introduced"][i] = False
                    pending[i].insert(0, "(Josh compacted your context. "
                                         "Your own summary of the "
                                         "conversation so far:)\n\n" + summary)
                    log(agent.name, summary,
                        meta="context compacted — self-summary")
                    note = f"{agent.name}'s context compacted."
                    io.emit("status", {"text": note})
                    store.system(note, round=state["rnd"])
                    store.save(state)
                    cond.notify_all()
                continue

            io.emit("thinking", {"speaker": key, "provider": providers[i],
                                 "name": agent.name, "round": lap,
                                 "turns": state["max"], "turn": turn_no,
                                 "until_done": bool(state.get("until_done")),
                                 "ceiling": state.get("turn_ceiling")})
            reply = None
            try:
                try:
                    reply = agent.turn(message)
                except Exception as e1:
                    fatal = fatal_seat_error(agent, e1)
                    if fatal:
                        with cond:
                            commit_skip(state, i, fatal, io, fatal=True)
                        stop_all("fatal")
                        return
                    io.emit("status", {"text": f"{agent.name} error "
                                               f"({str(e1)[:200]}) — "
                                               f"retrying once…"})
                    try:
                        reply = agent.turn(message)
                    except Exception as e2:
                        with cond:
                            commit_skip(state, i,
                                        f"{agent.name} failed twice; "
                                        f"skipping. ({str(e2)[:200]})", io)
                        reply = None
                        fails += 1
                if reply is not None and not (reply or "").strip():
                    with cond:
                        commit_skip(state, i,
                                    f"{agent.name} returned an empty reply; "
                                    f"skipping (nothing sent to the "
                                    f"others).", io)
                    reply = None
                    fails += 1
            finally:
                io.emit("thinking_done", {"speaker": key})

            if reply is None:                # a skip: park or back off
                if fails >= 3:
                    with cond:
                        parked[i] = True
                        note = (f"{agent.name} keeps failing — parked for "
                                f"this run (its queue keeps accumulating; "
                                f"/clear {agent.name.lower()} on a later "
                                f"continue revives it).")
                        io.emit("agent_error", {"speaker": key,
                                                "provider": providers[i],
                                                "message": note})
                        store.system(note, round=state["rnd"])
                        store.save(state)
                        cond.notify_all()
                    return
                with cond:                   # wait for new content or backoff
                    busy[i] = False          # a backoff is idle, not working
                    baseline = len(pending[i])
                    deadline = time.time() + FREE_RETRY_BACKOFF
                    while (not flow["stop"]
                           and len(pending[i]) == baseline
                           and time.time() < deadline):
                        cond.wait(timeout=0.25)
                continue

            with cond:
                fails = 0
                state["rnd"] = 1 + state["turn"] // n    # lap for captions
                commit_reply(state, i, reply, consumed, io)
                handle_spawn_directives(state, i, reply, io, mgr)
                taken[i] += 1
                wrapped_now = (state["closing"] is None
                               and wrap_called(reply))
                if wrapped_now:
                    state["closing"] = [slot_ids[k] for k in range(n)
                                        if k != i and not parked[k]]
                    store.save(state)
                    io.emit("status", {"text": f"{agent.name} called it — "
                                               f"the others each get one "
                                               f"more turn…"})
                cond.notify_all()
            if wrapped_now:
                return                       # the wrapper has said goodbye

    def handle_command(h):
        head = h[1:].split()[0].lower() if len(h) > 1 else ""
        if head in ("clear", "compact"):
            arg = h.partition(" ")[2].strip()
            with cond:
                state["log"]("Josh (human)", h, meta="command")
                idxs = match_seats(agents, arg)
                if not idxs:
                    note = f"No seat matches '{arg}'. {HELP_TEXT}"
                    io.emit("status", {"text": note})
                    store.system(note, round=state["rnd"])
                    store.save(state)
                    return False
                for k in idxs:
                    inbox[k].append(head)
                names = ", ".join(agents[k].name for k in idxs)
                io.emit("status", {"text": f"/{head} queued — {names} "
                                           f"applies it before its next "
                                           f"turn."})
                cond.notify_all()
            return False
        with cond:
            stop = dispatch_command(state, h, io)
            cond.notify_all()                # /turns//ceiling may unblock
        return stop

    threads = [threading.Thread(target=seat_loop, args=(i,), daemon=True)
               for i in range(n)]
    for t in threads:
        t.start()
    while any(t.is_alive() for t in threads):
        for h in io.drain_human():
            if h.startswith("/"):
                if handle_command(h):
                    stop_all("stopped")
                continue
            with cond:
                row = log("Josh (human)", h)
                io.emit("message", row)
                for j in range(n):
                    pending[j].append(f"Josh (human) interjects: {h}")
                store.save(state)
                cond.notify_all()
        if io.should_stop():
            stop_all("stopped")
        with cond:
            if mgr.drain_into_pending():
                cond.notify_all()        # a delivery may unblock a seat
            live = sum(1 for k in range(n) if not parked[k])
            if live < 2 and state["closing"] is None and not flow["stop"]:
                note = "Fewer than two live seats — pausing."
                io.emit("status", {"text": note})
                store.system(note, round=state["rnd"])
                store.save(state)
                stop_all("fatal")
            if (state["closing"] is None and not flow["stop"]
                    and not budget_left() and not any(busy)
                    and not any(inbox[k] for k in range(n))):
                if state.get("until_done"):
                    ceiling = (state.get("turn_ceiling") or DEFAULT_CEILING)
                    note = (f"Safety ceiling reached ({ceiling} turns) "
                            f"without a wrap — pausing. Continue the chat "
                            f"to extend the ceiling, or /stop for good.")
                    io.emit("status", {"text": note})
                    store.system(note, round=state["rnd"])
                    store.save(state)
                stop_all("cap")
        for t in threads:
            t.join(timeout=0.25)
    with cond:
        mgr.finish()
    if flow["outcome"] is None:
        flow["outcome"] = "wrapped" if state["closing"] is not None else "cap"
    return flow["outcome"]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(prog="ai-chat", description="AI-to-AI chat relay")
    ap.add_argument("topic", help="what they should talk about")
    ap.add_argument("--turns", type=int, default=10,
                    help="max rounds; each round = every agent speaks once (default 10)")
    ap.add_argument("--agents", default="claude,gpt,gemini",
                    help="comma list of provider[:model[:effort]][=label] tokens; "
                         "providers: claude, gpt, gemini, repeatable for "
                         "duplicate seats, e.g. claude:opus:high,claude:haiku:low "
                         "or \"claude=Optimist,claude=Skeptic\" (default all three)")
    ap.add_argument("--start", default=None,
                    help="who speaks first: slot number (1-based), label "
                         "(e.g. \"claude 2\"), or provider name")
    ap.add_argument("--yolo", action="store_true",
                    help="full autonomy incl. shell access (use with care)")
    ap.add_argument("--mode", default=DEFAULT_MODE.replace("_", "-"),
                    choices=[m.replace("_", "-") for m in MODES],
                    help="turn-taking mode (default round-robin); other "
                         "modes land feature by feature")
    ap.add_argument("--moderator", default=None,
                    metavar="provider[:model[:effort]]",
                    help="who moderates in --mode moderator (default "
                         "claude:claude-haiku-4-5:low); the moderator is not "
                         "a seat — one cheap stateless call per turn")
    ap.add_argument("--no-native-subagents", action="store_true",
                    help="don't tell seats they may use their CLI's built-in "
                         "subagent tools (tier-1 spawning is on by default)")
    ap.add_argument("--spawn-helpers", type=int, default=0, metavar="N",
                    help="let seats spawn up to N one-shot helper AIs via "
                         "[[SPAWN: provider | task]] (default 0 = off; "
                         "each helper is a real CLI call)")
    ap.add_argument("--spawn-teams", type=int, default=0, metavar="N",
                    help="let seats spawn up to N sub-conversations via "
                         "[[TEAM: seats | task]] (default 0 = off; a team "
                         "is MANY real CLI calls)")
    ap.add_argument("--until-done", action="store_true",
                    help="no round cap: run until a seat wraps (or the "
                         "moderator calls DONE), bounded by --ceiling")
    ap.add_argument("--ceiling", type=int, default=DEFAULT_CEILING,
                    help=f"until-done safety ceiling: hard stop after N "
                         f"total turns (default {DEFAULT_CEILING})")
    ap.add_argument("--claude-model", default=None,
                    help="e.g. claude-fable-5, claude-opus-5, claude-opus-4-8, "
                         "claude-sonnet-5, claude-haiku-4-5, or aliases "
                         "opus/sonnet/haiku (default: CLI default, Opus 4.8)")
    ap.add_argument("--claude-effort", default=None,
                    help="low|medium|high|xhigh|max (default: CLI default)")
    ap.add_argument("--gpt-model", default=None,
                    help="e.g. gpt-5.6-sol (default: ~/.codex/config.toml)")
    ap.add_argument("--gpt-effort", default=None,
                    help="low|medium|high|xhigh|max|ultra, model-dependent "
                         "(default: config.toml)")
    ap.add_argument("--gemini-model", default="gemini-3.7-flash-high",
                    help="agy model slug, see `agy models` (default gemini-3.7-flash-high)")
    ap.add_argument("--gemini-effort", default=None,
                    help="low|medium|high (default: baked into the model slug)")
    ap.add_argument("--role", action="append", default=[],
                    metavar='"SEAT=NAME"',
                    help="public role name for a seat, visible to every seat "
                         "in the roster line; SEAT is a label (\"claude 2\") "
                         "or a provider (all its seats); repeatable; a SEAT "
                         "that matches nothing is an error")
    ap.add_argument("--role-instructions", action="append", default=[],
                    metavar='"SEAT=TEXT"',
                    help="private role instructions only that seat sees; "
                         "same SEAT grammar as --role; repeatable")
    args = ap.parse_args()

    mode = args.mode.replace("-", "_")
    if mode not in IMPLEMENTED_MODES:
        ok = ", ".join(m.replace("_", "-") for m in IMPLEMENTED_MODES)
        sys.exit(f"--mode {args.mode} isn't available yet (implemented: {ok})")
    moderator_spec = None
    if args.moderator:
        mp, mm, me, mlabel = parse_agent_token(args.moderator)
        if mlabel or mp not in AGENT_TYPES:
            sys.exit(f"--moderator needs provider[:model[:effort]] with a "
                     f"provider from {sorted(AGENT_TYPES)} "
                     f"(got {args.moderator!r})")
        moderator_spec = {"provider": mp, "model": mm, "effort": me}
        if mode != "moderator":
            print(f"{DIM}note: --moderator is ignored outside "
                  f"--mode moderator{RESET}")
    if args.until_done and args.turns != 10:
        print(f"{DIM}note: --turns is ignored with --until-done "
              f"(the --ceiling bounds the run){RESET}")

    slots = [parse_agent_token(t) for t in args.agents.split(",") if t.strip()]
    unknown = sorted({p for p, _, _, _ in slots if p not in AGENT_TYPES})
    if unknown or len(slots) < 2:
        sys.exit(f"--agents needs 2+ tokens provider[:model[:effort]][=label] "
                 f"with providers from {sorted(AGENT_TYPES)} (got {args.agents!r})")
    agy_fallback = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                "agy", "bin", "agy.exe")
    for p in sorted({p for p, _, _, _ in slots}):
        cli = AGENT_TYPES[p].cli
        if not shutil.which(cli) and not (cli == "agy" and os.path.exists(agy_fallback)):
            sys.exit(f"'{cli}' CLI not found on PATH (agent '{p}')")

    # per-token model/effort win; the provider-wide flags fill the gaps
    tuning = {
        "claude": (args.claude_model, args.claude_effort),
        "gpt": (args.gpt_model, args.gpt_effort),
        "gemini": (args.gemini_model, args.gemini_effort),
    }
    try:
        labels = assign_labels([(p, lb) for p, _, _, lb in slots])
    except ValueError as e:
        sys.exit(str(e))
    seats = [(p, m or tuning[p][0], e or tuning[p][1], lb)
             for (p, m, e, _), lb in zip(slots, labels)]

    if args.start:
        s = args.start.strip()
        idx = None
        if s.isdigit() and 1 <= int(s) <= len(seats):
            idx = int(s) - 1  # 1-based slot index
        else:
            low = s.lower()
            idx = next((i for i, seat in enumerate(seats)
                        if seat[3].lower() == low), None)
            if idx is None:  # provider name -> its first seat
                idx = next((i for i, seat in enumerate(seats)
                            if seat[0] == low), None)
        if idx is None:
            valid = [str(i + 1) for i in range(len(seats))] + [s[3] for s in seats]
            sys.exit(f"--start must be a slot number, label, or provider "
                     f"(valid: {', '.join(valid)})")
        seats = seats[idx:] + seats[:idx]

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", args.topic.lower())[:40].strip("-") or "chat"
    session_dir = os.path.join(SESSIONS_DIR, f"{stamp}-{slug}")
    workspace = os.path.join(session_dir, "workspace")
    os.makedirs(workspace, exist_ok=True)
    transcript = os.path.join(session_dir, "transcript.md")
    say_file = os.path.join(session_dir, "say.txt")

    agents = [AGENT_TYPES[p](workspace, yolo=args.yolo,
                             model=m, effort=e, name=lb)
              for p, m, e, lb in seats]
    for a in agents:  # suffixed/custom labels inherit the provider's color
        COLORS.setdefault(a.name, COLORS.get(type(a).name, ""))
    try:  # after assign_labels — role targets resolve against final labels
        apply_role_flags(agents, args.role, "--role")
        apply_role_flags(agents, args.role_instructions, "--role-instructions")
    except ValueError as e:
        sys.exit(str(e))

    store = SessionStore(session_dir)
    store.open_transcript(args.topic, agents, args.turns)

    def tuning_str(a):
        bits = [a.model or "CLI default"] + ([a.effort] if a.effort else []) \
            + ([f"role: {a.role}"] if a.role else [])
        return f"{a.name} ({', '.join(bits)})"

    print(f"{BOLD}ai-chat{RESET} — {args.topic}")
    print(f"{DIM}participants : {' ↔ '.join(tuning_str(a) for a in agents)}")
    if args.until_done:
        print(f"rounds       : until done (ceiling {max(1, args.ceiling)} turns)")
    else:
        print(f"rounds       : up to {args.turns}")
    if mode != DEFAULT_MODE:
        print(f"turn order   : {mode.replace('_', ' ')}")
    print(f"tools        : {'YOLO (unsandboxed)' if args.yolo else 'web + shared workspace (sandboxed)'}")
    print(f"transcript   : {transcript}")
    print(f"interject    : type + Enter anytime · /stop ends · /turns N recaps "
          f"· /clear [seat] · /compact [seat]{RESET}")

    human_q = queue.Queue()
    start_stdin_reader(human_q)

    state = {"agents": agents, "slot_ids": list(range(len(agents))),
             "providers": [p for p, _, _, _ in seats],
             "workspace": workspace, "transcript": transcript,
             "topic": args.topic, "title": args.topic, "created": store.created,
             "yolo": bool(args.yolo), "turns": args.turns,
             "rnd": 0, "max": args.turns, "ended": False, "mode": mode,
             "moderator": moderator_spec,
             "until_done": bool(args.until_done),
             "turn_ceiling": max(1, args.ceiling) if args.until_done else None,
             "spawn": {"tier1": not args.no_native_subagents,
                       "max_helpers": max(0, args.spawn_helpers),
                       "helpers_used": 0,
                       "max_teams": max(0, args.spawn_teams),
                       "teams_used": 0},
             "pending": {i: [] for i in range(len(agents))},
             "introduced": [False] * len(agents), "store": store}

    def echo(row):
        # rows carry the UI name ("Josh"); the console palette is keyed on the
        # transcript's older label, so map it back rather than lose the color.
        # One lock acquisition for banner + body: parallel commits print from
        # seat threads and must not interleave mid-block.
        with _PRINT_LOCK:
            banner("Josh (human)" if row["speaker"] == "josh" else row["name"],
                   " · ".join(x for x in (row.get("role") or "",
                                          row["meta"]) if x))
            print(row["text"])

    state["log"] = make_log(state, store, echo=echo)

    store.save(state)  # a chat exists the moment it starts, not after turn 1

    try:
        run_rounds(state, CLIIO(human_q, say_file))
    except KeyboardInterrupt:
        print(f"\n{DIM}interrupted — transcript saved.{RESET}")

    store.save(state, ended=True)
    with open(transcript, "a", encoding="utf-8") as f:
        f.write("\n---\n*conversation ended*\n")
    print(f"\n{BOLD}Done.{RESET} Transcript: {transcript}")
    print(f"{DIM}reopen in the app: {store.id}{RESET}")


if __name__ == "__main__":
    main()
