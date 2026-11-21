"""Tests for the routing package (docs/rfcs/provider-routing.md).

Core tests drive `core`/`RoutingService` through fakes for each port, with no patching. Adapter
tests check each production adapter against its port. The architecture test keeps the
dependency direction: model, ports and core never import adapters. No claude, gh or network.
"""
import ast
import io
import json
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

from gardener import routing
from gardener.dispatch import CLAUDE_BIN, DispatchResult, Harness, HarnessConfig, Mode
from gardener.routing import Health, RepoProfile, RoutingError, RoutingService, core, ports
from gardener.routing.__main__ import main as routing_main

ALL = [m.value for m in Mode]
CONFIG = {
    "providers": {
        "claude-sub": {"harness": "claude-code", "cost": "subscription", "concurrency": 2, "modes": ALL},
        "local-gpu": {"harness": "claude-code", "claude_bin": "/opt/local-gpu/claude-local", "cost": "free",
                      "local": True, "context_tokens": 65536, "modes": ["report"], "max_tier": "M",
                      "health_url": "http://gpu.test/health"},
        "orket": {"harness": "command", "command": ["orket-extension", "gardener"], "cost": "free",
                  "local": True, "context_tokens": 32768, "modes": ["report"], "max_tier": "S"},
    },
    "rules": [
        {"modes": ["report"], "tiers": ["S", "M"], "providers": ["orket", "local-gpu", "claude-sub"]},
        {"modes": ["*"], "tiers": ["*"], "providers": ["claude-sub"]},
    ],
    "repos": {
        "o/big": {"tier": "L"},
        "o/private": {"local_only": True},
        "o/pinned": {"providers": ["local-gpu"]},
    },
}
PACKAGE = Path(routing.__file__).parent


def parse(raw=CONFIG):
    return routing.parse_config(raw, routing.DEFAULT_KINDS)


def profile(tokens, tier=None):
    t = tier or ("S" if tokens <= 60_000 else "M" if tokens <= 400_000 else "L")
    return RepoProfile(files=10, text_bytes=tokens * 4, est_tokens=tokens, has_tests=True, tier=t)


def result(**kw):
    base = dict(ok=True, result_text="done", raw_stdout="", stderr="", exit_code=0, duration_ms=1,
                cost_usd=0.2, session_id=None, permission_denials=[], is_error=False)
    base.update(kw)
    return DispatchResult(**base)


# ----------------------------------------------------------------- fakes, one per port
class FakeHealth:
    def __init__(self, down=None):
        self.down = down or {}
        self.asked = []

    def check(self, provider):
        self.asked.append(provider.name)
        return Health.down(self.down[provider.name]) if provider.name in self.down else Health.up()


class FakeReadiness:
    def __init__(self, missing=()):
        self.missing = set(missing)

    def check(self, provider, mode):
        return Health.down("binary not found") if provider.name in self.missing else Health.up()


class FakeProfiler:
    def __init__(self, tokens):
        self.tokens = tokens

    def profile(self, path, tier_tokens):
        return profile(self.tokens)


class FakeLimiter:
    def __init__(self):
        self.held = []

    @contextmanager
    def hold(self, provider, on_wait=None):
        self.held.append(provider.name)
        yield


class FakeRunner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.configs = []

    def __call__(self, harness_config, **kwargs):
        self.configs.append(harness_config)
        return self.outcomes[len(self.configs) - 1]


class RecordingObserver:
    def __init__(self):
        self.events = []

    def routed(self, decision, profile):
        self.events.append(("routed", [p.name for p in decision.candidates]))

    def waiting(self, provider):
        self.events.append(("waiting", provider.name))

    def escalating(self, provider, trigger, next_provider):
        self.events.append(("escalating", provider, trigger, next_provider))


