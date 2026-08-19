"""A registry of gardener's own *live dispatching processes*, so a running
`align`/`tend`/`overnight` can be listed and stopped by name instead of by
hand with `ps` and `kill`.

Why this exists: until this module, a running gardener had no handle. The
process supervisor that started it (devsrv on Android, Task Scheduler on
WSL2, cron/systemd elsewhere — see `docs/OVERNIGHT.md`) knows about the
*supervisor's own* child, which is not always the process actually
dispatching, and knows nothing about the `claude` process underneath it.
Stopping an overnight run therefore meant reading `ps` output, guessing
which pid was gardener, and discovering afterwards that the dispatched
`claude` (and whatever build it had spawned) was still running as an
orphan. That is the exact failure this module removes: gardener records
its own sessions, and `gardener stop` signals a session's whole process
tree.

## Shape: deliberately docker-like

`gardener ps` / `gardener ps -a` / `gardener stop <id>` / `gardener kill
<id>` mirror the docker CLI's names, flags (`-a`, `-q`, `--time`,
`--signal`), and id-prefix matching, because that vocabulary is already in
the operator's fingers. The mapping is only skin deep — a gardener session
is a plain OS process, not a container, and nothing here isolates,
namespaces, or restarts anything.

## Liveness is a lock, not a pid check

Each session holds an exclusive `fcntl.flock` on its own registry file for
its entire run (same stdlib mechanism as `repo_lock.py`). A reader decides
"is this session alive?" by trying to take a *shared* lock non-blocking:
if it gets one, no process holds the file, so that session is over. This
is deliberately not `os.kill(pid, 0)` — an overnight run can outlive its
own logs by hours on a phone that reuses pids freely, and a stale registry
file whose pid has been recycled onto an unrelated process would otherwise
read as "running" and, far worse, be a target `gardener stop` would signal.
The kernel releases a flock when the holder dies however it dies (clean
exit, SIGKILL, or the whole UserLand session being swiped away), so the
registry self-heals with no reaper process and no cleanup on the crash
path.

The pid is still recorded, and still what gets signalled — but only for a
session the lock already proved is alive.

## Stopping kills the tree, not just gardener

A dispatching gardener's real work happens in a `claude` child, which in
turn spawns builds (`mvn`, `npm`, `pytest`). Signalling only gardener
leaves those running and holding the repo's clone directory. `stop`
therefore collects the session pid's descendants from `/proc` first and
signals the whole set, deepest first. `/proc`-walking is Linux-only and
best-effort by design: on a platform without it, or for a process that
exits between the walk and the signal, stopping degrades to signalling
just the pids it could confirm — never to an exception.

Exited session files are kept (a small, bounded number of them, see
`prune_exited`) so `gardener ps -a` can show what recently ran, matching
`docker ps -a`. They are *not* run history — `state.py`'s sqlite db is
still the only durable record of what a run did.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import secrets
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

SESSIONS_DIR_NAME = "sessions"

#: How many *exited* session files to keep for `gardener ps -a`. Live
#: sessions are never pruned regardless of this number. Same reasoning as
#: `run_log.DEFAULT_KEEP`: nothing else deletes these, and an unbounded
#: write-only directory on a phone is a slow leak rather than a
#: theoretical one.
DEFAULT_KEEP_EXITED = 50

#: Default grace period `stop()` gives a session's process tree to exit on
#: SIGTERM before escalating to SIGKILL, matching `docker stop`'s `--time`
#: flag (whose own default is 10s). A gardener session mid-`claude`
#: dispatch has cleanup worth allowing: `cmd_overnight` persists its
#: resume cursor per batch, and `run_log.tee_stderr` restores stderr on
#: the way out even for a `BaseException`.
DEFAULT_STOP_TIMEOUT_SECONDS = 10.0

#: How long to wait between liveness polls while `stop()` is waiting out
#: its grace period.
_POLL_INTERVAL_SECONDS = 0.2

_PPID_RE = re.compile(r"^PPid:\s+(\d+)", re.MULTILINE)


class SessionLookupError(Exception):
    """Raised when an id/target given to `stop`/`kill` matches no session,
    or matches more than one."""


def _default_state_dir() -> Path:
    # Same override and default as state.py/notify.py/overnight.py/
    # repo_lock.py/run_log.py's own `_default_state_dir` — kept as a
    # separate copy rather than a shared import, matching how those modules
    # already each define it themselves.
    override = os.environ.get("GARDENER_STATE_DIR")
    return Path(override) if override else Path.home() / ".local" / "state" / "gardener"


def default_sessions_dir(state_dir: Optional[Path] = None) -> Path:
    """The directory session registry files are written to."""
    base = state_dir or _default_state_dir()
    return base / SESSIONS_DIR_NAME


def new_session_id() -> str:
    """A short hex id, docker-style: long enough that a collision inside one
    sessions directory is not a practical concern, short enough to retype.
    Callers can inject their own generator (see `register`) so tests never
    depend on randomness."""
    return secrets.token_hex(4)


@dataclass(frozen=True)
class Session:
    """One recorded dispatching run. `running` is resolved at read time by
    probing the registry file's lock — it is not stored in the file."""

    id: str
    pid: int
    command: str
    target: str
    started_at: str
    running: bool
    path: Path
    log_path: Optional[Path] = None

    @property
    def short_id(self) -> str:
        return self.id[:8]

    def started_datetime(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self.started_at)
        except (TypeError, ValueError):
            return None

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds since this session started — its runtime while it is
        running, and how long ago it started once it has exited (no end
        time is recorded: a session killed outright never gets to write
        one, and a column that is only sometimes a duration is worse than
        one that is always an age)."""
        started = self.started_datetime()
        if started is None:
            return None
        current = now or datetime.now(timezone.utc)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return max(0.0, (current - started).total_seconds())


def _read_session_file(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _is_locked(path: Path) -> bool:
    """True if some process still holds this session file's exclusive lock.

    A file we cannot open at all is reported as *not* locked: an
    unreadable registry entry is a broken record, and treating it as a live
    session would make it a `stop` target."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return True
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _session_from_file(path: Path) -> Optional[Session]:
    data = _read_session_file(path)
    if data is None:
        return None
    try:
        pid = int(data["pid"])
        session_id = str(data["id"])
        command = str(data["command"])
    except (KeyError, TypeError, ValueError):
        return None
    log_path = data.get("log_path")
    return Session(
        id=session_id,
        pid=pid,
        command=command,
        target=str(data.get("target") or "-"),
        started_at=str(data.get("started_at") or ""),
        running=_is_locked(path),
        path=path,
        log_path=Path(log_path) if log_path else None,
    )


