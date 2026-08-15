"""Real sqlite3 against a tmp db file — no mocking the thing this module
exists to wrap."""
import os
import sqlite3
import unittest
from contextlib import closing
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


class TestSessionStats(unittest.TestCase):
    """`session_stats` is what the dashboard's headline panel is scoped to,
    so what it must get right is the *boundary*: a previous night's errors
    and dollars counted as this night's is the bug it exists to fix (issue
    #105)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "gardener.sqlite3"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _record(self, timestamp, outcome="tend", cost=None, repo="owner/a"):
        state.record_run(
            state.Run(
                repo=repo, mode="tend", outcome=outcome,
                timestamp=timestamp, cost_usd=cost,
            ),
            db_path=self.db_path,
        )

    def test_missing_db_is_a_zeroed_session_not_an_error(self):
        got = state.session_stats(db_path=self.db_path)
        self.assertEqual((got.runs, got.errors, got.cost_usd), (0, 0, 0.0))
        self.assertIsNone(got.started_at)
        self.assertIsNone(got.ended_at)

    def test_existing_db_with_no_rows_is_a_zeroed_session(self):
        # Distinct from the missing-db case above: the file exists (log
        # pruning and a manual DELETE both leave one behind), so the early
        # return doesn't cover it — the walk itself has to survive.
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.executescript(state.SCHEMA)
        got = state.session_stats(db_path=self.db_path)
        self.assertEqual(got.runs, 0)
        self.assertIsNone(got.started_at)

    def test_runs_within_the_gap_are_one_session(self):
        self._record("2026-08-09T01:00:00+00:00", cost=1.0)
        self._record("2026-08-09T03:00:00+00:00", outcome="error", cost=0.5)
        self._record("2026-08-09T04:30:00+00:00", cost=0.25)

        got = state.session_stats(db_path=self.db_path)
        self.assertEqual((got.runs, got.errors, got.cost_usd), (3, 1, 1.75))
        self.assertEqual(got.started_at, "2026-08-09T01:00:00+00:00")
        self.assertEqual(got.ended_at, "2026-08-09T04:30:00+00:00")

    def test_a_previous_nights_runs_are_excluded(self):
        # The exact shape of the reported bug: a 40-row window spanning two
        # dates attributed the older night's error and spend to this one.
        self._record("2026-08-08T02:34:00+00:00", outcome="error", cost=9.0)
        self._record("2026-08-08T03:00:00+00:00", cost=9.0)
        self._record("2026-08-09T02:00:00+00:00", cost=1.0)
        self._record("2026-08-09T02:30:00+00:00", cost=2.0)

        got = state.session_stats(db_path=self.db_path)
        self.assertEqual((got.runs, got.errors, got.cost_usd), (2, 0, 3.0))
        self.assertEqual(got.started_at, "2026-08-09T02:00:00+00:00")

    def test_the_gap_boundary_is_exact(self):
        # Exactly the threshold still counts as the same session; one
        # second more does not.
        self._record("2026-08-09T00:00:00+00:00")
        self._record("2026-08-09T06:00:00+00:00")
        self.assertEqual(state.session_stats(db_path=self.db_path).runs, 2)

        self._record("2026-08-09T12:00:01+00:00")
        self.assertEqual(state.session_stats(db_path=self.db_path).runs, 1)

    def test_unbroken_activity_is_still_capped_at_the_max_span(self):
        # The gap rule alone chains: runs every five hours are each inside
        # the threshold, so without a cap a week of them would be one
        # "session" — the multi-day window this scoping exists to remove,
        # arrived at from the other direction.
        for hour in range(0, 40, 5):
            day, rest = divmod(hour, 24)
            self._record(f"2026-08-{9 + day:02d}T{rest:02d}:00:00+00:00")

        # Newest is 2026-08-10T11:00; the cap admits everything back to
        # 2026-08-09T15:00 and stops one row short of a second day.
        got = state.session_stats(db_path=self.db_path)
        self.assertEqual(got.ended_at, "2026-08-10T11:00:00+00:00")
        self.assertEqual(got.started_at, "2026-08-09T15:00:00+00:00")
        self.assertEqual(got.runs, 5)

    def test_the_span_cap_is_measured_from_the_newest_run(self):
        self._record("2026-08-09T00:00:00+00:00")
        self._record("2026-08-10T00:00:00+00:00")
        # Exactly the cap, so still one session.
        self.assertEqual(
            state.session_stats(db_path=self.db_path, gap_seconds=48 * 3600).runs, 2
        )
        self.assertEqual(
            state.session_stats(
                db_path=self.db_path, gap_seconds=48 * 3600, max_span_seconds=86399
            ).runs,
            1,
        )

    def test_gap_seconds_is_injectable(self):
        self._record("2026-08-09T01:00:00+00:00")
        self._record("2026-08-09T02:00:00+00:00")
        self.assertEqual(
            state.session_stats(db_path=self.db_path, gap_seconds=60).runs, 1
        )

    def test_null_costs_sum_to_zero_rather_than_none(self):
        self._record("2026-08-09T01:00:00+00:00", cost=None)
        self._record("2026-08-09T02:00:00+00:00", cost=1.25)
        self.assertEqual(state.session_stats(db_path=self.db_path).cost_usd, 1.25)

    def test_a_session_spans_every_repo_not_just_one(self):
        # A concurrent batch records two repos inside the same window; the
        # panel this feeds is garden-wide, unlike repo_stats.
        self._record("2026-08-09T01:00:00+00:00", repo="owner/a")
        self._record("2026-08-09T01:05:00+00:00", repo="owner/b")
        self.assertEqual(state.session_stats(db_path=self.db_path).runs, 2)

    def test_an_unreadable_timestamp_ends_the_session_rather_than_joining_it(self):
        self._record("2026-08-09T01:00:00+00:00")
        self._record("not a timestamp")
        self._record("2026-08-09T02:00:00+00:00")

        # The two readable runs either side of the unreadable one are not
        # silently merged across it — the walk stops where it can no longer
        # reason about the gap.
        got = state.session_stats(db_path=self.db_path)
        self.assertEqual(got.runs, 1)
        self.assertEqual(got.started_at, "2026-08-09T02:00:00+00:00")

    def test_an_unreadable_newest_timestamp_does_not_swallow_the_history(self):
        self._record("2026-08-09T01:00:00+00:00")
        self._record("")

        got = state.session_stats(db_path=self.db_path)
        self.assertEqual(got.runs, 1)
        self.assertEqual(got.started_at, "")

    def test_naive_and_zulu_timestamps_are_read_as_utc(self):
        # Nothing gardener writes looks like either, but the db is a plain
        # file an operator can edit, and mixing naive with aware datetimes
        # would raise on the subtraction rather than degrade.
        self._record("2026-08-09T01:00:00")
        self._record("2026-08-09T02:00:00Z")
        self.assertEqual(state.session_stats(db_path=self.db_path).runs, 2)


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
