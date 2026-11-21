"""`python -m gardener.routing`: explain where a run would go, for any checkout.

`gardener route` is the same with the cached clone and the readiness checks a real dispatch
makes; this form takes any path and makes no readiness check.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gardener.dispatch import Mode
from gardener.routing.adapters import ROUTING_ENV, AssumeHealthy, JsonFileSource
from gardener.routing.model import RoutingError
from gardener.routing.service import RoutingService


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gardener.routing",
                                 description="Explain which provider gardener would route a run to.")
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--path", required=True, type=Path, help="local clone to profile")
    ap.add_argument("--mode", required=True, choices=[m.value for m in Mode])
    ap.add_argument("--config", type=Path, help=f"routing file (default: ${ROUTING_ENV} or state dir)")
    ap.add_argument("--no-health", action="store_true", help="don't probe provider health URLs")
    a = ap.parse_args(argv)
    try:
        config = JsonFileSource(a.config).load()
    except RoutingError as e:
        print(f"routing file error: {e}", file=sys.stderr)
        return 2
    if config is None:
        print("no routing file: routing is off, gardener uses its single configured harness")
        return 0
    service = RoutingService(config)
    if a.no_health:
        service.health = AssumeHealthy()
    print(service.explain(Mode(a.mode), a.repo, a.path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
