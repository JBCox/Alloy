"""builder_host: one BuilderSession for the app, its event pump, and the settings file.

Token-free and Blender-free: the session is built with the builder suite's fake runner and scripted seats
(tests/builder/_fake_runner.py, test_engine._adapters), exactly as tests/builder/test_viewmodel.py does.
Every emitted payload goes through a bare json.dumps here, which is the proof that the host only ever
hands the app JSON.  Run: python tests/test_builder_host.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "builder"))

import _no_cli  # noqa: E402

_no_cli.install()

from PIL import Image  # noqa: E402

from _fake_runner import FakeRunner  # noqa: E402
from test_engine import PARTS, _adapters, _runner  # noqa: E402

import builder_host  # noqa: E402
from builder.operations import Operations  # noqa: E402
from builder.ownership import OwnershipManager  # noqa: E402
from builder.project import Project  # noqa: E402
from builder.records import Record  # noqa: E402
from builder.references import References  # noqa: E402
from builder.viewmodel import BuilderSession  # noqa: E402


def make_project(workdir: Path) -> Path:
    """The engine suite's `project` fixture (test_engine.py) as a plain function; returns the closed workflow dir."""
    prj = Project.create(workdir / "wf", name="Lamp", asset_name="Lamp", extra={"first_component": "Head"})
    try:
        refs = References(prj)
        for label in ("front", "side"):
            p = workdir / f"{label}.png"
            Image.new("RGB", (120, 90), (90, 90, 90)).save(p)
            refs.add(p, labels=[label], kind="target")
        seed = workdir / "seed.blend"
        seed.write_bytes(b"BLENDER-fake-seed")
        ops = Operations(prj, None, OwnershipManager(prj.store))
        ops.register_revision(seed, parent_revision_id=None, created_by_op_id=None, actor="engine",
                              identity_map=[{"alloy_id": p, "type": "OBJECT"} for p in PARTS], note="fixture revision 0")
        for p in PARTS:
            prj.store.upsert(Record.new("part", {"name": p, "blender_ids": [p]}, id=p), actor="engine", event="part.created")
    finally:
        prj.close()
    return workdir / "wf"


class FakeSession:
    """A session whose events queue is pre-filled: proves the pump's ordering and coalescing without an engine."""

    def __init__(self, cfg, mock, burst):
        import queue

        self.config = cfg
        self.mock = mock
        self.events = queue.Queue()
        for item in burst:
            self.events.put(item)
        self.refreshed = 0
        self.closed = False

    def refresh(self):
        self.refreshed += 1

    def close(self, timeout=None):
        self.closed = True
        self.events.put(("closed",))


class HostTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alloy-host-"))
        self.emitted: list[tuple[str, dict]] = []
        self.hosts: list[builder_host.BuilderHost] = []

    def tearDown(self):
        for h in self.hosts:
            h.shutdown(10)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------------------------------------
    def settings_path(self) -> str:
        return os.path.join(self.tmp, "sessions", "builder.json")

    def make_host(self, session_factory=None, measure=None) -> builder_host.BuilderHost:
        def factory(cfg, mock):
            cfg.agents["A"].provider = "mock"
            cfg.agents["B"].provider = "mock"
            runner = _runner()
            if measure is not None:
                runner.measure_data = measure
            return BuilderSession(cfg, runner_factory=lambda c, p: runner, adapters_factory=lambda c, m: _adapters(),
                                  mock=mock)

        host = builder_host.BuilderHost(
            settings_path=self.settings_path,
            emit=lambda event, payload: self.emitted.append((event, json.loads(json.dumps(payload)))),
            session_factory=session_factory or factory)
        self.hosts.append(host)
        return host

    def wait_for(self, kind: str, job: str | None = None, timeout: float = 90.0, start: int = 0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for event, payload in self.emitted[start:]:
                if payload.get("kind") == kind and (job is None or payload.get("job") == job):
                    return payload
                if kind != "error" and payload.get("kind") == "error" and (job is None or payload.get("job") == job):
                    self.fail(f"job {payload.get('job')} failed: {payload.get('message')}")
            time.sleep(0.05)
        self.fail(f"no {kind!r} payload for job {job!r} within {timeout}s; got "
                  f"{[(p.get('kind'), p.get('job')) for _, p in self.emitted[start:]]}")

    def snapshot_after(self, job: str, start: int = 0, timeout: float = 90.0) -> dict:
        """The snapshot the worker publishes AFTER a job's done (the pump may emit it in the next burst)."""
        self.wait_for("done", job, start=start)
        i = next(k for k in range(start, len(self.emitted)) if self.emitted[k][1].get("kind") == "done"
                 and self.emitted[k][1].get("job") == job)
        return self.wait_for("snapshot", start=i + 1, timeout=timeout)["snapshot"]

    def last_snapshot(self, start: int = 0) -> dict:
        snaps = [p["snapshot"] for _, p in self.emitted[start:] if p.get("kind") == "snapshot"]
        self.assertTrue(snaps, "no snapshot emitted")
        return snaps[-1]

    # -- projects -----------------------------------------------------------------------------------------
    def test_new_project_creates_the_layout_opens_it_and_records_recent(self):
        host = self.make_host()
        wf = self.tmp / "lamp-wf"
        r = host.new_project(name="Lamp", asset="Lamp", workflow_dir=str(wf), first_component="Head")
        self.assertEqual(r, {"ok": True, "job": "new_project"})
        done = self.wait_for("done", "new_project")
        self.assertTrue(done["result"]["created"])
        self.assertTrue((wf / "project.json").is_file())
        self.wait_for("snapshot")
        kinds = [p["kind"] for _, p in self.emitted]
        self.assertEqual(kinds[:3], ["busy", "done", "snapshot"])
        self.assertTrue(host.state()["open"])
        self.assertEqual(Path(host.state()["workflow_dir"]), wf)
        self.assertEqual(self.last_snapshot()["project"]["name"], "Lamp")
        recent = host.recent()["recent"]
        self.assertEqual(recent[0]["name"], "Lamp")
        self.assertEqual(Path(recent[0]["workflow_dir"]), wf)
        self.assertTrue(recent[0]["exists"])
        self.assertTrue(Path(self.settings_path()).is_file())

    def test_new_project_without_a_location_lands_under_sessions_builder_projects(self):
        host = self.make_host()
        host.new_project(name="Lamp two")
        done = self.wait_for("done", "new_project")
        self.assertEqual(Path(done["result"]["workflow_dir"]),
                         self.tmp / "sessions" / "builder-projects" / "lamp-two")

    def test_run_to_the_gate_with_scripted_seats_overlay_boxes_and_accept(self):
        measure = {"bboxes": {"p_bracket": {"min": [-0.15, -0.15, 2.3], "max": [0.15, 0.15, 2.4]},
                              "p_housing": {"min": [-0.45, -0.18, 1.7], "max": [0.45, 0.18, 2.2]}},
                   "distances": [], "ratios": {}, "overlaps": [], "limitations": ["fake"]}
        host = self.make_host(measure=measure)
        wf = make_project(self.tmp)
        host.open(str(wf))
        self.wait_for("snapshot")
        n = len(self.emitted)
        self.assertEqual(host.preflight(live=True), {"ok": True, "job": "preflight_live"})   # scripted seats: free
        self.wait_for("done", "preflight_live", start=n)
        n = len(self.emitted)
        self.assertEqual(host.start(attended=True), {"ok": True, "job": "start_run"})
        snap = self.snapshot_after("start_run", start=n)
        self.assertEqual(snap["stage"]["execution"], "waiting_for_user")
        # events from the engine rode along, in order, before the job's done
        kinds = [p["kind"] for _, p in self.emitted[n:]]
        self.assertEqual(kinds[0], "busy")
        self.assertIn("event", kinds)
        self.assertLess(kinds.index("event"), kinds.index("done"))
        self.assertEqual(host.snapshot()["snapshot"]["stage"]["execution"], "waiting_for_user")   # the cache
        side = next(r for r in snap["renders"] if r["view_name"] == "side" and r["state"] == "ok")
        rects = host.overlay_rects(side["id"])["rects"]
        self.assertEqual({r["part_id"] for r in rects}, {"p_bracket", "p_housing"})
        self.assertEqual(host.overlay_rects("rnd_missing"), {"ok": True, "rects": []})
        comp = snap["components"][0]["id"]
        n = len(self.emitted)
        self.assertEqual(host.accept(comp), {"ok": True, "job": "accept"})
        self.assertEqual(self.snapshot_after("accept", start=n)["components"][0]["acceptance_state"], "accepted_at_revision")

    def test_every_payload_carries_kind_seq_and_state_and_no_chat_keys(self):
        host = self.make_host()
        wf = make_project(self.tmp)
        host.open(str(wf))
        self.wait_for("snapshot")
        seqs = []
        for event, payload in self.emitted:
            self.assertEqual(event, "builder")
            self.assertIn(payload["kind"], ("busy", "done", "error", "event", "snapshot", "closed"))
            self.assertIsInstance(payload["seq"], int)
            self.assertIn("state", payload)
            for banned in ("chat_id", "session_id", "session", "background"):
                self.assertNotIn(banned, payload)
            seqs.append(payload["seq"])
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))

    def test_errors_are_sentences_never_exceptions(self):
        host = self.make_host()
        self.assertEqual(host.open(str(self.tmp / "nope")), {"ok": True, "job": "open"})
        err = self.wait_for("error", "open")
        self.assertIn("no workflow project", err["message"])
        self.assertFalse(host.state()["open"])
        self.assertIn("no workflow project", host.state()["last_error"])
        n = len(self.emitted)
        host.accept("x")
        err = self.wait_for("error", "accept", start=n)
        self.assertIn("no workflow project is open", err["message"])
        self.assertIsInstance(host.overlay_rects("rnd_x"), dict)     # no snapshot yet: an empty answer, not a raise
        self.assertEqual(host.overlay_rects("rnd_x")["rects"], [])

    # -- settings -----------------------------------------------------------------------------------------
    def test_settings_roundtrip_deep_merges_replaces_presets_and_never_raises(self):
        host = self.make_host()
        path = Path(self.settings_path())
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 1, "recent": [{"workflow_dir": "X:/old", "name": "Old"}],
                                    "builder": {"limits": {"max_requests": 3, "max_renders": 9},
                                                "presets": {"a": {"name": "A"}, "b": {"name": "B"}}}}),
                        encoding="utf-8")
        got = host.get_settings()
        self.assertTrue(got["ok"])
        self.assertEqual(got["config"]["limits"]["max_requests"], 3)
        self.assertEqual(got["presets"], ["a", "b"])
        self.assertEqual(got["seats"], ["A", "B"])
        r = host.save_settings({"limits": {"max_requests": 5}})
        self.assertTrue(r["ok"], r)
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["builder"]["limits"], {"max_requests": 5, "max_renders": 9})   # deep merge keeps siblings
        self.assertEqual(doc["builder"]["presets"], {"a": {"name": "A"}, "b": {"name": "B"}})
        self.assertEqual(doc["recent"][0]["name"], "Old")                                     # untouched
        host.save_settings({"presets": {"a": {"name": "A2"}}})
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["builder"]["presets"], {"a": {"name": "A2"}})                    # presets replace as a section
        self.assertEqual(host.get_settings()["config"]["limits"]["max_requests"], 5)
        # a bad value is reported as a warning, and the file still validates
        r = host.save_settings({"limits": {"attempts_per_finding": "nope"}})
        self.assertTrue(r["ok"])
        self.assertTrue(any("attempts_per_finding" in w for w in r["warnings"]))
        # a corrupt file is defaults plus a warning, never a raise
        path.write_text("{not json", encoding="utf-8")
        got = host.get_settings()
        self.assertTrue(got["ok"])
        self.assertEqual(got["config"]["limits"]["max_requests"], 0)
        self.assertTrue(any("builder.json" in w for w in got["warnings"]))
        # the next session picks the saved config up
        host.save_settings({"attended": False})
        host.refresh()
        self.wait_for("done", "refresh")
        self.assertFalse(host._session.config.attended)

    def test_json_safe_covers_paths_dataclasses_datetimes_sets_and_nan(self):
        import dataclasses
        import datetime

        @dataclasses.dataclass
        class Q:
            value: float
            note: str

        out = builder_host.json_safe({"p": Path("C:/x"), "d": datetime.datetime(2026, 9, 8, 1, 2, 3), "s": {1, 2},
                                      "q": Q(1.5, "n"), "nan": float("nan"), "b": b"\xff\x00", "t": (1, 2),
                                      3: "int key"})
        json.dumps(out)
        self.assertEqual(out["p"], "C:/x" if os.sep == "/" else str(Path("C:/x")))
        self.assertEqual(out["d"], "2026-09-08T01:02:03")
        self.assertEqual(sorted(out["s"]), [1, 2])
        self.assertEqual(out["q"], {"value": 1.5, "note": "n"})
        self.assertIsNone(out["nan"])
        self.assertEqual(out["t"], [1, 2])
        self.assertEqual(out["3"], "int key")

    # -- images -------------------------------------------------------------------------------------------
    def test_resolve_image_is_confined_to_the_open_workflow_dir(self):
        host = self.make_host()
        self.assertEqual(host.resolve_image("anything.png"), {"error": "not available"})   # nothing open
        wf = make_project(self.tmp)
        host.open(str(wf))
        snap_payload = self.wait_for("snapshot")
        ref_file = snap_payload["snapshot"]["references"][0]["file"]
        found = host.resolve_image(ref_file)
        self.assertTrue(found["ok"])
        self.assertTrue(Path(found["path"]).is_file())
        outside = self.tmp / "outside.png"
        Image.new("RGB", (4, 4)).save(outside)
        for bad in (str(outside), os.path.join(str(wf), "..", "outside.png"), str(wf / "refs" / "missing.png"),
                    "..\\..\\outside.png"):
            self.assertEqual(host.resolve_image(bad), {"error": "not available"}, bad)

    # -- lifecycle ----------------------------------------------------------------------------------------
    def test_close_project_refuses_during_a_run_unless_forced(self):
        host = self.make_host()
        wf = make_project(self.tmp)
        host.open(str(wf))
        self.wait_for("snapshot")
        with host._lock:
            host._busy_job = "start_run"          # what the pump records while a run job is in flight
        r = host.close_project()
        self.assertFalse(r["ok"])
        self.assertIn("in progress", r["reason"])
        with host._lock:
            host._busy_job = None
        n = len(self.emitted)
        self.assertEqual(host.close_project(), {"ok": True, "job": "close_project"})
        done = self.wait_for("done", "close_project", start=n)
        self.assertEqual(done["result"], {"closed": True})
        self.wait_for("snapshot", start=n)
        self.assertEqual(self.last_snapshot(n), {})              # the host says "nothing open" in the snapshot's own terms
        self.assertFalse(host.state()["open"])

    def test_pump_emits_a_burst_in_order_with_only_its_last_snapshot(self):
        burst = [("busy", "x"), ("snapshot", {"a": 1}), ("event", {"event": "stage", "stage": "build"}),
                 ("snapshot", {"a": 2}), ("done", "x", {"fine": True}), ("snapshot", {"a": 3})]
        host = self.make_host(session_factory=lambda cfg, mock: FakeSession(cfg, mock, burst))
        host.refresh()
        self.wait_for("done", "x")
        time.sleep(0.3)
        seen = [(p["kind"], p.get("snapshot")) for _, p in self.emitted]
        self.assertEqual([k for k, _ in seen], ["busy", "event", "done", "snapshot"])
        self.assertEqual(seen[-1][1], {"a": 3})
        self.assertEqual(host.snapshot()["snapshot"], {"a": 3})

    def test_shutdown_closes_the_session_and_ends_the_pump(self):
        host = self.make_host()
        wf = make_project(self.tmp)
        host.open(str(wf))
        self.wait_for("snapshot")
        pump = host._pump
        host.shutdown(10)
        self.wait_for("closed")
        pump.join(5)
        self.assertFalse(pump.is_alive())
        self.assertFalse(host.state()["open"])
        self.assertEqual(host.open(str(wf)), {"ok": True, "job": "open"})    # a new session after shutdown

    def test_import_failure_is_a_sentence(self):
        saved = builder_host.IMPORT_ERROR
        builder_host.IMPORT_ERROR = "ImportError: No module named PIL"
        try:
            host = self.make_host()
            for r in (host.open("x"), host.start(), host.new_project(name="n"), host.get_settings()):
                self.assertEqual(r, {"error": "The Model Builder is unavailable: ImportError: No module named PIL"})
            self.assertFalse(host.state()["available"])
        finally:
            builder_host.IMPORT_ERROR = saved

    def test_add_reference_submits_one_job_per_path_and_defaults_labels(self):
        host = self.make_host()
        wf = make_project(self.tmp)
        host.open(str(wf))
        self.wait_for("snapshot")
        a, b = self.tmp / "a.png", self.tmp / "b.png"
        for p in (a, b):
            Image.new("RGB", (32, 16), (10, 10, 10)).save(p)
        n = len(self.emitted)
        r = host.add_reference([str(a), str(b)], labels=None, kind="hypothesis")
        self.assertEqual(r, {"ok": True, "jobs": 2})
        self.wait_for("done", "add_reference", start=n)
        deadline = time.time() + 30
        while time.time() < deadline and sum(1 for _, p in self.emitted[n:] if p.get("kind") == "done") < 2:
            time.sleep(0.05)
        snap = self.last_snapshot(n)
        added = [r for r in snap["references"] if r["kind"] == "hypothesis"]
        self.assertEqual(len(added), 2)
        self.assertEqual(added[0]["labels"], ["other"])
        self.assertEqual(host.add_reference([]), {"ok": False, "reason": "no files were chosen"})


if __name__ == "__main__":
    unittest.main()
