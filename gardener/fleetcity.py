"""Fleet City: the repository fleet as a city, on the hub (`/city`).

Stephenson-Software RFC 0026 draws every repository as a building, one
district per owner, with height for activity, colour for health and a
timelapse of gardener's runs lighting the roofs. This module is the hub's
half of it:

* `build_city` joins what the hub holds (every device's runs, the garden
  lists devices pushed, the latest heartbeats) with an optional fleet
  snapshot and lays the city out. `/api/city` serves the result to the page
  (`web/city.html`, drawn by `web/city.{js,wasm}` from `fleetcity/main.cpp`),
  and `render_still` draws the same layout headless for the nightly digest.
* The **fleet snapshot** is optional: a `city.json` written by the
  Fleet City builder (RFC 0026 §1, schema 1), copied by the operator into
  the hub's data directory. It adds the repositories gardener never ran,
  their practice scores, 90-day commit counts and visibility. Without it
  the city is the repositories the hub knows about, measured by gardener's
  own history. Nothing in this repository ships one: it names private
  repositories, which is why `/city` and `/api/city` are behind the hub's
  operator sign-in and why the tests use synthetic names only.

The hub only reads here. Snapshot text is data: names are checked against
`cli.REPO_RE`, and nothing in it but the fields named below is copied.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from gardener import state

WEB_DIR = Path(__file__).resolve().parent / "web"

# URL -> (file in WEB_DIR, content type). Nothing outside this table is
# served, so a crafted path can never reach another file.
ASSETS = {
    "/city": ("city.html", "text/html; charset=utf-8"),
    "/city/": ("city.html", "text/html; charset=utf-8"),
    "/city/city.js": ("city.js", "text/javascript; charset=utf-8"),
    "/city/city.wasm": ("city.wasm", "application/wasm"),
}
#: The page's URLs a signed-out browser is sent to sign in from (the rest
#: get a plain 401).
PAGE_PATHS = ("/city", "/city/")


def is_asset_path(path: str) -> bool:
    return path in ASSETS


def asset(path: str) -> Optional[tuple[str, bytes]]:
    """(content type, body) for one of the page's URLs, or None when the
    path isn't one of them or the file is missing from this install."""
    entry = ASSETS.get(path)
    if entry is None:
        return None
    name, content_type = entry
    try:
        return content_type, (WEB_DIR / name).read_bytes()
    except OSError:
        return None


# ---------------------------------------------------------------- snapshot

SNAPSHOT_NAME = "city.json"
SNAPSHOT_MAX_BYTES = 16 * 1024 * 1024
SNAPSHOT_MAX_REPOS = 5000


class InvalidSnapshot(ValueError):
    pass


def _opt_number(value: object, low: float, high: float) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < low or value > high:
        return None
    return value


def parse_snapshot(doc: object) -> dict:
    """A Fleet City builder's `city.json` (schema 1) reduced to the fields Fleet City
    draws, or `InvalidSnapshot`. Unknown fields are dropped, so nothing the
    builder adds later (or a free-text field) reaches the page unread."""
    from gardener.cli import REPO_RE

    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise InvalidSnapshot("not a schema 1 city.json")
    repos = doc.get("repos")
    if not isinstance(repos, list) or len(repos) > SNAPSHOT_MAX_REPOS:
        raise InvalidSnapshot(f"repos must be a list of at most {SNAPSHOT_MAX_REPOS}")
    out = []
    seen = set()
    for r in repos:
        if not isinstance(r, dict):
            raise InvalidSnapshot("every repo must be an object")
        full = f"{r.get('owner')}/{r.get('name')}"
        if not REPO_RE.match(full) or len(full) > 200:
            raise InvalidSnapshot("a repo has an invalid owner/name")
        if full in seen:
            continue
        seen.add(full)
        created = r.get("createdAt") if isinstance(r.get("createdAt"), str) else None
        out.append({
            "repo": full,
            "private": r.get("private") if isinstance(r.get("private"), bool) else None,
            "fork": r.get("fork") is True,
            "createdAt": created[:32] if created else None,
            "score": _opt_number(r.get("score"), 0, 100),
            "commits90d": _opt_number(r.get("commits90d"), 0, 1e7),
            "daysStale": _opt_number(r.get("daysStale"), 0, 1e6),
        })
    owners = [o for o in (doc.get("owners") or []) if isinstance(o, str)][:50]
    generated = doc.get("generatedAt") if isinstance(doc.get("generatedAt"), str) else None
    return {"generatedAt": generated[:32] if generated else None, "owners": owners, "repos": out}


