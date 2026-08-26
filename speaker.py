"""Speaker -- read-aloud of agent replies through Windows SAPI, no cloud TTS.

The app's identity is "no API keys, no accounts", so text-to-speech here is
the synthesizer Windows already ships: System.Speech, driven through a short
child PowerShell process. Unlike dictation's Wispr Flow seam there is nothing
to refuse by name -- SAPI is local, free and offline, which makes it the only
kind of voice this project was ever going to ship.

Three rules shape this file, mirroring dictation.py:

 1. **The text never touches a command line.** Interpolating a reply into a
    -Command string is injection with extra steps (a quote or a subexpression
    in some agent's prose becomes code), and Windows caps a command line near
    32,767 chars anyway. The script carries only the decoding machinery; the
    reply rides stdin as base64 UTF-8 and is decoded inside PowerShell. The
    base64 payload is pure ASCII besides, so the console-codepage gotcha
    (PowerShell reads BOM-less text as ANSI) can never bite it either.

 2. **Latest wins, and stopping is killing.** Read-aloud narrates a live
    conversation; nobody wants a queue reading three messages behind it. A new
    speak() first reaps the previous child; stop() does the same and is safe
    when idle -- the same rule as dictation's stop-with-no-start, because a UI
    button must never punish a second click.

 3. **The speaking flag is derived, never remembered.** `speaking` asks the
    live child whether it is still running instead of trusting a boolean some
    code path forgot to clear, so no exception anywhere can leave it lying.

Gotchas worth keeping: spawning PowerShell costs roughly 200 ms per utterance
(cold start + Add-Type), which is fine for read-aloud and buys enormous
simplicity -- there is no persistent host process to babysit or reap. Every
public method is best-effort and swallows unexpected exceptions, but state
stays consistent because it is derived. Nothing here imports relay, app or
webview.

The runner seam (`Speaker(runner=...)`) stands in for subprocess.Popen the
same way Recorder(stream_factory=...) stands in for PortAudio: tests inject
fakes, and no test ever launches PowerShell unless it opts in explicitly.
"""

import base64
import platform
import shutil
import subprocess
import threading
import unicodedata

# Replies are narration, not audiobooks: 4000 chars is several minutes of
# speech and keeps the base64 payload (about 5.5 KB) comfortably sane for a
# single stdin write.
MAX_CHARS = 4000

# Grace given to a terminated child before escalating to kill, in seconds.
_REAP_GRACE = 0.5

# One line on purpose: powershell -Command receives this as ONE argv element,
# so it must contain no interpolation of anything. Pure ASCII. Speak() is
# synchronous inside the child, so the child EXITING is the signal that the
# utterance finished -- exactly what the `speaking` property polls.
_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "Add-Type -AssemblyName System.Speech;"
    "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "$p=[Console]::In.ReadToEnd();"
    "$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p.Trim()));"
    "[void]$s.Speak($t);"
    "$s.Dispose()"
)

# subprocess.CREATE_NO_WINDOW only exists on Windows; getattr keeps this
# module importable (and probe-testable) elsewhere. Hidden window + DEVNULL
# output pipes is the house shape for background subprocesses. stdin stays
# PIPE deliberately: it IS the delivery channel (see rule 1).
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _default_runner(script):
    """Spawn the SAPI host. Returns a Popen-like object with stdin=PIPE."""
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW)


