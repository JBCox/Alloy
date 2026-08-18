"""File/image viewing bridge: confinement, MIME, caps, placeholders.

The bridge (app.Api.read_image / list_workspace_files) must serve ONLY files
beneath the ACTIVE session's workspace — resolved from the live workspace
value, never a path rebuilt from the session id. Escapes (.., outside
absolute paths, junction/symlink hops) are rejected, and every failure is a
quiet {"error": …} the UI turns into a placeholder — never an exception.

Run:  python tests/test_bridge_files.py     (token-free, no CLI calls)
"""

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
import app
from test_app_headless import FakeWindow, scripted_agent_class

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


def write_png(path, size=(4, 4), color=(200, 60, 60, 255)):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if HAVE_PIL:
        Image.new("RGBA", size, color).save(path, "PNG")
    else:  # 1x1 red PNG
        with open(path, "wb") as f:
            f.write(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
                "2mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="))


def make_junction(link, target):
    """Windows directory junction (no admin needed). Returns True on success."""
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                           capture_output=True, timeout=15)
        return r.returncode == 0 and os.path.isdir(link)
    except OSError:
        return False


class BridgeFilesBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-bridge-test-")
        self.ws = os.path.join(self.tmp, "workspace")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.ws)
        os.makedirs(self.outside)
        self.api = app.Api()
        self.api._window = FakeWindow()
        # the LIVE workspace value, exactly where the loops put it
        self.api._conv = {"workspace": self.ws}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ReadImageTests(BridgeFilesBase):
    def test_reads_image_inside_workspace(self):
        write_png(os.path.join(self.ws, "chart.png"))
        for req in ("chart.png", os.path.join(self.ws, "chart.png")):
            r = self.api.read_image(req)
            self.assertTrue(r.get("ok"), r)
            self.assertTrue(r["data_uri"].startswith("data:image/"), r)
            self.assertEqual(r["name"], "chart.png")
            # the payload is real base64 that decodes back to bytes
            b64 = r["data_uri"].split(",", 1)[1]
            self.assertTrue(base64.b64decode(b64))

    def test_reads_image_in_subfolder(self):
        write_png(os.path.join(self.ws, "out", "render.png"))
        r = self.api.read_image(os.path.join("out", "render.png"))
        self.assertTrue(r.get("ok"), r)

    def test_dotdot_escape_rejected(self):
        write_png(os.path.join(self.outside, "secret.png"))
        r = self.api.read_image(os.path.join("..", "outside", "secret.png"))
        self.assertIn("error", r)
        self.assertNotIn("data_uri", r)

    def test_dotdot_mid_path_rejected(self):
        write_png(os.path.join(self.outside, "secret.png"))
        r = self.api.read_image(
            os.path.join("sub", "..", "..", "outside", "secret.png"))
        self.assertIn("error", r)

    def test_absolute_outside_rejected(self):
        write_png(os.path.join(self.outside, "secret.png"))
        r = self.api.read_image(os.path.join(self.outside, "secret.png"))
        self.assertIn("error", r)
        self.assertNotIn("data_uri", r)

    def test_other_drive_rejected(self):
        # commonpath raises ValueError across drives — must come back as a
        # quiet error, never an exception through the bridge
        r = self.api.read_image("Z:\\secret.png")
        self.assertIn("error", r)

    def test_junction_escape_rejected(self):
        write_png(os.path.join(self.outside, "secret.png"))
        link = os.path.join(self.ws, "jump")
        if not make_junction(link, self.outside):
            self.skipTest("mklink /J unavailable")
        # sanity: the junction itself works, so ONLY confinement can save us
        self.assertTrue(
            os.path.isfile(os.path.join(link, "secret.png")))
        r = self.api.read_image(os.path.join("jump", "secret.png"))
        self.assertIn("error", r)
        self.assertNotIn("data_uri", r)

    def test_symlink_escape_rejected(self):
        write_png(os.path.join(self.outside, "secret.png"))
        link = os.path.join(self.ws, "sym.png")
        try:
            os.symlink(os.path.join(self.outside, "secret.png"), link)
        except OSError:
            self.skipTest("symlinks need privilege on this machine")
        r = self.api.read_image("sym.png")
        self.assertIn("error", r)
        self.assertNotIn("data_uri", r)

    def test_missing_file_is_quiet_error(self):
        # replay case: messages.jsonl rows outlive the files they mention
        r = self.api.read_image("gone.png")
        self.assertIn("error", r)

    def test_forbidden_and_missing_look_identical(self):
        # the reply must not disclose whether a forbidden path exists
        write_png(os.path.join(self.outside, "secret.png"))
        missing = self.api.read_image("gone.png")
        escaped = self.api.read_image(
            os.path.join("..", "outside", "secret.png"))
        self.assertEqual(missing, escaped)

    def test_extension_enforced(self):
        p = os.path.join(self.ws, "notes.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("not an image")
        r = self.api.read_image("notes.txt")
        self.assertIn("error", r)

    def test_size_cap(self):
        write_png(os.path.join(self.ws, "big.png"))
        old = app.IMAGE_MAX_BYTES
        app.IMAGE_MAX_BYTES = 10
        try:
            r = self.api.read_image("big.png")
        finally:
            app.IMAGE_MAX_BYTES = old
        self.assertIn("error", r)

    def test_no_active_workspace(self):
        self.api._conv = None
        r = self.api.read_image("chart.png")
        self.assertIn("error", r)

    def test_garbage_paths_never_raise(self):
        for bad in (None, 7, "", "a\x00b.png", ["x.png"]):
            r = self.api.read_image(bad)
            self.assertIn("error", r, bad)

    @unittest.skipUnless(HAVE_PIL, "thumbnails need Pillow")
    def test_thumbnail_first_full_on_demand(self):
        write_png(os.path.join(self.ws, "big.png"), size=(1200, 900))
        thumb = self.api.read_image("big.png")
        full = self.api.read_image("big.png", full=True)
        self.assertTrue(thumb.get("ok") and full.get("ok"))
        self.assertLess(len(thumb["data_uri"]), len(full["data_uri"]))
        # full res returns the ORIGINAL bytes untouched
        with open(os.path.join(self.ws, "big.png"), "rb") as f:
            self.assertEqual(base64.b64decode(
                full["data_uri"].split(",", 1)[1]), f.read())
        # and the thumbnail really is thumbnail-scale
        b64 = thumb["data_uri"].split(",", 1)[1]
        import io
        with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
            self.assertLessEqual(max(im.size), app.THUMB_EDGE)


class ListFilesTests(BridgeFilesBase):
    def test_lists_newest_first_with_flags(self):
        write_png(os.path.join(self.ws, "old.png"))
        with open(os.path.join(self.ws, "new.txt"), "w") as f:
            f.write("x")
        os.utime(os.path.join(self.ws, "old.png"), (1000, 1000))
        r = self.api.list_workspace_files()
        self.assertEqual(os.path.normcase(r["workspace"]),
                         os.path.normcase(os.path.realpath(self.ws)))
        names = [f["name"] for f in r["files"]]
        self.assertEqual(names, ["new.txt", "old.png"])
        flags = {f["name"]: f["is_image"] for f in r["files"]}
        self.assertEqual(flags, {"new.txt": False, "old.png": True})
        # abs paths stay inside the workspace (the rail's Open-in-OS uses them)
        for f in r["files"]:
            self.assertIsNotNone(
                app.confine_to_workspace(self.ws, f["abs"]), f)

    def test_no_workspace_is_empty_not_error(self):
        self.api._conv = None
        self.assertEqual(self.api.list_workspace_files(),
                         {"workspace": None, "files": []})

    def test_skips_dot_and_dep_dirs(self):
        write_png(os.path.join(self.ws, ".git", "objects", "x.png"))
        write_png(os.path.join(self.ws, "node_modules", "y.png"))
        write_png(os.path.join(self.ws, "keep.png"))
        names = [f["name"] for f in self.api.list_workspace_files()["files"]]
        self.assertEqual(names, ["keep.png"])


class LiveWorkspaceTests(unittest.TestCase):
    """End-to-end: the bridge resolves against the workspace the conversation
    ACTUALLY runs in (a picked folder), and the default session layout —
    the path a session-id rebuild would produce — is out of bounds."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-bridge-e2e-")
        self._old_sessions = app.SESSIONS_DIR
        # app._conversation builds paths from app.SESSIONS_DIR, but
        # open_session/session_path resolve ids via relay.SESSIONS_DIR
        app.SESSIONS_DIR = relay.SESSIONS_DIR = self.tmp
        self._old_types = dict(relay.AGENT_TYPES)

    def tearDown(self):
        app.SESSIONS_DIR = relay.SESSIONS_DIR = self._old_sessions
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_chat(self, **extra):
        relay.AGENT_TYPES["claude"] = scripted_agent_class("Claude", ["c1"])
        relay.AGENT_TYPES["gpt"] = scripted_agent_class("GPT", ["g1"])
        api = app.Api()
        api._window = FakeWindow()
        cfg = {"opener": "hi", "turns": 1, "brief": False,
               "seats": [{"id": 0, "provider": "claude", "enabled": True},
                         {"id": 1, "provider": "gpt", "enabled": True}]}
        cfg.update(extra)
        api._conversation(cfg)
        api._emit_q.join()
        return api

    def test_picked_folder_is_the_boundary(self):
        proj = os.path.join(self.tmp, "picked-project")
        os.makedirs(proj)
        api = self._run_chat(workspace=proj)
        write_png(os.path.join(proj, "in-project.png"))
        # a file in the SESSION dir (default layout) is outside the live
        # workspace and must be rejected — never resolved via the session id
        write_png(os.path.join(api._session_dir, "in-session.png"))
        self.assertTrue(api.read_image("in-project.png").get("ok"))
        self.assertIn("error", api.read_image(
            os.path.join(api._session_dir, "in-session.png")))

    def test_default_workspace_is_the_boundary(self):
        api = self._run_chat()
        ws = api._conv["workspace"]
        write_png(os.path.join(ws, "made.png"))
        # meta.json one level up is real but out of bounds
        self.assertTrue(os.path.isfile(
            os.path.join(api._session_dir, "meta.json")))
        self.assertTrue(api.read_image("made.png").get("ok"))
        self.assertIn("error",
                      api.read_image(os.path.join("..", "meta.json")))

    def test_reopened_chat_serves_its_own_workspace(self):
        api = self._run_chat()
        ws = api._conv["workspace"]
        write_png(os.path.join(ws, "kept.png"))
        sid = os.path.basename(api._session_dir)
        # a fresh process reopening the chat: live state rebuilt from disk
        api2 = app.Api()
        api2._window = FakeWindow()
        r = api2.open_session(sid)
        self.assertTrue(r.get("ok"), r)
        self.assertTrue(api2.read_image("kept.png").get("ok"))
        # closing the chat closes the bridge
        api2.reset_conversation()
        self.assertIn("error", api2.read_image("kept.png"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
