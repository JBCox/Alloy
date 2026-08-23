"""Dictation: recorder state machine, transcriber seam, and the app bridge.

Token-free and hardware-free — no microphone is opened and no speech model is
loaded. The recorder takes a `stream_factory` seam and the transcribers are
swappable, which is the whole reason both exist as parameters.

What this suite is really guarding:

 * A stop that lands WHILE the device is still opening must not leave a live
   stream running. That race is real (opening PortAudio takes long enough for
   a quick tap to finish first) and invisible until the app is recording
   forever with no button lit.
 * Nothing may forge text. An empty recording, a dead model and a refusing
   engine must all come back with no words at all.
 * Every bridge method must return instantly — they are called on pywebview's
   js bridge thread, where a blocking call deadlocks the window.

Run:  python tests/test_dictation.py
"""

import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app
import dictation


# ------------------------------------------------------------- test doubles --

class FakeStream:
    """Stands in for a sounddevice RawInputStream."""

    def __init__(self, sample_rate, on_frames, open_delay=0.0):
        self.sample_rate = sample_rate
        self.on_frames = on_frames
        self.open_delay = open_delay
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self):
        if self.open_delay:
            time.sleep(self.open_delay)
        self.started = True

    def feed(self, seconds=1.0):
        frames = int(self.sample_rate * seconds)
        self.on_frames(b"\x01\x00" * frames)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def factory(box, open_delay=0.0):
    def make(sample_rate, on_frames):
        stream = FakeStream(sample_rate, on_frames, open_delay)
        box.append(stream)
        return stream
    return make


class FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)

    def events(self, name=None):
        out = [json.loads(s[len("uiEvent("):-1]) for s in self.calls]
        return [e for e in out if name is None or e["event"] == name]


class ScriptedTranscriber(dictation.Transcriber):
    def __init__(self, text="", error=None):
        self.text = text
        self.error = error
        self.seen = []
        self.warmed = 0

    def warm(self):
        self.warmed += 1

    def transcribe(self, pcm16, sample_rate=dictation.SAMPLE_RATE):
        self.seen.append(pcm16)
        if self.error:
            raise self.error
        return self.text


# --------------------------------------------------------------- recorder ----

class RecorderTests(unittest.TestCase):
    def test_start_capture_stop_returns_the_audio(self):
        made = []
        rec = dictation.Recorder(stream_factory=factory(made))
        self.assertTrue(rec.start())
        self.assertTrue(rec.recording)
        made[0].feed(0.5)
        pcm = rec.stop()
        self.assertEqual(len(pcm), int(dictation.SAMPLE_RATE * 0.5) * 2)
        self.assertAlmostEqual(dictation.pcm_seconds(pcm), 0.5, places=3)
        self.assertFalse(rec.recording)
        self.assertTrue(made[0].stopped and made[0].closed)

    def test_double_start_is_refused(self):
        rec = dictation.Recorder(stream_factory=factory([]))
        rec.start()
        with self.assertRaises(dictation.DictationUnavailable):
            rec.start()

    def test_stop_without_start_is_empty_not_an_error(self):
        # hold-to-talk can lose its pointerup; punishing that buys nothing
        rec = dictation.Recorder(stream_factory=factory([]))
        self.assertEqual(rec.stop(), b"")

    def test_cancel_discards_the_audio(self):
        made = []
        rec = dictation.Recorder(stream_factory=factory(made))
        rec.start()
        made[0].feed(1.0)
        self.assertEqual(rec.cancel(), b"")
        self.assertTrue(made[0].closed)
        self.assertFalse(rec.recording)

    def test_a_failed_open_leaves_the_recorder_idle(self):
        def boom(sample_rate, on_frames):
            raise dictation.DictationUnavailable("no device")
        rec = dictation.Recorder(stream_factory=boom)
        with self.assertRaises(dictation.DictationUnavailable):
            rec.start()
        self.assertFalse(rec.recording)
        made = []                     # and it is still usable afterwards
        rec._factory = factory(made)
        self.assertTrue(rec.start())

    def test_stop_during_open_closes_the_stream_it_never_owned(self):
        """The race: a tap finishes before PortAudio finishes opening.

        Without the abandon handshake the late stream would be installed after
        the stop and keep recording with nothing watching it.
        """
        made = []
        rec = dictation.Recorder(stream_factory=factory(made, open_delay=0.15))
        result = {}

        def go():
            result["started"] = rec.start()

        t = threading.Thread(target=go)
        t.start()
        time.sleep(0.03)              # inside the open, before it returns
        self.assertEqual(rec.stop(), b"")
        t.join(timeout=5)
        self.assertFalse(result["started"], "start claimed a stream it lost")
        self.assertTrue(made[0].closed, "the abandoned stream stayed open")
        self.assertFalse(rec.recording)

    def test_the_length_cap_holds(self):
        made = []
        rec = dictation.Recorder(max_seconds=1.0, stream_factory=factory(made))
        rec.start()
        made[0].feed(3.0)
        pcm = rec.stop()
        self.assertTrue(rec.truncated)
        self.assertLessEqual(dictation.pcm_seconds(pcm), 3.0)
        self.assertGreater(len(pcm), 0)


