"""Persist a dispatching run's own stderr narration to a file under
`~/.local/state/gardener/logs/`, so something other than the terminal it
was launched from can see what a run is doing while it runs.

Why this exists: every progress line gardener prints — `gardener: tending
<repo> (allow_merge=...)`, `overnight dispatching ... (N-M/T candidates
this run ...)`, `notify: sent to Discord: ...` — goes to stderr and
nowhere else. Whoever launched the process sees them; nothing else can.
`dashboard.py` was written to read exactly these lines back out of "the
active log file" (see its `find_active_log`/`parse_in_progress`), but no
such file was ever written: gardener's own state dir had no `logs/`
directory in it, so the dashboard's live panels ("Tonight", "Currently
tending", "Live log") were permanently empty for a run launched any way
other than a terminal someone was already watching — which is every
unattended run, i.e. the entire case the dashboard exists for.

The fix is deliberately gardener-local: gardener writes its own log, to a
path gardener owns, and the dashboard reads it. It would have been fewer
lines to teach the dashboard where this device's process supervisor
happens to put captured stderr, but that would make gardener's live
visibility depend on being launched by one specific external tool, and
break silently if that tool were reconfigured or replaced. Nothing here
knows how the process was started.

## This must never break a dispatch

Logging is strictly an observability aid: a run that can't open its log
file (read-only state dir, out of disk, a `logs` path that is somehow a
regular file) must still dispatch normally. Every failure path here
degrades to "no log file, one warning on stderr, run continues" — never
an exception that propagates into `cli.py`'s command functions. That
asymmetry is the point; don't tighten it into a hard failure later.

## Tee, not redirect

`sys.stderr` is *wrapped*, not replaced: lines still reach the original
stderr as well as the file, so launching a run in a terminal and watching
it behaves exactly as it did before this module existed. `fileno()` is
passed through to the real stderr, so anything handing `sys.stderr` to a
subprocess still gets a usable file descriptor — output written directly
to that descriptor by a child process bypasses the tee and won't appear
in the log, which is fine: the dashboard only ever parses gardener's own
progress lines, not a dispatched `claude`'s output.
"""
from __future__ import annotations

import contextlib
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional, TextIO

LOGS_DIR_NAME = "logs"

# How many run logs to retain, counting the current run's own. An
# overnight run's log is a few hundred KB at most, but nothing else ever
# deletes these, and this device is a phone — an unbounded write-only
# directory on it is a slow leak, not a theoretical one. Pruning runs once
# per run, just after that run's log is opened; since it keeps the most
# recently modified files and the current run's is always the newest, the
# live log can never be the file that gets deleted.
DEFAULT_KEEP = 30


def _default_state_dir() -> Path:
    # Same override and default as state.py/notify.py/overnight.py/
    # repo_lock.py's own `_default_state_dir` — kept as a separate copy
    # rather than a shared import, matching how those modules already each
    # define it themselves.
    override = os.environ.get("GARDENER_STATE_DIR")
    return Path(override) if override else Path.home() / ".local" / "state" / "gardener"


def default_logs_dir(state_dir: Optional[Path] = None) -> Path:
    """The directory run logs are written to.

    `dashboard.default_logs_dir` resolves the same path from the reading
    side; `tests/test_run_log.py` asserts the two agree, so a change to
    either can't silently leave the dashboard globbing an empty directory
    again."""
    base = state_dir or _default_state_dir()
    return base / LOGS_DIR_NAME


def log_file_name(command: str, started_at: datetime) -> str:
    """`<command>-<YYYYmmdd-HHMMSS>.log` — the `.log` suffix is load-bearing:
    `dashboard.find_active_log` globs for exactly that."""
    return f"{command}-{started_at.strftime('%Y%m%d-%H%M%S')}.log"


def prune_old_logs(logs_dir: Path, keep: int = DEFAULT_KEEP) -> list[Path]:
    """Delete all but the `keep` most recently modified `*.log` files.

    Returns the paths actually removed. Best-effort like everything else
    here: a file that can't be deleted is skipped, not raised over."""
    try:
        logs = sorted(
            (p for p in logs_dir.glob("*.log") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    removed = []
    for stale in logs[keep:]:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError:
            continue
    return removed


class _TeeStream:
    """Writes to two streams. Only the methods a `print(..., file=...)` call
    and a subprocess handoff actually touch are implemented."""

    def __init__(self, primary: TextIO, mirror: TextIO):
        self._primary = primary
        self._mirror = mirror

    def write(self, s: str) -> int:
        n = self._primary.write(s)
        try:
            self._mirror.write(s)
            # Unbuffered by intent: the dashboard polls this file every 4s
            # and a run can sit inside a single `claude` dispatch for
            # minutes. A half-written buffer flushed only at process exit
            # would defeat the entire point of writing the file.
            self._mirror.flush()
        except (OSError, ValueError):
            # Disk filled, or the file was closed underneath us. Keep the
            # run going against the real stderr.
            pass
        return n

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._primary.flush()
        with contextlib.suppress(OSError, ValueError):
            self._mirror.flush()

    def isatty(self) -> bool:
        return self._primary.isatty()

    def fileno(self) -> int:
        return self._primary.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._primary, "encoding", "utf-8")


@contextlib.contextmanager
def tee_stderr(
    command: str,
    logs_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    keep: int = DEFAULT_KEEP,
) -> Iterator[Optional[Path]]:
    """Mirror everything written to `sys.stderr` into a new run log for the
    duration of the block. Yields the log's path, or None if no log could
    be opened (in which case stderr is left exactly as it was).

    `sys.stderr` is restored on the way out no matter how the block exits,
    including on `BaseException` — `cmd_overnight` is specifically designed
    to survive being killed mid-run (see its resume-cursor persistence), so
    a leaked stderr wrapper pointing at a closed file is a real risk, not a
    hypothetical one."""
    directory = logs_dir or default_logs_dir()
    started_at = now or datetime.now()
    handle = None
    path: Optional[Path] = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / log_file_name(command, started_at)
        # Append rather than truncate: two runs started in the same second
        # resolve to the same name, and interleaved lines are a far better
        # outcome than one run's log silently erasing the other's.
        handle = open(path, "a", encoding="utf-8")
    except OSError as e:
        print(
            f"gardener: could not open a run log in {directory} ({e}) — "
            "continuing without one; the dashboard's live panels will be "
            "empty for this run",
            file=sys.stderr,
        )
        yield None
        return

    # max(1, ...) so a caller passing keep=0 can't delete the log this run
    # is about to write into.
    prune_old_logs(directory, keep=max(1, keep))
    original = sys.stderr
    sys.stderr = _TeeStream(original, handle)
    try:
        yield path
    finally:
        sys.stderr = original
        with contextlib.suppress(OSError, ValueError):
            handle.close()
