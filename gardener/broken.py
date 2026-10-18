"""Broken-first ordering for `overnight --strategy parpt`.

RICE says which repos matter most over time; it says nothing about which
one is broken tonight. A repo whose default branch is red, or whose
latest release was held by a release gate, goes to the front of the
night's order — ahead of RICE, and ahead of the resume cursor's "once
per cycle" rule, since waiting a whole cycle (weeks, on a big garden)
to look at a red build defeats the point.

Two guards keep a repo tend can't fix from eating every night:

- at most `DEFAULT_BROKEN_FIRST_LIMIT` repos jump the queue per night
  (highest RICE first among them), and
- a repo gardener already tended within `DEFAULT_BROKEN_COOLDOWN_DAYS`
  doesn't jump again; it waits for its normal turn.

Two sources, both best-effort (whatever can't be read is simply not
counted as broken):

- **CI**: the default branch head's combined check state, for every
  garden repo, from batched `gh api graphql` queries. `FAILURE` and
  `ERROR` count; pending and no-checks don't.
- **Release ledger** (``GARDENER_RELEASE_LEDGER``, the path to a
  quartermaster-style ``ledger.json``; env var, then ``notify.env``): a
  repo whose newest entry is ``blocked`` and was recorded within
  `LEDGER_MAX_AGE_DAYS`. ``failed`` isn't counted: there it was the gate
  run itself that couldn't be dispatched, which nothing in the repo fixes.

The parsers and the ordering are pure; `fetch_broken` is the only I/O.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from gardener import usage

ENV_RELEASE_LEDGER = "GARDENER_RELEASE_LEDGER"

DEFAULT_BROKEN_FIRST_LIMIT = 5
DEFAULT_BROKEN_COOLDOWN_DAYS = 3
LEDGER_MAX_AGE_DAYS = 14
CI_BATCH_SIZE = 50
RED_STATES = frozenset({"FAILURE", "ERROR"})

# repo -> one-line reason it counts as broken
Broken = dict[str, str]
Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _parse_time(value: object) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def ci_query(repos: list[str]) -> str:
    """One GraphQL query for every repo's default-branch check state,
    aliased `r0`, `r1`, ... in `repos` order."""
    parts = []
    for i, repo in enumerate(repos):
        owner, name = repo.split("/", 1)
        parts.append(
            f'r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) '
            "{ defaultBranchRef { name target { ... on Commit { statusCheckRollup { state } } } } }"
        )
    return "query { " + " ".join(parts) + " }"


def parse_ci_response(repos: list[str], response: object) -> Broken:
    """Red default branches from one `ci_query` answer. A repo GitHub
    couldn't resolve (renamed, no access) comes back null and is skipped."""
    data = (response or {}).get("data") if isinstance(response, dict) else None
    broken: Broken = {}
    for i, repo in enumerate(repos):
        node = (data or {}).get(f"r{i}") or {}
        ref = node.get("defaultBranchRef") or {}
        state = ((ref.get("target") or {}).get("statusCheckRollup") or {}).get("state")
        if state in RED_STATES:
            broken[repo] = f"CI is red on {ref.get('name') or 'the default branch'}"
    return broken


def parse_ledger(ledger: object, garden: list[str], now: datetime) -> Broken:
    """Repos whose newest ledger entry is a recent `blocked`."""
    entries = (ledger or {}).get("entries") if isinstance(ledger, dict) else None
    known = {repo.lower(): repo for repo in garden}
    newest: dict[str, dict] = {}
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and str(entry.get("repository", "")).lower() in known:
            newest[known[str(entry["repository"]).lower()]] = entry  # the ledger is append-only
    broken: Broken = {}
    cutoff = now - timedelta(days=LEDGER_MAX_AGE_DAYS)
    for repo, entry in newest.items():
        when = _parse_time(entry.get("recordedAt"))
        if entry.get("decision") == "blocked" and when is not None and when >= cutoff:
            broken[repo] = f"release {entry.get('version')} was held by a release gate"
    return broken


def broken_first(
    order: list[str],
    full_order: list[str],
    broken: Broken,
    last_tended: Mapping[str, Optional[str]],
    now: datetime,
    limit: int = DEFAULT_BROKEN_FIRST_LIMIT,
    cooldown_days: int = DEFAULT_BROKEN_COOLDOWN_DAYS,
) -> tuple[list[str], list[str]]:
    """`order` with up to `limit` broken repos moved to the front, taken
    in `full_order` (RICE) order and skipping any tended within the
    cooldown. Returns (new order, the repos that jumped)."""
    cutoff = now - timedelta(days=cooldown_days)
    jumped = []
    for repo in full_order:
        if len(jumped) >= limit:
            break
        if repo not in broken:
            continue
        tended = _parse_time(last_tended.get(repo)) if last_tended.get(repo) else None
        if tended is not None and tended >= cutoff:
            continue
        jumped.append(repo)
    return jumped + [r for r in order if r not in jumped], jumped


def _gh_graphql(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def fetch_broken(
    garden: list[str],
    now: datetime,
    env: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path] = None,
    run: Runner = _gh_graphql,
) -> Broken:
    """Every broken garden repo and why. Never raises."""
    broken: Broken = {}
    repos = [r for r in garden if r.count("/") == 1]
    for start in range(0, len(repos), CI_BATCH_SIZE):
        batch = repos[start:start + CI_BATCH_SIZE]
        try:
            res = run(["gh", "api", "graphql", "-f", f"query={ci_query(batch)}"])
            # gh exits non-zero when any alias failed to resolve, but still
            # prints the data for the rest — so parse stdout regardless.
            broken.update(parse_ci_response(batch, json.loads(res.stdout or "{}")))
        except (subprocess.TimeoutExpired, OSError, ValueError) as e:
            print(f"gardener: broken-first: CI check skipped for {len(batch)} repo(s) ({e})",
                  file=sys.stderr)
    ledger_path = usage._setting(ENV_RELEASE_LEDGER, env, config_path)
    if ledger_path:
        try:
            ledger = json.loads(Path(ledger_path).expanduser().read_text())
            for repo, reason in parse_ledger(ledger, garden, now).items():
                broken[repo] = f"{broken[repo]}; {reason}" if repo in broken else reason
        except (OSError, ValueError) as e:
            print(f"gardener: broken-first: release ledger skipped ({e})", file=sys.stderr)
    return broken
