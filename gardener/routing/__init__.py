"""Provider routing: choose the provider (harness + model + endpoint) per repo and mode.

Design: docs/rfcs/provider-routing.md. Off unless a routing file exists: with none, gardener
behaves exactly as before, one global harness from `GARDENER_HARNESS` & co.

Laid out as ports and adapters:

- `model.py`: the domain values (`Provider`, `Rule`, `RoutingConfig`, `RepoProfile`,
  `Decision`, `Health`). No I/O, no harness detail.
- `ports/`: one file per protocol the core talks through (`ProviderKind`, `HealthProbe`,
  `Readiness`, `RepoProfiler`, `RoutingSource`, `ConcurrencyLimiter`, `AgentRunner`,
  `RoutingObserver`); `ports/__init__.py` has the diagram.
- `core.py`: the policy (`parse_config`, `route`, `run_with_escalation`). Pure.
- `adapters/`: one file per production implementation (the `claude-code` and `command`
  kinds, the HTTP health contract, the filesystem profiler, `routing.json`, per-provider
  slots, stderr logging). `DispatchReadiness` and `DispatchRunner` only adapt
  `dispatch.check_harness_ready` and `dispatch.run_agent`: gardener's harness choice and
  safety checks stay in `dispatch.py`.
- `service.py`: `RoutingService`, the one entry point, with every port wired.

A new kind of provider (say, a direct HTTP API harness) is one new file in `adapters/` and
one entry in `adapters.DEFAULT_KINDS`; the core, the routing file format and the callers
don't change.

The routing file is `routing.json` in gardener's state directory (`GARDENER_STATE_DIR`,
default `~/.local/state/gardener/`), or wherever `GARDENER_ROUTING` points:

    {
      "providers": {
        "claude-sub": {"harness": "claude-code", "cost": "subscription", "concurrency": 2,
                       "context_tokens": 200000, "modes": ["*"]},
        "local-gpu":  {"harness": "claude-code", "claude_bin": "/path/to/claude-local",
                       "cost": "free", "local": true, "concurrency": 1, "context_tokens": 65536,
                       "modes": ["report"], "max_tier": "M",
                       "health_url": "http://gpu-host:8799/health"}
      },
      "rules": [
        {"modes": ["report"], "tiers": ["S", "M"], "providers": ["local-gpu", "claude-sub"]},
        {"modes": ["*"], "tiers": ["*"], "providers": ["claude-sub"]}
      ],
      "repos": {
        "owner/huge-repo": {"tier": "L"},
        "owner/private-repo": {"local_only": true}
      }
    }
"""
from gardener.routing.adapters import (
    DEFAULT_KINDS, PROVIDER_SLOTS, ROUTING_ENV, AssumeHealthy, ClaudeCodeKind, CommandKind, DispatchReadiness,
    DispatchRunner, FilesystemProfiler, HttpHealthProbe, JsonFileSource, NullObserver, SemaphoreLimiter,
    StderrObserver, default_routing_path,
)
from gardener.routing.core import EX_TEMPFAIL, escalation_trigger, parse_config, route, run_with_escalation
from gardener.routing.model import (
    TIERS, Decision, Health, Provider, RepoProfile, RoutingConfig, RoutingError, Rule,
)
from gardener.routing.service import RoutingService

__all__ = [
    "DEFAULT_KINDS", "PROVIDER_SLOTS", "ROUTING_ENV", "TIERS", "EX_TEMPFAIL",
    "AssumeHealthy", "ClaudeCodeKind", "CommandKind", "DispatchReadiness", "DispatchRunner",
    "FilesystemProfiler", "HttpHealthProbe", "JsonFileSource", "NullObserver", "SemaphoreLimiter", "StderrObserver",
    "Decision", "Health", "Provider", "RepoProfile", "RoutingConfig", "RoutingError", "Rule", "RoutingService",
    "default_routing_path", "escalation_trigger", "load_routing", "parse_config", "route", "run_with_escalation",
]


def load_routing(path=None):
    """The routing config from `routing.json` (or `path`), or None when routing is off."""
    return JsonFileSource(path).load()
