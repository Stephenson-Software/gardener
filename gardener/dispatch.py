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

## `tend` mode and the headless "ask the user" problem

`gardener tend` dispatches a target repo's own `<slug>-dev-loop` skill (or,
if none exists yet, `create-dev-loop` first to generate one — see
`dev_loop.py`) instead of the read-mostly alignment prompt `align` uses.
Every `*-dev-loop` skill observed on this machine is written to *stop and
ask the user* via the `AskUserQuestion` tool before merging or taking a
destructive action. Dispatched headlessly with nobody there to answer, that
must not hang. The following was confirmed by hand (2026-07-18, Claude Code
CLI 2.1.214) against real `claude -p` invocations before being relied on:

1. **`AskUserQuestion` is not present at all in a headless `-p` session,
   by default, regardless of `--tools`.** A session run with `--tools
   default` (every built-in tool) was asked to list every tool name — direct
   and deferred-but-visible — available to it; `AskUserQuestion` appeared in
   neither list. `--tools Read,AskUserQuestion` was also tried explicitly;
   the resulting session reported only `Read` as available and did not
   error on the unrecognized name (consistent with the existing `--tools`
   finding above: an unrecognized name is silently dropped, not rejected).
   So the tool is structurally absent from `-p` mode before gardener does
   anything — there is no `--tools` list to author that would newly expose
   it.
2. **Without it, a session told to "stop and ask before merging" does not
   hang, and does not fabricate an answer.** A run given `--tools Read,Bash`
   / `--allowedTools Bash(echo *)` and instructed (mirroring a real
   dev-loop's Phase 8 language) to get human confirmation via
   `AskUserQuestion` before merging returned in 22s, `is_error: false`,
   explaining plainly that it had no such tool, that it would not fabricate
   a substitute, and that it would not merge. This is a favorable default,
   but `tend`'s prompt (see `dev_loop.py`'s `HEADLESS_SAFETY_PREAMBLE`) still
   states the fallback explicitly rather than relying on the model to
   improvise it every time: leave whatever's safely completed (an open PR)
   in place, and end the final answer with one line starting
   `DECISION NEEDED:` naming what's needed and by whom. Re-run with that
   preamble: same non-hang behavior, 6.9s, zero permission denials, ending
   in exactly a `DECISION NEEDED:` line.
