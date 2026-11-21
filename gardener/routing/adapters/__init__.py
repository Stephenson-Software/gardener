"""Production adapters for the routing ports, one file per implementation.

| Port               | Adapters                                   |
|--------------------|--------------------------------------------|
| ProviderKind       | ClaudeCodeKind, CommandKind                |
| HealthProbe        | HttpHealthProbe, AssumeHealthy             |
| Readiness          | DispatchReadiness                          |
| RepoProfiler       | FilesystemProfiler                         |
| RoutingSource      | JsonFileSource                             |
| ConcurrencyLimiter | SemaphoreLimiter (`PROVIDER_SLOTS`)        |
| AgentRunner        | DispatchRunner                             |
| RoutingObserver    | StderrObserver, NullObserver               |
"""
from typing import Mapping

from gardener.routing.adapters.assume_healthy import AssumeHealthy
from gardener.routing.adapters.claude_code_kind import ClaudeCodeKind
from gardener.routing.adapters.command_kind import CommandKind
from gardener.routing.adapters.dispatch_readiness import DispatchReadiness
from gardener.routing.adapters.dispatch_runner import DispatchRunner
from gardener.routing.adapters.filesystem_profiler import FilesystemProfiler
from gardener.routing.adapters.http_health_probe import HttpHealthProbe
from gardener.routing.adapters.json_file_source import ROUTING_ENV, JsonFileSource, default_routing_path
from gardener.routing.adapters.null_observer import NullObserver
from gardener.routing.adapters.semaphore_limiter import PROVIDER_SLOTS, SemaphoreLimiter
from gardener.routing.adapters.stderr_observer import StderrObserver

#: The provider kinds a routing file may name. A new kind of provider is one more entry.
DEFAULT_KINDS: Mapping = {k.name: k for k in (ClaudeCodeKind(), CommandKind())}

__all__ = [
    "DEFAULT_KINDS", "PROVIDER_SLOTS", "ROUTING_ENV", "AssumeHealthy", "ClaudeCodeKind", "CommandKind",
    "DispatchReadiness", "DispatchRunner", "FilesystemProfiler", "HttpHealthProbe", "JsonFileSource",
    "NullObserver", "SemaphoreLimiter", "StderrObserver", "default_routing_path",
]
