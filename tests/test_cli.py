"""Argument parsing, mutual-exclusivity of --implement/--file-issue, and
prompt templating — the deterministic parts of cli.py that don't require
actually invoking `claude` or `gh`."""
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from gardener import merge_allowlist, state
from gardener.cli import (
    REPO_RE,
    _notify_run,
    build_parser,
    build_prompt,
    extract_gap_summary,
    merge_eligible,
)
from gardener.dispatch import TEND_DEFAULT_TIMEOUT_SECONDS, Mode
from gardener.notify import Level


class TestArgParsing(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_align_requires_repo(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["align"])

    def test_align_defaults_to_report_only(self):
        args = self.parser.parse_args(["align", "--repo", "owner/name"])
        self.assertFalse(args.implement)
        self.assertFalse(args.file_issue)

    def test_align_implement_flag(self):
        args = self.parser.parse_args(["align", "--repo", "owner/name", "--implement"])
        self.assertTrue(args.implement)
        self.assertFalse(args.file_issue)

    def test_align_file_issue_flag(self):
        args = self.parser.parse_args(["align", "--repo", "owner/name", "--file-issue"])
        self.assertTrue(args.file_issue)
        self.assertFalse(args.implement)

    def test_implement_and_file_issue_are_mutually_exclusive(self):
        # argparse's mutually_exclusive_group rejects this combination
        # itself, at parse time, with a SystemExit(2) — belt and suspenders
        # alongside cmd_align's own explicit check.
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self.parser.parse_args(
                    ["align", "--repo", "owner/name", "--implement", "--file-issue"]
                )
        self.assertEqual(ctx.exception.code, 2)

    def test_status_defaults(self):
        args = self.parser.parse_args(["status"])
        self.assertIsNone(args.repo)
        self.assertEqual(args.limit, 20)

    def test_status_repo_filter(self):
        args = self.parser.parse_args(["status", "--repo", "owner/name"])
        self.assertEqual(args.repo, "owner/name")

    def test_no_subcommand_errors(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args([])


class TestTendArgParsing(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_tend_requires_repo(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["tend"])

    def test_tend_defaults(self):
        args = self.parser.parse_args(["tend", "--repo", "owner/name"])
        self.assertFalse(args.allow_merge)
        self.assertEqual(args.timeout, TEND_DEFAULT_TIMEOUT_SECONDS)
        self.assertFalse(args.no_refresh_target)

    def test_tend_allow_merge_flag(self):
        args = self.parser.parse_args(["tend", "--repo", "owner/name", "--allow-merge"])
        self.assertTrue(args.allow_merge)

    def test_tend_timeout_override(self):
        args = self.parser.parse_args(["tend", "--repo", "owner/name", "--timeout", "120"])
        self.assertEqual(args.timeout, 120)


class TestAllowlistArgParsing(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_allowlist_requires_an_action(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["allowlist"])

    def test_allowlist_list(self):
        args = self.parser.parse_args(["allowlist", "list"])
        self.assertEqual(args.allowlist_action, "list")

    def test_allowlist_add_requires_repo(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["allowlist", "add"])

    def test_allowlist_add(self):
        args = self.parser.parse_args(["allowlist", "add", "--repo", "owner/name"])
        self.assertEqual(args.allowlist_action, "add")
        self.assertEqual(args.repo, "owner/name")

    def test_allowlist_remove(self):
        args = self.parser.parse_args(["allowlist", "remove", "--repo", "owner/name"])
        self.assertEqual(args.allowlist_action, "remove")
        self.assertEqual(args.repo, "owner/name")


class TestMergeEligible(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "merge_allowlist.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_false_when_flag_not_passed_even_if_allowlisted(self):
        merge_allowlist.add("owner/name", path=self.path)
        self.assertFalse(merge_eligible("owner/name", False, allowlist_path=self.path))

    def test_false_when_flag_passed_but_not_allowlisted(self):
        self.assertFalse(merge_eligible("owner/name", True, allowlist_path=self.path))

    def test_true_only_when_both_hold(self):
        merge_allowlist.add("owner/name", path=self.path)
        self.assertTrue(merge_eligible("owner/name", True, allowlist_path=self.path))


class TestRepoRegex(unittest.TestCase):
    def test_accepts_valid_owner_repo(self):
        for good in ["dmccoystephenson/gardener", "Org-Name/repo.name_2", "a/b"]:
            self.assertTrue(REPO_RE.match(good), good)

    def test_rejects_missing_slash(self):
        self.assertIsNone(REPO_RE.match("just-a-name"))

    def test_rejects_flag_injection_attempts(self):
        for bad in ["--upload-pack=x/y", "/leading-slash", "owner/", "owner/--evil"]:
            self.assertIsNone(REPO_RE.match(bad), bad)


class TestPromptTemplating(unittest.TestCase):
    def test_build_prompt_substitutes_all_placeholders(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target"
            conv = Path(td) / "conv"
            target.mkdir()
            conv.mkdir()
            prompt = build_prompt(
                Mode.REPORT, "owner/name", target, conv, "main"
            )
        self.assertNotIn("$repo", prompt)
        self.assertNotIn("$target_cwd", prompt)
        self.assertNotIn("$conventions_dir", prompt)
        self.assertNotIn("$mode_instructions", prompt)
        self.assertIn("owner/name", prompt)
        self.assertIn("GARDENER_SUMMARY", prompt)

    def test_report_mode_prompt_says_no_write_tools(self):
        with tempfile.TemporaryDirectory() as td:
            prompt = build_prompt(Mode.REPORT, "owner/name", Path(td), Path(td), "main")
        self.assertIn("do NOT have file-write or shell", prompt)

    def test_implement_mode_prompt_mentions_pr(self):
        with tempfile.TemporaryDirectory() as td:
            prompt = build_prompt(Mode.IMPLEMENT, "owner/name", Path(td), Path(td), "main")
        self.assertIn("gh pr create", prompt)

    def test_file_issue_mode_prompt_forbids_multiple_issues(self):
        with tempfile.TemporaryDirectory() as td:
            prompt = build_prompt(Mode.FILE_ISSUE, "owner/name", Path(td), Path(td), "main")
        self.assertIn("Do not open more than one issue", prompt)


class TestExtractGapSummary(unittest.TestCase):
    def test_extracts_summary_line(self):
        text = "## Gaps\n...\n\nGARDENER_SUMMARY: 4 gaps found across 3 sections — no CI workflow"
        self.assertEqual(
            extract_gap_summary(text), "4 gaps found across 3 sections — no CI workflow"
        )

    def test_falls_back_to_truncated_text_when_no_marker(self):
        text = "x" * 300
        summary = extract_gap_summary(text)
        self.assertTrue(summary.endswith("…"))
        self.assertEqual(len(summary), 201)

    def test_short_text_without_marker_is_returned_whole(self):
        self.assertEqual(extract_gap_summary("short answer"), "short answer")


class TestNotifyRun(unittest.TestCase):
    """_notify_run's job is purely translating a recorded state.Run into a
    (title, message, level) tuple and handing it to a Notifier — this is
    the "business logic" the notify.py module docstring says belongs in
    cli.py, not there. These tests never touch the network: the notifier
    itself is mocked out entirely."""

    def _run(self, **overrides):
        defaults = dict(
            repo="owner/name",
            mode="report",
            outcome="report",
            timestamp=state.now_iso(),
            gap_summary="3 gaps found",
        )
        defaults.update(overrides)
        return state.Run(**defaults)

    @patch("gardener.cli.notify.default_notifier")
    def test_error_outcome_is_always_error_level_regardless_of_mode(self, mock_default_notifier):
        mock_notifier = mock_default_notifier.return_value
        for mode in ("report", "implement", "file-issue", "tend"):
            with self.subTest(mode=mode):
                mock_notifier.reset_mock()
                run = self._run(mode=mode, outcome="error", gap_summary="boom")
                _notify_run(run)
                mock_notifier.notify.assert_called_once()
                _title, _message, level = mock_notifier.notify.call_args[0]
                self.assertEqual(level, Level.ERROR)

    @patch("gardener.cli.notify.default_notifier")
    def test_report_mode_success_is_info_level(self, mock_default_notifier):
        mock_notifier = mock_default_notifier.return_value
        run = self._run(mode="report", outcome="report")
        _notify_run(run)
        _title, message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.INFO)
        self.assertEqual(message, "3 gaps found")

    @patch("gardener.cli.notify.default_notifier")
    def test_implement_success_is_flagged_distinctly_from_report(self, mock_default_notifier):
        mock_notifier = mock_default_notifier.return_value
        run = self._run(mode="implement", outcome="implement")
        _notify_run(run)
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.WARNING)

    @patch("gardener.cli.notify.default_notifier")
    def test_file_issue_success_is_flagged_distinctly_from_report(self, mock_default_notifier):
        mock_notifier = mock_default_notifier.return_value
        run = self._run(mode="file-issue", outcome="file-issue")
        _notify_run(run)
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.WARNING)

    @patch("gardener.cli.notify.default_notifier")
    def test_unknown_future_mode_success_is_treated_as_a_mutation(self, mock_default_notifier):
        # Simulates a future subcommand (e.g. `tend`) whose mode value
        # _notify_run has never seen — it must still be flagged as a
        # mutation rather than silently defaulting to INFO.
        mock_notifier = mock_default_notifier.return_value
        run = self._run(mode="tend", outcome="tend")
        _notify_run(run)
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.WARNING)

    @patch("gardener.cli.notify.default_notifier")
    def test_notifier_raising_does_not_propagate(self, mock_default_notifier):
        mock_default_notifier.return_value.notify.side_effect = RuntimeError("network is down")
        run = self._run()
        with redirect_stderr(io.StringIO()):
            _notify_run(run)  # must not raise


if __name__ == "__main__":
    unittest.main()
