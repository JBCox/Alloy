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


def status(msg):
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

    def __init__(self, workspace, yolo=False, model=None, effort=None, name=None):
        self.workspace = workspace
        self.yolo = yolo
        self.model = model
        self.effort = effort
        self.session_id = None
        self.uid = uuid.uuid4().hex[:8]
        if name:  # instance attr shadows the class attr (duplicate-provider seats)
            self.name = name

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
                # swallow the positional prompt that follows it
                "--allowedTools=WebSearch,WebFetch,Read,Write,Edit,Glob,Grep",
            ]
        cmd.append(message)  # positional prompt goes last (required for --resume)
        return cmd

    def parse(self, stdout):
        data = json.loads(stdout.strip().splitlines()[-1])
        # -p --resume forks to a fresh session id each call; track the newest
        self.session_id = data.get("session_id", self.session_id)
        return (data.get("result") or "").strip()


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

def wrap_called(reply):
    """True only when a seat PLAYS the wrap token to close its turn.

    This used to be a bare `WRAP_TOKEN in reply`, which meant a seat that merely
    *mentioned* the token -- e.g. while discussing how wrapping works -- ended the
    conversation by accident.

    The rule is that the token must TERMINATE the reply. Requiring it to be the
    entire last line was too strict in the other direction: seats overwhelmingly
    play it by closing a sentence with it ("Good place to stop. [[WRAP]]"), and
    that form would never have fired -- a silent false negative that leaves the
    wrap mechanic looking implemented while conversations always run to the round
    cap. Ending on the token covers both that and the on-its-own-line form, while
    every mid-reply mention still has text after it. Quoted or code-span mentions
    (`[[WRAP]]`, "[[WRAP]]") end on the closing mark, not the token, so they are
    safe even in the last position.
    """
    return (reply or "").rstrip().endswith(WRAP_TOKEN)


