"""The routing core: parse a routing config, order providers for a run, escalate down the list.

Pure policy. Everything it needs from outside comes in through a port (`ports.py`), so it never
opens a file or a socket, spawns a process or prints.
"""
from __future__ import annotations

import dataclasses
import re
from contextlib import nullcontext
from types import MappingProxyType
from typing import Mapping, Optional

from gardener.dispatch import DispatchResult, HarnessConfig, Mode
from gardener.routing.model import (
    COSTS, DEFAULT_CONTEXT_MULTIPLE, DEFAULT_TIER_TOKENS, ESCALATE_ON_DEFAULT, TIERS, Decision, Health,
    Provider, RepoProfile, RoutingConfig, RoutingError, Rule,
)
from gardener.routing.ports import (
    AgentRunner, ConcurrencyLimiter, HealthProbe, ProviderKind, Readiness, RoutingObserver,
)

EX_TEMPFAIL = 75

#: Provider keys routing itself reads; every other key belongs to the provider's kind.
CORE_PROVIDER_KEYS = frozenset({"harness", "cost", "local", "concurrency", "context_tokens", "modes",
                                "max_tier", "health_url"})

#: Key names a routing file must never carry: it holds no secrets (keys stay in the provider's
#: own environment or the operator's settings), and refusing these keeps one from being committed
#: or pushed around by accident. `context_tokens`/`tier_tokens` don't match: whole words only.
CREDENTIAL_KEY_RE = re.compile(
    r"(^|_)(api_?key|key|token|secret|password|passwd|credentials?|auth|bearer)$", re.IGNORECASE)


# ----------------------------------------------------------------- config
def _refuse_credentials(raw, where: str) -> None:
    if isinstance(raw, dict):
        for k, v in raw.items():
            # A repo slug (owner/name) is a name, not a setting.
            if isinstance(k, str) and "/" not in k and CREDENTIAL_KEY_RE.search(k):
                raise RoutingError(f"{where}: key {k!r} looks like a credential; routing files hold "
                                   "no secrets (keep them in the provider's own environment)")
            _refuse_credentials(v, f"{where}.{k}")
    elif isinstance(raw, list):
        for item in raw:
            _refuse_credentials(item, where)


def _modes(raw, where: str) -> frozenset:
    values = set(raw or [])
    known = {m.value for m in Mode} | {"*"}
    bad = values - known
    if bad:
        raise RoutingError(f"{where}: unknown mode(s) {sorted(bad)} (known: {sorted(known - {'*'})})")
    return frozenset(values)


def _parse_provider(name: str, raw: dict, kinds: Mapping[str, ProviderKind]) -> Provider:
    where = f"provider {name!r}"
    if not isinstance(raw, dict):
        raise RoutingError(f"{where}: must be a JSON object")
    if "unscoped_modes" in raw or "allow_unscoped" in raw:
        raise RoutingError(f"{where}: unscoped modes can only be allowed by the operator's "
                           "GARDENER_HARNESS_ALLOW_UNSCOPED setting, not by a routing file")
    kind_name = raw.get("harness", "claude-code")
    kind = kinds.get(kind_name)
    if kind is None:
        raise RoutingError(f"{where}: unknown harness {kind_name!r} (known: {sorted(kinds)})")
    unknown = set(raw) - CORE_PROVIDER_KEYS - kind.settings_keys
    if unknown:
        raise RoutingError(f"{where}: unknown key(s) {sorted(unknown)} for harness {kind_name!r} "
                           f"(it takes {sorted(kind.settings_keys) or 'none'})")
    settings = kind.validate(name, {k: raw[k] for k in kind.settings_keys if k in raw})
    max_tier = raw.get("max_tier", "L")
    if max_tier not in TIERS:
        raise RoutingError(f"{where}: max_tier must be one of {TIERS}")
    cost = raw.get("cost", "subscription")
    if cost not in COSTS:
        raise RoutingError(f"{where}: cost must be one of {COSTS}")
    modes = _modes(raw.get("modes", [m.value for m in Mode]), where)
    if "*" in modes:
        modes = frozenset(m.value for m in Mode)
    return Provider(
        name=name, kind=kind_name, settings=MappingProxyType(dict(settings)), cost=cost,
        local=bool(raw.get("local", False)), concurrency=int(raw.get("concurrency", 1)),
        context_tokens=int(raw.get("context_tokens", 200_000)),
        modes=frozenset(Mode(m) for m in modes), max_tier=max_tier, health_url=raw.get("health_url"),
    )


