"""Drives the client against a real HTTP server on a loopback port -- the
standard library's own, so the tests have no more dependencies than the
client does."""
import json
import logging
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gardener.trace_client import TraceClient


class _Capture:
    def __init__(self):
        self.requests = []
        self.reply_status = 201
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.release.set()  # by default answer at once


def _server(capture):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            capture.release.wait(10)
            capture.requests.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": body.decode("utf-8"),
            })
            self.send_response(capture.reply_status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            capture.arrived.set()

        def log_message(self, *args):  # keep test output quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class TraceClientTest(unittest.TestCase):
    def setUp(self):
        self.capture = _Capture()
        self.server = _server(self.capture)
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.log = []
        handler = logging.Handler()
        handler.emit = lambda record: self.log.append(record)
        self.handler = handler
        logging.getLogger("trace").addHandler(handler)
        logging.getLogger("trace").setLevel(logging.DEBUG)

    def tearDown(self):
        self.server.shutdown()
        logging.getLogger("trace").removeHandler(self.handler)

    def test_report_posts_the_event_to_the_metrics_endpoint_with_the_key(self):
        client = TraceClient(self.base_url + "/", "MyGame", key="k-123")
        client.report("startup")
        self.assertTrue(self.capture.arrived.wait(5), "the report should reach the server")
        request = self.capture.requests[0]
        self.assertEqual("/api/metrics", request["path"], "a trailing slash on the base URL must not double up")
        self.assertEqual("Bearer k-123", request["authorization"])
        self.assertTrue(request["content_type"].startswith("application/json"))
        self.assertEqual({"application": "MyGame", "name": "startup"}, json.loads(request["body"]))
        client.close()

    def test_report_carries_value_and_tags_when_given(self):
        client = TraceClient(self.base_url, "MyGame", key="k")
        client.report("world-load", 2.5, {"seed": "42", "size": 'the "big" one'})
        self.assertTrue(self.capture.arrived.wait(5))
        self.assertEqual({"application": "MyGame", "name": "world-load", "value": 2.5,
                          "tags": {"seed": "42", "size": 'the "big" one'}},
                         json.loads(self.capture.requests[0]["body"]))
        client.close()

    def test_report_returns_before_the_server_answers(self):
        self.capture.release.clear()  # a server that never replies
        client = TraceClient(self.base_url, "MyGame", key="k")
        before = time.monotonic()
        client.report("startup")
        elapsed = time.monotonic() - before
        self.assertLess(elapsed, 1.0, "report() took %.3fs; it must not wait on the network" % elapsed)
        self.capture.release.set()
        client.close()

    def test_report_does_not_raise_when_nothing_is_listening(self):
        probe = _server(_Capture())
        dead_port = probe.server_address[1]
        probe.shutdown()
        probe.server_close()
        client = TraceClient("http://127.0.0.1:%d" % dead_port, "MyGame", key="k")
        client.report("startup")  # must not raise
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any("could not deliver" in r.getMessage() for r in self.log):
            time.sleep(0.05)
        client.close()
        self.assertTrue(any("could not deliver" in r.getMessage() for r in self.log), [r.getMessage() for r in self.log])
        self.assertTrue(all(r.levelno == logging.DEBUG for r in self.log))

    def test_report_does_not_raise_when_the_server_rejects_the_key(self):
        self.capture.reply_status = 401
        client = TraceClient(self.base_url, "MyGame", key="revoked")
        client.report("startup")
        self.assertTrue(self.capture.arrived.wait(5))
        client.close()
        self.assertTrue(any("answered 401" in r.getMessage() for r in self.log), [r.getMessage() for r in self.log])

    def test_disabled_client_sends_nothing(self):
        for client in (TraceClient(self.base_url, "MyGame", key="k", enabled=False),
                       TraceClient(self.base_url, "MyGame"),
                       TraceClient(self.base_url, "MyGame", key="  "),
                       TraceClient.disabled()):
            self.assertFalse(client.enabled)
            client.report("startup")
            client.close()
        self.assertFalse(self.capture.arrived.wait(0.3), "nothing should have been sent")
        self.assertEqual([], self.capture.requests)

    def test_report_ignores_a_blank_name(self):
        client = TraceClient(self.base_url, "MyGame", key="k")
        client.report("")
        client.report("   ")
        client.close()
        self.assertFalse(self.capture.arrived.wait(0.3))

    def test_constructor_rejects_a_missing_base_url_or_application(self):
        for base_url, application in ((None, "MyGame"), (" ", "MyGame"), ("http://x", None), ("http://x", "")):
            with self.assertRaises(ValueError):
                TraceClient(base_url, application)

    def test_json_drops_nan_and_none_tags(self):
        from gardener.trace_client import _json
        body = json.loads(_json("App", "n", float("nan"), {"ok": "line\nbreak", "none": None, None: "x"}))
        self.assertEqual({"application": "App", "name": "n", "tags": {"ok": "line\nbreak"}}, body)

    def test_queue_is_bounded_and_drops_rather_than_grows(self):
        self.capture.release.clear()  # hold the sender on the first report
        client = TraceClient(self.base_url, "MyGame", key="k")
        flood = TraceClient.QUEUE_CAPACITY * 3
        for _ in range(flood):
            client.report("flood")
        dropped = sum(1 for r in self.log if "queue full" in r.getMessage())
        self.assertGreaterEqual(dropped, flood - TraceClient.QUEUE_CAPACITY - 1,
                                "an unbounded queue would have accepted all %d" % flood)
        self.capture.release.set()
        client.close()

    def test_close_sends_what_was_just_queued_before_stopping(self):
        # A CLI reports once and exits at once. Without draining, the event
        # races the sender thread and is lost a good fraction of the time;
        # 30 back-to-back report()+close() pairs make that fraction visible.
        for i in range(30):
            client = TraceClient(self.base_url, "MyCli", key="k")
            client.report("startup", tags={"run": str(i)})
            client.close()
        self.assertEqual(30, len(self.capture.requests), "every report()+close() pair must deliver")

    def test_close_still_returns_within_the_timeout_when_the_server_hangs(self):
        self.capture.release.clear()  # never answers
        client = TraceClient(self.base_url, "MyCli", key="k")
        client.report("startup")
        before = time.monotonic()
        client.close(timeout=1.0)
        self.assertLess(time.monotonic() - before, 2.0, "draining must be bounded by the timeout")
        self.capture.release.set()

    def test_close_is_prompt_and_idempotent(self):
        client = TraceClient(self.base_url, "MyGame", key="k")
        client.report("startup")
        before = time.monotonic()
        client.close()
        client.close()
        self.assertLess(time.monotonic() - before, TraceClient.TIMEOUT_SECONDS + 1)
        self.assertFalse(client.enabled)


if __name__ == "__main__":
    unittest.main()
