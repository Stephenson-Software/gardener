"""Tests for `gardener/garden3d.py`: the 3D garden page at /3d.

The page's JavaScript and the renderer (`garden3d/main.cpp`) have no test
runner here (stdlib-only Python), so they are verified by rendering, as
the 2D garden is (docs/DASHBOARD.md). What is asserted here is the serving
side: exactly three URLs, the right content types, the hub's operator
check, and that the page only calls functions the build exports.
"""
import http.client
import re
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from gardener import dashboard, garden3d
from test_hub import OPERATOR, _basic, _env, _HubFixture

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

    def test_an_operator_gets_them(self):
        status, headers, body = self.hub.request("GET", "/3d", _basic(OPERATOR))
        self.assertEqual(status, 200)
        self.assertIn(b"garden3d-canvas", body)
        status, headers, body = self.hub.request("GET", "/3d/garden3d.wasm", _basic(OPERATOR))
        self.assertEqual((status, headers["Content-Type"]), (200, "application/wasm"))


if __name__ == "__main__":
    unittest.main()
