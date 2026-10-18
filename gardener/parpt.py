"""Autonomous Parpt scoring of garden repos, and the `parpt` overnight
strategy built on it.

[Parpt](https://github.com/Stephenson-Software/Parpt) (MIT) scores a
project on five 1-5 dimensions — impact, confidence, ease, reach, effort —
by asking four questions per dimension and rounding the mean of the
answers. It then ranks projects by RICE (reach * impact * confidence /
effort). gardener reuses that framework so the overnight run can tend the
highest-value repos first, but answers the questions itself: a short,
read-only `report`-mode dispatch per repo reads the clone (plus a few facts
gardener fetches from `gh` up front) and replies with one JSON object.
Nobody has to fill the questionnaire in by hand.

Two files live under the state dir:

- `parpt_scores.json` — gardener's own record, keyed by repo: the five
  scores, every per-question answer, the agent's one-line reasons, when it
  was scored, and what the scoring run cost. This is what `--strategy parpt`
  reads.
- `parpt/projects.json` — the same scores in Parpt's own on-disk format
  (only the seven `Project` fields, since Parpt's default ObjectMapper
  rejects unknown properties), rewritten after every scoring run, so
  `java -Dapp.projects.file=<state>/parpt/projects.json -jar parpt.jar`
  lists and views gardener's scores in Parpt itself.

Everything in this module is pure (no `gh`, no `claude`) so it stays
unit-testable; `cli.py`'s `score_repo` does the fetching and dispatching.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Parpt's own questionnaire, verbatim from its application.yaml, in its
# order. Kept identical on purpose: a gardener score and a hand-entered
# Parpt score mean the same thing only if they answer the same questions.
QUESTIONS: dict[str, tuple[str, ...]] = {
    "impact": (
        "Will this make/save money? (1=tiny impact, 5=huge impact)",
        "Will this make users happier? (1=slightly, 5=dramatically)",
        "Will this give us an edge over competitors? (1=small edge, 5=game-changing)",
        "Will this solve any major problems? (1=minor issues, 5=critical problems)",
    ),
    "confidence": (
        "Do we understand what needs to be built? (1=very unclear, 5=crystal clear)",
        "Have we done something similar before? (1=never, 5=many times)",
        "Do we have the right skills in the team? (1=missing key skills, 5=perfect fit)",
        "Are the requirements likely to change? (1=constantly changing, 5=very stable)",
    ),
    "ease": (
        "How straightforward is this to build? (1=very complex, 5=very simple)",
        "Do we have the tools we need? (1=need many new tools, 5=have everything)",
        "Can we reuse existing code/systems? (1=start from scratch, 5=mostly reusable)",
        "How easy is it to test? (1=very difficult, 5=very easy)",
    ),
    "reach": (
        "How many users will this help? (1=very few, 5=almost all)",
        "Will this attract new users? (1=unlikely, 5=definitely)",
        "Will users notice this change? (1=barely noticeable, 5=very visible)",
        "How often will users benefit from this? (1=rarely, 5=daily)",
    ),
    "effort": (
        "How long will this take to build? (1=very quick, 5=very long)",
        "How many people need to be involved? (1=just a few people, 5=many teams)",
        "Will this be hard to maintain? (1=very easy, 5=very difficult)",
        "Does this need ongoing work? (1=set and forget, 5=lots of upkeep)",
    ),
}
DIMENSIONS: tuple[str, ...] = tuple(QUESTIONS)

# A score older than this is re-scored before it is trusted again: a repo's
# issues, usage and code all move, and a months-old ranking would keep
# tending (or neglecting) a repo for reasons that no longer hold.
DEFAULT_STALE_DAYS = 30

# How many repos `overnight --strategy parpt` scores before ordering. Each
# is a short read-only dispatch, but they come out of the same night's
# budget as the tends, so the backlog is worked off a few repos per night
# (or all at once with `gardener score --all`).
DEFAULT_RESCORE_LIMIT = 5

# A scoring run reads, it doesn't build: far shorter than a tend.
DEFAULT_SCORE_TIMEOUT_SECONDS = 600


def default_scores_path() -> Path:
    override = os.environ.get("GARDENER_STATE_DIR")
    base = Path(override) if override else Path.home() / ".local" / "state" / "gardener"
    return base / "parpt_scores.json"


def default_parpt_projects_path(scores_path: Optional[Path] = None) -> Path:
    return (scores_path or default_scores_path()).parent / "parpt" / "projects.json"


def default_priorities_path() -> Path:
    return default_scores_path().parent / "parpt_priorities.md"


def load_priorities(path: Optional[Path] = None) -> str:
    """The operator's priorities file, or "" when there is none. Parpt's
    questions measure business value ("will this make money", "an edge over
    competitors"), so without the owner's own goals a scoring run can only
    judge a hobby project by its GitHub stars — the first full backfill
    ranked the operator's flagship project 111th of 126 for exactly that
    reason. This file is where those goals go: free text, owner-written,
    read into every scoring prompt."""
    try:
        return (path or default_priorities_path()).read_text().strip()
    except OSError:
        return ""


def priorities_digest(priorities: str) -> str:
    """A short fingerprint of the priorities text, stored with each score
    so editing the file marks every score made under the old text stale."""
    return hashlib.sha256(priorities.encode()).hexdigest()[:12] if priorities else ""


def parpt_round(answers: list[int]) -> int:
    """The mean of a dimension's answers, rounded exactly the way Parpt's
    `Math.round(float)` does (half up), not Python's half-to-even `round`
    — 2.5 must become 3 here, as it does in Parpt."""
    return int(math.floor(sum(answers) / len(answers) + 0.5))


def rice(scores: dict) -> Optional[float]:
    """Parpt's RICE formula. None for a record without a positive effort
    (Parpt would divide by zero; gardener treats it as unscored)."""
    try:
        effort = scores["effort"]
        if effort <= 0:
            return None
        return scores["reach"] * scores["impact"] * scores["confidence"] / effort
    except (KeyError, TypeError):
        return None


def ice(scores: dict) -> Optional[float]:
    try:
        return float(scores["impact"] * scores["confidence"] * scores["ease"])
    except (KeyError, TypeError):
        return None


def build_score_prompt(
    repo: str, facts: dict, priorities: str = "", usage: Optional[dict] = None,
) -> str:
    """The prompt for one read-only scoring dispatch. `facts` is whatever
    `cli.py` fetched up front (stars, open issues, last push, ...), since
    report mode has no shell and can't run `gh` itself. `usage` is this
    repo's real-usage signals from `reach.py` (trace installs, SpigotMC and
    plugin-site downloads), when any source knows it."""
    lines = [
        f"You are scoring the repository {repo} with the Parpt prioritization "
        "questionnaire, to decide how early an automated maintenance agent "
        "should work on it. The current directory is a clone of it. Read "
        "whatever you need (README, docs, source, tests, build files) — you "
        "cannot and must not modify anything.",
        "",
        "Read each question as being about continued work on this "
        "project as a whole: \"this\" is the project, \"users\" are the people "
        "who use it, and \"the team\" is one maintainer working with an AI "
        "coding agent.",
        "",
        "Facts gathered from GitHub:",
    ]
    if facts:
        lines += [f"- {k}: {v}" for k, v in facts.items()]
    else:
        lines.append("- (unavailable)")
    if usage:
        lines += [
            "",
            "Real usage, from the owner's own telemetry and download "
            "listings. This is the best evidence of reach there is; weigh "
            "it well above stars:",
        ]
        lines += [f"- {k}: {v}" for k, v in usage.items()]
    if priorities:
        lines += [
            "",
            "The owner's priorities. Judge value, reach and impact by these "
            "goals first; GitHub popularity is only a weak signal next to them:",
            "",
            priorities,
        ]
    lines += ["", "Answer every question with an integer from 1 to 5:"]
    for dim in DIMENSIONS:
        lines.append(f"{dim}:")
        lines += [f"  {i + 1}. {q}" for i, q in enumerate(QUESTIONS[dim])]
    lines += [
        "",
        "Reply with ONLY these ten lines, nothing before or after them:",
        *[f"{dim}: <four answers in question order, space-separated>" for dim in DIMENSIONS],
        *[f"{dim} reason: <one sentence naming the evidence>" for dim in DIMENSIONS],
        "For example: `reach: 2 1 3 2` and `reach reason: 4 stars and no releases, so few users.`",
    ]
    return "\n".join(lines)


class ScoreParseError(ValueError):
    pass


_DIM_ALT = "|".join(DIMENSIONS)
# `impact: 1 2 1 2` (commas, brackets or markdown bold around it tolerated)
_ANSWERS_RE = re.compile(
    rf"^[\s*`-]*({_DIM_ALT})[*`]*\s*:\s*\[?\s*(\d)\s*[, ]\s*(\d)\s*[, ]\s*(\d)\s*[, ]\s*(\d)\s*\]?[\s*`.]*$",
    re.IGNORECASE | re.MULTILINE,
)
_REASON_RE = re.compile(rf"^[\s*`-]*({_DIM_ALT})\s+reason[*`]*\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_score_reply(text: str) -> dict:
    """Validate an agent reply into `{"answers": {dim: [4 ints]}, "scores":
    {dim: int}, "reasons": {dim: str}}`. Raises ScoreParseError on anything
    malformed — a half-valid reply is not stored, since a wrong score would
    silently reorder the garden for a month.

    The reply is ten plain lines (`impact: 1 2 1 2`, `impact reason: ...`),
    not JSON: in the first full-garden backfill about a quarter of JSON
    replies were malformed (a reason under the wrong key, a stray brace),
    because free prose inside a JSON string is exactly what a model gets
    wrong. A line per answer has nothing to unbalance."""
    text = text or ""
    answers: dict[str, list[int]] = {}
    for m in _ANSWERS_RE.finditer(text):
        dim = m.group(1).lower()
        if dim in answers:
            raise ScoreParseError(f"{dim} answered more than once")
        answers[dim] = [int(g) for g in m.groups()[1:]]
    for dim in DIMENSIONS:
        values = answers.get(dim)
        if values is None:
            raise ScoreParseError(f"no `{dim}: a b c d` line in the reply")
        if not all(1 <= v <= 5 for v in values):
            raise ScoreParseError(f"{dim} answers must be 1-5, got {values!r}")
    reasons = {dim: "" for dim in DIMENSIONS}
    for m in _REASON_RE.finditer(text):
        reasons[m.group(1).lower()] = m.group(2).strip()
    return {
        "answers": answers,
        "scores": {dim: parpt_round(answers[dim]) for dim in DIMENSIONS},
        "reasons": reasons,
    }


def load_scores(path: Optional[Path] = None) -> dict[str, dict]:
    """Every stored record, keyed by repo. `{}` for a missing or corrupt
    file — an unreadable score file means "nothing scored yet", which the
    strategy already handles, not a reason to abort the night."""
    path = path or default_scores_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.get("repos", {}).items() if isinstance(v, dict)}


def save_score(repo: str, record: dict, path: Optional[Path] = None) -> None:
    """Store one repo's record, then regenerate Parpt's projects file from
    the full set. Both writes go through a temp file + rename so a run
    killed mid-write can't leave either file truncated."""
    path = path or default_scores_path()
    records = load_scores(path)
    records[repo] = record
    _write_json(path, {"repos": dict(sorted(records.items()))})
    _write_json(default_parpt_projects_path(path), to_parpt_projects(records))


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def to_parpt_projects(records: dict[str, dict]) -> list[dict]:
    """Records in Parpt's `Project` JSON shape — exactly its seven fields."""
    projects = []
    for repo, record in sorted(records.items()):
        scores = record.get("scores") or {}
        if rice(scores) is None:
            continue
        reasons = record.get("reasons") or {}
        description = " ".join(
            f"{dim.capitalize()}: {reasons[dim]}" for dim in DIMENSIONS if reasons.get(dim)
        ) or f"Scored by gardener on {record.get('scored_at', '?')}."
        projects.append({
            "name": repo,
            "description": description,
            **{dim: scores[dim] for dim in DIMENSIONS},
        })
    return projects


