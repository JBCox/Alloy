"""Fork tests — token-free, standalone (never imports relay/app).

Run: python tests/test_fork.py
"""

import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fork


def build_session(root, sid, n_rows=4):
    """A plausible v2 session: meta, messages, transcript, sidecars."""
    d = os.path.join(root, sid)
    os.makedirs(d)
    rows = []
    for i in range(n_rows):
        if i % 2 == 0:
            row = {"message_id": f"m{i}", "origin": "human", "audience": "*",
                   "delivered_to": [], "speaker": "josh", "provider": None,
                   "name": "Josh", "text": f"body {i}", "round": i // 2,
                   "meta": "", "role": None,
                   "ts": f"2026-08-25T10:0{i}:00"}
        else:
            row = {"message_id": f"m{i}", "origin": "seat",
                   "audience": ["josh"], "delivered_to": [],
                   "speaker": "claude", "provider": "claude",
                   "name": "Claude", "text": f"body {i}", "round": i // 2,
                   "meta": "", "role": "Researcher",
                   "ts": f"2026-08-25T10:0{i}:00"}
        rows.append(row)
    meta = {
        "v": 2, "id": sid, "title": "Original chat",
        "created": "2026-08-25T10:00:00", "updated": "2026-08-25T10:03:00",
        "ended": True, "workspace": os.path.join(d, "workspace"),
        "topic": "testing", "turns": 10, "rnd": 2, "max": 10,
        "parent": {"id": "parent-session", "seat": "claude",
                   "label": "Claude"},
        "children": ["child-a"],
        "seats": [{
            "id": "s1", "provider": "claude", "label": "Claude",
            "model": "claude-haiku-4-5", "effort": "low",
            "session_id": "cli-uuid-1234", "role": "Researcher",
            "role_instructions": "Cite everything.",
            "introduced": True, "pending": [],
        }],
    }
    with open(os.path.join(d, "messages.jsonl"), "w",
              encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(d, "transcript.md"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write("# AI Chat — Original chat\n\nstale transcript\n")
    with open(os.path.join(d, "meta.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    os.makedirs(os.path.join(d, "workspace"))
    with open(os.path.join(d, "workspace", "note.txt"), "w",
              encoding="utf-8") as f:
        f.write("kept through the copy")
    for name in ("outcome.json", "say.txt"):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
            json.dump({"sidecar": name}, f)
    return d, rows, meta


class ForkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="alloy-fork-test-")
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def forks(self, pattern="*-fork-*"):
        return [n for n in os.listdir(self.root) if "-fork-" in n] \
            if pattern else []

    def test_1_fork_whole_conversation(self):
        build_session(self.root, "src")
        out = fork.fork_session("src", sessions_dir=self.root)
        self.assertIn("ok", out, out)
        new_dir = os.path.join(self.root, out["id"])
        self.assertTrue(out["path"].startswith(self.root))
        self.assertTrue(os.path.isdir(new_dir))
        self.assertTrue(out["id"].startswith("src-fork-"))
        self.assertEqual(out["messages"], 4)
        rows = []
        with open(os.path.join(new_dir, "messages.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        self.assertEqual([r["message_id"] for r in rows],
                         ["m0", "m1", "m2", "m3"])
        with open(os.path.join(new_dir, "transcript.md"), encoding="utf-8") as f:
            tr = f.read()
        self.assertIn("body 3", tr)
        self.assertIn("# AI Chat — Original chat (fork)", tr)
        with open(os.path.join(new_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["id"], out["id"])
        self.assertEqual(os.path.basename(new_dir), out["id"])
        self.assertIn("(fork)", meta["title"])
        self.assertFalse(meta["ended"])
        self.assertEqual(meta["fork_of"], {"id": "src", "message_id": None})
        self.assertNotIn("children", meta)
        self.assertIsNone(meta["parent"])
        self.assertEqual(len(meta["seats"]), 1)
        self.assertNotIn("session_id", meta["seats"][0])
        self.assertEqual(meta["seats"][0]["label"], "Claude")
        self.assertFalse(os.path.exists(os.path.join(new_dir, "outcome.json")))
        self.assertFalse(os.path.exists(os.path.join(new_dir, "say.txt")))
        with open(os.path.join(new_dir, "workspace", "note.txt"),
                  encoding="utf-8") as f:
            self.assertEqual(f.read(), "kept through the copy")

    def test_2_fork_at_middle_message(self):
        _, rows, _ = build_session(self.root, "src")
        out = fork.fork_session("src", upto_message_id="m1",
                                sessions_dir=self.root)
        self.assertIn("ok", out, out)
        new_dir = os.path.join(self.root, out["id"])
        kept = []
        with open(os.path.join(new_dir, "messages.jsonl"), encoding="utf-8") as f:
            kept = [json.loads(line) for line in f if line.strip()]
        self.assertEqual([r["message_id"] for r in kept], ["m0", "m1"])
        with open(os.path.join(new_dir, "transcript.md"), encoding="utf-8") as f:
            tr = f.read()
        self.assertIn("body 0", tr)
        self.assertIn("body 1", tr)
        self.assertNotIn("body 2", tr)
        self.assertNotIn("body 3", tr)

    def test_3_source_untouched(self):
        src, _, _ = build_session(self.root, "src")
        with open(os.path.join(src, "messages.jsonl"), "rb") as f:
            msgs_before = f.read()
        with open(os.path.join(src, "meta.json"), "rb") as f:
            meta_before = f.read()
        with open(os.path.join(src, "transcript.md"), "rb") as f:
            tr_before = f.read()
        out = fork.fork_session("src", upto_message_id="m0",
                                sessions_dir=self.root)
        self.assertIn("ok", out, out)
        with open(os.path.join(src, "messages.jsonl"), "rb") as f:
            self.assertEqual(f.read(), msgs_before)
        with open(os.path.join(src, "meta.json"), "rb") as f:
            self.assertEqual(f.read(), meta_before)
        with open(os.path.join(src, "transcript.md"), "rb") as f:
            self.assertEqual(f.read(), tr_before)
        self.assertTrue(os.path.exists(os.path.join(src, "outcome.json")))
        self.assertTrue(os.path.exists(os.path.join(src, "say.txt")))

    def test_4_bad_message_id_errors_without_creating(self):
        build_session(self.root, "src")
        out = fork.fork_session("src", upto_message_id="nope",
                                sessions_dir=self.root)
        self.assertIn("error", out)
        self.assertNotIn("ok", out)
        self.assertEqual(self.forks(), [])

    def test_5_missing_and_legacy_sources_error(self):
        out = fork.fork_session("ghost", sessions_dir=self.root)
        self.assertIn("error", out)
        legacy = os.path.join(self.root, "legacy")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "transcript.md"), "w",
                  encoding="utf-8") as f:
            f.write("# AI Chat — old\n")
        out = fork.fork_session("legacy", sessions_dir=self.root)
        self.assertIn("error", out)
        self.assertIn("legacy", out["error"].lower())
        self.assertEqual(self.forks(), [])

    def test_6_collision_picks_next_suffix(self):
        build_session(self.root, "src")
        fixed = types.SimpleNamespace(
            datetime=types.SimpleNamespace(
                now=lambda: __import__("datetime").datetime(2026, 8, 25,
                                                            12, 30, 45)))
        original = fork.datetime
        fork.datetime = fixed
        try:
            first = fork.fork_session("src", sessions_dir=self.root)
            self.assertIn("ok", first, first)
            self.assertEqual(first["id"], "src-fork-123045")
            second = fork.fork_session("src", sessions_dir=self.root)
            self.assertIn("ok", second, second)
            self.assertEqual(second["id"], "src-fork-123045-2")
            third = fork.fork_session("src", upto_message_id="m1",
                                      sessions_dir=self.root)
            self.assertIn("ok", third, third)
            self.assertEqual(third["id"], "src-fork-123045-3")
        finally:
            fork.datetime = original

    def test_7_kept_rows_round_trip_identical(self):
        _, rows, _ = build_session(self.root, "src")
        out = fork.fork_session("src", upto_message_id="m2",
                                sessions_dir=self.root)
        new_dir = os.path.join(self.root, out["id"])
        kept = []
        with open(os.path.join(new_dir, "messages.jsonl"), encoding="utf-8") as f:
            kept = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(kept, rows[:3])

    def test_8_copy_failure_leaves_no_half_fork(self):
        build_session(self.root, "src")
        real_copytree = fork.shutil.copytree
        called = []

        def boom(*args, **kwargs):
            called.append(True)
            raise OSError("disk full")

        fork.shutil.copytree = boom
        try:
            out = fork.fork_session("src", sessions_dir=self.root)
        finally:
            fork.shutil.copytree = real_copytree
        self.assertTrue(called)
        self.assertIn("error", out)
        self.assertIn("disk full", out["error"])
        self.assertEqual(self.forks(), [])


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=2).result
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, "
          f"{len(result.failures) + len(result.errors)} failed")
    sys.exit(0 if result.wasSuccessful() else 1)
