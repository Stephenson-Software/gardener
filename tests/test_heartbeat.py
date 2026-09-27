"""Tests for RFC 0010's live-state heartbeats: `gardener/heartbeat.py` on
the device side and the hub's `POST /api/v1/heartbeat`, `device_live`
table, and `live_device_states`.

Every hub here is a real one on a loopback port (`test_hub._HubFixture`);
nothing reaches a real hub. `now` is injected wherever age matters."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gardener import dashboard, heartbeat, hub, sessions
from test_hub import DEVICE_TOKEN, PHONE_TOKEN, _HubTestCase

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _session(sid="a1b2c3d4"):
    return sessions.Session(id=sid, pid=1, command="overnight", target="garden",
                            started_at="2026-09-27T01:00:00+00:00", running=True,
                            path=Path("/nonexistent"))


def _beat(seq=1, sid="a1b2c3d4", interval=60, ending=False, **extra):
    body = {
        "seq": seq, "sent_at": "2026-09-27T01:00:05+00:00", "interval_seconds": interval,
        "ending": ending, "gardener_version": "0.0.0",
        "session": {"id": sid, "command": "overnight", "target": "garden",
                    "started_at": "2026-09-27T01:00:00+00:00"},
        "in_progress": ["owner/a"],
        "batch_progress": {"start": 1, "end": 2, "total": 10},
        "overnight_run": {"strategy": "random", "budget_hours": 8.0, "garden_size_at_start": 10,
                          "started_at": "2026-09-27T01:00:00", "elapsed_seconds": 5,
                          "remaining_seconds": 28795},
        "slots": [{"repo": "owner/a", "phase": "running", "started_at": None,
                   "idle_seconds": 12, "stalled": False, "rate_limit_seen": False}],
    }
    body.update(extra)
    return body


class TestHeartbeatRoute(_HubTestCase):
    def post(self, body, token=DEVICE_TOKEN):
        raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        status, _, answer = self.hub.request("POST", "/api/v1/heartbeat", headers, raw)
        return status, json.loads(answer or b"{}")

    def states(self, now=NOW):
        return {d["device"]: d for d in hub.live_device_states(self.hub.db_path, now=now)}

    def test_a_beat_is_stored_under_the_tokens_device(self):
        self.assertEqual(self.post(_beat()), (200, {"stored": True}))
        self.assertEqual(self.post(_beat(), token=PHONE_TOKEN)[0], 200)
        got = hub.live_device_states(self.hub.db_path)
        self.assertEqual([d["device"] for d in got], ["box", "phone"])
        self.assertEqual(got[0]["snapshot"]["batch_progress"], {"start": 1, "end": 2, "total": 10})

    def test_fields_the_hub_does_not_name_are_never_stored(self):
        """The owner decided no log or transcript text leaves a device; the
        hub enforces it too, whatever a device sends."""
        body = _beat(log_tail=["SECRET log line"])
        body["slots"][0]["activity"] = {"last_text": "SECRET model text"}
        body["slots"][0]["transcript"] = "/home/SECRET/path.jsonl"
        body["overnight_run"]["log"] = "/home/SECRET/overnight.log"
        self.assertEqual(self.post(body)[0], 200)
        stored = json.dumps(hub.live_device_states(self.hub.db_path))
        self.assertNotIn("SECRET", stored)

    def test_invalid_beats_are_refused_naming_the_field(self):
        bad_phase = _beat()
        bad_phase["slots"][0]["phase"] = "dreaming"
        bad_repo = _beat(in_progress=["not a repo"])
        no_interval = _beat()
        del no_interval["interval_seconds"]
        for body, field in ((bad_phase, "phase"), (bad_repo, "owner/name"),
                            (no_interval, "interval_seconds"), ([], "object")):
            status, answer = self.post(body)
            self.assertEqual(status, 400, field)
            self.assertIn(field, answer["error"])
        self.assertEqual(hub.live_device_states(self.hub.db_path), [])

    def test_auth_and_size(self):
        self.assertEqual(self.post(_beat(), token=None)[0], 401)
        self.assertEqual(self.post(_beat(), token="nope")[0], 401)
        self.assertEqual(self.post(b"x" * (hub.HEARTBEAT_MAX_BYTES + 1))[0], 413)

    def test_an_older_beat_of_the_same_session_is_ignored(self):
        self.post(_beat(seq=5))
        self.assertEqual(self.post(_beat(seq=4)), (200, {"stored": False}))
        self.assertEqual(self.states()["box"]["snapshot"]["seq"], 5)
        # A new session starts its own count.
        self.assertEqual(self.post(_beat(seq=1, sid="ffff0000")), (200, {"stored": True}))

    def _state_at(self, age, interval=60, ending=False):
        hub.store_heartbeat(self.hub.db_path, "box", hub.heartbeat_from_wire(
            _beat(interval=interval, ending=ending)),
            received_at=(NOW - timedelta(seconds=age)).isoformat(timespec="seconds"))
        return self.states()["box"]["state"]

    def test_live_stale_idle_are_measured_in_the_senders_interval(self):
        window = dashboard.ACTIVE_LOG_WINDOW_SECONDS
        self.assertEqual(self._state_at(180), "live")
        self.assertEqual(self._state_at(181), "stale")
        self.assertEqual(self._state_at(window), "stale")
        self.assertEqual(self._state_at(window + 1), "idle")
        self.assertEqual(self._state_at(900, interval=300), "live")  # the phone at 300 s
        self.assertEqual(self._state_at(0, ending=True), "idle")


class TestDeviceSender(_HubTestCase):
    def sender(self, interval=60, send=None):
        config = hub.HubConfig(url=self.hub.url, token=DEVICE_TOKEN, heartbeat_seconds=interval)
        snap = lambda session, seq, interval, ending=False, state_dir=None: _beat(
            seq=seq, sid=session.id, interval=interval, ending=ending)
        return heartbeat.Heartbeat(config, _session(), send=send, snapshot=snap)

    def test_beats_then_an_ending_beat_reach_the_hub(self):
        hb = self.sender()
        self.assertTrue(hb.beat())
        states = hub.live_device_states(self.hub.db_path)
        self.assertEqual(states[0]["state"], "live")
        hb.stop()
        states = hub.live_device_states(self.hub.db_path)
        self.assertEqual((states[0]["state"], states[0]["snapshot"]["seq"]), ("idle", 2))

    def test_a_hub_without_the_route_turns_it_off_with_one_note(self):
        calls = []

        def old_hub(body, timeout):
            calls.append(body)
            raise hub.HubError("POST /api/v1/heartbeat → HTTP 404", status=404)

        hb = self.sender(send=old_hub)
        with redirect_stderr(io.StringIO()) as err:
            self.assertFalse(hb.beat())
            self.assertFalse(hb.beat())
            hb.stop()
        self.assertEqual(len(calls), 1)
        self.assertTrue(hb.disabled)
        self.assertEqual(err.getvalue().count("NOTE"), 1)
        self.assertIn("upgrade the hub", err.getvalue())

    def test_failures_note_once_back_off_and_skip_the_ending_beat(self):
        calls = []

        def down(body, timeout):
            calls.append(body)
            raise hub.HubError("unreachable")

        hb = self.sender(send=down)
        with redirect_stderr(io.StringIO()) as err:
            for _ in range(3):
                self.assertFalse(hb.beat())
            hb.stop()
        self.assertEqual(len(calls), 3)  # no ending beat to a hub that is down
        self.assertEqual(err.getvalue().count("NOTE"), 1)

    def test_the_loop_backs_off_doubling_up_to_the_cap(self):
        hb = self.sender(interval=60, send=lambda b, t: (_ for _ in ()).throw(hub.HubError("x")))
        waits = []

        class FakeStop:
            def wait(self, delay):
                waits.append(delay)
                return len(waits) >= 6

        hb._stop = FakeStop()
        with redirect_stderr(io.StringIO()):
            hb._run()
        self.assertEqual(waits, [120, 240, 480, 600, 600, 600])

    def test_a_snapshot_error_never_raises(self):
        hb = self.sender()
        hb._snapshot = lambda *a, **k: 1 / 0
        with redirect_stderr(io.StringIO()) as err:
            self.assertFalse(hb.beat())
        self.assertIn("ZeroDivisionError", err.getvalue())


class TestRunning(_HubTestCase):
    """`heartbeat.running`, which `cli._dispatch` wraps every dispatching
    command in, end to end: the real snapshot over a temp state dir."""

    def configure(self, **extra):
        values = {hub.URL_ENV: self.hub.url, hub.TOKEN_ENV: DEVICE_TOKEN, **extra}
        (self.state_dir / hub.CONFIG_FILENAME).write_text(
            "".join(f"{k}={v}\n" for k, v in values.items()))

    def test_no_hub_no_heartbeat(self):
        with heartbeat.running(_session()) as hb:
            self.assertIsNone(hb)

    def test_zero_turns_it_off(self):
        self.configure(**{hub.HEARTBEAT_ENV: "0"})
        with heartbeat.running(_session()) as hb:
            self.assertIsNone(hb)

    def test_a_dispatch_leaves_an_idle_row_behind(self):
        self.configure()
        with heartbeat.running(_session()) as hb:
            self.assertIsNotNone(hb)
        self.assertFalse(hb._thread.is_alive())
        got = hub.live_device_states(self.hub.db_path)
        self.assertEqual(got[0]["device"], "box")
        self.assertEqual(got[0]["state"], "idle")
        self.assertTrue(got[0]["snapshot"]["ending"])

    def test_config_parses_the_interval(self):
        self.configure(**{hub.HEARTBEAT_ENV: "300"})
        self.assertEqual(hub.load_config(self.state_dir).heartbeat_seconds, 300)
        self.configure(**{hub.HEARTBEAT_ENV: "soon"})
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(hub.load_config(self.state_dir).heartbeat_seconds, 60)
        self.assertIn("not a whole number", err.getvalue())


class TestBuildSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        logs = self.base / "logs"
        logs.mkdir()
        started = datetime.now()
        (logs / f"overnight-{started:%Y%m%d-%H%M%S}.log").write_text("\n".join([
            "gardener: overnight starting — 10 repo(s) in garden, strategy=random, budget=8.0h, "
            "0 repo(s) already attempted this cycle",
            "gardener: overnight dispatching tend for o/a, o/b (1-2/10 candidates this run, concurrency=2)...",
            "gardener: tending o/a (allow_merge=True)",
            "gardener: tending o/b (allow_merge=True)",
            "gardener: o/a checked out at /home/SECRET/cache/o__a",
            "gardener: NOTE — SECRET error text from a run",
        ]) + "\n")

    def test_the_snapshot_carries_structure_and_no_log_text(self):
        with patch.dict(os.environ, {"GARDENER_STATE_DIR": str(self.base)}):
            snap = heartbeat.build_snapshot(_session(), seq=3, interval_seconds=60,
                                            state_dir=self.base)
        self.assertEqual(snap["in_progress"], ["o/a", "o/b"])
        self.assertEqual(snap["batch_progress"], {"start": 1, "end": 2, "total": 10})
        self.assertEqual(snap["overnight_run"]["strategy"], "random")
        # Sent as UTC, not the device's naive local time.
        self.assertTrue(snap["overnight_run"]["started_at"].endswith("+00:00"))
        self.assertEqual(heartbeat._utc("2026-09-27T01:00:00+02:00"), "2026-09-26T23:00:00+00:00")
        self.assertIsNone(heartbeat._utc("soon"))
        self.assertEqual([(s["repo"], s["phase"]) for s in snap["slots"]],
                         [("o/a", "preparing"), ("o/b", "cloning")])
        self.assertNotIn("SECRET", json.dumps(snap))
        # No local path either: not the log's, not the state dir's.
        self.assertNotIn(str(self.base), json.dumps(snap))
        # And the hub accepts exactly what a device builds.
        self.assertEqual(hub.heartbeat_from_wire(snap)["slots"], snap["slots"])


if __name__ == "__main__":
    unittest.main()


class TestHubStatusPayload(_HubTestCase):
    """`/api/status` on a hub renders each device's heartbeat (RFC 0010 §5)."""

    def put(self, device, age, in_progress, ending=False, interval=60):
        body = _beat(interval=interval, ending=ending, in_progress=in_progress)
        hub.store_heartbeat(self.hub.db_path, device, hub.heartbeat_from_wire(body),
                            received_at=(datetime.now(timezone.utc) - timedelta(seconds=age))
                            .isoformat(timespec="seconds"))

    def status(self):
        from test_hub import OPERATOR, _basic
        code, _, body = self.hub.request("GET", "/api/status", _basic(OPERATOR))
        self.assertEqual(code, 200)
        return json.loads(body)

    def test_in_flight_is_the_union_of_live_devices_only(self):
        self.put("box", 30, ["owner/a", "owner/b"])
        self.put("phone", 30, ["owner/b", "owner/c"])
        self.put("tablet", 600, ["owner/stale"])            # stale: may have stopped
        self.put("laptop", 30, ["owner/done"], ending=True)  # idle
        payload = self.status()
        self.assertEqual(payload["schema"], 4)
        self.assertEqual(payload["in_progress"], ["owner/a", "owner/b", "owner/c"])
        by = {d["device"]: d for d in payload["live_devices"]}
        self.assertEqual({k: v["state"] for k, v in by.items()},
                         {"box": "live", "phone": "live", "tablet": "stale", "laptop": "idle"})
        self.assertEqual(by["box"]["batch_progress"], {"start": 1, "end": 2, "total": 10})
        self.assertEqual(by["box"]["overnight_run"]["strategy"], "random")
        self.assertEqual(by["tablet"]["in_progress"], ["owner/stale"])

    def test_a_local_dashboard_is_unchanged(self):
        from gardener import dashboard as dash
        payload = dash.build_status(state_dir=self.state_dir)
        self.assertEqual(payload["live_devices"], [])
        self.assertEqual(payload["in_progress"], [])
