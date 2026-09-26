"""`gardener dashboard` — a small local, read-only web UI over gardener's
own state (sqlite run history, the garden/merge-allowlist JSON files, and
every `tend`/`overnight` log file still being written to) so a human
doesn't have to poll `gardener status` or tail log files by hand to see
what an unattended run is doing.

The log files this reads are written by `run_log.py`, which tees each
dispatching run's stderr narration into `<state>/logs/`. Both modules
resolve that directory independently; `tests/test_run_log.py` asserts they
agree, because when they didn't, every live panel here rendered empty
forever without anything failing.

Stdlib-only (`http.server`), matching gardener's own stdlib-only rule (see
CLAUDE.md) — no Flask/FastAPI, no new dependency. Read-only: this module
never writes anything gardener itself owns (state db, garden/allow-list
files, logs) — it only reads and renders them. `run_server` binds to
127.0.0.1 only, never 0.0.0.0 — this serves local run history and dollar
costs, and has no auth, so it must never be reachable off this machine.

## Live-progress parsing is a best-effort UI aid, not a new source of truth

`state.list_runs()` (the sqlite db `cli.py` already writes to on every
completed dispatch) is gardener's one real outcome record — this module
never invents a second one. The one thing the db can't show is "what's
dispatched *right now*, before it's finished and been recorded" — for
that, `parse_in_progress`/`parse_batch_progress` regex-parse the active
logs' own `gardener: tending <repo> (allow_merge=...)` / `gardener:
finished tending <repo>` lines (exactly what `cli.py`'s `_dispatch_tend`
prints, as a matched pair, on every one of its return paths — see its
docstring), which is still best-effort: a run killed outright between the
two lines leaves its repo showing as in progress until the log ages out
of the active window. Treat the in-progress list as "what the log
suggests is still running," not gardener's authoritative record of it.

`notify.py`'s `notify: sent to Discord: gardener <mode>: ...` line is
also accepted as terminal, but only as a fallback for logs written before
the `finished tending` line existed — it must never be the *only* way a
repo clears, because `NullNotifier` (the documented no-webhook-configured
case) prints nothing at all, which is what left every repo pinned in
"Currently tending" for the whole life of a log (issue #51).

## More than one run can be live at once

`repo_lock.py` exists precisely because a manual `gardener tend` alongside
the devsrv-managed `overnight` run is a supported configuration, and each
dispatching invocation gets its own `<command>-<stamp>.log`. So the live
panels read *every* log written to within `ACTIVE_LOG_WINDOW_SECONDS`
(`find_active_logs`), not just the single newest one — otherwise starting
a one-repo manual tend silently swapped the whole page over to it and the
overnight run it was running alongside vanished with no indication (issue
#50). The log *tail* panel still shows one file at a time, since
interleaving two raw narrations would be unreadable, but the payload
carries a tail per live log (`log_tails`) and the panel's caption becomes
a picker over them once there is more than one — naming the others
without being able to open them was the least useful of the three
possible states (issue #117). A picked log is remembered for as long as
it stays live, so the newest-mtime default cannot yank the view away
mid-read when two runs are both writing.

## Three different progress numbers, kept apart

An `overnight` run has three nested progress questions and they are not
interchangeable: how far through *this batch* it is (`parse_batch_progress`,
from the log's own `(N-M/T candidates this run)` line), how much of *this
invocation's time budget* is left (`parse_overnight_start` for the budget
and the strategy, `log_started_at` for when the run began), and how far
through *the cycle over the whole garden* it is (`overnight.read_attempted`
/`read_cursor` against the garden list). Only the first was ever on the
page, and its denominator is this invocation's candidate count, which is
itself a consequence of how many repos a previous night already attempted —
so `candidates 1–2 of 29` could not say whether the garden was nearly
through a cycle or had just started one (issue #113).

The cycle number is the one the design is actually built around: `overnight`
resumes across several nights when the garden is bigger than one budget
window (see `docs/OVERNIGHT.md`). Which cursor key holds it depends on the
running strategy — `next_index` for `round-robin`, the `attempted` name list
for `issue-count`/`random` — so the payload reports both and names the
strategy alongside, rather than shipping one of them under a
strategy-neutral name that silently means nothing under the default.

## The headline panel names its own window

The page's first panel — runs, cost, errors, in flight — is scoped by
`state.session_stats()` to the newest run plus every run contiguous with
it, a session being whatever activity is unbroken by a gap longer than
`state.SESSION_GAP_SECONDS` and no longer in total than
`state.MAX_SESSION_SPAN_SECONDS`. It was previously headed "Tonight" over the
most recent `run_limit` (40) rows, which is not a night and never claimed
to be: `state.list_runs()` applies no time predicate at all, so with a
garden of ~32 repos filling most of that window in one cycle, the panel
routinely reported a previous night's failures and dollars as this night's
(issue #105). Its error count is the page's most glanceable failure
signal, so it is the number that could least afford an implied window.
The Recent runs table keeps the row window — there "the last 40 runs" is
exactly what the table says it is.

## The garden view

The garden and the merge allow-list used to be two panels of bare repo
pills. With both lists opted-in garden-wide they were the same ~32 names
printed twice, and neither said anything about how a repo was actually
doing. `build_garden_rows` joins them with `state.repo_stats()` into one
row per repo instead, rendered two ways by the page: a table, and a plot
that draws each repo as a plant. Every property of a plant — height,
leaf droop, blossom, leaf litter, mound width — is a column of that row,
listed in the page's own legend; none of it is invented. Growth is drawn
from *all-time* stats, not the `run_limit` window the Recent runs table
uses, so a plant's size means "how much tending this repo has had," not
"how recently it appeared in the log tail."

Each plant is a `<button>` opening a detail card under the plot, not a
`<div>` carrying its facts in a `title=` tooltip: this page is written
phone-first (see the `max-width: 720px` block), and a hover tooltip is the
one interaction a touch device cannot perform, so on the default view of
the layout's target device every per-repo fact was unreachable (issue
#114). The card is also where `last_run`/`last_outcome` surface — the two
`build_garden_rows` fields no view rendered at all.

## Saying so when the page is no longer live

A poll that fails leaves every panel rendering the last good snapshot,
which is the failure mode most likely to happen during an unattended
overnight run (server OOM-killed, phone off the network) and the hardest
to notice. `refresh` therefore separates the four ways a poll can fail —
the fetch itself, a non-2xx status (`res.ok`, checked rather than left to
`res.json()` throwing on the error body), an unparseable body, and a
render that throws — and each marks `<body class="stale">` under its own
name, desaturating the content and replacing the header heartbeat with
the *age* of what's on screen rather than a static caption (issue #116).
Only once every panel has actually rendered the new snapshot is the page
marked fresh again.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import sys
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from gardener import dispatch, garden, merge_allowlist, overnight, state

LOGS_DIR_NAME = "logs"
DEFAULT_PORT = 8765

# How stale a log may be and still count as one of the runs currently in
# flight. Derived from the real dispatch timeout rather than picked: a
# single `tend` can sit inside one `claude` subprocess for the whole of
# `TEND_DEFAULT_TIMEOUT_SECONDS` without gardener printing a single line,
# so anything shorter would drop a genuinely-running overnight batch off
# the page mid-dispatch. The margin covers the clone/refresh and
# record/notify work either side of that subprocess.
ACTIVE_LOG_WINDOW_SECONDS = dispatch.TEND_DEFAULT_TIMEOUT_SECONDS + 300

TENDING_RE = re.compile(r"^gardener: tending (\S+) \(allow_merge=")
FINISHED_RE = re.compile(r"^gardener: finished tending (\S+)$")
NOTIFY_RE = re.compile(r"^notify: sent to Discord: gardener (\S+): (?:MUTATION — |FAILED — )?(.+)$")
# Both shapes `cmd_overnight` emits: the `N-M/T` range form for a
# concurrent batch, and the bare `N/T` form for the (default) sequential
# `--concurrency 1` run, where the batch is a single repo.
BATCH_RE = re.compile(r"\((\d+)(?:-(\d+))?/(\d+) candidates this run")
# `cmd_overnight`'s opening line, which is the only place the run's own
# time budget and the strategy it picked are stated. Both of its branches
# (round-robin, and the name-keyed issue-count/random one) print the same
# three fields in the same order before diverging, so this matches up to
# `budget=` and stops — the resume clause after it differs per strategy.
OVERNIGHT_START_RE = re.compile(
    r"overnight starting .*?(\d+) repo\(s\) in garden, "
    r"strategy=([\w-]+), budget=([\d.]+)h"
)
# `run_log.log_file_name`'s `<command>-<YYYYmmdd-HHMMSS>.log`. The stamp is
# `datetime.now()` at the top of the dispatching run, i.e. naive local
# time, and is read back the same way below.
LOG_STAMP_RE = re.compile(r"-(\d{8}-\d{6})\.log$")


def default_logs_dir(state_dir: Optional[Path] = None) -> Path:
    base = state_dir or state.default_state_dir()
    return base / LOGS_DIR_NAME


def find_active_log(logs_dir: Path) -> Optional[Path]:
    """The most recently modified `*.log` file, or None if the logs dir is
    missing/empty. 'Active' is a heuristic (most-recently-written), not a
    guarantee the process behind it is still running — a just-finished
    run's log is still the most relevant one to show until a newer run
    starts writing."""
    if not logs_dir.exists():
        return None
    # Guarded the same way `find_active_logs` guards its own stat, and for
    # the same reason: `run_log.prune_old_logs` runs at the start of every
    # dispatching run and can delete a log between this glob and the stat
    # below. Unguarded, that raced `OSError` propagated all the way out of
    # `do_GET` (issue #121) — and this is the path every poll takes once
    # no log is fresh enough to be "active", i.e. all day.
    dated = []
    for path in logs_dir.glob("*.log"):
        try:
            if path.is_file():
                dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not dated:
        return None
    return max(dated, key=lambda d: d[0])[1]


def find_active_logs(
    logs_dir: Path,
    window_seconds: float = ACTIVE_LOG_WINDOW_SECONDS,
    now: Optional[float] = None,
) -> list[Path]:
    """Every log the live panels should be built from, most recently
    modified first.

    That is every `*.log` written to within `window_seconds` — plural
    because two gardener processes at once is supported, not a misuse (see
    the module docstring). When nothing is that fresh it falls back to
    `find_active_log`'s single newest log, so a finished run's narration
    still renders instead of the page going blank the moment its last line
    ages out.

    Deliberately uncapped: `build_status` reads every log this returns on
    every 4 s poll, but silently dropping one is the exact failure this
    replaces, and `run_log.DEFAULT_KEEP` already bounds the directory at
    30 files — of which only concurrently-dispatching ones can be inside
    the window at all."""
    if not logs_dir.exists():
        return []
    cutoff = (now if now is not None else time.time()) - window_seconds
    dated = []
    for path in logs_dir.glob("*.log"):
        try:
            if path.is_file():
                dated.append((path.stat().st_mtime, path))
        except OSError:
            # Pruned by `run_log.prune_old_logs` between the glob and the
            # stat — not an error, just one fewer log to render.
            continue
    fresh = sorted((d for d in dated if d[0] >= cutoff), key=lambda d: d[0], reverse=True)
    if fresh:
        return [path for _mtime, path in fresh]
    newest = find_active_log(logs_dir)
    return [newest] if newest else []


# `print(msg, file=sys.stderr)` writes the message and its newline as two
# separate writes, so two `overnight --concurrency N` dispatch threads
# printing at the same moment interleave as `<msg A><msg B>\n\n` — measured
# on every overnight log on the box this was found on (3–28 glued lines
# each), e.g. `gardener: tending o/a (allow_merge=True)gardener: tending
# o/b (allow_merge=True)`. Every progress regex here is anchored at `^`, so
# the second message on such a line was invisible: a repo whose `tending`
# line was glued never showed as in flight, and one whose `finished
# tending` line was glued stayed in flight for the life of the log. The
# split point is a message prefix directly after a non-space character —
# except after `/`, because a repo named `gardener` legitimately prints
# `owner/gardener: PR opened` mid-line.
_GLUED_MESSAGE_RE = re.compile(r"(?<=[^\s/])(?=(?:gardener|notify): )")


def split_glued_lines(lines: list[str]) -> list[str]:
    """`lines` with every glued line (see `_GLUED_MESSAGE_RE`) split back
    into the separate messages it was written as."""
    out: list[str] = []
    for line in lines:
        if "gardener: " in line[1:] or "notify: " in line[1:]:
            out.extend(_GLUED_MESSAGE_RE.split(line))
        else:
            out.append(line)
    return out


def tail_lines(path: Path, n: int = 400) -> list[str]:
    """Return the last `n` lines of `path` without loading the entire file.

    Seeks backwards from the end of the file in chunks, stopping as soon as
    `n` newlines have been collected — so only the tail is ever in memory,
    regardless of file size. This matters at a 4 s poll rate when a
    long-running overnight session can produce a multi-MB log."""
    try:
        f = path.open("rb")
    except OSError:
        return []
    with f:
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return []
        chunk_size = 4096
        collected: list[bytes] = []
        pos = size
        newlines_found = 0
        # Read backwards in chunks until we have n+1 newlines (n+1 so we
        # can discard any partial leading line) or reach the start.
        while pos > 0 and newlines_found <= n:
            read_size = min(chunk_size, pos)
            pos -= read_size
            f.seek(pos)
            chunk = f.read(read_size)
            collected.append(chunk)
            newlines_found += chunk.count(b"\n")
        raw = b"".join(reversed(collected))
    lines = split_glued_lines(raw.decode("utf-8", errors="replace").splitlines())
    return lines[-n:]


def head_lines(path: Path, n: int = 200) -> list[str]:
    """Return the first `n` lines of `path`.

    The counterpart to `tail_lines`, and needed for exactly one thing: the
    `overnight starting` header carrying the run's time budget is the
    *first* line a run writes, and a full garden's narration runs to well
    over `tail_lines`' 400-line window, so by the time the budget is worth
    knowing it has long since scrolled out of the tail. Reads line by line
    and stops at `n`, so a multi-MB log costs the same as a short one."""
    try:
        f = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return []
    with f:
        lines: list[str] = []
        for line in f:
            lines.extend(split_glued_lines([line.rstrip("\n")]))
            if len(lines) >= n:
                break
    return lines[:n]


def log_started_at(path: Path) -> Optional[float]:
    """The epoch seconds encoded in a run log's own filename, or None if it
    doesn't carry `run_log.log_file_name`'s stamp.

    The start of a run is not written *into* the log — only into its name —
    and it is what "elapsed" is measured from. Parsed as naive local time
    because `run_log.tee_stderr` stamps it with `datetime.now()`; both
    sides are the same machine, since the dashboard only ever reads logs
    this device wrote."""
    m = LOG_STAMP_RE.search(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()
    except (ValueError, OSError, OverflowError):
        return None


def parse_in_progress(lines: list[str]) -> list[str]:
    """Repos with a `gardener: tending X` line in this log and no matching
    `gardener: finished tending X` line after it.

    A `tend`-mode notify line (success or failure), or a *failed*
    `create-dev-loop` notify, is accepted as terminal too — but only so
    logs written before the `finished tending` line existed still clear;
    `cli.py`'s `_dispatch_tend` now emits that line on every return path,
    including the ones no notify ever covered. A *successful*
    create-dev-loop notify is deliberately not terminal: the real tend
    dispatch still has to run and report. See the module docstring for why
    this is best-effort, not authoritative.

    Read in line order rather than as two sets, so the *last* marker for a
    repo wins: two invocations started in the same second share one log
    file (`run_log.tee_stderr` appends rather than truncates for exactly
    that case), and a repo that finished and was then started again must
    read as in flight, not as finished."""
    order: list[str] = []
    in_flight: dict[str, bool] = {}
    for line in lines:
        m = TENDING_RE.match(line)
        if m:
            if m.group(1) not in in_flight:
                order.append(m.group(1))
            in_flight[m.group(1)] = True
            continue
        m = FINISHED_RE.match(line)
        if m:
            if m.group(1) in in_flight:
                in_flight[m.group(1)] = False
            continue
        m = NOTIFY_RE.match(line)
        if m:
            mode, repo = m.group(1), m.group(2)
            if repo in in_flight and (
                mode == "tend" or (mode == "create-dev-loop" and "FAILED — " in line)
            ):
                in_flight[repo] = False
    return [repo for repo in order if in_flight[repo]]


def _safe_list(fn) -> list:
    """Call `fn()` and return its result, or `[]` on `ValueError`.

    Guards `build_status` against a corrupt/mid-write garden or
    merge-allowlist JSON file — the dashboard must stay usable even when
    those files are temporarily invalid."""
    try:
        return fn()
    except ValueError:
        return []


def parse_batch_progress(lines: list[str]) -> Optional[tuple[int, int, int]]:
    """(range_start, range_end, total) from the most recent 'candidates
    this run' line `cmd_overnight` prints, or None if this log has no such
    line (e.g. a plain `tend --repo` dispatch, not `overnight`).

    `cmd_overnight` prints two shapes: 'N-M/T' for a concurrent batch, and
    a bare 'N/T' for the default `--concurrency 1` run, whose batch is one
    repo. The latter is read as a batch of one (`end = start`), so the
    common sequential case renders progress rather than nothing."""
    match = None
    for line in lines:
        m = BATCH_RE.search(line)
        if m:
            match = m
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) is not None else start
    return (start, end, int(match.group(3)))


def parse_overnight_start(lines: list[str]) -> Optional[dict]:
    """`{garden_size, strategy, budget_hours}` from the `overnight
    starting` header `cmd_overnight` prints, or None if these lines have no
    such header (a plain `tend --repo` log, or an `overnight` log truncated
    before its first line).

    The last match wins, for the same reason `run_log.tee_stderr` opens its
    log in append mode: two runs started within the same second share one
    file, and the newer header is the one describing the run still going."""
    match = None
    for line in lines:
        m = OVERNIGHT_START_RE.search(line)
        if m:
            match = m
    if match is None:
        return None
    return {
        "garden_size": int(match.group(1)),
        "strategy": match.group(2),
        "budget_hours": float(match.group(3)),
    }


def build_garden_rows(
    garden_repos: list[str],
    allowed_repos: list[str],
    stats: dict[str, state.RepoStats],
    in_progress: list[str],
) -> list[dict]:
    """One row per repo the operator has opted in anywhere, joining the
    two opt-in lists with that repo's run history.

    Replaces what used to be two separate dashboard panels listing the
    garden and the merge allow-list as bare pills. Those were ~32
    near-identical repo names printed twice, which read as duplication and
    still left the actually interesting question — *how is each repo
    doing* — unanswered. Merge-allow-list membership is a column here
    (`can_merge`) rather than a second list.

    A repo on the allow-list but not in the garden is still a row
    (`in_garden: False`): dropping it would lose the one fact the old
    allow-list panel carried that the garden panel didn't, and the
    mismatch is worth seeing — it means something is permitted to merge
    that `overnight` will never dispatch.

    Rows are sorted by repo name, matching `gardener garden list`. This is
    the payload's canonical order, not necessarily the drawn one: the plot
    view re-orders client-side to put the repos needing attention first
    (issue #132), and the table view sorts by whichever column the reader
    picked. Keeping the wire order stable and alphabetical is what lets
    both of those be presentation decisions."""
    repos = sorted(set(garden_repos) | set(allowed_repos))
    allowed = set(allowed_repos)
    in_flight = set(in_progress)
    garden_set = set(garden_repos)
    rows = []
    for repo in repos:
        s = stats.get(repo)
        rows.append(
            {
                "repo": repo,
                "in_garden": repo in garden_set,
                "can_merge": repo in allowed,
                "in_flight": repo in in_flight,
                "runs": s.runs if s else 0,
                "successes": s.successes if s else 0,
                "errors": s.errors if s else 0,
                "last_run": s.last_run if s else None,
                "last_success": s.last_success if s else None,
                "last_outcome": s.last_outcome if s else None,
                "cost_usd": round(s.cost_usd, 2) if s else 0.0,
                # Aggregated by `repo_stats` all along and dropped here,
                # so no view could show how much of a time budget a repo
                # actually consumes (issue #137).
                "duration_ms": s.duration_ms if s else 0,
            }
        )
    return rows


#: Bumped whenever a key the page reads is renamed, removed, or changes
#: meaning. The page compares it against its own baked-in copy and refuses
#: to render a payload it doesn't understand, instead of interpolating
#: `undefined` into the stat tiles under a confident heartbeat (issue
#: #123). This is a real, routine skew, not a theoretical one: `overnight`
#: self-updates before each run, so a tab left open across a restart is
#: the normal case, and #119's `recent_*` → `session_*` rename is exactly
#: the shape of change that produced it. Bumped to 3 when
#: `overnight_next_index` became `overnight_cycle`/`overnight_run` (issue
#: #113) — the same shape of change again.
PAYLOAD_SCHEMA = 3


def build_status(
    state_dir: Optional[Path] = None,
    run_limit: int = 40,
    log_tail_lines: int = 400,
    repo: Optional[str] = None,
    history_days: int = 14,
    garden_repos: Optional[list[str]] = None,
    allowed_repos: Optional[list[str]] = None,
    hub: bool = False,
) -> dict:
    """The `/api/status` payload.

    `garden_repos`/`allowed_repos` replace the two opt-in list files when
    given. The hub (`gardener/hub.py`, RFC 0007) passes the union of what
    each device last pushed, since its own state dir holds no garden. `hub`
    marks the payload as coming from a store that combines devices, so the
    page can say that the log-backed panels are per-device instead of
    rendering them empty, which would read as "nothing is running"."""
    base = state_dir or state.default_state_dir()
    db_path = base / "gardener.sqlite3"
    logs_dir = default_logs_dir(base)

    # `repo` narrows the Recent runs table to one repo's history. Both it
    # and `run_limit` were already `list_runs` parameters that nothing ever
    # passed, so the plant detail card could report "17 errors" with no way
    # to reach any of them (issue #138).
    runs = state.list_runs(db_path=db_path, limit=run_limit, repo=repo)
    # Every live log, not just the newest — a manual `tend` started
    # alongside the overnight run used to hide it completely (issue #50).
    active_logs = find_active_logs(logs_dir)
    lines_by_log = {path: tail_lines(path, log_tail_lines) for path in active_logs}
    active_log = active_logs[0] if active_logs else None
    log_lines = lines_by_log.get(active_log, [])

    in_progress: list[str] = []
    for path in active_logs:
        for repo in parse_in_progress(lines_by_log[path]):
            if repo not in in_progress:
                in_progress.append(repo)
    # The freshest log that actually has a batch line, so an `overnight`
    # run's progress bar survives a `tend --repo` log being written more
    # recently — a plain tend has no batch line of its own to replace it
    # with, and blanking the bar would read as "the batch ended".
    batch = None
    for path in active_logs:
        batch = parse_batch_progress(lines_by_log[path])
        if batch is not None:
            break

    # The run's own budget and strategy, from the freshest live log that
    # has an `overnight starting` header — read from the *head* of the file
    # rather than `lines_by_log`'s tail, because that header is line 1 and a
    # full garden's narration is longer than the tail window (see
    # `head_lines`). Same "first log that has one wins" fallback as `batch`.
    overnight_run = None
    for path in active_logs:
        started = parse_overnight_start(head_lines(path))
        if started is not None:
            overnight_run = {
                "strategy": started["strategy"],
                "budget_hours": started["budget_hours"],
                # Named apart from `overnight_cycle`'s `garden_size`, which
                # is the garden as it is *now*: this one is what the run
                # itself saw when it started, and a repo added mid-run
                # makes them legitimately disagree.
                "garden_size_at_start": started["garden_size"],
                "log": str(path),
            }
            began = log_started_at(path)
            elapsed = max(0.0, time.time() - began) if began is not None else None
            overnight_run["started_at"] = (
                datetime.fromtimestamp(began).isoformat() if began is not None else None
            )
            # Whole seconds: the page polls every 4 s and renders these to
            # the nearest minute, so sub-second precision is noise in every
            # `curl /api/status | jq` read of the payload.
            overnight_run["elapsed_seconds"] = None if elapsed is None else round(elapsed)
            overnight_run["remaining_seconds"] = (
                round(max(0.0, started["budget_hours"] * 3600 - elapsed))
                if elapsed is not None
                else None
            )
            break

    # Scoped to the newest contiguous burst of runs, not to the `run_limit`
    # slice above: the panel these feed is the one read to answer "how did
    # tonight go", and a fixed row count reaches back however far it has to,
    # so it routinely reports a previous night's errors and spend as this
    # night's (issue #105). The Recent runs table keeps the row window —
    # there "the last 40" is exactly what it claims to be.
    session = state.session_stats(db_path=db_path)

    if garden_repos is None:
        garden_repos = _safe_list(lambda: garden.list_garden(path=base / "garden.json"))
    if allowed_repos is None:
        allowed_repos = _safe_list(
            lambda: merge_allowlist.list_allowed(path=base / "merge_allowlist.json")
        )
    garden_rows = build_garden_rows(
        garden_repos, allowed_repos, state.repo_stats(db_path=db_path), in_progress
    )

    # Where the *cycle* through the garden stands, which is the unit
    # `overnight` is actually built around — it resumes across several
    # nights when the garden is bigger than one budget window. Both cursor
    # keys are reported rather than one, because the file holds both and
    # only the running strategy says which is meaningful: `next_index` is
    # round-robin's, `attempted` is issue-count/random's (see
    # `overnight.py`'s docstring). The payload used to carry `next_index`
    # alone under the strategy-neutral name `overnight_next_index`, which
    # reads as 0 — "cycle just started" — for the whole of a `random` run,
    # the default (issue #113).
    cursor_path = base / "overnight_cursor.json"
    attempted = overnight.read_attempted(path=cursor_path)
    overnight_cycle = {
        "garden_size": len(garden_repos),
        "attempted": len(attempted),
        "next_index": overnight.read_cursor(path=cursor_path),
        # Only known while a run is live, since the cursor file doesn't
        # record which strategy wrote it.
        "strategy": overnight_run["strategy"] if overnight_run else None,
    }

    return {
        "schema": PAYLOAD_SCHEMA,
        "generated_at": state.now_iso(),
        "hub": hub,
        # Echoed back so the page can caption the runs table with the
        # filter actually applied, rather than assuming its own request
        # shape survived.
        "runs_filter": {"repo": repo, "limit": run_limit},
        # `active_log` is the log the page tails by default — the newest,
        # and the one `log_tail` carries; `active_logs` is every log the
        # in-flight/batch panels were built from. `log_tails` carries one
        # narration per entry of `active_logs`, keyed by the same path
        # strings, so the reader can switch the tail to any of them
        # instead of being told a second run exists and left there
        # (issue #117). Costs no extra I/O: `lines_by_log` was already
        # read in full on this poll to build the aggregated panels, and
        # was then discarded down to one file.
        "active_log": str(active_log) if active_log else None,
        "active_logs": [str(p) for p in active_logs],
        "log_tail": log_lines[-200:],
        "log_tails": {str(p): lines[-200:] for p, lines in lines_by_log.items()},
        "in_progress": in_progress,
        "batch_progress": (
            {"start": batch[0], "end": batch[1], "total": batch[2]} if batch else None
        ),
        "runs": [
            {
                "id": r.id,
                "repo": r.repo,
                "mode": r.mode,
                "outcome": r.outcome,
                "timestamp": r.timestamp,
                "summary": r.gap_summary,
                "duration_ms": r.duration_ms,
                "cost_usd": r.cost_usd,
                "device": r.device,
            }
            for r in runs
        ],
        # Whether the runs above came from more than one device, so the
        # page shows a Device column only when it can tell rows apart: a
        # single device's store (every local one) would repeat one name
        # down the whole table.
        "multi_device": len({r.device for r in runs if r.device}) > 1,
        # Named `session_*` rather than the `recent_*` these replaced: the
        # numbers now mean a different window, and a renamed key breaks a
        # `curl /api/status | jq` check loudly, where a silently
        # re-scoped one under the old name would not.
        "stats": {
            "session_run_count": session.runs,
            "session_cost_usd": round(session.cost_usd, 2),
            "session_error_count": session.errors,
            "session_started_at": session.started_at,
            "session_ended_at": session.ended_at,
            "session_duration_ms": session.duration_ms,
        },
        # The session's error rows, not just the count in `stats` — see
        # `SessionStats.errors_detail` for why a count alone misreads a
        # single systemic failure as N unrelated ones (issue #136).
        "session_errors": [
            {
                "repo": r.repo,
                "timestamp": r.timestamp,
                "summary": r.gap_summary,
            }
            for r in session.errors_detail
        ],
        # Per-night rollup, so a night that was a total loss stays visible
        # after it stops being the newest session (issue #138).
        "history": [
            {
                "day": d.day,
                "runs": d.runs,
                "errors": d.errors,
                "cost_usd": round(d.cost_usd, 2),
                "duration_ms": d.duration_ms,
            }
            for d in state.daily_stats(db_path=db_path, days=history_days)
        ],
        # `garden`/`merge_allowlist` stay in the payload as the raw lists
        # they always were — `garden_rows` is the joined view the UI
        # renders, but the flat lists are what a `curl /api/status | jq`
        # check of "is repo X opted in" is easiest to read.
        "garden": garden_repos,
        "merge_allowlist": allowed_repos,
        "garden_rows": garden_rows,
        # `overnight_run` is *this invocation* — budget, elapsed, remaining;
        # `overnight_cycle` is the garden-wide rotation the invocation is
        # one slice of. They are genuinely different numbers, and the batch
        # counter above is a third; conflating any two of them is what made
        # the old single bar unreadable.
        "overnight_run": overnight_run,
        "overnight_cycle": overnight_cycle,
    }


PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gardener dashboard</title>
<style>
  :root {
    color-scheme: dark light;
    --bg: #14171a; --panel: #1c2023; --text: #e7ece8; --muted: #9aa39c;
    --border: #2c3236; --accent: #5fbf85; --warn: #e3a35a; --err: #ef6a63;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    /* Garden-plot palette — soil, terracotta, seed, leaf litter. */
    --soil: #3b3129; --pot: #96603f; --seed: #b28f5c; --fallen: #6d4c33;
    /* Leaf/stem per health bucket. Tokens rather than the hardcoded hexes
       these were, because half the plot (soil, pot, seed, litter) already
       flipped with the theme and half did not, so in light mode the one
       signal the legend calls primary — colour — collapsed to ~2.7:1 with
       the buckets within 0.3 of each other (issue #131). The light values
       are darkened to clear 3:1 on --panel and spread by luminance so the
       ramp stays ordinal. Inline SVG resolves var() the same way --soil
       already relies on. */
    --leaf-thriving: #56d182; --stem-thriving: #3d7f52;
    --leaf-steady:   #7fae5c; --stem-steady:   #437a46;
    --leaf-dry:      #bfae4a; --stem-dry:      #7c7a3c;
    --leaf-wilting:  #cf8437; --stem-wilting:  #7d5a33;
    --leaf-struggling: #c25f4c; --stem-struggling: #6f4c3c;
    --leaf-unplanted: #7d8a80; --stem-unplanted: #6d7a71;
    /* Blossom tints — decoration, but they carry the "may merge" fact, and
       #f0c356 on white was 1.66:1. */
    --bloom-1: #e77fa7; --bloom-2: #e88f6c; --bloom-3: #c88ad8;
    --bloom-4: #8fb8e0; --bloom-5: #eec55f; --bloom-faded: #c9a06a;
    --bloom-centre: #f0c356;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f5f6f4; --panel: #ffffff; --text: #1b1f1c; --muted: #5b645d;
      /* --warn was #b3661a: 4.01:1 on --bg, and its only consumer is the
         stale caption — the least readable text on the page in exactly
         the situation it exists for (issue #128). #8f5010 is 5.6:1. */
      --border: #dfe3de; --accent: #2f7a4f; --warn: #8f5010; --err: #b3261e;
      --soil: #7a6450; --pot: #b56f47; --seed: #8b6a3c; --fallen: #8a6448;
      --leaf-thriving: #1f7d45; --stem-thriving: #175c33;
      --leaf-steady:   #4a7f2a; --stem-steady:   #3a621f;
      --leaf-dry:      #8a7a12; --stem-dry:      #6b5f0e;
      --leaf-wilting:  #a35a12; --stem-wilting:  #7d450d;
      --leaf-struggling: #a8321f; --stem-struggling: #7d2417;
      --leaf-unplanted: #6b746d; --stem-unplanted: #5c655e;
      --bloom-1: #c2557e; --bloom-2: #c06442; --bloom-3: #9c5cae;
      --bloom-4: #4d7ba8; --bloom-5: #a8842a; --bloom-faded: #8a6a3c;
      --bloom-centre: #a87a1e;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    -webkit-text-size-adjust: 100%; overflow-wrap: break-word;
  }
  header {
    padding: 1rem 1.5rem; border-bottom: 1px solid var(--border);
    display: flex; align-items: baseline; gap: 0.25rem 1rem; flex-wrap: wrap;
    /* Sticky so the "updated ..." heartbeat stays on screen while scrolling
       a long log on a phone — on a 4 s poll it's the main signal that the
       page is still live, and it's useless scrolled off the top. */
    position: sticky; top: 0; z-index: 1; background: var(--bg);
  }
  header h1 { font-size: 1.1rem; margin: 0; }
  header .sub { color: var(--muted); font-size: 0.85rem; }
  main {
    padding: 1.5rem; display: grid; gap: 1.25rem;
    /* `min(360px, 100%)`, not a bare 360px: on a viewport narrower than
       360px + padding the bare form makes the track wider than the grid
       and the whole body scrolls sideways. */
    grid-template-columns: repeat(auto-fit, minmax(min(360px, 100%), 1fr));
    max-width: 1400px; margin: 0 auto;
  }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; padding: 1rem 1.2rem; overflow: auto;
  }
  .panel h2 {
    font-size: 0.75rem; text-transform: uppercase; letter-spacing: .06em;
    color: var(--muted); margin: 0 0 0.75rem;
  }
  /* The session panel's window caption lives inside its heading, so it has
     to opt out of the heading's uppercase label treatment — it's a
     timestamp, not a label. */
  .panel h2 .sub { text-transform: none; letter-spacing: 0; font-weight: 400; }
  .wide { grid-column: 1 / -1; }
  /* Visually hidden but still announced — the standard clip-rect form.
     Used where a glyph carries meaning no screen reader can read out of
     it (issue #130). */
  .sr-only {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0;
  }
  /* The top band, laid out explicitly rather than left to main's auto-fit.
     auto-fit fitted three 360px tracks at the 1400px cap while this row
     holds only two panels, and every panel below is .wide, so track 3 was
     pinned open and a 437px column of the page's most valuable band sat
     empty while "Currently tending" was squeezed beside it (issue #134). */
  .band {
    grid-column: 1 / -1; display: grid; gap: 1.25rem;
    grid-template-columns: minmax(260px, 1fr) 2fr;
  }
  @media (max-width: 860px) { .band { grid-template-columns: 1fr; } }
  /* A grid rather than a flex row: four stats wrapped by flex leave a
     ragged last line on a phone, where an even 2x2 reads as one block.
     Pinned to four columns above the phone breakpoint, because auto-fit
     inside the narrower track fitted only three and orphaned "in flight"
     — the one tile that is scanned first — alone on a second line. */
  .stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.75rem 1.25rem; }
  @media (min-width: 721px) { .stats { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
  /* tabular-nums so the cost figure doesn't jitter in width on every 4 s
     poll, matching .plant .meta and .garden-table td.num. */
  .stat .n {
    font-size: 1.6rem; font-weight: 600; line-height: 1.2;
    font-variant-numeric: tabular-nums;
  }
  .stat .l { color: var(--muted); font-size: 0.78rem; }
  /* The page's semantics everywhere else (.outcome-error, .pill.live, the
     plant glow) are --err and --accent; the stat tiles rendered every
     number in plain --text, so "16 errors" read exactly like "40 runs".
     Neutral at zero, so an all-clear night stays quiet. */
  .stat.is-err .n { color: var(--err); }
  .stat.is-live .n { color: var(--accent); }
  .pill {
    display: inline-block; padding: 0.1rem 0.55rem; border-radius: 999px;
    font-size: 0.75rem; border: 1px solid var(--border); margin: 0.15rem;
    /* Single-line, because border-radius: 999px clamps to half the height:
       a wrapped two-line pill (a 67-char repo slug on a phone) resolves to
       a ~20px corner arc against 8.8px of horizontal padding, so the arc
       cuts inside the text box and clips the leading glyphs (issue #135). */
    max-width: 100%; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; vertical-align: bottom;
  }
  .pill.live { border-color: var(--accent); color: var(--accent); }
  /* The in-flight names are what this panel exists to show; at 0.75rem
     they rendered smaller than the neutral count in the tile beside them. */
  #in-progress .pill { font-size: 0.95rem; padding: 0.2rem 0.7rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; }
  td.repo { white-space: nowrap; font-family: var(--mono); font-size: 0.8rem; }
  td.summary { color: var(--text); }
  td.device { white-space: nowrap; color: var(--muted); }
  /* Log-backed panels a hub has no data for; see renderHubChrome. */
  .is-hub .log-panel, .is-hub #live-link, .is-hub #cycle, .is-hub #budget, .is-hub #batch { display: none; }
  #hub-user { margin-left: auto; }
  .is-hub #hub-user + #live-link { margin-left: 0; }
  .outcome-error { color: var(--err); }
  .outcome-tend, .outcome-created { color: var(--accent); }
  pre#log {
    font-family: var(--mono); font-size: 0.78rem; white-space: pre-wrap;
    word-break: break-word; max-height: min(480px, 60vh); overflow-y: auto; margin: 0;
    line-height: 1.45;
  }
  /* Keeps the picker on the heading's line without putting it inside the
     heading — see the markup for why. `baseline` rather than `center` so
     the select's text sits on the same line as the caption's, and the
     h2's own bottom margin carries the whole row. */
  .log-head {
    display: flex; align-items: baseline; gap: 0.6rem;
    flex-wrap: wrap; margin-bottom: 0.75rem;
  }
  .log-head h2 { margin-bottom: 0; }
  /* Matches .table-sort select, including the 16px: anything smaller
     makes iOS Safari zoom the whole page when the control takes focus,
     and this page is written phone-first. The explicit [hidden] rule is
     what keeps the single-log case rendering as it always did, and is
     needed here rather than merely defensive: .log-head is a flex
     container, so `display: flex` on the picker's parent does not stop
     the UA `hidden` rule being overridden if .log-pick ever gains a
     display of its own. */
  .log-pick[hidden] { display: none; }
  .log-pick select {
    font: inherit; font-size: 16px; font-family: var(--mono);
    background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 999px;
    padding: 0.15rem 0.5rem; max-width: 100%;
  }
  .log-pick select:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .empty { color: var(--muted); font-style: italic; }
  .progress-bar {
    height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; margin-top: 0.4rem;
  }
  .progress-bar > div { height: 100%; background: var(--accent); }
  /* A poll that fails must not render as a healthy dashboard. Everything
     below the header is desaturated so "last known" can never be mistaken
     for "live"; the header keeps full contrast because it is the part
     carrying the explanation and the snapshot's age.

     Desaturation, not heavy dimming, is what carries the signal: the
     colour on this page (live pills, the progress bar, a plot of green
     plants) all drains at once, which is unmissable.

     The opacity that used to accompany it is gone. It was set to 0.78 on
     the reasoning that 0.4 "would drop below readable contrast" — but
     0.78 already did: composited over --bg it put muted text at 4.43:1
     dark and 3.74:1 light, both under AA, across every panel heading, th,
     stat label and plant caption on the page (issue #128). grayscale
     alone costs no contrast at all.

     Desaturation is also invisible to a screen reader, on a monochrome
     display, and with severe colour vision deficiency, so it can't be the
     only signal — #stale-badge below is the non-colour half. */
  body.stale main { filter: grayscale(1); }
  body.stale #updated { color: var(--warn); }
  #stale-badge {
    display: none; font-size: 0.75rem; font-weight: 600;
    color: var(--warn); border: 1px solid var(--warn); border-radius: 999px;
    padding: 0.05rem 0.5rem;
  }
  body.stale #stale-badge { display: inline-block; }

  /* First paint, before any poll has returned. The static markup used to
     assert "nothing in flight" and render empty tables as fact — a claim
     made before a single byte had been fetched, and the same "looks
     healthy but isn't" failure #116 fixed for later polls (issue #124).
     Reserving the heights also stops the whole page jumping when the
     first payload lands. */
  body.loading .stat .n, body.loading #garden-summary { color: var(--muted); }
  body.loading #plot, body.loading #garden-rows, body.loading #runs { min-height: 4rem; }
  body.loading pre#log { min-height: 6rem; }

  /* ---- Garden panel (plot + table) ---- */
  .toolbar {
    display: flex; align-items: center; gap: 0.5rem 0.9rem;
    flex-wrap: wrap; margin-bottom: 0.9rem;
  }
  .tabs { display: flex; gap: 0.35rem; }
  .tab {
    font: inherit; font-size: 0.8rem; cursor: pointer;
    background: transparent; color: var(--muted);
    border: 1px solid var(--border); border-radius: 999px; padding: 0.25rem 0.8rem;
    /* 29.2px tall as written, on the only route to the table view — which
       is the only view carrying Errors/Cost/Merge as sortable columns
       (issue #135). Inline-flex so the min-height actually centres. */
    display: inline-flex; align-items: center; min-height: 34px;
  }
  .tab[aria-selected="true"] { color: var(--accent); border-color: var(--accent); }
  /* auto-fill (not auto-fit) so a garden of three plants stays three
     small plants at the left rather than three stretched across 1400px. */
  .plot {
    display: grid; gap: 0.25rem;
    grid-template-columns: repeat(auto-fill, minmax(84px, 1fr));
  }
  /* Sky above, an earth band under the soil mound. Applied per cell
     rather than to the whole plot, because the plot wraps onto several
     rows and a single container gradient would put the horizon through
     the middle of the second row of plants. The stops line up with
     SOIL_Y in the SVG, whose aspect ratio is fixed. */
  .plant {
    text-align: center; padding: 0.2rem 0.1rem 0.35rem; border-radius: 10px;
    border: 1px solid transparent;
    background:
      linear-gradient(to bottom,
        transparent 0%, transparent 62%,
        color-mix(in srgb, var(--soil) 22%, transparent) 78%,
        color-mix(in srgb, var(--soil) 30%, transparent) 88%,
        transparent 92%);
  }
  .plant.potted { border-style: dashed; border-color: var(--border); }
  .plant svg { display: block; width: 100%; height: auto; }
  .plant .nm {
    font-size: 0.68rem; line-height: 1.25; overflow-wrap: anywhere;
    margin-top: 0.1rem;
  }
  .plant .meta { font-size: 0.62rem; color: var(--muted); font-variant-numeric: tabular-nums; }
  .plant.live { border-color: var(--accent); }
  .plant.live .nm { color: var(--accent); }
  /* A plant is a real button, not a div with a click handler: every fact
     about a repo except its short name used to live only in `title=`, a
     hover tooltip, on a layout deliberately tuned for a phone (issue
     #114). Tapping one opens the detail card below the plot instead;
     `title` is kept because it costs nothing for pointer users. */
  button.plant {
    font: inherit; color: inherit; width: 100%; display: block; cursor: pointer;
  }
  /* After .plant.live so a selected in-flight plant reads as selected —
     same specificity, so source order is what decides. */
  .plant.selected {
    border-color: var(--accent);
    background-color: color-mix(in srgb, var(--accent) 12%, transparent);
  }
  .plant:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .plant-detail {
    margin-top: 0.8rem; border: 1px solid var(--accent); border-radius: 10px;
    padding: 0.7rem 0.9rem;
    background: color-mix(in srgb, var(--accent) 8%, transparent);
  }
  .plant-detail .pd-head {
    display: flex; align-items: baseline; gap: 0.5rem; justify-content: space-between;
  }
  .plant-detail .pd-head b {
    font-family: var(--mono); font-size: 0.85rem; overflow-wrap: anywhere;
  }
  /* The glyph stays small; the hit area does not. As written this was a
     ~17x23px target — on the card that #114 added specifically for touch
     users (issue #135). The negative margins keep .pd-head's baseline
     layout where it was despite the grown box. */
  .pd-close {
    font: inherit; background: transparent; color: var(--muted);
    border: 0; cursor: pointer; line-height: 1;
    display: flex; align-items: center; justify-content: center;
    min-width: 44px; min-height: 44px; padding: 0;
    margin: -0.6rem -0.5rem -0.6rem 0;
  }
  .pd-close:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
  .plant-detail dl {
    margin: 0.5rem 0 0; display: grid; grid-template-columns: auto 1fr;
    gap: 0.15rem 0.75rem; font-size: 0.8rem;
  }
  .plant-detail dt { color: var(--muted); }
  .plant-detail dd { margin: 0; }
  /* The glow halo is the only animated thing on the page; refresh()
     re-renders the plot only when the data actually changed, so this
     animation isn't restarted from zero on every 4 s poll. */
  @keyframes breathe { 0%, 100% { opacity: 0.15; } 50% { opacity: 0.5; } }
  .plant.live .glow { animation: breathe 2.4s ease-in-out infinite; }
  @media (prefers-reduced-motion: reduce) { .plant.live .glow { animation: none; opacity: 0.3; } }
  .legend { margin-top: 0.9rem; font-size: 0.78rem; color: var(--muted); }
  .legend summary { cursor: pointer; display: flex; align-items: center; min-height: 34px; }
  .legend ul { margin: 0.5rem 0 0; padding-left: 1.1rem; }
  .legend li { margin-bottom: 0.15rem; }
  .legend b { color: var(--text); font-weight: 600; }
  /* A real <button> inside the th, not a click handler on the th itself: a
     <th> is not focusable and has no activation behaviour, so the sort was
     reachable by mouse only and by nothing else (issue #106). The button
     takes over the cell's padding so the whole header cell stays the hit
     target it was. */
  .garden-table th[data-sort] { padding: 0; white-space: nowrap; }
  .garden-table th[data-sort] button {
    font: inherit; color: inherit; background: transparent; border: 0;
    cursor: pointer; user-select: none; width: 100%;
    padding: 0.4rem 0.5rem;
    /* All three are inherited properties that a UA button stylesheet
       resets, so the header keeps looking like a header. */
    text-align: inherit; text-transform: inherit; letter-spacing: inherit;
  }
  .garden-table th[data-sort] button:hover { color: var(--text); }
  .garden-table th[data-sort] button:focus-visible {
    outline: 2px solid var(--accent); outline-offset: -2px;
  }
  /* Sits beside the label of whichever column is sorted; aria-sort on the
     th carries the same fact for a screen reader. */
  .garden-table th .sort-ind { color: var(--accent); margin-left: 0.25rem; }
  .garden-table th[aria-sort="none"] .sort-ind { visibility: hidden; }
  /* The garden table's header cells are its sort control as well as its
     labels, so the phone card layout's `table thead { display: none }`
     doesn't drop a repetition — it removes the feature outright, on the
     one device this page is written for (issue #120). This is the same
     `gardenSort` state offered from inside the table view, rendered at
     exactly the widths where the header row isn't. Its counterpart rule
     sits beside that `thead` one in the phone block below, so the two
     can't drift onto different breakpoints. */
  .table-sort {
    display: none; align-items: center; gap: 0.4rem 0.6rem;
    flex-wrap: wrap; margin-bottom: 0.7rem;
  }
  .table-sort label { color: var(--muted); font-size: 0.78rem; }
  /* 16px for the same reason .filter-box is: anything smaller makes iOS
     Safari zoom the whole page when the control takes focus. */
  .table-sort select {
    font: inherit; font-size: 16px; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 999px;
    padding: 0.25rem 0.7rem; min-height: 34px; max-width: 12rem;
  }
  .table-sort select:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  /* 34px like the tabs and the filter box, not .chip's 30px: this control
     only ever renders on a touch layout (issue #135's target rule). */
  .table-sort .chip { min-height: 34px; }
  .garden-table td.num, .garden-table th.num { text-align: right; font-variant-numeric: tabular-nums; }
  /* The status dot takes the page's own semantic tokens, not the plant's
     naturalistic leaf colour. Those leaf colours are an illustration and
     are not a severity ramp: measured in CIELab, steady→thriving was 6.7
     apart (effectively one colour, and 60 of 135 repos) while struggling —
     dispatched repeatedly, never once successful — was a muted brown that
     read calmer than wilting, and no bucket ever reached --err
     (issue #131). A 0.6rem unbordered dot is the table's only health
     carrier, so it gets the ramp that actually escalates. */
  .dot {
    display: inline-block; width: 0.6rem; height: 0.6rem; border-radius: 50%;
    margin-right: 0.35rem; vertical-align: baseline;
    background: var(--muted);
  }
  .dot.h-thriving, .dot.h-steady { background: var(--accent); }
  .dot.h-dry, .dot.h-wilting { background: var(--warn); }
  .dot.h-struggling { background: var(--err); }
  .dot.h-unplanted { background: var(--muted); opacity: 0.5; }
  .muted { color: var(--muted); }
  /* Failure triage. One block per distinct reason, repos beneath it. */
  .failure { padding: 0.35rem 0; border-bottom: 1px solid var(--border); }
  .failure:last-child { border-bottom: none; }
  .failure-reason { color: var(--err); font-size: 0.85rem; }
  .failure-repos { font-family: var(--mono); font-size: 0.78rem; margin-top: 0.15rem; }
  .day-sep td {
    color: var(--muted); font-size: 0.72rem; text-transform: uppercase;
    letter-spacing: .04em; padding-top: 0.6rem; border-bottom: none;
  }
  .pd-links { margin: 0.6rem 0 0; display: flex; gap: 0.4rem; flex-wrap: wrap; }
  .pd-links .chip { text-decoration: none; display: inline-flex; align-items: center; }
  a { color: var(--accent); }
  a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  td.repo a { color: inherit; text-decoration: none; }
  td.repo a:hover, td.repo a:focus-visible { text-decoration: underline; }
  pre#log:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  /* Garden filter. 16px on the input is deliberate: anything smaller makes
     iOS Safari zoom the whole page on focus. */
  .filter-box {
    font: inherit; font-size: 16px; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 999px;
    padding: 0.25rem 0.7rem; min-height: 34px; max-width: 15rem; flex: 1 1 8rem;
  }
  .filter-box:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  .chip {
    font: inherit; font-size: 0.75rem; cursor: pointer; color: var(--muted);
    background: transparent; border: 1px solid var(--border);
    border-radius: 999px; padding: 0.1rem 0.6rem; min-height: 30px;
  }
  .chip[aria-pressed="true"] { color: var(--accent); border-color: var(--accent); }
  .chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }

  /* Phone layout. The runs table is the one thing that genuinely can't
     shrink: six columns, one of which is a free-text summary sentence.
     Rather than leave it as a sideways-scrolling strip inside `.panel`
     (readable only a column at a time), each row becomes a card — repo on
     its own line, the short scalar fields as one muted meta line beneath,
     summary last. `td::before` supplies the labels the hidden `thead` was
     carrying, so no cell loses its meaning. */
  @media (max-width: 720px) {
    header { padding: 0.75rem 1rem; }
    main { padding: 1rem; gap: 1rem; }
    .panel { padding: 0.9rem 1rem; border-radius: 12px; }
    table thead { display: none; }
    /* The other half of hiding the header row: the garden table's headers
       are also its only sort control, so that control has to exist
       somewhere else at exactly these widths (issue #120). Sticky within
       #table-view's own scroll box (see below) — the header row it stands
       in for scrolled with the table, but a 135-row garden inside a 70vh
       box puts the control out of reach within one flick otherwise. */
    .table-sort {
      display: flex; position: sticky; top: 0; z-index: 1;
      background: var(--panel); padding: 0.2rem 0;
    }
    table, table tbody, table tr, table td { display: block; width: 100%; }
    table tr {
      display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 0.5rem;
      border: 1px solid var(--border); border-radius: 10px;
      padding: 0.6rem 0.75rem; margin-bottom: 0.6rem;
    }
    table tr:last-child { margin-bottom: 0; }
    table td { border-bottom: none; padding: 0.1rem 0; }
    td::before {
      content: attr(data-label); color: var(--muted);
      font-size: 0.7rem; text-transform: uppercase; letter-spacing: .04em;
      margin-right: 0.3rem;
    }
    td.repo {
      order: 1; flex: 1 0 100%; white-space: normal; word-break: break-all;
      font-size: 0.85rem; font-weight: 600;
    }
    td.repo::before { content: none; }
    td.time, td.mode, td.outcome, td.cost, td.device { order: 2; flex: 0 0 auto; font-size: 0.78rem; }
    td.time::before, td.cost::before { content: none; }
    td.summary { order: 3; flex: 1 0 100%; margin-top: 0.35rem; font-size: 0.85rem; }
    td.summary::before { content: none; }
    /* The empty-state row is a single full-width cell, not a card. */
    tr.is-empty { display: block; border: none; padding: 0; }
    /* Same card treatment for the garden table: repo on its own line,
       the six short scalar cells flowing beneath it as one meta line. */
    .garden-table td { order: 2; flex: 0 0 auto; width: auto; font-size: 0.78rem; }
    .garden-table td.repo { order: 1; flex: 1 0 100%; }
    /* Plants get a little bigger on a phone — a 3-4 column plot at
       ~100px reads as a garden; eight 84px columns read as clip art.
       That reasoning holds for a twelve-repo garden and breaks badly at
       135: at a 360px viewport minmax(96px) fits exactly two columns, so
       the plot became ~68 rows and roughly 16,000px tall — about 25
       screens to flick past to reach the live log, which is the panel
       with the per-second detail during a run (issue #132). 72px gets
       three columns back, and the plot scrolls inside its own box the way
       pre#log already does rather than scrolling the page. */
    .plot { grid-template-columns: repeat(auto-fill, minmax(72px, 1fr)); }
    #plot-view .plot { max-height: 62vh; overflow-y: auto; }
    /* The cell grew at this breakpoint but its captions never did: 9.92px
       for the only per-plant numbers on the page, and 10.88px for the only
       way to tell two plants apart without tapping. -webkit-text-size-
       adjust: 100% on body disables the inflation that would otherwise
       have compensated (issue #135). */
    .plant .nm { font-size: 0.8rem; }
    .plant .meta { font-size: 0.72rem; }
    /* During a run the log is what's being watched, so it comes before a
       garden of 135 plants rather than after it. */
    .garden-panel { order: 3; }
    .log-panel { order: 2; }
    /* Same reasoning as the plot above, applied to the long tables: as
       cards, 40 runs came to ~10,700px and the garden's 135 rows to
       ~8,000px, so the page was ~20 screens tall however tidy each card
       was. Each scrolls in its own box, the way pre#log already does. */
    #runs-panel table, #table-view { max-height: 70vh; overflow-y: auto; }
    /* The history table is five short numeric columns that fit a 360px
       screen as a table. Turning it into 14 stacked cards cost 2,100px to
       show ~70 characters of data, so it opts out of the card layout. */
    .history-table thead { display: table-header-group; }
    .history-table, .history-table tbody, .history-table tr, .history-table td {
      display: revert; width: auto;
    }
    .history-table tr {
      border: none; border-radius: 0; padding: 0; margin: 0;
      border-bottom: 1px solid var(--border);
    }
    .history-table td { border-bottom: 1px solid var(--border); padding: 0.35rem 0.4rem; }
    .history-table td::before { content: none; }
    .history-table { font-size: 0.78rem; }
  }
</style>
</head>
<body class="loading">
<header>
  <h1><span aria-hidden="true">🌱</span> gardener dashboard</h1>
  <span class="sub" id="updated">loading…</span>
  <span id="stale-badge">⚠ stale</span>
  <span class="sub" id="hub-user" hidden></span>
  <a class="sub" id="live-link" href="/live" style="margin-left:auto">live view →</a>
</header>
<!-- The page rewrites every panel on a 4 s poll and had no live region at
     all, so a tend starting, the error count moving, or the page going
     stale were all silent to a screen reader (issue #127). Written only on
     transitions, never on every poll — the heartbeat caption changes each
     time and would babble. -->
<p id="announce" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></p>
<main>
  <div class="band">
    <div class="panel">
      <h2>Latest session <span class="sub" id="session-window"></span></h2>
      <div class="stats" id="stats"></div>
      <!-- Three nested progress numbers, outermost first: the cycle over
           the whole garden, this invocation's time budget, then the batch
           within it. They are different denominators and were previously
           collapsed to the innermost one alone (issue #113). -->
      <div id="cycle"></div>
      <div id="budget"></div>
      <div id="batch"></div>
    </div>
    <div class="panel">
      <h2>Currently tending</h2>
      <div id="in-progress" class="empty">checking…</div>
    </div>
  </div>
  <div class="panel wide" id="failures-panel" hidden>
    <h2>Failures this session <span class="sub" id="failures-window"></span></h2>
    <div id="failures"></div>
  </div>
  <div class="panel wide garden-panel">
    <h2 id="garden-heading">Garden</h2>
    <div class="toolbar">
      <div class="tabs" role="tablist" aria-label="Garden view">
        <button class="tab" id="tab-plot" role="tab" aria-selected="true"
                aria-controls="plot-view" tabindex="0"><span aria-hidden="true">🌿</span> Plot</button>
        <button class="tab" id="tab-table" role="tab" aria-selected="false"
                aria-controls="table-view" tabindex="-1"><span aria-hidden="true">▤</span> Table</button>
      </div>
      <input type="search" class="filter-box" id="garden-filter" autocomplete="off"
             placeholder="filter repos…" aria-label="Filter garden by repo name">
      <span class="sub" id="garden-summary"></span>
    </div>
    <div id="plot-view" role="tabpanel" aria-labelledby="tab-plot" tabindex="0">
      <div class="plot" id="plot"></div>
      <div id="plant-detail" class="plant-detail" hidden></div>
      <details class="legend">
        <summary>How a plant is drawn</summary>
        <ul>
          <li><b>Height &amp; leaves</b> — successful tends, all-time</li>
          <li><b>Colour &amp; droop</b> — days since the last successful tend</li>
          <li><b>Short brown stem, one drooping leaf</b> — dispatched before, but never successfully</li>
          <li><b>Blossom</b> — on the merge allow-list, so a tend may merge its own PR</li>
          <li><b>Fallen brown leaves</b> — recorded errors</li>
          <li><b>Soil mound</b> — dollars spent tending it</li>
          <li><b>Terracotta pot</b> — allow-listed but not in the garden, so <code>overnight</code> never plants it</li>
          <li><b>Glow</b> — being tended right now</li>
        </ul>
      </details>
    </div>
    <div id="table-view" role="tabpanel" aria-labelledby="tab-table" tabindex="0" hidden>
      <!-- Inside the table view rather than in the panel toolbar above:
           sorting only affects this view (the plot orders itself by
           attention), so a sort control beside the view tabs would sit
           there doing nothing whenever the plot is the one on screen.
           The options are built from the header cells at load, never
           written out here a second time — see buildGardenSortOptions. -->
      <div class="table-sort">
        <label for="garden-sort">Sort by</label>
        <select id="garden-sort"></select>
        <button type="button" class="chip" id="garden-sort-dir"
                aria-label="Sorted ascending. Activate to sort descending.">
          <span aria-hidden="true" id="garden-sort-dir-glyph">▲</span>
          <span id="garden-sort-dir-label">ascending</span>
        </button>
      </div>
      <table class="garden-table" role="table">
        <caption class="sr-only">Every opted-in repo with its health, tend and error counts, cost and merge eligibility</caption>
        <thead><tr>
          <th scope="col" data-sort="repo" aria-sort="none"><button type="button">Repo<span class="sort-ind" aria-hidden="true">▲</span></button></th>
          <th scope="col" data-sort="health" aria-sort="none"><button type="button">Health<span class="sort-ind" aria-hidden="true">▲</span></button></th>
          <th scope="col" data-sort="successes" aria-sort="none" class="num"><button type="button">Tends<span class="sort-ind" aria-hidden="true">▲</span></button></th>
          <th scope="col" data-sort="errors" aria-sort="none" class="num"><button type="button">Errors<span class="sort-ind" aria-hidden="true">▲</span></button></th>
          <th scope="col" data-sort="last_success" aria-sort="none"><button type="button">Last tended<span class="sort-ind" aria-hidden="true">▲</span></button></th>
          <th scope="col" data-sort="duration_ms" aria-sort="none" class="num"><button type="button">Time<span class="sort-ind" aria-hidden="true">▲</span></button></th>
          <th scope="col" data-sort="cost_usd" aria-sort="none" class="num"><button type="button">Cost<span class="sort-ind" aria-hidden="true">▲</span></button></th>
          <th scope="col" data-sort="can_merge" aria-sort="none"><button type="button">Merge<span class="sort-ind" aria-hidden="true">▲</span></button></th>
        </tr></thead>
        <tbody id="garden-rows"></tbody>
      </table>
    </div>
  </div>
  <div class="panel wide" id="runs-panel">
    <h2>Recent runs <span class="sub" id="runs-window"></span></h2>
    <table role="table">
      <caption class="sr-only">The most recent dispatched runs, newest first</caption>
      <thead><tr>
        <th scope="col">Time</th><th scope="col">Repo</th><th scope="col">Mode</th>
        <th scope="col">Outcome</th><th scope="col">Time taken</th><th scope="col">Cost</th>
        <th scope="col">Summary</th>
        <th scope="col" class="device-col" hidden>Device</th>
      </tr></thead>
      <tbody id="runs"></tbody>
    </table>
  </div>
  <div class="panel wide log-panel">
    <!-- The picker sits *beside* the heading rather than inside it, laid
         out on the same line by .log-head. A <select> inside an <h2>
         contributes its selected option to that heading's accessible
         name, so heading-by-heading navigation would announce "Live log,
         which live log to tail, tend-….log, (/full/path/tend-….log)" —
         the filename twice, inside a name that is supposed to be a label.
         The picker is hidden outright while only one log is live, so the
         ordinary single-run case renders exactly as it did before it
         existed: a heading and the tailed file's path. -->
    <div class="log-head">
      <h2>Live log <span class="sub" id="log-path"></span></h2>
      <label class="log-pick" id="log-pick-wrap" hidden>
        <span class="sr-only">Which live log to tail</span>
        <select id="log-pick"></select>
      </label>
    </div>
    <!-- role="log" so the append is announced as an updating log rather
         than silently, and tabindex so a scrollable region has a focusable
         owner — without one it is unreachable by keyboard in Safari. -->
    <pre id="log" role="log" aria-label="Live log" tabindex="0"></pre>
  </div>
  <div class="panel wide">
    <h2>Per-night history</h2>
    <table class="history-table" role="table">
      <caption class="sr-only">Runs, errors, cost and time spent per calendar day</caption>
      <thead><tr>
        <th scope="col">Day</th><th scope="col">Runs</th><th scope="col">Errors</th>
        <th scope="col">Cost</th><th scope="col">Time</th>
      </tr></thead>
      <tbody id="history"></tbody>
    </table>
  </div>
</main>
<script>
// Quotes are escaped as well as &<> because this helper is used for
// attribute values (the plot's `title=`), not just text nodes — &<>
// alone would let a name containing a quote close the attribute early.
// Escaping them is harmless in text context, so one helper covers both.
function esc(s) {
  return (s ?? "").replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}[c]));
}
function fmtCost(c) { return c == null ? "—" : "$" + c.toFixed(2); }
// Adaptive, because the same formatter now covers both ends of the range
// it was written for and never used at: a single tend is ~700-1000s, while
// a session total is hours. A flat seconds rendering would print a night
// as "10976s".
function fmtDur(ms) {
  if (ms == null) return "—";
  const s = ms / 1000;
  if (s < 90) return s.toFixed(0) + "s";
  if (s < 5400) return (s / 60).toFixed(0) + "m";
  return (s / 3600).toFixed(1) + "h";
}
// The try/catch this replaces could never fire: toLocaleTimeString returns
// the *string* "Invalid Date" for an unparseable input rather than
// throwing, so the `return ts` fallback was unreachable and a bad
// timestamp rendered as "Invalid Date" (issue #125).
function shortTime(ts) {
  if (!ts) return "—";
  if (isNaN(Date.parse(ts))) return String(ts);
  return new Date(ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
}
// GitHub URL for a repo, and for an issue/PR number within it. GitHub
// redirects /issues/<n> to the pull request when the number is a PR, so
// one form covers both without gardener having to record which it was.
function repoUrl(repo) { return "https://github.com/" + encodeURI(repo); }
function issueUrl(repo, n) { return repoUrl(repo) + "/issues/" + encodeURIComponent(n); }
function repoLink(repo) {
  return `<a href="${esc(repoUrl(repo))}" target="_blank" rel="noopener noreferrer">${esc(repo)}</a>`;
}
// Run summaries are full of "PR #194 opened" and "13 issue(s) filed" — the
// whole reason the page is open is to go and look at those, and nothing on
// the page was a link at all (issue #140).
//
// The raw string is tokenized and each literal run escaped separately,
// rather than escaping first and matching over the result. Matching
// afterwards finds the digits *inside* numeric character references: esc
// turns an apostrophe into `&#39;`, so "You've hit your session limit" —
// a real summary in the live history — matched `#39` and rendered as
// `You&#39;ve` with a link through the middle of the entity.
function linkifyRefs(summary, repo) {
  const s = summary == null ? "" : String(summary);
  if (!repo) return esc(s);
  let out = "", last = 0, m;
  const re = /#(\\d+)/g;
  while ((m = re.exec(s)) !== null) {
    out += esc(s.slice(last, m.index))
        + `<a href="${esc(issueUrl(repo, m[1]))}" target="_blank" rel="noopener noreferrer">`
        + esc(m[0]) + `</a>`;
    last = m.index + m[0].length;
  }
  return out + esc(s.slice(last));
}

/* ---------------------------------------------------------------------
   The garden: one row per opted-in repo, drawn two ways.

   Every visual property below is a real number out of gardener's own run
   history — nothing here is decoration for its own sake. The mapping is
   spelled out in the page's own legend, so what you see can always be
   traced back to a column. See build_garden_rows() for the row shape.
   --------------------------------------------------------------------- */
const SOIL_Y = 112, BASE_X = 50;

// leaf/stem are var() references, not hexes: half the plot already themed
// itself and half didn't, so in light mode the buckets collapsed to within
// 0.3 of each other at ~2.7:1 (issue #131). See the :root palette for the
// values and why the light ramp is darker.
const HEALTH = {
  thriving:   {label: "thriving",   leaf: "var(--leaf-thriving)",   stem: "var(--stem-thriving)",   droop: 0.00},
  steady:     {label: "steady",     leaf: "var(--leaf-steady)",     stem: "var(--stem-steady)",     droop: 0.15},
  dry:        {label: "dry",        leaf: "var(--leaf-dry)",        stem: "var(--stem-dry)",        droop: 0.45},
  wilting:    {label: "wilting",    leaf: "var(--leaf-wilting)",    stem: "var(--stem-wilting)",    droop: 0.85},
  struggling: {label: "struggling", leaf: "var(--leaf-struggling)", stem: "var(--stem-struggling)", droop: 0.70},
  unplanted:  {label: "not tended", leaf: "var(--leaf-unplanted)",  stem: "var(--stem-unplanted)",  droop: 0.00},
};
// Worst first, so sorting the Health column ascending surfaces the repos
// that need attention rather than the ones that don't.
const HEALTH_ORDER = ["unplanted", "struggling", "wilting", "dry", "steady", "thriving"];

function healthOf(row) {
  if (!row.runs) return "unplanted";
  if (!row.last_success) return "struggling";
  const d = daysSince(row.last_success);
  // `null < 2` is true, so a truthy-but-unparseable last_success used to
  // fall straight through to "thriving" — the healthiest plant in the
  // garden, with "never" printed on its own caption, sorted to the healthy
  // end of the column that exists to surface trouble (issue #125).
  if (d == null) return "struggling";
  if (d < 2) return "thriving";
  if (d < 5) return "steady";
  if (d < 10) return "dry";
  return "wilting";
}

function daysSince(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return isNaN(t) ? null : (Date.now() - t) / 86400000;
}
function fmtAge(d) {
  if (d == null) return "never";
  if (d < 1 / 24) return "just now";
  if (d < 1) return Math.round(d * 24) + "h ago";
  return Math.round(d) + "d ago";
}

// Deterministic per-repo jitter. Every plant gets its own lean, leaf
// lengths and petal rotation so the plot doesn't look stamped out — but
// seeded from the repo name, never Math.random(), so a plant keeps the
// same shape across re-renders and only changes when its data does.
function hashName(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
function rngFrom(seed) {
  let s = seed || 1;
  return () => { s = (Math.imul(s, 1664525) + 1013904223) >>> 0; return s / 4294967296; };
}

function n1(v) { return v.toFixed(1); }

// A leaf as two mirrored quadratic curves from its attachment point out
// to its tip — a lens shape, wider in the middle.
function leafPath(x, y, dx, dy) {
  const tx = x + dx, ty = y + dy;
  const len = Math.hypot(dx, dy) || 1;
  const w = len * 0.3;
  const nx = -dy / len * w, ny = dx / len * w;
  const mx = x + dx / 2, my = y + dy / 2;
  return `M${n1(x)},${n1(y)} Q${n1(mx + nx)},${n1(my + ny)} ${n1(tx)},${n1(ty)}`
       + ` Q${n1(mx - nx)},${n1(my - ny)} ${n1(x)},${n1(y)}Z`;
}

// Point at parameter t on the quadratic the stem is drawn with, so
// leaves attach to the stem instead of floating beside it.
function onStem(t, cx, cy, tx, ty) {
  const u = 1 - t;
  return [u * u * BASE_X + 2 * u * t * cx + t * t * tx,
          u * u * SOIL_Y + 2 * u * t * cy + t * t * ty];
}

function plantSvg(row, maxCost) {
  const rnd = rngFrom(hashName(row.repo));
  const key = healthOf(row);
  const h = HEALTH[key];
  const parts = [];

  if (row.in_flight) {
    parts.push(`<circle class="glow" cx="50" cy="86" r="34" fill="var(--accent)" opacity="0.18"/>`);
  }

  // Ground: a mound of soil whose width is the dollars this repo has
  // cost, or a pot when it is allow-listed but not actually planted.
  const spend = maxCost > 0 ? Math.min(1, row.cost_usd / maxCost) : 0;
  if (row.in_garden) {
    const rx = 15 + 17 * spend;
    parts.push(`<ellipse cx="50" cy="${SOIL_Y + 4}" rx="${n1(rx)}" ry="5.5" fill="var(--soil)"/>`);
  } else {
    parts.push(`<path d="M36,${SOIL_Y} L64,${SOIL_Y} L60,${SOIL_Y + 15} L40,${SOIL_Y + 15}Z" fill="var(--pot)"/>`
             + `<rect x="34" y="${SOIL_Y - 5}" width="32" height="6" rx="2" fill="var(--pot)"/>`);
  }

  if (row.runs === 0) {
    // Never dispatched: a seed in the ground, nothing above it yet.
    parts.push(`<ellipse cx="50" cy="${SOIL_Y + 2}" rx="3.4" ry="2.6" fill="var(--seed)"/>`);
  } else {
    const maturity = Math.min(row.successes, 16) / 16;
    const height = 16 + maturity * 16 * 4.3;
    const lean = (rnd() - 0.5) * 18;
    // Droop bends the tip over, but only so far: scaled by the full droop
    // a struggling repo's short stem ends up lying flat on the soil,
    // which reads as debris rather than as a plant in trouble.
    const topX = BASE_X + lean * (0.55 + h.droop * 0.35);
    const topY = SOIL_Y - height * (1 - h.droop * 0.18);
    const cx = BASE_X + lean * 0.35, cy = SOIL_Y - height * 0.55;
    const thick = 1.3 + Math.min(row.successes, 12) * 0.13;
    parts.push(`<path d="M50,${SOIL_Y} Q${n1(cx)},${n1(cy)} ${n1(topX)},${n1(topY)}"`
             + ` fill="none" stroke="${h.stem}" stroke-width="${n1(thick)}" stroke-linecap="round"/>`);

    const pairs = Math.min(1 + Math.floor(row.successes / 2), 6);
    for (let i = 0; i < pairs; i++) {
      const t = (i + 1) / (pairs + 0.7) + (rnd() - 0.5) * 0.06;
      const [px, py] = onStem(t, cx, cy, topX, topY);
      const side = i % 2 === 0 ? -1 : 1;
      // Leaves lift toward the light when the repo is freshly tended and
      // hang down as it goes untended — that is the whole droop signal.
      const angle = 0.62 - h.droop * 1.5 + (rnd() - 0.5) * 0.3;
      const len = 9 + Math.min(row.successes, 10) * 0.85 + rnd() * 3;
      parts.push(`<path d="${leafPath(px, py, side * len * Math.cos(angle), -len * Math.sin(angle))}"`
               + ` fill="${h.leaf}" opacity="${n1(0.78 + rnd() * 0.22)}"/>`);
    }

    // A blossom means this repo is on the merge allow-list: it is
    // allowed to bear fruit on its own, i.e. merge its own PRs. Which
    // flower it is, is decoration — deterministic per repo so the garden
    // isn't 32 identical stamps, but it carries no meaning of its own.
    if (row.can_merge && row.successes > 0) {
      const blooms = ["var(--bloom-1)", "var(--bloom-2)", "var(--bloom-3)",
                      "var(--bloom-4)", "var(--bloom-5)"];
      const tint = h.droop > 0.5 ? "var(--bloom-faded)" : blooms[Math.floor(rnd() * blooms.length)];
      const petals = 5 + Math.floor(rnd() * 2);
      // Petal size follows the plant: a full-size flower on a one-tend
      // sprout reads as top-heavy clip art rather than a young plant.
      const pr = 1.9 + maturity * 1.4;
      const spin = rnd() * 6.28;
      for (let k = 0; k < petals; k++) {
        const a = spin + k * (6.2832 / petals);
        parts.push(`<circle cx="${n1(topX + Math.cos(a) * pr * 1.15)}" cy="${n1(topY + Math.sin(a) * pr * 1.15)}"`
                 + ` r="${n1(pr)}" fill="${tint}" opacity="0.92"/>`);
      }
      parts.push(`<circle cx="${n1(topX)}" cy="${n1(topY)}" r="${n1(pr * 0.66)}" fill="var(--bloom-centre)"/>`);
    }

    // Grass at the foot of an established plant. Decoration, like the
    // lean and the leaf jitter — it makes a bed of plants look planted
    // rather than pasted, and encodes nothing.
    if (row.in_garden && row.successes > 2) {
      for (let i = 0; i < 3; i++) {
        const gx = 50 + (rnd() - 0.5) * 30, gh = 4 + rnd() * 5;
        parts.push(`<path d="M${n1(gx)},${n1(SOIL_Y + 2)} Q${n1(gx + (rnd() - 0.5) * 4)},${n1(SOIL_Y + 2 - gh * 0.6)} `
                 + `${n1(gx + (rnd() - 0.5) * 8)},${n1(SOIL_Y + 2 - gh)}" fill="none" stroke="${h.leaf}"`
                 + ` stroke-width="0.9" opacity="0.5" stroke-linecap="round"/>`);
      }
    }
  }

  // Errors that this repo actually recorded, as leaf litter on the soil.
  for (let i = 0; i < Math.min(row.errors, 6); i++) {
    const lx = 26 + rnd() * 48, ly = SOIL_Y + 1 + rnd() * 6;
    parts.push(`<ellipse cx="${n1(lx)}" cy="${n1(ly)}" rx="3.4" ry="1.5" fill="var(--fallen)"`
             + ` transform="rotate(${n1((rnd() - 0.5) * 60)} ${n1(lx)} ${n1(ly)})"/>`);
  }

  return `<svg viewBox="0 0 100 130" aria-hidden="true">${parts.join("")}</svg>`;
}

let gardenSort = {key: "repo", dir: 1};
let gardenRows = [];
let lastPlotSig = null;
// Which plant's detail card is open, by repo name rather than by element:
// the plot's innerHTML is rebuilt whenever its signature changes, so an
// element reference wouldn't survive a re-render.
let selectedPlant = null;

// Seconds/minutes granularity, unlike fmtAge's days — this describes how
// old the snapshot on screen is during a run of failed 4 s polls, where
// "just now" would be wrong within half a minute.
function fmtSince(iso) {
  const t = Date.parse(iso ?? "");
  if (isNaN(t)) return "unknown age";
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
}

// Substring match over the full owner/name, applied to both views. With
// 135 rows the plot is ~135 tiles whose only visible label is the short
// name, and four of those short names are ambiguous across owners, so
// there was no way to find a named repo at all (issue #139).
let gardenFilter = "";
function filteredGardenRows() {
  if (!gardenFilter) return gardenRows;
  const needle = gardenFilter.toLowerCase();
  return gardenRows.filter(r => r.repo.toLowerCase().includes(needle));
}

// Keys are precomputed once per sort rather than inside the comparator:
// healthOf + Date.parse per comparison is ~1800 calls per poll at n=135,
// on a table that re-sorts on every render.
function sortedGardenRows(rows) {
  const k = gardenSort.key;
  const val = r =>
    k === "repo" ? r.repo.toLowerCase()
    : k === "health" ? HEALTH_ORDER.indexOf(healthOf(r))
    : k === "last_success" ? (r.last_success ? Date.parse(r.last_success) || 0 : 0)
    : k === "can_merge" ? (r.can_merge ? 1 : 0)
    : (r[k] ?? 0);
  return (rows || filteredGardenRows())
    .map(r => ({r, key: val(r)}))
    .sort((a, b) => {
      if (a.key < b.key) return -1 * gardenSort.dir;
      if (a.key > b.key) return 1 * gardenSort.dir;
      return a.r.repo.localeCompare(b.r.repo);
    })
    .map(x => x.r);
}

// Plot order: attention first, alphabetical within each band so plants
// don't jump between polls. Alphabetical-only put the two in-flight repos
// wherever the alphabet happened to place them — 1.5% of 135 tiles, marked
// by a 1px border, routinely below the fold on the default tab (#132).
// The docstring justifying name order cited a rebuild "every 4 s poll",
// which lastPlotSig stopped being true.
const PLOT_BAND = {struggling: 1, wilting: 2, dry: 3, steady: 4, thriving: 5, unplanted: 6};
function plotOrderedRows(rows) {
  return rows.slice().sort((a, b) => {
    if (a.in_flight !== b.in_flight) return a.in_flight ? -1 : 1;
    const ba = PLOT_BAND[healthOf(a)] || 9, bb = PLOT_BAND[healthOf(b)] || 9;
    if (ba !== bb) return ba - bb;
    return a.repo.localeCompare(b.repo);
  });
}

// The <thead> is static markup that renderGardenTable never used to touch,
// so nothing on the page said which column produced the order on screen —
// and `dir` toggles on every repeat click, so "sort Health to find what
// needs water" silently produced the exact inverse on the second click
// (issue #106). aria-sort carries it for a screen reader, the caret for
// everyone else. The caret is present but hidden on the inactive columns
// rather than absent, so sorting doesn't shift the header widths.
function renderGardenSortHeaders() {
  for (const th of document.querySelectorAll(".garden-table th[data-sort]")) {
    const active = th.dataset.sort === gardenSort.key;
    const ascending = gardenSort.dir === 1;
    th.setAttribute("aria-sort", !active ? "none" : ascending ? "ascending" : "descending");
    const ind = th.querySelector(".sort-ind");
    if (ind) ind.textContent = active && !ascending ? "▼" : "▲";
  }
  syncGardenSortControl();
}

// The narrow-viewport half of the same header. Called from
// renderGardenSortHeaders rather than from its own render path, so the
// header cells and this control are written from one read of `gardenSort`
// and cannot end up describing different orders (issue #120).
function syncGardenSortControl() {
  const select = document.getElementById("garden-sort");
  if (select && select.value !== gardenSort.key) select.value = gardenSort.key;
  const button = document.getElementById("garden-sort-dir");
  if (!button) return;
  const ascending = gardenSort.dir === 1;
  const glyph = document.getElementById("garden-sort-dir-glyph");
  const label = document.getElementById("garden-sort-dir-label");
  if (glyph) glyph.textContent = ascending ? "▲" : "▼";
  if (label) label.textContent = ascending ? "ascending" : "descending";
  // The caret is aria-hidden, so the accessible name has to carry the
  // direction in words as well as what activating the button will do —
  // a name of "▲" announces as nothing useful (issue #130).
  button.setAttribute("aria-label", ascending
    ? "Sorted ascending. Activate to sort descending."
    : "Sorted descending. Activate to sort ascending.");
}

// One entry point for every control that changes the order, so a second
// control can't set the state without the first one being re-rendered
// from it.
function setGardenSort(key, dir) {
  gardenSort = {key, dir};
  renderGardenTable();
}

// Same change-detection the plot has had. Without it the tbody was
// replaced wholesale 15 times a minute with identical content, which
// destroys any text selection inside it within 4 s — selecting a repo name
// or a summary to paste into an issue was not reliably possible, and on
// Android the selection handles vanish mid-gesture (issue #126).
let lastGardenTableSig = null;
function renderGardenTable() {
  renderGardenSortHeaders();
  const rows = sortedGardenRows();
  const sig = JSON.stringify([gardenSort, gardenFilter, rows.map(r =>
    [r.repo, r.successes, r.errors, r.cost_usd, r.duration_ms, r.can_merge,
     r.in_garden, r.in_flight, healthOf(r), fmtAge(daysSince(r.last_success))])]);
  if (sig === lastGardenTableSig) return;
  lastGardenTableSig = sig;
  // role="row"/"cell" are re-asserted explicitly because the ≤720px card
  // layout sets display:block on table/tr/td, which strips the implicit
  // table roles in every engine — leaving both tables announced as an
  // undifferentiated run of text on a phone (issue #129).
  document.getElementById("garden-rows").innerHTML = rows.map(r => {
    const key = healthOf(r);
    const h = HEALTH[key];
    const age = fmtAge(daysSince(r.last_success));
    return `<tr role="row">
      <td class="repo" role="rowheader" data-label="Repo">${repoLink(r.repo)}${r.in_garden ? "" : ` <span class="muted">(not planted)</span>`}</td>
      <td role="cell" data-label="Health"><span class="dot h-${esc(key)}"></span>${r.in_flight ? `<span class="pill live">tending now</span>` : esc(h.label)}</td>
      <td class="num" role="cell" data-label="Tends">${r.successes}</td>
      <td class="num ${r.errors ? "outcome-error" : "muted"}" role="cell" data-label="Errors">${r.errors}</td>
      <td role="cell" data-label="Last tended">${esc(age)}</td>
      <td class="num" role="cell" data-label="Time">${esc(fmtDur(r.duration_ms))}</td>
      <td class="num" role="cell" data-label="Cost">${fmtCost(r.cost_usd)}</td>
      <td role="cell" data-label="Merge">${r.can_merge
        ? `<span aria-hidden="true">✓</span> allowed`
        : `<span class="muted"><span aria-hidden="true">—</span><span class="sr-only">not allow-listed</span></span>`}</td>
    </tr>`;
  }).join("") || `<tr class="is-empty" role="row"><td colspan="8" class="empty" role="cell">${
    gardenFilter ? "no repo matches this filter"
                 : "nothing opted in — <code>gardener garden add --repo owner/name</code>"}</td></tr>`;
}

// A plant's caption is its short name, which stops identifying it as soon
// as two owners have a repo of the same name — the live garden has four
// such pairs after repos moved between orgs, drawn as indistinguishable
// plants (issue #139). Only the ambiguous ones pay the width cost.
let ambiguousShortNames = new Set();
function shortName(repo) { return repo.split("/").pop(); }
function plantLabel(repo) {
  return ambiguousShortNames.has(shortName(repo)) ? repo : shortName(repo);
}
function recomputeAmbiguousNames() {
  const seen = new Map();
  for (const r of gardenRows) {
    const s = shortName(r.repo);
    seen.set(s, (seen.get(s) || 0) + 1);
  }
  ambiguousShortNames = new Set([...seen].filter(([, n]) => n > 1).map(([s]) => s));
}

function renderGarden(rows) {
  gardenRows = rows || [];
  recomputeAmbiguousNames();
  document.getElementById("garden-heading").textContent = `Garden (${gardenRows.length})`;

  const counts = {};
  for (const r of gardenRows) { const k = healthOf(r); counts[k] = (counts[k] || 0) + 1; }
  const needsWater = (counts.dry || 0) + (counts.wilting || 0) + (counts.struggling || 0);
  const mergeable = gardenRows.filter(r => r.can_merge).length;
  const potted = gardenRows.filter(r => !r.in_garden).length;
  // `counts.unplanted` was computed here and then dropped — partly because
  // the variable that used to be named `unplanted` meant something else
  // entirely (allow-listed but not in the garden, now `potted`). So the
  // single largest fact about a 135-repo garden, that 72 of them have
  // never been dispatched at all, appeared nowhere on the page (#139).
  const neverTended = counts.unplanted || 0;
  const shown = filteredGardenRows().length;
  document.getElementById("garden-summary").textContent = gardenRows.length
    ? (gardenFilter ? `${shown} of ${gardenRows.length} shown · ` : "")
      + `${counts.thriving || 0} thriving · ${needsWater} needing water`
      + (neverTended ? ` · ${neverTended} never tended` : "")
      + ` · ${mergeable} may merge`
      + (potted ? ` · ${potted} allow-listed but not planted` : "")
    : "";

  // Re-render the plot only when something it draws actually changed —
  // otherwise the 4 s poll would restart the in-flight glow animation
  // from zero every time, and rebuild 32 SVGs for nothing.
  // The rendered age string is in the signature, not just healthOf's
  // 2/5/10-day buckets: the age is visible text on every plant, and is
  // also its button's accessible name now, so a plot left open overnight
  // would otherwise keep saying "just now" hours later while the table
  // view — re-rendered unconditionally — said "3h ago" for the same repo.
  const plotRows = plotOrderedRows(filteredGardenRows());
  const sig = JSON.stringify(plotRows.map(r =>
    [r.repo, r.successes, r.errors, r.cost_usd, r.can_merge, r.in_garden, r.in_flight,
     healthOf(r), fmtAge(daysSince(r.last_success))]));
  if (sig !== lastPlotSig) {
    lastPlotSig = sig;
    // The plot is rebuilt wholesale, so a plant that currently has the
    // keyboard would drop focus to the document every time any repo's
    // drawn data changed — which during a run is often.
    const focused = document.activeElement;
    const refocus = focused && focused.matches && focused.matches("#plot .plant[data-repo]")
      ? focused.dataset.repo : null;
    const maxCost = gardenRows.reduce((m, r) => Math.max(m, r.cost_usd), 0);
    document.getElementById("plot").innerHTML = plotRows.map(r => {
      const h = HEALTH[healthOf(r)];
      const age = fmtAge(daysSince(r.last_success));
      const cls = ["plant", r.in_flight ? "live" : "", r.in_garden ? "" : "potted"].filter(Boolean).join(" ");
      const title = `${r.repo} — ${h.label}, ${r.successes} tend(s), ${r.errors} error(s), `
        + `last tended ${age}, ${fmtCost(r.cost_usd)}${r.can_merge ? ", may merge" : ""}`;
      // A button, not a div: it must be focusable, activatable with
      // Enter/Space, and carry an accessible name, since the SVG inside
      // it is aria-hidden and the visible text is only the leaf name.
      return `<button type="button" class="${cls}" data-repo="${esc(r.repo)}" title="${esc(title)}"
        aria-label="${esc(title)}" aria-expanded="false" aria-controls="plant-detail">
        ${plantSvg(r, maxCost)}
        <div class="nm">${esc(plantLabel(r.repo))}</div>
        <div class="meta">${r.successes}✓${r.errors ? " " + r.errors + "✗" : ""} · ${esc(age)}</div>
      </button>`;
    }).join("") || `<div class="empty">nothing planted yet</div>`;
    // preventScroll: the rebuild is driven by a poll, not by the reader.
    // Scrolling the garden panel back into view under someone who has
    // scrolled down to the log tail would be the poll stealing the page.
    if (refocus) focusPlant(refocus, true);
  }

  // Unconditional, even when the plot itself didn't re-render: the detail
  // card shows fields (last_run, last_outcome) that aren't part of the
  // plot signature, so they'd otherwise freeze at whatever they were when
  // the drawing last changed.
  if (selectedPlant && !gardenRows.some(r => r.repo === selectedPlant)) selectedPlant = null;
  syncPlantSelection();

  renderGardenTable();
}

// Matched by walking the plants rather than by an attribute selector, so
// a repo name never has to be escaped into a selector string.
function focusPlant(repo, preventScroll) {
  for (const el of document.querySelectorAll("#plot .plant[data-repo]")) {
    if (el.dataset.repo === repo) { el.focus({preventScroll: !!preventScroll}); return; }
  }
}

// The plot's per-plant detail. Everything here is already in the row the
// plot is drawn from; before this it was reachable only as a `title`
// tooltip, i.e. not at all on a touch device (issue #114).
let lastDetailHtml = null;
function renderPlantDetail() {
  const el = document.getElementById("plant-detail");
  const r = gardenRows.find(x => x.repo === selectedPlant);
  if (!r) {
    el.hidden = true;
    el.innerHTML = "";
    lastDetailHtml = null;
    return;
  }
  const h = HEALTH[healthOf(r)];
  const lastAttempt = r.last_run
    ? fmtAge(daysSince(r.last_run)) + (r.last_outcome ? " · " + r.last_outcome : "")
    : "never dispatched";
  // `last_run`/`last_outcome` were the two row fields no view rendered.
  // "the most recent attempt errored" is a different fact from "the last
  // success was three days ago", and the more urgent of the two.
  const facts = [
    ["Health", r.in_flight ? "tending now" : h.label, r.in_flight ? "outcome-tend" : ""],
    ["Tends", `${r.successes} of ${r.runs} run(s)`, ""],
    ["Errors", String(r.errors), r.errors ? "outcome-error" : "muted"],
    ["Last tended", fmtAge(daysSince(r.last_success)), ""],
    ["Last attempt", lastAttempt, r.last_outcome === "error" ? "outcome-error" : ""],
    ["Time spent", fmtDur(r.duration_ms), ""],
    ["Cost", fmtCost(r.cost_usd), ""],
    ["Merge", r.can_merge ? "allow-listed — a tend may merge its own PR" : "not allow-listed", ""],
    ["Planted", r.in_garden ? "in the garden" : "allow-listed, but not in the garden", ""],
  ];
  // The card reported "17 errors" and offered no route to any of them —
  // the clearest dead end on the page (issue #138). "Show runs" refetches
  // the payload filtered to this repo, so the Recent runs table below
  // becomes this repo's own history.
  const html = `<div class="pd-head"><b>${esc(r.repo)}</b>`
    + `<button type="button" class="pd-close" data-close="1" aria-label="Close details">`
    + `<span aria-hidden="true">✕</span></button></div>`
    + `<dl>${facts.map(([k, v, cls]) =>
        `<dt>${esc(k)}</dt><dd class="${cls}">${esc(v)}</dd>`).join("")}</dl>`
    + `<p class="pd-links">`
    + `<button type="button" class="chip" data-show-runs="${esc(r.repo)}"`
    + ` aria-pressed="${String(runsRepoFilter === r.repo)}">Show this repo's runs</button> `
    + `<a class="chip" href="${esc(repoUrl(r.repo))}" target="_blank" rel="noopener noreferrer">Open on GitHub</a>`
    + `</p>`;
  // Rewritten only when it actually changed. renderGarden runs on every
  // 4 s poll, so an unconditional innerHTML would take focus off the
  // close button once a poll and restart any transition on the card.
  if (html !== lastDetailHtml) {
    // It does still change on its own — `in_flight` flips when a tend
    // starts or ends, and the counts move when one finishes — so the
    // close button has to be put back if it was where the keyboard was.
    const hadFocus = el.contains(document.activeElement);
    el.innerHTML = html;
    lastDetailHtml = html;
    if (hadFocus) {
      const close = el.querySelector("[data-close]");
      if (close) close.focus({preventScroll: true});
    }
  }
  // A region with a name, so the card announces what it is rather than
  // being an unlabelled div 135 buttons downstream of its trigger.
  el.setAttribute("role", "region");
  el.setAttribute("aria-label", r.repo + " details");
  el.tabIndex = -1;
  el.hidden = false;
}

// `moveFocus` is set only when a reader actually opened the card, never on
// a poll-driven re-render. One shared card serves every plant and sits
// after the whole plot, so activating the first of 135 buttons announced
// "expanded" and left the reader 134 buttons away from the content that
// appeared (issue #133). The close side already restored focus correctly,
// which is what made the asymmetry look unintended.
function syncPlantSelection(moveFocus) {
  for (const el of document.querySelectorAll("#plot .plant[data-repo]")) {
    const on = el.dataset.repo === selectedPlant;
    el.setAttribute("aria-expanded", String(on));
    el.classList.toggle("selected", on);
  }
  renderPlantDetail();
  if (moveFocus && selectedPlant) {
    const card = document.getElementById("plant-detail");
    if (!card.hidden) card.focus({preventScroll: true});
  }
}

// Delegated, because both the plot and the detail card are replaced
// wholesale by innerHTML — a listener bound to a plant would be discarded
// on the next re-render.
document.getElementById("plot").addEventListener("click", ev => {
  const btn = ev.target.closest(".plant[data-repo]");
  if (!btn) return;
  selectedPlant = selectedPlant === btn.dataset.repo ? null : btn.dataset.repo;
  syncPlantSelection(true);
});
document.getElementById("plant-detail").addEventListener("click", ev => {
  const showRuns = ev.target.closest("[data-show-runs]");
  if (showRuns) {
    const repo = showRuns.dataset.showRuns;
    runsRepoFilter = runsRepoFilter === repo ? null : repo;
    lastDetailHtml = null;   // the chip's aria-pressed just changed
    refresh(true);
    return;
  }
  if (!ev.target.closest("[data-close]")) return;
  const previous = selectedPlant;
  selectedPlant = null;
  syncPlantSelection();
  // Focus goes back to the plant that opened the card, not to the top of
  // the document — closing a disclosure shouldn't lose the reader's place.
  focusPlant(previous);
});

// Roving tabindex: only the selected tab is in the page's tab order, so
// Tab steps past the whole tablist and Left/Right move within it. That,
// plus aria-controls/role="tabpanel" in the markup, is what the declared
// role="tablist" was promising and not implementing (issue #116).
function setTabState(el, selected) {
  el.setAttribute("aria-selected", String(selected));
  el.tabIndex = selected ? 0 : -1;
}

function showGardenView(view) {
  const isPlot = view !== "table";
  document.getElementById("plot-view").hidden = !isPlot;
  document.getElementById("table-view").hidden = isPlot;
  setTabState(document.getElementById("tab-plot"), isPlot);
  setTabState(document.getElementById("tab-table"), !isPlot);
  try { localStorage.setItem("gardenView", isPlot ? "plot" : "table"); } catch (e) {}
}

const GARDEN_TABS = [
  {id: "tab-plot", view: "plot"},
  {id: "tab-table", view: "table"},
];
GARDEN_TABS.forEach((tab, i) => {
  const el = document.getElementById(tab.id);
  el.addEventListener("click", () => showGardenView(tab.view));
  el.addEventListener("keydown", ev => {
    // A Map, not an object literal: `ev.key in {...}` would also match
    // every inherited Object.prototype name.
    const targets = new Map([
      ["ArrowLeft", i - 1], ["ArrowRight", i + 1],
      ["Home", 0], ["End", GARDEN_TABS.length - 1],
    ]);
    if (!targets.has(ev.key)) return;
    ev.preventDefault();
    const next = GARDEN_TABS[(targets.get(ev.key) + GARDEN_TABS.length) % GARDEN_TABS.length];
    showGardenView(next.view);
    document.getElementById(next.id).focus();
  });
});
// Bound to the button inside each header cell, not to the cell: the button
// is what brings focusability and Enter/Space activation with it, which a
// bare <th> has neither of.
for (const th of document.querySelectorAll(".garden-table th[data-sort]")) {
  const button = th.querySelector("button");
  if (!button) continue;
  button.addEventListener("click", () => {
    const key = th.dataset.sort;
    setGardenSort(key, gardenSort.key === key ? -gardenSort.dir : 1);
  });
}
// The select's options are derived from the header cells rather than
// written out a second time in the markup: both controls drive one
// `gardenSort`, so a column added to the table has to appear in both or in
// neither — and a hand-maintained second list is exactly what wouldn't.
function buildGardenSortOptions() {
  const select = document.getElementById("garden-sort");
  if (!select) return;
  for (const th of document.querySelectorAll(".garden-table th[data-sort]")) {
    const option = document.createElement("option");
    option.value = th.dataset.sort;
    // The button's first child is its label text node; the caret span
    // after it is decoration and must not reach an option's label.
    const first = th.querySelector("button") && th.querySelector("button").firstChild;
    option.textContent = ((first && first.textContent) || th.dataset.sort).trim();
    select.append(option);
  }
}
buildGardenSortOptions();
document.getElementById("garden-sort").addEventListener("change", ev => {
  setGardenSort(ev.target.value, gardenSort.dir);
});
document.getElementById("garden-sort-dir").addEventListener("click", () => {
  setGardenSort(gardenSort.key, -gardenSort.dir);
});
// Filtering re-renders both views. `input` rather than `change` so it
// narrows as you type, which is the whole point at 135 rows.
document.getElementById("garden-filter").addEventListener("input", ev => {
  gardenFilter = ev.target.value.trim();
  renderGarden(gardenRows);
});
// The header has to state the default sort before the first poll lands,
// not only once a click has happened — `{key: "repo", dir: 1}` is as much
// a sort as any other, and was equally unlabelled.
renderGardenSortHeaders();
try { showGardenView(localStorage.getItem("gardenView") || "plot"); } catch (e) { showGardenView("plot"); }

// The header caption is the page's only claim about its own liveness, so
// a failed poll has to change more than that one string: every panel goes
// on rendering the last good snapshot, and a dashboard whose server died
// used to look like a healthy one with a small caption (issue #116).
let lastGoodAt = null;
let consecutiveFailures = 0;
let runsRepoFilter = null;

// Announced only when the page crosses between live and stale, or when the
// set of in-flight repos actually changes — never on the 4 s heartbeat
// itself. See #announce in the markup.
let lastAnnounced = "";
function announce(msg) {
  if (!msg || msg === lastAnnounced) return;
  lastAnnounced = msg;
  document.getElementById("announce").textContent = msg;
}

function markFresh(generatedAt) {
  const wasStale = document.body.classList.contains("stale");
  lastGoodAt = generatedAt;
  consecutiveFailures = 0;
  document.body.classList.remove("stale");
  // First successful poll: the skeleton comes off, and every panel stops
  // being a placeholder and starts being a claim.
  document.body.classList.remove("loading");
  document.getElementById("updated").textContent =
    "updated " + new Date(generatedAt).toLocaleTimeString();
  if (wasStale) announce("Dashboard is live again");
}

function markStale(reason) {
  consecutiveFailures++;
  const wasStale = document.body.classList.contains("stale");
  document.body.classList.add("stale");
  const failed = consecutiveFailures + " failed poll" + (consecutiveFailures > 1 ? "s" : "");
  const neverLoaded = lastGoodAt === null;
  document.getElementById("updated").textContent =
    (neverLoaded
      ? "no data yet"
      : "stale — showing data from " + fmtSince(lastGoodAt))
    + " · " + reason + " (" + failed + ")";
  // "never loaded" is a different state from "went stale", and the panels
  // have to say so too rather than only the caption: on a cold start whose
  // first poll failed, the page otherwise kept asserting "nothing in
  // flight" over empty tables (issue #124).
  if (neverLoaded) {
    document.getElementById("in-progress").innerHTML =
      `<span class="empty">could not reach the server — nothing loaded yet</span>`;
  }
  if (!wasStale) announce("Dashboard went stale: " + reason);
}

let pollInFlight = false;
let pollGeneration = 0;
let inFlightController = null;

// How long a single poll may hang before it is abandoned. Without this,
// the only failure the staleness model covered was a poll that *errored*:
// a poll that stalls (Wi-Fi to cellular, a suspended host, a stalled WSL2
// loopback forwarder) delivers no RST, so the request sat open for the OS
// timeout — minutes — with pollInFlight pinned true, every subsequent tick
// returning early, and the header still claiming the page was live
// (issue #122).
const POLL_TIMEOUT_MS = 10000;

// The caption was written once per successful poll and never re-evaluated,
// so the page had no independent sense of its own age between polls. This
// ticks separately from the fetch, so a page whose polls have simply
// stopped happening still goes stale on time.
const STALE_AFTER_MS = 3 * 4000;
setInterval(() => {
  if (lastGoodAt === null || document.body.classList.contains("stale")) return;
  if (Date.now() - Date.parse(lastGoodAt) > STALE_AFTER_MS) markStale("no response");
}, 1000);

async function refresh(force) {
  // One poll at a time. Both the interval and the visibilitychange
  // listener call this, so a slow request against a restarting server
  // could otherwise land *after* a later successful one and re-mark a
  // live page stale. The payload is a whole snapshot, so dropping a poll
  // that starts while another is still running loses nothing.
  if (pollInFlight) {
    // A reader-driven refetch (a filter chip) supersedes the poll already
    // running rather than racing it: the old request is aborted and its
    // result dropped by the generation check below, so there is still
    // never more than one live request and no doomed poll can land after
    // a newer one and re-mark a live page stale.
    if (!force) return;
    if (inFlightController) inFlightController.abort();
  }
  const gen = ++pollGeneration;
  pollInFlight = true;
  const controller = new AbortController();
  inFlightController = controller;
  const timer = setTimeout(() => controller.abort(), POLL_TIMEOUT_MS);
  try {
    let res;
    const query = runsRepoFilter ? "?repo=" + encodeURIComponent(runsRepoFilter) : "";
    try {
      res = await fetch("/api/status" + query, {signal: controller.signal});
    } catch (e) {
      // Superseded on purpose is not a failure to report.
      if (gen !== pollGeneration) return;
      markStale(e && e.name === "AbortError" ? "no response" : "fetch failed");
      return;
    } finally {
      clearTimeout(timer);
    }
    if (gen !== pollGeneration) return;
    // Checked explicitly rather than left to res.json() throwing on the
    // error body: that route happened to catch a 500, and a 2xx with an
    // unparseable body reached the same branch as a dead server and said
    // the same thing. Each of the three now names itself.
    if (!res.ok) { markStale("server returned " + res.status); return; }
    let data;
    try {
      data = await res.json();
    } catch (e) {
      markStale("bad response body");
      return;
    }
    try {
      assertPayload(data);
      renderStatus(data);
    } catch (e) {
      // Without this the page would end up in neither state: the caption
      // frozen at the last good time, no staleness class, nothing in the
      // panels updated — exactly the "looks live but isn't" case.
      markStale("render failed");
      return;
    }
    // Last, not first: the heartbeat claims every panel is showing this
    // snapshot, so it is only claimed once they actually are.
    markFresh(data.generated_at);
  } finally {
    // Only the generation that still owns the slot clears it — an aborted,
    // superseded poll must not release the flag out from under its
    // replacement.
    if (gen === pollGeneration) {
      pollInFlight = false;
      inFlightController = null;
    }
  }
}

// A session can start on a previous calendar day — an overnight run begun
// at 23:00 and read at 02:00 is one session — so a bare clock time would be
// ambiguous in exactly the case this panel exists for.
function sessionTime(iso) {
  const t = Date.parse(iso ?? "");
  if (isNaN(t)) return "";
  const d = new Date(t);
  const sameDay = d.toDateString() === new Date().toDateString();
  return (sameDay ? "" : d.toLocaleDateString([], {month: "short", day: "numeric"}) + " ")
    + shortTime(iso);
}

// Both ends, not just the start. A session that finished hours ago would
// otherwise read as one still running — "since 1:00 AM" over this
// morning's numbers, beside an "in flight" tile counting tonight's — which
// is two windows in one panel, the thing this panel was rescoped to stop
// doing. An unreadable timestamp yields "" rather than a dangling
// preposition: `session_stats` deliberately survives one, so the page has
// to as well.
function sessionWindow(stats) {
  const start = sessionTime(stats.session_started_at);
  const end = sessionTime(stats.session_ended_at);
  if (!start) return end;
  if (!end || end === start) return start;
  return start + " – " + end;
}

// The page's own copy of dashboard.py's PAYLOAD_SCHEMA. A mismatch, or a
// missing key, routes into the existing markStale("render failed") path
// instead of interpolating the string "undefined" into the stat tiles and
// then calling markFresh over the top of it (issue #123). Missing keys
// stringify rather than throw, which is exactly why this has to be an
// explicit check rather than something render code discovers naturally.
const PAGE_SCHEMA = 3;
const REQUIRED_KEYS = ["stats", "runs", "garden_rows", "log_tail", "in_progress"];
const REQUIRED_STAT_KEYS = ["session_run_count", "session_cost_usd", "session_error_count"];
function assertPayload(data) {
  if (!data || typeof data !== "object") throw new Error("payload is not an object");
  if (data.schema !== PAGE_SCHEMA) {
    throw new Error("payload schema " + data.schema + " != page schema " + PAGE_SCHEMA);
  }
  for (const k of REQUIRED_KEYS) {
    if (!(k in data)) throw new Error("payload is missing " + k);
  }
  for (const k of REQUIRED_STAT_KEYS) {
    if (!(k in data.stats)) throw new Error("payload.stats is missing " + k);
  }
}

let lastRunsSig = null;
let lastInFlightKey = null;
function announceInFlight(repos) {
  const key = repos.slice().sort().join(",");
  if (key === lastInFlightKey) return;
  const first = lastInFlightKey === null;
  lastInFlightKey = key;
  if (first) return;   // the initial load is not a transition
  announce(repos.length ? "Now tending " + repos.join(", ") : "Nothing in flight");
}

// A count in a stat tile cannot say what failed. The failures in a real
// overnight run are overwhelmingly one systemic cause repeated across many
// repos — an exhausted session limit, a `claude` that left PATH, a cached
// clone stuck dirty — which the page rendered as N unrelated repo failures
// interleaved among the successes (issue #136). Identical summaries are
// collapsed so the shape of the night is visible at a glance.
function renderFailures(errors, st) {
  const panel = document.getElementById("failures-panel");
  if (!errors.length) { panel.hidden = true; return; }
  panel.hidden = false;
  document.getElementById("failures-window").textContent =
    st.session_run_count ? `${errors.length} of ${st.session_run_count} runs` : "";
  const groups = new Map();
  for (const e of errors) {
    const reason = (e.summary || "no summary recorded").split("\\n")[0].trim();
    if (!groups.has(reason)) groups.set(reason, []);
    groups.get(reason).push(e);
  }
  document.getElementById("failures").innerHTML =
    [...groups].map(([reason, rows]) => {
      const repos = rows.map(r => repoLink(r.repo)).join(", ");
      return `<div class="failure">`
        + `<div class="failure-reason">${linkifyRefs(reason, rows[0].repo)}`
        + (rows.length > 1 ? ` <span class="muted">×${rows.length}</span>` : "")
        + `</div><div class="failure-repos sub">${repos}</div></div>`;
    }).join("");
}

// One line per calendar day. Neither session_stats nor repo_stats can say
// whether things are getting better or worse, so a night that was a total
// loss became invisible the moment it stopped being the newest one (#138).
function renderHistory(days) {
  document.getElementById("history").innerHTML = days.map(d => `
    <tr role="row">
      <td role="cell" data-label="Day">${esc(d.day)}</td>
      <td class="num" role="cell" data-label="Runs">${d.runs}</td>
      <td class="num ${d.errors ? "outcome-error" : "muted"}" role="cell" data-label="Errors">${d.errors}</td>
      <td class="num" role="cell" data-label="Cost">${fmtCost(d.cost_usd)}</td>
      <td class="num" role="cell" data-label="Time">${esc(fmtDur(d.duration_ms))}</td>
    </tr>
  `).join("") || `<tr class="is-empty" role="row"><td colspan="5" class="empty" role="cell">no history yet</td></tr>`;
}

// How far through the cycle over the whole garden the rotation is — the
// unit `overnight` is designed around, since it resumes across several
// nights when the garden is bigger than one budget window. Which cursor
// key holds that position depends on the strategy that wrote it, and the
// file records both without recording which (see overnight.py), so each
// branch below names the cursor it actually read rather than presenting
// one of them as "the" cycle position.
function cycleProgress(oc) {
  if (!oc || !oc.garden_size) return null;
  const total = oc.garden_size;
  if (oc.strategy === "round-robin") {
    const n = oc.next_index % total;
    return {label: `cycle ${n} of ${total} · round-robin`, done: n, total};
  }
  if (oc.strategy) {
    return {label: `cycle ${oc.attempted} of ${total} attempted · ${oc.strategy}`,
            done: oc.attempted, total};
  }
  // No run is live, so the strategy is unknown and only the cursor's own
  // contents can say which rotation is part-way through.
  if (oc.attempted) {
    return {label: `cycle ${oc.attempted} of ${total} attempted · from the last issue-count/random run`,
            done: oc.attempted, total};
  }
  if (oc.next_index % total) {
    const n = oc.next_index % total;
    return {label: `cycle ${n} of ${total} · from the last round-robin run`, done: n, total};
  }
  return null;
}

function renderCycle(oc) {
  const cp = cycleProgress(oc);
  document.getElementById("cycle").innerHTML = cp
    ? `<div class="sub">${esc(cp.label)}</div>
       <div class="progress-bar"><div style="width:${Math.min(100, 100 * cp.done / cp.total)}%"></div></div>`
    : "";
}

// The invocation's own time budget. `overnight` stops dispatching when it
// runs out, so "how much is left" is what says whether the rest of the
// cycle can plausibly land tonight — and it was on no panel at all, even
// though the run states it on its first line (issue #113).
function renderBudget(run) {
  const el = document.getElementById("budget");
  if (!run) { el.innerHTML = ""; return; }
  const budgetMs = run.budget_hours * 3600 * 1000;
  const parts = [`budget ${fmtDur(budgetMs)}`];
  if (run.elapsed_seconds != null) {
    parts.push(`${fmtDur(run.elapsed_seconds * 1000)} elapsed`);
    parts.push(run.remaining_seconds ? `${fmtDur(run.remaining_seconds * 1000)} left` : "budget spent");
  }
  if (run.started_at) parts.push(`started ${shortTime(run.started_at)}`);
  const pct = run.elapsed_seconds != null && budgetMs > 0
    ? Math.min(100, 100 * run.elapsed_seconds * 1000 / budgetMs)
    : null;
  el.innerHTML = `<div class="sub">${esc(parts.join(" · "))}</div>`
    + (pct == null ? "" : `<div class="progress-bar"><div style="width:${pct}%"></div></div>`);
}

// A hub (RFC 0007) serves this page over the run history of every device,
// but logs, sessions, and the overnight cursor stay on the device that
// wrote them. Those panels say so rather than render empty, since an empty
// "Currently tending" reads as "nothing is running".
function renderHubChrome(data) {
  document.body.classList.toggle("is-hub", !!data.hub);
  const who = document.getElementById("hub-user");
  if (data.hub && data.hub_user) {
    who.hidden = false;
    who.innerHTML = `signed in as ${esc(data.hub_user)} · <a href="/logout">sign out</a>`;
  } else {
    who.hidden = true;
  }
}

function renderStatus(data) {
  renderHubChrome(data);
  // The window is stated, never implied: this panel used to be headed
  // "Tonight" over whatever the last 40 rows happened to span (issue #105).
  const st = data.stats;
  // `overnight` is a time-budget scheduler, so how long the session
  // actually spent is what explains why only a fraction of the candidates
  // were attempted — a number recorded on every run and shown nowhere
  // (issue #137). Kept in the caption rather than as a fifth stat tile,
  // which would re-orphan the last tile the four-column grid just fixed.
  document.getElementById("session-window").textContent =
    st.session_run_count
      ? sessionWindow(st) + (st.session_duration_ms ? " · " + fmtDur(st.session_duration_ms) + " spent" : "")
      : "";
  const liveCount = data.in_progress.length;
  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="n">${st.session_run_count}</div><div class="l">runs</div></div>
    <div class="stat"><div class="n">${fmtCost(st.session_cost_usd)}</div><div class="l">cost</div></div>
    <div class="stat${st.session_error_count ? " is-err" : ""}"><div class="n">${st.session_error_count}</div><div class="l">errors</div></div>
    <div class="stat${liveCount ? " is-live" : ""}"><div class="n">${data.hub ? "—" : liveCount}</div><div class="l">in flight</div></div>
  `;

  renderCycle(data.overnight_cycle);
  renderBudget(data.overnight_run);

  const bp = data.batch_progress;
  // A default (--concurrency 1) run reports a batch of one, where start
  // and end are the same candidate — read as a range it says "3–3".
  const bpLabel = !bp ? "" : bp.start === bp.end
    ? `candidate ${bp.start}`
    : `candidates ${bp.start}–${bp.end}`;
  document.getElementById("batch").innerHTML = bp
    ? `<div class="sub">${bpLabel} of ${bp.total} this run</div>
       <div class="progress-bar"><div style="width:${Math.min(100, 100 * bp.end / bp.total)}%"></div></div>`
    : `<div class="empty">no overnight batch in this log</div>`;

  const ip = document.getElementById("in-progress");
  ip.innerHTML = data.hub
    ? `<span class="empty">per-device: open the dashboard on the device that is dispatching</span>`
    : liveCount
    ? data.in_progress.map(r =>
        `<span class="pill live" title="${esc(r)}">${esc(r)}</span>`).join("")
    : `<span class="empty">nothing in flight</span>`;
  ip.className = "";
  // Identity, not just count: one tend finishing as another starts leaves
  // the count unchanged while the actual work moved on.
  announceInFlight(data.in_progress);

  renderFailures(data.session_errors || [], st);
  renderGarden(data.garden_rows);
  renderHistory(data.history || []);

  // The window is named, the way "Latest session" already names its own —
  // this table routinely spans several calendar days and said nothing
  // about which (issue #141).
  const filt = data.runs_filter || {};
  const span = data.runs.length
    ? sessionTime(data.runs[data.runs.length - 1].timestamp) + " – " + sessionTime(data.runs[0].timestamp)
    : "";
  document.getElementById("runs-window").innerHTML =
    (filt.repo ? `${esc(filt.repo)} only · ` : "")
    + (data.runs.length ? `last ${data.runs.length}${span ? " · " + esc(span) : ""}` : "")
    + (filt.repo ? ` <button type="button" class="chip" id="clear-runs-filter">show all repos</button>` : "");

  // The data-label attributes are what the narrow-viewport card layout
  // renders via td::before once the thead is hidden — see the stylesheet.
  // Guarded by a signature for the same reason the garden table is: an
  // unconditional rebuild destroys any text selection within 4 s (#126).
  const multiDevice = !!data.multi_device;
  const runsSig = JSON.stringify([filt, multiDevice, data.runs.map(r => r.id ?? r.timestamp + r.repo)]);
  if (runsSig !== lastRunsSig) {
    lastRunsSig = runsSig;
    for (const th of document.querySelectorAll("#runs-panel .device-col")) th.hidden = !multiDevice;
    let lastDay = null;
    document.getElementById("runs").innerHTML = data.runs.map(r => {
      // A muted separator whenever the calendar day changes, so four days
      // of rows can't read as one morning.
      const day = r.timestamp ? String(r.timestamp).slice(0, 10) : "";
      const sep = day && day !== lastDay && lastDay !== null
        ? `<tr class="is-empty day-sep" role="row"><td colspan="${multiDevice ? 8 : 7}" role="cell">${esc(day)}</td></tr>` : "";
      lastDay = day;
      return sep + `
      <tr role="row">
        <td class="time" role="cell" data-label="Time">${esc(sessionTime(r.timestamp) || "—")}</td>
        <td class="repo" role="rowheader" data-label="Repo">${repoLink(r.repo)}</td>
        <td class="mode" role="cell" data-label="Mode">${esc(r.mode)}</td>
        <td class="outcome outcome-${esc(r.outcome)}" role="cell" data-label="Outcome">${esc(r.outcome)}</td>
        <td class="dur num" role="cell" data-label="Time taken">${esc(fmtDur(r.duration_ms))}</td>
        <td class="cost" role="cell" data-label="Cost">${fmtCost(r.cost_usd)}</td>
        <td class="summary" role="cell" data-label="Summary">${linkifyRefs(r.summary, r.repo)}</td>
        ${multiDevice ? `<td class="device" role="cell" data-label="Device">${esc(r.device || "—")}</td>` : ""}
      </tr>`;
    }).join("") || `<tr class="is-empty" role="row"><td colspan="${multiDevice ? 8 : 7}" class="empty" role="cell">no runs recorded yet</td></tr>`;
  }

  renderLog(data);
}

// Which log the reader explicitly picked, or null for "whichever is
// newest". Held across polls so a mid-poll mtime flip between two
// concurrently-writing runs can't yank the view away from the one being
// read, and cleared the moment that log stops being live — the same
// shape as the plot/table view choice, minus the persistence: a picked
// log is meaningful only while it is still being written to.
let logSelection = null;
// Set when the reader switches logs, so the next render starts the new
// file at its newest line instead of inheriting the previous one's
// scroll offset, which points at unrelated content.
let logJumpToBottom = false;

// The tail panel shows one file at a time — interleaving two raw
// narrations would be unreadable — but every live log's tail is in the
// payload, so when a second run exists the caption becomes a picker over
// them rather than a note saying they are there and cannot be read
// (issue #117). With one live log the picker stays hidden and this
// renders exactly what it always did.
function renderLog(data) {
  const logs = data.active_logs || [];
  // `log_tails` is additive, so a page loaded from a newer server than
  // the one now answering still renders the default tail rather than
  // going blank.
  const tails = data.log_tails || {};
  if (logSelection && !logs.includes(logSelection)) logSelection = null;
  const selected = logSelection || data.active_log;
  const wrap = document.getElementById("log-pick-wrap");
  const pick = document.getElementById("log-pick");
  wrap.hidden = logs.length < 2;
  if (!wrap.hidden) {
    // Rebuilt only when the set of live logs actually changes: this runs
    // on every 4 s poll, and replacing the options under an open <select>
    // closes it in the reader's face.
    const sig = logs.join("\\u0000");
    if (pick.dataset.sig !== sig) {
      pick.dataset.sig = sig;
      // Built as elements rather than interpolated HTML, like
      // `buildGardenSortOptions`: a log path is server-supplied text and
      // `option.value`/`textContent` cannot be escaped wrongly.
      pick.replaceChildren();
      for (const p of logs) {
        const option = document.createElement("option");
        option.value = p;
        option.textContent = logName(p);
        // The filename is what the option is labelled with; the full
        // path is on the option itself for anyone who needs it.
        option.title = p;
        pick.append(option);
      }
    }
    if (pick.value !== selected) pick.value = selected;
  }
  // The full path stays in the caption in both cases — the picker labels
  // its options by filename, which is what distinguishes them, not by the
  // state dir they all share.
  document.getElementById("log-path").textContent = selected ? "(" + selected + ")" : "";
  const logEl = document.getElementById("log");
  const wasAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  const tail = (selected && tails[selected]) || (selected === data.active_log ? data.log_tail : []) || [];
  logEl.textContent = tail.join("\\n") || "(no active log)";
  if (wasAtBottom || logJumpToBottom) logEl.scrollTop = logEl.scrollHeight;
  logJumpToBottom = false;
}

// The filename, which is `run_log.log_file_name`'s
// `<command>-<YYYYmmdd-HHMMSS>.log` and is what tells two live logs
// apart. Split on both separators, since the state dir is a POSIX path
// on the devices this runs on but the page is read from Windows too.
function logName(path) {
  const parts = String(path).split(/[\\\\/]/);
  return parts[parts.length - 1] || String(path);
}

document.getElementById("log-pick").addEventListener("change", ev => {
  logSelection = ev.target.value || null;
  // Re-fetch rather than re-render a snapshot this function doesn't
  // keep, matching how the runs filter chip switches views.
  logJumpToBottom = true;
  refresh(true);
});

// Every poll costs a sqlite aggregate over the whole run history plus a
// tail read of every live log, so a tab left in the background for a
// whole overnight run would compete for CPU and I/O with the dispatched
// runs the page exists to watch. The payload is a full snapshot with no
// incremental state, so skipping polls while hidden loses nothing as
// long as one fires the moment the tab is looked at again.
// Delegated: #runs-window is rewritten on every render that changes the
// filter, so a bound listener would be discarded.
document.getElementById("runs-window").addEventListener("click", ev => {
  if (!ev.target.closest("#clear-runs-filter")) return;
  runsRepoFilter = null;
  lastDetailHtml = null;
  refresh(true);
});

function tick() { if (!document.hidden) refresh(); }
refresh();
setInterval(tick, 4000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
</script>
</body>
</html>
"""


#: Upper bound on `?limit=`. The Recent runs table is rendered wholesale
#: into the DOM, so an unbounded limit would let a hand-typed URL build a
#: multi-megabyte payload and a table to match.
MAX_RUN_LIMIT = 500


def _status_query(query: str) -> dict:
    """`build_status` kwargs parsed from `/api/status`'s query string.

    Deliberately total: anything unparseable is dropped in favour of the
    default rather than raising, because this runs inside the poll path
    and a malformed hand-typed URL should degrade to the normal page, not
    to a 500."""
    params = urllib.parse.parse_qs(query)
    kwargs: dict = {}
    raw_limit = params.get("limit", [None])[0]
    if raw_limit is not None:
        try:
            kwargs["run_limit"] = max(1, min(MAX_RUN_LIMIT, int(raw_limit)))
        except ValueError:
            pass
    raw_repo = params.get("repo", [None])[0]
    if raw_repo:
        kwargs["repo"] = raw_repo
    return kwargs


def _live():
    """`gardener.live`, imported on first use: it builds on this module's
    log-parsing helpers, so importing it at the top of this one would be
    circular."""
    from gardener import live

    return live


class _DashboardHandler(BaseHTTPRequestHandler):
    state_dir: Optional[Path] = None

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        # BaseHTTPRequestHandler logs every request to stderr by default;
        # a 4s-polling dashboard would spam gardener's own terminal output
        # with a request line every 4 seconds for no benefit.
        pass

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode("utf-8"))
        elif parsed.path == "/api/status":
            # `BaseHTTPRequestHandler.handle_one_request` does not catch
            # exceptions from the dispatched method — only socket.timeout —
            # so an unguarded raise here reached `socketserver.handle_error`
            # and closed the socket having written *zero bytes*. The page
            # reports that as "fetch failed", which `docs/DASHBOARD.md`
            # documents as the server being gone, for what is usually a
            # transient read error (issue #121). A real 500 is a state the
            # page already names separately.
            try:
                payload = build_status(state_dir=self.state_dir, **_status_query(parsed.query))
                body = json.dumps(payload).encode("utf-8")
            except Exception as exc:  # noqa: BLE001 - a dashboard poll must never kill the socket
                # `log_message` is stubbed out to keep the 4 s poll from
                # spamming stderr, so without this an error left no trace
                # at all beyond the traceback socketserver prints.
                print(f"gardener dashboard: /api/status failed: {exc!r}", file=sys.stderr)
                error_body = json.dumps({"error": type(exc).__name__, "detail": str(exc)})
                self._send(500, "application/json; charset=utf-8", error_body.encode("utf-8"))
                return
            self._send(200, "application/json; charset=utf-8", body)
        elif parsed.path == "/live":
            self._send(200, "text/html; charset=utf-8", _live().LIVE_PAGE_HTML.encode("utf-8"))
        elif parsed.path == "/api/live":
            # Same "a real 500, never zero bytes" rule as /api/status above.
            try:
                body = json.dumps(_live().build_live(state_dir=self.state_dir)).encode("utf-8")
            except Exception as exc:  # noqa: BLE001 - a dashboard poll must never kill the socket
                print(f"gardener dashboard: /api/live failed: {exc!r}", file=sys.stderr)
                error_body = json.dumps({"error": type(exc).__name__, "detail": str(exc)})
                self._send(500, "application/json; charset=utf-8", error_body.encode("utf-8"))
                return
            self._send(200, "application/json; charset=utf-8", body)
        else:
            self.send_response(404)
            self.end_headers()


