"""Offline tier: the real `gardener align` CLI end to end, with stand-in
agents instead of a model. No network, no model, no spend; runs in CI.

What it pins, through the real process boundary rather than mocks:

- the command-harness contract (prompt on stdin, mode/spec/model/timeout
  in the environment, cwd is the target clone, envelope parsing);
- report mode's after-the-fact check that the clone was left unchanged;
- the refusal of write modes on an unscoped command harness;
- the Claude Code harness's real argv (via a fake `claude`), including that
  report mode is read-only tools under plan permissions and that
  `bypassPermissions` never appears;
- the conventions subdirectory reaching the harness as its `add_dirs`.
"""
import json
import sys
import time
import unittest

from integration.support import STUB_AGENT, IntegrationCase, Sandbox, pid_alive


class CommandHarnessContract(IntegrationCase):
    def setUp(self):
        self.box = Sandbox(self)

    def test_report_envelope_is_recorded_as_a_successful_run(self):
        self.box.stub_harness("ok-json")
        self.box.settings["GARDENER_HARNESS_MODEL"] = "stub-model"
        self.box.settings["GARDENER_ALIGN_TIMEOUT"] = "321"
        r = self.box.align()
        self.assertEqual(r.code, 0, r.describe())
        self.assertSafeReport(r)
        self.assertIn("GARDENER_SUMMARY: 1 gap(s)", r.stdout)
        self.assertEqual(r.run["outcome"], "report")
        self.assertEqual(r.run["gap_summary"], "1 gap(s) found across 1 section(s) — README lacks a test command")
        self.assertEqual(r.run["claude_session_id"], "stub-session")

        rec = r.record
        self.assertEqual(len(r.records), 1)
        self.assertEqual(rec["cwd"], str(r.clone))
        self.assertIn(self.box.repo, rec["stdin"])
        self.assertEqual(rec["env"]["GARDENER_HARNESS_MODE"], "report")
        self.assertEqual(rec["env"]["GARDENER_HARNESS_MODEL"], "stub-model")
        self.assertEqual(rec["env"]["GARDENER_HARNESS_TIMEOUT"], "321")
        spec = json.loads(rec["env"]["GARDENER_HARNESS_SPEC"])
        self.assertEqual(spec["mode"], "report")
        self.assertEqual(spec["permission_mode"], "plan")
        self.assertEqual(sorted(spec["tools"]), ["Glob", "Grep", "Read"])
        self.assertEqual(spec["allowed_tools"], [])
        self.assertFalse(spec["merge_allowed"])
        self.assertEqual(len(spec["add_dirs"]), 1)
        self.assertTrue(spec["add_dirs"][0].endswith("conventions"), spec["add_dirs"])

    def test_plain_text_output_is_a_valid_answer(self):
        self.box.stub_harness("ok-text")
        r = self.box.align()
        self.assertEqual(r.code, 0, r.describe())
        self.assertSafeReport(r)
        self.assertIsNone(r.envelope)
        self.assertEqual(r.run["outcome"], "report")

    def test_unset_model_is_not_passed(self):
        self.box.stub_harness("ok-json")
        r = self.box.align()
        self.assertEqual(r.code, 0, r.describe())
        self.assertNotIn("GARDENER_HARNESS_MODEL", r.record["env"])

    def test_error_envelope_fails_the_run(self):
        self.box.stub_harness("error-json")
        r = self.box.align()
        self.assertEqual(r.code, 1, r.describe())
        self.assertSafeReport(r)
        self.assertEqual(r.run["outcome"], "error")
        self.assertIn("stub agent failed", r.run["gap_summary"])

    def test_blocked_envelope_fails_the_run(self):
        self.box.stub_harness("blocked-json")
        r = self.box.align()
        self.assertEqual(r.code, 1, r.describe())
        self.assertTrue(r.envelope["blocked"])
        self.assertEqual(r.run["outcome"], "error")

    def test_nonzero_exit_fails_the_run(self):
        self.box.stub_harness("exit-nonzero")
        r = self.box.align()
        self.assertEqual(r.code, 1, r.describe())
        self.assertSafeReport(r)
        self.assertEqual(r.run["outcome"], "error")

    def test_timeout_fails_the_run(self):
        self.box.stub_harness("sleep")
        self.box.settings["GARDENER_ALIGN_TIMEOUT"] = "3"
        r = self.box.align(timeout=120)
        self.assertEqual(r.code, 1, r.describe())
        self.assertIn("timed out after 3s", r.stderr)
        self.assertLess(r.seconds, 60)
        # The harness's own child must die with it, not run on orphaned.
        child = int((self.box.records / "sleep-child.pid").read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and pid_alive(child):
            time.sleep(0.1)
        self.assertFalse(pid_alive(child), "the harness's child outlived gardener's timeout")

    def test_signals_to_gardener_kill_the_agent(self):
        """The agent runs in its own session, so a Ctrl-C at the terminal or
        a SIGTERM to gardener alone never reaches it directly; gardener must
        take it down on the way out."""
        import signal

        for signum in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum.name):
                box = Sandbox(self)
                box.stub_harness("sleep")
                proc = box.start_align()
                pid_file = box.records / "sleep-child.pid"
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline and not pid_file.exists():
                    time.sleep(0.1)
                self.assertTrue(pid_file.exists(), "the agent never started")
                child = int(pid_file.read_text())
                proc.send_signal(signum)
                self.assertNotEqual(proc.wait(timeout=30), 0)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and pid_alive(child):
                    time.sleep(0.1)
                self.assertFalse(pid_alive(child), f"the agent outlived {signum.name} to gardener")

    def test_missing_command_setting_is_a_setup_error(self):
        self.box.settings["GARDENER_HARNESS"] = "command"
        r = self.box.align()
        self.assertEqual(r.code, 2, r.describe())
        self.assertIn("GARDENER_HARNESS_COMMAND", r.stderr)
        self.assertEqual(r.records, [])


