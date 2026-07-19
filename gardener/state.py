"""SQLite-backed run history for gardener.

One row per dispatched `claude` run gardener records — `align`, `tend`,
and the `create-dev-loop` bootstrap `tend` runs first when a target repo
has no `<slug>-dev-loop` skill yet (see `cli.py`'s `cmd_align`/
`_dispatch_tend`). This is deliberately the only place gardener keeps
state across runs — no config daemon, no server, just a local db file
next to everything else gardener caches (`~/.local/state/gardener/` by
default, overridable for tests via `GARDENER_STATE_DIR`).
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
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


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
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
    with closing(_connect(db_path)) as conn:
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