3. **`Write` is not sandboxed to `cwd`/`--add-dir`.** A session given only
   `--tools Write --allowedTools Write` successfully wrote a file well
   outside its `cwd` and outside any `--add-dir`, with zero permission
   prompts or denials. This means `--add-dir` is not a filesystem
   boundary — it does not need to be (and isn't relied on to be) passed for
   `create-dev-loop`'s dispatch to be able to reach `~/local-skills/` and
   `~/.claude/commands/`, but it also means the *only* real boundary on
   Write/Edit in any mode that grants them is the instructional layer plus
   which directories the prompt tells Claude to touch — the same trust
   level `align --implement`'s Write/Edit already operate under.
4. **A broad `Bash(gh pr *)` allow-pattern structurally permits `gh pr
   merge`.** Tested directly: with `--allowedTools "Bash(gh pr *)"`, `gh pr
   merge 999999 --squash` actually executed (and failed only because that
   PR number doesn't exist, not because it was denied — zero
   `permission_denials`). `align --implement`'s existing `Bash(gh pr *)`
   pattern therefore already relies on its prompt's "Do not merge the PR"
   instruction alone, not a structural block — noted here because `tend`
   mode deliberately does NOT repeat this: its Bash allow-list enumerates
   specific `gh pr <verb> *` patterns (create/edit/view/list/diff/checks/
   comment/close) and never a bare `Bash(gh pr *)`, so `Bash(gh pr merge *)`
   can be added or withheld independently (see "Merge allow-list" below).
5. **`Agent` and `ScheduleWakeup` are present by default and were excluded
   the same way `AskUserQuestion` already is absent.** A dev-loop skill's
   review phase normally fans out a review subagent (`Agent`) and its
   wait-for-review phase polls via `ScheduleWakeup`. Neither has a
   synchronous, single-headless-session equivalent, and both are avenues
   for a dispatched run to sit doing nothing productive for a long time
   inside gardener's timeout window. Confirmed directly: a session given
   `--tools Read,Grep,Glob,Edit,Write,Bash,Skill` (tend mode's actual list)
   reported exactly that set back when asked to list its tools — no Agent,
   no ScheduleWakeup, no ToolSearch, no ReportFindings, no AskUserQuestion.
   `tend`'s prompt preamble additionally instructs the dispatched run to
   perform any review step inline itself and skip any "wait for review"
   loop rather than discovering the tools are missing partway through a
   cycle.
6. **The dev-loop template's own "Phase N — Next cycle: return to Phase 1"
   instruction is a real, distinct hang/cost risk from the ones above** —
   taken literally in a single agentic session it would repeat the entire
   cycle until the timeout fired, not just decline to ask a question. This
   was not exercised to actual failure (that would mean deliberately
   burning a full timeout window to observe it) — instead it's mitigated
   the same way the AskUserQuestion problem is: an explicit instruction
   (`HEADLESS_SAFETY_PREAMBLE`) to perform exactly one pass through the
   phases and stop at the "next cycle" phase instead of looping, backed by
   `TEND_DEFAULT_TIMEOUT_SECONDS` as a hard backstop either way.
7. **`Skill` needs a per-skill-name `--allowedTools` entry, the same way
   `Bash` needs a per-command pattern.** Discovered live during the first
   real end-to-end `tend` dispatch (`dmccoystephenson/snmp-command-generator`,
   2026-07-18): the run tried `Skill: code-review` (exactly what the
   preamble tells it to prefer for its inline review step) and was denied
   — a bare `"Skill"` in `--tools` was not enough on its own.
   `TEND_BASE_ALLOWED_TOOLS` includes `"Skill(code-review)"` specifically
   (confirmed permitted in isolation afterward); no other skill name is
   pre-approved, so any other `Skill` invocation still requires the same
   denied-not-hung behavior as everything else out of scope.

## Merge allow-list (`tend --allow-merge`)

`Bash(gh pr merge *)` is added to `tend` mode's `--allowedTools` if and only
if BOTH: (a) `--allow-merge` was passed on the `gardener tend` CLI
invocation, AND (b) the target repo is present in gardener's local merge
allow-list (`gardener/merge_allowlist.py`, managed via
`gardener allowlist add/remove/list`). This is built by `tend_mode_spec()`
below rather than a static `MODE_SPECS` entry, since it's the one mode whose
`allowedTools` legitimately varies per invocation — everything else about
the safety posture (tool list, permission mode, the non-merge Bash
patterns) is fixed. When either condition is false, the merge pattern is
simply never added to the argv gardener builds — `claude` never sees it as
a candidate to approve, the same structural-absence mechanism as every
other excluded tool in this file, not an instruction asking the model to
refrain.

## Live transcript visibility

`run_claude` below still dispatches via one blocking `subprocess.run` call,
exactly as described above — that mechanism (timeout, kill-on-timeout,
`--output-format json` capture/parsing) is untouched. The one addition is a
background daemon thread, started right before that blocking call, which
polls briefly for the session's own live JSONL transcript file (which
Claude Code writes for every `-p` session regardless) and prints its path
to stderr as soon as it shows up — often several minutes before the
dispatch itself completes. See `transcript.py`'s module docstring for the
empirically-confirmed transcript-path encoding rule and the full design;
this is a visibility nicety layered on top of the dispatch, not a change to
the dispatch mechanism itself, and a failure or timeout inside it can never
affect (or even be noticed by) the real dispatch.
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

from gardener.transcript import start_transcript_watcher

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

# 45 min. align's 30-minute default was calibrated for a read-mostly gap
# pass. tend runs a full triage -> implement -> test -> PR -> docs-check ->
# self-audit cycle in ONE session with no subagent fan-out and no
# wait-for-review loop (both structurally excluded, see module docstring),
# so it needs more room than align for install/build/test steps align never
# has to do — but it still must fail fast enough that one stuck repo can't
# consume an entire overnight multi-repo batch. 45 min is roughly 1.5x
# align's ceiling: generous for one cycle on a modest repo, while still
# giving an operator dispatching N repos sequentially overnight a
# predictable per-repo ceiling (N * 45 min) to plan a batch size around.
TEND_DEFAULT_TIMEOUT_SECONDS = 2700

# 15 min, not exposed as a separate CLI flag — this is an internal step of
# `gardener tend` (generating a missing dev-loop skill before the real tend
# dispatch), not something an operator dispatches directly. It only has to
# cover repo exploration + writing one skill file, which is lighter than
# either align's gap analysis or a full tend cycle.
CREATE_DEV_LOOP_TIMEOUT_SECONDS = 900


class Mode(str, Enum):
    REPORT = "report"
    IMPLEMENT = "implement"
    FILE_ISSUE = "file-issue"
    CREATE_DEV_LOOP = "create-dev-loop"
    TEND = "tend"


@dataclass(frozen=True)
class ModeSpec:
    tools: tuple[str, ...]
    permission_mode: str
    allowed_tools: tuple[str, ...] = field(default_factory=tuple)


# The actual safety gate. Report mode's tool list contains no tool capable
# of writing a file or running a shell command — confirmed live, see the
# module docstring — so no permission-mode or allowedTools scoping can
# matter for it; it's included anyway as defense in depth.
#
# Tend mode's non-merge Bash surface is deliberately broader than
# implement's: a real dev-loop cycle has to be able to run *the target
# repo's own* build/test command, which gardener can't know in advance
# (see dev_loop.py) — so a short, curated set of common
# language/build-tool invocation prefixes is included alongside git/gh.
# This is a wider trust surface than align --implement's, on purpose and
# documented as such (see README's Safety model section) — not an
# oversight. `gh pr merge` is never part of this fixed list; it's added
# per-invocation by tend_mode_spec() only when eligible (see module
# docstring's "Merge allow-list" section).
TEND_BASE_ALLOWED_TOOLS: tuple[str, ...] = (
    "Edit",
    "Write",
    # The Skill tool itself needs its own --allowedTools entry per skill
    # name, same as Bash needs a per-command pattern — confirmed live: a
    # bare "Skill" in --tools without "Skill(code-review)" here was denied
    # ("Execute skill: code-review" -> tool_use_id denied, toolDenialKind
    # "user-rejected") during the first real end-to-end tend dispatch
    # (dmccoystephenson/snmp-command-generator, 2026-07-18). Re-tested in
    # isolation with the pattern added below and confirmed permitted.
    # code-review is the one skill tend's preamble tells a dispatched run
    # to prefer for its inline review step (see dev_loop.py) — it reviews
    # and fixes in place, the same trust level Edit/Write above already
    # carry, so pre-approving it doesn't widen tend's actual trust surface.
    "Skill(code-review)",
    "Bash(git *)",
    "Bash(gh pr create *)",
    "Bash(gh pr edit *)",
    "Bash(gh pr view *)",
    "Bash(gh pr list *)",
    "Bash(gh pr diff *)",
    "Bash(gh pr checks *)",
    "Bash(gh pr comment *)",
    "Bash(gh pr close *)",
    "Bash(gh issue *)",
    "Bash(gh repo view *)",
    "Bash(gh run list *)",
    "Bash(gh run view *)",
    "Bash(gh run watch *)",
    # Common build/test invocation verbs — see comment above.
    "Bash(python3 *)",
    "Bash(python *)",
    "Bash(pip install *)",
    "Bash(pip3 install *)",
    "Bash(npm install *)",
    "Bash(npm test*)",
    "Bash(npm run *)",
    "Bash(pytest *)",
    "Bash(go build *)",
    "Bash(go test *)",
    "Bash(go vet *)",
    "Bash(mvn *)",
    "Bash(./gradlew *)",
    "Bash(gradle *)",
    "Bash(make *)",
    "Bash(cargo build *)",
    "Bash(cargo test *)",
)

# Never included in TEND_BASE_ALLOWED_TOOLS above — added by tend_mode_spec()
# only when both --allow-merge and the repo's own allow-list entry hold.
# See module docstring's "Merge allow-list" section and merge_allowlist.py.
MERGE_ALLOWED_TOOL = "Bash(gh pr merge *)"

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
    Mode.CREATE_DEV_LOOP: ModeSpec(
        # Generates ~/local-skills/<slug>-dev-loop/<slug>-dev-loop.md and
        # symlinks it into ~/.claude/commands/ — see dev_loop.py. Read-only
        # against the target repo itself (exploration only); Write is only
        # ever instructed to touch the skill file location, never the
        # target repo (see the prompt's explicit scoping note — Write
        # itself isn't structurally confined to any directory, per the
        # module docstring's finding #3).
        tools=("Read", "Grep", "Glob", "Write", "Bash"),
        permission_mode="default",
        allowed_tools=(
            "Write",
            "Bash(git *)",
            "Bash(gh repo view *)",
            "Bash(gh pr list *)",
            "Bash(mkdir *)",
            "Bash(ln *)",
            "Bash(ls *)",
        ),
    ),
    # Mode.TEND has no static entry here — its allowed_tools varies by
    # whether this invocation is merge-eligible. Use tend_mode_spec().
}


