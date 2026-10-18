"""Real-usage signals for Parpt scoring's reach questions.

Without these, a scoring dispatch judges reach from GitHub stars, which
for a hobby fleet say almost nothing (most repos have 0–5 whatever their
use). Three optional sources say much more, and all three name their
repos, so no hand-kept mapping is needed:

- **trace** (``GARDENER_REACH_TRACE_URL``, a trace base URL): the public
  ``/api/public/summary`` — startup events per program from real
  installations, with test servers, the author's own services and page
  views already left out. A program is matched to the garden repo whose
  name is the same once case and punctuation are dropped
  (``MedievalFactions`` ↔ ``Medieval-Factions``), and only when exactly
  one garden repo matches.
- **SpigotMC** (``GARDENER_REACH_SPIGET_AUTHOR``, a SpigotMC author id):
  every resource of that author via the Spiget API, with its download
  count, matched by the resource's source-code link.
- **dansplugins.com-style plugin API** (``GARDENER_REACH_DPC_API_URL``):
  ``/api/v1/plugins`` (matched by each plugin's ``githubUrl``) and each
  plugin's ``/downloads``.

Each setting is read from the environment, then ``notify.env`` (the same
precedence ``usage.py`` uses); unset means that source is skipped. There
are no built-in defaults — these are one owner's services. Every fetch is
best-effort: a source that is down or answers garbage is left out with
one stderr line, and scoring goes on without it.

The parse functions are pure (JSON in, facts out) so they're tested
without a network; ``fetch_usage_facts`` is the only part that does I/O.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Mapping, Optional

from gardener import __version__, usage

ENV_TRACE_URL = "GARDENER_REACH_TRACE_URL"
ENV_SPIGET_AUTHOR = "GARDENER_REACH_SPIGET_AUTHOR"
ENV_DPC_API_URL = "GARDENER_REACH_DPC_API_URL"

SPIGET_API = "https://api.spiget.org/v2"
DEFAULT_TIMEOUT_SECONDS = 15

# repo ("owner/name", as the garden spells it) -> {fact label: text}
UsageFacts = dict[str, dict[str, str]]
Fetcher = Callable[[str], object]

_GITHUB_RE = re.compile(r"github\.com/([^/\s]+)/([^/\s#?]+)", re.IGNORECASE)


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def github_repo(url: Optional[str]) -> Optional[str]:
    """``owner/name`` from a GitHub URL, or None."""
    m = _GITHUB_RE.search(url or "")
    if not m:
        return None
    name = m.group(2)
    if name.endswith(".git"):
        name = name[:-4]
    return f"{m.group(1)}/{name}"


def _by_full_name(garden: list[str]) -> dict[str, str]:
    return {repo.lower(): repo for repo in garden}


def parse_trace_summary(summary: object, garden: list[str]) -> UsageFacts:
    """Facts from trace's ``/api/public/summary`` (a list of
    ``{application, count, lastSeen}``)."""
    by_name: dict[str, list[str]] = {}
    for repo in garden:
        by_name.setdefault(_normalize(repo.split("/")[-1]), []).append(repo)
    facts: UsageFacts = {}
    for row in summary if isinstance(summary, list) else []:
        if not isinstance(row, dict):
            continue
        matches = by_name.get(_normalize(str(row.get("application", ""))), [])
        count = row.get("count")
        if len(matches) != 1 or not isinstance(count, int):
            continue
        text = (f"{count} startups reported from real installations (all time; "
                "test servers and the owner's own services excluded)")
        if row.get("lastSeen"):
            text += f", last seen {row['lastSeen']}"
        facts.setdefault(matches[0], {})["trace usage"] = text
    return facts


def parse_spiget_resources(resources: object, garden: list[str]) -> UsageFacts:
    """Facts from Spiget's author-resources listing."""
    known = _by_full_name(garden)
    facts: UsageFacts = {}
    for res in resources if isinstance(resources, list) else []:
        if not isinstance(res, dict):
            continue
        repo = known.get((github_repo(res.get("sourceCodeLink")) or "").lower())
        if repo is None:
            continue
        downloads = res.get("downloads")
        text = f"listed (resource {res.get('id')})"
        if isinstance(downloads, int):
            text += f", {downloads} downloads"
        facts.setdefault(repo, {})["SpigotMC"] = text
    return facts


def parse_dpc_plugins(plugins: object, garden: list[str]) -> dict[str, str]:
    """``{repo: slug}`` for each listed plugin whose GitHub repo is in the garden."""
    known = _by_full_name(garden)
    slugs: dict[str, str] = {}
    for plugin in plugins if isinstance(plugins, list) else []:
        if not isinstance(plugin, dict) or not plugin.get("slug"):
            continue
        repo = known.get((github_repo(plugin.get("githubUrl")) or "").lower())
        if repo is not None:
            slugs[repo] = str(plugin["slug"])
    return slugs


def describe_dpc_downloads(downloads: object) -> str:
    """The fact text for one plugin's ``/downloads`` answer (or None's)."""
    text = "listed"
    if isinstance(downloads, dict) and isinstance(downloads.get("total"), int):
        text += f", {downloads['total']} downloads through the site"
        if downloads.get("latestTag") and isinstance(downloads.get("latest"), int):
            text += f" ({downloads['latest']} of the latest release {downloads['latestTag']})"
    return text


def _get_json(url: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> object:
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": f"gardener/{__version__}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _merge(into: UsageFacts, more: UsageFacts) -> None:
    for repo, facts in more.items():
        into.setdefault(repo, {}).update(facts)


def fetch_usage_facts(
    garden: list[str],
    env: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path] = None,
    get_json: Fetcher = _get_json,
) -> UsageFacts:
    """Every configured source's facts for `garden`, fetched once per
    scoring run. Never raises."""
    facts: UsageFacts = {}

    def attempt(label: str, fn: Callable[[], UsageFacts]) -> None:
        try:
            _merge(facts, fn())
        except Exception as e:  # network, HTTP, JSON — all just "skip this source"
            print(f"gardener: usage signals: {label} skipped ({e})", file=sys.stderr)

    trace_url = usage._setting(ENV_TRACE_URL, env, config_path)
    if trace_url:
        attempt("trace", lambda: parse_trace_summary(
            get_json(trace_url.rstrip("/") + "/api/public/summary"), garden))

    author = usage._setting(ENV_SPIGET_AUTHOR, env, config_path)
    if author:
        attempt("SpigotMC", lambda: parse_spiget_resources(get_json(
            f"{SPIGET_API}/authors/{urllib.parse.quote(author)}/resources"
            "?size=500&fields=id,name,downloads,sourceCodeLink"), garden))

    dpc_url = usage._setting(ENV_DPC_API_URL, env, config_path)
    if dpc_url:
        base = dpc_url.rstrip("/") + "/api/v1/plugins"

        def dpc() -> UsageFacts:
            out: UsageFacts = {}
            for repo, slug in parse_dpc_plugins(get_json(base), garden).items():
                try:
                    downloads = get_json(f"{base}/{urllib.parse.quote(slug)}/downloads")
                except Exception:
                    downloads = None
                out[repo] = {"dansplugins.com": describe_dpc_downloads(downloads)}
            return out

        attempt("plugin site", dpc)

    return facts
