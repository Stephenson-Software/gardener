"""Port: ConcurrencyLimiter."""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Callable, Optional, Protocol, runtime_checkable

from gardener.routing.model import Provider


@runtime_checkable
class ConcurrencyLimiter(Protocol):
    """Holds one of a provider's `concurrency` slots for the length of an attempt.
    `on_wait(provider)` is called once if the caller has to wait."""

    def hold(self, provider: Provider,
             on_wait: Optional[Callable[[Provider], None]] = None) -> AbstractContextManager: ...
