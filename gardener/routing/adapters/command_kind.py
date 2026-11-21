"""Adapter: ProviderKind for `"harness": "command"`."""
from __future__ import annotations

import dataclasses
from typing import Mapping

from gardener.dispatch import CLAUDE_BIN, Harness, HarnessConfig
from gardener.routing.model import Provider, RoutingError


@dataclasses.dataclass(frozen=True)
class CommandKind:
    """Any program speaking gardener's harness contract (docs/HARNESSES.md). Which modes it
    may run unscoped is the operator's call alone: `base.unscoped_modes` passes through."""

    name: str = "command"
    settings_keys: frozenset = frozenset({"command", "model"})

    def validate(self, provider: str, settings: Mapping) -> Mapping:
        command = settings.get("command")
        if not (isinstance(command, list) and command and all(isinstance(a, str) and a for a in command)):
            raise RoutingError(f"provider {provider!r}: harness 'command' needs a 'command' list of strings")
        return dict(settings, command=tuple(command))

    def harness_config(self, provider: Provider, base: HarnessConfig) -> HarnessConfig:
        return dataclasses.replace(base, harness=Harness.COMMAND, command=tuple(provider.settings["command"]),
                                   claude_bin=CLAUDE_BIN, model=provider.settings.get("model"))
