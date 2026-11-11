"""Synthetic trace usage for Fleet City's weather (RFC 0032 spike).

Every repository name comes from city_fixture's made-up fleet, so nothing
private reaches a sample or a still. `flood_series` is shaped like the
MedievalFactions /power flood of 2026-10-23 (one server sending the same
command about ten times a second): an ordinary trickle, then one repository
at ~600 events a minute for 40 minutes, tagged `flood` by trace's
FloodGuard above 60 a minute, then back to a trickle.

    python tests/weather_fixture.py <out dir>

draws one still per named state (clear, showers, busy, flood, lightning).
"""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

WINDOW = 300  # seconds per sample


def _names(city: dict) -> list[str]:
    return [b["repo"] for b in city["buildings"]]


def flood_series(city: dict, minutes: int = 120, seed: int = 11) -> list[dict]:
    """One sample per WINDOW over `minutes`; the flood runs from minute 40
    to minute 80 on one repository."""
    rng = random.Random(seed)
    names = _names(city)
    busy = rng.sample(names, max(1, len(names) // 6))
    target = names[len(names) // 3]
    out = []
    for start in range(0, minutes * 60, WINDOW):
        repos = {}
        for n in busy:
            repos[n] = {"events": rng.choice([0, 1, 2, 4, 9, 30]), "flood": 0, "errors": 0}
        if 40 * 60 <= start < 80 * 60:
            per_window = 600 * WINDOW // 60
            # FloodGuard: the first 60 in each minute are stored untagged.
            repos[target] = {"events": 60 * WINDOW // 60, "flood": per_window - 60 * WINDOW // 60, "errors": 0}
        out.append({"t": start, "window_seconds": WINDOW, "repos": repos, "flooding": target})
    return out


def states(city: dict, seed: int = 5) -> dict[str, dict]:
    """The named states a still is drawn for."""
    rng = random.Random(seed)
    names = _names(city)
    series = flood_series(city)
    peak = max(series, key=lambda s: sum(r["flood"] for r in s["repos"].values()))
    showers = {n: {"events": rng.choice([1, 2, 3, 6])} for n in rng.sample(names, len(names) // 10)}
    busy = {n: {"events": rng.choice([3, 12, 40, 150, 290])} for n in rng.sample(names, len(names) // 3)}
    lightning = dict(showers)
    for n in rng.sample(sorted(showers), 3):
        lightning[n] = dict(lightning[n], errors=2)
    return {
        "clear": {"window_seconds": WINDOW, "repos": {}},
        "showers": {"window_seconds": WINDOW, "repos": showers},
        "busy": {"window_seconds": WINDOW, "repos": busy},
        "flood": {"window_seconds": WINDOW, "repos": peak["repos"]},
        "lightning": {"window_seconds": WINDOW, "repos": lightning},
    }


def main(out_dir: Path) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import city_fixture
    from gardener import fleetcity, hub

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "hub"
        city_fixture.write_hub(data_dir, 120)
        db = data_dir / "gardener.sqlite3"
        city = fleetcity.build_city(db, snapshot=fleetcity.load_snapshot(data_dir),
                                    garden_repos=hub.union_device_lists(db)["garden"], now=city_fixture.NOW)
    (out_dir / "flood-series.json").write_text(json.dumps(flood_series(city), indent=1))
    for name, sample in states(city).items():
        (out_dir / f"{name}.json").write_text(json.dumps(sample, indent=1))
        result = fleetcity.render_still(city, out_dir / f"weather-{name}.png", weather=sample, hours=24)
        print(name, result["weather"], result["gpu"])
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
