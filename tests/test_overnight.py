"""overnight.py's pure logic — cursor persistence, rotation order, batching
for concurrent dispatch, the budget/headroom check, and outcome
classification/summary building. None of this invokes `claude`, `git`, or
`gh`; `cmd_overnight`'s real-dispatch orchestration in cli.py is covered
separately in test_cli.py with `_dispatch_tend` mocked."""
import json
import tempfile
import unittest
from pathlib import Path

from gardener import overnight, state
from gardener.notify import Level


class TestCursor(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "cursor.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_missing_file_reads_as_zero_not_an_error(self):
        self.assertEqual(overnight.read_cursor(path=self.path), 0)

    def test_write_then_read_round_trips(self):
        overnight.write_cursor(3, path=self.path)
        self.assertEqual(overnight.read_cursor(path=self.path), 3)

    def test_creates_parent_directories(self):
        nested = Path(self._tmpdir.name) / "a" / "b" / "cursor.json"
        overnight.write_cursor(1, path=nested)
        self.assertTrue(nested.exists())

    def test_malformed_json_reads_as_zero(self):
        self.path.write_text("not json{{{")
        self.assertEqual(overnight.read_cursor(path=self.path), 0)

    def test_non_object_json_reads_as_zero(self):
        self.path.write_text(json.dumps([1, 2, 3]))
        self.assertEqual(overnight.read_cursor(path=self.path), 0)

    def test_negative_index_reads_as_zero(self):
        self.path.write_text(json.dumps({"next_index": -5}))
        self.assertEqual(overnight.read_cursor(path=self.path), 0)

    def test_non_int_index_reads_as_zero(self):
        self.path.write_text(json.dumps({"next_index": "three"}))
        self.assertEqual(overnight.read_cursor(path=self.path), 0)

    def test_persists_as_plain_json_object(self):
        overnight.write_cursor(2, path=self.path)
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw, {"next_index": 2})


class TestReposToAttempt(unittest.TestCase):
    def test_empty_garden_returns_empty(self):
        self.assertEqual(overnight.repos_to_attempt([], 0), [])

    def test_starts_at_zero_by_default(self):
        garden = ["a", "b", "c"]
        self.assertEqual(overnight.repos_to_attempt(garden, 0), ["a", "b", "c"])

    def test_rotates_from_a_middle_index(self):
        garden = ["a", "b", "c", "d"]
        self.assertEqual(overnight.repos_to_attempt(garden, 2), ["c", "d", "a", "b"])

    def test_out_of_range_index_wraps_via_modulo(self):
        garden = ["a", "b", "c"]
        self.assertEqual(overnight.repos_to_attempt(garden, 7), overnight.repos_to_attempt(garden, 1))

    def test_every_repo_appears_exactly_once(self):
        garden = ["a", "b", "c", "d", "e"]
        order = overnight.repos_to_attempt(garden, 3)
        self.assertEqual(sorted(order), sorted(garden))
        self.assertEqual(len(order), len(garden))

    def test_single_repo_garden(self):
        self.assertEqual(overnight.repos_to_attempt(["only"], 0), ["only"])


class TestBatchRepos(unittest.TestCase):
    def test_empty_order_returns_empty(self):
        self.assertEqual(overnight.batch_repos([], 3), [])

    def test_concurrency_one_yields_one_repo_per_batch(self):
        # Today's exact sequential behavior — the default.
        self.assertEqual(
            overnight.batch_repos(["a", "b", "c"], 1),
            [["a"], ["b"], ["c"]],
        )

    def test_zero_or_negative_concurrency_is_treated_as_one(self):
        self.assertEqual(overnight.batch_repos(["a", "b"], 0), [["a"], ["b"]])
        self.assertEqual(overnight.batch_repos(["a", "b"], -5), [["a"], ["b"]])

    def test_splits_into_consecutive_batches_preserving_order(self):
        self.assertEqual(
            overnight.batch_repos(["a", "b", "c", "d", "e"], 2),
            [["a", "b"], ["c", "d"], ["e"]],
        )

    def test_concurrency_covering_the_whole_list_yields_one_batch(self):
        self.assertEqual(overnight.batch_repos(["a", "b", "c"], 10), [["a", "b", "c"]])

    def test_every_repo_appears_exactly_once_across_all_batches(self):
        garden = ["a", "b", "c", "d", "e", "f", "g"]
        batches = overnight.batch_repos(garden, 3)
        flattened = [repo for batch in batches for repo in batch]
        self.assertEqual(flattened, garden)


class TestHasTimeForAnotherRepo(unittest.TestCase):
    def test_first_repo_always_attempted_when_budget_positive(self):
        # Even a budget far smaller than one tend call's own worst-case
        # timeout still lets the very first repo of a run go — otherwise a
        # short verification --hours would dispatch nothing at all.
        self.assertTrue(overnight.has_time_for_another_repo(0, 60, 2700, attempted_so_far=0))

    def test_zero_budget_never_attempts_even_the_first_repo(self):
        self.assertFalse(overnight.has_time_for_another_repo(0, 0, 2700, attempted_so_far=0))

    def test_negative_budget_never_attempts(self):
        self.assertFalse(overnight.has_time_for_another_repo(0, -1, 2700, attempted_so_far=0))

    def test_second_repo_requires_full_headroom(self):
        # 100s elapsed, 2700s budget, 2700s per-repo timeout: 100+2700 > 2700
        self.assertFalse(overnight.has_time_for_another_repo(100, 2700, 2700, attempted_so_far=1))

    def test_second_repo_proceeds_when_headroom_available(self):
        # Fast first dispatch (100s) leaves plenty of room in an 8h budget.
        self.assertTrue(overnight.has_time_for_another_repo(100, 8 * 3600, 2700, attempted_so_far=1))

    def test_exactly_enough_headroom_is_allowed(self):
        self.assertTrue(overnight.has_time_for_another_repo(0, 2700, 2700, attempted_so_far=1))

    def test_one_second_short_of_headroom_is_refused(self):
        self.assertFalse(overnight.has_time_for_another_repo(1, 2700, 2700, attempted_so_far=1))