def tend_mode_spec(allow_merge_eligible: bool) -> ModeSpec:
    """Build tend mode's ModeSpec. `allow_merge_eligible` must already be
    the fully-evaluated result of BOTH `--allow-merge` on the CLI AND the
    target repo being present in the local merge allow-list (see
    cli.py's `cmd_tend` and `merge_allowlist.py`) — this function does not
    re-check either condition, it only decides whether to append
    MERGE_ALLOWED_TOOL to the fixed base list.

    No Agent, ScheduleWakeup, AskUserQuestion, ToolSearch, or
    ReportFindings in `tools` — confirmed absent/excluded by design, see
    module docstring point 5. AskUserQuestion is never reachable regardless
    (point 1); the other four are actively left out of `tools` here so they
    don't newly become available even though they exist by default in an
    unscoped session.
    """
    allowed = TEND_BASE_ALLOWED_TOOLS + ((MERGE_ALLOWED_TOOL,) if allow_merge_eligible else ())
    return ModeSpec(
        tools=("Read", "Grep", "Glob", "Edit", "Write", "Bash", "Skill"),
        permission_mode="default",
        allowed_tools=allowed,
    )


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
    mode_spec: Optional[ModeSpec] = None,
) -> list[str]:
    spec = mode_spec if mode_spec is not None else MODE_SPECS[mode]
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
    mode_spec: Optional[ModeSpec] = None,
) -> DispatchResult:
    """Dispatch one headless `claude -p` run and block until it finishes.

    `mode_spec` is required for Mode.TEND (build it with `tend_mode_spec()`
    first, since its allowed_tools varies per invocation) and optional for
    every other mode, which fall back to their fixed `MODE_SPECS` entry.

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
    if mode_spec is None and mode not in MODE_SPECS:
        raise DispatchError(f"unknown mode: {mode} (no mode_spec given and no fixed entry)")

    argv = _build_invocation(mode, prompt, cwd, add_dirs or [], model=model, mode_spec=mode_spec)

    # Started right before the blocking call below so the poll runs
    # concurrently with the real dispatch, not before or after it — see
    # this module's "Live transcript visibility" docstring section and
    # transcript.py. `after` is wall-clock time taken here, before the
    # subprocess exists, so a transcript file created in the brief window
    # between this line and `claude` actually starting is still counted.
    start_transcript_watcher(cwd, after=time.time())

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
