"""Argument parsing, mutual-exclusivity of --implement/--file-issue, and
prompt templating — the deterministic parts of cli.py that don't require
actually invoking `claude` or `gh`.

`align`'s conventions repo has no built-in default (see conventions.py), so
every test that exercises it passes `CONVENTIONS_URL` explicitly rather
than depending on `$GARDENER_CONVENTIONS_URL` being set in the environment
running the suite."""
import argparse
import fcntl
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gardener import (
    dashboard, dev_loop, garden, merge_allowlist, notify, overnight, repo_lock, selfupdate,
    sessions, state,
)
from gardener.cli import (
    CLEAN_TIMEOUT_SECONDS,
    PRESERVED_DEPENDENCY_DIRS,
    REFRESH_TIMEOUT_SECONDS,
    REPO_RE,
    TendResult,
    clone_or_refresh_target_repo,
    current_branch,
    _blocking_reason,
    _default_branch_name,
    _first_blocked_index,
    _notify_run,
    _record_and_notify,
    _safe_record_run,
    build_parser,
    build_prompt,
    cmd_align,
    cmd_allowlist,
    cmd_dashboard,
    cmd_garden,
    cmd_kill,
    cmd_ps,
    cmd_stop,
    cmd_overnight,
    cmd_status,
    cmd_tail_transcript,
    cmd_tend,
    cmd_update,
    _parse_signal,
    _session_target,
    DENIAL_MAX_CHARS,
    DENIAL_PRINT_LIMIT,
    denial_report_lines,
    extract_gap_summary,
    format_denial,
    fetch_issue_counts,
    fetch_open_issue_count,
    find_orphaned_pr,
    main,
    merge_eligible,
    repo_arg,
)
from gardener.dispatch import (
    AUTH_RETRY_BACKOFF_SECONDS,
    TEND_DEFAULT_TIMEOUT_SECONDS,
    DispatchResult,
    Mode,
    is_device_global_failure,
    looks_like_auth_failure,
)
from gardener.notify import Level

CONVENTIONS_URL = "https://example.invalid/conventions.git"

#: Set by `setUpModule` so a test that forgets to patch the notifier
#: cannot reach a real Discord webhook. See that function's docstring.
_notifier_fence = None


def setUpModule():
    """Fence the whole module off from the operator's real Discord webhook.

    Many tests here drive `cmd_overnight`/`cmd_tend`, which alert on their
    outcome via `notify.default_notifier()`. A test that patches the thing
    it's asserting on but *not* the notifier still runs the real alerting
    path, and `default_notifier` resolves a webhook from the ambient
    environment — so on a configured box the suite posts a genuine alert
    built from test fixture data. That is not hypothetical: a test that
    raised `RuntimeError("boom")` out of `self_update` posted a real
    "gardener overnight: self-update FAILED / unexpected error: boom"
    embed every time the suite ran, which on this deployment is nightly
    (gardener tends its own repo). Individual tests patching
    `default_notifier` is still the right thing to do and is what asserts
    on alerting behavior; this is the backstop that keeps the *next*
    oversight silent instead of paging someone.

    Both webhook sources have to be closed (see `notify.load_webhook_url`):
    the `GARDENER_DISCORD_WEBHOOK_URL` env var, and the `notify.env` file
    under `$GARDENER_STATE_DIR`/`~/.local/state/gardener`. Pointing the
    state dir at an empty temp dir closes the second. With neither
    configured, `default_notifier()` returns a `NullNotifier`.
    """
    global _notifier_fence
    _notifier_fence = tempfile.TemporaryDirectory()
    os.environ.pop(notify.DISCORD_WEBHOOK_ENV_VAR, None)
    os.environ["GARDENER_STATE_DIR"] = _notifier_fence.name


def tearDownModule():
    global _notifier_fence
    if _notifier_fence is not None:
        _notifier_fence.cleanup()
        _notifier_fence = None


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

    def test_strategy_defaults_to_random(self):
        args = self.parser.parse_args(["overnight"])
        self.assertEqual(args.strategy, overnight.DEFAULT_OVERNIGHT_STRATEGY.value)
        self.assertEqual(args.strategy, overnight.Strategy.RANDOM.value)

    def test_concurrency_defaults_to_two(self):
        args = self.parser.parse_args(["overnight"])
        self.assertEqual(args.concurrency, overnight.DEFAULT_OVERNIGHT_CONCURRENCY)
        self.assertEqual(args.concurrency, 2)

    def test_strategy_accepts_issue_count_and_random(self):
        for value in ("issue-count", "random"):
            args = self.parser.parse_args(["overnight", "--strategy", value])
            self.assertEqual(args.strategy, value)

    def test_strategy_rejects_unknown_value(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["overnight", "--strategy", "bogus"])

    def test_self_update_defaults_to_true(self):
        args = self.parser.parse_args(["overnight"])
        self.assertTrue(args.self_update)

    def test_no_self_update_flag_disables_it(self):
        args = self.parser.parse_args(["overnight", "--no-self-update"])
        self.assertFalse(args.self_update)


