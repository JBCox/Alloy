"""Desktop control -- reading and driving Windows UI through the accessibility
tree, without ever taking the screen away from Josh.

This is the Python port of PerryLink/dsh-click (Apache-2.0). It ships as a
LIBRARY: no MCP server, no seat exposure, no relay integration. Nothing here
imports relay, app or webview, and importing it is cheap on any OS -- the
.NET UIAutomation assemblies load lazily, on first real use.

Scope is **Windows only**, deliberately and permanently for this wave. macOS
(AXUIElement) and Linux (AT-SPI) are out of scope: they are different trees
with different identity models, and a half-ported one would be a button that
looks like it works. ``probe()`` says so by name rather than raising an
AttributeError out of a missing ``ctypes.windll``.

Five rules shape this file.

 1. **Observe, cite, verify, act.** ``screen_shot``/``screen_read``/``app_list``
    are free. Every mutator demands ``based_on={"observation_id", "window_id"}``
    and then takes a FULL re-snapshot before it touches anything. If the world
    moved, the mutator refuses by name -- ``UNKNOWN_OBSERVATION``, ``EXPIRED``,
    ``STALE_IDENTITY``, ``STALE_TREE``, ``STALE_PIXELS`` -- and every refusal
    sentence ends with the instruction that fixes it. A model that acts on a
    stale tree clicks whatever slid into that rectangle, which is the one
    failure mode of this whole category.

 2. **The cursor never moves and the foreground is never stolen.** No
    SendInput, no mouse_event, no SetCursorPos, no SetForegroundWindow --
    anywhere, ever. Actions go through UIA patterns (``InvokePattern``,
    ``ValuePattern``, ``ScrollPattern``) and fall back to PostMessage into the
    target window's own queue. Josh keeps his mouse while a seat works. The
    honest cost is that posted input is best-effort: many modern frameworks
    (Chromium, WPF, UWP) ignore synthetic messages to an unfocused window, so
    every mutator reports ``delivered: "uia" | "posted" | "none"`` and never
    claims a posted message was received.

 3. **Refusing Alloy is not a safety preference, it is the never-forge rule.**
    A seat that can click Alloy's own approval modal makes every approval in
    this app forgeable -- the human-in-the-loop becomes decorative. So the
    entire Alloy process tree, every WebView2/Chromium window class, and the
    seat CLIs' own consoles are refused at every permission rung including a
    future "full". There is no override parameter, on purpose.

 4. **Elements are re-resolved by identity, never by index.** ``element_id`` is
    the UIA RuntimeId, dot-joined; acting looks it up again through
    ``FindFirst(RuntimeIdProperty)``. dsh-click's index-into-a-cached-list is
    exactly how a click lands on the row below the one that was read.

 5. **Truncation announces itself, and a black frame says it is black.** A
    pruned tree reports how many elements were seen; a PrintWindow that came
    back flat reports ``blank: True`` with no image instead of handing a model
    a black rectangle to reason about.

The ``backend`` argument is the test seam, the same shape as
``dictation.stream_factory`` and ``speaker.runner``: ``Desktop(backend=Fake())``
drives every path in this file with zero hardware, zero windows and zero
tokens. ``tests/test_desktop.py`` never opens a real window.

Deliberate divergences from dsh-click are marked ``DIVERGENCE`` in comments.
"""

import hashlib
import os
import platform
import re
import threading
import time
import unicodedata
from collections import OrderedDict

# ------------------------------------------------------------- constants ---

# A Chromium/WebView2 window blows past this easily; a native dialog uses a
# dozen. The cap is a budget, not a promise -- see _prune, which RANKS rather
# than truncating in document order (DIVERGENCE from dsh-click's flat DFS).
MAX_ELEMENTS = 500
MAX_DEPTH = 32
# Hard bound on nodes VISITED, so a hostile or cyclic provider cannot make the
# walk unbounded even though only MAX_ELEMENTS survive it.
MAX_VISIT = 6000

# Every model-visible string is cut to this. Element names are labels, not
# documents, and a runaway one is how a tree render eats a context window.
MAX_STRING = 200

# 30 s is a compromise: long enough for a model to think between reading and
# acting, short enough that a stale click is unlikely to be a surprising one.
OBSERVATION_TTL = 30.0
# Eight is enough for "read three windows, act on the second" and small enough
# that nothing here is a cache with a lifetime problem.
OBSERVATION_MAX = 8
OBSERVATION_ID_LEN = 32

# Screenshots are for a model to look at, not for archival.
MAX_SIDE = 2560

# PrintWindow flag: render the whole window including DirectComposition
# content. Not every renderer honours it, which is why _capture checks for a
# flat frame instead of trusting the return code.
PW_RENDERFULLCONTENT = 2

MOUSE_MESSAGES = {                      # (button-down, button-up)
    "left": (0x0201, 0x0202),           # WM_LBUTTONDOWN / WM_LBUTTONUP
    "right": (0x0204, 0x0205),          # WM_RBUTTONDOWN / WM_RBUTTONUP
}
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WHEEL_DELTA = 120

# One table, two paths. dsh-click mapped "up" to ScrollAmount.SmallIncrement
# in its UIA path while its wheel path used +1, so the two disagreed on which
# way "up" went -- a real bug, and the kind that only shows up on a window
# that happens to expose ScrollPattern. Both paths below derive from the sign
# here: +1 means "toward the end of the content" (down, or right).
SCROLL_DIRECTIONS = {
    "up": (-1, "vertical"),
    "down": (1, "vertical"),
    "left": (-1, "horizontal"),
    "right": (1, "horizontal"),
}

# Control types that carry meaning even with no pattern, and containers that
# carry none without a name. Used by _prune only.
INTERACTIVE_TYPES = frozenset((
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem",
    "MenuItem", "RadioButton", "Slider", "SplitButton", "TabItem", "Tree",
    "TreeItem", "Spinner", "Document", "Text", "Image", "Table", "List",
    "Menu", "Window", "TitleBar", "DataItem", "Calendar", "ProgressBar",
))
CONTAINER_TYPES = frozenset((
    "Pane", "Group", "Custom", "Separator", "Thumb", "ToolBar", "Header",
    "HeaderItem", "ScrollBar", "StatusBar", "Unknown",
))

# The self-approval class. Chrome_WidgetWin_* is Alloy's own WebView2 host --
# and also Chrome, Discord, and the Claude desktop app, because at the window
# class level they are indistinguishable. That over-refusal is the correct
# direction: refusing a browser costs a tool call, while missing Alloy's own
# window costs every approval gate in the app.
WEBVIEW_CLASS_PREFIXES = ("Chrome_WidgetWin_",)
# The seat CLIs and their consoles. A seat driving a terminal has escalated to
# an unlogged shell, and it already HAS a shell -- so this buys nothing and
# costs the whole audit trail.
SEAT_CONSOLE_EXES = frozenset((
    "claude.exe", "codex.exe", "agy.exe", "opencode.exe", "node.exe",
    "python.exe", "pythonw.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "conhost.exe", "windowsterminal.exe", "openconsole.exe",
))
# Ancestor walk stops here: past this point we are looking at the shell, and
# claiming its whole subtree would refuse the entire desktop.
_SHELL_ROOTS = frozenset((
    "explorer.exe", "services.exe", "wininit.exe", "winlogon.exe",
    "svchost.exe", "userinit.exe", "system", "systemd", "init",
))
_PID_CACHE_TTL = 2.0

# Refusal codes. The five staleness ones are the contract from the design; the
# rest are "you cited something that cannot be acted on".
UNKNOWN_OBSERVATION = "UNKNOWN_OBSERVATION"
EXPIRED = "EXPIRED"
STALE_IDENTITY = "STALE_IDENTITY"
STALE_TREE = "STALE_TREE"
STALE_PIXELS = "STALE_PIXELS"
ELEMENT_GONE = "ELEMENT_GONE"
NO_VALUE_PATTERN = "NO_VALUE_PATTERN"
PASSWORD_FIELD = "PASSWORD_FIELD"
STALENESS_CODES = (UNKNOWN_OBSERVATION, EXPIRED, STALE_IDENTITY, STALE_TREE,
                   STALE_PIXELS)

_REOBSERVE = "run screen_read again before acting."


class DesktopError(RuntimeError):
    """Something went wrong mid-action. The message says what, and what now."""


class DesktopUnavailable(DesktopError):
    """A piece of the stack is missing. The message names the missing piece."""