# ------------------------------------------------------------ transcribers ---

class TranscriberTests(unittest.TestCase):
    def test_registry_builds_the_default_engine(self):
        self.assertIs(type(dictation.make_transcriber()),
                      dictation.WhisperTranscriber)
        self.assertIs(type(dictation.make_transcriber("wispr_flow")),
                      dictation.WisprFlowTranscriber)
        with self.assertRaises(dictation.DictationUnavailable):
            dictation.make_transcriber("nope")

    def test_whisper_loads_once_and_only_once(self):
        loads = []

        def loader(name, device, compute):
            loads.append((name, device, compute))
            return FakeModel("hello world")

        t = dictation.WhisperTranscriber(model="base.en", loader=loader)
        t.warm()
        text = t.transcribe(b"\x01\x00" * dictation.SAMPLE_RATE)
        self.assertEqual(text, "hello world")
        self.assertEqual(loads, [("base.en", "cpu", "int8")])

    def test_english_model_pins_the_language(self):
        model = FakeModel("hi")
        t = dictation.WhisperTranscriber(model="base.en",
                                         loader=lambda *a: model)
        t.transcribe(b"\x01\x00" * dictation.SAMPLE_RATE)
        self.assertEqual(model.kwargs.get("language"), "en")
        multi = FakeModel("hi")
        t2 = dictation.WhisperTranscriber(model="small", loader=lambda *a: multi)
        t2.transcribe(b"\x01\x00" * dictation.SAMPLE_RATE)
        self.assertNotIn("language", multi.kwargs)

    def test_empty_audio_yields_no_words(self):
        t = dictation.WhisperTranscriber(loader=lambda *a: FakeModel("ghost"))
        self.assertEqual(t.transcribe(b""), "")

    def test_a_wrong_sample_rate_is_refused_not_resampled(self):
        # silently resampling would produce fluent, wrong words
        t = dictation.WhisperTranscriber(loader=lambda *a: FakeModel("x"))
        with self.assertRaises(dictation.DictationUnavailable):
            t.transcribe(b"\x01\x00" * 100, sample_rate=44100)

    def test_a_dead_model_raises_rather_than_returning_text(self):
        def loader(*_a):
            raise dictation.DictationUnavailable("model gone")
        t = dictation.WhisperTranscriber(loader=loader)
        with self.assertRaises(dictation.DictationUnavailable):
            t.transcribe(b"\x01\x00" * dictation.SAMPLE_RATE)

    def test_wispr_flow_seam_refuses_by_name(self):
        t = dictation.WisprFlowTranscriber()
        with self.assertRaises(dictation.DictationUnavailable) as caught:
            t.transcribe(b"\x01\x00" * 100)
        self.assertIn("api key", str(caught.exception).lower())


class FakeModel:
    def __init__(self, text):
        self.text = text
        self.kwargs = {}

    def transcribe(self, audio, **kwargs):
        self.kwargs = kwargs

        class Seg:
            def __init__(self, t):
                self.text = t
        return [Seg(self.text)], {}


# ------------------------------------------------------------------ probe ----

class ProbeTests(unittest.TestCase):
    def _probe_with(self, present, devices=1, home=None):
        real_has, real_devs = dictation._has, dictation._input_device_count
        dictation._has = lambda m: m in present
        dictation._input_device_count = lambda: devices
        try:
            return dictation.probe(model="base.en", home=home)
        finally:
            dictation._has, dictation._input_device_count = real_has, real_devs

    def test_missing_pieces_are_named_one_at_a_time(self):
        cases = [
            (set(), 1, "sounddevice"),
            ({"sounddevice"}, 0, "microphone"),
            ({"sounddevice"}, 1, "faster-whisper"),
            ({"sounddevice", "faster_whisper"}, 1, "numpy"),
        ]
        for present, devices, needle in cases:
            info = self._probe_with(present, devices)
            self.assertFalse(info["available"], needle)
            self.assertIn(needle, info["reason"].lower())

    def test_available_when_every_piece_is_there(self):
        info = self._probe_with({"sounddevice", "faster_whisper", "numpy"})
        self.assertTrue(info["available"])
        self.assertEqual(info["engine"], "whisper")

    def test_an_uncached_model_is_available_but_says_so(self):
        info = self._probe_with({"sounddevice", "faster_whisper", "numpy"},
                                home=os.path.join(os.sep, "nowhere-at-all"))
        self.assertTrue(info["available"])   # a first-run download is legitimate
        self.assertFalse(info["cached"])
        self.assertIn("download", info["reason"].lower())