# ----------------------------------------------------------------- core: config
class ConfigTests(unittest.TestCase):
    def test_parse_valid(self):
        c = parse()
        self.assertEqual(set(c.providers), {"claude-sub", "local-gpu", "orket"})
        self.assertEqual(c.providers["local-gpu"].modes, frozenset({Mode.REPORT}))
        self.assertEqual(c.providers["orket"].kind, "command")
        self.assertEqual(c.providers["orket"].settings["command"], ("orket-extension", "gardener"))

    def test_rejects_bad_configs(self):
        bad = [
            {"providers": {}},
            {"providers": {"a": {"harness": "nope"}}},
            {"providers": {"a": {"harness": "command"}}},
            {"providers": {"a": {"harness": "command", "command": []}}},
            {"providers": {"a": {"modes": ["reprot"]}}},
            {"providers": {"a": {"max_tier": "XL"}}},
            {"providers": {"a": {"cost": "cheap"}}},
            {"providers": {"a": {"claude_bin": ""}}},
            {"providers": {"a": {"comand": ["x"]}}},                       # typo: unknown key
            {"providers": {"a": {"harness": "command", "command": ["x"], "claude_bin": "y"}}},  # not its key
            {"providers": {"a": {}}, "rules": [{"providers": ["b"]}]},
            {"providers": {"a": {}}, "rules": [{"tiers": ["Q"], "providers": ["a"]}]},
            {"providers": {"a": {}}, "repos": {"o/r": {"providers": ["zzz"]}}},
            {"providers": {"a": {}}, "escalate_on": ["sometimes"]},
            {"providers": {"a": {}}, "tier_tokens": {"S": 500, "M": 100}},
        ]
        for raw in bad:
            with self.subTest(raw=raw):
                with self.assertRaises(RoutingError):
                    parse(raw)

    def test_routing_file_cannot_widen_command_harness_scope(self):
        raw = {"providers": {"a": {"harness": "command", "command": ["x"], "unscoped_modes": ["tend"]}}}
        with self.assertRaises(RoutingError) as cm:
            parse(raw)
        self.assertIn("GARDENER_HARNESS_ALLOW_UNSCOPED", str(cm.exception))

    def test_kinds_are_pluggable(self):
        """A new provider kind is one adapter: the core accepts its keys and nothing else changes."""

        class EchoKind:
            name = "echo"
            settings_keys = frozenset({"voice"})

            def validate(self, provider, settings):
                return settings

            def harness_config(self, provider, base):
                return HarnessConfig(harness=Harness.COMMAND, command=("echo", provider.settings["voice"]))

        kinds = dict(routing.DEFAULT_KINDS, echo=EchoKind())
        c = routing.parse_config({"providers": {"e": {"harness": "echo", "voice": "hi"}},
                                  "rules": [{"providers": ["e"]}]}, kinds)
        self.assertIsInstance(kinds["echo"], ports.ProviderKind)
        d = routing.route(c, Mode.REPORT, "o/r", profile(1))
        runner = FakeRunner([result()])
        core.run_with_escalation(d, runner, HarnessConfig(), kinds)
        self.assertEqual(runner.configs[0].command, ("echo", "hi"))


# ----------------------------------------------------------------- core: routing
class RouteTests(unittest.TestCase):
    def setUp(self):
        self.c = parse()

    def names(self, d):
        return [p.name for p in d.candidates]

    def test_small_report_prefers_cheap_local(self):
        d = routing.route(self.c, Mode.REPORT, "o/small", profile(20_000), health=FakeHealth())
        self.assertEqual(self.names(d), ["orket", "local-gpu", "claude-sub"])
        self.assertEqual(d.rule, 0)

    def test_tier_limits(self):
        d = routing.route(self.c, Mode.REPORT, "o/medium", profile(300_000), health=FakeHealth())
        self.assertEqual(self.names(d), ["local-gpu", "claude-sub"])
        self.assertIn("max_tier", dict(d.skipped)["orket"])

    def test_context_multiple(self):
        d = routing.route(self.c, Mode.REPORT, "o/m", profile(600_000, tier="M"), health=FakeHealth())
        self.assertNotIn("local-gpu", self.names(d))
        self.assertIn("context", dict(d.skipped)["local-gpu"])

    def test_write_modes_go_to_subscription(self):
        d = routing.route(self.c, Mode.TEND, "o/small", profile(1_000), health=FakeHealth())
        self.assertEqual(self.names(d), ["claude-sub"])
        self.assertEqual(d.rule, 1)

    def test_repo_tier_override(self):
        d = routing.route(self.c, Mode.REPORT, "o/big", profile(1_000), health=FakeHealth())
        self.assertEqual(d.tier, "L")
        self.assertEqual(self.names(d), ["claude-sub"])

    def test_local_only_repo_never_leaves_local_providers(self):
        d = routing.route(self.c, Mode.REPORT, "o/private", profile(1_000), health=FakeHealth())
        self.assertEqual(self.names(d), ["orket", "local-gpu"])
        self.assertIn("local_only", dict(d.skipped)["claude-sub"])
        d = routing.route(self.c, Mode.TEND, "o/private", profile(1_000), health=FakeHealth())
        self.assertEqual(self.names(d), [])            # no local provider may tend: refuse, don't leak

    def test_repo_pinned_providers(self):
        d = routing.route(self.c, Mode.REPORT, "o/pinned", profile(1_000), health=FakeHealth())
        self.assertEqual(self.names(d), ["local-gpu"])
        self.assertIsNone(d.rule)

    def test_paused_provider_skipped(self):
        health = FakeHealth(down={"local-gpu": "paused by its operator"})
        d = routing.route(self.c, Mode.REPORT, "o/small", profile(1_000), health=health)
        self.assertEqual(self.names(d), ["orket", "claude-sub"])
        self.assertIn("paused", dict(d.skipped)["local-gpu"])
        self.assertEqual(health.asked, ["local-gpu"])   # only providers with a health_url are probed

    def test_readiness_runs_before_health(self):
        health = FakeHealth()
        d = routing.route(self.c, Mode.REPORT, "o/small", profile(1_000), health=health,
                          readiness=FakeReadiness(missing={"local-gpu"}))
        self.assertNotIn("local-gpu", self.names(d))
        self.assertEqual(health.asked, [])

    def test_explain_mentions_choices_and_skips(self):
        text = routing.route(self.c, Mode.REPORT, "o/medium", profile(300_000), health=FakeHealth()).explain()
        self.assertIn("1. local-gpu", text)
        self.assertIn("skip orket", text)


