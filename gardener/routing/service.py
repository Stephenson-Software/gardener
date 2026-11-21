"""The application service: the one object callers use, with every port wired.

`dispatch.run_agent` (through `_run_routed`), `gardener route` and `overnight`'s batch
planning all go through a `RoutingService`. Its defaults are the production adapters; tests
pass doubles for any port.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from gardener.dispatch import HarnessConfig, Mode
from gardener.routing import core
from gardener.routing.adapters import (
    DEFAULT_KINDS, PROVIDER_SLOTS, AssumeHealthy, FilesystemProfiler, HttpHealthProbe, StderrObserver,
)
from gardener.routing.model import Decision, Provider, RepoProfile, RoutingConfig
from gardener.routing.ports import (
    AgentRunner, ConcurrencyLimiter, HealthProbe, ProviderKind, Readiness, RepoProfiler, RoutingObserver,
)


@dataclass
class RoutingService:
    config: RoutingConfig
    kinds: Mapping[str, ProviderKind] = field(default_factory=lambda: DEFAULT_KINDS)
    profiler: RepoProfiler = field(default_factory=FilesystemProfiler)
    health: HealthProbe = field(default_factory=HttpHealthProbe)
    limiter: ConcurrencyLimiter = PROVIDER_SLOTS
    observer: RoutingObserver = field(default_factory=StderrObserver)

    def harness_config(self, provider: Provider, base: HarnessConfig) -> HarnessConfig:
        """The `HarnessConfig` `dispatch.run_agent` would run this provider with."""
        return self.kinds[provider.kind].harness_config(provider, base)

    def profile(self, path: Path) -> RepoProfile:
        return self.profiler.profile(path, self.config.tier_tokens)

    def decide(self, mode: Mode, repo: str, path: Path,
               readiness: Optional[Readiness] = None) -> tuple:
        """(decision, profile) for a run of `mode` on `repo`, whose clone is at `path`."""
        profile = self.profile(path)
        return core.route(self.config, mode, repo, profile, health=self.health, readiness=readiness), profile

    def dispatch(self, mode: Mode, repo: str, path: Path, runner: AgentRunner, base: HarnessConfig,
                 readiness: Optional[Readiness] = None, **run_kwargs) -> tuple:
        """Route and run. Returns (decision, result); result is None when no provider was
        eligible, so the caller can refuse the run in its own terms."""
        decision, profile = self.decide(mode, repo, path, readiness)
        self.observer.routed(decision, profile)
        if not decision.candidates:
            return decision, None
        result, _attempts = core.run_with_escalation(
            decision, runner, base, self.kinds, self.config.escalate_on,
            limiter=self.limiter, observer=self.observer, mode=mode, **run_kwargs,
        )
        return decision, result

    def explain(self, mode: Mode, repo: str, path: Path, readiness: Optional[Readiness] = None) -> str:
        """The profile line plus `Decision.explain()`: what `gardener route` prints."""
        decision, profile = self.decide(mode, repo, path, readiness)
        return profile.describe() + "\n" + decision.explain()

    def first_choice(self, mode: Mode, repo: str, path: Path) -> Optional[str]:
        """The provider a run would most likely start on, for planning only (`overnight`'s
        batches): no health probe and no readiness check, and None when there is no clone to
        profile yet or no candidate. Never raises: a planning guess must not stop a batch."""
        try:
            if not path.is_dir():
                return None
            decision = core.route(self.config, mode, repo, self.profile(path), health=AssumeHealthy())
        except Exception:  # noqa: BLE001 - see docstring
            return None
        return decision.candidates[0].name if decision.candidates else None
