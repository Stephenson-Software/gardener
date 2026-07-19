"""Pure, unit-testable logic for `gardener overnight`.

Orchestration — actually dispatching `tend` in-process for each repo,
writing the resume cursor to disk, firing the batch summary notification —
lives in `cli.py`'s `cmd_overnight`, which composes the functions below
with real wall-clock time and a real (or mocked, in tests) dispatch. Kept
separate so the budget/rotation/classification logic can be fully covered
by tests that never invoke `claude`, `git`, or `gh` (see gardener/CLAUDE.md's
testing conventions) — mirrors `garden.py`/`merge_allowlist.py` being the
pure state layer under `cli.py`'s thin `cmd_*` wrappers.

## Design

`gardener overnight` reads the garden (`garden.py`, a separate opt-in list
from the merge allow-list — see `garden.py`'s module docstring) and
dispatches `gardener tend --repo <repo> --allow-merge` in-process for each
one, one after another, until either the garden is exhausted for this run
or the time budget (`--hours`) runs out. Passing `--allow-merge`
unconditionally is intentional and safe: `merge_eligible()` in `cli.py`
(unchanged by this module) still requires the target repo to *also* be on
the separate merge allow-list before `gh pr merge` is ever reachable in the
dispatched session — being in the garden alone never authorizes a merge.

Two things this module deliberately does NOT try to do:

- **Predict real dispatch duration.** `has_time_for_another_repo` is
  called by the orchestration loop after each real dispatch completes,
  using the actual elapsed wall-clock time so far — not a precomputed
  `hours*3600 / TEND_DEFAULT_TIMEOUT_SECONDS` repo count. A night of
  faster-than-worst-case tend calls (79-250s observed in practice per
  README's Project Status, well under the 2700s ceiling) can therefore fit
  more repos than the worst-case arithmetic would suggest, while a night
  that hits the ceiling still can't blow through the budget by more than
  one repo's worth.
- **Guarantee the whole garden gets tended in one run.** If the garden is
  longer than one night's budget allows, `repos_to_attempt` always starts
  from the resume cursor (`read_cursor`/`write_cursor`) rather than the
  top of the list, and `cmd_overnight` advances that cursor by however
  many repos it actually attempted — so the *next* `gardener overnight`
  invocation picks up where this one left off instead of re-tending the
  same first few repos every night and never reaching the rest.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from gardener import notify, state

# A full night's sleep — the use case this subcommand exists for. 8h
# comfortably covers several TEND_DEFAULT_TIMEOUT_SECONDS (45 min)
# worst-case cycles back to back (8h / 45min ~= 10 repos worst case, more
# in practice — see the module docstring above on real observed durations)
# while still being a bounded default rather than "run until the garden is
# empty, how ever long that takes" if `--hours` is omitted.
DEFAULT_OVERNIGHT_HOURS = 8.0


def default_cursor_path() -> Path:
    override = os.environ.get("GARDENER_STATE_DIR")
    base = Path(override) if override else Path.home() / ".local" / "state" / "gardener"
    return base / "overnight_cursor.json"


def read_cursor(path: Optional[Path] = None) -> int:
    """0 (start of the garden list) if no cursor has ever been written, or
    if the file is missing/corrupt — same "absence is a safe, normal
    default" posture as garden.py/merge_allowlist.py, not an error."""
    path = path or default_cursor_path()
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return 0
    idx = data.get("next_index", 0) if isinstance(data, dict) else 0
    return idx if isinstance(idx, int) and idx >= 0 else 0


def write_cursor(index: int, path: Optional[Path] = None) -> None:
    path = path or default_cursor_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"next_index": index}) + "\n")


def batch_repos(order: list[str], concurrency: int) -> list[list[str]]:
    """Split `repos_to_attempt`'s ordered list into consecutive batches of at
    most `concurrency` repos each, preserving order — `cmd_overnight`
    dispatches one batch at a time, concurrently within a batch and
    sequentially across batches. `concurrency <= 1` yields one repo per
    batch, i.e. today's exact sequential behavior; this is the default so
    existing cron invocations see no change unless `--concurrency` is
    explicitly raised. A batch's own wall-clock time is still bounded by one
    repo's `TEND_DEFAULT_TIMEOUT_SECONDS` (everything in a batch runs in
    parallel, not stacked), so `has_time_for_another_repo`'s existing
    headroom check remains correct when called once per batch instead of
    once per repo — see `cli.py`'s `cmd_overnight`."""
    size = max(1, concurrency)
    return [order[i : i + size] for i in range(0, len(order), size)]


def repos_to_attempt(garden: list[str], start_index: int) -> list[str]:
    """The full garden, rotated to start at `start_index` (mod length) —
    round-robins across nights instead of always starting from the top of
    the list, so a garden longer than one night's budget eventually reaches
    every repo rather than only ever tending the first few. Each repo
    appears at most once in the returned order; how many of them a single
    `overnight` invocation actually dispatches is decided separately, in
    real time, by `has_time_for_another_repo` as the caller works through
    this order."""
    if not garden:
        return []
    n = len(garden)
    start = start_index % n
    return [garden[(start + i) % n] for i in range(n)]


