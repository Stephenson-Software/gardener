"""The ports: every way the routing core touches the outside world, one file each.

The core (`core.py`) is written against these protocols only. Each has a production adapter
in `adapters/` (one file per implementation), and tests swap in doubles. Adding a new kind
of provider means writing one `ProviderKind` adapter, nothing else.

    driving side                     core                         driven side (ports)
    ------------                     ----                         -------------------
    cli `gardener route`  --->  RoutingService  ---> RoutingSource      (where the config lives)
    dispatch.run_agent    --->   (service.py)   ---> RepoProfiler       (how big is this repo)
    overnight planning    --->       |          ---> ProviderKind       (turn a provider into a run)
                                     |          ---> Readiness          (can it run here at all)
                                   core.py      ---> HealthProbe        (is it up right now)
                              (route, escalate) ---> ConcurrencyLimiter (is it free)
                                                ---> AgentRunner        (do the run)
                                                ---> RoutingObserver    (tell someone)
"""
from gardener.routing.ports.provider_kind import ProviderKind
from gardener.routing.ports.health_probe import HealthProbe
from gardener.routing.ports.readiness import Readiness
from gardener.routing.ports.repo_profiler import RepoProfiler
from gardener.routing.ports.routing_source import RoutingSource
from gardener.routing.ports.concurrency_limiter import ConcurrencyLimiter
from gardener.routing.ports.agent_runner import AgentRunner
from gardener.routing.ports.routing_observer import RoutingObserver

__all__ = ["ProviderKind", "HealthProbe", "Readiness", "RepoProfiler", "RoutingSource", "ConcurrencyLimiter", "AgentRunner", "RoutingObserver"]
