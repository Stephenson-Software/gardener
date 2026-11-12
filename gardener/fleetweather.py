"""Weather over Fleet City: trace usage as rain, floods as storms
(Stephenson-Software RFC 0032).

The city (`fleetcity.py`) lays out one building per repository; this module
reads how much each one is *used*, from trace, and turns it into one weather
cell per building:

* `fetch_window` reads trace's `GET /api/metrics/window` (every program's
  events over a trailing window, ordinary and flood-tagged apart; aggregates
  only) with a reader's basic credential, which trace accepts on that one
  path and nowhere else.
* `load_program_map` reads `programs.json` from the hub's data directory:
  which repository each trace program belongs to (trace keys by program,
  the city by repository). A program it does not name never rains.
* `sample` joins the two into the RFC's sample, keyed by repository:

      {"window_seconds": 300,
       "repos": {"owner/name": {"events": 120, "flood": 0, "errors": 0}}}

  `events` are the ordinary uses in the window (trace's `uses`: a coalesced
  report counts as the repeats it stands for), `flood` the events trace's
  FloodGuard tagged `flood=true`. `errors` drives lightning; nothing fills
  it yet (RFC 0032, Open Question 3: trace has no error events), so the
  hook is drawn when a sample carries it and is otherwise always zero.
* `classify`, `forecast` and `sky` turn the sample into cells; `cells_payload`
  is what `/api/city` adds for the page, and the drawing helpers below put
  the same cells on the headless still.

Weather is off unless the hub's environment turns it on
(`GARDENER_HUB_WEATHER=1` with `GARDENER_HUB_TRACE_URL` and
`GARDENER_HUB_TRACE_READER`). Every failure to read trace leaves the city
clear with a status saying why; it never fails `/api/city` or the still.
"""
from __future__ import annotations

import base64
import json
import math
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, Optional

# ------------------------------------------------------------------ config

ENABLED_ENV = "GARDENER_HUB_WEATHER"
TRACE_URL_ENV = "GARDENER_HUB_TRACE_URL"
TRACE_READER_ENV = "GARDENER_HUB_TRACE_READER"
_ON = frozenset(("1", "true", "yes", "on"))

#: The trailing window asked of trace, in seconds (RFC 0032 §1).
WINDOW_SECONDS = 300
#: How long one answer from trace is reused: the page polls `/api/city`
#: once a minute, and two viewers must not mean two reads.
CACHE_SECONDS = 60
#: The longest a read of trace may take before the city is drawn clear.
FETCH_TIMEOUT = 5.0
#: The most of trace's answer read, and the most programs used from it.
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_PROGRAMS = 2000

PROGRAMS_NAME = "programs.json"
PROGRAMS_MAX_BYTES = 256 * 1024


class Config:
    """Where trace is and how to read it, from the hub's environment."""

    def __init__(self, enabled: bool, url: Optional[str], reader: Optional[str]):
        self.enabled = enabled
        self.url = url
        self.reader = reader

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        enabled = (env.get(ENABLED_ENV) or "").strip().lower() in _ON
        url = (env.get(TRACE_URL_ENV) or "").strip().rstrip("/") or None
        reader = env.get(TRACE_READER_ENV) or None
        return cls(enabled, url, reader)

    def problem(self) -> Optional[str]:
        """Why weather can't be read with this configuration, or None."""
        if not self.enabled:
            return "off"
        if not self.url:
            return f"{TRACE_URL_ENV} is not set"
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return f"{TRACE_URL_ENV} is not an http(s) URL"
        if not self.reader or ":" not in self.reader:
            return f"{TRACE_READER_ENV} must be user:password"
        return None


# ------------------------------------------------------------ program map


class InvalidProgramMap(ValueError):
    pass


