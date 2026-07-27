"""Slug derivation and prompt-building are pure functions — no subprocess,
no filesystem beyond what a real skill install would leave on disk (which
`has_dev_loop_skill` reads, tested here against a tmp dir so it never
depends on this machine's actual ~/local-skills)."""
import unittest
from pathlib import Path
from unittest.mock import patch

from gardener import dev_loop
from gardener.dispatch import MODE_SPECS, Mode


class TestSlugify(unittest.TestCase):
    def test_simple_name(self):
        self.assertEqual(dev_loop.slugify_repo_name("example-org/gateway"), "gateway")

    def test_lowercases_and_hyphenates(self):
        self.assertEqual(
            dev_loop.slugify_repo_name("example-org/Calculator-GUI-Using-SDL"),
            "calculator-gui-using-sdl",
        )

    def test_collapses_non_alnum_runs(self):
        self.assertEqual(dev_loop.slugify_repo_name("owner/My_Repo.Name"), "my-repo-name")

    def test_strips_leading_trailing_separators(self):
        self.assertEqual(dev_loop.slugify_repo_name("owner/-weird-.name-"), "weird-name")

    def test_empty_name_raises(self):
        with self.assertRaises(ValueError):
            dev_loop.slugify_repo_name("owner/---")

    def test_skill_slug_appends_dev_loop_suffix(self):
        self.assertEqual(dev_loop.skill_slug("example-org/gateway"), "gateway-dev-loop")


