"""Alloy's desktop-control MCP server — the delivery half of computer use.

`desktop.py` is the library and knows nothing about seats. THIS is the process
a seat's CLI actually talks to: a stdio MCP server that exposes the desktop
tools and, before any mutating one runs, gets Josh's answer.

CONTRACT — read before changing anything:

* **Everything comes from the environment, nothing from the model.** The rung,
  the approval channel, Alloy's pid and the allowlist are all injected by the
  relay when it spawns this server. A tool argument can never widen what this
  process may do, because the model writes the arguments.

* **Fail closed, at every seam.** No rung set, no approval directory, an
  unparseable answer, a timeout, a crashed relay — all deny. The alternative
  is a gate that opens when nobody is listening, which is worse than no gate.

* **Alloy's pid is TOLD, never inferred.** `desktop.alloy_pids()` climbs its
  OWN ancestry, which is right when the library runs inside the app and wrong
  here: this server is a grandchild of the seat CLI, and in a terminal-launched
  CLI it would not find Alloy at all. `ALLOY_APP_PID` closes that, and the
  refusal of Alloy's own window stops depending on an exe-name coincidence.

* **There is no "allow always".** The only standing allowance is
  `ALLOY_DESKTOP_ALLOWLIST`, which Josh sets up front in config, not by
  clicking a button while a run is waiting on him. A runtime grant that
  outlives the action it was asked for is the thing this design exists to
  refuse — see the four rungs below.

Rungs (`ALLOY_DESKTOP_RUNG`):

    off        nothing works; every tool answers that desktop control is off
    ask        observers free; every mutator asks Josh and expires with the
               observation it cites
    allowlist  as `ask`, but a window whose title or exe matches a pattern
               Josh configured proceeds without asking; everything else asks
    full       observers and mutators both proceed with no prompt, including
               unattended

Two refusals hold at EVERY rung including `full`, and they live in
`desktop.py` so this process cannot relax them: Alloy's own process tree /
WebView2 windows / seat consoles (a seat that can click Alloy's approval modal
makes every approval forgeable), and password fields.

Wire protocol for approvals is the one `approval_hook.py` already proved:
write `<id>.req` into a directory the relay watches, poll for `<id>.ans`,
treat anything else as deny. Reused rather than reinvented so there is one
shape to reason about — but on a SEPARATE directory, because a desktop
request must not be answered by the tool-approval path's standing turn verdict.
"""

import json
import os
import re
import sys
import time
import uuid

POLL = 0.25
# Shorter than approval_hook's 600 s on purpose: that one blocks a CLI child
# that has nothing else to do, while this one blocks a seat mid-turn whose
# idle watchdog is running. A desktop question Josh has not answered in three
# minutes is one he is not sitting in front of.
APPROVAL_TIMEOUT = 180

RUNGS = ("off", "ask", "allowlist", "full")
OBSERVERS = ("screen_read", "screen_shot", "app_list")


def _env(name, default=""):
    return (os.environ.get(name) or default).strip()


def rung():
    value = _env("ALLOY_DESKTOP_RUNG", "off").lower()
    return value if value in RUNGS else "off"


def _allowlist():
    """Compiled patterns from Josh's config, or []. A bad pattern is dropped
    rather than crashing the server — but it is dropped, never widened."""
    raw = _env("ALLOY_DESKTOP_ALLOWLIST")
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        items = [raw]
    out = []
    for item in items if isinstance(items, list) else [items]:
        try:
            out.append(re.compile(str(item), re.I))
        except re.error:
            continue
    return out


def _allowlisted(window):
    """True when Josh pre-approved THIS window by title or executable."""
    pats = _allowlist()
    if not pats:
        return False
    hay = [str((window or {}).get("title") or ""),
           str((window or {}).get("exe") or "")]
    return any(p.search(h) for p in pats for h in hay if h)


def _app_pids():
    """Alloy's own pids, as told to us. Never inferred here — see the docstring."""
    pids = set()
    for chunk in _env("ALLOY_APP_PID").replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            pids.add(int(chunk))
    return pids


