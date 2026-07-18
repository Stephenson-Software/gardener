"""Local, gardener-level allow-list of repos `gardener tend --allow-merge`
is permitted to actually merge PRs in.

A JSON file next to gardener's other cached/persisted state
(`~/.local/state/gardener/merge_allowlist.json` by default, same directory
`state.py` uses, overridable the same way via `GARDENER_STATE_DIR`) —
deliberately not SQLite: this is a short, hand-editable list an operator
adds to occasionally, not a growing log of runs, so plain JSON is the
simpler fit. A missing file means an empty allow-list, not an error — the
safe default (`gardener tend` with no allowlist configured yet must never
be treated as "everything is allowed").

`gardener tend --allow-merge --repo X` only ever adds `Bash(gh pr merge *)`
to the dispatched session's `--allowedTools` when X is present here AND
`--allow-merge` was passed — see `dispatch.py`'s `tend_mode_spec()` and its
module docstring's "Merge allow-list" section for the structural
enforcement. This module only reads/writes the list; it makes no
enforcement decision itself.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def default_allowlist_path() -> Path:
    override = os.environ.get("GARDENER_STATE_DIR")
    base = Path(override) if override else Path.home() / ".local" / "state" / "gardener"
    return base / "merge_allowlist.json"


def _load(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"merge allow-list at {path} is not valid JSON: {e}") from e
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError(f"merge allow-list at {path} must be a JSON array of strings")
    return data


def _save(path: Path, repos: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Sorted + deduped on every write so the file stays diff-friendly and
    # hand-editable without gardener re-adding an accidental duplicate.
    path.write_text(json.dumps(sorted(set(repos)), indent=2) + "\n")


def list_allowed(path: Path | None = None) -> list[str]:
    return sorted(_load(path or default_allowlist_path()))


def is_allowed(repo: str, path: Path | None = None) -> bool:
    return repo in _load(path or default_allowlist_path())


def add(repo: str, path: Path | None = None) -> bool:
    """Returns True if `repo` was newly added, False if it was already present."""
    path = path or default_allowlist_path()
    repos = _load(path)
    if repo in repos:
        return False
    repos.append(repo)
    _save(path, repos)
    return True


def remove(repo: str, path: Path | None = None) -> bool:
    """Returns True if `repo` was present and removed, False if it wasn't there."""
    path = path or default_allowlist_path()
    repos = _load(path)
    if repo not in repos:
        return False
    repos.remove(repo)
    _save(path, repos)
    return True
