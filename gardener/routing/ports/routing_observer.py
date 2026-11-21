"""Port: RoutingObserver."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from gardener.routing.model import Decision, Provider, RepoProfile


@runtime_checkable
class RoutingObserver(Protocol):
    """Told what routing decided and did, for logs and UIs. Must not raise."""

    def routed(self, decision: Decision, profile: RepoProfile) -> None: ...

    def waiting(self, provider: Provider) -> None: ...

    def escalating(self, provider: str, trigger: str, next_provider: str) -> None: ...
