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
        argv = _build_invocation(Mode.REPORT, "prompt text", Path("/tmp"), [])
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
        argv = _build_invocation(Mode.REPORT, "prompt text", Path("/tmp"), [Path("/a")])
        self.assertEqual(argv[argv.index("-p") + 1], "prompt text")
        self.assertEqual(argv[argv.index("--add-dir") + 1], "/a")
        # --add-dir's value must be the directory, not the prompt swallowed
        # in as an extra variadic item.
        self.assertNotEqual(argv[argv.index("--add-dir") + 1], "prompt text")

    def test_implement_mode_argv_scopes_bash_and_uses_default_permission(self):
        argv = _build_invocation(Mode.IMPLEMENT, "prompt", Path("/tmp"), [])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        allowed = argv[argv.index("--allowedTools") + 1]
        self.assertIn("Bash(git *)", allowed)
        self.assertNotIn("bypassPermissions", argv)

    def test_add_dirs_are_passed_through(self):
        argv = _build_invocation(Mode.REPORT, "prompt", Path("/tmp"), [Path("/a"), Path("/b")])
        self.assertEqual(argv.count("--add-dir"), 2)
        self.assertIn("/a", argv)
        self.assertIn("/b", argv)

    def test_model_override_passed_through(self):
        argv = _build_invocation(Mode.REPORT, "prompt", Path("/tmp"), [], model="opus")
        self.assertIn("--model", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")

    def test_never_constructs_bypass_permissions_regardless_of_mode(self):
        for mode in Mode:
            if mode not in MODE_SPECS:
                # Mode.TEND has no fixed MODE_SPECS entry — its spec varies
                # per invocation and must be built with tend_mode_spec()
                # (see TestTendModeSpec below).
                continue
            argv = _build_invocation(mode, "prompt", Path("/tmp"), [])
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
        argv = _build_invocation(Mode.CREATE_DEV_LOOP, "prompt", Path("/tmp"), [])
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
            Mode.TEND, "prompt", Path("/tmp"), [], mode_spec=tend_mode_spec(False)
        )
        argv_eligible = _build_invocation(
            Mode.TEND, "prompt", Path("/tmp"), [], mode_spec=tend_mode_spec(True)
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


if __name__ == "__main__":
    unittest.main()