class TestHasDevLoopSkill(unittest.TestCase):
    def test_missing_command_path_is_false(self):
        with patch.object(dev_loop, "COMMANDS_DIR", Path("/nonexistent/path/for/testing")):
            self.assertFalse(dev_loop.has_dev_loop_skill("some-dev-loop"))

    def test_existing_file_is_true(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            commands_dir = Path(td) / "commands"
            commands_dir.mkdir()
            (commands_dir / "foo-dev-loop.md").write_text("# foo-dev-loop\n")
            with patch.object(dev_loop, "COMMANDS_DIR", commands_dir):
                self.assertTrue(dev_loop.has_dev_loop_skill("foo-dev-loop"))

    def test_broken_symlink_is_false(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            commands_dir = Path(td) / "commands"
            commands_dir.mkdir()
            (commands_dir / "foo-dev-loop.md").symlink_to(Path(td) / "does-not-exist.md")
            with patch.object(dev_loop, "COMMANDS_DIR", commands_dir):
                self.assertFalse(dev_loop.has_dev_loop_skill("foo-dev-loop"))


class TestStep6Unreachable(unittest.TestCase):
    """See issue #12: create-dev-loop's Step 6 (`gh repo create`) was
    structurally denied by MODE_SPECS[Mode.CREATE_DEV_LOOP] until
    2026-07-19, when `Bash(gh repo create *)` (plus `gh api user`/`gh label
    create`) was deliberately granted — see dispatch.py's MODE_SPECS
    comment. `step6_unreachable()` itself still checks this live rather
    than assuming, so it correctly flips back to True if that grant is ever
    withdrawn — this class covers both directions."""

    def test_false_against_the_real_mode_spec(self):
        self.assertFalse(dev_loop.step6_unreachable())

    def test_true_if_gh_repo_create_is_withdrawn(self):
        from gardener.dispatch import MODE_SPECS, Mode, ModeSpec

        real_spec = MODE_SPECS[Mode.CREATE_DEV_LOOP]
        narrowed_spec = ModeSpec(
            tools=real_spec.tools,
            permission_mode=real_spec.permission_mode,
            allowed_tools=tuple(
                p for p in real_spec.allowed_tools if not p.startswith("Bash(gh repo create")
            ),
        )
        with patch.dict(MODE_SPECS, {Mode.CREATE_DEV_LOOP: narrowed_spec}):
            self.assertTrue(dev_loop.step6_unreachable())


class TestPromptBuilding(unittest.TestCase):
    def test_create_prompt_specifies_exact_slug_and_forbids_target_repo_writes(self):
        prompt = dev_loop.build_create_dev_loop_prompt(
            "owner/name", "name-dev-loop", Path("/tmp/target")
        )
        self.assertIn("name-dev-loop", prompt)
        self.assertIn("/create-dev-loop", prompt)
        self.assertIn("target repo checkout", prompt)
        self.assertIn("nowhere else", prompt)
        self.assertIn("GARDENER_SUMMARY", prompt)

    def test_create_prompt_does_not_prescribe_a_command_its_mode_lacks(self):
        # The review-posting instruction names `gh pr comment`, which
        # tend_mode_spec() grants and MODE_SPECS[Mode.CREATE_DEV_LOOP] does
        # not. It therefore belongs in build_tend_prompt's per-dispatch
        # block, never in the preamble both prompts share — telling a
        # create-dev-loop run to reach for a denied command is the exact
        # wasted-turns failure that instruction exists to prevent.
        prompt = dev_loop.build_create_dev_loop_prompt(
            "owner/name", "name-dev-loop", Path("/tmp/target")
        )
        granted = MODE_SPECS[Mode.CREATE_DEV_LOOP].allowed_tools
        self.assertNotIn("Bash(gh pr comment *)", granted)
        self.assertNotIn("gh pr comment", prompt)

    def test_create_prompt_tells_dispatched_session_to_perform_step_6(self):
        prompt = dev_loop.build_create_dev_loop_prompt(
            "owner/name", "name-dev-loop", Path("/tmp/target")
        )
        self.assertIn("Step 6", prompt)
        self.assertIn("gh repo create", prompt)
        self.assertIn("gh label create", prompt)
        self.assertIn("Perform", prompt)
        self.assertIn("do not attempt `gh repo delete`", prompt)

    def test_tend_prompt_includes_headless_safety_preamble(self):
        prompt = dev_loop.build_tend_prompt("owner/name", "name-dev-loop", Path("/tmp/t"), "main", False)
        self.assertIn("AskUserQuestion", prompt)
        self.assertIn("DECISION NEEDED", prompt)
        self.assertIn("/name-dev-loop", prompt)

    def test_tend_prompt_forbids_merge_when_not_eligible(self):
        prompt = dev_loop.build_tend_prompt("owner/name", "name-dev-loop", Path("/tmp/t"), "main", False)
        self.assertIn("has NOT been authorized to merge", prompt)
        self.assertNotIn("HAS been explicitly pre-authorized to merge", prompt)

    def test_tend_prompt_authorizes_merge_when_eligible(self):
        prompt = dev_loop.build_tend_prompt("owner/name", "name-dev-loop", Path("/tmp/t"), "main", True)
        self.assertIn("HAS been explicitly pre-authorized to merge", prompt)
        self.assertNotIn("has NOT been authorized to merge", prompt)

    def test_tend_prompt_names_gh_pr_comment_as_the_review_posting_path(self):
        # See issue #40: dev-loop skills all document the review step as
        # `gh api .../reviews` / `gh pr review`, neither of which tend's
        # allow-list grants. The preamble must name the one command that
        # does work, so a run doesn't burn turns on two denials first.
        prompt = dev_loop.build_tend_prompt(
            "owner/name", "name-dev-loop", Path("/tmp/t"), "main", False
        )
        self.assertIn("gh pr comment", prompt)
        self.assertIn("gh pr review", prompt)
        self.assertIn("path:line", prompt)

    def test_tend_prompt_does_not_call_a_missing_review_object_a_blocked_decision(self):
        # A degraded review is an accepted trade, not a DECISION NEEDED —
        # otherwise every tend run would end asking a human about it.
        prompt = dev_loop.build_tend_prompt(
            "owner/name", "name-dev-loop", Path("/tmp/t"), "main", False
        )
        # Collapse whitespace first: the phrase is line-wrapped in the
        # source, so a raw substring check can only match a fragment on one
        # side of the wrap — and the fragment that fits ("missing Review
        # object as a blocked decision") is polarity-blind, passing just as
        # happily if the "do not treat the" were ever dropped and the
        # instruction inverted. Normalizing lets one assertion cover the
        # negation and survive a future re-wrap of the same sentence.
        self.assertIn(
            "do not treat the missing Review object as a blocked decision",
            " ".join(prompt.split()),
        )

    def test_tend_prompt_overrides_hardcoded_working_directory(self):
        prompt = dev_loop.build_tend_prompt("owner/name", "name-dev-loop", Path("/tmp/t"), "main", False)
        self.assertIn("ignore that line", prompt)

    def test_tend_prompt_forbids_looping_back_to_phase_1(self):
        prompt = dev_loop.build_tend_prompt("owner/name", "name-dev-loop", Path("/tmp/t"), "main", False)
        self.assertIn("exactly ONE pass", prompt)

    def test_tend_prompt_always_instructs_marking_new_prs(self):
        prompt = dev_loop.build_tend_prompt("owner/name", "name-dev-loop", Path("/tmp/t"), "main", False)
        self.assertIn(dev_loop.ORPHAN_MARKER, prompt)
        self.assertIn("NEW pull request", prompt)

    def test_tend_prompt_without_orphan_has_no_continuation_instructions(self):
        prompt = dev_loop.build_tend_prompt("owner/name", "name-dev-loop", Path("/tmp/t"), "main", False)
        self.assertNotIn("gardener found an existing OPEN pull request", prompt)

    def test_tend_prompt_with_orphan_instructs_continuation_not_a_fresh_start(self):
        orphan = dev_loop.OrphanedPR(number=238, head_branch="feature/plugin-icons-and-versions")
        prompt = dev_loop.build_tend_prompt(
            "owner/name", "name-dev-loop", Path("/tmp/t"), "main", False, orphaned_pr=orphan
        )
        self.assertIn("gardener found an existing OPEN pull request", prompt)
        self.assertIn("#238", prompt)
        self.assertIn(
            "git fetch origin feature/plugin-icons-and-versions && "
            "git checkout feature/plugin-icons-and-versions",
            prompt,
        )
        self.assertIn("do not start a second, duplicate branch/PR", prompt)


if __name__ == "__main__":
    unittest.main()
