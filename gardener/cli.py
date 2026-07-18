"""argparse-based CLI: `gardener align`, `gardener tend`,
`gardener allowlist`, and `gardener status`.

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
import re
import shutil
import subprocess
import sys
from pathlib import Path
from string import Template
from typing import Optional

from gardener import conventions, dev_loop, merge_allowlist, notify, state
from gardener.dispatch import (
    CREATE_DEV_LOOP_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    TEND_DEFAULT_TIMEOUT_SECONDS,
    DispatchError,
    Mode,
    run_claude,
    tend_mode_spec,
)

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "align_repo.md.tmpl"

MODE_INSTRUCTIONS = {
    Mode.REPORT: """4. Report the gap checklist only. You do NOT have file-write or shell
   tools available in this session — that is enforced at the tool level,
   not just by these instructions — so there is nothing further to
   authorize or refuse. Do not attempt to open a PR or an issue.""",
    Mode.IMPLEMENT: """4. You are authorized to implement the identified gaps directly in this
   checked-out repository, following ITS OWN conventions (language, build
   tool, test framework) rather than copying dms-conventions' literal
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
     the gaps closed, referencing dms-conventions doc(s) by URL instead of
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
        for cmd in (
            ["git", "fetch", "--depth", "1", "origin", default_branch],
            # -B (create-or-reset) rather than plain checkout: correctly
            # lands on a clean copy of the default branch regardless of
            # what was checked out before (a different branch, a detached
            # HEAD, or the default branch itself but stale/dirty).
            ["git", "checkout", "-B", default_branch, f"origin/{default_branch}"],
            ["git", "clean", "-fdx"],
        ):
            res = _run(cmd, cwd=dest, timeout=60)
            if res.returncode != 0:
                raise RuntimeError(f"refresh of {dest} failed at `{' '.join(cmd)}`: {res.stderr.strip()}")
    return dest


def current_branch(repo_dir: Path) -> str:
    res = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, timeout=15)
    return res.stdout.strip() or "main"


