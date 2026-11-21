"""The routing domain: plain values, no I/O and no knowledge of any harness.

A `Provider` here is only what routing reasons about: cost, locality, trust (modes, tier),
context size and concurrency. How a provider is actually run is its *kind*'s business (a
`ProviderKind` adapter, `ports.py`): `kind` names the adapter and `settings` is the part of
the provider's config that only that adapter reads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional

from gardener.dispatch import Mode

TIERS = ("S", "M", "L")
#: Upper bound of estimated tokens (whole repo, text files) for tiers S and M; above M is L.
DEFAULT_TIER_TOKENS = {"S": 60_000, "M": 400_000}
#: A provider is skipped when the repo's estimated tokens exceed this many times its context
#: window: agents read selectively, but past this point they thrash (compaction loops).
DEFAULT_CONTEXT_MULTIPLE = 8
ESCALATE_ON_DEFAULT = frozenset({"blocked", "timeout", "tempfail", "error"})
COSTS = ("free", "subscription", "metered")


class RoutingError(ValueError):
    pass


@dataclass(frozen=True)
class Health:
    """A port's answer to "can this provider take a run now?"."""
    ok: bool
    reason: str = ""

    @classmethod
    def up(cls) -> "Health":
        return cls(True, "")

    @classmethod
    def down(cls, reason: str) -> "Health":
        return cls(False, reason)


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str = "claude-code"           # names the ProviderKind adapter that runs it
    settings: Mapping = field(default_factory=lambda: MappingProxyType({}))  # that adapter's keys
    cost: str = "subscription"          # free | subscription | metered
    local: bool = False                 # runs on hardware the operator controls (privacy routing)
    concurrency: int = 1
    context_tokens: int = 200_000
    modes: frozenset = frozenset(Mode)
    max_tier: str = "L"
    health_url: Optional[str] = None

    @property
    def model(self) -> Optional[str]:
        return self.settings.get("model")


@dataclass(frozen=True)
class Rule:
    modes: frozenset          # Mode values, or {"*"}
    tiers: frozenset          # tiers, or {"*"}
    providers: tuple

    def matches(self, mode: Mode, tier: str) -> bool:
        return ("*" in self.modes or mode.value in self.modes) and ("*" in self.tiers or tier in self.tiers)


@dataclass(frozen=True)
class RoutingConfig:
    providers: dict
    rules: tuple
    repos: dict = field(default_factory=dict)
    tier_tokens: dict = field(default_factory=lambda: dict(DEFAULT_TIER_TOKENS))
    context_multiple: float = DEFAULT_CONTEXT_MULTIPLE
    escalate_on: frozenset = ESCALATE_ON_DEFAULT


@dataclass(frozen=True)
class RepoProfile:
    files: int
    text_bytes: int
    est_tokens: int
    has_tests: bool
    tier: str

    def describe(self) -> str:
        return (f"profile: {self.files} text files, ~{self.est_tokens:,} tokens, "
                f"tests: {'yes' if self.has_tests else 'no'} -> tier {self.tier}")


@dataclass
class Decision:
    repo: str
    mode: Mode
    tier: str
    rule: Optional[int]                 # index of the matching rule, None = repo override or no rule
    candidates: list
    skipped: list                       # (provider name, reason)

    def explain(self) -> str:
        lines = [f"{self.repo} · {self.mode.value} · tier {self.tier} · "
                 + (f"rule {self.rule}" if self.rule is not None else "repo override / no rule")]
        for i, p in enumerate(self.candidates, 1):
            lines.append(f"  {i}. {p.name} ({p.kind}{', ' + p.model if p.model else ''}; {p.cost}"
                         f"{', local' if p.local else ''})")
        for name, why in self.skipped:
            lines.append(f"  - skip {name}: {why}")
        if not self.candidates:
            lines.append("  (no candidates: gardener would refuse this run)")
        return "\n".join(lines)


def tier_for(est_tokens: int, tier_tokens: Mapping) -> str:
    return "S" if est_tokens <= tier_tokens["S"] else "M" if est_tokens <= tier_tokens["M"] else "L"
