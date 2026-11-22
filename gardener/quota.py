"""Whether Claude can be used right now, and how long to wait when it can't.

`cmd_overnight` used to find out that the subscription was unusable the
expensive way: by dispatching a tend into every repo in the garden. Each
one failed in about two seconds, at no cost, and each one sent its own
"tend: FAILED" alert. On 2026-11-18 that was 231 failures and 232 Discord
alerts, 70 of which Discord refused with HTTP 429, because the failure
wording ("Claude Code can't use your Claude subscription") was not one the
abort check knew.

This module holds the three pieces that stop that happening:

- `probe` makes one tiny read-only report-mode call through `run_agent` before the night starts,
  so an unusable subscription, an exhausted usage window or a broken login
  is found once, not once per repo.
- `parse_reset_time` reads the reset time out of a usage-limit message
  ("You've hit your session limit · resets 12am (UTC)").
- `seconds_to_wait` decides whether waiting for that reset still leaves
  room in the night's budget for at least one tend.

All three are pure or take their side effects as arguments, so the tests
never call `claude` and never sleep.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9 has no zoneinfo
    ZoneInfo = None  # type: ignore[assignment]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

from gardener.dispatch import (
    Mode,
    is_device_global_failure,
    looks_like_usage_limit,
    run_agent,
)

#: The probe's prompt. Read-only report mode, one word back: the point is
#: only to learn whether the CLI will run at all.
PROBE_PROMPT = "Reply with the single word OK. Do not use any tools."

#: Generous for a one-word reply; a probe that times out is treated as
#: "unknown", not as "blocked" (see `ProbeResult`).
PROBE_TIMEOUT_SECONDS = 180

#: Added to a parsed reset time before the next try. The CLI rounds the
#: reset to the hour in its message, and starting a minute early only buys
#: another failure.
RESET_MARGIN_SECONDS = 120

_MONTHS = {
    name: i
    for i, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}

# "resets 12am", "resets 3:30pm (UTC)", "resets Nov 25, 5am (America/Denver)",
# "resets Nov 25 at 5am". The wording is not a stable contract, so the
# pattern is loose and anything it can't read means "no known reset time".
_RESET_RE = re.compile(
    r"resets\s+(?:at\s+)?"
    r"(?:(?P<mon>[a-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)

# The older form: "Claude AI usage limit reached|1795052965" (epoch seconds).
_EPOCH_RE = re.compile(r"limit reached\|(?P<epoch>\d{9,11})", re.IGNORECASE)


@dataclass(frozen=True)
class ProbeResult:
    """What one probe found.

    `ok` — Claude answered; go ahead.
    `blocked` — it failed for a device-wide reason (`is_device_global_failure`):
    the subscription can't be used, the usage window is spent, or the login
    is broken. Every tend tonight would fail the same way.
    Neither — it failed for some other reason (a timeout, an odd exit). The
    caller should go ahead: a flaky probe must not cost a whole night, and the
    batch loop still aborts on a real device-wide failure.
    """

    ok: bool
    blocked: bool
    text: str


def probe(
    harness_config,
    model: Optional[str] = None,
    timeout: int = PROBE_TIMEOUT_SECONDS,
    runner: Callable = run_agent,
) -> ProbeResult:
    """Make one tiny read-only report-mode call on the configured harness
    and classify it. Never raises. No `repo` is passed, so a routing file
    never applies: this asks about the base harness only."""
    try:
        with tempfile.TemporaryDirectory(prefix="gardener-probe-") as tmp:
            result = runner(
                Mode.REPORT,
                PROBE_PROMPT,
                Path(tmp),
                model=model,
                timeout=timeout,
                harness_config=harness_config,
            )
    except Exception as e:  # noqa: BLE001 - a probe that can't run is "unknown", not fatal
        return ProbeResult(ok=False, blocked=False, text=f"probe could not run: {e}")
    text = "\n".join(part for part in (result.result_text or "", result.stderr or "") if part).strip()
    if result.ok:
        return ProbeResult(ok=True, blocked=False, text=text)
    blocked = bool(result.blocked) or is_device_global_failure(result.result_text or "", result.stderr or "")
    return ProbeResult(ok=False, blocked=blocked, text=text or "no output")


def _zone(name: Optional[str], fallback: tzinfo) -> tzinfo:
    if not name:
        return fallback
    name = name.strip()
    if name.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    if ZoneInfo is None:
        return fallback
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return fallback


def parse_reset_time(text: str, now: datetime) -> Optional[datetime]:
    """The reset time a usage-limit message names, as an aware datetime, or
    None when the text names none this can read.

    `now` must be timezone-aware; its zone stands in for a message that
    gives no zone of its own. A time with no date means its next occurrence
    after `now`; a dated one that has already passed this year means next
    year's.
    """
    if not text:
        return None
    epoch = _EPOCH_RE.search(text)
    if epoch:
        return datetime.fromtimestamp(int(epoch.group("epoch")), tz=timezone.utc)
    match = _RESET_RE.search(text)
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    if not (1 <= hour <= 12 and 0 <= minute <= 59):
        return None
    hour = hour % 12 + (12 if match.group("ampm").lower() == "pm" else 0)
    zone = _zone(match.group("tz"), now.tzinfo or timezone.utc)
    local_now = now.astimezone(zone)

    mon = match.group("mon")
    if mon:
        month = _MONTHS.get(mon.lower()[:3])
        if month is None:
            return None
        day = int(match.group("day"))
        for year in (local_now.year, local_now.year + 1):
            try:
                candidate = datetime(year, month, day, hour, minute, tzinfo=zone)
            except ValueError:
                return None
            # A date more than a day in the past is next year's (a message
            # read on Dec 31 naming "Jan 1"); a few hours stale is still now.
            if candidate >= local_now - timedelta(days=1):
                return candidate
        return None

    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def seconds_to_wait(
    text: str,
    now: datetime,
    remaining_budget_seconds: float,
    tend_timeout: float,
    margin_seconds: float = RESET_MARGIN_SECONDS,
) -> Optional[float]:
    """How long to sleep before trying again, or None when waiting is not
    worth it: the failure isn't a usage limit, its message names no reset
    time this can read, or the wait would leave less than one tend's
    timeout of budget behind it."""
    if not looks_like_usage_limit(text, ""):
        return None
    reset = parse_reset_time(text, now)
    if reset is None:
        return None
    wait = max(0.0, (reset - now).total_seconds()) + margin_seconds
    if wait + tend_timeout > remaining_budget_seconds:
        return None
    return wait