def preamble(agent, others, topic, turns, workspace):
    other_names = " and ".join(a.name for a in others)
    topic_line = f"Topic: {topic}\n\n" if (topic or "").strip() else ""
    dup_note = ""
    if any(type(a) is type(agent) for a in others):
        dup_note = (
            f"Note: one or more of the other participants run on the same "
            f"underlying model as you. They are separate instances with their "
            f"own memory -- not echoes of you. Any relayed message prefixed with "
            f"another name and 'said:' was written by that other instance, never "
            f"by you. Always speak only as {agent.name}. "
        )
    return (
        f"You are {agent.name}, in a live multi-AI conversation with {other_names}. "
        f"{dup_note}"
        f"Messages from the other participant(s) are relayed to you verbatim, prefixed "
        f"with the speaker's name. A human (Josh) set this up and may occasionally "
        f"interject; he is otherwise not involved -- talk to the other AI(s), not to him.\n\n"
        f"{topic_line}"
        f"Ground rules:\n"
        f"- Conversational replies, a few paragraphs at most. No markdown headers.\n"
        f"- You share a scratch workspace (your current directory) with the other "
        f"participant(s) -- you may read/write files there if useful, e.g. to "
        f"co-write a document.\n"
        f"- The conversation runs at most {turns} rounds. If the topic feels "
        f"genuinely exhausted, END a reply with the token {WRAP_TOKEN} to wind "
        f"down -- it must be the very last thing you write. Mentioning it "
        f"anywhere earlier, or in quotes/backticks, does not trigger it.\n"
        f"- Be yourself; disagree freely; build on each other's points.\n"
    )


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
    args = ap.parse_args()

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

    def log(speaker, text, meta=""):
        with open(transcript, "a", encoding="utf-8") as f:
            f.write(f"\n## {speaker}{f'  · {meta}' if meta else ''}\n\n{text}\n")
        banner(speaker, meta)
        print(text)

    with open(transcript, "w", encoding="utf-8") as f:
        f.write(f"# AI Chat — {args.topic}\n\n"
                f"*{datetime.datetime.now():%Y-%m-%d %H:%M} · "
                f"{' ↔ '.join(a.name for a in agents)} · "
                f"max {args.turns} rounds*\n")

    def tuning_str(a):
        bits = [a.model or "CLI default"] + ([a.effort] if a.effort else [])
        return f"{a.name} ({', '.join(bits)})"

    print(f"{BOLD}ai-chat{RESET} — {args.topic}")
    print(f"{DIM}participants : {' ↔ '.join(tuning_str(a) for a in agents)}")
    print(f"rounds       : up to {args.turns}")
    print(f"tools        : {'YOLO (unsandboxed)' if args.yolo else 'web + shared workspace (sandboxed)'}")
    print(f"transcript   : {transcript}")
    print(f"interject    : type + Enter anytime · /stop ends · /turns N recaps "
          f"· /clear [seat] · /compact [seat]{RESET}")

    human_q = queue.Queue()
    start_stdin_reader(human_q)

    pending = {i: [] for i in range(len(agents))}  # unseen messages per agent
    introduced = [False] * len(agents)
    closing_left = None  # once someone says WRAP, others each get one last turn
    max_turns = args.turns
    stopping = False

    try:
        rnd = 0
        while rnd < max_turns and not stopping:
            rnd += 1
            for i, agent in enumerate(agents):
                for h in drain_human_input(human_q, say_file):
                    if h.lower() == "/stop":
                        stopping = True
                        break
                    m = re.match(r"/turns\s+(\d+)", h.lower())
                    if m:
                        max_turns = int(m.group(1))
                        status(f"turn cap is now {max_turns}")
                        continue
                    m = re.match(r"/(clear|compact)(?:\s+(.*))?$", h, re.I)
                    if m:
                        cmd, arg = m.group(1).lower(), (m.group(2) or "").strip()
                        idxs = match_seats(agents, arg)
                        if not idxs:
                            status(f"no seat matches '{arg}'")
                            continue
                        for k in idxs:
                            a2 = agents[k]
                            if cmd == "compact":
                                status(f"compacting {a2.name}'s context…")
                                try:
                                    summary = compact_agent(a2)
                                except Exception as e:
                                    status(f"{a2.name} compact failed: {e}")
                                    continue
                                introduced[k] = False
                                pending[k].insert(0, "(Josh compacted your "
                                                  "context. Your own summary of "
                                                  "the conversation so far:)\n\n"
                                                  + summary)
                                log(a2.name, summary,
                                    meta="context compacted — self-summary")
                            else:
                                a2.session_id = None
                                introduced[k] = False
                                pending[k].insert(0, CLEAR_NOTE)
                                status(f"{a2.name}'s context cleared")
                        continue
                    if h.startswith("/"):
                        status(f"unknown command {h!r} — /stop · /turns N · "
                               f"/clear [seat] · /compact [seat]")
                        continue
                    log("Josh (human)", h)
                    for j in range(len(agents)):
                        pending[j].append(f"Josh (human) interjects: {h}")
                if stopping:
                    break

                parts = []
                first_turn = not introduced[i]
                if first_turn:
                    parts.append(preamble(agent, [a for a in agents if a is not agent],
                                          args.topic, args.turns, workspace))
                    if i == 0 and not pending[i]:
                        parts.append("You open the conversation. Go.")
                queued = pending[i]
                pending[i] = []
                if queued:
                    parts.append("\n\n".join(queued))
                message = "\n\n".join(parts)

                status(f"round {rnd}/{max_turns} · {agent.name} is thinking…")
                try:
                    reply = agent.turn(message)
                except Exception as e:
                    status(f"{agent.name} error: {e}")
                    status("retrying once…")
                    try:
                        reply = agent.turn(message)
                    except Exception as e2:
                        status(f"{agent.name} failed twice — skipping this "
                               f"round ({e2})")
                        # Restore only the queue entries consumed for this turn.
                        # Requeueing `message` would also persist the generated
                        # preamble/opener and recursively embed prior prompts.
                        pending[i] = queued + pending[i]
                        continue

                # Never forge a turn. "(no reply)" used to be relayed to the other
                # seats as if the agent had said it, which hid a hard failure for
                # a whole conversation. Adapters now raise on empty, so this is a
                # backstop: skip visibly and keep the queue rather than invent text.
                if not (reply or "").strip():
                    status(f"{agent.name} returned an empty reply — skipping "
                           f"this round (nothing sent to the others)")
                    pending[i] = queued + pending[i]
                    continue

                if first_turn:
                    introduced[i] = True
                log(agent.name, reply, meta=f"round {rnd}")
                for j, other in enumerate(agents):
                    if other is not agent:
                        pending[j].append(f"{agent.name} said:\n{reply}")

                if closing_left is None and wrap_called(reply):
                    closing_left = len(agents) - 1
                    status(f"{agent.name} called it — closing remarks…")
                elif closing_left is not None:
                    closing_left -= 1
                    if closing_left <= 0:
                        status("conversation wrapped.")
                        stopping = True
                        break
    except KeyboardInterrupt:
        print(f"\n{DIM}interrupted — transcript saved.{RESET}")

    with open(transcript, "a", encoding="utf-8") as f:
        f.write("\n---\n*conversation ended*\n")
    print(f"\n{BOLD}Done.{RESET} Transcript: {transcript}")


if __name__ == "__main__":
    main()
