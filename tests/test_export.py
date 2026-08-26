"""HTML transcript export tests — token-free. Run: python tests/test_export.py"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import export


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="alloy-export-")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def session(self, name, rows=None, meta=None, files=()):
        d = os.path.join(self.root, name)
        os.makedirs(d)
        if rows is not None:
            with open(os.path.join(d, "messages.jsonl"), "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
        if meta is not None:
            with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f)
        for fname, content in files:
            with open(os.path.join(d, fname), "w", encoding="utf-8") as f:
                f.write(content)
        return d

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    ROW = {"message_id": "m1", "origin": "human", "audience": "*",
           "delivered_to": [], "speaker": "josh", "provider": None,
           "name": "Josh", "text": "hello", "round": 0, "meta": None,
           "role": None, "ts": "2026-08-22T14:02:11"}

    META = {"title": "Test chat", "created": "2026-08-22T14:00:00",
            "updated": "2026-08-22T15:00:00", "mode": "round_robin",
            "seats": [{"provider": "claude", "label": "Claude",
                       "model": "claude-opus-5", "effort": "high"}]}

    def test_happy_path(self):
        rows = [
            dict(self.ROW),
            dict(self.ROW, message_id="m2", origin="relay", speaker="system",
                 name="System", text="note"),
            dict(self.ROW, message_id="m3", origin="seat", speaker="c1",
                 provider="claude", name="Claude", text="hi Josh", round=1,
                 role="Researcher", meta="helper for Claude", ts="2026-08-22T14:05:09",
                 activity=[{"kind": "tool", "text": "ran ls"},
                           {"kind": "edit", "text": "wrote a.py"}]),
            dict(self.ROW, message_id="m4", origin="seat", speaker="g1",
                 provider="gpt", name="GPT", text="agreed", round=2,
                 usage={"cost_usd": 0.01, "input_tokens": 100,
                        "output_tokens": 50}),
        ]
        d = self.session("s1", rows=rows, meta=self.META)
        res = export.export_session(d)
        self.assertTrue(res["ok"])
        self.assertEqual(res["messages"], 4)
        self.assertEqual(res["path"], os.path.abspath(os.path.join(d, "export.html")))
        html_out = self.read(res["path"])
        self.assertIn("Test chat", html_out)
        self.assertIn("14:02", html_out)
        self.assertIn("14:05", html_out)
        self.assertIn("Claude &middot; claude &middot; claude-opus-5 (high)", html_out)
        self.assertIn("helper for Claude", html_out)
        self.assertIn("Worked through 2 steps", html_out)
        self.assertIn("ran ls", html_out)
        self.assertIn("cost_usd: 0.01", html_out)
        self.assertIn("input_tokens: 100", html_out)
        self.assertEqual(html_out.count("<article class='msg'>"), 4)
        # provider colors
        self.assertIn("#D97757", html_out)
        self.assertIn("#10A37F", html_out)

    def test_escaping(self):
        rows = [dict(self.ROW, text="<script>alert(1)</script>"),
                dict(self.ROW, message_id="m2", origin="seat", speaker="c1",
                     provider="claude", name="Claude<b>", text="ok")]
        d = self.session("s2", rows=rows,
                         meta=dict(self.META, title='<>&"quoted"'))
        res = export.export_session(d)
        self.assertTrue(res["ok"])
        html_out = self.read(res["path"])
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_out)
        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertIn("&lt;&gt;&amp;&quot;quoted&quot;", html_out)
        self.assertIn("Claude&lt;b&gt;", html_out)

    def test_legacy_folder(self):
        d = self.session("s3", files=[("transcript.md", "# old chat\n\nbody here")])
        res = export.export_session(d)
        self.assertTrue(res["ok"])
        self.assertEqual(res["messages"], 0)
        html_out = self.read(res["path"])
        self.assertIn("<pre class='legacy'>", html_out)
        self.assertIn("# old chat", html_out)
        self.assertIn(">s3<", html_out)  # header from folder name

    def test_empty_conversation(self):
        d = self.session("s4", rows=[], meta=self.META)
        res = export.export_session(d)
        self.assertTrue(res["ok"])
        self.assertEqual(res["messages"], 0)
        html_out = self.read(res["path"])
        self.assertEqual(html_out.count("<article class='msg'>"), 0)

    def test_malformed_lines_skipped(self):
        d = self.session("s5")
        with open(os.path.join(d, "messages.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(dict(self.ROW)) + "\n")
            f.write("{not json at all\n")
            f.write(json.dumps(dict(self.ROW, message_id="m2")) + "\n")
        res = export.export_session(d)
        self.assertTrue(res["ok"])
        self.assertEqual(res["messages"], 2)

    def test_errors(self):
        res = export.export_session(os.path.join(self.root, "nope"))
        self.assertIn("error", res)
        d = self.session("s6")  # neither file
        res = export.export_session(d)
        self.assertIn("error", res)
        self.assertIn("neither messages.jsonl", res["error"])

    def test_custom_out_path_creates_parents(self):
        d = self.session("s7", rows=[dict(self.ROW)], meta=self.META)
        out = os.path.join(self.root, "deep", "nested", "out.html")
        res = export.export_session(d, out_path=out)
        self.assertTrue(res["ok"])
        self.assertTrue(os.path.isfile(out))
        self.assertEqual(os.path.abspath(out), res["path"])

    def test_deterministic_output(self):
        rows = [dict(self.ROW),
                dict(self.ROW, message_id="m2", origin="seat", speaker="c1",
                     provider="opencode", name="Ox Alpha", text="hey",
                     ts="2026-08-22T16:30:00")]
        d = self.session("s8", rows=rows, meta=self.META)
        p1 = os.path.join(self.root, "a.html")
        p2 = os.path.join(self.root, "b.html")
        r1 = export.export_session(d, out_path=p1)
        r2 = export.export_session(d, out_path=p2)
        self.assertTrue(r1["ok"] and r2["ok"])
        with open(p1, "rb") as f1, open(p2, "rb") as f2:
            self.assertEqual(f1.read(), f2.read())


if __name__ == "__main__":
    unittest.main()