class ReportModeTamperCheck(IntegrationCase):
    """The command harness has no structural read-only ceiling, so gardener
    checks HEAD and `git status` after the run. A stub that changes the
    clone and claims success must still fail."""

    def tamper(self, behaviour: str):
        box = Sandbox(self)
        box.stub_harness(behaviour)
        r = box.align()
        self.assertEqual(r.code, 1, r.describe())
        self.assertFalse(r.envelope["is_error"], "the stub claimed success")
        self.assertIn("report-mode dispatch modified the target clone", r.stderr + r.run["gap_summary"]
                      if r.run else r.stderr)
        self.assertEqual(r.run["outcome"], "error")
        return r

    def test_untracked_file_fails(self):
        self.assertIn("stub-untracked.txt", self.tamper("write-file").clone_porcelain)

    def test_edited_tracked_file_fails(self):
        self.assertIn("README.md", self.tamper("edit-file").clone_porcelain)

    def test_commit_fails(self):
        r = self.tamper("commit")
        self.assertNotEqual(r.clone_head_before, r.clone_head_after)


class WriteModeGate(IntegrationCase):
    def test_write_modes_are_refused_before_dispatch(self):
        for flag in ("--implement", "--file-issue"):
            with self.subTest(flag=flag):
                box = Sandbox(self)
                box.stub_harness("ok-json")
                r = box.align(flag)
                self.assertNotEqual(r.code, 0, r.describe())
                self.assertIn("refusing", r.stderr)
                self.assertIn("GARDENER_HARNESS_ALLOW_UNSCOPED", r.stderr)
                self.assertEqual(r.records, [], "the harness ran despite the refusal")

    def test_opt_in_passes_the_write_mode_through(self):
        box = Sandbox(self)
        box.stub_harness("ok-json")
        box.settings["GARDENER_HARNESS_ALLOW_UNSCOPED"] = "implement"
        r = box.align("--implement")
        self.assertEqual(len(r.records), 1, r.describe())
        self.assertEqual(r.record["env"]["GARDENER_HARNESS_MODE"], "implement")
        spec = json.loads(r.record["env"]["GARDENER_HARNESS_SPEC"])
        self.assertNotEqual(spec["permission_mode"], "bypassPermissions")
        self.assertFalse(spec["merge_allowed"])

    def test_opt_in_is_per_mode(self):
        box = Sandbox(self)
        box.stub_harness("ok-json")
        box.settings["GARDENER_HARNESS_ALLOW_UNSCOPED"] = "implement"
        r = box.align("--file-issue")
        self.assertNotEqual(r.code, 0, r.describe())
        self.assertEqual(r.records, [])


