"""An optional shared run history across devices (RFC 0007).

gardener's run history is a local SQLite file per device, and until this
module nothing read one device's history from another. So the dashboard on
the box never showed what the phone tended, and the phone's spend was
missing from the box's view of a subscription limit both devices draw on.
The hub is one more gardener process, `gardener hub serve`, that every
configured device pushes its runs to and that serves the existing
dashboard over the combined history.

## It adds, it never replaces

A device with no `GARDENER_HUB_URL` behaves exactly as before. A device
with one still writes its local store *first*. That row is the durable
record, and the push is a copy made afterwards. A run the hub has not
acknowledged keeps `pushed_at IS NULL` locally (the outbox) and goes with
the next push, so a phone that was offline all night catches up on its
first run with a signal, and a hub that is down costs nothing but delay.
Nothing here can fail a dispatch: `push_after_record` never raises.

## Idempotent by construction

Every run carries a `run_uuid` (`state.ADDED_COLUMNS`), the hub inserts
with `ON CONFLICT(run_uuid) DO NOTHING`, and its response lists every uuid
from the batch that it now holds, including ones it already had. A push
whose response was lost is therefore safe to repeat, which is all a retry
or a `gardener hub sync` backfill ever is.

## Auth is in the process, not in front of it

The history names private repositories, and its summaries quote their
issues. So every route but `/healthz` requires a credential, checked
here rather than by a reverse proxy, so the hub is safe on any host:

- **Devices** write with a bearer token. The hub holds only
  `name:sha256(token)` pairs (`GARDENER_HUB_DEVICE_TOKENS`), and every row
  a token pushes is stored under that token's name, whatever the row's own
  `device` says. So a leaked phone token cannot write rows that appear to
  come from the box. The token being the authority, and not the row, is
  deliberate. A device's local rows carry whatever name it resolved when
  they were recorded (hostname until `GARDENER_DEVICE_NAME` is set), and
  a check that the two match would strand a device's whole history in its
  outbox the first time that name changed. That happened on the first
  real backfill. Device tokens can also read the `/api/v1/` endpoints.
- **Operators** read the dashboard with HTTP basic auth checked against
  `GARDENER_HUB_OPERATOR_BASIC_SHA256` (hex sha256 of `user:password`),
  and/or sign in through a UserAuth service when
  `GARDENER_HUB_USERAUTH_URL` is set, with `GARDENER_HUB_OPERATORS` as the
  username allowlist (UserAuth registration is open, so the allowlist is
  the authorisation). The UserAuth mode is optional so that a self-hoster
  who doesn't run UserAuth never meets it.

`serve` refuses to start with no operator credential configured, rather
than serving the history to anyone who finds the URL.

Stdlib only, like the rest of gardener: `http.server`, `urllib`, `hmac`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Mapping, Optional

from gardener import __version__, dashboard, garden, merge_allowlist, notify, state

URL_ENV = "GARDENER_HUB_URL"
TOKEN_ENV = "GARDENER_HUB_TOKEN"
CONFIG_FILENAME = "hub.env"

DEVICE_TOKENS_ENV = "GARDENER_HUB_DEVICE_TOKENS"
OPERATOR_BASIC_ENV = "GARDENER_HUB_OPERATOR_BASIC_SHA256"
USERAUTH_URL_ENV = "GARDENER_HUB_USERAUTH_URL"
OPERATORS_ENV = "GARDENER_HUB_OPERATORS"

#: Rows per push request, and the most a hub accepts in one.
PUSH_BATCH = 100
MAX_BATCH = 500
#: Largest request body the hub reads. A full batch of runs with long
#: summaries is well under this; anything bigger is not a gardener.
MAX_BODY_BYTES = 4 * 1024 * 1024
#: Per request, and for one whole drain on the dispatch path. The first
#: push after configuring a device can be a long history; the deadline
#: keeps that off the dispatch path, and `gardener hub sync` finishes it.
REQUEST_TIMEOUT_SECONDS = 5.0
PUSH_DEADLINE_SECONDS = 20.0
#: How long a UserAuth `/session/validate` answer is reused. Bounds both
#: the load on UserAuth from a 4 s poll and how long a revoked session
#: keeps working here.
VALIDATE_CACHE_SECONDS = 60.0
SESSION_COOKIE = "gardener_hub_session"
MAX_SUMMARY_CHARS = 64 * 1024


class HubError(Exception):
    """A hub request failed: unreachable, refused, or answered badly."""


# --------------------------------------------------------------------------
# Device side
# --------------------------------------------------------------------------


@dataclass
class HubConfig:
    url: str
    token: Optional[str]


def config_path(state_dir: Optional[Path] = None) -> Path:
    """`hub.env` next to the run history, the same two-source pattern as
    `notify.env`: an env var wins, the file serves the cron/Task Scheduler
    contexts where exporting one per invocation isn't practical. gardener
    never writes this file."""
    return (state_dir or state.default_state_dir()) / CONFIG_FILENAME


def load_config(state_dir: Optional[Path] = None) -> Optional[HubConfig]:
    """This device's hub settings, or None when no hub is configured, which
    is the default and means local-only, exactly as before RFC 0007.

    The token is never a CLI flag, so it stays out of `ps` output."""
    file_values: dict[str, str] = {}
    path = config_path(state_dir)
    if path.is_file():
        try:
            file_values = notify._parse_env_style_file(path)
        except OSError as e:
            print(f"gardener: NOTE — could not read {path}: {e}", file=sys.stderr)

    def pick(name: str) -> Optional[str]:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
        value = file_values.get(name)
        return value.strip() if value and value.strip() else None

    url = pick(URL_ENV)
    if not url:
        return None
    return HubConfig(url=url.rstrip("/"), token=pick(TOKEN_ENV))


#: The `Run` fields a push carries, and the only ones `fetch_runs` reads
#: back. `id` is deliberately absent: it is local to each store.
WIRE_FIELDS = (
    "run_uuid", "device", "repo", "timestamp", "mode", "outcome",
    "gap_summary", "exit_code", "duration_ms", "cost_usd", "claude_session_id",
)


def run_to_wire(run: state.Run) -> dict:
    return {
        "run_uuid": run.run_uuid,
        "device": run.device,
        "repo": run.repo,
        "timestamp": run.timestamp,
        "mode": run.mode,
        "outcome": run.outcome,
        "gap_summary": run.gap_summary,
        "exit_code": run.exit_code,
        "duration_ms": run.duration_ms,
        "cost_usd": run.cost_usd,
        "claude_session_id": run.claude_session_id,
    }


def _request(
    config: HubConfig,
    method: str,
    path: str,
    body: Optional[dict] = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict:
    if not config.token:
        raise HubError(f"{URL_ENV} is set but {TOKEN_ENV} is not")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        config.url + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
            # Some fronts refuse urllib's default user agent outright.
            "User-Agent": f"gardener/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read(500).decode("utf-8", "replace").strip()
        raise HubError(f"{method} {path} → HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError) as e:
        raise HubError(f"{method} {path} failed: {e}") from e
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError as e:
        raise HubError(f"{method} {path} answered with invalid JSON") from e


@dataclass
class PushResult:
    pushed: int = 0
    remaining: int = 0
    error: Optional[str] = None


def _device_lists(state_dir: Path) -> dict:
    """This device's garden and merge allow-list, sent with every push so
    the hub's garden view shows every device's opt-ins. Display only: the
    hub never sends them back, and each device's lists stay its own."""
    lists = {}
    try:
        lists["garden"] = garden.list_garden(path=state_dir / "garden.json")
    except Exception:  # noqa: BLE001 - an unreadable list is doctor's finding, not the push's
        pass
    try:
        lists["merge_allowlist"] = merge_allowlist.list_allowed(
            path=state_dir / "merge_allowlist.json"
        )
    except Exception:  # noqa: BLE001 - as above
        pass
    return lists


