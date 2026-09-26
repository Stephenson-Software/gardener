"""The dashboard's `/live` view — what an `overnight` run is doing *right
now*, served beside the main page by the same `gardener dashboard` server.

## Why a second page

The main page (`dashboard.PAGE_HTML`) is a history-and-garden view: its
headline panel is a session aggregate, most of its height is the garden
plot, and "what is running" is a list of repo names. Watching a run live
through it meant tailing the raw log to answer three questions it could
not: what each of the `--concurrency` slots is actually doing, whether any
of them has gone quiet, and whether the run is about to abort on an
exhausted usage window — which it did on five of the last seven nights
this was written against. This page answers those, and deliberately
nothing else; history and the garden stay on the main page.

## Where each fact comes from

Nothing here is a new source of truth, same rule as `dashboard.py`:

- **The run** — budget, strategy, start — is `dashboard.parse_overnight_start`
  and `dashboard.log_started_at` over the newest `overnight-*.log`; whether
  its process is still alive is `sessions.list_sessions`, the same
  `fcntl.flock` probe `gardener ps` trusts (never a pid probe — see
  `sessions.py`). A log with no live session behind it is shown as stopped
  even when it has no end line, because `gardener stop` and a crash both
  leave exactly that.
- **The slots** are the repos named on the run's most recent `overnight
  dispatching tend for A, B, … (N-M/T candidates this run)` line.
  `cmd_overnight` waits for a whole batch before starting the next, so a
  slot whose repo has finished sits idle until the slowest one in its batch
  does — the page shows those as finished-and-waiting rather than dropping
  them, since that idle time is exactly what a live view should expose.
- **What a slot is doing** comes from the Claude Code transcript its
  dispatch writes, found through the `session transcript: <path>` line
  `transcript.py` logs, matched to a repo by `transcript.encode_cwd` of the
  clone path from that repo's `checked out at <path>` line — log lines from
  concurrent dispatches interleave, so matching by position would pair a
  repo with a neighbour's transcript. Transcripts are read incrementally
  (`TranscriptCache`) so a 3 s poll re-reads only what was appended.
- **Finished runs and spend** are `state.runs_since` over the sqlite run
  history, the one real outcome record.

## The usage-limit gauge is a measured proxy, not the meter

Claude Code exposes no remaining-quota figure to a headless session; the
transcript records the limit only once it has been hit (an
`isApiErrorMessage` line with `"error": "rate_limit"` and text like
"You've hit your session limit · resets 6am (America/Denver)"). So the
page shows two things instead. First, a hit as soon as any slot's
transcript records one, before the failed run is even recorded. Second,
gardener's own completed-run spend over the trailing `LIMIT_WINDOW_HOURS`
(the length of Claude's session-limit window) beside the same figure
measured just before each recent hit. Measured over the history this was
built against, twelve hits between 2026-09-06 and 2026-09-25 fell between
$69 and $161 of trailing-5 h spend — a band, not a line, because
interactive Claude use counts against the same limit and is invisible
here, and `cost_usd` is the API-equivalent figure `claude -p` reports, not
the plan's own meter. The page says both of those in its caption.
"""
from __future__ import annotations

import json
import re
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from gardener import dashboard, dispatch, garden, overnight, sessions, state, transcript

#: Bumped whenever a key the page reads is renamed, removed, or changes
#: meaning — same contract as `dashboard.PAYLOAD_SCHEMA`: the page refuses
#: a payload it doesn't understand rather than rendering `undefined`.
LIVE_SCHEMA = 1

#: How long a running slot's transcript may go without a new line before
#: the slot is flagged as quiet. A healthy dispatch writes a line per tool
#: call; the longest legitimate silences are single builds/test runs (a
#: cold Gradle or Maven build), which run to a few minutes. Ten minutes
#: flags a genuinely stuck session well inside
#: `dispatch.TEND_DEFAULT_TIMEOUT_SECONDS` without flagging those.
STALL_SECONDS = 10 * 60

#: Claude's session-limit window.
LIMIT_WINDOW_HOURS = 5

#: How far back to look for past limit hits to compare against.
LIMIT_HISTORY_DAYS = 30

#: Rows failing on the limit arrive in a burst — every slot of the batch,
#: then the abort. Rows closer together than this are one hit, dated by the
#: first of them.
HIT_CLUSTER_GAP_SECONDS = 3 * 3600

#: How many recent hits the gauge's band is drawn from.
LIMIT_HITS_SHOWN = 10

#: How much of the run log to read per poll. More than `dashboard`'s 400
#: because a batch's own narration (clone, skill lookup, transcript, every
#: denied tool call) can run long and the batch line has to be inside it.
LOG_TAIL_LINES = 1500

BATCH_DISPATCH_RE = re.compile(
    r"^gardener: overnight dispatching tend for (.+?) \(\d+(?:-\d+)?/\d+ candidates this run"
)
# `org/repo` only — `align` prints the same shape for its conventions clone
# ("conventions checked out at"), which has no slash and is not a slot.
CHECKED_OUT_RE = re.compile(r"^gardener: (\S+/\S+) checked out at (.+)$")
TRANSCRIPT_RE = re.compile(r"^gardener: session transcript: (\S+\.jsonl)")
OVERNIGHT_DONE_RE = re.compile(r"^gardener: overnight done — (.+)$")
OVERNIGHT_ABORT_RE = re.compile(r"^gardener: overnight aborting — (.+?)\. Not advancing")
OVERNIGHT_LOG_GLOB = "overnight-*.log"


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt is not None else None


# --------------------------------------------------------------------------
# Log parsing
# --------------------------------------------------------------------------


def current_batch_lines(lines: list[str]) -> tuple[list[str], list[str]]:
    """(repos in the most recent batch, the log lines from that batch's
    dispatch line onward). Both empty if these lines have no batch line.

    Scoping the rest of the parsing to the current batch's lines is what
    keeps a clone path or transcript from an earlier batch from being
    matched to a slot."""
    for i in range(len(lines) - 1, -1, -1):
        m = BATCH_DISPATCH_RE.match(lines[i])
        if m:
            return [r.strip() for r in m.group(1).split(",") if r.strip()], lines[i:]
    return [], []


def parse_clone_paths(lines: list[str]) -> dict[str, str]:
    """repo -> the cache clone path its dispatch runs in. Last line wins."""
    found: dict[str, str] = {}
    for line in lines:
        m = CHECKED_OUT_RE.match(line)
        if m:
            found[m.group(1)] = m.group(2).strip()
    return found


