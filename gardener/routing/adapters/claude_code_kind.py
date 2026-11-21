"""Adapter: ProviderKind for `"harness": "claude-code"`."""
from __future__ import annotations

import dataclasses
from typing import Mapping

from gardener.dispatch import CLAUDE_BIN, Harness, HarnessConfig
from gardener.routing.model import Provider, RoutingError


@dataclasses.dataclass(frozen=True)
class ClaudeCodeKind:
    """Claude Code, optionally through a wrapper (`claude_bin`) that points it at another
    model provider. Keeps every `base` setting it doesn't own."""

    name: str = "claude-code"
    settings_keys: frozenset = frozenset({"claude_bin", "model"})

    def validate(self, provider: str, settings: Mapping) -> Mapping:
        for key in ("claude_bin", "model"):
            if key in settings and not (isinstance(settings[key], str) and settings[key]):
                raise RoutingError(f"provider {provider!r}: {key} must be a non-empty string")
        return settings

    def harness_config(self, provider: Provider, base: HarnessConfig) -> HarnessConfig:
        return dataclasses.replace(base, harness=Harness.CLAUDE_CODE, command=(),
                                   claude_bin=provider.settings.get("claude_bin", CLAUDE_BIN),
                                   model=provider.settings.get("model"))