def parse_program_map(doc: object) -> dict[str, list[str]]:
    """`programs.json` -- `{"program": "owner/repo"}` or
    `{"program": ["owner/repo", ...]}` -- as {program: [repos]}, or
    `InvalidProgramMap`. A program that names several repositories rains on
    each; several programs naming one repository add up there."""
    from gardener.cli import REPO_RE

    if not isinstance(doc, dict) or len(doc) > MAX_PROGRAMS:
        raise InvalidProgramMap(f"must be an object of at most {MAX_PROGRAMS} programs")
    out: dict[str, list[str]] = {}
    for program, target in doc.items():
        if not isinstance(program, str) or not program or len(program) > 255:
            raise InvalidProgramMap("a program name is empty or too long")
        repos = [target] if isinstance(target, str) else target
        if not isinstance(repos, list) or not repos or len(repos) > 20:
            raise InvalidProgramMap(f"{program!r} must name a repository or a list of up to 20")
        for repo in repos:
            if not isinstance(repo, str) or len(repo) > 200 or not REPO_RE.match(repo):
                raise InvalidProgramMap(f"{program!r} names something that is not owner/name")
        out[program] = list(dict.fromkeys(repos))
    return out


def load_program_map(data_dir: Path) -> Optional[dict[str, list[str]]]:
    """The hub's `programs.json`, or None when there is none. One that can't
    be read or parsed is reported on stderr and ignored (no weather), like
    the fleet snapshot."""
    path = Path(data_dir) / PROGRAMS_NAME
    try:
        if path.stat().st_size > PROGRAMS_MAX_BYTES:
            print(f"gardener hub: {path} is over {PROGRAMS_MAX_BYTES} bytes; ignored", file=sys.stderr)
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"gardener hub: {path} unreadable ({type(exc).__name__}); ignored", file=sys.stderr)
        return None
    try:
        return parse_program_map(doc)
    except InvalidProgramMap as exc:
        print(f"gardener hub: {path} ignored: {exc}", file=sys.stderr)
        return None


# ------------------------------------------------------------------ trace


class WeatherUnavailable(RuntimeError):
    pass


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WeatherUnavailable("trace answered with a count that is not a whole number")
    return min(value, 10 ** 12)


def parse_window(doc: object) -> dict:
    """trace's window answer reduced to {"window_seconds", "programs":
    {name: {"events", "uses", "flood"}}}, or `WeatherUnavailable`. Nothing
    but those fields is read."""
    if not isinstance(doc, dict) or not isinstance(doc.get("programs"), list):
        raise WeatherUnavailable("trace's answer is not a usage window")
    seconds = doc.get("windowSeconds")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 86400:
        raise WeatherUnavailable("trace's answer has no window length")
    programs: dict[str, dict] = {}
    for row in doc["programs"][:MAX_PROGRAMS]:
        if not isinstance(row, dict) or not isinstance(row.get("application"), str):
            raise WeatherUnavailable("trace's answer has a row without a program")
        programs[row["application"]] = {"events": _count(row.get("events")), "uses": _count(row.get("uses")),
                                        "flood": _count(row.get("flood"))}
    return {"window_seconds": seconds, "programs": programs}


