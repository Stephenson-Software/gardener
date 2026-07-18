"""Argument parsing, mutual-exclusivity of --implement/--file-issue, and
prompt templating — the deterministic parts of cli.py that don't require
actually invoking `claude` or `gh`."""
import argparse
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from gardener import dev_loop, garden, merge_allowlist, overnight, state
from gardener.cli import (
    REPO_RE,
    TendResult,
    _notify_run,
    build_parser,
    build_prompt,
    cmd_garden,
    cmd_overnight,
    cmd_tend,
    extract_gap_summary,
    find_orphaned_pr,
    merge_eligible,
)
from gardener.dispatch import TEND_DEFAULT_TIMEOUT_SECONDS, DispatchResult, Mode
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


class TestGardenArgParsing(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_garden_requires_an_action(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["garden"])

    def test_garden_list(self):
        args = self.parser.parse_args(["garden", "list"])
        self.assertEqual(args.garden_action, "list")

    def test_garden_add_requires_repo(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["garden", "add"])

    def test_garden_add(self):
        args = self.parser.parse_args(["garden", "add", "--repo", "owner/name"])
        self.assertEqual(args.garden_action, "add")
        self.assertEqual(args.repo, "owner/name")

    def test_garden_remove(self):
        args = self.parser.parse_args(["garden", "remove", "--repo", "owner/name"])
        self.assertEqual(args.garden_action, "remove")
        self.assertEqual(args.repo, "owner/name")


class TestOvernightArgParsing(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_overnight_defaults(self):
        args = self.parser.parse_args(["overnight"])
        self.assertEqual(args.hours, overnight.DEFAULT_OVERNIGHT_HOURS)
        self.assertIsNone(args.model)

    def test_overnight_hours_override(self):
        args = self.parser.parse_args(["overnight", "--hours", "0.5"])
        self.assertEqual(args.hours, 0.5)

    def test_overnight_requires_no_repo(self):
        # unlike align/tend, overnight takes its target list from the
        # garden, not a --repo flag
        args = self.parser.parse_args(["overnight"])
        self.assertFalse(hasattr(args, "repo"))


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


class TestCmdGarden(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "garden.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_list_when_empty(self):
        args = argparse.Namespace(garden_action="list", garden_file=self.path)
        with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()) as out:
            cmd_garden(args)
        self.assertIn("garden is empty", out.getvalue())

    def test_add_then_list(self):
        add_args = argparse.Namespace(garden_action="add", repo="owner/name", garden_file=self.path)
        with patch("sys.stdout", new=io.StringIO()):
            cmd_garden(add_args)
        self.assertEqual(garden.list_garden(path=self.path), ["owner/name"])

    def test_remove(self):
        garden.add("owner/name", path=self.path)
        remove_args = argparse.Namespace(garden_action="remove", repo="owner/name", garden_file=self.path)
        with patch("sys.stdout", new=io.StringIO()):
            cmd_garden(remove_args)
        self.assertEqual(garden.list_garden(path=self.path), [])


class TestCmdTendNotifications(unittest.TestCase):
    """cmd_tend previously recorded outcomes via state.record_run but never
    called _notify_run — a real gap versus cmd_align's existing pattern (and
    versus notify.py's own module docstring, which already anticipated "a
    future mode (e.g. a tend subcommand)" using this exact machinery).
    Closed as part of adding `gardener overnight`, since overnight's
    per-repo Discord alerts depend on tend firing them itself. These tests
    cover that fix directly, with clone/dispatch fully mocked."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_db = Path(self._tmpdir.name) / "state.sqlite3"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _args(self, repo="owner/name"):
        return argparse.Namespace(
            repo=repo, allow_merge=False, model=None,
            timeout=TEND_DEFAULT_TIMEOUT_SECONDS, no_refresh_target=False,
            state_db=self.state_db,
        )

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", return_value=True)
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_successful_tend_fires_a_notification(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, mock_default_notifier
    ):
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: 0 issues, no PR opened, repo already aligned",
            raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
            session_id="s1", permission_denials=[], is_error=False,
        )
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()):
            exit_code = cmd_tend(self._args())

        self.assertEqual(exit_code, 0)
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        # tend is a mutation-capable mode (like implement/file-issue), not report
        self.assertEqual(level, Level.WARNING)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", return_value=True)
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_dispatch_tend_prints_the_done_summary_even_when_called_directly(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, mock_default_notifier
    ):
        """Regression test: `cmd_overnight` calls `_dispatch_tend` directly
        (not `cmd_tend`) to avoid the redirect_stdout thread-safety hazard
        (see TendResult's docstring / issue #15). The "done in Xms" summary
        line used to live only in cmd_tend's wrapper, which meant it never
        appeared in `gardener overnight`'s log for any repo — confirmed
        missing for real during --concurrency 3 testing (2026-07-18), even
        though the run had genuinely completed and been recorded correctly.
        `_dispatch_tend` must print this itself so both callers get it."""
        from gardener.cli import _dispatch_tend

        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: 0 issues, no PR opened, repo already aligned",
            raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
            session_id="s1", permission_denials=[], is_error=False,
        )

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = _dispatch_tend(self._args())

        self.assertTrue(result.dispatched)
        self.assertIn("gardener: done in 100ms", stderr.getvalue())

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", return_value=True)
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr")
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_orphaned_pr_is_threaded_into_the_tend_prompt(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, mock_default_notifier
    ):
        """When find_orphaned_pr locates a previous interrupted tend's PR,
        cmd_tend must pass it through to build_tend_prompt so the dispatched
        session is told to continue that PR instead of starting fresh — not
        just log it and drop it on the floor."""
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_orphan.return_value = dev_loop.OrphanedPR(number=238, head_branch="feature/x")
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: 0 issues, PR #238 opened, continued prior work",
            raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
            session_id="s1", permission_denials=[], is_error=False,
        )

        with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()):
            exit_code = cmd_tend(self._args())

        self.assertEqual(exit_code, 0)
        mock_run_claude.assert_called_once()
        _args, kwargs = mock_run_claude.call_args
        self.assertIn("#238", kwargs["prompt"])
        self.assertIn("feature/x", kwargs["prompt"])
        self.assertIn("gardener found an existing OPEN pull request", kwargs["prompt"])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", return_value=True)
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_failed_dispatch_fires_an_error_level_notification(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, mock_default_notifier
    ):
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=False, result_text="", raw_stdout="", stderr="boom", exit_code=1, duration_ms=50,
            cost_usd=None, session_id=None, permission_denials=[], is_error=True,
        )
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()):
            exit_code = cmd_tend(self._args())

        self.assertEqual(exit_code, 1)
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.ERROR)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.clone_or_refresh_target_repo", side_effect=RuntimeError("clone failed"))
    def test_setup_error_fires_an_error_level_notification(self, mock_clone, mock_default_notifier):
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()):
            exit_code = cmd_tend(self._args())

        self.assertEqual(exit_code, 1)
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.ERROR)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.clone_or_refresh_target_repo", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30))
    def test_timeout_expired_during_setup_is_recorded_not_a_raw_crash(self, mock_clone, mock_default_notifier):
        # A hung git/gh call (subprocess.TimeoutExpired) used to propagate
        # unhandled out of clone_or_refresh_target_repo, crashing the whole
        # process rather than being recorded/alerted like every other setup
        # failure — see gardener/CLAUDE.md's testing conventions.
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()):
            exit_code = cmd_tend(self._args())

        self.assertEqual(exit_code, 1)
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.ERROR)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.clone_or_refresh_target_repo", side_effect=OSError("git not found"))
    def test_os_error_during_setup_is_recorded_not_a_raw_crash(self, mock_clone, mock_default_notifier):
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()):
            exit_code = cmd_tend(self._args())

        self.assertEqual(exit_code, 1)
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.ERROR)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", return_value=False)
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_create_dev_loop_bootstrap_failure_fires_a_notification(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, mock_default_notifier
    ):
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=False, result_text="", raw_stdout="", stderr="skill generation failed", exit_code=1,
            duration_ms=10, cost_usd=None, session_id=None, permission_denials=[], is_error=True,
        )
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()):
            exit_code = cmd_tend(self._args())

        self.assertEqual(exit_code, 1)
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.ERROR)
        # the tend dispatch itself must never have been attempted
        mock_run_claude.assert_called_once()

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", return_value=False)
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_create_dev_loop_dispatch_gets_local_skills_and_commands_add_dirs(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, mock_default_notifier
    ):
        """Regression test for the bug where create-dev-loop's dispatch had no
        add_dirs at all — unlike align's add_dirs=[conv.path] a bit earlier in
        cli.py — leaving Read/Bash(mkdir *)/Bash(ln *)/etc. sandboxed out of
        ~/local-skills/ and ~/.claude/commands/ even though this mode's entire
        job is reading/writing exactly those two directories. A stale partial
        artifact from an earlier failed attempt then had no recovery path on
        retry: the dispatched session couldn't even Read what was in its way.
        See dev_loop.py's LOCAL_SKILLS_DIR/COMMANDS_DIR and dispatch.py's
        module docstring finding #3 for the full mechanism."""
        mock_clone.return_value = Path(self._tmpdir.name)
        # has_dev_loop_skill is False both before and after the dispatch in
        # this test (the mock never flips to True), so cmd_tend reports the
        # bootstrap as failed and returns before ever reaching the real tend
        # dispatch — irrelevant to what's under test here, which is only the
        # add_dirs actually passed to the create-dev-loop run_claude() call.
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: created dev-loop skill",
            raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
            session_id="s1", permission_denials=[], is_error=False,
        )

        with redirect_stderr(io.StringIO()):
            cmd_tend(self._args())

        mock_run_claude.assert_called_once()
        _args, kwargs = mock_run_claude.call_args
        self.assertEqual(kwargs["mode"], Mode.CREATE_DEV_LOOP)
        self.assertEqual(
            kwargs["add_dirs"],
            [dev_loop.LOCAL_SKILLS_DIR, dev_loop.COMMANDS_DIR],
        )


class TestFindOrphanedPR(unittest.TestCase):
    """find_orphaned_pr's job is: find an open PR whose body carries
    dev_loop.ORPHAN_MARKER (see that module's docstring on why), never raise
    (a `gh` hiccup here must not sink an otherwise-normal `tend` dispatch),
    and pick deterministically among more than one match. `gardener.cli._run`
    is mocked throughout — this must never invoke a real `gh` process, same
    testing convention as clone_or_refresh_target_repo's own tests."""

    def _completed(self, stdout="", returncode=0, stderr=""):
        return subprocess.CompletedProcess(
            args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run")
    def test_no_open_prs_returns_none(self, mock_run, mock_which):
        mock_run.return_value = self._completed(stdout="[]")
        self.assertIsNone(find_orphaned_pr("owner/name"))

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run")
    def test_open_prs_without_the_marker_are_ignored(self, mock_run, mock_which):
        mock_run.return_value = self._completed(stdout=json.dumps([
            {"number": 1, "headRefName": "feature/human-work", "body": "just a normal PR",
             "createdAt": "2026-07-18T10:00:00Z"},
        ]))
        self.assertIsNone(find_orphaned_pr("owner/name"))

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run")
    def test_marked_pr_is_found(self, mock_run, mock_which):
        mock_run.return_value = self._completed(stdout=json.dumps([
            {"number": 238, "headRefName": "feature/plugin-icons-and-versions",
             "body": f"some body\n\n{dev_loop.ORPHAN_MARKER}\n", "createdAt": "2026-07-18T10:00:00Z"},
        ]))
        orphan = find_orphaned_pr("owner/name")
        self.assertEqual(orphan, dev_loop.OrphanedPR(number=238, head_branch="feature/plugin-icons-and-versions"))

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run")
    def test_most_recently_created_marked_pr_wins_when_more_than_one(self, mock_run, mock_which):
        mock_run.return_value = self._completed(stdout=json.dumps([
            {"number": 100, "headRefName": "feature/older", "body": dev_loop.ORPHAN_MARKER,
             "createdAt": "2026-07-17T10:00:00Z"},
            {"number": 200, "headRefName": "feature/newer", "body": dev_loop.ORPHAN_MARKER,
             "createdAt": "2026-07-18T10:00:00Z"},
        ]))
        orphan = find_orphaned_pr("owner/name")
        self.assertEqual(orphan.number, 200)

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run")
    def test_gh_failure_is_treated_as_no_orphan_not_raised(self, mock_run, mock_which):
        mock_run.return_value = self._completed(returncode=1, stderr="not authenticated")
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(find_orphaned_pr("owner/name"))

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30))
    def test_gh_timeout_is_treated_as_no_orphan_not_raised(self, mock_run, mock_which):
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(find_orphaned_pr("owner/name"))

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run")
    def test_malformed_json_is_treated_as_no_orphan_not_raised(self, mock_run, mock_which):
        mock_run.return_value = self._completed(stdout="not json")
        self.assertIsNone(find_orphaned_pr("owner/name"))

    @patch("gardener.cli.shutil.which", return_value=None)
    def test_missing_gh_binary_returns_none_without_calling_run(self, mock_which):
        with patch("gardener.cli._run") as mock_run:
            self.assertIsNone(find_orphaned_pr("owner/name"))
            mock_run.assert_not_called()


class TestCmdOvernight(unittest.TestCase):
    """cmd_overnight's real-time, real-dispatch orchestration —
    `_dispatch_tend` is mocked (never invokes `claude`/`git`/`gh`), and
    `time.monotonic` is mocked where the budget/headroom behavior itself is
    under test. Patches `gardener.cli._dispatch_tend` rather than `cmd_tend`
    directly since `cmd_overnight` calls the former (via
    `_dispatch_one_for_overnight`) — see `TendResult`'s docstring for why
    `cmd_tend`'s printed stdout is no longer how this data flows."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self.garden_file = tmp / "garden.json"
        self.cursor_file = tmp / "cursor.json"
        self.state_db = tmp / "state.sqlite3"
        self.calls: list[str] = []

    def tearDown(self):
        self._tmpdir.cleanup()

    def _args(self, hours=8.0, model=None, concurrency=1):
        return argparse.Namespace(
            hours=hours, model=model, garden_file=self.garden_file,
            cursor_file=self.cursor_file, state_db=self.state_db,
            concurrency=concurrency,
        )

    def _fake_dispatch_tend(self, outcomes: dict):
        def fake(args):
            self.calls.append(args.repo)
            spec = outcomes.get(args.repo, {})
            run = state.Run(
                repo=args.repo, mode="tend",
                outcome="error" if spec.get("error") else "tend",
                timestamp=state.now_iso(),
                gap_summary=spec.get("gap_summary", ""),
            )
            state.record_run(run, db_path=args.state_db)
            errored = bool(spec.get("error"))
            return TendResult(
                exit_code=1 if errored else 0, ok=not errored,
                result_text=spec.get("stdout", ""), run=run,
            )
        return fake

    def test_empty_garden_is_a_clean_no_op(self):
        with redirect_stderr(io.StringIO()):
            exit_code = cmd_overnight(self._args())
        self.assertEqual(exit_code, 0)
        self.assertEqual(self.calls, [])

    @patch("gardener.cli.notify.default_notifier")
    def test_corrupted_garden_file_is_reported_not_a_raw_crash(self, mock_default_notifier):
        # A torn/corrupted garden.json (e.g. this device killing a prior
        # process mid-write) used to propagate a raw ValueError out of
        # garden.list_garden and crash the whole overnight batch before any
        # per-repo dispatch could even begin.
        self.garden_file.write_text("not json{{{")
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()):
            exit_code = cmd_overnight(self._args())

        self.assertEqual(exit_code, 1)
        self.assertEqual(self.calls, [])
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.ERROR)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_passes_allow_merge_unconditionally_to_every_dispatch(self, mock_dispatch_tend, mock_default_notifier):
        garden.add("owner/a", path=self.garden_file)
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({"owner/a": {}})
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args(hours=8.0))
        self.assertEqual(len(mock_dispatch_tend.call_args_list), 1)
        dispatched_args = mock_dispatch_tend.call_args_list[0].args[0]
        self.assertTrue(dispatched_args.allow_merge)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_dispatches_every_repo_when_budget_allows(self, mock_dispatch_tend, mock_default_notifier):
        garden.add("owner/a", path=self.garden_file)
        garden.add("owner/b", path=self.garden_file)
        garden.add("owner/c", path=self.garden_file)
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({
            "owner/a": {"stdout": "GARDENER_SUMMARY: no PR opened"},
            "owner/b": {"stdout": "GARDENER_SUMMARY: PR #1 opened"},
            "owner/c": {"stdout": "GARDENER_SUMMARY: PR #2 merged"},
        })
        with redirect_stderr(io.StringIO()):
            exit_code = cmd_overnight(self._args(hours=8.0))
        self.assertEqual(exit_code, 0)
        self.assertEqual(self.calls, ["owner/a", "owner/b", "owner/c"])
        # a full cycle was consumed -> cursor wraps back to the start
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 0)
        mock_default_notifier.return_value.notify.assert_called_once()

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.time.monotonic")
    @patch("gardener.cli._dispatch_tend")
    def test_stops_dispatching_once_budget_is_exhausted(self, mock_dispatch_tend, mock_monotonic, mock_default_notifier):
        garden.add("owner/a", path=self.garden_file)
        garden.add("owner/b", path=self.garden_file)
        garden.add("owner/c", path=self.garden_file)
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({r: {} for r in ("owner/a", "owner/b", "owner/c")})
        # start_time=0; iteration 1 elapsed=0 (first repo always attempted);
        # iteration 2 elapsed=50s against a 72s budget with a 2700s
        # per-repo reservation -> nowhere close to enough headroom, stop.
        mock_monotonic.side_effect = [0.0, 0.0, 50.0, 50.0]
        with redirect_stderr(io.StringIO()):
            exit_code = cmd_overnight(self._args(hours=0.02))  # 72s budget
        self.assertEqual(exit_code, 0)
        self.assertEqual(self.calls, ["owner/a"])
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 1)

    @patch("gardener.cli.notify.default_notifier")
    def test_resumes_from_the_cursor_across_two_invocations(self, mock_default_notifier):
        garden.add("owner/a", path=self.garden_file)
        garden.add("owner/b", path=self.garden_file)
        outcomes = {"owner/a": {}, "owner/b": {}}

        with patch("gardener.cli._dispatch_tend", side_effect=self._fake_dispatch_tend(outcomes)), \
                patch("gardener.cli.time.monotonic", side_effect=[0.0, 0.0, 50.0, 50.0]):
            with redirect_stderr(io.StringIO()):
                cmd_overnight(self._args(hours=0.02))
        self.assertEqual(self.calls, ["owner/a"])

        self.calls.clear()
        with patch("gardener.cli._dispatch_tend", side_effect=self._fake_dispatch_tend(outcomes)), \
                patch("gardener.cli.time.monotonic", side_effect=[0.0, 0.0, 50.0, 50.0]):
            with redirect_stderr(io.StringIO()):
                cmd_overnight(self._args(hours=0.02))
        # second run must pick up "owner/b", not re-tend "owner/a"
        self.assertEqual(self.calls, ["owner/b"])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_one_repo_raising_does_not_abort_the_batch(self, mock_dispatch_tend, mock_default_notifier):
        garden.add("owner/a", path=self.garden_file)
        garden.add("owner/b", path=self.garden_file)

        def flaky(args):
            self.calls.append(args.repo)
            if args.repo == "owner/a":
                raise RuntimeError("simulated crash")
            run = state.Run(repo=args.repo, mode="tend", outcome="tend", timestamp=state.now_iso())
            state.record_run(run, db_path=args.state_db)
            return TendResult(exit_code=0, ok=True, run=run)

        mock_dispatch_tend.side_effect = flaky
        with redirect_stderr(io.StringIO()):
            exit_code = cmd_overnight(self._args(hours=8.0))
        self.assertEqual(exit_code, 0)
        self.assertEqual(self.calls, ["owner/a", "owner/b"])
        # both were attempted despite the crash -> full cycle -> cursor wraps
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 0)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_summary_notification_reflects_outcomes(self, mock_dispatch_tend, mock_default_notifier):
        garden.add("owner/a", path=self.garden_file)
        garden.add("owner/b", path=self.garden_file)
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({
            "owner/a": {"error": True},
            "owner/b": {"stdout": "GARDENER_SUMMARY: PR #4 opened"},
        })
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args(hours=8.0))
        mock_notifier = mock_default_notifier.return_value
        mock_notifier.notify.assert_called_once()
        title, message, level = mock_notifier.notify.call_args[0]
        self.assertIn("2 repo(s) tended", title)
        self.assertIn("1 error(s)", title)
        self.assertEqual(level, Level.WARNING)
        self.assertIn("1 PR(s) opened", message)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_concurrency_dispatches_a_batch_at_once_and_preserves_order(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        """With --concurrency 2, a garden of 3 repos dispatches in batches of
        [a, b] then [c] — this asserts both that every repo still gets
        attempted (via a ThreadPoolExecutor for the first batch) and that
        `outcomes`/the batch summary preserve `order`, not completion order,
        even though `b` is made to "finish" before `a` inside the batch."""
        garden.add("owner/a", path=self.garden_file)
        garden.add("owner/b", path=self.garden_file)
        garden.add("owner/c", path=self.garden_file)

        import threading
        release_a = threading.Event()

        def fake(args):
            self.calls.append(args.repo)
            if args.repo == "owner/a":
                # Let owner/b's fake dispatch (below) complete first, so
                # completion order is deliberately b-then-a within the batch.
                release_a.wait(timeout=5)
            elif args.repo == "owner/b":
                release_a.set()
            run = state.Run(
                repo=args.repo, mode="tend", outcome="tend", timestamp=state.now_iso(),
                gap_summary=f"GARDENER_SUMMARY: PR opened for {args.repo}",
            )
            state.record_run(run, db_path=args.state_db)
            return TendResult(exit_code=0, ok=True, result_text=f"GARDENER_SUMMARY: PR opened for {args.repo}", run=run)

        mock_dispatch_tend.side_effect = fake
        with redirect_stderr(io.StringIO()):
            exit_code = cmd_overnight(self._args(hours=8.0, concurrency=2))
        self.assertEqual(exit_code, 0)
        self.assertEqual(sorted(self.calls), ["owner/a", "owner/b", "owner/c"])
        # cursor still advances past every repo attempted, batching aside
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 0)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_concurrency_one_never_touches_a_thread_pool(self, mock_dispatch_tend, mock_default_notifier):
        """Default concurrency=1 must take the exact pre-concurrency
        sequential path (no ThreadPoolExecutor at all) — a regression here
        would mean every existing cron invocation silently starts paying
        thread-pool overhead it never asked for."""
        garden.add("owner/a", path=self.garden_file)
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({"owner/a": {}})
        with patch("gardener.cli.ThreadPoolExecutor") as mock_pool, redirect_stderr(io.StringIO()):
            cmd_overnight(self._args(hours=8.0, concurrency=1))
        mock_pool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
