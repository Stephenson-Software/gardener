"""trace-client 0.1.1 -- https://github.com/Stephenson-Software/trace-client-python

One call to report that a program was used. Copy this file into a project as
is, or vendor the package; either way there is nothing else to add. Standard
library only; Python 3.8+.

MIT licensed. Keep this header when vendoring so the file can be found again.

Vendored into gardener unmodified apart from this note.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.error
import urllib.request
from typing import Dict, Mapping, Optional

__version__ = "0.1.1"

_LOG = logging.getLogger("trace")


class TraceClient:
    """Reports usage events to a trace server, and never gets in the way of
    the program doing the reporting.

    Three properties hold for every call to :meth:`report`:

    * **It returns immediately.** The HTTP call happens on a single daemon
      thread owned by this client. A game loop can report from its main
      thread without a frame ever waiting on the network.
    * **It never raises.** A server that is down, slow, or rejecting the key
      is a dropped report, not an exception in the host program. Failures
      are logged at DEBUG on the ``trace`` logger and otherwise not at all.
    * **It is bounded.** At most :attr:`QUEUE_CAPACITY` reports wait to be
      sent; beyond that, new reports are dropped rather than accumulated.
      A trace server that is unreachable for a week costs a few kilobytes,
      not the host's memory.

    Reporting is opt-out: ``enabled=False``, or no key, yields a client that
    does nothing and costs nothing. Programs that run on other people's
    machines should expose that switch in their settings and say so once.

    ::

        trace = TraceClient("https://trace.example.org", "roam",
                            key=settings.usage_key, enabled=settings.usage_reporting)
        trace.report("startup", tags={"version": __version__})
        ...
        trace.close()  # on shutdown
    """

    QUEUE_CAPACITY = 256
    TIMEOUT_SECONDS = 5.0

    def __init__(self, base_url: str, application: str, *, key: Optional[str] = None,
                 enabled: bool = True) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required")
        if not application or not application.strip():
            raise ValueError("application is required")
        self._endpoint = base_url.strip().rstrip("/") + "/api/metrics"
        self._application = application.strip()
        self._key = (key or "").strip()
        self._queue: Optional["queue.Queue[Optional[bytes]]"] = None
        self._thread: Optional[threading.Thread] = None
        if enabled and self._key:
            self._queue = queue.Queue(maxsize=self.QUEUE_CAPACITY)
            self._thread = threading.Thread(target=self._drain, name="trace-client/" + self._application,
                                            daemon=True)
            self._thread.start()

    @classmethod
    def disabled(cls) -> "TraceClient":
        """A client that reports nothing. Useful as a default before settings are read."""
        return cls("http://disabled.invalid", "disabled", enabled=False)

    @property
    def enabled(self) -> bool:
        """Whether :meth:`report` will actually send anything."""
        return self._queue is not None

    def report(self, name: str, value: Optional[float] = None,
               tags: Optional[Mapping[str, str]] = None) -> None:
        """Report that ``name`` happened, with an optional numeric value and
        optional string tags. Returns immediately; see the class docstring."""
        if self._queue is None or not name or not name.strip():
            return
        try:
            body = _json(self._application, name, value, tags)
            self._queue.put_nowait(body)
        except queue.Full:
            _LOG.debug("[trace] queue full, dropped %s", name)
        except Exception as failure:  # noqa: BLE001 - a report must never be the reason a program stops
            _LOG.debug("[trace] could not queue %s: %s", name, failure)

    def close(self, timeout: float = TIMEOUT_SECONDS) -> None:
        """Stop the sending thread, giving reports already queued up to
        ``timeout`` seconds in total to be sent first. A program that reports
        and then exits within milliseconds -- a CLI, a short script -- would
        otherwise lose its one event to the race between queueing it and the
        sender thread picking it up. The bound still holds: an unreachable
        server costs at most ``timeout``, never a hang. Safe to call more than
        once, and on a disabled client."""
        if self._queue is None or self._thread is None:
            return
        q, thread = self._queue, self._thread
        self._queue = None  # report() is a no-op from here on
        try:
            q.put_nowait(None)  # sentinel behind whatever is queued
        except queue.Full:
            # A full queue means 256 reports are waiting; the sentinel would be
            # the 257th. Drop the oldest to make room -- one lost report beats a
            # thread that never stops.
            try:
                q.get_nowait()
                q.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass
        thread.join(timeout)

    # -- internals --------------------------------------------------------

    def _drain(self) -> None:
        q = self._queue
        assert q is not None
        while True:
            body = q.get()
            if body is None:
                return
            self._send(body)

    def _send(self, body: bytes) -> None:
        request = urllib.request.Request(
            self._endpoint, data=body, method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": "Bearer " + self._key,
                "User-Agent": "trace-client-python/%s (%s)" % (__version__, self._application),
            })
        try:
            with urllib.request.urlopen(request, timeout=self.TIMEOUT_SECONDS) as response:
                status = response.status
                response.read()
        except urllib.error.HTTPError as answered:
            status = answered.code
            try:
                answered.read()
            except Exception:  # noqa: BLE001
                pass
        except Exception as failure:  # noqa: BLE001 - see report()
            _LOG.debug("[trace] could not deliver %s: %s", body, failure)
            return
        if status != 201:
            _LOG.debug("[trace] trace server answered %s for %s", status, body)


def _json(application: str, name: str, value: Optional[float], tags: Optional[Mapping[str, str]]) -> bytes:
    payload: Dict[str, object] = {"application": application, "name": name}
    if value is not None and value == value and value not in (float("inf"), float("-inf")):
        payload["value"] = value
    if tags:
        clean = {str(k): str(v) for k, v in tags.items() if k is not None and v is not None}
        if clean:
            payload["tags"] = clean
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")