class DesktopRefused(DesktopError):
    """Policy said no. Raised, not returned, because it is never negotiable.

    The split with the returned refusals is deliberate: a returned refusal
    means "the world moved, look again", which is a mechanical next step for
    the model. A raised one means "you may not do this at all", which is not.
    """

    def __init__(self, message, code="REFUSED"):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------- sanitization ---

_REDACT_ASSIGNMENT = re.compile(
    r"\b(api[_-]?key|apikey|token|secret|password|passwd|pwd)\b"
    r"(\s*[:=]\s*|\s+is\s+)(\S+)", re.IGNORECASE)
_REDACT_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}"
                         r"(?:\.[A-Za-z0-9_\-]*)?")
_REDACT_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._\-~+/=]{8,}",
                            re.IGNORECASE)
# Two separators or a drive letter. One slash is "Save/Load", not a path, and
# head-first truncation of a label reads as corruption.
_PATHISH = re.compile(r"[A-Za-z]:[\\/]|(?:[\\/][^\\/]+){2,}")


def strip_controls(text):
    """Drop C0/C1 control characters; fold tabs and newlines into spaces.

    Element names render one per line, so a name carrying a newline would
    forge an extra row in the text output -- and one carrying an ANSI escape
    would reach whatever terminal is downstream.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    out = []
    for ch in text:
        if ch in "\t\r\n":
            out.append(" ")
        elif unicodedata.category(ch) == "Cc" or ch == "\x7f":
            continue
        else:
            out.append(ch)
    return " ".join("".join(out).split())


def redact(text):
    """Blank out the secret shapes. Applied to EVERY model-visible string.

    A window title or an element name is arbitrary text from somebody else's
    app; a token that crosses into a transcript is a token that has leaked.
    """
    if not text:
        return text
    text = _REDACT_JWT.sub("[redacted-jwt]", text)
    text = _REDACT_BEARER.sub("Bearer [redacted]", text)
    return _REDACT_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]",
                                  text)


def truncate(text, limit=MAX_STRING, path=False):
    """Cut to `limit`. Paths lose their HEAD so the filename survives.

    `C:\\Users\\joshu\\...\\report.xlsx` truncated from the tail is a string
    that tells you nothing; truncated from the head it still names the file.
    """
    if not text or len(text) <= limit:
        return text
    if path:
        keep = max(1, limit - 1)
        return "\u2026" + text[-keep:]
    return text[:max(0, limit - 1)] + "\u2026"


def sanitize(text, limit=MAX_STRING, path=None):
    """The one pipeline every model-visible and logged string goes through."""
    cleaned = redact(strip_controls(text))
    if path is None:
        path = bool(cleaned) and bool(_PATHISH.search(cleaned))
    return truncate(cleaned, limit, path=path)


# ------------------------------------------------------------ pure helpers --

def pack_lparam(x, y):
    """LPARAM for a mouse message: high word is y, low word is x."""
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def scroll_amount(direction):
    """UIA ScrollAmount name + axis for a direction. Reads SCROLL_DIRECTIONS."""
    entry = SCROLL_DIRECTIONS.get(str(direction).strip().lower())
    if entry is None:
        raise DesktopError(
            f"Unknown scroll direction {direction!r}; use one of "
            f"{', '.join(sorted(SCROLL_DIRECTIONS))}.")
    sign, axis = entry
    return ("SmallIncrement" if sign > 0 else "SmallDecrement"), axis


def wheel_delta(direction, notches=1):
    """WM_MOUSE(H)WHEEL delta + horizontal flag. Reads SCROLL_DIRECTIONS.

    Win32's two wheels disagree with each other, not with us: a POSITIVE
    vertical delta means the wheel rotated away from the user, i.e. toward the
    BEGINNING of the content, while a positive horizontal delta means right,
    i.e. toward the END. Both are derived from the same `sign` as
    scroll_amount above, which is the whole point -- the two paths cannot
    drift apart the way dsh-click's did.
    """
    entry = SCROLL_DIRECTIONS.get(str(direction).strip().lower())
    if entry is None:
        raise DesktopError(
            f"Unknown scroll direction {direction!r}; use one of "
            f"{', '.join(sorted(SCROLL_DIRECTIONS))}.")
    sign, axis = entry
    steps = max(1, int(notches or 1))
    if axis == "vertical":
        return -sign * steps * WHEEL_DELTA, False
    return sign * steps * WHEEL_DELTA, True


VK = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "space": 0x20,
    "insert": 0x2D, "ins": 0x2D, "home": 0x24, "end": 0x23,
    "pgup": 0x21, "pageup": 0x21, "pgdn": 0x22, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    "win": 0x5B, "super": 0x5B, "meta": 0x5B,
}
for _i in range(1, 13):
    VK[f"f{_i}"] = 0x6F + _i                      # F1 = 0x70 ... F12 = 0x7B
for _c in "0123456789":
    VK[_c] = 0x30 + int(_c)
for _c in "abcdefghijklmnopqrstuvwxyz":
    VK[_c] = 0x41 + (ord(_c) - ord("a"))
del _i, _c

MODIFIER_KEYS = ("ctrl", "control", "shift", "alt", "win", "super", "meta")
# Printable characters for these get a WM_CHAR; the others are commands.
_CHARLESS_MODIFIERS = frozenset(("ctrl", "control", "alt", "win", "super",
                                 "meta"))
_CHAR_FOR = {"enter": "\r", "tab": "\t", "space": " "}


def parse_keys(spec):
    """'ctrl+shift+s' or ['ctrl', 's'] -> (['ctrl', 'shift'], 's').

    The last token is the key; everything before it must be a modifier, so a
    typo'd chord is a hard error rather than a silent single keypress.
    """
    if isinstance(spec, (list, tuple)):
        parts = [str(p).strip().lower() for p in spec if str(p).strip()]
    else:
        parts = [p.strip().lower() for p in str(spec or "").split("+")
                 if p.strip()]
    if not parts:
        raise DesktopError("No key was given; pass e.g. 'enter' or 'ctrl+s'.")
    key = parts[-1]
    mods = parts[:-1]
    for name in mods:
        if name not in MODIFIER_KEYS:
            raise DesktopError(
                f"{name!r} is not a modifier; modifiers are "
                f"{', '.join(sorted(set(MODIFIER_KEYS)))}.")
    if key not in VK:
        raise DesktopError(
            f"Unknown key {key!r}. Known keys: letters, digits, F1-F12, and "
            f"{', '.join(sorted(k for k in VK if len(k) > 1))}.")
    return mods, key


def key_char(key, modifiers):
    """The WM_CHAR payload for a keypress, or None when it is a command."""
    if any(m in _CHARLESS_MODIFIERS for m in modifiers):
        return None                       # Ctrl+S is a command, not an 's'
    if key in _CHAR_FOR:
        return _CHAR_FOR[key]
    if len(key) == 1 and key.isalpha():
        return key.upper() if "shift" in modifiers else key
    if len(key) == 1 and key.isdigit() and "shift" not in modifiers:
        # Shift+digit is a layout-dependent symbol (! on US, " on UK). Guessing
        # would type the wrong character silently, so no WM_CHAR is sent and
        # the key-down/up pair stands on its own.
        return key
    return None


def rects_intersect(a, b):
    if not a or not b:
        return False
    return not (a["x"] + a["width"] <= b["x"]
                or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"]
                or b["y"] + b["height"] <= a["y"])


def point_in_rect(x, y, rect):
    if not rect:
        return False
    return (rect["x"] <= x < rect["x"] + rect["width"]
            and rect["y"] <= y < rect["y"] + rect["height"])


def rect_centre(rect):
    return (int(rect["x"] + rect["width"] // 2),
            int(rect["y"] + rect["height"] // 2))


def _sha(*parts):
    h = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            h.update(part)
        else:
            h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def identity_string(window):
    """The bytes that make two window observations 'the same window'."""
    rect = window.get("rect") or {}
    return "|".join(str(v) for v in (
        window.get("window_id", ""), window.get("pid", ""),
        window.get("exe", ""), window.get("title", ""),
        window.get("class_name", ""),
        rect.get("x", ""), rect.get("y", ""),
        rect.get("width", ""), rect.get("height", "")))


def tree_hash(elements):
    """sha256 over 'elementId|controlType|name|x,y,w,h' joined by ';'."""
    rows = []
    for el in elements:
        rect = el.get("rect") or {}
        rows.append("{}|{}|{}|{},{},{},{}".format(
            el.get("element_id", ""), el.get("control_type", ""),
            el.get("name", ""), rect.get("x", 0), rect.get("y", 0),
            rect.get("width", 0), rect.get("height", 0)))
    return hashlib.sha256(";".join(rows).encode("utf-8", "replace")).hexdigest()


def observation_id(identity, tree_h, shot_h):
    return _sha(identity, tree_h, shot_h)[:OBSERVATION_ID_LEN]


# ---------------------------------------------------------------- backend ---

class Backend:
    """What ``Desktop`` needs from the world. Every method is overridable.

    Deliberately dumb: no policy, no hashing, no pruning, no verification.
    Everything a test needs to reason about lives in Desktop, and everything
    that needs a real window lives here -- which is what makes the fake in
    tests/test_desktop.py fifty lines instead of a simulator.
    """

    name = "backend"

    def available(self):
        """(ok, reason). Reason names the missing piece, dictation-style."""
        return False, "No desktop backend is configured."

    # ----- observation
    def list_windows(self):
        """[{window_id, pid, exe, title, class_name, rect}] for visible tops."""
        raise NotImplementedError

    def window(self, window_id):
        """One window's identity snapshot, or None when it is gone."""
        raise NotImplementedError

    def walk(self, window_id, max_depth=MAX_DEPTH, max_visit=MAX_VISIT):
        """{'elements': [...], 'seen': n, 'hit_visit_cap': bool}."""
        raise NotImplementedError

    def capture(self, window_id, max_side=MAX_SIDE):
        """{'png': bytes|None, 'width', 'height', 'blank': bool, 'raw': bytes}."""
        raise NotImplementedError

    def process_identity(self, window_id):
        """(pid, exe) as the OS reports them right now, or (None, None)."""
        raise NotImplementedError

    # ----- action
    def resolve(self, window_id, element_id):
        """Re-find an element by RuntimeId. None when it is gone."""
        raise NotImplementedError

    def invoke(self, handle):
        raise NotImplementedError

    def get_value(self, handle):
        raise NotImplementedError

    def set_value(self, handle, text):
        raise NotImplementedError

    def scroll_pattern(self, handle, amount, axis):
        raise NotImplementedError

    def post_click(self, window_id, x, y, button):
        raise NotImplementedError

    def post_wheel(self, window_id, x, y, delta, horizontal):
        raise NotImplementedError

    def post_key(self, window_id, kind, vk, char=None):
        """kind is 'down' | 'up' | 'char'."""
        raise NotImplementedError


