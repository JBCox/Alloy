"""Local webhook trigger: let OUTSIDE events start an Alloy conversation.

A CI job fails, Task Scheduler fires, a script finishes -- anything that can
POST can now open a chat without touching the GUI:

    srv = WebhookServer(start_chat)        # loopback-only, by construction
    srv.start()                            # serves on a daemon thread
    # ... elsewhere: POST http://127.0.0.1:<port>/start  {"topic": "fix it"}
    srv.stop()

`start_chat(payload) -> dict` is caller-supplied (app.py wires it to actually
launching a conversation). Its return value is merged under {"ok": True} and
serialized back; if it raises, the caller gets {"error": "<sentence>"} with
status 500 and the traceback goes to the LOG, never to the wire.

Security posture -- this module's reason to exist:

- LOOPBACK ONLY, by construction. Any host that resolves to a non-loopback
  address raises ValueError before a socket exists. The check is resolution
  (getaddrinfo), not string matching, so "localhost" passes while a LAN
  hostname or "0.0.0.0" fails: on Windows SO_REUSEADDR makes a wildcard bind
  genuinely reachable from the network, and a remote-code-START endpoint must
  never depend on callers remembering that.
- Optional SHARED TOKEN: pass `token="..."` and every POST /start must carry
  an exactly-matching X-Alloy-Token header, compared with
  hmac.compare_digest (never ==, whose early-exit leaks prefix matches by
  timing). GET /health stays open so monitoring probes need no secret.
- NO TLS, deliberately: loopback traffic never leaves the machine, so
  encrypting it protects nothing against anyone who could already read this
  process's memory. A cert story would only add self-signed-trust mess. If a
  REMOTE trigger is ever wanted, put an authenticating reverse proxy in
  front; do not grow this file into a TLS endpoint.

Validation follows the event-hooks culture (relay.write_event_hooks):
unknown top-level keys REJECT loudly -- a typo'd "seatz" would otherwise look
accepted and silently do nothing -- and every rejection answers with one
plain sentence naming what was wrong.

Standalone stdlib-only module: imports nothing from relay/app, so tests never
load the engine and app.py can embed this without pulling the world in.
"""

import hmac
import ipaddress
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_log = logging.getLogger("alloy.webhook")

BODY_MAX = 64 * 1024          # POST bodies larger than this are refused
_DRAIN_CAP = 8 * 1024 * 1024  # ...but drained up to here first, so the 413
                              # actually arrives before any socket reset
TOPIC_MAX = 500               # a topic is a sentence, not a document
SEATS_MAX = 8                 # the stage itself caps practical rosters lower
SEAT_MAX = 80                 # room for "claude:claude-haiku-4-5:low=Label"
TURNS_MIN = 1
TURNS_MAX = 500
WORKSPACE_MAX = 300           # Windows paths run long; this is generous
OPTIONAL_KEYS = ("seats", "turns", "workspace")
TOKEN_HEADER = "X-Alloy-Token"
ERROR_EXCERPT_MAX = 300       # exception detail allowed into a response body

_NOT_FOUND = "Not found."


class _PayloadError(ValueError):
    """A rejected /start body. The message is always one complete sentence."""


def _require_loopback(host):
    """Return `host` unchanged if it ONLY ever resolves to a loopback address.

    Raises ValueError otherwise. This is the load-bearing wall of the module:
    everything else here assumes the server cannot be reached from another
    machine. Resolution rather than string comparison means "localhost" and
    "::1" pass, "example.com" (public IPs) and "10.0.0.1" fail, and
    "0.0.0.0" fails -- which matters most on Windows, where SO_REUSEADDR
    semantics make a wildcard bind reachable from other hosts.
    """
    name = str(host or "").strip()
    if not name:
        raise ValueError("Webhook host must be given (use the default).")
    try:
        infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError("Webhook host %r does not resolve." % name) from exc
    for info in infos:
        addr = info[4][0].split("%", 1)[0]   # drop an IPv6 zone index if any
        if not ipaddress.ip_address(addr).is_loopback:
            raise ValueError(
                "Webhook host %r resolves to non-loopback address %s -- "
                "refusing to listen beyond this machine." % (name, addr))
    return name


def _format_url(host, port):
    host_part = "[%s]" % host if ":" in host else host   # bare IPv6 needs []
    return "http://%s:%d" % (host_part, port)


