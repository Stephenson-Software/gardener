"""overnight.py's pure logic — cursor persistence, rotation order, batching
for concurrent dispatch, the budget/headroom check, and outcome
classification/summary building. None of this invokes `claude`, `git`, or
`gh`; `cmd_overnight`'s real-dispatch orchestration in cli.py is covered
separately in test_cli.py with `_dispatch_tend` mocked."""
import json
import random
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


class TestAttemptedCursor(unittest.TestCase):
    """read_attempted/write_attempted — the name-keyed resume cursor used by
    issue-count/random, instead of round-robin's bare index (see
    overnight.py's module docstring)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "cursor.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_missing_file_reads_as_empty_list(self):
        self.assertEqual(overnight.read_attempted(path=self.path), [])

    def test_write_then_read_round_trips(self):
        overnight.write_attempted(["a/one", "a/two"], path=self.path)
        self.assertEqual(overnight.read_attempted(path=self.path), ["a/one", "a/two"])

    def test_malformed_json_reads_as_empty_list(self):
        self.path.write_text("not json{{{")
        self.assertEqual(overnight.read_attempted(path=self.path), [])

    def test_non_object_json_reads_as_empty_list(self):
        self.path.write_text(json.dumps([1, 2, 3]))
        self.assertEqual(overnight.read_attempted(path=self.path), [])

    def test_non_list_of_strings_reads_as_empty_list(self):
        self.path.write_text(json.dumps({"attempted": [1, 2, 3]}))
        self.assertEqual(overnight.read_attempted(path=self.path), [])

    def test_creates_parent_directories(self):
        nested = Path(self._tmpdir.name) / "a" / "b" / "cursor.json"
        overnight.write_attempted(["a/one"], path=nested)
        self.assertTrue(nested.exists())

    def test_write_attempted_preserves_next_index_in_same_file(self):
        # round-robin's own field must survive a later issue-count/random
        # run writing to the same cursor file — each strategy only touches
        # its own key (see overnight.py's docstring).
        overnight.write_cursor(3, path=self.path)
        overnight.write_attempted(["a/one"], path=self.path)
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["next_index"], 3)
        self.assertEqual(raw["attempted"], ["a/one"])
        self.assertEqual(overnight.read_cursor(path=self.path), 3)

    def test_write_cursor_preserves_attempted_in_same_file(self):
        overnight.write_attempted(["a/one"], path=self.path)
        overnight.write_cursor(2, path=self.path)
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["attempted"], ["a/one"])
        self.assertEqual(raw["next_index"], 2)
        self.assertEqual(overnight.read_attempted(path=self.path), ["a/one"])


class TestOrderByIssueCount(unittest.TestCase):
    def test_sorts_descending_by_count(self):
        order = overnight.order_by_issue_count(
            ["a/low", "a/high", "a/mid"], {"a/low": 1, "a/high": 10, "a/mid": 5}
        )
        self.assertEqual(order, ["a/high", "a/mid", "a/low"])

    def test_missing_counts_treated_as_zero_lowest_priority(self):
        order = overnight.order_by_issue_count(
            ["a/known", "a/unknown"], {"a/known": 1}
        )
        self.assertEqual(order, ["a/known", "a/unknown"])

    def test_all_missing_falls_back_to_alphabetical(self):
        order = overnight.order_by_issue_count(["c/repo", "a/repo", "b/repo"], {})
        self.assertEqual(order, ["a/repo", "b/repo", "c/repo"])

    def test_ties_break_alphabetically(self):
        order = overnight.order_by_issue_count(
            ["b/repo", "a/repo"], {"b/repo": 5, "a/repo": 5}
        )
        self.assertEqual(order, ["a/repo", "b/repo"])

    def test_every_repo_appears_exactly_once(self):
        garden = ["a", "b", "c", "d"]
        order = overnight.order_by_issue_count(garden, {"a": 2, "c": 9})
        self.assertEqual(sorted(order), sorted(garden))

    def test_empty_garden_returns_empty(self):
        self.assertEqual(overnight.order_by_issue_count([], {}), [])


class TestRandomOrder(unittest.TestCase):
    def test_injectable_rng_gives_a_deterministic_shuffle(self):
        garden = ["a", "b", "c", "d", "e"]
        order1 = overnight.random_order(garden, rng=random.Random(42))
        order2 = overnight.random_order(garden, rng=random.Random(42))
        self.assertEqual(order1, order2)

    def test_different_seeds_can_give_different_orders(self):
        garden = ["a", "b", "c", "d", "e", "f", "g", "h"]
        order1 = overnight.random_order(garden, rng=random.Random(1))
        order2 = overnight.random_order(garden, rng=random.Random(2))
        self.assertNotEqual(order1, order2)

    def test_every_repo_appears_exactly_once(self):
        garden = ["a", "b", "c", "d", "e"]
        order = overnight.random_order(garden, rng=random.Random(7))
        self.assertEqual(sorted(order), sorted(garden))
        self.assertEqual(len(order), len(garden))

    def test_does_not_mutate_the_input_list(self):
        garden = ["a", "b", "c"]
        overnight.random_order(garden, rng=random.Random(3))
        self.assertEqual(garden, ["a", "b", "c"])

    def test_empty_garden_returns_empty(self):
        self.assertEqual(overnight.random_order([], rng=random.Random(1)), [])

    def test_omitted_rng_still_returns_a_valid_permutation(self):
        # No injected rng -> falls back to a real random.Random() instance;
        # this just confirms that path doesn't crash and still permutes.
        garden = ["a", "b", "c"]
        order = overnight.random_order(garden)
        self.assertEqual(sorted(order), garden)


class TestResumeOrder(unittest.TestCase):
    def test_nothing_attempted_yet_returns_full_order_unchanged(self):
        order, cycle_reset = overnight.resume_order(["a", "b", "c"], [])
        self.assertEqual(order, ["a", "b", "c"])
        self.assertFalse(cycle_reset)

    def test_filters_out_already_attempted_preserving_relative_order(self):
        order, cycle_reset = overnight.resume_order(["a", "b", "c", "d"], ["a", "c"])
        self.assertEqual(order, ["b", "d"])
        self.assertFalse(cycle_reset)

    def test_everything_attempted_resets_to_full_order(self):
        order, cycle_reset = overnight.resume_order(["a", "b"], ["b", "a"])
        self.assertEqual(order, ["a", "b"])
        self.assertTrue(cycle_reset)

    def test_attempted_names_no_longer_in_the_garden_are_harmless(self):
        # e.g. a repo removed from the garden between runs.
        order, cycle_reset = overnight.resume_order(["a", "b"], ["x", "y"])
        self.assertEqual(order, ["a", "b"])
        self.assertFalse(cycle_reset)

    def test_empty_full_order_reports_cycle_reset(self):
        order, cycle_reset = overnight.resume_order([], [])
        self.assertEqual(order, [])
        self.assertTrue(cycle_reset)


class TestNextAttempted(unittest.TestCase):
    def test_appends_newly_attempted_when_cycle_not_reset(self):
        result = overnight.next_attempted(["a"], False, ["b", "c"])
        self.assertEqual(result, ["a", "b", "c"])

    def test_cycle_reset_discards_prior_attempted(self):
        result = overnight.next_attempted(["a", "b"], True, ["c"])
        self.assertEqual(result, ["c"])

    def test_deduplicates_while_preserving_order(self):
        result = overnight.next_attempted(["a", "b"], False, ["b", "c"])
        self.assertEqual(result, ["a", "b", "c"])

    def test_no_new_attempts_leaves_prior_list_unchanged(self):
        result = overnight.next_attempted(["a", "b"], False, [])
        self.assertEqual(result, ["a", "b"])


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
