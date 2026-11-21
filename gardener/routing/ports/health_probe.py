"""Port: HealthProbe."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from gardener.routing.model import Health, Provider


@runtime_checkable
class HealthProbe(Protocol):
    """Is the provider up right now? Only asked about providers with a `health_url`."""

    def check(self, provider: Provider) -> Health: ...
