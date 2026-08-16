"""SQLite-backed run history for gardener.

One row per dispatched `claude` run gardener records — `align`, `tend`,
and the one-off `create-dev-loop` bootstrap dispatch that `tend` runs
first when a target repo has no `<slug>-dev-loop` skill yet (see
`cli.py`'s `_dispatch_tend`). This is deliberately the only place
gardener keeps state across runs — no config daemon, no server, just a
local db file next to everything else gardener caches
(`~/.local/state/gardener/` by default, overridable for tests via
`GARDENER_STATE_DIR`).
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    mode TEXT NOT NULL,
    gap_summary TEXT,
    outcome TEXT NOT NULL,
    exit_code INTEGER,
    duration_ms INTEGER,
    cost_usd REAL,
    claude_session_id TEXT
);
"""


def default_state_dir() -> Path:
    override = os.environ.get("GARDENER_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "gardener"


def default_db_path() -> Path:
    return default_state_dir() / "gardener.sqlite3"


#: The one outcome `cli.py` records for a dispatch that failed.
ERROR_OUTCOME = "error"

#: The two outcomes of the create-dev-loop bootstrap dispatch `tend` runs
#: when a target repo has no `<slug>-dev-loop` skill yet. Named here (and
#: used by `cli.py`'s `_run_tend_dispatch`) rather than spelled as bare
#: literals at the record site, so the classification below and the code
#: that produces these values can't drift apart — every other non-error
#: outcome is a `Mode` value, which `tests/test_cli.py` already pins to
#: this module; these two are the pair that would otherwise be free-
#: floating strings in three files.
CREATED_OUTCOME = "created"
CREATED_INCOMPLETE_OUTCOME = "created_incomplete"

#: Outcomes `cli.py` records for a dispatch that actually did its job.
#: Kept as a set rather than `outcome != ERROR_OUTCOME` so a future outcome
#: has to be classified deliberately instead of silently counting as a
#: success — but a value in *neither* this set nor `ERROR_OUTCOME` is not
#: "deliberate", it's invisible: `repo_stats()` counts it as neither, so
#: the dashboard's garden view draws a repo whose runs all succeeded as a
#: struggling plant with zero tends. That is exactly what `implement`,
#: `file-issue`, and `created_incomplete` did until issue #67; see
#: `KNOWN_OUTCOMES` below for the guard against it recurring.
SUCCESS_OUTCOMES = frozenset({
    # `cmd_align` records the mode's own name on success, so every
    # non-error `align` outcome is a `Mode` value verbatim.
    "report",
    "implement",
    "file-issue",
    # `_run_tend_dispatch`, likewise.
    "tend",
    # The create-dev-loop bootstrap dispatch. `CREATED_INCOMPLETE_OUTCOME`
    # counts as a success too: it means the skill *was* created and usable
    # (`_run_tend_dispatch` goes straight on to the real tend dispatch
    # after it) — what's incomplete is create-dev-loop's own Step 6 GitHub
    # tracker repo, which says nothing about how the target repo is doing.
    CREATED_OUTCOME,
    CREATED_INCOMPLETE_OUTCOME,
})

#: Every outcome value `cli.py` can record. Nothing reads this at runtime;
#: it exists so `tests/test_state.py` can assert the classification above
#: covers the whole vocabulary, and `tests/test_cli.py` that every `Mode`
#: value a run records verbatim is in it.
KNOWN_OUTCOMES = SUCCESS_OUTCOMES | {ERROR_OUTCOME}


@dataclass
class RepoStats:
    """All-time aggregate of one repo's run history.

    Deliberately whole-history, not the `list_runs(limit=N)` window the
    dashboard's Recent runs table uses: this is what "how well established
    is this plant" is drawn from, and a repo tended steadily for a week
    shouldn't look like a seedling just because it's outside the last 40
    rows."""

    repo: str
    runs: int
    successes: int
    errors: int
    first_run: Optional[str] = None
    last_run: Optional[str] = None
    last_success: Optional[str] = None
    last_outcome: Optional[str] = None
    cost_usd: float = 0.0
    duration_ms: int = 0


#: How long a quiet gap has to be before the runs either side of it count
#: as separate sessions. Six hours is chosen from what the run history
#: actually looks like, not picked round: inside one `overnight` batch the
#: gap between two recorded runs is at most one dispatch
#: (`dispatch.TEND_DEFAULT_TIMEOUT_SECONDS`, well under an hour), while the
#: gap between one night's run and the next is most of a waking day. Any
#: threshold between those two separates nights without splitting one.
SESSION_GAP_SECONDS = 6 * 3600

#: The longest a session may span in total, however unbroken it is. The gap
#: rule alone chains: an `overnight` rotation ending at 03:00 and a manual
#: `tend` at 08:30 are under the threshold apart, so they fold together,
#: and every further sub-gap run extends the chain — which is the
#: multi-night straddle this window exists to remove, arrived at the long
#: way round. A day is the outer bound because "Latest session" can then
#: never mean "the last three days"; it also bounds the walk below, which
#: would otherwise have no ceiling on the rows it reads per poll.
MAX_SESSION_SPAN_SECONDS = 24 * 3600


@dataclass
class SessionStats:
    """The most recent unbroken burst of activity in the run history.

    Deliberately *not* the `list_runs(limit=N)` window: the dashboard panel
    these feed is the one an operator reads to answer "how did tonight go",
    and a fixed row count routinely straddles two nights, attributing a
    previous night's failures and spend to this one (issue #105). A gap of
    `SESSION_GAP_SECONDS` with nothing recorded in it ends the session, so
    the window is however long the activity actually was — one repo tended
    by hand, or a full overnight rotation.

    `started_at`/`ended_at` are the raw timestamp strings of the oldest and
    newest run in the window, so a caller can say *which* window it is
    showing rather than leaving the reader to assume."""

    runs: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    #: The session's error rows themselves, newest first — not just the
    #: count above. A count alone cannot say *what* failed, and the
    #: failures in a real overnight run are overwhelmingly one systemic
    #: cause repeated across many repos (an exhausted session limit, a
    #: `claude` that left PATH, a cached clone stuck dirty), which reads
    #: as N unrelated repo failures when only the total is shown
    #: (issue #136). Collected during the same walk the count comes from,
    #: so this costs no extra query.
    errors_detail: list["Run"] = field(default_factory=list)


@dataclass
class Run:
    repo: str
    mode: str
    outcome: str
    timestamp: str
    gap_summary: Optional[str] = None
    exit_code: Optional[int] = None
    duration_ms: Optional[int] = None
    cost_usd: Optional[float] = None
    claude_session_id: Optional[str] = None
    id: Optional[int] = None


def _connect(db_path: Path, ensure_schema: bool = True) -> sqlite3.Connection:
    """Open the run-history db.

    `ensure_schema=False` is the *read* path: a reader should not create
    the state directory, the db file, or the schema as a side effect of
    reading. Readers are guarded by their own `db_path.exists()` check, so
    the table-creating side effect was never what they depended on.

    What this does and does not fix, measured rather than assumed (the
    lock contention originally claimed in issue #121 is not real in the
    steady state, and the note there has been corrected):

    - Table already present, a writer mid-`record_run` holding RESERVED:
      both paths succeed. `CREATE TABLE IF NOT EXISTS` on an existing
      table is a no-op that takes no write lock, so it never contended.
    - A writer holding EXCLUSIVE: both paths fail with `database is
      locked`. A plain SELECT is blocked too in the default
      rollback-journal mode, so this parameter cannot help; what keeps
      that from killing the poll is `dashboard._DashboardHandler`
      returning a real 500 instead of zero bytes. WAL would fix it
      properly and is a larger decision than this.
    - No `runs` table yet and a writer holding RESERVED: `ensure_schema`
      raises `database is locked` where the read path raises `no such
      table`. This is the one case the parameter genuinely changes, and
      it is a narrow first-run window."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    if ensure_schema:
        conn.execute(SCHEMA)
        conn.commit()
    return conn


def record_run(run: Run, db_path: Optional[Path] = None) -> int:
    """Insert a run row, return its id."""
    db_path = db_path or default_db_path()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """
            INSERT INTO runs
                (repo, timestamp, mode, gap_summary, outcome,
                 exit_code, duration_ms, cost_usd, claude_session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.repo,
                run.timestamp,
                run.mode,
                run.gap_summary,
                run.outcome,
                run.exit_code,
                run.duration_ms,
                run.cost_usd,
                run.claude_session_id,
            ),
        )
        conn.commit()
        return cur.lastrowid


