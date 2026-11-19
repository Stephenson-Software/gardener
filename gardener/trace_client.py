"""trace-client 0.5.0 -- https://github.com/Stephenson-Software/trace-client-python

One call to report that a program was used. Copy this file into a project as
is, or vendor the package; either way there is nothing else to add. Standard
library only; Python 3.8+.

MIT licensed. Keep this header when vendoring so the file can be found again.

Vendored into gardener unmodified apart from this note.
"""
from __future__ import annotations

import decimal
import json
import logging
import numbers
import os
import queue
import re
import threading
import urllib.error
import urllib.request
import uuid
from typing import Dict, Mapping, Optional, Union

__version__ = "0.5.0"

_LOG = logging.getLogger("trace")

#: Environment variables that turn reporting off for every program using a
#: trace client, checked before the program's own setting. Both fail closed:
#: ``TRACE_USAGE_REPORTING`` set to anything but blank, ``true``, ``on``,
#: ``yes`` or ``1`` turns reporting off (so ``off``, ``false``, ``0``, ``no``,
#: ``disabled``, a typo, ...), and so does ``DO_NOT_TRACK`` set to anything
#: but blank, ``0``, ``false``, ``off`` or ``no`` (so ``1``, ``true``,
#: ``yes``, ...; see https://consoledonottrack.com). Case and surrounding
#: space do not matter.
ENV_TRACE_USAGE_REPORTING = "TRACE_USAGE_REPORTING"
ENV_DO_NOT_TRACK = "DO_NOT_TRACK"
_ON_VALUES = frozenset(("true", "on", "yes", "1"))
_NEGATIVE_VALUES = frozenset(("false", "off", "no", "0"))

#: The values :attr:`TraceClient.disabled_reason` can take. First match wins.
REASON_ENVIRONMENT = "environment"
REASON_CONFIG = "config"
REASON_NO_KEY = "no key"

#: The longest a tag value the client sets itself -- the program version, the
#: installation ID -- may be, after trimming: the trace server's limit on a
#: tag value.
MAX_TAG_LENGTH = 255

#: The most tags one event carries: the trace server's limit. The ``install``
#: tag is only added while an event has fewer than this.
MAX_TAGS = 32

#: The tag every event carries the installation's ID as.
INSTALL_TAG = "install"

# What install_id_from_file accepts as an ID on a line of its file.
_INSTALL_ID_LINE = re.compile(r"^[A-Za-z0-9_.-]{1,%d}$" % MAX_TAG_LENGTH)

# What the User-Agent header cannot carry: everything but printable Latin-1.
_NOT_IN_A_HEADER = re.compile(r"[^\x20-\x7e\xa0-\xff]")


class _RedirectsNotFollowed(urllib.request.HTTPRedirectHandler):
    """Declines every redirect, so the redirect itself is the answer. Following
    one would re-send the key to whatever host ``Location`` names, and a 301,
    302 or 303 would turn the POST into an empty GET that loses the event
    anyway. trace-client-java does not follow redirects either."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Every report goes through this opener rather than urlopen: urlopen follows
# redirects, and install_opener would change the host program's urlopen too.
_OPENER = urllib.request.build_opener(_RedirectsNotFollowed)


def environment_opts_out(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the environment asks for usage reporting to be off. Fails
    closed: ``TRACE_USAGE_REPORTING`` set to anything but blank or a clear
    "on" (``true``, ``on``, ``yes``, ``1``), or ``DO_NOT_TRACK`` set to
    anything but blank or a clear "no" (``0``, ``false``, ``off``, ``no``),
    opts out; case and surrounding space do not matter. An environment that
    cannot be read -- or a value that is not a string -- opts out too: it may
    hold ``DO_NOT_TRACK=1``. Never raises."""
    try:
        env = os.environ if environ is None else environ
        usage_reporting = (env.get(ENV_TRACE_USAGE_REPORTING) or "").strip().lower()
        if usage_reporting and usage_reporting not in _ON_VALUES:
            return True
        do_not_track = (env.get(ENV_DO_NOT_TRACK) or "").strip().lower()
        return bool(do_not_track) and do_not_track not in _NEGATIVE_VALUES
    except Exception:  # noqa: BLE001 - an environment that cannot be read counts as opting out
        return True


