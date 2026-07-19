"""conventions.py backs `gardener align`'s entire dms-conventions checkout/
validation step but had zero test coverage (issue #8). `_run_git`/
`ensure_conventions` are mocked at the subprocess boundary the same way
test_dispatch.py mocks `gardener.dispatch.subprocess.run` — no real git
process is ever invoked here."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gardener.conventions import (
    DMS_CONVENTIONS_URL,
    REQUIRED_DOCS,
    ConventionsError,
    ConventionsSource,
    ensure_conventions,
)


class TestVerifyComplete(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _touch_all_required_docs(self):
        for doc in REQUIRED_DOCS:
            full = self.path / doc
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text("stub")

    def test_passes_when_every_required_doc_exists(self):
        self._touch_all_required_docs()
        ConventionsSource(path=self.path).verify_complete()  # must not raise

    def test_raises_naming_the_missing_docs(self):
        self._touch_all_required_docs()
        (self.path / "docs" / "CODEOWNERS.md").unlink()
        (self.path / "ALIGNMENT_CHECKLIST.md").unlink()

        with self.assertRaises(ConventionsError) as ctx:
            ConventionsSource(path=self.path).verify_complete()
        self.assertIn("docs/CODEOWNERS.md", str(ctx.exception))
        self.assertIn("ALIGNMENT_CHECKLIST.md", str(ctx.exception))
        self.assertIn(DMS_CONVENTIONS_URL, str(ctx.exception))


class TestEnsureConventions(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmpdir.name) / "dms-conventions"

    def tearDown(self):
        self._tmpdir.cleanup()

    @patch("gardener.conventions.ConventionsSource.verify_complete")
    @patch("gardener.conventions._run_git")
    def test_clones_when_no_git_dir_exists_yet(self, mock_run_git, mock_verify):
        source = ensure_conventions(cache_dir=self.cache_dir)

        mock_run_git.assert_called_once_with(
            ["clone", "--depth", "1", DMS_CONVENTIONS_URL, str(self.cache_dir)]
        )
        self.assertEqual(source.path, self.cache_dir)
        mock_verify.assert_called_once()

    @patch("gardener.conventions.ConventionsSource.verify_complete")
    @patch("gardener.conventions._run_git")
    def test_fetches_and_hard_resets_when_git_dir_exists_and_refresh_true(self, mock_run_git, mock_verify):
        (self.cache_dir / ".git").mkdir(parents=True)

        ensure_conventions(cache_dir=self.cache_dir, refresh=True)

        self.assertEqual(
            mock_run_git.call_args_list,
            [
                unittest.mock.call(["fetch", "--depth", "1", "origin"], cwd=self.cache_dir),
                unittest.mock.call(["reset", "--hard", "origin/HEAD"], cwd=self.cache_dir),
            ],
        )
        mock_verify.assert_called_once()

    @patch("gardener.conventions.ConventionsSource.verify_complete")
    @patch("gardener.conventions._run_git")
    def test_no_git_calls_when_git_dir_exists_and_refresh_false(self, mock_run_git, mock_verify):
        (self.cache_dir / ".git").mkdir(parents=True)

        ensure_conventions(cache_dir=self.cache_dir, refresh=False)

        mock_run_git.assert_not_called()
        mock_verify.assert_called_once()

    @patch("gardener.conventions._run_git")
    def test_incomplete_checkout_raises_conventions_error(self, mock_run_git):
        # _run_git is mocked to a no-op, so the "clone" never actually
        # populates cache_dir — verify_complete (not mocked here) should
        # then genuinely catch the missing docs.
        with self.assertRaises(ConventionsError):
            ensure_conventions(cache_dir=self.cache_dir)


class TestRunGit(unittest.TestCase):
    @patch("gardener.conventions.subprocess.run")
    def test_called_process_error_is_wrapped_as_conventions_error(self, mock_run):
        from gardener.conventions import _run_git

        mock_run.side_effect = subprocess.CalledProcessError(
            returncode=128, cmd=["git", "clone"], stderr="fatal: repository not found"
        )
        with self.assertRaises(ConventionsError) as ctx:
            _run_git(["clone", "https://example.invalid/x.git", "/tmp/x"])
        self.assertIn("fatal: repository not found", str(ctx.exception))

    @patch("gardener.conventions.subprocess.run")
    def test_timeout_is_wrapped_as_conventions_error(self, mock_run):
        from gardener.conventions import _run_git

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=60)
        with self.assertRaises(ConventionsError) as ctx:
            _run_git(["fetch", "origin"])
        self.assertIn("timed out", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
