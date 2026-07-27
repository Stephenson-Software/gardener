"""Clones/refreshes a local cache of a *conventions repo* and locates the
docs gardener's prompt template needs.

A conventions repo is the actual source of truth for what "aligned" means.
gardener never invents or duplicates convention content — it only points a
dispatched Claude Code run at these files. Which repo that is, is entirely
the operator's choice: there is deliberately **no default**, because a
baked-in default would mean `align` silently audits every target repo
against somebody else's opinions. Configure it with the
`GARDENER_CONVENTIONS_URL` environment variable or `align`'s
`--conventions-repo` flag; with neither set, `align` fails fast with the
setup instructions rather than guessing (see `resolve_url`).

Any git repo satisfying the `REQUIRED_DOCS` layout below works —
`verify_complete` enforces exactly that contract and nothing else, so the
content of those docs is unconstrained. See the "Conventions repo" section
of README.md for the authoring guide.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

CONVENTIONS_URL_ENV = "GARDENER_CONVENTIONS_URL"

# The layout contract a conventions repo must satisfy. Every entry except
# `README.md` is also on the reading list `prompts/align_repo.md.tmpl`
# points a dispatched run at, so those two must stay in sync — a doc in one
# and not the other means either a run reads a file gardener never verified
# exists, or gardener demands a file no run ever reads. `README.md` is the
# deliberate exception: it's required as a cheap sanity check that the
# clone is actually a conventions repo (rather than, say, a mistyped URL
# that resolved to something else), not because a run reads it.
#
# Read-only: gardener never commits into its own cache of a conventions
# repo, it only clones/pulls it.
REQUIRED_DOCS = [
    "README.md",
    "ALIGNMENT_PROMPT.md",
    "ALIGNMENT_CHECKLIST.md",
    "docs/CLAUDE_MD_STRUCTURE.md",
    "docs/README_STRUCTURE.md",
    "docs/CONTRIBUTING_STANDARDS.md",
    "docs/ISSUE_TEMPLATES.md",
    "docs/CODEOWNERS.md",
    "docs/CI_STRUCTURE.md",
    "docs/COMMIT_PR_CONVENTIONS.md",
    "docs/REVIEW_PROMPTING.md",
    "docs/DEV_LOOP_PATTERNS.md",
]


class ConventionsError(RuntimeError):
    pass


# Raised when no conventions repo is configured at all. Deliberately
# diagnoses *and* tells you how to recover in one message: an operator
# hitting this has usually just installed gardener and has no idea a
# conventions repo is a thing yet, so naming the two ways to set it and
# the layout it has to have is the difference between a dead end and a
# next step.
_NO_URL_MESSAGE = (
    "No conventions repo is configured, so `gardener align` has nothing to\n"
    "audit against. `align` compares a target repo to a git repo of your own\n"
    "engineering conventions; gardener deliberately ships no default, since\n"
    "auditing your repos against somebody else's conventions is worse than\n"
    "not running.\n"
    "\n"
    "Point it at one, either way:\n"
    "  export GARDENER_CONVENTIONS_URL=https://github.com/you/your-conventions.git\n"
    "  gardener align --repo <owner/repo> --conventions-repo <git-url>\n"
    "\n"
    "That repo must contain these files (contents are entirely up to you):\n"
    + "".join(f"  {doc}\n" for doc in REQUIRED_DOCS)
    + "\n"
    "ALIGNMENT_PROMPT.md states what to audit for; ALIGNMENT_CHECKLIST.md\n"
    "gives the checklist shape a run reports back in. See the \"Conventions\n"
    "repo\" section of gardener's README for the authoring guide.\n"
    "\n"
    "Only `align` needs this — `tend`, `garden`, and `overnight` dispatch a\n"
    "repo's own dev-loop skill and work without a conventions repo."
)


def resolve_url(explicit: str | None = None) -> str:
    """The configured conventions repo URL: an explicit `--conventions-repo`
    value wins, then `$GARDENER_CONVENTIONS_URL`. Raises rather than falling
    back to a default — see this module's docstring for why there isn't one."""
    url = explicit or os.environ.get(CONVENTIONS_URL_ENV, "").strip()
    if not url:
        raise ConventionsError(_NO_URL_MESSAGE)
    return url


def default_cache_dir() -> Path:
    override = os.environ.get("GARDENER_CACHE_DIR")
    if override:
        return Path(override) / "conventions"
    return Path.home() / ".cache" / "gardener" / "conventions"


@dataclass
class ConventionsSource:
    path: Path
    url: str | None = None

    def checklist_path(self) -> Path:
        return self.path / "ALIGNMENT_CHECKLIST.md"

    def alignment_prompt_path(self) -> Path:
        return self.path / "ALIGNMENT_PROMPT.md"

    def verify_complete(self) -> None:
        missing = [d for d in REQUIRED_DOCS if not (self.path / d).is_file()]
        if missing:
            source = self.url or "the configured conventions repo"
            raise ConventionsError(
                f"Conventions checkout at {self.path} is missing required "
                f"doc(s): {', '.join(missing)}. gardener's alignment prompt "
                "reads every file in that list, so a run would audit against "
                "an incomplete rubric.\n"
                f"Add the missing file(s) to {source}, or re-run without "
                "--no-refresh-conventions to pull a version that has them."
            )


def _run_git(args: list[str], cwd: Path | None = None, timeout: int = 60) -> None:
    try:
        subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as e:
        raise ConventionsError(
            f"git {' '.join(args)} failed: {e.stderr.strip() or e.stdout.strip()}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise ConventionsError(f"git {' '.join(args)} timed out after {timeout}s") from e


def _origin_url(cache_dir: Path) -> str | None:
    """The existing cache's `origin` URL, or None if it can't be read. None
    (rather than a raise) so an unreadable/half-written cache is treated as
    "doesn't match" and gets repointed + refreshed, not as a hard failure."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(cache_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def ensure_conventions(
    cache_dir: Path | None = None, refresh: bool = True, url: str | None = None
) -> ConventionsSource:
    """Clone the configured conventions repo into the local cache if absent,
    or fetch + hard-reset to origin's default branch if present and `refresh`
    is True. This is always a read-only checkout from gardener's point of
    view — nothing is ever committed or pushed from this cache.

    `url` overrides `$GARDENER_CONVENTIONS_URL`; with neither set this raises
    `ConventionsError` rather than cloning a default (see `resolve_url`).
    Note the resolution happens even when the cache already exists, so a
    misconfigured setup fails the same way on every run rather than only the
    first."""
    resolved = resolve_url(url)
    cache_dir = cache_dir or default_cache_dir()
    if not (cache_dir / ".git").is_dir():
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["clone", "--depth", "1", resolved, str(cache_dir)])
    else:
        # The cache is a single fixed directory, but the URL is now
        # configurable — so an existing cache may be a checkout of a
        # *different* conventions repo than the one now configured. Repoint
        # and force a refresh in that case, even under `--no-refresh-
        # conventions`: honoring that flag here would silently audit the
        # target against the previously-configured repo's conventions,
        # which is a wrong answer rather than a stale one.
        if _origin_url(cache_dir) != resolved:
            _run_git(["remote", "set-url", "origin", resolved], cwd=cache_dir)
            refresh = True
        if refresh:
            _run_git(["fetch", "--depth", "1", "origin"], cwd=cache_dir)
            _run_git(["reset", "--hard", "origin/HEAD"], cwd=cache_dir)

    source = ConventionsSource(path=cache_dir, url=resolved)
    source.verify_complete()
    return source