def push_pending(
    config: HubConfig,
    db_path: Optional[Path] = None,
    deadline_seconds: Optional[float] = PUSH_DEADLINE_SECONDS,
    batch_size: int = PUSH_BATCH,
    clock: Callable[[], float] = time.monotonic,
) -> PushResult:
    """Drain the outbox to the hub in batches, oldest first, until it is
    empty, a request fails, or `deadline_seconds` passes (None: no
    deadline, for `gardener hub sync`). Only uuids the hub lists as held
    are marked pushed. Raises nothing: a failure is `PushResult.error`."""
    db_path = db_path or state.default_db_path()
    started = clock()
    result = PushResult()
    lists = _device_lists(db_path.parent)
    while True:
        try:
            batch = state.pending_push(db_path=db_path, limit=batch_size)
        except (sqlite3.Error, OSError) as e:
            result.error = f"could not read the outbox: {e}"
            break
        if not batch:
            break
        if deadline_seconds is not None and clock() - started > deadline_seconds:
            break
        body = {"runs": [run_to_wire(r) for r in batch], **lists}
        try:
            answer = _request(config, "POST", "/api/v1/runs", body)
        except HubError as e:
            result.error = str(e)
            break
        held = [u for u in answer.get("held", []) if isinstance(u, str)]
        try:
            state.mark_pushed(held, state.now_iso(), db_path=db_path)
        except (sqlite3.Error, OSError) as e:
            result.error = f"hub accepted runs but marking them pushed failed: {e}"
            break
        result.pushed += len(held)
        if len(held) < len(batch):
            # The hub refused part of the batch without a 4xx. Stop rather
            # than resend the same rows in a loop.
            result.error = f"hub held {len(held)} of {len(batch)} runs in the batch"
            break
    try:
        result.remaining, _oldest = state.pending_push_summary(db_path=db_path)
    except (sqlite3.Error, OSError):
        pass
    return result


