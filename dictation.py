"""Dictation — a microphone in the composer, with no API key anywhere.

Josh asked whether Wispr Flow could be added natively. It cannot be embedded:
the only two surfaces Wispr ships are the desktop app (system-wide keystroke
injection — nothing an app can "add") and the Flow API, which is gated behind
an enterprise approval plus a billed API key. This repo's first line is "No API
keys anywhere", so the engine here is a LOCAL one, and Flow is left as a seam
rather than a dependency (see ``WisprFlowTranscriber``).

Three rules shape this file:

 1. **Capture happens in Python, not in the WebView.** pywebview 6.2.1's
    edgechromium backend has no ``PermissionRequested`` handling at all (grep
    its ``platforms/edgechromium.py`` — only the Qt backend mentions
    permissions), so ``getUserMedia`` inside the window is a coin flip. A
    Python-side stream also hands us exactly the 16 kHz mono int16 buffer that
    BOTH faster-whisper and the documented Flow API want, so the seam below is
    real rather than aspirational.
 2. **Never forge text.** A silent recording, a failed model load, a missing
    microphone — none of them may produce words. They return "" and the caller
    says so out loud. This is the same rule as the loop's never-forge-a-turn:
    invented content is worse than a visible failure, and dictation feeds
    straight into a prompt that three CLIs will act on.
 3. **Degrade honestly and by name.** ``probe()`` reports WHICH piece is
    missing, so the UI can say "sounddevice isn't installed" instead of showing
    a mic button that does nothing — the same distinction the Accounts panel
    draws between not_installed and signed_out.

Nothing here imports relay, app or webview: this module is importable, and
testable, on its own.
"""

import os
import threading

# 16 kHz mono 16-bit PCM is not a preference, it is the intersection of what
# faster-whisper accepts as a bare array and what the Flow API documents.
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
BYTES_PER_FRAME = 2

DEFAULT_MODEL = "base.en"
MODEL_ENV = "ALLOY_WHISPER_MODEL"
# A stuck recorder (a pointerup lost to a dragged-away cursor, say) must not eat
# RAM until the app dies: 16 kHz * 2 bytes = 32 KB/s, so 10 minutes ~= 19 MB.
MAX_SECONDS = 600
# Below this there is no speech to find, only a mis-click.
MIN_SECONDS = 0.25


class DictationUnavailable(RuntimeError):
    """A piece of the stack is missing or refused. The message names the fix."""


# ---------------------------------------------------------------- capture ---

def _sounddevice_stream(sample_rate, on_frames):
    """Open a raw 16 kHz mono int16 input stream. Raises DictationUnavailable.

    RawInputStream hands the callback a buffer we can turn straight into bytes,
    which keeps numpy out of the audio thread entirely.
    """
    try:
        import sounddevice
    except Exception as exc:                     # not installed, or no PortAudio
        raise DictationUnavailable(
            "Microphone capture needs the sounddevice package "
            "(pip install sounddevice).") from exc

    def _cb(indata, frames, time_info, status):  # runs on PortAudio's thread
        try:
            on_frames(bytes(indata))
        except Exception:
            pass                                 # an audio callback may never raise

    try:
        return sounddevice.RawInputStream(
            samplerate=sample_rate, channels=CHANNELS, dtype=DTYPE, callback=_cb)
    except Exception as exc:
        raise DictationUnavailable(
            f"Could not open the microphone: {exc}") from exc


