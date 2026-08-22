"""Pre-flight health check over gardener's *own* local state — the cache
clones, the two opt-in lists, the state directory, and the external CLIs
every dispatch shells out to.

Why this exists, concretely: an unattended `gardener overnight` run is the
worst possible place to discover that a repo has been failing for days.
Every check below was written from a real failure that actually consumed a
garden slot on a real overnight run (2026-08-19's log sweep), not from
imagining what might go wrong:

- **A dirty cache clone fails `tend` every single run, silently and
  forever.** `cli.py`'s `clone_or_refresh_target_repo` refreshes with
  `git checkout -B <default> origin/<default>`, which git refuses when
  local changes would be overwritten. Nothing ever cleans that up, so a
  clone left dirty by a killed dev-loop run (this device kills background
  processes without warning — see `docs/OVERNIGHT.md`) burns that repo's
  slot on every subsequent night. Three repos were in exactly this state
  for a week, erroring 2-3 times each.
- **A renamed target repo is worse, because the first run after a cache
  clear "fixes" it.** `gh repo clone <old-name>` follows GitHub's rename
  redirect and writes the *canonical* URL as `origin`, so a fresh clone
  succeeds — and then every subsequent refresh trips
  `clone_or_refresh_target_repo`'s origin check (which compares the
  configured name against the origin URL as a substring) and fails. The
  operator clears the cache, gets one good run, and the failure comes
  back. The real fix is renaming the garden/allow-list entry, which
  `doctor` can only surface if it asks GitHub for each entry's canonical
  `nameWithOwner`.
- **A malformed opt-in list silently means "empty".** `garden.py` and
  `merge_allowlist.py` both treat a missing file as an empty list — the
  correct safe default — but that also means a state directory that has
  gone unwritable reads as "nothing to do" rather than as a problem.

Everything here is read-only and reports; nothing is repaired. That's
deliberate, and it is the same posture as the rest of gardener: the two
remediations these findings call for are `git stash`-vs-discard on a clone
holding possibly-unpushed work, and rewriting an opt-in *safety* list
(the merge allow-list included). Neither is a decision to make on the
operator's behalf from inside a health check, so every `Finding` carries
the exact command to run instead.

Checks needing the network go through `gh` and degrade to
`Severity.SKIPPED` rather than failing the report, so `doctor` stays
useful on a phone with no signal — the device this runs on most. Pass
`offline=True` to skip them outright.

Every subprocess call goes through the injectable `run_fn` (same pattern
as `selfupdate.py`'s), so all of this is unit-testable with no real git
checkout, no network, and no `gh` — see `tests/test_doctor.py`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from gardener import repo_lock

RunFn = Callable[[list[str], Optional[Path], int], subprocess.CompletedProcess]

#: Short timeout for the local `git` calls — these are all metadata reads
#: against an existing clone, not fetches.
GIT_TIMEOUT_SECONDS = 30

#: `gh repo view` reaches the network; give it the same headroom
#: `cli.py`'s own `gh` helpers use.
GH_TIMEOUT_SECONDS = 30

#: External CLIs gardener shells out to, and what breaks without each.
REQUIRED_CLIS = {
    "git": "cloning and refreshing every target repo",
    "gh": "resolving default branches, cloning private repos, and merging",
    "claude": "every dispatch — align, tend, and overnight",
}


class Severity(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"
    #: A check that could not run at all (no network, `gh` missing). Never
    #: an assertion that the thing being checked is healthy.
    SKIPPED = "skipped"


@dataclass
class Finding:
    check: str
    severity: Severity
    message: str
    repo: Optional[str] = None
    #: Exact command the operator can run to resolve this. `doctor` never
    #: runs it — see the module docstring for why.
    fix: Optional[str] = None


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in (Severity.WARN, Severity.ERROR)]

    @property
    def worst(self) -> Severity:
        for severity in (Severity.ERROR, Severity.WARN, Severity.SKIPPED):
            if any(f.severity is severity for f in self.findings):
                return severity
        return Severity.OK


def _default_run(argv: list[str], cwd: Optional[Path], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout
    )


def _git(run_fn: RunFn, repo_dir: Path, *args: str) -> tuple[int, str]:
    """`(returncode, stripped stdout)` for a git call in `repo_dir`. A
    timeout or an OS-level failure is reported as a non-zero return with an
    empty stdout rather than raised — a single unreadable clone must never
    abort the whole report."""
    try:
        res = run_fn(["git", *args], repo_dir, GIT_TIMEOUT_SECONDS)
    except (subprocess.SubprocessError, OSError):
        return 1, ""
    return res.returncode, (res.stdout or "").strip()


def dir_to_repo_name(dirname: str) -> str:
    """`Owner__repo-name` -> `Owner/repo-name`, the inverse of the
    `repo.replace("/", "__")` scheme `clone_or_refresh_target_repo` uses to
    name a cache directory."""
    return dirname.replace("__", "/", 1)


def canonical_repo_name(
    repo: str, run_fn: RunFn = _default_run, timeout: int = GH_TIMEOUT_SECONDS
) -> Optional[str]:
    """The repo's current `owner/name` per GitHub, following any rename or
    transfer. None when `gh` can't answer (offline, not authenticated, repo
    gone) — indistinguishable outcomes from here, and none of them are
    something `doctor` should report as a rename."""
    try:
        res = run_fn(["gh", "repo", "view", repo, "--json", "nameWithOwner"], None, timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout or "")
    except json.JSONDecodeError:
        return None
    name = data.get("nameWithOwner") if isinstance(data, dict) else None
    return name if isinstance(name, str) and name else None


def check_required_clis(which_fn: Callable[[str], Optional[str]] = shutil.which) -> list[Finding]:
    findings = []
    for name, why in REQUIRED_CLIS.items():
        if which_fn(name):
            findings.append(Finding("cli", Severity.OK, f"{name} found on PATH"))
        else:
            findings.append(
                Finding(
                    "cli",
                    Severity.ERROR,
                    f"{name} not found on PATH — needed for {why}",
                    fix=f"install {name} and make sure it's on PATH",
                )
            )
    return findings


def check_gh_auth(run_fn: RunFn = _default_run, offline: bool = False) -> list[Finding]:
    if offline:
        return [Finding("gh-auth", Severity.SKIPPED, "skipped (--offline)")]
    try:
        res = run_fn(["gh", "auth", "status"], None, GH_TIMEOUT_SECONDS)
    except (subprocess.SubprocessError, OSError) as e:
        return [Finding("gh-auth", Severity.SKIPPED, f"could not run `gh auth status`: {e}")]
    if res.returncode != 0:
        return [
            Finding(
                "gh-auth",
                Severity.ERROR,
                "gh is not authenticated — private clones and every merge will fail",
                fix="gh auth login",
            )
        ]
    return [Finding("gh-auth", Severity.OK, "gh is authenticated")]


def check_state_dir(state_dir: Path) -> list[Finding]:
    """The state directory holds the garden, the merge allow-list, the
    resume cursor, and the run db. A missing directory is fine (first run);
    an unwritable one is not, and neither is a list file that no longer
    parses — both read downstream as "empty", which is silently wrong."""
    findings = []
    if not state_dir.exists():
        return [
            Finding(
                "state-dir",
                Severity.OK,
                f"{state_dir} does not exist yet (created on first write)",
            )
        ]
    if not os.access(state_dir, os.W_OK | os.X_OK):
        findings.append(
            Finding(
                "state-dir",
                Severity.ERROR,
                f"{state_dir} is not writable — run history and cursor updates will be lost",
                fix=f"chmod u+wx {state_dir}",
            )
        )
    else:
        findings.append(Finding("state-dir", Severity.OK, f"{state_dir} is writable"))

    for filename in ("garden.json", "merge_allowlist.json", "overnight_cursor.json"):
        path = state_dir / filename
        if not path.exists():
            continue
        try:
            json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            findings.append(
                Finding(
                    "state-dir",
                    Severity.ERROR,
                    f"{path} is unreadable or not valid JSON ({e}) — it will be treated as empty",
                    fix=f"inspect and repair (or delete) {path}",
                )
            )
        else:
            findings.append(Finding("state-dir", Severity.OK, f"{filename} parses"))
    return findings


def check_cache_clone(
    repo_dir: Path,
    run_fn: RunFn = _default_run,
    locked_fn: Callable[[str], bool] = repo_lock.is_repo_locked,
) -> list[Finding]:
    """One cache clone's refresh-readiness. Both failure modes here are the
    ones `clone_or_refresh_target_repo` actually trips on: an origin that
    no longer matches the configured repo name, and a working tree with
    local modifications that `git checkout -B` would have to overwrite.

    Untracked files are reported separately and only as a WARN: the refresh
    runs `git clean -fdx` and is *expected* to remove them, so they only
    matter when they'd collide with a file on the incoming branch — which
    is a real observed failure, but a much rarer one than a modified
    tracked file.

    A repo another gardener process currently holds the lock on is reported
    as SKIPPED and nothing else: a tend in flight has *legitimately* dirtied
    its clone and will clean up after itself, so flagging that as a problem
    would make `doctor` unusable during exactly the unattended overnight run
    it exists to protect."""
    repo = dir_to_repo_name(repo_dir.name)
    if locked_fn(repo):
        return [
            Finding(
                "cache-clone",
                Severity.SKIPPED,
                "currently locked by a running gardener process — skipped rather than "
                "reporting an in-flight dispatch's own working tree as dirty",
                repo=repo,
            )
        ]
    findings = []

    code, origin = _git(run_fn, repo_dir, "remote", "get-url", "origin")
    if code != 0:
        return [
            Finding(
                "cache-clone",
                Severity.ERROR,
                f"{repo_dir} has no readable git origin — not a usable clone",
                repo=repo,
                fix=f"rm -rf {repo_dir}",
            )
        ]
    if repo not in origin:
        findings.append(
            Finding(
                "cache-clone",
                Severity.ERROR,
                f"origin is {origin}, which does not contain {repo} — every refresh will "
                f"refuse to reuse this clone",
                repo=repo,
                fix=f"rm -rf {repo_dir}  # and see the repo-renamed finding, if any",
            )
        )

    code, porcelain = _git(run_fn, repo_dir, "status", "--porcelain")
    if code != 0:
        findings.append(
            Finding(
                "cache-clone",
                Severity.ERROR,
                f"could not read git status in {repo_dir}",
                repo=repo,
                fix=f"rm -rf {repo_dir}",
            )
        )
        return findings

    lines = [line for line in porcelain.splitlines() if line.strip()]
    modified = [line for line in lines if not line.startswith("??")]
    untracked = [line for line in lines if line.startswith("??")]

    if modified:
        _, branch = _git(run_fn, repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
        where = f" on branch {branch}" if branch else ""
        findings.append(
            Finding(
                "cache-clone",
                Severity.ERROR,
                f"{len(modified)} modified file(s){where} — `git checkout -B` will refuse to "
                f"overwrite them and every tend of this repo will fail until it's clean",
                repo=repo,
                fix=f"git -C {repo_dir} stash push -u  # preserves the work; `git stash pop` restores it",
            )
        )
    if untracked:
        findings.append(
            Finding(
                "cache-clone",
                Severity.WARN,
                f"{len(untracked)} untracked path(s) — normally harmless (the refresh runs "
                f"`git clean -fdx`), but a collision with an incoming file fails the checkout",
                repo=repo,
                fix=f"git -C {repo_dir} clean -nd  # review, then drop -n to remove",
            )
        )
    if not findings:
        findings.append(Finding("cache-clone", Severity.OK, "clean and refreshable", repo=repo))
    return findings


def check_cache_clones(
    cache_dir: Path,
    run_fn: RunFn = _default_run,
    locked_fn: Callable[[str], bool] = repo_lock.is_repo_locked,
) -> list[Finding]:
    if not cache_dir.exists():
        return [Finding("cache-clone", Severity.OK, f"{cache_dir} does not exist yet")]
    findings = []
    for entry in sorted(cache_dir.iterdir()):
        if not (entry / ".git").is_dir():
            continue
        findings.extend(check_cache_clone(entry, run_fn=run_fn, locked_fn=locked_fn))
    if not findings:
        return [Finding("cache-clone", Severity.OK, f"no clones cached under {cache_dir}")]
    return findings


def check_repo_names(
    repos: list[str],
    source: str,
    run_fn: RunFn = _default_run,
    offline: bool = False,
) -> list[Finding]:
    """Every configured repo name still resolves to itself on GitHub.

    A name that resolves to a *different* `owner/name` has been renamed or
    transferred, which is the failure this whole module exists to catch
    early: it doesn't break anything until the cache clone is next
    refreshed, and clearing the cache appears to fix it for exactly one
    run. A name that doesn't resolve at all is reported as SKIPPED, not as
    an error — offline, rate-limited, and deleted are the same answer from
    here."""
    if offline:
        return [Finding("repo-name", Severity.SKIPPED, f"{source}: skipped (--offline)")]
    findings = []
    unresolved = []
    for repo in repos:
        canonical = canonical_repo_name(repo, run_fn=run_fn)
        if canonical is None:
            unresolved.append(repo)
        elif canonical != repo:
            findings.append(
                Finding(
                    "repo-name",
                    Severity.ERROR,
                    f"{source} entry {repo} is now {canonical} on GitHub — clones will keep "
                    f"failing the origin check after the first refresh",
                    repo=repo,
                    fix=f"gardener {source} remove --repo {repo} && "
                    f"gardener {source} add --repo {canonical}",
                )
            )
    if unresolved:
        findings.append(
            Finding(
                "repo-name",
                Severity.SKIPPED,
                f"{source}: could not resolve {len(unresolved)} repo(s) via gh "
                f"({', '.join(unresolved[:3])}{'...' if len(unresolved) > 3 else ''}) — "
                f"offline, rate-limited, or no longer accessible",
            )
        )
    if not findings:
        findings.append(Finding("repo-name", Severity.OK, f"{source}: every entry resolves to itself"))
    return findings


def run_checks(
    cache_dir: Path,
    state_dir: Path,
    garden_repos: list[str],
    allowlist_repos: list[str],
    run_fn: RunFn = _default_run,
    which_fn: Callable[[str], Optional[str]] = shutil.which,
    locked_fn: Callable[[str], bool] = repo_lock.is_repo_locked,
    offline: bool = False,
) -> Report:
    """Every check, in the order an operator would want to read them:
    can gardener run at all, is its own state sound, then the per-repo
    findings. Purely a composition of the check functions above — all the
    inputs are passed in so this whole module stays testable without
    touching a real state directory."""
    findings = []
    findings.extend(check_required_clis(which_fn=which_fn))
    findings.extend(check_gh_auth(run_fn=run_fn, offline=offline))
    findings.extend(check_state_dir(state_dir))
    findings.extend(check_cache_clones(cache_dir, run_fn=run_fn, locked_fn=locked_fn))
    findings.extend(check_repo_names(garden_repos, "garden", run_fn=run_fn, offline=offline))
    findings.extend(
        check_repo_names(allowlist_repos, "allowlist", run_fn=run_fn, offline=offline)
    )
    return Report(findings)