# ------------------------------------------------- the real Windows backend --

_UIA = {}
_UIA_LOCK = threading.Lock()
_UIA_ERROR = [None]


def load_uia():
    """Load the .NET UIAutomation assemblies once. Returns the namespace dict.

    ``clr.AddReference("UIAutomationClient")`` does NOT work here (it throws
    FileNotFoundException -- the assemblies are not in the GAC probing path
    pythonnet uses), so they are loaded by absolute path out of the WPF
    directory. Measured cost: ~0.2-0.5 s, once, which is exactly why this is
    lazy and never runs at import time.
    """
    with _UIA_LOCK:
        if _UIA:
            return _UIA
        if _UIA_ERROR[0]:
            raise DesktopUnavailable(_UIA_ERROR[0])
        try:
            import glob
            import clr                                    # noqa: F401
            import System
            import System.Reflection as Reflection
            root = os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"),
                "Microsoft.NET", "Framework64", "v4.0.30319", "WPF")
            found = sorted(glob.glob(os.path.join(root, "UIAutomation*.dll")))
            if not found:                                 # 32-bit fallback
                root = root.replace("Framework64", "Framework")
                found = sorted(glob.glob(os.path.join(root,
                                                      "UIAutomation*.dll")))
            if not found:
                raise DesktopUnavailable(
                    "The .NET UIAutomation assemblies were not found under "
                    f"{root} -- desktop control needs the .NET Framework 4 "
                    "WPF assemblies that ship with Windows.")
            for path in found:
                Reflection.Assembly.LoadFrom(path)
            from System.Windows.Automation import (
                AutomationElement, Condition, ControlType, InvokePattern,
                PropertyCondition, ScrollAmount, ScrollPattern, TreeScope,
                TreeWalker, ValuePattern)
            _UIA.update({
                "System": System, "AutomationElement": AutomationElement,
                "Condition": Condition, "ControlType": ControlType,
                "InvokePattern": InvokePattern,
                "PropertyCondition": PropertyCondition,
                "ScrollAmount": ScrollAmount, "ScrollPattern": ScrollPattern,
                "TreeScope": TreeScope, "TreeWalker": TreeWalker,
                "ValuePattern": ValuePattern,
            })
            return _UIA
        except DesktopUnavailable as exc:
            _UIA_ERROR[0] = str(exc)
            raise
        except Exception as exc:
            _UIA_ERROR[0] = (
                "Could not load the .NET UIAutomation assemblies "
                f"({type(exc).__name__}: {exc}). Desktop control needs "
                "pythonnet plus the Windows .NET Framework 4 runtime.")
            raise DesktopUnavailable(_UIA_ERROR[0]) from exc


def _q(fn, default=None):
    """Read one UIA property. A hostile or slow provider yields a blank field.

    Every single .Current.* read goes through this. A provider that throws on
    one property must not break the walk -- dsh-click learned this the hard
    way and so does anything that walks somebody else's UI.
    """
    try:
        return fn()
    except Exception:
        return default


