# RFC (draft): Per-repo provider routing for gardener

**Author:** Daniel Stephenson (drafted by Claude)
**Date:** 2026-11-20
**Status:** Draft; phase 2 implemented, manual verification pending (unnumbered)
**Version:** 0.1
**Project:** gardener
**Tags:** #rfc #gardener #harness #routing #models #self-hosted

---

## Key Term Definitions

- **Harness:** how gardener runs an agent: `claude-code` (the `claude` CLI) or `command` (any program following `docs/HARNESSES.md`). Chosen today by one global setting, `GARDENER_HARNESS`.
- **Provider:** a named place a run can go, i.e. a harness plus whatever it points at: Claude Code on the subscription, Claude Code through a wrapper such as `claude-local` that points it at a self-hosted model, Orket's gardener extension, a metered API. Has a cost class, a context window, a concurrency limit, and optionally a health URL.
- **Tier:** a repo's size class (S/M/L) from a cheap profile of its clone (estimated tokens of its text files), or set by hand.
- **Routing policy:** an ordered list of rules mapping (mode, tier) to an ordered list of providers, plus per-repo overrides.
- **Escalation:** moving a run to the next provider in its list when the current one is blocked, times out, is paused (exit 75) or errors.
- **Capability eval:** any repeatable test suite run against a provider that turns its results into verdicts such as "overnight reports: yes/no". gardener doesn't ship one; the operator brings it.

## TL;DR

gardener picks one harness and one model for every repo and every mode. With a free local provider now available (an open-weights 30B coding model on a single consumer GPU, ~31 tok/s, 64k context) and evidence that it handles report runs on small repos but not large ones, gardener should **route each run** to the cheapest provider that is trusted with that mode at that repo's size, and **escalate** to a stronger provider when the cheap one fails or is unavailable. A `routing.json` in the state directory declares providers, rules and per-repo overrides. A missing file means today's behaviour, unchanged. Phase 2 is implemented: `dispatch.run_agent` routes and escalates when a routing file exists, runs record their provider, `overnight` respects per-provider concurrency, and `gardener route` explains a decision. It still needs the manual verification below before it's trusted.

## Motivation

### One harness for everything is either too expensive or too weak

On 2026-11-20 gardener's report mode was run through `claude-local` (Claude Code against that self-hosted model) with the fake `gh` and a sandboxed config:

| Run | Result |
|---|---|
| gardener's live Claude-tier fixture | passed, ~80 s if alone on the GPU |
| `Dans-Plugins/FlyCommand` (~16k tokens of text) vs the real conventions repo | full checklist in the right format, ~160 s; some wrong items (marked existing README sections missing, claimed to know branch protection) |
| `Dans-Plugins/Medieval-Factions` (~1.05M tokens) | stopped at 34 min: 9 automatic compactions, re-reading the same 8 files, never wrote the report |

So for the local provider, small repos are feasible (as triage) and large repos aren't. Today's only choices are all-subscription or all-local, and neither fits a garden that contains both kinds of repo.

### The fleet is mixed

The garden spans one-file plugins and large projects. Most report runs are on small repos. Write modes (`tend`, `--implement`) carry real risk, and they should stay on the strongest provider until evidence says otherwise.

### Providers fail in ways gardener can't yet route around

A paused self-hosted provider (its GPU is needed for something else) makes its wrapper exit 75 within a second. Without routing, an overnight batch fails repo after repo. Patch `fix/` "Treat a claude wrapper's EX_TEMPFAIL exit as a blocked run" (drafted 2026-11-20) stops the batch cleanly. Routing turns the same signal into "use the next provider".

### Some repos shouldn't leave the house

A local provider makes privacy routing possible for the first time: an operator can mark a repo `local_only`, so its contents only ever go to providers that run on hardware they control. If no local provider is trusted with the mode, the run is refused rather than sent elsewhere.

## Goals

- **No routing file, no change.** Behaviour, settings and on-disk state stay exactly as today.
- Route per (repo, mode) to an ordered provider list from declarative rules, with per-repo overrides (tier, pinned providers, `local_only`).
- Profile repos cheaply and deterministically (no model calls) to pick a tier.
- Escalate on blocked / timeout / tempfail / error, with the triggers configurable.
- Make every decision explainable (`gardener route --repo … [--mode …]`, or `python -m gardener.routing --repo … --path … --mode …`), and record which provider ran in the run history.
- Keep every existing safety guarantee: `MODE_SPECS`, the `bypassPermissions` ban, and the `command` harness's report-only default unless the operator opts in.