def build_prompt(mode: Mode, repo: str, target_cwd: Path, conventions_dir: Path, default_branch: str) -> str:
    template = Template(PROMPT_TEMPLATE_PATH.read_text())
    return template.substitute(
        repo=repo,
        target_cwd=str(target_cwd),
        conventions_dir=str(conventions_dir),
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


def cmd_align(args: argparse.Namespace) -> int:
    if args.implement and args.file_issue:
        print("error: --implement and --file-issue are mutually exclusive", file=sys.stderr)
        return 2

    mode = Mode.IMPLEMENT if args.implement else Mode.FILE_ISSUE if args.file_issue else Mode.REPORT

    print(f"gardener: aligning {args.repo} against dms-conventions (mode={mode.value})", file=sys.stderr)
    if mode is Mode.REPORT:
        print("gardener: report-only — Claude has no write/shell tools in this run", file=sys.stderr)

    try:
        conv = conventions.ensure_conventions(refresh=not args.no_refresh_conventions)
        print(f"gardener: dms-conventions checked out at {conv.path}", file=sys.stderr)

        target_dir = clone_or_refresh_target_repo(
            args.repo, default_repos_cache_dir(), refresh=not args.no_refresh_target
        )
        print(f"gardener: {args.repo} checked out at {target_dir}", file=sys.stderr)

        branch = current_branch(target_dir)
        prompt = build_prompt(mode, args.repo, target_dir, conv.path, branch)

        print("gardener: dispatching claude (this can take a while)...", file=sys.stderr)
        result = run_claude(
            mode=mode,
            prompt=prompt,
            cwd=target_dir,
            add_dirs=[conv.path],
            model=args.model,
            timeout=args.timeout,
        )
    except (DispatchError, RuntimeError, ValueError) as e:
        print(f"gardener: error: {e}", file=sys.stderr)
        failed_run = state.Run(
            repo=args.repo,
            mode=mode.value,
            outcome="error",
            timestamp=state.now_iso(),
            gap_summary=str(e),
        )
        state.record_run(failed_run, db_path=args.state_db)
        _notify_run(failed_run)
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
    state.record_run(completed_run, db_path=args.state_db)
    _notify_run(completed_run)

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


def cmd_tend(args: argparse.Namespace) -> int:
    print(f"gardener: tending {args.repo} (allow_merge={args.allow_merge})", file=sys.stderr)

    try:
        target_dir = clone_or_refresh_target_repo(
            args.repo, default_repos_cache_dir(), refresh=not args.no_refresh_target
        )
        print(f"gardener: {args.repo} checked out at {target_dir}", file=sys.stderr)
        branch = current_branch(target_dir)

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
                model=args.model,
                timeout=CREATE_DEV_LOOP_TIMEOUT_SECONDS,
            )
            state.record_run(
                state.Run(
                    repo=args.repo,
                    mode=Mode.CREATE_DEV_LOOP.value,
                    outcome="error" if not create_result.ok else "created",
                    timestamp=state.now_iso(),
                    gap_summary=extract_gap_summary(create_result.result_text)
                    if create_result.result_text
                    else (create_result.stderr or "no output"),
                    exit_code=create_result.exit_code,
                    duration_ms=create_result.duration_ms,
                    cost_usd=create_result.cost_usd,
                    claude_session_id=create_result.session_id,
                ),
                db_path=args.state_db,
            )
            if not create_result.ok or not dev_loop.has_dev_loop_skill(slug):
                print(
                    f"gardener: error: create-dev-loop dispatch did not produce a usable "
                    f"/{slug} skill (ok={create_result.ok}, "
                    f"exists={dev_loop.has_dev_loop_skill(slug)}) — not proceeding to tend",
                    file=sys.stderr,
                )
                return 1
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

        tend_prompt = dev_loop.build_tend_prompt(args.repo, slug, target_dir, branch, eligible)
        result = run_claude(
            mode=Mode.TEND,
            prompt=tend_prompt,
            cwd=target_dir,
            model=args.model,
            timeout=args.timeout,
            mode_spec=tend_mode_spec(eligible),
        )
    except (DispatchError, RuntimeError, ValueError) as e:
        print(f"gardener: error: {e}", file=sys.stderr)
        state.record_run(
            state.Run(
                repo=args.repo,
                mode=Mode.TEND.value,
                outcome="error",
                timestamp=state.now_iso(),
                gap_summary=str(e),
            ),
            db_path=args.state_db,
        )
        return 1

    outcome = "error" if not result.ok else Mode.TEND.value
    gap_summary = extract_gap_summary(result.result_text) if result.result_text else (result.stderr or "no output")

    state.record_run(
        state.Run(
            repo=args.repo,
            mode=Mode.TEND.value,
            outcome=outcome,
            timestamp=state.now_iso(),
            gap_summary=gap_summary,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            cost_usd=result.cost_usd,
            claude_session_id=result.session_id,
        ),
        db_path=args.state_db,
    )

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
            "gardener: NOTE — the dispatched run attempted action(s) outside this mode's "
            "pre-approved scope and they were blocked (see denials above)",
            file=sys.stderr,
        )
    if result.timed_out:
        print(f"gardener: timed out after {args.timeout}s", file=sys.stderr)
        return 1
    return 0 if result.ok else 1


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gardener",
        description="Align a target repo against dms-conventions via a dispatched Claude Code run.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    align = sub.add_parser("align", help="Audit (and optionally fix) a target repo's alignment gaps")
    align.add_argument("--repo", required=True, help="owner/name of the target GitHub repo")
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
        "--no-refresh-conventions", action="store_true",
        help="Reuse the cached dms-conventions checkout as-is instead of fetching latest",
    )
    align.add_argument(
        "--no-refresh-target", action="store_true",
        help="Reuse a cached target-repo checkout as-is instead of fetching latest",
    )
    align.add_argument("--state-db", type=Path, default=None, help=argparse.SUPPRESS)
    align.set_defaults(func=cmd_align)

    tend = sub.add_parser(
        "tend",
        help="Dispatch a target repo's own *-dev-loop skill (generating one first if needed)",
    )
    tend.add_argument("--repo", required=True, help="owner/name of the target GitHub repo")
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
    tend.set_defaults(func=cmd_tend)

    allowlist = sub.add_parser("allowlist", help="Manage the tend --allow-merge repo allow-list")
    allowlist_sub = allowlist.add_subparsers(dest="allowlist_action", required=True)

    allowlist_list = allowlist_sub.add_parser("list", help="Print every allow-listed repo")
    allowlist_list.add_argument("--allowlist-path", dest="allowlist_path", type=Path, default=None, help=argparse.SUPPRESS)
    allowlist_list.set_defaults(func=cmd_allowlist)

    allowlist_add = allowlist_sub.add_parser("add", help="Add a repo to the allow-list")
    allowlist_add.add_argument("--repo", required=True, help="owner/name of the repo to allow")
    allowlist_add.add_argument("--allowlist-path", dest="allowlist_path", type=Path, default=None, help=argparse.SUPPRESS)
    allowlist_add.set_defaults(func=cmd_allowlist)

    allowlist_remove = allowlist_sub.add_parser("remove", help="Remove a repo from the allow-list")
    allowlist_remove.add_argument("--repo", required=True, help="owner/name of the repo to remove")
    allowlist_remove.add_argument("--allowlist-path", dest="allowlist_path", type=Path, default=None, help=argparse.SUPPRESS)
    allowlist_remove.set_defaults(func=cmd_allowlist)

    status = sub.add_parser("status", help="Show local run history")
    status.add_argument("--repo", default=None, help="Filter to one owner/name repo")
    status.add_argument("--limit", type=int, default=20)
    status.add_argument("--state-db", type=Path, default=None, help=argparse.SUPPRESS)
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