def sanitize_payload(data):
    """Validate one parsed /start body into the exact dict on_start receives.

    Raises _PayloadError for every rejection. Only keys present in the request
    appear in the result (topic always; optional keys only when sent), so the
    callback can distinguish "not asked" from "asked with defaults".
    """
    if not isinstance(data, dict):
        raise _PayloadError("The /start body must be a JSON object.")
    unknown = sorted(set(map(str, data)) - {"topic"} - set(OPTIONAL_KEYS))
    if unknown:
        raise _PayloadError(
            "Unknown key(s) %s -- expected: topic, seats, turns, workspace."
            % ", ".join(repr(k) for k in unknown))

    out = {}
    topic = data.get("topic")
    if not isinstance(topic, str):
        raise _PayloadError("Topic is required and must be a string.")
    topic = topic.strip()
    if not topic:
        raise _PayloadError("Topic must not be empty.")
    if len(topic) > TOPIC_MAX:
        raise _PayloadError("Topic is limited to %d characters." % TOPIC_MAX)
    out["topic"] = topic

    if "seats" in data:
        seats = data["seats"]
        if not isinstance(seats, list):
            raise _PayloadError("Seats must be a list of short strings.")
        clean = []
        for seat in seats:
            if not isinstance(seat, str):
                raise _PayloadError("Every seat must be a string.")
            seat = seat.strip()
            if not seat:
                raise _PayloadError("Seat names must not be empty.")
            if len(seat) > SEAT_MAX:
                raise _PayloadError(
                    "Seat names are limited to %d characters." % SEAT_MAX)
            clean.append(seat)
        if len(clean) > SEATS_MAX:
            raise _PayloadError("At most %d seats may be requested."
                                % SEATS_MAX)
        out["seats"] = clean

    if "turns" in data:
        turns = data["turns"]
        # bool is an int subclass in Python: without the explicit check,
        # True would sail through isinstance(turns, int) and mean 1 turn.
        if isinstance(turns, bool) or not isinstance(turns, int):
            raise _PayloadError("Turns must be an integer.")
        if not TURNS_MIN <= turns <= TURNS_MAX:
            raise _PayloadError("Turns must be between %d and %d."
                                % (TURNS_MIN, TURNS_MAX))
        out["turns"] = turns

    if "workspace" in data:
        workspace = data["workspace"]
        if not isinstance(workspace, str):
            raise _PayloadError("Workspace must be a string path.")
        workspace = workspace.strip()
        if not workspace:
            raise _PayloadError("Workspace must not be empty.")
        # Existence/containment is NOT checked here: resolving a real folder
        # is the app's job at start time, and probing the filesystem from a
        # request handler would turn a validation error into an oracle about
        # which paths exist.
        if len(workspace) > WORKSPACE_MAX:
            raise _PayloadError("Workspace path is limited to %d characters."
                                % WORKSPACE_MAX)
        out["workspace"] = workspace

    return out


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"     # every reply carries Content-Length
    server_version = "AlloyWebhook/1"

    def log_message(self, fmt, *args):
        # BaseHTTPRequestHandler logs to stderr; house rule is the logger.
        _log.debug("%s %s", self.address_string(), fmt % args)

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        self._send_json(404, {"error": _NOT_FOUND})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            # Deliberately unauthenticated even when a token is set: a health
            # probe reveals nothing but liveness, and monitors need no secret.
            return self._send_json(200, {"ok": True,
                                         "started": self.server.facade.started})
        self._not_found()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/start":
            return self._handle_start()
        self._not_found()

    # Anything but GET/POST on these two routes is simply not a feature;
    # answering 404 (not 405) keeps the surface describable in one sentence.
    do_PUT = _not_found
    do_DELETE = _not_found
    do_PATCH = _not_found

    def _handle_start(self):
        facade = self.server.facade

        # Auth FIRST, before reading a byte of body: an unauthenticated
        # request should buy nothing, not even parse work.
        if facade.token is not None:
            provided = self.headers.get(TOKEN_HEADER) or ""
            if not hmac.compare_digest(provided.encode("utf-8"),
                                       facade.token.encode("utf-8")):
                # The body was never read, so this keep-alive connection's
                # stream position is unknown -- close it rather than risk
                # desyncing whatever pipelined bytes are still in flight.
                self.close_connection = True
                return self._send_json(
                    401, {"error": "Missing or wrong %s header." % TOKEN_HEADER})

        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self.close_connection = True
            return self._send_json(
                400, {"error": "The request needs a plain Content-Length."})
        length = max(0, length)

        if length > BODY_MAX:
            # Refused, but DRAINED FIRST up to a sane bound: answering 413
            # with megabytes still inbound makes Windows reset the socket
            # before the client has read the response (a race that only
            # shows under load — the test suite caught it twice). Draining
            # a bounded remainder costs microseconds on loopback; beyond
            # the drain cap the connection is simply abandoned.
            self.close_connection = True
            remaining = min(length - BODY_MAX, _DRAIN_CAP)
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except OSError:
                pass
            return self._send_json(
                413, {"error": "The request body is limited to %d bytes."
                               % BODY_MAX})

        raw = self.rfile.read(length)
        try:
            # UnicodeDecodeError subclasses ValueError, so bad UTF-8 and bad
            # JSON land in the same honest bucket.
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._send_json(400, {"error": "The body is not valid JSON."})
        if not isinstance(data, dict):
            return self._send_json(
                400, {"error": "The body must be a JSON object."})

        try:
            payload = sanitize_payload(data)
        except _PayloadError as exc:
            return self._send_json(400, {"error": str(exc)})

        try:
            result = facade.on_start(payload)
        except Exception as exc:
            # Full traceback to the log; ONE line to the wire. A traceback in
            # a response both leaks internals and trains clients to ignore us.
            _log.exception("Webhook start callback failed.")
            return self._send_json(500, {
                "error": _failure_sentence(exc)})
        if not isinstance(result, dict):
            _log.error("Webhook start callback returned %r, not a dict.",
                       type(result).__name__)
            return self._send_json(500, {
                "error": "The start callback did not return an object."})
        facade.note_started()

        out = {"ok": True}
        out.update(result)
        return self._send_json(200, out)