## Non-Goals

- **Choosing models by quality inside gardener.** gardener routes; deciding what a provider is good enough for is evidence from outside (a capability eval, run history), written into the routing file by the operator.
- **Splitting one run across providers** (e.g. a cheap model drafts, a strong one finishes). Escalation reruns the whole job.
- **Learning routes automatically.** Phase 3 suggests changes from run history; it never edits `routing.json` on its own.
- **Billing.** Cost classes order preferences; gardener doesn't meter spend.

## Detailed Design

### 1. `routing.json`

Same pattern as `garden.json` and the merge allow-list: hand-edited JSON in the state directory (`GARDENER_STATE_DIR`, default `~/.local/state/gardener/`), or wherever `GARDENER_ROUTING` points. Missing file means routing is off. An invalid file is an error at dispatch, never a silent fallback.

```json
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
    "Dans-Plugins/Medieval-Factions": {"tier": "L"},
    "owner/private-repo": {"local_only": true}
  },
  "tier_tokens": {"S": 60000, "M": 400000},
  "context_multiple": 8,
  "escalate_on": ["blocked", "timeout", "tempfail", "error"]
}
```

A provider maps onto today's `HarnessConfig`: `harness`, `command`, `claude_bin`, `model`. Timeouts come from the operator's existing settings. **`unscoped_modes` can't be set in a routing file.** The `command` harness's write-mode opt-in stays `GARDENER_HARNESS_ALLOW_UNSCOPED` only, and a routing file that tries to set it is rejected.

### 2. Repo profile and tier

`profile_repo(clone)` walks the clone, skipping `.git`, dot-directories and common build/vendor directories (`node_modules`, `build`, `target`, …). It counts text files up to 1 MB and estimates tokens as text bytes ÷ 4. It notes whether tests exist. Tier S ≤ 60k tokens, M ≤ 400k, else L (configurable). Measured on 2026-11-20: FlyCommand 15 files / ~16k tokens → S; Medieval-Factions 614 files / ~1.05M tokens → L.

### 3. Route

For (mode, repo): apply the repo override if any (`tier`, or a pinned `providers` list), otherwise take the first rule matching (mode, tier). Then filter the providers in order, recording a reason for each one dropped:

1. the mode isn't in the provider's `modes` (not trusted with it);
2. the tier is above the provider's `max_tier`;
3. the repo is `local_only` and the provider isn't `local`;
4. estimated tokens are above `context_multiple` × `context_tokens` (agents read selectively, but past this point they thrash; Medieval-Factions at ~16× the local model's window is the observed case);
5. the provider's `health_url` reports unreachable or paused.

An empty list means the run is **refused** with the explanation. It never falls back to a default the policy didn't name.

### 4. Escalate

`run_with_escalation()` calls `run_agent(harness_config=provider.harness_config(base), …)` for each candidate in order. It stops at the first result that isn't an escalation trigger: `blocked`, `timed_out`, exit 75 (EX_TEMPFAIL), or `not ok and is_error`. A report that ran and found problems is a final answer, not a failure. Each attempt is returned so the caller can record it.

### 5. Wiring (phase 2)

Per CLAUDE.md, "the agent harness is chosen in `dispatch.py`", so routing plugs in at `run_agent`. When a routing file exists, `run_agent` profiles the clone, routes and escalates. `cli.py` keeps calling `run_agent` and nothing else. Runs gain `provider` and `escalated_from` columns, so the dashboard, hub and notifications show where each run went. `overnight` limits concurrency per provider (a single-GPU provider serves one request at a time), not only per batch.

### 6. Evidence in, not guesses (phase 2–3)

- **Phase 2:** the operator sets each provider's `modes` and `max_tier` from evidence. For self-hosted providers that's a capability eval run, with verdicts like "unattended report jobs" and "Claude Code as an interactive agent". A provider-model change (new model, quant, context) means re-running that eval and updating its routing entry. That's one file change, reviewed like any other.
- **Phase 3:** `gardener route --suggest` reads run history per (repo, mode, provider) and proposes overrides, e.g. "Medieval-Factions report on local-gpu: 0/3 successes, consider pinning claude-sub". It only proposes.

## Alternatives Considered

- **Several gardener installs with different global harnesses.** Works today with no code, but the operator has to split the garden by hand, there's no escalation, and run history is split too.
- **Route by mode only (no tiers).** Simpler, but the 2026-11-20 runs show the size limit is real: the same mode works on FlyCommand and loops on Medieval-Factions.
- **Let the model decide** (ask a cheap model to assess the repo first). Costs a model call per run, isn't deterministic, and the cheap model's judgement of its own limits is exactly what's in doubt.
- **A proxy that switches models behind one endpoint** (route inside a self-hosted gateway). Possible for model choice, but it can't change the harness, can't keep `local_only` repos local by policy, and can't see gardener's modes or run history.

## Drawbacks, Risks, and Open Questions

- **Profiles are crude.** Bytes ÷ 4 over the whole clone overstates what a run reads, and ignores how hard the code is. Tier overrides and phase 3 history are the correction, but early routes will sometimes be wrong. The cost of a wrong route is bounded: an escalation (wasted time) or a weaker report (triage quality).
- **Escalation costs time.** A run that loops for its whole timeout before escalating wastes it. Should a provider get its own shorter timeout (e.g. local-gpu report runs capped at 15 min)? *Open.*
- **Quality isn't checked before accepting a cheap answer.** A report from a weaker model that contains wrong items is still accepted (observed: FlyCommand). A "verify with a stronger provider" step is a possible phase 4. *Open:* is that worth the subscription use it costs?
- **Recorded cost is wrong for non-Claude models.** Claude Code prices a run on a self-hosted model as if a Claude model served it ($0.15–0.25 recorded, $0 spent). *Decided (phase 2):* gardener records `cost_usd` as NULL (unknown) for a `cost: free` provider's run, in `core.run_with_escalation`, the one place that knows which provider produced a result. NULL rather than 0: gardener didn't measure a zero, it knows the reported figure is meaningless, and every reader already treats NULL as "no cost" (SQL `SUM` skips it, the CLI prints `?`); the `provider` column says why. Fixing it in the wrapper instead would need every wrapper to rewrite Claude Code's JSON, and wouldn't cover a `command` harness that reports a cost. Subscription and metered providers keep the reported figure. Only the final attempt's cost is recorded; an escalated run's earlier attempts (normally on free providers) aren't.
- **More configuration to get wrong.** Mitigated by strict validation (unknown providers, modes or tiers fail loudly) and the explain command.
- **Health checks add latency** (≤ 3 s per provider with a `health_url`, once per run).
- **Concurrency slots are per process.** `overnight`'s threads share them, so `--concurrency 2` never sends two runs at once to a `concurrency: 1` provider (the second waits), and `overnight` plans batches from cached clones so that rarely happens. A separate `align` started while `overnight` runs is not counted. A cross-process limit (lock files per slot) is possible later if that turns out to matter; the provider can also queue requests itself.

## Security and Deployment

- No new network listeners. Health checks are outbound GETs to URLs the operator wrote.
- Keys stay where they are today: in the wrapper's environment (e.g. `claude-local` reads its endpoint and key from its own environment) or the operator's settings. `routing.json` holds no secrets: a key anywhere in it whose name looks like a credential (`api_key`, `key`, `token`, `secret`, `password`, `credential(s)`, `auth`, `bearer`, as a whole word) is rejected (implemented in phase 2).
- `local_only` is a hard filter. A repo marked local never reaches a non-local provider, even when that means refusing the run.
- Safety constraints stay in `dispatch.py`. Routing only chooses among `HarnessConfig`s, which go through the existing `_check_permission_mode` / `check_harness_ready` paths. A routed `command` provider's unscoped modes are always the operator's `GARDENER_HARNESS_ALLOW_UNSCOPED` (read even when the global harness is `claude-code`), and a provider that can't run a mode here is skipped with the reason, before its health probe.

## Migration / Rollout

1. **Phase 1 (this RFC's prototype):** `gardener/router.py` + `tests/test_router.py` + `python -m gardener.router` explain (since restructured as the `gardener/routing/` package, below). Not wired. Zero behaviour change.
2. **Phase 2 (implemented; manual verification pending):** wire into `run_agent` behind the presence of `routing.json`. Add `provider`/`escalated_from` columns (an additive migration, like the hub's), per-provider concurrency in `overnight`, and docs (`docs/HARNESSES.md` routing section, `docs/USAGE.md`). Manual verification as CLAUDE.md requires for dispatch changes: a real `align` routed to a self-hosted provider on a low-stakes repo, then escalated by pausing it mid-batch.
3. **Phase 3:** `gardener route --suggest` from history.

## Phase 2 as implemented

- **Wiring.** `run_agent` gained `repo` (and `routing_path` for tests). Every `run_agent` call in `cli.py` (align, tend, the create-dev-loop bootstrap, Parpt scoring) passes the repo; `cwd` is already its clone. With a repo and a routing file, `dispatch._run_routed` profiles, routes (with a readiness check per provider: `check_harness_ready` on its own `HarnessConfig`) and calls `RoutingService.dispatch` (`core.run_with_escalation`), whose attempts call `run_agent` again without a repo. No repo, or no routing file, is the old code path untouched (tested: identical argv and result).
- **Refusal.** No candidates returns a failed `DispatchResult` (`is_error`, not `blocked`, `failure_reason` = "routing refused this run: no eligible provider" plus the explanation). Callers record it as an `error` run and alert, like any failed run; `overnight` moves on to the next repo, since a refusal is about this repo.
- **Provenance.** `runs` gains `provider` and `escalated_from` (comma-separated `name:trigger`), added to `state.ADDED_COLUMNS` so `_migrate` adds them to an existing db; no backfill. The hub wire format carries both as optional fields: an older hub ignores them and an older device's pushes are still accepted. `gardener status` adds a provider column only when a listed run has one, and alerts add a `provider:` line. The dashboard doesn't show them yet.
- **Concurrency.** `routing.PROVIDER_SLOTS` (`adapters/semaphore_limiter.py`, one `BoundedSemaphore` per provider) is held around each attempt, escalations included. `overnight` with a routing file and `--concurrency` > 1 batches with `overnight.batch_repos_by_provider`: contiguous batches cut short where a provider's cap would be exceeded, from a no-health `RoutingService.first_choice` on each cached clone. Order is never changed, so the round-robin cursor stays a prefix count.
- **Pre-flight.** With a routing file, `overnight` checks that some provider trusted with `tend` can run here (`check_routing_ready`) instead of checking `GARDENER_HARNESS`, and an invalid routing file stops it before the batch with an alert.
- **CLI.** `gardener route --repo owner/name [--mode report] [--path DIR] [--no-health] [--no-ready]` explains a decision from the cached clone gardener already keeps. `python -m gardener.routing` does the same for any directory.
- **Code layout: ports and adapters.** `gardener/routing/` keeps the policy free of I/O so a new provider type, health contract or config store is one new file:
  - `model.py`: domain values; `Provider` carries only what routing reasons about, plus a `kind` and that kind's own `settings`.
  - `core.py`: `parse_config`, `route`, `run_with_escalation`; pure, written against the ports only.
  - `ports/`: one protocol per file: `ProviderKind`, `HealthProbe`, `Readiness`, `RepoProfiler`, `RoutingSource`, `ConcurrencyLimiter`, `AgentRunner`, `RoutingObserver`.
  - `adapters/`: one implementation per file: the `claude-code` and `command` kinds, `HttpHealthProbe` (the `{"ok", "paused"}` contract), `AssumeHealthy`, `FilesystemProfiler`, `JsonFileSource`, `SemaphoreLimiter`, `StderrObserver`, `NullObserver`, and `DispatchReadiness`/`DispatchRunner`, which only call `dispatch.check_harness_ready`/`run_agent`, so the safety checks stay in `dispatch.py`.
  - `service.py`: `RoutingService`, the composition root every caller uses.
  
  `tests/test_routing.py` fails if `model`, `core` or `ports` import an adapter. The move was checked against the single-module version on 600 (mode, repo, size, health, readiness) combinations: identical candidates, skip reasons, harness configs and escalation results. It made the file stricter in two ways: a provider key its kind doesn't own (e.g. a typo like `comand`) and an unknown `cost` are rejected, where they used to be ignored; and a health answer that isn't a JSON object counts as unreachable, where it used to raise.
- **Not done:** per-provider timeouts (still open above), cross-process slots, dashboard columns, `route --suggest` (phase 3).

### Manual verification (required before relying on it; CLAUDE.md's rule for dispatch changes)

Use a low-stakes small repo (FlyCommand-sized, tier S) and a large one (Medieval-Factions-sized, tier L), an empty merge allow-list, and the `local-gpu` provider configured as in §1 with its real `claude_bin` and `health_url`.

1. **Off is off.** With no `routing.json`, `gardener align --repo <small>`: stderr has no `gardener: routing:` line, and `gardener status` has no provider column.
2. **Explain.** Write `routing.json`. `gardener route --repo <small>` lists `1. local-gpu`, `2. claude-sub`; `gardener route --repo <large>` lists only `claude-sub`, with local-gpu skipped for tier/context; `--mode tend` on either lists only `claude-sub`.
3. **Routed report.** `gardener align --repo <small>`: stderr shows `routing <small> (report): … providers: local-gpu, claude-sub` and the run finishes on local-gpu. `git -C ~/.cache/gardener/repos/<owner>__<name> status --porcelain` is empty, `git log` has no new commit, `gh repo view <small> --json pushedAt` is unchanged. `gardener status --repo <small>` shows `local-gpu`, and `sqlite3 ~/.local/state/gardener/gardener.sqlite3 "select provider, escalated_from, cost_usd from runs order by id desc limit 1"` gives `local-gpu||` (cost NULL).
4. **Escalation.** Pause the self-hosted provider at its end, so `/health` reports paused: `gardener route --repo <small>` shows `skip local-gpu: paused`. Then make it pass its health probe but fail requests (pause it right after the probe, or point `claude_bin` at a stub wrapper that exits 75) and run `align` again: stderr shows `local-gpu ended with tempfail; escalating to claude-sub`, the record has `provider=claude-sub`, `escalated_from=local-gpu:tempfail` and a real cost, and the clone is still clean.
5. **Refusal.** Add `"<small>": {"local_only": true}` under `repos` and run `gardener tend --repo <small>` (no `--allow-merge`): nothing is dispatched, the run is recorded as `error` with `routing refused this run …`, and an alert fires. Remove the override.
6. **Concurrency.** Put two tier-S repos in the garden, temporarily give local-gpu `"modes": ["report", "tend"]` and a rule sending tend on tier S to it, and run `gardener overnight --hours 0.2 --concurrency 2`: the log shows the two repos in separate batches, the provider's own request log shows one request at a time, and each run is recorded with its own provider and notification. Restore the file.
7. **Tend on the subscription.** `gardener tend --repo <small>` (no `--allow-merge`) runs on `claude-sub`; no merge occurred (`gh pr list --state all`), and `permission_denials` is empty or only correctly out-of-scope attempts.

## RFC Checklist

- [x] Motivation grounded in observed runs (2026-11-20 gardener-on-a-self-hosted-model results)
- [x] Alternatives considered
- [x] Drawbacks, risks, open questions
- [x] Safety model unchanged (routing can't widen the `command` harness's scope; `local_only` refuses rather than leaks)
- [x] Prototype with tests (24, no network, no `claude`)
- [x] Phase 2 wiring + migration + per-provider concurrency + `gardener route` (`tests/test_routing_wiring.py`, no network, no `claude`)
- [ ] Phase 2 manual verification (steps in "Phase 2 as implemented")
- [ ] Decision on where this RFC lives and whether it gets a number

## Status and Next Steps

Draft for discussion. Phase 2 is implemented (see "Phase 2 as implemented"); its manual verification is still to run. Phase 1's prototype dry run on the real clones routed FlyCommand report → the local provider then claude-sub, FlyCommand tend → claude-sub, and Medieval-Factions report/tend → claude-sub. That matches what the 2026-11-20 runs showed each provider can handle.
