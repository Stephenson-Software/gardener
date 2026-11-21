"""Adapter: RoutingObserver that says nothing."""
from __future__ import annotations


class NullObserver:
    def routed(self, decision, profile) -> None:
        pass

    def waiting(self, provider) -> None:
        pass

    def escalating(self, provider, trigger, next_provider) -> None:
        pass
