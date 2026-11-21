"""Port: RoutingSource."""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from gardener.routing.model import RoutingConfig


@runtime_checkable
class RoutingSource(Protocol):
    """Where the routing config comes from. `load()` is None when routing is off."""

    def describe(self) -> str: ...

    def load(self) -> Optional[RoutingConfig]: ...