def list_sessions(
    sessions_dir: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    include_exited: bool = False,
) -> list[Session]:
    """Every recorded session, newest first. Unreadable or half-written
    files are skipped rather than raised over — `ps` must stay usable while
    a session is being registered."""
    directory = sessions_dir or default_sessions_dir(state_dir)
    try:
        files = sorted(directory.glob("*.json"))
    except OSError:
        return []
    found: list[Session] = []
    for path in files:
        session = _session_from_file(path)
        if session is None:
            continue
        if session.running or include_exited:
            found.append(session)
    found.sort(key=lambda s: s.started_at, reverse=True)
    return found


def prune_exited(
    sessions_dir: Path,
    keep: int = DEFAULT_KEEP_EXITED,
    now_pid: Optional[int] = None,
) -> list[Path]:
    """Delete all but the `keep` most recent *exited* session files.

    Running sessions are never pruned — a live session's registry file is
    the only thing that makes it stoppable. `now_pid` (the registering
    process, whose own file may not be lockable yet at the moment this
    runs) is likewise never pruned. Best-effort: a file that can't be
    deleted is skipped, not raised over."""
    try:
        files = sorted(sessions_dir.glob("*.json"))
    except OSError:
        return []
    exited: list[tuple[float, Path]] = []
    for path in files:
        session = _session_from_file(path)
        if session is None:
            # A file that isn't a readable session record is exactly what
            # should be cleaned up, but it has no timestamp to rank by;
            # sort it oldest so it goes first.
            exited.append((0.0, path))
            continue
        if session.running or (now_pid is not None and session.pid == now_pid):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        exited.append((mtime, path))
    exited.sort(key=lambda pair: pair[0], reverse=True)
    removed = []
    for _mtime, stale in exited[max(0, keep):]:
        try:
            stale.unlink()
            removed.append(stale)
        except OSError:
            continue
    return removed


