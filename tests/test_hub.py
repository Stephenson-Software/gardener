"""Tests for `gardener/hub.py` (RFC 0007).

Every test runs a real hub on a loopback port with a temp store, and the
device side against a real local sqlite file, so a push is the actual
HTTP round trip and the actual inserts. UserAuth is a loopback stub that
speaks the contract `hub.UserAuthClient` documents; nothing here reaches
a real hub or a real UserAuth.
"""
import base64
import http.client
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import time
import uuid
from contextlib import closing, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gardener import cli, dev_loop, hub, state

DEVICE_TOKEN = "box-token-for-tests"
PHONE_TOKEN = "phone-token-for-tests"
OPERATOR = "operator:correct horse"


def _env(**extra):
    env = {
        hub.DEVICE_TOKENS_ENV: (
            f"box:{hub.token_digest(DEVICE_TOKEN)},phone:{hub.token_digest(PHONE_TOKEN)}"
        ),
        hub.OPERATOR_BASIC_ENV: hub.token_digest(OPERATOR),
    }
    env.update(extra)
    return env


class _HubFixture:
    """A real hub on 127.0.0.1:<free port>, serving a temp data dir."""

    def __init__(self, env):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "hub"
        auth = hub.ServerAuth.from_env(env)
        hub.prepare_store(self.data_dir)
        handler = type("TestHubHandler", (hub.HubHandler,), {
            "data_dir": self.data_dir,
            "auth": auth,
            "userauth": hub.UserAuthClient(auth.userauth_url) if auth.userauth_url else None,
        })
        self.handler_userauth = handler.userauth
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def db_path(self):
        return self.data_dir / "gardener.sqlite3"

    def request(self, method, path, headers=None, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            answer = dict(resp.getheaders())
            answer["cookies"] = [v for k, v in resp.getheaders() if k.lower() == "set-cookie"]
            return resp.status, answer, resp.read()
        finally:
            conn.close()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()


def _jar(headers):
    """The Cookie header a browser would send after these Set-Cookies."""
    pairs = (c.split(";", 1)[0] for c in headers["cookies"])
    return {"Cookie": "; ".join(pairs)}


def _named(headers, name):
    return next(c for c in headers["cookies"] if c.startswith(name + "="))


def _basic(userpass):
    return {"Authorization": "Basic " + base64.b64encode(userpass.encode()).decode()}


class _HubTestCase(unittest.TestCase):
    hub_env = None

    def setUp(self):
        self.hub = _HubFixture(self.hub_env or _env())
        self.addCleanup(self.hub.close)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        self.db_path = self.state_dir / "gardener.sqlite3"
        env = patch.dict(os.environ, {
            "GARDENER_STATE_DIR": str(self.state_dir),
            "GARDENER_DEVICE_NAME": "box",
        })
        env.start()
        self.addCleanup(env.stop)
        for name in (hub.URL_ENV, hub.TOKEN_ENV):
            os.environ.pop(name, None)
        self.config = hub.HubConfig(url=self.hub.url, token=DEVICE_TOKEN)

    def record(self, timestamp="2026-09-26T01:00:00+00:00", repo="owner/a", outcome="tend", **kw):
        run = state.Run(repo=repo, mode="tend", outcome=outcome, timestamp=timestamp, **kw)
        state.record_run(run, db_path=self.db_path)
        return run

    def hub_rows(self):
        with closing(sqlite3.connect(str(self.hub.db_path))) as conn:
            return conn.execute(
                "SELECT run_uuid, device, repo, timestamp FROM runs ORDER BY timestamp"
            ).fetchall()


class TestLoadConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = Path(self._tmp.name)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in (hub.URL_ENV, hub.TOKEN_ENV):
            os.environ.pop(name, None)

    def test_no_hub_is_the_default(self):
        self.assertIsNone(hub.load_config(self.state_dir))

    def test_hub_env_file_configures_a_device(self):
        (self.state_dir / "hub.env").write_text(
            f"{hub.URL_ENV}=https://hub.example/\n{hub.TOKEN_ENV}=secret\n"
        )
        self.assertEqual(
            hub.load_config(self.state_dir), hub.HubConfig("https://hub.example", "secret")
        )

    def test_env_var_wins_over_the_file(self):
        (self.state_dir / "hub.env").write_text(f"{hub.URL_ENV}=https://file.example\n")
        os.environ[hub.URL_ENV] = "https://env.example"
        self.assertEqual(hub.load_config(self.state_dir).url, "https://env.example")

    def test_a_url_without_a_token_is_a_push_error_not_a_crash(self):
        os.environ[hub.URL_ENV] = "https://hub.example"
        config = hub.load_config(self.state_dir)
        self.assertIsNone(config.token)
        with self.assertRaises(hub.HubError):
            hub._request(config, "GET", "/api/v1/runs")


class TestServerAuth(unittest.TestCase):
    def test_refuses_to_start_with_no_operator_credential(self):
        auth = hub.ServerAuth.from_env({hub.DEVICE_TOKENS_ENV: ""})
        self.assertTrue(any("no operator credential" in p for p in auth.problems()))

    def test_userauth_without_an_allowlist_is_refused(self):
        auth = hub.ServerAuth.from_env({hub.USERAUTH_URL_ENV: "http://userauth:9998"})
        self.assertTrue(any(hub.OPERATORS_ENV in p for p in auth.problems()))

    def test_either_operator_mode_alone_is_enough(self):
        self.assertEqual(hub.ServerAuth.from_env(_env()).problems(), [])
        self.assertEqual(hub.ServerAuth.from_env({
            hub.USERAUTH_URL_ENV: "http://userauth:9998", hub.OPERATORS_ENV: "dan",
        }).problems(), [])

    def test_a_malformed_device_token_entry_is_an_error(self):
        with self.assertRaises(ValueError):
            hub.ServerAuth.from_env({hub.DEVICE_TOKENS_ENV: "box:not-a-digest"})

    def test_serve_exits_2_without_listening_when_misconfigured(self):
        with tempfile.TemporaryDirectory() as d, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(hub.serve("127.0.0.1", 0, Path(d), env={}), 2)
        self.assertIn("refusing to start", err.getvalue())

    def test_serve_exits_2_rather_than_raising_on_a_malformed_device_token_entry(self):
        env = dict(_env(), **{hub.DEVICE_TOKENS_ENV: "box:not-a-digest"})
        with tempfile.TemporaryDirectory() as d, redirect_stderr(io.StringIO()) as err:
            self.assertEqual(hub.serve("127.0.0.1", 0, Path(d), env=env), 2)
        self.assertIn("refusing to start", err.getvalue())
        self.assertIn(hub.DEVICE_TOKENS_ENV, err.getvalue())

    def test_a_mis_pasted_raw_token_is_never_echoed(self):
        token = hub.mint_token()
        for entry in (token, f"phone:{token}"):
            with self.subTest(entry=entry):
                env = {hub.DEVICE_TOKENS_ENV: f"box:{'0' * 64},{entry}"}
                with self.assertRaises(ValueError) as caught:
                    hub.ServerAuth.from_env(env)
                self.assertIn("entry 2", str(caught.exception))
                self.assertNotIn(token[:8], str(caught.exception))


class TestPush(_HubTestCase):
    def test_push_copies_the_outbox_and_marks_it_pushed(self):
        runs = [self.record(f"2026-09-26T0{n}:00:00+00:00") for n in range(3)]
        result = hub.push_pending(self.config, db_path=self.db_path)
        self.assertEqual((result.pushed, result.remaining, result.error), (3, 0, None))
        self.assertEqual([r[0] for r in self.hub_rows()], [r.run_uuid for r in runs])
        self.assertEqual({r[1] for r in self.hub_rows()}, {"box"})
        self.assertEqual(state.pending_push_summary(db_path=self.db_path), (0, None))

    def test_a_repeated_push_stores_nothing_twice(self):
        # The lost-response case: the hub stored the batch, the device
        # never learned it did, and sends it again.
        self.record()
        hub.push_pending(self.config, db_path=self.db_path)
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.execute("UPDATE runs SET pushed_at = NULL")
            conn.commit()
        result = hub.push_pending(self.config, db_path=self.db_path)
        self.assertEqual((result.pushed, result.error), (1, None))
        self.assertEqual(len(self.hub_rows()), 1)

    def test_the_outbox_drains_in_batches_oldest_first(self):
        for n in range(5):
            self.record(f"2026-09-26T0{n}:00:00+00:00")
        result = hub.push_pending(self.config, db_path=self.db_path, batch_size=2)
        self.assertEqual((result.pushed, result.remaining), (5, 0))

    def test_the_deadline_leaves_the_rest_queued(self):
        for n in range(4):
            self.record(f"2026-09-26T0{n}:00:00+00:00")
        ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0])
        result = hub.push_pending(
            self.config, db_path=self.db_path, batch_size=1,
            deadline_seconds=10, clock=lambda: next(ticks),
        )
        self.assertEqual((result.pushed, result.remaining, result.error), (1, 3, None))

    def test_rows_are_stored_under_the_tokens_device_whatever_they_claim(self):
        # A phone token can't write rows that look like the box's, and a
        # device whose local name drifted (hostname before
        # GARDENER_DEVICE_NAME was set) still delivers its history: the
        # first real backfill refused all 1,769 rows when the row's name
        # had to match the token's.
        self.record(device="phone")
        self.record(timestamp="2026-09-26T02:00:00+00:00", device="Walter")
        result = hub.push_pending(self.config, db_path=self.db_path)
        self.assertEqual((result.pushed, result.error), (2, None))
        self.assertEqual({r[1] for r in self.hub_rows()}, {"box"})

    def test_an_outcome_the_hub_doesnt_know_is_refused_and_stays_queued(self):
        self.record(outcome="some-future-outcome")
        result = hub.push_pending(self.config, db_path=self.db_path)
        self.assertIn("upgrade the hub", result.error)
        self.assertEqual(state.pending_push_summary(db_path=self.db_path)[0], 1)

    def test_an_unknown_token_is_refused(self):
        self.record()
        result = hub.push_pending(hub.HubConfig(self.hub.url, "wrong"), db_path=self.db_path)
        self.assertIn("HTTP 401", result.error)

    def test_an_unreachable_hub_queues_and_never_raises(self):
        run = self.record()
        (self.state_dir / "hub.env").write_text(
            f"{hub.URL_ENV}=http://127.0.0.1:9\n{hub.TOKEN_ENV}=x\n"
        )
        with redirect_stderr(io.StringIO()) as err:
            hub.push_after_record(self.db_path)
        self.assertIn("1 run(s) queued", err.getvalue())
        self.assertEqual(state.list_runs(db_path=self.db_path)[0].run_uuid, run.run_uuid)

    def test_recording_a_run_pushes_it_when_a_hub_is_configured(self):
        (self.state_dir / "hub.env").write_text(
            f"{hub.URL_ENV}={self.hub.url}\n{hub.TOKEN_ENV}={DEVICE_TOKEN}\n"
        )
        run = state.Run(repo="owner/a", mode="tend", outcome="tend", timestamp=state.now_iso())
        cli._safe_record_run(run, self.db_path)
        self.assertEqual([r[0] for r in self.hub_rows()], [run.run_uuid])

    def test_recording_without_a_hub_touches_no_network(self):
        with patch.object(hub, "push_pending") as push:
            cli._safe_record_run(
                state.Run(repo="owner/a", mode="tend", outcome="tend", timestamp=state.now_iso()),
                self.db_path,
            )
        push.assert_not_called()

    def test_a_legacy_history_is_backfilled_by_sync(self):
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.execute(
                "CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, "
                "timestamp TEXT NOT NULL, mode TEXT NOT NULL, gap_summary TEXT, outcome TEXT NOT NULL, "
                "exit_code INTEGER, duration_ms INTEGER, cost_usd REAL, claude_session_id TEXT)"
            )
            conn.executemany(
                "INSERT INTO runs (repo, timestamp, mode, outcome) VALUES (?, ?, 'tend', 'tend')",
                [("owner/a", f"2026-07-2{n}T00:00:00+00:00") for n in range(3)],
            )
            conn.commit()
        (self.state_dir / "hub.env").write_text(
            f"{hub.URL_ENV}={self.hub.url}\n{hub.TOKEN_ENV}={DEVICE_TOKEN}\n"
        )
        args = cli.build_parser().parse_args(["hub", "sync"])
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(args.func(args), 0)
        self.assertIn("pushed 3 run(s); 0 still queued", out.getvalue())
        self.assertEqual(len(self.hub_rows()), 3)
        # A second sync is a no-op, not a second copy.
        with redirect_stdout(io.StringIO()) as out:
            args.func(args)
        self.assertIn("pushed 0 run(s)", out.getvalue())
        self.assertEqual(len(self.hub_rows()), 3)


