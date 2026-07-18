"""Live visibility into a dispatched `claude -p` session's own JSONL
transcript file, plus a small pretty-printer for it.

## Why this exists

`dispatch.run_claude` blocks on `subprocess.run(capture_output=True)` and
only returns once the whole session is done — see `dispatch.py`'s module
docstring for why that's the right call for `gardener align`/`tend`'s
synchronous, scriptable "one call in, one result out" design. That's still
correct, but it means a `tend` dispatch can run for up to
`TEND_DEFAULT_TIMEOUT_SECONDS` (45 min) with zero visibility into what's
happening while it runs — a real gap for someone watching `devsrv logs
gardener-overnight -f` or the dashboard's log viewer live overnight.

Claude Code already solves the actual data-availability half of this
problem on its own, for every `-p` session, no special flag required: it
writes a JSONL transcript, growing in real time, to
`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`. This module doesn't
change how `run_claude` dispatches anything — it only (a) figures out
where that file is going to land, (b) polls briefly for it to show up, and
(c) prints its path so a human (or `tail -f`) can go find it while the real
dispatch is still running. `dispatch.run_claude` runs this in a background
daemon thread started right before its blocking `subprocess.run` call, so
the poll happens concurrently with the dispatch, never before or after it,
and — bounded to a few seconds by `POLL_TIMEOUT_SECONDS` — can never
meaningfully delay or fail the dispatch it's layered on top of.

## The encoding rule (confirmed empirically, not assumed)

`<encoded-cwd>` is the session's working directory with every character
that is NOT `[A-Za-z0-9]` replaced 1:1 with `-` — no collapsing of runs of
special characters, including the path's leading `/`. Confirmed directly
(2026-07-18), two ways:

1. A real, throwaway `claude -p` session was launched with `cwd`
   deliberately containing a dot, an underscore, a hyphen, and nested
   slashes:
   `/tmp/claude-2000/-home-userland/2c0a53d4-2197-4dd7-88c9-2e2382e7c79b/scratchpad/enc_probe.fresh/sub-dir_name.example`
   This produced the transcript directory
   `~/.claude/projects/-tmp-claude-2000--home-userland-2c0a53d4-2197-4dd7-88c9-2e2382e7c79b-scratchpad-enc-probe-fresh-sub-dir-name-example/`
   — every `.`, `_`, and `/` became `-`; every character that was already
   `-` (in `claude-2000`, the UUID, `sub-dir`) stayed `-`; and two
   *consecutive* special characters (the `/` right before, and the literal
   leading `-` of, the `-home-userland` path segment) produced the
   observed `--`, confirming runs are not collapsed to one `-`.
2. Cross-checked against a real, already-existing gardener dispatch's own
   transcript directory, from a live `tend` run against
   `Stephenson-Software/gateway` earlier the same night: cwd
   `/home/userland/.cache/gardener/repos/Stephenson-Software__gateway`
   (gardener's own `repo.replace("/", "__")` cache-dir naming, see
   `cli.py`'s `clone_or_refresh_target_repo`) produced
   `-home-userland--cache-gardener-repos-Stephenson-Software--gateway` —
   the `__` gardener itself introduced became `--`, and the single `-`
   already inside `Stephenson-Software` stayed a single `-`. Same rule,
   independently confirmed against data gardener didn't generate for this
   purpose.

`encode_cwd` below is exactly `re.sub(r"[^A-Za-z0-9]", "-", str(cwd))` —
nothing cleverer than that was needed to explain either example.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Optional, TextIO, Union

PathLike = Union[str, Path]

CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

# Bounded on purpose — see module docstring. If the transcript file hasn't
# shown up within this window, the watcher thread gives up silently and the
# real dispatch it's layered on top of is completely unaffected either way.
POLL_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.2


def claude_projects_dir() -> Path:
    """`~/.claude/projects` by default; honors `CLAUDE_CONFIG_DIR` the same
    way the `claude` CLI itself does, so this stays correct on a machine
    where that's been overridden rather than silently looking in the wrong
    place."""
    override = os.environ.get(CLAUDE_CONFIG_DIR_ENV)
    base = Path(override) if override else Path.home() / ".claude"
    return base / "projects"


def encode_cwd(cwd: PathLike) -> str:
    """See module docstring's "The encoding rule" section for the two real
    invocations that confirmed this transform."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def project_transcript_dir(cwd: PathLike) -> Path:
    return claude_projects_dir() / encode_cwd(cwd)


