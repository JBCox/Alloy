"""The Model Builder's bridge: real app.Api, fake window, no tokens, no Blender.

Every builder_* method answers on the calling (bridge) thread without a subprocess; truth arrives as
`uiEvent({"event": "builder", ...})` packets that carry no chat keys; an unattended start consults the
stored plan-quota brake without probing; images are served only from inside the open workflow dir.
Run: python tests/test_builder_bridge.py
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "builder"))

import _no_cli  # noqa: E402

_no_cli.install()

import app  # noqa: E402
import builder_host  # noqa: E402
import relay  # noqa: E402
from test_app_headless import FakeWindow  # noqa: E402
from test_builder_host import make_project  # noqa: E402
from test_engine import _adapters, _runner  # noqa: E402
from builder.viewmodel import BuilderSession  # noqa: E402


class DialogWindow(FakeWindow):
    def __init__(self, answer=("a.png", "b.png")):
        super().__init__()
        self.answer = answer
        self.dialogs = []

    def create_file_dialog(self, *args, **kwargs):
        self.dialogs.append((args, kwargs))
        return self.answer


def seated_host(api):
    """A host whose sessions run on the builder suite's fake runner and scripted seats."""
    def factory(cfg, mock):
        cfg.agents["A"].provider = "mock"
        cfg.agents["B"].provider = "mock"
        return BuilderSession(cfg, runner_factory=lambda c, p: _runner(), adapters_factory=lambda c, m: _adapters(),
                              mock=mock)

    host = builder_host.BuilderHost(
        settings_path=lambda: os.path.join(relay.SESSIONS_DIR, builder_host.SETTINGS_FILE),
        emit=api.emit, confine=app.confine_to_workspace, session_factory=factory)
    api._builder_host = host
    return host


def builder_events(window):
    return [e for e in window.events() if e.get("event") == "builder"]


