"""Adapter: RoutingSource backed by `routing.json`."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping, Optional

from gardener.routing.core import parse_config
from gardener.routing.model import RoutingConfig, RoutingError

ROUTING_ENV = "GARDENER_ROUTING"
ROUTING_FILE_NAME = "routing.json"


def default_routing_path(env=None) -> Path:
    env = os.environ if env is None else env
    if env.get(ROUTING_ENV):
        return Path(env[ROUTING_ENV])
    from gardener import state

    return state.default_state_dir() / ROUTING_FILE_NAME


class JsonFileSource:
    """Hand-edited JSON like `garden.json`, in the state directory or at `$GARDENER_ROUTING`.
    A missing file means routing is off."""

    def __init__(self, path: Optional[Path] = None, kinds: Optional[Mapping] = None):
        from gardener.routing.adapters import DEFAULT_KINDS

        self.path = Path(path) if path is not None else default_routing_path()
        self.kinds = kinds if kinds is not None else DEFAULT_KINDS

    def describe(self) -> str:
        return str(self.path)

    def load(self) -> Optional[RoutingConfig]:
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            raise RoutingError(f"{self.path}: not valid JSON ({e})") from None
        return parse_config(raw, self.kinds)
