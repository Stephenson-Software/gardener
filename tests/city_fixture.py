"""A synthetic fleet for Fleet City's tests and screenshots.

Every name here is made up (`acme`, `globex`, ... with tree and stone
words), so nothing private can reach a fixture, a test log or a picture
made from it. Deterministic: the same seed builds the same fleet.

    python tests/city_fixture.py <hub data dir> [--repos N] [--now]

writes a hub store and a `city.json` snapshot there, for a local hub to
serve (docs/HUB.md, "Fleet City").
"""
from __future__ import annotations

import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

OWNERS = ["acme", "globex", "initech", "umbrella", "hooli"]
WORDS = ["birch", "onyx", "maple", "slate", "cedar", "quartz", "willow", "basalt", "aspen", "flint",
         "rowan", "jasper", "alder", "marble", "hazel", "garnet"]
NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)


def fleet(count: int = 120, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    weights = [0.42, 0.22, 0.17, 0.11, 0.08]
    out = []
    for i in range(count):
        owner = rng.choices(OWNERS, weights)[0]
        out.append({
            "owner": owner, "name": f"{rng.choice(WORDS)}-{i:03d}",
            "private": rng.random() < 0.6, "fork": rng.random() < 0.05,
            "createdAt": (NOW - timedelta(days=3000 - i * 20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "score": rng.choice([None, 35, 55, 70, 85, 95]),
            "commits90d": rng.choice([0, 0, 1, 3, 8, 20, 60, None]),
            "daysStale": rng.randint(0, 900),
        })
    return out


def snapshot(repos: list[dict]) -> dict:
    return {"schema": 1, "generatedAt": "2026-10-26T00:00:00Z", "owners": OWNERS,
            "repos": [dict(r, id=i, full=f"{r['owner']}/{r['name']}", runs=[]) for i, r in enumerate(repos)]}


def runs(repos: list[dict], nights: int = 14, seed: int = 7, now: datetime = NOW):
    """Runs over the last `nights` evenings on a third of the fleet."""
    from gardener import state

    rng = random.Random(seed)
    garden = [f"{r['owner']}/{r['name']}" for r in repos[::3]]
    out = []
    for n in range(nights):
        evening = (now - timedelta(days=n)).replace(hour=1, minute=0)  # 19:00 the evening before, in Colorado
        for repo in rng.sample(garden, min(len(garden), 12)):
            t = evening + timedelta(minutes=rng.randint(0, 9 * 60))
            outcome = rng.choices(["tend", "error", "created"], [0.82, 0.14, 0.04])[0]
            out.append(state.Run(repo=repo, mode="tend", outcome=outcome, timestamp=t.isoformat(),
                                 run_uuid=str(uuid.UUID(int=rng.getrandbits(128))),
                                 device=rng.choice(["box", "phone"])))
    return garden, out


def write_hub(data_dir: Path, count: int = 120, now: datetime = NOW) -> list[str]:
    from gardener import hub, state

    repos = fleet(count)
    hub.prepare_store(data_dir)
    garden, rows = runs(repos, now=now)
    state.insert_runs(rows, db_path=data_dir / "gardener.sqlite3")
    hub.store_device_lists(data_dir / "gardener.sqlite3", "box", {"garden": garden})
    (data_dir / "city.json").write_text(json.dumps(snapshot(repos)), encoding="utf-8")
    return garden


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    target = Path(sys.argv[1])
    n = int(sys.argv[sys.argv.index("--repos") + 1]) if "--repos" in sys.argv else 120
    # --now: runs on the evenings up to now (for a live-looking local page).
    when = datetime.now(timezone.utc) if "--now" in sys.argv else NOW
    write_hub(target, n, now=when)
    print(f"wrote a synthetic fleet of {n} repos to {target}")
