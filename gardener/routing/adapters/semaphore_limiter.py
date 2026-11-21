"""Adapter: ConcurrencyLimiter with one semaphore per provider, process-wide."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Optional

from gardener.routing.model import Provider


class SemaphoreLimiter:
    """At most `provider.concurrency` simultaneous runs per provider, across threads.

    One process-wide instance (`PROVIDER_SLOTS`) is shared by every routed dispatch, so it
    covers escalations too (a run escalating onto a busy provider waits for it rather than
    doubling up). Keyed by (name, concurrency) so a routing file edited between runs of one
    long process gets its new limit. Per process only: a manual `align` started while
    `overnight` runs is not counted against it."""

    def __init__(self):
        self._lock = threading.Lock()
        self._semaphores = {}

    def _semaphore(self, provider: Provider) -> threading.BoundedSemaphore:
        key = (provider.name, max(1, provider.concurrency))
        with self._lock:
            if key not in self._semaphores:
                self._semaphores[key] = threading.BoundedSemaphore(key[1])
            return self._semaphores[key]

    @contextmanager
    def hold(self, provider: Provider, on_wait: Optional[Callable[[Provider], None]] = None):
        sem = self._semaphore(provider)
        if not sem.acquire(blocking=False):
            if on_wait is not None:
                on_wait(provider)
            sem.acquire()
        try:
            yield
        finally:
            sem.release()


PROVIDER_SLOTS = SemaphoreLimiter()
