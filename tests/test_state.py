"""Real sqlite3 against a tmp db file — no mocking the thing this module
exists to wrap."""
import os
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from gardener import garden, merge_allowlist, notify, overnight, repo_lock, run_log, state


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


class TestRepoStats(unittest.TestCase):
    """`repo_stats` is what the dashboard's garden plot draws a plant from,
    so it aggregates the *whole* history, not a recent-N window."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "gardener.sqlite3"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _record(self, repo, outcome, timestamp, cost=None, duration=None, mode="tend"):
        state.record_run(
            state.Run(
                repo=repo, mode=mode, outcome=outcome, timestamp=timestamp,
                cost_usd=cost, duration_ms=duration,
            ),
            db_path=self.db_path,
        )

    def test_missing_db_returns_empty_mapping(self):
        self.assertEqual(state.repo_stats(db_path=self.db_path), {})

    def test_aggregates_per_repo_across_the_whole_history(self):
        self._record("owner/a", "tend", "2026-07-01T00:00:00+00:00", cost=1.0, duration=1000)
        self._record("owner/a", "error", "2026-07-02T00:00:00+00:00", cost=0.5, duration=500)
        self._record("owner/a", "tend", "2026-07-03T00:00:00+00:00", cost=2.0, duration=2000)
        self._record("owner/b", "created", "2026-07-04T00:00:00+00:00", mode="create-dev-loop")

        stats = state.repo_stats(db_path=self.db_path)
        self.assertEqual(set(stats), {"owner/a", "owner/b"})
        a = stats["owner/a"]
        self.assertEqual((a.runs, a.successes, a.errors), (3, 2, 1))
        self.assertEqual(a.first_run, "2026-07-01T00:00:00+00:00")
        self.assertEqual(a.last_run, "2026-07-03T00:00:00+00:00")
        self.assertEqual(a.cost_usd, 3.5)
        self.assertEqual(a.duration_ms, 3500)
        # `created` is a success too — the create-dev-loop bootstrap
        # dispatch did its job, so it must not count as a wilting repo.
        self.assertEqual(stats["owner/b"].successes, 1)

    def test_last_success_ignores_a_later_error(self):
        # A repo tended successfully yesterday and erroring today is not
        # untended — the plot's droop is keyed on last_success, so an
        # error must not be allowed to masquerade as a fresh tend.
        self._record("owner/a", "tend", "2026-07-01T00:00:00+00:00")
        self._record("owner/a", "error", "2026-07-05T00:00:00+00:00")
        stats = state.repo_stats(db_path=self.db_path)["owner/a"]
        self.assertEqual(stats.last_success, "2026-07-01T00:00:00+00:00")
        self.assertEqual(stats.last_run, "2026-07-05T00:00:00+00:00")
        self.assertEqual(stats.last_outcome, "error")

    def test_never_successful_repo_has_no_last_success(self):
        self._record("owner/a", "error", "2026-07-01T00:00:00+00:00")
        stats = state.repo_stats(db_path=self.db_path)["owner/a"]
        self.assertIsNone(stats.last_success)
        self.assertEqual(stats.successes, 0)

    def test_last_outcome_uses_insertion_order_not_the_timestamp(self):
        # Timestamps are second-resolution, so two repos dispatched
        # concurrently can tie; `id` is what actually orders them.
        self._record("owner/a", "tend", "2026-07-01T00:00:00+00:00")
        self._record("owner/a", "error", "2026-07-01T00:00:00+00:00")
        self.assertEqual(state.repo_stats(db_path=self.db_path)["owner/a"].last_outcome, "error")

    def test_null_costs_sum_to_zero_rather_than_none(self):
        self._record("owner/a", "tend", "2026-07-01T00:00:00+00:00", cost=None)
        stats = state.repo_stats(db_path=self.db_path)["owner/a"]
        self.assertEqual(stats.cost_usd, 0.0)
        self.assertEqual(stats.duration_ms, 0)

    def test_every_known_outcome_counts_as_a_success_or_an_error(self):
        """No outcome may fall through the gap between `SUCCESS_OUTCOMES`
        and `ERROR_OUTCOME`. One that does is counted as neither, which is
        invisible rather than conservative: the dashboard's garden view
        reads `successes`/`last_success` straight off this, so a repo whose
        runs all succeeded gets drawn as a struggling plant with zero
        tends. `implement`, `file-issue`, and `created_incomplete` all did
        exactly that until issue #67."""
        for i, outcome in enumerate(sorted(state.KNOWN_OUTCOMES)):
            self._record("owner/a", outcome, f"2026-07-{i + 1:02d}T00:00:00+00:00")
        stats = state.repo_stats(db_path=self.db_path)["owner/a"]
        self.assertEqual(stats.runs, len(state.KNOWN_OUTCOMES))
        self.assertEqual(
            stats.successes + stats.errors,
            stats.runs,
            "an outcome in KNOWN_OUTCOMES is classified as neither a success nor an "
            "error — add it to SUCCESS_OUTCOMES or make it ERROR_OUTCOME",
        )
        self.assertEqual(stats.successes, len(state.SUCCESS_OUTCOMES))
        self.assertEqual(stats.errors, 1)

    def test_a_successful_align_implement_run_is_a_success(self):
        """`cmd_align` records `mode.value` verbatim on success, so an
        `--implement`/`--file-issue` run's outcome is the mode name — not
        `tend`. Regression guard for issue #67."""
        self._record("owner/a", "implement", "2026-07-01T00:00:00+00:00", mode="implement")
        self._record("owner/a", "file-issue", "2026-07-02T00:00:00+00:00", mode="file-issue")
        stats = state.repo_stats(db_path=self.db_path)["owner/a"]
        self.assertEqual((stats.runs, stats.successes, stats.errors), (2, 2, 0))
        self.assertEqual(stats.last_success, "2026-07-02T00:00:00+00:00")

    def test_a_step6_incomplete_bootstrap_is_still_a_success(self):
        """`created_incomplete` means the `<slug>-dev-loop` skill was
        created and usable — `_run_tend_dispatch` proceeds straight to the
        real tend dispatch after it. What's incomplete is create-dev-loop's
        own Step 6 GitHub tracker repo, which says nothing about the target
        repo's health.

        Spelled as a literal rather than `state.CREATED_INCOMPLETE_OUTCOME`
        on purpose: this is the exact string already sitting in rows of
        every existing `gardener.sqlite3`, and `repo_stats` has to keep
        classifying those correctly even if the constant is ever renamed.
        `tests/test_cli.py` covers the constant-to-record-site coupling."""
        self._record(
            "owner/a", "created_incomplete", "2026-07-01T00:00:00+00:00", mode="create-dev-loop"
        )
        stats = state.repo_stats(db_path=self.db_path)["owner/a"]
        self.assertEqual((stats.successes, stats.errors), (1, 0))
        self.assertEqual(stats.last_success, "2026-07-01T00:00:00+00:00")


