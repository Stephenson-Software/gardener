"""Adapter: RoutingObserver that logs `gardener: routing ...` lines to stderr."""
from __future__ import annotations

import sys

from gardener.routing.model import Decision, Provider, RepoProfile


class StderrObserver:
    def __init__(self, stream=None):
        self._stream = stream

    def _print(self, line: str) -> None:
        print(line, file=self._stream or sys.stderr)

    def routed(self, decision: Decision, profile: RepoProfile) -> None:
        names = ", ".join(p.name for p in decision.candidates) or "none"
        self._print(f"gardener: routing {decision.repo} ({decision.mode.value}): ~{profile.est_tokens:,} "
                    f"tokens, tier {decision.tier}, providers: {names}")
        for name, why in decision.skipped:
            self._print(f"gardener: routing: skipped {name}: {why}")

    def waiting(self, provider: Provider) -> None:
        self._print(f"gardener: routing: {provider.name} is at its concurrency limit "
                    f"({provider.concurrency}); waiting for a free slot")

    def escalating(self, provider: str, trigger: str, next_provider: str) -> None:
        self._print(f"gardener: routing: {provider} ended with {trigger}; escalating to {next_provider}")
