"""argparse-based CLI: `gardener align`, `gardener tend`,
`gardener allowlist`, `gardener garden`, `gardener overnight`,
`gardener status`, `gardener tail-transcript`, and `gardener dashboard`.

This module is pure orchestration — it validates input, prepares the
target repo and conventions checkouts, builds the prompt, calls
`dispatch.run_claude`, and records the outcome. All judgment about what's
actually wrong with a repo (or what to actually change) happens inside the
dispatched Claude run, not here. `tend`'s own safety mechanics (which tools
exist, which get pre-approved, the headless-dispatch prompt preamble) live
in `dispatch.py` and `dev_loop.py`, not here either — see those modules'
docstrings.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Optional

from gardener import (
    conventions, dashboard, dev_loop, garden, merge_allowlist, notify, overnight, repo_lock,
    run_log, state, transcript,
)
from gardener.dispatch import (
    AUTH_RETRY_BACKOFF_SECONDS,
    CREATE_DEV_LOOP_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    TEND_DEFAULT_TIMEOUT_SECONDS,
    DispatchError,
    Mode,
    is_device_global_failure,
    looks_like_auth_failure,
    looks_like_network_failure,
    looks_like_usage_limit,
    run_claude,
    tend_mode_spec,
)

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

# Per-command timeouts for the cache-clone refresh in
# `clone_or_refresh_target_repo`. The fetch/checkout steps are
# network/index-bound and short; `git clean` is bound by how fast this
# device can unlink a large number of files, which is a different order of
# magnitude — see PRESERVED_DEPENDENCY_DIRS below for the real failure that
# separated these two.
REFRESH_TIMEOUT_SECONDS = 60
CLEAN_TIMEOUT_SECONDS = 300

# Directories `git clean -fdx` is told to leave alone during a refresh.
#
# These are dependency caches: large, expensive to recreate, never part of
# the repo's source, and — unlike a build output directory — not something
# a stale copy of can meaningfully corrupt the next run's results. Build
# outputs (`build/`, `target/`, `dist/`) are deliberately NOT listed; those
# still get cleaned, since a stale one leaking into a later run is exactly
# what the `-x` clean is here to prevent.
#
# Grounded in a real, repeating failure: Dans-Plugins/dansplugins-dot-com
# failed every single overnight run with `Command '['git', 'clean',
# '-fdx']' timed out after 60 seconds` — its cache clone carries a 190MB
# `node_modules` (194MB of a 194MB checkout), and unlinking that many files
# on this device's proot filesystem does not finish in 60s. Preserving it
# fixes the timeout and, as a bonus, saves the dispatched run from
# reinstalling the same dependencies on every single tend.
#
# `-e` patterns are still honored when `-x` is passed (`-x` discards the
# *standard* ignore rules, not the ones given explicitly with `-e`), which
# is what makes this work without giving up the rest of the clean.
PRESERVED_DEPENDENCY_DIRS = ("node_modules", ".venv", "venv", ".gradle")


def repo_arg(value: str) -> str:
    """argparse `type=` for every `--repo` that names a repo gardener will
    act on (`align`, `tend`, `allowlist add`, `garden add`), so a typo is
    rejected as a normal usage error at parse time rather than hours later
    in an unattended `overnight` run — or, for the allow-list, never: an
    entry that can't match is a repo the operator believes is
    merge-authorized while `merge_eligible()` silently returns False.

    Deliberately not applied to `allowlist remove` / `garden remove`:
    removing an entry that is *already* malformed (hand-edited into the
    JSON, or added before this validation existed) has to stay possible.
    `clone_or_refresh_target_repo`'s own check stays as the backstop for
    callers that never go through argparse — `cmd_overnight`'s synthetic
    Namespace in `_dispatch_one_for_overnight`, and a hand-edited
    `garden.json`."""
    if not REPO_RE.match(value):
        raise argparse.ArgumentTypeError(f"must look like owner/name, got: {value!r}")
    return value

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "align_repo.md.tmpl"

MODE_INSTRUCTIONS = {
    Mode.REPORT: """4. Report the gap checklist only. You do NOT have file-write or shell
   tools available in this session — that is enforced at the tool level,
   not just by these instructions — so there is nothing further to
   authorize or refuse. Do not attempt to open a PR or an issue.""",
    Mode.IMPLEMENT: """4. You are authorized to implement the identified gaps directly in this
   checked-out repository, following ITS OWN conventions (language, build
   tool, test framework) rather than copying the conventions repo's literal
   examples. Concretely:
   - Create a branch named per COMMIT_PR_CONVENTIONS.md's prefix
     convention (feature/... or fix/...).
   - Make the minimal changes needed to close the gaps found in step 2.
     Do not refactor existing code or alter application behavior.
   - Commit using this repo's own established commit style if it has one,
     otherwise the imperative-mood/no-trailing-period convention from
     COMMIT_PR_CONVENTIONS.md. Only add a Co-Authored-By trailer if this
     repo's own history already uses that convention.
   - Push the branch and open a pull request (`gh pr create`) summarizing
     the gaps closed, referencing the conventions doc(s) by URL instead of
     restating their rules in the PR body.
   - Only `git *` and `gh pr *` commands are available to you; nothing
     else is pre-approved and there is no human available in this session
     to approve anything further. Do not merge the PR.""",
    Mode.FILE_ISSUE: """4. You are authorized to open exactly ONE GitHub issue in this repo
   (`gh issue create`) summarizing every gap found in step 2, scoped so a
   *-dev-loop skill in this repo (if one exists) can pick it up next
   cycle. Do not implement any fix yourself — you have no file-write
   tools available in this session, only `gh issue *` commands are
   pre-approved. Do not open more than one issue.""",
}


def default_repos_cache_dir() -> Path:
    import os

    override = os.environ.get("GARDENER_CACHE_DIR")
    base = Path(override) if override else Path.home() / ".cache" / "gardener"
    return base / "repos"


def _run(argv: list[str], cwd: Optional[Path] = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout
    )


def _default_branch_name(repo: str) -> str:
    res = _run(
        ["gh", "repo", "view", repo, "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        timeout=30,
    )
    branch = (res.stdout or "").strip()
    if res.returncode != 0 or not branch:
        raise RuntimeError(f"could not determine default branch for {repo}: {res.stderr.strip()}")
    return branch


def clone_or_refresh_target_repo(repo: str, cache_dir: Path, refresh: bool = True) -> Path:
    """Read-only checkout of the target repo, via `gh repo clone` (so it
    picks up the same auth `gh` already has, private repos included). A
    refresh hard-resets to origin's default branch first — defense in
    depth so a leftover local mutation from a hypothetical earlier
    implement/tend-mode run never leaks into a later report-mode gap read.

    Always lands on the repo's real default branch (via `gh repo view
    --json defaultBranchRef`), not whatever branch happened to be checked
    out — a real bug found while testing `gardener tend` for real
    (2026-07-18): a previous `tend` run had left the cache clone checked
    out on a feature branch it pushed (`test-cli-output-file-coverage`);
    the next run's refresh tried `git reset --hard
    origin/test-cli-output-file-coverage`, which failed outright because
    the preceding `git fetch --depth 1 origin` (no refspec) doesn't
    reliably bring down a non-default branch's ref for a shallow clone.
    Fetching and checking out the actual default branch by name every time
    sidesteps this rather than trusting whatever ref HEAD happened to be
    on from a previous run."""
    if not REPO_RE.match(repo):
        raise ValueError(f"--repo must look like owner/name, got: {repo!r}")
    if shutil.which("gh") is None:
        raise RuntimeError("`gh` not found on PATH — install the GitHub CLI first")

    dest = cache_dir / repo.replace("/", "__")
    if not (dest / ".git").is_dir():
        cache_dir.mkdir(parents=True, exist_ok=True)
        res = _run(["gh", "repo", "clone", repo, str(dest), "--", "--depth", "1"], timeout=180)
        if res.returncode != 0:
            raise RuntimeError(f"gh repo clone {repo} failed: {res.stderr.strip()}")
    elif refresh:
        origin = _run(["git", "remote", "get-url", "origin"], cwd=dest, timeout=15)
        if repo not in (origin.stdout or ""):
            raise RuntimeError(
                f"cache dir {dest} exists but its origin doesn't match {repo} — refusing to reuse it"
            )
        default_branch = _default_branch_name(repo)
        clean_cmd = ["git", "clean", "-fdx"]
        for keep in PRESERVED_DEPENDENCY_DIRS:
            clean_cmd += ["-e", keep]
        for cmd, cmd_timeout in (
            (["git", "fetch", "--depth", "1", "origin", default_branch], REFRESH_TIMEOUT_SECONDS),
            # -B (create-or-reset) rather than plain checkout: correctly
            # lands on a clean copy of the default branch regardless of
            # what was checked out before (a different branch, a detached
            # HEAD, or the default branch itself but stale/dirty).
            (
                ["git", "checkout", "-B", default_branch, f"origin/{default_branch}"],
                REFRESH_TIMEOUT_SECONDS,
            ),
            (clean_cmd, CLEAN_TIMEOUT_SECONDS),
        ):
            res = _run(cmd, cwd=dest, timeout=cmd_timeout)
            if res.returncode != 0:
                raise RuntimeError(f"refresh of {dest} failed at `{' '.join(cmd)}`: {res.stderr.strip()}")
    return dest


def current_branch(repo_dir: Path) -> str:
    res = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, timeout=15)
    return res.stdout.strip() or "main"


def find_orphaned_pr(repo: str, timeout: int = 30) -> Optional[dev_loop.OrphanedPR]:
    """Look for an open PR on `repo` that carries `dev_loop.ORPHAN_MARKER`
    in its body — i.e. one opened by a previous `tend` dispatch that never
    got to record a completed `state.Run` (this device killing the whole
    `gardener overnight` process mid-dispatch is the expected cause, see
    overnight.py's resume-cursor caveat). Best-effort: any `gh` failure
    (not authenticated, network hiccup, malformed JSON) is treated the same
    as "no orphan found" rather than raised, since this check must never be
    the reason a normal `tend` dispatch fails outright — it's an
    enhancement over a fresh dispatch, not a precondition for one.

    If more than one open PR carries the marker (e.g. two interrupted runs
    in a row before either could be continued), the most recently created
    one wins — sorted explicitly here rather than trusting `gh pr list`'s
    own default ordering, so this stays deterministic and testable.
    """
    if shutil.which("gh") is None:
        return None
    try:
        res = _run(
            ["gh", "pr", "list", "--repo", repo, "--state", "open",
             "--json", "number,headRefName,body,createdAt", "--limit", "50"],
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"gardener: NOTE — could not check for orphaned PRs on {repo} (non-fatal): "
              f"{e}", file=sys.stderr)
        return None
    if res.returncode != 0:
        print(f"gardener: NOTE — could not check for orphaned PRs on {repo} (non-fatal): "
              f"{res.stderr.strip()}", file=sys.stderr)
        return None
    try:
        prs = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return None
    candidates = [pr for pr in prs if dev_loop.ORPHAN_MARKER in (pr.get("body") or "")]
    if not candidates:
        return None
    candidates.sort(key=lambda pr: pr.get("createdAt", ""), reverse=True)
    newest = candidates[0]
    return dev_loop.OrphanedPR(number=newest["number"], head_branch=newest["headRefName"])


def fetch_open_issue_count(repo: str, timeout: int = 30) -> Optional[int]:
    """One repo's open-issue count, via `gh issue list --state open --json
    number -q length` — the actual `gh`-calling side of the `issue-count`
    overnight strategy (see `overnight.py`'s `order_by_issue_count`, which
    takes the result as an already-fetched mapping rather than calling this
    itself, so that function stays unit-testable without `gh`).

    Best-effort, same posture as `find_orphaned_pr`: any `gh` failure (not
    authenticated, network hiccup, timeout, malformed output) returns `None`
    rather than raising — one repo's fetch failing must not abort ordering
    the rest of the garden; `order_by_issue_count` treats a `None`/missing
    entry as count 0 (lowest priority), not a crash."""
    if shutil.which("gh") is None:
        return None
    try:
        res = _run(
            ["gh", "issue", "list", "--repo", repo, "--state", "open",
             "--json", "number", "-q", "length"],
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    try:
        return int((res.stdout or "").strip())
    except ValueError:
        return None


def fetch_issue_counts(garden: list[str], timeout: int = 30) -> dict[str, int]:
    """Every garden repo's open-issue count, one `gh` call per repo. Issue
    #6 also floats a batched GraphQL query as an alternative to N sequential
    `gh` calls — skipped here since gardener's garden is meant to stay a
    small, hand-curated, opt-in list (see `garden.py`'s module docstring),
    not something growing large enough for N sequential `gh` calls once per
    `overnight` run to matter; revisit if that assumption stops holding.

    Repos whose fetch fails are simply omitted from the returned mapping —
    `order_by_issue_count` treats a missing entry as priority 0, so one
    repo's `gh` hiccup deprioritizes it this run rather than aborting the
    whole ordering."""
    counts: dict[str, int] = {}
    for repo in garden:
        count = fetch_open_issue_count(repo, timeout=timeout)
        if count is not None:
            counts[repo] = count
    return counts


def build_prompt(
    mode: Mode,
    repo: str,
    target_cwd: Path,
    conventions_dir: Path,
    default_branch: str,
    conventions_url: str,
) -> str:
    template = Template(PROMPT_TEMPLATE_PATH.read_text())
    return template.substitute(
        repo=repo,
        target_cwd=str(target_cwd),
        conventions_dir=str(conventions_dir),
        conventions_url=conventions_url,
        default_branch=default_branch,
        mode_instructions=MODE_INSTRUCTIONS[mode],
    )


SUMMARY_RE = re.compile(r"GARDENER_SUMMARY:\s*(.+)")


def extract_gap_summary(result_text: str) -> str:
    m = SUMMARY_RE.search(result_text)
    if m:
        return m.group(1).strip()
    # Claude didn't follow the requested footer format exactly — fall back
    # to a truncated excerpt rather than losing the outcome entirely.
    stripped = result_text.strip()
    return (stripped[:200] + "…") if len(stripped) > 200 else stripped


def _notify_run(run: state.Run) -> None:
    """Fire an outcome notification for a recorded run. Thin by design —
    this only turns an already-recorded `state.Run` into a (title,
    message, level) tuple and hands it to a `Notifier`; the actual
    alerting mechanics live in notify.py.

    Deliberately generic across whatever `mode` value is present, not just
    the ones defined in this file: outcome "error" is always ERROR, mode
    "report" (Mode.REPORT.value) is always the routine/informational case,
    and anything else is treated as an authorized mutation worth flagging
    distinctly — this covers any future mode (e.g. a `tend` subcommand)
    that also calls `state.record_run(...)` and then this same helper,
    without this function needing to know that mode's specifics.
    """
    if run.outcome == "error":
        level = notify.Level.ERROR
        title = f"gardener {run.mode}: FAILED — {run.repo}"
    elif run.mode == Mode.REPORT.value:
        level = notify.Level.INFO
        title = f"gardener {run.mode}: {run.repo}"
    else:
        # Any non-report, non-error outcome means this mode was authorized
        # to actually mutate something (branch/commit/PR, issue, ...) —
        # stand this out from a routine report-only run rather than
        # blending it in.
        level = notify.Level.WARNING
        title = f"gardener {run.mode}: MUTATION — {run.repo}"

    message = run.gap_summary or "(no summary)"
    try:
        notify.default_notifier().notify(title, message, level)
    except Exception as e:  # noqa: BLE001 - alerting must never break the run it reports on
        print(f"gardener: notification failed (non-fatal): {e}", file=sys.stderr)


def _safe_record_run(run: state.Run, db_path: Optional[Path]) -> None:
    """state.record_run, without ever raising — mirrors _notify_run's own
    "alerting must never break the run it reports on" stance, extended to
    the record step itself. Without this, a state.record_run failure (e.g.
    sqlite3.OperationalError, or an OSError creating
    ~/.local/state/gardener/) raw-crashes the whole process merely for
    trying to record a dispatch that already completed — see issue #9."""
    try:
        state.record_run(run, db_path=db_path)
    except Exception as e:  # noqa: BLE001 - recording must never break the run it reports on
        print(f"gardener: state.record_run failed (non-fatal): {e}", file=sys.stderr)


def _record_and_notify(run: state.Run, db_path: Optional[Path]) -> None:
    """Record a completed run and notify its outcome, without ever raising."""
    _safe_record_run(run, db_path)
    _notify_run(run)


def cmd_align(args: argparse.Namespace) -> int:
    if args.implement and args.file_issue:
        print("error: --implement and --file-issue are mutually exclusive", file=sys.stderr)
        return 2

    mode = Mode.IMPLEMENT if args.implement else Mode.FILE_ISSUE if args.file_issue else Mode.REPORT

    # Resolved before the lock and before any progress output: an
    # unconfigured conventions repo is a setup error, and printing
    # "aligning X against ..." first would imply a run had started.
    try:
        conventions_url = conventions.resolve_url(args.conventions_repo)
    except conventions.ConventionsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    print(f"gardener: aligning {args.repo} against {conventions_url} (mode={mode.value})", file=sys.stderr)
    if mode is Mode.REPORT:
        print("gardener: report-only — Claude has no write/shell tools in this run", file=sys.stderr)

    try:
        with repo_lock.repo_lock(args.repo):
            conv = conventions.ensure_conventions(
                refresh=not args.no_refresh_conventions, url=conventions_url
            )
            print(f"gardener: conventions checked out at {conv.path}", file=sys.stderr)

            target_dir = clone_or_refresh_target_repo(
                args.repo, default_repos_cache_dir(), refresh=not args.no_refresh_target
            )
            print(f"gardener: {args.repo} checked out at {target_dir}", file=sys.stderr)

            branch = current_branch(target_dir)
            prompt = build_prompt(
                mode, args.repo, target_dir, conv.path, branch, conventions_url
            )

            print("gardener: dispatching claude (this can take a while)...", file=sys.stderr)
            result = run_claude(
                mode=mode,
                prompt=prompt,
                cwd=target_dir,
                add_dirs=[conv.path],
                model=args.model,
                timeout=args.timeout,
            )
    except repo_lock.RepoLockedError as e:
        print(f"gardener: {e}", file=sys.stderr)
        locked_run = state.Run(
            repo=args.repo,
            mode=mode.value,
            outcome="error",
            timestamp=state.now_iso(),
            gap_summary=str(e),
        )
        _record_and_notify(locked_run, args.state_db)
        return 1
    except (DispatchError, RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as e:
        print(f"gardener: error: {e}", file=sys.stderr)
        failed_run = state.Run(
            repo=args.repo,
            mode=mode.value,
            outcome="error",
            timestamp=state.now_iso(),
            gap_summary=str(e),
        )
        _record_and_notify(failed_run, args.state_db)
        return 1

    outcome = "error" if not result.ok else mode.value
    gap_summary = extract_gap_summary(result.result_text) if result.result_text else (result.stderr or "no output")

    completed_run = state.Run(
        repo=args.repo,
        mode=mode.value,
        outcome=outcome,
        timestamp=state.now_iso(),
        gap_summary=gap_summary,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        cost_usd=result.cost_usd,
        claude_session_id=result.session_id,
    )
    _record_and_notify(completed_run, args.state_db)

    print(result.result_text or "(no output)")
    print("", file=sys.stderr)
    print(
        f"gardener: done in {result.duration_ms}ms, "
        f"cost=${result.cost_usd if result.cost_usd is not None else '?'}, "
        f"denials={len(result.permission_denials)}, ok={result.ok}",
        file=sys.stderr,
    )
    if result.permission_denials:
        print(
            "gardener: NOTE — Claude attempted action(s) outside this mode's "
            "pre-approved scope and they were blocked (see denials above)",
            file=sys.stderr,
        )
    if result.timed_out:
        print(f"gardener: timed out after {args.timeout}s", file=sys.stderr)
        return 1
    return 0 if result.ok else 1


def merge_eligible(repo: str, allow_merge_flag: bool, allowlist_path: Optional[Path] = None) -> bool:
    """The single place `--allow-merge` and the allow-list are combined.
    Both must hold — see dispatch.py's tend_mode_spec() and its module
    docstring's "Merge allow-list" section for how this feeds the actual
    structural gate (whether Bash(gh pr merge *) is ever added to the
    dispatched session's --allowedTools)."""
    return allow_merge_flag and merge_allowlist.is_allowed(repo, path=allowlist_path)


@dataclass
class TendResult:
    """What one `_dispatch_tend` call actually produced, returned as
    structured data instead of the printed-stdout capture `cmd_overnight`
    used to rely on (`io.StringIO()` + `contextlib.redirect_stdout` —
    process-global, not thread-safe, and the actual blocker to dispatching
    more than one repo's `tend` concurrently — see issue #15). `dispatched`
    is False for the two paths that return before the real tend dispatch
    ever runs (a setup exception, or a failed create-dev-loop bootstrap) —
    `cmd_tend` uses it to skip printing a result/timing summary for those,
    matching its exact original behavior."""

    exit_code: int
    dispatched: bool = True
    ok: bool = False
    result_text: str = ""
    run: Optional[state.Run] = None
    duration_ms: Optional[int] = None
    cost_usd: Optional[float] = None
    permission_denials: list = field(default_factory=list)
    timed_out: bool = False
    # Propagated straight from `DispatchResult.auth_failed` — see its
    # comment there and `cmd_overnight`, which stops the whole batch on it.
    auth_failed: bool = False
    # Propagated straight from `DispatchResult.blocked` — the broader
    # device-global signal `cmd_overnight` actually aborts on.
    blocked: bool = False


def _dispatch_tend(args: argparse.Namespace) -> TendResult:
    """The actual clone/dispatch/record/notify work behind `gardener tend`.
    Split out from `cmd_tend` (a thin CLI wrapper around this) so a caller
    that wants the dispatched result — namely `cmd_overnight`, which may
    call this from a worker thread when `--concurrency` > 1 — gets it as a
    plain return value, not by intercepting stdout.

    Brackets the work with a matched pair of repo-naming stderr lines —
    `gardener: tending <repo> ...` on the way in and `gardener: finished
    tending <repo>` on the way out — which is what `dashboard.py`'s
    `parse_in_progress` reads to decide what is still in flight. The
    closing line is in a `finally` on purpose: `_run_tend_dispatch` has
    four separate return paths (setup exception, repo already locked,
    failed create-dev-loop bootstrap, normal completion) and the dashboard
    used to have no marker at all for the first three, so it inferred
    completion from `notify.py`'s Discord-success line instead — which an
    operator with no webhook configured never prints, leaving every repo
    the log ever started stuck in "Currently tending" forever (issue #51).
    Both lines name the repo so a `--concurrency 2` log, where two
    dispatches interleave, stays attributable to a human reading it too."""
    print(f"gardener: tending {args.repo} (allow_merge={args.allow_merge})", file=sys.stderr)
    try:
        return _run_tend_dispatch(args)
    finally:
        print(f"gardener: finished tending {args.repo}", file=sys.stderr)


def _run_tend_dispatch(args: argparse.Namespace) -> TendResult:
    """`_dispatch_tend`'s body — see it for why the two are separate."""
    try:
        with repo_lock.repo_lock(args.repo):
            target_dir = clone_or_refresh_target_repo(
                args.repo, default_repos_cache_dir(), refresh=not args.no_refresh_target
            )
            print(f"gardener: {args.repo} checked out at {target_dir}", file=sys.stderr)
            branch = current_branch(target_dir)

            orphaned_pr = find_orphaned_pr(args.repo)
            if orphaned_pr is not None:
                print(
                    f"gardener: found orphaned PR #{orphaned_pr.number} (branch "
                    f"{orphaned_pr.head_branch!r}) on {args.repo} — this run will continue "
                    "it instead of starting fresh",
                    file=sys.stderr,
                )

            slug = dev_loop.skill_slug(args.repo)
            if not dev_loop.has_dev_loop_skill(slug):
                print(
                    f"gardener: no existing /{slug} skill — dispatching create-dev-loop first",
                    file=sys.stderr,
                )
                create_prompt = dev_loop.build_create_dev_loop_prompt(args.repo, slug, target_dir)
                create_result = run_claude(
                    mode=Mode.CREATE_DEV_LOOP,
                    prompt=create_prompt,
                    cwd=target_dir,
                    # Symmetric with align's add_dirs=[conv.path]: this dispatch's
                    # whole job is reading/writing exactly these two directories
                    # (see dev_loop.py's LOCAL_SKILLS_DIR/COMMANDS_DIR and
                    # dispatch.py's module docstring finding #3) — without this,
                    # Read/Bash(mkdir *)/Bash(ls *) etc. are sandboxed out of both
                    # dirs even though Write itself isn't, so a first attempt that
                    # leaves any partial state (skill file written, symlink not
                    # yet created) has no recovery path on retry: the dispatched
                    # session can't even read what's in its way. Confirmed as the
                    # root cause of a real failed run (dmccoystephenson/gardener,
                    # 2026-07-18) — see this repo's git history for the transcript
                    # comparison against that same overnight run's other,
                    # successful dev-loop dispatch, which had no stale artifact
                    # blocking it.
                    add_dirs=[dev_loop.LOCAL_SKILLS_DIR, dev_loop.COMMANDS_DIR],
                    model=args.model,
                    timeout=CREATE_DEV_LOOP_TIMEOUT_SECONDS,
                )
                # gh repo create is never in this mode's allowed_tools (a
                # deliberate, higher-risk-class exclusion — see
                # dispatch.py's MODE_SPECS[Mode.CREATE_DEV_LOOP]), so
                # create-dev-loop's own Step 6 ("Create a private GitHub repo
                # for the skill") always gets structurally skipped here. Check
                # this live rather than assuming, per dev_loop.step6_unreachable's
                # own docstring — see issue #12.
                step6_gap = create_result.ok and dev_loop.step6_unreachable()
                create_run = state.Run(
                    repo=args.repo,
                    mode=Mode.CREATE_DEV_LOOP.value,
                    outcome=state.ERROR_OUTCOME
                    if not create_result.ok
                    else (
                        state.CREATED_INCOMPLETE_OUTCOME if step6_gap else state.CREATED_OUTCOME
                    ),
                    timestamp=state.now_iso(),
                    gap_summary=extract_gap_summary(create_result.result_text)
                    if create_result.result_text
                    else (create_result.stderr or "no output"),
                    exit_code=create_result.exit_code,
                    duration_ms=create_result.duration_ms,
                    cost_usd=create_result.cost_usd,
                    claude_session_id=create_result.session_id,
                )
                _safe_record_run(create_run, args.state_db)
                if not create_result.ok or not dev_loop.has_dev_loop_skill(slug):
                    print(
                        f"gardener: error: create-dev-loop dispatch did not produce a usable "
                        f"/{slug} skill (ok={create_result.ok}, "
                        f"exists={dev_loop.has_dev_loop_skill(slug)}) — not proceeding to tend",
                        file=sys.stderr,
                    )
                    # This dispatch's own outcome is already recorded above with
                    # its real mode ("create-dev-loop"); notify with that same
                    # outcome rather than fabricating a separate "tend" failure,
                    # since the tend dispatch itself never ran.
                    _notify_run(create_run)
                    return TendResult(exit_code=1, dispatched=False, run=create_run)
                if step6_gap:
                    print(
                        f"gardener: WARNING — /{slug} skill created, but create-dev-loop's "
                        "Step 6 (GitHub repo + issue tracker) was skipped — `gh repo create` "
                        "is outside this dispatch's allowed tools (see CLAUDE.md's dispatch "
                        "safety model). This skill's own dev-loop cycle has nowhere to file "
                        "self-audit findings until a human completes create-dev-loop's Step 6 "
                        f"manually (create a private GitHub repo for /{slug} and point it there).",
                        file=sys.stderr,
                    )
                    _notify_run(create_run)
                else:
                    print(f"gardener: /{slug} skill created", file=sys.stderr)
            else:
                print(f"gardener: found existing /{slug} skill", file=sys.stderr)

            eligible = merge_eligible(args.repo, args.allow_merge)
            if args.allow_merge and not eligible:
                print(
                    f"gardener: NOTE — --allow-merge was passed but {args.repo} is not in the "
                    "merge allow-list (see `gardener allowlist add`) — merge stays disabled this run",
                    file=sys.stderr,
                )
            print(f"gardener: dispatching /{slug} (merge_eligible={eligible})...", file=sys.stderr)

            tend_prompt = dev_loop.build_tend_prompt(
                args.repo, slug, target_dir, branch, eligible, orphaned_pr=orphaned_pr
            )
            result = run_claude(
                mode=Mode.TEND,
                prompt=tend_prompt,
                cwd=target_dir,
                model=args.model,
                timeout=args.timeout,
                mode_spec=tend_mode_spec(eligible),
            )
    except repo_lock.RepoLockedError as e:
        print(f"gardener: {e}", file=sys.stderr)
        locked_run = state.Run(
            repo=args.repo,
            mode=Mode.TEND.value,
            outcome="error",
            timestamp=state.now_iso(),
            gap_summary=str(e),
        )
        _record_and_notify(locked_run, args.state_db)
        return TendResult(exit_code=1, dispatched=False, run=locked_run)
    except (DispatchError, RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as e:
        print(f"gardener: error: {e}", file=sys.stderr)
        failed_run = state.Run(
            repo=args.repo,
            mode=Mode.TEND.value,
            outcome="error",
            timestamp=state.now_iso(),
            gap_summary=str(e),
        )
        _record_and_notify(failed_run, args.state_db)
        return TendResult(exit_code=1, dispatched=False, run=failed_run)

    outcome = "error" if not result.ok else Mode.TEND.value
    gap_summary = extract_gap_summary(result.result_text) if result.result_text else (result.stderr or "no output")

    completed_run = state.Run(
        repo=args.repo,
        mode=Mode.TEND.value,
        outcome=outcome,
        timestamp=state.now_iso(),
        gap_summary=gap_summary,
        exit_code=result.exit_code,
        duration_ms=result.duration_ms,
        cost_usd=result.cost_usd,
        claude_session_id=result.session_id,
    )
    _record_and_notify(completed_run, args.state_db)

    # Printed here (stderr), not left to cmd_tend's wrapper, so this summary
    # line still shows up in `gardener overnight`'s log too — cmd_overnight
    # calls _dispatch_tend directly, bypassing cmd_tend entirely (see
    # TendResult's docstring), so a print living only in cmd_tend would
    # silently never fire for the overnight path. Confirmed missing for
    # real during --concurrency testing (2026-07-18): all three repos in
    # that batch completed and were correctly recorded/classified, but
    # overnight's log never showed a "done in Xms" line for any of them.
    print("", file=sys.stderr)
    print(
        f"gardener: done in {result.duration_ms}ms, "
        f"cost=${result.cost_usd if result.cost_usd is not None else '?'}, "
        f"denials={len(result.permission_denials)}, ok={result.ok}",
        file=sys.stderr,
    )
    if result.permission_denials:
        print(
            "gardener: NOTE — the dispatched run attempted action(s) outside this mode's "
            "pre-approved scope and they were blocked (see denials above)",
            file=sys.stderr,
        )
    if result.timed_out:
        print(f"gardener: timed out after {args.timeout}s", file=sys.stderr)

    exit_code = 1 if (not result.ok or result.timed_out) else 0
    return TendResult(
        exit_code=exit_code,
        ok=result.ok,
        result_text=result.result_text or "",
        run=completed_run,
        duration_ms=result.duration_ms,
        cost_usd=result.cost_usd,
        permission_denials=result.permission_denials,
        timed_out=result.timed_out,
        auth_failed=result.auth_failed,
        blocked=result.blocked,
    )


def cmd_tend(args: argparse.Namespace) -> int:
    """`gardener tend`'s CLI entry point — dispatches via `_dispatch_tend`
    (which already prints the "done in Xms" stderr summary itself — see its
    body — so both direct CLI use and `cmd_overnight` get it) and prints
    the dispatched result text to stdout, then returns the exit code.
    Prints nothing beyond `_dispatch_tend`'s own stderr progress lines when
    the dispatch never actually ran (`dispatched=False` — a setup error or
    a failed create-dev-loop bootstrap), matching this function's original
    behavior before the `TendResult` split."""
    result = _dispatch_tend(args)
    if not result.dispatched:
        return result.exit_code
    print(result.result_text or "(no output)")
    return result.exit_code


def cmd_allowlist(args: argparse.Namespace) -> int:
    path = args.allowlist_path
    if args.allowlist_action == "list":
        repos = merge_allowlist.list_allowed(path=path)
        if not repos:
            print("merge allow-list is empty")
            return 0
        for r in repos:
            print(r)
        return 0
    if args.allowlist_action == "add":
        added = merge_allowlist.add(args.repo, path=path)
        print(f"{'added' if added else 'already present'}: {args.repo}")
        return 0
    if args.allowlist_action == "remove":
        removed = merge_allowlist.remove(args.repo, path=path)
        print(f"{'removed' if removed else 'was not present'}: {args.repo}")
        return 0
    raise AssertionError(f"unreachable allowlist_action: {args.allowlist_action}")


def cmd_garden(args: argparse.Namespace) -> int:
    """Structurally identical to cmd_allowlist above, over garden.py instead
    of merge_allowlist.py — two independent opt-in lists, same shape, kept
    as separate small functions rather than parameterized into one so each
    stays a direct, obvious mapping onto its own module."""
    path = args.garden_file
    if args.garden_action == "list":
        repos = garden.list_garden(path=path)
        if not repos:
            print("garden is empty")
            return 0
        for r in repos:
            print(r)
        return 0
    if args.garden_action == "add":
        added = garden.add(args.repo, path=path)
        print(f"{'added' if added else 'already present'}: {args.repo}")
        return 0
    if args.garden_action == "remove":
        removed = garden.remove(args.repo, path=path)
        print(f"{'removed' if removed else 'was not present'}: {args.repo}")
        return 0
    raise AssertionError(f"unreachable garden_action: {args.garden_action}")


def _dispatch_one_for_overnight(repo: str, args: argparse.Namespace) -> overnight.RepoOutcome:
    """One repo's `tend --allow-merge` dispatch on `cmd_overnight`'s behalf,
    called either directly (concurrency=1) or from a `ThreadPoolExecutor`
    worker thread (concurrency>1 — see `cmd_overnight`). Never raises: one
    repo's crash must not abort the batch or leave a `Future` holding an
    unhandled exception, so it's caught here and turned into an errored
    `RepoOutcome` the same way the pre-concurrency sequential loop did."""
    tend_args = argparse.Namespace(
        repo=repo,
        allow_merge=True,
        model=args.model,
        timeout=TEND_DEFAULT_TIMEOUT_SECONDS,
        no_refresh_target=False,
        state_db=args.state_db,
    )
    try:
        result = _dispatch_tend(tend_args)
    except Exception as e:  # noqa: BLE001 - one repo's crash must not abort the batch
        print(f"gardener: overnight: {repo} raised an unexpected error: {e}", file=sys.stderr)
        # Classified from the exception text because this path is the *only*
        # place a GitHub outage can surface: `_dispatch_tend` resolves the
        # default branch and clones before it ever invokes `claude`, and both
        # steps raise RuntimeError on failure (see `_default_branch`/clone).
        # There is no DispatchResult here to carry a `blocked` flag, and
        # leaving it False is exactly what let 17 recorded runs march the
        # whole garden through an unreachable api.github.com.
        return overnight.RepoOutcome(
            repo=repo,
            errored=True,
            gap_summary=str(e),
            blocked=is_device_global_failure(str(e)),
        )
    outcome = overnight.classify_outcome(repo, result.run, result.result_text)
    outcome.blocked = result.blocked
    if not outcome.blocked and outcome.errored:
        # A failure recorded as a normal errored run rather than raised —
        # `_dispatch_tend` catches some setup failures itself and reports
        # them through `state.Run.gap_summary`, which is then all we have to
        # classify from.
        outcome.blocked = is_device_global_failure(outcome.gap_summary)
    return outcome


def _blocking_reason(outcomes: list) -> tuple:
    """`(what went wrong, what the operator should do)` for the first
    blocked repo in `outcomes`, so the abort message and its notification
    name the real cause instead of always saying "could not authenticate".

    Worth the branching: these three demand genuinely different actions —
    logging `claude` back in, waiting out a quota reset, and checking the
    network are not interchangeable, and an operator told the wrong one
    wastes the rest of the night acting on it. Falls back to generic
    wording if a repo is flagged by a marker set that grows past these
    three, rather than asserting a reason it can't actually support."""
    text = next((o.gap_summary or "" for o in outcomes if o.blocked), "")
    if looks_like_usage_limit(text, ""):
        return (
            "the usage/session limit is exhausted",
            "Wait for the reset time named in the message above, then re-run "
            "`gardener overnight` — credentials are fine, there is simply no quota left.",
        )
    if looks_like_network_failure(text, ""):
        return (
            "GitHub was unreachable",
            "Check this device's connectivity and https://githubstatus.com, then re-run "
            "`gardener overnight`.",
        )
    if looks_like_auth_failure(text, ""):
        return (
            f"the dispatched run could not authenticate "
            f"(after {len(AUTH_RETRY_BACKOFF_SECONDS)} retries)",
            "Re-run `gardener overnight` once credentials work again — `claude` may just "
            "need a fresh login.",
        )
    return (
        "a device-wide failure blocked the batch",
        "Resolve the condition reported above, then re-run `gardener overnight`.",
    )


def _first_blocked_index(outcomes: list) -> int:
    """Position of the first device-globally-blocked repo in `outcomes`,
    which is maintained in the same order as this run's `order` list (batches
    are dispatched in order and each batch's outcomes are re-sorted back into
    `repo_batch` order — see `cmd_overnight`), so this doubles as the number
    of repos the round-robin resume cursor may safely advance by. Returns
    `len(outcomes)` if nothing was blocked, i.e. "advance past all of them",
    matching the non-abort path."""
    for i, outcome in enumerate(outcomes):
        if outcome.blocked:
            return i
    return len(outcomes)


def cmd_overnight(args: argparse.Namespace) -> int:
    """Tend every repo in the garden, one after another, within an overall
    time budget — the "tend to my garden while I sleep" entry point. See
    overnight.py's module docstring for the budget/rotation/resume design;
    this function is just the real-time, real-dispatch composition of it.

    Dispatches `tend --repo <repo> --allow-merge` **in-process** by calling
    `_dispatch_tend` directly with a synthetic argparse.Namespace, rather
    than shelling out to `gardener` as a subprocess of itself — reuses
    `_dispatch_tend`'s entire existing implementation (clone/refresh, skill
    bootstrap, the merge-eligibility gate, state.record_run, and
    `_notify_run`) unchanged for each repo. `--allow-merge` is passed
    unconditionally and is safe to: merge_eligible() (unchanged) still
    requires the repo to also be on the separate merge allow-list before
    `gh pr merge` is ever reachable in the dispatched session (see
    garden.py's module docstring) — being in the garden alone never
    authorizes a merge.

    Dispatches in batches of `args.concurrency` repos (default
    `overnight.DEFAULT_OVERNIGHT_CONCURRENCY`; pass 1 for strictly
    sequential dispatch) via `_dispatch_one_for_overnight`,
    run concurrently within a batch on a `ThreadPoolExecutor` when
    `concurrency > 1` — see that function's docstring and `overnight.py`'s
    `batch_repos` for why this is safe now that `_dispatch_tend` returns a
    `TendResult` instead of `cmd_overnight` needing to capture stdout.

    `args.strategy` (default `overnight.DEFAULT_OVERNIGHT_STRATEGY`, see
    `overnight.Strategy` and its
    module docstring's "Repo-selection strategies and the resume cursor"
    section) picks this run's ordering function and, correspondingly, which
    half of the resume-cursor file it reads/writes: `round-robin` uses
    `read_cursor`/`write_cursor` (a bare index, unchanged from before this
    flag existed); `issue-count`/`random` use `read_attempted`/
    `write_attempted` (repo names) plus `resume_order`/`next_attempted`
    instead, since neither of those two strategies' orderings are stable
    across runs — a bare index would silently resume at the wrong repo.
    """
    try:
        garden_list = garden.list_garden(path=args.garden_file)
    except ValueError as e:
        # A corrupted garden.json (e.g. a torn write from this device
        # killing a prior process mid-write — see garden.py's _save) must
        # not crash the whole unattended batch with a raw traceback; every
        # other cmd_overnight failure path is caught and alerted (see the
        # batch summary notification below), this is the setup-failure
        # equivalent for the one thing that happens before any per-repo
        # dispatch begins.
        print(f"gardener: overnight: error: {e}", file=sys.stderr)
        try:
            notify.default_notifier().notify(
                "gardener overnight: FAILED — could not read garden", str(e), notify.Level.ERROR
            )
        except Exception as notify_err:  # noqa: BLE001 - the alert must never mask the original error
            print(f"gardener: overnight: notification failed (non-fatal): {notify_err}", file=sys.stderr)
        return 1
    if not garden_list:
        print(
            "gardener: garden is empty — nothing to tend overnight "
            "(see `gardener garden add --repo owner/name`)",
            file=sys.stderr,
        )
        return 0

    budget_seconds = args.hours * 3600
    cursor_path = args.cursor_file or overnight.default_cursor_path()
    strategy = overnight.Strategy(
        getattr(args, "strategy", None) or overnight.DEFAULT_OVERNIGHT_STRATEGY.value
    )

    start_index: Optional[int] = None
    attempted_before: list[str] = []
    cycle_reset = False
    if strategy is overnight.Strategy.ROUND_ROBIN:
        start_index = overnight.read_cursor(path=cursor_path) % len(garden_list)
        order = overnight.repos_to_attempt(garden_list, start_index)
        print(
            f"gardener: overnight starting — {len(garden_list)} repo(s) in garden, "
            f"strategy={strategy.value}, budget={args.hours}h, "
            f"resuming at index {start_index} ({order[0]})",
            file=sys.stderr,
        )
    else:
        if strategy is overnight.Strategy.ISSUE_COUNT:
            print(f"gardener: overnight: fetching open-issue counts for {len(garden_list)} repo(s)...",
                  file=sys.stderr)
            counts = fetch_issue_counts(garden_list)
            full_order = overnight.order_by_issue_count(garden_list, counts)
        else:
            seed = getattr(args, "random_seed", None)
            rng = random.Random(seed) if seed is not None else None
            full_order = overnight.random_order(garden_list, rng=rng)
        attempted_before = overnight.read_attempted(path=cursor_path)
        order, cycle_reset = overnight.resume_order(full_order, attempted_before)
        print(
            f"gardener: overnight starting — {len(garden_list)} repo(s) in garden, "
            f"strategy={strategy.value}, budget={args.hours}h, "
            + (
                "starting a fresh cycle (every repo already attempted last cycle)"
                if cycle_reset
                else f"{len(attempted_before)} repo(s) already attempted this cycle"
            ),
            file=sys.stderr,
        )

    # An absent attribute (a synthetic Namespace, not the real parser) falls
    # back to the default; an explicit 0/negative is still clamped to 1
    # rather than silently widening to the default.
    requested_concurrency = getattr(args, "concurrency", None)
    if requested_concurrency is None:
        requested_concurrency = overnight.DEFAULT_OVERNIGHT_CONCURRENCY
    concurrency = max(1, requested_concurrency)
    outcomes: list[overnight.RepoOutcome] = []
    start_time = time.monotonic()
    attempted = 0
    aborted_on_block = False

    def persist_cursor() -> None:
        """Write the resume cursor for everything attempted so far.

        Called after *every* batch, not once at the end of the run, because
        on this device a long `overnight` run is more likely to be killed
        mid-garden (Android kills every background process when UserLand is
        swiped away — see `~/.claude/CLAUDE.md`) than to reach the end of
        its loop. A cursor written only after the loop is a cursor that
        exists mainly for the runs that didn't need it: the 2026-07-25 run
        tended 6 repos, was killed, and the next run started the cycle over
        from zero because none of those 6 had ever been persisted (issue
        #42). Writing per batch is what makes docs/OVERNIGHT.md's "a run
        that gets interrupted partway through the garden doesn't lose
        progress on the repos it already finished" actually true.

        Both branches are computed from the accumulated `outcomes`/
        `attempted` rather than just this batch's, so this is idempotent —
        calling it again after the loop rewrites the same values. The
        blocked-abort rule is unchanged and simply applies as of whatever
        has been attempted at call time.
        """
        if strategy is overnight.Strategy.ROUND_ROBIN:
            # On a blocked abort, advance only as far as the LAST repo that
            # got a real attempt before the first blocked one — a bare index
            # can't express "skip the middle one", so anything at or after
            # the first blocked repo is left to be re-attempted next run.
            # With concurrency > 1 that can mean re-tending a repo later in
            # the same batch that did succeed; re-tending is idempotent
            # enough (it's just another cycle) and far cheaper than silently
            # skipping a repo.
            advanced = _first_blocked_index(outcomes) if aborted_on_block else attempted
            overnight.write_cursor((start_index + advanced) % len(garden_list), path=cursor_path)
        else:
            # Here the cursor is a set of repo names rather than a position,
            # so it can be precise: drop exactly the blocked repos and keep
            # every genuinely-attempted one, whatever order they ran in.
            newly_attempted = [outcome.repo for outcome in outcomes if not outcome.blocked]
            overnight.write_attempted(
                overnight.next_attempted(attempted_before, cycle_reset, newly_attempted),
                path=cursor_path,
            )

    for repo_batch in overnight.batch_repos(order, concurrency):
        elapsed = time.monotonic() - start_time
        # Checked once per batch, not once per repo: a batch's own
        # wall-clock time is bounded by one repo's TEND_DEFAULT_TIMEOUT_SECONDS
        # (everything inside it runs in parallel, not stacked), so the
        # existing "elapsed + one repo's timeout <= budget" headroom check
        # is still the right test here — see overnight.batch_repos.
        if not overnight.has_time_for_another_repo(
            elapsed, budget_seconds, TEND_DEFAULT_TIMEOUT_SECONDS, attempted
        ):
            print(
                f"gardener: overnight stopping — insufficient budget remaining for "
                f"another repo ({budget_seconds - elapsed:.0f}s left, "
                f"need {TEND_DEFAULT_TIMEOUT_SECONDS}s headroom)",
                file=sys.stderr,
            )
            break

        progress = (
            f"{attempted + 1}/{len(order)}" if len(repo_batch) == 1
            else f"{attempted + 1}-{attempted + len(repo_batch)}/{len(order)}"
        )
        print(
            f"gardener: overnight dispatching tend for {', '.join(repo_batch)} "
            f"({progress} candidates this run"
            + (f", concurrency={len(repo_batch)}" if len(repo_batch) > 1 else "")
            + ")...",
            file=sys.stderr,
        )
        if len(repo_batch) == 1:
            # No thread pool at all for the concurrency=1 path —
            # byte-for-byte the same call sequence overnight has always made.
            batch_outcomes = [_dispatch_one_for_overnight(repo_batch[0], args)]
        else:
            with ThreadPoolExecutor(max_workers=len(repo_batch)) as pool:
                futures = {
                    pool.submit(_dispatch_one_for_overnight, repo, args): repo
                    for repo in repo_batch
                }
                results_by_repo = {futures[f]: f.result() for f in as_completed(futures)}
            # Preserve repo_batch's order in `outcomes`, not completion order,
            # so the batch summary's per-repo list stays deterministic.
            batch_outcomes = [results_by_repo[repo] for repo in repo_batch]
        outcomes.extend(batch_outcomes)
        attempted += len(repo_batch)
        aborted_on_block = any(outcome.blocked for outcome in batch_outcomes)

        # Persisted here, before the abort branch below acts on it, because
        # `persist_cursor` reads `aborted_on_block` to decide how far the
        # cursor may advance — writing first and classifying after would
        # advance it straight past the repos that just failed to
        # authenticate, which is the exact 2026-07-24 failure #38 fixed.
        persist_cursor()

        # A blocked failure is global to this device, not specific to the
        # repo that hit it: every remaining repo in the garden is about to
        # fail the same way, in seconds, for free. Stop the run instead of
        # marching the whole garden through a condition none of them can
        # survive. Each of the three classes has done exactly that here: a
        # ~20-minute credential blip burned 12 of 15 repos on 2026-07-24; an
        # exhausted usage window burned 20 of 32 in four minutes on
        # 2026-07-25 and 15 more on 2026-07-20; an unreachable
        # api.github.com burned 17 across 2026-07-21 and 2026-07-25 — every
        # one with the cursor advanced past it, leaving the garden untended
        # until the next night.
        if aborted_on_block:
            reason, recovery = _blocking_reason(batch_outcomes)
            print(
                f"gardener: overnight aborting — {reason}. "
                "Not advancing the resume cursor past the affected repo(s), so they are "
                f"re-attempted rather than skipped. {recovery}",
                file=sys.stderr,
            )
            break

    # Rewrites what the last batch already persisted, except in the one case
    # the loop never reached a batch at all (an empty garden order, or no
    # budget headroom even for the first repo) — there the cursor still needs
    # its no-op refresh to preserve the pre-per-batch-persistence behavior.
    persist_cursor()

    elapsed_total = time.monotonic() - start_time
    skipped = len(order) - attempted
    summary = overnight.build_batch_summary(outcomes, elapsed_total, skipped)
    print(f"gardener: overnight done — {summary.message}", file=sys.stderr)
    if aborted_on_block:
        # Sent in addition to (not instead of) the batch summary below: the
        # summary reports what this run did, which on a blocked abort reads
        # as a pile of ordinary per-repo errors and buries the one thing an
        # operator actually has to act on.
        reason, recovery = _blocking_reason(outcomes)
        try:
            notify.default_notifier().notify(
                f"gardener overnight: ABORTED — {reason}",
                f"{skipped} repo(s) left untended and the resume cursor was held back so they "
                f"aren't skipped. {recovery}",
                notify.Level.ERROR,
            )
        except Exception as e:  # noqa: BLE001 - the alert must never fail the run it reports on
            print(f"gardener: overnight abort notification failed (non-fatal): {e}", file=sys.stderr)
    try:
        notify.default_notifier().notify(summary.title, summary.message, summary.level)
    except Exception as e:  # noqa: BLE001 - the batch summary alert must never fail the run it reports on
        print(f"gardener: overnight summary notification failed (non-fatal): {e}", file=sys.stderr)

    return 0


def cmd_status(args: argparse.Namespace) -> int:
    runs = state.list_runs(db_path=args.state_db, repo=args.repo, limit=args.limit)
    if not runs:
        print("no runs recorded yet")
        return 0
    header = f"{'timestamp':<20} {'repo':<32} {'mode':<11} {'outcome':<10} summary"
    print(header)
    print("-" * len(header))
    for r in runs:
        summary = (r.gap_summary or "").replace("\n", " ")
        if len(summary) > 60:
            summary = summary[:57] + "..."
        print(f"{r.timestamp:<20} {r.repo:<32} {r.mode:<11} {r.outcome:<10} {summary}")
    return 0


def cmd_tail_transcript(args: argparse.Namespace) -> int:
    """Pretty-print a Claude Code session transcript (`.jsonl`) — the same
    file `align`/`tend`/`overnight` now print the path to via `gardener:
    session transcript: ...` shortly after dispatch starts (see
    `dispatch.py`'s "Live transcript visibility" section and
    `transcript.py`). Standalone from the rest of gardener's dispatch flow
    deliberately: it only ever reads a file, so it's safe to point at a
    transcript from a still-in-progress dispatch (`-f`/`--follow`) or one
    that already finished."""
    return transcript.print_transcript(args.path, follow=args.follow)


def cmd_dashboard(args: argparse.Namespace) -> int:
    port = dashboard.find_free_port(preferred=args.port)
    if port != args.port:
        print(
            f"gardener: port {args.port} is already in use — serving on {port} instead",
            file=sys.stderr,
        )
    dashboard.run_server(port=port, state_dir=args.state_dir)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gardener",
        description="Dispatch Claude Code against a fleet of repos: audit one against your "
                    "engineering conventions, or make real progress via each repo's own dev-loop skill.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    align = sub.add_parser("align", help="Audit (and optionally fix) a target repo's alignment gaps")
    align.add_argument("--repo", required=True, type=repo_arg, help="owner/name of the target GitHub repo")
    group = align.add_mutually_exclusive_group()
    group.add_argument(
        "--implement", action="store_true",
        help="Authorize Claude to implement fixes in the target repo (branch, commit, PR)",
    )
    group.add_argument(
        "--file-issue", action="store_true",
        help="Authorize Claude to open one scoped GitHub issue summarizing the gaps",
    )
    align.add_argument("--model", default=None, help="Model override passed through to `claude`")
    align.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Seconds to wait for the dispatched claude run (default {DEFAULT_TIMEOUT_SECONDS})",
    )
    align.add_argument(
        "--conventions-repo", default=None, metavar="GIT_URL",
        help="Git URL of the conventions repo to audit against "
             f"(default: ${conventions.CONVENTIONS_URL_ENV}; required, no built-in default)",
    )
    align.add_argument(
        "--no-refresh-conventions", action="store_true",
        help="Reuse the cached conventions checkout as-is instead of fetching latest",
    )
    align.add_argument(
        "--no-refresh-target", action="store_true",
        help="Reuse a cached target-repo checkout as-is instead of fetching latest",
    )
    align.add_argument("--state-db", type=Path, default=None, help=argparse.SUPPRESS)
    # `log_name` marks a subcommand as one whose stderr narration is worth
    # persisting to a run log — see `main`. Only the dispatching commands
    # set it; `status`/`dashboard`/`allowlist`/`garden` print a result and
    # exit, and would just litter the logs dir with near-empty files.
    align.set_defaults(func=cmd_align, log_name="align")

    tend = sub.add_parser(
        "tend",
        help="Dispatch a target repo's own *-dev-loop skill (generating one first if needed)",
    )
    tend.add_argument("--repo", required=True, type=repo_arg, help="owner/name of the target GitHub repo")
    tend.add_argument(
        "--allow-merge", action="store_true",
        help="Permit `gh pr merge` this run — still requires --repo be in the merge allow-list "
             "(see `gardener allowlist add`); either condition alone is not enough",
    )
    tend.add_argument("--model", default=None, help="Model override passed through to `claude`")
    tend.add_argument(
        "--timeout", type=int, default=TEND_DEFAULT_TIMEOUT_SECONDS,
        help=f"Seconds to wait for the dispatched tend run (default {TEND_DEFAULT_TIMEOUT_SECONDS})",
    )
    tend.add_argument(
        "--no-refresh-target", action="store_true",
        help="Reuse a cached target-repo checkout as-is instead of fetching latest",
    )
    tend.add_argument("--state-db", type=Path, default=None, help=argparse.SUPPRESS)
    tend.set_defaults(func=cmd_tend, log_name="tend")

    allowlist = sub.add_parser("allowlist", help="Manage the tend --allow-merge repo allow-list")
    allowlist_sub = allowlist.add_subparsers(dest="allowlist_action", required=True)

    allowlist_list = allowlist_sub.add_parser("list", help="Print every allow-listed repo")
    allowlist_list.add_argument("--allowlist-path", dest="allowlist_path", type=Path, default=None, help=argparse.SUPPRESS)
    allowlist_list.set_defaults(func=cmd_allowlist)

    allowlist_add = allowlist_sub.add_parser("add", help="Add a repo to the allow-list")
    allowlist_add.add_argument("--repo", required=True, type=repo_arg, help="owner/name of the repo to allow")
    allowlist_add.add_argument("--allowlist-path", dest="allowlist_path", type=Path, default=None, help=argparse.SUPPRESS)
    allowlist_add.set_defaults(func=cmd_allowlist)

    allowlist_remove = allowlist_sub.add_parser("remove", help="Remove a repo from the allow-list")
    allowlist_remove.add_argument("--repo", required=True, help="owner/name of the repo to remove")
    allowlist_remove.add_argument("--allowlist-path", dest="allowlist_path", type=Path, default=None, help=argparse.SUPPRESS)
    allowlist_remove.set_defaults(func=cmd_allowlist)

    garden_parser = sub.add_parser(
        "garden", help="Manage the opt-in list of repos `gardener overnight` tends"
    )
    garden_sub = garden_parser.add_subparsers(dest="garden_action", required=True)

    garden_list = garden_sub.add_parser("list", help="Print every repo in the garden")
    garden_list.add_argument("--garden-file", dest="garden_file", type=Path, default=None, help=argparse.SUPPRESS)
    garden_list.set_defaults(func=cmd_garden)

    garden_add = garden_sub.add_parser("add", help="Add a repo to the garden")
    garden_add.add_argument("--repo", required=True, type=repo_arg, help="owner/name of the repo to tend overnight")
    garden_add.add_argument("--garden-file", dest="garden_file", type=Path, default=None, help=argparse.SUPPRESS)
    garden_add.set_defaults(func=cmd_garden)

    garden_remove = garden_sub.add_parser("remove", help="Remove a repo from the garden")
    garden_remove.add_argument("--repo", required=True, help="owner/name of the repo to remove")
    garden_remove.add_argument("--garden-file", dest="garden_file", type=Path, default=None, help=argparse.SUPPRESS)
    garden_remove.set_defaults(func=cmd_garden)

    overnight_parser = sub.add_parser(
        "overnight",
        help="Tend every repo in the garden, one after another, within a time budget",
    )
    overnight_parser.add_argument(
        "--hours", type=float, default=overnight.DEFAULT_OVERNIGHT_HOURS,
        help=f"Overall time budget in hours (default {overnight.DEFAULT_OVERNIGHT_HOURS})",
    )
    overnight_parser.add_argument("--model", default=None, help="Model override passed through to each dispatched tend run")
    overnight_parser.add_argument(
        "--concurrency", type=int, default=overnight.DEFAULT_OVERNIGHT_CONCURRENCY,
        help=f"How many repos to tend at once (default "
             f"{overnight.DEFAULT_OVERNIGHT_CONCURRENCY}). Each one dispatches a "
             "`claude -p` session simultaneously via separate OS "
             "processes/threads — mind this device's real CPU/RAM limits before "
             "raising it further for an unattended run; pass 1 for strictly "
             "sequential dispatch.",
    )
    overnight_parser.add_argument(
        "--strategy", choices=[s.value for s in overnight.Strategy],
        default=overnight.DEFAULT_OVERNIGHT_STRATEGY.value,
        help=f"Repo selection order for this run (default "
             f"{overnight.DEFAULT_OVERNIGHT_STRATEGY.value}). random reshuffles the "
             "garden fresh every run. round-robin rotates the alphabetically-sorted "
             "garden from where the last run left off. issue-count sorts descending "
             "by each repo's live open-GitHub-issue count (one `gh` call per garden "
             "repo). issue-count/random resume by repo name rather than list "
             "position, since their ordering isn't stable across runs — this keeps "
             "the same every-repo-per-cycle guarantee round-robin has; see "
             "docs/OVERNIGHT.md.",
    )
    overnight_parser.add_argument("--garden-file", dest="garden_file", type=Path, default=None, help=argparse.SUPPRESS)
    overnight_parser.add_argument("--cursor-file", dest="cursor_file", type=Path, default=None, help=argparse.SUPPRESS)
    overnight_parser.add_argument("--state-db", type=Path, default=None, help=argparse.SUPPRESS)
    overnight_parser.add_argument("--random-seed", type=int, default=None, help=argparse.SUPPRESS)
    overnight_parser.set_defaults(func=cmd_overnight, log_name="overnight")

    status = sub.add_parser("status", help="Show local run history")
    status.add_argument("--repo", default=None, help="Filter to one owner/name repo")
    status.add_argument(
        "--limit", type=int, default=20,
        help="How many most-recent runs to show (default 20)",
    )
    status.add_argument("--state-db", type=Path, default=None, help=argparse.SUPPRESS)
    status.set_defaults(func=cmd_status)

    tail_transcript = sub.add_parser(
        "tail-transcript",
        help="Pretty-print a Claude Code session transcript (tool calls, text, tool results)",
    )
    tail_transcript.add_argument(
        "path", type=Path,
        help="Path to the transcript .jsonl file (see the 'gardener: session transcript: "
             "...' line an align/tend/overnight dispatch prints to stderr)",
    )
    tail_transcript.add_argument(
        "-f", "--follow", action="store_true",
        help="Keep reading as the file grows, like `tail -f`, instead of exiting at EOF",
    )
    tail_transcript.set_defaults(func=cmd_tail_transcript)

    dashboard_parser = sub.add_parser(
        "dashboard",
        help="Serve a local read-only web UI over run history + the active tend/overnight log",
    )
    dashboard_parser.add_argument(
        "--port", type=int, default=dashboard.DEFAULT_PORT,
        help=f"Port to bind on 127.0.0.1 (default {dashboard.DEFAULT_PORT}; picks a free "
             "port instead if this one is already in use)",
    )
    dashboard_parser.add_argument("--state-dir", type=Path, default=None, help=argparse.SUPPRESS)
    dashboard_parser.set_defaults(func=cmd_dashboard)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log_name = getattr(args, "log_name", None)
    if log_name is None:
        return args.func(args)
    # Wrapping here rather than inside each command function means every
    # line a dispatching run prints is captured, including the ones printed
    # before/after the command's own body (argparse errors have already
    # exited by this point, so nothing is lost by starting the log here).
    with run_log.tee_stderr(log_name) as path:
        if path is not None:
            print(f"gardener: run log: {path}", file=sys.stderr)
        return args.func(args)