def ask_josh(action, detail, window):
    """Block until Josh answers, and return True only for an explicit yes.

    Every other outcome — no channel configured, an unwritable directory, a
    timeout, junk in the answer file — is False. The reason rides back to the
    model so a refusal reads as a decision rather than a malfunction.
    """
    reqdir = _env("ALLOY_DESKTOP_APPROVAL_DIR")
    if not reqdir:
        return False, ("Alloy has no approval channel for desktop actions in "
                       "this conversation, so this is declined.")
    rid = uuid.uuid4().hex[:12]
    payload = {
        "id": rid,
        "kind": "desktop",
        "seat": _env("ALLOY_DESKTOP_SEAT", "a seat"),
        "action": action,
        "detail": detail,
        "window": {k: (window or {}).get(k)
                   for k in ("title", "exe", "pid", "window_id")},
        "ts": time.time(),
    }
    try:
        os.makedirs(reqdir, exist_ok=True)
        tmp = os.path.join(reqdir, rid + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, os.path.join(reqdir, rid + ".req"))
    except OSError as e:
        return False, f"Alloy could not reach Josh for approval ({e})."

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
    return False, ("Josh did not answer within "
                   f"{APPROVAL_TIMEOUT // 60} minutes, so this is declined.")


def gate(tool, describe):
    """(allowed, reason) for one call. The ONLY place a rung is interpreted.

    `describe()` is a CALLABLE returning (window, detail), not a value, so
    that a refusal costs nothing: at rung `off` we must not enumerate the
    desktop merely to explain that we are not allowed to look at it, and an
    observer at a permissive rung needs no window lookup at all.
    """
    level = rung()
    if level == "off":
        return False, ("Desktop control is off for this conversation. Josh "
                       "turns it on in the composer's desktop control, per "
                       "conversation — you cannot enable it from here.")
    if tool in OBSERVERS or level == "full":
        return True, ""
    window, detail = describe()
    if level == "allowlist" and _allowlisted(window):
        title = (window or {}).get("title") or "that window"
        return True, f"Josh pre-approved {title} for this conversation."
    return ask_josh(tool, detail, window)


# --------------------------------------------------------------- tools ----

def _schema(props, required=()):
    return {"type": "object", "properties": props,
            "required": list(required), "additionalProperties": False}

_BASED_ON = {
    "type": "object",
    "description": ("The observation you are acting on. Take a screen_read "
                    "first and cite the observation_id and window_id it "
                    "returned — an action that cites a stale observation is "
                    "refused rather than applied to a screen that moved."),
    "properties": {"observation_id": {"type": "string"},
                   "window_id": {"type": "string"}},
    "required": ["observation_id", "window_id"],
    "additionalProperties": False,
}

TOOLS = [
    ("app_list", "List the windows on this desktop that Alloy will let you "
                 "touch. Free — cites nothing. Windows Alloy refuses are "
                 "listed with the reason so you do not retry them.",
     _schema({})),
    ("screen_read", "Read one window's accessibility tree: every element with "
                    "its id, role, name, position and the patterns it "
                    "supports. This is how you SEE a window — prefer it to a "
                    "screenshot, it is cheaper and exact. Returns an "
                    "observation_id that every action must cite.",
     _schema({"window_id": {"type": "string"}}, ["window_id"])),
    ("screen_shot", "A PNG of one window, for when the tree is not enough "
                    "(a canvas, an image, a custom-drawn control). Also "
                    "returns an observation_id.",
     _schema({"window_id": {"type": "string"}}, ["window_id"])),
    ("click", "Click one element (preferred) or a point inside the cited "
              "window. The mouse pointer never moves and the window is never "
              "brought to the front.",
     _schema({"based_on": _BASED_ON,
              "element_id": {"type": "string",
                             "description": "From screen_read. Prefer this."},
              "x": {"type": "integer"}, "y": {"type": "integer"},
              "button": {"type": "string", "enum": ["left", "right"]}},
             ["based_on"])),
    ("type_text", "Set the text of one editable element. Refused rather than "
                  "typed blind if the element exposes no value pattern, and "
                  "refused outright for password fields.",
     _schema({"based_on": _BASED_ON, "element_id": {"type": "string"},
              "text": {"type": "string"}},
             ["based_on", "element_id", "text"])),
    ("scroll", "Scroll an element, or the cited window.",
     _schema({"based_on": _BASED_ON, "element_id": {"type": "string"},
              "direction": {"type": "string",
                            "enum": ["up", "down", "left", "right"]},
              "notches": {"type": "integer"}},
             ["based_on", "direction"])),
    ("key", "Send a key or chord (e.g. \"Ctrl+S\", \"Enter\") to the cited "
            "window.",
     _schema({"based_on": _BASED_ON, "keys": {"type": "string"}},
             ["based_on", "keys"])),
]


