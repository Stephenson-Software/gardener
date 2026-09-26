"""SQLite-backed run history for gardener.

One row per dispatched `claude` run gardener records — `align`, `tend`,
and the one-off `create-dev-loop` bootstrap dispatch that `tend` runs
first when a target repo has no `<slug>-dev-loop` skill yet (see
`cli.py`'s `_dispatch_tend`). This is deliberately the only place
gardener keeps state across runs — no config daemon, just a local db file
next to everything else gardener caches (`~/.local/state/gardener/` by
default, overridable for tests via `GARDENER_STATE_DIR`).

A hub (`hub.py`, RFC 0007) is an optional *copy*, not a replacement: the
local file is always written first and stays the device's record. The hub
stores the same `runs` table, fed by every device's pushes, which is why
every reader here orders by time rather than by `id` (`NEWEST_FIRST`).
"""
from __future__ import annotations

import os
import sqlite3
import uuid
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
    claude_session_id TEXT,
    run_uuid TEXT,
    device TEXT,
    pushed_at TEXT
);
"""

#: Columns added after the original schema, in the order `_migrate` adds
#: them to a db created before they existed. `CREATE TABLE IF NOT EXISTS`
#: is a no-op on an existing table, so an older db only gains these through
#: `_migrate`; a new one is created with them already present.
#:
#: - `run_uuid` is a run's identity across devices. `id` is a per-file
#:   autoincrement, so two devices' run #412 are different runs, and a
#:   store that combines them (the hub, RFC 0007) needs a key that
#:   doesn't collide.
#: - `device` is which machine dispatched the run (`notify.load_device_name`,
#:   the same name an alert's footer carries).
#: - `pushed_at` is when a hub acknowledged the row, NULL until then. It is
#:   only meaningful in a device's local store.
ADDED_COLUMNS = (
    ("run_uuid", "TEXT"),
    ("device", "TEXT"),
    ("pushed_at", "TEXT"),
)

#: The index statements `_migrate` ensures. The unique index is what makes
#: `run_uuid` an identity rather than a label: a second row claiming the
#: same uuid is refused by sqlite, not by every caller remembering to
#: check. The timestamp index serves the recency ordering every reader
#: below uses.
INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS runs_run_uuid ON runs(run_uuid)",
    "CREATE INDEX IF NOT EXISTS runs_timestamp ON runs(timestamp)",
)

#: Namespace for the deterministic uuids `_migrate` gives rows recorded
#: before `run_uuid` existed. Fixed forever: changing it would give every
#: backfilled row a second identity.
LEGACY_RUN_NAMESPACE = uuid.UUID("5b8f7a4e-3c1d-4e2a-9f60-6a7d2c0b9e11")

#: Recency order for every reader. Not `id`: in a store that combines
#: devices, rows arrive in push order, so a phone that was offline all
#: night inserts its runs after the box's morning ones, and `id` order
#: would interleave two nights. `now_iso()` writes fixed-width UTC, so the
#: string order is the time order; `id` breaks the ties that
#: second-resolution timestamps produce inside one concurrent batch.
#: A row whose timestamp doesn't start like a date (the db is a plain file
#: an operator can edit) has no place in time at all, so it sorts after
#: every readable row: as "newest" it would stand in for the latest
#: session and hide the real one.
NEWEST_FIRST = (
    "ORDER BY (timestamp GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*') DESC, "
    "timestamp DESC, id DESC"
)


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
    run_uuid: Optional[str] = None
    device: Optional[str] = None


def _row_to_run(r: sqlite3.Row) -> Run:
    """A `runs` row as a `Run`, tolerating a db that predates the
    `ADDED_COLUMNS`. Readers never migrate (see `_connect`), so a dashboard
    polling a db no writer has opened since upgrading still sees the old
    columns only, and must render rather than raise."""
    keys = r.keys()
    return Run(
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
        run_uuid=r["run_uuid"] if "run_uuid" in keys else None,
        device=r["device"] if "device" in keys else None,
    )


def local_device_name() -> str:
    """This device's name, for rows it records. Deferred import: `notify`
    owns the resolution (env, then notify.env, then hostname) so an alert
    and a run row can never name the same machine differently."""
    from gardener import notify

    return notify.load_device_name()


def legacy_run_uuid(device: str, row_id: int, timestamp: str) -> str:
    """The uuid `_migrate` assigns a row recorded before `run_uuid` existed.

    Deterministic rather than random so the backfill needs no
    bookkeeping to be safe: it runs inside one transaction, and the same
    row always gets the same value."""
    return str(uuid.uuid5(LEGACY_RUN_NAMESPACE, f"{device}:{row_id}:{timestamp}"))


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a db created by an older gardener up to `ADDED_COLUMNS`.

    Additive only: columns are added, never dropped or retyped, and the
    only rows touched are the ones whose new columns are NULL. Every row a
    device's local store holds before this runs was dispatched by that
    device (the store was never shared), so a NULL `device` is filled with
    this device's name. All in one transaction, so an interrupted
    migration leaves the db as it was.

    `BEGIN IMMEDIATE` takes the write lock *before* reading which columns
    exist: `overnight` records from several threads, and on the first run
    after an upgrade two of them would otherwise both see a column missing,
    both `ALTER TABLE`, and the loser's `duplicate column` error would cost
    that run its record."""
    if not _needs_migration(conn):
        for statement in INDEXES:
            conn.execute(statement)
        conn.commit()
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        present = _columns(conn)
        for name, kind in ADDED_COLUMNS:
            if name not in present:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")
        device = local_device_name()
        conn.execute("UPDATE runs SET device = ? WHERE device IS NULL", (device,))
        rows = conn.execute(
            "SELECT id, device, timestamp FROM runs WHERE run_uuid IS NULL"
        ).fetchall()
        conn.executemany(
            "UPDATE runs SET run_uuid = ? WHERE id = ?",
            [(legacy_run_uuid(d, i, t), i) for i, d, t in rows],
        )
        for statement in INDEXES:
            conn.execute(statement)
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(runs)")}