def _failure_sentence(exc):
    """One flat, bounded sentence for a failed callback.

    The exception MESSAGE is included because it is the difference between
    "debuggable from the client side" and a support ticket -- but flattened
    (tracebacks never ride along) and capped, since messages can embed
    arbitrary prompt text.
    """
    detail = " ".join(str(exc).split())[:ERROR_EXCERPT_MAX]
    if detail:
        return "Start failed (%s): %s" % (type(exc).__name__, detail)
    return "Start failed (%s)." % type(exc).__name__


class _HTTPD(ThreadingHTTPServer):
    daemon_threads = True      # the app must never hang on exit for a trigger

    # WHY False: Python's default sets SO_REUSEADDR, whose BSD meaning
    # ("reuse TIME_WAIT sockets") is NOT what Windows does with it -- there
    # it permits binding over a port another live socket already owns. A
    # second Alloy webhook hijacking the first's port would silently steal
    # its starts. Ephemeral ports make reuse unnecessary; correctness beats
    # restart convenience.
    allow_reuse_address = False


class WebhookServer:
    """Loopback HTTP trigger wired to one caller-supplied start callback."""

    def __init__(self, on_start, host="127.0.0.1", port=0, token=None):
        if not callable(on_start):
            raise TypeError("on_start must be callable.")
        # An empty-string token would demand a header equal to "", i.e. an
        # auth gate that curl satisfies by accident -- treat it as unset.
        token = token.strip() if isinstance(token, str) else None
        if token is not None and not token:
            token = None
        self.on_start = on_start
        self.host = _require_loopback(host)
        self.requested_port = int(port)
        self.token = token
        # port/url stay None until a socket REALLY exists: reporting a port
        # we merely asked for would read as "ready" while start() returned
        # False on a busy port.
        self.port = None
        self.url = None
        self._httpd = None
        self._thread = None
        self._lifecycle = threading.Lock()
        self._stats_lock = threading.Lock()
        self._starts_ok = 0

    @property
    def started(self):
        """True once ANY /start has succeeded (survives stop/restart)."""
        with self._stats_lock:
            return self._starts_ok > 0

    @property
    def serving(self):
        """True while the socket is OPEN — distinct from `started`, which
        deliberately survives a stop. Callers asking 'is it listening now?'
        want this one."""
        with self._lifecycle:
            return self._httpd is not None

    def note_started(self):
        with self._stats_lock:
            self._starts_ok += 1

    def start(self):
        """Begin serving on a daemon thread. True on success.

        False means the port was taken (OSError): the caller surfaces that
        honestly instead of this module retrying into someone else's socket.
        """
        with self._lifecycle:
            if self._httpd is not None:
                return True          # already serving; idempotent
            try:
                httpd = _HTTPD((self.host, self.requested_port), _Handler)
            except OSError:
                _log.warning("Webhook port %d on %s is taken.",
                             self.requested_port, self.host)
                return False
            httpd.facade = self      # handlers reach config via self.server
            self._httpd = httpd
            # bind() already happened inside the constructor, so the OS has
            # assigned the ephemeral port by now.
            self.port = httpd.server_address[1]
            self.url = _format_url(self.host, self.port)
            self._thread = threading.Thread(
                target=httpd.serve_forever, kwargs={"poll_interval": 0.25},
                name="alloy-webhook", daemon=True)
            self._thread.start()
            _log.info("Webhook listening on %s.", self.url)
            return True

    def stop(self):
        """Shut down and release the socket. Idempotent, safe pre-start."""
        with self._lifecycle:
            httpd, thread = self._httpd, self._thread
            self._httpd = None
            self._thread = None
            if httpd is None:
                return
            httpd.shutdown()         # serve_forever's loop exits cleanly
            if thread is not None:
                thread.join(timeout=5.0)
            httpd.server_close()     # releases the listening socket NOW
            # port/url are KEPT after stop on purpose: they still answer
            # "what was this bound to", and a stopped server answers any
            # connection attempt with a refusal either way.
            _log.info("Webhook stopped (%s).", self.url)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Never started? stop() is safe anyway, so `with` needs no guard.
        self.stop()
        return False
