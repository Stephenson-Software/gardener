"""Provider routing wired into gardener (docs/rfcs/provider-routing.md, phase 2).

`dispatch.run_agent`'s routed path, provenance on the run record (state, hub
wire format, `status`, notifications), `overnight`'s per-provider batching
and slots, and `gardener route`. Like `test_dispatch.py`, every dispatch
stops at a mocked `_run_in_process_group` (or a mocked `run_agent`/
`run_claude`): nothing here invokes `claude`, `gh` or the network, and
health checks are always patched.
"""
import argparse
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gardener import cli, dispatch, garden, hub, overnight, routing, state
from gardener.routing import Health
from gardener.dispatch import DispatchError, DispatchResult, Harness, HarnessConfig, Mode, run_agent

_fence = None
_fence_dir = None


def setUpModule():
    """The same fence as test_cli.py's: no real Discord webhook, hub, usage
    report, harness setting or routing file reaches these tests, since
    several of them record runs and notify through the real code path."""
    global _fence, _fence_dir
    _fence_dir = tempfile.TemporaryDirectory()
    # notify.env and hub.env live in the state dir: an empty one closes both.
    _fence = patch.dict(os.environ, {"GARDENER_USAGE_REPORTING_ENABLED": "false",
                                     "GARDENER_STATE_DIR": _fence_dir.name})
    _fence.start()
    for name in ("GARDENER_DISCORD_WEBHOOK_URL", hub.URL_ENV, "GARDENER_ROUTING", dispatch.HARNESS_ENV,
                 dispatch.COMMAND_ENV, dispatch.ALLOW_UNSCOPED_ENV, dispatch.CLAUDE_BIN_ENV,
                 dispatch.MODEL_ENV):
        os.environ.pop(name, None)


def tearDownModule():
    _fence.stop()
    _fence_dir.cleanup()

ALL = [m.value for m in Mode]
LOCAL_BIN = "/opt/local-gpu/claude-local"
ROUTING = {
    "providers": {
        "claude-sub": {"harness": "claude-code", "cost": "subscription", "concurrency": 2, "modes": ALL},
        "local-gpu": {"harness": "claude-code", "claude_bin": LOCAL_BIN, "cost": "free", "local": True,
                     "concurrency": 1, "context_tokens": 65536, "modes": ["report"], "max_tier": "M",
                     "health_url": "http://gpu.test/health"},
    },
    "rules": [
        {"modes": ["report"], "tiers": ["S", "M"], "providers": ["local-gpu", "claude-sub"]},
        {"modes": ["*"], "tiers": ["*"], "providers": ["claude-sub"]},
    ],
    "repos": {"o/private": {"local_only": True}},
}


def completed(obj=None, returncode=0, stdout=None):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode,
        stdout=json.dumps(obj) if stdout is None else stdout, stderr="",
    )


def ok_json(cost=0.2):
    return completed({"result": "GARDENER_SUMMARY: 1 gap", "is_error": False, "total_cost_usd": cost,
                      "session_id": "s1"})


def result(**kw):
    base = dict(ok=True, result_text="GARDENER_SUMMARY: done", raw_stdout="{}", stderr="", exit_code=0,
                duration_ms=1, cost_usd=0.5, session_id="s", permission_denials=[], is_error=False)
    base.update(kw)
    return DispatchResult(**base)


