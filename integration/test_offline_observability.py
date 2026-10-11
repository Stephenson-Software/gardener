"""Offline tier: what happens around a run, through the real CLI.

- Discord alerts and the usage-reporting `startup` event, each sent to a
  loopback stub server that records exactly what arrived.
- `gardener status`.
- The dashboard's status API.
- The hub, end to end: a real `gardener hub serve`, a device pushing to it,
  `status --all-devices`, and the local-first guarantee. A run made while
  the hub is down is recorded locally first and delivered by `hub sync`
  later.
- `gardener doctor --offline`.

Every service stays on 127.0.0.1; nothing leaves the machine.
"""
import base64
import hashlib
import json
import time
import unittest
import urllib.error
import urllib.request

from integration.support import IntegrationCase, Sandbox, StubServer, free_port, wait_http


def _non_loopback_address():
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1: no packet is sent
            address = sock.getsockname()[0]
    except OSError:
        return None
    return None if address.startswith("127.") else address


def stub(test) -> StubServer:
    server = StubServer()
    test.addCleanup(server.close)
    return server


class Alerts(IntegrationCase):
    def test_a_report_run_sends_one_info_alert(self):
        hook = stub(self)
        box = Sandbox(self)
        box.stub_harness("ok-json")
        box.settings["GARDENER_DISCORD_WEBHOOK_URL"] = hook.url + "/webhook"
        box.settings["GARDENER_DEVICE_NAME"] = "it-device"
        r = box.align()
        self.assertEqual(r.code, 0, r.describe())
        self.assertEqual(len(hook.requests), 1, [x["body"] for x in hook.requests])
        sent = hook.requests[0]
        self.assertEqual(sent["path"], "/webhook")
        self.assertNotIn("Python-urllib", sent["headers"].get("User-Agent", ""))
        embed = json.loads(sent["body"])["embeds"][0]
        self.assertIn(box.repo, embed["title"] + embed["description"])
        self.assertEqual(embed["footer"]["text"], "it-device")

    def test_a_failed_run_alerts_in_a_different_colour(self):
        hook_ok, hook_err = stub(self), stub(self)
        colours = []
        for hook, behaviour in ((hook_ok, "ok-json"), (hook_err, "error-json")):
            box = Sandbox(self)
            box.stub_harness(behaviour)
            box.settings["GARDENER_DISCORD_WEBHOOK_URL"] = hook.url
            box.align()
            self.assertEqual(len(hook.requests), 1)
            colours.append(json.loads(hook.requests[0]["body"])["embeds"][0]["color"])
        self.assertNotEqual(colours[0], colours[1], "an error alert looks like a success")

    def test_no_webhook_means_no_alert_and_no_failure(self):
        """Unconfigured alerting is a clean no-op (`NullNotifier`), not an
        error and not a warning on every run."""
        box = Sandbox(self)
        box.stub_harness("ok-json")
        r = box.align()
        self.assertEqual(r.code, 0, r.describe())
        self.assertNotIn("notify:", r.stderr)


class UsageReporting(IntegrationCase):
    def run_with(self, **settings):
        trace = stub(self)
        box = Sandbox(self)
        box.stub_harness("ok-json")
        box.settings.update({"GARDENER_USAGE_REPORTING_ENABLED": "true",
                             "GARDENER_USAGE_REPORTING_ENDPOINT": trace.url})
        box.settings.update(settings)
        box.extra_env.update({"DO_NOT_TRACK": "", "TRACE_USAGE_REPORTING": ""})
        r = box.align()
        self.assertEqual(r.code, 0, r.describe())
        return trace

    def test_one_startup_event_is_sent_with_the_version(self):
        from gardener import __version__

        trace = self.run_with()
        self.assertEqual(len(trace.requests), 1, [x["path"] for x in trace.requests])
        body = trace.requests[0]["body"].decode()
        self.assertIn("startup", body)
        self.assertIn(__version__, body)
        self.assertNotIn(Sandbox.__module__, body)

    def test_the_opt_out_sends_nothing(self):
        trace = self.run_with(GARDENER_USAGE_REPORTING_ENABLED="false")
        self.assertEqual(trace.requests, [])


class Status(IntegrationCase):
    def test_status_lists_recorded_runs(self):
        box = Sandbox(self)
        box.stub_harness("ok-json")
        box.align()
        box.stub_harness("error-json")
        box.align()
        out = box.cli("status")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(box.repo, out.stdout)
        self.assertIn("report", out.stdout)
        self.assertIn("error", out.stdout)