class TestClassifyOutcome(unittest.TestCase):
    def _run(self, **overrides):
        defaults = dict(repo="owner/name", mode="tend", outcome="tend", timestamp=state.now_iso())
        defaults.update(overrides)
        return state.Run(**defaults)

    def test_no_run_recorded_is_errored(self):
        outcome = overnight.classify_outcome("owner/name", None, "")
        self.assertTrue(outcome.errored)

    def test_error_outcome_is_errored(self):
        run = self._run(outcome="error", gap_summary="boom")
        outcome = overnight.classify_outcome("owner/name", run, "")
        self.assertTrue(outcome.errored)
        self.assertEqual(outcome.gap_summary, "boom")

    def test_pr_opened_detected_from_stdout(self):
        run = self._run(gap_summary="1 issue closed, PR #7 opened, added tests")
        outcome = overnight.classify_outcome(
            "owner/name", run, "...\nGARDENER_SUMMARY: 1 issue closed, PR #7 opened, added tests\n"
        )
        self.assertFalse(outcome.errored)
        self.assertTrue(outcome.pr_opened)
        self.assertFalse(outcome.pr_merged)
        self.assertFalse(outcome.decision_needed)

    def test_pr_merged_detected_from_stdout(self):
        run = self._run(gap_summary="0 issues, PR #9 merged, cleanup")
        outcome = overnight.classify_outcome(
            "owner/name", run, "GARDENER_SUMMARY: 0 issues, PR #9 merged, cleanup\n"
        )
        self.assertTrue(outcome.pr_merged)

    def test_decision_needed_detected_from_stdout(self):
        run = self._run(gap_summary="PR #3 opened")
        text = "GARDENER_SUMMARY: PR #3 opened\nDECISION NEEDED: human must review and merge PR #3"
        outcome = overnight.classify_outcome("owner/name", run, text)
        self.assertTrue(outcome.decision_needed)

    def test_no_pr_and_no_decision_needed(self):
        run = self._run(gap_summary="0 issues found, no PR opened, repo already aligned")
        outcome = overnight.classify_outcome("owner/name", run, "GARDENER_SUMMARY: 0 issues found\n")
        self.assertFalse(outcome.pr_opened)
        self.assertFalse(outcome.pr_merged)
        self.assertFalse(outcome.decision_needed)


class TestBuildBatchSummary(unittest.TestCase):
    def test_no_repos_attempted(self):
        summary = overnight.build_batch_summary([], 0, skipped=0)
        self.assertEqual(summary.level, Level.INFO)
        self.assertIn("no repos were dispatched", summary.message)

    def test_all_ok_is_success_level(self):
        outcomes = [
            overnight.RepoOutcome(repo="a/one", pr_opened=True),
            overnight.RepoOutcome(repo="a/two"),
        ]
        summary = overnight.build_batch_summary(outcomes, 120, skipped=0)
        self.assertEqual(summary.level, Level.SUCCESS)
        self.assertIn("2 repo(s) attempted", summary.message)
        self.assertIn("1 PR(s) opened", summary.message)

    def test_any_error_is_warning_unless_all_errored(self):
        outcomes = [
            overnight.RepoOutcome(repo="a/one", errored=True),
            overnight.RepoOutcome(repo="a/two", pr_opened=True),
        ]
        summary = overnight.build_batch_summary(outcomes, 120, skipped=0)
        self.assertEqual(summary.level, Level.WARNING)

    def test_all_errored_is_error_level(self):
        outcomes = [overnight.RepoOutcome(repo="a/one", errored=True)]
        summary = overnight.build_batch_summary(outcomes, 60, skipped=0)
        self.assertEqual(summary.level, Level.ERROR)

    def test_decision_needed_is_warning(self):
        outcomes = [overnight.RepoOutcome(repo="a/one", decision_needed=True, pr_opened=True)]
        summary = overnight.build_batch_summary(outcomes, 60, skipped=0)
        self.assertEqual(summary.level, Level.WARNING)

    def test_skipped_count_mentioned_when_nonzero(self):
        outcomes = [overnight.RepoOutcome(repo="a/one")]
        summary = overnight.build_batch_summary(outcomes, 60, skipped=2)
        self.assertIn("2 not reached this run", summary.message)

    def test_skipped_count_omitted_when_zero(self):
        outcomes = [overnight.RepoOutcome(repo="a/one")]
        summary = overnight.build_batch_summary(outcomes, 60, skipped=0)
        self.assertNotIn("not reached", summary.message)

    def test_title_counts_attempted_and_errored(self):
        outcomes = [
            overnight.RepoOutcome(repo="a/one", errored=True),
            overnight.RepoOutcome(repo="a/two"),
        ]
        summary = overnight.build_batch_summary(outcomes, 60, skipped=0)
        self.assertIn("2 repo(s) tended", summary.title)
        self.assertIn("1 error(s)", summary.title)


if __name__ == "__main__":
    unittest.main()