# ----------------------------------------------------------------- core: escalation
class EscalationTests(unittest.TestCase):
    def setUp(self):
        self.d = routing.route(parse(), Mode.REPORT, "o/small", profile(1_000), health=FakeHealth())

    def run_it(self, outcomes, **kw):
        runner = FakeRunner(outcomes)
        res, attempts = routing.run_with_escalation(self.d, runner, HarnessConfig(), routing.DEFAULT_KINDS, **kw)
        return res, attempts, runner

    def test_first_good_answer_wins(self):
        res, attempts, runner = self.run_it([result()])
        self.assertTrue(res.ok)
        self.assertEqual(attempts, [("orket", None)])
        self.assertEqual(runner.configs[0].harness, Harness.COMMAND)
        self.assertEqual(res.provider, "orket")
        self.assertIsNone(res.cost_usd)                 # free provider: no made-up price

    def test_escalates_through_tempfail_and_timeout(self):
        observer, limiter = RecordingObserver(), FakeLimiter()
        res, attempts, runner = self.run_it([result(ok=False, exit_code=75, is_error=True),
                                             result(ok=False, timed_out=True, exit_code=None),
                                             result()], observer=observer, limiter=limiter)
        self.assertEqual([a[0] for a in attempts], ["orket", "local-gpu", "claude-sub"])
        self.assertEqual([a[1] for a in attempts], ["tempfail", "timeout", None])
        self.assertEqual(runner.configs[1].claude_bin, "/opt/local-gpu/claude-local")
        self.assertEqual(res.escalated_from, ("orket:tempfail", "local-gpu:timeout"))
        self.assertEqual(res.cost_usd, 0.2)             # subscription keeps its number
        self.assertEqual(limiter.held, ["orket", "local-gpu", "claude-sub"])
        self.assertEqual(observer.events, [("escalating", "orket", "tempfail", "local-gpu"),
                                           ("escalating", "local-gpu", "timeout", "claude-sub")])

    def test_trigger_not_in_escalate_on_stops(self):
        res, attempts, _ = self.run_it([result(ok=False, is_error=True, exit_code=1), result()],
                                       escalate_on=frozenset({"blocked"}))
        self.assertFalse(res.ok)
        self.assertEqual(attempts, [("orket", "error")])

    def test_blocked_escalates(self):
        _, attempts, _ = self.run_it([result(ok=False, blocked=True, is_error=True), result()])
        self.assertEqual(attempts[0], ("orket", "blocked"))

    def test_no_candidates_refuses_loudly(self):
        empty = routing.Decision("o/r", Mode.TEND, "S", None, [], [("local-gpu", "not trusted with tend")])
        with self.assertRaises(RoutingError) as cm:
            routing.run_with_escalation(empty, FakeRunner([result()]), HarnessConfig(), routing.DEFAULT_KINDS)
        self.assertIn("not trusted with tend", str(cm.exception))


