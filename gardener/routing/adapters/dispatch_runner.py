"""Adapter: AgentRunner via `dispatch.run_agent`, unrouted.

Every attempt goes back through `run_agent`, so it takes the same `run_claude` /
`run_command_harness` paths and the same permission checks as an unrouted run.
"""
from __future__ import annotations

from gardener.dispatch import DispatchResult, HarnessConfig


class DispatchRunner:
    def __call__(self, harness_config: HarnessConfig, **kwargs) -> DispatchResult:
        from gardener import dispatch

        return dispatch.run_agent(harness_config=harness_config, **kwargs)