def fetch_window(config: Config, seconds: int = WINDOW_SECONDS, timeout: float = FETCH_TIMEOUT,
                 opener: Callable = urllib.request.urlopen) -> dict:
    """One read of trace's `GET /api/metrics/window`, parsed. Raises
    `WeatherUnavailable` with a short reason (never the credential)."""
    problem = config.problem()
    if problem:
        raise WeatherUnavailable(problem)
    request = urllib.request.Request(
        f"{config.url}/api/metrics/window?seconds={int(seconds)}",
        headers={"Accept": "application/json",
                 "Authorization": "Basic " + base64.b64encode(config.reader.encode("utf-8")).decode("ascii")})
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise WeatherUnavailable(f"trace answered {exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise WeatherUnavailable(f"trace unreachable ({type(exc).__name__})") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise WeatherUnavailable("trace's answer is too large")
    try:
        doc = json.loads(body.decode("utf-8"))
    except ValueError:
        raise WeatherUnavailable("trace's answer is not JSON") from None
    return parse_window(doc)


def sample(window: dict, program_map: Mapping[str, list[str]]) -> dict:
    """The RFC's per-repository sample from trace's per-program window."""
    repos: dict[str, dict] = {}
    for program, counts in window["programs"].items():
        for repo in program_map.get(program, ()):
            rec = repos.setdefault(repo, {"events": 0, "flood": 0, "errors": 0})
            rec["events"] += max(counts["uses"], counts["events"])
            rec["flood"] += counts["flood"]
    return {"window_seconds": window["window_seconds"], "repos": repos}


def parse_sample(doc: object) -> dict:
    """A sample in the RFC's shape (a file for `city-still --weather-sample`)
    reduced to the fields drawn, or ValueError."""
    from gardener.cli import REPO_RE

    if not isinstance(doc, dict) or not isinstance(doc.get("repos"), dict):
        raise ValueError("not a weather sample: needs window_seconds and repos")
    seconds = doc.get("window_seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 86400:
        raise ValueError("window_seconds must be a number of seconds between 1 and 86400")
    repos = {}
    for repo, rec in list(doc["repos"].items())[:MAX_PROGRAMS]:
        if not isinstance(repo, str) or not REPO_RE.match(repo) or not isinstance(rec, dict):
            raise ValueError("every repos key must be owner/name with an object of counts")
        counts = {}
        for key in ("events", "flood", "errors"):
            value = rec.get(key, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{repo}: {key} must be a whole number")
            counts[key] = value
        repos[repo] = counts
    return {"window_seconds": seconds, "repos": repos}


class Forecaster:
    """Reads the weather for the hub at most once per `CACHE_SECONDS`, and
    remembers the last outcome, good or bad, for that long."""

    def __init__(self, config: Config, fetch: Callable[[Config], dict] = fetch_window,
                 clock: Callable[[], float] = time.monotonic):
        self.config = config
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._at: Optional[float] = None
        self._outcome: tuple[Optional[dict], str] = (None, "off")

    def window(self) -> tuple[Optional[dict], str]:
        """(trace's window or None, status): status is "ok", "off", or why
        trace could not be read."""
        if not self.config.enabled:
            return None, "off"
        with self._lock:
            now = self._clock()
            if self._at is not None and now - self._at < CACHE_SECONDS:
                return self._outcome
            try:
                self._outcome = (self._fetch(self.config), "ok")
            except WeatherUnavailable as exc:
                print(f"gardener hub: weather unavailable: {exc}", file=sys.stderr)
                self._outcome = (None, str(exc))
            self._at = now
            return self._outcome

    def sample(self, data_dir: Path) -> tuple[Optional[dict], str]:
        """(the per-repository sample or None, status), with the program map
        read from `data_dir`."""
        window, status = self.window()
        if window is None:
            return None, status
        program_map = load_program_map(data_dir)
        if program_map is None:
            return None, f"no usable {PROGRAMS_NAME} in the hub's data directory"
        return sample(window, program_map), "ok"


def cells_payload(city: dict, sample_doc: Optional[dict], status: str) -> dict:
    """What `/api/city` adds under `weather`: the status, the window, the
    whole sky, and one row per building with weather --
    `[building id, kind, intensity, lightning (0/1), per minute]`."""
    if sample_doc is None:
        return {"status": status, "window_seconds": None, "sky": sky({}, len(city["buildings"])), "cells": []}
    cells = forecast(city, sample_doc)
    return {
        "status": status,
        "window_seconds": sample_doc["window_seconds"],
        "sky": sky(cells, len(city["buildings"])),
        "cells": [[bid, c["kind"], c["intensity"], 1 if c["lightning"] else 0, c["per_minute"]]
                  for bid, c in sorted(cells.items())],
    }


# ---------------------------------------------------------------- weather

CLEAR, DRIZZLE, RAIN, STORM = 0, 1, 2, 3
KIND_NAME = ("clear", "drizzle", "rain", "storm")

#: Per minute. Below DRIZZLE_BELOW is drizzle, from it up to STORM_AT is
#: rain, and STORM_AT or more ordinary events, or any flood event, is a
#: storm. STORM_AT matches trace's FloodGuard threshold (60 in 60 s).
DRIZZLE_BELOW = 1.0
STORM_AT = 60.0
#: The rate at which a cell is drawn at full intensity (10 a second: the
#: MedievalFactions /power flood of 2026-10-23).
FULL_AT = 600.0


def classify(rec: dict, window_seconds: float) -> dict:
    """One building's weather from its counts over the window."""
    minutes = max(window_seconds, 1.0) / 60.0
    events = max(0, int(rec.get("events") or 0))
    flood = max(0, int(rec.get("flood") or 0))
    errors = max(0, int(rec.get("errors") or 0))
    rate, flood_rate = events / minutes, flood / minutes
    total = rate + flood_rate
    if flood > 0 or rate >= STORM_AT:
        kind = STORM
    elif rate >= DRIZZLE_BELOW:
        kind = RAIN
    elif events > 0:
        kind = DRIZZLE
    else:
        kind = CLEAR
    intensity = 0.0 if kind == CLEAR else min(1.0, max(0.12, math.log10(1 + total) / math.log10(1 + FULL_AT)))
    return {"kind": kind, "intensity": round(intensity, 3), "lightning": errors > 0,
            "per_minute": round(total, 2)}


def forecast(city: dict, sample: dict) -> dict[int, dict]:
    """{building id: cell} for every building the sample names; buildings
    it does not name are clear and left out."""
    window = float(sample.get("window_seconds") or 60)
    ids = {b["repo"]: b["id"] for b in city["buildings"]}
    out = {}
    for repo, rec in (sample.get("repos") or {}).items():
        if repo in ids:
            cell = classify(rec, window)
            if cell["kind"] != CLEAR or cell["lightning"]:
                out[ids[repo]] = cell
    return out


def sky(cells: dict[int, dict], buildings: int) -> dict:
    """The whole sky: how overcast the city is (0..1), from the share of
    buildings under weather, storms counting three times."""
    if not buildings:
        return {"overcast": 0.0, "storms": 0, "lightning": 0}
    weight = sum(3 if c["kind"] == STORM else 1 for c in cells.values())
    return {"overcast": round(min(1.0, weight / max(8.0, buildings * 0.25)), 3),
            "storms": sum(1 for c in cells.values() if c["kind"] == STORM),
            "lightning": sum(1 for c in cells.values() if c["lightning"])}


def summary(cells: dict[int, dict]) -> dict[str, int]:
    """How many cells of each kind, and how many with lightning."""
    out = {name: 0 for name in KIND_NAME[1:]}
    for c in cells.values():
        out[KIND_NAME[c["kind"]]] += 1
    out["lightning"] = sum(1 for c in cells.values() if c["lightning"])
    return out


def point_lights(strata, city: dict, cells: dict[int, dict]) -> list:
    """A flash under every cloud that has lightning (Strata takes at most a
    handful of point lights, so the brightest eight)."""
    by_id = {b["id"]: b for b in city["buildings"]}
    struck = sorted((c["intensity"], bid) for bid, c in cells.items() if c["lightning"])[-8:]
    lights = []
    for _, bid in struck:
        b = by_id[bid]
        light = strata.PointLight()
        light.position = ((b["x0"] + b["x1"]) / 2, b["h"] + 3.0, (b["z0"] + b["z1"]) / 2)
        light.range = 9.0
        light.color = (0.75, 0.8, 1.0)
        light.intensity = 6.0
        lights.append(light)
    return lights


def _spread(kind: int) -> float:
    """Half-width of a cell's cloud and rain in world units (a plot is
    about 2): a storm covers its neighbours too, so a flood reads from
    across the city."""
    return {STORM: 2.6, RAIN: 1.25}.get(kind, 0.9)


def _cloud_base(b: dict, c: dict) -> float:
    return b["h"] + (1.6 if c["kind"] == STORM else 2.2 + 1.0 * c["intensity"])


def add_cloud_meshes(e, strata, city: dict, cells: dict[int, dict], box) -> None:
    """Rain and storm clouds as translucent slabs: two meshes in all (one
    per kind), so weather costs Strata at most two of its 256 meshes. Soft
    points blend additively and cannot draw a dark cloud; a slab can. Call
    before the frame (meshes are scene state)."""
    by_id = {b["id"]: b for b in city["buildings"]}
    for kind, rgba in ((RAIN, (0.30, 0.32, 0.36, 0.55)), (STORM, (0.06, 0.06, 0.08, 0.82))):
        mesh = {"p": [], "n": [], "c": [], "uv": [], "i": []}
        for bid, c in sorted(cells.items()):
            if c["kind"] != kind:
                continue
            b = by_id[bid]
            cx, cz, r, y = (b["x0"] + b["x1"]) / 2, (b["z0"] + b["z1"]) / 2, _spread(kind), _cloud_base(b, c)
            box(mesh, cx - r, y, cz - r, cx + r, y + 0.5 + 0.6 * c["intensity"], cz + r, rgba[:3])
            box(mesh, cx - r * 0.6, y + 0.4, cz - r * 0.7, cx + r * 0.7, y + 1.1 + 0.8 * c["intensity"],
                cz + r * 0.5, rgba[:3])
        if mesh["i"]:
            e.add_mesh(mesh["p"], mesh["i"], color=(1, 1, 1, rgba[3]), normals=mesh["n"], colors=mesh["c"],
                       flags=strata.MESH_TRANSLUCENT)


def draw(e, strata, city: dict, cells: dict[int, dict], seed: int = 1) -> None:
    """Wisps (soft points) over drizzle, rain (line streaks) and lightning
    (a jagged line and a flash) over each building with weather; the rain
    and storm clouds themselves are `add_cloud_meshes`. Call between
    begin_frame and end_frame. Deterministic for a seed."""
    rng = random.Random(seed)
    by_id = {b["id"]: b for b in city["buildings"]}
    clouds, flashes, rain, bolts = [], [], [], []
    for bid, c in sorted(cells.items()):
        b = by_id[bid]
        cx, cz = (b["x0"] + b["x1"]) / 2, (b["z0"] + b["z1"]) / 2
        k, kind = c["intensity"], c["kind"]
        base = _cloud_base(b, c)
        r = _spread(kind)
        grey = 0.5
        puffs = 4 if kind in (DRIZZLE, CLEAR) else 0
        for _ in range(puffs):
            clouds.append(strata.Point(
                position=(cx + rng.uniform(-0.9, 0.9), base + rng.uniform(0.0, 0.7), cz + rng.uniform(-0.9, 0.9)),
                size_pixels=10 + 18 * k, world_diameter=1.3 + 1.4 * k,
                color=(grey, grey, grey * 1.06, 0.55 + 0.35 * k), flags=strata.POINT_SOFT))
        if kind != CLEAR:
            drops = int(6 + 70 * k * k) * (4 if kind == STORM else 1) if kind != DRIZZLE else 5
            slant = 0.35 * k if kind == STORM else 0.08
            length = 0.35 + 0.9 * k
            colour = (0.62, 0.74, 0.95, 0.35 + 0.45 * k)
            for _ in range(drops):
                x, z = cx + rng.uniform(-r, r), cz + rng.uniform(-r, r)
                y = rng.uniform(0.3 if kind == STORM else b["h"] + 0.2, base)
                rain.append(strata.LineVertex(position=(x, y, z), color=colour))
                rain.append(strata.LineVertex(position=(x + slant * length, y - length, z), color=colour))
        if c["lightning"]:
            x, y, z = cx, base, cz
            white = (0.92, 0.94, 1.0, 1.0)
            while y > b["h"] + 0.15:
                nx, ny, nz = x + rng.uniform(-0.35, 0.35), max(b["h"] + 0.12, y - rng.uniform(0.3, 0.7)), \
                    z + rng.uniform(-0.35, 0.35)
                bolts.append(strata.LineVertex(position=(x, y, z), color=white))
                bolts.append(strata.LineVertex(position=(nx, ny, nz), color=white))
                x, y, z = nx, ny, nz
            flashes.append(strata.Point(position=(cx, base, cz), size_pixels=40, world_diameter=4.5,
                                        color=(0.8, 0.85, 1.0, 0.55), flags=strata.POINT_SOFT))
    if clouds or flashes:
        e.submit_points(clouds + flashes)
    if rain:
        e.submit_lines(rain)
    if bolts:
        e.submit_lines(bolts)


def lighting_for(lighting, overcast: float):
    """Darken the evening under an overcast sky: the sun sinks toward dusk
    as more of the city is under weather."""
    lighting.time_of_day = 0.735 + 0.035 * overcast
    return lighting


def clear_colour(overcast: float) -> tuple:
    """The still's clear colour, a shade lighter (cloud-lit) as the sky closes in."""
    return (0.02 + 0.02 * overcast, 0.03 + 0.02 * overcast, 0.06 + 0.01 * overcast, 1.0)


def describe_summary(s: Mapping[str, int]) -> str:
    return f"weather: {s['drizzle']} drizzle  {s['rain']} rain  {s['storm']} storm  {s['lightning']} lightning"


def describe(cells: Optional[dict[int, dict]]) -> str:
    return describe_summary(summary(cells or {}))
