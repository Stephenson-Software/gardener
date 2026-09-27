"""Live-state heartbeats from a dispatching device to the hub (RFC 0010).

While a dispatching command (`align`, `tend`, `overnight`) runs on a device
with a hub configured, a daemon thread sends the hub a small snapshot of
what the device is doing now: the repos in flight, the batch, the overnight
budget, and one entry per slot. It sends one immediately, then one every
`GARDENER_HUB_HEARTBEAT_SECONDS` (default 60; 0 turns them off), and a
final one marked `ending` when the command exits. The hub keeps only the
latest per device and labels it live, stale, or idle by its age
(`hub.live_device_states`).

Three rules here are load-bearing:

- **Nothing from a log or transcript leaves the device.** The snapshot is
  built from the same functions the local dashboard uses
  (`dashboard.progress_from_logs`, `live.build_live`), then reduced to
  structured fields: repo names, phases, times, and flags. No log lines,
  tool inputs, model text, or local paths (owner decision, 2026-09-27).
  The hub also drops anything it doesn't name (`hub.heartbeat_from_wire`).
- **It never affects the dispatch.** Everything runs on the thread or is
  caught. A failed beat is dropped, never queued: a heartbeat is only worth
  anything while it is current, and the next one replaces it. Failures
  back off (doubling, up to `MAX_BACKOFF_SECONDS`) and print one `NOTE`
  per session.
- **An older hub turns it off quietly.** A hub without the route answers
  404, and the thread stops for the rest of the session with one `NOTE`,
  so upgrading a device before its hub is harmless.
"""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from gardener import __version__, dashboard, hub, live, sessions, state

MAX_BACKOFF_SECONDS = 600
#: The final `ending` beat is sent on the way out of the command, so it gets
#: one short try: a dispatch must not wait on an unreachable hub to exit.
FINAL_BEAT_TIMEOUT_SECONDS = 3.0


def build_snapshot(
    session: sessions.Session,
    seq: int,
    interval_seconds: int,
    ending: bool = False,
    state_dir: Optional[Path] = None,
) -> dict:
    """The heartbeat body: RFC 0010 §1's fields, from this device's own logs
    and run history, with nothing that came from a log line's text."""
    base = state_dir or state.default_state_dir()
    active_logs = dashboard.find_active_logs(dashboard.default_logs_dir(base))
    lines_by_log = {path: dashboard.tail_lines(path) for path in active_logs}
    in_progress, batch, overnight_run = dashboard.progress_from_logs(active_logs, lines_by_log)
    if overnight_run is not None:
        overnight_run = {k: v for k, v in overnight_run.items() if k != "log"}
        # `build_status` writes this as the device's naive local time, which
        # the local page reads correctly and a hub viewer in another
        # timezone would not. Sent as UTC.
        overnight_run["started_at"] = _utc(overnight_run.get("started_at"))
    slots = [
        {
            "repo": slot["repo"],
            "phase": slot["phase"],
            "started_at": slot.get("started_at"),
            "idle_seconds": slot.get("idle_seconds"),
            "stalled": bool(slot.get("stalled")),
            # A flag only: the limit message (and its reset time) is text.
            "rate_limit_seen": bool((slot.get("activity") or {}).get("rate_limit")),
        }
        for slot in live.build_live(state_dir=base).get("slots", [])
    ]
    return {
        "seq": seq,
        "sent_at": state.now_iso(),
        "interval_seconds": interval_seconds,
        "ending": ending,
        "gardener_version": __version__,
        "session": {
            "id": session.id,
            "command": session.command,
            "target": session.target,
            "started_at": session.started_at,
        },
        "in_progress": in_progress,
        "batch_progress": (
            {"start": batch[0], "end": batch[1], "total": batch[2]} if batch else None
        ),
        "overnight_run": overnight_run,
        "slots": slots,
    }


def _utc(value: Optional[str]) -> Optional[str]:
    """A naive local ISO time as aware UTC; an aware one converted; None or
    an unreadable one as None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


class Heartbeat:
    """The sender. `send` and `snapshot` are injectable so tests drive it
    without a clock or a real dispatch."""

    def __init__(
        self,
        config: hub.HubConfig,
        session: sessions.Session,
        state_dir: Optional[Path] = None,
        send: Optional[Callable[[dict, float], None]] = None,
        snapshot: Optional[Callable[..., dict]] = None,
    ):
        self.config = config
        self.session = session
        self.state_dir = state_dir
        self.interval = config.heartbeat_seconds
        self._send = send or self._post
        self._snapshot = snapshot or build_snapshot
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.seq = 0
        self.sent = 0
        self.disabled = False
        self.failing = False
        self._noted = False

    def _post(self, body: dict, timeout: float) -> None:
        hub._request(self.config, "POST", "/api/v1/heartbeat", body, timeout=timeout)

    def _note(self, message: str) -> None:
        if not self._noted:
            self._noted = True
            print(f"gardener: NOTE — hub heartbeat {message} (non-fatal)", file=sys.stderr)

    def beat(self, ending: bool = False, timeout: float = hub.REQUEST_TIMEOUT_SECONDS) -> bool:
        """Build and send one beat. Never raises; returns whether it was sent."""
        if self.disabled:
            return False
        self.seq += 1
        try:
            body = self._snapshot(self.session, self.seq, self.interval, ending=ending,
                                  state_dir=self.state_dir)
            self._send(body, timeout)
        except hub.HubError as e:
            if e.status == 404:
                self.disabled = True
                self._note("is off for this session: the hub has no heartbeat route yet "
                           "(upgrade the hub)")
            else:
                self.failing = True
                self._note(f"failed, retrying with backoff: {e}")
            return False
        except Exception as e:  # noqa: BLE001 - observability must never break a dispatch
            self.failing = True
            self._note(f"failed, retrying with backoff: {e!r}")
            return False
        self.failing = False
        self.sent += 1
        return True

    def _run(self) -> None:
        delay = self.interval
        while not self.disabled:
            ok = self.beat()
            delay = self.interval if ok else min(max(delay * 2, self.interval), MAX_BACKOFF_SECONDS)
            if self._stop.wait(delay):
                return

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="gardener-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the loop, then send the `ending` beat once, briefly, unless
        the hub is unsupported or was already failing."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=FINAL_BEAT_TIMEOUT_SECONDS + hub.REQUEST_TIMEOUT_SECONDS)
        if not self.disabled and not self.failing:
            self.beat(ending=True, timeout=FINAL_BEAT_TIMEOUT_SECONDS)


@contextmanager
def running(session: Optional[sessions.Session]) -> Iterator[Optional[Heartbeat]]:
    """Heartbeat for the duration of the block when this device has a hub,
    a token, and a non-zero interval. Yields the sender or None; never
    raises out of setup or teardown."""
    beat: Optional[Heartbeat] = None
    try:
        config = hub.load_config()
        if (session is not None and config is not None and config.token
                and config.heartbeat_seconds > 0):
            beat = Heartbeat(config, session)
            beat.start()
    except Exception as e:  # noqa: BLE001 - as above
        print(f"gardener: NOTE — hub heartbeat did not start (non-fatal): {e!r}", file=sys.stderr)
        beat = None
    try:
        yield beat
    finally:
        if beat is not None:
            try:
                beat.stop()
            except Exception:  # noqa: BLE001 - as above
                pass
