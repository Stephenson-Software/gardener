"""dispatch.py's job is entirely about *what argv gets built* and *how the
result is interpreted* — never actually invoke `claude` here, mock
subprocess.run instead."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from gardener.dispatch import (
    ALLOWED_PERMISSION_MODES,
    FORBIDDEN_PERMISSION_MODE,
    MERGE_ALLOWED_TOOL,
    MODE_SPECS,
    TEND_BASE_ALLOWED_TOOLS,
    DispatchError,
    Mode,
    _build_invocation,
    is_device_global_failure,
    looks_like_auth_failure,
    looks_like_network_failure,
    looks_like_usage_limit,
    run_claude,
    tend_mode_spec,
)


class TestModeSpecs(unittest.TestCase):
    def test_bypass_permissions_never_configured_for_any_mode(self):
        for mode, spec in MODE_SPECS.items():
            self.assertNotEqual(
                spec.permission_mode, FORBIDDEN_PERMISSION_MODE,
                f"{mode} must never use {FORBIDDEN_PERMISSION_MODE}",
            )

    def test_every_configured_permission_mode_is_in_the_allow_list(self):
        for mode, spec in MODE_SPECS.items():
            self.assertIn(spec.permission_mode, ALLOWED_PERMISSION_MODES, mode)

    def test_report_mode_has_no_mutating_tool(self):
        spec = MODE_SPECS[Mode.REPORT]
        for mutating in ("Write", "Edit", "Bash", "NotebookEdit"):
            self.assertNotIn(mutating, spec.tools)

    def test_file_issue_mode_has_no_write_or_edit_tool(self):
        spec = MODE_SPECS[Mode.FILE_ISSUE]
        self.assertNotIn("Write", spec.tools)
        self.assertNotIn("Edit", spec.tools)

    def test_implement_and_file_issue_scope_bash_narrowly(self):
        for mode in (Mode.IMPLEMENT, Mode.FILE_ISSUE):
            spec = MODE_SPECS[mode]
            self.assertIn("Bash", spec.tools)
            self.assertTrue(spec.allowed_tools, f"{mode} must scope Bash via allowedTools")
            for pattern in spec.allowed_tools:
                if pattern.startswith("Bash("):
                    self.assertNotEqual(pattern, "Bash(*)")


class TestBuildInvocation(unittest.TestCase):
    def test_report_mode_argv_has_no_write_tools_and_uses_plan_permission(self):
        argv = _build_invocation(Mode.REPORT, "prompt text", [])
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        tools_value = argv[argv.index("--tools") + 1]
        self.assertNotIn("Write", tools_value.split(","))
        self.assertNotIn("Bash", tools_value.split(","))
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--allowedTools", argv)
        # Prompt must sit right after -p, not at the end — --add-dir is
        # variadic and would otherwise swallow a trailing prompt as one
        # more directory (a real bug this test guards against regressing).
        self.assertEqual(argv[argv.index("-p") + 1], "prompt text")

    def test_add_dir_does_not_swallow_the_prompt(self):
        argv = _build_invocation(Mode.REPORT, "prompt text", [Path("/a")])
        self.assertEqual(argv[argv.index("-p") + 1], "prompt text")
        self.assertEqual(argv[argv.index("--add-dir") + 1], "/a")
        # --add-dir's value must be the directory, not the prompt swallowed
        # in as an extra variadic item.
        self.assertNotEqual(argv[argv.index("--add-dir") + 1], "prompt text")

    def test_implement_mode_argv_scopes_bash_and_uses_default_permission(self):
        argv = _build_invocation(Mode.IMPLEMENT, "prompt", [])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertIn("Bash(git *)", allowed)
        self.assertNotIn("bypassPermissions", argv)

    def test_add_dirs_are_passed_through(self):
        argv = _build_invocation(Mode.REPORT, "prompt", [Path("/a"), Path("/b")])
        self.assertEqual(argv.count("--add-dir"), 2)
        self.assertIn("/a", argv)
        self.assertIn("/b", argv)

    def test_model_override_passed_through(self):
        argv = _build_invocation(Mode.REPORT, "prompt", [], model="opus")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")

    def test_never_constructs_bypass_permissions_regardless_of_mode(self):
        for mode in Mode:
            if mode not in MODE_SPECS:
                # Mode.TEND has no fixed MODE_SPECS entry — its spec varies
                # per invocation and must be built with tend_mode_spec()
                # (see TestTendModeSpec below).
                continue
            argv = _build_invocation(mode, "prompt", [])
            self.assertNotIn("bypassPermissions", argv)


class TestCreateDevLoopModeSpec(unittest.TestCase):
    def test_has_write_but_no_bash_gh_pr_merge(self):
        spec = MODE_SPECS[Mode.CREATE_DEV_LOOP]
        self.assertIn("Write", spec.tools)
        self.assertIn("Bash", spec.tools)
        # gh pr list (read-only exploration, per create-dev-loop's own Step
        # 2) is fine here — merge is the one gh pr subcommand this mode
        # must never be able to reach.
        for pattern in spec.allowed_tools:
            self.assertFalse(pattern.startswith("Bash(gh pr merge"), pattern)
        self.assertNotEqual(spec.permission_mode, FORBIDDEN_PERMISSION_MODE)
        self.assertIn(spec.permission_mode, ALLOWED_PERMISSION_MODES)

    def test_argv_builds_without_a_mode_spec_override(self):
        argv = _build_invocation(Mode.CREATE_DEV_LOOP, "prompt", [])
        self.assertIn("Write", argv[argv.index("--tools") + 1].split(","))


class TestTendModeSpec(unittest.TestCase):
    def test_merge_tool_absent_when_not_eligible(self):
        spec = tend_mode_spec(allow_merge_eligible=False)
        self.assertNotIn(MERGE_ALLOWED_TOOL, spec.allowed_tools)

    def test_merge_tool_present_only_when_eligible(self):
        spec = tend_mode_spec(allow_merge_eligible=True)
        self.assertIn(MERGE_ALLOWED_TOOL, spec.allowed_tools)

    def test_base_allowed_tools_never_include_bare_gh_pr_wildcard(self):
        # A bare "Bash(gh pr *)" would structurally re-permit merge via
        # pattern matching regardless of eligibility (confirmed live — see
        # dispatch.py's module docstring point 4) — tend must never use it.
        self.assertNotIn("Bash(gh pr *)", TEND_BASE_ALLOWED_TOOLS)

    def test_merge_pattern_never_leaks_into_base_list(self):
        self.assertNotIn(MERGE_ALLOWED_TOOL, TEND_BASE_ALLOWED_TOOLS)

    def test_no_askuserquestion_agent_or_schedulewakeup_in_tools(self):
        for eligible in (True, False):
            spec = tend_mode_spec(eligible)
            for excluded in ("AskUserQuestion", "Agent", "ScheduleWakeup"):
                self.assertNotIn(excluded, spec.tools)

    def test_never_uses_bypass_permissions(self):
        for eligible in (True, False):
            spec = tend_mode_spec(eligible)
            self.assertNotEqual(spec.permission_mode, FORBIDDEN_PERMISSION_MODE)
            self.assertIn(spec.permission_mode, ALLOWED_PERMISSION_MODES)

    def test_argv_reflects_eligibility_via_mode_spec_override(self):
        argv_ineligible = _build_invocation(
            Mode.TEND, "prompt", [], mode_spec=tend_mode_spec(False)
        )
        argv_eligible = _build_invocation(
            Mode.TEND, "prompt", [], mode_spec=tend_mode_spec(True)
        )
        self.assertNotIn(MERGE_ALLOWED_TOOL, argv_ineligible[argv_ineligible.index("--allowedTools") + 1])
        self.assertIn(MERGE_ALLOWED_TOOL, argv_eligible[argv_eligible.index("--allowedTools") + 1])

    def test_tend_mode_requires_explicit_mode_spec(self):
        with self.assertRaises(DispatchError):
            run_claude(Mode.TEND, "prompt", Path("/tmp"))


def _fake_completed(stdout_obj, returncode=0):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=json.dumps(stdout_obj), stderr=""
    )


class TestRunClaude(unittest.TestCase):
    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_successful_run_parses_json_result(self, mock_run, _which):
        mock_run.return_value = _fake_completed({
            "result": "## Gap checklist\nGARDENER_SUMMARY: 2 gaps found",
            "is_error": False,
            "total_cost_usd": 0.42,
            "session_id": "sess-1",
            "permission_denials": [],
        })
        result = run_claude(Mode.REPORT, "prompt", Path("/tmp"))
        self.assertTrue(result.ok)
        self.assertIn("GARDENER_SUMMARY", result.result_text)
        self.assertEqual(result.cost_usd, 0.42)
        self.assertEqual(result.session_id, "sess-1")
        self.assertEqual(result.permission_denials, [])

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_is_error_flag_makes_result_not_ok(self, mock_run, _which):
        mock_run.return_value = _fake_completed({
            "result": "something went wrong",
            "is_error": True,
        })
        result = run_claude(Mode.REPORT, "prompt", Path("/tmp"))
        self.assertFalse(result.ok)
        self.assertTrue(result.is_error)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_permission_denials_surfaced_in_result(self, mock_run, _which):
        mock_run.return_value = _fake_completed({
            "result": "done",
            "is_error": False,
            "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}],
        })
        result = run_claude(Mode.IMPLEMENT, "prompt", Path("/tmp"))
        self.assertEqual(len(result.permission_denials), 1)
        self.assertEqual(result.permission_denials[0]["tool_name"], "Bash")

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_unparseable_stdout_is_reported_as_not_ok(self, mock_run, _which):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="not json", stderr=""
        )
        result = run_claude(Mode.REPORT, "prompt", Path("/tmp"))
        self.assertFalse(result.ok)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5))
    def test_timeout_is_reported_not_raised(self, _mock_run, _which):
        result = run_claude(Mode.REPORT, "prompt", Path("/tmp"), timeout=5)
        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)

    @patch("gardener.dispatch.shutil.which", return_value=None)
    def test_missing_claude_binary_raises_dispatch_error(self, _which):
        with self.assertRaises(DispatchError):
            run_claude(Mode.REPORT, "prompt", Path("/tmp"))

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    @patch("gardener.dispatch.start_transcript_watcher")
    def test_starts_transcript_watcher_with_the_dispatch_cwd_before_subprocess_run(
        self, mock_watcher, mock_run, _which
    ):
        # The watcher must be started before subprocess.run blocks, with the
        # same cwd the dispatch itself uses — see transcript.py and
        # dispatch.py's "Live transcript visibility" docstring section. This
        # never touches a real filesystem or thread: start_transcript_watcher
        # itself is mocked out entirely, this only checks the wiring.
        mock_run.return_value = _fake_completed({"result": "ok", "is_error": False})
        cwd = Path("/some/repo/checkout")
        run_claude(Mode.REPORT, "prompt", cwd)
        mock_watcher.assert_called_once()
        args, kwargs = mock_watcher.call_args
        self.assertEqual(args[0], cwd)
        self.assertIn("after", kwargs)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5))
    @patch("gardener.dispatch.start_transcript_watcher")
    def test_transcript_watcher_still_started_even_on_timeout(self, mock_watcher, _mock_run, _which):
        # The watcher is started unconditionally before the blocking call —
        # it doesn't know or care how the dispatch turns out.
        run_claude(Mode.REPORT, "prompt", Path("/tmp"), timeout=5)
        mock_watcher.assert_called_once()


# The exact text observed on 2026-07-24 when twelve of fifteen overnight
# repos failed in under a minute — see AUTH_FAILURE_MARKERS. Kept verbatim
# here rather than paraphrased so these tests stay anchored to a real
# failure rather than to the matcher's own wording.
REAL_AUTH_FAILURE_TEXT = "Failed to authenticate: OAuth session expired and could not be refreshed"


class TestLooksLikeAuthFailure(unittest.TestCase):
    def test_matches_the_real_observed_failure_text(self):
        self.assertTrue(looks_like_auth_failure(REAL_AUTH_FAILURE_TEXT, ""))

    def test_matches_regardless_of_which_stream_carried_it(self):
        self.assertTrue(looks_like_auth_failure("", REAL_AUTH_FAILURE_TEXT))

    def test_match_is_case_insensitive(self):
        self.assertTrue(looks_like_auth_failure(REAL_AUTH_FAILURE_TEXT.upper(), ""))

    def test_ordinary_failure_text_is_not_an_auth_failure(self):
        self.assertFalse(
            looks_like_auth_failure("tests failed: 3 assertions in test_foo.py", "npm ERR!")
        )


# The exact texts observed in `gardener status` history for the other two
# device-global classes, kept verbatim for the same reason as the auth one
# above. The usage-limit string burned 20 of the garden's 32 repos in four
# minutes on 2026-07-25 (and 15 more on 2026-07-20); the two GitHub strings
# burned 17 across 2026-07-21 and 2026-07-25. None of them were classified
# at the time, so the resume cursor advanced past every affected repo.
REAL_USAGE_LIMIT_TEXT = "You've hit your session limit \u00b7 resets 12am (UTC)"
REAL_NETWORK_TEXT = (
    "could not determine default branch for owner/repo: error connecting to "
    "api.github.com\ncheck your internet connection or https://githubstatus.com"
)
REAL_GRAPHQL_EOF_TEXT = (
    'could not determine default branch for owner/repo: Post '
    '"https://api.github.com/graphql": unexpected EOF'
)


class TestLooksLikeUsageLimit(unittest.TestCase):
    def test_matches_the_real_observed_failure_text(self):
        self.assertTrue(looks_like_usage_limit(REAL_USAGE_LIMIT_TEXT, ""))

    def test_match_is_case_insensitive_and_stream_agnostic(self):
        self.assertTrue(looks_like_usage_limit("", REAL_USAGE_LIMIT_TEXT.upper()))

    def test_a_usage_limit_is_not_misread_as_an_auth_failure(self):
        """The distinction has teeth: an auth failure is retried in-process,
        a usage limit must not be (its reset is hours away)."""
        self.assertFalse(looks_like_auth_failure(REAL_USAGE_LIMIT_TEXT, ""))

    def test_ordinary_failure_text_is_not_a_usage_limit(self):
        self.assertFalse(looks_like_usage_limit("tests failed: 3 assertions", ""))


class TestLooksLikeNetworkFailure(unittest.TestCase):
    def test_matches_the_real_connectivity_text(self):
        self.assertTrue(looks_like_network_failure(REAL_NETWORK_TEXT, ""))

    def test_matches_the_real_graphql_eof_text(self):
        self.assertTrue(looks_like_network_failure(REAL_GRAPHQL_EOF_TEXT, ""))

    def test_ordinary_failure_text_is_not_a_network_failure(self):
        self.assertFalse(looks_like_network_failure("tests failed: 3 assertions", ""))


class TestIsDeviceGlobalFailure(unittest.TestCase):
    """The single predicate `cmd_overnight` aborts a batch on — it must
    cover all three real classes, since missing any one of them costs a
    whole overnight cycle."""

    def test_covers_every_real_observed_failure(self):
        for text in (
            REAL_AUTH_FAILURE_TEXT,
            REAL_USAGE_LIMIT_TEXT,
            REAL_NETWORK_TEXT,
            REAL_GRAPHQL_EOF_TEXT,
        ):
            with self.subTest(text=text[:40]):
                self.assertTrue(is_device_global_failure(text))

    def test_a_genuine_per_repo_failure_is_not_device_global(self):
        """The costly false positive: if an ordinary repo failure tripped
        this, one bad repo would abort the whole garden every night."""
        for text in (
            "tests failed: 3 assertions in test_foo.py",
            "PR #12 opened; 2 issues filed",
            "build failed: compilation error in Main.java",
        ):
            with self.subTest(text=text[:40]):
                self.assertFalse(is_device_global_failure(text))


class TestAuthFailureRetry(unittest.TestCase):
    """`run_claude` retries an auth failure and nothing else. `sleep_fn` is
    always injected here — these tests must never actually sleep."""

    def setUp(self):
        self.slept = []

    def _auth_failure(self):
        return _fake_completed(
            {"result": REAL_AUTH_FAILURE_TEXT, "is_error": True, "total_cost_usd": 0.0},
            returncode=1,
        )

    def _usage_limit_failure(self):
        return _fake_completed(
            {"result": REAL_USAGE_LIMIT_TEXT, "is_error": True, "total_cost_usd": 0.0},
            returncode=1,
        )

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_usage_limit_is_flagged_blocked_but_never_retried(self, mock_run, _which):
        """A usage window resets at a wall-clock hour that is routinely
        hours out, so the in-process backoff cannot outlast it — retrying
        would only burn the night's budget asleep. It must still be
        `blocked`, which is what actually stops the batch."""
        mock_run.return_value = self._usage_limit_failure()
        result = run_claude(
            Mode.REPORT, "prompt", Path("/tmp"),
            auth_backoff_seconds=(1, 2, 3), sleep_fn=self.slept.append,
        )
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(self.slept, [])
        self.assertTrue(result.blocked)
        self.assertFalse(result.auth_failed)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_auth_failure_is_also_flagged_blocked(self, mock_run, _which):
        mock_run.return_value = self._auth_failure()
        result = run_claude(
            Mode.REPORT, "prompt", Path("/tmp"),
            auth_backoff_seconds=(), sleep_fn=self.slept.append,
        )
        self.assertTrue(result.blocked)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_an_ordinary_failure_is_not_blocked(self, mock_run, _which):
        mock_run.return_value = _fake_completed(
            {"result": "tests failed: 3 assertions", "is_error": True}, returncode=1,
        )
        result = run_claude(
            Mode.REPORT, "prompt", Path("/tmp"),
            auth_backoff_seconds=(), sleep_fn=self.slept.append,
        )
        self.assertFalse(result.blocked)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_auth_failure_is_flagged_on_the_result(self, mock_run, _which):
        mock_run.return_value = self._auth_failure()
        result = run_claude(
            Mode.REPORT, "prompt", Path("/tmp"),
            auth_backoff_seconds=(), sleep_fn=self.slept.append,
        )
        self.assertTrue(result.auth_failed)
        self.assertFalse(result.ok)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_retries_until_backoff_is_exhausted_then_gives_up(self, mock_run, _which):
        mock_run.return_value = self._auth_failure()
        result = run_claude(
            Mode.REPORT, "prompt", Path("/tmp"),
            auth_backoff_seconds=(1, 2, 3), sleep_fn=self.slept.append,
        )
        # 3 backoff entries => 4 total attempts, sleeping between each pair.
        self.assertEqual(mock_run.call_count, 4)
        self.assertEqual(self.slept, [1, 2, 3])
        self.assertTrue(result.auth_failed)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_stops_retrying_as_soon_as_auth_recovers(self, mock_run, _which):
        mock_run.side_effect = [
            self._auth_failure(),
            self._auth_failure(),
            _fake_completed({"result": "GARDENER_SUMMARY: done", "is_error": False}),
        ]
        result = run_claude(
            Mode.REPORT, "prompt", Path("/tmp"),
            auth_backoff_seconds=(1, 2, 3), sleep_fn=self.slept.append,
        )
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(self.slept, [1, 2])
        self.assertTrue(result.ok)
        self.assertFalse(result.auth_failed)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_ordinary_failure_is_never_retried(self, mock_run, _which):
        # The important half of the policy: a tend cycle that genuinely
        # failed has already mutated its branch, so a blind second run is
        # exactly the wrong response — only auth failures are retried.
        mock_run.return_value = _fake_completed(
            {"result": "the build broke", "is_error": True}, returncode=1
        )
        result = run_claude(
            Mode.REPORT, "prompt", Path("/tmp"),
            auth_backoff_seconds=(1, 2, 3), sleep_fn=self.slept.append,
        )
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(self.slept, [])
        self.assertFalse(result.auth_failed)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=5))
    def test_timeout_is_not_treated_as_an_auth_failure(self, mock_run, _which):
        result = run_claude(
            Mode.REPORT, "prompt", Path("/tmp"), timeout=5,
            auth_backoff_seconds=(1, 2, 3), sleep_fn=self.slept.append,
        )
        self.assertTrue(result.timed_out)
        self.assertFalse(result.auth_failed)
        self.assertEqual(mock_run.call_count, 1)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_successful_run_quoting_an_auth_string_is_not_an_auth_failure(self, mock_run, _which):
        # A repo whose own code or docs mention these strings must not have
        # its successful tend reclassified — hence the `not ok` guard.
        mock_run.return_value = _fake_completed({
            "result": f"Documented the '{REAL_AUTH_FAILURE_TEXT}' error path",
            "is_error": False,
        })
        result = run_claude(
            Mode.REPORT, "prompt", Path("/tmp"),
            auth_backoff_seconds=(1,), sleep_fn=self.slept.append,
        )
        self.assertTrue(result.ok)
        self.assertFalse(result.auth_failed)
        self.assertEqual(mock_run.call_count, 1)

    @patch("gardener.dispatch.shutil.which", return_value="/usr/bin/claude")
    @patch("gardener.dispatch.subprocess.run")
    def test_auth_failure_detected_without_a_json_envelope(self, mock_run, _which):
        # claude dying before it emits any JSON — the message is only on
        # stderr in that case, which is still an auth failure.
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout="", stderr=REAL_AUTH_FAILURE_TEXT
        )
        result = run_claude(
            Mode.REPORT, "prompt", Path("/tmp"),
            auth_backoff_seconds=(1,), sleep_fn=self.slept.append,
        )
        self.assertTrue(result.auth_failed)
        self.assertEqual(mock_run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
