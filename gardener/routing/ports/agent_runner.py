"""Port: AgentRunner."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from gardener.dispatch import DispatchResult, HarnessConfig


@runtime_checkable
class AgentRunner(Protocol):
    """Does one run on one harness configuration: `dispatch.run_agent`, unrouted."""

    def __call__(self, harness_config: HarnessConfig, **kwargs) -> DispatchResult: ...