def parse_transcript_paths(lines: list[str]) -> list[str]:
    """Every transcript path these lines announce, in order."""
    return [m.group(1) for m in (TRANSCRIPT_RE.match(line) for line in lines) if m]


def transcript_for_clone(clone: str, transcript_paths: list[str]) -> Optional[str]:
    """The newest announced transcript written from `clone`'s directory, or
    None. A tend that first bootstraps its dev-loop skill runs two
    `claude` sessions in the same clone; the later one is the tend."""
    encoded = transcript.encode_cwd(clone)
    for path in reversed(transcript_paths):
        if Path(path).parent.name == encoded:
            return path
    return None


def parse_run_end(lines: list[str]) -> Optional[dict]:
    """How the run in these lines ended, or None if it has no end line yet.

    `{"kind": "aborted", "reason": ...}` when `cmd_overnight` printed its
    blocked-failure abort (it prints the ordinary `overnight done` summary
    after it, so the abort has to be looked for first), else
    `{"kind": "done", "summary": ...}`."""
    done = None
    aborted = None
    for line in lines:
        m = OVERNIGHT_ABORT_RE.match(line)
        if m:
            aborted = m.group(1)
            continue
        m = OVERNIGHT_DONE_RE.match(line)
        if m:
            done = m.group(1)
    if done is None and aborted is None:
        return None
    if aborted is not None:
        return {"kind": "aborted", "reason": aborted, "summary": done}
    return {"kind": "done", "reason": None, "summary": done}


# --------------------------------------------------------------------------
# Transcripts
# --------------------------------------------------------------------------

# Input keys worth showing for a tool call, most descriptive first. A Bash
# call's own `description` reads better than its command line when given.
_TOOL_DETAIL_KEYS = (
    "description", "command", "file_path", "notebook_path", "pattern",
    "skill", "url", "query", "prompt",
)


def tool_detail(tool_input, clone: Optional[str] = None, limit: int = 160) -> str:
    """One short human-readable line for a tool call's input. Paths inside
    the slot's own clone are shown relative to it — every one of them
    starts with the same `/root/.cache/gardener/repos/...` prefix, which is
    noise on a card that already names the repo. Other paths under the
    home directory (a skill file, Claude's own memory dir) are shown as
    `~/...` for the same reason — on a phone-width card the full path wraps
    to four lines."""
    if not isinstance(tool_input, dict):
        return ""
    prefix = clone.rstrip("/") + "/" if clone else None
    home = str(Path.home()).rstrip("/") + "/"
    for key in _TOOL_DETAIL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            if prefix:
                value = value.replace(prefix, "")
            if home != "/":
                value = value.replace(home, "~/")
            return _clip(value, limit)
    return ""


def _message_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


class _TranscriptState:
    __slots__ = (
        "offset", "started_at", "last_event_at", "tool_ids", "last_tool",
        "last_text", "tokens_by_message", "rate_limit", "api_error", "model",
    )

    def __init__(self) -> None:
        self.offset = 0
        self.started_at: Optional[str] = None
        self.last_event_at: Optional[str] = None
        self.tool_ids: set = set()
        self.last_tool: Optional[dict] = None
        self.last_text: Optional[dict] = None
        # One message is written as several lines (one per content block),
        # each repeating the message's usage — keyed by message id so it's
        # counted once.
        self.tokens_by_message: dict = {}
        self.rate_limit: Optional[dict] = None
        self.api_error: Optional[dict] = None
        self.model: Optional[str] = None

    def feed(self, raw: str, clone: Optional[str]) -> None:
        try:
            entry = json.loads(raw)
        except ValueError:
            return
        if not isinstance(entry, dict):
            return
        ts = entry.get("timestamp")
        if isinstance(ts, str):
            if self.started_at is None:
                self.started_at = ts
            self.last_event_at = ts
        message = entry.get("message")
        if not isinstance(message, dict):
            return
        if entry.get("isApiErrorMessage") or entry.get("error"):
            text = _clip(_message_text(message.get("content")), 240)
            if entry.get("error") == "rate_limit" or dispatch.looks_like_usage_limit(text, ""):
                self.rate_limit = {"message": text, "at": ts}
            else:
                self.api_error = {"message": text, "at": ts}
            return
        if entry.get("type") != "assistant":
            return
        if isinstance(message.get("model"), str):
            self.model = message["model"]
        usage = message.get("usage")
        if isinstance(usage, dict) and message.get("id"):
            out = usage.get("output_tokens")
            if isinstance(out, int):
                self.tokens_by_message[message["id"]] = out
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                block_id = block.get("id") or f"{ts}:{len(self.tool_ids)}"
                if block_id in self.tool_ids:
                    continue
                self.tool_ids.add(block_id)
                self.last_tool = {
                    "name": block.get("name") or "?",
                    "detail": tool_detail(block.get("input"), clone),
                    "at": ts,
                }
            elif block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"].strip()
                if text:
                    self.last_text = {"text": _clip(text, 280), "at": ts}

    def summary(self) -> dict:
        return {
            "started_at": self.started_at,
            "last_event_at": self.last_event_at,
            "tool_calls": len(self.tool_ids),
            "output_tokens": sum(self.tokens_by_message.values()),
            "last_tool": self.last_tool,
            "last_text": self.last_text,
            "rate_limit": self.rate_limit,
            "api_error": self.api_error,
            "model": self.model,
        }


