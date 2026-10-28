"""Tests for `gardener/fleetcity.py`: Fleet City at the hub's /city
(Stephenson-Software RFC 0026).

The page's JavaScript and the renderer (`fleetcity/main.cpp`) have no test
runner here (stdlib-only Python), so they are verified by rendering, as
/3d is. What is asserted here: the serving side (six URLs, the operator
check: a signed-out browser is sent to /login, the data is a 401), the
snapshot parser, the layout and data join, and the headless still when
Strata's binding is available. Every name is synthetic (tests/city_fixture.py).
"""
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gardener import cli, fleetcity, hub, state

import city_fixture
from test_hub import OPERATOR, PHONE_TOKEN, _basic, _env, _HubFixture, _UserAuthStub

REPO = Path(__file__).resolve().parent.parent
NOW = city_fixture.NOW


def _run(repo, outcome, when, device="box"):
    return state.Run(repo=repo, mode="tend", outcome=outcome, timestamp=when.isoformat(),
                     run_uuid=f"{repo}-{when.isoformat()}", device=device)


class TestAssets(unittest.TestCase):
    def test_each_url_maps_to_its_file_and_type(self):
        self.assertEqual(fleetcity.asset("/city")[0], "text/html; charset=utf-8")
        self.assertEqual(fleetcity.asset("/city/")[0], "text/html; charset=utf-8")
        self.assertEqual(fleetcity.asset("/city/city.js")[0], "text/javascript; charset=utf-8")
        content_type, body = fleetcity.asset("/city/city.wasm")
        self.assertEqual(content_type, "application/wasm")
        self.assertEqual(body[:4], b"\0asm")
        for kit_script in ("/city/strata-kit.js", "/city/strata-loader.js"):
            content_type, body = fleetcity.asset(kit_script)
            self.assertEqual(content_type, "text/javascript; charset=utf-8")
            self.assertIn(b"MIT licensed, as Strata", body)

    def test_the_kit_scripts_are_the_same_files_as_the_3d_gardens(self):
        # One vendored kit: /city and /3d serve the very same two files.
        from gardener import garden3d
        for name in ("strata-kit.js", "strata-loader.js"):
            self.assertEqual(fleetcity.ASSETS[f"/city/{name}"], garden3d.ASSETS[f"/3d/{name}"])

    def test_nothing_outside_the_table_is_served(self):
        for path in ("/city/city.html", "/city/../hub.py", "/city/x", "/city.wasm", "/city/garden3d.js",
                     "/city/strata_kit.h", "/city/build-kit.sh"):
            self.assertIsNone(fleetcity.asset(path), path)
            self.assertFalse(fleetcity.is_asset_path(path), path)

    def test_the_page_calls_only_exported_functions(self):
        # build.sh names the city's own exports; the vendored kit's
        # build-kit.sh adds its sk_* shell and malloc/free. A function the
        # page (or the kit's page shell) calls but the build doesn't export
        # is undefined in the browser, and nothing but a render would notice.
        build = (REPO / "fleetcity" / "build.sh").read_text(encoding="utf-8")
        kit_build = (REPO / "strata-kit" / "build-kit.sh").read_text(encoding="utf-8")
        exported = (set(re.search(r"KIT_EXPORTS=(\S+)", build).group(1).split(","))
                    | set(re.search(r'^exports="([^"]+)"', kit_build, re.M).group(1).split(",")))
        page = (fleetcity.WEB_DIR / "city.html").read_text(encoding="utf-8")
        kit = (fleetcity.WEB_DIR / "strata-kit.js").read_text(encoding="utf-8")
        called = set(re.findall(r"mod\.(_\w+)\(", page)) | set(re.findall(r"mod\.(_\w+)\(", kit))
        self.assertTrue(called)
        self.assertIn("_fc_pick", called)
        self.assertIn("_sk_frame", called)
        self.assertEqual(called - exported, set())
        glue = (fleetcity.WEB_DIR / "city.js").read_text(encoding="utf-8")
        for name in called:
            self.assertIn(name, glue, f"the committed build predates {name}; rerun fleetcity/build.sh")
        # The kit's build exports HEAPF32, not HEAPU8: the page must not use it.
        self.assertNotIn("mod.HEAPU8", page)

    def test_the_page_loads_its_assets_from_the_served_urls(self):
        page = (fleetcity.WEB_DIR / "city.html").read_text(encoding="utf-8")
        for url in ("/city/strata-loader.js", "/city/strata-kit.js"):
            self.assertIn(f'src="{url}"', page)
            self.assertIn(url, fleetcity.ASSETS)
        self.assertIn('script: "/city/city.js"', page)
        self.assertIn('wasm: "/city/city.wasm"', page)
        self.assertIn('base: "/city/"', page)  # locateFile, for anything else the glue asks for
        self.assertIn('fetch("/api/city"', page)

    def test_the_still_pins_the_same_strata_release_as_the_fetch_script(self):
        script = (REPO / "fleetcity" / "fetch-strata-python.sh").read_text(encoding="utf-8")
        self.assertIn(f"LIB_SHA256={fleetcity.STRATA_LIB_SHA256}", script)
        self.assertIn(f"VERSION={fleetcity.STRATA_RELEASE}", script)


