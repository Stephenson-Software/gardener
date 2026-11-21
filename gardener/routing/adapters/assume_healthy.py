"""Adapter: HealthProbe that never probes (planning, `--no-health`)."""
from __future__ import annotations

from gardener.routing.model import Health, Provider


class AssumeHealthy:
    def check(self, provider: Provider) -> Health:
        return Health.up()