def push_after_record(db_path: Optional[Path] = None) -> None:
    """The dispatch path's push: after a run is recorded locally, send the
    outbox if a hub is configured. Never raises and never prints unless
    something failed. The run is already safe in the local store, and the
    rows stay queued for the next push."""
    try:
        config = load_config((db_path or state.default_db_path()).parent)
        if config is None:
            return
        result = push_pending(config, db_path=db_path)
        if result.error:
            print(
                f"gardener: NOTE — hub push failed, {result.remaining} run(s) queued "
                f"for the next one (non-fatal): {result.error}",
                file=sys.stderr,
            )
    except Exception as e:  # noqa: BLE001 - the push must never break the run it copies
        print(f"gardener: NOTE — hub push failed (non-fatal): {e}", file=sys.stderr)


def fetch_runs(config: HubConfig, repo: Optional[str] = None, limit: int = 20) -> list[state.Run]:
    """The hub's newest runs across every device, for `status --all-devices`."""
    query = {"limit": str(limit)}
    if repo:
        query["repo"] = repo
    answer = _request(config, "GET", "/api/v1/runs?" + urllib.parse.urlencode(query))
    return [
        state.Run(**{k: row.get(k) for k in WIRE_FIELDS})
        for row in answer.get("runs", [])
        if isinstance(row, dict)
    ]


# --------------------------------------------------------------------------
# Hub side
# --------------------------------------------------------------------------

