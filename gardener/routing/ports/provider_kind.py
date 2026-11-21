"""Port: ProviderKind."""
from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

from gardener.dispatch import HarnessConfig
from gardener.routing.model import Provider


@runtime_checkable
class ProviderKind(Protocol):
    """How one kind of provider is configured and run (one adapter per `kind`).

    `settings_keys` are the provider-config keys this kind owns; `validate` checks them and
    returns the settings to keep. `harness_config` turns a provider into the `HarnessConfig`
    that `dispatch.run_agent` runs: the harness itself is still chosen, and its safety checks
    still made, in `dispatch.py`. It must keep the operator's `base` settings it doesn't own
    (timeouts, and above all `unscoped_modes`, which only the operator may widen)."""

    name: str
    settings_keys: frozenset

    def validate(self, provider: str, settings: Mapping) -> Mapping: ...

    def harness_config(self, provider: Provider, base: HarnessConfig) -> HarnessConfig: ...