class TranscriptCache:
    """Incremental per-file transcript summaries.

    A transcript grows to several MB over a long tend, and the page polls
    every few seconds for up to `--concurrency` of them, so each poll reads
    only the bytes appended since the last one. Only whole lines are
    consumed — a line still being written is left for the next poll rather
    than parsed as truncated JSON. A file that shrank is re-read from the
    start. Entries for transcripts no poll asked for are dropped by
    `retain`, so the cache is bounded by the slots on screen."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, _TranscriptState] = {}

    def summary(self, path: str, clone: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            st = self._states.get(path)
            try:
                size = Path(path).stat().st_size
            except OSError:
                return st.summary() if st else None
            if st is None or size < st.offset:
                st = _TranscriptState()
                self._states[path] = st
            if size > st.offset:
                try:
                    with open(path, "rb") as f:
                        f.seek(st.offset)
                        data = f.read(size - st.offset)
                except OSError:
                    return st.summary()
                end = data.rfind(b"\n")
                if end >= 0:
                    st.offset += end + 1
                    for raw in data[: end + 1].decode("utf-8", errors="replace").splitlines():
                        if raw.strip():
                            st.feed(raw, clone)
            return st.summary()

    def retain(self, paths) -> None:
        keep = set(paths)
        with self._lock:
            for path in list(self._states):
                if path not in keep:
                    del self._states[path]


_CACHE = TranscriptCache()


# --------------------------------------------------------------------------
# Spend and the usage limit
# --------------------------------------------------------------------------


def _run_time(run: state.Run) -> Optional[datetime]:
    return state._parse_timestamp(run.timestamp)


def is_limit_failure(run: state.Run) -> bool:
    """A recorded run that failed on the usage window. Only error rows are
    consulted, per `dispatch.looks_like_usage_limit`'s own rule — a repo
    whose summary merely discusses rate limits is not a hit."""
    return run.outcome == state.ERROR_OUTCOME and dispatch.looks_like_usage_limit(
        run.gap_summary or "", ""
    )


def spend_between(runs: list[state.Run], start: datetime, end: datetime) -> float:
    """Summed `cost_usd` of runs recorded in [start, end)."""
    total = 0.0
    for run in runs:
        when = _run_time(run)
        if when is not None and start <= when < end and run.cost_usd:
            total += run.cost_usd
    return total


def limit_hits(
    runs: list[state.Run],
    window_hours: float = LIMIT_WINDOW_HOURS,
    cluster_gap_seconds: float = HIT_CLUSTER_GAP_SECONDS,
) -> list[dict]:
    """One entry per usage-limit hit in `runs` (oldest first): when it
    happened, the limit message (which carries the reset time), how many
    repos failed on it, and the trailing-window spend just before it —
    the figure the live gauge is compared against."""
    hits: list[dict] = []
    last_at: Optional[datetime] = None
    for run in runs:
        if not is_limit_failure(run):
            continue
        when = _run_time(run)
        if when is None:
            continue
        if last_at is not None and (when - last_at).total_seconds() <= cluster_gap_seconds:
            hits[-1]["repos"] += 1
            last_at = when
            continue
        hits.append(
            {
                "at": _iso(when),
                "message": _clip(run.gap_summary or "", 200),
                "repos": 1,
                "trailing_cost_usd": round(
                    spend_between(runs, when - timedelta(hours=window_hours), when), 2
                ),
            }
        )
        last_at = when
    return hits


def build_limit(
    runs: list[state.Run],
    now: datetime,
    live_hit: Optional[dict],
    window_hours: float = LIMIT_WINDOW_HOURS,
    shown: int = LIMIT_HITS_SHOWN,
) -> dict:
    """The usage-limit gauge. `level` is `hit` when a slot has just hit the
    limit (or a run in this invocation already failed on it), `near` when
    trailing spend has reached the lowest figure any recent hit happened
    at, else `ok`."""
    hits = limit_hits(runs, window_hours)[-shown:]
    current = round(spend_between(runs, now - timedelta(hours=window_hours), now + timedelta(seconds=1)), 2)
    trailing = [h["trailing_cost_usd"] for h in hits]
    band = (
        {
            "min": min(trailing),
            "median": round(statistics.median(trailing), 2),
            "max": max(trailing),
        }
        if trailing
        else None
    )
    if live_hit is not None:
        level = "hit"
    elif band is not None and current >= band["min"]:
        level = "near"
    else:
        level = "ok"
    return {
        "level": level,
        "window_hours": window_hours,
        "current_cost_usd": current,
        "band": band,
        "hits": list(reversed(hits)),
        "live_hit": live_hit,
    }


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------


def newest_overnight_log(logs_dir: Path) -> Optional[Path]:
    """The most recently modified `overnight-*.log`, or None. Guarded
    against `run_log.prune_old_logs` deleting a file mid-scan, same as
    `dashboard.find_active_log`."""
    if not logs_dir.exists():
        return None
    dated = []
    for path in logs_dir.glob(OVERNIGHT_LOG_GLOB):
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return max(dated, key=lambda d: d[0])[1] if dated else None


def _session_for(log: Path, state_dir: Path) -> tuple[Optional[sessions.Session], bool]:
    """(the running `overnight` session writing `log`, whether the session
    registry could be consulted at all)."""
    directory = sessions.default_sessions_dir(state_dir)
    if not directory.exists():
        return None, False
    running = [s for s in sessions.list_sessions(sessions_dir=directory) if s.command == "overnight"]
    for s in running:
        if s.log_path is not None and Path(s.log_path) == log:
            return s, True
    # A session registered before `log_path` was recorded: match the only
    # running overnight rather than none.
    unlabeled = [s for s in running if s.log_path is None]
    return (unlabeled[0] if len(unlabeled) == 1 else None), True


def _run_row(run: state.Run) -> dict:
    return {
        "repo": run.repo,
        "mode": run.mode,
        "outcome": run.outcome,
        "ok": run.outcome != state.ERROR_OUTCOME,
        "timestamp": run.timestamp,
        "duration_ms": run.duration_ms,
        "cost_usd": round(run.cost_usd, 2) if run.cost_usd is not None else None,
        "summary": _clip(run.gap_summary or "", 600),
    }


def _seconds_between(earlier: Optional[str], later: datetime) -> Optional[int]:
    when = state._parse_timestamp(earlier)
    if when is None:
        return None
    return max(0, round((later - when).total_seconds()))


def build_slots(
    batch: list[str],
    batch_lines: list[str],
    in_progress: list[str],
    finished_by_repo: dict[str, state.Run],
    run_alive: bool,
    now: datetime,
    cache: TranscriptCache,
) -> list[dict]:
    """One card per repo in the current batch."""
    clones = parse_clone_paths(batch_lines)
    transcripts = parse_transcript_paths(batch_lines)
    in_flight = set(in_progress)
    slots = []
    for repo in batch:
        clone = clones.get(repo)
        tpath = transcript_for_clone(clone, transcripts) if clone else None
        summary = cache.summary(tpath, clone) if tpath else None
        if repo not in in_flight:
            phase = "finished"
        elif not run_alive:
            phase = "stopped"
        elif tpath:
            phase = "running"
        elif clone:
            phase = "preparing"
        else:
            phase = "cloning"
        idle = (
            _seconds_between(summary["last_event_at"], now)
            if summary and phase == "running"
            else None
        )
        finished = finished_by_repo.get(repo)
        slots.append(
            {
                "repo": repo,
                "phase": phase,
                "clone": clone,
                "transcript": tpath,
                "started_at": summary["started_at"] if summary else None,
                "idle_seconds": idle,
                "stalled": idle is not None and idle >= STALL_SECONDS,
                "activity": summary,
                "result": _run_row(finished) if finished and phase == "finished" else None,
            }
        )
    return slots


def _alerts(run: Optional[dict], slots: list[dict], limit: dict) -> list[dict]:
    alerts = []
    hit = limit.get("live_hit")
    if hit:
        alerts.append(
            {
                "level": "error",
                "text": f"Usage limit hit ({hit.get('repo')}): {hit.get('message')}"
                + (" — the run aborts once this batch finishes." if run and run["alive"] else ""),
            }
        )
    if run and not run["alive"] and run["end"] is None:
        alerts.append(
            {
                "level": "warn",
                "text": "No live gardener session is writing this run's log — it was stopped or "
                "the process died. Repos it had in flight never recorded a result.",
            }
        )
    for slot in slots:
        if slot["stalled"]:
            alerts.append(
                {
                    "level": "warn",
                    "text": f"{slot['repo']} has written nothing to its transcript for "
                    f"{slot['idle_seconds'] // 60} min.",
                }
            )
        activity = slot.get("activity") or {}
        if activity.get("api_error") and slot["phase"] == "running":
            alerts.append(
                {"level": "warn", "text": f"{slot['repo']}: API error — {activity['api_error']['message']}"}
            )
    if limit["level"] == "near" and limit.get("band"):
        band = limit["band"]
        alerts.append(
            {
                "level": "warn",
                "text": f"${limit['current_cost_usd']:.0f} spent in the last "
                f"{limit['window_hours']} h — recent usage-limit hits came at "
                f"${band['min']:.0f}–${band['max']:.0f}.",
            }
        )
    running = [s for s in slots if s["phase"] in ("running", "preparing", "cloning")]
    waiting = [s for s in slots if s["phase"] == "finished"]
    if run and run["alive"] and running and waiting:
        names = ", ".join(s["repo"] for s in running)
        alerts.append(
            {
                "level": "info",
                "text": f"{len(waiting)} of {len(slots)} slots idle — the next batch starts when "
                f"{names} finish{'es' if len(running) == 1 else ''}.",
            }
        )
    return alerts


def build_live(
    state_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    cache: Optional[TranscriptCache] = None,
) -> dict:
    base = state_dir or state.default_state_dir()
    db_path = base / "gardener.sqlite3"
    now = now or datetime.now(timezone.utc)
    cache = cache or _CACHE

    history = state.runs_since(now - timedelta(days=LIMIT_HISTORY_DAYS), db_path=db_path)

    log = newest_overnight_log(dashboard.default_logs_dir(base))
    run = None
    slots: list[dict] = []
    finished: list[state.Run] = []
    live_hit = None
    if log is not None:
        head = dashboard.head_lines(log)
        start = dashboard.parse_overnight_start(head)
        lines = dashboard.tail_lines(log, LOG_TAIL_LINES)
        began_epoch = dashboard.log_started_at(log)
        began = (
            datetime.fromtimestamp(began_epoch, tz=timezone.utc) if began_epoch is not None else None
        )
        session, registry_known = _session_for(log, base)
        end = parse_run_end(lines)
        try:
            mtime = datetime.fromtimestamp(log.stat().st_mtime, tz=timezone.utc)
        except OSError:
            mtime = None
        if registry_known:
            alive = session is not None
        else:
            # No registry to consult (a state dir from before `sessions.py`):
            # fall back to the log having no end line and still being written.
            alive = end is None and mtime is not None and (
                (now - mtime).total_seconds() < dashboard.ACTIVE_LOG_WINDOW_SECONDS
            )
        budget_hours = start["budget_hours"] if start else None
        elapsed = (now - began).total_seconds() if began else None
        ends_at = began + timedelta(hours=budget_hours) if began and budget_hours else None
        batch_progress = dashboard.parse_batch_progress(lines)
        batch, batch_lines = current_batch_lines(lines)
        in_progress = dashboard.parse_in_progress(lines)

        if began is not None:
            finished = [r for r in history if (_run_time(r) or now) >= began]
            if not alive and mtime is not None:
                finished = [r for r in finished if (_run_time(r) or now) <= mtime + timedelta(minutes=1)]
        finished_by_repo = {r.repo: r for r in finished}

        slots = build_slots(batch, batch_lines, in_progress, finished_by_repo, alive, now, cache)
        cache.retain(s["transcript"] for s in slots if s["transcript"])

        for slot in slots:
            hit = (slot.get("activity") or {}).get("rate_limit")
            if hit and slot["phase"] in ("running", "finished", "stopped"):
                live_hit = {"repo": slot["repo"], "message": hit["message"], "at": hit["at"]}
                break
        if live_hit is None:
            for r in finished:
                if is_limit_failure(r):
                    live_hit = {"repo": r.repo, "message": _clip(r.gap_summary or "", 200), "at": r.timestamp}
                    break

        ok_runs = [r for r in finished if r.outcome != state.ERROR_OUTCOME]
        durations = [r.duration_ms for r in finished if r.duration_ms]
        cost = sum(r.cost_usd or 0 for r in finished)
        hours_in = (elapsed / 3600) if elapsed else None
        remaining = (
            max(0.0, budget_hours * 3600 - elapsed) if budget_hours and elapsed is not None else None
        )
        per_hour = (len(finished) / hours_in) if hours_in and hours_in >= 0.25 and finished else None
        candidates_left = (
            max(0, batch_progress[2] - batch_progress[1]) if batch_progress else None
        )
        projected = None
        if alive and per_hour is not None and remaining is not None:
            projected = round(per_hour * remaining / 3600)
            if candidates_left is not None:
                projected = min(projected, candidates_left)

        run = {
            "log": str(log),
            "session_id": session.short_id if session else None,
            "pid": session.pid if session else None,
            "alive": alive,
            "strategy": start["strategy"] if start else None,
            "budget_hours": budget_hours,
            "started_at": _iso(began),
            "ends_at": _iso(ends_at),
            "last_write_at": _iso(mtime),
            "elapsed_seconds": round(elapsed) if elapsed is not None else None,
            "remaining_seconds": round(remaining) if remaining is not None else None,
            "concurrency": len(batch) or None,
            "batch": (
                {"start": batch_progress[0], "end": batch_progress[1], "total": batch_progress[2]}
                if batch_progress
                else None
            ),
            "end": end,
            "pace": {
                "finished": len(finished),
                "ok": len(ok_runs),
                "errors": len(finished) - len(ok_runs),
                "cost_usd": round(cost, 2),
                "avg_duration_seconds": round(sum(durations) / len(durations) / 1000)
                if durations
                else None,
                "per_hour": round(per_hour, 1) if per_hour is not None else None,
                "cost_per_hour": round(cost / hours_in, 2) if hours_in and hours_in >= 0.25 else None,
                "candidates_left": candidates_left,
                "projected_more": projected,
            },
        }

    garden_repos = dashboard._safe_list(lambda: garden.list_garden(path=base / "garden.json"))
    cursor_path = base / "overnight_cursor.json"
    cycle = {
        "garden_size": len(garden_repos),
        "attempted": len(overnight.read_attempted(path=cursor_path)),
        "next_index": overnight.read_cursor(path=cursor_path),
    }

    limit = build_limit(history, now, live_hit)
    return {
        "schema": LIVE_SCHEMA,
        "generated_at": _iso(now),
        "run": run,
        "cycle": cycle,
        "slots": slots,
        "finished": [_run_row(r) for r in reversed(finished)][:60],
        "limit": limit,
        "alerts": _alerts(run, slots, limit),
    }


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

LIVE_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gardener · live</title>
<style>
  /* Same palette as the main dashboard page, so the two read as one tool. */
  :root {
    color-scheme: dark light;
    --bg: #14171a; --panel: #1c2023; --panel-2: #22272a; --text: #e7ece8; --muted: #9aa39c;
    --border: #2c3236; --accent: #5fbf85; --warn: #e3a35a; --err: #ef6a63; --info: #7fb2d9;
    --track: #2a3034; --band: rgba(227,163,90,.22);
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f5f6f4; --panel: #ffffff; --panel-2: #f3f5f2; --text: #1b1f1c; --muted: #5b645d;
      --border: #dfe3de; --accent: #2f7a4f; --warn: #8f5010; --err: #b3261e; --info: #2d628c;
      --track: #e6e9e5; --band: rgba(143,80,16,.16);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    -webkit-text-size-adjust: 100%; overflow-wrap: anywhere;
  }
  a { color: inherit; }
  header {
    position: sticky; top: 0; z-index: 2; background: var(--bg);
    border-bottom: 1px solid var(--border); padding: .75rem 1.25rem;
    display: flex; align-items: baseline; gap: .25rem 1rem; flex-wrap: wrap;
  }
  header h1 { font-size: 1.05rem; margin: 0; }
  header .sub { color: var(--muted); font-size: .85rem; }
  header nav { margin-left: auto; font-size: .85rem; }
  header nav a { color: var(--muted); }
  body.stale main { filter: saturate(.3); opacity: .7; }
  body.stale #beat { color: var(--warn); }
  main { max-width: 1200px; margin: 0 auto; padding: 1.25rem; display: grid; gap: 1.25rem; }
  section > h2 {
    font-size: .75rem; text-transform: uppercase; letter-spacing: .06em;
    color: var(--muted); margin: 0 0 .6rem; font-weight: 600;
  }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 1rem 1.2rem; }
  .muted { color: var(--muted); }
  .mono { font-family: var(--mono); font-size: .85em; }
  .num { font-variant-numeric: tabular-nums; }

  /* Hero */
  #hero { display: grid; gap: .9rem; }
  .status-line { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem 1rem; }
  .state { display: inline-flex; align-items: center; gap: .5rem; font-size: 1.35rem; font-weight: 650; }
  .dot { width: .7rem; height: .7rem; border-radius: 50%; background: var(--muted); flex: none; }
  .dot.live { background: var(--accent); box-shadow: 0 0 0 0 var(--accent); animation: pulse 2s infinite; }
  .dot.warn { background: var(--warn); } .dot.err { background: var(--err); }
  @keyframes pulse { 0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 60%, transparent); }
                     70% { box-shadow: 0 0 0 .5rem transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
  @media (prefers-reduced-motion: reduce) { .dot.live { animation: none; } }
  .facts { display: flex; flex-wrap: wrap; gap: .25rem 1.25rem; color: var(--muted); font-size: .9rem; }
  .facts b { color: var(--text); font-weight: 600; }
  .bar { position: relative; height: .6rem; border-radius: 99px; background: var(--track); overflow: hidden; }
  .bar > i { position: absolute; inset: 0 auto 0 0; background: var(--accent); border-radius: 99px; }
  .bar-row { display: grid; gap: .3rem; }
  .bar-cap { display: flex; justify-content: space-between; gap: 1rem; font-size: .85rem; color: var(--muted); flex-wrap: wrap; }
  .bar-cap b { color: var(--text); font-weight: 600; }
  .bars { display: grid; gap: .8rem; grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr)); }

  /* Alerts */
  #alerts { display: grid; gap: .5rem; }
  #alerts:empty { display: none; }
  .alert { border-radius: 8px; padding: .6rem .9rem; border: 1px solid; font-size: .92rem; display: flex; gap: .6rem; }
  .alert.error { border-color: var(--err); background: color-mix(in srgb, var(--err) 12%, var(--panel)); }
  .alert.warn { border-color: var(--warn); background: color-mix(in srgb, var(--warn) 12%, var(--panel)); }
  .alert.info { border-color: var(--border); background: var(--panel); color: var(--muted); }
  .alert .ico { flex: none; font-weight: 700; }
  .alert.error .ico { color: var(--err); } .alert.warn .ico { color: var(--warn); } .alert.info .ico { color: var(--info); }

  /* Slots */
  #slots { display: grid; gap: .9rem; grid-template-columns: repeat(auto-fit, minmax(min(330px, 100%), 1fr)); }
  .slot { background: var(--panel); border: 1px solid var(--border); border-left: 4px solid var(--accent);
          border-radius: 10px; padding: .85rem 1rem; display: grid; gap: .45rem; align-content: start; min-width: 0; }
  .slot.preparing, .slot.cloning { border-left-color: var(--info); }
  .slot.finished, .slot.stopped { border-left-color: var(--border); background: var(--panel-2); }
  .slot.finished.failed { border-left-color: var(--err); }
  .slot.stalled { border-left-color: var(--warn); }
  .slot.limited { border-left-color: var(--err); }
  .slot-head { display: flex; justify-content: space-between; align-items: baseline; gap: .75rem; }
  .slot-head a { font-weight: 650; text-decoration: none; min-width: 0; }
  .slot-head a:hover { text-decoration: underline; }
  .clock { font-family: var(--mono); font-size: 1.05rem; font-variant-numeric: tabular-nums; flex: none; }
  .pill { display: inline-block; font-size: .72rem; font-weight: 650; text-transform: uppercase; letter-spacing: .05em;
          padding: .05rem .45rem; border-radius: 99px; border: 1px solid currentColor; }
  .pill.running { color: var(--accent); } .pill.preparing, .pill.cloning { color: var(--info); }
  .pill.finished, .pill.stopped { color: var(--muted); } .pill.failed, .pill.limited { color: var(--err); }
  .pill.stalled { color: var(--warn); }
  .meta { display: flex; flex-wrap: wrap; gap: .2rem .9rem; font-size: .82rem; color: var(--muted); }
  .action { font-family: var(--mono); font-size: .84rem; background: var(--panel-2); border-radius: 6px;
            padding: .4rem .55rem; display: grid; gap: .1rem; }
  .slot.finished .action, .slot.stopped .action { background: var(--panel); }
  .action .tool { color: var(--accent); font-weight: 650; }
  .action .ago { color: var(--muted); font-size: .78rem; }
  .said { color: var(--muted); font-size: .86rem; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .empty { color: var(--muted); font-size: .92rem; }

  /* Pace + limit */
  .two { display: grid; gap: 1.25rem; grid-template-columns: repeat(auto-fit, minmax(min(360px, 100%), 1fr)); }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: .75rem; }
  .tile .v { font-size: 1.35rem; font-weight: 650; font-variant-numeric: tabular-nums; }
  .tile .k { font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
  .tile .v.err { color: var(--err); }
  .gauge { position: relative; height: 1.1rem; border-radius: 6px; background: var(--track); overflow: hidden; margin: .6rem 0 .3rem; }
  .gauge .band { position: absolute; top: 0; bottom: 0; background: var(--band); border-left: 1px dashed var(--warn); border-right: 1px dashed var(--warn); }
  .gauge .fill { position: absolute; left: 0; top: .3rem; bottom: .3rem; border-radius: 4px; background: var(--accent); }
  .gauge.near .fill { background: var(--warn); } .gauge.hit .fill { background: var(--err); }
  .gauge .med { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--warn); }
  .gauge-cap { display: flex; justify-content: space-between; font-size: .78rem; color: var(--muted); }
  .note { font-size: .82rem; color: var(--muted); margin: .6rem 0 0; }
  .hits { margin: .5rem 0 0; padding: 0; list-style: none; font-size: .82rem; color: var(--muted); display: grid; gap: .15rem; }

  /* Feed */
  #feed { display: grid; gap: 0; }
  .row { display: grid; grid-template-columns: 4.2rem 1.1rem minmax(0, 19rem) 4.2rem 3.6rem minmax(0, 1fr);
         gap: .75rem; align-items: baseline; padding: .45rem 0; border-top: 1px solid var(--border); font-size: .88rem; }
  .row:first-child { border-top: 0; }
  .row .ok { color: var(--accent); } .row .bad { color: var(--err); }
  .row summary { cursor: pointer; color: var(--muted); list-style: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row summary::-webkit-details-marker { display: none; }
  .row details[open] summary { white-space: normal; color: var(--text); }
  .row .repo { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  @media (max-width: 720px) {
    main { padding: 1rem; }
    .row { grid-template-columns: 3.6rem 1rem minmax(0, 1fr) auto; }
    .row .dur { display: none; }
    .row details { grid-column: 1 / -1; }
  }
</style>
</head>
<body>
<header>
  <h1>🌱 gardener · live</h1>
  <span class="sub" id="beat">connecting…</span>
  <nav><a href="/">garden &amp; history →</a></nav>
</header>
<p id="announce" style="position:absolute;left:-9999px" role="status" aria-live="polite"></p>
<main>
  <section id="hero" class="panel"><div class="empty">Loading…</div></section>
  <div id="alerts"></div>
  <section>
    <h2 id="slots-title">Slots</h2>
    <div id="slots"></div>
  </section>
  <div class="two">
    <section class="panel"><h2>Pace · this run</h2><div id="pace"></div></section>
    <section class="panel"><h2>Usage limit</h2><div id="limit"></div></section>
  </div>
  <section class="panel">
    <h2>Finished this run <span id="feed-count" class="muted" style="text-transform:none;letter-spacing:0;font-weight:400"></span></h2>
    <div id="feed"></div>
  </section>
</main>
<script>
const SCHEMA = %%SCHEMA%%;
const STALL_SECONDS = %%STALL%%;
const POLL_MS = 3000;
let data = null;
let skewMs = 0;           // server clock minus browser clock
let lastOk = 0;
let staleReason = null;
let lastAnnounced = "";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const now = () => Date.now() + skewMs;
const ts = (iso) => (iso ? Date.parse(iso) : NaN);

function dur(sec, withSeconds) {
  if (sec == null || !isFinite(sec)) return "—";
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (withSeconds) return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s).padStart(2, "0");
  if (h) return h + "h " + String(m).padStart(2, "0") + "m";
  if (m) return m + "m";
  return s + "s";
}
function ago(iso) {
  const t = ts(iso);
  if (!isFinite(t)) return "";
  const s = Math.max(0, (now() - t) / 1000);
  return s < 5 ? "just now" : dur(s) + " ago";
}
function clock(iso) {
  const t = ts(iso);
  return isFinite(t) ? new Date(t).toLocaleTimeString([], {hour: "numeric", minute: "2-digit"}) : "—";
}
function money(v, digits) { return v == null ? "—" : "$" + Number(v).toFixed(digits ?? 2); }
function repoLink(repo) { return `<a href="https://github.com/${esc(repo)}" target="_blank" rel="noopener">${esc(repo)}</a>`; }
// "#123" in a run summary -> that repo's issue/PR (GitHub redirects /issues/N to a PR).
function linkRefs(text, repo) {
  return esc(text).replace(/(^|[\s(])#(\d+)\b/g, (m, pre, n) =>
    `${pre}<a href="https://github.com/${esc(repo)}/issues/${n}" target="_blank" rel="noopener">#${n}</a>`);
}

function renderHero() {
  const r = data.run, c = data.cycle;
  if (!r) { $("hero").innerHTML = `<div class="state"><span class="dot"></span>No overnight run on record</div>`; return; }
  let dot = "dot", label;
  if (r.alive) { dot += data.limit.level === "hit" ? " err" : " live"; label = "Running"; }
  else if (r.end && r.end.kind === "aborted") { dot += " err"; label = "Aborted"; }
  else if (r.end) { label = "Finished"; }
  else { dot += " warn"; label = "Stopped"; }
  const facts = [];
  if (r.session_id) facts.push(`session <b class="mono">${esc(r.session_id)}</b>`);
  if (r.concurrency) facts.push(`<b>${r.concurrency}</b> at a time`);
  if (r.strategy) facts.push(`${esc(r.strategy)} order`);
  facts.push(`started <b>${clock(r.started_at)}</b>`);
  if (!r.alive && r.last_write_at) facts.push(`last activity <b>${clock(r.last_write_at)}</b>`);
  let endLine = "";
  if (!r.alive && r.end) {
    endLine = `<div class="muted">${r.end.kind === "aborted" ? "Aborted — " + esc(r.end.reason) + ". " : ""}${esc(r.end.summary || "")}</div>`;
  }

  let bars = "";
  if (r.budget_hours) {
    const budget = r.budget_hours * 3600;
    const el = r.alive ? (now() - ts(r.started_at)) / 1000 : r.elapsed_seconds;
    const pct = Math.min(100, (el / budget) * 100);
    // Tonight's list used up: the run ends with this batch, not at the
    // budget's end, and saying "ends 4:05 AM" would be hours wrong.
    const lastBatch = r.alive && r.pace.candidates_left === 0;
    const right = !r.alive
      ? `<span>budget ${r.budget_hours}h</span>`
      : lastBatch
        ? `<span><b>last batch</b> · ends when it finishes</span>`
        : `<span><b data-until="${esc(r.ends_at)}">${dur(budget - el)}</b> left · ends ${clock(r.ends_at)}</span>`;
    bars += `<div class="bar-row"><div class="bar-cap"><span>Time · <b ${r.alive ? `data-since="${esc(r.started_at)}"` : ""}>${dur(r.alive ? el : Math.min(el, budget))}</b> in</span>${right}</div>
      <div class="bar"><i style="width:${pct.toFixed(1)}%"></i></div></div>`;
  }
  if (r.batch) {
    const done = r.batch.start - 1;
    bars += `<div class="bar-row"><div class="bar-cap"><span>Tonight's list · batch <b>${r.batch.start}–${r.batch.end}</b> of <b>${r.batch.total}</b></span>
      <span>${r.pace.candidates_left} not started</span></div>
      <div class="bar"><i style="width:${((done / r.batch.total) * 100).toFixed(1)}%"></i></div></div>`;
  }
  if (c && c.garden_size) {
    bars += `<div class="bar-row"><div class="bar-cap"><span>Garden cycle · <b>${c.attempted}</b> of <b>${c.garden_size}</b> repos</span>
      <span>${Math.max(0, c.garden_size - c.attempted)} to go</span></div>
      <div class="bar"><i style="width:${Math.min(100, (c.attempted / c.garden_size) * 100).toFixed(1)}%"></i></div></div>`;
  }
  $("hero").innerHTML = `<div class="status-line"><span class="state"><span class="${dot}"></span>${label}</span>
      <span class="facts">${facts.join("<span aria-hidden=true>·</span>")}</span></div>${endLine}<div class="bars">${bars}</div>`;
}

function renderAlerts() {
  const icons = {error: "!", warn: "▲", info: "i"};
  $("alerts").innerHTML = data.alerts.map((a) =>
    `<div class="alert ${a.level}"><span class="ico" aria-hidden="true">${icons[a.level] || "·"}</span><span>${esc(a.text)}</span></div>`).join("");
  const loud = data.alerts.filter((a) => a.level !== "info").map((a) => a.text).join(" ");
  if (loud !== lastAnnounced) { $("announce").textContent = loud; lastAnnounced = loud; }
}

function renderSlots() {
  const slots = data.slots, r = data.run;
  const running = slots.filter((s) => ["running", "preparing", "cloning"].includes(s.phase)).length;
  $("slots-title").textContent = slots.length
    ? `Slots · ${running} working${slots.length - running ? ", " + (slots.length - running) + " done" : ""}`
    : "Slots";
  if (!slots.length) {
    $("slots").innerHTML = `<div class="empty panel">${r && r.alive ? "Waiting for the first batch…" : "Nothing in flight."}</div>`;
    return;
  }
  $("slots").innerHTML = slots.map((s) => {
    const a = s.activity || {};
    const res = s.result;
    const limited = !!a.rate_limit;
    const failed = res && !res.ok;
    const cls = ["slot", s.phase, s.stalled ? "stalled" : "", limited ? "limited" : "", failed ? "failed" : ""].join(" ");
    let pill = s.phase;
    let pillCls = s.phase;
    if (limited) { pill = "usage limit"; pillCls = "limited"; }
    else if (s.stalled) { pill = "quiet " + dur(s.idle_seconds); pillCls = "stalled"; }
    else if (failed) { pill = "failed"; pillCls = "failed"; }
    else if (s.phase === "finished") { pill = r && r.alive ? "done · waiting" : "done"; }

    let clockHtml = "";
    if (s.phase === "running" && s.started_at) clockHtml = `<span class="clock" data-since="${esc(s.started_at)}" data-secs>${dur((now() - ts(s.started_at)) / 1000, true)}</span>`;
    else if (res && res.duration_ms) clockHtml = `<span class="clock muted">${dur(res.duration_ms / 1000, true)}</span>`;

    const meta = [];
    if (a.tool_calls != null) meta.push(`${a.tool_calls} tool calls`);
    if (a.output_tokens) meta.push(`${(a.output_tokens / 1000).toFixed(1)}k tokens out`);
    if (res && res.cost_usd != null) meta.push(money(res.cost_usd));
    if (a.model) meta.push(esc(a.model.replace(/^claude-/, "")));

    let body = "";
    if (limited) {
      body = `<div class="action"><span style="color:var(--err)">${esc(a.rate_limit.message)}</span></div>`;
    } else if (res) {
      body = `<div class="said">${linkRefs(res.summary, s.repo)}</div>`;
    } else if (a.last_tool) {
      body = `<div class="action"><span><span class="tool">${esc(a.last_tool.name)}</span> ${esc(a.last_tool.detail)}</span>
        <span class="ago" data-ago="${esc(a.last_tool.at)}">${ago(a.last_tool.at)}</span></div>`;
      if (a.last_text && ts(a.last_text.at) >= ts(a.last_tool.at) - 60000) body += `<div class="said">“${esc(a.last_text.text)}”</div>`;
    } else if (s.phase === "cloning") {
      body = `<div class="empty">Cloning / refreshing the repo…</div>`;
    } else if (s.phase === "preparing") {
      body = `<div class="empty">Checking its dev-loop skill and open PRs…</div>`;
    } else if (s.phase === "stopped") {
      body = `<div class="empty">Stopped before it finished — no result recorded.</div>`;
    }
    return `<article class="${cls}">
      <div class="slot-head">${repoLink(s.repo)}${clockHtml}</div>
      <div class="meta"><span class="pill ${pillCls}">${esc(pill)}</span>${meta.map((m) => `<span>${m}</span>`).join("")}</div>
      ${body}</article>`;
  }).join("");
}

function renderPace() {
  const r = data.run;
  if (!r) { $("pace").innerHTML = `<div class="empty">—</div>`; return; }
  const p = r.pace;
  const tiles = [
    [p.finished, "finished"],
    [p.errors, "errors", p.errors ? "err" : ""],
    [money(p.cost_usd, 0), "spent"],
    [p.avg_duration_seconds != null ? dur(p.avg_duration_seconds) : "—", "avg repo"],
    [p.per_hour != null ? p.per_hour : "—", "repos / hour"],
    [p.cost_per_hour != null ? money(p.cost_per_hour, 0) : "—", "per hour"],
  ];
  let proj = "";
  if (r.alive && p.projected_more != null) {
    proj = `<p class="note">At this pace about <b>${p.projected_more}</b> more repo${p.projected_more === 1 ? "" : "s"} before the budget runs out`
      + (p.candidates_left != null ? ` (${p.candidates_left} left on tonight's list).` : ".") + `</p>`;
  }
  $("pace").innerHTML = `<div class="tiles">${tiles.map(([v, k, c]) =>
    `<div class="tile"><div class="v ${c || ""}">${esc(v)}</div><div class="k">${k}</div></div>`).join("")}</div>${proj}`;
}

function renderLimit() {
  const L = data.limit;
  const cur = L.current_cost_usd, band = L.band;
  const scale = Math.max(cur, band ? band.max : 0, 1) * 1.12;
  const pct = (v) => ((v / scale) * 100).toFixed(1) + "%";
  let gauge = `<div class="gauge ${L.level}">`;
  if (band) gauge += `<div class="band" style="left:${pct(band.min)};width:calc(${pct(band.max)} - ${pct(band.min)})"></div>
                      <div class="med" style="left:${pct(band.median)}"></div>`;
  gauge += `<div class="fill" style="width:${pct(cur)}"></div></div>
    <div class="gauge-cap"><span>$0</span>${band ? `<span>past hits ${money(band.min, 0)}–${money(band.max, 0)}</span>` : ""}</div>`;
  let head;
  if (L.level === "hit") head = `<div class="state" style="font-size:1.1rem"><span class="dot err"></span>Limit hit</div><p>${esc(L.live_hit.message)}</p>`;
  else head = `<div><span class="tile"><span class="v">${money(cur, 0)}</span></span> <span class="muted">spent in the last ${L.window_hours} h</span></div>`;
  let hits = "";
  if (L.hits.length) {
    hits = `<ul class="hits">${L.hits.slice(0, 4).map((h) => `<li>${new Date(ts(h.at)).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"})} — hit at ${money(h.trailing_cost_usd, 0)}; ${h.repos} repo${h.repos === 1 ? "" : "s"} failed</li>`).join("")}</ul>`;
  } else {
    hits = `<p class="note">No usage-limit hits in the last 30 days.</p>`;
  }
  $("limit").innerHTML = head + gauge + hits +
    `<p class="note">Gardener's own completed-run spend (API-equivalent) over the ${L.window_hours}-hour limit window, against the same figure just before each recent hit. Interactive Claude use shares the limit and isn't counted, so the limit can hit early.</p>`;
}

function renderFeed() {
  const f = data.finished;
  $("feed-count").textContent = f.length ? `· ${f.length}${f.length >= 60 ? " most recent" : ""}` : "";
  if (!f.length) { $("feed").innerHTML = `<div class="empty">Nothing finished yet.</div>`; return; }
  $("feed").innerHTML = f.map((x) => `<div class="row">
      <span class="muted num">${clock(x.timestamp)}</span>
      <span class="${x.ok ? "ok" : "bad"}" title="${esc(x.outcome)}">${x.ok ? "✓" : "✕"}</span>
      <span class="repo">${repoLink(x.repo)}</span>
      <span class="muted num dur">${x.duration_ms ? dur(x.duration_ms / 1000) : "—"}</span>
      <span class="muted num">${money(x.cost_usd)}</span>
      <details><summary>${linkRefs(x.summary, x.repo)}</summary></details>
    </div>`).join("");
}

// Clocks tick every second between polls, from the last payload.
function tick() {
  document.querySelectorAll("[data-since]").forEach((el) => {
    const s = (now() - ts(el.dataset.since)) / 1000;
    el.textContent = dur(s, el.hasAttribute("data-secs"));
  });
  document.querySelectorAll("[data-until]").forEach((el) => { el.textContent = dur((ts(el.dataset.until) - now()) / 1000); });
  document.querySelectorAll("[data-ago]").forEach((el) => { el.textContent = ago(el.dataset.ago); });
  if (staleReason) {
    $("beat").textContent = `⚠ ${staleReason} — showing data from ${lastOk ? dur((Date.now() - lastOk) / 1000) + " ago" : "never"}`;
  } else if (lastOk) {
    $("beat").textContent = `live · updated ${dur((Date.now() - lastOk) / 1000)} ago`;
  }
}

function markStale(reason) { staleReason = reason; document.body.classList.add("stale"); tick(); }

async function poll() {
  try {
    let res;
    try { res = await fetch("/api/live", {cache: "no-store"}); }
    catch (e) { markStale("dashboard server unreachable"); return; }
    if (!res.ok) { markStale("server error " + res.status); return; }
    let d;
    try { d = await res.json(); } catch (e) { markStale("unreadable response"); return; }
    if (d.schema !== SCHEMA) { markStale("the server was updated — reload this page"); return; }
    data = d;
    skewMs = Date.parse(d.generated_at) - Date.now();
    if (!isFinite(skewMs) || Math.abs(skewMs) < 2000) skewMs = 0;
    try {
      renderHero(); renderAlerts(); renderSlots(); renderPace(); renderLimit(); renderFeed();
    } catch (e) { console.error(e); markStale("render failed"); return; }
    lastOk = Date.now(); staleReason = null; document.body.classList.remove("stale");
    document.title = (d.run && d.run.alive ? (d.limit.level === "hit" ? "⛔ " : "● ") : "") + "gardener · live";
    tick();
  } finally {
    setTimeout(poll, POLL_MS);
  }
}
setInterval(tick, 1000);
poll();
</script>
</body>
</html>
""".replace("%%SCHEMA%%", str(LIVE_SCHEMA)).replace("%%STALL%%", str(STALL_SECONDS))
