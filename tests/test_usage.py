"""The startup usage event: what it carries, where it goes, and how it is
switched off.

Every test that sends anything sends it to a loopback server started here.
Nothing reaches the real trace service: the endpoint is always overridden
to that server (or reporting is switched off), and the one command driven
through `cli.main` is `status`, which only reads a sqlite db in a tmp state
dir — it never invokes `claude`, `gh`, or `git`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import gardener
from gardener import cli, notify, usage
from gardener.trace_client import TraceClient


def _stub(release: threading.Event | None = None):
    """A loopback trace server that records every POST and answers 201 —
    after `release` is set, if one is given, so a test can stand in for a
    server that never answers."""
    requests = []
    arrived = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if release is not None:
                release.wait(30)
            requests.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(body.decode("utf-8")),
            })
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()
            arrived.set()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, requests, arrived


class _NoConfigFile:
    """Point every setting lookup at a tmp state dir with no `notify.env`,
    so a test never reads the operator's real one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.config_path = self.state_dir / "notify.env"
        patcher = patch.dict(os.environ, {"GARDENER_STATE_DIR": self._tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (usage.ENV_ENABLED, usage.ENV_ENDPOINT, usage.ENV_KEY):
            os.environ.pop(name, None)


class TestSettings(_NoConfigFile, unittest.TestCase):
    def test_defaults_are_on_and_point_at_the_production_service(self):
        self.assertTrue(usage.enabled({}))
        self.assertEqual("https://trace.danielstephenson.dev", usage.endpoint({}))
        self.assertEqual(usage.DEFAULT_KEY, usage.key({}))
        self.assertTrue(usage.DEFAULT_KEY.strip(), "the bundled key must not be blank")

    def test_only_an_explicit_no_turns_it_off(self):
        for value in ("0", "false", "FALSE", "no", "off", " Off "):
            self.assertFalse(usage.enabled({usage.ENV_ENABLED: value}), value)
        for value in ("", "1", "true", "yes", "anything"):
            self.assertTrue(usage.enabled({usage.ENV_ENABLED: value}), repr(value))

    def test_endpoint_and_key_come_from_the_environment_when_set(self):
        env = {usage.ENV_ENDPOINT: " http://127.0.0.1:1/ ", usage.ENV_KEY: " k "}
        self.assertEqual("http://127.0.0.1:1/", usage.endpoint(env))
        self.assertEqual("k", usage.key(env))
        # Blank means unset, not "no key".
        self.assertEqual(usage.DEFAULT_KEY, usage.key({usage.ENV_KEY: "  "}))

    def test_settings_fall_back_to_notify_env_under_the_state_dir(self):
        """Same file and same precedence `notify.load_device_name` uses:
        env var first, then the same name in `notify.env`."""
        self.config_path.write_text(
            f"DISCORD_WEBHOOK_URL=https://example.invalid/hook\n"
            f"{usage.ENV_ENABLED}=false\n"
            f"{usage.ENV_ENDPOINT}='http://127.0.0.1:1'\n"
            f'{usage.ENV_KEY}="file-key"\n'
        )
        self.assertEqual(self.config_path, notify.default_webhook_config_path())
        self.assertFalse(usage.enabled({}))
        self.assertEqual("http://127.0.0.1:1", usage.endpoint({}))
        self.assertEqual("file-key", usage.key({}))
        # The env var wins over the file at every level.
        env = {usage.ENV_ENABLED: "true", usage.ENV_ENDPOINT: "http://127.0.0.1:2", usage.ENV_KEY: "env-key"}
        self.assertTrue(usage.enabled(env))
        self.assertEqual("http://127.0.0.1:2", usage.endpoint(env))
        self.assertEqual("env-key", usage.key(env))
        # And a blank env var falls through to the file rather than clearing it.
        self.assertEqual("file-key", usage.key({usage.ENV_KEY: " "}))

    def test_a_missing_or_unreadable_config_file_means_defaults(self):
        self.assertFalse(self.config_path.exists())
        self.assertTrue(usage.enabled({}))
        self.assertEqual(usage.DEFAULT_KEY, usage.key({}))
        self.config_path.mkdir()  # a directory: is_file() is False, so "not configured"
        self.assertTrue(usage.enabled({}))
        with patch("gardener.usage.notify._parse_env_style_file", side_effect=OSError("nope")):
            self.config_path.rmdir()
            self.config_path.write_text(f"{usage.ENV_ENABLED}=false\n")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertTrue(usage.enabled({}))
            self.assertIn("could not read", stderr.getvalue())

    def test_disabled_builds_a_client_that_sends_nothing(self):
        client = usage.build_client({usage.ENV_ENABLED: "false"})
        self.assertFalse(client.enabled)
        client = usage.build_client({usage.ENV_KEY: "   ", usage.ENV_ENABLED: "0"})
        self.assertFalse(client.enabled)

    def test_a_broken_setting_yields_the_no_op_client_rather_than_raising(self):
        with patch("gardener.usage.endpoint", side_effect=RuntimeError("boom")):
            client = usage.build_client({})
        self.assertFalse(client.enabled)

    def test_startup_tags_are_version_and_service_only(self):
        self.assertEqual({"version": gardener.__version__, "service": "true"}, usage.startup_tags())


class TestStartAndStop(_NoConfigFile, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server, self.requests, self.arrived = _stub()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.env = {usage.ENV_ENDPOINT: self.base_url, usage.ENV_KEY: "test-key"}

    def test_start_sends_one_startup_event_tagged_as_a_service(self):
        client = usage.start(self.env)
        try:
            self.assertTrue(self.arrived.wait(5))
        finally:
            usage.stop(client)
        self.assertEqual(1, len(self.requests))
        request = self.requests[0]
        self.assertEqual("/api/metrics", request["path"])
        self.assertEqual("Bearer test-key", request["authorization"])
        self.assertEqual(
            {"application": "gardener", "name": "startup",
             "tags": {"version": gardener.__version__, "service": "true"}},
            request["body"],
        )

    def test_the_body_carries_nothing_about_the_machine_or_the_run(self):
        client = usage.start(self.env)
        self.arrived.wait(5)
        usage.stop(client)
        body = json.dumps(self.requests[0]["body"])
        for forbidden in (os.getcwd(), str(Path.home()), self.state_dir.name):
            self.assertNotIn(forbidden, body)
        self.assertEqual({"application", "name", "tags"}, set(self.requests[0]["body"]))

    def test_stop_right_after_start_does_not_lose_the_event(self):
        """A short run: start, then stop immediately, thirty times over.
        The client's `close` (0.1.1+) sends what is still queued before
        stopping — with 0.1.0 a sibling CLI measured 7 of 30 lost — so
        every one of these must arrive."""
        for _ in range(30):
            client = usage.start(self.env)
            usage.stop(client)
        deadline = time.monotonic() + 10
        while len(self.requests) < 30 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(30, len(self.requests))

    def test_stop_returns_within_the_timeout_when_the_server_never_answers(self):
        """An unreachable or hung trace server delays exit by at most the
        timeout `stop` is given, never indefinitely."""
        release = threading.Event()
        server, requests, _ = _stub(release)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        env = {usage.ENV_ENDPOINT: "http://127.0.0.1:%d" % server.server_address[1],
               usage.ENV_KEY: "test-key"}
        client = usage.start(env)
        started = time.monotonic()
        usage.stop(client, timeout=1.0)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual([], requests)
        # And a port nothing listens on is a dropped event, not a hang.
        client = usage.start({usage.ENV_ENDPOINT: "http://127.0.0.1:9", usage.ENV_KEY: "test-key"})
        started = time.monotonic()
        usage.stop(client, timeout=1.0)
        self.assertLess(time.monotonic() - started, 3.0)

    def test_stop_tolerates_a_disabled_client_and_a_foreign_object(self):
        usage.stop(TraceClient.disabled())
        usage.stop(object())  # no `_queue`, no `close` — still never raises

    def test_disabled_sends_nothing(self):
        client = usage.start({**self.env, usage.ENV_ENABLED: "false"})
        usage.stop(client)
        self.assertFalse(self.arrived.wait(0.5))
        self.assertEqual([], self.requests)


class TestMainWiring(_NoConfigFile, unittest.TestCase):
    """`cli.main` reports once per invocation and stops the client on every
    exit path. `status` is the command driven here: it reads the run db in
    the tmp state dir and nothing else."""

    def setUp(self):
        super().setUp()
        self.server, self.requests, self.arrived = _stub()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]
        patcher = patch.dict(os.environ, {usage.ENV_ENDPOINT: self.base_url, usage.ENV_KEY: "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_status_reports_one_startup_and_prints_nothing_extra(self):
        code, out, err = self._main(["status"])
        self.assertEqual(0, code)
        self.assertTrue(self.arrived.wait(5))
        self.assertEqual(1, len(self.requests))
        self.assertEqual("startup", self.requests[0]["body"]["name"])
        self.assertEqual("gardener", self.requests[0]["body"]["application"])
        self.assertNotIn("trace", out.lower())
        self.assertNotIn("usage", err.lower())

    def test_ps_quiet_output_stays_machine_readable(self):
        code, out, err = self._main(["ps", "-q"])
        self.assertEqual(0, code)
        self.assertEqual("", out)  # no sessions, and nothing from reporting either
        self.assertTrue(self.arrived.wait(5))

    def test_opted_out_sends_nothing(self):
        with patch.dict(os.environ, {usage.ENV_ENABLED: "false"}):
            code, _, _ = self._main(["status"])
        self.assertEqual(0, code)
        self.assertFalse(self.arrived.wait(0.5))
        self.assertEqual([], self.requests)

    def test_opted_out_via_notify_env_sends_nothing(self):
        self.config_path.write_text(f"{usage.ENV_ENABLED}=false\n")
        code, _, _ = self._main(["status"])
        self.assertEqual(0, code)
        self.assertFalse(self.arrived.wait(0.5))
        self.assertEqual([], self.requests)

    def test_client_is_stopped_even_when_the_command_raises(self):
        with patch("gardener.cli.cmd_status", side_effect=RuntimeError("boom")), \
                patch("gardener.cli.usage.stop", wraps=usage.stop) as stop:
            with self.assertRaises(RuntimeError):
                cli.main(["status"])
        stop.assert_called_once()
        self.assertTrue(self.arrived.wait(5))

    def test_help_does_not_count_as_a_run(self):
        with self.assertRaises(SystemExit):
            self._main(["--help"])
        self.assertFalse(self.arrived.wait(0.5))
        self.assertEqual([], self.requests)


if __name__ == "__main__":
    unittest.main()