# ----------------------------------------------------------------- service
class ServiceTests(unittest.TestCase):
    def service(self, tokens=1_000, **kw):
        return RoutingService(parse(), profiler=FakeProfiler(tokens), health=kw.pop("health", FakeHealth()),
                              limiter=FakeLimiter(), observer=kw.pop("observer", RecordingObserver()), **kw)

    def test_dispatch_routes_and_runs(self):
        observer = RecordingObserver()
        svc = self.service(observer=observer)
        decision, res = svc.dispatch(Mode.REPORT, "o/r", Path("."), FakeRunner([result()]), HarnessConfig(),
                                     prompt="p")
        self.assertEqual(res.provider, "orket")
        self.assertEqual(observer.events[0], ("routed", ["orket", "local-gpu", "claude-sub"]))

    def test_dispatch_with_no_candidate_returns_none(self):
        decision, res = self.service().dispatch(Mode.TEND, "o/private", Path("."), FakeRunner([]),
                                                HarnessConfig())
        self.assertIsNone(res)
        self.assertEqual(decision.candidates, [])

    def test_explain_has_profile_line(self):
        text = self.service(tokens=300_000).explain(Mode.REPORT, "o/r", Path("."))
        self.assertTrue(text.startswith("profile: 10 text files, ~300,000 tokens"))
        self.assertIn("1. local-gpu", text)

    def test_first_choice_never_probes_health(self):
        health = FakeHealth(down={"local-gpu": "paused"})
        svc = self.service(tokens=300_000, health=health)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(svc.first_choice(Mode.REPORT, "o/r", Path(d)), "local-gpu")
            self.assertIsNone(svc.first_choice(Mode.REPORT, "o/r", Path(d) / "missing"))
        self.assertEqual(health.asked, [])