class TestStateDirIsHonouredEverywhere(unittest.TestCase):
    """Eight helpers across seven modules each resolve `GARDENER_STATE_DIR`
    with their own private copy of the same three lines — `state.py`,
    `garden.py`, `merge_allowlist.py`, `overnight.py`, `notify.py`,
    `run_log.py`, and `repo_lock.py`. Nothing makes them agree; they agree
    only because each was written the same way.

    That matters because they are two halves of the same conversations: the
    dashboard reads the logs dir `run_log.py` writes, `cmd_overnight` reads
    the cursor beside the db `state.py` records into, and `repo_lock.py`'s
    exclusion is only exclusion if every process computes the same lock
    path. One module drifting doesn't fail loudly — it silently reads or
    writes somewhere else. Assert the agreement rather than trusting seven
    copies to stay in sync.

    Filenames are asserted verbatim, not derived, so a rename is also
    caught: these paths are on-disk state a deployed box already has, and
    changing one silently orphans real data.
    """

    #: (label, callable taking no args, expected path relative to the state dir)
    def _cases(self):
        return [
            ("state.default_state_dir", state.default_state_dir, ""),
            ("state.default_db_path", state.default_db_path, "gardener.sqlite3"),
            ("garden.default_garden_path", garden.default_garden_path, "garden.json"),
            (
                "merge_allowlist.default_allowlist_path",
                merge_allowlist.default_allowlist_path,
                "merge_allowlist.json",
            ),
            ("notify.default_webhook_config_path", notify.default_webhook_config_path, "notify.env"),
            ("overnight.default_cursor_path", overnight.default_cursor_path, "overnight_cursor.json"),
            ("run_log.default_logs_dir", run_log.default_logs_dir, "logs"),
            ("repo_lock.lock_file_path", lambda: repo_lock.lock_file_path("owner/repo"), "locks/owner__repo.lock"),
        ]

    def test_every_helper_honours_the_override(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with patch.dict("os.environ", {"GARDENER_STATE_DIR": str(base)}, clear=False):
                for label, fn, relative in self._cases():
                    with self.subTest(helper=label):
                        expected = base / relative if relative else base
                        self.assertEqual(fn(), expected)

    def test_every_helper_falls_back_to_the_same_home_dir(self):
        """With no override set, all eight must land under
        `~/.local/state/gardener` — the path `README.md` and `docs/USAGE.md`
        both tell an operator to look in."""
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            expected_base = fake_home / ".local" / "state" / "gardener"
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("GARDENER_STATE_DIR", None)
                with patch.object(Path, "home", classmethod(lambda cls: fake_home)):
                    for label, fn, relative in self._cases():
                        with self.subTest(helper=label):
                            expected = expected_base / relative if relative else expected_base
                            self.assertEqual(fn(), expected)


if __name__ == "__main__":
    unittest.main()