DEVICE_LISTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_lists (
    device TEXT NOT NULL,
    list TEXT NOT NULL,
    repos TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (device, list)
);
"""
KNOWN_LISTS = ("garden", "merge_allowlist")


def token_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def mint_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class ServerAuth:
    """The hub's credentials, read once from the environment at start."""

    #: sha256(token) → device name.
    device_tokens: dict[str, str] = field(default_factory=dict)
    operator_basic_digest: Optional[str] = None
    userauth_url: Optional[str] = None
    operators: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "ServerAuth":
        tokens: dict[str, str] = {}
        for entry in (env.get(DEVICE_TOKENS_ENV) or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            name, sep, digest = entry.rpartition(":")
            if not sep or not name or len(digest) != 64:
                raise ValueError(
                    f"{DEVICE_TOKENS_ENV}: expected device:sha256hex entries, got {entry[:40]!r}"
                )
            tokens[digest.lower()] = name
        userauth = (env.get(USERAUTH_URL_ENV) or "").strip().rstrip("/") or None
        operators = frozenset(
            u.strip() for u in (env.get(OPERATORS_ENV) or "").split(",") if u.strip()
        )
        basic = (env.get(OPERATOR_BASIC_ENV) or "").strip().lower() or None
        return cls(tokens, basic, userauth, operators)

    def problems(self) -> list[str]:
        """Why `serve` must refuse to start, if anything."""
        out = []
        if self.userauth_url and not self.operators:
            out.append(f"{USERAUTH_URL_ENV} is set but {OPERATORS_ENV} is empty: nobody could sign in")
        if not self.operator_basic_digest and not self.userauth_url:
            out.append(
                f"no operator credential: set {OPERATOR_BASIC_ENV} and/or "
                f"{USERAUTH_URL_ENV} + {OPERATORS_ENV}; the hub will not serve the "
                "history unauthenticated"
            )
        if self.operator_basic_digest and len(self.operator_basic_digest) != 64:
            out.append(f"{OPERATOR_BASIC_ENV} is not a hex sha256")
        return out

    def device_for_token(self, token: str) -> Optional[str]:
        digest = token_digest(token)
        for known, name in self.device_tokens.items():
            if hmac.compare_digest(known, digest):
                return name
        return None

    def basic_ok(self, userpass: str) -> Optional[str]:
        if not self.operator_basic_digest:
            return None
        if hmac.compare_digest(self.operator_basic_digest, token_digest(userpass)):
            return userpass.split(":", 1)[0]
        return None


class UserAuthClient:
    """Signs operators in through a UserAuth service and checks their
    sessions, caching each `/session/validate` answer for
    `VALIDATE_CACHE_SECONDS`.

    Contract (UserAuth at 4b93c13): `POST /login` with JSON
    `{username, password}` answers `{token, tokenType, expiresAt,
    refreshToken}`; `GET /session/validate` with the token as a Bearer
    answers `{valid, username, roles, ...}` and refuses a revoked token."""

    def __init__(self, base_url: str, clock: Callable[[], float] = time.monotonic):
        self.base_url = base_url
        self.clock = clock
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _call(self, method: str, path: str, body: Optional[dict] = None,
              token: Optional[str] = None) -> Optional[dict]:
        headers = {"Content-Type": "application/json", "User-Agent": f"gardener/{__version__}"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError:
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            print(f"gardener hub: UserAuth {path} unreachable: {e}", file=sys.stderr)
            return None

    def login(self, username: str, password: str) -> Optional[str]:
        answer = self._call("POST", "/login", {"username": username, "password": password})
        token = answer.get("token") if answer else None
        return token if isinstance(token, str) and token else None

    def username_for(self, token: str) -> Optional[str]:
        now = self.clock()
        with self._lock:
            hit = self._cache.get(token)
            if hit and hit[1] > now:
                return hit[0]
        answer = self._call("GET", "/session/validate", token=token)
        if not answer or answer.get("valid") is not True or not answer.get("username"):
            with self._lock:
                self._cache.pop(token, None)
            return None
        with self._lock:
            self._cache[token] = (answer["username"], now + VALIDATE_CACHE_SECONDS)
        return answer["username"]

    def logout(self, token: str) -> None:
        with self._lock:
            self._cache.pop(token, None)
        self._call("POST", "/logout", token=token)


def store_device_lists(db_path: Path, device: str, lists: Mapping[str, list[str]]) -> None:
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(DEVICE_LISTS_SCHEMA)
        with conn:
            for name in KNOWN_LISTS:
                repos = lists.get(name)
                if repos is None:
                    continue
                conn.execute(
                    "INSERT INTO device_lists (device, list, repos, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(device, list) DO UPDATE SET repos = excluded.repos, "
                    "updated_at = excluded.updated_at",
                    (device, name, json.dumps(sorted(set(repos))), state.now_iso()),
                )


def union_device_lists(db_path: Path) -> dict[str, list[str]]:
    """Each list as the union of what every device last pushed."""
    out: dict[str, set[str]] = {name: set() for name in KNOWN_LISTS}
    if not db_path.exists():
        return {name: [] for name in KNOWN_LISTS}
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(DEVICE_LISTS_SCHEMA)
        for name, repos in conn.execute("SELECT list, repos FROM device_lists"):
            if name in out:
                try:
                    out[name].update(r for r in json.loads(repos) if isinstance(r, str))
                except json.JSONDecodeError:
                    continue
    return {name: sorted(repos) for name, repos in out.items()}


class InvalidRun(ValueError):
    pass


def run_from_wire(row: object, device: str) -> state.Run:
    """A pushed row as a `Run`, or `InvalidRun` naming the bad field.

    Strict because the hub is shared: a device on a newer gardener with an
    outcome this hub doesn't know is refused with a 400 and keeps the rows
    in its outbox until the hub is upgraded, instead of storing rows every
    aggregate here would silently miscount (see `state.KNOWN_OUTCOMES`)."""
    from gardener.cli import REPO_RE

    if not isinstance(row, dict):
        raise InvalidRun("each run must be an object")

    def text(name: str, required: bool = True, limit: int = 200) -> Optional[str]:
        value = row.get(name)
        if value is None and not required:
            return None
        if not isinstance(value, str) or (required and not value):
            raise InvalidRun(f"{name} must be a non-empty string")
        if len(value) > limit:
            raise InvalidRun(f"{name} is longer than {limit} characters")
        return value

    def number(name: str, kind: type) -> Optional[float]:
        value = row.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InvalidRun(f"{name} must be a number")
        return kind(value)

    run_uuid = text("run_uuid")
    try:
        uuid.UUID(run_uuid)
    except ValueError:
        raise InvalidRun("run_uuid is not a uuid") from None
    # The row's own `device` is not trusted or checked: the token decides
    # (see the module docstring).
    repo = text("repo")
    if not REPO_RE.match(repo):
        raise InvalidRun(f"repo {repo!r} is not owner/name")
    timestamp = text("timestamp")
    if state._parse_timestamp(timestamp) is None:
        raise InvalidRun(f"timestamp {timestamp!r} is not ISO 8601")
    outcome = text("outcome")
    if outcome not in state.KNOWN_OUTCOMES:
        raise InvalidRun(
            f"outcome {outcome!r} is unknown to this hub (gardener {__version__}); upgrade the hub"
        )
    return state.Run(
        run_uuid=run_uuid,
        device=device,
        repo=repo,
        timestamp=timestamp,
        mode=text("mode", limit=64),
        outcome=outcome,
        gap_summary=text("gap_summary", required=False, limit=MAX_SUMMARY_CHARS),
        exit_code=number("exit_code", int),
        duration_ms=number("duration_ms", int),
        cost_usd=number("cost_usd", float),
        claude_session_id=text("claude_session_id", required=False),
    )


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gardener hub · sign in</title>
<style>
  :root { --bg: #f6f7f4; --panel: #fff; --text: #1d241c; --muted: #5d6a5a; --border: #d8ddd3; --accent: #2f7d32; --err: #b3261e; }
  @media (prefers-color-scheme: dark) { :root { --bg: #131812; --panel: #1b2219; --text: #e3e9df; --muted: #9aa894; --border: #2e392b; --accent: #7bc47f; --err: #f2b8b5; } }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--text); font: 15px/1.5 system-ui, sans-serif; padding: 16px; box-sizing: border-box; }
  form { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 1.5rem; width: 100%; max-width: 20rem; display: grid; gap: 0.75rem; }
  h1 { font-size: 1.1rem; margin: 0 0 0.25rem; }
  label { display: grid; gap: 0.25rem; font-size: 0.85rem; color: var(--muted); }
  input { font: inherit; padding: 0.5rem; border-radius: 8px; border: 1px solid var(--border); background: var(--bg); color: var(--text); }
  button { font: inherit; padding: 0.55rem; border-radius: 8px; border: 0; background: var(--accent); color: #fff; cursor: pointer; }
  .err { color: var(--err); font-size: 0.85rem; margin: 0; }
</style></head>
<body><form method="post" action="/login">
  <h1><span aria-hidden="true">🌱</span> gardener hub</h1>
  $error
  <label>Username <input name="username" autocomplete="username" required autofocus></label>
  <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
  <button type="submit">Sign in</button>
</form></body></html>
"""


class HubHandler(dashboard._DashboardHandler):
    """The dashboard's handler, behind auth, over the hub's store, plus the
    `/api/v1/` endpoints devices push to and read from."""

    data_dir: Optional[Path] = None
    auth: ServerAuth = ServerAuth()
    userauth: Optional[UserAuthClient] = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gardener.sqlite3"

    # -- helpers ---------------------------------------------------------

    def _json(self, code: int, payload: dict, extra_headers: Optional[dict] = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, cookie: Optional[str] = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _cookie_token(self) -> Optional[str]:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and value:
                return value
        return None

    def _identify(self) -> tuple[Optional[str], Optional[str]]:
        """(operator, device) for this request; either may be None."""
        header = self.headers.get("Authorization") or ""
        scheme, _, credential = header.partition(" ")
        if scheme.lower() == "bearer" and credential:
            return None, self.auth.device_for_token(credential.strip())
        if scheme.lower() == "basic" and credential:
            try:
                userpass = base64.b64decode(credential.strip(), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None, None
            return self.auth.basic_ok(userpass), None
        token = self._cookie_token()
        if token and self.userauth is not None:
            username = self.userauth.username_for(token)
            if username and username in self.auth.operators:
                return username, None
        return None, None

    def _deny(self, browser: bool) -> None:
        if browser and self.userauth is not None:
            self._redirect("/login")
            return
        headers = {}
        if self.auth.operator_basic_digest and self.userauth is None:
            headers["WWW-Authenticate"] = 'Basic realm="gardener hub", charset="UTF-8"'
        self._json(401, {"error": "unauthorized"}, headers)

    def _read_body(self) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": f"body must be at most {MAX_BODY_BYTES} bytes"})
            return None
        return self.rfile.read(length)

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            self._send(200, "text/plain; charset=utf-8", b"ok\n")
            return
        if path == "/login":
            if self.userauth is None:
                self._send(404, "text/plain; charset=utf-8", b"sign-in is not enabled on this hub\n")
                return
            page = LOGIN_PAGE.replace("$error", "")
            self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
            return
        if path == "/logout":
            token = self._cookie_token()
            if token and self.userauth is not None:
                self.userauth.logout(token)
            self._redirect(
                "/login" if self.userauth is not None else "/",
                f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict",
            )
            return

        operator, device = self._identify()
        if path.startswith("/api/v1/"):
            if operator is None and device is None:
                self._deny(browser=False)
                return
            self._api_get(path, parsed.query)
            return
        if operator is None:
            self._deny(browser=path in ("/", "/index.html"))
            return
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", dashboard.PAGE_HTML.encode("utf-8"))
        elif path == "/api/status":
            try:
                lists = union_device_lists(self.db_path)
                payload = dashboard.build_status(
                    state_dir=self.data_dir,
                    garden_repos=lists["garden"],
                    allowed_repos=lists["merge_allowlist"],
                    hub=True,
                    **dashboard._status_query(parsed.query),
                )
                payload["hub_user"] = operator if self._cookie_token() else None
            except Exception as exc:  # noqa: BLE001 - a poll must get a real 500, never zero bytes
                print(f"gardener hub: /api/status failed: {exc!r}", file=sys.stderr)
                self._json(500, {"error": type(exc).__name__, "detail": str(exc)})
                return
            self._json(200, payload)
        elif path in ("/live", "/api/live"):
            self._send(
                404, "text/plain; charset=utf-8",
                b"the live view reads a device's own logs; open it on the device that is dispatching\n",
            )
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def _api_get(self, path: str, query: str) -> None:
        params = urllib.parse.parse_qs(query)
        if path == "/api/v1/runs":
            try:
                limit = max(1, min(dashboard.MAX_RUN_LIMIT, int(params.get("limit", ["20"])[0])))
            except ValueError:
                limit = 20
            repo = params.get("repo", [None])[0]
            runs = state.list_runs(db_path=self.db_path, repo=repo, limit=limit)
            self._json(200, {"runs": [run_to_wire(r) for r in runs]})
        elif path == "/api/v1/latest-success":
            repo = params.get("repo", [None])[0]
            mode = params.get("mode", [None])[0]
            if not repo or not mode:
                self._json(400, {"error": "repo and mode are required"})
                return
            when = state.latest_success_at(repo, mode, db_path=self.db_path)
            self._json(200, {"latest_success_at": when.isoformat() if when else None})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        path = urllib.parse.urlparse(self.path).path
        if path == "/login" and self.userauth is not None:
            self._login()
        elif path == "/api/v1/runs":
            self._push()
        else:
            self._json(404, {"error": "not found"})

    def _login(self) -> None:
        raw = self._read_body()
        if raw is None:
            return
        form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        username = (form.get("username") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        token = None
        if username in self.auth.operators and password:
            token = self.userauth.login(username, password)
        if not token:
            # One message for a wrong password and a user not on the
            # allowlist, so the page doesn't reveal which usernames exist.
            page = LOGIN_PAGE.replace("$error", '<p class="err" role="alert">Sign-in failed.</p>')
            self._send(401, "text/html; charset=utf-8", page.encode("utf-8"))
            return
        self._redirect(
            "/", f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict",
        )

    def _push(self) -> None:
        header = self.headers.get("Authorization") or ""
        scheme, _, credential = header.partition(" ")
        device = self.auth.device_for_token(credential.strip()) if scheme.lower() == "bearer" else None
        if device is None:
            self._json(401, {"error": "a device token is required to push"})
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._json(400, {"error": "body is not JSON"})
            return
        rows = body.get("runs") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            self._json(400, {"error": "runs must be a list"})
            return
        if len(rows) > MAX_BATCH:
            self._json(400, {"error": f"at most {MAX_BATCH} runs per push"})
            return
        try:
            runs = [run_from_wire(row, device) for row in rows]
        except InvalidRun as e:
            self._json(400, {"error": str(e)})
            return
        try:
            held = state.insert_runs(runs, db_path=self.db_path)
            lists = {
                name: [r for r in body[name] if isinstance(r, str)]
                for name in KNOWN_LISTS
                if isinstance(body.get(name), list)
            }
            if lists:
                store_device_lists(self.db_path, device, lists)
        except sqlite3.Error as e:
            print(f"gardener hub: push from {device} failed: {e!r}", file=sys.stderr)
            self._json(503, {"error": "store unavailable, retry later"})
            return
        self._json(200, {"held": held})


def prepare_store(data_dir: Path) -> Path:
    """Create the hub's store if needed and put it in WAL mode, so the
    dashboard's reads don't block behind a device's push (the case
    `state._connect`'s docstring names WAL as the fix for)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "gardener.sqlite3"
    with closing(state._connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(DEVICE_LISTS_SCHEMA)
        conn.commit()
    return db_path


def serve(
    host: str,
    port: int,
    data_dir: Path,
    env: Mapping[str, str] = os.environ,
) -> int:
    auth = ServerAuth.from_env(env)
    problems = auth.problems()
    if problems:
        for p in problems:
            print(f"gardener hub: refusing to start — {p}", file=sys.stderr)
        return 2
    if not auth.device_tokens:
        print(
            f"gardener hub: NOTE — {DEVICE_TOKENS_ENV} is empty, so no device can push yet "
            "(`gardener hub token --device NAME` mints one)",
            file=sys.stderr,
        )
    prepare_store(data_dir)
    HubHandler.data_dir = data_dir
    HubHandler.auth = auth
    HubHandler.userauth = UserAuthClient(auth.userauth_url) if auth.userauth_url else None
    httpd = ThreadingHTTPServer((host, port), HubHandler)
    modes = [m for m, on in (("basic", auth.operator_basic_digest), ("UserAuth", auth.userauth_url)) if on]
    print(
        f"gardener hub: serving {data_dir} on http://{host}:{port} "
        f"({len(auth.device_tokens)} device token(s); operator sign-in: {', '.join(modes)})",
        file=sys.stderr,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