class TestHubStatusCommand(_HubTestCase):
    def _status(self):
        args = cli.build_parser().parse_args(["hub", "status"])
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = args.func(args)
        return code, out.getvalue(), err.getvalue()

    def test_no_hub_says_local_only(self):
        self.assertEqual(self._status()[:2], (0, "no hub configured (GARDENER_HUB_URL unset): "
                                                "run history is local only\n"))

    def test_reports_the_queue_even_when_the_hub_is_unreachable(self):
        self.record("2026-09-26T01:00:00+00:00")
        self.record("2026-09-26T02:00:00+00:00")
        os.environ[hub.URL_ENV] = "http://127.0.0.1:9"
        os.environ[hub.TOKEN_ENV] = "x"
        code, out, _ = self._status()
        self.assertEqual(code, 0)
        self.assertIn("queued: 2 run(s) not yet pushed, oldest 2026-09-26T01:00:00+00:00", out)
        self.assertIn("on hub: unknown, the hub could not be asked", out)

    def _configure(self):
        os.environ[hub.URL_ENV] = self.hub.url
        os.environ[hub.TOKEN_ENV] = DEVICE_TOKEN

    def test_matches_what_this_device_pushed_under_its_token_name(self):
        """The local name (`box` here, `localhost` on the real phone) and the
        hub's (the token's) differ by design; the hub's `you` bridges them."""
        self._configure()
        for hour in (1, 2, 3):
            self.record(f"2026-09-26T0{hour}:00:00+00:00")
        hub.push_pending(self.config, db_path=self.db_path)
        self.record("2026-09-26T04:00:00+00:00")
        code, out, _ = self._status()
        self.assertIn("queued: 1 run(s)", out)
        self.assertIn("on hub: 3 run(s) as 'box', matching the 3 pushed from here", out)
        self.assertEqual(code, 0)

    def test_a_count_mismatch_exits_1(self):
        self._configure()
        self.record("2026-09-26T01:00:00+00:00")
        hub.push_pending(self.config, db_path=self.db_path)
        stray = state.Run(repo="owner/a", mode="tend", outcome="tend", device="box",
                          timestamp="2026-09-26T05:00:00+00:00", run_uuid=str(uuid.uuid4()))
        state.insert_runs([stray], db_path=self.hub.db_path)
        code, out, _ = self._status()
        self.assertEqual(code, 1)
        self.assertIn("on hub: 2 run(s) as 'box', but 1 were pushed from here: MISMATCH", out)

    def test_a_url_without_a_token_is_an_error(self):
        os.environ[hub.URL_ENV] = "http://127.0.0.1:9"
        code, _, err = self._status()
        self.assertEqual(code, 1)
        self.assertIn(hub.TOKEN_ENV, err)


