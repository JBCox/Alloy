"""Static contracts for the transcript's user-controlled scrolling."""

import os
import re
import unittest
from html.parser import HTMLParser


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui", "index.html")


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.parents = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        marker = attrs.get("id") or attrs.get("class")
        if marker:
            self.parents[marker] = self.stack[-1] if self.stack else None
        if tag not in {"img", "input", "meta", "br", "hr", "link"}:
            self.stack.append(marker or tag)

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()


class TranscriptScrollContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UI, encoding="utf-8") as f:
            cls.source = f.read()

    def test_feed_and_jump_button_share_a_dedicated_viewport(self):
        tree = _Tree()
        tree.feed(self.source)
        self.assertEqual(tree.parents.get("feed"), "feed-wrap")
        self.assertEqual(tree.parents.get("jumpBtn"), "feed-wrap")
        self.assertIn('id="feed" tabindex="0"', self.source)

    def test_flex_chain_can_shrink_so_feed_remains_scrollable(self):
        self.assertRegex(
            self.source,
            r"\.stage\s*\{[^}]*min-height:\s*0[^}]*overflow:\s*hidden",
        )
        self.assertRegex(
            self.source,
            r"\.feed-wrap\s*\{[^}]*min-height:\s*0[^}]*overflow:\s*hidden",
        )
        self.assertRegex(
            self.source,
            r"#feed\s*\{[^}]*height:\s*100%[^}]*overflow-y:\s*auto",
        )

    def test_scroll_handlers_are_bound_once(self):
        calls = re.findall(r"^\s*bindFeedScroll\(\);", self.source, re.MULTILINE)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