def find_new_transcript(project_dir: Path, after: float) -> Optional[Path]:
    """One non-blocking look: the most recently modified `*.jsonl` file in
    `project_dir` whose mtime is >= `after` (a wall-clock `time.time()`
    value taken just before the session was launched) — so a *pre-existing*
    transcript from an earlier dispatch into the same cwd (e.g. `tend`'s own
    `create-dev-loop` dispatch immediately followed by its main tend
    dispatch, both into the same repo checkout) is never mistaken for this
    one. Returns None, never raises, if the directory doesn't exist yet or
    nothing qualifies."""
    if not project_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for p in project_dir.glob("*.jsonl"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime >= after:
            candidates.append((mtime, p))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def poll_for_new_transcript(
    cwd: PathLike,
    after: float,
    timeout: float = POLL_TIMEOUT_SECONDS,
    interval: float = POLL_INTERVAL_SECONDS,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Optional[Path]:
    """Poll `project_transcript_dir(cwd)` for up to `timeout` seconds
    (budget measured via `time_fn`, default `time.monotonic`) for a new
    transcript file. Returns the file's Path as soon as one shows up, or
    None once the window closes. `time_fn`/`sleep_fn` are injectable so
    tests can exercise the loop without ever sleeping for a real second."""
    project_dir = project_transcript_dir(cwd)
    deadline = time_fn() + timeout
    while True:
        found = find_new_transcript(project_dir, after)
        if found is not None:
            return found
        if time_fn() >= deadline:
            return None
        sleep_fn(interval)


def log_transcript_when_found(
    cwd: PathLike,
    after: float,
    stream: Optional[TextIO] = None,
    timeout: float = POLL_TIMEOUT_SECONDS,
    interval: float = POLL_INTERVAL_SECONDS,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    """Meant to run in a background thread (see `start_transcript_watcher`
    below). Polls for the new transcript and, if found in time, prints its
    path via the same `print(..., file=sys.stderr)` convention the rest of
    gardener already uses, so it shows up in `devsrv logs -f`/the
    dashboard's log viewer within seconds of dispatch start. Never raises:
    a problem here must never be able to affect, or even be visible to, the
    real dispatch this is a visibility nicety layered on top of."""
    out = stream if stream is not None else sys.stderr
    try:
        path = poll_for_new_transcript(
            cwd, after, timeout=timeout, interval=interval, time_fn=time_fn, sleep_fn=sleep_fn
        )
        if path is not None:
            print(
                f"gardener: session transcript: {path} (tail -f it for live detail, "
                f"or `gardener tail-transcript -f {path}`)",
                file=out,
            )
    except Exception:  # noqa: BLE001 - a visibility nicety must never break dispatch
        pass


def start_transcript_watcher(cwd: PathLike, after: Optional[float] = None) -> threading.Thread:
    """Starts and returns the daemon thread described above. `after`
    defaults to `time.time()` (wall-clock, comparable to filesystem mtimes)
    taken at call time — callers should call this BEFORE the subprocess
    actually starts, not after, so a transcript created in the small window
    between this call and the subprocess actually launching is still
    counted (never the reverse: a real transcript must not be missed
    because `after` was computed too late). A daemon thread is used
    deliberately — this must never keep the process alive on its own or
    need an explicit join; `dispatch.run_claude`'s blocking `subprocess.run`
    call already governs how long a dispatch actually takes."""
    started_at = after if after is not None else time.time()
    thread = threading.Thread(
        target=log_transcript_when_found,
        args=(cwd, started_at),
        daemon=True,
    )
    thread.start()
    return thread


# ---- Pretty-printer ----
#
# Same shape as the ad hoc Python snippet that was hand-typed to tail the
# real `Stephenson-Software/gateway` transcript on 2026-07-18 (parsing each
# line's `message.content[].type` for `tool_use`/`text`/`tool_result`) —
# this is that snippet made permanent, tested, and reachable as `gardener
# tail-transcript`.

_TRUNCATE_DEFAULT = 200


def _truncate(text: str, limit: int = _TRUNCATE_DEFAULT) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_result_text(content) -> str:
    """`tool_result` blocks' own `content` is usually a plain string, but
    can also be a list of content blocks (e.g. images) — extract whatever
    text is present rather than dumping a raw structure that isn't a
    string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content)


def format_transcript_line(raw_line: str) -> Optional[str]:
    """Parse one raw JSONL line from a transcript file into a single
    readable summary line, or None if the line is blank, not valid JSON, or
    doesn't contain any `tool_use`/`text`/`tool_result` block worth
    surfacing (e.g. a `queue-operation` or `ai-title` bookkeeping line, or a
    non-dict/non-message line). Never raises on malformed input — a
    transcript file is Claude Code's own internal format, not something
    gardener controls the shape of, so this has to degrade gracefully
    rather than assume every line matches today's shape."""
    line = raw_line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role", "?")

    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return None

    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            name = block.get("name", "?")
            tool_input = _truncate(json.dumps(block.get("input", {}), default=str), 150)
            parts.append(f"[{role}] tool_use {name}({tool_input})")
        elif btype == "text":
            text = block.get("text", "")
            if text.strip():
                parts.append(f"[{role}] text: {_truncate(text)}")
        elif btype == "tool_result":
            flag = "ERROR" if block.get("is_error") else "ok"
            result_text = _truncate(_tool_result_text(block.get("content", "")))
            parts.append(f"[{role}] tool_result ({flag}): {result_text}")
    if not parts:
        return None
    return "\n".join(parts)


def iter_pretty_lines(
    path: Path,
    follow: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.5,
) -> Iterator[str]:
    """Yield one formatted string per meaningful transcript event.

    `follow=False` (default): read whatever is in the file right now and
    stop once EOF is reached — "dump what's there now and exit".
    `follow=True`: keep polling for newly appended lines, like `tail -f`,
    forever — meant for an interactive `gardener tail-transcript -f`
    session, not for anything automated, so it's the one part of this
    module without a bounded timeout by design (an interactive follow is
    expected to run until the user stops it). `sleep_fn` is injectable so
    tests can exercise the follow loop without a real sleep."""
    with path.open("r") as f:
        while True:
            line = f.readline()
            if line:
                formatted = format_transcript_line(line)
                if formatted is not None:
                    yield formatted
                continue
            if not follow:
                return
            sleep_fn(poll_interval)


def print_transcript(path: Path, follow: bool = False, stream: Optional[TextIO] = None) -> int:
    """The `gardener tail-transcript` command's implementation. Returns a
    process exit code rather than raising, matching every other `cmd_*`
    function in `cli.py`."""
    out = stream if stream is not None else sys.stdout
    if not path.is_file():
        print(f"gardener: no such transcript file: {path}", file=sys.stderr)
        return 1
    try:
        for formatted in iter_pretty_lines(path, follow=follow):
            print(formatted, file=out)
    except (KeyboardInterrupt, BrokenPipeError):
        # KeyboardInterrupt: Ctrl-C out of a `-f` follow session, the normal
        # way to stop one. BrokenPipeError: piped into something like
        # `head` that closed its end early — also normal, not a real error.
        return 0
    return 0