class TestUpdateArgParsing(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_update_defaults_check_to_false(self):
        args = self.parser.parse_args(["update"])
        self.assertFalse(args.check)

    def test_update_check_flag(self):
        args = self.parser.parse_args(["update", "--check"])
        self.assertTrue(args.check)


class TestTailTranscriptArgParsing(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_requires_path(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["tail-transcript"])

    def test_path_is_converted_to_a_path_object(self):
        args = self.parser.parse_args(["tail-transcript", "/tmp/session.jsonl"])
        self.assertEqual(args.path, Path("/tmp/session.jsonl"))

    def test_follow_defaults_to_false(self):
        args = self.parser.parse_args(["tail-transcript", "/tmp/session.jsonl"])
        self.assertFalse(args.follow)

    def test_follow_flag_short_and_long_form(self):
        for flag in ("-f", "--follow"):
            args = self.parser.parse_args(["tail-transcript", "/tmp/session.jsonl", flag])
            self.assertTrue(args.follow)


class TestDashboardArgParsing(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_port_defaults_to_dashboard_default_port(self):
        args = self.parser.parse_args(["dashboard"])
        self.assertEqual(args.port, dashboard.DEFAULT_PORT)

    def test_port_override(self):
        args = self.parser.parse_args(["dashboard", "--port", "9001"])
        self.assertEqual(args.port, 9001)


class TestUsageDocumentsEveryVisibleFlag(unittest.TestCase):
    """`docs/USAGE.md` is the command reference, and CLAUDE.md's
    documentation sources-of-truth table requires its flag claims to match
    what `cli.py` actually accepts. Adding a flag and forgetting the doc is
    invisible otherwise, so assert it here rather than relying on a manual
    sweep to catch it later.

    Scoped to flags argparse actually shows: several path/state overrides
    (`--state-db`, `--garden-file`, `--cursor-file`, `--allowlist-path`,
    `--state-dir`) and the `--random-seed` test hook are declared with
    `help=argparse.SUPPRESS`, i.e. deliberately hidden from `--help`.
    Documenting those in the user-facing reference would contradict that
    decision, so suppression is treated as the signal for what belongs in
    USAGE.md — the same switch, read by both.
    """

    USAGE_PATH = Path(__file__).resolve().parent.parent / "docs" / "USAGE.md"

    def _visible_flags(self):
        """Every long option across the parser tree, minus suppressed ones."""
        found = {}

        def walk(parser, command):
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for name, subparser in action.choices.items():
                        walk(subparser, f"{command} {name}".strip())
                    continue
                if action.help == argparse.SUPPRESS:
                    continue
                for opt in action.option_strings:
                    if opt.startswith("--") and opt != "--help":
                        found.setdefault(opt, command)

        walk(build_parser(), "")
        return found

    def test_every_visible_long_flag_appears_in_usage_md(self):
        usage = self.USAGE_PATH.read_text()
        undocumented = {
            flag: command for flag, command in self._visible_flags().items() if flag not in usage
        }
        self.assertEqual(
            undocumented,
            {},
            f"flags accepted by the CLI but absent from {self.USAGE_PATH.name}: {undocumented}",
        )

    def test_suppressed_flags_are_actually_suppressed(self):
        """Guards the exemption above: if one of these ever becomes visible,
        it becomes user-facing and the test above must start requiring it.
        Without this, un-suppressing a flag would silently widen the
        exemption instead of tightening the requirement."""
        visible = self._visible_flags()
        for flag in ("--state-db", "--garden-file", "--cursor-file", "--allowlist-path", "--random-seed"):
            with self.subTest(flag=flag):
                self.assertNotIn(flag, visible)


class TestLogNameWiring(unittest.TestCase):
    """main() only wraps a subcommand's run in a `run_log` when the parser
    set `log_name` for it — the dispatching commands (align/tend/overnight),
    never the read-only ones. See cli.py's `main` and CLAUDE.md's testing
    section."""

    def setUp(self):
        self.parser = build_parser()

    def test_dispatching_subcommands_carry_their_own_log_name(self):
        cases = {
            "align": ["align", "--repo", "owner/name"],
            "tend": ["tend", "--repo", "owner/name"],
            "overnight": ["overnight"],
        }
        for name, argv in cases.items():
            with self.subTest(command=name):
                args = self.parser.parse_args(argv)
                self.assertEqual(getattr(args, "log_name", None), name)

    def test_read_only_subcommands_have_no_log_name(self):
        cases = {
            "status": ["status"],
            "allowlist": ["allowlist", "list"],
            "garden": ["garden", "list"],
            "tail-transcript": ["tail-transcript", "/tmp/session.jsonl"],
            "dashboard": ["dashboard"],
        }
        for name, argv in cases.items():
            with self.subTest(command=name):
                args = self.parser.parse_args(argv)
                self.assertIsNone(getattr(args, "log_name", None))

    @patch("gardener.cli.run_log.tee_stderr")
    @patch("gardener.cli.cmd_status")
    def test_main_does_not_open_a_log_for_a_read_only_command(
        self, mock_cmd_status, mock_tee_stderr
    ):
        mock_cmd_status.return_value = 0

        result = main(["status"])

        self.assertEqual(result, 0)
        mock_cmd_status.assert_called_once()
        mock_tee_stderr.assert_not_called()

    @patch("gardener.cli.run_log.tee_stderr")
    @patch("gardener.cli.cmd_align")
    def test_main_opens_a_log_for_a_dispatching_command(
        self, mock_cmd_align, mock_tee_stderr
    ):
        mock_cmd_align.return_value = 0
        mock_tee_stderr.return_value.__enter__.return_value = None

        result = main(["align", "--repo", "owner/name"])

        self.assertEqual(result, 0)
        mock_cmd_align.assert_called_once()
        mock_tee_stderr.assert_called_once_with("align")


class TestFetchIssueCounts(unittest.TestCase):
    """fetch_open_issue_count/fetch_issue_counts's job is: get one/many
    repos' open-issue count via `gh issue list`, never raise (a `gh` hiccup
    must not sink the whole issue-count ordering), and treat a fetch failure
    as "omit this repo" rather than crashing. `gardener.cli._run` is mocked
    throughout — this must never invoke a real `gh` process, same testing
    convention as find_orphaned_pr's own tests."""

    def _completed(self, stdout="", returncode=0, stderr=""):
        return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run")
    def test_returns_the_parsed_count(self, mock_run, mock_which):
        mock_run.return_value = self._completed(stdout="7\n")
        self.assertEqual(fetch_open_issue_count("owner/name"), 7)

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run")
    def test_zero_open_issues(self, mock_run, mock_which):
        mock_run.return_value = self._completed(stdout="0\n")
        self.assertEqual(fetch_open_issue_count("owner/name"), 0)

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run")
    def test_gh_failure_returns_none_not_raised(self, mock_run, mock_which):
        mock_run.return_value = self._completed(returncode=1, stderr="not authenticated")
        self.assertIsNone(fetch_open_issue_count("owner/name"))

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30))
    def test_gh_timeout_returns_none_not_raised(self, mock_run, mock_which):
        self.assertIsNone(fetch_open_issue_count("owner/name"))

    @patch("gardener.cli.shutil.which", return_value="/usr/bin/gh")
    @patch("gardener.cli._run")
    def test_malformed_output_returns_none(self, mock_run, mock_which):
        mock_run.return_value = self._completed(stdout="not a number")
        self.assertIsNone(fetch_open_issue_count("owner/name"))

    @patch("gardener.cli.shutil.which", return_value=None)
    def test_missing_gh_binary_returns_none_without_calling_run(self, mock_which):
        with patch("gardener.cli._run") as mock_run:
            self.assertIsNone(fetch_open_issue_count("owner/name"))
            mock_run.assert_not_called()

    @patch("gardener.cli.fetch_open_issue_count")
    def test_fetch_issue_counts_queries_every_repo(self, mock_fetch):
        mock_fetch.side_effect = lambda repo, timeout=30: {"a/one": 3, "a/two": 0}.get(repo)
        counts = fetch_issue_counts(["a/one", "a/two"])
        self.assertEqual(counts, {"a/one": 3, "a/two": 0})

    @patch("gardener.cli.fetch_open_issue_count")
    def test_fetch_issue_counts_omits_repos_whose_fetch_failed(self, mock_fetch):
        mock_fetch.side_effect = lambda repo, timeout=30: None if repo == "a/broken" else 5
        counts = fetch_issue_counts(["a/ok", "a/broken"])
        self.assertEqual(counts, {"a/ok": 5})


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


class TestRepoArg(unittest.TestCase):
    """`repo_arg` is the argparse `type=` wrapper around REPO_RE; it must
    raise ArgumentTypeError (which argparse turns into a usage error)
    rather than ValueError, and must return the value unchanged."""

    def test_returns_a_valid_value_unchanged(self):
        self.assertEqual(repo_arg("dmccoystephenson/gardener"), "dmccoystephenson/gardener")

    def test_raises_argument_type_error_for_a_malformed_value(self):
        for bad in ["just-a-name", "owner/", "--upload-pack=x/y", ""]:
            with self.assertRaises(argparse.ArgumentTypeError, msg=bad):
                repo_arg(bad)


class TestRepoValidationAtParseTime(unittest.TestCase):
    """A typo'd --repo must be rejected as a usage error (exit 2) before
    any lock/network/dispatch work — and before it can be written into the
    garden or merge allow-list, where it would otherwise sit until an
    unattended run reached it (or, for the allow-list, never match at
    all)."""

    def setUp(self):
        self.parser = build_parser()

    def _assert_usage_error(self, argv):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                self.parser.parse_args(argv)
        self.assertEqual(ctx.exception.code, 2, argv)

    def test_align_rejects_malformed_repo(self):
        self._assert_usage_error(["align", "--repo", "gardener"])

    def test_tend_rejects_malformed_repo(self):
        self._assert_usage_error(["tend", "--repo", "gardener"])

    def test_allowlist_add_rejects_malformed_repo(self):
        self._assert_usage_error(["allowlist", "add", "--repo", "gardener"])

    def test_garden_add_rejects_malformed_repo(self):
        self._assert_usage_error(["garden", "add", "--repo", "gardener"])

    def test_remove_subcommands_still_accept_a_malformed_repo(self):
        # An entry hand-edited into the JSON (or added before this
        # validation existed) has to stay removable.
        for argv in (
            ["allowlist", "remove", "--repo", "malformed-entry"],
            ["garden", "remove", "--repo", "malformed-entry"],
        ):
            args = self.parser.parse_args(argv)
            self.assertEqual(args.repo, "malformed-entry")

    def test_status_repo_filter_is_not_validated(self):
        # `status --repo` only filters already-recorded history; it never
        # dispatches, so a malformed value is a no-match, not an error.
        args = self.parser.parse_args(["status", "--repo", "malformed-entry"])
        self.assertEqual(args.repo, "malformed-entry")


class TestPromptTemplating(unittest.TestCase):
    def test_build_prompt_substitutes_all_placeholders(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target"
            conv = Path(td) / "conv"
            target.mkdir()
            conv.mkdir()
            prompt = build_prompt(
                Mode.REPORT, "owner/name", target, conv, "main", CONVENTIONS_URL
            )
        self.assertNotIn("$repo", prompt)
        self.assertNotIn("$target_cwd", prompt)
        self.assertNotIn("$conventions_dir", prompt)
        self.assertNotIn("$conventions_url", prompt)
        self.assertNotIn("$mode_instructions", prompt)
        self.assertIn("owner/name", prompt)
        # The conventions repo is operator-configured with no built-in
        # default, so the dispatched run has to be told which one it is
        # auditing against — a hardcoded URL in the template would be wrong.
        self.assertIn(CONVENTIONS_URL, prompt)
        self.assertIn("GARDENER_SUMMARY", prompt)

    def test_report_mode_prompt_says_no_write_tools(self):
        with tempfile.TemporaryDirectory() as td:
            prompt = build_prompt(Mode.REPORT, "owner/name", Path(td), Path(td), "main", CONVENTIONS_URL)
        self.assertIn("do NOT have file-write or shell", prompt)

    def test_implement_mode_prompt_mentions_pr(self):
        with tempfile.TemporaryDirectory() as td:
            prompt = build_prompt(Mode.IMPLEMENT, "owner/name", Path(td), Path(td), "main", CONVENTIONS_URL)
        self.assertIn("gh pr create", prompt)

    def test_implement_mode_prompt_requires_passive_voice_and_gardener_attribution(self):
        with tempfile.TemporaryDirectory() as td:
            prompt = build_prompt(Mode.IMPLEMENT, "owner/name", Path(td), Path(td), "main", CONVENTIONS_URL)
        self.assertIn(dev_loop.ATTRIBUTION_REQUIREMENT, prompt)
        self.assertIn(dev_loop.GARDENER_REPO_URL, prompt)

    def test_file_issue_mode_prompt_forbids_multiple_issues(self):
        with tempfile.TemporaryDirectory() as td:
            prompt = build_prompt(Mode.FILE_ISSUE, "owner/name", Path(td), Path(td), "main", CONVENTIONS_URL)
        self.assertIn("Do not open more than one issue", prompt)

    def test_file_issue_mode_prompt_requires_passive_voice_and_gardener_attribution(self):
        with tempfile.TemporaryDirectory() as td:
            prompt = build_prompt(Mode.FILE_ISSUE, "owner/name", Path(td), Path(td), "main", CONVENTIONS_URL)
        self.assertIn(dev_loop.ATTRIBUTION_REQUIREMENT, prompt)
        self.assertIn(dev_loop.GARDENER_REPO_URL, prompt)


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


class TestFirstBlockedIndex(unittest.TestCase):
    """`_first_blocked_index` doubles as how far the round-robin resume
    cursor may safely advance (cli.py's own docstring) — an off-by-one here
    would silently skip or re-attempt a repo, so it's worth pinning
    directly rather than only through `cmd_overnight` integration tests."""

    def test_empty_list_returns_zero(self):
        self.assertEqual(_first_blocked_index([]), 0)

    def test_no_blocked_repo_returns_full_length(self):
        outcomes = [overnight.RepoOutcome(repo="a"), overnight.RepoOutcome(repo="b")]
        self.assertEqual(_first_blocked_index(outcomes), 2)

    def test_returns_index_of_first_blocked_repo(self):
        outcomes = [
            overnight.RepoOutcome(repo="a"),
            overnight.RepoOutcome(repo="b", blocked=True),
            overnight.RepoOutcome(repo="c", blocked=True),
        ]
        self.assertEqual(_first_blocked_index(outcomes), 1)

    def test_blocked_repo_at_index_zero(self):
        outcomes = [overnight.RepoOutcome(repo="a", blocked=True), overnight.RepoOutcome(repo="b")]
        self.assertEqual(_first_blocked_index(outcomes), 0)


class TestBlockingReason(unittest.TestCase):
    """`_blocking_reason` picks the operator-facing (reason, recovery)
    message pair for the first blocked repo — the three real failure
    classes demand genuinely different operator actions (cli.py's own
    docstring), so each branch is worth pinning by name rather than just
    asserting *that* a message was returned."""

    def test_usage_limit_marker_gives_wait_for_reset_guidance(self):
        outcomes = [
            overnight.RepoOutcome(
                repo="a", blocked=True,
                gap_summary="You've hit your session limit · resets 12am (UTC)",
            )
        ]
        reason, recovery = _blocking_reason(outcomes)
        self.assertEqual(reason, "the usage/session limit is exhausted")
        self.assertIn("Wait for the reset time", recovery)

    def test_network_failure_marker_gives_connectivity_guidance(self):
        outcomes = [
            overnight.RepoOutcome(
                repo="a", blocked=True,
                gap_summary="error connecting to api.github.com",
            )
        ]
        reason, recovery = _blocking_reason(outcomes)
        self.assertEqual(reason, "GitHub was unreachable")
        self.assertIn("connectivity", recovery)

    def test_auth_failure_marker_gives_relogin_guidance_and_names_retry_count(self):
        outcomes = [
            overnight.RepoOutcome(
                repo="a", blocked=True,
                gap_summary="Failed to authenticate: OAuth session expired",
            )
        ]
        reason, recovery = _blocking_reason(outcomes)
        self.assertEqual(
            reason,
            f"the dispatched run could not authenticate (after {len(AUTH_RETRY_BACKOFF_SECONDS)} retries)",
        )
        self.assertIn("fresh login", recovery)

    def test_unrecognized_marker_falls_back_to_generic_wording(self):
        outcomes = [
            overnight.RepoOutcome(repo="a", blocked=True, gap_summary="something unclassified broke"),
        ]
        reason, recovery = _blocking_reason(outcomes)
        self.assertEqual(reason, "a device-wide failure blocked the batch")
        self.assertIn("Resolve the condition reported above", recovery)

    def test_uses_the_first_blocked_repo_when_several_are_blocked(self):
        outcomes = [
            overnight.RepoOutcome(repo="a", blocked=False),
            overnight.RepoOutcome(
                repo="b", blocked=True,
                gap_summary="error connecting to api.github.com",
            ),
            overnight.RepoOutcome(
                repo="c", blocked=True,
                gap_summary="Failed to authenticate: OAuth session expired",
            ),
        ]
        reason, _recovery = _blocking_reason(outcomes)
        self.assertEqual(reason, "GitHub was unreachable")

    def test_no_blocked_repo_falls_back_to_generic_wording(self):
        outcomes = [overnight.RepoOutcome(repo="a"), overnight.RepoOutcome(repo="b")]
        reason, recovery = _blocking_reason(outcomes)
        self.assertEqual(reason, "a device-wide failure blocked the batch")
        self.assertIn("Resolve the condition reported above", recovery)


class TestRecordedOutcomeVocabulary(unittest.TestCase):
    """`cmd_align` and `_run_tend_dispatch` record `mode.value` verbatim as
    a successful run's `state.Run.outcome`, so the two vocabularies are
    coupled: a `Mode` whose value isn't in `state.KNOWN_OUTCOMES` gets
    recorded as an outcome `state.repo_stats()` classifies as neither a
    success nor an error, and the dashboard draws the repo as struggling
    with zero tends (issue #67). Asserting the coupling here is what makes
    adding a Mode fail loudly instead of silently."""

    def test_every_mode_recorded_verbatim_is_a_known_state_outcome(self):
        for mode in Mode:
            if mode is Mode.CREATE_DEV_LOOP:
                # The one Mode that never records its own value: its
                # bootstrap dispatch records `created`/`created_incomplete`
                # instead (see cli.py's `_run_tend_dispatch`), both of
                # which KNOWN_OUTCOMES carries directly.
                continue
            with self.subTest(mode=mode.value):
                self.assertIn(mode.value, state.KNOWN_OUTCOMES)

    def test_the_create_dev_loop_bootstrap_outcomes_are_known(self):
        """The one pair of outcomes that isn't a `Mode` value.
        `_run_tend_dispatch` records these via the same `state` constants
        asserted here, so the record site and the classification can't
        drift apart the way three copies of a bare string literal could."""
        self.assertIn(state.CREATED_OUTCOME, state.KNOWN_OUTCOMES)
        self.assertIn(state.CREATED_INCOMPLETE_OUTCOME, state.KNOWN_OUTCOMES)


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


class TestSafeRecordRun(unittest.TestCase):
    """_safe_record_run/_record_and_notify must never raise, even when
    state.record_run itself fails — see issue #9: a bare state.record_run
    call on cmd_align/cmd_tend's success path used to raw-crash the whole
    process for exceptions state.record_run can raise that aren't among
    the (DispatchError, RuntimeError, ValueError,
    subprocess.TimeoutExpired, OSError) tuple those functions already
    catch around dispatch itself, e.g. sqlite3.OperationalError."""

    def _run(self, **overrides):
        defaults = dict(
            repo="owner/name", mode="tend", outcome="tend",
            timestamp=state.now_iso(), gap_summary="did stuff",
        )
        defaults.update(overrides)
        return state.Run(**defaults)

    @patch("gardener.cli.state.record_run", side_effect=RuntimeError("disk full"))
    def test_safe_record_run_swallows_a_record_failure(self, mock_record):
        with redirect_stderr(io.StringIO()) as err:
            _safe_record_run(self._run(), None)  # must not raise
        self.assertIn("state.record_run failed", err.getvalue())

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.state.record_run", side_effect=RuntimeError("disk full"))
    def test_record_and_notify_still_notifies_when_recording_fails(self, mock_record, mock_default_notifier):
        mock_notifier = mock_default_notifier.return_value
        run = self._run()
        with redirect_stderr(io.StringIO()):
            _record_and_notify(run, None)  # must not raise
        mock_notifier.notify.assert_called_once()


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


class TestCmdAllowlist(unittest.TestCase):
    """Mirrors TestCmdGarden — cmd_allowlist and cmd_garden are structurally
    identical (see cmd_garden's own docstring), but only cmd_garden had
    coverage until now (issue #5)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name) / "merge_allowlist.json"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_list_when_empty(self):
        args = argparse.Namespace(allowlist_action="list", allowlist_path=self.path)
        with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()) as out:
            cmd_allowlist(args)
        self.assertIn("merge allow-list is empty", out.getvalue())

    def test_add_then_list(self):
        add_args = argparse.Namespace(allowlist_action="add", repo="owner/name", allowlist_path=self.path)
        with patch("sys.stdout", new=io.StringIO()):
            cmd_allowlist(add_args)
        self.assertEqual(merge_allowlist.list_allowed(path=self.path), ["owner/name"])

    def test_remove(self):
        merge_allowlist.add("owner/name", path=self.path)
        remove_args = argparse.Namespace(allowlist_action="remove", repo="owner/name", allowlist_path=self.path)
        with patch("sys.stdout", new=io.StringIO()):
            cmd_allowlist(remove_args)
        self.assertEqual(merge_allowlist.list_allowed(path=self.path), [])


class TestCmdStatus(unittest.TestCase):
    """cmd_status's own rendering (empty-history message, header/row
    formatting, long-summary truncation) had no direct coverage before —
    only argparse-level coverage in TestArgParsing. Uses a real sqlite3
    tmp-dir db the same way TestCmdAlign/TestCmdOvernight already do
    (state.record_run is the thing under test's data source, not something
    to mock away)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_db = Path(self._tmpdir.name) / "state.sqlite3"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _args(self, repo=None, limit=20):
        return argparse.Namespace(repo=repo, limit=limit, state_db=self.state_db)

    def test_no_runs_prints_a_clear_message(self):
        with patch("sys.stdout", new=io.StringIO()) as out:
            result = cmd_status(self._args())
        self.assertEqual(result, 0)
        self.assertIn("no runs recorded yet", out.getvalue())

    def test_prints_header_and_row_for_a_recorded_run(self):
        run = state.Run(
            repo="owner/name", mode="tend", outcome="pr_opened",
            timestamp=state.now_iso(), gap_summary="opened a PR",
        )
        state.record_run(run, db_path=self.state_db)
        with patch("sys.stdout", new=io.StringIO()) as out:
            result = cmd_status(self._args())
        self.assertEqual(result, 0)
        output = out.getvalue()
        self.assertIn("timestamp", output)
        self.assertIn("repo", output)
        self.assertIn("mode", output)
        self.assertIn("outcome", output)
        self.assertIn("owner/name", output)
        self.assertIn("tend", output)
        self.assertIn("pr_opened", output)
        self.assertIn("opened a PR", output)

    def test_long_summary_is_truncated_with_an_ellipsis(self):
        long_summary = "x" * 100
        run = state.Run(
            repo="owner/name", mode="align", outcome="report",
            timestamp=state.now_iso(), gap_summary=long_summary,
        )
        state.record_run(run, db_path=self.state_db)
        with patch("sys.stdout", new=io.StringIO()) as out:
            cmd_status(self._args())
        output = out.getvalue()
        self.assertNotIn(long_summary, output)
        self.assertIn("x" * 57 + "...", output)

    def test_repo_filter_is_passed_through_to_state_list_runs(self):
        state.record_run(
            state.Run(repo="owner/one", mode="tend", outcome="pr_opened", timestamp=state.now_iso()),
            db_path=self.state_db,
        )
        state.record_run(
            state.Run(repo="owner/two", mode="tend", outcome="pr_opened", timestamp=state.now_iso()),
            db_path=self.state_db,
        )
        with patch("sys.stdout", new=io.StringIO()) as out:
            cmd_status(self._args(repo="owner/one"))
        output = out.getvalue()
        self.assertIn("owner/one", output)
        self.assertNotIn("owner/two", output)


class TestCmdTailTranscript(unittest.TestCase):
    """cmd_tail_transcript is a thin pass-through to
    transcript.print_transcript — this only checks the wiring (args
    forwarded, return code forwarded), not print_transcript's own
    behavior, which test_transcript.py already covers directly."""

    def _args(self, path=Path("/tmp/session.jsonl"), follow=False):
        return argparse.Namespace(path=path, follow=follow)

    def test_forwards_path_and_follow_and_return_code(self):
        with patch("gardener.cli.transcript.print_transcript", return_value=0) as mock_print:
            result = cmd_tail_transcript(self._args(path=Path("/tmp/a.jsonl"), follow=True))
        mock_print.assert_called_once_with(Path("/tmp/a.jsonl"), follow=True)
        self.assertEqual(result, 0)

    def test_forwards_a_non_zero_return_code(self):
        with patch("gardener.cli.transcript.print_transcript", return_value=1):
            result = cmd_tail_transcript(self._args())
        self.assertEqual(result, 1)


class TestCmdDashboard(unittest.TestCase):
    """cmd_dashboard's own job is picking a port (falling back and warning
    if the requested one is taken) and wiring state_dir through to
    run_server — dashboard.find_free_port/run_server's own behavior is
    covered directly in test_dashboard.py."""

    def _args(self, port=dashboard.DEFAULT_PORT, state_dir=None):
        return argparse.Namespace(port=port, state_dir=state_dir)

    def test_serves_on_the_requested_port_when_free(self):
        with patch("gardener.cli.dashboard.find_free_port", return_value=8765) as mock_find, \
                patch("gardener.cli.dashboard.run_server") as mock_run, \
                patch("sys.stderr", new=io.StringIO()) as err:
            result = cmd_dashboard(self._args(port=8765))
        mock_find.assert_called_once_with(preferred=8765)
        mock_run.assert_called_once_with(port=8765, state_dir=None)
        self.assertEqual(result, 0)
        self.assertNotIn("already in use", err.getvalue())

    def test_falls_back_and_warns_when_the_requested_port_is_taken(self):
        with patch("gardener.cli.dashboard.find_free_port", return_value=8766), \
                patch("gardener.cli.dashboard.run_server") as mock_run, \
                patch("sys.stderr", new=io.StringIO()) as err:
            result = cmd_dashboard(self._args(port=8765))
        mock_run.assert_called_once_with(port=8766, state_dir=None)
        self.assertEqual(result, 0)
        self.assertIn("8765", err.getvalue())
        self.assertIn("already in use", err.getvalue())
        self.assertIn("8766", err.getvalue())

    def test_state_dir_is_forwarded_to_run_server(self):
        state_dir = Path("/tmp/gardener-state")
        with patch("gardener.cli.dashboard.find_free_port", return_value=8765), \
                patch("gardener.cli.dashboard.run_server") as mock_run:
            cmd_dashboard(self._args(state_dir=state_dir))
        mock_run.assert_called_once_with(port=8765, state_dir=state_dir)


class TestCmdAlign(unittest.TestCase):
    """cmd_align's own orchestration (mode selection, state.record_run/
    _notify_run wiring, exit codes) had no direct coverage — only its helper
    functions (build_prompt, extract_gap_summary, REPO_RE) did. Mirrors
    TestCmdTendNotifications's shape (issue #5), with conventions.ensure_
    conventions and clone_or_refresh_target_repo both mocked."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_db = Path(self._tmpdir.name) / "state.sqlite3"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _args(self, repo="owner/name", implement=False, file_issue=False,
              conventions_repo=CONVENTIONS_URL):
        return argparse.Namespace(
            repo=repo, implement=implement, file_issue=file_issue, model=None,
            timeout=1800, no_refresh_conventions=False, no_refresh_target=False,
            conventions_repo=conventions_repo, state_db=self.state_db,
        )

    @patch("gardener.cli.conventions.ensure_conventions")
    def test_unconfigured_conventions_repo_exits_before_dispatching(self, mock_conv):
        """An unconfigured conventions repo is a setup error, not a run
        failure: cmd_align must bail with exit 2 without cloning anything
        or recording a run."""
        with patch.dict("os.environ", {}, clear=True):
            stderr = io.StringIO()
            with redirect_stderr(stderr), patch("sys.stdout", new=io.StringIO()):
                exit_code = cmd_align(self._args(conventions_repo=None))

        self.assertEqual(exit_code, 2)
        mock_conv.assert_not_called()
        self.assertIn("GARDENER_CONVENTIONS_URL", stderr.getvalue())

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.clone_or_refresh_target_repo")
    @patch("gardener.cli.conventions.ensure_conventions")
    def test_report_mode_fires_an_info_level_notification(
        self, mock_conv, mock_clone, mock_branch, mock_run_claude, mock_default_notifier
    ):
        mock_conv.return_value = SimpleNamespace(path=Path(self._tmpdir.name))
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: 2 gaps found, no changes made",
            raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
            session_id="s1", permission_denials=[], is_error=False,
        )
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()):
            exit_code = cmd_align(self._args())

        self.assertEqual(exit_code, 0)
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.INFO)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.clone_or_refresh_target_repo")
    @patch("gardener.cli.conventions.ensure_conventions")
    def test_implement_mode_fires_a_warning_level_mutation_notification(
        self, mock_conv, mock_clone, mock_branch, mock_run_claude, mock_default_notifier
    ):
        mock_conv.return_value = SimpleNamespace(path=Path(self._tmpdir.name))
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: PR #12 opened closing 2 gaps",
            raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
            session_id="s1", permission_denials=[], is_error=False,
        )
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()):
            exit_code = cmd_align(self._args(implement=True))

        self.assertEqual(exit_code, 0)
        mock_run_claude.assert_called_once()
        self.assertEqual(mock_run_claude.call_args.kwargs["mode"], Mode.IMPLEMENT)
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.WARNING)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.clone_or_refresh_target_repo")
    @patch("gardener.cli.conventions.ensure_conventions")
    def test_file_issue_mode_fires_a_warning_level_mutation_notification(
        self, mock_conv, mock_clone, mock_branch, mock_run_claude, mock_default_notifier
    ):
        mock_conv.return_value = SimpleNamespace(path=Path(self._tmpdir.name))
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: issue #7 filed for 2 gaps",
            raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
            session_id="s1", permission_denials=[], is_error=False,
        )
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()):
            exit_code = cmd_align(self._args(file_issue=True))

        self.assertEqual(exit_code, 0)
        mock_run_claude.assert_called_once()
        self.assertEqual(mock_run_claude.call_args.kwargs["mode"], Mode.FILE_ISSUE)
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.WARNING)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.clone_or_refresh_target_repo")
    @patch("gardener.cli.conventions.ensure_conventions")
    def test_failed_dispatch_fires_an_error_level_notification(
        self, mock_conv, mock_clone, mock_branch, mock_run_claude, mock_default_notifier
    ):
        mock_conv.return_value = SimpleNamespace(path=Path(self._tmpdir.name))
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=False, result_text="", raw_stdout="", stderr="boom", exit_code=1, duration_ms=50,
            cost_usd=None, session_id=None, permission_denials=[], is_error=True,
        )
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()):
            exit_code = cmd_align(self._args())

        self.assertEqual(exit_code, 1)
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.ERROR)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.conventions.ensure_conventions", side_effect=RuntimeError("clone failed"))
    def test_setup_error_fires_an_error_level_notification(self, mock_conv, mock_default_notifier):
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()):
            exit_code = cmd_align(self._args())

        self.assertEqual(exit_code, 1)
        mock_notifier.notify.assert_called_once()
        _title, _message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.ERROR)

    def test_implement_and_file_issue_mutual_exclusivity_guard(self):
        # Belt-and-suspenders check inside cmd_align itself, independent of
        # argparse's own mutually_exclusive_group (see
        # TestArgParsing.test_implement_and_file_issue_are_mutually_exclusive) —
        # exercised here by constructing the Namespace directly, bypassing
        # argparse entirely.
        with redirect_stderr(io.StringIO()) as err:
            exit_code = cmd_align(self._args(implement=True, file_issue=True))
        self.assertEqual(exit_code, 2)
        self.assertIn("mutually exclusive", err.getvalue())

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.repo_lock.repo_lock", side_effect=repo_lock.RepoLockedError("owner/name"))
    def test_repo_already_locked_skips_the_dispatch_and_notifies(
        self, mock_lock, mock_run_claude, mock_default_notifier
    ):
        # Another gardener process (a manual invocation, or an overlapping
        # overnight run) holding this repo's lock must short-circuit before
        # any clone/dispatch happens — never silently proceed to clone or
        # dispatch claude against the same working tree.
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()):
            exit_code = cmd_align(self._args())

        self.assertEqual(exit_code, 1)
        mock_run_claude.assert_not_called()
        mock_notifier.notify.assert_called_once()
        _title, message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.ERROR)
        self.assertIn("already being worked on", message)


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
    @patch("gardener.cli.state.record_run", side_effect=RuntimeError("sqlite is locked"))
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", return_value=True)
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_record_run_failure_on_success_path_does_not_crash(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, mock_record, mock_default_notifier
    ):
        # Regression test for issue #9: a completed, successful dispatch
        # must still be reported (exit code, notification) even if
        # state.record_run itself raises trying to persist it.
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: 0 issues, no PR opened, repo already aligned",
            raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
            session_id="s1", permission_denials=[], is_error=False,
        )
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()):
            exit_code = cmd_tend(self._args())  # must not raise

        self.assertEqual(exit_code, 0)
        mock_notifier.notify.assert_called_once()

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
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", side_effect=[False, True])
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_create_dev_loop_bootstrap_success_is_a_plain_success(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, mock_default_notifier
    ):
        """Step 6 (`gh repo create`) has been in create-dev-loop's allowed
        tools since 2026-07-19 (issue #12) — a successful bootstrap against
        the real MODE_SPECS now reports plainly, with no incomplete-skill
        WARNING. See test_create_dev_loop_bootstrap_success_warns_if_step_6_is_unreachable
        below for the (mocked) case where that grant has been withdrawn."""
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.side_effect = [
            DispatchResult(
                ok=True, result_text="GARDENER_SUMMARY: created dev-loop skill for name-dev-loop",
                raw_stdout="{}", stderr="", exit_code=0, duration_ms=50, cost_usd=0.01,
                session_id="s1", permission_denials=[], is_error=False,
            ),
            DispatchResult(
                ok=True, result_text="GARDENER_SUMMARY: 0 issues, no PR opened, repo already aligned",
                raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
                session_id="s2", permission_denials=[], is_error=False,
            ),
        ]
        mock_notifier = mock_default_notifier.return_value

        stderr = io.StringIO()
        with redirect_stderr(stderr), patch("sys.stdout", new=io.StringIO()):
            exit_code = cmd_tend(self._args())

        self.assertEqual(exit_code, 0)
        self.assertNotIn("WARNING", stderr.getvalue())
        self.assertIn("skill created", stderr.getvalue())
        # no notification for the (now-complete) bootstrap, one for the tend run itself
        self.assertEqual(mock_notifier.notify.call_count, 1)
        # Same record-site pin as the step-6-unreachable case below, for
        # the complete-bootstrap outcome.
        bootstrap = [
            r for r in state.list_runs(db_path=self.state_db) if r.mode == Mode.CREATE_DEV_LOOP.value
        ]
        self.assertEqual([r.outcome for r in bootstrap], ["created"])
        self.assertEqual(bootstrap[0].outcome, state.CREATED_OUTCOME)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.step6_unreachable", return_value=True)
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", side_effect=[False, True])
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_create_dev_loop_bootstrap_success_warns_if_step_6_is_unreachable(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_step6, mock_run_claude, mock_default_notifier
    ):
        """If create-dev-loop's `gh repo create` grant is ever withdrawn
        again (dev_loop.step6_unreachable() -> True), a successful bootstrap
        must still be surfaced as an incomplete skill, not a plain success
        (issue #12), rather than silently reporting "skill created" with no
        indication its issue tracker is missing."""
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.side_effect = [
            DispatchResult(
                ok=True, result_text="GARDENER_SUMMARY: created dev-loop skill for name-dev-loop",
                raw_stdout="{}", stderr="", exit_code=0, duration_ms=50, cost_usd=0.01,
                session_id="s1", permission_denials=[], is_error=False,
            ),
            DispatchResult(
                ok=True, result_text="GARDENER_SUMMARY: 0 issues, no PR opened, repo already aligned",
                raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
                session_id="s2", permission_denials=[], is_error=False,
            ),
        ]
        mock_notifier = mock_default_notifier.return_value

        stderr = io.StringIO()
        with redirect_stderr(stderr), patch("sys.stdout", new=io.StringIO()):
            exit_code = cmd_tend(self._args())

        self.assertEqual(exit_code, 0)
        self.assertIn("WARNING", stderr.getvalue())
        self.assertIn("Step 6", stderr.getvalue())
        # one notification for the incomplete bootstrap, one for the tend run itself
        self.assertEqual(mock_notifier.notify.call_count, 2)
        # The bootstrap row records the literal string `repo_stats` has to
        # classify, pinning the record site to state's own constant — the
        # `created_incomplete` half of TestRecordedOutcomeVocabulary's
        # assertion is only meaningful if this is what actually lands in
        # the db (issue #67).
        bootstrap = [
            r for r in state.list_runs(db_path=self.state_db) if r.mode == Mode.CREATE_DEV_LOOP.value
        ]
        self.assertEqual([r.outcome for r in bootstrap], ["created_incomplete"])
        self.assertEqual(bootstrap[0].outcome, state.CREATED_INCOMPLETE_OUTCOME)

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

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.clone_or_refresh_target_repo")
    @patch("gardener.cli.repo_lock.repo_lock", side_effect=repo_lock.RepoLockedError("owner/name"))
    def test_repo_already_locked_skips_the_dispatch_and_notifies(
        self, mock_lock, mock_clone, mock_run_claude, mock_default_notifier
    ):
        # Guards the exact scenario `gardener overnight` and a manual
        # `gardener tend`/`gardener align` racing each other against the
        # same repo is meant to prevent: never clone into or dispatch
        # against a repo another gardener process already holds the lock
        # for (see repo_lock.py's module docstring).
        mock_notifier = mock_default_notifier.return_value

        with redirect_stderr(io.StringIO()), patch("sys.stdout", new=io.StringIO()):
            exit_code = cmd_tend(self._args())

        self.assertEqual(exit_code, 1)
        mock_clone.assert_not_called()
        mock_run_claude.assert_not_called()
        mock_notifier.notify.assert_called_once()
        _title, message, level = mock_notifier.notify.call_args[0]
        self.assertEqual(level, Level.ERROR)
        self.assertIn("already being worked on", message)


class TestFormatDenial(unittest.TestCase):
    """`permission_denials` entries come from `claude`'s JSON output, a
    format gardener doesn't own (see dispatch.py's module docstring), so
    every assertion here is about degrading rather than about a shape being
    guaranteed — an entry that isn't the expected dict must still render,
    not raise."""

    def test_bash_denial_renders_the_command(self):
        self.assertEqual(
            format_denial({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}),
            "Bash(rm -rf /)",
        )

    def test_file_tool_denial_renders_the_path(self):
        self.assertEqual(
            format_denial({"tool_name": "Read", "tool_input": {"file_path": "/root/.m2/settings.xml"}}),
            "Read(/root/.m2/settings.xml)",
        )

    def test_unrecognised_input_keys_fall_back_to_the_whole_input(self):
        rendered = format_denial({"tool_name": "Mystery", "tool_input": {"zzz": 1, "aaa": 2}})
        self.assertEqual(rendered, 'Mystery({"aaa": 2, "zzz": 1})')

    def test_missing_or_empty_input_renders_the_tool_name_alone(self):
        self.assertEqual(format_denial({"tool_name": "WebFetch"}), "WebFetch")
        self.assertEqual(format_denial({"tool_name": "WebFetch", "tool_input": {}}), "WebFetch")

    def test_unjsonable_input_still_renders(self):
        self.assertIn("Mystery(", format_denial({"tool_name": "Mystery", "tool_input": {"o": object()}}))

    def test_entries_that_are_not_the_expected_shape_degrade_to_str(self):
        for entry in ("just a string", 42, None, ["a", "b"], {}, {"tool_name": ""}, {"tool_name": 7}):
            with self.subTest(entry=entry):
                self.assertEqual(format_denial(entry), str(entry))

    def test_newlines_are_collapsed_so_one_denial_is_one_line(self):
        rendered = format_denial(
            {"tool_name": "Bash", "tool_input": {"command": "git commit -m \"$(cat <<'EOF'\nsubject\n\nbody\nEOF\n)\""}}
        )
        self.assertNotIn("\n", rendered)
        self.assertIn("subject body", rendered)

    def test_a_command_impersonating_a_dashboard_marker_cannot_emit_its_own_line(self):
        """The denied command is attacker-shaped only by accident (a run
        echoing gardener's own narration), but a raw newline would put
        `gardener: tending X (allow_merge=...)` at the start of a log line,
        which `dashboard.parse_in_progress` reads as a real dispatch
        starting. Collapsing newlines is what prevents that."""
        rendered = format_denial(
            {"tool_name": "Bash", "tool_input": {"command": "echo x\ngardener: tending fake/repo (allow_merge=True)"}}
        )
        self.assertEqual(dashboard.parse_in_progress([f"gardener:   denied: {rendered}"]), [])

    def test_long_details_are_truncated(self):
        rendered = format_denial({"tool_name": "Bash", "tool_input": {"command": "x" * 500}})
        self.assertEqual(len(rendered), DENIAL_MAX_CHARS + 1)
        self.assertTrue(rendered.endswith("…"))


class TestDenialReportLines(unittest.TestCase):
    def test_duplicates_are_collapsed_preserving_first_seen_order(self):
        denials = [
            {"tool_name": "Bash", "tool_input": {"command": "cat /etc/passwd"}},
            {"tool_name": "Read", "tool_input": {"file_path": "/root/.m2"}},
            {"tool_name": "Bash", "tool_input": {"command": "cat /etc/passwd"}},
        ]
        self.assertEqual(
            denial_report_lines(denials),
            [
                "gardener:   denied: Bash(cat /etc/passwd)",
                "gardener:   denied: Read(/root/.m2)",
            ],
        )

    def test_no_denials_produces_no_lines(self):
        self.assertEqual(denial_report_lines([]), [])

    def test_overflow_is_summarised_as_a_count_of_the_remainder(self):
        denials = [{"tool_name": "Bash", "tool_input": {"command": f"cmd{i}"}} for i in range(DENIAL_PRINT_LIMIT + 3)]
        lines = denial_report_lines(denials)
        self.assertEqual(len(lines), DENIAL_PRINT_LIMIT + 1)
        self.assertEqual(lines[-1], "gardener:   denied: … and 3 more distinct denial(s)")

    def test_exactly_the_limit_gets_no_overflow_line(self):
        denials = [{"tool_name": "Bash", "tool_input": {"command": f"cmd{i}"}} for i in range(DENIAL_PRINT_LIMIT)]
        lines = denial_report_lines(denials)
        self.assertEqual(len(lines), DENIAL_PRINT_LIMIT)
        self.assertNotIn("more distinct denial", lines[-1])

    def test_the_limit_counts_distinct_denials_not_raw_entries(self):
        denials = [{"tool_name": "Bash", "tool_input": {"command": "same"}}] * (DENIAL_PRINT_LIMIT + 5)
        self.assertEqual(denial_report_lines(denials), ["gardener:   denied: Bash(same)"])

    def test_none_of_the_lines_are_read_as_dashboard_progress_markers(self):
        """`_dispatch_tend` prints these into the same stderr the dashboard
        parses, so the new lines must be inert to both regexes — including
        the overflow line."""
        denials = [{"tool_name": "Bash", "tool_input": {"command": f"cmd{i}"}} for i in range(DENIAL_PRINT_LIMIT + 2)]
        lines = ["gardener: tending owner/name (allow_merge=False)"] + denial_report_lines(denials)
        self.assertEqual(dashboard.parse_in_progress(lines), ["owner/name"])


class TestDenialsArePrintedBeforeTheNoteThatCitesThem(unittest.TestCase):
    """Issue #99: both dispatch paths printed only a count, then a NOTE
    saying "see denials above" with nothing above it. The assertion that
    matters is the ordering — the NOTE's promise has to be true at the
    point it is made."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_db = Path(self._tmpdir.name) / "state.sqlite3"
        self.denials = [
            {"tool_name": "Bash", "tool_input": {"command": "gh pr review 1 --approve"}},
            {"tool_name": "Read", "tool_input": {"file_path": "/root/.m2/repository"}},
        ]

    def tearDown(self):
        self._tmpdir.cleanup()

    def _assert_denials_precede_the_note(self, stderr_text: str, subject: str):
        lines = stderr_text.splitlines()
        note = next(i for i, line in enumerate(lines) if line.startswith("gardener: NOTE —"))
        denied = [i for i, line in enumerate(lines) if line.startswith("gardener:   denied: ")]
        self.assertEqual(len(denied), 2, stderr_text)
        self.assertTrue(all(i < note for i in denied), stderr_text)
        self.assertIn(f"gardener: NOTE — {subject} attempted", lines[note])
        self.assertIn("see denials above", lines[note])
        self.assertIn("gardener:   denied: Bash(gh pr review 1 --approve)", lines)
        self.assertIn("gardener:   denied: Read(/root/.m2/repository)", lines)
        # The count is still printed — the denial list supplements it.
        self.assertIn("denials=2", stderr_text)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.clone_or_refresh_target_repo")
    @patch("gardener.cli.conventions.ensure_conventions")
    def test_cmd_align_prints_them(self, mock_conv, mock_clone, mock_branch, mock_run_claude, _mock_notifier):
        mock_conv.return_value = SimpleNamespace(path=Path(self._tmpdir.name))
        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: 1 gap found", raw_stdout="{}", stderr="",
            exit_code=0, duration_ms=100, cost_usd=0.01, session_id="s1",
            permission_denials=self.denials, is_error=False,
        )
        args = argparse.Namespace(
            repo="owner/name", implement=False, file_issue=False, model=None, timeout=1800,
            no_refresh_conventions=False, no_refresh_target=False,
            conventions_repo=CONVENTIONS_URL, state_db=self.state_db,
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch("sys.stdout", new=io.StringIO()):
            cmd_align(args)

        self._assert_denials_precede_the_note(stderr.getvalue(), "Claude")

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", return_value=True)
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_dispatch_tend_prints_them_without_disturbing_the_progress_markers(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, _mock_notifier
    ):
        from gardener.cli import _dispatch_tend

        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: 1 issue filed", raw_stdout="{}", stderr="",
            exit_code=0, duration_ms=100, cost_usd=0.01, session_id="s1",
            permission_denials=self.denials, is_error=False,
        )
        args = argparse.Namespace(
            repo="owner/name", allow_merge=False, model=None,
            timeout=TEND_DEFAULT_TIMEOUT_SECONDS, no_refresh_target=False, state_db=self.state_db,
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch("sys.stdout", new=io.StringIO()):
            _dispatch_tend(args)

        self._assert_denials_precede_the_note(stderr.getvalue(), "the dispatched run")
        # Real stderr from the real function, through the real parser — the
        # repo must still clear, same drift-guard as the class below.
        self.assertEqual(dashboard.parse_in_progress(stderr.getvalue().splitlines()), [])


class TestDispatchTendProgressMarkers(unittest.TestCase):
    """`_dispatch_tend`'s stderr narration is the dashboard's only source
    for "what is running right now", so the writing half here and the
    reading half in `dashboard.parse_in_progress` are asserted together —
    same drift-guard shape as `cmd_overnight`'s batch-line test above.

    Every one of `_dispatch_tend`'s four return paths must clear the repo,
    not just the one that reaches a Discord notification: before issue #51
    the dashboard inferred completion from `notify.py`'s webhook-success
    line, which `NullNotifier` (no webhook configured — a documented,
    supported setup) never prints, so every repo the log ever started
    stayed pinned in "Currently tending" for the life of that log."""

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

    def _in_progress(self, stderr: io.StringIO) -> list:
        return dashboard.parse_in_progress(stderr.getvalue().splitlines())

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", return_value=True)
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_completed_dispatch_clears_the_repo_with_no_notifier_output(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, mock_default_notifier
    ):
        from gardener.cli import _dispatch_tend

        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: 0 issues, no PR opened, repo already aligned",
            raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
            session_id="s1", permission_denials=[], is_error=False,
        )
        # The default notifier is a MagicMock here, so it prints nothing —
        # exactly what NullNotifier does when no webhook is configured.
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch("sys.stdout", new=io.StringIO()):
            _dispatch_tend(self._args())

        self.assertIn("gardener: tending owner/name (allow_merge=False)", stderr.getvalue())
        self.assertIn("gardener: finished tending owner/name", stderr.getvalue())
        self.assertEqual(self._in_progress(stderr), [])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.clone_or_refresh_target_repo", side_effect=RuntimeError("clone failed"))
    def test_setup_error_before_dispatch_still_clears_the_repo(
        self, mock_clone, mock_default_notifier
    ):
        from gardener.cli import _dispatch_tend

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = _dispatch_tend(self._args())

        self.assertFalse(result.dispatched)
        self.assertEqual(self._in_progress(stderr), [])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.repo_lock.repo_lock")
    def test_repo_already_locked_still_clears_the_repo(self, mock_lock, mock_default_notifier):
        from gardener.cli import _dispatch_tend

        mock_lock.side_effect = repo_lock.RepoLockedError("owner/name is already being tended")

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = _dispatch_tend(self._args())

        self.assertFalse(result.dispatched)
        self.assertEqual(self._in_progress(stderr), [])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", return_value=False)
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_failed_create_dev_loop_bootstrap_still_clears_the_repo(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, mock_default_notifier
    ):
        from gardener.cli import _dispatch_tend

        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=False, result_text="", raw_stdout="{}", stderr="boom", exit_code=1,
            duration_ms=100, cost_usd=0.01, session_id="s1",
            permission_denials=[], is_error=True,
        )

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = _dispatch_tend(self._args())

        self.assertFalse(result.dispatched)
        self.assertEqual(self._in_progress(stderr), [])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.clone_or_refresh_target_repo", side_effect=KeyboardInterrupt)
    def test_interrupted_dispatch_still_clears_the_repo(self, mock_clone, mock_default_notifier):
        # The marker is printed from a `finally`, so even the kill
        # `cmd_overnight`'s cursor-durability test simulates leaves the
        # panel accurate rather than pinning the repo forever.
        from gardener.cli import _dispatch_tend

        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(KeyboardInterrupt):
            _dispatch_tend(self._args())

        self.assertEqual(self._in_progress(stderr), [])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.run_claude")
    @patch("gardener.cli.dev_loop.has_dev_loop_skill", return_value=True)
    @patch("gardener.cli.current_branch", return_value="main")
    @patch("gardener.cli.find_orphaned_pr", return_value=None)
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_only_the_finished_repo_clears_when_two_dispatches_interleave(
        self, mock_clone, mock_orphan, mock_branch, mock_has_skill, mock_run_claude, mock_default_notifier
    ):
        # Both markers name their repo, which is what makes a
        # --concurrency 2 log — where two dispatches write to the same
        # file at once — attributable rather than ambiguous.
        from gardener.cli import _dispatch_tend

        mock_clone.return_value = Path(self._tmpdir.name)
        mock_run_claude.return_value = DispatchResult(
            ok=True, result_text="GARDENER_SUMMARY: 0 issues, no PR opened, repo already aligned",
            raw_stdout="{}", stderr="", exit_code=0, duration_ms=100, cost_usd=0.01,
            session_id="s1", permission_denials=[], is_error=False,
        )

        stderr = io.StringIO()
        with redirect_stderr(stderr), patch("sys.stdout", new=io.StringIO()):
            print("gardener: tending owner/other (allow_merge=False)", file=sys.stderr)
            _dispatch_tend(self._args())

        self.assertEqual(self._in_progress(stderr), ["owner/other"])


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

    def _args(self, hours=8.0, model=None, concurrency=1, strategy="round-robin", random_seed=None):
        return argparse.Namespace(
            hours=hours, model=model, garden_file=self.garden_file,
            cursor_file=self.cursor_file, state_db=self.state_db,
            concurrency=concurrency, strategy=strategy, random_seed=random_seed, self_update=False,
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
                auth_failed=bool(spec.get("auth_failed")),
                blocked=bool(spec.get("auth_failed") or spec.get("blocked")),
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
    @patch("gardener.cli._dispatch_tend")
    def test_progress_lines_are_parseable_by_the_dashboard(self, mock_dispatch_tend, mock_default_notifier):
        # The dashboard's BATCH_RE reads these exact lines out of the log,
        # so the two are a producer/consumer contract — asserting against
        # cmd_overnight's *real* stderr (not a hand-copied fixture) is what
        # catches the two drifting apart, which is how the sequential
        # `N/T` form went unparsed for every default-concurrency run.
        for repo in ("owner/a", "owner/b", "owner/c"):
            garden.add(repo, path=self.garden_file)
        outcomes = {r: {} for r in ("owner/a", "owner/b", "owner/c")}

        for concurrency, expected in ((1, [(1, 1, 3), (2, 2, 3), (3, 3, 3)]),
                                      (2, [(1, 2, 3), (3, 3, 3)])):
            with self.subTest(concurrency=concurrency):
                self.calls.clear()
                overnight.write_cursor(0, path=self.cursor_file)
                mock_dispatch_tend.side_effect = self._fake_dispatch_tend(outcomes)
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    cmd_overnight(self._args(hours=8.0, concurrency=concurrency))

                batch_lines = [
                    line for line in stderr.getvalue().splitlines()
                    if "candidates this run" in line
                ]
                self.assertEqual(len(batch_lines), len(expected))
                self.assertEqual(
                    [dashboard.parse_batch_progress([line]) for line in batch_lines],
                    expected,
                )

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
        """An explicit `--concurrency 1` must take the exact pre-concurrency
        sequential path (no ThreadPoolExecutor at all) — this is no longer
        the default (see `DEFAULT_OVERNIGHT_CONCURRENCY`), so it is now the
        opt-out an operator picks deliberately, and it has to keep working
        as the genuine zero-overhead sequential path it always was."""
        garden.add("owner/a", path=self.garden_file)
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({"owner/a": {}})
        with patch("gardener.cli.ThreadPoolExecutor") as mock_pool, redirect_stderr(io.StringIO()):
            cmd_overnight(self._args(hours=8.0, concurrency=1))
        mock_pool.assert_not_called()


class TestCmdOvernightSelfUpdate(unittest.TestCase):
    """cmd_overnight's self-update integration — `selfupdate.self_update`
    itself is unit-tested exhaustively in test_selfupdate.py, so this only
    covers the wiring: called by default, skipped with --no-self-update,
    and never allowed to abort the run even if it misbehaves."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self.garden_file = tmp / "garden.json"
        self.cursor_file = tmp / "cursor.json"
        self.state_db = tmp / "state.sqlite3"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _args(self, self_update=True):
        return argparse.Namespace(
            hours=8.0, model=None, garden_file=self.garden_file,
            cursor_file=self.cursor_file, state_db=self.state_db,
            concurrency=1, strategy="round-robin", random_seed=None,
            self_update=self_update,
        )

    @patch("gardener.cli.selfupdate.self_update")
    def test_self_update_runs_by_default(self, mock_self_update):
        mock_self_update.return_value = selfupdate.UpdateResult(
            selfupdate.UpdateStatus.UP_TO_DATE, "already up to date (abc1234)"
        )
        with redirect_stderr(io.StringIO()) as stderr:
            cmd_overnight(self._args())
        mock_self_update.assert_called_once_with()
        self.assertIn("gardener: self-update: already up to date", stderr.getvalue())

    @patch("gardener.cli.selfupdate.self_update")
    def test_no_self_update_flag_skips_it(self, mock_self_update):
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args(self_update=False))
        mock_self_update.assert_not_called()

    @patch("gardener.cli.selfupdate.self_update")
    def test_missing_self_update_attr_still_defaults_to_running_it(self, mock_self_update):
        """A bare Namespace without a `self_update` attribute at all (e.g.
        an older caller predating this flag) must still get the same
        on-by-default behavior as an explicit `self_update=True`."""
        mock_self_update.return_value = selfupdate.UpdateResult(
            selfupdate.UpdateStatus.UP_TO_DATE, "already up to date (abc1234)"
        )
        args = self._args()
        del args.self_update
        with redirect_stderr(io.StringIO()):
            cmd_overnight(args)
        mock_self_update.assert_called_once_with()

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.selfupdate.self_update")
    def test_a_raising_self_update_never_aborts_the_run(self, mock_self_update, _mock_notifier):
        mock_self_update.side_effect = RuntimeError("boom")
        with redirect_stderr(io.StringIO()) as stderr:
            exit_code = cmd_overnight(self._args())
        # Empty garden either way, but the point is this returns cleanly
        # (0, the empty-garden no-op) instead of a raw traceback.
        self.assertEqual(exit_code, 0)
        self.assertIn("gardener: self-update: unexpected error (non-fatal): boom", stderr.getvalue())


class TestCmdOvernightSelfUpdateAlerting(unittest.TestCase):
    """Every self-update skip must reach the operator's notifier, not just
    stderr — an unattended box tending with stale code is precisely the
    case nobody is reading logs for. The routine up-to-date/updated path
    must stay silent, or the nightly noise gets muted and takes the real
    warnings with it."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self.garden_file = tmp / "garden.json"
        self.cursor_file = tmp / "cursor.json"
        self.state_db = tmp / "state.sqlite3"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _args(self):
        return argparse.Namespace(
            hours=8.0, model=None, garden_file=self.garden_file,
            cursor_file=self.cursor_file, state_db=self.state_db,
            concurrency=1, strategy="round-robin", random_seed=None,
            self_update=True,
        )

    def _run_with(self, result):
        """Run cmd_overnight with self_update mocked to `result`, returning
        the list of (title, message, level) calls made to the notifier."""
        with patch("gardener.cli.selfupdate.self_update", return_value=result), \
                patch("gardener.cli.notify.default_notifier") as mock_notifier:
            with redirect_stderr(io.StringIO()):
                cmd_overnight(self._args())
        return mock_notifier.return_value.notify.call_args_list

    def test_every_skip_status_alerts_as_a_warning(self):
        skips = [
            selfupdate.UpdateStatus.SKIPPED_NO_GIT,
            selfupdate.UpdateStatus.SKIPPED_DIRTY,
            selfupdate.UpdateStatus.SKIPPED_DETACHED,
            selfupdate.UpdateStatus.SKIPPED_NO_UPSTREAM,
            selfupdate.UpdateStatus.SKIPPED_NOT_FAST_FORWARD,
        ]
        for status in skips:
            with self.subTest(status=status):
                calls = self._run_with(selfupdate.UpdateResult(status, "skipped because reasons"))
                self.assertEqual(len(calls), 1)
                title, message, level = calls[0].args
                self.assertIn("SKIPPED", title)
                self.assertIn("stale code", title)
                self.assertIn("skipped because reasons", message)
                self.assertIs(level, Level.WARNING)

    def test_the_diverged_case_reports_both_shas(self):
        """The status that actually bit this deployment: a force-pushed
        origin. The alert has to carry enough to act on without ssh'ing in."""
        calls = self._run_with(selfupdate.UpdateResult(
            selfupdate.UpdateStatus.SKIPPED_NOT_FAST_FORWARD,
            "HEAD has diverged from origin/main — not a fast-forward, skipping self-update",
            "abc1234567", "def7654321",
        ))
        self.assertEqual(len(calls), 1)
        _, message, level = calls[0].args
        self.assertIn("abc123456", message)
        self.assertIn("def765432", message)
        self.assertIs(level, Level.WARNING)

    def test_error_status_alerts_at_error_level(self):
        calls = self._run_with(selfupdate.UpdateResult(
            selfupdate.UpdateStatus.ERROR, "git fetch failed: network unreachable"
        ))
        self.assertEqual(len(calls), 1)
        title, message, level = calls[0].args
        self.assertIn("FAILED", title)
        self.assertIn("network unreachable", message)
        self.assertIs(level, Level.ERROR)

    def test_routine_outcomes_stay_silent(self):
        for status, msg in [
            (selfupdate.UpdateStatus.UP_TO_DATE, "already up to date (abc1234)"),
            (selfupdate.UpdateStatus.UPDATED, "updated abc1234 -> def5678"),
        ]:
            with self.subTest(status=status):
                self.assertEqual(self._run_with(selfupdate.UpdateResult(status, msg)), [])

    def test_an_escaping_exception_alerts_too(self):
        """`self_update` is written never to raise, so if one escapes
        anyway it's the least likely thing to be noticed — the
        belt-and-suspenders path has to alert, not just log."""
        with patch("gardener.cli.selfupdate.self_update", side_effect=RuntimeError("boom")), \
                patch("gardener.cli.notify.default_notifier") as mock_notifier:
            with redirect_stderr(io.StringIO()):
                exit_code = cmd_overnight(self._args())
        self.assertEqual(exit_code, 0)
        calls = mock_notifier.return_value.notify.call_args_list
        self.assertEqual(len(calls), 1)
        title, message, level = calls[0].args
        self.assertIn("FAILED", title)
        self.assertIn("boom", message)
        self.assertIs(level, Level.ERROR)

    def test_a_failing_notifier_never_aborts_the_run(self):
        """Mirrors _notify_run's stance: alerting must never break the run
        it reports on."""
        with patch("gardener.cli.selfupdate.self_update", return_value=selfupdate.UpdateResult(
                selfupdate.UpdateStatus.SKIPPED_DIRTY, "dirty tree")), \
                patch("gardener.cli.notify.default_notifier",
                      side_effect=RuntimeError("discord down")):
            with redirect_stderr(io.StringIO()) as stderr:
                exit_code = cmd_overnight(self._args())
        self.assertEqual(exit_code, 0)
        self.assertIn("notification failed (non-fatal): discord down", stderr.getvalue())


class TestCmdUpdate(unittest.TestCase):
    @patch("gardener.cli.selfupdate.self_update")
    def test_prints_result_and_returns_0_on_success(self, mock_self_update):
        mock_self_update.return_value = selfupdate.UpdateResult(
            selfupdate.UpdateStatus.UPDATED, "updated abc1234 -> def5678", "abc1234", "def5678"
        )
        with redirect_stderr(io.StringIO()), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = cmd_update(argparse.Namespace(check=False))
        self.assertEqual(exit_code, 0)
        self.assertIn("gardener: self-update: updated abc1234 -> def5678", stdout.getvalue())
        mock_self_update.assert_called_once_with(check_only=False)

    @patch("gardener.cli.selfupdate.self_update")
    def test_check_flag_is_threaded_through(self, mock_self_update):
        mock_self_update.return_value = selfupdate.UpdateResult(
            selfupdate.UpdateStatus.UPDATE_AVAILABLE, "update available: abc1234 -> def5678"
        )
        with patch("sys.stdout", new_callable=io.StringIO):
            cmd_update(argparse.Namespace(check=True))
        mock_self_update.assert_called_once_with(check_only=True)

    @patch("gardener.cli.selfupdate.self_update")
    def test_error_status_returns_1(self, mock_self_update):
        mock_self_update.return_value = selfupdate.UpdateResult(
            selfupdate.UpdateStatus.ERROR, "git fetch failed: could not resolve host"
        )
        with patch("sys.stdout", new_callable=io.StringIO):
            exit_code = cmd_update(argparse.Namespace(check=False))
        self.assertEqual(exit_code, 1)

    @patch("gardener.cli.selfupdate.self_update")
    def test_skip_statuses_return_0_not_a_cli_failure(self, mock_self_update):
        for status in (
            selfupdate.UpdateStatus.SKIPPED_NO_GIT,
            selfupdate.UpdateStatus.SKIPPED_DIRTY,
            selfupdate.UpdateStatus.SKIPPED_DETACHED,
            selfupdate.UpdateStatus.SKIPPED_NO_UPSTREAM,
            selfupdate.UpdateStatus.SKIPPED_NOT_FAST_FORWARD,
        ):
            mock_self_update.return_value = selfupdate.UpdateResult(status, "skipped")
            with patch("sys.stdout", new_callable=io.StringIO):
                exit_code = cmd_update(argparse.Namespace(check=False))
            self.assertEqual(exit_code, 0, f"{status} should not be a CLI failure")


class TestCloneOrRefreshGuards(unittest.TestCase):
    """The checks `clone_or_refresh_target_repo` makes *before* it will
    touch a cache clone. Each one is the last thing standing between a
    dispatch and the wrong working tree, and none were covered — `_run` is
    mocked throughout, so no real `git`/`gh` process runs."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmpdir.name)
        self.dest = self.cache_dir / "owner__repo"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_malformed_repo_is_rejected_before_anything_runs(self):
        with patch("gardener.cli._run") as mock_run:
            with self.assertRaises(ValueError) as ctx:
                clone_or_refresh_target_repo("not-a-repo", self.cache_dir)
        self.assertIn("owner/name", str(ctx.exception))
        mock_run.assert_not_called()

    def test_missing_gh_is_a_clear_error_not_a_confusing_subprocess_failure(self):
        with patch("gardener.cli.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                clone_or_refresh_target_repo("owner/repo", self.cache_dir)
        self.assertIn("`gh` not found on PATH", str(ctx.exception))

    def test_a_cache_dir_whose_origin_does_not_match_is_refused(self):
        """The important one. This directory is named after the repo, so a
        stale or hand-created clone pointing somewhere else would otherwise
        be dispatched against under the requested repo's name — Claude
        reading and potentially writing the wrong project. Refusing beats
        re-pointing: silently fixing it would hide however it got there."""
        (self.dest / ".git").mkdir(parents=True)

        def fake(argv, cwd=None, timeout=120):
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="https://github.com/someone/else\n", stderr=""
            )

        with patch("gardener.cli._run", side_effect=fake):
            with patch("gardener.cli.shutil.which", return_value="/usr/bin/gh"):
                with self.assertRaises(RuntimeError) as ctx:
                    clone_or_refresh_target_repo("owner/repo", self.cache_dir, refresh=True)
        message = str(ctx.exception)
        self.assertIn("refusing to reuse it", message)
        self.assertIn("owner/repo", message)

    def test_a_failed_clone_surfaces_ghs_stderr(self):
        def fake(argv, cwd=None, timeout=120):
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout="", stderr="repository not found\n"
            )

        with patch("gardener.cli._run", side_effect=fake):
            with patch("gardener.cli.shutil.which", return_value="/usr/bin/gh"):
                with self.assertRaises(RuntimeError) as ctx:
                    clone_or_refresh_target_repo("owner/repo", self.cache_dir)
        self.assertIn("repository not found", str(ctx.exception))


class TestDefaultBranchName(unittest.TestCase):
    """`_default_branch_name`'s failure wording is load-bearing beyond this
    function: `dispatch.py`'s device-global failure classifier deliberately
    does *not* match this wrapper sentence, because it is raised both for a
    transient GitHub outage and for a permanently deleted/renamed repo (see
    `NETWORK_FAILURE_MARKERS`' comment). Pin the shape so that reasoning
    stays checkable."""

    def _result(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_returns_the_branch_name_stripped(self):
        with patch("gardener.cli._run", return_value=self._result(stdout="develop\n")):
            self.assertEqual(_default_branch_name("owner/repo"), "develop")

    def test_nonzero_exit_raises_with_the_repo_and_stderr(self):
        with patch("gardener.cli._run", return_value=self._result(returncode=1, stderr="  boom  ")):
            with self.assertRaises(RuntimeError) as ctx:
                _default_branch_name("owner/repo")
        message = str(ctx.exception)
        self.assertIn("could not determine default branch for owner/repo", message)
        self.assertIn("boom", message)

    def test_empty_output_with_a_zero_exit_still_raises(self):
        """`gh` can exit 0 having printed nothing. Returning "" here would
        produce `git checkout ""` further down rather than a clear error."""
        with patch("gardener.cli._run", return_value=self._result(returncode=0, stdout="  \n")):
            with self.assertRaises(RuntimeError):
                _default_branch_name("owner/repo")


class TestCurrentBranch(unittest.TestCase):
    def test_returns_the_checked_out_branch(self):
        result = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="feature/x\n", stderr="")
        with patch("gardener.cli._run", return_value=result):
            self.assertEqual(current_branch(Path("/tmp/anything")), "feature/x")

    def test_empty_output_falls_back_to_main(self):
        """A detached HEAD or a failed `rev-parse` yields nothing; the
        fallback keeps callers from building a branch-less command."""
        result = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="\n", stderr="")
        with patch("gardener.cli._run", return_value=result):
            self.assertEqual(current_branch(Path("/tmp/anything")), "main")


class TestCloneOrRefreshClean(unittest.TestCase):
    """The refresh step's `git clean` invocation. `_run` is mocked — these
    tests never actually run `git`/`gh`.

    Regression coverage for a real, every-single-run failure:
    Dans-Plugins/dansplugins-dot-com's cache clone carries a 190MB
    `node_modules`, and `git clean -fdx` could not unlink it inside the
    60s timeout every refresh command used to share, so that repo failed
    with `Command '['git', 'clean', '-fdx']' timed out after 60 seconds`
    before its tend dispatch could even start."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.dest = Path(self._tmpdir.name) / "owner__repo"
        (self.dest / ".git").mkdir(parents=True)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _refresh(self, mock_run):
        def fake(argv, cwd=None, timeout=120):
            stdout = "main\n" if argv[:2] == ["gh", "repo"] else "https://github.com/owner/repo\n"
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout, stderr="")

        mock_run.side_effect = fake
        with patch("gardener.cli.shutil.which", return_value="/usr/bin/gh"):
            clone_or_refresh_target_repo("owner/repo", self.dest.parent, refresh=True)
        return [call.args[0] for call in mock_run.call_args_list]

    def _clean_call(self, mock_run):
        for call in mock_run.call_args_list:
            if call.args[0][:2] == ["git", "clean"]:
                return call
        self.fail("no `git clean` invocation was made")

    @patch("gardener.cli._run")
    def test_clean_preserves_dependency_caches(self, mock_run):
        self._refresh(mock_run)
        argv = self._clean_call(mock_run).args[0]
        for preserved in PRESERVED_DEPENDENCY_DIRS:
            self.assertIn(preserved, argv)
            self.assertEqual(argv[argv.index(preserved) - 1], "-e")

    @patch("gardener.cli._run")
    def test_clean_still_removes_everything_else(self, mock_run):
        # -fdx is retained: the point is to keep dependency caches, not to
        # stop cleaning. A stale build output must still be removed.
        self._refresh(mock_run)
        argv = self._clean_call(mock_run).args[0]
        self.assertEqual(argv[:3], ["git", "clean", "-fdx"])
        for build_output in ("build", "target", "dist"):
            self.assertNotIn(build_output, argv)

    @patch("gardener.cli._run")
    def test_clean_gets_a_longer_timeout_than_fetch_and_checkout(self, mock_run):
        self._refresh(mock_run)
        self.assertEqual(self._clean_call(mock_run).kwargs["timeout"], CLEAN_TIMEOUT_SECONDS)
        self.assertGreater(CLEAN_TIMEOUT_SECONDS, REFRESH_TIMEOUT_SECONDS)
        for call in mock_run.call_args_list:
            if call.args[0][:2] in (["git", "fetch"], ["git", "checkout"]):
                self.assertEqual(call.kwargs["timeout"], REFRESH_TIMEOUT_SECONDS)

    @patch("gardener.cli._run")
    def test_a_failing_clean_still_raises_with_the_full_command(self, mock_run):
        def fake(argv, cwd=None, timeout=120):
            if argv[:2] == ["git", "clean"]:
                return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="boom")
            stdout = "main\n" if argv[:2] == ["gh", "repo"] else "https://github.com/owner/repo\n"
            return subprocess.CompletedProcess(argv, returncode=0, stdout=stdout, stderr="")

        mock_run.side_effect = fake
        with patch("gardener.cli.shutil.which", return_value="/usr/bin/gh"):
            with self.assertRaises(RuntimeError) as ctx:
                clone_or_refresh_target_repo("owner/repo", self.dest.parent, refresh=True)
        self.assertIn("git clean", str(ctx.exception))


class TestCmdOvernightAuthAbort(unittest.TestCase):
    """An auth failure is device-global, so `cmd_overnight` must stop the
    whole run and hold its resume cursor back rather than marching the rest
    of the garden through a credential that's currently broken.

    Regression coverage for a real incident (2026-07-24): twelve of fifteen
    garden repos failed in under a minute with
    "Failed to authenticate: OAuth session expired and could not be
    refreshed", the cursor advanced past all twelve, and those repos went
    untended for the night even though auth recovered ~20 minutes later."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self.garden_file = tmp / "garden.json"
        self.cursor_file = tmp / "cursor.json"
        self.state_db = tmp / "state.sqlite3"
        self.calls: list[str] = []
        for repo in ("owner/a", "owner/b", "owner/c"):
            garden.add(repo, path=self.garden_file)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _args(self, strategy="round-robin", concurrency=1):
        return argparse.Namespace(
            hours=8.0, model=None, garden_file=self.garden_file,
            cursor_file=self.cursor_file, state_db=self.state_db,
            concurrency=concurrency, strategy=strategy, random_seed=None, self_update=False,
        )

    def _fake_dispatch_tend(self, auth_failures: set, gap_summary: str = "Failed to authenticate: OAuth session expired"):
        """`gap_summary` is parameterized so the same harness can drive any
        of the three device-global classes — the abort path keys off
        `blocked`, which the real dispatch layer sets for all of them."""
        def fake(args):
            self.calls.append(args.repo)
            failed = args.repo in auth_failures
            run = state.Run(
                repo=args.repo, mode="tend",
                outcome="error" if failed else "tend",
                timestamp=state.now_iso(),
                gap_summary=gap_summary if failed else "",
            )
            state.record_run(run, db_path=args.state_db)
            return TendResult(
                exit_code=1 if failed else 0, ok=not failed, run=run,
                auth_failed=failed and looks_like_auth_failure(gap_summary, ""),
                blocked=failed and is_device_global_failure(gap_summary),
            )
        return fake

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_auth_failure_stops_the_run_instead_of_burning_the_garden(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({"owner/a"})
        with redirect_stderr(io.StringIO()):
            exit_code = cmd_overnight(self._args())
        self.assertEqual(exit_code, 0)
        # owner/b and owner/c were never dispatched — they'd have failed
        # identically, in seconds, and been marked as attempted.
        self.assertEqual(self.calls, ["owner/a"])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_cursor_is_not_advanced_past_an_auth_failed_repo(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({"owner/a"})
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args())
        # Still 0: the next invocation retries owner/a rather than skipping it.
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 0)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_cursor_keeps_the_progress_made_before_the_auth_failure(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        # owner/a tended fine; owner/b then hit the auth wall. The run's real
        # progress (one repo) is kept, and the cursor stops exactly at the
        # repo that failed — not before it, not past it.
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({"owner/b"})
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args())
        self.assertEqual(self.calls, ["owner/a", "owner/b"])
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 1)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_usage_limit_stops_the_run_and_holds_the_cursor(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        """The 2026-07-25 22:34 failure, replayed: an exhausted usage window
        used to read as an ordinary per-repo error, so the batch marched on
        and the cursor advanced past 20 repos that never got a real
        attempt."""
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend(
            {"owner/a"}, gap_summary="You've hit your session limit \u00b7 resets 12am (UTC)",
        )
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args())
        self.assertEqual(self.calls, ["owner/a"])
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 0)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_usage_limit_notification_names_the_quota_not_the_credentials(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        """Recovery differs per class — waiting out a quota and re-logging in
        are not interchangeable, so the alert must not say "authenticate"."""
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend(
            {"owner/a"}, gap_summary="You've hit your session limit \u00b7 resets 12am (UTC)",
        )
        mock_notifier = mock_default_notifier.return_value
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args())
        title, message, level = mock_notifier.notify.call_args_list[0].args
        self.assertEqual(level, Level.ERROR)
        self.assertIn("limit", title.lower())
        self.assertNotIn("authenticate", title.lower())
        self.assertIn("quota", message.lower())

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.clone_or_refresh_target_repo")
    def test_github_outage_before_dispatch_stops_the_run_and_holds_the_cursor(
        self, mock_clone, mock_default_notifier
    ):
        """The path that produced 17 recorded failures. A GitHub outage hits
        while resolving the default branch / cloning — before `claude` is
        ever invoked — so it raises rather than returning a DispatchResult,
        and is classified from the exception text instead."""
        mock_clone.side_effect = RuntimeError(
            "could not determine default branch for owner/a: error connecting to "
            "api.github.com\ncheck your internet connection or https://githubstatus.com"
        )
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args())
        # Cursor held at 0: owner/a never got a real attempt, so the next
        # invocation must re-attempt it rather than skip it.
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 0)
        titles = [c.args[0].lower() for c in mock_default_notifier.return_value.notify.call_args_list]
        abort = [t for t in titles if "aborted" in t]
        self.assertEqual(len(abort), 1, f"expected one abort alert, got titles: {titles}")
        self.assertIn("github", abort[0])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_an_ordinary_repo_failure_still_advances_the_cursor(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        """The guard against over-triggering: a repo whose tend genuinely
        failed must NOT abort the garden or hold the cursor, or one broken
        repo would stall every subsequent night."""
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend(
            {"owner/a"}, gap_summary="build failed: compilation error in Main.java",
        )
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args())
        # Every repo still got its turn, and the cursor completed the cycle.
        self.assertEqual(self.calls, ["owner/a", "owner/b", "owner/c"])
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 0)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_auth_abort_fires_a_dedicated_error_notification(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({"owner/a"})
        mock_notifier = mock_default_notifier.return_value
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args())
        # Two notifications: the dedicated auth alert plus the usual batch
        # summary — the summary alone reads as ordinary per-repo errors and
        # buries the one thing an operator has to act on.
        self.assertEqual(mock_notifier.notify.call_count, 2)
        title, message, level = mock_notifier.notify.call_args_list[0].args
        self.assertIn("authenticate", title.lower())
        self.assertEqual(level, Level.ERROR)
        self.assertIn("cursor", message.lower())

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_notifier_failure_on_the_abort_path_does_not_crash_the_run(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({"owner/a"})
        mock_default_notifier.return_value.notify.side_effect = RuntimeError("webhook down")
        with redirect_stderr(io.StringIO()):
            exit_code = cmd_overnight(self._args())
        self.assertEqual(exit_code, 0)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_non_auth_error_does_not_abort_the_run(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        # The complement of the above: an ordinary per-repo failure is local
        # to that repo, so the rest of the garden must still be tended.
        def fake(args):
            self.calls.append(args.repo)
            failed = args.repo == "owner/a"
            run = state.Run(
                repo=args.repo, mode="tend", outcome="error" if failed else "tend",
                timestamp=state.now_iso(), gap_summary="the build broke" if failed else "",
            )
            state.record_run(run, db_path=args.state_db)
            return TendResult(exit_code=1 if failed else 0, ok=not failed, run=run)

        mock_dispatch_tend.side_effect = fake
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args())
        self.assertEqual(self.calls, ["owner/a", "owner/b", "owner/c"])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_name_based_cursor_drops_only_the_auth_failed_repo(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        # For the name-keyed strategies the cursor is a set, not a position,
        # so it can be exact: the repo that tended fine stays recorded as
        # attempted and only the auth-failed one is left for the next run.
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend({"owner/b"})
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args(strategy="random", concurrency=2))
        attempted = overnight.read_attempted(path=self.cursor_file)
        self.assertNotIn("owner/b", attempted)
        for repo in self.calls:
            if repo != "owner/b":
                self.assertIn(repo, attempted)


class TestCmdOvernightCursorSurvivesAKill(unittest.TestCase):
    """The resume cursor must be current after every *batch*, not only after
    the loop finishes — on this device a long run is more likely to be killed
    mid-garden (Android kills background processes on a task swipe-away) than
    to reach the end of its loop.

    Regression coverage for a real incident (2026-07-25, issue #42): a run
    tended six repos between 19:12 and 19:43, was killed, and the run that
    autostart brought back at 19:56 logged "0 repo(s) already attempted this
    cycle" against a cursor whose mtime was still 06:25 — none of the six had
    ever been persisted, so the cycle restarted from zero.

    A kill is simulated with a `BaseException` (what a real signal-driven
    teardown looks like from inside the loop) that `_dispatch_one_for_overnight`'s
    `except Exception` deliberately does not catch, so it propagates out of
    `cmd_overnight` exactly as a kill would — the post-loop code never runs.
    Every assertion here fails against the pre-fix write-once-at-the-end code."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self.garden_file = tmp / "garden.json"
        self.cursor_file = tmp / "cursor.json"
        self.state_db = tmp / "state.sqlite3"
        self.calls: list[str] = []
        for repo in ("owner/a", "owner/b", "owner/c", "owner/d"):
            garden.add(repo, path=self.garden_file)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _args(self, strategy="round-robin", concurrency=1):
        return argparse.Namespace(
            hours=8.0, model=None, garden_file=self.garden_file,
            cursor_file=self.cursor_file, state_db=self.state_db,
            concurrency=concurrency, strategy=strategy, random_seed=1234, self_update=False,
        )

    def _dispatch_until_killed(self, kill_on: str):
        """Tends normally until `kill_on` is dispatched, then dies the way a
        killed process does — no cleanup, no post-loop cursor write."""
        def fake(args):
            if args.repo == kill_on:
                raise KeyboardInterrupt("simulated task-swipe kill")
            self.calls.append(args.repo)
            run = state.Run(
                repo=args.repo, mode="tend", outcome="tend",
                timestamp=state.now_iso(), gap_summary="",
            )
            state.record_run(run, db_path=args.state_db)
            return TendResult(exit_code=0, ok=True, run=run)
        return fake

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_round_robin_cursor_keeps_repos_finished_before_a_kill(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        mock_dispatch_tend.side_effect = self._dispatch_until_killed("owner/c")
        with redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            cmd_overnight(self._args())
        self.assertEqual(self.calls, ["owner/a", "owner/b"])
        # Points at owner/c — the two finished repos are not re-tended, and
        # the one that was in flight when the kill landed still is.
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 2)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_name_keyed_cursor_keeps_repos_finished_before_a_kill(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        mock_dispatch_tend.side_effect = self._dispatch_until_killed("owner/c")
        with redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            cmd_overnight(self._args(strategy="random"))
        self.assertTrue(self.calls, "expected at least one repo before the kill")
        attempted = overnight.read_attempted(path=self.cursor_file)
        self.assertEqual(attempted, self.calls)
        self.assertNotIn("owner/c", attempted)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_persistence_is_per_batch_not_mid_batch(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        # Killed inside the *second* batch: the first batch's two repos are
        # persisted, and the second batch's other repo — which may well have
        # finished concurrently — is deliberately not, since a partially
        # completed batch is re-attempted whole (re-tending is idempotent
        # enough; silently skipping a repo is not).
        mock_dispatch_tend.side_effect = self._dispatch_until_killed("owner/c")
        with redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            cmd_overnight(self._args(concurrency=2))
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 2)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_a_kill_before_the_first_batch_completes_leaves_the_cursor_alone(
        self, mock_dispatch_tend, mock_default_notifier
    ):
        overnight.write_cursor(1, path=self.cursor_file)
        mock_dispatch_tend.side_effect = self._dispatch_until_killed("owner/b")
        with redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            cmd_overnight(self._args())
        # Started at owner/b and never finished it: nothing to record, and
        # nothing lost either — the cursor stays exactly where it was.
        self.assertEqual(self.calls, [])
        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 1)


class TestCmdOvernightStrategies(unittest.TestCase):
    """cmd_overnight's --strategy wiring: issue-count sorts by a mocked
    fetch_issue_counts (never invokes `gh`), random uses an injectable seed
    (never a real shuffle), and both resume by repo name across two
    invocations rather than round-robin's bare index — see overnight.py's
    module docstring for why."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self.garden_file = tmp / "garden.json"
        self.cursor_file = tmp / "cursor.json"
        self.state_db = tmp / "state.sqlite3"
        self.calls: list[str] = []

    def tearDown(self):
        self._tmpdir.cleanup()

    def _args(self, hours=8.0, strategy="round-robin", random_seed=None, concurrency=1):
        return argparse.Namespace(
            hours=hours, model=None, garden_file=self.garden_file,
            cursor_file=self.cursor_file, state_db=self.state_db,
            concurrency=concurrency, strategy=strategy, random_seed=random_seed, self_update=False,
        )

    def _fake_dispatch_tend(self):
        def fake(args):
            self.calls.append(args.repo)
            run = state.Run(repo=args.repo, mode="tend", outcome="tend", timestamp=state.now_iso())
            state.record_run(run, db_path=args.state_db)
            return TendResult(exit_code=0, ok=True, run=run)
        return fake

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.fetch_issue_counts")
    @patch("gardener.cli._dispatch_tend")
    def test_issue_count_strategy_dispatches_highest_count_first(
        self, mock_dispatch_tend, mock_fetch_counts, mock_default_notifier
    ):
        garden.add("owner/low", path=self.garden_file)
        garden.add("owner/high", path=self.garden_file)
        garden.add("owner/mid", path=self.garden_file)
        mock_fetch_counts.return_value = {"owner/low": 1, "owner/high": 20, "owner/mid": 5}
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend()

        with redirect_stderr(io.StringIO()):
            exit_code = cmd_overnight(self._args(hours=8.0, strategy="issue-count"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(self.calls, ["owner/high", "owner/mid", "owner/low"])
        mock_fetch_counts.assert_called_once()
        # a full cycle completed -> attempted resets, not a bare index
        raw = json.loads(self.cursor_file.read_text())
        self.assertNotIn("next_index", raw)

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_random_strategy_with_a_seed_is_deterministic(self, mock_dispatch_tend, mock_default_notifier):
        for repo in ("owner/a", "owner/b", "owner/c", "owner/d"):
            garden.add(repo, path=self.garden_file)
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend()

        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args(hours=8.0, strategy="random", random_seed=1234))
        first_run_order = list(self.calls)
        self.calls.clear()

        # Reset the cursor file so the second invocation starts a fresh
        # cycle too, isolating this assertion to "same seed -> same order"
        # rather than resume-filtering across runs (covered separately below).
        self.cursor_file.unlink()
        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args(hours=8.0, strategy="random", random_seed=1234))

        self.assertEqual(first_run_order, self.calls)
        self.assertEqual(sorted(self.calls), ["owner/a", "owner/b", "owner/c", "owner/d"])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli.time.monotonic")
    @patch("gardener.cli._dispatch_tend")
    def test_random_strategy_resumes_by_name_not_index_across_invocations(
        self, mock_dispatch_tend, mock_monotonic, mock_default_notifier
    ):
        garden.add("owner/a", path=self.garden_file)
        garden.add("owner/b", path=self.garden_file)
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend()
        # Budget exhausted after exactly one repo each run (mirrors
        # test_stops_dispatching_once_budget_is_exhausted's own numbers).
        mock_monotonic.side_effect = [0.0, 0.0, 50.0, 50.0, 0.0, 0.0, 50.0, 50.0]

        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args(hours=0.02, strategy="random", random_seed=99))
        first_attempted = list(self.calls)
        self.assertEqual(len(first_attempted), 1)
        self.calls.clear()

        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args(hours=0.02, strategy="random", random_seed=99))
        second_attempted = list(self.calls)
        self.assertEqual(len(second_attempted), 1)

        # The second run must not repeat the first run's already-attempted
        # repo — resuming by name, not a stale index into a reshuffled order.
        self.assertNotEqual(first_attempted, second_attempted)
        self.assertEqual(sorted(first_attempted + second_attempted), ["owner/a", "owner/b"])

    @patch("gardener.cli.notify.default_notifier")
    @patch("gardener.cli._dispatch_tend")
    def test_round_robin_cursor_file_untouched_by_a_random_run(self, mock_dispatch_tend, mock_default_notifier):
        """round-robin's next_index must survive a --strategy random run
        against the same cursor file (they share the file but use different
        keys — see overnight.py's docstring)."""
        garden.add("owner/a", path=self.garden_file)
        garden.add("owner/b", path=self.garden_file)
        overnight.write_cursor(1, path=self.cursor_file)
        mock_dispatch_tend.side_effect = self._fake_dispatch_tend()

        with redirect_stderr(io.StringIO()):
            cmd_overnight(self._args(hours=8.0, strategy="random", random_seed=5))

        self.assertEqual(overnight.read_cursor(path=self.cursor_file), 1)


class TestSessionCommandParsing(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_ps_defaults_to_running_sessions_only(self):
        args = self.parser.parse_args(["ps"])
        self.assertFalse(args.all)
        self.assertFalse(args.quiet)

    def test_ps_short_flags_match_docker(self):
        args = self.parser.parse_args(["ps", "-a", "-q"])
        self.assertTrue(args.all)
        self.assertTrue(args.quiet)

    def test_stop_takes_several_sessions(self):
        args = self.parser.parse_args(["stop", "abc", "def"])
        self.assertEqual(args.session, ["abc", "def"])

    def test_stop_grace_period_defaults_to_the_shared_constant(self):
        args = self.parser.parse_args(["stop", "abc"])
        self.assertEqual(args.time, sessions.DEFAULT_STOP_TIMEOUT_SECONDS)
        self.assertEqual(self.parser.parse_args(["stop", "-t", "3", "abc"]).time, 3.0)

    def test_kill_defaults_to_sigkill(self):
        self.assertEqual(self.parser.parse_args(["kill", "abc"]).signal, "KILL")
        self.assertEqual(self.parser.parse_args(["kill", "-s", "INT", "abc"]).signal, "INT")


class TestParseSignal(unittest.TestCase):
    def test_accepts_every_spelling_docker_does(self):
        self.assertEqual(_parse_signal("KILL"), int(signal.SIGKILL))
        self.assertEqual(_parse_signal("SIGKILL"), int(signal.SIGKILL))
        self.assertEqual(_parse_signal("term"), int(signal.SIGTERM))
        self.assertEqual(_parse_signal("9"), 9)

    def test_unknown_name_says_what_to_pass_instead(self):
        with self.assertRaises(ValueError) as ctx:
            _parse_signal("NOPE")
        self.assertIn("KILL/TERM/INT", str(ctx.exception))


class TestSessionTarget(unittest.TestCase):
    def test_a_repo_command_targets_that_repo(self):
        self.assertEqual(_session_target(SimpleNamespace(repo="owner/name")), "owner/name")

    def test_overnight_targets_the_garden(self):
        self.assertEqual(_session_target(SimpleNamespace()), "garden")
        self.assertEqual(_session_target(SimpleNamespace(repo=None)), "garden")


class TestSessionCommands(unittest.TestCase):
    """`ps`/`stop`/`kill` against a real sessions directory, with the actual
    signalling mocked — no test here may signal a real process."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self.sessions_dir = self.state_dir / "sessions"
        self.sessions_dir.mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _write(self, session_id, target="owner/name", command="tend", pid=4242):
        path = self.sessions_dir / f"{session_id}.json"
        path.write_text(json.dumps({
            "id": session_id, "pid": pid, "command": command, "target": target,
            "started_at": "2026-08-19T03:00:00+00:00", "log_path": None,
        }))
        return path

    def _args(self, **kwargs):
        base = dict(state_dir=self.state_dir, all=False, quiet=False, session=[], time=1.0,
                    signal="KILL")
        base.update(kwargs)
        return SimpleNamespace(**base)

    def _running(self, path):
        """Hold `path`'s lock for the rest of the test, so the session it
        describes reads as running."""
        fd = os.open(path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)

    def test_ps_lists_a_running_session(self):
        self._running(self._write("abcdef01", target="owner/live"))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cmd_ps(self._args()), 0)
        self.assertIn("abcdef01", out.getvalue())
        self.assertIn("owner/live", out.getvalue())

    def test_ps_hides_exited_sessions_until_asked(self):
        self._write("abcdef01", target="owner/gone")
        out = io.StringIO()
        with redirect_stdout(out):
            cmd_ps(self._args())
        self.assertIn("no gardener sessions running", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            cmd_ps(self._args(all=True))
        self.assertIn("owner/gone", out.getvalue())
        self.assertIn("exited", out.getvalue())

    def test_ps_quiet_prints_bare_ids(self):
        self._running(self._write("abcdef01"))
        out = io.StringIO()
        with redirect_stdout(out):
            cmd_ps(self._args(quiet=True))
        self.assertEqual(out.getvalue().strip(), "abcdef01")

    def test_stop_without_a_session_or_all_explains_both_options(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(cmd_stop(self._args()), 2)
        self.assertIn("--all", err.getvalue())
        self.assertIn("gardener ps", err.getvalue())

    def test_stop_resolves_by_prefix_and_reports_what_it_stopped(self):
        self._running(self._write("abcdef01", target="owner/live"))
        out, calls = io.StringIO(), []

        def fake_stop(session, **kwargs):
            calls.append(session.id)
            return sessions.StopResult(session=session, signalled=[session.pid],
                                       escalated=False, stopped=True)

        with patch("gardener.cli.sessions.stop", side_effect=fake_stop):
            with redirect_stdout(out):
                self.assertEqual(cmd_stop(self._args(session=["abc"])), 0)
        self.assertEqual(calls, ["abcdef01"])
        self.assertIn("stopped abcdef01", out.getvalue())

    def test_stop_passes_the_grace_period_through(self):
        self._running(self._write("abcdef01"))
        seen = {}

        def fake_stop(session, **kwargs):
            seen.update(kwargs)
            return sessions.StopResult(session=session, signalled=[], escalated=False, stopped=True)

        with patch("gardener.cli.sessions.stop", side_effect=fake_stop):
            with redirect_stdout(io.StringIO()):
                cmd_stop(self._args(session=["abcdef01"], time=42.0))
        self.assertEqual(seen.get("timeout"), 42.0)

    def test_stop_all_over_no_sessions_is_a_reported_no_op(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cmd_stop(self._args(all=True)), 0)
        self.assertIn("nothing to stop", out.getvalue())

    def test_stop_reports_failure_when_the_process_survives(self):
        self._running(self._write("abcdef01"))

        def fake_stop(session, **kwargs):
            return sessions.StopResult(session=session, signalled=[session.pid],
                                       escalated=True, stopped=False)

        err = io.StringIO()
        with patch("gardener.cli.sessions.stop", side_effect=fake_stop):
            with redirect_stderr(err):
                self.assertEqual(cmd_stop(self._args(session=["abcdef01"])), 1)
        self.assertIn("still running", err.getvalue())

    def test_kill_sends_the_named_signal_without_escalating(self):
        self._running(self._write("abcdef01"))
        seen = {}

        def fake_stop(session, **kwargs):
            seen.update(kwargs)
            return sessions.StopResult(session=session, signalled=[], escalated=False, stopped=True)

        with patch("gardener.cli.sessions.stop", side_effect=fake_stop):
            with redirect_stdout(io.StringIO()):
                cmd_kill(self._args(session=["abcdef01"], signal="INT"))
        self.assertEqual(seen.get("sig"), int(signal.SIGINT))
        self.assertFalse(seen.get("escalate"))

    def test_kill_rejects_an_unknown_signal_before_signalling_anything(self):
        self._running(self._write("abcdef01"))
        err = io.StringIO()
        with patch("gardener.cli.sessions.stop") as mock_stop:
            with redirect_stderr(err):
                self.assertEqual(cmd_kill(self._args(session=["abcdef01"], signal="NOPE")), 1)
        mock_stop.assert_not_called()
        self.assertIn("unknown signal", err.getvalue())



if __name__ == "__main__":
    unittest.main()
