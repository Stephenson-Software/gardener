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
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from gardener import cli, hub, state

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
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.cleanup()


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

    def test_reports_the_queue_without_contacting_the_hub(self):
        self.record("2026-09-26T01:00:00+00:00")
        self.record("2026-09-26T02:00:00+00:00")
        os.environ[hub.URL_ENV] = "http://127.0.0.1:9"
        os.environ[hub.TOKEN_ENV] = "x"
        code, out, _ = self._status()
        self.assertEqual(code, 0)
        self.assertIn("queued: 2 run(s) not yet pushed, oldest 2026-09-26T01:00:00+00:00", out)

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
    """Speaks the UserAuth contract `hub.UserAuthClient` documents."""

    users = {"dan": "pw", "stranger": "pw"}
    validate_calls = 0
    revoked: set = set()

    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/login":
            creds = json.loads(body)
            if self.users.get(creds.get("username")) == creds.get("password"):
                self._json(200, {"token": "jwt-" + creds["username"], "tokenType": "Bearer",
                                 "expiresAt": "2026-09-26T10:00:00Z", "refreshToken": "r"})
            else:
                self._json(401, {"error": "bad credentials"})
        elif self.path == "/logout":
            type(self).revoked.add(self.headers.get("Authorization", "")[7:])
            self._json(200, {})

    def do_GET(self):
        if self.path == "/session/validate":
            type(self).validate_calls += 1
            token = self.headers.get("Authorization", "")[7:]
            if token.startswith("jwt-") and token not in self.revoked:
                self._json(200, {"valid": True, "username": token[4:], "roles": []})
            else:
                self._json(401, {"error": "invalid"})


class TestUserAuthSignIn(_HubTestCase):
    def setUp(self):
        _UserAuthStub.validate_calls = 0
        _UserAuthStub.revoked = set()
        self.ua = ThreadingHTTPServer(("127.0.0.1", 0), _UserAuthStub)
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
        cookie = headers["Set-Cookie"]
        for attr in ("HttpOnly", "Secure", "SameSite=Strict"):
            self.assertIn(attr, cookie)
        session = {"Cookie": cookie.split(";", 1)[0]}
        status, _, body = self.hub.request("GET", "/api/status", session)
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
        session = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        for _ in range(3):
            self.assertEqual(self.hub.request("GET", "/api/status", session)[0], 200)
        self.assertEqual(_UserAuthStub.validate_calls, 1)

    def test_signing_out_revokes_the_session(self):
        _, headers, _ = self._login("dan", "pw")
        session = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        status, headers, _ = self.hub.request("GET", "/logout", session)
        self.assertEqual(status, 303)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.hub.request("GET", "/api/status", session)[0], 401)

    def test_basic_auth_still_works_alongside_sign_in(self):
        self.assertEqual(self.hub.request("GET", "/api/status", _basic(OPERATOR))[0], 200)


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


if __name__ == "__main__":
    unittest.main()