class TestSnapshot(unittest.TestCase):
    def test_only_named_fields_are_kept(self):
        doc = city_fixture.snapshot(city_fixture.fleet(3))
        doc["repos"][0]["description"] = "free text never drawn"
        parsed = fleetcity.parse_snapshot(doc)
        self.assertEqual(len(parsed["repos"]), 3)
        self.assertEqual(set(parsed["repos"][0]),
                         {"repo", "private", "fork", "createdAt", "score", "commits90d", "daysStale"})
        self.assertEqual(parsed["owners"], city_fixture.OWNERS)

    def test_a_bad_name_or_schema_is_refused(self):
        with self.assertRaises(fleetcity.InvalidSnapshot):
            fleetcity.parse_snapshot({"schema": 2, "repos": []})
        with self.assertRaises(fleetcity.InvalidSnapshot):
            fleetcity.parse_snapshot({"schema": 1, "repos": [{"owner": "acme", "name": "../etc"}]})

    def test_out_of_range_numbers_become_unknown(self):
        doc = {"schema": 1, "repos": [{"owner": "acme", "name": "x", "score": 900, "commits90d": -1,
                                       "daysStale": True, "private": "yes"}]}
        [r] = fleetcity.parse_snapshot(doc)["repos"]
        self.assertEqual((r["score"], r["commits90d"], r["daysStale"], r["private"]), (None, None, None, None))

    def test_a_broken_file_is_ignored_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(fleetcity.load_snapshot(Path(tmp)))
            (Path(tmp) / "city.json").write_text("{not json", encoding="utf-8")
            with _quiet():
                self.assertIsNone(fleetcity.load_snapshot(Path(tmp)))


class _quiet:
    def __enter__(self):
        self._err = sys.stderr
        sys.stderr = open(os.devnull, "w")

    def __exit__(self, *exc):
        sys.stderr.close()
        sys.stderr = self._err


