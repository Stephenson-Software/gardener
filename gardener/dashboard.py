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
#50). The log *tail* panel still shows one file, since interleaving two
raw narrations would be unreadable, but the payload names the others so
the page can say how many it isn't showing.

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
to notice. `refresh` therefore checks `res.ok` explicitly — a 2xx with an
unparseable body used to be indistinguishable from a dead server — and a
failure marks `<body class="stale">`, dimming the content and replacing
the header heartbeat with the *age* of what's on screen rather than a
static caption (issue #116).
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import sys
import time
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
    logs = [p for p in logs_dir.glob("*.log") if p.is_file()]
    if not logs:
        return None
    return max(logs, key=lambda p: p.stat().st_mtime)


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
    lines = raw.decode("utf-8", errors="replace").splitlines()
    return lines[-n:]


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

    Rows are sorted by repo name, matching `gardener garden list` — a
    stable order matters more than a clever one here, because the plot
    view draws a plant per row and re-renders every 4 s poll."""
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
            }
        )
    return rows


def build_status(
    state_dir: Optional[Path] = None,
    run_limit: int = 40,
    log_tail_lines: int = 400,
) -> dict:
    base = state_dir or state.default_state_dir()
    db_path = base / "gardener.sqlite3"
    logs_dir = default_logs_dir(base)

    runs = state.list_runs(db_path=db_path, limit=run_limit)
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

    recent_cost = sum(r.cost_usd for r in runs if r.cost_usd)
    recent_error_count = sum(1 for r in runs if r.outcome == "error")

    garden_repos = _safe_list(lambda: garden.list_garden(path=base / "garden.json"))
    allowed_repos = _safe_list(
        lambda: merge_allowlist.list_allowed(path=base / "merge_allowlist.json")
    )
    garden_rows = build_garden_rows(
        garden_repos, allowed_repos, state.repo_stats(db_path=db_path), in_progress
    )

    return {
        "generated_at": state.now_iso(),
        # `active_log` is the one whose raw narration `log_tail` shows;
        # `active_logs` is every log the in-flight/batch panels were built
        # from, so the page can say how many runs it is *not* tailing
        # rather than silently showing one of several.
        "active_log": str(active_log) if active_log else None,
        "active_logs": [str(p) for p in active_logs],
        "log_tail": log_lines[-200:],
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
            }
            for r in runs
        ],
        "stats": {
            "recent_run_count": len(runs),
            "recent_cost_usd": round(recent_cost, 2),
            "recent_error_count": recent_error_count,
        },
        # `garden`/`merge_allowlist` stay in the payload as the raw lists
        # they always were — `garden_rows` is the joined view the UI
        # renders, but the flat lists are what a `curl /api/status | jq`
        # check of "is repo X opted in" is easiest to read.
        "garden": garden_repos,
        "merge_allowlist": allowed_repos,
        "garden_rows": garden_rows,
        "overnight_next_index": overnight.read_cursor(path=base / "overnight_cursor.json"),
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
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f5f6f4; --panel: #ffffff; --text: #1b1f1c; --muted: #5b645d;
      --border: #dfe3de; --accent: #2f7a4f; --warn: #b3661a; --err: #b3261e;
      --soil: #7a6450; --pot: #b56f47; --seed: #8b6a3c; --fallen: #8a6448;
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
  .wide { grid-column: 1 / -1; }
  /* A grid rather than a flex row: four stats wrapped by flex leave a
     ragged last line on a phone, where an even 2x2 reads as one block. */
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(6.5rem, 1fr)); gap: 0.75rem 1.25rem; }
  .stat .n { font-size: 1.6rem; font-weight: 600; line-height: 1.2; }
  .stat .l { color: var(--muted); font-size: 0.78rem; }
  .pill {
    display: inline-block; padding: 0.1rem 0.55rem; border-radius: 999px;
    font-size: 0.75rem; border: 1px solid var(--border); margin: 0.15rem;
  }
  .pill.live { border-color: var(--accent); color: var(--accent); }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; }
  td.repo { white-space: nowrap; font-family: var(--mono); font-size: 0.8rem; }
  td.summary { color: var(--text); }
  .outcome-error { color: var(--err); }
  .outcome-tend, .outcome-created { color: var(--accent); }
  pre#log {
    font-family: var(--mono); font-size: 0.78rem; white-space: pre-wrap;
    word-break: break-word; max-height: min(480px, 60vh); overflow-y: auto; margin: 0;
    line-height: 1.45;
  }
  .empty { color: var(--muted); font-style: italic; }
  .progress-bar {
    height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; margin-top: 0.4rem;
  }
  .progress-bar > div { height: 100%; background: var(--accent); }
  /* A poll that fails must not render as a healthy dashboard. Everything
     below the header is dimmed and desaturated so "last known" can never
     be mistaken for "live"; the header keeps full contrast because it is
     the part carrying the explanation and the snapshot's age. */
  body.stale main { opacity: 0.42; filter: grayscale(0.7); }
  body.stale #updated { color: var(--warn); }

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
  .pd-close {
    font: inherit; background: transparent; color: var(--muted);
    border: 0; cursor: pointer; padding: 0 0.3rem; line-height: 1;
  }
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
  .legend summary { cursor: pointer; }
  .legend ul { margin: 0.5rem 0 0; padding-left: 1.1rem; }
  .legend li { margin-bottom: 0.15rem; }
  .legend b { color: var(--text); font-weight: 600; }
  .garden-table th[data-sort] { cursor: pointer; user-select: none; white-space: nowrap; }
  .garden-table th[data-sort]:hover { color: var(--text); }
  .garden-table td.num, .garden-table th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .dot {
    display: inline-block; width: 0.6rem; height: 0.6rem; border-radius: 50%;
    margin-right: 0.35rem; vertical-align: baseline;
  }
  .muted { color: var(--muted); }

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
    td.time, td.mode, td.outcome, td.cost { order: 2; flex: 0 0 auto; font-size: 0.78rem; }
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
       ~100px reads as a garden; eight 84px columns read as clip art. */
    .plot { grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); }
  }
</style>
</head>
<body>
<header>
  <h1>🌱 gardener dashboard</h1>
  <span class="sub" id="updated">loading…</span>
</header>
<main>
  <div class="panel">
    <h2>Tonight</h2>
    <div class="stats" id="stats"></div>
    <div id="batch"></div>
  </div>
  <div class="panel">
    <h2>Currently tending</h2>
    <div id="in-progress" class="empty">nothing in flight</div>
  </div>
  <div class="panel wide garden-panel">
    <h2 id="garden-heading">Garden</h2>
    <div class="toolbar">
      <div class="tabs" role="tablist" aria-label="Garden view">
        <button class="tab" id="tab-plot" role="tab" aria-selected="true"
                aria-controls="plot-view" tabindex="0">🌿 Plot</button>
        <button class="tab" id="tab-table" role="tab" aria-selected="false"
                aria-controls="table-view" tabindex="-1">▤ Table</button>
      </div>
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
      <table class="garden-table">
        <thead><tr>
          <th data-sort="repo">Repo</th>
          <th data-sort="health">Health</th>
          <th data-sort="successes" class="num">Tends</th>
          <th data-sort="errors" class="num">Errors</th>
          <th data-sort="last_success">Last tended</th>
          <th data-sort="cost_usd" class="num">Cost</th>
          <th data-sort="can_merge">Merge</th>
        </tr></thead>
        <tbody id="garden-rows"></tbody>
      </table>
    </div>
  </div>
  <div class="panel wide">
    <h2>Recent runs</h2>
    <table>
      <thead><tr><th>Time</th><th>Repo</th><th>Mode</th><th>Outcome</th><th>Cost</th><th>Summary</th></tr></thead>
      <tbody id="runs"></tbody>
    </table>
  </div>
  <div class="panel wide">
    <h2>Live log <span class="sub" id="log-path"></span></h2>
    <pre id="log"></pre>
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
function fmtDur(ms) { return ms == null ? "—" : (ms / 1000).toFixed(0) + "s"; }
function shortTime(ts) {
  if (!ts) return "—";
  try { return new Date(ts).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); }
  catch { return ts; }
}

/* ---------------------------------------------------------------------
   The garden: one row per opted-in repo, drawn two ways.

   Every visual property below is a real number out of gardener's own run
   history — nothing here is decoration for its own sake. The mapping is
   spelled out in the page's own legend, so what you see can always be
   traced back to a column. See build_garden_rows() for the row shape.
   --------------------------------------------------------------------- */
const SOIL_Y = 112, BASE_X = 50;

const HEALTH = {
  thriving:   {label: "thriving",   leaf: "#4caf6d", stem: "#3d7f52", droop: 0.00},
  steady:     {label: "steady",     leaf: "#5aa860", stem: "#437a46", droop: 0.15},
  dry:        {label: "dry",        leaf: "#a8a54e", stem: "#7c7a3c", droop: 0.45},
  wilting:    {label: "wilting",    leaf: "#a8763f", stem: "#7d5a33", droop: 0.85},
  struggling: {label: "struggling", leaf: "#9c6a55", stem: "#6f4c3c", droop: 0.70},
  unplanted:  {label: "not tended", leaf: "#7d8a80", stem: "#6d7a71", droop: 0.00},
};
// Worst first, so sorting the Health column ascending surfaces the repos
// that need attention rather than the ones that don't.
const HEALTH_ORDER = ["unplanted", "struggling", "wilting", "dry", "steady", "thriving"];

function healthOf(row) {
  if (!row.runs) return "unplanted";
  if (!row.last_success) return "struggling";
  const d = daysSince(row.last_success);
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
      const blooms = ["#e77fa7", "#e88f6c", "#c88ad8", "#8fb8e0", "#eec55f"];
      const tint = h.droop > 0.5 ? "#c9a06a" : blooms[Math.floor(rnd() * blooms.length)];
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
      parts.push(`<circle cx="${n1(topX)}" cy="${n1(topY)}" r="${n1(pr * 0.66)}" fill="#f0c356"/>`);
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

function sortedGardenRows() {
  const k = gardenSort.key;
  const val = r =>
    k === "repo" ? r.repo.toLowerCase()
    : k === "health" ? HEALTH_ORDER.indexOf(healthOf(r))
    : k === "last_success" ? (r.last_success ? Date.parse(r.last_success) : 0)
    : k === "can_merge" ? (r.can_merge ? 1 : 0)
    : r[k];
  return gardenRows.slice().sort((a, b) => {
    const x = val(a), y = val(b);
    if (x < y) return -1 * gardenSort.dir;
    if (x > y) return 1 * gardenSort.dir;
    return a.repo.localeCompare(b.repo);
  });
}

function renderGardenTable() {
  const rows = sortedGardenRows();
  document.getElementById("garden-rows").innerHTML = rows.map(r => {
    const h = HEALTH[healthOf(r)];
    const age = fmtAge(daysSince(r.last_success));
    return `<tr>
      <td class="repo" data-label="Repo">${esc(r.repo)}${r.in_garden ? "" : ` <span class="muted">(not planted)</span>`}</td>
      <td data-label="Health"><span class="dot" style="background:${h.leaf}"></span>${r.in_flight ? `<span class="pill live">tending now</span>` : h.label}</td>
      <td class="num" data-label="Tends">${r.successes}</td>
      <td class="num ${r.errors ? "outcome-error" : "muted"}" data-label="Errors">${r.errors}</td>
      <td data-label="Last tended">${age}</td>
      <td class="num" data-label="Cost">${fmtCost(r.cost_usd)}</td>
      <td data-label="Merge">${r.can_merge ? "✓ allowed" : `<span class="muted">—</span>`}</td>
    </tr>`;
  }).join("") || `<tr class="is-empty"><td colspan="7" class="empty">nothing opted in — <code>gardener garden add --repo owner/name</code></td></tr>`;
}

function renderGarden(rows) {
  gardenRows = rows || [];
  document.getElementById("garden-heading").textContent = `Garden (${gardenRows.length})`;

  const counts = {};
  for (const r of gardenRows) { const k = healthOf(r); counts[k] = (counts[k] || 0) + 1; }
  const needsWater = (counts.dry || 0) + (counts.wilting || 0) + (counts.struggling || 0);
  const mergeable = gardenRows.filter(r => r.can_merge).length;
  const unplanted = gardenRows.filter(r => !r.in_garden).length;
  document.getElementById("garden-summary").textContent = gardenRows.length
    ? `${counts.thriving || 0} thriving · ${needsWater} needing water · ${mergeable} may merge`
      + (unplanted ? ` · ${unplanted} allow-listed but not planted` : "")
    : "";

  // Re-render the plot only when something it draws actually changed —
  // otherwise the 4 s poll would restart the in-flight glow animation
  // from zero every time, and rebuild 32 SVGs for nothing.
  const sig = JSON.stringify(gardenRows.map(r =>
    [r.repo, r.successes, r.errors, r.cost_usd, r.can_merge, r.in_garden, r.in_flight, healthOf(r)]));
  if (sig !== lastPlotSig) {
    lastPlotSig = sig;
    // The plot is rebuilt wholesale, so a plant that currently has the
    // keyboard would drop focus to the document every time any repo's
    // drawn data changed — which during a run is often.
    const focused = document.activeElement;
    const refocus = focused && focused.matches && focused.matches("#plot .plant[data-repo]")
      ? focused.dataset.repo : null;
    const maxCost = gardenRows.reduce((m, r) => Math.max(m, r.cost_usd), 0);
    document.getElementById("plot").innerHTML = gardenRows.map(r => {
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
        <div class="nm">${esc(r.repo.split("/").pop())}</div>
        <div class="meta">${r.successes}✓${r.errors ? " " + r.errors + "✗" : ""} · ${esc(age)}</div>
      </button>`;
    }).join("") || `<div class="empty">nothing planted yet</div>`;
    if (refocus) focusPlant(refocus);
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
function focusPlant(repo) {
  for (const el of document.querySelectorAll("#plot .plant[data-repo]")) {
    if (el.dataset.repo === repo) { el.focus(); return; }
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
    ["Cost", fmtCost(r.cost_usd), ""],
    ["Merge", r.can_merge ? "allow-listed — a tend may merge its own PR" : "not allow-listed", ""],
    ["Planted", r.in_garden ? "in the garden" : "allow-listed, but not in the garden", ""],
  ];
  const html = `<div class="pd-head"><b>${esc(r.repo)}</b>`
    + `<button type="button" class="pd-close" data-close="1" aria-label="Close details">✕</button></div>`
    + `<dl>${facts.map(([k, v, cls]) =>
        `<dt>${esc(k)}</dt><dd class="${cls}">${esc(v)}</dd>`).join("")}</dl>`;
  // Rewritten only when it actually changed. renderGarden runs on every
  // 4 s poll, so an unconditional innerHTML would take focus off the
  // close button once a poll and restart any transition on the card.
  if (html !== lastDetailHtml) {
    el.innerHTML = html;
    lastDetailHtml = html;
  }
  el.hidden = false;
}

function syncPlantSelection() {
  for (const el of document.querySelectorAll("#plot .plant[data-repo]")) {
    const on = el.dataset.repo === selectedPlant;
    el.setAttribute("aria-expanded", String(on));
    el.classList.toggle("selected", on);
  }
  renderPlantDetail();
}

// Delegated, because both the plot and the detail card are replaced
// wholesale by innerHTML — a listener bound to a plant would be discarded
// on the next re-render.
document.getElementById("plot").addEventListener("click", ev => {
  const btn = ev.target.closest(".plant[data-repo]");
  if (!btn) return;
  selectedPlant = selectedPlant === btn.dataset.repo ? null : btn.dataset.repo;
  syncPlantSelection();
});
document.getElementById("plant-detail").addEventListener("click", ev => {
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
for (const th of document.querySelectorAll(".garden-table th[data-sort]")) {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    gardenSort = {key, dir: gardenSort.key === key ? -gardenSort.dir : 1};
    renderGardenTable();
  });
}
try { showGardenView(localStorage.getItem("gardenView") || "plot"); } catch (e) { showGardenView("plot"); }

// The header caption is the page's only claim about its own liveness, so
// a failed poll has to change more than that one string: every panel goes
// on rendering the last good snapshot, and a dashboard whose server died
// used to look like a healthy one with a small caption (issue #116).
let lastGoodAt = null;
let consecutiveFailures = 0;

function markFresh(generatedAt) {
  lastGoodAt = generatedAt;
  consecutiveFailures = 0;
  document.body.classList.remove("stale");
  document.getElementById("updated").textContent =
    "updated " + new Date(generatedAt).toLocaleTimeString();
}

function markStale(reason) {
  consecutiveFailures++;
  document.body.classList.add("stale");
  const failed = consecutiveFailures + " failed poll" + (consecutiveFailures > 1 ? "s" : "");
  document.getElementById("updated").textContent =
    (lastGoodAt === null
      ? "no data yet"
      : "stale — showing data from " + fmtSince(lastGoodAt))
    + " · " + reason + " (" + failed + ")";
}

async function refresh() {
  let data;
  try {
    const res = await fetch("/api/status");
    // Checked explicitly rather than left to res.json() throwing on the
    // error body: that route happened to catch a 500, but a 2xx carrying
    // a bad body was indistinguishable from a dead server.
    if (!res.ok) { markStale("server returned " + res.status); return; }
    data = await res.json();
  } catch (e) {
    markStale("fetch failed");
    return;
  }

  document.getElementById("stats").innerHTML = `
    <div class="stat"><div class="n">${data.stats.recent_run_count}</div><div class="l">recent runs</div></div>
    <div class="stat"><div class="n">${fmtCost(data.stats.recent_cost_usd)}</div><div class="l">recent cost</div></div>
    <div class="stat"><div class="n">${data.stats.recent_error_count}</div><div class="l">errors</div></div>
    <div class="stat"><div class="n">${data.in_progress.length}</div><div class="l">in flight</div></div>
  `;

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
  ip.innerHTML = data.in_progress.length
    ? data.in_progress.map(r => `<span class="pill live">${esc(r)}</span>`).join("")
    : `<span class="empty">nothing in flight</span>`;
  ip.className = "";

  renderGarden(data.garden_rows);

  // The data-label attributes are what the narrow-viewport card layout
  // renders via td::before once the thead is hidden — see the stylesheet.
  document.getElementById("runs").innerHTML = data.runs.map(r => `
    <tr>
      <td class="time" data-label="Time">${shortTime(r.timestamp)}</td>
      <td class="repo" data-label="Repo">${esc(r.repo)}</td>
      <td class="mode" data-label="Mode">${esc(r.mode)}</td>
      <td class="outcome outcome-${esc(r.outcome)}" data-label="Outcome">${esc(r.outcome)}</td>
      <td class="cost" data-label="Cost">${fmtCost(r.cost_usd)}</td>
      <td class="summary" data-label="Summary">${esc(r.summary)}</td>
    </tr>
  `).join("") || `<tr class="is-empty"><td colspan="6" class="empty">no runs recorded yet</td></tr>`;

  // Name the log being tailed, and say plainly when there are others.
  // The in-flight and batch panels above already aggregate every live
  // log; this pre is the one panel that still shows a single file, so an
  // unlabelled tail is the only place a second run could hide.
  const others = Math.max(0, (data.active_logs || []).length - 1);
  document.getElementById("log-path").textContent = data.active_log
    ? "(" + data.active_log + (others ? ` · ${others} other live log${others > 1 ? "s" : ""} not tailed` : "") + ")"
    : "";
  const logEl = document.getElementById("log");
  const wasAtBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  logEl.textContent = data.log_tail.join("\\n") || "(no active log)";
  if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;

  // Last, not first: the heartbeat claims every panel above is showing
  // this snapshot, so it must only be claimed once they actually are.
  markFresh(data.generated_at);
}

// Every poll costs a sqlite aggregate over the whole run history plus a
// tail read of every live log, so a tab left in the background for a
// whole overnight run would compete for CPU and I/O with the dispatched
// runs the page exists to watch. The payload is a full snapshot with no
// incremental state, so skipping polls while hidden loses nothing as
// long as one fires the moment the tab is looked at again.
function tick() { if (!document.hidden) refresh(); }
refresh();
setInterval(tick, 4000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
</script>
</body>
</html>
"""


class _DashboardHandler(BaseHTTPRequestHandler):
    state_dir: Optional[Path] = None

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        # BaseHTTPRequestHandler logs every request to stderr by default;
        # a 4s-polling dashboard would spam gardener's own terminal output
        # with a request line every 4 seconds for no benefit.
        pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        if self.path == "/" or self.path == "/index.html":
            body = PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            payload = build_status(state_dir=self.state_dir)
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
