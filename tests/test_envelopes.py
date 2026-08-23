"""V2 message-envelope, addressing, artifact, and history contracts."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import relay
from test_loop import RecordingIO, build_state, jsonl_rows


class FakeDigest:
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.last_usage = {"input_tokens": 20, "output_tokens": 5,
                           "total_tokens": 25, "cost_usd": 0.001}
        self.session_id = None

    def turn(self, prompt):
        if self.error:
            raise self.error
        return self.reply


class EnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai-chat-envelope-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_broadcast_rows_record_intended_and_actual_delivery(self):
        state = build_state(self.tmp, [["a"], ["b"], ["c"]], turns=1,
                            labels=["A", "B", "C"])
        relay.run_rounds(state, RecordingIO())
        rows = [r for r in jsonl_rows(state) if r["origin"] == "seat"]
        self.assertEqual([r["audience"] for r in rows], ["*", "*", "*"])
        self.assertEqual([r["delivered_to"] for r in rows],
                         [[1, 2], [0, 2], [0, 1]])
        self.assertEqual(len({r["message_id"] for r in rows}), 3)

    def test_explicit_addressing_narrows_delivery_and_prompt_history(self):
        state = build_state(
            self.tmp, [["private [[TO: B]]"], ["b"], ["c"]], turns=1,
            labels=["A", "B", "C"])
        state["_digest_summarizer"] = FakeDigest("A raised a scoped point.")
        relay.run_rounds(state, RecordingIO())
        all_rows = jsonl_rows(state)
        rows = [r for r in all_rows if r["origin"] == "seat"]
        self.assertEqual(rows[0]["audience"], [1])
        self.assertEqual(rows[0]["delivered_to"], [1])
        self.assertIn("A said:\nprivate [[TO: B]]", state["agents"][1].prompts[0])
        self.assertNotIn("private", state["agents"][2].prompts[0])
        digest = next(r for r in all_rows if r.get("digest_of"))
        self.assertEqual(digest["origin"], "relay")
        self.assertEqual(digest["speaker"], "system")
        self.assertEqual(digest["delivered_to"], [2])
        self.assertEqual(digest["digest_of"], [rows[0]["message_id"]])
        self.assertIn("A raised a scoped point", state["agents"][2].prompts[0])
        self.assertEqual(state["usage"]["by_kind"]["digest"]["calls"], 1)
        self.assertEqual([r["text"] for r in relay.seat_history(rows, 2)],
                         ["b", "c"])

    def test_digest_failure_falls_back_to_verbatim_source_without_loss(self):
        state = build_state(
            self.tmp, [["exact hidden [[TO: B]]"], ["b"], ["c"]], turns=1,
            labels=["A", "B", "C"])
        state["_digest_summarizer"] = FakeDigest(
            error=RuntimeError("summarizer offline"))
        relay.run_rounds(state, RecordingIO())
        rows = jsonl_rows(state)
        source = next(r for r in rows if r.get("intent") != "status"
                      and r.get("speaker") == 0)
        fallback = next(r for r in rows if r.get("digest_of"))
        self.assertEqual(fallback["digest_of"], [source["message_id"]])
        self.assertIn("original hidden messages, verbatim", fallback["text"])
        self.assertIn("exact hidden [[TO: B]]", fallback["text"])
        self.assertIn("exact hidden [[TO: B]]", state["agents"][2].prompts[0])
        self.assertEqual(state["hidden"]["2"], [])

    def test_invalid_or_self_only_address_falls_back_to_broadcast(self):
        state = build_state(self.tmp, [["a [[TO: A]]"], ["b"], ["c"]],
                            turns=1, labels=["A", "B", "C"])
        io = RecordingIO()
        relay.run_rounds(state, io)
        first = next(r for r in jsonl_rows(state) if r["origin"] == "seat")
        self.assertEqual(first["audience"], "*")
        self.assertEqual(first["delivered_to"], [1, 2])
        self.assertTrue(any("broadcasting normally" in p.get("text", "")
                            for event, p in io.events if event == "status"))

    def test_human_and_relay_origins_are_not_forged(self):
        state = build_state(self.tmp, [["a"], ["b"]], turns=1,
                            labels=["A", "B"])
        human = state["log"]("Josh (human)", "hello")
        relay_row = state["log"]("relay", "notice")
        self.assertEqual((human["origin"], human["audience"],
                          human["delivered_to"]),
                         ("human", "*", [0, 1]))
        self.assertEqual((relay_row["origin"], relay_row["audience"],
                          relay_row["delivered_to"]),
                         ("relay", [], []))

    def test_verified_edit_activity_creates_confined_artifact_descriptor(self):
        workspace = os.path.join(self.tmp, "workspace")
        os.makedirs(workspace)
        target = os.path.join(workspace, "result.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("done")
        state = build_state(
            self.tmp,
            [[("made it", [{"kind": "edit", "text": "editing result.txt",
                             "path_raw": target}])], ["ack"]],
            turns=1, labels=["A", "B"], workspace=workspace)
        relay.run_rounds(state, RecordingIO())
        row = next(r for r in jsonl_rows(state) if r["speaker"] == 0)
        artifact = row["artifacts"][0]
        self.assertEqual(artifact["path"], "result.txt")
        self.assertEqual(artifact["source_message_id"], row["message_id"])
        self.assertEqual(artifact["producer"], 0)
        self.assertEqual(artifact["size"], 4)

    def test_artifacts_reject_escape_and_missing_paths(self):
        workspace = os.path.join(self.tmp, "workspace")
        os.makedirs(workspace)
        found = relay.artifact_descriptors(
            workspace,
            [{"path": r"..\outside.txt"}, {"path": "missing.txt"}],
            0, "message")
        self.assertEqual(found, [])

    def test_legacy_rows_remain_visible_in_every_seat_history(self):
        legacy = {"speaker": 0, "text": "old broadcast"}
        hidden = {"speaker": 0, "text": "private", "delivered_to": [1]}
        own = {"speaker": 2, "text": "own", "delivered_to": []}
        self.assertEqual(relay.seat_history([legacy, hidden, own], 2),
                         [legacy, own])


class EnvelopeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "ui", "index.html"), encoding="utf-8") as f:
            cls.source = f.read()

    def test_delivery_badge_and_history_lens_use_persisted_fields(self):
        self.assertIn('id="historyLens"', self.source)
        self.assertIn('class="delivery-pill"', self.source)
        self.assertIn("row.delivered_to", self.source)
        self.assertIn("message.dataset.speaker === seatId", self.source)
        self.assertIn("delivered.includes(seatId)", self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
