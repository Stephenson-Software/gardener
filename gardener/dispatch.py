"""Subprocess wrapper around the `claude` CLI — gardener's only integration
point with Claude Code. Safety constraints live here, built into how each
mode's invocation is constructed, not applied afterward as a check on the
result.

## Sync vs. background dispatch

pocket-rig's dashboard (`~/pocket-rig/dashboard/server.py`) dispatches with
`claude --bg` because it's answering an HTTP request that can't hang open —
the browser gets a pid back immediately and polls `claude agents`/session
transcripts for progress.

gardener is invoked from a terminal or cron and *can* block until Claude
finishes. So it uses `claude -p/--print` (headless "run once, print, exit"
mode) instead: one subprocess call, full stdout captured synchronously,
nothing to poll, nothing left running after the CLI command returns. This
is also what makes `gardener align`'s output pipeable and its exit code
meaningful in a script/cron context, which `--bg` (fire-and-forget,
returns instantly) can't give you.

`--output-format json` is used rather than the default text stream: it
returns one structured object with the final answer (`result`), cost,
duration, session id, and — critically for the safety story below —
`permission_denials`, so gardener can log not just what Claude said but
whether it ever *tried* something out of scope and was blocked.

## The permission model (this is the part that actually matters)

Every mode is built from three independent layers, confirmed by hand
against a real `claude -p` invocation before being relied on here (see
gardener's own commit history / README for the transcript):

1. **`--tools`** is a hard allow-list of which *tool types* exist in the
   session at all. It does not accept scoped patterns like `Bash(git *)`
   — a name that isn't an exact built-in tool name is silently dropped.
   Confirmed directly: a report-mode session given `--tools Read,Grep,Glob`
   has no Write/Edit/Bash tool object to call, at all — not "denied", not
   available. This is gardener's real ceiling for report mode: Claude
   physically cannot mutate anything because there is no tool that would
   let it.

2. **`--permission-mode default`** is what implement/file-issue mode use
   for the same reason plain `claude` does interactively: every tool call
   needs approval. There is no human at the other end of a headless `-p`
   run to grant that approval — confirmed directly: a `Bash` call outside
   an `--allowedTools` pattern comes back in `permission_denials` and does
   NOT execute, no hang, no silent pass-through. `acceptEdits` was tried
   and rejected for this: it auto-approved a Bash call that was
   deliberately left out of `--allowedTools`, which defeats the scoping
   below entirely.

3. **`--allowedTools`** pre-approves specific, narrow patterns (e.g.
   `Bash(git *)`, `Bash(gh pr *)`) so *only* those specific commands run
   without a human — everything else still needs one, and since there
   isn't one, everything else is auto-denied. This is what lets implement
   mode use git/gh at all without opening the door to arbitrary shell
   commands.

`bypassPermissions` (or any equivalent "skip all checks" mode) is never
constructed here, for any mode, under any flag combination — enforced by
`_build_invocation` raising if it's ever reached, not just by omission.
This mirrors pocket-rig's dashboard, which hard-rejects it server-side
rather than merely defaulting away from it.

`--strict-mcp-config` (with no `--mcp-config` given) is passed for every
mode so a dispatched run never inherits whatever MCP servers happen to be
configured for the invoking user (Gmail/Drive/Calendar connectors, etc.) —
gardener's job is reading/writing one git repo, not anything reachable via
the operator's personal integrations.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

CLAUDE_BIN = "claude"

# Every permission mode `claude --help` currently lists, MINUS
# bypassPermissions. This is the same floor pocket-rig's dashboard enforces
# (see server.py's CLAUDE_PERMISSION_MODES) — kept as an explicit allow-list
# here too, rather than "just don't pass bypassPermissions", so a future
# edit that threads a mode through from a new flag can't accidentally widen
# this without also editing the set that makes it valid.
ALLOWED_PERMISSION_MODES = {"plan", "default", "acceptEdits", "auto", "dontAsk", "manual"}
FORBIDDEN_PERMISSION_MODE = "bypassPermissions"

DEFAULT_TIMEOUT_SECONDS = 1800  # 30 min — reading + analyzing a real repo can be slow.


class Mode(str, Enum):
    REPORT = "report"
    IMPLEMENT = "implement"
    FILE_ISSUE = "file-issue"


@dataclass(frozen=True)
class ModeSpec:
    tools: tuple[str, ...]
    permission_mode: str
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)


# The actual safety gate. Report mode's tool list contains no tool capable
# of writing a file or running a shell command — confirmed live, see the
# module docstring — so no permission-mode or allowedTools scoping can
# matter for it; it's included anyway as defense in depth.
MODE_SPECS: dict[Mode, ModeSpec] = {
    Mode.REPORT: ModeSpec(
        tools=("Read", "Grep", "Glob"),
        permission_mode="plan",
    ),
    Mode.IMPLEMENT: ModeSpec(
        tools=("Read", "Grep", "Glob", "Edit", "Write", "Bash"),
        permission_mode="default",
        allowed_tools=("Edit", "Write", "Bash(git *)", "Bash(gh pr *)"),
    ),
    Mode.FILE_ISSUE: ModeSpec(
        # Deliberately no Edit/Write at all — file-issue mode has no
        # legitimate reason to touch a file in the target repo.
        tools=("Read", "Grep", "Glob", "Bash"),
        permission_mode="default",
        allowed_tools=("Bash(gh issue *)",),
    ),
}


class DispatchError(RuntimeError):
    pass


@dataclass
class DispatchResult:
    ok: bool
    result_text: str
    raw_stdout: str
    stderr: str
    exit_code: Optional[int]
    duration_ms: int
    cost_usd: Optional[float]
    session_id: Optional[str]
    permission_denials: list
    is_error: bool
    timed_out: bool = False


def _build_invocation(
    mode: Mode,
    prompt: str,
    cwd: Path,
    add_dirs: list[Path],
    model: Optional[str] = None,
) -> list[str]:
    spec = MODE_SPECS[mode]
    if spec.permission_mode == FORBIDDEN_PERMISSION_MODE:
        # Unreachable given MODE_SPECS above, but kept as a hard runtime
        # gate rather than trusting the table never regresses.
        raise DispatchError("refusing to dispatch with bypassPermissions")
    if spec.permission_mode not in ALLOWED_PERMISSION_MODES:
        raise DispatchError(f"unrecognized permission mode: {spec.permission_mode}")

    # The prompt goes immediately after -p, not at the end: --add-dir takes
    # a variadic list (`<directories...>`) and will otherwise swallow a
    # trailing positional prompt as one more directory to add, leaving
    # claude with no prompt at all — confirmed directly against a real
    # invocation (it failed with "Input must be provided either through
    # stdin or as a prompt argument") before this ordering was fixed.
    argv = [
        CLAUDE_BIN,
        "-p", prompt,
        "--output-format", "json",
        "--permission-mode", spec.permission_mode,
        "--tools", ",".join(spec.tools),
        "--strict-mcp-config",
    ]
    if spec.allowed_tools:
        argv += ["--allowedTools", " ".join(spec.allowed_tools)]
    for d in add_dirs:
        argv += ["--add-dir", str(d)]
    if model:
        argv += ["--model", model]
    return argv


def run_claude(
    mode: Mode,
    prompt: str,
    cwd: Path,
    add_dirs: Optional[list[Path]] = None,
    model: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> DispatchResult:
    """Dispatch one headless `claude -p` run and block until it finishes.

    Raises DispatchError for setup problems (bad mode, `claude` binary
    missing). A Claude-side failure (non-zero exit, is_error in the JSON
    result, or a timeout) is reported back in DispatchResult rather than
    raised, since "the run happened but found a problem" is a normal
    outcome gardener needs to log, not an exceptional one.
    """
    if shutil.which(CLAUDE_BIN) is None:
        raise DispatchError(
            f"`{CLAUDE_BIN}` not found on PATH — install Claude Code CLI first"
        )
    if mode not in MODE_SPECS:
        raise DispatchError(f"unknown mode: {mode}")

    argv = _build_invocation(mode, prompt, cwd, add_dirs or [], model=model)

    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        return DispatchResult(
            ok=False,
            result_text="",
            raw_stdout=(e.stdout or ""),
            stderr=(e.stderr or "") + f"\ntimed out after {timeout}s",
            exit_code=None,
            duration_ms=duration_ms,
            cost_usd=None,
            session_id=None,
            permission_denials=[],
            is_error=True,
            timed_out=True,
        )
    duration_ms = int((time.monotonic() - start) * 1000)

    stdout = proc.stdout or ""
    parsed = None
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        parsed = None

    if parsed is None:
        return DispatchResult(
            ok=False,
            result_text="",
            raw_stdout=stdout,
            stderr=proc.stderr or "",
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            cost_usd=None,
            session_id=None,
            permission_denials=[],
            is_error=True,
        )

    is_error = bool(parsed.get("is_error"))
    return DispatchResult(
        ok=(proc.returncode == 0 and not is_error),
        result_text=parsed.get("result", ""),
        raw_stdout=stdout,
        stderr=proc.stderr or "",
        exit_code=proc.returncode,
        duration_ms=duration_ms,
        cost_usd=parsed.get("total_cost_usd"),
        session_id=parsed.get("session_id"),
        permission_denials=parsed.get("permission_denials", []),
        is_error=is_error,
    )