class Recorder:
    """Start/stop microphone capture, returning raw PCM bytes.

    ``stream_factory`` is the test seam: it takes ``(sample_rate, on_frames)``
    and returns an object with ``start()``/``stop()``/``close()``, so the whole
    state machine is exercisable without PortAudio or a microphone.
    """

    def __init__(self, sample_rate=SAMPLE_RATE, max_seconds=MAX_SECONDS,
                 stream_factory=None):
        self.sample_rate = int(sample_rate)
        self.max_frames = max(1, int(self.sample_rate * max_seconds))
        self._factory = stream_factory or _sounddevice_stream
        self._lock = threading.Lock()
        self._stream = None
        self._starting = False
        self._abandoned = False
        self._chunks = []
        self._frames = 0
        self.truncated = False

    @property
    def recording(self):
        with self._lock:
            return self._stream is not None or self._starting

    def _on_frames(self, data):
        if not data:
            return
        with self._lock:
            room = self.max_frames - self._frames
            if room <= 0:
                self.truncated = True
                return
            frames = len(data) // BYTES_PER_FRAME
            if frames > room:                    # clip, don't just stop after
                data = data[:room * BYTES_PER_FRAME]
                frames = room
                self.truncated = True
            self._chunks.append(data)
            self._frames += frames

    @staticmethod
    def _shutdown(stream):
        if stream is None:
            return
        for step in ("stop", "close"):
            try:
                getattr(stream, step)()
            except Exception:
                pass                             # teardown is best-effort

    def start(self):
        """Begin capturing. Raises DictationUnavailable if already recording.

        Returns False when a stop/cancel landed WHILE the device was opening —
        a real race, because opening PortAudio takes long enough for a quick
        tap to finish first, and the loser of that race must not leave a live
        stream feeding a buffer nobody will ever read.
        """
        with self._lock:
            if self._stream is not None or self._starting:
                raise DictationUnavailable("Already recording.")
            self._starting = True
            self._abandoned = False
            self._chunks, self._frames, self.truncated = [], 0, False
        try:
            stream = self._factory(self.sample_rate, self._on_frames)
            stream.start()
        except Exception:
            with self._lock:
                self._starting = self._abandoned = False
            raise
        with self._lock:
            self._starting = False
            stale, self._abandoned = self._abandoned, False
            if not stale:
                self._stream = stream
        if stale:
            self._shutdown(stream)
            with self._lock:
                self._chunks, self._frames = [], 0
            return False
        return True

    def _finish(self, keep):
        with self._lock:
            stream, self._stream = self._stream, None
            if self._starting:                   # the device is still opening
                self._abandoned = True
            self._starting = False
            chunks, self._chunks = self._chunks, []
            self._frames = 0
        self._shutdown(stream)
        return b"".join(chunks) if keep else b""

    def stop(self):
        """Stop and return the captured PCM. A stop with no start returns empty.

        Deliberately not an error: the UI's hold-to-talk can lose its pointerup
        to a dragged-away cursor, and punishing that with an exception buys
        nothing.
        """
        return self._finish(True)

    def cancel(self):
        """Stop and throw the audio away (Escape while holding)."""
        self._finish(False)
        return b""


def pcm_seconds(pcm16, sample_rate=SAMPLE_RATE):
    return len(pcm16) / float(BYTES_PER_FRAME * max(1, int(sample_rate)))


# ----------------------------------------------------------- transcribers ---

class Transcriber:
    """One method, so a new engine is one class and one registry entry."""

    name = "base"
    label = "Transcriber"

    def transcribe(self, pcm16, sample_rate=SAMPLE_RATE):
        raise NotImplementedError

    def warm(self):
        """Optional: pay the load cost before the first real recording."""
        return None


def _pcm16_to_float32(pcm16):
    import numpy
    usable = len(pcm16) - (len(pcm16) % BYTES_PER_FRAME)
    if usable <= 0:
        return numpy.zeros(0, dtype=numpy.float32)
    ints = numpy.frombuffer(pcm16[:usable], dtype=numpy.int16)
    return ints.astype(numpy.float32) / 32768.0


def _load_whisper(model_name, device, compute_type):
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise DictationUnavailable(
            "Local dictation needs the faster-whisper package "
            "(pip install faster-whisper).") from exc
    try:
        return WhisperModel(model_name, device=device, compute_type=compute_type)
    except Exception as exc:
        raise DictationUnavailable(
            f"Could not load the {model_name!r} speech model: {exc}") from exc


class WhisperTranscriber(Transcriber):
    """faster-whisper, on the CPU, offline, no account and no key.

    The model is loaded once and cached on the instance. That load is the slow
    part (~1-3 s for base.en), which is why the app warms it on a worker thread
    rather than inside the first recording.
    """

    name = "whisper"
    label = "Whisper (local)"

    def __init__(self, model=None, device="cpu", compute_type="int8",
                 loader=None, vad=True):
        self.model_name = ((model or os.environ.get(MODEL_ENV) or "").strip()
                           or DEFAULT_MODEL)
        self.device = device
        self.compute_type = compute_type
        self.vad = vad
        self._loader = loader or _load_whisper
        self._model = None
        self._lock = threading.Lock()

    def warm(self):
        with self._lock:
            if self._model is None:
                self._model = self._loader(
                    self.model_name, self.device, self.compute_type)
        return self._model

    def transcribe(self, pcm16, sample_rate=SAMPLE_RATE):
        if not pcm16:
            return ""
        if int(sample_rate) != SAMPLE_RATE:
            # Whisper assumes 16 kHz for a bare array. Resampling silently would
            # produce fluent, wrong words — refuse instead.
            raise DictationUnavailable(
                f"Whisper needs {SAMPLE_RATE} Hz audio, got {sample_rate}.")
        audio = _pcm16_to_float32(pcm16)
        if not len(audio):
            return ""
        model = self.warm()
        kwargs = {"beam_size": 1, "condition_on_previous_text": False}
        if self.model_name.endswith(".en"):
            kwargs["language"] = "en"
        if self.vad:
            kwargs["vad_filter"] = True
        try:
            segments, _info = model.transcribe(audio, **kwargs)
            text = " ".join((s.text or "").strip() for s in segments)
        except Exception as exc:
            raise DictationUnavailable(f"Transcription failed: {exc}") from exc
        return " ".join(text.split())