def allows_reporting(setting: object) -> bool:
    """Whether a program's own reporting setting, as read from its
    configuration, allows reporting -- for ``enabled=``. Fails closed: only
    ``True``, or a string reading ``true``, ``on``, ``yes`` or ``1`` (any
    case, surrounding space ignored), allows it. Anything else turns
    reporting off: ``False``, ``"false"``, ``"off"``, ``"no"``, a typo, a
    number, ``None`` (a missing setting), a quoted ``'"false"'``.

    Pass the raw value, e.g. ``settings.get("usage_reporting")``, rather than
    ``bool(...)`` of it: ``bool("false")`` is ``True``. The constructor reads
    ``enabled=`` through this function too, so ``enabled="false"`` is off."""
    if isinstance(setting, bool):
        return setting
    return isinstance(setting, str) and setting.strip().lower() in _ON_VALUES


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
    does nothing and costs nothing. So does the environment: the constructor
    checks ``TRACE_USAGE_REPORTING=off`` and ``DO_NOT_TRACK=1`` before it
    looks at ``enabled``, so a user can switch off every trace-reporting
    program at once. Every switch fails closed: a value that does not clearly
    mean "on" turns reporting off (see :func:`environment_opts_out` and
    :func:`allows_reporting`), and nothing in this client turns a switch
    back on. :attr:`disabled_reason` says which of those applied
    (``"environment"``, ``"config"`` or ``"no key"``; ``None`` when on) so
    the program can say so in its notice. Programs that run on other
    people's machines should expose that switch in their settings and say
    so once, pointing at https://danielstephenson.dev/usage-reporting.

    Every event carries the program's own version as the tag ``version`` --
    the third argument, required, so a ``command`` event can be tied to a
    release as well as a ``startup`` one. An event's own ``version`` tag wins
    over it.

    Every event also carries a random per-installation ID as the tag
    ``install``, so the trace server can count distinct installations rather
    than raw events -- when the program supplies one: ``install_id=`` (an ID
    it stores itself) or ``install_id_file=`` (a path the client loads the ID
    from, or writes a new random one to; see :meth:`install_id_from_file`).
    Without either, no ``install`` tag is sent and nothing is written
    anywhere. Both are resolved only after the opt-outs, so a disabled client
    never makes up an ID and never writes one. An event's own ``install`` tag
    wins over it.

    ::

        trace = TraceClient("https://trace.example.org", "roam", __version__,
                            key=settings.usage_key,
                            enabled=allows_reporting(settings.usage_reporting))
        trace.report("startup")
        ...
        trace.close()  # on shutdown
    """

    QUEUE_CAPACITY = 256
    TIMEOUT_SECONDS = 5.0

    def __init__(self, base_url: str, application: str, version: str, *, key: Optional[str] = None,
                 enabled: Union[bool, str, None] = True, install_id: Optional[str] = None,
                 install_id_file: Optional[Union[str, "os.PathLike[str]"]] = None) -> None:
        """A client for the program named ``application``, at ``version``,
        reporting to the trace server at ``base_url``. The version is sent as
        the tag ``version`` on every event; a blank one, or one longer than
        :data:`MAX_TAG_LENGTH` characters, is a :class:`ValueError`.

        ``install_id`` is the installation's ID, sent as the tag ``install``
        on every event. It should be random -- e.g. a :func:`uuid.uuid4` the
        program stores in its own settings -- and never derived from a
        person, account or address. Trimmed; ``None`` or blank means none;
        longer than :data:`MAX_TAG_LENGTH` characters is a
        :class:`ValueError`. ``install_id_file`` is a path to load it from
        (or create it in) with :meth:`install_id_from_file`, only when the
        client is enabled; an explicit ``install_id`` wins over it.

        A value that is not a string for ``base_url``, ``application``,
        ``version``, ``key`` or ``install_id`` is a :class:`ValueError` naming
        the argument, even on a client that would be disabled: a settings
        file that reads ``version: 1.4`` as a number should say so. An empty
        one (``0``, ``False``, ``[]``) still means no value, as it did before
        that check: no key, no ID, or "<argument> is required"."""
        for argument, given in (("base_url", base_url), ("application", application), ("version", version),
                                ("key", key), ("install_id", install_id)):
            if given and not isinstance(given, str):
                raise ValueError("%s must be a string, not %s" % (argument, type(given).__name__))
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required")
        if not application or not application.strip():
            raise ValueError("application is required")
        if not version or not version.strip():
            raise ValueError("version is required")
        if len(version.strip()) > MAX_TAG_LENGTH:
            raise ValueError("version is longer than %d characters" % MAX_TAG_LENGTH)
        explicit_install_id = (install_id or "").strip() or None
        if explicit_install_id is not None and len(explicit_install_id) > MAX_TAG_LENGTH:
            raise ValueError("install_id is longer than %d characters" % MAX_TAG_LENGTH)
        self._endpoint = base_url.strip().rstrip("/") + "/api/metrics"
        self._application = application.strip()
        self._version = version.strip()
        self._key = (key or "").strip()
        self._queue: Optional["queue.Queue[Optional[bytes]]"] = None
        self._thread: Optional[threading.Thread] = None
        #: Why this client reports nothing: ``"environment"`` (the
        #: ``TRACE_USAGE_REPORTING`` / ``DO_NOT_TRACK`` variables),
        #: ``"config"`` (``enabled`` not ``True`` or an "on" string -- see
        #: :func:`allows_reporting`) or ``"no key"``; ``None`` when it
        #: reports. Decided once, here, in that order of precedence.
        self.disabled_reason: Optional[str] = None
        if environment_opts_out():
            self.disabled_reason = REASON_ENVIRONMENT
        elif not allows_reporting(enabled):
            self.disabled_reason = REASON_CONFIG
        elif not self._key:
            self.disabled_reason = REASON_NO_KEY
        self._install_id: Optional[str] = None
        if self.disabled_reason is None:
            # After the opt-outs, never before: a disabled client neither
            # makes up an ID nor writes one to disk.
            if explicit_install_id is not None:
                self._install_id = explicit_install_id
            elif install_id_file is not None:
                self._install_id = TraceClient.install_id_from_file(install_id_file)
            self._queue = queue.Queue(maxsize=self.QUEUE_CAPACITY)
            self._thread = threading.Thread(target=self._drain, name="trace-client/" + self._application,
                                            daemon=True)
            self._thread.start()

    @classmethod
    def disabled(cls) -> "TraceClient":
        """A client that reports nothing. Useful as a default before settings are read."""
        return cls("http://disabled.invalid", "disabled", "disabled", enabled=False)

    @property
    def install_id(self) -> Optional[str]:
        """The per-installation ID every event carries as the tag ``install``,
        or ``None`` when the client is disabled or was given none (no
        ``install_id`` and no ``install_id_file``)."""
        return self._install_id

    @staticmethod
    def install_id_from_file(path: Union[str, "os.PathLike[str]"]) -> str:
        """The installation's ID kept in the file at ``path``, which the
        program chooses -- there is no default location. The first line that
        is an ID (``[A-Za-z0-9_.-]``, at most :data:`MAX_TAG_LENGTH`
        characters, surrounding whitespace ignored) is returned. If the file
        does not exist or holds no such line, a new random :func:`uuid.uuid4`
        is written to it (parent directories created) and returned. Delete
        the file to get a new one.

        Never raises: if the file exists but cannot be read, or cannot be
        written, a fresh random ID is returned for this process only, and an
        unreadable file is left as it is.

        Called directly, this writes whatever the opt-outs say. Pass the path
        as ``install_id_file=`` instead to keep the guarantee that a disabled
        client writes nothing."""
        fresh = str(uuid.uuid4())
        try:
            target = os.fspath(path)
            if not target or not str(target).strip():
                return fresh
            try:
                with open(target, "r", encoding="utf-8") as existing:
                    for line in existing:
                        candidate = line.strip()
                        if _INSTALL_ID_LINE.match(candidate):
                            return candidate
            except FileNotFoundError:
                pass
            except Exception as failure:  # noqa: BLE001 - unreadable: never overwrite it
                _LOG.debug("[trace] could not read install ID file %s, using an in-memory one: %s",
                           target, failure)
                return fresh
            parent = os.path.dirname(os.path.abspath(target))
            os.makedirs(parent, exist_ok=True)
            with open(target, "w", encoding="utf-8") as written:
                written.write(fresh + "\n")
            return fresh
        except Exception as failure:  # noqa: BLE001 - an ID must never be the reason a program stops
            _LOG.debug("[trace] could not write install ID file %s, using an in-memory one: %s",
                       path, failure)
            return fresh

    @property
    def enabled(self) -> bool:
        """Whether :meth:`report` will actually send anything. ``False`` after
        :meth:`close` too; :attr:`disabled_reason` keeps the reason it was
        built off, if it was."""
        return self._queue is not None

    def report(self, name: str, value: Optional[float] = None,
               tags: Optional[Mapping[str, str]] = None) -> None:
        """Report that ``name`` happened, with an optional numeric value and
        optional string tags. Returns immediately; see the class docstring.
        A blank ``name`` sends nothing. A ``value`` that is NaN, infinite or
        not a real number (a string, a bool), and any tag whose key or value
        is ``None``, is left out of the event; the event is still sent."""
        if self._queue is None:
            return
        try:
            if not name or not name.strip():
                return
            tags = _with_install(_with_version(tags, self._version), self._install_id)
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
        server costs at most ``timeout``, never a hang. Reports still queued
        when ``timeout`` runs out are not discarded: the daemon thread keeps
        sending them after ``close()`` returns, then stops, and whatever it
        has not sent when the program exits is lost. On a full queue the
        oldest queued report is dropped (logged at DEBUG) to make room for
        the signal that stops the thread. Safe to call more than once, and
        on a disabled client."""
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
                dropped = q.get_nowait()
                _LOG.debug("[trace] queue full at close, dropped %s", dropped)
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
            try:
                self._send(body)
            except Exception as failure:  # noqa: BLE001 - a dead sender would leave the queue filling forever
                _LOG.debug("[trace] sender failed on %s: %s", body, failure)

    def _send(self, body: bytes) -> None:
        try:
            # Built inside the try: a base URL without a scheme fails here, and
            # an uncaught error would kill the sender thread with a traceback.
            request = urllib.request.Request(
                self._endpoint, data=body, method="POST",
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": "Bearer " + self._key,
                    "User-Agent": _user_agent(self._application),
                })
            with _OPENER.open(request, timeout=self.TIMEOUT_SECONDS) as response:
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


