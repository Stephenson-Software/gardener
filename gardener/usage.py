"""Usage reporting: one `startup` event per invocation, sent to trace.

gardener is a service the operator hosts (a nightly job on the operator's
own devices), so it reports the way the other hosted services in this
ecosystem do: a single ``startup`` event tagged ``service=true`` and
``version``, once per invocation of the CLI, and nothing else. No repo
name, no device name, no hostname, no path, nothing from a run — the body
is the program name, the event name and those two tags, exactly what
:func:`startup_tags` returns. The trace operator page hides
``service=true`` events from its fleet view, which is the point of the tag.

Configuration follows the two-source precedence `notify.py` already
established for `GARDENER_DISCORD_WEBHOOK_URL`/`GARDENER_DEVICE_NAME`: an
environment variable wins, otherwise the same name in the
``$GARDENER_STATE_DIR/notify.env`` file (for the cron/Task Scheduler/`devsrv`
contexts where exporting an env var per invocation isn't practical), and a
built-in default when neither is set. No new config mechanism is invented.

    GARDENER_USAGE_REPORTING_ENABLED    default true; 0/false/no/off turns it off
    GARDENER_USAGE_REPORTING_ENDPOINT   default https://trace.danielstephenson.dev
    GARDENER_USAGE_REPORTING_KEY        default: the key issued for gardener

The key is a program identifier, not a secret that grants anything, which
is why it ships as a default in code rather than in the state directory —
an installation whose ``notify.env`` predates this module still reports.

The vendored client (`trace_client.py`) never raises and never blocks the
caller: reporting happens on one daemon thread, a trace server that is down
is a dropped event, and everything in this module is wrapped so that
reporting can never be why a gardener run fails. Nothing here ever writes
to stdout — `gardener ps -q` and friends print machine-readable output
there, and the client itself logs only at DEBUG on the ``trace`` logger.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Dict, Mapping, Optional

from gardener import __version__, notify
from gardener.trace_client import TraceClient

APPLICATION = "gardener"
DEFAULT_ENDPOINT = "https://trace.danielstephenson.dev"
DEFAULT_KEY = "eZuzFawyY-fjMPzLSARa6cfxgX_wRl-MMPNBnJ_aVDc"

ENV_ENABLED = "GARDENER_USAGE_REPORTING_ENABLED"
ENV_ENDPOINT = "GARDENER_USAGE_REPORTING_ENDPOINT"
ENV_KEY = "GARDENER_USAGE_REPORTING_KEY"

_FALSE = {"0", "false", "no", "off"}


def _read_config_file(config_path: Optional[Path]) -> Dict[str, str]:
    """The ``notify.env`` values, or nothing: a missing or unreadable file is
    the normal "not configured" state, never an error (same posture as
    `notify.load_webhook_url`)."""
    path = config_path or notify.default_webhook_config_path()
    try:
        if not path.is_file():
            return {}
        return notify._parse_env_style_file(path)
    except OSError as e:
        print(f"usage: could not read {path}: {e}", file=sys.stderr)
        return {}


def _setting(name: str, env: Optional[Mapping[str, str]], config_path: Optional[Path]) -> str:
    """Env var first, then the same name in ``notify.env``; blank at either
    level falls through to the next, so a blank value means "unset", not
    "set to nothing"."""
    source = os.environ if env is None else env
    value = (source.get(name) or "").strip()
    if value:
        return value
    return (_read_config_file(config_path).get(name) or "").strip()


def enabled(env: Optional[Mapping[str, str]] = None, config_path: Optional[Path] = None) -> bool:
    """Whether reporting is on. Unset means on; only an explicit no turns it off."""
    return _setting(ENV_ENABLED, env, config_path).lower() not in _FALSE


def endpoint(env: Optional[Mapping[str, str]] = None, config_path: Optional[Path] = None) -> str:
    return _setting(ENV_ENDPOINT, env, config_path) or DEFAULT_ENDPOINT


def key(env: Optional[Mapping[str, str]] = None, config_path: Optional[Path] = None) -> str:
    return _setting(ENV_KEY, env, config_path) or DEFAULT_KEY


def startup_tags() -> Dict[str, str]:
    """Everything a startup event carries besides its name."""
    return {"version": __version__, "service": "true"}


def build_client(env: Optional[Mapping[str, str]] = None, config_path: Optional[Path] = None) -> TraceClient:
    """A client configured from the environment/``notify.env``, or a no-op
    one when it is switched off. Building it cannot fail: any surprise
    yields the no-op."""
    try:
        return TraceClient(
            endpoint(env, config_path), APPLICATION,
            key=key(env, config_path), enabled=enabled(env, config_path),
        )
    except Exception:  # noqa: BLE001 - reporting must never be why gardener fails
        return TraceClient.disabled()


def start(env: Optional[Mapping[str, str]] = None, config_path: Optional[Path] = None) -> TraceClient:
    """Report that gardener started, and hand back the client so the caller
    can :func:`stop` it before the process exits."""
    client = build_client(env, config_path)
    try:
        client.report("startup", tags=startup_tags())
    except Exception:  # noqa: BLE001 - the client already promises this; belt and suspenders
        pass
    return client


def stop(client: TraceClient, timeout: float = TraceClient.TIMEOUT_SECONDS) -> None:
    """Close the client before the process exits, so a short run's event
    is sent rather than lost.

    Closing matters more here than in a long-running service: the sender
    is a daemon thread, so process exit would cut it off mid-request, and
    `gardener status`/`gardener ps` finish in milliseconds. The client's
    ``close`` (0.1.1+) gives whatever is still queued up to ``timeout``
    seconds in total to be sent, then stops the thread — so exit is delayed
    by at most the client's own timeout: an unreachable trace server is a
    request that times out, not a hang. Never raises, even on something
    that isn't a client.
    """
    try:
        client.close(timeout)
    except Exception:  # noqa: BLE001 - never let reporting be why gardener fails
        pass