# The ONLY keys each tool may receive. Derived from the schemas above so the
# two cannot drift, and deliberately narrower than the Python signatures:
# `allow_password` and `strict_pixels` are absent everywhere, which is what
# makes them settings rather than arguments.
ALLOWED_ARGS = {name: frozenset(schema["properties"]) for name, _, schema in TOOLS}


def _detail(tool, args, window):
    """The sentence Josh reads on the approval card. His decision is only as
    good as this line, so it names the window AND what will happen in it."""
    where = (window or {}).get("title") or (window or {}).get("exe") or "a window"
    if tool == "click":
        what = args.get("element_id") or f"({args.get('x')}, {args.get('y')})"
        return f"click {what} in “{where}”"
    if tool == "type_text":
        text = str(args.get("text") or "")
        shown = text if len(text) <= 60 else text[:57] + "…"
        return f"type “{shown}” into {args.get('element_id')} in “{where}”"
    if tool == "scroll":
        return f"scroll {args.get('direction', 'down')} in “{where}”"
    if tool == "key":
        return f"press {args.get('keys')} in “{where}”"
    return f"{tool} in “{where}”"


class Runner:
    """Holds the one Desktop instance and turns a tool call into text."""

    def __init__(self, desk=None):
        self._desk = desk
        self._error = None
        if desk is None:
            try:
                import desktop as _desktop
                self._desk = _desktop.Desktop(self_pids=_app_pids() or None)
                self._mod = _desktop
            except Exception as e:            # no Windows, missing piece, …
                self._error = f"Desktop control is unavailable here: {e}"
        else:
            import desktop as _desktop
            self._mod = _desktop

    def _window_for(self, tool, args):
        """The window this call targets, for the gate and the approval card.

        Resolved by ENUMERATION (app_list walks window handles; it does not
        walk a UIA tree), so it is cheap and, more importantly, it describes
        the window as it is right now rather than as the model asserted it.
        Failure yields the bare id: a card that cannot name the window still
        has to be shown, it just has less to say.
        """
        wid = args.get("window_id") or (args.get("based_on") or {}).get("window_id")
        if not wid:
            return {}
        try:
            for win in (self._desk.app_list() or {}).get("windows") or []:
                if str(win.get("window_id")) == str(wid):
                    return win
        except Exception:
            pass
        return {"window_id": wid}

    def call(self, tool, args):
        if self._error:
            return self._error
        allowed_keys = ALLOWED_ARGS.get(tool)
        if allowed_keys is None:
            return f"Unknown tool: {tool}"
        # Whitelist the keys. The schema already says additionalProperties
        # false, but the schema is enforced by whoever is calling us, and the
        # kwargs we would otherwise splat include `allow_password` and
        # `strict_pixels` — two safety defaults the MODEL must never be able
        # to flip by naming them.
        args = {k: v for k, v in dict(args or {}).items() if k in allowed_keys}

        def describe():
            window = self._window_for(tool, args)
            return window, _detail(tool, args, window)

        allowed, reason = gate(tool, describe)
        if not allowed:
            # A refusal is an ANSWER, not a failure: say so plainly so the
            # seat moves on or asks Josh in the transcript, instead of
            # retrying a gate that will keep saying no.
            return f"Refused: {reason}"
        try:
            result = getattr(self._desk, tool)(**args)
        except self._mod.DesktopError as e:
            return f"Refused: {e}"
        except TypeError as e:
            return f"Bad arguments for {tool}: {e}"
        except Exception as e:
            return f"{tool} failed: {type(e).__name__}: {e}"
        text = result.get("text") if isinstance(result, dict) else None
        out = text or json.dumps(result, ensure_ascii=False, default=str)
        if reason and tool not in OBSERVERS:
            out = f"({reason})\n{out}"
        return out


def build_server(runner=None):
    """The MCP wiring, separated so tests drive `Runner` without a transport."""
    from mcp.server import Server
    import mcp.types as t

    run = runner or Runner()
    app = Server("alloy-desktop")

    @app.list_tools()
    async def list_tools():
        return [t.Tool(name=n, description=d, inputSchema=s)
                for n, d, s in TOOLS]

    @app.call_tool()
    async def call_tool(name, arguments):
        return [t.TextContent(type="text", text=run.call(name, arguments))]

    return app


def main():
    import asyncio
    from mcp.server.stdio import stdio_server

    app = build_server()

    async def serve():
        async with stdio_server() as (reader, writer):
            await app.run(reader, writer, app.create_initialization_options())

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
