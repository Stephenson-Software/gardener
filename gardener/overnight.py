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

## Repo-selection strategies and the resume cursor (issue #6, #14)

`--strategy` (see `cli.py`'s `overnight_parser`) selects which of three
pluggable `garden -> ordered list[str]`-shaped functions below decides this
run's attempt order:

- `round-robin` (`repos_to_attempt`, default — byte-for-byte the original,
  only implementation, unchanged): rotates the alphabetically-sorted garden
  to start at the resume cursor.
- `issue-count` (`order_by_issue_count`): sorts descending by each repo's
  live open-GitHub-issue count. The count-fetching `gh` call itself is kept
  out of this function (and out of this whole module) — it's injected in
  as an already-fetched `dict[str, int]` — so this stays unit-testable
  without ever invoking `gh`; the actual fetch lives in `cli.py`'s
  `fetch_issue_counts`, mirroring `find_orphaned_pr`'s existing gh-calling
  pattern there.
- `random` (`random_order`): a fresh shuffle every call. Takes an
  injectable `random.Random` rather than reaching for the `random` module's
  global functions, so tests can assert a deterministic shuffle instead of
  mocking global state — the same `time_fn`/`sleep_fn`-injection convention
  `cli.py`'s transcript polling and this module's own tests already use.

**The resume cursor design decision.** `round-robin`'s existing
`read_cursor`/`write_cursor` (a bare list index into the alphabetically-
sorted garden, in `overnight_cursor.json`'s `next_index` field) is only
meaningful because round-robin's ordering is stable across runs — index 3
means the same repo every time, as long as the garden itself hasn't
changed. `issue-count` and `random` both break that assumption: issue-count
re-sorts by a live count that can change between runs (a repo gaining or
losing issues reshuffles the whole ordering), and random reshuffles
unconditionally every run — under either, a bare index would silently
resume at the *wrong* repo, not just a suboptimal one.

Rather than force either of those two into round-robin's index-based
cursor (which would be actively wrong) or give them no resume behavior at
all (which would re-attempt whichever repos happen to sort/shuffle first
every single night, exactly the fairness problem issue #14 exists to
solve), this module keys their resume state by **repo name** instead of
position: `read_attempted`/`write_attempted` persist the list of repo names
already attempted since the current "cycle" through the garden started
(a separate `attempted` field in the same cursor JSON file — round-robin's
`next_index` and issue-count/random's `attempted` coexist in one file
without clobbering each other, so switching `--strategy` across runs
doesn't lose either strategy's own progress). `resume_order` filters this
run's strategy-ordered candidate list down to whatever hasn't been
attempted yet in the current cycle (preserving that run's own order,
e.g. still highest-issue-count-first); once every repo in the garden has
been attempted at least once, the cycle is considered complete and the
next call starts a fresh cycle from a freshly-computed full ordering
(`next_attempted` resets the persisted list to just what's newly attempted
that fresh-cycle run, discarding the just-completed cycle's now-irrelevant
history) — so both non-round-robin strategies still guarantee every garden
repo gets attempted at least once per cycle, the same fairness guarantee
round-robin's rotation already provided, just keyed by name instead of
position so a re-sort or a reshuffle can never point it at the wrong repo.

**When the cursor is written** is `cli.py`'s `cmd_overnight` (see its
`persist_cursor`), not this module's business — but it matters to
everything above: both writers are called after *every batch*, so a run
killed mid-garden keeps the progress it already made. Writing once at the
end of the run instead is what made the whole resume mechanism a no-op for
the runs that most needed it (issue #42).
"""
from __future__ import annotations

import json
import os
import random
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from gardener import notify, state


class Strategy(str, Enum):
    """`gardener overnight --strategy` values — see this module's docstring
    section on repo-selection strategies and the resume cursor for what each
    one actually does and why the cursor works differently per strategy."""

    ROUND_ROBIN = "round-robin"
    ISSUE_COUNT = "issue-count"
    RANDOM = "random"


# A full night's sleep — the use case this subcommand exists for. 8h
# comfortably covers several TEND_DEFAULT_TIMEOUT_SECONDS (45 min)
# worst-case cycles back to back (8h / 45min ~= 10 repos worst case, more
# in practice — see the module docstring above on real observed durations)
# while still being a bounded default rather than "run until the garden is
# empty, how ever long that takes" if `--hours` is omitted.
DEFAULT_OVERNIGHT_HOURS = 8.0

# Two repos at a time. This device has no true process isolation and real,
# shared CPU/RAM (see README's "no true always-on daemon guarantee"
# caveat), so this is a deliberately modest step up from strictly
# sequential rather than "as wide as the garden is long" — a batch is still
# bounded by one repo's TEND_DEFAULT_TIMEOUT_SECONDS either way (see
# `batch_repos`), so the budget arithmetic is unaffected by this default.
DEFAULT_OVERNIGHT_CONCURRENCY = 2

# Reshuffle every run rather than always walking the garden in the same
# alphabetical rotation. Both defaults changed together deliberately: with
# `round-robin`, a repo's *position* in the sorted garden decided which
# repos shared a concurrent batch and which got reached on a short night,
# so the same neighbours were tended together every time. `random` costs
# nothing in fairness — it resumes by repo name, not list position, so the
# "every garden repo is attempted at least once per cycle" guarantee
# round-robin's rotation provided still holds (see the resume-cursor
# section of this module's docstring above).
DEFAULT_OVERNIGHT_STRATEGY = Strategy.RANDOM


def default_cursor_path() -> Path:
    override = os.environ.get("GARDENER_STATE_DIR")
    base = Path(override) if override else Path.home() / ".local" / "state" / "gardener"
    return base / "overnight_cursor.json"


def _load_cursor_file(path: Path) -> dict:
    """The raw cursor JSON object, or `{}` if missing/corrupt/not-an-object
    — shared by `read_cursor`/`read_attempted` (each only look at their own
    key) and by both write functions (each merges into whatever the other
    strategy already persisted, rather than clobbering it — see this
    module's docstring on why round-robin's `next_index` and issue-count/
    random's `attempted` coexist in one file)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def read_cursor(path: Optional[Path] = None) -> int:
    """0 (start of the garden list) if no cursor has ever been written, or
    if the file is missing/corrupt — same "absence is a safe, normal
    default" posture as garden.py/merge_allowlist.py, not an error.

    `round-robin`-only: see this module's docstring for why `issue-count`/
    `random` use `read_attempted` instead."""
    data = _load_cursor_file(path or default_cursor_path())
    idx = data.get("next_index", 0)
    return idx if isinstance(idx, int) and idx >= 0 else 0


def _write_cursor_file(path: Path, data: dict) -> None:
    """Replace the cursor file atomically: write a sibling temp file, then
    `os.replace` it into place (atomic within a filesystem, which a sibling
    in the same directory guarantees).

    A plain `write_text` truncates first and writes second, so a process
    killed in between leaves a torn or empty file — which both readers
    treat as "no cursor at all" (`_load_cursor_file`), i.e. silently
    restarting the cycle. That window used to be hit at most once per run;
    now that `cmd_overnight` persists after every batch (issue #42), it
    would be open once per batch instead, on a device whose defining
    trait is killing processes without warning. Trading one more small
    file write for a cursor that is either the old value or the new one,
    never a half-written one, is the whole point of writing per batch."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data) + "\n")
    os.replace(tmp, path)


def write_cursor(index: int, path: Optional[Path] = None) -> None:
    """`round-robin`-only. Merges into the existing cursor file rather than
    overwriting it outright, so a previous `--strategy issue-count`/
    `random` run's own `attempted` field (see `write_attempted`) survives a
    later round-robin run untouched — each strategy only ever touches its
    own key in this shared file. Written atomically — see
    `_write_cursor_file`."""
    path = path or default_cursor_path()
    data = _load_cursor_file(path)
    data["next_index"] = index
    _write_cursor_file(path, data)


def read_attempted(path: Optional[Path] = None) -> list[str]:
    """Repo names already attempted since the current cycle through the
    garden started, for `issue-count`/`random` — the name-keyed equivalent
    of `read_cursor`'s bare index, used instead of it because neither
    strategy's ordering is stable across runs (see this module's docstring).
    Empty list if no cursor has ever been written for this key, or if the
    file/field is missing, corrupt, or not a list of strings — same
    "absence is a safe, normal default" posture as `read_cursor`."""
    data = _load_cursor_file(path or default_cursor_path())
    names = data.get("attempted", [])
    if isinstance(names, list) and all(isinstance(n, str) for n in names):
        return names
    return []


def write_attempted(names: list[str], path: Optional[Path] = None) -> None:
    """`issue-count`/`random`-only. Merges into the existing cursor file the
    same way `write_cursor` does, so it never clobbers round-robin's own
    `next_index` field. Written atomically — see `_write_cursor_file`."""
    path = path or default_cursor_path()
    data = _load_cursor_file(path)
    data["attempted"] = names
    _write_cursor_file(path, data)


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


def order_by_issue_count(garden: list[str], issue_counts: dict[str, int]) -> list[str]:
    """`issue-count` strategy: `garden` sorted descending by each repo's
    open-issue count, taken from an already-fetched `issue_counts` mapping
    rather than calling `gh` itself — the actual `gh issue list ...` fetch
    lives in `cli.py`'s `fetch_issue_counts` (mirroring `find_orphaned_pr`'s
    existing gh-calling pattern there), kept out of this function so it
    stays unit-testable without ever invoking `gh` (see gardener/CLAUDE.md's
    testing conventions).

    A repo missing from `issue_counts` (its fetch failed, or `gh`/network
    was unavailable) is treated as count 0 — the lowest priority, same as a
    repo that genuinely has no open issues — rather than crashing the whole
    ordering over one repo's fetch failure, or artificially prioritizing it.
    Ties (including "everything missing, everything 0") break alphabetically
    so the result is fully deterministic given the same inputs — the same
    tie-break `garden.py`'s own sorted storage already uses."""
    return sorted(garden, key=lambda repo: (-issue_counts.get(repo, 0), repo))


def random_order(garden: list[str], rng: Optional[random.Random] = None) -> list[str]:
    """`random` strategy: a fresh shuffle of `garden` every call. Takes an
    injectable `random.Random` (falling back to a real one, seeded from
    system entropy, only when `rng` is omitted) rather than reaching for the
    `random` module's global functions directly — so tests can assert
    against a deterministic shuffle (`random.Random(<fixed seed>)`) instead
    of mocking global state, the same injection convention `cli.py`'s
    transcript polling (`time_fn`/`sleep_fn`) and this module's own tests
    already use."""
    rng = rng if rng is not None else random.Random()
    order = list(garden)
    rng.shuffle(order)
    return order


def resume_order(full_order: list[str], attempted: list[str]) -> tuple[list[str], bool]:
    """The `issue-count`/`random` equivalent of `repos_to_attempt`'s
    index-based rotation, but keyed by repo name (see this module's
    docstring for why a bare index doesn't work once the ordering itself
    isn't stable across runs).

    `full_order` is this run's freshly-(re)computed strategy ordering
    (`order_by_issue_count`/`random_order`'s output); `attempted` is every
    repo name already attempted since the current cycle through the garden
    began (`read_attempted`'s output). Returns `(order_for_this_run,
    cycle_reset)`:

    - If any repo in `full_order` hasn't been attempted yet this cycle,
      returns just those, in `full_order`'s own relative order (still
      highest-issue-count-first, or still this run's shuffle order) —
      `cycle_reset=False`.
    - If every repo has already been attempted (the cycle is complete),
      returns `full_order` unchanged, starting a fresh cycle —
      `cycle_reset=True`. Without this fallback, a completed cycle would
      leave an empty candidate list and `overnight` would silently stop
      dispatching anything at all under these two strategies once every
      repo had been attempted once.
    """
    attempted_set = set(attempted)
    remaining = [repo for repo in full_order if repo not in attempted_set]
    if remaining:
        return remaining, False
    return full_order, True


def next_attempted(attempted_before: list[str], cycle_reset: bool, newly_attempted: list[str]) -> list[str]:
    """The `attempted` list to persist (`write_attempted`) after a run under
    `issue-count`/`random`: `attempted_before` (this cycle's attempted names
    prior to this run) plus `newly_attempted` (the repos this run actually
    dispatched, in the order `cmd_overnight` attempted them) — unless
    `resume_order` reported `cycle_reset=True` for this run, in which case
    the just-completed cycle's history is no longer relevant and this run's
    own attempts start the next cycle's history instead. Deduplicates while
    preserving order, defensively, though the two lists are not expected to
    overlap in normal operation."""
    base = [] if cycle_reset else attempted_before
    seen: set[str] = set()
    result: list[str] = []
    for name in (*base, *newly_attempted):
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


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
    # Set by `cli.py`'s `_dispatch_one_for_overnight` from the dispatch
    # layer's own signal rather than inferred from `gap_summary` text here —
    # this is a fact that layer already established (see
    # `dispatch.is_device_global_failure`), not something to re-derive by
    # pattern-matching a summary string a second time.
    #
    # True when the failure was about *this device*, not this repo: broken
    # credentials, an exhausted usage window, or an unreachable GitHub. Such
    # a repo never got a real attempt, so the resume cursor must not advance
    # past it — that is the whole reason this flag is carried up here.
    blocked: bool = False


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