def parse_config(raw: dict, kinds: Mapping[str, ProviderKind]) -> RoutingConfig:
    """Validate a routing file's JSON against the core's rules and each provider's kind."""
    if not isinstance(raw, dict):
        raise RoutingError("routing file must be a JSON object")
    _refuse_credentials(raw, "routing file")
    providers = {name: _parse_provider(name, p, kinds) for name, p in (raw.get("providers") or {}).items()}
    if not providers:
        raise RoutingError("routing file defines no providers")

    def known(names, where):
        missing = [n for n in names if n not in providers]
        if missing:
            raise RoutingError(f"{where}: unknown provider(s) {missing}")
        return tuple(names)

    rules = []
    for i, r in enumerate(raw.get("rules") or []):
        tiers = frozenset(r.get("tiers", ["*"]))
        if tiers - set(TIERS) - {"*"}:
            raise RoutingError(f"rule {i}: tiers must be from {TIERS} or '*'")
        rules.append(Rule(modes=_modes(r.get("modes", ["*"]), f"rule {i}"), tiers=tiers,
                          providers=known(r.get("providers") or [], f"rule {i}")))
    repos = {}
    for slug, o in (raw.get("repos") or {}).items():
        if "tier" in o and o["tier"] not in TIERS:
            raise RoutingError(f"repo {slug}: tier must be one of {TIERS}")
        if "providers" in o:
            known(o["providers"], f"repo {slug}")
        repos[slug] = dict(o)
    tier_tokens = dict(DEFAULT_TIER_TOKENS, **(raw.get("tier_tokens") or {}))
    if not tier_tokens["S"] < tier_tokens["M"]:
        raise RoutingError("tier_tokens: S must be below M")
    escalate_on = frozenset(raw.get("escalate_on", ESCALATE_ON_DEFAULT))
    if escalate_on - ESCALATE_ON_DEFAULT:
        raise RoutingError(f"escalate_on: unknown trigger(s) {sorted(escalate_on - ESCALATE_ON_DEFAULT)}")
    return RoutingConfig(providers=providers, rules=tuple(rules), repos=repos, tier_tokens=tier_tokens,
                         context_multiple=float(raw.get("context_multiple", DEFAULT_CONTEXT_MULTIPLE)),
                         escalate_on=escalate_on)


# ----------------------------------------------------------------- routing
def route(config: RoutingConfig, mode: Mode, repo: str, profile: RepoProfile,
          health: Optional[HealthProbe] = None, readiness: Optional[Readiness] = None) -> Decision:
    """Order the providers for (mode, repo). Policy checks first (trust, tier, locality,
    context), then `readiness`, then `health` (the slow one). With no probe given, a provider
    counts as up: that's planning, not dispatch."""
    override = config.repos.get(repo, {})
    tier = override.get("tier", profile.tier)
    rule_index = None
    if "providers" in override:
        names = tuple(override["providers"])
    else:
        names = ()
        for i, r in enumerate(config.rules):
            if r.matches(mode, tier):
                names, rule_index = r.providers, i
                break
    candidates, skipped = [], []
    for name in names:
        p = config.providers[name]
        if mode not in p.modes:
            skipped.append((name, f"not trusted with {mode.value}"))
        elif TIERS.index(tier) > TIERS.index(p.max_tier):
            skipped.append((name, f"tier {tier} above its max_tier {p.max_tier}"))
        elif override.get("local_only") and not p.local:
            skipped.append((name, "repo is local_only and this provider isn't local"))
        elif profile.est_tokens > p.context_tokens * config.context_multiple:
            skipped.append((name, f"~{profile.est_tokens:,} tokens > {config.context_multiple:g}x its "
                                  f"{p.context_tokens:,}-token context"))
        else:
            state = readiness.check(p, mode) if readiness is not None else Health.up()
            if state.ok and p.health_url and health is not None:
                state = health.check(p)
            if state.ok:
                candidates.append(p)
            else:
                skipped.append((name, state.reason))
    return Decision(repo=repo, mode=mode, tier=tier, rule=rule_index, candidates=candidates, skipped=skipped)


def escalation_trigger(result: DispatchResult) -> Optional[str]:
    """Which escalate_on trigger a result hits, or None if it's a final answer. Exit 75 is checked
    first: the dispatcher also marks it `blocked`, but its trigger is `tempfail`."""
    if result.exit_code == EX_TEMPFAIL:
        return "tempfail"
    if result.blocked:
        return "blocked"
    if result.timed_out:
        return "timeout"
    if not result.ok and result.is_error:
        return "error"
    return None


def run_with_escalation(decision: Decision, runner: AgentRunner, base: HarnessConfig,
                        kinds: Mapping[str, ProviderKind],
                        escalate_on: frozenset = ESCALATE_ON_DEFAULT,
                        limiter: Optional[ConcurrencyLimiter] = None,
                        observer: Optional[RoutingObserver] = None,
                        **run_kwargs) -> tuple:
    """Run the candidates in order until one gives a final answer.

    Each attempt is `runner(harness_config=<its kind's config>, **run_kwargs)`, holding one of
    its provider's `limiter` slots. Returns (result, attempts), attempts being
    [(provider, trigger-or-None)]. The result carries `provider` and `escalated_from`, and no
    cost for a `free` provider: Claude Code prices every run as if a Claude model served it,
    so a run through a self-hosted model reports a price nobody paid. None means "no
    meaningful price", which every reader already handles; 0.0 would claim a measurement.
    Raises RoutingError when there are no candidates, so a misrouted job fails loudly
    instead of silently using a default.
    """
    if not decision.candidates:
        raise RoutingError("no eligible provider:\n" + decision.explain())
    attempts = []
    result = None
    for i, p in enumerate(decision.candidates):
        config = kinds[p.kind].harness_config(p, base)
        hold = limiter.hold(p, on_wait=observer.waiting if observer else None) if limiter else nullcontext()
        with hold:
            result = runner(harness_config=config, **run_kwargs)
        trigger = escalation_trigger(result)
        result = dataclasses.replace(
            result,
            provider=p.name,
            escalated_from=tuple(f"{name}:{t}" for name, t in attempts),
            cost_usd=None if p.cost == "free" else result.cost_usd,
        )
        attempts.append((p.name, trigger))
        if trigger is None or trigger not in escalate_on:
            break
        if observer is not None and i + 1 < len(decision.candidates):
            observer.escalating(p.name, trigger, decision.candidates[i + 1].name)
    return result, attempts