class Dashboard(IntegrationCase):
    def test_the_status_api_serves_the_recorded_run(self):
        box = Sandbox(self)
        box.stub_harness("ok-json")
        box.align()
        port = free_port()
        proc = box.start_cli("dashboard", "--port", str(port))
        self.addCleanup(lambda: (proc.terminate(), proc.wait(timeout=30)))
        base = f"http://127.0.0.1:{port}"
        self.assertTrue(wait_http(base + "/api/status"), "the dashboard never came up")
        with urllib.request.urlopen(base + "/api/status", timeout=10) as resp:
            status = resp.read().decode()
        self.assertIn(box.repo, status)
        for page in ("/", "/live"):
            with urllib.request.urlopen(base + page, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
        # Bound to loopback only: not reachable on the machine's own network
        # address (skipped where there is none besides loopback).
        address = _non_loopback_address()
        if address:
            with self.assertRaises(OSError):
                urllib.request.urlopen(f"http://{address}:{port}/api/status", timeout=3)


class Hub(IntegrationCase):
    """A real hub process and a real device, both on loopback."""

    TOKEN = "it-device-token-0123456789abcdef"
    OPERATOR = ("operator", "it-password")

    def start_hub(self, port: int) -> Sandbox:
        hub = Sandbox(self)
        digest = hashlib.sha256(self.TOKEN.encode()).hexdigest()
        hub.settings["GARDENER_HUB_DEVICE_TOKENS"] = f"it-device:{digest}"
        hub.settings["GARDENER_HUB_OPERATOR_BASIC_SHA256"] = hashlib.sha256(
            ":".join(self.OPERATOR).encode()).hexdigest()
        proc = hub.start_cli("hub", "serve", "--port", str(port), "--data-dir", str(hub.root / "hub-data"))
        self.addCleanup(lambda: (proc.terminate(), proc.wait(timeout=30)))
        self.assertTrue(wait_http(f"http://127.0.0.1:{port}/healthz"), "the hub never came up")
        return hub

    def device(self, port: int) -> Sandbox:
        box = Sandbox(self)
        box.stub_harness("ok-json")
        box.settings["GARDENER_HUB_URL"] = f"http://127.0.0.1:{port}"
        box.settings["GARDENER_HUB_TOKEN"] = self.TOKEN
        return box

    def hub_runs(self, port: int) -> list:
        auth = base64.b64encode(":".join(self.OPERATOR).encode()).decode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/runs?limit=50",
                                     headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.load(resp)
        return body["runs"] if isinstance(body, dict) else body

    def test_a_run_reaches_the_hub_under_the_tokens_device(self):
        port = free_port()
        self.start_hub(port)
        box = self.device(port)
        r = box.align()
        self.assertEqual(r.code, 0, r.describe())
        runs = self.hub_runs(port)
        self.assertEqual([(x["repo"], x["device"]) for x in runs], [(box.repo, "it-device")])
        everywhere = box.cli("status", "--all-devices")
        self.assertEqual(everywhere.returncode, 0, everywhere.stderr)
        self.assertIn(box.repo, everywhere.stdout)

    def test_the_hub_refuses_an_unknown_token(self):
        port = free_port()
        self.start_hub(port)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/runs", data=b'{"runs": []}', method="POST",
                                     headers={"Authorization": "Bearer not-a-token",
                                              "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=10)
        self.assertIn(err.exception.code, (401, 403))

    def test_a_run_made_while_the_hub_is_down_is_kept_and_synced_later(self):
        """Local first: the push can fail without consequence, and the run
        stays queued until the hub acknowledges it."""
        port = free_port()
        box = self.device(port)  # nothing listening yet
        r = box.align()
        self.assertEqual(r.code, 0, r.describe())
        self.assertEqual(len(box.runs()), 1, "the run was not recorded locally")
        queued = box.cli("hub", "status")
        self.assertIn("1", queued.stdout + queued.stderr)

        self.start_hub(port)
        sync = box.cli("hub", "sync")
        self.assertEqual(sync.returncode, 0, sync.stdout + sync.stderr)
        self.assertEqual([x["repo"] for x in self.hub_runs(port)], [box.repo])
        again = box.cli("hub", "sync")
        self.assertEqual(again.returncode, 0)
        self.assertEqual(len(self.hub_runs(port)), 1, "a second sync duplicated the run")


class Doctor(IntegrationCase):
    def test_offline_doctor_is_clean_on_a_fresh_install(self):
        box = Sandbox(self, local_github=True)
        box.stub_harness("ok-json")
        box.align()
        out = box.cli("doctor", "--offline")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("no problems found", out.stdout + out.stderr)

    def test_doctor_flags_a_dirty_cache_clone(self):
        box = Sandbox(self, local_github=True)
        box.stub_harness("ok-json")
        box.align()
        (box.clone_path() / "left-behind.txt").write_text("from an interrupted run\n")
        out = box.cli("doctor", "--offline")
        text = out.stdout + out.stderr
        self.assertIn("untracked", text)
        self.assertIn(box.repo, text)
        self.assertTrue((box.clone_path() / "left-behind.txt").exists(), "doctor repaired something")


if __name__ == "__main__":
    unittest.main()
