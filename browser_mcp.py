"""Alloy's browser-control MCP server -- the delivery half of web use.

Alloy writes no browser library and forks nothing. This process is a GATING
PROXY: it spawns Google's `chrome-devtools-mcp` as a child, speaks MCP to it
over stdio, republishes a curated subset of its tools to the seat's own CLI,
and applies Alloy's off/read/ask/full ladder before anything that changes a
page. Chrome is the vendor's problem; the fence is ours.

CONTRACT -- read before changing anything:

* **Everything comes from the environment, nothing from the model.** The rung,
  the allowed sites, the approval channel and the workspace are injected by
  the relay when it spawns this server. A tool argument can never widen what
  this process may do, because the model writes the arguments.

* **Fail closed, at every seam.** No rung, no approval directory, no vendor on
  disk, an unparseable answer, a timeout, a dead child -- all deny.

WHY THE FENCE IS A CHROME FLAG AND NOT OUR OWN CHECK
----------------------------------------------------
`--allowedUrlPattern` is enforced inside Chrome's network stack. It blocks
navigations AND subresources, and it survives `evaluate_script` -- the one
tool that walks straight through any allowlist applied at the tool layer. A
gate we implemented here would be defeated by a single `fetch()`. So the
load-bearing control lives in the fence and nothing load-bearing lives in the
prompt.

FOUR RULES, EACH MEASURED AGAINST chrome-devtools-mcp 1.7.0 (2026-08-26).
Do not rediscover these the hard way:

1. **ALLOWLIST-ONLY, ALWAYS.** `--allowedUrlPattern` and
   `--blockedUrlPattern` are MUTUALLY EXCLUSIVE -- passing both prints
   "Arguments allowedUrlPattern and blockedUrlPattern are mutually exclusive"
   and the server never handshakes at all. An allowlist is also strictly
   stronger: measured against `https://example.com/*` it refused
   `file:///C:/Windows/win.ini`, `http://127.0.0.1:8765/start`, `chrome://version`
   AND `http://lvh.me:8765/start` -- a public, permanent DNS alias for
   127.0.0.1 that no blacklist could ever enumerate. It refuses `lvh.me`
   because it is not listed, not because somebody thought of it. This module
   never emits `--blockedUrlPattern`.

2. **EMPTY MEANS DENY-ALL.** Never `if patterns:`. With nothing configured we
   emit the sentinel `https://alloy.invalid/__never__` -- measured to parse
   and to block `file://`, `https://example.com/` and `lvh.me` alike. An
   OMITTED flag is no fence at all, and a reviewer read `~/.codex/auth.json`
   through exactly that gap.

3. **THE FENCE MUST PROVE ITSELF.** The vendor SILENTLY ACCEPTS UNKNOWN FLAGS:
   passing `--allowedUrlPatterns` (one character, the plural) produced no
   warning, no error, no non-zero exit -- and `file:///C:/Windows/win.ini`
   then navigated successfully. So on the first tool call this proxy navigates
   a scratch page to a URL that MUST be refused, and if it is not refused it
   latches dead and every later call says why. The fence demonstrates itself
   or the capability does not exist.

4. **A URL-POLICY REFUSAL COMES BACK WITH `isError` FALSE**, with the reason in
   the text body ("... is blocked by blocklist/allowlist rules."). Nothing here
   may decide anything from `isError` alone -- in particular an approval note
   must never be stamped onto a call the fence actually refused.

Rungs (`ALLOY_BROWSER_RUNG`):

    off    nothing works; the relay does not even register this server
    read   look, do not touch: navigate, snapshot, screenshot, read the
           console and the network log. No clicking, typing or scripting.
    ask    as `read`, plus interaction -- and every interaction waits for Josh
    full   interaction with no prompt, including unattended

`read` exists because a browser has no equivalent of "look at a window":
reaching a page IS a network request. Navigation is therefore free at every
live rung, bounded by the fence rather than by a prompt, and the ladder gates
what changes a page rather than what reads one.

TWO THINGS ARE WITHHELD STRUCTURALLY, AT EVERY RUNG INCLUDING `full`:

* **Scripting, when loopback is inside the fence.** A dev server is the single
  most valuable browser use case, so a loopback pattern is allowed -- but
  loopback is also where Alloy's own webhook listens, and `evaluate_script`
  carries no URL, so it cannot be gated by origin. When a configured pattern
  can reach loopback, `evaluate_script` is not published at all for that
  conversation and the instructions say so. (`navigate_page`'s `initScript` is
  the same power under another name and is dropped unconditionally.)
* **Every argument that names a path to write.** They are simply not in the
  keep-set, so no published tool can name a path except `upload_file`, whose
  path argument is required and which READS.

  The vendor confines file paths to whatever MCP roots the client negotiated,
  and to the OS temp directory when it negotiated none. The first draft read
  that as "writes only" and left roots unnegotiated -- which made `upload_file`
  a tool that passed Alloy's workspace check, asked Josh, and then ALWAYS
  failed at the vendor with "Access denied: ... not within any of the
  configured workspace roots" (measured against 1.7.0; `input.js` declares
  `verifyFilesSchema: ['filePath']` and `ToolHandler` validates it before it
  even resolves the page). So this proxy DOES negotiate roots, with exactly
  one root: the conversation's working folder. The vendor's boundary and
  Alloy's are then the same boundary, enforced in the process that does the
  I/O -- and with no workspace there are no roots, so the vendor denies every
  path, which is the direction to fail.

HONEST CEILING -- this belongs beside every rung, not in a footnote:
the ladder is an ENFORCING control only while the seat's `permission` is
`read_only` or `ask`. At `auto` and `full` the seat holds a shell, and a shell
can launch its own Chrome with no fence at all, or answer this proxy's own
approval files. There the rung is a guardrail against accident, not against an
adversary. What stays enforcing at every permission level is the Chrome-side
fence, because it lives in a process the seat cannot write to.

Standalone: imports nothing from relay/app/webview, so tests never load the
engine and the import is cheap and total-failure-free off Windows.
"""

