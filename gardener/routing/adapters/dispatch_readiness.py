"""Adapter: Readiness via `dispatch.check_harness_ready`.

The check itself stays in `dispatch.py`, where gardener's harness choice and safety checks
live (CLAUDE.md, "The agent harness is chosen in dispatch.py too"); this only adapts it.
"""
from __future__ import annotations

from typing import Mapping, Optional

from gardener.dispatch import DispatchError, HarnessConfig, Mode, check_harness_ready
from gardener.routing.model import Health, Provider


class DispatchReadiness:
    """A provider whose binary is missing, or a `command` provider asked for a mode the
    operator never opted in, is skipped with that reason instead of failing the run."""

    def __init__(self, base: HarnessConfig, kinds: Optional[Mapping] = None):
        from gardener.routing.adapters import DEFAULT_KINDS

        self.base = base
        self.kinds = kinds if kinds is not None else DEFAULT_KINDS

    def check(self, provider: Provider, mode: Mode) -> Health:
        try:
            check_harness_ready(mode, self.kinds[provider.kind].harness_config(provider, self.base))
        except DispatchError as e:
            return Health.down(str(e))
        return Health.up()
