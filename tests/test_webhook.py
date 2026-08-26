"""Token-free tests for the local webhook trigger (webhook.py).

Everything here drives a REAL ThreadingHTTPServer bound to an ephemeral
loopback port through plain urllib -- no CLI is spawned, no engine is
imported, no model tokens are spent. The only "token" in play is the fake
shared secret this suite invents for its auth tests.

Run:  python tests/test_webhook.py
"""

import json
import os
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webhook


# --------------------------------------------------------------- plumbing --

_UNSET = object()


class RecordingCallback:
    """on_start stand-in: records payloads, answers a scripted result."""

    def __init__(self, result=_UNSET, exc=None):
        # Sentinel (not None) so a callback scripted to return None -- which
        # must serialize as bare {"ok": true} -- can actually do so.
        self.result = {"id": "abc"} if result is _UNSET else result
        self.exc = exc
        self.payloads = []
        self.lock = threading.Lock()

    def __call__(self, payload):
        with self.lock:
            self.payloads.append(payload)
        if self.exc is not None:
            raise self.exc
        return self.result


def _parse(raw):
    text = raw.decode("utf-8")
    try:
        return json.loads(text)
    except ValueError:
        return text


def _request(method, url, raw=None, json_body=None, headers=None):
    """One HTTP exchange -> (status, parsed-body). Never raises on 4xx/5xx."""
    data = raw
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, _parse(exc.read())
        finally:
            exc.close()          # else Python warns about the unread object


class WebhookCase(unittest.TestCase):
    """Shared harness: every server is ephemeral-port and always stopped."""

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for srv in self.servers:
            srv.stop()

    def serve(self, callback=None, token=None):
        cb = callback or RecordingCallback()
        srv = webhook.WebhookServer(cb, token=token)
        self.assertTrue(srv.start())
        self.servers.append(srv)
        return srv, cb

    def post_start(self, srv, raw=None, json_body=None, headers=None):
        return _request("POST", srv.url + "/start", raw=raw,
                        json_body=json_body, headers=headers)

    def assert_port_free(self, port):
        """The socket must really be released, not merely shut down."""
        last = None
        for _ in range(40):
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", port))
                probe.close()
                return
            except OSError as exc:
                last = exc
                probe.close()
                time.sleep(0.05)
        self.fail("port %d was never freed: %s" % (port, last))


# ------------------------------------------------------------ happy paths --