# ----------------------------------------------------------------- adapters
class AdapterContractTests(unittest.TestCase):
    def test_every_adapter_satisfies_its_port(self):
        pairs = [
            (routing.ClaudeCodeKind(), ports.ProviderKind),
            (routing.CommandKind(), ports.ProviderKind),
            (routing.HttpHealthProbe(), ports.HealthProbe),
            (routing.AssumeHealthy(), ports.HealthProbe),
            (routing.DispatchReadiness(HarnessConfig()), ports.Readiness),
            (routing.FilesystemProfiler(), ports.RepoProfiler),
            (routing.JsonFileSource(Path("/nonexistent")), ports.RoutingSource),
            (routing.SemaphoreLimiter(), ports.ConcurrencyLimiter),
            (routing.DispatchRunner(), ports.AgentRunner),
            (routing.StderrObserver(), ports.RoutingObserver),
            (routing.NullObserver(), ports.RoutingObserver),
        ]
        for adapter, port in pairs:
            with self.subTest(adapter=type(adapter).__name__):
                self.assertIsInstance(adapter, port)

    def test_kinds_keep_operator_settings(self):
        base = HarnessConfig(unscoped_modes=frozenset({Mode.TEND}), align_timeout=123)
        c = parse()
        hc = routing.ClaudeCodeKind().harness_config(c.providers["local-gpu"], base)
        self.assertEqual((hc.harness, hc.claude_bin), (Harness.CLAUDE_CODE, "/opt/local-gpu/claude-local"))
        self.assertEqual(hc.align_timeout, 123)
        self.assertEqual(hc.unscoped_modes, frozenset({Mode.TEND}))   # the operator's, untouched
        hc = routing.CommandKind().harness_config(c.providers["orket"], HarnessConfig())
        self.assertEqual((hc.harness, hc.command, hc.claude_bin), (Harness.COMMAND, ("orket-extension", "gardener"),
                                                                   CLAUDE_BIN))
        self.assertEqual(hc.unscoped_modes, frozenset())

    def test_http_health_contract(self):
        provider = parse().providers["local-gpu"]

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(body=None, error=None):
            def open_(url, timeout):
                if error:
                    raise error
                return Answer(body)
            return open_

        cases = [
            (b'{"ok": true, "paused": false}', None, True, ""),
            (b'{"ok": true, "paused": true}', None, False, "paused"),
            (b'{"ok": false}', None, False, "unhealthy"),
            (b'[1, 2]', None, False, "not a JSON object"),
            (b'not json', None, False, "unreachable"),
            (None, OSError("refused"), False, "unreachable"),
        ]
        for body, error, ok, reason in cases:
            with self.subTest(body=body, error=error):
                h = routing.HttpHealthProbe(opener=opener(body, error)).check(provider)
                self.assertEqual(h.ok, ok)
                self.assertIn(reason, h.reason)

    def test_filesystem_profiler_counts_text_skips_vendor_and_binary(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("x = 1\n" * 1000)          # 6000 bytes
            (root / "node_modules").mkdir()
            (root / "node_modules" / "big.js").write_text("y" * 100_000)
            (root / "logo.png").write_bytes(b"\x89PNG\0\0" * 1000)
            (root / "tests").mkdir()
            (root / "tests" / "test_a.py").write_text("pass\n")
            p = routing.FilesystemProfiler().profile(root)
        self.assertEqual((p.files, p.text_bytes, p.est_tokens), (2, 6005, 6005 // 4))
        self.assertTrue(p.has_tests)
        self.assertEqual(p.tier, "S")

    def test_filesystem_profiler_tier_thresholds(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.txt").write_text("z" * 4000)                     # ~1000 tokens
            self.assertEqual(routing.FilesystemProfiler().profile(Path(d), {"S": 100, "M": 500}).tier, "L")
            self.assertEqual(routing.FilesystemProfiler().profile(Path(d), {"S": 100, "M": 5000}).tier, "M")

    def test_json_file_source(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "routing.json"
            self.assertIsNone(routing.JsonFileSource(p).load())            # missing: routing off
            p.write_text(json.dumps(CONFIG), encoding="utf-8-sig")         # a BOM is fine
            self.assertIn("local-gpu", routing.JsonFileSource(p).load().providers)
            p.write_text("{nope")
            with self.assertRaises(RoutingError):
                routing.JsonFileSource(p).load()

    def test_stderr_observer_lines(self):
        buf = io.StringIO()
        obs = routing.StderrObserver(buf)
        d = routing.route(parse(), Mode.REPORT, "o/r", profile(300_000), health=FakeHealth())
        obs.routed(d, profile(300_000))
        obs.escalating("local-gpu", "tempfail", "claude-sub")
        text = buf.getvalue()
        self.assertIn("gardener: routing o/r (report): ~300,000 tokens, tier M, providers: local-gpu, claude-sub",
                      text)
        self.assertIn("skipped orket", text)
        self.assertIn("local-gpu ended with tempfail; escalating to claude-sub", text)


# ----------------------------------------------------------------- architecture
class ArchitectureTests(unittest.TestCase):
    """Dependencies point inward: the domain, the ports and the core never import adapters."""

    def imports(self, path):
        names = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
        return names

    def test_inner_layers_never_import_adapters_or_service(self):
        inner = [PACKAGE / "model.py", PACKAGE / "core.py", *sorted((PACKAGE / "ports").glob("*.py"))]
        for path in inner:
            with self.subTest(path=path.name):
                bad = {m for m in self.imports(path)
                       if m.startswith(("gardener.routing.adapters", "gardener.routing.service"))}
                self.assertEqual(bad, set())

    def test_one_class_per_port_and_adapter_file(self):
        for folder in ("ports", "adapters"):
            for path in sorted((PACKAGE / folder).glob("*.py")):
                if path.name == "__init__.py":
                    continue
                with self.subTest(path=f"{folder}/{path.name}"):
                    classes = [n for n in ast.parse(path.read_text()).body if isinstance(n, ast.ClassDef)]
                    self.assertEqual(len(classes), 1, [c.name for c in classes])


# ----------------------------------------------------------------- CLI dry run
class CliTests(unittest.TestCase):
    def test_explain_without_routing_file(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = routing_main(["--repo", "o/r", "--path", d, "--mode", "report",
                                     "--config", str(Path(d) / "none.json")])
        self.assertEqual(code, 0)
        self.assertIn("routing is off", buf.getvalue())

    def test_explain_with_routing_file(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "routing.json"
            cfg.write_text(json.dumps(CONFIG))
            (Path(d) / "a.py").write_text("print(1)\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = routing_main(["--repo", "o/r", "--path", d, "--mode", "report", "--config", str(cfg),
                                     "--no-health"])
        self.assertEqual(code, 0)
        self.assertIn("1. orket", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
