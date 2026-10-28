"""Tests for `gardener/garden3d.py`: the 3D garden page at /3d.

The page's JavaScript and the renderer (`garden3d/main.cpp`) have no test
runner here (stdlib-only Python), so they are verified by rendering, as
the 2D garden is (docs/DASHBOARD.md). What is asserted here is the serving
side: exactly three URLs, the right content types, the hub's operator
check, and that the page only calls functions the build exports.
"""
import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from gardener import dashboard, garden3d
from unittest.mock import patch

from test_hub import OPERATOR, PHONE_TOKEN, _basic, _env, _HubFixture

REPO = Path(__file__).resolve().parent.parent


class TestAssets(unittest.TestCase):
    def test_each_url_maps_to_its_file_and_type(self):
        self.assertEqual(garden3d.asset("/3d")[0], "text/html; charset=utf-8")
        self.assertEqual(garden3d.asset("/3d/")[0], "text/html; charset=utf-8")
        self.assertEqual(garden3d.asset("/3d/garden3d.js")[0], "text/javascript; charset=utf-8")
        content_type, body = garden3d.asset("/3d/garden3d.wasm")
        self.assertEqual(content_type, "application/wasm")
        self.assertEqual(body[:4], b"\0asm")

    def test_nothing_outside_the_table_is_served(self):
        for path in ("/3d/garden3d.html", "/3d/../hub.py", "/3d/web/garden3d.js", "/3d/x", "/garden3d.wasm"):
            self.assertIsNone(garden3d.asset(path), path)
            self.assertFalse(garden3d.is_asset_path(path), path)

    def test_a_missing_file_is_none_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = garden3d.WEB_DIR
            garden3d.WEB_DIR = Path(tmp)
            try:
                self.assertIsNone(garden3d.asset("/3d/garden3d.wasm"))
            finally:
                garden3d.WEB_DIR = original

    def test_the_page_calls_only_exported_functions(self):
        # A function the page calls but build.sh doesn't export is undefined
        # in the browser, and nothing but a render would notice.
        page = (garden3d.WEB_DIR / "garden3d.html").read_text(encoding="utf-8")
        build = (REPO / "garden3d" / "build.sh").read_text(encoding="utf-8")
        exported = set(re.search(r"-sEXPORTED_FUNCTIONS=(\S+)", build).group(1).split(","))
        called = set(re.findall(r"mod\.(_\w+)\(", page))
        self.assertTrue(called)
        self.assertEqual(called - exported, set())
        glue = (garden3d.WEB_DIR / "garden3d.js").read_text(encoding="utf-8")
        for name in called:
            self.assertIn(name, glue, f"the committed build predates {name}; rerun garden3d/build.sh")

    def test_the_page_loads_its_assets_from_the_served_urls(self):
        page = (garden3d.WEB_DIR / "garden3d.html").read_text(encoding="utf-8")
        self.assertIn('src="/3d/garden3d.js"', page)
        self.assertIn('"/3d/" + p', page)  # locateFile, for the .wasm

    def test_the_web_files_ship_in_the_package(self):
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('"web/*"', pyproject)


class TestGardeners(unittest.TestCase):
    """The gardeners (one per device) are drawn from fields /api/status
    already carries. What can be asserted without a browser: the local
    payload names its device, the hub's doesn't (its devices are named by
    their tokens), and the page never reads log text (RFC 0010: no log or
    transcript text leaves a device, and the 3D page adds none)."""

    def test_a_local_payload_names_its_device(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"GARDENER_DEVICE_NAME": "Walter"}):
            self.assertEqual(dashboard.build_status(state_dir=Path(tmp))["device"], "Walter")

    def test_a_hub_payload_names_no_device_of_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(dashboard.build_status(state_dir=Path(tmp), hub=True, live_devices=[])["device"])

    def test_the_page_reads_no_log_text(self):
        page = (garden3d.WEB_DIR / "garden3d.html").read_text(encoding="utf-8")
        for field in ("log_tail", "log_tails", "active_log", "summary"):
            self.assertNotRegex(page, rf"\.{field}\b", field)

    def test_the_page_draws_gardeners_from_live_devices(self):
        page = (garden3d.WEB_DIR / "garden3d.html").read_text(encoding="utf-8")
        self.assertIn("data.live_devices", page)
        self.assertIn("mod._g3_set_gardeners(", page)
        self.assertIn("mod._g3_set_shed(", page)


class TestDashboardServesThePage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        handler = type("H", (dashboard._DashboardHandler,), {"state_dir": Path(self._tmp.name)})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.getheader("Content-Type"), resp.read()
        finally:
            conn.close()

    def test_page_and_engine_are_served(self):
        status, content_type, body = self.get("/3d")
        self.assertEqual((status, content_type), (200, "text/html; charset=utf-8"))
        self.assertIn(b"garden3d-canvas", body)
        status, content_type, body = self.get("/3d/garden3d.wasm")
        self.assertEqual((status, content_type), (200, "application/wasm"))

    def test_the_2d_page_links_to_it(self):
        self.assertIn('href="/3d"', dashboard.PAGE_HTML)

    def test_other_paths_under_3d_are_404(self):
        self.assertEqual(self.get("/3d/garden3d.html")[0], 404)


class TestHubServesThePage(unittest.TestCase):
    def setUp(self):
        self.hub = _HubFixture(_env())
        self.addCleanup(self.hub.close)

    def test_the_page_and_engine_require_an_operator(self):
        for path in ("/3d", "/3d/garden3d.js", "/3d/garden3d.wasm"):
            self.assertEqual(self.hub.request("GET", path)[0], 401, path)

    def test_its_status_names_each_beating_device_by_token(self):
        beat = {"seq": 1, "interval_seconds": 60, "ending": False, "session": {"id": "s1", "command": "overnight"},
                "in_progress": ["acme/forge"], "slots": [{"repo": "acme/forge", "phase": "running"}]}
        headers = {"Authorization": f"Bearer {PHONE_TOKEN}"}
        self.assertEqual(self.hub.request("POST", "/api/v1/heartbeat", headers, json.dumps(beat))[0], 200)
        status, _, body = self.hub.request("GET", "/api/status", _basic(OPERATOR))
        data = json.loads(body)
        self.assertIsNone(data["device"])
        [device] = data["live_devices"]
        self.assertEqual((device["device"], device["state"], device["in_progress"]),
                         ("phone", "live", ["acme/forge"]))

    def test_an_operator_gets_them(self):
        status, headers, body = self.hub.request("GET", "/3d", _basic(OPERATOR))
        self.assertEqual(status, 200)
        self.assertIn(b"garden3d-canvas", body)
        status, headers, body = self.hub.request("GET", "/3d/garden3d.wasm", _basic(OPERATOR))
        self.assertEqual((status, headers["Content-Type"]), (200, "application/wasm"))


if __name__ == "__main__":
    unittest.main()