class Tmp(unittest.TestCase):
    """A temp dir holding a small clone and (optionally) a routing file. The
    routing file is always passed explicitly or via GARDENER_STATE_DIR=tmp,
    never read from the operator's real state dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clone = self.tmp / "clone"
        self.clone.mkdir()
        (self.clone / "main.py").write_text("print('hi')\n" * 50)
        self.routing_file = self.tmp / "routing.json"
        env = patch.dict(os.environ, {"GARDENER_STATE_DIR": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GARDENER_ROUTING", None)
        os.environ.pop(dispatch.ALLOW_UNSCOPED_ENV, None)
        health = patch("gardener.routing.adapters.http_health_probe.HttpHealthProbe.check",
                       return_value=Health.up())
        self.health = health.start()
        self.addCleanup(health.stop)
        which = patch("gardener.dispatch.shutil.which", side_effect=lambda name: f"/usr/bin/{name}")
        self.which = which.start()
        self.addCleanup(which.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def write_routing(self, raw=None):
        self.routing_file.write_text(json.dumps(ROUTING if raw is None else raw))


class NoRoutingFileTests(Tmp):
    """No routing file: run_agent is byte-for-byte the single-harness path."""

    @patch("gardener.dispatch._run_in_process_group")
    def test_repo_without_routing_file_dispatches_exactly_as_before(self, mock_run):
        mock_run.return_value = ok_json()
        config = HarnessConfig(claude_bin="/opt/claude", model="opus")
        with redirect_stderr(io.StringIO()):
            before = run_agent(Mode.REPORT, "p", self.clone, add_dirs=[self.tmp], timeout=9,
                               harness_config=config)
            after = run_agent(Mode.REPORT, "p", self.clone, add_dirs=[self.tmp], timeout=9,
                              harness_config=config, repo="o/r", routing_path=self.routing_file)
        self.assertEqual(mock_run.call_args_list[0], mock_run.call_args_list[1])
        self.assertEqual(before, after)
        self.assertIsNone(after.provider)
        self.assertEqual(after.escalated_from, ())
        self.assertEqual(after.cost_usd, 0.2)

    @patch("gardener.dispatch.run_claude")
    def test_repo_without_routing_file_calls_run_claude_unchanged(self, mock_run_claude):
        spec = dispatch.tend_mode_spec(False)
        run_agent(Mode.TEND, "p", Path("/tmp"), model="m", timeout=7, mode_spec=spec,
                  harness_config=HarnessConfig(), repo="o/r")
        mock_run_claude.assert_called_once_with(
            Mode.TEND, "p", Path("/tmp"), add_dirs=None, model="m", timeout=7, mode_spec=spec,
            claude_bin=dispatch.CLAUDE_BIN,
        )

    @patch("gardener.dispatch.run_claude")
    def test_no_repo_ignores_a_routing_file(self, mock_run_claude):
        self.write_routing()
        run_agent(Mode.REPORT, "p", self.clone, harness_config=HarnessConfig(),
                  routing_path=self.routing_file)
        self.assertEqual(mock_run_claude.call_args.kwargs["claude_bin"], dispatch.CLAUDE_BIN)
        self.health.assert_not_called()

    def test_invalid_routing_file_is_a_dispatch_error(self):
        self.routing_file.write_text("{not json")
        with self.assertRaises(DispatchError):
            run_agent(Mode.REPORT, "p", self.clone, harness_config=HarnessConfig(), repo="o/r",
                      routing_path=self.routing_file)


class RoutedDispatchTests(Tmp):
    def setUp(self):
        super().setUp()
        self.write_routing()

    def run_routed(self, mode=Mode.REPORT, repo="o/r", **kw):
        with redirect_stderr(io.StringIO()) as err:
            res = run_agent(mode, "p", self.clone, harness_config=HarnessConfig(), repo=repo,
                            routing_path=self.routing_file, **kw)
        return res, err.getvalue()

    @patch("gardener.dispatch._run_in_process_group")
    def test_small_report_goes_to_the_free_provider_with_no_cost(self, mock_run):
        mock_run.return_value = ok_json(cost=0.21)
        res, err = self.run_routed()
        self.assertEqual(mock_run.call_args.args[0][0], LOCAL_BIN)
        self.assertTrue(res.ok)
        self.assertEqual(res.provider, "local-gpu")
        self.assertEqual(res.escalated_from, ())
        self.assertIsNone(res.cost_usd)      # Claude Code's made-up price for a non-Claude model
        self.assertIn("providers: local-gpu, claude-sub", err)

    @patch("gardener.dispatch._run_in_process_group")
    def test_tempfail_escalates_to_the_next_provider(self, mock_run):
        mock_run.side_effect = [completed(stdout="", returncode=75), ok_json(cost=0.3)]
        res, err = self.run_routed()
        self.assertEqual([c.args[0][0] for c in mock_run.call_args_list], [LOCAL_BIN, dispatch.CLAUDE_BIN])
        self.assertEqual(res.provider, "claude-sub")
        self.assertEqual(res.escalated_from, ("local-gpu:tempfail",))
        self.assertEqual(res.cost_usd, 0.3)  # a subscription provider keeps its reported cost
        self.assertIn("local-gpu ended with tempfail; escalating to claude-sub", err)

    @patch("gardener.dispatch._run_in_process_group")
    def test_write_modes_keep_their_mode_spec(self, mock_run):
        mock_run.return_value = ok_json()
        res, _ = self.run_routed(mode=Mode.TEND, mode_spec=dispatch.tend_mode_spec(False))
        argv = mock_run.call_args.args[0]
        self.assertEqual(argv[0], dispatch.CLAUDE_BIN)
        self.assertEqual(argv[argv.index("--permission-mode") + 1],
                         dispatch.tend_mode_spec(False).permission_mode)
        self.assertNotIn("bypassPermissions", argv)
        self.assertEqual(res.provider, "claude-sub")

    @patch("gardener.dispatch._run_in_process_group")
    def test_paused_provider_is_skipped(self, mock_run):
        self.health.return_value = Health.down("paused by its operator")
        mock_run.return_value = ok_json()
        res, err = self.run_routed()
        self.assertEqual(mock_run.call_args.args[0][0], dispatch.CLAUDE_BIN)
        self.assertEqual(res.provider, "claude-sub")
        self.assertIn("skipped local-gpu: paused", err)

    @patch("gardener.dispatch._run_in_process_group")
    def test_provider_whose_binary_is_missing_is_skipped(self, mock_run):
        self.which.side_effect = lambda name: None if name == LOCAL_BIN else f"/usr/bin/{name}"
        mock_run.return_value = ok_json()
        res, err = self.run_routed()
        self.assertEqual(res.provider, "claude-sub")
        self.assertIn("skipped local-gpu", err)
        self.health.assert_not_called()      # readiness runs before the slow health probe

    @patch("gardener.dispatch._run_in_process_group")
    def test_no_candidates_is_a_refused_failed_result_not_a_fallback(self, mock_run):
        res, _ = self.run_routed(mode=Mode.TEND, repo="o/private", mode_spec=dispatch.tend_mode_spec(False))
        mock_run.assert_not_called()
        self.assertFalse(res.ok)
        self.assertTrue(res.is_error)
        self.assertFalse(res.blocked)
        self.assertIsNone(res.provider)
        self.assertIn("routing refused this run", res.failure_reason)
        self.assertIn("local_only", res.failure_reason)


class CommandProviderScopeTests(Tmp):
    """A routed `command` provider can only run what the operator's own
    GARDENER_HARNESS_ALLOW_UNSCOPED allows, whatever the global harness."""

    RAW = {
        "providers": {"orket": {"harness": "command", "command": ["orket-ext"], "modes": ALL}},
        "rules": [{"providers": ["orket"]}],
    }

    def test_tend_is_refused_without_the_operators_opt_in(self):
        self.write_routing(self.RAW)
        with patch("gardener.dispatch._read_settings_file", return_value={}):
            with redirect_stderr(io.StringIO()):
                res = run_agent(Mode.TEND, "p", self.clone, mode_spec=dispatch.tend_mode_spec(False),
                                harness_config=HarnessConfig(), repo="o/r", routing_path=self.routing_file)
        self.assertFalse(res.ok)
        self.assertIn(dispatch.ALLOW_UNSCOPED_ENV, res.failure_reason)

    @patch("gardener.dispatch.run_command_harness")
    def test_tend_runs_with_the_operators_opt_in(self, mock_command):
        mock_command.return_value = result()
        self.write_routing(self.RAW)
        with patch.dict(os.environ, {dispatch.ALLOW_UNSCOPED_ENV: "tend"}), redirect_stderr(io.StringIO()):
            res = run_agent(Mode.TEND, "p", self.clone, mode_spec=dispatch.tend_mode_spec(False),
                            harness_config=HarnessConfig(), repo="o/r", routing_path=self.routing_file)
        config = mock_command.call_args.args[3]
        self.assertEqual(config.harness, Harness.COMMAND)
        self.assertEqual(config.unscoped_modes, frozenset({Mode.TEND}))
        self.assertEqual(res.provider, "orket")


class CheckRoutingReadyTests(unittest.TestCase):
    def test_ready_when_one_trusted_provider_can_run(self):
        config = routing.parse_config(ROUTING, routing.DEFAULT_KINDS)
        with patch("gardener.dispatch.shutil.which", return_value="/usr/bin/x"), \
             patch("gardener.dispatch._read_settings_file", return_value={}), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop(dispatch.ALLOW_UNSCOPED_ENV, None)
            dispatch.check_routing_ready(Mode.TEND, config, HarnessConfig())

    def test_not_ready_names_each_provider(self):
        config = routing.parse_config(ROUTING, routing.DEFAULT_KINDS)
        with patch("gardener.dispatch.shutil.which", return_value=None), \
             patch("gardener.dispatch._read_settings_file", return_value={}):
            with self.assertRaises(DispatchError) as cm:
                dispatch.check_routing_ready(Mode.TEND, config, HarnessConfig())
        self.assertIn("claude-sub", str(cm.exception))
        self.assertNotIn("local-gpu", str(cm.exception))   # not trusted with tend, so not a candidate


class CredentialTests(unittest.TestCase):
    def test_routing_file_refuses_credential_looking_keys(self):
        for raw in (
            {"providers": {"a": {"api_key": "sk-x"}}},
            {"providers": {"a": {"env": {"PROVIDER_KEY": "x"}}}},
            {"providers": {"a": {}}, "token": "x"},
        ):
            with self.subTest(raw=raw), self.assertRaises(routing.RoutingError):
                routing.parse_config(raw, routing.DEFAULT_KINDS)

    def test_token_counts_and_repo_slugs_are_not_credentials(self):
        raw = dict(ROUTING, tier_tokens={"S": 1, "M": 2}, repos={"o/my_token": {"tier": "S"}})
        routing.parse_config(raw, routing.DEFAULT_KINDS)


class ProviderSlotTests(unittest.TestCase):
    def test_concurrency_one_never_runs_two_at_once(self):
        provider = routing.Provider(name="local-gpu", concurrency=1)
        decision = routing.Decision("o/r", Mode.REPORT, "S", 0, [provider], [])
        slots = routing.SemaphoreLimiter()
        release = threading.Event()
        waiting = threading.Event()
        lock = threading.Lock()
        running = {"now": 0, "max": 0, "calls": 0}

        def run(harness_config, **kw):
            with lock:
                running["now"] += 1
                running["calls"] += 1
                running["max"] = max(running["max"], running["now"])
                first = running["calls"] == 1
            if first:
                release.wait(timeout=5)
            with lock:
                running["now"] -= 1
            return result()

        observer = routing.NullObserver()
        observer.waiting = lambda p: waiting.set()
        threads = [threading.Thread(target=routing.run_with_escalation,
                                    args=(decision, run, HarnessConfig(), routing.DEFAULT_KINDS),
                                    kwargs={"limiter": slots, "observer": observer})
                   for _ in range(2)]
        threads[0].start()
        while running["calls"] < 1:
            threading.Event().wait(0.001)
        threads[1].start()
        self.assertTrue(waiting.wait(timeout=5))     # the second is held at the slot...
        self.assertEqual(running["calls"], 1)         # ...not running
        release.set()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(running["calls"], 2)
        self.assertEqual(running["max"], 1)

    def test_concurrency_two_runs_two_at_once(self):
        provider = routing.Provider(name="claude-sub", concurrency=2)
        decision = routing.Decision("o/r", Mode.REPORT, "S", 0, [provider], [])
        slots = routing.SemaphoreLimiter()
        barrier = threading.Barrier(2, timeout=5)
        outcomes = []

        def run(harness_config, **kw):
            barrier.wait()                                # both must be inside at once
            return result()

        threads = [threading.Thread(target=lambda: outcomes.append(
            routing.run_with_escalation(decision, run, HarnessConfig(), routing.DEFAULT_KINDS, limiter=slots)))
            for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(len(outcomes), 2)

    def test_slots_are_released_when_a_run_raises(self):
        provider = routing.Provider(name="local-gpu", concurrency=1)
        slots = routing.SemaphoreLimiter()
        with self.assertRaises(RuntimeError):
            with slots.hold(provider):
                raise RuntimeError("boom")
        with slots.hold(provider):              # would deadlock if the slot leaked
            pass


class BatchByProviderTests(unittest.TestCase):
    def test_no_caps_is_exactly_batch_repos(self):
        order = [f"o/{i}" for i in range(7)]
        for concurrency in (1, 2, 3):
            self.assertEqual(overnight.batch_repos_by_provider(order, concurrency, {}, {}),
                             overnight.batch_repos(order, concurrency))

    def test_a_concurrency_one_provider_never_shares_a_batch_with_itself(self):
        order = ["o/a", "o/b", "o/c", "o/d"]
        provider_of = {"o/a": "local-gpu", "o/b": "local-gpu", "o/c": "claude-sub", "o/d": None}
        batches = overnight.batch_repos_by_provider(order, 2, provider_of, {"local-gpu": 1, "claude-sub": 2})
        self.assertEqual(batches, [["o/a"], ["o/b", "o/c"], ["o/d"]])
        self.assertEqual([r for b in batches for r in b], order)   # contiguous, never reordered

    def test_batch_size_still_bounded_by_concurrency(self):
        order = ["o/a", "o/b", "o/c"]
        batches = overnight.batch_repos_by_provider(order, 2, {r: "claude-sub" for r in order}, {"claude-sub": 5})
        self.assertEqual(batches, [["o/a", "o/b"], ["o/c"]])

    def test_first_choice_needs_a_clone(self):
        config = routing.parse_config(ROUTING, routing.DEFAULT_KINDS)
        with tempfile.TemporaryDirectory() as d:
            service = routing.RoutingService(config)
            self.assertIsNone(service.first_choice(Mode.TEND, "o/r", Path(d) / "missing"))
            (Path(d) / "a.py").write_text("x = 1\n")
            self.assertEqual(service.first_choice(Mode.REPORT, "o/r", Path(d)), "local-gpu")
            self.assertEqual(service.first_choice(Mode.TEND, "o/r", Path(d)), "claude-sub")


class OvernightRoutingTests(Tmp):
    def setUp(self):
        super().setUp()
        self.garden_file = self.tmp / "garden.json"
        self.cursor_file = self.tmp / "cursor.json"
        self.state_db = self.tmp / "state.sqlite3"
        for repo in ("o/a", "o/b", "o/c"):
            garden.add(repo, path=self.garden_file)

    def args(self, concurrency=2):
        return argparse.Namespace(
            hours=8.0, model=None, garden_file=self.garden_file, cursor_file=self.cursor_file,
            state_db=self.state_db, concurrency=concurrency, strategy="round-robin", random_seed=None,
            self_update=False,
        )

    def fake_tend(self, batches_seen):
        lock = threading.Lock()

        def fake(args):
            with lock:
                batches_seen.append(args.repo)
            run = state.Run(repo=args.repo, mode="tend", outcome="tend", timestamp=state.now_iso(),
                            gap_summary="GARDENER_SUMMARY: ok")
            return cli.TendResult(exit_code=0, ok=True, result_text="ok", run=run)
        return fake

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_batches_respect_planned_provider_caps(self, mock_tend, _notifier):
        routing = dict(ROUTING)
        routing["providers"] = dict(ROUTING["providers"])
        routing["providers"]["local-gpu"] = dict(ROUTING["providers"]["local-gpu"], modes=ALL)
        routing["rules"] = [{"providers": ["local-gpu", "claude-sub"]}]
        (self.tmp / "routing.json").write_text(json.dumps(routing))
        seen = []
        mock_tend.side_effect = self.fake_tend(seen)
        plan = {"o/a": "local-gpu", "o/b": "local-gpu", "o/c": "claude-sub"}
        with patch("gardener.cli._plan_providers", return_value=plan), \
             patch("gardener.cli.overnight.batch_repos_by_provider",
                   wraps=overnight.batch_repos_by_provider) as batcher, \
             redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.cmd_overnight(self.args()), 0)
        self.assertEqual(batcher.call_args.args[3], {"claude-sub": 2, "local-gpu": 1})
        self.assertIn("dispatching tend for o/a (", err.getvalue())
        self.assertIn("dispatching tend for o/b, o/c (", err.getvalue())
        self.assertEqual(sorted(seen), ["o/a", "o/b", "o/c"])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_without_routing_batches_are_unchanged(self, mock_tend, _notifier):
        seen = []
        mock_tend.side_effect = self.fake_tend(seen)
        with patch("gardener.cli.overnight.batch_repos_by_provider") as batcher, \
             patch("gardener.cli.check_harness_ready"), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.cmd_overnight(self.args()), 0)
        batcher.assert_not_called()
        self.assertIn("dispatching tend for o/a, o/b (", err.getvalue())

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_invalid_routing_file_stops_the_batch_up_front(self, mock_tend, notifier):
        (self.tmp / "routing.json").write_text("[]")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_overnight(self.args()), 1)
        mock_tend.assert_not_called()
        self.assertIn("harness not ready", notifier.return_value.notify.call_args.args[0])


class StateProvenanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "s.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip(self):
        state.record_run(state.Run(repo="o/r", mode="report", outcome="report", timestamp=state.now_iso(),
                                   provider="claude-sub", escalated_from="local-gpu:tempfail"), db_path=self.db)
        run = state.list_runs(db_path=self.db)[0]
        self.assertEqual((run.provider, run.escalated_from), ("claude-sub", "local-gpu:tempfail"))

    def test_older_db_gains_the_columns_and_keeps_its_rows(self):
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute("""CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL,
                timestamp TEXT NOT NULL, mode TEXT NOT NULL, gap_summary TEXT, outcome TEXT NOT NULL,
                exit_code INTEGER, duration_ms INTEGER, cost_usd REAL, claude_session_id TEXT,
                run_uuid TEXT, device TEXT, pushed_at TEXT)""")
            conn.execute("INSERT INTO runs (repo, timestamp, mode, outcome, run_uuid, device) VALUES "
                         "('o/old', '2026-11-01T00:00:00Z', 'tend', 'tend', "
                         "'00000000-0000-4000-8000-000000000001', 'box')")
        # Readers never migrate, and must still read the old row.
        self.assertIsNone(state.list_runs(db_path=self.db)[0].provider)
        state.record_run(state.Run(repo="o/new", mode="report", outcome="report", timestamp=state.now_iso(),
                                   provider="local-gpu"), db_path=self.db)
        runs = {r.repo: r for r in state.list_runs(db_path=self.db)}
        self.assertIsNone(runs["o/old"].provider)
        self.assertEqual(runs["o/old"].device, "box")
        self.assertEqual(runs["o/new"].provider, "local-gpu")


class HubWireTests(unittest.TestCase):
    def run_(self, **kw):
        return state.Run(repo="o/r", mode="report", outcome="report", timestamp="2026-11-20T00:00:00Z",
                         run_uuid="00000000-0000-4000-8000-000000000002", device="box", **kw)

    def test_provenance_round_trips(self):
        wire = hub.run_to_wire(self.run_(provider="local-gpu", escalated_from=None))
        self.assertEqual(wire["provider"], "local-gpu")
        back = hub.run_from_wire(wire, "box")
        self.assertEqual((back.provider, back.escalated_from), ("local-gpu", None))

    def test_a_push_from_an_older_device_is_still_accepted(self):
        wire = hub.run_to_wire(self.run_())
        del wire["provider"], wire["escalated_from"]
        self.assertIsNone(hub.run_from_wire(wire, "box").provider)


class CliProvenanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_db = self.tmp / "state.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_agent")
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.clone_or_refresh_target_repo")
    @patch("gardener.cli.conventions.ensure_conventions")
    def test_align_routes_by_repo_and_records_provenance(self, mock_conv, mock_clone, _branch, mock_run_agent,
                                                          notifier):
        mock_conv.return_value = SimpleNamespace(path=self.tmp)
        mock_clone.return_value = self.tmp
        mock_run_agent.return_value = result(provider="claude-sub", escalated_from=("local-gpu:tempfail",),
                                             cost_usd=0.4)
        args = argparse.Namespace(
            repo="o/r", implement=False, file_issue=False, model=None, timeout=1800,
            no_refresh_conventions=False, no_refresh_target=False,
            conventions_repo="https://example.invalid/conventions.git", state_db=self.state_db,
        )
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_align(args), 0)
        self.assertEqual(mock_run_agent.call_args.kwargs["repo"], "o/r")
        run = state.list_runs(db_path=self.state_db)[0]
        self.assertEqual((run.provider, run.escalated_from), ("claude-sub", "local-gpu:tempfail"))
        message = notifier.return_value.notify.call_args.args[1]
        self.assertIn("provider: claude-sub (escalated from local-gpu:tempfail)", message)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_agent")
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.clone_or_refresh_target_repo")
    @patch("gardener.cli.conventions.ensure_conventions")
    def test_a_refused_run_is_recorded_as_a_failure(self, mock_conv, mock_clone, _branch, mock_run_agent,
                                                     notifier):
        mock_conv.return_value = SimpleNamespace(path=self.tmp)
        mock_clone.return_value = self.tmp
        mock_run_agent.return_value = dispatch._refused_result("routing refused this run: no eligible provider")
        args = argparse.Namespace(
            repo="o/r", implement=False, file_issue=False, model=None, timeout=1800,
            no_refresh_conventions=False, no_refresh_target=False,
            conventions_repo="https://example.invalid/conventions.git", state_db=self.state_db,
        )
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_align(args), 1)
        run = state.list_runs(db_path=self.state_db)[0]
        self.assertEqual(run.outcome, "error")
        self.assertIn("routing refused", run.gap_summary)
        self.assertIsNone(run.provider)

    def test_unrouted_notification_is_unchanged(self):
        with patch("gardener.cli.notify.default_notifier") as notifier:
            cli._notify_run(state.Run(repo="o/r", mode="report", outcome="report", timestamp="t",
                                      gap_summary="2 gaps"))
        self.assertEqual(notifier.return_value.notify.call_args.args[1], "2 gaps")

    def status(self):
        with redirect_stdout(io.StringIO()) as out:
            cli.cmd_status(argparse.Namespace(repo=None, limit=20, state_db=self.state_db))
        return out.getvalue()

    def test_status_shows_a_provider_column_only_for_routed_runs(self):
        state.record_run(state.Run(repo="o/r", mode="report", outcome="report", timestamp=state.now_iso()),
                         db_path=self.state_db)
        self.assertNotIn("provider", self.status())
        state.record_run(state.Run(repo="o/r", mode="report", outcome="report", timestamp=state.now_iso(),
                                   provider="claude-sub", escalated_from="local-gpu:tempfail"),
                         db_path=self.state_db)
        out = self.status()
        self.assertIn("provider", out.splitlines()[0])
        self.assertIn("claude-sub*", out)
        self.assertIn("* escalated", out)


class RouteCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "a.py").write_text("print(1)\n")
        env = patch.dict(os.environ, {"GARDENER_STATE_DIR": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def route(self, *argv):
        args = cli.build_parser().parse_args(["route", *argv])
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            code = args.func(args)
        return code, out.getvalue(), err.getvalue()

    def test_parses_with_report_as_the_default_mode(self):
        args = cli.build_parser().parse_args(["route", "--repo", "o/r"])
        self.assertEqual((args.mode, args.func), ("report", cli.cmd_route))

    def test_without_a_routing_file_says_routing_is_off(self):
        code, out, _ = self.route("--repo", "o/r", "--routing-file", str(self.tmp / "none.json"))
        self.assertEqual(code, 0)
        self.assertIn("routing is off", out)

    def test_explains_from_a_path(self):
        cfg = self.tmp / "routing.json"
        cfg.write_text(json.dumps(ROUTING))
        with patch("gardener.dispatch.shutil.which", return_value="/usr/bin/x"):
            code, out, _ = self.route("--repo", "o/r", "--routing-file", str(cfg), "--path", str(self.tmp),
                                      "--no-health")
        self.assertEqual(code, 0)
        self.assertIn("tier S", out)
        self.assertIn("1. local-gpu", out)

    def test_uses_the_cached_clone_and_errors_when_there_is_none(self):
        cfg = self.tmp / "routing.json"
        cfg.write_text(json.dumps(ROUTING))
        with patch.dict(os.environ, {"GARDENER_CACHE_DIR": str(self.tmp / "cache")}):
            code, _, err = self.route("--repo", "o/r", "--routing-file", str(cfg), "--no-health")
            self.assertEqual(code, 2)
            self.assertIn("no clone of o/r", err)
            clone = cli.cached_clone_path("o/r")
            clone.mkdir(parents=True)
            (clone / "x.py").write_text("x = 1\n")
            with patch("gardener.dispatch.shutil.which", return_value="/usr/bin/x"):
                code, out, _ = self.route("--repo", "o/r", "--mode", "tend", "--routing-file", str(cfg),
                                          "--no-health")
        self.assertEqual(code, 0)
        self.assertIn("1. claude-sub", out)
        self.assertNotIn("local-gpu", out)    # rule 1 names only claude-sub for tend


if __name__ == "__main__":
    unittest.main()