def load_snapshot(data_dir: Path) -> Optional[dict]:
    """The hub's fleet snapshot, or None when there is none. A snapshot that
    can't be read or parsed is reported on stderr and ignored: the city then
    falls back to the hub's own data rather than failing."""
    path = data_dir / SNAPSHOT_NAME
    try:
        if path.stat().st_size > SNAPSHOT_MAX_BYTES:
            print(f"gardener hub: {path} is over {SNAPSHOT_MAX_BYTES} bytes; ignored", file=sys.stderr)
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"gardener hub: {path} unreadable ({type(exc).__name__}); ignored", file=sys.stderr)
        return None
    try:
        return parse_snapshot(doc)
    except InvalidSnapshot as exc:
        print(f"gardener hub: {path} ignored: {exc}", file=sys.stderr)
        return None


# ------------------------------------------------------------------ layout

PITCH = 1.7        # distance between plot centres
FOOTPRINT = 1.1    # building width and depth
STREET = 4.0       # between districts
MARGIN = 0.8       # inside a district, around the plots
TEXTURE_WIDTH = 32

#: Health codes, as the page and the still colour them.
UNKNOWN, POOR, FAIR, GOOD = 0, 1, 2, 3
#: Linear RGB (Strata's colour space), indexed by health code.
HEALTH_COLOURS = (
    (0.30, 0.30, 0.32),  # unknown: grey
    (0.50, 0.09, 0.06),  # poor: red
    (0.62, 0.40, 0.05),  # fair: amber
    (0.10, 0.42, 0.14),  # good: green
)
#: Event kinds in `events`: a successful run, a run that created the
#: repo's dev-loop skill, and an errored run.
EVENT_OK, EVENT_CREATED, EVENT_ERROR = 0, 1, 2
GLOW_COLOURS = (
    (1.00, 0.82, 0.38),  # ok: warm window light
    (0.35, 0.85, 1.00),  # created: cyan
    (1.00, 0.12, 0.08),  # error: red
)

#: Each night of the timelapse starts at 18:00 UTC (noon in Colorado,
#: before any device's evening start) and runs 24 h.
NIGHT_START_HOUR_UTC = 18
DEFAULT_NIGHTS = 14
MAX_NIGHTS = 60
MAX_EVENTS = 50000


def columns(count: int) -> int:
    """Plots per row: the square root rounded up to a multiple of 4, so it
    changes only when a district crosses a threshold (RFC 0026 §2)."""
    return max(4, int(math.ceil(math.ceil(math.sqrt(max(count, 1))) / 4.0)) * 4)


def height(repo: dict) -> float:
    """Activity, log-scaled. Commits on the default branch in 90 days when
    the snapshot knows them (RFC 0026 §3); else days since the last push,
    inverted; else gardener's runs on it in 90 days."""
    commits = repo.get("commits90d")
    if commits is not None:
        return round(0.35 + 1.1 * math.log2(1 + commits), 4)
    stale = repo.get("daysStale")
    if stale is not None:
        return round(0.35 + max(0.0, 2.0 - math.log10(1 + stale)), 4)
    return round(0.35 + 0.8 * math.log2(1 + repo.get("runs90d", 0)), 4)


def health(repo: dict, colour_by: str, now: datetime) -> int:
    """`score`: the dashboard's bands (>= 80, 50-79, < 50; RFC 0026 §3).
    `gardener`: how recently gardener last succeeded on it (< 3 d good,
    < 10 d fair, older or never poor), for a hub with no snapshot."""
    if colour_by == "score":
        score = repo.get("score")
        if score is None:
            return UNKNOWN
        return GOOD if score >= 80 else FAIR if score >= 50 else POOR
    if not repo.get("runs"):
        return UNKNOWN
    last = state._parse_timestamp(repo.get("last_success"))
    if last is None:
        return POOR
    days = (now - last).total_seconds() / 86400
    return GOOD if days < 3 else FAIR if days < 10 else POOR


