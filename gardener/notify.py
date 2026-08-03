"""Pluggable outcome notifications for gardener — lets a run's result reach
someone without them actively running `gardener status` or watching
terminal output.

Mirrors a Discord-alerting convention used elsewhere in this ecosystem
(title/description/color Discord embed via a webhook URL,
silent no-op if unconfigured, never fails its caller) but in stdlib
Python, matching gardener's own stdlib-only rule (see gardener/CLAUDE.md).

This module intentionally has no idea what "align" or "tend" or any other
gardener subcommand's outcome *means* — it only knows how to *present* an
already-decided (title, message, level). Callers (cli.py) are responsible
for turning a `state.Run`-shaped outcome into that (title, message, level)
tuple; that's business logic and belongs there, not here (see
gardener/CLAUDE.md's "what belongs here vs. elsewhere" principle applied
to this module too — notify.py owns *how* to alert, not *when* or *why*).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import Optional


class Level(str, Enum):
    """A notifier maps these onto its own presentation (e.g. Discord embed
    colors). Callers pick one; notifiers never re-derive it."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class Notifier(ABC):
    """A notification sink. Implementations MUST NOT raise — a notifier
    failing (bad webhook, network error, ...) must never break the actual
    gardener run it's reporting on. Concrete implementations are
    responsible for catching their own errors; `notify()` should log and
    return, not propagate."""

    @abstractmethod
    def notify(self, title: str, message: str, level: Level = Level.INFO) -> None:
        raise NotImplementedError


class NullNotifier(Notifier):
    """The "nothing configured" state — a clean no-op rather than a special
    case callers need to check for."""

    def notify(self, title: str, message: str, level: Level = Level.INFO) -> None:
        return None


class CompositeNotifier(Notifier):
    """Fans one notification out to zero-to-many concrete notifiers. Each
    concrete notifier is already required to swallow its own errors; this
    adds a second layer so a bug in that contract can't take down the
    others in the list or bubble up to the caller."""

    def __init__(self, notifiers: list[Notifier]):
        self._notifiers = list(notifiers)

    def notify(self, title: str, message: str, level: Level = Level.INFO) -> None:
        for n in self._notifiers:
            try:
                n.notify(title, message, level)
            except Exception as e:  # noqa: BLE001 - defense in depth, see class docstring
                print(f"notify: {type(n).__name__} raised despite its no-raise contract: {e}", file=sys.stderr)


# Discord embed colors (decimal RGB). SUCCESS/ERROR match the exact values
# an existing monitoring convention elsewhere in this ecosystem already
# uses (a health-monitor script's recovery/failure alerts); WARNING
# matches that same convention's cert-check "expiring soon" alert color.
# INFO has no precedent there (those alerts are binary healthy/unhealthy)
# — Discord's own "blurple" was picked as a neutral, non-alarming default
# for it.
DISCORD_COLORS: dict[Level, int] = {
    Level.INFO: 3447003,
    Level.SUCCESS: 3066993,
    Level.WARNING: 16776960,
    Level.ERROR: 15158332,
}

DISCORD_WEBHOOK_ENV_VAR = "GARDENER_DISCORD_WEBHOOK_URL"


def _default_state_dir() -> Path:
    override = os.environ.get("GARDENER_STATE_DIR")
    return Path(override) if override else Path.home() / ".local" / "state" / "gardener"


def default_webhook_config_path() -> Path:
    """A gitignored, `.env`-style file next to gardener's sqlite state
    (`$GARDENER_STATE_DIR/notify.env`, same override gardener already uses
    for its run-history db) — for a persistent/cron context where setting
    an env var every invocation isn't practical. gardener never creates
    this file itself, only reads it."""
    return _default_state_dir() / "notify.env"


def _parse_env_style_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_webhook_url(config_path: Optional[Path] = None) -> Optional[str]:
    """`GARDENER_DISCORD_WEBHOOK_URL` env var wins if set (the simplest
    path, and consistent with gardener's other `GARDENER_*` env-var
    overrides in conventions.py/state.py). Otherwise falls back to
    `DISCORD_WEBHOOK_URL=...` in the notify.env file above, for a
    long-running/cron context where exporting an env var per-invocation
    isn't practical. Returns None (not raising) if neither is set or the
    file can't be read — "not configured" is a normal state, not an
    error."""
    env_val = os.environ.get(DISCORD_WEBHOOK_ENV_VAR)
    if env_val:
        return env_val.strip() or None

    path = config_path or default_webhook_config_path()
    if not path.is_file():
        return None
    try:
        return _parse_env_style_file(path).get("DISCORD_WEBHOOK_URL") or None
    except OSError as e:
        print(f"notify: could not read {path}: {e}", file=sys.stderr)
        return None


class DiscordNotifier(Notifier):
    """Posts a Discord embed via a webhook URL. stdlib `urllib.request`
    only — no `requests` dependency, matching gardener's stdlib-only
    convention. Mirrors the same alerting convention referenced above:
    silent no-op if no webhook is configured, and never raises even if
    the POST itself fails
    (bad webhook, network error, Discord outage, ...) — see the `Notifier`
    base class docstring for why that contract matters here specifically:
    an alert about a gardener run must never be able to break that run."""

    def __init__(self, webhook_url: Optional[str] = None, timeout: float = 10.0):
        self._webhook_url = webhook_url if webhook_url is not None else load_webhook_url()
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._webhook_url)

    def notify(self, title: str, message: str, level: Level = Level.INFO) -> None:
        if not self._webhook_url:
            print(f"notify: no {DISCORD_WEBHOOK_ENV_VAR} configured — skipping alert: {title}", file=sys.stderr)
            return

        payload = json.dumps(
            {
                "username": "gardener",
                "embeds": [
                    {
                        "title": title,
                        "description": message,
                        "color": DISCORD_COLORS.get(level, DISCORD_COLORS[Level.INFO]),
                    }
                ],
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self._webhook_url,
            data=payload,
            # Discord's edge (Cloudflare) 403s requests carrying urllib's
            # default `Python-urllib/x.y` User-Agent — confirmed directly:
            # the identical payload succeeded via curl and failed via
            # urlopen with no other difference. Any non-default UA clears
            # it; this one just says what's actually posting.
            headers={"Content-Type": "application/json", "User-Agent": "gardener-discord-notifier/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                status = getattr(resp, "status", 200)
                if status >= 400:
                    print(f"notify: Discord webhook returned HTTP {status} for: {title}", file=sys.stderr)
                else:
                    print(f"notify: sent to Discord: {title}", file=sys.stderr)
        except (urllib.error.URLError, OSError, ValueError) as e:
            # Covers HTTPError (subclass of URLError), connection failures,
            # timeouts, and a malformed webhook URL — never propagate.
            print(f"notify: FAILED to send to Discord: {title}: {e}", file=sys.stderr)


def default_notifier() -> Notifier:
    """The notifier gardener's CLI uses unless told otherwise: Discord if a
    webhook is configured, a clean no-op otherwise. `CompositeNotifier`
    exists for anyone who wants to register additional concrete notifiers
    later without changing any call site that already uses this."""
    discord = DiscordNotifier()
    if discord.configured:
        return discord
    return NullNotifier()