def _sanitize(text):
    """Strip control characters and cap length. Returns "" when nothing to say.

    Tab and newline survive (SAPI reads them as pauses); every other control
    character -- C0, C1, DEL, escape sequences -- is dropped via its Unicode
    category, so an agent reply containing terminal codes cannot reach the
    synthesizer. The cap is applied silently: a clipped sentence beats a
    refused one for read-aloud, same spirit as the activity-text caps.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    kept = [ch for ch in text if ch in "\n\t"
            or unicodedata.category(ch) != "Cc"]
    return "".join(kept)[:MAX_CHARS]


def _feed(proc, token):
    """Deliver the base64 payload, then close stdin so ReadToEnd() returns.

    Runs on a daemon thread because a full pipe blocks until the (slow to
    start) child reads -- and that must never stall speak() or stop().
    Touches nothing shared: a broken pipe here means the child lost the race
    against a stop()/speak() that replaced it, and the swallow-everything
    rule keeps this thread from ever surfacing that.
    """
    try:
        proc.stdin.write(token.encode("ascii"))
        proc.stdin.flush()
    except Exception:
        pass                                 # teardown is best-effort
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


def _reap(proc):
    """Terminate a child, escalating to kill if it ignores the grace window.

    Runs on a daemon thread; best-effort like Recorder._shutdown. On Windows
    terminate IS kill (TerminateProcess), so the escalation mostly matters
    for fakes and for the day this code runs somewhere POSIX-flavoured.
    """
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(_REAP_GRACE)
    except Exception:
        pass
    try:
        if proc.poll() is None:              # still alive after terminate?
            proc.kill()
            proc.wait(_REAP_GRACE * 2)
    except Exception:
        pass


class Speaker:
    """Read text aloud via Windows SAPI. Latest utterance wins.

    ``runner`` is the test seam: a callable(script) -> process-like object
    carrying ``poll()``, ``terminate()``, ``kill()``, ``wait(timeout)`` and a
    ``stdin`` with ``write``/``flush``/``close``. The default spawns hidden
    PowerShell; injected fakes keep every test offline and hardware-free.

    Locking: one lock guarding only the current-process slot. Feeder and
    reaper threads capture their process before starting and touch nothing
    shared afterwards, so no lock is ever held across a write or a wait.
    """

    def __init__(self, runner=None):
        self._runner = runner or _default_runner
        self._lock = threading.Lock()
        self._proc = None
        self._daemons = []                   # feeder/reaper threads, pruned as they die

    # ------------------------------------------------------------ public --

    def speak(self, text):
        """Speak `text` now, replacing whatever is currently being spoken.

        Returns True when an utterance started, False for empty text or a
        failed spawn. On a failed spawn any earlier utterance simply keeps
        playing -- a replacement that could not start must not silence the
        one that works. Setup is synchronous and fast (Popen returns at once)
        so `speaking` is truthful by the time this returns; the blocking
        stdin write rides the daemon feeder thread instead of the caller.
        """
        payload = _sanitize(text)
        if not payload:
            return False
        token = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        try:
            proc = self._runner(_SCRIPT)
        except Exception:
            return False                     # best-effort; silence, not a crash
        # Swap under the lock, reap the loser outside it. Whoever holds the
        # lock last IS the latest speaker, and each swap reaps only the proc
        # it captured as `old` -- never its successor's.
        with self._lock:
            old, self._proc = self._proc, proc
        if old is not None:
            self._spawn(_reap, old)
        self._spawn(_feed, proc, token)
        return True

    def stop(self):
        """Interrupt the current utterance now. Idle-safe, never raises.

        A stop with nothing running is False, not an error -- the UI's stop
        button can be clicked twice, exactly like dictation's pointerup can
        land after a drag-away. Returns True when there was a run to stop.
        """
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is None:
            return False
        self._spawn(_reap, proc)
        return True

    @property
    def speaking(self):
        """True while a child SAPI process is alive. Derived, never stale."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:
            return False                     # an unreadable process is not claimed

    # --------------------------------------------------------- internals --

    def _spawn(self, target, *args):
        thread = threading.Thread(target=target, args=args, daemon=True)
        with self._lock:
            self._daemons = [t for t in self._daemons if t.is_alive()]
            self._daemons.append(thread)
        thread.start()

    def _drain_workers(self, timeout=5.0):
        """Join live feeder/reaper threads. Test convenience, not production."""
        with self._lock:
            threads = list(self._daemons)
        for thread in threads:
            thread.join(timeout)


def probe():
    """Whether read-aloud can work on THIS machine, and why not if not.

    Deliberately cheap and side-effect free: it checks the OS and that
    powershell exists on PATH, and NEVER launches SAPI just to answer -- the
    same discipline as the Accounts panel distinguishing not_installed from
    signed_out instead of guessing. Probing from a boot path costs nothing.
    """
    if platform.system() != "Windows":
        return {"available": False,
                "detail": "Read-aloud uses Windows SAPI, which only exists "
                          "on Windows."}
    if shutil.which("powershell") is None:
        return {"available": False,
                "detail": "Windows PowerShell (powershell.exe) was not found "
                          "on PATH."}
    return {"available": True,
            "detail": "Read-aloud is ready (local Windows SAPI)."}