def _night_start(when: datetime) -> datetime:
    start = when.replace(hour=NIGHT_START_HOUR_UTC, minute=0, second=0, microsecond=0)
    return start if start <= when else start - timedelta(days=1)


def _hub_runs(db_path: Path) -> list[tuple[str, str, str]]:
    if not db_path.exists():
        return []
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        return conn.execute("SELECT repo, timestamp, outcome FROM runs ORDER BY timestamp").fetchall()


def _event_kind(outcome: str) -> Optional[int]:
    if outcome == state.ERROR_OUTCOME:
        return EVENT_ERROR
    if outcome in (state.CREATED_OUTCOME, state.CREATED_INCOMPLETE_OUTCOME):
        return EVENT_CREATED
    if outcome in state.SUCCESS_OUTCOMES:
        return EVENT_OK
    return None


def _in_flight(live_devices: Iterable[dict]) -> set[str]:
    out = set()
    for d in live_devices or []:
        if d.get("state") not in ("live", "stale"):
            continue
        snap = d.get("snapshot") or {}
        out.update(r for r in snap.get("in_progress") or [] if isinstance(r, str))
    return out


def build_city(
    db_path: Path,
    snapshot: Optional[dict] = None,
    garden_repos: Iterable[str] = (),
    live_devices: Iterable[dict] = (),
    nights: int = DEFAULT_NIGHTS,
    now: Optional[datetime] = None,
) -> dict:
    """The city as one JSON-able dict: districts and buildings already laid
    out, each building's numbers for its card, and the run events of the
    last `nights` nights for the timelapse. Deterministic for the same
    inputs, so the page and the still draw the same city."""
    now = now or datetime.now(timezone.utc)
    nights = max(1, min(int(nights), MAX_NIGHTS))
    newest_night = _night_start(now)
    window_start = newest_night - timedelta(days=nights - 1)
    since_90d = now - timedelta(days=90)

    repos: dict[str, dict] = {}
    order: dict[str, tuple] = {}
    for r in (snapshot or {}).get("repos", []):
        repos[r["repo"]] = dict(r, runs=0, runs90d=0, last_run=None, last_outcome=None, last_success=None)
        order[r["repo"]] = (0, r.get("createdAt") or "", r["repo"])

    def ensure(name: str) -> dict:
        if name not in repos:
            repos[name] = {"repo": name, "private": None, "fork": False, "createdAt": None, "score": None,
                           "commits90d": None, "daysStale": None, "runs": 0, "runs90d": 0, "last_run": None,
                           "last_outcome": None, "last_success": None}
        return repos[name]

    raw_events = []
    for name, timestamp, outcome in _hub_runs(db_path):
        when = state._parse_timestamp(timestamp)
        if when is None or "/" not in (name or ""):
            continue
        r = ensure(name)
        # Plot order without a snapshot: the first run gardener made on it,
        # so a repository new to the hub takes the next plot (§2).
        order.setdefault(name, (1, when.isoformat(), name))
        r["runs"] += 1
        if when >= since_90d:
            r["runs90d"] += 1
        r["last_run"], r["last_outcome"] = timestamp, outcome
        if outcome in state.SUCCESS_OUTCOMES:
            r["last_success"] = timestamp
        kind = _event_kind(outcome)
        if kind is not None and window_start <= when <= now:
            raw_events.append((name, int(when.timestamp()), kind))
    garden = set(garden_repos)
    for name in sorted(garden):
        ensure(name)
        order.setdefault(name, (2, "", name))
    flying = _in_flight(live_devices)

    colour_by = "score" if snapshot else "gardener"
    owner_rank = {o: i for i, o in enumerate((snapshot or {}).get("owners", []))}
    by_owner: dict[str, list[str]] = {}
    for name in sorted(repos, key=lambda n: order[n]):
        by_owner.setdefault(name.split("/", 1)[0], []).append(name)
    owners = sorted(by_owner, key=lambda o: (owner_rank.get(o, len(owner_rank)), -len(by_owner[o]), o))

    # Districts packed into rows, about as wide as the city is deep.
    specs = []
    for owner in owners:
        names = by_owner[owner]
        cols = columns(len(names))
        # Rows are reserved four at a time (vacant lots until filled), so a
        # district that gains a repository rarely pushes its neighbours.
        rows_n = int(math.ceil(len(names) / cols / 4.0)) * 4
        specs.append((owner, names, cols, cols * PITCH + 2 * MARGIN, rows_n * PITCH + 2 * MARGIN))
    area = sum((w + STREET) * (d + STREET) for *_, w, d in specs)
    max_w = max([math.sqrt(area) * 1.2] + [w for *_, w, _ in specs])
    placed, x, z, row_d, width = [], 0.0, 0.0, 0.0, 0.0
    for spec in specs:
        if x > 0 and x + spec[3] > max_w:
            z += row_d + STREET
            x, row_d = 0.0, 0.0
        placed.append((spec, x, z))
        x += spec[3] + STREET
        row_d = max(row_d, spec[4])
        width = max(width, x - STREET)
    depth = z + row_d
    # Anchored at the first district's corner, not centred: centring would
    # move every plot when a new row of districts deepens the city (§2).
    ox, oz = 0.0, 0.0

    districts, buildings, ids = [], [], {}
    for (owner, names, cols, w, d), dx, dz in placed:
        x0, z0 = round(dx + ox, 4), round(dz + oz, 4)
        districts.append({"owner": owner, "count": len(names), "x0": x0, "z0": z0,
                          "x1": round(x0 + w, 4), "z1": round(z0 + d, 4)})
        for k, name in enumerate(names):
            r = repos[name]
            cx = x0 + MARGIN + (k % cols + 0.5) * PITCH
            cz = z0 + MARGIN + (k // cols + 0.5) * PITCH
            half = FOOTPRINT / 2
            ids[name] = len(buildings)
            buildings.append({
                "id": len(buildings), "repo": name, "district": len(districts) - 1,
                "x0": round(cx - half, 4), "z0": round(cz - half, 4),
                "x1": round(cx + half, 4), "z1": round(cz + half, 4),
                "h": height(r), "health": health(r, colour_by, now),
                "private": r["private"], "fork": r["fork"], "score": r["score"],
                "commits90d": r["commits90d"], "daysStale": r["daysStale"],
                "runs": r["runs"], "runs90d": r["runs90d"], "last_run": r["last_run"],
                "last_outcome": r["last_outcome"], "last_success": r["last_success"],
                "in_garden": name in garden, "in_flight": name in flying,
            })
    events = [[ids[n], t, k] for n, t, k in raw_events][-MAX_EVENTS:]
    texture = (TEXTURE_WIDTH, max(1, int(math.ceil(max(1, len(buildings)) / TEXTURE_WIDTH))))
    return {
        "schema": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "now": int(now.timestamp()),
        "sources": {
            "snapshot": (snapshot or {}).get("generatedAt") if snapshot else None,
            "snapshot_repos": len((snapshot or {}).get("repos", [])),
        },
        "colour_by": colour_by,
        "height_by": ("commits" if any(r.get("commits90d") is not None for r in repos.values())
                      else "runs"),
        "texture": list(texture),
        "bounds": [round(ox, 4), round(oz, 4), round(ox + width, 4), round(oz + depth, 4)],
        "districts": districts,
        "buildings": buildings,
        "nights": [int((window_start + timedelta(days=i)).timestamp()) for i in range(nights)],
        "events": events,
    }


def glow_levels(city: dict, at: int, fade_seconds: int = 6 * 3600) -> dict[int, tuple[int, float]]:
    """{building id: (event kind, level 0..1)} at unix time `at`: each run
    lights its roof fully and fades over `fade_seconds`. The page computes
    the same thing in JS (`glowAt`); the still uses this one."""
    out: dict[int, tuple[int, float]] = {}
    for bid, t, kind in city["events"]:
        if t > at or at - t > fade_seconds:
            continue
        level = 1.0 - (at - t) / fade_seconds
        prev = out.get(bid)
        if prev is None or level >= prev[1]:
            out[bid] = (kind, level)
    return out


def palettes(city: dict, glow: dict[int, tuple[int, float]]) -> tuple[bytes, bytes]:
    """The body and roof palettes (RGBA8, one texel per building id)."""
    w, h = city["texture"]
    body, roof = bytearray(w * h * 4), bytearray(w * h * 4)

    def put(buf, i, rgb):
        buf[i * 4:i * 4 + 4] = bytes([max(0, min(255, round(v * 255))) for v in rgb] + [255])

    for b in city["buildings"]:
        rgb = HEALTH_COLOURS[b["health"]]
        put(body, b["id"], rgb)
        dim = tuple(v * 0.35 for v in rgb)
        g = glow.get(b["id"])
        if g:
            k = g[1]
            dim = tuple(d * (1 - k) + c * k for d, c in zip(dim, GLOW_COLOURS[g[0]]))
        put(roof, b["id"], dim)
    return bytes(body), bytes(roof)


# ------------------------------------------------------------------- still

STRATA_LIB_ENV = "GARDENER_STRATA_LIB"
STRATA_PYTHON_ENV = "GARDENER_STRATA_PYTHON"
#: The libstrata release the still is drawn with, and its SHA-256 (from the
#: release's SHA256SUMS). `fleetcity/fetch-strata-python.sh` fetches it and
#: the Python binding; the still refuses any other file.
STRATA_RELEASE = "0.6.0"
STRATA_LIB_SHA256 = "3815068f90afa67b42177359a7ea918a5de321d7f7bad8706fa5275c4906af2a"


class StillUnavailable(RuntimeError):
    pass


def _load_strata(lib_path: Optional[str], binding_dir: Optional[str]):
    import hashlib
    import importlib

    lib_path = lib_path or os.environ.get(STRATA_LIB_ENV)
    binding_dir = binding_dir or os.environ.get(STRATA_PYTHON_ENV)
    if not lib_path or not binding_dir:
        raise StillUnavailable(
            f"the still needs Strata's Python binding and library: set {STRATA_PYTHON_ENV} and "
            f"{STRATA_LIB_ENV} (fleetcity/fetch-strata-python.sh fetches both)")
    try:
        digest = hashlib.sha256(Path(lib_path).read_bytes()).hexdigest()
    except OSError as exc:
        raise StillUnavailable(f"can't read {lib_path}: {exc}") from exc
    if digest != STRATA_LIB_SHA256:
        raise StillUnavailable(f"{lib_path} is not libstrata {STRATA_RELEASE} (sha256 {digest[:12]}…)")
    # Software rendering (Mesa llvmpipe) unless the caller chose otherwise:
    # a server has no GPU, and under WSL the d3d12 driver Mesa would pick
    # crashes strata_create in a headless EGL context (seen 2026-10-27).
    os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
    os.environ.setdefault("GALLIUM_DRIVER", "llvmpipe")
    if binding_dir not in sys.path:
        sys.path.insert(0, binding_dir)
    try:
        strata = importlib.import_module("strata")
    except ImportError as exc:
        raise StillUnavailable(f"no strata binding in {binding_dir}: {exc}") from exc
    if getattr(strata, "__version__", None) != STRATA_RELEASE:
        raise StillUnavailable(f"the strata binding is {getattr(strata, '__version__', '?')}, not {STRATA_RELEASE}")
    return strata, strata.load(lib_path)


def _box(mesh: dict, x0, y0, z0, x1, y1, z1, rgb, u=0.0, v=0.0) -> None:
    """main.cpp's `box` without the bottom face: the same winding and uvs."""
    faces = (
        ((x0, y1, z1), (x1, y1, z1), (x1, y1, z0), (x0, y1, z0), (0, 1, 0)),
        ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1), (0, 0, 1)),
        ((x1, y0, z0), (x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (0, 0, -1)),
        ((x1, y0, z1), (x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (1, 0, 0)),
        ((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0), (-1, 0, 0)),
    )
    for *corners, n in faces:
        base = len(mesh["p"]) // 3
        for p in corners:
            mesh["p"].extend(p)
            mesh["n"].extend(n)
            mesh["c"].extend(rgb)
            mesh["uv"].extend((u, v))
        mesh["i"].extend((base, base + 1, base + 2, base, base + 2, base + 3))


def _ascii(text: str) -> bytes:
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text).encode("ascii")


