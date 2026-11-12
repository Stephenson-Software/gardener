"""Tests for `gardener/fleetweather.py`: weather over Fleet City
(Stephenson-Software RFC 0032).

Asserted here: the weather rules, the program map and trace-answer parsers,
the read of trace (with a stub opener, never the network), the hub's cache,
what `/api/city` adds, and the still with weather when Strata's binding is
available. Every repository name is synthetic (tests/city_fixture.py).
"""
import base64
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

from gardener import fleetcity, fleetweather as fw, hub

import city_fixture
import weather_fixture
from test_fleetcity import REPO, _quiet, _strata_paths
from test_hub import OPERATOR, _basic, _env, _HubFixture


def _city(count=60):
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "hub"
        city_fixture.write_hub(d, count)
        db = d / "gardener.sqlite3"
        return fleetcity.build_city(db, snapshot=fleetcity.load_snapshot(d),
                                    garden_repos=hub.union_device_lists(db)["garden"], now=city_fixture.NOW)


def _window(*rows, seconds=300):
    return {"windowSeconds": seconds, "since": "x", "until": "y",
            "programs": [{"application": a, "events": e, "uses": u, "flood": f} for a, e, u, f in rows]}


CONFIG = fw.Config(True, "https://trace.example", "hub:r3ad")


class TestClassify(unittest.TestCase):
    def test_no_events_is_clear(self):
        self.assertEqual(fw.classify({}, 300)["kind"], fw.CLEAR)

    def test_a_trickle_is_drizzle_and_steady_use_is_rain(self):
        self.assertEqual(fw.classify({"events": 2}, 300)["kind"], fw.DRIZZLE)
        self.assertEqual(fw.classify({"events": 30}, 300)["kind"], fw.RAIN)

    def test_any_flood_or_the_floodguard_rate_is_a_storm(self):
        self.assertEqual(fw.classify({"events": 1, "flood": 1}, 300)["kind"], fw.STORM)
        self.assertEqual(fw.classify({"events": 300}, 300)["kind"], fw.STORM)
        self.assertEqual(fw.classify({"events": 299}, 300)["kind"], fw.RAIN)

    def test_the_mf_power_flood_is_full_intensity(self):
        # ~10 a second for a five-minute window.
        cell = fw.classify({"events": 300, "flood": 2700}, 300)
        self.assertEqual(cell["intensity"], 1.0)
        self.assertEqual(cell["per_minute"], 600.0)

    def test_errors_strike_lightning_even_under_a_clear_sky(self):
        cell = fw.classify({"errors": 1}, 300)
        self.assertTrue(cell["lightning"])
        self.assertEqual(cell["kind"], fw.CLEAR)


class TestForecast(unittest.TestCase):
    def test_flood_series_peaks_as_one_storm(self):
        city = _city()
        states = weather_fixture.states(city)
        self.assertEqual(fw.forecast(city, states["clear"]), {})
        flood = fw.forecast(city, states["flood"])
        self.assertEqual(fw.summary(flood)["storm"], 1)
        self.assertGreater(fw.sky(flood, len(city["buildings"]))["overcast"], 0)
        self.assertEqual(fw.summary(fw.forecast(city, states["lightning"]))["lightning"], 3)

    def test_a_repository_not_in_the_city_is_left_out(self):
        city = _city(10)
        self.assertEqual(fw.forecast(city, {"window_seconds": 300, "repos": {"nobody/here": {"events": 99}}}), {})


class TestProgramMap(unittest.TestCase):
    def test_one_repo_or_several(self):
        self.assertEqual(fw.parse_program_map({"A": "acme/birch-001", "B": ["acme/x", "globex/y", "acme/x"]}),
                         {"A": ["acme/birch-001"], "B": ["acme/x", "globex/y"]})

    def test_refuses_what_is_not_owner_name(self):
        for doc in ([], {"A": "no-slash"}, {"A": []}, {"A": ["../x"]}, {"A": 3}, {"": "acme/x"}):
            with self.assertRaises(fw.InvalidProgramMap, msg=repr(doc)):
                fw.parse_program_map(doc)

    def test_loading_a_missing_or_broken_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp, _quiet():
            self.assertIsNone(fw.load_program_map(Path(tmp)))
            (Path(tmp) / fw.PROGRAMS_NAME).write_text("{not json")
            self.assertIsNone(fw.load_program_map(Path(tmp)))
            (Path(tmp) / fw.PROGRAMS_NAME).write_text(json.dumps({"A": "acme/x"}))
            self.assertEqual(fw.load_program_map(Path(tmp)), {"A": ["acme/x"]})