def has_time_for_another_repo(
    elapsed_seconds: float,
    budget_seconds: float,
    per_repo_timeout_seconds: int,
    attempted_so_far: int,
) -> bool:
    """Whether `overnight` should start dispatching one more repo this run.

    The first repo of a run is always attempted, as long as any budget was
    given at all (`budget_seconds > 0`) — a run that dispatches zero repos
    just because `--hours` happens to be smaller than one tend call's own
    worst-case timeout would silently do nothing, defeating the point of
    running it (and this is what lets a short `--hours 0.1` smoke-test
    invocation actually dispatch something real for verification). This is
    still bounded regardless by that one dispatch's own subprocess timeout
    (`TEND_DEFAULT_TIMEOUT_SECONDS`) — a short `--hours` can run at most one
    tend call past the requested budget, never unboundedly long.

    Every repo after the first requires enough headroom left in the budget
    for one more worst-case tend call before it's started — "leave enough
    headroom for one more tend call's max timeout" — computed from real
    elapsed time (actual dispatch durations so far), not a precomputed
    worst-case-per-repo plan.
    """
    if budget_seconds <= 0:
        return False
    if attempted_so_far == 0:
        return True
    return elapsed_seconds + per_repo_timeout_seconds <= budget_seconds


# Best-effort matches against the `GARDENER_SUMMARY:`/`DECISION NEEDED:`
# line formats `dev_loop.py`'s `build_tend_prompt` already asks the
# dispatched Claude run to produce (see its docstring) — the same
# "semi-structured but LLM-authored, so match loosely" posture `cli.py`'s
# own `extract_gap_summary` already takes for the same text, not a new
# contract layered on top.
PR_OPENED_RE = re.compile(r"PR #\d+ opened")
PR_MERGED_RE = re.compile(r"PR #\d+ merged")
DECISION_NEEDED_RE = re.compile(r"DECISION NEEDED:")


@dataclass
class RepoOutcome:
    repo: str
    errored: bool = False
    pr_opened: bool = False
    pr_merged: bool = False
    decision_needed: bool = False
    gap_summary: str = ""


def classify_outcome(repo: str, run: Optional[state.Run], result_text: str) -> RepoOutcome:
    """Turns one repo's already-recorded `state.Run` (the same row
    `_dispatch_tend` itself inserted via `state.record_run` — reused here,
    not duplicated) plus the dispatched run's own `result_text` (now passed
    through directly as a `TendResult` field — see `cli.py`'s
    `_dispatch_tend`/`TendResult` — rather than recovered by capturing
    `cmd_tend`'s stdout, which was never safe to do once a repo's dispatch
    could run on a worker thread; `contextlib.redirect_stdout` patches
    `sys.stdout` process-wide, not per-thread) into a batch-summary
    classification for `build_batch_summary`."""
    if run is None or run.outcome == "error":
        return RepoOutcome(
            repo=repo, errored=True, gap_summary=(run.gap_summary if run else "no run recorded")
        )
    text = result_text or ""
    return RepoOutcome(
        repo=repo,
        pr_opened=bool(PR_OPENED_RE.search(text)),
        pr_merged=bool(PR_MERGED_RE.search(text)),
        decision_needed=bool(DECISION_NEEDED_RE.search(text)),
        gap_summary=run.gap_summary or "",
    )


@dataclass
class BatchSummary:
    title: str
    message: str
    level: notify.Level


def build_batch_summary(outcomes: list[RepoOutcome], elapsed_seconds: float, skipped: int) -> BatchSummary:
    """One digest across the whole batch, in addition to (not instead of)
    each repo's own per-repo notification (fired by `cmd_tend` itself via
    `_notify_run`, reused unchanged) — so the operator wakes up to one
    clear summary instead of having to piece N separate messages together
    by hand."""
    attempted = len(outcomes)
    errored = sum(1 for o in outcomes if o.errored)
    pr_opened = sum(1 for o in outcomes if o.pr_opened)
    pr_merged = sum(1 for o in outcomes if o.pr_merged)
    decision_needed = sum(1 for o in outcomes if o.decision_needed)
    minutes = elapsed_seconds / 60

    if attempted == 0:
        message = "no repos were dispatched this run (see gardener's stderr log for why)"
        level = notify.Level.INFO
    else:
        lines = []
        for o in outcomes:
            if o.errored:
                status = "ERROR"
            elif o.pr_merged:
                status = "merged"
            elif o.pr_opened:
                status = "PR opened"
            elif o.decision_needed:
                status = "decision needed"
            else:
                status = "ok, no PR"
            lines.append(f"- {o.repo}: {status}")
        message = (
            f"{attempted} repo(s) attempted in {minutes:.1f}m — "
            f"{pr_opened} PR(s) opened, {pr_merged} merged, "
            f"{decision_needed} awaiting a decision, {errored} errored"
        )
        if skipped:
            message += f", {skipped} not reached this run (resumes next `overnight`)"
        message += "\n" + "\n".join(lines)

        if errored == attempted:
            level = notify.Level.ERROR
        elif errored or decision_needed:
            level = notify.Level.WARNING
        else:
            level = notify.Level.SUCCESS

    title = f"gardener overnight: {attempted} repo(s) tended, {errored} error(s)"
    return BatchSummary(title=title, message=message, level=level)