def render_still(
    city: dict,
    out: Path,
    width: int = 1280,
    height: int = 720,
    hours: float = 24,
    lib_path: Optional[str] = None,
    binding_dir: Optional[str] = None,
) -> dict:
    """Draws the city headless (EGL, no display) at dusk with every roof a
    run lit in the last `hours`, and saves it as a PNG. Labels name the
    districts (owners) only, never a repository. Returns a small summary
    for the caption."""
    strata, lib = _load_strata(lib_path, binding_dir)
    at = city["now"]
    glow = glow_levels(city, at, fade_seconds=int(hours * 3600))
    body_px, roof_px = palettes(city, glow)
    tw, th = city["texture"]
    x0, z0, x1, z1 = city["bounds"]
    span = max(x1 - x0, z1 - z0, 10.0)
    with strata.Engine(lib, width, height) as e:
        flags = strata.TEXTURE_NEAREST | strata.TEXTURE_CLAMP_U | strata.TEXTURE_CLAMP_V
        body_tex = e.create_texture_rgba8(tw, th, body_px, flags)
        roof_tex = e.create_texture_rgba8(tw, th, roof_px, flags)
        ground = {"p": [], "n": [], "c": [], "uv": [], "i": []}
        _box(ground, x0 - 6, -0.2, z0 - 6, x1 + 6, 0.0, z1 + 6, (0.045, 0.047, 0.052))
        for d in city["districts"]:
            _box(ground, d["x0"], 0.0, d["z0"], d["x1"], 0.04, d["z1"], (0.16, 0.16, 0.15))
        e.add_mesh(ground["p"], ground["i"], normals=ground["n"], colors=ground["c"], uvs=ground["uv"])
        for di in range(len(city["districts"])):
            bodies = {"p": [], "n": [], "c": [], "uv": [], "i": []}
            roofs = {"p": [], "n": [], "c": [], "uv": [], "i": []}
            for b in city["buildings"]:
                if b["district"] != di:
                    continue
                u, v = (b["id"] % tw + 0.5) / tw, (b["id"] // tw + 0.5) / th
                _box(bodies, b["x0"], 0.04, b["z0"], b["x1"], 0.04 + b["h"], b["z1"], (1, 1, 1), u, v)
                _box(roofs, b["x0"] + 0.06, 0.04 + b["h"], b["z0"] + 0.06, b["x1"] - 0.06, 0.12 + b["h"],
                     b["z1"] - 0.06, (1, 1, 1), u, v)
            if bodies["i"]:
                e.add_mesh(bodies["p"], bodies["i"], normals=bodies["n"], colors=bodies["c"], uvs=bodies["uv"],
                           texture=body_tex)
                e.add_mesh(roofs["p"], roofs["i"], normals=roofs["n"], colors=roofs["c"], uvs=roofs["uv"],
                           texture=roof_tex, flags=strata.MESH_UNLIT)
        yaw, pitch, dist = 0.8, 0.66, span * 1.3
        centre = ((x0 + x1) / 2, 0.0, (z0 + z1) / 2)
        eye = (centre[0] + dist * math.cos(pitch) * math.cos(yaw), dist * math.sin(pitch),
               centre[2] + dist * math.cos(pitch) * math.sin(yaw))
        e.set_camera(lib.look_at(eye, centre, fov_y_degrees=45, aspect=width / height, near_plane=0.5,
                                 far_plane=max(600.0, dist * 4)))
        lighting = lib.lighting_defaults(strata.LIGHTING_SUN)
        lighting.time_of_day = 0.735  # low evening sun: long shadows, lit roofs still read
        lighting.cascade_count = 2
        lighting.shadow_map_size = 2048
        lighting.shadow_distance = dist * 2.5
        lighting.fog = 1
        e.set_lighting(lighting)
        lit = {"ok": 0, "created": 0, "error": 0}
        for kind, _ in glow.values():
            lit[("ok", "created", "error")[kind]] += 1
        e.begin_frame(clear_color=(0.02, 0.03, 0.06, 1), clear_flags=strata.CLEAR_COLOR | strata.CLEAR_DEPTH
                      | strata.CLEAR_SKY)
        # A soft glow over every lit roof, so a night's runs read at a glance.
        by_id = {b["id"]: b for b in city["buildings"]}
        glows = []
        for bid, (kind, level) in glow.items():
            b = by_id[bid]
            glows.append(strata.Point(
                position=((b["x0"] + b["x1"]) / 2, 0.5 + b["h"], (b["z0"] + b["z1"]) / 2),
                size_pixels=6 + 10 * level, world_diameter=1.6, color=(*GLOW_COLOURS[kind], 0.35 + 0.6 * level),
                flags=strata.POINT_SOFT))
        if glows:
            e.submit_points(glows)
        texts = []
        keep = []
        vp = _view_proj(lib, eye, centre, width, height, dist)
        for d in city["districts"]:
            sx = _project(vp, ((d["x0"] + d["x1"]) / 2, 0.5, d["z1"] + 0.8), width, height)
            if sx is None:
                continue
            label = _ascii(f"{d['owner']} ({d['count']})")
            keep.append(label)
            texts.append(strata.Text(x=min(max(8, sx[0] - len(label) * 8), width - 8 - len(label) * 16), y=sx[1], pixel_size=16, flags=strata.TEXT_SHADOW,
                                     color=(1, 1, 1, 0.95), utf8=label))
        caption = _ascii(
            f"Fleet City  {city['generated_at'][:10]}  {len(city['buildings'])} repos  last {hours:g} h: "
            f"{lit['ok']} tended  {lit['created']} created  {lit['error']} errored")
        legend = _ascii(
            "height: " + ("commits, 90 days" if city["height_by"] == "commits" else "gardener runs, 90 days")
            + "   colour: " + ("dashboard score" if city["colour_by"] == "score" else "last gardener success")
            + "   roofs: amber tended, cyan created, red errored")
        keep += [caption, legend]
        texts.append(strata.Text(x=16, y=16, pixel_size=16, flags=strata.TEXT_SHADOW, color=(1, 1, 1, 1),
                                 utf8=caption))
        texts.append(strata.Text(x=16, y=height - 28, pixel_size=8 if width < 1600 else 16,
                                 flags=strata.TEXT_SHADOW, color=(0.85, 0.88, 0.95, 1), utf8=legend))
        e.submit_text(texts)
        e.end_frame()
        out.parent.mkdir(parents=True, exist_ok=True)
        e.save_screenshot_png(out)
        gpu = e.gpu_description()
    return {"repos": len(city["buildings"]), "lit": lit, "gpu": gpu, "path": str(out)}


def _view_proj(lib, eye, centre, width, height, dist):
    cam = lib.look_at(eye, centre, fov_y_degrees=45, aspect=width / height, near_plane=0.5,
                      far_plane=max(600.0, dist * 4))
    p, v = list(cam.projection), list(cam.view)
    return [sum(p[k * 4 + r] * v[c * 4 + k] for k in range(4)) for c in range(4) for r in range(4)]


def _project(m, point, width, height):
    x, y, z = point
    cx = m[0] * x + m[4] * y + m[8] * z + m[12]
    cy = m[1] * x + m[5] * y + m[9] * z + m[13]
    cw = m[3] * x + m[7] * y + m[11] * z + m[15]
    if cw <= 0.01:
        return None
    return ((cx / cw * 0.5 + 0.5) * width, (1 - (cy / cw * 0.5 + 0.5)) * height)
