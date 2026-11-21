"""Port: RepoProfiler."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from gardener.routing.model import RepoProfile


@runtime_checkable
class RepoProfiler(Protocol):
    """How big is this clone? Decides the repo's tier and the context check."""

    def profile(self, path: Path, tier_tokens: Mapping) -> RepoProfile: ...
