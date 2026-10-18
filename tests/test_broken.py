"""Tests for gardener/broken.py — broken-first ordering. The `gh` runner
is always injected; nothing here touches GitHub."""
import json
import subprocess
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gardener import broken

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


def _ci_node(state, branch="main"):
    return {"defaultBranchRef": {"name": branch, "target": {"statusCheckRollup": state and {"state": state}}}}


class TestCi(unittest.TestCase):
    def test_query_aliases_every_repo_in_order(self):
        q = broken.ci_query(["o/a", "o/b"])
        self.assertIn('r0: repository(owner: "o", name: "a")', q)
        self.assertIn('r1: repository(owner: "o", name: "b")', q)

    def test_only_failure_and_error_count_as_red(self):
        repos = ["o/fail", "o/err", "o/ok", "o/pending", "o/nochecks", "o/gone"]
        response = {"data": {"r0": _ci_node("FAILURE"), "r1": _ci_node("ERROR", "master"),
                             "r2": _ci_node("SUCCESS"), "r3": _ci_node("PENDING"),
                             "r4": _ci_node(None), "r5": None}}
        self.assertEqual(broken.parse_ci_response(repos, response),
                         {"o/fail": "CI is red on main", "o/err": "CI is red on master"})

    def test_garbage_response_is_nothing_broken(self):
        self.assertEqual(broken.parse_ci_response(["o/a"], None), {})
        self.assertEqual(broken.parse_ci_response(["o/a"], {"errors": []}), {})


class TestLedger(unittest.TestCase):
    def _entry(self, repo, decision, days_ago, version="1.0.0"):
        return {"repository": repo, "decision": decision, "version": version,
                "recordedAt": (NOW - timedelta(days=days_ago)).isoformat()}

    def test_newest_recent_blocked_entry_counts(self):
        ledger = {"entries": [
            self._entry("O/Held", "blocked", 1, "2.0.0"),
            self._entry("O/Fixed", "blocked", 3), self._entry("O/Fixed", "published", 1),
            self._entry("O/Old", "blocked", 30),
            self._entry("O/GateDown", "failed", 1),
            self._entry("O/NotInGarden", "blocked", 1),
        ]}
        garden = ["o/held", "O/Fixed", "O/Old", "O/GateDown"]
        self.assertEqual(broken.parse_ledger(ledger, garden, NOW),
                         {"o/held": "release 2.0.0 was held by a release gate"})

    def test_garbage_ledger_is_nothing_broken(self):
        self.assertEqual(broken.parse_ledger([], ["o/a"], NOW), {})
        self.assertEqual(broken.parse_ledger({"entries": ["x", {"repository": "o/a"}]}, ["o/a"], NOW), {})


class TestBrokenFirst(unittest.TestCase):
    FULL = ["o/a", "o/b", "o/c", "o/d"]

    def test_broken_repos_jump_in_rice_order_even_if_already_attempted(self):
        order, jumped = broken.broken_first(
            ["o/a", "o/d"], self.FULL, {"o/c": "red", "o/a": "red"}, {}, NOW)
        self.assertEqual(jumped, ["o/a", "o/c"])
        self.assertEqual(order, ["o/a", "o/c", "o/d"])

    def test_limit_and_cooldown(self):
        recent = (NOW - timedelta(days=1)).isoformat()
        old = (NOW - timedelta(days=10)).isoformat()
        order, jumped = broken.broken_first(
            ["o/a", "o/b", "o/c", "o/d"], self.FULL,
            {r: "red" for r in self.FULL}, {"o/a": recent, "o/b": old}, NOW, limit=2)
        self.assertEqual(jumped, ["o/b", "o/c"])
        self.assertEqual(order, ["o/b", "o/c", "o/a", "o/d"])

    def test_nothing_broken_leaves_the_order_alone(self):
        self.assertEqual(broken.broken_first(["o/b", "o/a"], self.FULL, {}, {}, NOW), (["o/b", "o/a"], []))


class TestFetchBroken(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.missing_config = Path(self._dir.name) / "notify.env"  # never the operator's real one

    def tearDown(self):
        self._dir.cleanup()

    def _runner(self, red):
        calls = []

        def run(argv):
            calls.append(argv)
            query = argv[-1]
            repos = [r for r in red if f'name: "{r.split("/")[1]}"' in query]
            data = {}
            for i in range(query.count("repository(")):
                data[f"r{i}"] = _ci_node("SUCCESS")
            for r in repos:
                idx = query[:query.index(f'name: "{r.split("/")[1]}"')].count("repository(") - 1
                data[f"r{idx}"] = _ci_node("FAILURE")
            # gh exits 1 when an alias fails, but stdout still has the data
            return subprocess.CompletedProcess(argv, 1, stdout=json.dumps({"data": data}), stderr="")
        return run, calls

    def test_batches_and_reads_stdout_despite_a_nonzero_exit(self):
        garden = [f"o/r{i}" for i in range(broken.CI_BATCH_SIZE + 3)]
        run, calls = self._runner(["o/r1", f"o/r{broken.CI_BATCH_SIZE + 1}"])
        found = broken.fetch_broken(garden, NOW, env={}, config_path=self.missing_config, run=run)
        self.assertEqual(len(calls), 2)
        self.assertEqual(set(found), {"o/r1", f"o/r{broken.CI_BATCH_SIZE + 1}"})

    def test_ledger_reasons_join_ci_reasons(self):
        ledger = Path(self._dir.name) / "ledger.json"
        ledger.write_text(json.dumps({"entries": [
            {"repository": "o/r1", "decision": "blocked", "version": "2.0.0", "recordedAt": NOW.isoformat()}]}))
        run, _ = self._runner(["o/r1"])
        found = broken.fetch_broken(["o/r1", "o/r2"], NOW, env={broken.ENV_RELEASE_LEDGER: str(ledger)},
                                    config_path=self.missing_config, run=run)
        self.assertEqual(found, {"o/r1": "CI is red on main; release 2.0.0 was held by a release gate"})

    def test_failures_are_skipped_not_raised(self):
        def run(argv):
            raise subprocess.TimeoutExpired(argv, 120)
        with unittest.mock.patch("sys.stderr"):
            found = broken.fetch_broken(["o/a"], NOW, env={broken.ENV_RELEASE_LEDGER: "/nonexistent/ledger.json"},
                                        config_path=self.missing_config, run=run)
        self.assertEqual(found, {})


if __name__ == "__main__":
    unittest.main()