def _needs_migration(conn: sqlite3.Connection) -> bool:
    """Whether any `ADDED_COLUMNS` is missing or any row lacks an identity.
    Checked without a lock on every writer open; the common case (an
    already-migrated db) costs a pragma and one scan of a table that grows
    by a row per dispatch."""
    if any(name not in _columns(conn) for name, _ in ADDED_COLUMNS):
        return True
    return conn.execute(
        "SELECT 1 FROM runs WHERE run_uuid IS NULL OR device IS NULL LIMIT 1"
    ).fetchone() is not None


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
        _migrate(conn)
    return conn


def record_run(run: Run, db_path: Optional[Path] = None) -> int:
    """Insert a run row, return its id.

    A run without a `run_uuid` or `device` gets them here, and they are
    written back onto `run` so the caller holds the identity the row was
    stored under."""
    db_path = db_path or default_db_path()
    if run.run_uuid is None:
        run.run_uuid = str(uuid.uuid4())
    if run.device is None:
        run.device = local_device_name()
    with closing(_connect(db_path)) as conn:
        cur = conn.execute(
            """
            INSERT INTO runs
                (repo, timestamp, mode, gap_summary, outcome,
                 exit_code, duration_ms, cost_usd, claude_session_id,
                 run_uuid, device)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                run.run_uuid,
                run.device,
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
                f"""
                SELECT * FROM runs WHERE repo = ?
                {NEWEST_FIRST} LIMIT ?
                """,
                (repo, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM runs {NEWEST_FIRST} LIMIT ?", (limit,)
            ).fetchall()
    return [_row_to_run(r) for r in rows]


def runs_since(since: datetime, db_path: Optional[Path] = None) -> list[Run]:
    """Every run recorded at or after `since`, oldest first.

    `list_runs` is a row window, which is the wrong shape for "what has
    this overnight invocation finished so far" and "what was spent in the
    last five hours" — both are time windows, and a row count reaches back
    however far it has to. The comparison is done on parsed timestamps
    rather than as a SQL string comparison, for the same reason
    `_parse_timestamp` is defensive: the db is a plain file an operator can
    edit. The scan is newest-first (`NEWEST_FIRST`) and stops at the first
    row older than `since`, so it reads the window and one row more rather
    than the whole table."""
    db_path = db_path or default_db_path()
    if not db_path.exists():
        return []
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    found: list[Run] = []
    with closing(_connect(db_path, ensure_schema=False)) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(f"SELECT * FROM runs {NEWEST_FIRST}"):
            when = _parse_timestamp(r["timestamp"])
            if when is not None and when < since:
                break
            found.append(_row_to_run(r))
    found.reverse()
    return found


def latest_success_at(repo: str, mode: str, db_path: Optional[Path] = None) -> Optional[datetime]:
    """When `repo` last recorded a `mode` run whose outcome is in
    `SUCCESS_OUTCOMES`, or None if it never has (or the db doesn't exist).

    Every writer records `timestamp` *after* the dispatch returns, so this
    is when the newest successful run finished, not when it started — which
    is what `cli.py`'s `find_orphaned_pr` needs: a `tend` that completed
    after a marked PR was opened either opened that PR itself or was handed
    it as a continuation, and either way ended deliberately rather than
    being interrupted (issue #164). Rows whose timestamp can't be parsed are
    skipped rather than raised, per `_parse_timestamp`."""
    db_path = db_path or default_db_path()
    if not db_path.exists():
        return None
    placeholders = ",".join("?" for _ in SUCCESS_OUTCOMES)
    with closing(_connect(db_path, ensure_schema=False)) as conn:
        rows = conn.execute(
            f"SELECT timestamp FROM runs WHERE repo = ? AND mode = ? AND outcome IN ({placeholders})",
            (repo, mode, *sorted(SUCCESS_OUTCOMES)),
        ).fetchall()
    parsed = [when for (value,) in rows if (when := _parse_timestamp(value)) is not None]
    return max(parsed, default=None)


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
    rather than the whole table even though the query has no LIMIT. The
    `NEWEST_FIRST` ordering is a sort over the table, which at one row per
    dispatch is a few thousand rows.

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
            f"FROM runs {NEWEST_FIRST}"
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
        # The newest row per repo, for its outcome, in `NEWEST_FIRST` order:
        # the timestamp first, because in a combined store `MAX(id)` is the
        # row pushed last, not the run that finished last; then `id`,
        # because timestamps are second-resolution and two runs of a
        # concurrent batch recorded in the same second would tie.
        latest = {
            r["repo"]: r["outcome"]
            for r in conn.execute(
                f"""
                SELECT repo, outcome FROM runs AS outer_runs
                WHERE id = (
                    SELECT id FROM runs WHERE repo = outer_runs.repo {NEWEST_FIRST} LIMIT 1
                )
                """
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


def pending_push(db_path: Optional[Path] = None, limit: int = 100) -> list[Run]:
    """Runs a hub has not acknowledged yet (`pushed_at IS NULL`), oldest
    first, so an outbox that drains in several batches sends history in
    the order it happened. Reads through the writer path: the outbox only
    exists once the migration has added `pushed_at`."""
    db_path = db_path or default_db_path()
    if not db_path.exists():
        return []
    with closing(_connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM runs WHERE pushed_at IS NULL ORDER BY timestamp, id LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_run(r) for r in rows]


def pending_push_summary(db_path: Optional[Path] = None) -> tuple[int, Optional[str]]:
    """How many runs are waiting for a hub, and the timestamp of the oldest.
    A read: an unmigrated db has no outbox column yet, so it reports
    every row as waiting, which is what the first push will send."""
    db_path = db_path or default_db_path()
    if not db_path.exists():
        return 0, None
    with closing(_connect(db_path, ensure_schema=False)) as conn:
        if "pushed_at" not in _columns(conn):
            count, oldest = conn.execute("SELECT COUNT(*), MIN(timestamp) FROM runs").fetchone()
        else:
            count, oldest = conn.execute(
                "SELECT COUNT(*), MIN(timestamp) FROM runs WHERE pushed_at IS NULL"
            ).fetchone()
    return count, oldest


def mark_pushed(run_uuids: list[str], when: str, db_path: Optional[Path] = None) -> None:
    """Record that a hub acknowledged these runs. Only the uuids the hub
    listed as held are passed here, so a run the hub refused stays in the
    outbox."""
    if not run_uuids:
        return
    db_path = db_path or default_db_path()
    with closing(_connect(db_path)) as conn:
        conn.executemany(
            "UPDATE runs SET pushed_at = ? WHERE run_uuid = ? AND pushed_at IS NULL",
            [(when, u) for u in run_uuids],
        )
        conn.commit()


def insert_runs(runs: list[Run], db_path: Optional[Path] = None) -> list[str]:
    """Store runs pushed from a device, and return the uuids of every one
    of them the store now holds.

    Idempotent by `run_uuid`: a run already present is left alone, not
    updated, and still reported as held. A device whose previous push
    succeeded but whose copy of the response was lost resends the same
    batch, and this is what makes that safe."""
    if not runs:
        return []
    db_path = db_path or default_db_path()
    with closing(_connect(db_path)) as conn:
        with conn:
            conn.executemany(
                """
                INSERT INTO runs
                    (repo, timestamp, mode, gap_summary, outcome,
                     exit_code, duration_ms, cost_usd, claude_session_id,
                     run_uuid, device)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_uuid) DO NOTHING
                """,
                [
                    (r.repo, r.timestamp, r.mode, r.gap_summary, r.outcome,
                     r.exit_code, r.duration_ms, r.cost_usd, r.claude_session_id,
                     r.run_uuid, r.device)
                    for r in runs
                ],
            )
        uuids = [r.run_uuid for r in runs]
        placeholders = ",".join("?" for _ in uuids)
        held = {
            row[0]
            for row in conn.execute(
                f"SELECT run_uuid FROM runs WHERE run_uuid IN ({placeholders})", uuids
            )
        }
    return [u for u in uuids if u in held]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