class ConventionsSubdir(IntegrationCase):
    def test_subdir_becomes_the_harness_add_dir(self):
        box = Sandbox(self)
        box.stub_harness("ok-json")
        box.settings["GARDENER_CONVENTIONS_SUBDIR"] = "compact"
        r = box.align()
        self.assertEqual(r.code, 0, r.describe())
        spec = json.loads(r.record["env"]["GARDENER_HARNESS_SPEC"])
        self.assertTrue(spec["add_dirs"][0].endswith("conventions/compact"), spec["add_dirs"])
        self.assertIn("conventions/compact/ALIGNMENT_CHECKLIST.md", r.record["stdin"])

    def test_flag_overrides_setting(self):
        box = Sandbox(self)
        box.stub_harness("ok-json")
        box.settings["GARDENER_CONVENTIONS_SUBDIR"] = "does-not-exist"
        r = box.align("--conventions-subdir", "compact")
        self.assertEqual(r.code, 0, r.describe())

    def test_missing_subdir_is_refused_before_dispatch(self):
        box = Sandbox(self)
        box.stub_harness("ok-json")
        r = box.align("--conventions-subdir", "nope")
        self.assertNotEqual(r.code, 0, r.describe())
        self.assertEqual(r.records, [])

    def test_escaping_subdir_is_refused(self):
        box = Sandbox(self)
        box.stub_harness("ok-json")
        r = box.align("--conventions-subdir", "../..")
        self.assertNotEqual(r.code, 0, r.describe())
        self.assertEqual(r.records, [])


class ClaudeCodeHarnessArgv(IntegrationCase):
    """The default harness, through a fake `claude` that records its argv.
    This is the one place the real `claude` invocation gardener builds is
    observed end to end without spending anything."""

    def run_fake(self, *args):
        box = Sandbox(self)
        box.settings["GARDENER_CLAUDE_BIN"] = box.fake_claude()
        r = box.align(*args)
        recs = [rec for rec in (json.loads(p.read_text()) for p in sorted(box.records.glob("claude-*.json")))]
        return r, recs

    def test_report_mode_is_read_only(self):
        r, recs = self.run_fake()
        self.assertEqual(r.code, 0, r.describe())
        self.assertSafeReport(r)
        self.assertEqual(len(recs), 1)
        argv = recs[0]["argv"]
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")
        self.assertIn("--add-dir", argv)
        self.assertNotIn("bypassPermissions", " ".join(argv))
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(recs[0]["cwd"], str(r.clone))
        self.assertEqual(r.run["outcome"], "report")
        self.assertEqual(r.run["cost_usd"], 0.01)

    def test_timeout_kills_claudes_children(self):
        box = Sandbox(self)
        box.settings["GARDENER_CLAUDE_BIN"] = box.fake_claude()
        box.settings["GARDENER_ALIGN_TIMEOUT"] = "3"
        box.extra_env["GARDENER_IT_CLAUDE_BEHAVIOUR"] = "sleep"
        r = box.align(timeout=120)
        self.assertEqual(r.code, 1, r.describe())
        self.assertIn("timed out after 3s", r.stderr)
        child = int((box.records / "sleep-child.pid").read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and pid_alive(child):
            time.sleep(0.1)
        self.assertFalse(pid_alive(child), "claude's child outlived gardener's timeout (gardener#187)")

    def test_no_mode_ever_requests_bypass(self):
        for flag in ("--implement", "--file-issue"):
            with self.subTest(flag=flag):
                r, recs = self.run_fake(flag)
                self.assertEqual(len(recs), 1, r.describe())
                joined = " ".join(recs[0]["argv"])
                self.assertNotIn("bypassPermissions", joined)
                self.assertNotIn("dangerously", joined)


class Hermeticity(IntegrationCase):
    def test_stub_agent_entrypoint_exists(self):
        self.assertTrue(STUB_AGENT.exists())
        self.assertTrue(sys.executable)


if __name__ == "__main__":
    unittest.main()
