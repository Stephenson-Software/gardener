"""Adapter: HealthProbe over HTTP (the provider health contract)."""
from __future__ import annotations

import json
import urllib.request
from typing import Callable

from gardener.routing.model import Health, Provider


class HttpHealthProbe:
    """A GET of `health_url` answers 2xx with a JSON object; `"paused": true` or
    `"ok": false` means don't use it now. Any other answer, or none, means unreachable."""

    def __init__(self, timeout: float = 3.0, opener: Callable = urllib.request.urlopen):
        self.timeout = timeout
        self._open = opener

    def check(self, provider: Provider) -> Health:
        try:
            with self._open(provider.health_url, timeout=self.timeout) as r:
                body = json.loads(r.read() or b"{}")
        except (OSError, ValueError) as e:
            return Health.down(f"unreachable ({e.__class__.__name__})")
        if not isinstance(body, dict):
            return Health.down("unreachable (health answer is not a JSON object)")
        if body.get("paused"):
            return Health.down("paused by its operator")
        if body.get("ok") is False:
            return Health.down("unhealthy")
        return Health.up()