def list_runs(
    db_path: Optional[Path] = None,
    repo: Optional[str] = None,
    limit: int = 20,
) -> list[Run]:
    """Most recent runs first, optionally filtered to one repo."""
    db_path = db_path or default_db_path()
    if not db_path.exists():
        return []
    with closing(_connect(db_path, ensure_schema=False)) as conn:
        conn.row_factory = sqlite3.Row
        if repo:
            rows = conn.execute(
                """
                SELECT * FROM runs WHERE repo = ?
                ORDER BY id DESC LIMIT ?
                """,
                (repo, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [
        Run(
            id=r["id"],
            repo=r["repo"],
            timestamp=r["timestamp"],
            mode=r["mode"],
            gap_summary=r["gap_summary"],
            outcome=r["outcome"],
            exit_code=r["exit_code"],
            duration_ms=r["duration_ms"],
            cost_usd=r["cost_usd"],
            claude_session_id=r["claude_session_id"],
        )
        for r in rows
    ]


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """A recorded `timestamp` as an aware datetime, or None if it can't be
    read as one.

    Every timestamp gardener writes comes from `now_iso()` and is UTC and
    aware, but this parses defensively: the db is a plain file an operator
    can edit, and a single unreadable row must not take the dashboard down.
    A naive value is read as UTC rather than rejected, since that is what
    every writer in this repo means by one — and mixing naive with aware
    would raise on the subtraction in `session_stats` rather than degrade."""
    if not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def session_stats(
    db_path: Optional[Path] = None,
    gap_seconds: float = SESSION_GAP_SECONDS,
    max_span_seconds: float = MAX_SESSION_SPAN_SECONDS,
) -> SessionStats:
    """Aggregate of the newest run and every run contiguous with it.

    Walks the history newest-first and stops at the first gap longer than
    `gap_seconds`, or once the window would span more than
    `max_span_seconds` in total — the rows are streamed rather than
    fetched, and the walk breaks out, so this reads at most a day's rows
    rather than the whole table even though the query has no LIMIT (the
    ordering is on the primary key, so there is no sort to pay for either).

    An empty or missing db is a zeroed `SessionStats`, not an error: the
    dashboard renders before anything has ever been dispatched."""
    db_path = db_path or default_db_path()
    stats = SessionStats()
    if not db_path.exists():
        return stats
    newest: Optional[datetime] = None
    previous: Optional[datetime] = None
    with closing(_connect(db_path, ensure_schema=False)) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(
            "SELECT id, repo, mode, timestamp, outcome, gap_summary, cost_usd, duration_ms "
            "FROM runs ORDER BY id DESC"
        ):
            current = _parse_timestamp(row["timestamp"])
            # Measured from the newest run rather than from the previous
            # one: unbroken activity is still capped, so a chain of runs
            # each inside the gap can't quietly grow into a multi-day
            # window under a heading that says "session".
            if (
                newest is not None
                and current is not None
                and (newest - current).total_seconds() > max_span_seconds
            ):
                break
            # An unreadable timestamp ends the session rather than joining
            # it: with no time there is no way to tell which side of a gap
            # the row belongs on, and guessing would silently fold a
            # previous night's numbers into this one — the exact failure
            # this function exists to fix.
            if stats.runs and (
                current is None
                or previous is None
                or (previous - current).total_seconds() > gap_seconds
            ):
                break
            stats.runs += 1
            if row["outcome"] == ERROR_OUTCOME:
                stats.errors += 1
                stats.errors_detail.append(
                    Run(
                        id=row["id"],
                        repo=row["repo"],
                        mode=row["mode"],
                        outcome=row["outcome"],
                        timestamp=row["timestamp"],
                        gap_summary=row["gap_summary"],
                        cost_usd=row["cost_usd"],
                        duration_ms=row["duration_ms"],
                    )
                )
            stats.cost_usd += row["cost_usd"] or 0.0
            stats.duration_ms += row["duration_ms"] or 0
            # Newest row first, so the last one seen is the oldest.
            stats.started_at = row["timestamp"]
            if stats.ended_at is None:
                stats.ended_at = row["timestamp"]
                newest = current
            previous = current
    stats.cost_usd = round(stats.cost_usd, 4)
    return stats


@dataclass
class DayStats:
    """One calendar day's rollup of the run history."""

    day: str
    runs: int
    errors: int
    cost_usd: float = 0.0
    duration_ms: int = 0


def daily_stats(db_path: Optional[Path] = None, days: int = 14) -> list[DayStats]:
    """The last `days` calendar days that have any runs, newest first.

    The only aggregate here that crosses sessions. `session_stats` answers
    "how did tonight go" and `repo_stats` "how is this repo doing", but
    neither can answer "is this getting better or worse" — so a night that
    was a total loss is invisible the moment it stops being the newest one
    (issue #138). Grouped in sqlite rather than folded in Python: the
    history is the whole table, and this runs on every dashboard poll.

    Days are grouped by the raw timestamp prefix, matching how
    `now_iso()` writes them, so this needs no timezone handling of its
    own — and inherits `now_iso()`'s timezone, whatever that is."""
    db_path = db_path or default_db_path()
    if not db_path.exists():
        return []
    with closing(_connect(db_path, ensure_schema=False)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT substr(timestamp, 1, 10) AS day,
                   COUNT(*) AS runs,
                   SUM(CASE WHEN outcome = ? THEN 1 ELSE 0 END) AS errors,
                   SUM(COALESCE(cost_usd, 0)) AS cost_usd,
                   SUM(COALESCE(duration_ms, 0)) AS duration_ms
            FROM runs
            GROUP BY day
            ORDER BY day DESC
            LIMIT ?
            """,
            (ERROR_OUTCOME, days),
        ).fetchall()
    return [
        DayStats(
            day=r["day"],
            runs=r["runs"],
            errors=r["errors"] or 0,
            cost_usd=round(r["cost_usd"] or 0.0, 4),
            duration_ms=int(r["duration_ms"] or 0),
        )
        for r in rows
    ]


def repo_stats(db_path: Optional[Path] = None) -> dict[str, RepoStats]:
    """All-time per-repo aggregates, keyed by `owner/repo`.

    One GROUP BY over the whole `runs` table plus one lookup of each
    repo's newest row — cheap enough for the dashboard's 4 s poll (the db
    is a few hundred rows and grows by one per dispatch), and far cheaper
    than pulling every run into Python to fold there.

    Repos with no recorded run at all are simply absent from the result;
    it's the caller's job to decide what an untended garden member looks
    like, since only the caller knows the garden list."""
    db_path = db_path or default_db_path()
    if not db_path.exists():
        return {}
    placeholders = ",".join("?" for _ in SUCCESS_OUTCOMES)
    successes = sorted(SUCCESS_OUTCOMES)
    with closing(_connect(db_path, ensure_schema=False)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT repo,
                   COUNT(*) AS runs,
                   SUM(CASE WHEN outcome IN ({placeholders}) THEN 1 ELSE 0 END) AS successes,
                   SUM(CASE WHEN outcome = ? THEN 1 ELSE 0 END) AS errors,
                   MIN(timestamp) AS first_run,
                   MAX(timestamp) AS last_run,
                   MAX(CASE WHEN outcome IN ({placeholders}) THEN timestamp END) AS last_success,
                   COALESCE(SUM(cost_usd), 0) AS cost_usd,
                   COALESCE(SUM(duration_ms), 0) AS duration_ms
            FROM runs
            GROUP BY repo
            """,
            (*successes, ERROR_OUTCOME, *successes),
        ).fetchall()
        # The newest row per repo, for its outcome. `MAX(id)` rather than
        # `MAX(timestamp)`: timestamps are second-resolution, so two runs
        # of a concurrent batch recorded in the same second would tie,
        # while the autoincrement id never does.
        latest = {
            r["repo"]: r["outcome"]
            for r in conn.execute(
                "SELECT repo, outcome FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY repo)"
            ).fetchall()
        }
    return {
        r["repo"]: RepoStats(
            repo=r["repo"],
            runs=r["runs"],
            successes=r["successes"],
            errors=r["errors"],
            first_run=r["first_run"],
            last_run=r["last_run"],
            last_success=r["last_success"],
            last_outcome=latest.get(r["repo"]),
            cost_usd=round(r["cost_usd"], 4),
            duration_ms=int(r["duration_ms"]),
        )
        for r in rows
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