class UiaBackend(Backend):
    """The real thing: UIAutomation for the tree, PostMessage for input."""

    name = "uia"

    # Deliberately stateless: there is NO cache of live AutomationElements
    # between a walk and an action. Holding one would make "re-resolve by
    # RuntimeId" optional, and the day somebody reaches for the cached handle
    # instead is the day a click lands on the row below the one that was read.

    # ------------------------------------------------------- availability --

    def available(self):
        for module, fix in (("win32gui", "pywin32 (pip install pywin32)"),
                            ("clr", "pythonnet (pip install pythonnet)"),
                            ("PIL", "Pillow (pip install Pillow)"),
                            ("psutil", "psutil (pip install psutil)")):
            if not _has(module):
                return False, f"Desktop control needs {fix}."
        try:
            load_uia()
        except DesktopUnavailable as exc:
            return False, str(exc)
        return True, ""

    # ------------------------------------------------------- observation ---

    @staticmethod
    def _hwnd(window_id):
        try:
            return int(str(window_id).split(":")[-1])
        except Exception as exc:
            raise DesktopError(f"{window_id!r} is not a window id.") from exc

    @staticmethod
    def _exe_for(pid):
        try:
            import psutil
            return psutil.Process(int(pid)).exe() or ""
        except Exception:
            return ""

    def _describe(self, hwnd):
        import win32gui
        import win32process
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            return None
        pid = None
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            pass
        return {
            "window_id": f"w:{hwnd}",
            "hwnd": int(hwnd),
            "pid": pid,
            "exe": self._exe_for(pid) if pid else "",
            "title": _q(lambda: win32gui.GetWindowText(hwnd), "") or "",
            "class_name": _q(lambda: win32gui.GetClassName(hwnd), "") or "",
            "rect": {"x": left, "y": top,
                     "width": max(0, right - left),
                     "height": max(0, bottom - top)},
        }

    def list_windows(self):
        import win32gui
        found = []

        def collect(hwnd, _extra):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                if not win32gui.GetWindowText(hwnd):
                    return
                info = self._describe(hwnd)
                if info and info["rect"]["width"] and info["rect"]["height"]:
                    found.append(info)
            except Exception:
                pass                       # one bad window may not end the walk

        win32gui.EnumWindows(collect, None)
        return found

    def window(self, window_id):
        import win32gui
        hwnd = self._hwnd(window_id)
        try:
            if not win32gui.IsWindow(hwnd):
                return None
        except Exception:
            return None
        return self._describe(hwnd)

    def process_identity(self, window_id):
        import win32process
        hwnd = self._hwnd(window_id)
        try:
            _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return None, None
        return pid, self._exe_for(pid)

    def _root(self, window_id):
        uia = load_uia()
        hwnd = self._hwnd(window_id)
        try:
            return uia["AutomationElement"].FromHandle(
                uia["System"].IntPtr(int(hwnd)))
        except Exception as exc:
            raise DesktopError(
                f"Window {window_id} has no accessibility tree ({exc}).") from exc

    @staticmethod
    def _element_dict(el, depth, uia):
        rid = _q(lambda: list(el.GetRuntimeId()), None)
        if not rid:
            return None                    # no stable identity -> unusable
        element_id = ".".join(str(int(n)) for n in rid)
        ctype = _q(lambda: el.Current.ControlType.ProgrammaticName, "") or ""
        ctype = ctype.split(".")[-1] if ctype else "Unknown"
        rect = _q(lambda: el.Current.BoundingRectangle, None)
        box = {"x": 0, "y": 0, "width": 0, "height": 0}
        if rect is not None:
            empty = _q(lambda: bool(rect.IsEmpty), True)
            if not empty:
                vals = _q(lambda: (rect.X, rect.Y, rect.Width, rect.Height),
                          None)
                if vals and all(abs(v) < 1e7 for v in vals):
                    box = {"x": int(vals[0]), "y": int(vals[1]),
                           "width": int(vals[2]), "height": int(vals[3])}
        patterns = []
        for label, pattern in (("value", uia["ValuePattern"].Pattern),
                               ("invoke", uia["InvokePattern"].Pattern),
                               ("scroll", uia["ScrollPattern"].Pattern)):
            if _q(lambda p=pattern: el.TryGetCurrentPattern(p)[0], False):
                patterns.append(label)
        # Sanitized here AND again in Desktop._public_element. It is
        # idempotent, and the boundary must hold even for a backend that
        # forgets -- but a 4 MB element name should not travel that far first.
        return {
            "element_id": element_id,
            "control_type": ctype,
            "name": sanitize(_q(lambda: el.Current.Name, "") or ""),
            "automation_id": sanitize(
                _q(lambda: el.Current.AutomationId, "") or ""),
            "class_name": sanitize(_q(lambda: el.Current.ClassName, "") or ""),
            "rect": box,
            "enabled": bool(_q(lambda: el.Current.IsEnabled, False)),
            "patterns": patterns,
            "is_password": bool(_q(
                lambda: el.GetCurrentPropertyValue(
                    uia["AutomationElement"].IsPasswordProperty), False)),
            "depth": depth,
        }

    def walk(self, window_id, max_depth=MAX_DEPTH, max_visit=MAX_VISIT):
        uia = load_uia()
        walker = uia["TreeWalker"].ControlViewWalker
        root = self._root(window_id)
        elements, seen, hit_cap = [], 0, False
        stack = [(root, 0)]
        while stack:
            el, depth = stack.pop()
            seen += 1
            if seen > max_visit:
                hit_cap = True
                break
            info = self._element_dict(el, depth, uia)
            if info is not None:
                elements.append(info)
            if depth >= max_depth:
                continue
            kids = []
            child = _q(lambda: walker.GetFirstChild(el))
            guard = 0
            while child is not None:
                if guard >= max_visit:
                    # A sibling list longer than the whole visit budget is a
                    # cap being hit, and a cap that stays quiet is the bug
                    # this repo names by hand every time: truncation
                    # announces itself.
                    hit_cap = True
                    break
                kids.append(child)
                child = _q(lambda c=child: walker.GetNextSibling(c))
                guard += 1
            stack.extend((k, depth + 1) for k in reversed(kids))
        return {"elements": elements, "seen": seen, "hit_visit_cap": hit_cap}

    def capture(self, window_id, max_side=MAX_SIDE):
        import ctypes
        import io
        import win32gui
        import win32ui
        from PIL import Image
        hwnd = self._hwnd(window_id)
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width, height = max(1, right - left), max(1, bottom - top)
        window_dc = mfc_dc = save_dc = bitmap = None
        try:
            window_dc = win32gui.GetWindowDC(hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(window_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bitmap = win32ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
            save_dc.SelectObject(bitmap)
            # PrintWindow, not CopyFromScreen: this is the only call that can
            # render a window that is occluded or entirely behind another one,
            # which is the normal state of a window a seat is working on.
            ok = ctypes.windll.user32.PrintWindow(
                hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
            info = bitmap.GetInfo()
            raw = bytes(bitmap.GetBitmapBits(True))
            image = Image.frombuffer(
                "RGB", (info["bmWidth"], info["bmHeight"]), raw,
                "raw", "BGRX", 0, 1)
        finally:
            # GDI objects leak per call if this is skipped, and a screenshot
            # tool is called in loops.
            if bitmap is not None:
                _q(lambda: win32gui.DeleteObject(bitmap.GetHandle()))
            if save_dc is not None:
                _q(save_dc.DeleteDC)
            if mfc_dc is not None:
                _q(mfc_dc.DeleteDC)
            if window_dc:
                _q(lambda: win32gui.ReleaseDC(hwnd, window_dc))
        extrema = _q(lambda: image.getextrema(), None)
        blank = bool(not ok or (extrema and all(lo == hi for lo, hi in extrema)))
        if max(image.size) > max_side:
            scale = max_side / float(max(image.size))
            image = image.resize(
                (max(1, int(image.size[0] * scale)),
                 max(1, int(image.size[1] * scale))), Image.LANCZOS)
        png = None
        if not blank:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            png = buffer.getvalue()
        return {"png": png, "width": image.size[0], "height": image.size[1],
                "blank": blank, "raw": bytes(raw)}

    # ------------------------------------------------------------ action ---

    def resolve(self, window_id, element_id):
        uia = load_uia()
        try:
            from System import Array, Int32
        except Exception as exc:
            raise DesktopUnavailable(f"pythonnet is unusable: {exc}") from exc
        try:
            parts = Array[Int32]([int(n) for n in str(element_id).split(".")])
        except Exception:
            return None
        root = self._root(window_id)
        condition = _q(lambda: uia["PropertyCondition"](
            uia["AutomationElement"].RuntimeIdProperty, parts))
        if condition is None:
            return None
        if _q(lambda: ".".join(str(int(n)) for n in root.GetRuntimeId()),
              None) == str(element_id):
            return root
        # Identity lookup, never an index into the cached list: the list is a
        # snapshot and the tree is not.
        return _q(lambda: root.FindFirst(uia["TreeScope"].Descendants,
                                         condition))

    def invoke(self, handle):
        uia = load_uia()
        ok, pattern = handle.TryGetCurrentPattern(uia["InvokePattern"].Pattern)
        if not ok or pattern is None:
            return False
        pattern.Invoke()
        return True

    def get_value(self, handle):
        uia = load_uia()
        ok, pattern = handle.TryGetCurrentPattern(uia["ValuePattern"].Pattern)
        if not ok or pattern is None:
            return None
        return _q(lambda: pattern.Current.Value, None)

    def set_value(self, handle, text):
        uia = load_uia()
        ok, pattern = handle.TryGetCurrentPattern(uia["ValuePattern"].Pattern)
        if not ok or pattern is None:
            return False
        pattern.SetValue(text)
        return True

    def scroll_pattern(self, handle, amount, axis):
        uia = load_uia()
        ok, pattern = handle.TryGetCurrentPattern(uia["ScrollPattern"].Pattern)
        if not ok or pattern is None:
            return False
        none_amount = uia["ScrollAmount"].NoAmount
        step = getattr(uia["ScrollAmount"], amount)
        if axis == "vertical":
            pattern.Scroll(none_amount, step)
        else:
            pattern.Scroll(step, none_amount)
        return True

    def _to_client(self, hwnd, x, y):
        import win32gui
        try:
            return win32gui.ScreenToClient(hwnd, (int(x), int(y)))
        except Exception:
            return int(x), int(y)

    def post_click(self, window_id, x, y, button):
        import win32gui
        hwnd = self._hwnd(window_id)
        down, up = MOUSE_MESSAGES[button]
        # Button messages carry CLIENT coordinates; the element rect is in
        # screen space, so this conversion is not optional.
        cx, cy = self._to_client(hwnd, x, y)
        lparam = pack_lparam(cx, cy)
        # PostMessage, never SendInput/mouse_event: the cursor stays exactly
        # where Josh left it and the target window never comes forward.
        win32gui.PostMessage(hwnd, 0x0200, 0, lparam)          # WM_MOUSEMOVE
        win32gui.PostMessage(hwnd, down, 1 if button == "left" else 2, lparam)
        win32gui.PostMessage(hwnd, up, 0, lparam)
        return True

    def post_wheel(self, window_id, x, y, delta, horizontal):
        import win32gui
        hwnd = self._hwnd(window_id)
        message = WM_MOUSEHWHEEL if horizontal else WM_MOUSEWHEEL
        # Wheel messages carry SCREEN coordinates in lParam, unlike button
        # messages, which carry client ones. Getting this backwards scrolls
        # whatever happens to be under the wrong point.
        wparam = (int(delta) & 0xFFFF) << 16
        win32gui.PostMessage(hwnd, message, wparam,
                             pack_lparam(int(x), int(y)))
        return True

    def post_key(self, window_id, kind, vk, char=None):
        import win32api
        import win32gui
        hwnd = self._hwnd(window_id)
        scan = _q(lambda: win32api.MapVirtualKey(int(vk), 0), 0) or 0
        if kind == "char":
            win32gui.PostMessage(hwnd, 0x0102, ord(char or " "),
                                 1 | (scan << 16))            # WM_CHAR
        elif kind == "down":
            win32gui.PostMessage(hwnd, 0x0100, int(vk), 1 | (scan << 16))
        else:
            win32gui.PostMessage(hwnd, 0x0101, int(vk),
                                 1 | (scan << 16) | (1 << 30) | (1 << 31))
        return True


# ------------------------------------------------------- refusal machinery --

def _has(module):
    """Is this module importable? sys.modules FIRST, and that ordering is a bug fix.

    pythonnet replaces its own ``clr`` module at import time with one whose
    ``__spec__`` is None, so ``find_spec("clr")`` RAISES ValueError from that
    moment on. A find_spec-only probe therefore reported "pythonnet is
    missing" in the very process that had just walked a real window with it --
    the exact dead-button lie probe() exists to prevent, and invisible until
    something calls probe() AFTER the first use rather than before.
    """
    import importlib.util
    import sys
    if module in sys.modules:
        return True
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def alloy_pids():
    """Every pid that IS Alloy: this process, its ancestors, its children.

    Ancestors are included by pid only -- climbing to explorer.exe and
    claiming its whole subtree would refuse the entire desktop, which is a
    refusal nobody would keep. Descendants ARE claimed wholesale, because they
    are the seat CLIs by definition.
    """
    pids = {os.getpid()}
    try:
        import psutil
    except Exception:
        return pids
    try:
        me = psutil.Process()
    except Exception:
        return pids
    try:
        for child in me.children(recursive=True):
            pids.add(child.pid)
    except Exception:
        pass
    parent, hops = _q(lambda: me.parent()), 0
    while parent is not None and hops < 12:
        name = (_q(lambda p=parent: p.name(), "") or "").lower()
        if name in _SHELL_ROOTS:
            break
        pids.add(parent.pid)
        parent = _q(lambda p=parent: p.parent())
        hops += 1
    return pids


class _PidCache:
    """alloy_pids() with a 2 s memory. Seat CLIs come and go mid-run."""

    def __init__(self, source=None, clock=None):
        self._source = source or alloy_pids
        self._clock = clock or time.monotonic
        self._value, self._stamp, self._lock = None, -1e9, threading.Lock()

    def get(self):
        with self._lock:
            now = self._clock()
            if self._value is None or now - self._stamp > _PID_CACHE_TTL:
                try:
                    self._value = set(self._source())
                except Exception:
                    self._value = {os.getpid()}
                self._stamp = now
            return self._value


# ----------------------------------------------------------------- Desktop --

class Desktop:
    """The tool surface. One instance owns one observation store.

    ``backend`` is the test seam. ``self_pids`` overrides the self-approval
    pid set (tests hand it a literal set). ``deny_windows`` is the
    user-configurable deny-list: substrings matched case-insensitively against
    title, class name and exe path.
    """

    def __init__(self, backend=None, clock=None, self_pids=None,
                 deny_windows=None, ttl=OBSERVATION_TTL,
                 max_elements=MAX_ELEMENTS, strict_pixels=False):
        self._backend = backend if backend is not None else UiaBackend()
        self._clock = clock or time.monotonic
        self._pids = _PidCache(
            source=(lambda: self_pids) if self_pids is not None else None,
            clock=self._clock)
        self.deny_windows = [str(d).lower() for d in (deny_windows or [])]
        self.ttl = float(ttl)
        self.max_elements = int(max_elements)
        # strict_pixels defaults to FALSE, and that is the design, not laziness.
        # A raw-pixel hash refuses on a blinking caret, a spinner, a clock, an
        # antialiased focus ring -- i.e. on healthy behaviour. Alloy already
        # learned this once with the turn watchdog: a check that fires on
        # normal operation does not make people careful, it teaches them to
        # route around it, and then it protects nothing. The machinery stays,
        # opt-in, for the cases where a still frame really is the contract.
        self.strict_pixels = bool(strict_pixels)
        self._observations = OrderedDict()
        self._lock = threading.RLock()

    # ------------------------------------------------------------ policy ---

    def _forbid(self, window):
        """The reason this window may never be touched, or None.

        Applies to OBSERVERS too. Reading Alloy's own approval modal is how a
        seat learns to forge it, and reading somebody's password manager is
        exfiltration with a screenshot.
        """
        if not window:
            return None
        cls = (window.get("class_name") or "")
        exe = (window.get("exe") or "")
        base = os.path.basename(exe).lower()
        title = (window.get("title") or "")
        pid = window.get("pid")
        for prefix in WEBVIEW_CLASS_PREFIXES:
            if cls.startswith(prefix):
                return ("that window is a WebView2/Chromium host "
                        f"(class {cls}), which is what Alloy's own UI runs in. "
                        "A seat that can click Alloy's approval modal makes "
                        "every approval in this app forgeable, so the whole "
                        "class is refused -- there is no override.")
        if pid is not None and int(pid) in self._pids.get():
            return ("that window belongs to Alloy's own process tree "
                    f"(pid {pid}). Alloy may not drive Alloy -- there is no "
                    "override.")
        if base in SEAT_CONSOLE_EXES:
            return (f"that window is a seat CLI or a console ({base}). Driving "
                    "a terminal is an unlogged shell, and every seat already "
                    "has a shell -- there is no override.")
        for needle in self.deny_windows:
            if needle and (needle in title.lower() or needle in cls.lower()
                           or needle in exe.lower()):
                return (f"that window matches the deny-list entry {needle!r}. "
                        "Remove the entry to allow it.")
        return None

    def _require_allowed(self, window):
        reason = self._forbid(window)
        if reason:
            raise DesktopRefused("Refused: " + reason, code="SELF_APPROVAL")

    # ------------------------------------------------------- observation ---

    @staticmethod
    def _public_element(el):
        """Normalize + sanitize ONE element on the way to the model.

        This runs in Desktop, not in the backend, on purpose: sanitization is
        a property of the boundary, and a backend that forgot to call it (or a
        future one that never knew about it) must not be able to leak a token
        into a transcript. It also flattens whatever key soup a backend hands
        over, so a sloppy provider cannot inject fields into the output.
        """
        rect = el.get("rect") or {}
        out = {
            "element_id": str(el.get("element_id") or ""),
            "control_type": sanitize(el.get("control_type") or "Unknown", 64),
            "name": sanitize(el.get("name") or ""),
            "automation_id": sanitize(el.get("automation_id") or ""),
            "rect": {"x": int(rect.get("x") or 0), "y": int(rect.get("y") or 0),
                     "width": int(rect.get("width") or 0),
                     "height": int(rect.get("height") or 0)},
            "enabled": bool(el.get("enabled")),
            "patterns": [p for p in (el.get("patterns") or [])
                         if p in ("value", "invoke", "scroll")],
            "is_password": bool(el.get("is_password")),
            "depth": int(el.get("depth") or 0),
        }
        if el.get("class_name"):
            out["class_name"] = sanitize(el["class_name"])
        return out

    def _prune(self, raw):
        """Rank, then present in document order. DIVERGENCE from dsh-click.

        dsh-click walks depth-first and stops at the cap, so on a Chromium
        window (which blows a 500 cap several times over) the model receives
        the first 500 nodes of the DOM -- scaffolding -- and never sees a
        single button. Ranking picks the elements that can actually be acted
        on; re-sorting the survivors back into document order keeps the text
        render reading like the screen.
        """
        window_rect = raw.get("window_rect")
        kept = []
        for index, raw_el in enumerate(raw["elements"]):
            el = self._public_element(raw_el)
            if not el["element_id"]:
                continue               # no RuntimeId means no stable identity
            name = el.get("name") or ""
            patterns = el.get("patterns") or []
            ctype = el.get("control_type") or "Unknown"
            rect = el.get("rect") or {}
            visible = bool(rect.get("width")) and bool(rect.get("height"))
            if visible and window_rect:
                visible = rects_intersect(rect, window_rect)
            if not name and not patterns and ctype in CONTAINER_TYPES:
                continue                   # a nameless pane is scaffolding
            score = 0
            if visible:
                score += 4
            if patterns:
                score += 2
            if ctype in INTERACTIVE_TYPES:
                score += 2
            if name:
                score += 1
            if el.get("enabled"):
                score += 1
            kept.append((score, index, el))
        dropped_containers = len(raw["elements"]) - len(kept)
        kept.sort(key=lambda row: (-row[0], row[1]))
        truncated = len(kept) > self.max_elements
        kept = kept[:self.max_elements]
        kept.sort(key=lambda row: row[1])
        return ([row[2] for row in kept], truncated, dropped_containers)

    def _snapshot(self, window_id, want_pixels=True):
        """The whole truth about one window: identity + tree + pixels.

        Both observers and every mutator's verification go through here, which
        is what makes observation_id genuinely mean "this tree AND these
        pixels" rather than whichever one happened to be taken.
        """
        window = self._backend.window(window_id)
        if window is None:
            raise DesktopError(
                f"Window {window_id} no longer exists -- run app_list again.")
        self._require_allowed(window)
        window = dict(window)
        window["title"] = sanitize(window.get("title") or "")
        window["exe"] = sanitize(window.get("exe") or "", path=True)
        window["class_name"] = sanitize(window.get("class_name") or "")
        walked = self._backend.walk(window_id)
        walked = dict(walked)
        walked["window_rect"] = window.get("rect")
        elements, truncated, dropped = self._prune(walked)
        shot = None
        if want_pixels:
            try:
                shot = self._backend.capture(window_id)
            except Exception as exc:       # a capture failure is not fatal
                shot = {"png": None, "width": 0, "height": 0, "blank": True,
                        "raw": b"", "error": f"{type(exc).__name__}: {exc}"}
        raw_bytes = (shot or {}).get("raw") or b""
        shot_h = hashlib.sha256(raw_bytes).hexdigest()
        tree_h = tree_hash(elements)
        ident = identity_string(window)
        obs = {
            "observation_id": observation_id(ident, tree_h, shot_h),
            "window_id": window["window_id"],
            "window": window,
            "identity": ident,
            "elements": elements,
            "seen": walked.get("seen", len(elements)),
            "kept": len(elements),
            "truncated": bool(truncated),
            "dropped_containers": dropped,
            "hit_visit_cap": bool(walked.get("hit_visit_cap")),
            "tree_hash": tree_h,
            "shot_hash": shot_h,
            "shot": shot,
            "at": self._clock(),
            "wall": time.time(),
        }
        return obs

    def _remember(self, obs):
        with self._lock:
            self._observations[obs["observation_id"]] = obs
            self._observations.move_to_end(obs["observation_id"])
            while len(self._observations) > OBSERVATION_MAX:
                self._observations.popitem(last=False)
        return obs

    def _forget(self, obs_id):
        with self._lock:
            self._observations.pop(obs_id, None)

    # ----------------------------------------------------- verification ----

    def _refusal(self, code, message):
        return {"ok": False, "delivered": "none", "refusal": code,
                "message": message}

    def verify(self, based_on, strict_pixels=None):
        """(fresh_observation, refusal_or_None) for a cited observation.

        Takes a FULL re-snapshot. That is the expensive, correct choice: a
        cheap check (does the element still exist?) passes in exactly the case
        the whole mechanism exists for, which is another element having slid
        into that rectangle.
        """
        based_on = based_on or {}
        obs_id = str(based_on.get("observation_id") or "")
        window_id = str(based_on.get("window_id") or "")
        if not obs_id or not window_id:
            return None, self._refusal(
                UNKNOWN_OBSERVATION,
                "based_on needs both observation_id and window_id -- "
                + _REOBSERVE)
        with self._lock:
            old = self._observations.get(obs_id)
            if old is not None:
                self._observations.move_to_end(obs_id)
        if old is None:
            return None, self._refusal(
                UNKNOWN_OBSERVATION,
                f"Observation {obs_id} is not on record (at most "
                f"{OBSERVATION_MAX} are kept, newest first) -- " + _REOBSERVE)
        if old["window_id"] != window_id:
            return None, self._refusal(
                UNKNOWN_OBSERVATION,
                f"Observation {obs_id} was taken of window "
                f"{old['window_id']}, not {window_id} -- " + _REOBSERVE)
        age = self._clock() - old["at"]
        if age > self.ttl:
            return None, self._refusal(
                EXPIRED,
                f"Observation {obs_id} is {age:.0f}s old and the limit is "
                f"{self.ttl:.0f}s -- " + _REOBSERVE)
        strict = self.strict_pixels if strict_pixels is None else bool(
            strict_pixels)
        try:
            fresh = self._snapshot(window_id, want_pixels=strict)
        except DesktopRefused:
            raise
        except DesktopError as exc:
            return None, self._refusal(STALE_IDENTITY, f"{exc} " + _REOBSERVE)
        if fresh["identity"] != old["identity"]:
            return fresh, self._refusal(
                STALE_IDENTITY,
                f"Window {window_id} is not the window observation {obs_id} "
                f"was taken of (was {old['identity']!r}, is now "
                f"{fresh['identity']!r}) -- " + _REOBSERVE)
        if fresh["tree_hash"] != old["tree_hash"]:
            return fresh, self._refusal(
                STALE_TREE,
                f"The accessibility tree of {window_id} changed since "
                f"observation {obs_id} was taken -- " + _REOBSERVE)
        if strict and fresh["shot_hash"] != old["shot_hash"]:
            return fresh, self._refusal(
                STALE_PIXELS,
                f"The pixels of {window_id} changed since observation "
                f"{obs_id} was taken -- " + _REOBSERVE)
        return fresh, None

    # ---------------------------------------------------------- observers --

    def app_list(self):
        """Every visible top-level window, with WHY the refused ones are.

        Refused windows are listed rather than hidden: a model that cannot see
        Alloy's window will keep hunting for it, and "you may not" is a much
        better answer than an empty list.
        """
        windows = []
        for raw in self._backend.list_windows():
            info = {
                "window_id": raw.get("window_id"),
                "pid": raw.get("pid"),
                "exe": sanitize(raw.get("exe") or "", path=True),
                "title": sanitize(raw.get("title") or ""),
                "class_name": sanitize(raw.get("class_name") or ""),
                "rect": raw.get("rect") or {},
            }
            reason = self._forbid(raw)
            info["controllable"] = reason is None
            if reason:
                info["refusal"] = "Refused: " + reason
            windows.append(info)
        return {"ok": True, "windows": windows, "count": len(windows),
                "text": render_windows(windows)}

    def screen_read(self, window_id):
        """The accessibility tree of one window. Free, and cites nothing."""
        obs = self._remember(self._snapshot(window_id, want_pixels=True))
        return self._observation_result(obs, include_image=False)

    def screen_shot(self, window_id):
        """A PNG of one window, occluded or not, plus the same observation id.

        Uses PrintWindow(PW_RENDERFULLCONTENT), never CopyFromScreen: a window
        a seat is working on is normally behind something, and a screen grab
        would return whatever is in front of it.
        """
        obs = self._remember(self._snapshot(window_id, want_pixels=True))
        return self._observation_result(obs, include_image=True)

    def _observation_result(self, obs, include_image):
        shot = obs.get("shot") or {}
        note = []
        if obs["truncated"]:
            note.append(
                f"Showing the {obs['kept']} most actionable of "
                f"{obs['seen']} elements seen (ranked by whether they are "
                "visible, have a pattern and have a name); the rest were "
                "dropped, not hidden.")
        if obs["hit_visit_cap"]:
            note.append(f"The walk stopped at the {MAX_VISIT}-node visit cap, "
                        "so parts of this window were never reached.")
        result = {
            "ok": True,
            "observation_id": obs["observation_id"],
            "window_id": obs["window_id"],
            "window": obs["window"],
            "elements": obs["elements"],
            "seen": obs["seen"],
            "kept": obs["kept"],
            "truncated": obs["truncated"],
            "tree_hash": obs["tree_hash"],
            "shot_hash": obs["shot_hash"],
            "captured_at": obs["wall"],
            "expires_in": self.ttl,
        }
        if include_image:
            if shot.get("blank") or not shot.get("png"):
                result["image_png"] = None
                result["blank"] = True
                detail = shot.get("error")
                note.append(
                    "The screenshot came back blank"
                    + (f" ({detail})" if detail else
                       " -- this renderer does not honour "
                       "PrintWindow(PW_RENDERFULLCONTENT)")
                    + ", so no image is returned rather than a black frame. "
                      "Use screen_read for this window.")
            else:
                result["image_png"] = shot["png"]
                result["blank"] = False
                result["width"] = shot.get("width")
                result["height"] = shot.get("height")
        result["note"] = " ".join(note)
        result["text"] = render_observation(obs, note=result["note"])
        return result

    # ---------------------------------------------------------- mutators ---

    def _cited_element(self, fresh, element_id):
        for el in fresh["elements"]:
            if el["element_id"] == element_id:
                return el
        return None

    def _bracket(self, window_id, action):
        """Run `action` between two reads of the window's process identity.

        DIVERGENCE from dsh-click, whose check reads "before" and "after"
        back-to-back AFTER the action already ran -- it compares a value with
        itself and can never fail. Bracketing is the entire point: a window
        handle that changed process mid-action is a different program holding
        the click.
        """
        before = self._backend.process_identity(window_id)
        try:
            outcome = action()
        finally:
            after = self._backend.process_identity(window_id)
        return outcome, (before == after), before, after

    def _acted(self, result, obs_id, stable, before, after):
        result["process_stable"] = bool(stable)
        if not stable:
            result["warning"] = (
                f"The window changed process during the action (was {before}, "
                f"is now {after}) -- the click may have gone to a different "
                "program. Verify before trusting it.")
        # The observation is consumed on purpose: whatever just happened
        # changed the window, so the cited snapshot is now a lie. Making the
        # model re-observe costs one call and removes a whole class of
        # "acted twice on a tree that only existed once".
        self._forget(obs_id)
        result["observation_consumed"] = obs_id
        tail = ("The action changed this window, so observation "
                f"{obs_id} has been discarded -- run screen_read again "
                "before the next action.")
        result["note"] = (result.get("note", "") + " " + tail).strip()
        return result

    def click(self, based_on, element_id=None, x=None, y=None, button="left",
              strict_pixels=None):
        """Click a cited element, or a point inside the cited window.

        InvokePattern first (``delivered="uia"``), a posted button pair second
        (``delivered="posted"``). Never SendInput, never SetForegroundWindow --
        Josh keeps his cursor.
        """
        button = str(button or "left").lower()
        if button not in MOUSE_MESSAGES:
            raise DesktopError(
                f"Unknown button {button!r}; use 'left' or 'right'.")
        fresh, refusal = self.verify(based_on, strict_pixels)
        if refusal:
            return refusal
        obs_id = based_on["observation_id"]
        window_id = fresh["window_id"]
        target = None
        if element_id:
            target = self._cited_element(fresh, element_id)
            if target is None:
                return self._refusal(
                    ELEMENT_GONE,
                    f"Element {element_id} is not in window {window_id} any "
                    "more -- " + _REOBSERVE)
            point = rect_centre(target["rect"])
        else:
            if x is None or y is None:
                raise DesktopError(
                    "click needs either element_id or both x and y.")
            point = (int(x), int(y))
            # There is no global-desktop click primitive, on purpose: a click
            # that can land anywhere is a click nobody cited.
            if not point_in_rect(point[0], point[1], fresh["window"]["rect"]):
                raise DesktopRefused(
                    f"({point[0]}, {point[1]}) is outside window {window_id} "
                    f"{fresh['window']['rect']}. Every click must land inside "
                    "the window it cites; there is no desktop-wide click.",
                    code="OUT_OF_WINDOW")

        def act():
            if target is not None and "invoke" in (target.get("patterns") or []):
                handle = self._backend.resolve(window_id, element_id)
                if handle is None:
                    return "gone"
                try:
                    if self._backend.invoke(handle):
                        return "uia"
                except Exception:
                    pass                   # fall through to the posted path
            self._backend.post_click(window_id, point[0], point[1], button)
            return "posted"

        delivered, stable, before, after = self._bracket(window_id, act)
        if delivered == "gone":
            return self._refusal(
                ELEMENT_GONE,
                f"Element {element_id} could not be re-resolved in window "
                f"{window_id} -- " + _REOBSERVE)
        result = {"ok": True, "delivered": delivered, "window_id": window_id,
                  "element_id": element_id, "point": {"x": point[0],
                                                      "y": point[1]},
                  "button": button}
        if delivered == "posted":
            result["note"] = (
                "Delivered as a posted message to the window's queue. Posted "
                "input is best-effort: Chromium, WPF and UWP surfaces often "
                "ignore synthetic messages while unfocused, and nothing here "
                "confirms receipt.")
        return self._acted(result, obs_id, stable, before, after)

    def type_text(self, based_on, element_id, text, allow_password=False,
                  strict_pixels=None):
        """Set a cited field's value through ValuePattern, with rollback.

        Refuses outright when the element has no ValuePattern. DIVERGENCE from
        the obvious fallback: focusing the field and posting characters would
        type into whatever actually has focus if the focus call lost, which is
        how a password ends up in a chat window.
        """
        if not element_id:
            raise DesktopError("type_text needs the element_id of a field.")
        text = "" if text is None else str(text)
        fresh, refusal = self.verify(based_on, strict_pixels)
        if refusal:
            return refusal
        obs_id = based_on["observation_id"]
        window_id = fresh["window_id"]
        target = self._cited_element(fresh, element_id)
        if target is None:
            return self._refusal(
                ELEMENT_GONE,
                f"Element {element_id} is not in window {window_id} any more "
                "-- " + _REOBSERVE)
        if target.get("is_password") and not allow_password:
            # The seat can only type a secret it was GIVEN, which means the
            # secret is already in the prompt and therefore in the transcript,
            # the session log and every seat's context. Refusing by default
            # makes that a deliberate choice instead of an accident.
            return self._refusal(
                PASSWORD_FIELD,
                f"Element {element_id} is a password field. A seat can only "
                "type a secret it was given, so that secret is already in the "
                "prompt and therefore in this session's transcript. Pass "
                "allow_password=True only if that is acceptable.")
        if "value" not in (target.get("patterns") or []):
            return self._refusal(
                NO_VALUE_PATTERN,
                f"Element {element_id} ({target.get('control_type')}) has no "
                "value pattern, and typing into whatever happens to have "
                "focus instead would be a guess. Cite an element whose "
                "patterns include 'value'.")
        handle = self._backend.resolve(window_id, element_id)
        if handle is None:
            return self._refusal(
                ELEMENT_GONE,
                f"Element {element_id} could not be re-resolved in window "
                f"{window_id} -- " + _REOBSERVE)
        original = None
        try:
            original = self._backend.get_value(handle)
        except Exception:
            original = None

        def act():
            return self._backend.set_value(handle, text)

        try:
            ok, stable, before, after = self._bracket(window_id, act)
        except Exception as exc:
            raise DesktopError(self._rollback_message(handle, original, exc))
        if not ok:
            return self._refusal(
                NO_VALUE_PATTERN,
                f"Element {element_id} refused a value at action time -- "
                + _REOBSERVE)
        result = {"ok": True, "delivered": "uia", "window_id": window_id,
                  "element_id": element_id, "length": len(text),
                  "previous_length": len(original or "")
                  if original is not None else None}
        return self._acted(result, obs_id, stable, before, after)

    def _rollback_message(self, handle, original, exc):
        """Try to put the old value back, then say plainly whether it worked."""
        if original is None:
            return (f"Typing failed ({type(exc).__name__}: {exc}). The field's "
                    "original value could not be read beforehand, so NO "
                    "restore was attempted -- the field may hold partial "
                    "text. Read it with screen_read before acting again.")
        try:
            self._backend.set_value(handle, original)
        except Exception as restore_exc:
            return (f"Typing failed ({type(exc).__name__}: {exc}). The "
                    "original value was NOT restored "
                    f"({type(restore_exc).__name__}: {restore_exc}) -- the "
                    "field may hold partial text. Read it with screen_read "
                    "before acting again.")
        return (f"Typing failed ({type(exc).__name__}: {exc}). The original "
                "value was restored. Read the field with screen_read before "
                "acting again.")

    def scroll(self, based_on, direction="down", notches=1, element_id=None,
               strict_pixels=None):
        """ScrollPattern first, a posted wheel second. Both agree on direction.

        The agreement is not a comment, it is a shared table: see
        SCROLL_DIRECTIONS, scroll_amount and wheel_delta.
        """
        amount, axis = scroll_amount(direction)
        delta, horizontal = wheel_delta(direction, notches)
        fresh, refusal = self.verify(based_on, strict_pixels)
        if refusal:
            return refusal
        obs_id = based_on["observation_id"]
        window_id = fresh["window_id"]
        target = None
        if element_id:
            target = self._cited_element(fresh, element_id)
            if target is None:
                return self._refusal(
                    ELEMENT_GONE,
                    f"Element {element_id} is not in window {window_id} any "
                    "more -- " + _REOBSERVE)
            point = rect_centre(target["rect"])
        else:
            point = rect_centre(fresh["window"]["rect"])

        def act():
            if target is not None and "scroll" in (target.get("patterns") or []):
                handle = self._backend.resolve(window_id, element_id)
                if handle is not None:
                    try:
                        if self._backend.scroll_pattern(handle, amount, axis):
                            return "uia"
                    except Exception:
                        pass
            steps = max(1, int(notches or 1))
            for _ in range(steps):
                self._backend.post_wheel(window_id, point[0], point[1],
                                         delta // steps, horizontal)
            return "posted"

        delivered, stable, before, after = self._bracket(window_id, act)
        result = {"ok": True, "delivered": delivered, "window_id": window_id,
                  "direction": direction, "notches": max(1, int(notches or 1)),
                  "axis": axis, "amount": amount, "delta": delta,
                  "element_id": element_id}
        return self._acted(result, obs_id, stable, before, after)

    def key(self, based_on, keys, strict_pixels=None):
        """Post a keystroke or chord to the cited window's queue.

        Modifiers go down in order and come up in REVERSE, which is the only
        ordering a receiver can unwind correctly.
        """
        mods, main = parse_keys(keys)
        fresh, refusal = self.verify(based_on, strict_pixels)
        if refusal:
            return refusal
        obs_id = based_on["observation_id"]
        window_id = fresh["window_id"]
        char = key_char(main, mods)

        def act():
            for name in mods:
                self._backend.post_key(window_id, "down", VK[name])
            self._backend.post_key(window_id, "down", VK[main])
            if char is not None:
                self._backend.post_key(window_id, "char", VK[main], char)
            self._backend.post_key(window_id, "up", VK[main])
            for name in reversed(mods):
                self._backend.post_key(window_id, "up", VK[name])
            return "posted"

        delivered, stable, before, after = self._bracket(window_id, act)
        result = {"ok": True, "delivered": delivered, "window_id": window_id,
                  "keys": "+".join(mods + [main]), "modifiers": list(mods),
                  "key": main,
                  "note": ("Delivered as posted messages to the window's "
                           "queue. Posted input is best-effort: Chromium, WPF "
                           "and UWP surfaces often ignore synthetic key "
                           "messages while unfocused, and an Alt chord in "
                           "particular usually needs real focus.")}
        return self._acted(result, obs_id, stable, before, after)


# ----------------------------------------------------------- text renderer --

def render_element(el):
    rect = el.get("rect") or {}
    line = "- [{}] {} \"{}\" at ({}, {}) {}x{}".format(
        el.get("element_id", "?"), el.get("control_type", "Unknown"),
        el.get("name", ""), rect.get("x", 0), rect.get("y", 0),
        rect.get("width", 0), rect.get("height", 0))
    if not el.get("enabled", True):
        line += " (disabled)"
    if el.get("is_password"):
        line += " (password)"
    patterns = el.get("patterns") or []
    if patterns:
        line += " patterns: " + ",".join(patterns)
    return line


def render_observation(obs, note=""):
    """The text twin of the dict. This is what makes a text-only model
    first-class here rather than a second-class citizen holding a PNG."""
    window = obs.get("window") or {}
    rect = window.get("rect") or {}
    lines = [
        "Window {} \"{}\" [{}] pid {} at ({}, {}) {}x{}".format(
            obs.get("window_id"), window.get("title", ""),
            window.get("class_name", ""), window.get("pid"),
            rect.get("x", 0), rect.get("y", 0),
            rect.get("width", 0), rect.get("height", 0)),
        "{} element(s) shown of {} seen{}".format(
            obs.get("kept", 0), obs.get("seen", 0),
            " (TRUNCATED)" if obs.get("truncated") else ""),
        "",
    ]
    lines.extend(render_element(el) for el in obs.get("elements", []))
    lines.append("")
    if note:
        lines.append(note)
    lines.append(
        "To act, cite observation_id=\"{}\" and window_id=\"{}\" in based_on. "
        "It expires in {:.0f}s and any change to this window invalidates "
        "it.".format(obs.get("observation_id"), obs.get("window_id"),
                     OBSERVATION_TTL))
    return "\n".join(lines)


def render_windows(windows):
    lines = ["{} window(s)".format(len(windows)), ""]
    for win in windows:
        rect = win.get("rect") or {}
        line = "- [{}] \"{}\" ({}) pid {} at ({}, {}) {}x{}".format(
            win.get("window_id"), win.get("title", ""),
            win.get("class_name", ""), win.get("pid"),
            rect.get("x", 0), rect.get("y", 0),
            rect.get("width", 0), rect.get("height", 0))
        if not win.get("controllable", True):
            line += "  REFUSED"
        lines.append(line)
        if win.get("refusal"):
            lines.append("    " + win["refusal"])
    lines.append("")
    lines.append("Call screen_read(window_id) on one of the non-refused "
                 "windows to get an observation_id.")
    return "\n".join(lines)


# ------------------------------------------------------------------ probe ---

def probe(system=None, backend=None):
    """What desktop control can do on THIS machine, and why not if not.

    Same honesty contract as dictation.probe(): name the missing piece, never
    return a bare False that leaves the UI showing a dead button. `system` is
    the test seam for the non-Windows verdict, which must be a clean answer
    and never an AttributeError out of a missing ctypes.windll.

    Loading the UIAutomation assemblies costs ~0.2-0.5 s the first time and is
    cached afterwards, so this is cheap to call repeatedly but should not run
    on a UI thread the first time. That is a deliberate difference from
    speaker.probe(): "pythonnet is installed" and "the assemblies actually
    load" are different facts, and only the second one is worth reporting.
    """
    name = system or platform.system()
    info = {"available": False, "system": name, "backend": "uia",
            "reason": "", "pieces": {}}
    if name != "Windows":
        info["reason"] = (
            "Desktop control drives the Windows UI Automation tree, which "
            f"only exists on Windows (this is {name}). macOS AXUIElement and "
            "Linux AT-SPI are deliberately out of scope for this module.")
        return info
    for label, module in (("pywin32", "win32gui"), ("pythonnet", "clr"),
                          ("Pillow", "PIL"), ("psutil", "psutil")):
        info["pieces"][label] = _has(module)
    missing = [label for label, ok in info["pieces"].items() if not ok]
    if missing:
        info["reason"] = ("Desktop control needs " + ", ".join(missing)
                          + " (pip install " + " ".join(missing) + ").")
        return info
    try:
        ok, reason = (backend or UiaBackend()).available()
    except Exception as exc:
        ok, reason = False, f"{type(exc).__name__}: {exc}"
    info["pieces"]["UIAutomation"] = bool(ok)
    if not ok:
        info["reason"] = reason
        return info
    info["available"] = True
    info["reason"] = ("Desktop control is ready (UI Automation tree + "
                      "PostMessage input; the cursor never moves).")
    return info


# ----------------------------------------------------- module-level surface --

_DEFAULT = None
_DEFAULT_LOCK = threading.Lock()


def default_desktop():
    """The process-wide instance the module-level functions share."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = Desktop()
        return _DEFAULT


def app_list():
    return default_desktop().app_list()


def screen_read(window_id):
    return default_desktop().screen_read(window_id)


def screen_shot(window_id):
    return default_desktop().screen_shot(window_id)


def click(based_on, **kwargs):
    return default_desktop().click(based_on, **kwargs)


def type_text(based_on, element_id, text, **kwargs):
    return default_desktop().type_text(based_on, element_id, text, **kwargs)


def scroll(based_on, **kwargs):
    return default_desktop().scroll(based_on, **kwargs)


def key(based_on, keys, **kwargs):
    return default_desktop().key(based_on, keys, **kwargs)
