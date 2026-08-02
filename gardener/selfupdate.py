"""Self-update: keeps gardener's *own* checkout current with `origin`, for
the box that runs `gardener overnight` unattended (Task Scheduler, cron,
`devsrv`) with nobody watching to notice new commits landed upstream and
run `git pull` by hand.

This is deliberately narrow and conservative — it only ever fast-forwards
gardener's own repo checkout to `origin/<current-branch>`, never touches a
target repo or the conventions repo (those are `cli.py`'s
`clone_or_refresh_target_repo`/`conventions.py`'s `ensure_conventions`, an
entirely separate concern), and never does anything that could lose local
work: a dirty tree, a detached `HEAD`, or a local branch that isn't a
fast-forward ancestor of `origin` all degrade to a skip, never a `reset
--hard` or a forced merge. `git status --porcelain`'s untracked (`??`)
lines don't count as dirty — an ordinary checkout of this repo picks up
untracked local files (an editor's `.claude/` scratch dir, `__pycache__`,
etc.) that have nothing to do with whether a fast-forward is safe.

`find_repo_root` locates gardener's own checkout by walking up from this
module's file looking for a `.git` — this only resolves to something real
for an **editable** install (`pip install -e .`, `Editable project
location` in `pip show gardener` pointing at a real clone); a wheel/sdist
install copies `selfupdate.py` into `site-packages` with no `.git`
anywhere above it, so self-update correctly reports
`SKIPPED_NO_GIT` rather than guessing at some other directory.

Every `git` call goes through the injectable `run_fn` (mirrors
`overnight.py`'s injectable `time_fn`/`sleep_fn` pattern) so
`self_update`'s branching is fully unit-testable without a real git
checkout or network access — see `tests/test_selfupdate.py`.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

RunFn = Callable[[list[str], Path, int], subprocess.CompletedProcess]


class UpdateStatus(str, Enum):
    UP_TO_DATE = "up_to_date"
    UPDATE_AVAILABLE = "update_available"
    UPDATED = "updated"
    SKIPPED_NO_GIT = "skipped_no_git"
    SKIPPED_DIRTY = "skipped_dirty"
    SKIPPED_DETACHED = "skipped_detached"
    SKIPPED_NOT_FAST_FORWARD = "skipped_not_fast_forward"
    ERROR = "error"


@dataclass
class UpdateResult:
    status: UpdateStatus
    message: str
    old_sha: Optional[str] = None
    new_sha: Optional[str] = None


def _default_run(argv: list[str], cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def find_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from `start` (default: this module's own file) looking for a
    directory containing `.git`. Returns None if none is found before
    reaching the filesystem root — the "installed as a package copy, not a
    git checkout" case `self_update` treats as `SKIPPED_NO_GIT`."""
    current = (start or Path(__file__).resolve().parent).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _call(
    run_fn: RunFn, argv: list[str], root: Path, timeout: int
) -> tuple[Optional[subprocess.CompletedProcess], Optional[str]]:
    try:
        return run_fn(argv, root, timeout), None
    except subprocess.TimeoutExpired:
        return None, f"`{' '.join(argv)}` timed out after {timeout}s"
    except (subprocess.SubprocessError, OSError) as e:
        return None, f"`{' '.join(argv)}` failed to run: {e}"