class TestHubReads(_HubTestCase):
    def _push_from(self, device, token, timestamp, lists):
        run = state.Run(repo=f"owner/{device}", mode="tend", outcome="tend",
                        timestamp=timestamp, device=device,
                        run_uuid=f"00000000-0000-4000-8000-00000000000{len(device)}")
        body = json.dumps({"runs": [hub.run_to_wire(run)], **lists})
        status, _, _ = self.hub.request(
            "POST", "/api/v1/runs", {"Authorization": f"Bearer {token}"}, body
        )
        self.assertEqual(status, 200)

    def test_healthz_needs_no_credential_and_says_nothing(self):
        status, _, body = self.hub.request("GET", "/healthz")
        self.assertEqual((status, body), (200, b"ok\n"))

    def test_the_dashboard_requires_an_operator(self):
        for path in ("/", "/api/status", "/api/v1/runs"):
            status, headers, _ = self.hub.request("GET", path)
            self.assertEqual(status, 401, path)
        status, headers, _ = self.hub.request("GET", "/")
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))

    def test_a_wrong_password_is_refused(self):
        status, _, _ = self.hub.request("GET", "/api/status", _basic("operator:wrong"))
        self.assertEqual(status, 401)

    def test_a_device_token_reads_the_api_but_not_the_dashboard(self):
        bearer = {"Authorization": f"Bearer {DEVICE_TOKEN}"}
        self.assertEqual(self.hub.request("GET", "/api/v1/runs", bearer)[0], 200)
        self.assertEqual(self.hub.request("GET", "/api/status", bearer)[0], 401)

    def test_status_combines_devices_and_their_gardens(self):
        self._push_from("box", DEVICE_TOKEN, "2026-09-26T01:00:00+00:00",
                        {"garden": ["owner/box", "owner/shared"], "merge_allowlist": []})
        self._push_from("phone", PHONE_TOKEN, "2026-09-26T02:00:00+00:00",
                        {"garden": ["owner/phone", "owner/shared"], "merge_allowlist": ["owner/phone"]})
        status, _, body = self.hub.request("GET", "/api/status", _basic(OPERATOR))
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["hub"])
        self.assertTrue(payload["multi_device"])
        self.assertEqual([r["device"] for r in payload["runs"]], ["phone", "box"])
        self.assertEqual(payload["garden"], ["owner/box", "owner/phone", "owner/shared"])
        self.assertEqual(payload["merge_allowlist"], ["owner/phone"])
        self.assertIsNone(payload["hub_user"])

    def test_the_live_view_is_named_per_device_not_served_empty(self):
        status, _, body = self.hub.request("GET", "/live", _basic(OPERATOR))
        self.assertEqual(status, 404)
        self.assertIn(b"device that is dispatching", body)

    def test_status_all_devices_reads_the_hub(self):
        self._push_from("phone", PHONE_TOKEN, "2026-09-26T02:00:00+00:00", {})
        os.environ[hub.URL_ENV] = self.hub.url
        os.environ[hub.TOKEN_ENV] = DEVICE_TOKEN
        args = cli.build_parser().parse_args(["status", "--all-devices"])
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(args.func(args), 0)
        self.assertIn("phone", out.getvalue())
        self.assertIn("owner/phone", out.getvalue())

    def test_latest_success_endpoint_answers_from_the_combined_history(self):
        self._push_from("phone", PHONE_TOKEN, "2026-09-26T02:00:00+00:00", {})
        status, _, body = self.hub.request(
            "GET", "/api/v1/latest-success?repo=owner/phone&mode=tend",
            {"Authorization": f"Bearer {DEVICE_TOKEN}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["latest_success_at"], "2026-09-26T02:00:00+00:00")


class _UserAuthStub(BaseHTTPRequestHandler):
    """Speaks the UserAuth contract `hub.UserAuthClient` documents, including
    single-use refresh tokens whose reuse ends the session."""

    users = {"dan": "pw", "stranger": "pw"}
    validate_calls = 0
    refresh_calls = 0
    reuse_detected = 0
    minted = 0
    #: Access tokens past their expiry, and ones signed out.
    expired: set = set()
    revoked: set = set()
    #: refresh token -> username, and the ones already exchanged.
    live_refresh: dict = {}
    used_refresh: dict = {}
    #: Status to answer instead, per path.
    failing: dict = {}
    refresh_delay = 0.0
    malformed_refresh = False

    @classmethod
    def reset(cls):
        cls.validate_calls = cls.refresh_calls = cls.reuse_detected = cls.minted = 0
        cls.expired, cls.revoked = set(), set()
        cls.live_refresh, cls.used_refresh, cls.failing = {}, {}, {}
        cls.refresh_delay = 0.0
        cls.malformed_refresh = False

    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _issue(self, username):
        stub = type(self)
        stub.minted += 1
        refresh = f"r-{username}-{stub.minted}"
        stub.live_refresh[refresh] = username
        return {"token": f"jwt-{username}.{stub.minted}", "tokenType": "Bearer",
                "expiresAt": "2026-09-26T10:00:00Z", "refreshToken": refresh}

    def _end_sessions_of(self, username):
        stub = type(self)
        stub.live_refresh = {t: u for t, u in stub.live_refresh.items() if u != username}

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        stub = type(self)
        if self.path in self.failing:
            self._json(self.failing[self.path], {"error": "failing"})
        elif self.path == "/login":
            creds = json.loads(body)
            if self.users.get(creds.get("username")) == creds.get("password"):
                self._json(200, self._issue(creds["username"]))
            else:
                self._json(401, {"error": "bad credentials"})
        elif self.path == "/token/refresh":
            stub.refresh_calls += 1
            time.sleep(self.refresh_delay)
            token = json.loads(body)["refreshToken"]
            if token in stub.used_refresh:
                stub.reuse_detected += 1
                self._end_sessions_of(stub.used_refresh[token])
                self._json(401, {"error": "invalid refresh token"})
            elif token not in stub.live_refresh:
                self._json(401, {"error": "invalid refresh token"})
            elif self.malformed_refresh:
                self._json(200, {"token": "evil\r\nSet-Cookie: x=y", "refreshToken": "r"})
            else:
                username = stub.live_refresh.pop(token)
                stub.used_refresh[token] = username
                self._json(200, self._issue(username))
        elif self.path == "/logout":
            token = self.headers.get("Authorization", "")[7:]
            if not self._good(token):
                self._json(401, {"error": "invalid"})
                return
            stub.revoked.add(token)
            self._end_sessions_of(self._subject(token))
            self._json(200, {})

    @staticmethod
    def _subject(token):
        return token[4:].split(".")[0]

    def _good(self, token):
        return token.startswith("jwt-") and token not in self.revoked and token not in self.expired

    def do_GET(self):
        if self.path in self.failing:
            self._json(self.failing[self.path], {"error": "failing"})
        elif self.path == "/session/validate":
            type(self).validate_calls += 1
            token = self.headers.get("Authorization", "")[7:]
            if self._good(token):
                self._json(200, {"valid": True, "username": self._subject(token), "roles": []})
            else:
                self._json(401, {"error": "invalid"})


class TestUserAuthSignIn(_HubTestCase):
    def setUp(self):
        _UserAuthStub.reset()
        self.ua = ThreadingHTTPServer(("127.0.0.1", 0), _UserAuthStub)
        self.ua.daemon_threads = True
        threading.Thread(target=self.ua.serve_forever, daemon=True).start()
        self.addCleanup(self.ua.server_close)
        self.addCleanup(self.ua.shutdown)
        self.hub_env = _env(**{
            hub.USERAUTH_URL_ENV: f"http://127.0.0.1:{self.ua.server_address[1]}",
            hub.OPERATORS_ENV: "dan",
        })
        super().setUp()

    def _login(self, username, password):
        return self.hub.request(
            "POST", "/login", {"Content-Type": "application/x-www-form-urlencoded"},
            f"username={username}&password={password}",
        )

    def test_an_anonymous_browser_is_sent_to_sign_in(self):
        status, headers, _ = self.hub.request("GET", "/")
        self.assertEqual((status, headers.get("Location")), (303, "/login"))
        self.assertEqual(self.hub.request("GET", "/login")[0], 200)

    def test_signing_in_sets_a_session_the_dashboard_accepts(self):
        status, headers, _ = self._login("dan", "pw")
        self.assertEqual(status, 303)
        self.assertEqual(len(headers["cookies"]), 2)
        for cookie in headers["cookies"]:
            for attr in ("HttpOnly", "Secure", "SameSite=Strict"):
                self.assertIn(attr, cookie)
        status, _, body = self.hub.request("GET", "/api/status", _jar(headers))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["hub_user"], "dan")

    def test_a_valid_userauth_account_off_the_allowlist_cannot_sign_in(self):
        # UserAuth registration is open; the allowlist is the authorisation.
        status, headers, body = self._login("stranger", "pw")
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)
        self.assertIn(b"Sign-in failed", body)

    def test_a_userauth_token_minted_elsewhere_is_refused_off_the_allowlist(self):
        # Registration at UserAuth is open, so anyone can get a valid
        # token without going through this hub's /login and set the cookie
        # by hand. The allowlist has to hold on every request, not only at
        # sign-in.
        session = {"Cookie": f"{hub.SESSION_COOKIE}=jwt-stranger"}
        self.assertEqual(self.hub.request("GET", "/api/status", session)[0], 401)
        self.assertEqual(self.hub.request("GET", "/", session)[0], 303)

    def test_a_wrong_password_and_an_unknown_user_look_the_same(self):
        self.assertEqual(self._login("dan", "nope")[2], self._login("stranger", "pw")[2])

    def test_session_checks_are_cached(self):
        _, headers, _ = self._login("dan", "pw")
        session = _jar(headers)
        for _ in range(3):
            self.assertEqual(self.hub.request("GET", "/api/status", session)[0], 200)
        self.assertEqual(_UserAuthStub.validate_calls, 1)

    def test_signing_out_revokes_the_session(self):
        _, headers, _ = self._login("dan", "pw")
        session = _jar(headers)
        status, headers, _ = self.hub.request("GET", "/logout", session)
        self.assertEqual(status, 303)
        self.assertEqual(len(headers["cookies"]), 2)
        for cookie in headers["cookies"]:
            self.assertIn("Max-Age=0", cookie)
        self.assertEqual(self.hub.request("GET", "/api/status", session)[0], 401)
        self.assertEqual(_UserAuthStub.live_refresh, {})

    def test_basic_auth_still_works_alongside_sign_in(self):
        self.assertEqual(self.hub.request("GET", "/api/status", _basic(OPERATOR))[0], 200)

    # -- refresh ---------------------------------------------------------

    def _signed_in(self):
        return _jar(self._login("dan", "pw")[1])

    def _expire_access(self, session):
        access = session["Cookie"].split(";")[0].split("=", 1)[1]
        _UserAuthStub.expired.add(access)

    def test_only_the_refresh_cookie_outlives_the_browser_session(self):
        _, headers, _ = self._login("dan", "pw")
        self.assertIn(f"Max-Age={30 * 24 * 3600}", _named(headers, hub.REFRESH_COOKIE))
        self.assertNotIn("Max-Age", _named(headers, hub.SESSION_COOKIE))

    def test_an_expired_access_token_is_renewed_and_the_new_cookies_are_used(self):
        session = self._signed_in()
        self._expire_access(session)
        status, headers, body = self.hub.request("GET", "/api/status", session)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["hub_user"], "dan")
        renewed = _jar(headers)
        self.assertNotEqual(renewed, session)
        for _ in range(3):
            status, headers, _ = self.hub.request("GET", "/api/status", renewed)
            self.assertEqual((status, headers["cookies"]), (200, []))
        self.assertEqual(_UserAuthStub.refresh_calls, 1)

    def test_a_browser_restart_signs_back_in_from_the_refresh_cookie(self):
        _, headers, _ = self._login("dan", "pw")
        reopened = {"Cookie": _named(headers, hub.REFRESH_COOKIE).split(";", 1)[0]}
        status, headers, _ = self.hub.request("GET", "/", reopened)
        self.assertEqual(status, 200)
        self.assertEqual(len(headers["cookies"]), 2)

    def test_a_refused_refresh_signs_out_and_stops_trying(self):
        session = self._signed_in()
        self._expire_access(session)
        _UserAuthStub.live_refresh.clear()
        status, headers, _ = self.hub.request("GET", "/api/status", session)
        self.assertEqual(status, 401)
        self.assertEqual(len(headers["cookies"]), 2)
        for cookie in headers["cookies"]:
            self.assertIn("Max-Age=0", cookie)
        status, headers, _ = self.hub.request("GET", "/", session)
        self.assertEqual((status, headers.get("Location")), (303, "/login"))

    def test_an_outage_at_validate_leaves_the_session_alone(self):
        session = self._signed_in()
        _UserAuthStub.failing["/session/validate"] = 503
        for path, code in (("/api/status", 503), ("/", 503)):
            status, headers, _ = self.hub.request("GET", path, session)
            self.assertEqual((status, headers["cookies"]), (code, []))
            self.assertNotIn("Location", headers)
        self.assertEqual(_UserAuthStub.refresh_calls, 0)
        _UserAuthStub.failing.clear()
        self.assertEqual(self.hub.request("GET", "/api/status", session)[0], 200)

    def test_an_outage_or_rate_limit_at_refresh_leaves_the_session_alone(self):
        for failure in (503, 429, 500):
            with self.subTest(failure):
                session = self._signed_in()
                self._expire_access(session)
                _UserAuthStub.failing["/token/refresh"] = failure
                status, headers, _ = self.hub.request("GET", "/api/status", session)
                self.assertEqual((status, headers["cookies"]), (503, []))
                del _UserAuthStub.failing["/token/refresh"]
                self.hub.handler_userauth._refreshed.clear()
                self.assertEqual(self.hub.request("GET", "/api/status", session)[0], 200)

    def test_userauth_being_unreachable_is_an_outage_not_a_refusal(self):
        client = hub.UserAuthClient("http://127.0.0.1:9")
        with self.assertRaises(hub.UserAuthUnavailable):
            client.username_for("jwt-dan")
        with self.assertRaises(hub.UserAuthUnavailable):
            client.refresh("r")

    def test_tokens_issued_during_an_outage_still_reach_the_browser(self):
        # The old refresh token is spent the moment the exchange lands, so
        # failing the check of the new access token must not lose them.
        session = self._signed_in()
        self._expire_access(session)
        real = hub.UserAuthClient.username_for
        calls = []

        def flaky(client, token):
            calls.append(token)
            if len(calls) > 1:
                raise hub.UserAuthUnavailable("/session/validate")
            return real(client, token)

        with patch.object(hub.UserAuthClient, "username_for", flaky):
            status, headers, _ = self.hub.request("GET", "/api/status", session)
        self.assertEqual(status, 503)
        self.assertEqual(len(headers["cookies"]), 2)
        self.assertEqual(self.hub.request("GET", "/api/status", _jar(headers))[0], 200)

    def test_racing_requests_exchange_the_refresh_token_once(self):
        session = self._signed_in()
        self._expire_access(session)
        _UserAuthStub.refresh_delay = 0.2
        results = []

        def poll():
            results.append(self.hub.request("GET", "/api/status", session))

        threads = [threading.Thread(target=poll) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([r[0] for r in results], [200] * 8)
        self.assertEqual((_UserAuthStub.refresh_calls, _UserAuthStub.reuse_detected), (1, 0))
        self.assertEqual(len({_named(r[1], hub.REFRESH_COOKIE) for r in results}), 1)

    def test_a_straggler_with_the_old_cookies_gets_the_same_successor(self):
        session = self._signed_in()
        self._expire_access(session)
        first = self.hub.request("GET", "/api/status", session)
        second = self.hub.request("GET", "/api/status", session)
        self.assertEqual((first[0], second[0]), (200, 200))
        self.assertEqual(first[1]["cookies"], second[1]["cookies"])
        self.assertEqual((_UserAuthStub.refresh_calls, _UserAuthStub.reuse_detected), (1, 0))

    def test_the_grace_ends_and_the_next_exchange_reaches_userauth(self):
        now = [0.0]
        client = hub.UserAuthClient(
            f"http://127.0.0.1:{self.ua.server_address[1]}", clock=lambda: now[0])
        _UserAuthStub.live_refresh["r-dan-x"] = "dan"
        tokens = client.refresh("r-dan-x")
        now[0] = hub.REFRESH_GRACE_SECONDS - 1
        self.assertEqual(client.refresh("r-dan-x"), tokens)
        self.assertEqual(_UserAuthStub.refresh_calls, 1)
        now[0] = hub.REFRESH_GRACE_SECONDS + 1
        self.assertIsNone(client.refresh("r-dan-x"))
        self.assertEqual(_UserAuthStub.refresh_calls, 2)
        self.assertEqual(client._refreshed.keys(), {hub.token_digest("r-dan-x")})

    def test_a_waiting_request_sees_the_leaders_outage_as_an_outage(self):
        client = hub.UserAuthClient(f"http://127.0.0.1:{self.ua.server_address[1]}")
        _UserAuthStub.failing["/token/refresh"] = 503
        client._flights[hub.token_digest("r")] = done = threading.Event()
        done.set()
        with self.assertRaises(hub.UserAuthUnavailable):
            client.refresh("r")

    def test_signing_out_with_an_expired_access_token_still_ends_the_session(self):
        session = self._signed_in()
        self._expire_access(session)
        self.assertEqual(self.hub.request("GET", "/logout", session)[0], 303)
        self.assertEqual(_UserAuthStub.live_refresh, {})
        self.assertEqual(self.hub.request("GET", "/api/status", session)[0], 401)

    def test_signing_out_after_a_browser_restart_still_ends_the_session(self):
        _, headers, _ = self._login("dan", "pw")
        reopened = {"Cookie": _named(headers, hub.REFRESH_COOKIE).split(";", 1)[0]}
        self.assertEqual(self.hub.request("GET", "/logout", reopened)[0], 303)
        self.assertEqual(_UserAuthStub.live_refresh, {})

    def test_a_session_from_before_refresh_lasts_until_its_token_expires(self):
        session = {"Cookie": f"{hub.SESSION_COOKIE}=jwt-dan.old"}
        self.assertEqual(self.hub.request("GET", "/api/status", session)[0], 200)
        fresh = {"Cookie": f"{hub.SESSION_COOKIE}=jwt-dan.expired"}
        _UserAuthStub.expired.add("jwt-dan.expired")
        self.assertEqual(self.hub.request("GET", "/api/status", fresh)[0], 401)
        self.assertEqual(_UserAuthStub.refresh_calls, 0)

    def test_a_refresh_token_of_someone_off_the_allowlist_gets_nothing(self):
        _UserAuthStub.live_refresh["r-stranger-1"] = "stranger"
        session = {"Cookie": f"{hub.REFRESH_COOKIE}=r-stranger-1"}
        self.assertEqual(self.hub.request("GET", "/api/status", session)[0], 401)

    def test_a_malformed_token_never_reaches_a_response_header(self):
        session = self._signed_in()
        self._expire_access(session)
        _UserAuthStub.malformed_refresh = True
        status, headers, _ = self.hub.request("GET", "/api/status", session)
        self.assertEqual((status, headers["cookies"]), (503, []))


class TestDeviceHealth(_HubTestCase):
    """What only the hub can see: each device's last run, its latest
    session, whether it has gone quiet, and the daily digest over all of
    them. `now` is injected; the stores are real."""

    NOW = hub.datetime(2026, 9, 27, 12, 0, tzinfo=hub.timezone.utc)

    def put(self, device, timestamp, outcome="tend", repo="owner/a", cost=0.5):
        state.insert_runs([state.Run(
            repo=repo, mode="tend", outcome=outcome, timestamp=timestamp, device=device,
            cost_usd=cost, run_uuid=str(uuid.uuid4()),
        )], db_path=self.hub.db_path)

    def summaries(self):
        return {d["device"]: d for d in hub.device_summaries(self.hub.db_path, now=self.NOW)}

    def test_a_device_quiet_past_36_hours_is_flagged_and_one_within_it_is_not(self):
        self.put("box", "2026-09-27T03:00:00+00:00")
        self.put("phone", "2026-09-25T23:00:00+00:00")  # 37 h before NOW
        got = self.summaries()
        self.assertFalse(got["box"]["quiet"])
        self.assertEqual(got["box"]["quiet_seconds"], 9 * 3600)
        self.assertTrue(got["phone"]["quiet"])

    def test_the_36_hour_boundary(self):
        self.put("box", "2026-09-26T00:00:00+00:00")  # exactly 36 h
        self.assertFalse(self.summaries()["box"]["quiet"])

    def test_sessions_are_per_device_so_nights_do_not_interleave(self):
        """The box's all-error night must not be diluted by the phone's
        successful runs recorded in between."""
        for minute in (0, 20, 40):
            self.put("box", f"2026-09-27T02:{minute:02d}:00+00:00", outcome="error")
            self.put("phone", f"2026-09-27T02:{minute + 10:02d}:00+00:00")
        got = self.summaries()
        self.assertEqual(got["box"]["last_session"]["runs"], 3)
        self.assertTrue(got["box"]["last_session"]["all_errors"])
        self.assertFalse(got["phone"]["last_session"]["all_errors"])

    def test_one_success_clears_all_errors(self):
        self.put("box", "2026-09-27T02:00:00+00:00", outcome="error")
        self.put("box", "2026-09-27T02:30:00+00:00")
        self.assertFalse(self.summaries()["box"]["last_session"]["all_errors"])

    def test_the_devices_endpoint_names_the_callers_device(self):
        self.put("box", "2026-09-27T03:00:00+00:00")
        status, _, body = self.hub.request(
            "GET", "/api/v1/devices", {"Authorization": f"Bearer {PHONE_TOKEN}"})
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["you"], "phone")
        self.assertEqual([d["device"] for d in payload["devices"]], ["box"])
        status, _, body = self.hub.request("GET", "/api/v1/devices", _basic(OPERATOR))
        self.assertIsNone(json.loads(body)["you"])
        self.assertEqual(self.hub.request("GET", "/api/v1/devices")[0], 401)

    def test_devices_command_prints_json_for_the_watchdog(self):
        self.put("box", "2026-09-27T03:00:00+00:00", outcome="error")
        args = cli.build_parser().parse_args(
            ["hub", "devices", "--json", "--data-dir", str(self.hub.data_dir)])
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(args.func(args), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["quiet_after_seconds"], 36 * 3600)
        self.assertTrue(payload["devices"][0]["last_session"]["all_errors"])

    def test_digest_counts_the_window_per_device(self):
        self.put("box", "2026-09-26T11:00:00+00:00", cost=9.0)  # 25 h ago: outside
        self.put("box", "2026-09-27T02:00:00+00:00", cost=1.25)
        self.put("box", "2026-09-27T03:00:00+00:00", outcome="error", repo="owner/broken", cost=0.25)
        self.put("phone", "2026-09-25T20:00:00+00:00")
        text = hub.digest(self.hub.db_path, hours=24, now=self.NOW)
        self.assertIn("**box**: 2 run(s), 1 ok, 1 error(s), $1.50", text)
        self.assertIn("  errored: owner/broken", text)
        self.assertIn("**phone**: no runs in the last 24 h (last run 2026-09-25T20:00:00+00:00)", text)
        self.assertIn("Total: 2 run(s), $1.50 over the last 24 h.", text)
        self.assertIn("Quiet for over 36 h: phone", text)

    def test_digest_of_an_empty_hub(self):
        self.assertEqual(hub.digest(self.hub.db_path, now=self.NOW),
                         "No runs have reached the hub in the last 24 h.")


