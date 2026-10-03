"""Drives the client against a real HTTP server on a loopback port -- the
standard library's own, so the tests have no more dependencies than the
client does."""
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from gardener.trace_client import MAX_TAG_LENGTH, MAX_TAGS, TraceClient, environment_opts_out

_ENV_VARS = ("TRACE_USAGE_REPORTING", "DO_NOT_TRACK")


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
                "user_agent": self.headers.get("User-Agent"),
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
        # The machine running the tests may itself have opted out; every
        # test starts from a clean environment and sets what it needs.
        scrubbed = {k: v for k, v in os.environ.items() if k not in _ENV_VARS}
        patcher = mock.patch.dict(os.environ, scrubbed, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
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
        client = TraceClient(self.base_url + "/", "MyGame", "1.2.3", key="k-123")
        client.report("startup")
        self.assertTrue(self.capture.arrived.wait(5), "the report should reach the server")
        request = self.capture.requests[0]
        self.assertEqual("/api/metrics", request["path"], "a trailing slash on the base URL must not double up")
        self.assertEqual("Bearer k-123", request["authorization"])
        self.assertTrue(request["content_type"].startswith("application/json"))
        self.assertEqual({"application": "MyGame", "name": "startup", "tags": {"version": "1.2.3"}},
                         json.loads(request["body"]))
        client.close()

    def test_report_carries_value_and_tags_when_given(self):
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
        client.report("world-load", 2.5, {"seed": "42", "size": 'the "big" one'})
        self.assertTrue(self.capture.arrived.wait(5))
        self.assertEqual({"application": "MyGame", "name": "world-load", "value": 2.5,
                          "tags": {"seed": "42", "size": 'the "big" one', "version": "1.2.3"}},
                         json.loads(self.capture.requests[0]["body"]))
        client.close()

    def test_report_returns_before_the_server_answers(self):
        self.capture.release.clear()  # a server that never replies
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
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
        client = TraceClient("http://127.0.0.1:%d" % dead_port, "MyGame", "1.2.3", key="k")
        client.report("startup")  # must not raise
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any("could not deliver" in r.getMessage() for r in self.log):
            time.sleep(0.05)
        client.close()
        self.assertTrue(any("could not deliver" in r.getMessage() for r in self.log), [r.getMessage() for r in self.log])
        self.assertTrue(all(r.levelno == logging.DEBUG for r in self.log))

    def test_report_does_not_raise_when_the_server_rejects_the_key(self):
        self.capture.reply_status = 401
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="revoked")
        client.report("startup")
        self.assertTrue(self.capture.arrived.wait(5))
        client.close()
        self.assertTrue(any("answered 401" in r.getMessage() for r in self.log), [r.getMessage() for r in self.log])

    def test_disabled_client_sends_nothing(self):
        for client in (TraceClient(self.base_url, "MyGame", "1.2.3", key="k", enabled=False),
                       TraceClient(self.base_url, "MyGame", "1.2.3"),
                       TraceClient(self.base_url, "MyGame", "1.2.3", key="  "),
                       TraceClient.disabled()):
            self.assertFalse(client.enabled)
            client.report("startup")
            client.close()
        self.assertFalse(self.capture.arrived.wait(0.3), "nothing should have been sent")
        self.assertEqual([], self.capture.requests)

    def test_disabled_reason_is_none_when_the_client_reports(self):
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
        self.assertTrue(client.enabled)
        self.assertIsNone(client.disabled_reason)
        client.close()
        self.assertFalse(client.enabled, "close() stops reporting")
        self.assertIsNone(client.disabled_reason, "but the reason describes how it was built")

    def test_disabled_reason_names_the_config_flag_or_the_missing_key(self):
        self.assertEqual("config", TraceClient(self.base_url, "MyGame", "1.2.3", key="k", enabled=False).disabled_reason)
        self.assertEqual("config", TraceClient.disabled().disabled_reason)
        self.assertEqual("no key", TraceClient(self.base_url, "MyGame", "1.2.3").disabled_reason)
        self.assertEqual("no key", TraceClient(self.base_url, "MyGame", "1.2.3", key="  ").disabled_reason)
        self.assertEqual("config", TraceClient(self.base_url, "MyGame", "1.2.3", enabled=False).disabled_reason,
                         "the config flag is checked before the key")

    def _assert_environment_disables(self, variable, value):
        with mock.patch.dict(os.environ, {variable: value}):
            self.assertTrue(environment_opts_out(), "%s=%r should opt out" % (variable, value))
            client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
            self.assertFalse(client.enabled, "%s=%r should disable the client" % (variable, value))
            self.assertEqual("environment", client.disabled_reason)
            client.report("startup")
            client.close()
        self.assertFalse(self.capture.arrived.wait(0.2), "%s=%r: nothing should have been sent" % (variable, value))
        self.assertEqual([], self.capture.requests)

    def test_TRACE_USAGE_REPORTING_off_disables_reporting_for_every_accepted_value(self):
        for value in ("off", "false", "0", "no", "OFF", "False", "No", " off "):
            self._assert_environment_disables("TRACE_USAGE_REPORTING", value)

    def test_DO_NOT_TRACK_disables_reporting_for_every_accepted_value(self):
        for value in ("1", "true", "yes", "TRUE", "Yes", " 1 "):
            self._assert_environment_disables("DO_NOT_TRACK", value)

    def test_other_environment_values_leave_the_program_setting_in_charge(self):
        for variable, value in (("TRACE_USAGE_REPORTING", "on"), ("TRACE_USAGE_REPORTING", ""),
                                ("TRACE_USAGE_REPORTING", "disabled"), ("TRACE_USAGE_REPORTING", "1"),
                                ("DO_NOT_TRACK", "0"), ("DO_NOT_TRACK", ""), ("DO_NOT_TRACK", "false"),
                                ("DO_NOT_TRACK", "off")):
            with mock.patch.dict(os.environ, {variable: value}):
                self.assertFalse(environment_opts_out(), "%s=%r is not an opt-out" % (variable, value))
                client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
                self.assertTrue(client.enabled, "%s=%r must not disable the client" % (variable, value))
                self.assertIsNone(client.disabled_reason)
                client.close()
        self.assertFalse(environment_opts_out(), "an unset variable is not an opt-out")

    def test_environment_wins_over_the_config_flag_and_over_the_key(self):
        # enabled=True with a key, and the environment still says no.
        with mock.patch.dict(os.environ, {"TRACE_USAGE_REPORTING": "off"}):
            client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k", enabled=True)
            self.assertEqual("environment", client.disabled_reason)
            client.close()
        # enabled=False AND the environment: the environment is the reason.
        with mock.patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            self.assertEqual("environment",
                             TraceClient(self.base_url, "MyGame", "1.2.3", key="k", enabled=False).disabled_reason)
            self.assertEqual("environment", TraceClient(self.base_url, "MyGame", "1.2.3").disabled_reason,
                             "the environment is checked before the key too")
        # Once the variable is gone, the program's own setting is back in charge.
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
        self.assertTrue(client.enabled)
        client.report("startup")
        self.assertTrue(self.capture.arrived.wait(5))
        client.close()

    def test_environment_opts_out_accepts_an_explicit_mapping(self):
        self.assertTrue(environment_opts_out({"TRACE_USAGE_REPORTING": "off"}))
        self.assertTrue(environment_opts_out({"DO_NOT_TRACK": "yes"}))
        self.assertFalse(environment_opts_out({}))
        self.assertFalse(environment_opts_out({"DO_NOT_TRACK": "0", "TRACE_USAGE_REPORTING": "on"}))

    def test_user_agent_names_the_client_version(self):
        from gardener.trace_client import __version__
        self.assertEqual("0.4.0", __version__)
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
        client.report("startup")
        self.assertTrue(self.capture.arrived.wait(5))
        client.close()
        self.assertEqual("trace-client-python/0.4.0 (MyGame)", self.capture.requests[0]["user_agent"])

    def test_report_ignores_a_blank_name(self):
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
        client.report("")
        client.report("   ")
        client.close()
        self.assertFalse(self.capture.arrived.wait(0.3))

    def test_report_does_not_raise_for_a_name_that_is_not_a_string(self):
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
        client.report(123)  # must not raise
        client.close()
        self.assertFalse(self.capture.arrived.wait(0.3), "a report that could not be built is dropped")
        self.assertTrue(any("could not queue 123" in r.getMessage() for r in self.log),
                        [r.getMessage() for r in self.log])
        self.assertTrue(all(r.levelno == logging.DEBUG for r in self.log))

    def test_a_base_url_without_a_scheme_is_logged_not_a_dead_thread(self):
        crashes = []
        with mock.patch.object(threading, "excepthook", crashes.append):
            client = TraceClient("trace.example.org", "MyGame", "1.2.3", key="k")
            client.report("startup")
            client.report("shutdown")
            client.close()
        self.assertEqual([], crashes, "the sender thread must not die with a traceback on stderr")
        failures = [r for r in self.log if "could not deliver" in r.getMessage()]
        self.assertEqual(2, len(failures), "each report is logged and the thread keeps going: %s"
                         % [r.getMessage() for r in self.log])
        self.assertTrue(all(r.levelno == logging.DEBUG for r in self.log))

    def test_the_sender_thread_survives_an_unexpected_error_from_send(self):
        crashes = []
        real_send = TraceClient._send
        calls = []

        def send_that_fails_once(client, body):
            calls.append(body)
            if len(calls) == 1:
                raise RuntimeError("boom")
            real_send(client, body)

        with mock.patch.object(threading, "excepthook", crashes.append), \
                mock.patch.object(TraceClient, "_send", send_that_fails_once):
            client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
            client.report("first")
            client.report("second")
            self.assertTrue(self.capture.arrived.wait(5), "a later report is still delivered")
            client.close()
        self.assertEqual([], crashes, "the sender thread must not die with a traceback on stderr")
        self.assertEqual(["second"], [json.loads(r["body"])["name"] for r in self.capture.requests])
        self.assertTrue(any("sender failed" in r.getMessage() and "boom" in r.getMessage() for r in self.log),
                        [r.getMessage() for r in self.log])
        self.assertTrue(all(r.levelno == logging.DEBUG for r in self.log))

    def test_constructor_rejects_a_missing_base_url_or_application(self):
        for base_url, application in ((None, "MyGame"), (" ", "MyGame"), ("http://x", None), ("http://x", "")):
            with self.assertRaises(ValueError):
                TraceClient(base_url, application, "1.2.3")

    def test_constructor_rejects_a_missing_blank_or_overlong_version(self):
        for version in (None, "", "   ", "9" * (MAX_TAG_LENGTH + 1)):
            with self.assertRaises(ValueError, msg=repr(version)):
                TraceClient(self.base_url, "MyGame", version, key="k")
        with self.assertRaises(TypeError, msg="the version is required, not optional"):
            TraceClient(self.base_url, "MyGame", key="k")
        # Exactly the limit is fine, and so is an overlong-looking one that trims to it.
        TraceClient(self.base_url, "MyGame", "9" * MAX_TAG_LENGTH, enabled=False)
        TraceClient(self.base_url, "MyGame", "  " + "9" * MAX_TAG_LENGTH + "  ", enabled=False)

    def test_report_tags_a_command_with_the_program_version_trimmed(self):
        client = TraceClient(self.base_url, "MyGame", " 2.0.0-SNAPSHOT ", key="k")
        client.report("command", tags={"name": "home"})
        self.assertTrue(self.capture.arrived.wait(5))
        client.close()
        self.assertEqual('{"application":"MyGame","name":"command",'
                         '"tags":{"name":"home","version":"2.0.0-SNAPSHOT"}}',
                         self.capture.requests[0]["body"])

    def test_report_an_events_own_version_tag_wins_over_the_program_version(self):
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
        tags = {"version": "9.9.9"}
        client.report("startup", tags=tags)
        self.assertTrue(self.capture.arrived.wait(5))
        client.close()
        self.assertEqual('{"application":"MyGame","name":"startup","tags":{"version":"9.9.9"}}',
                         self.capture.requests[0]["body"])
        self.assertEqual({"version": "9.9.9"}, tags, "the caller's dict is not modified")

    def test_report_never_modifies_the_callers_tags(self):
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
        tags = {"name": "home"}
        client.report("command", tags=tags)
        self.assertTrue(self.capture.arrived.wait(5))
        client.close()
        self.assertEqual({"name": "home"}, tags)
        self.assertEqual({"name": "home", "version": "1.2.3"},
                         json.loads(self.capture.requests[0]["body"])["tags"])

    def test_with_version_never_modifies_the_callers_mapping(self):
        from gardener.trace_client import _with_version
        tags = {"name": "home"}
        merged = _with_version(tags, "1.2.3")
        self.assertEqual({"name": "home"}, tags)
        self.assertEqual({"name": "home", "version": "1.2.3"}, merged)
        self.assertEqual({"version": "1.2.3"}, _with_version(None, "1.2.3"))

    def test_json_drops_nan_and_none_tags(self):
        from gardener.trace_client import _json
        body = json.loads(_json("App", "n", float("nan"), {"ok": "line\nbreak", "none": None, None: "x"}))
        self.assertEqual({"application": "App", "name": "n", "tags": {"ok": "line\nbreak"}}, body)

    def test_queue_is_bounded_and_drops_rather_than_grows(self):
        self.capture.release.clear()  # hold the sender on the first report
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
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
            client = TraceClient(self.base_url, "MyCli", "1.2.3", key="k")
            client.report("startup", tags={"run": str(i)})
            client.close()
        self.assertEqual(30, len(self.capture.requests), "every report()+close() pair must deliver")

    def test_close_still_returns_within_the_timeout_when_the_server_hangs(self):
        self.capture.release.clear()  # never answers
        client = TraceClient(self.base_url, "MyCli", "1.2.3", key="k")
        client.report("startup")
        before = time.monotonic()
        client.close(timeout=1.0)
        self.assertLess(time.monotonic() - before, 2.0, "draining must be bounded by the timeout")
        self.capture.release.set()

    def test_close_is_prompt_and_idempotent(self):
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
        client.report("startup")
        before = time.monotonic()
        client.close()
        client.close()
        self.assertLess(time.monotonic() - before, TraceClient.TIMEOUT_SECONDS + 1)
        self.assertFalse(client.enabled)

    # -- the per-installation ID ------------------------------------------

    def _tmpdir(self):
        directory = tempfile.mkdtemp(prefix="trace-install-")
        self.addCleanup(shutil.rmtree, directory, True)
        return directory

    def _sent_tags(self, client, name="startup", tags=None):
        client.report(name, tags=tags)
        self.assertTrue(self.capture.arrived.wait(5), "the report should reach the server")
        client.close()
        return json.loads(self.capture.requests[-1]["body"])["tags"]

    def test_no_install_id_and_no_file_sends_no_install_tag(self):
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k")
        self.assertIsNone(client.install_id)
        self.assertEqual({"version": "1.2.3"}, self._sent_tags(client))

    def test_install_id_file_is_created_once_and_reused(self):
        path = os.path.join(self._tmpdir(), "nested", "deeper", "install-id")
        first = TraceClient(self.base_url, "MyGame", "1.2.3", key="k", install_id_file=path)
        self.assertEqual(str(uuid.UUID(first.install_id)), first.install_id, "a random UUID")
        with open(path, encoding="utf-8") as stored:
            self.assertEqual(first.install_id + "\n", stored.read(), "parent directories are created")
        first.close()
        second = TraceClient(self.base_url, "MyGame", "1.2.3", key="k", install_id_file=path)
        self.assertEqual(first.install_id, second.install_id, "the next run reuses it")
        self.assertEqual({"version": "1.2.3", "install": first.install_id}, self._sent_tags(second))

    def test_install_id_from_file_reads_the_first_valid_line(self):
        path = os.path.join(self._tmpdir(), "install-id")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n# not an id\n  my-own.id_1  \nsecond-id\n")
        self.assertEqual("my-own.id_1", TraceClient.install_id_from_file(path))
        with open(path, encoding="utf-8") as f:
            self.assertIn("# not an id", f.read(), "a file with an ID is never rewritten")

    def test_install_id_from_file_replaces_a_file_with_no_valid_line(self):
        path = os.path.join(self._tmpdir(), "install-id")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not an id\n" + "x" * (MAX_TAG_LENGTH + 1) + "\n")
        made = TraceClient.install_id_from_file(path)
        self.assertEqual(made, TraceClient.install_id_from_file(path))

    def test_unwritable_install_id_file_yields_an_in_memory_id_and_never_raises(self):
        # A path under a regular file cannot be created, even as root.
        blocker = os.path.join(self._tmpdir(), "a-file")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        path = os.path.join(blocker, "install-id")
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k", install_id_file=path)
        self.assertEqual(str(uuid.UUID(client.install_id)), client.install_id)
        self.assertFalse(os.path.exists(path))
        self.assertNotEqual(client.install_id, TraceClient.install_id_from_file(path),
                            "in memory: a new one each process")
        self.assertEqual({"version": "1.2.3", "install": client.install_id}, self._sent_tags(client))

    def test_unreadable_install_id_file_is_left_alone(self):
        directory = self._tmpdir()  # a directory cannot be read as a file
        made = TraceClient.install_id_from_file(directory)
        self.assertEqual(str(uuid.UUID(made)), made)
        self.assertTrue(os.path.isdir(directory))
        self.assertEqual([], os.listdir(directory))

    def test_install_id_from_file_never_raises_for_a_bad_path(self):
        for path in (None, "", "   ", 42):
            made = TraceClient.install_id_from_file(path)
            self.assertEqual(str(uuid.UUID(made)), made, repr(path))

    def test_a_disabled_client_never_makes_up_or_writes_an_install_id(self):
        directory = self._tmpdir()
        path = os.path.join(directory, "install-id")
        os.environ["DO_NOT_TRACK"] = "1"
        by_environment = TraceClient(self.base_url, "MyGame", "1.2.3", key="k", install_id_file=path,
                                     install_id="explicit")
        del os.environ["DO_NOT_TRACK"]
        by_config = TraceClient(self.base_url, "MyGame", "1.2.3", key="k", enabled=False, install_id_file=path)
        by_no_key = TraceClient(self.base_url, "MyGame", "1.2.3", install_id_file=path)
        for client in (by_environment, by_config, by_no_key):
            self.assertIsNone(client.install_id, client.disabled_reason)
        self.assertEqual([], os.listdir(directory), "nothing is written")

    def test_explicit_install_id_is_trimmed_sent_and_wins_over_the_file(self):
        path = os.path.join(self._tmpdir(), "install-id")
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k", install_id="  abc-123  ",
                             install_id_file=path)
        self.assertEqual("abc-123", client.install_id)
        self.assertFalse(os.path.exists(path), "the file is not consulted when an ID is given")
        self.assertEqual({"name": "home", "version": "1.2.3", "install": "abc-123"},
                         self._sent_tags(client, "command", {"name": "home"}))

    def test_blank_install_id_is_none_and_an_overlong_one_is_rejected(self):
        for blank in (None, "", "   "):
            self.assertIsNone(TraceClient(self.base_url, "MyGame", "1.2.3", key="k", install_id=blank).install_id)
        with self.assertRaises(ValueError):
            TraceClient(self.base_url, "MyGame", "1.2.3", key="k", install_id="x" * (MAX_TAG_LENGTH + 1))
        with self.assertRaises(ValueError, msg="rejected even on a disabled client, like the version"):
            TraceClient(self.base_url, "MyGame", "1.2.3", enabled=False, install_id="x" * (MAX_TAG_LENGTH + 1))
        exact = TraceClient(self.base_url, "MyGame", "1.2.3", key="k", install_id="x" * MAX_TAG_LENGTH)
        self.assertEqual("x" * MAX_TAG_LENGTH, exact.install_id)
        exact.close()

    def test_an_events_own_install_tag_wins(self):
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k", install_id="mine")
        tags = {"install": "theirs"}
        self.assertEqual({"install": "theirs", "version": "1.2.3"}, self._sent_tags(client, tags=tags))
        self.assertEqual({"install": "theirs"}, tags, "the caller's tags are never modified")

    def test_install_tag_never_pushes_an_event_past_the_tag_cap(self):
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k", install_id="mine")
        full = {"t%d" % i: "v" for i in range(MAX_TAGS - 1)}  # plus version = MAX_TAGS
        sent = self._sent_tags(client, tags=full)
        self.assertEqual(MAX_TAGS, len(sent))
        self.assertNotIn("install", sent)
        self.capture.arrived.clear()
        client = TraceClient(self.base_url, "MyGame", "1.2.3", key="k", install_id="mine")
        room = {"t%d" % i: "v" for i in range(MAX_TAGS - 2)}
        sent = self._sent_tags(client, tags=room)
        self.assertEqual(MAX_TAGS, len(sent))
        self.assertEqual("mine", sent["install"])


if __name__ == "__main__":
    unittest.main()
