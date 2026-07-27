"""Local, gardener-level opt-in list of repos `gardener overnight` is
permitted to tend while nobody is watching.

Mirrors `merge_allowlist.py`'s design exactly: a JSON file next to
gardener's other cached/persisted state (`~/.local/state/gardener/
garden.json` by default, same directory `merge_allowlist.py`/`state.py`
use, overridable the same way via `GARDENER_STATE_DIR`) — a short,
hand-editable list an operator adds to occasionally, not a growing log, so
plain JSON is the simpler fit over sqlite. A missing file means an empty
garden, not an error — the safe default: a repo is never touched by
`gardener overnight` just because it exists somewhere on this machine,
only because it was explicitly added here first (`gardener garden add`).

This is a second, independent opt-in gate from the merge allow-list
(`merge_allowlist.py`) — being in the garden only makes a repo eligible
for `gardener overnight` to dispatch `tend --allow-merge` against it;
whether that dispatch is ever actually *permitted to merge* anything still
depends entirely on the separate merge allow-list, completely unaffected
by this file. Both are opt-in, and both must hold independently for a
merge to ever happen — see `overnight.py`'s module docstring and
`cli.py`'s `merge_eligible()`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def default_garden_path() -> Path:
    override = os.environ.get("GARDENER_STATE_DIR")
    base = Path(override) if override else Path.home() / ".local" / "state" / "gardener"
    return base / "garden.json"


def _load(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"garden list at {path} is not valid JSON: {e}") from e
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError(f"garden list at {path} must be a JSON array of strings")
    return data


def _save(path: Path, repos: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sorted + deduped on every write so the file stays diff-friendly and
    # hand-editable without gardener re-adding an accidental duplicate —
    # same reasoning as merge_allowlist.py's _save. `overnight.py`'s
    # round-robin rotation relies on this sorted order being stable across
    # reads for its resume cursor to mean the same thing from one
    # `gardener overnight` invocation to the next.
    #
    # Written atomically (temp file + os.replace) rather than a direct
    # write_text, which truncates-then-writes in place: a process killed
    # mid-write (this device can and does kill background processes
    # without warning — see docs/OVERNIGHT.md's "Wiring it to 'tend to my
    # garden while I sleep'" section) would otherwise leave a torn, invalid-JSON
    # file for the next `gardener overnight` to trip over. os.replace is
    # atomic on the same filesystem, which the temp file always is since
    # it's a sibling of `path`.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(sorted(set(repos)), indent=2) + "\n")
    os.replace(tmp_path, path)


def list_garden(path: Path | None = None) -> list[str]:
    return sorted(_load(path or default_garden_path()))


def is_in_garden(repo: str, path: Path | None = None) -> bool:
    return repo in _load(path or default_garden_path())


def add(repo: str, path: Path | None = None) -> bool:
    """Returns True if `repo` was newly added, False if it was already present."""
    path = path or default_garden_path()
    repos = _load(path)
    if repo in repos:
        return False
    repos.append(repo)
    _save(path, repos)
    return True


def remove(repo: str, path: Path | None = None) -> bool:
    """Returns True if `repo` was present and removed, False if it wasn't there."""
    path = path or default_garden_path()
    repos = _load(path)
    if repo not in repos:
        return False
    repos.remove(repo)
    _save(path, repos)
    return True