import json
import os
import pathlib
import re
import socket
import sys
import tempfile
import time
import uuid

POLL = 0.25
# Same reasoning as desktop_mcp's: this blocks a seat mid-turn whose idle
# watchdog is running, not an idle CLI child. A question Josh has not answered
# in three minutes is one he is not sitting in front of.
APPROVAL_TIMEOUT = 180

RUNGS = ("off", "read", "ask", "full")

# The sentinel that means "deny everything". Measured to parse as a URLPattern
# and to block every scheme tried. `.invalid` is reserved by RFC 2606 and can
# never resolve, so even a fence that somehow leaked would reach nothing.
DENY_ALL_PATTERN = "https://alloy.invalid/__never__"

# The self-test target. A `file:` URL because rule 1 below rejects any pattern
# that could allowlist `file:`, so this can NEVER be reachable while the fence
# is live -- and the path does not exist, so a DEAD fence loads nothing
# sensitive either. Both halves matter.
FENCE_PROBE_URL = "file:///alloy-fence-selftest"

# The vendor's own wording for a fence refusal, measured from a live 1.7.0:
#   "Unable to navigate in the selected page: Navigation to <url> is blocked
#    by blocklist/allowlist rules."
# Matched case-insensitively on the distinctive half. If a vendor bump changes
# this sentence the self-test stops recognising a refusal and latches dead --
# which is the correct direction to fail.
BLOCKED_SIGNATURE = "blocked by blocklist/allowlist rules"

# Schemes a configured pattern may never name. Allowlisting any of them would
# defeat a confinement that exists elsewhere in the app: `file:` walks past
# every workspace check, `chrome:`/`devtools:` reach the browser's own
# internals, and `data:`/`blob:`/`javascript:` let a seat author a page and
# then act on it as if it came from the network.
STRUCTURAL_SCHEMES = ("file", "chrome", "chrome-untrusted", "chrome-extension",
                      "devtools", "view-source", "about", "data", "blob",
                      "javascript", "filesystem", "ws", "wss")

# Hostnames that are loopback by definition or by permanent public DNS. The
# list is a shortcut, not the mechanism -- `_reaches_loopback` also RESOLVES a
# concrete hostname, which is how `lvh.me` and `*.nip.io` are caught without
# anybody having to think of them (the same getaddrinfo rule webhook.py uses
# to prove its own bind address is loopback).
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1",
                  "lvh.me", "nip.io", "sslip.io", "localtest.me")


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def rung():
    value = _env("ALLOY_BROWSER_RUNG", "off").lower()
    return value if value in RUNGS else "off"


# ------------------------------------------------------- the site fence ----

def configured_sites():
    """The raw URLPattern strings Josh configured, as written. May be []."""
    raw = _env("ALLOY_BROWSER_SITES")
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        items = [raw]
    if not isinstance(items, list):
        items = [items]
    return [str(item).strip() for item in items if str(item).strip()]


def _scheme_of(pattern):
    """The scheme a pattern names, lowercased, or "" when it names none."""
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", pattern or "")
    return match.group(1).lower() if match else ""


def _host_of(pattern):
    """The host portion of a pattern, or "" when there is none to read.

    Deliberately crude: this feeds a REFUSAL decision, and anything it cannot
    parse is treated as wildcard-ish by the caller rather than as safe.
    """
    body = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", pattern or "")
    host = body.split("/", 1)[0]
    if host.startswith("["):                       # [::1]:8080
        end = host.find("]")
        return host[:end + 1].lower() if end != -1 else host.lower()
    return host.split(":", 1)[0].lower()