class TestOrphanCheckConsultsHub(_HubTestCase):
    """RFC 0007 phase 2: `find_orphaned_pr` hides a marked PR that a
    completed tend on ANY device postdates, taking the later of this
    device's history and the hub's, and never fails over the hub. `gh` is
    mocked; the hub and both stores are real."""

    PR_CREATED = "2026-09-24T10:00:00Z"
    BEFORE = "2026-09-24T09:00:00+00:00"
    AFTER = "2026-09-24T11:00:00+00:00"

    def setUp(self):
        super().setUp()
        self.configure(self.hub.url, DEVICE_TOKEN)
        which = patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
        which.start()
        self.addCleanup(which.stop)
        gh = patch("gardener.cli._run", return_value=subprocess.CompletedProcess(
            args=["gh"], returncode=0, stderr="", stdout=json.dumps([
                {"number": 151, "headRefName": "feature/x", "body": dev_loop.ORPHAN_MARKER,
                 "createdAt": self.PR_CREATED},
            ]),
        ))
        gh.start()
        self.addCleanup(gh.stop)

    def configure(self, url, token=None):
        lines = [f"{hub.URL_ENV}={url}"] + ([f"{hub.TOKEN_ENV}={token}"] if token else [])
        (self.state_dir / hub.CONFIG_FILENAME).write_text("\n".join(lines) + "\n")

    def phone_tend(self, timestamp, outcome="tend"):
        run = state.Run(repo="owner/a", mode="tend", outcome=outcome, timestamp=timestamp,
                        device="phone", run_uuid=str(uuid.uuid4()))
        status, _, _ = self.hub.request(
            "POST", "/api/v1/runs", {"Authorization": f"Bearer {PHONE_TOKEN}"},
            json.dumps({"runs": [hub.run_to_wire(run)]}),
        )
        self.assertEqual(status, 200)

    def orphan(self):
        with redirect_stderr(io.StringIO()) as err:
            found = cli.find_orphaned_pr("owner/a", db_path=self.db_path)
        return (found.number if found else None), err.getvalue()

    def test_a_hand_off_on_another_device_is_not_an_orphan_here(self):
        self.record(timestamp=self.BEFORE)
        self.phone_tend(self.AFTER)
        self.assertEqual(self.orphan(), (None, ""))

    def test_the_later_answer_wins_when_it_is_this_devices(self):
        """The hub holds only the phone's older success; this device's own
        newer one must still hide the PR. Taking the earlier answer (or the
        hub's over local) would call it an orphan."""
        self.record(timestamp=self.AFTER)
        self.phone_tend(self.BEFORE)
        self.assertEqual(self.orphan(), (None, ""))

    def test_a_failed_run_on_another_device_is_not_a_hand_off(self):
        self.record(timestamp=self.BEFORE)
        self.phone_tend(self.AFTER, outcome="error")
        self.assertEqual(self.orphan(), (151, ""))

    def test_an_unreachable_hub_falls_back_to_local_with_one_note(self):
        self.configure("http://127.0.0.1:9", DEVICE_TOKEN)
        self.record(timestamp=self.AFTER)
        number, err = self.orphan()
        self.assertIsNone(number)
        self.assertEqual(err.count("NOTE"), 1)
        self.assertIn("this device's history only", err)

    def test_a_hub_refusal_still_finds_the_orphan_from_local_history(self):
        self.configure(self.hub.url, "not-a-device-token")
        self.record(timestamp=self.BEFORE)
        number, err = self.orphan()
        self.assertEqual(number, 151)
        self.assertIn("HTTP 401", err)

    def test_no_hub_configured_asks_nothing(self):
        (self.state_dir / hub.CONFIG_FILENAME).unlink()
        self.record(timestamp=self.BEFORE)
        self.phone_tend(self.AFTER)
        with patch("gardener.hub.fetch_latest_success") as fetch:
            self.assertEqual(self.orphan(), (151, ""))
        fetch.assert_not_called()

    def test_an_unreadable_hub_answer_is_an_error_not_a_hand_off(self):
        config = hub.HubConfig(url=self.hub.url, token=DEVICE_TOKEN)
        with patch("gardener.hub._request", return_value={"latest_success_at": "yesterday"}):
            with self.assertRaises(hub.HubError):
                hub.fetch_latest_success(config, "owner/a", "tend")
        with patch("gardener.hub._request", return_value={"latest_success_at": None}):
            self.assertIsNone(hub.fetch_latest_success(config, "owner/a", "tend"))