@contextlib.contextmanager
def register(
    command: str,
    target: str,
    log_path: Optional[Path] = None,
    sessions_dir: Optional[Path] = None,
    state_dir: Optional[Path] = None,
    id_fn: Callable[[], str] = new_session_id,
    keep_exited: int = DEFAULT_KEEP_EXITED,
    now: Optional[datetime] = None,
) -> Iterator[Optional[Session]]:
    """Record this process as a live session for the duration of the block,
    holding the registry file's exclusive lock the whole time.

    Yields the `Session`, or None if it could not be registered. Failing to
    register must never break a dispatch — same asymmetry as
    `run_log.tee_stderr`, and for the same reason: this is observability
    and control, not the job itself. Every failure path here degrades to
    "no session record, one warning on stderr, run continues"."""
    import sys  # local: only needed on the degraded path's warning

    directory = sessions_dir or default_sessions_dir(state_dir)
    session_id = id_fn()
    started_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    fd = None
    path = directory / f"{session_id}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        payload = {
            "id": session_id,
            "pid": os.getpid(),
            "command": command,
            "target": target,
            "started_at": started_at,
            "log_path": str(log_path) if log_path else None,
        }
        os.write(fd, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
        os.fsync(fd)
    except OSError as e:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        print(
            f"gardener: could not register this run as a session in {directory} "
            f"({e}) — continuing without one; `gardener ps` will not list it and "
            "`gardener stop` will not be able to stop it",
            file=sys.stderr,
        )
        yield None
        return

    prune_exited(directory, keep=keep_exited, now_pid=os.getpid())
    session = Session(
        id=session_id,
        pid=os.getpid(),
        command=command,
        target=target,
        started_at=started_at,
        running=True,
        path=path,
        log_path=Path(log_path) if log_path else None,
    )
    try:
        yield session
    finally:
        # Closing the fd releases the flock, which is what makes this
        # session read as exited. The file itself stays for `ps -a`; the
        # kernel would have released the lock anyway had this process been
        # killed instead of reaching here.
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def resolve(ident: str, sessions: Sequence[Session]) -> Session:
    """Find the one session `ident` names: an exact id, a unique id prefix,
    or an exact target (`owner/repo`, or `garden`). Raises
    `SessionLookupError` naming what to do about it otherwise."""
    exact = [s for s in sessions if s.id == ident]
    if len(exact) == 1:
        return exact[0]
    matches = [s for s in sessions if s.id.startswith(ident) or s.target == ident]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SessionLookupError(
            f"no running session matches '{ident}' — run `gardener ps` to see "
            "what is running, or `gardener ps -a` if it may have already "
            "finished"
        )
    listed = ", ".join(f"{s.short_id} ({s.command} {s.target})" for s in matches)
    raise SessionLookupError(
        f"'{ident}' matches {len(matches)} sessions ({listed}) — give more of "
        "the session id, or run `gardener stop --all` to stop every one"
    )


def descendants(pid: int, proc_root: Path = Path("/proc")) -> list[int]:
    """Every descendant pid of `pid`, deepest first.

    Reads `/proc/<pid>/status`' `PPid:` line rather than shelling out to
    `ps`: stdlib-only, and this runs on a device where `ps`' own output
    format is not guaranteed. Returns `[]` on any platform or permission
    situation where `/proc` can't be walked — a caller that can only
    signal the session's own pid is still better than one that raises."""
    children: dict[int, list[int]] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
        except OSError:
            # The process exited between listing and reading. Normal.
            continue
        m = _PPID_RE.search(status)
        if not m:
            continue
        children.setdefault(int(m.group(1)), []).append(int(entry.name))

    found: list[int] = []
    frontier = [pid]
    seen = {pid}
    while frontier:
        nxt: list[int] = []
        for parent in frontier:
            for child in children.get(parent, []):
                if child in seen:
                    # A pid cycle is impossible in a sane process table,
                    # but this loop must terminate against a /proc read
                    # that was inconsistent mid-walk.
                    continue
                seen.add(child)
                found.append(child)
                nxt.append(child)
        frontier = nxt
    # Deepest first, so a build's children are signalled before the shell
    # that would otherwise be told about their exit.
    return list(reversed(found))


def _signal_pids(pids: Sequence[int], sig: int, kill_fn: Callable[[int, int], None]) -> list[int]:
    """Signal each pid, skipping the ones already gone. Returns the pids
    actually signalled."""
    signalled = []
    for pid in pids:
        try:
            kill_fn(pid, sig)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
        except OSError:
            continue
        signalled.append(pid)
    return signalled


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, just not ours to signal.
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class StopResult:
    """What `stop`/`kill` actually did, so the CLI can report it honestly
    rather than assuming the signal worked."""

    session: Session
    signalled: list[int]
    escalated: bool
    stopped: bool


def stop(
    session: Session,
    sig: int = signal.SIGTERM,
    timeout: float = DEFAULT_STOP_TIMEOUT_SECONDS,
    escalate: bool = True,
    kill_fn: Callable[[int, int], None] = os.kill,
    alive_fn: Callable[[int], bool] = pid_alive,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    proc_root: Path = Path("/proc"),
) -> StopResult:
    """Signal `session`'s whole process tree and wait for the session's own
    pid to go away, escalating to SIGKILL after `timeout` if it hasn't.

    `escalate=False` (what `gardener kill` uses) sends the signal once and
    reports whether the process is gone, without waiting or escalating."""
    tree = descendants(session.pid, proc_root=proc_root) + [session.pid]
    signalled = _signal_pids(tree, sig, kill_fn)
    if not escalate:
        return StopResult(
            session=session,
            signalled=signalled,
            escalated=False,
            stopped=not alive_fn(session.pid),
        )

    deadline = monotonic_fn() + max(0.0, timeout)
    while alive_fn(session.pid) and monotonic_fn() < deadline:
        sleep_fn(_POLL_INTERVAL_SECONDS)
    if not alive_fn(session.pid):
        # Descendants can outlive the parent that was signalled with it —
        # a `claude` that ignored SIGTERM, or a build it had spawned. Sweep
        # whatever is left of the original tree.
        leftovers = [p for p in tree if p != session.pid and alive_fn(p)]
        if leftovers:
            _signal_pids(leftovers, signal.SIGKILL, kill_fn)
        return StopResult(session=session, signalled=signalled, escalated=False, stopped=True)

    remaining = descendants(session.pid, proc_root=proc_root) + [session.pid]
    _signal_pids(remaining, signal.SIGKILL, kill_fn)
    deadline = monotonic_fn() + max(0.0, timeout)
    while alive_fn(session.pid) and monotonic_fn() < deadline:
        sleep_fn(_POLL_INTERVAL_SECONDS)
    return StopResult(
        session=session,
        signalled=signalled,
        escalated=True,
        stopped=not alive_fn(session.pid),
    )


def format_age(seconds: Optional[float]) -> str:
    """`1d02h`, `03:14`, `47s` — narrow by intent; `ps` output has to stay
    readable on a phone terminal."""
    if seconds is None:
        return "-"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60:02d}:{total % 60:02d}"
    if total < 86400:
        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}"
    return f"{total // 86400}d{(total % 86400) // 3600:02d}h"
