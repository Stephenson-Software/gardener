"""Slug derivation and prompt-building are pure functions — no subprocess,
no filesystem beyond what a real skill install would leave on disk (which
`has_dev_loop_skill` reads, tested here against a tmp dir so it never
depends on this machine's actual ~/local-skills)."""
import unittest
from pathlib import Path
from unittest.mock import patch

from gardener import dev_loop


class TestSlugify(unittest.TestCase):
    def test_simple_name(self):
        self.assertEqual(dev_loop.slugify_repo_name("dmccoystephenson/a-private-repo"), "a-private-repo")

    def test_lowercases_and_hyphenates(self):
        self.assertEqual(
            dev_loop.slugify_repo_name("dmccoystephenson/Simple-Calculator-GUI-Using-SDL"),
            "simple-calculator-gui-using-sdl",
        )

    def test_collapses_non_alnum_runs(self):
        self.assertEqual(dev_loop.slugify_repo_name("owner/My_Repo.Name"), "my-repo-name")

    def test_strips_leading_trailing_separators(self):
        self.assertEqual(dev_loop.slugify_repo_name("owner/-weird-.name-"), "weird-name")

    def test_empty_name_raises(self):
        with self.assertRaises(ValueError):
            dev_loop.slugify_repo_name("owner/---")

    def test_skill_slug_appends_dev_loop_suffix(self):
        self.assertEqual(dev_loop.skill_slug("dmccoystephenson/a-private-repo"), "a-private-repo-dev-loop")


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

    def test_tend_prompt_overrides_hardcoded_working_directory(self):
        prompt = dev_loop.build_tend_prompt("owner/name", "name-dev-loop", Path("/tmp/t"), "main", False)
        self.assertIn("ignore that line", prompt)

    def test_tend_prompt_forbids_looping_back_to_phase_1(self):
        prompt = dev_loop.build_tend_prompt("owner/name", "name-dev-loop", Path("/tmp/t"), "main", False)
        self.assertIn("exactly ONE pass", prompt)


if __name__ == "__main__":
    unittest.main()