def _resolves_to_loopback(host):
    """True when this hostname EVER resolves to a loopback address.

    Resolution, not string matching -- the whole point. `lvh.me` is a public
    DNS name that resolves to 127.0.0.1, and no list of spellings could keep
    up with the ones people invent. A lookup failure answers True: a name we
    could not check is not a name we may assume is safe.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, ValueError, OSError):
        return True
    for info in infos:
        addr = info[4][0]
        try:
            import ipaddress
            if ipaddress.ip_address(addr.split("%", 1)[0]).is_loopback:
                return True
        except ValueError:
            continue
    return False


def _reaches_loopback(pattern):
    """True when this pattern could reach 127.0.0.1 / ::1."""
    host = _host_of(pattern)
    if not host or "*" in host or "{" in host or "(" in host or ":" in host:
        # A wildcard, a URLPattern group, or something we could not read. Any
        # of those can match a loopback name, so treat them all as reaching it.
        return True
    if host in LOOPBACK_NAMES or any(host.endswith("." + n)
                                     for n in LOOPBACK_NAMES):
        return True
    return _resolves_to_loopback(host)


def classify_sites(patterns=None, webhook_port=None):
    """Split configured patterns into (kept, rejected, loopback_reach).

    `rejected` is a list of (pattern, sentence) and is never silently dropped:
    the caller states every rejection in the server's instructions AND the
    relay clamps the rung, because a fence Josh mis-wrote must not be quietly
    narrowed into one he did not ask for.
    """
    patterns = configured_sites() if patterns is None else list(patterns)
    if webhook_port is None:
        raw = _env("ALLOY_BROWSER_WEBHOOK_PORT")
        webhook_port = int(raw) if raw.isdigit() else None
    kept, rejected, loopback = [], [], False
    for pattern in patterns:
        scheme = _scheme_of(pattern)
        if scheme in STRUCTURAL_SCHEMES:
            rejected.append((pattern, "%s: URLs are never allowlisted -- they "
                                      "walk past confinements the rest of "
                                      "Alloy depends on." % scheme))
            continue
        if not scheme:
            rejected.append((pattern, "a pattern must start with http:// or "
                                      "https:// so its scheme is not itself a "
                                      "wildcard."))
            continue
        if scheme not in ("http", "https"):
            rejected.append((pattern, "only http and https sites can be "
                                      "allowlisted."))
            continue
        port = _port_of(pattern)
        if webhook_port and _reaches_loopback(pattern):
            if port == webhook_port:
                rejected.append((pattern, "that is the port Alloy's own "
                                          "webhook listens on; a browser "
                                          "aimed at Alloy's front door is "
                                          "refused outright."))
                continue
            if port is None:
                # `http://localhost:*/*` is the natural way to write "my dev
                # server, whatever port it took" -- and it INCLUDES the port
                # Alloy's webhook is on. A literal port is the only form that
                # can be checked, so ask for one rather than let a wildcard
                # quietly cover the front door (and, because nothing was
                # rejected, leave the rung unclamped as well).
                rejected.append((pattern, "a loopback pattern has to name one "
                                          "literal port while Alloy's webhook "
                                          "is listening -- a wildcard port "
                                          "would include the webhook's own."))
                continue
        kept.append(pattern)
        if _reaches_loopback(pattern):
            loopback = True
    return kept, rejected, loopback


# What a URLPattern with no port component actually matches. An omitted port
# is the empty string, and a URL normalizes its default port away -- so
# `http://127.0.0.1/*` matches port 80 and NOTHING else, which is why an
# absent port must not be lumped in with a wildcard one.
DEFAULT_PORTS = {"http": 80, "https": 443}


def _port_of(pattern):
    """The single port a pattern can match, or None when it matches many.

    None means "not a literal" -- a wildcard or a URLPattern group -- and the
    caller treats that as covering every port, including Alloy's own webhook.
    An ABSENT port is not that case: it is the scheme's default port, and
    reporting it as a wildcard would refuse `http://127.0.0.1/*` for a webhook
    listening on 8765, which it cannot reach.
    """
    body = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", pattern or "")
    host = body.split("/", 1)[0]
    if host.startswith("["):
        host = host[host.find("]") + 1:]
        tail = host[1:] if host.startswith(":") else ""
    else:
        parts = host.split(":", 1)
        tail = parts[1] if len(parts) == 2 else ""
    if not tail:
        return DEFAULT_PORTS.get(_scheme_of(pattern))
    return int(tail) if tail.isdigit() else None


def fence_patterns(kept):
    """What actually goes on the vendor's command line.

    The one place rule 2 is honoured: an empty keep-list becomes deny-all,
    never an omitted flag.
    """
    return list(kept) if kept else [DENY_ALL_PATTERN]


# ------------------------------------------------------------- the tools ---
# A curated republish. The NAMES and the ARGUMENT KEYS come from this table;
# the descriptions and schema bodies are copied from the vendor's live schema
# at startup, so the prose stays Google's while the fence stays ours. A vendor
# version bump cannot widen the surface -- a new property simply is not here.
#
# `class`: observe  = free at every live rung (reading a page)
#          navigate = free at every live rung (reaching a page; the fence, not
#                     a prompt, is what bounds where)
#          act      = refused at `read`, asks at `ask`, free at `full`

OBSERVE, NAVIGATE, ACT = "observe", "navigate", "act"

PUBLISH = {
    # ---- observing -------------------------------------------------------
    "list_pages":            (OBSERVE, ()),
    "take_snapshot":         (OBSERVE, ("verbose",)),
    "take_screenshot":       (OBSERVE, ("format", "quality", "uid", "fullPage")),
    "list_console_messages": (OBSERVE, ("pageSize", "pageIdx", "types",
                                        "includePreservedMessages",
                                        "serviceWorkerId")),
    "get_console_message":   (OBSERVE, ("msgid",)),
    "list_network_requests": (OBSERVE, ("pageSize", "pageIdx", "resourceTypes",
                                        "includePreservedRequests")),
    "get_network_request":   (OBSERVE, ("reqid",)),
    "wait_for":              (OBSERVE, ("text", "timeout")),
    # ---- reaching a page -------------------------------------------------
    "navigate_page":         (NAVIGATE, ("type", "url", "ignoreCache",
                                         "timeout")),
    "new_page":              (NAVIGATE, ("url", "background",
                                         "isolatedContext", "timeout")),
    "close_page":            (NAVIGATE, ("pageId",)),
    "select_page":           (NAVIGATE, ("pageId", "bringToFront")),
    "resize_page":           (NAVIGATE, ("width", "height")),
    # ---- changing a page -------------------------------------------------
    "click":                 (ACT, ("uid", "dblClick", "includeSnapshot")),
    "hover":                 (ACT, ("uid", "includeSnapshot")),
    "drag":                  (ACT, ("from_uid", "to_uid", "includeSnapshot")),
    "fill":                  (ACT, ("uid", "value", "includeSnapshot")),
    "fill_form":             (ACT, ("elements", "includeSnapshot")),
    "type_text":             (ACT, ("text", "submitKey")),
    "press_key":             (ACT, ("key", "includeSnapshot")),
    "handle_dialog":         (ACT, ("action", "promptText")),
    "upload_file":           (ACT, ("uid", "filePath", "includeSnapshot")),
    "emulate":               (ACT, ("networkConditions", "cpuThrottlingRate",
                                    "geolocation", "userAgent", "colorScheme",
                                    "viewport")),
    "evaluate_script":       (ACT, ("function", "args")),
}

# Vendor tools deliberately NOT republished, and why. Stated rather than
# silently absent: a seat that knows a capability was withheld asks Josh; a
# seat that finds a tool missing invents a workaround.
WITHHELD = {
    "performance_start_trace": "performance tracing is not wired up yet",
    "performance_stop_trace": "performance tracing is not wired up yet",
    "performance_analyze_insight": "performance tracing is not wired up yet",
    "lighthouse_audit": "a full audit run is long enough to look like a hang",
    "take_heapsnapshot": "it exists only to write a large file to disk",
}

# Argument keys dropped from tools that ARE published, and why. Same rule:
# every one of these is a channel Alloy could not see into.
DROPPED_ARGS = {
    ("navigate_page", "initScript"):
        "a script injected at document start is evaluate_script under another "
        "name, so it follows the same rule",
    ("navigate_page", "handleBeforeUnload"):
        "dismissing a page's unsaved-changes guard is a change to that page, "
        "and it was reachable at the look-only rung",
    ("emulate", "extraHttpHeaders"):
        "arbitrary request headers are a credential-injection channel",
    ("evaluate_script", "filePath"): "tools here do not write files",
    ("evaluate_script", "dialogAction"):
        "auto-answering a dialog is an action the approval card would not name",
    ("take_snapshot", "filePath"): "tools here do not write files",
    ("take_screenshot", "filePath"): "tools here do not write files",
    ("get_network_request", "requestFilePath"): "tools here do not write files",
    ("get_network_request", "responseFilePath"): "tools here do not write files",
}

# Tools whose `filePath` argument is READ, and must therefore stay inside the
# conversation's working folder. The vendor's own temp-dir restriction covers
# writes; this covers the one tool that reads.
CONFINED_PATH_ARGS = {"upload_file": "filePath"}


# ------------------------------------------------------------ the vendor ---

def find_vendor():
    """Absolute path to chrome-devtools-mcp's entry script, or "".

    Env first so the relay pins ONE copy (never `npx @latest`: the installed
    1.7.0 already advertises 1.8.0 on every startup, and a surface that moves
    under a curated republish is exactly what the reconciliation gate exists
    to notice). The search is a convenience for a bare `python browser_mcp.py`
    and for `probe()`.
    """
    override = _env("ALLOY_BROWSER_VENDOR")
    if override:
        return override if os.path.isfile(override) else ""
    tail = os.path.join("node_modules", "chrome-devtools-mcp", "build", "src",
                        "bin", "chrome-devtools-mcp.js")
    roots = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(os.path.join(local, "npm-cache", "_npx"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(os.path.join(appdata, "npm"))
    roots.append(os.path.expanduser(os.path.join("~", ".npm", "_npx")))
    for root in roots:
        direct = os.path.join(root, tail)
        if os.path.isfile(direct):
            return direct
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            candidate = os.path.join(root, entry, tail)
            if os.path.isfile(candidate):
                return candidate
    return ""


def find_node():
    """The node executable, honouring an explicit pin."""
    return _env("ALLOY_BROWSER_NODE") or "node"


def vendor_argv(kept=None, headless=None):
    """The exact command line for the vendor child.

    Every switch here is a decision, so each one is named:

    --isolated              a throwaway profile per run. Josh's real Chrome
                            profile is never opened, and nothing a seat logs
                            into survives into the next conversation.
    --usageStatistics false the vendor defaults this TRUE and sends usage data
    --performanceCrux false to Google. In a repo whose first line is "no API
                            keys anywhere", shipping that default silently
                            would be the dishonest option.
    --redactNetworkHeaders  the network tools are free observers here, and a
                            request's headers are the one part of it that
                            carries credentials.
    --allowedUrlPattern     the fence. Always emitted, never with a blocklist,
                            never omitted. See rules 1 and 2 up top.

    NOT passed, deliberately: --allowUnrestrictedPaths. Its absence is what
    keeps the vendor's file-writing tools inside the OS temp directory.
    """
    if kept is None:
        kept, _, _ = classify_sites()
    if headless is None:
        headless = _env("ALLOY_BROWSER_HEADLESS", "1").lower() not in (
            "0", "false", "no", "off")
    argv = [find_vendor(), "--isolated",
            "--usageStatistics", "false",
            "--performanceCrux", "false",
            "--redactNetworkHeaders", "true"]
    if headless:
        argv.append("--headless")
    for pattern in fence_patterns(kept):
        argv += ["--allowedUrlPattern", pattern]
    return argv


def probe():
    """What is missing, in the shape dictation.probe() established.

    Reports WHICH piece is absent rather than handing the UI a dead button.
    """
    vendor = find_vendor()
    out = {"available": False, "vendor": vendor, "node": find_node(),
           "reason": ""}
    if not vendor:
        out["reason"] = ("chrome-devtools-mcp is not installed. Run "
                         "`npx chrome-devtools-mcp@1.7.0 --version` once to "
                         "put it in the npx cache.")
        return out
    try:
        import mcp  # noqa: F401
    except Exception as exc:
        out["reason"] = "the MCP client library is missing (%s)." % exc
        return out
    import shutil
    if not (os.path.isabs(out["node"]) and os.path.isfile(out["node"])
            or shutil.which(out["node"])):
        out["reason"] = "node is not on PATH, so the browser server cannot start."
        return out
    out["available"] = True
    return out


# ------------------------------------------------------------- approvals ---

def ask_josh(action, detail, url):
    """Block until Josh answers, and return True only for an explicit yes.

    Byte-for-byte the wire protocol `approval_hook.py` proved and
    `desktop_mcp.py` reuses -- `<id>.req` in, `<id>.ans` out, everything else
    deny -- but on a THIRD directory. Three separate channels mean three
    separate watchers mean three separate verdicts: a "rest of this turn" Josh
    said to a Bash prompt must never answer a click on a web page, and neither
    must an answer he gave about a desktop window.
    """
    reqdir = _env("ALLOY_BROWSER_APPROVAL_DIR")
    if not reqdir:
        return False, ("Alloy has no approval channel for browser actions in "
                       "this conversation, so this is declined.")
    rid = uuid.uuid4().hex[:12]
    payload = {"id": rid, "kind": "browser",
               "seat": _env("ALLOY_BROWSER_SEAT", "a seat"),
               "action": action, "detail": detail, "url": url,
               "ts": time.time()}
    try:
        os.makedirs(reqdir, exist_ok=True)
        tmp = os.path.join(reqdir, rid + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, os.path.join(reqdir, rid + ".req"))
    except OSError as exc:
        return False, "Alloy could not reach Josh for approval (%s)." % exc

    ansfile = os.path.join(reqdir, rid + ".ans")
    deadline = time.time() + APPROVAL_TIMEOUT
    while time.time() < deadline:
        try:
            with open(ansfile, encoding="utf-8") as fh:
                ans = json.load(fh)
        except (OSError, ValueError):
            time.sleep(POLL)
            continue
        try:
            os.remove(ansfile)
        except OSError:
            pass
        allow = bool(ans.get("allow"))
        return allow, str(ans.get("reason") or
                          ("Josh approved this." if allow
                           else "Josh declined this."))
    return False, ("Josh did not answer within %d minutes, so this is "
                   "declined." % (APPROVAL_TIMEOUT // 60))


ALLOW, REFUSE, ASK = "allow", "refuse", "ask"


def decide(tool, level=None):
    """(verdict, reason) for one call — the ONLY place a rung is interpreted.

    Answers WITHOUT touching the browser, and that is the point: at rung `off`
    Chrome must not be launched merely to explain that we are not allowed to
    open it, and a look-only refusal must not cost a browser start either.
    Anything that needs Chrome — the fence self-test, the page lookup for an
    approval card — happens after this says `ask`.
    """
    level = rung() if level is None else level
    if level == "off":
        return REFUSE, ("Browser control is off for this conversation. Josh "
                        "turns it on in the composer's browser control, per "
                        "conversation -- you cannot enable it from here.")
    kind = PUBLISH.get(tool, (None, ()))[0]
    if kind is None:
        return REFUSE, "Unknown tool: %s" % tool
    if kind in (OBSERVE, NAVIGATE) or level == "full":
        return ALLOW, ""
    if level == "read":
        return REFUSE, ("Browser control is set to look-only for this "
                        "conversation, so reading pages is free but changing "
                        "one is not. Report what you found and let Josh decide.")
    return ASK, ""


def _confine(path, workspace):
    """`path` if it is inside `workspace`, else None.

    realpath BEFORE the containment check, so a junction or symlink out of the
    folder fails rather than resolving out of it after the fact -- the same
    rule `confine_to_workspace` follows in the app.
    """
    if not workspace:
        return None
    try:
        root = os.path.realpath(workspace)
        real = os.path.realpath(os.path.join(root, path) if not
                                os.path.isabs(path) else path)
    except (OSError, ValueError):
        return None
    if real == root or real.startswith(root + os.sep):
        return real
    return None


def _detail(tool, args, url):
    """The sentence Josh reads on the approval card.

    His decision is only as good as this line, so it names the page AND what
    will happen on it.
    """
    where = url or "the current page"
    target = args.get("uid") or args.get("from_uid") or ""
    if tool == "click":
        what = "double-click" if args.get("dblClick") else "click"
        return "%s %s on %s" % (what, target or "an element", where)
    if tool == "hover":
        return "hover %s on %s" % (target or "an element", where)
    if tool == "drag":
        return "drag %s onto %s on %s" % (target, args.get("to_uid"), where)
    if tool in ("fill", "type_text"):
        text = str(args.get("value") if tool == "fill" else args.get("text") or "")
        shown = text if len(text) <= 60 else text[:57] + "..."
        into = (" into %s" % target) if target else ""
        return "type \"%s\"%s on %s" % (shown, into, where)
    if tool == "fill_form":
        # The VALUES, not just a count. `fill` shows its text and `fill_form`
        # did not, so the way to keep a secret off the card Josh reads was to
        # send it through the plural tool -- and his decision is only ever as
        # good as this line.
        fields = args.get("elements")
        if isinstance(fields, list) and fields:
            shown = []
            for item in fields[:6]:
                value = (item or {}).get("value") if isinstance(item, dict) else item
                uid = (item or {}).get("uid") if isinstance(item, dict) else ""
                text = str(value)
                if len(text) > 40:
                    text = text[:37] + "..."
                shown.append('%s="%s"' % (uid or "?", text))
            more = "" if len(fields) <= 6 else " and %d more" % (len(fields) - 6)
            return "fill %s%s on %s" % (", ".join(shown), more, where)
        count = len(fields) if isinstance(fields, list) else "several"
        return "fill %s fields on %s" % (count, where)
    if tool == "press_key":
        return "press %s on %s" % (args.get("key"), where)
    if tool == "handle_dialog":
        # promptText is what gets TYPED into a prompt() box, and it was absent
        # from the card entirely.
        typed = str(args.get("promptText") or "")
        if typed:
            shown = typed if len(typed) <= 60 else typed[:57] + "..."
            return ('answer the dialog on %s with %s, typing "%s"'
                    % (where, args.get("action"), shown))
        return "answer the dialog on %s with %s" % (where, args.get("action"))
    if tool == "upload_file":
        return "upload %s to %s on %s" % (args.get("filePath"),
                                          target or "a field", where)
    if tool == "emulate":
        return "change the browser environment for %s" % where
    if tool == "evaluate_script":
        body = " ".join(str(args.get("function") or "").split())
        shown = body if len(body) <= 100 else body[:97] + "..."
        return "run a script on %s: %s" % (where, shown)
    return "%s on %s" % (tool, where)


# --------------------------------------------------------- the republish ---

def curate(vendor_tools, allow_script=True, level=None):
    """(published, dropped) -- the reconciliation gate.

    `vendor_tools` is the vendor's live tool list. For every tool we intend to
    publish we keep only the argument keys in our table and force
    `additionalProperties: false`. Two rules make this a gate rather than a
    filter:

    * A tool the installed vendor does not offer is DROPPED with a reason. A
      curated republish that invented a tool would be a capability note that
      lies.
    * A tool whose `required` list names an argument our keep-set does not
      have is DROPPED with a reason, never published with a mutilated schema.
      This is what catches a RENAMED required argument on a version bump --
      something a key whitelist alone cannot see -- and it mechanically caught
      that `upload_file` and `take_heapsnapshot` require a path argument,
      which the first draft of this design got wrong by hand.
    """
    # The RUNG decides what is published, not only what is allowed. A
    # look-only conversation that still advertises click/fill/type_text is
    # making the strongest capability claim a model reads and then refusing
    # every call against it -- the exact inverse of why WITHHELD is stated out
    # loud. `decide` still answers for them, so a seat that names one anyway
    # gets the informative refusal rather than "unknown tool".
    level = rung() if level is None else level
    by_name = {}
    for tool in vendor_tools:
        by_name[getattr(tool, "name", None) or tool["name"]] = tool
    published, dropped = [], []

    for name, reason in sorted(WITHHELD.items()):
        if name in by_name:
            dropped.append((name, reason))

    for name in PUBLISH:
        _kind, keep = PUBLISH[name]
        if name == "evaluate_script" and not allow_script:
            dropped.append((name, "Alloy could not rule out that a listed "
                                  "site reaches this machine's own loopback "
                                  "address, and a script carries no URL for "
                                  "the fence to check"))
            continue
        if _kind == ACT and level == "read":
            dropped.append((name, "browser control is set to look-only for "
                                  "this conversation"))
            continue
        vendor = by_name.get(name)
        if vendor is None:
            dropped.append((name, "the installed chrome-devtools-mcp does not "
                                  "offer it"))
            continue
        schema = _schema_of(vendor) or {}
        props = dict(schema.get("properties") or {})
        required = list(schema.get("required") or [])
        missing = [key for key in required if key not in keep]
        if missing:
            dropped.append((name, "its required argument %s is not one Alloy "
                                  "passes through, so publishing it would "
                                  "offer a tool that cannot work"
                                  % ", ".join(sorted(missing))))
            continue
        kept_props = {key: value for key, value in props.items() if key in keep}
        published.append({
            "name": name,
            "description": _description_of(vendor),
            "inputSchema": {"type": "object", "properties": kept_props,
                            "required": required,
                            "additionalProperties": False},
        })
    return published, dropped


def _description_of(tool):
    text = getattr(tool, "description", None)
    if text is None and isinstance(tool, dict):
        text = tool.get("description")
    return text or ""


def _schema_of(tool):
    schema = getattr(tool, "inputSchema", None)
    if schema is None and isinstance(tool, dict):
        schema = tool.get("inputSchema")
    return schema


def instructions_block(published, dropped, kept, rejected, level,
                       allow_script):
    """What the seat is told at connect time.

    Said out loud rather than left as an absence: a seat that knows a
    capability was withheld asks Josh, while a seat that merely finds a tool
    missing invents a workaround.
    """
    lines = ["Alloy's browser control. Chrome is driven by Google's "
             "chrome-devtools-mcp; Alloy sits in front of it and decides what "
             "is allowed."]
    if level == "read":
        lines.append("This conversation is LOOK-ONLY: you may reach and read "
                     "pages, but clicking, typing and scripting are refused.")
    elif level == "ask":
        lines.append("Reaching and reading pages is free. Anything that "
                     "changes a page waits for Josh, so do not plan a long "
                     "unattended sequence of clicks.")
    elif level == "full":
        lines.append("You may act on pages without being asked.")
    if kept:
        lines.append("Chrome can reach ONLY these sites: %s. Everything else "
                     "-- including this machine's own files and any address "
                     "not listed -- is blocked inside Chrome's network stack, "
                     "so there is no way around it and no point trying."
                     % ", ".join(kept))
    else:
        lines.append("No sites are allowlisted, so Chrome can reach NOTHING. "
                     "Say so plainly; Josh has to add a site before browsing "
                     "is possible.")
    for pattern, why in rejected:
        lines.append("Alloy refused the configured pattern %r: %s"
                     % (pattern, why))
    if not allow_script:
        lines.append("evaluate_script is not available in this conversation "
                     "because Alloy could not rule out that a listed site "
                     "reaches this machine's own loopback address (a wildcard "
                     "host or a name it could not resolve counts).")
    # Grouped by REASON, not one line per tool: at the look-only rung nine
    # tools share one sentence, and nine copies of it is the kind of noise
    # a model skims past -- which would defeat the whole point of stating a
    # withholding instead of leaving it as an absence.
    by_reason = {}
    for name, why in dropped:
        if name == "evaluate_script":
            continue                  # already said, with its own reason
        by_reason.setdefault(why, []).append(name)
    for why, names in by_reason.items():
        lines.append("%s %s not available: %s."
                     % (", ".join(sorted(names)),
                        "are" if len(names) > 1 else "is", why))
    return "\n".join(lines)


# ------------------------------------------------------------- the proxy ---

class Proxy:
    """Holds the vendor session and turns one tool call into one result.

    Async because the gate needs the current page URL for the approval card,
    and the only way to learn it is to ask the vendor.
    """

    def __init__(self, vendor=None, workspace=None):
        self._vendor = vendor
        self._workspace = workspace if workspace is not None else _env(
            "ALLOY_BROWSER_WORKSPACE")
        self._fence_ok = None          # None = not proven yet
        self._dead = ""                # non-empty = latched, with the reason
        # An MCP server can be handed overlapping requests, and the fence must
        # be proven exactly once: without this, two calls arriving together
        # both see `_fence_ok is None` and both navigate the probe.
        self._fence_lock = None
        self.published = []
        self.dropped = []
        self.kept, self.rejected, self.loopback = [], [], False

    # -- setup ------------------------------------------------------------
    async def load(self):
        """Fetch the vendor's live tools and curate them. Called once."""
        self.kept, self.rejected, self.loopback = classify_sites()
        listed = await self._vendor.list_tools()
        self.published, self.dropped = curate(listed.tools,
                                              allow_script=not self.loopback,
                                              level=rung())
        return self.published

    def instructions(self):
        return instructions_block(self.published, self.dropped, self.kept,
                                  self.rejected, rung(),
                                  allow_script=not self.loopback)

    # -- the self-test ----------------------------------------------------
    async def prove_fence(self):
        """Demonstrate the fence, once, on the first call that needs Chrome.

        Not at startup: Chrome is launched lazily and a turn whose seat never
        browses must not pay for it. Not repeatedly: the flags cannot change
        while the process lives.

        Anything other than a recognised refusal latches dead -- a fence that
        cannot show itself refusing is one we have no evidence exists, and the
        measured failure (a one-character typo in the flag name, silently
        accepted, `file://` then reachable) looks exactly like success from
        every other angle.
        """
        if self._fence_ok is not None:
            return self._fence_ok
        import asyncio
        if self._fence_lock is None:
            self._fence_lock = asyncio.Lock()
        async with self._fence_lock:
            if self._fence_ok is not None:      # settled while we waited
                return self._fence_ok
            return await self._prove_fence_once()

    async def _prove_fence_once(self):
        try:
            result = await self._vendor.call_tool(
                "navigate_page", {"url": FENCE_PROBE_URL})
            text = _text_of(result).lower()
        except Exception as exc:
            self._fence_ok = False
            self._dead = ("Alloy could not verify that Chrome's site fence is "
                          "active (%s), so browser control is switched off for "
                          "the rest of this conversation." % exc)
            return False
        self._fence_ok = BLOCKED_SIGNATURE in text
        if not self._fence_ok:
            self._dead = ("Alloy checked whether Chrome's site fence was "
                          "actually enforcing and it was not -- a page that "
                          "must have been refused loaded instead. Browser "
                          "control is switched off for the rest of this "
                          "conversation rather than run unfenced.")
        return self._fence_ok

    # -- one call ---------------------------------------------------------
    async def call(self, tool, args):
        if self._dead:
            return _refusal(self._dead)
        entry = PUBLISH.get(tool)
        if entry is None:
            return _refusal("Unknown tool: %s" % tool)
        if not any(t["name"] == tool for t in self.published):
            # A seat that names a withheld tool deserves the REASON it was
            # withheld -- which curate already computed and the instructions
            # already state. "Unknown tool" would send it looking for a
            # spelling mistake that is not there.
            why = dict(self.dropped).get(tool)
            if why:
                return _refusal("Refused: %s is not available here -- %s."
                                % (tool, why))
            _verdict, reason = decide(tool)
            return _refusal("Refused: %s" % (reason or
                                             "that tool is not available in "
                                             "this conversation."))
        keep = entry[1]
        # Whitelist the keys. The published schema already says
        # additionalProperties false and the SDK enforces it before we run --
        # but this process must not depend on whoever is calling it to hold
        # the fence, and the keys it drops include every path a tool could
        # write to.
        args = {k: v for k, v in dict(args or {}).items() if k in keep}

        path_arg = CONFINED_PATH_ARGS.get(tool)
        if path_arg and path_arg in args:
            safe = _confine(str(args[path_arg]), self._workspace)
            if safe is None:
                return _refusal(
                    "Refused: that path is outside this conversation's working "
                    "folder. Files a seat sends to a website have to come from "
                    "the folder Josh pointed the conversation at.")
            args[path_arg] = safe

        # The rung FIRST, because it is the only decision that needs nothing
        # but this process: a refusal must not start a browser, and Josh must
        # not be asked to approve a click on a browser whose fence has not
        # been shown to work.
        verdict, reason = decide(tool)
        if verdict == REFUSE:
            # A refusal is an ANSWER, not a failure. Saying so plainly is what
            # stops a seat retrying a gate that will keep saying no.
            return _refusal("Refused: %s" % reason)

        if not await self.prove_fence():
            return _refusal(self._dead)

        if verdict == ASK:
            url = await self._current_url()
            # OFF THE EVENT LOOP. `ask_josh` polls a directory for up to three
            # minutes, and this loop is also the one draining the vendor
            # child's stdio -- blocking it here would stall Chrome's whole
            # session while Josh reads the card, and a full pipe would wedge
            # the child outright. The wait is genuinely blocking work, so it
            # rides a thread, exactly like the app's other bounded I/O.
            import asyncio
            allowed, reason = await asyncio.to_thread(
                ask_josh, tool, _detail(tool, args, url), url)
            if not allowed:
                return _refusal("Refused: %s" % reason)

        try:
            result = await self._vendor.call_tool(tool, args)
        except Exception as exc:
            self._dead = ("The browser stopped responding (%s). Nothing came "
                          "back, so do not assume the last action happened."
                          % exc)
            return _refusal(self._dead)
        # Forward VERBATIM, isError and all: the vendor's own errors name the
        # URL and the selector, and restating them would lose that.
        #
        # But the note goes on only when the fence did NOT refuse. Rule 4 up
        # top: a URL-policy refusal comes back isError FALSE, so "Josh
        # approved this" stamped above "...is blocked by allowlist rules"
        # would read as an approval that carried the action through -- and the
        # module's own rule says an approval note must never ride a call the
        # fence refused.
        if reason and BLOCKED_SIGNATURE not in _text_of(result).lower():
            _prepend_note(result, "(%s)" % reason)
        return result

    async def _current_url(self):
        """The selected page's URL, for the approval card.

        The vendor renders each page as `<id>: <title> (<url>)` plus a
        trailing ` [selected]`, and THE TITLE IS WRITTEN BY THE PAGE. A
        leftmost match therefore reads a URL the page chose: a title of
        `(https://safe.test) [selected]` wins ahead of the real parenthesis,
        and Josh approves a click on a site the action never touches. So the
        parse is END-ANCHORED -- the real `(url) [selected]` is always last on
        the line, and a non-selected page's line ends with `(url)` and no
        marker, so no title can produce a later match than the truth.

        Same family as the wrap-token bug: a substring match on text somebody
        else controls. Anything that does not end in a plain http(s) URL is
        discarded, and the card then says "the current page" rather than
        something a page made up.
        """
        try:
            listed = await self._vendor.call_tool("list_pages", {})
        except Exception:
            return ""
        for line in _text_of(listed).splitlines():
            match = re.search(r"\(([^()\s]+)\)\s*\[selected\]\s*$", line)
            if not match:
                continue
            url = match.group(1)
            if _scheme_of(url) in ("http", "https"):
                return url
            return ""          # about:blank and friends: name nothing
        return ""


