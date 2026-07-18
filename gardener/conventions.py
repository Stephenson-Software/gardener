"""Clones/refreshes a local cache of dms-conventions and locates the docs
gardener's prompt template needs.

dms-conventions (https://github.com/dmccoystephenson/dms-conventions) is
phase 1 of this two-phase initiative and is the actual source of truth for
what "aligned" means. gardener never invents or duplicates convention
content — it only points a dispatched Claude Code run at these files.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

DMS_CONVENTIONS_URL = "https://github.com/dmccoystephenson/dms-conventions.git"

# Read-only: gardener never commits into its own cache of dms-conventions,
# it only clones/pulls it. Docs required for a full alignment pass, per
# ALIGNMENT_PROMPT.md's own reading list.
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


def default_cache_dir() -> Path:
    override = os.environ.get("GARDENER_CACHE_DIR")
    if override:
        return Path(override) / "dms-conventions"
    return Path.home() / ".cache" / "gardener" / "dms-conventions"


@dataclass
class ConventionsSource:
    path: Path

    def checklist_path(self) -> Path:
        return self.path / "ALIGNMENT_CHECKLIST.md"

    def alignment_prompt_path(self) -> Path:
        return self.path / "ALIGNMENT_PROMPT.md"

    def verify_complete(self) -> None:
        missing = [d for d in REQUIRED_DOCS if not (self.path / d).is_file()]
        if missing:
            raise ConventionsError(
                "dms-conventions checkout at "
                f"{self.path} is missing expected doc(s): {', '.join(missing)}. "
                "Re-run with --refresh-conventions, or check "
                f"{DMS_CONVENTIONS_URL} still has this layout."
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


def ensure_conventions(
    cache_dir: Path | None = None, refresh: bool = True
) -> ConventionsSource:
    """Clone dms-conventions into the local cache if absent, or fetch +
    hard-reset to origin's default branch if present and `refresh` is
    True. This is always a read-only checkout from gardener's point of
    view — nothing is ever committed or pushed from this cache."""
    cache_dir = cache_dir or default_cache_dir()
    if not (cache_dir / ".git").is_dir():
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["clone", "--depth", "1", DMS_CONVENTIONS_URL, str(cache_dir)])
    elif refresh:
        _run_git(["fetch", "--depth", "1", "origin"], cwd=cache_dir)
        _run_git(["reset", "--hard", "origin/HEAD"], cwd=cache_dir)

    source = ConventionsSource(path=cache_dir)
    source.verify_complete()
    return source
