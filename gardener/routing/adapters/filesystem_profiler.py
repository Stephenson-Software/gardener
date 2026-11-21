"""Adapter: RepoProfiler that walks a local clone."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

from gardener.routing.model import DEFAULT_TIER_TOKENS, RepoProfile, tier_for

SKIP_DIRS = {".git", "node_modules", "vendor", "build", "dist", "target", ".venv", "venv",
             "__pycache__", ".gradle", ".idea", "out", "bin", "obj"}
TEXT_LIMIT_BYTES = 1_000_000   # bigger files are treated as data, not code
TEST_MARKERS = ("test", "tests", "spec", "__tests__")


def _is_text(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return b"\0" not in f.read(4096)
    except OSError:
        return False


class FilesystemProfiler:
    """Cheap size profile of a clone: text bytes outside build/vendor dirs, ~4 bytes per token."""

    def profile(self, path: Path, tier_tokens: Optional[Mapping] = None) -> RepoProfile:
        tier_tokens = tier_tokens or DEFAULT_TIER_TOKENS
        files = text_bytes = 0
        has_tests = False
        for root, dirs, names in os.walk(path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            rel_parts = {p.lower() for p in Path(root).relative_to(path).parts}
            if rel_parts & set(TEST_MARKERS):
                has_tests = True
            for n in names:
                p = Path(root) / n
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                if size > TEXT_LIMIT_BYTES or not _is_text(p):
                    continue
                files += 1
                text_bytes += size
                low = n.lower()
                if low.startswith("test") or low.endswith(("_test.py", "test.java", ".test.js", ".spec.ts",
                                                           "tests.cs")):
                    has_tests = True
        est = text_bytes // 4
        return RepoProfile(files=files, text_bytes=text_bytes, est_tokens=est, has_tests=has_tests,
                           tier=tier_for(est, tier_tokens))