def _user_agent(application: str) -> str:
    """The ``User-Agent`` for ``application``. A header carries printable
    Latin-1 and nothing else, and one it cannot carry stops the request, so
    any other character becomes ``?`` here. The event itself keeps the exact
    name: what a program is called must not cost it every report."""
    return "trace-client-python/%s (%s)" % (__version__, _NOT_IN_A_HEADER.sub("?", application))


def _with_version(tags: Optional[Mapping[str, str]], version: str) -> Dict[str, str]:
    """The event's own tags plus ``version``, unless the event already carries
    one. A copy; the caller's mapping is never modified."""
    merged: Dict[str, str] = {}
    if tags:
        for k, v in dict(tags).items():
            if k is not None and v is not None:
                merged[str(k)] = str(v)
    if "version" not in merged:
        merged["version"] = version
    return merged


def _with_install(tags: Dict[str, str], install_id: Optional[str]) -> Dict[str, str]:
    """The tags plus ``install``, unless they already carry one, there is no
    ID, or adding it would pass :data:`MAX_TAGS`. A copy when it adds."""
    if install_id is None or INSTALL_TAG in tags or len(tags) >= MAX_TAGS:
        return tags
    merged = dict(tags)
    merged[INSTALL_TAG] = install_id
    return merged


def _number(value: object) -> Optional[float]:
    """``value`` as a number JSON can carry, or ``None`` for one it cannot:
    anything that is not a real number (a string, a bool), NaN and infinity.
    A ``Decimal`` or ``Fraction`` becomes a float rather than costing the
    event its place: ``json.dumps`` would refuse it, and the event with it."""
    if isinstance(value, bool) or not isinstance(value, (numbers.Real, decimal.Decimal)):
        return None
    if type(value) not in (int, float):
        try:
            value = float(value)
        except (OverflowError, ValueError):
            return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value  # type: ignore[return-value]


def _json(application: str, name: str, value: Optional[float], tags: Optional[Mapping[str, str]]) -> bytes:
    payload: Dict[str, object] = {"application": application, "name": name}
    number = _number(value)
    if number is not None:
        payload["value"] = number
    if tags:
        clean = {str(k): str(v) for k, v in tags.items() if k is not None and v is not None}
        if clean:
            payload["tags"] = clean
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")