def _parse_time(value) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def repos_needing_score(
    garden: list[str],
    records: dict[str, dict],
    limit: int,
    now: Optional[datetime] = None,
    stale_days: int = DEFAULT_STALE_DAYS,
    digest: Optional[str] = None,
) -> list[str]:
    """Up to `limit` garden repos to (re)score: never-scored ones first
    (alphabetically), then stale ones oldest-first. A record with an
    unreadable timestamp or no usable RICE counts as never scored. With
    `digest` (the current `priorities_digest`), a record scored under
    different priorities text is stale too, however recent."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=stale_days)
    missing: list[str] = []
    stale: list[tuple[datetime, str]] = []
    for repo in sorted(garden):
        record = records.get(repo)
        scored_at = _parse_time(record.get("scored_at")) if record else None
        if record is None or scored_at is None or rice(record.get("scores") or {}) is None:
            missing.append(repo)
        elif scored_at < cutoff or (digest is not None and record.get("priorities_digest", "") != digest):
            stale.append((scored_at, repo))
    ordered = missing + [repo for _, repo in sorted(stale)]
    return ordered[: max(limit, 0)]


def order_by_parpt_score(garden: list[str], records: dict[str, dict]) -> list[str]:
    """`parpt` strategy: `garden` sorted descending by RICE. An unscored
    repo sorts last (the same "missing is lowest priority" stance as
    `overnight.order_by_issue_count`); ICE breaks RICE ties, then the repo
    name, so the order is deterministic. Every repo still gets tended once
    per cycle — `overnight.resume_order` guarantees that — so the score only
    decides who goes first, never who gets skipped."""

    def key(repo: str):
        scores = (records.get(repo) or {}).get("scores") or {}
        r = rice(scores)
        i = ice(scores)
        return (r is None, -(r or 0.0), -(i or 0.0), repo)

    return sorted(garden, key=key)


@dataclass(frozen=True)
class ScoreOutcome:
    repo: str
    ok: bool
    message: str
    record: Optional[dict] = None
