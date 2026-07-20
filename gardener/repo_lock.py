"""Cross-process, per-repo exclusion so two gardener invocations never run
git operations against the same target repo's shared clone directory
(`~/.cache/gardener/repos/<owner>__<repo>`, see `cli.py`'s
`default_repos_cache_dir`/`clone_or_refresh_target_repo`) at the same time.

Why this exists: gardener has no coordination between separate processes —
`overnight` dispatches concurrently *within* one process via a
`ThreadPoolExecutor` (safe today: each repo in a batch is distinct, so each
gets its own clone directory), but nothing stops a second, independent
`gardener align`/`gardener tend`/`gardener overnight` invocation (run by
hand, or two overlapping `overnight` runs) from targeting a repo another
process is already mid-dispatch on. Two processes cloning/checking out/
`git clean -fdx`-ing the same working tree concurrently is exactly the
failure mode that has corrupted `.git/objects` in this ecosystem before —
see `~/a-private-repo-2/CLAUDE.md`'s documented `git worktree add`/`remove`
corruption incident. That was a different mechanism (concurrent worktree
add/remove), but the lesson is the same: concurrent git operations against
one shared directory are not safe to assume away.

Uses `fcntl.flock` (stdlib, matching gardener's stdlib-only rule — see
gardener/CLAUDE.md) on a per-repo lock file, non-blocking. Non-blocking is
deliberate: a stuck lock must never turn into `overnight` silently blocking
past its own per-repo timeout budget waiting for one to free up. A locked
repo is treated as a skip-this-run condition (see `RepoLockedError`'s
callers in `cli.py`), not a wait.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
from pathlib import Path
from typing import Iterator, Optional


class RepoLockedError(Exception):
    """Raised when another gardener process already holds `repo`'s lock."""

    def __init__(self, repo: str):
        super().__init__(
            f"{repo} is already being worked on by another gardener process "
            "(its per-repo lock is held) — skipping rather than risking "
            "concurrent git operations against the same clone"
        )
        self.repo = repo


def _default_state_dir() -> Path:
    # Same override and default as notify.py/overnight.py/state.py's own
    # `_default_state_dir` — kept as a separate copy rather than a shared
    # import, matching how those modules already each define it themselves.
    override = os.environ.get("GARDENER_STATE_DIR")
    return Path(override) if override else Path.home() / ".local" / "state" / "gardener"


def lock_file_path(repo: str, state_dir: Optional[Path] = None) -> Path:
    base = state_dir or _default_state_dir()
    # Same `owner/repo` -> `owner__repo` mapping as the clone-cache dir name
    # (`cli.py`'s `default_repos_cache_dir`) — not load-bearing (this is a
    # separate directory), just a familiar, collision-free naming choice.
    return base / "locks" / f"{repo.replace('/', '__')}.lock"


@contextlib.contextmanager
def repo_lock(repo: str, state_dir: Optional[Path] = None) -> Iterator[None]:
    """Hold an exclusive, non-blocking lock on `repo` for the wrapped block.
    Raises `RepoLockedError` immediately (never blocks/waits) if another
    process already holds it. The lock file itself is never removed — an
    empty, unlocked lock file lying around is not a problem, matching the
    rest of gardener's state files (garden.json, merge_allowlist.json, ...)
    which are all fine to exist with "nothing interesting in them" as their
    normal resting state.
    """
    path = lock_file_path(repo, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RepoLockedError(repo) from e
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
