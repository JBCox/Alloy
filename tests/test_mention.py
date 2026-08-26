"""Token-free tests for the human @-mention (feature #26).

"@SeatName rest" typed by Josh is delivered to that ONE seat only. Routing
lives in the engine (relay.parse_mention + relay.enqueue_josh_message — the
same funnel every loop drain site uses, and the same matcher /clear and
[[TO:]] use), so a loop-level test proves it; the app's opener seeding is
covered through a headless Api. The UI chip is a mirror only.

Run:  python tests/test_mention.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from relay import Agent
from test_loop import FakeAgent, RecordingIO, build_state, saved_meta


class ParseMentionTests(unittest.TestCase):
    def setUp(self):
        self.agents = [FakeAgent("", [], name=n) for n in
                       ("Claude", "Claude 2", "GPT")]

    def test_exact_and_case_insensitive(self):
        idx, rest = relay.parse_mention("@gpt check this", self.agents)
        self.assertEqual((idx, rest), (2, "check this"))
        idx, rest = relay.parse_mention("@GPT check this", self.agents)
        self.assertEqual((idx, rest), (2, "check this"))

    def test_multiword_label_longest_match(self):
        idx, rest = relay.parse_mention("@Claude 2 hello there", self.agents)
        self.assertEqual((idx, rest), (1, "hello there"))
        # plain "@Claude" must NOT be read as its multi-word neighbour
        idx, rest = relay.parse_mention("@Claude hi", self.agents)
        self.assertEqual(idx, 0)

    def test_no_match_stays_literal(self):
        for text in ("@nobody hello", "@claude", "email me @ home",
                     "", None, "@Claude"):
            idx, rest = relay.parse_mention(text, self.agents)
            self.assertIsNone(idx, repr(text))
            self.assertEqual(rest, text or "")

    def test_ambiguous_provider_stays_literal(self):
        # two ox seats share provider "ox": an @ox mention cannot pick one
        ox = [FakeAgent("", [], name="Alpha"), FakeAgent("", [], name="Beta")]
        idx, _ = relay.parse_mention("@ox do it", ox)
        self.assertIsNone(idx)


class LoopMentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-mention-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mentioned_seat_hears_it_others_do_not(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1,
                            labels=["Claude", "GPT"])
        io = RecordingIO(human_script=[["@gpt please look at this"]])
        relay.run_rounds(state, io)
        gpt = state["agents"][1]
        self.assertIn("Josh (human) says to you: please look at this",
                      gpt.prompts[0])
        # the other seat never received it — not even as a broadcast echo
        self.assertNotIn("please look at this",
                         state["agents"][0].prompts[0].replace(
                             "@gpt please look at this", ""))
        rows = [r for r in self._josh_rows(state)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audience"],
                         [state["slot_ids"][1]])
        self.assertEqual(rows[0]["text"], "@gpt please look at this")

    def test_unmatched_mention_broadcasts_literally(self):
        state = build_state(self.tmp, [["a1"], ["b1"]], turns=1)
        io = RecordingIO(human_script=[["@nobody hello everyone"]])
        relay.run_rounds(state, io)
        for a in state["agents"]:
            self.assertIn("Josh (human) interjects: @nobody hello everyone",
                          a.prompts[0])
        rows = list(self._josh_rows(state))
        self.assertEqual(rows[0]["audience"], "*")

    def test_free_mode_routes_the_mention(self):
        # Delivery is asynchronous in free mode: the coordinator enqueues at
        # its own pace while seats spend their budget, so accept EITHER the
        # consumed form (a prompt) or the queued form (still owed). What must
        # never happen is the OTHER seat hearing it.
        state = build_state(self.tmp, [["a1", "a2", "a3"], ["b1", "b2"]],
                            turns=5, labels=["Claude", "GPT"])
        state["mode"] = "free"
        io = RecordingIO(human_script=[[], ["@Claude just you"]])
        relay.run_rounds(state, io)
        got = ("Josh (human) says to you: just you")
        claude_had = any(got in p for p in state["agents"][0].prompts) or \
            any(got in q for q in state["pending"][0])
        self.assertTrue(claude_had)
        gpt_texts = [p.replace("@Claude just you", "")
                     for p in state["agents"][1].prompts]
        self.assertFalse(any(got in p for p in gpt_texts))
        self.assertFalse(any(got in q for q in state["pending"][1]))

    def _josh_rows(self, state):
        with open(state["store"].messages, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    if row.get("speaker") == "josh":
                        yield row


try:
    import app
    from test_app_headless import FakeWindow, scripted_agent_class
    _has_app = True
except Exception:                                    # pragma: no cover
    _has_app = False


@unittest.skipUnless(_has_app, "app/test_app_headless import failed")
class OpenerMentionTests(unittest.TestCase):
    """The app seeds openers OUTSIDE the loop, so the mention has to be
    parsed there too — proven against the real Api, headless."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-opener-")
        self._old_sessions = app.SESSIONS_DIR
        app.SESSIONS_DIR = self.tmp
        self._old_types = dict(relay.AGENT_TYPES)
        self.seen = {}                      # seat name -> prompts received

        def recording_class(name_):
            seen = self.seen.setdefault(name_, [])

            class Scripted(Agent):
                cli = "fake"

                def turn(self, message, on_activity=None):
                    seen.append(message)
                    self.session_id = f"fake-session-{self.uid}"
                    return f"{name_.lower()}-reply"

            Scripted.name = name_
            return Scripted

        relay.AGENT_TYPES["claude"] = recording_class("Claude")
        relay.AGENT_TYPES["gpt"] = recording_class("GPT")

    def tearDown(self):
        app.SESSIONS_DIR = self._old_sessions
        relay.AGENT_TYPES.clear()
        relay.AGENT_TYPES.update(self._old_types)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _start(self, opener):
        api = app.Api()
        api._window = FakeWindow()
        api._conversation({"opener": opener, "turns": 1,
                           "seats": [{"id": 0, "provider": "claude",
                                      "enabled": True},
                                     {"id": 1, "provider": "gpt",
                                      "enabled": True}]})
        api._emit_q.join()
        return api

    def _josh_rows(self, api):
        with open(os.path.join(api._session_dir, "messages.jsonl"),
                  encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    if row.get("speaker") == "josh":
                        yield row

    def test_opener_mention_queues_to_one_seat(self):
        api = self._start("@GPT you take this one alone")
        rows = list(self._josh_rows(api))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audience"], [1])
        # the named seat got it as a queued message...
        self.assertTrue(any("Josh (human) opens the conversation: you take "
                            "this one alone" in p for p in self.seen["GPT"]))
        # ...the other seat opened blind (its preamble may still carry the
        # Topic line, so match the QUEUE wording, not any substring)
        self.assertFalse(any("Josh (human)" in p
                             for p in self.seen["Claude"]))

    def test_plain_opener_still_broadcasts(self):
        api = self._start("hello everyone")
        rows = list(self._josh_rows(api))
        self.assertEqual(rows[0]["audience"], "*")


if __name__ == "__main__":
    unittest.main(verbosity=2)