class TestTraceWindow(unittest.TestCase):
    def test_parses_only_the_counts(self):
        doc = _window(("A", 3, 7, 2))
        doc["programs"][0]["install"] = "should-never-be-read"
        self.assertEqual(fw.parse_window(doc),
                         {"window_seconds": 300, "programs": {"A": {"events": 3, "uses": 7, "flood": 2}}})

    def test_refuses_an_answer_that_is_not_a_window(self):
        for doc in ({}, {"windowSeconds": 300}, _window(("A", -1, 0, 0)), _window(("A", 1, 1, True)),
                    {"windowSeconds": "300", "programs": []}):
            with self.assertRaises(fw.WeatherUnavailable, msg=repr(doc)):
                fw.parse_window(doc)

    def test_fetch_sends_the_reader_credential_to_the_window(self):
        seen = {}

        def opener(request, timeout):
            seen["url"], seen["auth"], seen["timeout"] = request.full_url, request.get_header("Authorization"), timeout
            return io.BytesIO(json.dumps(_window(("A", 1, 1, 0))).encode())

        self.assertEqual(fw.fetch_window(CONFIG, opener=opener)["programs"]["A"]["uses"], 1)
        self.assertEqual(seen["url"], "https://trace.example/api/metrics/window?seconds=300")
        self.assertEqual(seen["auth"], "Basic " + base64.b64encode(b"hub:r3ad").decode())

    def test_fetch_failures_are_short_reasons_without_the_credential(self):
        def refused(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

        def down(request, timeout):
            raise urllib.error.URLError("connection refused")

        def garbage(request, timeout):
            return io.BytesIO(b"<html>")

        for opener, reason in ((refused, "trace answered 403"), (down, "trace unreachable (URLError)"),
                               (garbage, "trace's answer is not JSON")):
            with self.assertRaises(fw.WeatherUnavailable) as caught:
                fw.fetch_window(CONFIG, opener=opener)
            self.assertEqual(str(caught.exception), reason)
            self.assertNotIn("r3ad", str(caught.exception))

    def test_configuration_problems(self):
        self.assertEqual(fw.Config.from_env({}).problem(), "off")
        self.assertIn("GARDENER_HUB_TRACE_URL", fw.Config.from_env({"GARDENER_HUB_WEATHER": "1"}).problem())
        self.assertIn("not an http(s) URL", fw.Config.from_env(
            {"GARDENER_HUB_WEATHER": "on", "GARDENER_HUB_TRACE_URL": "file:///etc"}).problem())
        self.assertIn("user:password", fw.Config.from_env(
            {"GARDENER_HUB_WEATHER": "yes", "GARDENER_HUB_TRACE_URL": "http://trace:8080"}).problem())
        self.assertIsNone(fw.Config.from_env(
            {"GARDENER_HUB_WEATHER": "true", "GARDENER_HUB_TRACE_URL": "http://trace:8080/",
             "GARDENER_HUB_TRACE_READER": "hub:pw"}).problem())

    def test_sample_maps_programs_onto_repositories_and_sums(self):
        window = fw.parse_window(_window(("A", 3, 7, 0), ("B", 1, 1, 50), ("Unmapped", 9, 9, 9)))
        out = fw.sample(window, {"A": ["acme/x"], "B": ["acme/x", "globex/y"]})
        self.assertEqual(out, {"window_seconds": 300, "repos": {
            "acme/x": {"events": 8, "flood": 50, "errors": 0},
            "globex/y": {"events": 1, "flood": 50, "errors": 0}}})


class TestForecaster(unittest.TestCase):
    def test_reads_trace_at_most_once_a_minute_and_remembers_failures(self):
        calls, now = [], [0.0]

        def fetch(config):
            calls.append(now[0])
            if len(calls) == 2:
                raise fw.WeatherUnavailable("trace answered 503")
            return fw.parse_window(_window(("A", 1, 1, 0)))

        forecaster = fw.Forecaster(CONFIG, fetch=fetch, clock=lambda: now[0])
        with _quiet():
            self.assertEqual(forecaster.window()[1], "ok")
            now[0] = 59
            self.assertEqual(forecaster.window()[1], "ok")
            now[0] = 61
            self.assertEqual(forecaster.window(), (None, "trace answered 503"))
            now[0] = 100
            self.assertEqual(forecaster.window(), (None, "trace answered 503"))
            now[0] = 125
            self.assertEqual(forecaster.window()[1], "ok")
        self.assertEqual(calls, [0.0, 61, 125])

    def test_off_never_reads(self):
        forecaster = fw.Forecaster(fw.Config(False, None, None), fetch=lambda c: self.fail("read trace"))
        self.assertEqual(forecaster.window(), (None, "off"))


class TestParseSample(unittest.TestCase):
    def test_keeps_the_counts(self):
        self.assertEqual(fw.parse_sample({"window_seconds": 300, "repos": {"acme/x": {"events": 4, "x": "y"}}}),
                         {"window_seconds": 300, "repos": {"acme/x": {"events": 4, "flood": 0, "errors": 0}}})

    def test_refuses_a_bad_sample(self):
        for doc in ({}, {"window_seconds": 0, "repos": {}}, {"window_seconds": 300, "repos": {"x": {}}},
                    {"window_seconds": 300, "repos": {"acme/x": {"events": -1}}}):
            with self.assertRaises(ValueError, msg=repr(doc)):
                fw.parse_sample(doc)


class TestHubCityWeather(unittest.TestCase):
    def setUp(self):
        self.hub = _HubFixture(_env())
        self.addCleanup(self.hub.close)
        city_fixture.write_hub(self.hub.data_dir, 25)

    def _set(self, forecaster):
        self.hub.httpd.RequestHandlerClass.weather = forecaster

    def _city(self):
        status, _, body = self.hub.request("GET", "/api/city", _basic(OPERATOR))
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_off_by_default_adds_nothing(self):
        self.assertNotIn("weather", self._city())
        self._set(fw.Forecaster(fw.Config(False, None, None)))
        self.assertNotIn("weather", self._city())

    def test_on_adds_one_row_per_building_with_weather(self):
        city = self._city()
        repo = city["buildings"][3]["repo"]
        (self.hub.data_dir / fw.PROGRAMS_NAME).write_text(json.dumps({"Prog": repo}))
        self._set(fw.Forecaster(CONFIG, fetch=lambda c: fw.parse_window(_window(("Prog", 10, 30, 200)))))
        weather = self._city()["weather"]
        self.assertEqual(weather["status"], "ok")
        self.assertEqual(weather["window_seconds"], 300)
        self.assertEqual(weather["cells"], [[3, fw.STORM, weather["cells"][0][2], 0, 46.0]])
        self.assertEqual(weather["sky"]["storms"], 1)

    def test_trace_down_or_no_map_is_a_clear_sky_with_a_reason(self):
        def down(config):
            raise fw.WeatherUnavailable("trace unreachable (URLError)")

        with _quiet():
            self._set(fw.Forecaster(CONFIG, fetch=down))
            self.assertEqual(self._city()["weather"]["status"], "trace unreachable (URLError)")
            self._set(fw.Forecaster(CONFIG, fetch=lambda c: fw.parse_window(_window(("Prog", 1, 1, 0)))))
            weather = self._city()["weather"]
        self.assertIn(fw.PROGRAMS_NAME, weather["status"])
        self.assertEqual(weather["cells"], [])


class TestStillWeather(unittest.TestCase):
    def test_a_bad_sample_file_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp, _quiet():
            bad = Path(tmp) / "w.json"
            bad.write_text("{}")
            from gardener import cli
            args = cli.build_parser().parse_args(["hub", "city-still", "--data-dir", tmp, "--out",
                                                  str(Path(tmp) / "x.png"), "--weather-sample", str(bad)])
            self.assertEqual(args.func(args), 2)

    @unittest.skipUnless(_strata_paths(), "set GARDENER_STRATA_LIB and GARDENER_STRATA_PYTHON "
                                          "(fleetcity/fetch-strata-python.sh) to render")
    def test_renders_the_flood_as_a_storm(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "hub"
            city_fixture.write_hub(data_dir, 60)
            sample = weather_fixture.states(_city(60))["flood"]
            sample_path = Path(tmp) / "flood.json"
            sample_path.write_text(json.dumps(sample))
            out = Path(os.environ.get("GARDENER_STILL_DIR") or tmp) / "weather-flood.png"
            proc = subprocess.run(
                [sys.executable, "-m", "gardener", "hub", "city-still", "--data-dir", str(data_dir),
                 "--out", str(out), "--width", "640", "--height", "360", "--weather-sample", str(sample_path)],
                capture_output=True, text=True, timeout=120,
                env=dict(os.environ, PYTHONPATH=str(REPO), GARDENER_USAGE_REPORTING_ENABLED="false"))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("weather:", proc.stdout)
            self.assertIn("1 storm", proc.stdout)
            head = out.read_bytes()[:24]
            self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", head[16:24]), (640, 360))


if __name__ == "__main__":
    unittest.main()