class HappyPathTests(WebhookCase):

    def test_minimal_topic_round_trips_and_sanitizes(self):
        srv, cb = self.serve()
        status, body = self.post_start(srv, json_body={"topic": "  ship it  "})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "id": "abc"})
        # The callback saw the STRIPPED topic and nothing else.
        self.assertEqual(cb.payloads, [{"topic": "ship it"}])

    def test_all_optional_keys_sanitize_into_callback_payload(self):
        srv, cb = self.serve()
        status, _ = self.post_start(srv, json_body={
            "topic": "fix the build",
            "seats": [" claude ", "gpt"],
            "turns": 12,
            "workspace": r"C:\tmp\proj",
        })
        self.assertEqual(status, 200)
        self.assertEqual(cb.payloads, [{
            "topic": "fix the build",
            "seats": ["claude", "gpt"],
            "turns": 12,
            "workspace": r"C:\tmp\proj",
        }])

    def test_topic_of_exactly_500_chars_accepted(self):
        srv, cb = self.serve()
        status, _ = self.post_start(srv, json_body={"topic": "a" * 500})
        self.assertEqual(status, 200)
        self.assertEqual(len(cb.payloads[0]["topic"]), 500)

    def test_callback_result_merges_under_ok_true(self):
        # The contract is {"ok": true, **result}: a callback may override
        # keys, which lets app.py answer {"ok": False, "why": ...} for a
        # refused launch while still using the 200 transport.
        srv, _ = self.serve(callback=RecordingCallback(
            result={"ok": False, "why": "busy"}))
        status, body = self.post_start(srv, json_body={"topic": "t"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": False, "why": "busy"})


# -------------------------------------------------------------- rejections --

class RejectionTests(WebhookCase):

    def setUp(self):
        super().setUp()
        self.srv, self.cb = self.serve()

    def reject(self, *args, **kwargs):
        status, body = self.post_start(self.srv, *args, **kwargs)
        self.assertEqual(status, 400, body)
        self.assertIn("error", body)
        return body["error"]

    def test_missing_or_nonstring_topic_rejected(self):
        for body in ({}, {"topic": 42}, {"topic": ["x"]}, {"topic": None}):
            err = self.reject(json_body=body)
            self.assertIn("Topic", err)

    def test_blank_topic_rejected(self):
        for topic in ("", "   ", "\n\t "):
            err = self.reject(json_body={"topic": topic})
            self.assertIn("empty", err)

    def test_oversized_topic_rejected(self):
        err = self.reject(json_body={"topic": "a" * 501})
        self.assertIn("500", err)

    def test_unknown_key_rejected_by_name(self):
        # Event-hooks culture: a typo'd key must look REJECTED, not accepted.
        err = self.reject(json_body={"topic": "t", "seat": "claude"})
        self.assertIn("'seat'", err)
        err = self.reject(json_body={"topic": "t", "topik": "x"})
        self.assertIn("'topik'", err)

    def test_seats_type_and_entry_validation(self):
        for seats in ("claude", [42], ["ok", ""], ["ok", None],
                      ["a" * 81]):
            err = self.reject(json_body={"topic": "t", "seats": seats})
            self.assertTrue(err.endswith("."))

    def test_seat_count_capped_at_8(self):
        eight = [{"topic": "t", "seats": ["s%d" % i for i in range(8)]}]
        status, _ = self.post_start(self.srv, json_body=eight[0])
        self.assertEqual(status, 200)
        err = self.reject(
            json_body={"topic": "t", "seats": ["s%d" % i for i in range(9)]})
        self.assertIn("8", err)

    def test_turns_range_type_and_bool_rejection(self):
        for turns in (0, -1, 501, "10", 2.5, True, False):
            err = self.reject(json_body={"topic": "t", "turns": turns})
            self.assertIn("Turns", err)
        for turns in (1, 500):
            status, _ = self.post_start(self.srv,
                                        json_body={"topic": "t",
                                                   "turns": turns})
            self.assertEqual(status, 200)

    def test_workspace_validation(self):
        for ws in (42, "", "   ", "w" * 301):
            err = self.reject(json_body={"topic": "t", "workspace": ws})
            self.assertIn("Workspace", err)
        status, _ = self.post_start(
            self.srv, json_body={"topic": "t", "workspace": "w" * 300})
        self.assertEqual(status, 200)

    def test_bad_json_rejected(self):
        err = self.reject(raw=b"{nope")
        self.assertIn("JSON", err)

    def test_non_object_json_rejected(self):
        for raw in (b"[1, 2]", b'"hello"', b"null", b"42"):
            err = self.reject(raw=raw)
            self.assertIn("object", err)

    def test_empty_body_rejected(self):
        err = self.reject(raw=b"")
        self.assertIn("JSON", err)

    def test_oversized_body_is_413(self):
        status, body = self.post_start(
            self.srv, raw=b"x" * (webhook.BODY_MAX + 1))
        self.assertEqual(status, 413, body)
        self.assertIn("error", body)

    def test_body_of_exactly_64k_accepted(self):
        compact = json.dumps({"topic": "t"}).encode("utf-8")
        padded = compact + b" " * (webhook.BODY_MAX - len(compact))
        self.assertEqual(len(padded), webhook.BODY_MAX)
        status, _ = self.post_start(self.srv, raw=padded)
        self.assertEqual(status, 200)


# ------------------------------------------------------------------- auth --

class AuthTests(WebhookCase):

    SECRET = "s3cret-token"

    def test_correct_token_allowed(self):
        srv, cb = self.serve(token=self.SECRET)
        status, body = self.post_start(
            srv, json_body={"topic": "t"}, headers={webhook.TOKEN_HEADER:
                                                    self.SECRET})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(cb.payloads), 1)

    def test_wrong_token_401(self):
        srv, cb = self.serve(token=self.SECRET)
        for wrong in ("S3cret-token",          # compare_digest is exact
                      self.SECRET + " ", "nope", ""):
            status, body = self.post_start(
                srv, json_body={"topic": "t"},
                headers={webhook.TOKEN_HEADER: wrong})
            self.assertEqual(status, 401, (wrong, body))
            self.assertIn(webhook.TOKEN_HEADER, body["error"])
        self.assertEqual(cb.payloads, [])

    def test_missing_token_401(self):
        srv, cb = self.serve(token=self.SECRET)
        status, body = self.post_start(srv, json_body={"topic": "t"})
        self.assertEqual(status, 401)
        self.assertEqual(cb.payloads, [])

    def test_without_token_no_header_needed(self):
        srv, cb = self.serve(token=None)
        status, _ = self.post_start(srv, json_body={"topic": "t"})
        self.assertEqual(status, 200)
        self.assertEqual(len(cb.payloads), 1)

    def test_bogus_header_ignored_without_token(self):
        srv, _ = self.serve(token=None)
        status, _ = self.post_start(
            srv, json_body={"topic": "t"},
            headers={webhook.TOKEN_HEADER: "whatever"})
        self.assertEqual(status, 200)

    def test_health_open_with_token_set(self):
        srv, _ = self.serve(token=self.SECRET)
        status, body = _request("GET", srv.url + "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "started": False})


# ---------------------------------------------------------------- routing --

class RoutingTests(WebhookCase):

    def test_health_reports_not_started_then_started(self):
        srv, _ = self.serve()
        status, body = _request("GET", srv.url + "/health")
        self.assertEqual((status, body), (200, {"ok": True, "started": False}))
        status, _ = self.post_start(srv, json_body={"topic": "t"})
        self.assertEqual(status, 200)
        _, body = _request("GET", srv.url + "/health")
        self.assertEqual(body, {"ok": True, "started": True})

    def test_post_health_404(self):
        srv, _ = self.serve()
        status, body = self.post_start(srv, json_body={"topic": "t"})
        # aim the SAME post shape at /health instead
        status, body = _request("POST", srv.url + "/health",
                                json_body={"topic": "t"})
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_get_start_404_wrong_method(self):
        srv, _ = self.serve()
        status, body = _request("GET", srv.url + "/start")
        self.assertEqual(status, 404)

    def test_unknown_path_404(self):
        srv, _ = self.serve()
        status, _ = _request("GET", srv.url + "/nope")
        self.assertEqual(status, 404)
        status, _ = _request("GET", srv.url + "/")
        self.assertEqual(status, 404)

    def test_other_methods_404(self):
        srv, _ = self.serve()
        for method in ("DELETE", "PUT"):
            req = urllib.request.Request(srv.url + "/start", method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    code = resp.status
            except urllib.error.HTTPError as exc:
                try:
                    code = exc.code
                finally:
                    exc.close()
            self.assertEqual(code, 404, method)

    def test_query_string_tolerated_on_health(self):
        srv, _ = self.serve()
        status, body = _request("GET", srv.url + "/health?probe=1")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])


# --------------------------------------------------------------- callback --

class CallbackTests(WebhookCase):

    def test_exception_becomes_500_sentence_and_survives(self):
        srv, _ = self.serve(callback=RecordingCallback(
            exc=RuntimeError("disk on fire")))
        status, body = self.post_start(srv, json_body={"topic": "t"})
        self.assertEqual(status, 500)
        self.assertEqual(body["error"],
                         "Start failed (RuntimeError): disk on fire")
        self.assertNotIn("Traceback", json.dumps(body))
        # The server must still be alive for the next trigger.
        status, _ = self.post_start(srv, json_body={"topic": "again"})
        self.assertEqual(status, 500)      # scripted failure again...
        srv.on_start = RecordingCallback() # ...so swap in a healthy callback
        status, _ = self.post_start(srv, json_body={"topic": "third"})
        self.assertEqual(status, 200)

    def test_long_multiline_exception_flattens_and_caps(self):
        boom = RuntimeError("\n".join(["x" * 80] * 20))
        srv, _ = self.serve(callback=RecordingCallback(exc=boom))
        status, body = self.post_start(srv, json_body={"topic": "t"})
        self.assertEqual(status, 500)
        err = body["error"]
        self.assertLessEqual(len(err), 340)
        self.assertNotIn("\n", err)

    def test_non_dict_return_is_500(self):
        srv, _ = self.serve(callback=RecordingCallback(result="hello"))
        status, body = self.post_start(srv, json_body={"topic": "t"})
        self.assertEqual(status, 500)
        self.assertIn("did not return an object", body["error"])

    def test_none_return_is_rejected_never_forged(self):
        # A callback that returns nothing has NOT stated a success; answering
        # {"ok": true} on its behalf would be the never-forge-a-turn rule,
        # one layer out. It is a 500 and a log line instead.
        srv, _ = self.serve(callback=RecordingCallback(result=None))
        status, body = self.post_start(srv, json_body={"topic": "t"})
        self.assertEqual(status, 500)
        self.assertIn("did not return an object", body["error"])


# --------------------------------------------------------------- lifecycle --

class LifecycleTests(WebhookCase):

    def test_stop_releases_the_socket(self):
        cb = RecordingCallback()
        srv = webhook.WebhookServer(cb)
        self.assertTrue(srv.start())
        port = srv.port
        srv.stop()
        self.assert_port_free(port)

    def test_double_stop_safe(self):
        srv = webhook.WebhookServer(RecordingCallback())
        srv.start()
        srv.stop()
        srv.stop()          # must be a quiet no-op, not an exception

    def test_stop_before_start_safe(self):
        srv = webhook.WebhookServer(RecordingCallback())
        srv.stop()

    def test_busy_port_returns_false_and_stays_stopped(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            taken = blocker.getsockname()[1]
            cb = RecordingCallback()
            srv = webhook.WebhookServer(cb, port=taken)
            self.servers.append(srv)
            self.assertFalse(srv.start())
            self.assertIsNone(srv.port)
            self.assertIsNone(srv.url)
            self.assertEqual(cb.payloads, [])
            self.assertFalse(srv.started)
        finally:
            blocker.close()

    def test_context_manager_stops_on_exit(self):
        cb = RecordingCallback()
        with webhook.WebhookServer(cb) as srv:
            self.assertTrue(srv.start())
            port = srv.port
            status, _ = _request("GET", srv.url + "/health")
            self.assertEqual(status, 200)
        self.assert_port_free(port)

    def test_non_loopback_hosts_refused(self):
        for host in ("0.0.0.0", "10.0.0.1", "192.168.1.5", ""):
            with self.assertRaises(ValueError, msg=host):
                webhook.WebhookServer(RecordingCallback(), host=host)

    def test_loopback_aliases_accepted(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            srv = webhook.WebhookServer(RecordingCallback(), host=host)
            self.servers.append(srv)

    def test_url_and_port_hold_real_bound_values(self):
        srv, _ = self.serve()
        self.assertIsInstance(srv.port, int)
        self.assertGreater(srv.port, 0)
        self.assertEqual(srv.url, "http://127.0.0.1:%d" % srv.port)

    def test_second_start_is_idempotent(self):
        srv, _ = self.serve()
        self.assertTrue(srv.start())       # already serving
        status, _ = _request("GET", srv.url + "/health")
        self.assertEqual(status, 200)


# ------------------------------------------------------------- concurrency --

class ConcurrencyTests(WebhookCase):

    def test_five_parallel_posts_all_answered(self):
        srv, cb = self.serve()
        topics = ["trigger-%d" % i for i in range(5)]

        def fire(topic):
            return self.post_start(srv, json_body={"topic": topic})

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(fire, topics))

        for topic, (status, body) in zip(topics, results):
            self.assertEqual(status, 200, (topic, body))
            self.assertEqual(body, {"ok": True, "id": "abc"}, topic)
        with cb.lock:
            seen = sorted(p["topic"] for p in cb.payloads)
        self.assertEqual(seen, sorted(topics))


if __name__ == "__main__":
    unittest.main(verbosity=1)