def wait_for(window, kind, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for e in builder_events(window):
            if e["payload"].get("kind") == kind:
                return e
            if e["payload"].get("kind") == "error" and kind != "error":
                raise AssertionError(f"job failed: {e['payload']}")
        time.sleep(0.05)
    raise AssertionError(f"no builder {kind} packet within {timeout}s")


class BuilderBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="alloy-builder-bridge-")
        self._old = (app.SESSIONS_DIR, relay.SESSIONS_DIR)
        app.SESSIONS_DIR = relay.SESSIONS_DIR = self.tmp
        self.apis = []

    def tearDown(self):
        for api in self.apis:
            api._builder_shutdown()
        app.SESSIONS_DIR, relay.SESSIONS_DIR = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def api(self, window=None):
        api = app.Api()
        api._window = window or DialogWindow()
        self.apis.append(api)
        return api

    def test_api_constructs_no_builder_host_or_thread(self):
        api = self.api()
        self.assertIsNone(api._builder_host)
        state = api.builder_state()
        self.assertTrue(state["available"])
        self.assertFalse(state["open"])
        self.assertIsNone(api._builder_host._session, "a state read must not start a session thread")

    def test_every_builder_method_answers_on_the_bridge_thread_without_a_subprocess(self):
        api = self.api()
        wf = str(Path(self.tmp) / "nothing-here")
        calls = [
            ("builder_state", ()), ("builder_snapshot", ()), ("builder_recent", ()), ("builder_get_settings", ()),
            ("builder_save_settings", ({"attended": True},)), ("builder_overlay_rects", ("rnd_x",)),
            ("builder_read_image", ("x.png", False)), ("builder_pick_files", ()), ("builder_pick_file", ("blend",)),
            ("builder_open", (wf,)), ("builder_close", (False,)), ("builder_preflight", (False,)),
            ("builder_start", (True, None, None)), ("builder_resume", ()), ("builder_pause", ()), ("builder_cancel", ()),
            ("builder_feedback", ("hello",)), ("builder_accept", ("cmp_x",)), ("builder_reopen", ("cmp_x", "why")),
            ("builder_waive", ("f_x", "because")), ("builder_request_correction", ("f_x", "")),
            ("builder_restore_checkpoint", ("ck_x",)), ("builder_set_limits", ({"max_requests": 1},)),
            ("builder_add_reference", (["a.png"], None, "target", False, "")), ("builder_register_source", ("x.blend",)),
            ("builder_register_empty", ()), ("builder_refresh", ()),
            ("builder_new_project", ("Demo", None, None, str(Path(self.tmp) / "demo-wf"), None, None)),
            ("builder_new_fixture", (str(Path(self.tmp) / "fx"),)),
        ]
        names = {n for n, _ in calls}
        real_popen = subprocess.Popen

        def boom(*a, **k):
            raise AssertionError(f"subprocess on the bridge thread: {a[:1]}")

        subprocess.Popen = boom
        try:
            for name, args in calls:
                fn = getattr(api, name)
                t0 = time.time()
                out = fn(*args)
                self.assertIsInstance(out, dict, name)
                self.assertLess(time.time() - t0, 5.0, f"{name} blocked the bridge thread")
        finally:
            subprocess.Popen = real_popen
        api._emit_q.join()
        # every public builder_* method on Api is in the table above (a new verb must be added here)
        public = {n for n, _ in inspect.getmembers(app.Api, inspect.isfunction) if n.startswith("builder_")}
        self.assertEqual(public - names, set())
        # all of them are positional with defaults: the bridge contract test binds them that way
        for n in public:
            for p in list(inspect.signature(getattr(app.Api, n)).parameters.values())[1:]:
                self.assertEqual(p.kind, inspect.Parameter.POSITIONAL_OR_KEYWORD, f"{n}.{p.name}")

    def test_builder_events_arrive_as_uiEvent_builder_packets_without_chat_keys(self):
        api = self.api()
        seated_host(api)
        wf = make_project(Path(self.tmp))
        self.assertEqual(api.builder_open(str(wf)), {"ok": True, "job": "open"})
        snap = wait_for(api._window, "snapshot")
        api._emit_q.join()
        self.assertTrue(snap["payload"]["state"]["open"])
        self.assertEqual(snap["payload"]["snapshot"]["project"]["name"], "Lamp")
        for e in builder_events(api._window):
            self.assertEqual(sorted(k for k in e if k != "event"), ["payload"])
            for banned in ("chat_id", "session_id", "session", "background"):
                self.assertNotIn(banned, e["payload"])
            json.dumps(e["payload"])
        self.assertEqual(api.builder_snapshot()["snapshot"]["project"]["name"], "Lamp")

    def test_unattended_start_consults_the_stored_brake_without_probing(self):
        api = self.api()
        seated_host(api)
        wf = make_project(Path(self.tmp))
        api.builder_open(str(wf))
        wait_for(api._window, "snapshot")
        # no brake configured: fails open, in words
        self.assertEqual(api.builder_start(False, None, None), {"ok": True, "job": "start_run"})
        calls = []

        def verdict(probe=True):
            calls.append(probe)
            return False, "Plan brake: 7-day utilization is at 91%, above the 50% brake."

        api._plan_brake_verdict = verdict
        r = api.builder_start(False)
        self.assertEqual(r, {"ok": False, "reason": "Plan brake: 7-day utilization is at 91%, above the 50% brake."})
        self.assertEqual(calls, [False], "the bridge thread must decide on the stored snapshot, never a probe")
        r = api.builder_start(True)
        self.assertEqual(r, {"ok": True, "job": "start_run"})
        self.assertEqual(calls, [False], "an attended start is never gated")

    def test_pick_files_opens_a_multi_select_image_dialog(self):
        win = DialogWindow(answer=("C:/pics/a.png", "C:/pics/b.png"))
        api = self.api(win)
        self.assertEqual(api.builder_pick_files(), {"paths": ["C:/pics/a.png", "C:/pics/b.png"]})
        args, kwargs = win.dialogs[-1]
        self.assertTrue(kwargs.get("allow_multiple"))
        self.assertTrue(any("*.png" in t for t in kwargs.get("file_types", ())))
        self.assertEqual(api.builder_pick_file("blend"), {"path": "C:/pics/a.png"})
        args, kwargs = win.dialogs[-1]
        self.assertFalse(kwargs.get("allow_multiple"))
        self.assertTrue(any("*.blend" in t for t in kwargs.get("file_types", ())))
        win.answer = None
        self.assertEqual(api.builder_pick_files(), {"paths": []})
        self.assertEqual(api.builder_pick_file(), {"path": None})

    def test_builder_read_image_is_confined_and_serves_data_uris(self):
        api = self.api()
        seated_host(api)
        self.assertEqual(api.builder_read_image("x.png"), {"error": "not available"})
        wf = make_project(Path(self.tmp))
        api.builder_open(str(wf))
        snap = wait_for(api._window, "snapshot")["payload"]["snapshot"]
        ref = snap["references"][0]["file"]
        thumb = api.builder_read_image(ref)
        self.assertTrue(thumb.get("ok"), thumb)
        self.assertTrue(thumb["data_uri"].startswith("data:image/"))
        full = api.builder_read_image(ref, True)
        self.assertTrue(full["data_uri"].startswith("data:image/png;base64,"))
        outside = Path(self.tmp) / "outside.png"
        shutil.copyfile(ref, outside)
        self.assertEqual(api.builder_read_image(str(outside)), {"error": "not available"})
        self.assertEqual(api.builder_read_image(os.path.join(str(wf), "..", "outside.png")), {"error": "not available"})
        self.assertEqual(api.builder_read_image(str(wf / "project.json")), {"error": "not an image"})

    def test_shutdown_hook_closes_the_host_session(self):
        api = self.api()
        seated_host(api)
        wf = make_project(Path(self.tmp))
        api.builder_open(str(wf))
        wait_for(api._window, "snapshot")
        api._builder_shutdown()
        wait_for(api._window, "closed")
        self.assertFalse(api.builder_state()["open"])


if __name__ == "__main__":
    unittest.main()
