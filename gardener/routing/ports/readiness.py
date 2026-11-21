"""Port: Readiness."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from gardener.dispatch import Mode
from gardener.routing.model import Health, Provider


@runtime_checkable
class Readiness(Protocol):
    """Can this provider run `mode` on this machine at all (binary present, mode allowed)?
    Asked before the health probe, which is the slow check."""

    def check(self, provider: Provider, mode: Mode) -> Health: ...