class TestMintToken(unittest.TestCase):
    def test_token_command_prints_a_token_and_only_its_digest_for_the_hub(self):
        args = cli.build_parser().parse_args(["hub", "token", "--device", "phone"])
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(args.func(args), 0)
        lines = [l.strip() for l in out.getvalue().splitlines()]
        token = lines[1]
        self.assertEqual(lines[3], f"phone:{hub.token_digest(token)}")
        self.assertEqual(hub.ServerAuth.from_env(
            {hub.DEVICE_TOKENS_ENV: lines[3]}).device_for_token(token), "phone")

    def test_a_name_that_would_break_the_token_list_is_a_usage_error(self):
        for name in ("", "   ", "a,b", "box ", " box", "two\nlines", "tab\there",
                     "dan's phone", "a:b", "$HOME", "double  space"):
            with self.subTest(name=name), redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as caught:
                    cli.build_parser().parse_args(["hub", "token", "--device", name])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("--device", err.getvalue())

    def test_every_accepted_name_round_trips_beside_another_entry(self):
        for name in ("phone", "my box", "box-1.lan", "a_b", "télé"):
            with self.subTest(name=name):
                args = cli.build_parser().parse_args(["hub", "token", "--device", name])
                with redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(args.func(args), 0)
                lines = [l.strip() for l in out.getvalue().splitlines()]
                other = f"box:{'0' * 64}"
                auth = hub.ServerAuth.from_env({hub.DEVICE_TOKENS_ENV: f"{other},{lines[3]}"})
                self.assertEqual(auth.device_for_token(lines[1]), name)


if __name__ == "__main__":
    unittest.main()
