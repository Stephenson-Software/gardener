"""Real sqlite3 against a tmp db file — no mocking the thing this module
exists to wrap."""
import unittest
from pathlib import Path
import tempfile

from gardener import state


class TestState(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "gardener.sqlite3"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_record_and_list_round_trip(self):
        run = state.Run(
            repo="dmccoystephenson/create-dev-loop",
            mode="report",
            outcome="report",
            timestamp=state.now_iso(),
            gap_summary="3 gaps found across 2 sections",
            exit_code=0,
            duration_ms=1234,
            cost_usd=0.05,
            claude_session_id="abc-123",
        )
        run_id = state.record_run(run, db_path=self.db_path)
        self.assertIsInstance(run_id, int)

        rows = state.list_runs(db_path=self.db_path)
        self.assertEqual(len(rows), 1)
        got = rows[0]
        self.assertEqual(got.repo, run.repo)
        self.assertEqual(got.mode, "report")
        self.assertEqual(got.gap_summary, run.gap_summary)
        self.assertEqual(got.exit_code, 0)
        self.assertEqual(got.claude_session_id, "abc-123")

    def test_list_runs_empty_db_path_returns_empty(self):
        missing = Path(self._tmpdir.name) / "does-not-exist.sqlite3"
        self.assertEqual(state.list_runs(db_path=missing), [])

    def test_list_runs_orders_most_recent_first(self):
        for i in range(3):
            state.record_run(
                state.Run(
                    repo="a/b",
                    mode="report",
                    outcome="report",
                    timestamp=f"2026-01-0{i+1}T00:00:00+00:00",
                    gap_summary=f"run {i}",
                ),
                db_path=self.db_path,
            )
        rows = state.list_runs(db_path=self.db_path)
        self.assertEqual([r.gap_summary for r in rows], ["run 2", "run 1", "run 0"])

    def test_list_runs_filters_by_repo(self):
        state.record_run(
            state.Run(repo="a/one", mode="report", outcome="report", timestamp=state.now_iso()),
            db_path=self.db_path,
        )
        state.record_run(
            state.Run(repo="a/two", mode="report", outcome="report", timestamp=state.now_iso()),
            db_path=self.db_path,
        )
        rows = state.list_runs(db_path=self.db_path, repo="a/one")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].repo, "a/one")

    def test_list_runs_respects_limit(self):
        for i in range(5):
            state.record_run(
                state.Run(repo="a/b", mode="report", outcome="report", timestamp=state.now_iso()),
                db_path=self.db_path,
            )
        rows = state.list_runs(db_path=self.db_path, limit=2)
        self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