def run_server(port: int = DEFAULT_PORT, state_dir: Optional[Path] = None, host: str = "127.0.0.1") -> None:
    """Blocks forever serving the dashboard. Binds to `host` (127.0.0.1 by
    default) only — this has no authentication, so it must never be bound
    to 0.0.0.0/a real interface. WSL2 forwards a loopback bind through to
    the Windows host's own localhost automatically, so the default is
    reachable from a Windows browser without any extra network config.

    Raises `ValueError` if `host` does not resolve to a loopback address,
    so a future caller can't accidentally expose the dashboard on 0.0.0.0
    just by passing a different `host`."""
    try:
        addr_info = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"dashboard host {host!r} could not be resolved: {exc}") from exc
    for _family, _type, _proto, _canonname, sockaddr in addr_info:
        ip = sockaddr[0]
        try:
            if not ipaddress.ip_address(ip).is_loopback:
                raise ValueError(
                    f"dashboard host {host!r} resolves to non-loopback address {ip!r}; "
                    "the dashboard has no authentication and must only bind to loopback"
                )
        except ValueError as exc:
            # Re-raise ValueErrors from our own check unchanged; swallow
            # ipaddress.ip_address() parse errors (shouldn't happen —
            # getaddrinfo returns valid IPs — but guard against unexpected
            # formats).
            if "loopback" in str(exc):
                raise
    # Class attribute, not an instance one: http.server instantiates
    # handler_class(request, client_address, server) itself per request,
    # so this is how per-server config (which state dir to read) reaches
    # each handler instance.
    _DashboardHandler.state_dir = state_dir
    # ThreadingHTTPServer so the 4s-polling browser tab and any concurrent
    # request never block on each other — each request only does local
    # file/sqlite reads, never a `claude`/`git`/`gh` subprocess.
    httpd = ThreadingHTTPServer((host, port), _DashboardHandler)
    print(f"gardener dashboard: serving on http://{host}:{port} (Ctrl+C to stop)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def find_free_port(preferred: int = DEFAULT_PORT, host: str = "127.0.0.1") -> int:
    """`preferred` if it's free, otherwise the next port the OS hands back
    — so a second `gardener dashboard` invocation (e.g. after a crash left
    the first one running) doesn't just fail with 'address in use'.

    Sets SO_REUSEADDR on the probe socket to match `run_server`'s actual
    server socket (`http.server.HTTPServer.allow_reuse_address = 1`,
    inherited by `ThreadingHTTPServer`) — without it, this probe reports a
    false "in use" for a port still sitting in TIME_WAIT from a just-killed
    previous instance, even though the real server would bind it fine."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        return s.getsockname()[1]
