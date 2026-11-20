"""Usage reporting: one `startup` event per invocation, sent to trace.

gardener is a service the operator hosts (a nightly job on the operator's
own devices), so it reports the way the other hosted services in this
ecosystem do: a single ``startup`` event tagged ``service=true`` and
``version``, once per invocation of the CLI, plus the random installation
ID every event carries, and nothing else. No repo name, no device name, no
hostname, no path, nothing from a run — the body is the program name, the
event name and three tags: ``service`` from :func:`startup_tags`, and
``version`` and ``install``, which the client adds to every event. The trace
operator page hides ``service=true`` events from its fleet view, which is the
point of the tag.

The installation ID (tag ``install``) is a random UUID the client keeps in
``$GARDENER_STATE_DIR/trace-install-id`` (:func:`install_id_file`, next to the
run-history db), so trace can count installations — one per device —
rather than invocations. ``TRACE_INSTALL_ID`` in the environment pins one
instead. The client only reads or creates the file when reporting is on;
deleting it resets the ID.

Configuration follows the two-source precedence `notify.py` already
established for `GARDENER_DISCORD_WEBHOOK_URL`/`GARDENER_DEVICE_NAME`: an
environment variable wins, otherwise the same name in the
``$GARDENER_STATE_DIR/notify.env`` file (for the cron/Task Scheduler/`devsrv`
contexts where exporting an env var per invocation isn't practical), and a
built-in default when neither is set. No new config mechanism is invented.

    GARDENER_USAGE_REPORTING_ENABLED    default (unset) true; anything but
                                        true/on/yes/1 turns it off
    GARDENER_USAGE_REPORTING_ENDPOINT   default https://trace.danielstephenson.dev
    GARDENER_USAGE_REPORTING_KEY        default: the key issued for gardener

Two more variables, honoured by every trace client, turn it off as well:
``TRACE_USAGE_REPORTING=off`` and ``DO_NOT_TRACK=1``. They are environment
variables only (not ``notify.env`` keys) and are checked ahead of gardener's
own setting -- here in :func:`enabled` for whichever mapping is passed, and
again by the client's constructor against the process environment -- so
they win even when the setting says on.

Every switch fails closed, so an opt-out is never read as on: gardener's own
setting is read through the client's ``allows_reporting``, so once it is set
(in the environment or in ``notify.env``, even to a blank value) only
true/on/yes/1 keeps reporting on; and a ``notify.env`` that exists but cannot
be read turns reporting off unless the environment variable says on, since it
may hold the opt-out. So does an
opt-out written under another spelling (``GARDENER_USAGE_REPORTING=off``,
``GARDENER_USAGE_REPORTING_ENABLE=false``,
``gardener_usage_reporting_enabled=no``,
``GARDENER_USAGE_REPORTING_DISABLED=true``), in ``notify.env`` or the
environment -- see :func:`has_near_miss_opt_out`. Details:
https://danielstephenson.dev/usage-reporting

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
import re
import sys
from pathlib import Path
from typing import Dict, Mapping, Optional

from gardener import __version__, notify, state
from gardener.trace_client import TraceClient, allows_reporting, environment_opts_out

APPLICATION = "gardener"
DEFAULT_ENDPOINT = "https://trace.danielstephenson.dev"
DEFAULT_KEY = "eZuzFawyY-fjMPzLSARa6cfxgX_wRl-MMPNBnJ_aVDc"

ENV_ENABLED = "GARDENER_USAGE_REPORTING_ENABLED"
ENV_ENDPOINT = "GARDENER_USAGE_REPORTING_ENDPOINT"
ENV_KEY = "GARDENER_USAGE_REPORTING_KEY"
#: Pins the installation ID instead of the file. Environment only, like the
#: client-wide opt-outs, and only ever handed to the client as its explicit ID.
ENV_INSTALL_ID = "TRACE_INSTALL_ID"
INSTALL_ID_FILENAME = "trace-install-id"

def _read_config_file(config_path: Optional[Path]) -> Optional[Dict[str, str]]:
    """The ``notify.env`` values; nothing for a missing file, the normal
    "not configured" state; ``None`` for a file that is there but cannot be
    read -- it may hold the opt-out, so :func:`enabled` reads that as off
    (endpoint and key just fall back to their defaults)."""
    path = config_path or notify.default_webhook_config_path()
    try:
        if not path.is_file():
            return {}
        return notify._parse_env_style_file(path)
    except (OSError, ValueError) as e:
        print(f"usage: could not read {path}: {e}", file=sys.stderr)
        return None


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def has_near_miss_opt_out(values: Mapping[str, str], gardener_only: bool = False) -> bool:
    """Whether ``values`` hold an attempt to turn usage reporting off that is
    not spelled exactly ``GARDENER_USAGE_REPORTING_ENABLED``. Such a line
    would be ignored and the default, on, would stand. Whoever wrote one did
    not ask for reporting, so each counts as off -- a name that spells "usage
    reporting" (any case, any separators) and:

    - spells "enabled" another way too (``..._ENABLE``, ``gardener_usage_reporting_enabled``)
      with a value that is not clearly on;
    - spells "disabled" (``..._DISABLED``) with a value that is on;
    - is the bare name (``GARDENER_USAGE_REPORTING``, ``USAGE_REPORTING``)
      with a value that is not clearly on.

    ``GARDENER_USAGE_REPORTING_ENDPOINT`` / ``_KEY`` are never read as a
    switch. With ``gardener_only`` (the process environment, which also
    holds other programs' switches) only names that mention gardener count.
    """
    for name, value in values.items():
        if name in (ENV_ENABLED, ENV_ENDPOINT, ENV_KEY):
            continue  # read exactly, by enabled() / endpoint() / key()
        key = _normalise_key(name)
        if "usagereport" not in key or (gardener_only and "gardener" not in key):
            continue
        if "disabl" in key:
            if allows_reporting(value):
                return True
        elif "enabl" in key or key.endswith(("usagereport", "usagereporting")):
            if not allows_reporting(value):
                return True
    return False


def _setting(name: str, env: Optional[Mapping[str, str]], config_path: Optional[Path]) -> str:
    """Env var first, then the same name in ``notify.env``; blank at either
    level falls through to the next, so a blank value means "unset", not
    "set to nothing"."""
    source = os.environ if env is None else env
    value = (source.get(name) or "").strip()
    if value:
        return value
    return ((_read_config_file(config_path) or {}).get(name) or "").strip()


def enabled(env: Optional[Mapping[str, str]] = None, config_path: Optional[Path] = None) -> bool:
    """Whether reporting is on. Fails closed. The client-wide
    ``TRACE_USAGE_REPORTING`` / ``DO_NOT_TRACK`` in the environment are
    consulted first. Then gardener's own ``GARDENER_USAGE_REPORTING_ENABLED``:
    a non-blank env var wins, otherwise ``notify.env``; whichever value is
    found is read through ``allows_reporting``, so only true/on/yes/1 keeps
    reporting on. A ``notify.env`` that cannot be read is off, and so is the
    setting present but blank everywhere. An opt-out written under another
    spelling (:func:`has_near_miss_opt_out`) in either place is off and wins
    over the real setting. Only when it is set nowhere does the default, on,
    apply."""
    source = os.environ if env is None else env
    if environment_opts_out(source):
        return False
    # An opt-out written under another spelling wins over the real switch.
    if has_near_miss_opt_out(source, gardener_only=True):
        return False
    config = _read_config_file(config_path)
    if config is not None and has_near_miss_opt_out(config):
        return False
    from_env = (source.get(ENV_ENABLED) or "").strip()
    if from_env:
        return allows_reporting(from_env)
    if config is None:
        return False
    if ENV_ENABLED in config:
        return allows_reporting(config[ENV_ENABLED])
    return ENV_ENABLED not in source


def endpoint(env: Optional[Mapping[str, str]] = None, config_path: Optional[Path] = None) -> str:
    return _setting(ENV_ENDPOINT, env, config_path) or DEFAULT_ENDPOINT


def key(env: Optional[Mapping[str, str]] = None, config_path: Optional[Path] = None) -> str:
    return _setting(ENV_KEY, env, config_path) or DEFAULT_KEY


def install_id_file() -> Path:
    """Where the client keeps this installation's random ID: gardener's own
    per-user state dir (``$GARDENER_STATE_DIR``, default
    ``~/.local/state/gardener``), next to the run-history db and
    ``notify.env``. Each device has its own, as it has its own db."""
    return state.default_state_dir() / INSTALL_ID_FILENAME


def startup_tags() -> Dict[str, str]:
    """The tags a startup event carries besides ``version``, which the client
    adds to every event itself (see :func:`build_client`)."""
    return {"service": "true"}


def build_client(env: Optional[Mapping[str, str]] = None, config_path: Optional[Path] = None) -> TraceClient:
    """A client configured from the environment/``notify.env``, or a no-op
    one when it is switched off. Always built through the client's
    constructor, which puts the process environment's
    ``TRACE_USAGE_REPORTING`` / ``DO_NOT_TRACK`` ahead of everything else
    and records why it is off in ``disabled_reason``. Building it cannot
    fail: any surprise yields the no-op.

    The installation ID (``TRACE_INSTALL_ID`` from ``env``/the environment,
    else :func:`install_id_file`) is resolved by the client only after those
    checks, so a disabled client never creates the file."""
    source = os.environ if env is None else env
    try:
        return TraceClient(
            endpoint(env, config_path), APPLICATION, __version__ or "unknown",
            key=key(env, config_path), enabled=enabled(env, config_path),
            install_id=source.get(ENV_INSTALL_ID), install_id_file=install_id_file(),
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
    ``close`` (0.1.1+; 0.5.0 is vendored) gives whatever is still queued up to ``timeout``
    seconds in total to be sent, then stops the thread — so exit is delayed
    by at most the client's own timeout: an unreachable trace server is a
    request that times out, not a hang. Never raises, even on something
    that isn't a client.
    """
    try:
        client.close(timeout)
    except Exception:  # noqa: BLE001 - never let reporting be why gardener fails
        pass