class WisprFlowTranscriber(Transcriber):
    """The Flow API seam. Deliberately not implemented — see the module docstring.

    Wispr's own engine is reachable and its protocol is documented, but access
    is gated: "your organization must be approved by the Flow team" before a key
    can be created, and the key is billed. Shipping an untested WebSocket client
    against an endpoint nobody here can call would be inventing an API, so what
    ships is the seam plus the contract, recorded once so a future
    implementation is transcription rather than research:

      * ``wss://platform-api.wisprflow.ai/api/v1/dash/ws?api_key=Bearer%20<KEY>``
        (or ``/dash/client_ws?client_key=...`` for client-side tokens).
      * Client sends ``{"type": "auth", ...}``, then ``{"type": "append",
        "position": n, "audio_packets": {...}}``, then ``{"type": "commit",
        "total_packets": n}``.
      * Audio is base64 single-channel 16-bit PCM WAV at 16 kHz, in chunks of
        equal duration (1 s recommended) — i.e. exactly what ``Recorder``
        already produces.
      * Server answers ``{"status": "text", "final": false|true, "body":
        {"text": ...}}``.
      * The auth frame accepts a ``conversation`` context object (participants +
        messages), which for THIS app is the interesting part: Alloy knows who
        is in the room.
    """

    name = "wispr_flow"
    label = "Wispr Flow API"

    def __init__(self, api_key=None, **_ignored):
        self.api_key = api_key

    def transcribe(self, pcm16, sample_rate=SAMPLE_RATE):
        raise DictationUnavailable(
            "The Wispr Flow API needs an approved organization and a billed "
            "API key (enterprise@wisprflow.ai). Alloy ships with local Whisper "
            "instead, so dictation costs nothing and needs no account.")


TRANSCRIBERS = {
    WhisperTranscriber.name: WhisperTranscriber,
    WisprFlowTranscriber.name: WisprFlowTranscriber,
}
DEFAULT_ENGINE = WhisperTranscriber.name


def make_transcriber(engine=None, **kwargs):
    engine = (engine or DEFAULT_ENGINE).strip().lower()
    cls = TRANSCRIBERS.get(engine)
    if cls is None:
        raise DictationUnavailable(f"Unknown dictation engine {engine!r}.")
    return cls(**kwargs)


# ------------------------------------------------------------------ probe ---

def _has(module):
    import importlib.util
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _input_device_count():
    try:
        import sounddevice
        return sum(1 for d in sounddevice.query_devices()
                   if d.get("max_input_channels", 0) > 0)
    except Exception:
        return 0


def _hf_cache_root(home=None):
    root = os.environ.get("HF_HUB_CACHE")
    if not root and os.environ.get("HF_HOME"):
        root = os.path.join(os.environ["HF_HOME"], "hub")
    if not root:
        root = os.path.join(home or os.path.expanduser("~"),
                            ".cache", "huggingface", "hub")
    return root


def model_cached(model_name, home=None):
    """True when the model is already in the HuggingFace cache.

    Not a gate on availability — a first-run download is legitimate — but the
    UI can warn about the wait instead of looking frozen.
    """
    if os.path.isdir(model_name):
        return True
    return os.path.isdir(os.path.join(
        _hf_cache_root(home), f"models--Systran--faster-whisper-{model_name}"))


def probe(model=None, home=None):
    """What dictation can actually do on THIS machine, and why not if not."""
    name = (model or os.environ.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL
    info = {"available": False, "engine": DEFAULT_ENGINE, "model": name,
            "label": WhisperTranscriber.label, "cached": False, "reason": ""}
    if not _has("sounddevice"):
        info["reason"] = ("Microphone capture needs the sounddevice package "
                          "(pip install sounddevice).")
        return info
    if not _input_device_count():
        info["reason"] = "No microphone was found on this machine."
        return info
    if not _has("faster_whisper"):
        info["reason"] = ("Local dictation needs the faster-whisper package "
                          "(pip install faster-whisper).")
        return info
    if not _has("numpy"):
        info["reason"] = "Local dictation needs numpy."
        return info
    info["cached"] = model_cached(name, home=home)
    info["available"] = True
    if not info["cached"]:
        info["reason"] = (f"First use will download the {name} model "
                          "(one time, then it is offline).")
    return info