def self_update(
    repo_root: Optional[Path] = None,
    check_only: bool = False,
    timeout: int = 30,
    run_fn: RunFn = _default_run,
) -> UpdateResult:
    """Fast-forward gardener's own checkout to `origin/<current-branch>` if
    it's safe to. Never raises — every failure mode (not a git checkout,
    dirty tree, detached HEAD, diverged branch, a `git` call itself
    failing) is a distinct `UpdateStatus`, so a caller like `cmd_overnight`
    can log it and keep going rather than let a self-update hiccup abort
    the unattended run it's meant to run ahead of.

    `check_only=True` stops right before the actual fast-forward (used by
    `gardener update --check`) and reports `UPDATE_AVAILABLE` instead of
    performing it.
    """
    root = repo_root or find_repo_root()
    if root is None:
        return UpdateResult(
            UpdateStatus.SKIPPED_NO_GIT,
            "gardener is not running from a git checkout (installed as a "
            "package copy, not editable) — self-update unavailable",
        )

    status_res, err = _call(run_fn, ["git", "status", "--porcelain"], root, timeout)
    if err:
        return UpdateResult(UpdateStatus.ERROR, err)
    if status_res.returncode != 0:
        return UpdateResult(
            UpdateStatus.ERROR, f"git status failed: {status_res.stderr.strip()}"
        )
    dirty = any(
        line and not line.startswith("??") for line in status_res.stdout.splitlines()
    )
    if dirty:
        return UpdateResult(
            UpdateStatus.SKIPPED_DIRTY,
            f"{root} has uncommitted local changes — skipping self-update",
        )

    branch_res, err = _call(
        run_fn, ["git", "rev-parse", "--abbrev-ref", "HEAD"], root, timeout
    )
    if err:
        return UpdateResult(UpdateStatus.ERROR, err)
    branch = branch_res.stdout.strip()
    if branch_res.returncode != 0 or not branch or branch == "HEAD":
        return UpdateResult(
            UpdateStatus.SKIPPED_DETACHED,
            f"{root} is on a detached HEAD — skipping self-update",
        )

    old_sha_res, err = _call(run_fn, ["git", "rev-parse", "HEAD"], root, timeout)
    if err:
        return UpdateResult(UpdateStatus.ERROR, err)
    old_sha = old_sha_res.stdout.strip()

    fetch_res, err = _call(
        run_fn, ["git", "fetch", "--quiet", "origin", branch], root, timeout
    )
    if err:
        return UpdateResult(UpdateStatus.ERROR, err)
    if fetch_res.returncode != 0:
        return UpdateResult(
            UpdateStatus.ERROR, f"git fetch failed: {fetch_res.stderr.strip()}"
        )

    remote_ref = f"origin/{branch}"
    new_sha_res, err = _call(run_fn, ["git", "rev-parse", remote_ref], root, timeout)
    if err:
        return UpdateResult(UpdateStatus.ERROR, err)
    if new_sha_res.returncode != 0:
        return UpdateResult(
            UpdateStatus.ERROR,
            f"could not resolve {remote_ref}: {new_sha_res.stderr.strip()}",
        )
    new_sha = new_sha_res.stdout.strip()

    if new_sha == old_sha:
        return UpdateResult(
            UpdateStatus.UP_TO_DATE, f"already up to date ({old_sha[:9]})", old_sha, new_sha
        )

    ancestor_res, err = _call(
        run_fn, ["git", "merge-base", "--is-ancestor", "HEAD", remote_ref], root, timeout
    )
    if err:
        return UpdateResult(UpdateStatus.ERROR, err)
    if ancestor_res.returncode != 0:
        return UpdateResult(
            UpdateStatus.SKIPPED_NOT_FAST_FORWARD,
            f"{root}'s HEAD ({old_sha[:9]}) has diverged from {remote_ref} "
            f"({new_sha[:9]}) — not a fast-forward, skipping self-update",
            old_sha,
            new_sha,
        )

    if check_only:
        return UpdateResult(
            UpdateStatus.UPDATE_AVAILABLE,
            f"update available: {old_sha[:9]} -> {new_sha[:9]}",
            old_sha,
            new_sha,
        )

    merge_res, err = _call(
        run_fn, ["git", "merge", "--ff-only", remote_ref], root, timeout
    )
    if err:
        return UpdateResult(UpdateStatus.ERROR, err)
    if merge_res.returncode != 0:
        return UpdateResult(
            UpdateStatus.ERROR, f"git merge --ff-only failed: {merge_res.stderr.strip()}"
        )

    return UpdateResult(
        UpdateStatus.UPDATED,
        f"updated {old_sha[:9]} -> {new_sha[:9]}",
        old_sha,
        new_sha,
    )