class TestBuildCity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "hub"
        hub.prepare_store(self.data_dir)
        self.db = self.data_dir / "gardener.sqlite3"

    def test_without_a_snapshot_the_city_is_the_hubs_repos(self):
        state.insert_runs([
            _run("acme/birch", "tend", NOW - timedelta(hours=3)),
            _run("acme/onyx", "error", NOW - timedelta(hours=2)),
            _run("globex/maple", "created", NOW - timedelta(days=20)),
        ], db_path=self.db)
        city = fleetcity.build_city(self.db, garden_repos=["acme/birch", "initech/cedar"], now=NOW)
        self.assertEqual(city["colour_by"], "gardener")
        repos = {b["repo"]: b for b in city["buildings"]}
        self.assertEqual(set(repos), {"acme/birch", "acme/onyx", "globex/maple", "initech/cedar"})
        self.assertEqual([d["owner"] for d in city["districts"]], ["acme", "globex", "initech"])
        self.assertEqual(repos["acme/birch"]["health"], fleetcity.GOOD)
        self.assertEqual(repos["acme/onyx"]["health"], fleetcity.POOR)      # never succeeded
        self.assertEqual(repos["globex/maple"]["health"], fleetcity.POOR)   # 20 days
        self.assertEqual(repos["initech/cedar"]["health"], fleetcity.UNKNOWN)
        self.assertTrue(repos["initech/cedar"]["in_garden"])
        # Only runs inside the window are events; the 20-day-old one is not.
        kinds = sorted(k for _, _, k in city["events"])
        self.assertEqual(kinds, [fleetcity.EVENT_OK, fleetcity.EVENT_ERROR])

    def test_a_snapshot_adds_the_fleet_and_colours_by_score(self):
        repos = city_fixture.fleet(40)
        snap = fleetcity.parse_snapshot(city_fixture.snapshot(repos))
        city = fleetcity.build_city(self.db, snapshot=snap, now=NOW)
        self.assertEqual(city["colour_by"], "score")
        self.assertEqual(len(city["buildings"]), 40)
        for b in city["buildings"]:
            score = b["score"]
            want = (fleetcity.UNKNOWN if score is None else fleetcity.GOOD if score >= 80
                    else fleetcity.FAIR if score >= 50 else fleetcity.POOR)
            self.assertEqual(b["health"], want, b["repo"])
        self.assertEqual(city["texture"], [32, 2])
        self.assertEqual(sorted(b["id"] for b in city["buildings"]), list(range(40)))

    def test_buildings_sit_inside_their_district_and_never_overlap(self):
        city = fleetcity.build_city(self.db, snapshot=fleetcity.parse_snapshot(
            city_fixture.snapshot(city_fixture.fleet(150))), now=NOW)
        for b in city["buildings"]:
            d = city["districts"][b["district"]]
            self.assertTrue(d["x0"] < b["x0"] < b["x1"] < d["x1"] and d["z0"] < b["z0"] < b["z1"] < d["z1"])
        plots = {(b["x0"], b["z0"]) for b in city["buildings"]}
        self.assertEqual(len(plots), 150)
        for a in city["districts"]:
            for b in city["districts"]:
                if a is not b:
                    self.assertFalse(a["x0"] < b["x1"] and b["x0"] < a["x1"] and a["z0"] < b["z1"] and b["z0"] < a["z1"])

    def test_plots_are_stable_when_a_repository_is_added(self):
        repos = city_fixture.fleet(30)
        before = fleetcity.build_city(self.db, snapshot=fleetcity.parse_snapshot(city_fixture.snapshot(repos)), now=NOW)
        newest = dict(repos[0], name="newest-999", createdAt="2026-10-27T00:00:00Z")
        after = fleetcity.build_city(self.db, snapshot=fleetcity.parse_snapshot(
            city_fixture.snapshot(repos + [newest])), now=NOW)
        old = {b["repo"]: (b["x0"], b["z0"]) for b in before["buildings"]}
        new = {b["repo"]: (b["x0"], b["z0"]) for b in after["buildings"]}
        self.assertEqual({k: new[k] for k in old}, old)
        self.assertIn(f"{repos[0]['owner']}/newest-999", new)

    def test_in_flight_comes_from_live_and_stale_heartbeats_only(self):
        live = [{"state": "live", "snapshot": {"in_progress": ["acme/birch"]}},
                {"state": "idle", "snapshot": {"in_progress": ["acme/onyx"]}}]
        city = fleetcity.build_city(self.db, garden_repos=["acme/birch", "acme/onyx"], live_devices=live, now=NOW)
        self.assertEqual({b["repo"]: b["in_flight"] for b in city["buildings"]},
                         {"acme/birch": True, "acme/onyx": False})

    def test_glow_fades_and_palettes_have_one_texel_per_building(self):
        state.insert_runs([_run("acme/birch", "error", NOW - timedelta(hours=3))], db_path=self.db)
        city = fleetcity.build_city(self.db, now=NOW)
        glow = fleetcity.glow_levels(city, city["now"])
        [(kind, level)] = glow.values()
        self.assertEqual(kind, fleetcity.EVENT_ERROR)
        self.assertAlmostEqual(level, 0.5, places=2)
        self.assertEqual(fleetcity.glow_levels(city, city["now"] + 4 * 3600), {})
        body, roof = fleetcity.palettes(city, glow)
        self.assertEqual(len(body), 32 * 4)
        self.assertNotEqual(body[:4], roof[:4])

    def test_the_payload_is_json_and_deterministic(self):
        state.insert_runs([_run("acme/birch", "tend", NOW - timedelta(hours=1))], db_path=self.db)
        a = json.dumps(fleetcity.build_city(self.db, now=NOW), sort_keys=True)
        self.assertEqual(a, json.dumps(fleetcity.build_city(self.db, now=NOW), sort_keys=True))


class TestHubServesTheCity(unittest.TestCase):
    def setUp(self):
        self.hub = _HubFixture(_env())
        self.addCleanup(self.hub.close)

    def test_page_engine_and_data_require_an_operator(self):
        for path in ("/city", "/city/", "/city/city.js", "/city/city.wasm", "/city/strata-kit.js",
                     "/city/strata-loader.js", "/api/city"):
            self.assertEqual(self.hub.request("GET", path)[0], 401, path)

    def test_a_device_token_cannot_read_the_city(self):
        headers = {"Authorization": f"Bearer {PHONE_TOKEN}"}
        for path in ("/city", "/api/city"):
            self.assertEqual(self.hub.request("GET", path, headers)[0], 401, path)

    def test_an_operator_gets_the_page_and_the_data(self):
        status, headers, body = self.hub.request("GET", "/city", _basic(OPERATOR))
        self.assertEqual(status, 200)
        self.assertIn(b"city-canvas", body)
        status, headers, _ = self.hub.request("GET", "/city/city.wasm", _basic(OPERATOR))
        self.assertEqual((status, headers["Content-Type"]), (200, "application/wasm"))
        for script in ("/city/strata-kit.js", "/city/strata-loader.js"):
            status, headers, _ = self.hub.request("GET", script, _basic(OPERATOR))
            self.assertEqual((status, headers["Content-Type"]), (200, "text/javascript; charset=utf-8"), script)
        city_fixture.write_hub(self.hub.data_dir, 25)
        status, headers, body = self.hub.request("GET", "/api/city", _basic(OPERATOR))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        data = json.loads(body)
        self.assertEqual((data["schema"], len(data["buildings"])), (1, 25))

    def test_a_bad_nights_value_is_a_400(self):
        self.assertEqual(self.hub.request("GET", "/api/city?nights=x", _basic(OPERATOR))[0], 400)