def _text_of(result):
    """Every text block of a CallToolResult, joined. Never raises."""
    out = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            out.append(text)
    return "\n".join(out)


def _prepend_note(result, note):
    """Put Alloy's one-line note above the vendor's own answer, in place."""
    try:
        import mcp.types as t
        content = list(getattr(result, "content", None) or [])
        result.content = [t.TextContent(type="text", text=note)] + content
    except Exception:
        pass


def _refusal(text):
    """A refusal as a CallToolResult. isError stays FALSE on purpose: this is
    a decision Alloy made, not a malfunction, and the seat should read it and
    move on rather than retry."""
    import mcp.types as t
    return t.CallToolResult(content=[t.TextContent(type="text", text=text)],
                            isError=False)


# --------------------------------------------------------------- serving ---

async def serve(proxy_factory=None):
    """Open the vendor session, then serve Alloy's own tools inside it.

    The vendor session is opened HERE and never lazily inside a request
    handler. That is an anyio cancel-scope constraint, not a style choice: a
    prototype that opened it on first use died with "Attempted to exit a
    cancel scope that isn't the current task's" and every list_tools timed out.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    import mcp.types as t

    vendor_path = find_vendor()
    if not vendor_path:
        raise SystemExit("chrome-devtools-mcp is not installed; Alloy's "
                         "browser server has nothing to proxy.")

    # A REAL file object. A bounded ring-buffer writer looks like the tidy
    # choice and raises io.UnsupportedOperation: fileno inside
    # anyio.open_process -- which would make the capability report itself
    # permanently unavailable on a perfectly healthy vendor.
    logdir = _env("ALLOY_BROWSER_APPROVAL_DIR") or tempfile.gettempdir()
    try:
        os.makedirs(logdir, exist_ok=True)
        errlog = open(os.path.join(logdir, "vendor-stderr.log"), "w",
                      encoding="utf-8", errors="replace")
    except OSError:
        errlog = tempfile.NamedTemporaryFile(
            prefix="alloy-browser-", suffix=".log", delete=False,
            mode="w", encoding="utf-8", errors="replace")

    params = StdioServerParameters(command=find_node(), args=vendor_argv(),
                                   env=dict(os.environ))
    # The SDK's stdio client puts the child in a Windows Job Object with
    # KILL_ON_JOB_CLOSE, which is the entire Chrome-reaping guarantee. Hand
    # rolling JSON-RPC here would mean owning ~100 lines of pywin32 to get it
    # back, and getting it wrong means orphan Chromes after every run.
    workspace = _env("ALLOY_BROWSER_WORKSPACE")

    async def list_roots(_ctx):
        """The ONE root the vendor may touch: this conversation's folder.

        Negotiating it replaces the vendor's default temp-dir root, which is
        what makes `upload_file` able to work at all -- and it makes the
        vendor's path check and Alloy's `_confine` the same boundary rather
        than two that disagree. No workspace means NO roots, and the vendor
        then denies every path.
        """
        roots = []
        if workspace and os.path.isdir(workspace):
            roots.append(t.Root(uri=pathlib.Path(workspace).resolve().as_uri(),
                                name="workspace"))
        return t.ListRootsResult(roots=roots)

    async with stdio_client(params, errlog=errlog) as (reader, writer):
        async with ClientSession(reader, writer,
                                 list_roots_callback=list_roots) as vendor:
            await vendor.initialize()
            proxy = (proxy_factory or Proxy)(vendor=vendor)
            await proxy.load()

            app = Server("alloy-browser", instructions=proxy.instructions())

            @app.list_tools()
            async def list_tools():
                return [t.Tool(name=spec["name"],
                               description=spec["description"],
                               inputSchema=spec["inputSchema"])
                        for spec in proxy.published]

            @app.call_tool()
            async def call_tool(name, arguments):
                return await proxy.call(name, arguments)

            async with stdio_server() as (sr, sw):
                await app.run(sr, sw, app.create_initialization_options())


def main():
    import asyncio
    if rung() == "off":
        # Registered-but-off should never happen (the relay does not register
        # the server at all), but if it does, do no browser work whatsoever --
        # not even a node handshake.
        raise SystemExit(0)
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