# ------------------------------------------------------------- app bridge ----

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.api = app.Api()
        self.win = FakeWindow()
        self.api._window = self.win
        self.made = []
        self._real_recorder = dictation.Recorder
        made = self.made
        dictation.Recorder = lambda **kw: self._real_recorder(
            stream_factory=factory(made), **kw)
        # A scripted engine by default: without one, _dict_warm would load the
        # real speech model on every test and turn a token-free suite into a
        # 25-second one.
        self.api._dict_engine = ScriptedTranscriber("")
        # Baseline AFTER Api() so its permanent emitter thread is excluded;
        # flush() then waits on exactly the worker threads a bridge call spawns.
        self._baseline = set(threading.enumerate())

    def tearDown(self):
        dictation.Recorder = self._real_recorder

    def use(self, transcriber):
        self.api._dict_engine = transcriber

    def flush(self):
        deadline = time.time() + 10
        while time.time() < deadline:
            extra = [t for t in threading.enumerate()
                     if t not in self._baseline and t.is_alive()]
            if not extra:
                break
            for t in extra:
                t.join(timeout=5)
        self.api._emit_q.join()

    def states(self):
        return [e["payload"]["state"] for e in self.win.events("dictation")]

    def test_start_returns_immediately_and_emits_recording(self):
        t0 = time.time()
        self.assertEqual(self.api.dictation_start()["ok"], True)
        self.assertLess(time.time() - t0, 0.5, "bridge call blocked")
        self.flush()
        self.assertIn("recording", self.states())

    def test_a_full_round_trip_delivers_the_text(self):
        self.use(ScriptedTranscriber("Ask GPT to review it."))
        self.api.dictation_start()
        self.flush()
        self.made[0].feed(1.5)
        self.api.dictation_stop()
        self.flush()
        self.assertEqual(self.states(), ["recording", "transcribing", "text"])
        last = self.win.events("dictation")[-1]["payload"]
        self.assertEqual(last["text"], "Ask GPT to review it.")

    def test_the_model_is_warmed_while_the_audio_is_still_coming(self):
        engine = ScriptedTranscriber("x")
        self.use(engine)
        self.api.dictation_start()
        self.flush()
        self.assertEqual(engine.warmed, 1, "model load never overlapped the speech")

    def test_a_too_short_tap_never_becomes_words(self):
        self.use(ScriptedTranscriber("phantom sentence"))
        self.api.dictation_start()
        self.flush()
        self.made[0].feed(0.05)
        self.api.dictation_stop()
        self.flush()
        self.assertEqual(self.states()[-1], "empty")
        self.assertEqual(self.win.events("dictation")[-1]["payload"]["text"], "")

    def test_a_silent_recording_is_empty_not_invented(self):
        self.use(ScriptedTranscriber(""))
        self.api.dictation_start()
        self.flush()
        self.made[0].feed(2.0)
        self.api.dictation_stop()
        self.flush()
        self.assertEqual(self.states()[-1], "empty")
        self.assertEqual(self.win.events("dictation")[-1]["payload"]["text"], "")

    def test_a_failing_engine_reports_the_error_with_no_text(self):
        self.use(ScriptedTranscriber(
            error=dictation.DictationUnavailable("model gone")))
        self.api.dictation_start()
        self.flush()
        self.made[0].feed(2.0)
        self.api.dictation_stop()
        self.flush()
        last = self.win.events("dictation")[-1]["payload"]
        self.assertEqual(last["state"], "error")
        self.assertEqual(last["text"], "")
        self.assertIn("model gone", last["note"])

    def test_cancel_transcribes_nothing(self):
        engine = ScriptedTranscriber("should never be said")
        self.use(engine)
        self.api.dictation_start()
        self.flush()
        self.made[0].feed(2.0)
        self.api.dictation_cancel()
        self.flush()
        self.assertEqual(engine.seen, [])
        self.assertEqual(self.states()[-1], "idle")

    def test_stop_without_start_is_harmless(self):
        self.assertEqual(self.api.dictation_stop(), {"ok": True,
                                                     "note": "not recording"})
        self.flush()
        self.assertEqual(self.win.events("dictation"), [])

    def test_a_second_start_does_not_open_a_second_microphone(self):
        self.api.dictation_start()
        self.flush()
        self.api.dictation_start()
        self.flush()
        self.assertEqual(len(self.made), 1)

    def test_fallback_config_never_claims_a_microphone(self):
        # the probe not having finished must not read as "no mic", nor as one
        self.assertFalse(app.Api._fallback_config()["dictation"]["available"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