class TestUserAuthGate(unittest.TestCase):
    """With UserAuth on (the live hub), a signed-out browser asking for the
    page is sent to /login, and the data is a plain 401."""

    def setUp(self):
        self.ua = ThreadingHTTPServer(("127.0.0.1", 0), _UserAuthStub)
        threading.Thread(target=self.ua.serve_forever, daemon=True).start()
        self.addCleanup(self.ua.server_close)
        self.addCleanup(self.ua.shutdown)
        self.hub = _HubFixture(_env(**{hub.USERAUTH_URL_ENV: f"http://127.0.0.1:{self.ua.server_address[1]}",
                                       hub.OPERATORS_ENV: "dan"}))
        self.addCleanup(self.hub.close)

    def test_anonymous_city_is_sent_to_sign_in(self):
        for path in ("/city", "/city/"):
            status, headers, _ = self.hub.request("GET", path)
            self.assertEqual((status, headers.get("Location")), (303, "/login"), path)

    def test_anonymous_api_city_is_401(self):
        status, headers, _ = self.hub.request("GET", "/api/city")
        self.assertEqual(status, 401)
        self.assertNotIn("Location", headers)
        self.assertEqual(self.hub.request("GET", "/city/city.wasm")[0], 401)
        self.assertEqual(self.hub.request("GET", "/city/strata-kit.js")[0], 401)

    def test_an_account_off_the_allowlist_is_refused(self):
        session = {"Cookie": f"{hub.SESSION_COOKIE}=jwt-stranger"}
        self.assertEqual(self.hub.request("GET", "/city", session)[0], 303)
        self.assertEqual(self.hub.request("GET", "/api/city", session)[0], 401)


def _strata_paths():
    lib, py = os.environ.get(fleetcity.STRATA_LIB_ENV), os.environ.get(fleetcity.STRATA_PYTHON_ENV)
    return (lib, py) if lib and py and Path(lib).exists() and Path(py).exists() else None


class TestCityStill(unittest.TestCase):
    def test_a_missing_binding_is_exit_2_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp, _quiet():
            env = {k: v for k, v in os.environ.items() if k not in (fleetcity.STRATA_LIB_ENV, fleetcity.STRATA_PYTHON_ENV)}
            with patch.dict(os.environ, env, clear=True):
                # The command itself, not main(): main() would also send a
                # usage event, and this test clears the environment.
                args = cli.build_parser().parse_args(
                    ["hub", "city-still", "--data-dir", tmp, "--out", str(Path(tmp) / "x.png")])
                rc = args.func(args)
            self.assertEqual(rc, 2)
            self.assertFalse((Path(tmp) / "x.png").exists())

    def test_a_library_that_is_not_the_pinned_release_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "libstrata.so"
            fake.write_bytes(b"not it")
            with self.assertRaisesRegex(fleetcity.StillUnavailable, "not libstrata 0.6.0"):
                fleetcity._load_strata(str(fake), tmp)

    def test_hours_are_bounded(self):
        with self.assertRaises(SystemExit), _quiet():
            cli.build_parser().parse_args(["hub", "city-still", "--out", "x.png", "--hours", "5000"])

    @unittest.skipUnless(_strata_paths(), "set GARDENER_STRATA_LIB and GARDENER_STRATA_PYTHON "
                                          "(fleetcity/fetch-strata-python.sh) to render")
    def test_renders_a_png_headless(self):
        lib, py = _strata_paths()
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "hub"
            city_fixture.write_hub(data_dir, 60)
            out = Path(tmp) / "still.png"
            proc = subprocess.run(
                [sys.executable, "-m", "gardener", "hub", "city-still", "--data-dir", str(data_dir),
                 "--out", str(out), "--width", "640", "--height", "360"],
                capture_output=True, text=True, timeout=120,
                env=dict(os.environ, PYTHONPATH=str(REPO), GARDENER_USAGE_REPORTING_ENABLED="false"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Fleet City: 60 repos", proc.stdout)
            head = out.read_bytes()[:24]
            self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", head[16:24]), (640, 360))


if __name__ == "__main__":
    unittest.main()
