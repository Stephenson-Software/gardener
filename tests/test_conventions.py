"""conventions.py backs `gardener align`'s entire conventions checkout/
validation step but had zero test coverage (issue #8). `_run_git`/
`ensure_conventions` are mocked at the subprocess boundary the same way
test_dispatch.py mocks `gardener.dispatch.subprocess.run` — no real git
process is ever invoked here.

Every test that reaches `ensure_conventions` passes an explicit `url=`, so
none of them depend on whatever `$GARDENER_CONVENTIONS_URL` happens to be
set to in the environment running the suite (`TestResolveUrl` clears it
explicitly, since that is the thing it is actually asserting about)."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gardener.conventions import (
    CONVENTIONS_URL_ENV,
    REQUIRED_DOCS,
    ConventionsError,
    ConventionsSource,
    _origin_url,
    default_cache_dir,
    ensure_conventions,
    resolve_url,
)

FAKE_URL = "https://example.invalid/conventions.git"
OTHER_URL = "https://example.invalid/other-conventions.git"


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
            ConventionsSource(path=self.path, url=FAKE_URL).verify_complete()
        self.assertIn("docs/CODEOWNERS.md", str(ctx.exception))
        self.assertIn("ALIGNMENT_CHECKLIST.md", str(ctx.exception))
        # Names the configured source, so the operator knows which repo to
        # add the missing file to rather than just that one is missing.
        self.assertIn(FAKE_URL, str(ctx.exception))


class TestResolveUrl(unittest.TestCase):
    """gardener deliberately ships no default conventions repo — a baked-in
    one would silently audit every target against somebody else's
    conventions. These assert the "no default" property directly, so a
    future edit can't reintroduce one without failing here."""

    def test_explicit_url_wins_over_environment(self):
        with patch.dict("os.environ", {CONVENTIONS_URL_ENV: OTHER_URL}):
            self.assertEqual(resolve_url(FAKE_URL), FAKE_URL)

    def test_falls_back_to_environment_variable(self):
        with patch.dict("os.environ", {CONVENTIONS_URL_ENV: FAKE_URL}):
            self.assertEqual(resolve_url(), FAKE_URL)

    def test_raises_when_unset_rather_than_using_a_default(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConventionsError) as ctx:
                resolve_url()
        message = str(ctx.exception)
        # Diagnose *and* guide recovery: both configuration mechanisms and
        # the required layout, not just "not configured".
        self.assertIn(CONVENTIONS_URL_ENV, message)
        self.assertIn("--conventions-repo", message)
        for doc in REQUIRED_DOCS:
            self.assertIn(doc, message)

    def test_blank_environment_variable_is_treated_as_unset(self):
        with patch.dict("os.environ", {CONVENTIONS_URL_ENV: "   "}):
            with self.assertRaises(ConventionsError):
                resolve_url()


class TestEnsureConventions(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmpdir.name) / "conventions"

    def tearDown(self):
        self._tmpdir.cleanup()

    @patch("gardener.conventions.ConventionsSource.verify_complete")
    @patch("gardener.conventions._run_git")
    def test_clones_when_no_git_dir_exists_yet(self, mock_run_git, mock_verify):
        source = ensure_conventions(cache_dir=self.cache_dir, url=FAKE_URL)

        mock_run_git.assert_called_once_with(
            ["clone", "--depth", "1", FAKE_URL, str(self.cache_dir)]
        )
        self.assertEqual(source.path, self.cache_dir)
        self.assertEqual(source.url, FAKE_URL)
        mock_verify.assert_called_once()

    @patch("gardener.conventions._origin_url", return_value=FAKE_URL)
    @patch("gardener.conventions.ConventionsSource.verify_complete")
    @patch("gardener.conventions._run_git")
    def test_fetches_and_hard_resets_when_git_dir_exists_and_refresh_true(self, mock_run_git, mock_verify, mock_origin):
        (self.cache_dir / ".git").mkdir(parents=True)

        ensure_conventions(cache_dir=self.cache_dir, refresh=True, url=FAKE_URL)

        self.assertEqual(
            mock_run_git.call_args_list,
            [
                unittest.mock.call(["fetch", "--depth", "1", "origin"], cwd=self.cache_dir),
                unittest.mock.call(["reset", "--hard", "origin/HEAD"], cwd=self.cache_dir),
            ],
        )
        mock_verify.assert_called_once()

    @patch("gardener.conventions._origin_url", return_value=FAKE_URL)
    @patch("gardener.conventions.ConventionsSource.verify_complete")
    @patch("gardener.conventions._run_git")
    def test_no_git_calls_when_git_dir_exists_and_refresh_false(self, mock_run_git, mock_verify, mock_origin):
        (self.cache_dir / ".git").mkdir(parents=True)

        ensure_conventions(cache_dir=self.cache_dir, refresh=False, url=FAKE_URL)

        mock_run_git.assert_not_called()
        mock_verify.assert_called_once()

    @patch("gardener.conventions._origin_url", return_value=OTHER_URL)
    @patch("gardener.conventions.ConventionsSource.verify_complete")
    @patch("gardener.conventions._run_git")
    def test_cache_from_a_different_conventions_repo_is_repointed_and_refreshed(self, mock_run_git, mock_verify, mock_origin):
        """The cache is one fixed directory but the URL is configurable, so
        an existing cache may belong to a different conventions repo. It
        must be repointed — reusing it would audit the target against the
        previously-configured repo, which is a wrong answer, not a stale
        one. `refresh=False` is passed deliberately: correctness here
        outranks honoring --no-refresh-conventions."""
        (self.cache_dir / ".git").mkdir(parents=True)

        ensure_conventions(cache_dir=self.cache_dir, refresh=False, url=FAKE_URL)

        self.assertEqual(
            mock_run_git.call_args_list,
            [
                unittest.mock.call(["remote", "set-url", "origin", FAKE_URL], cwd=self.cache_dir),
                unittest.mock.call(["fetch", "--depth", "1", "origin"], cwd=self.cache_dir),
                unittest.mock.call(["reset", "--hard", "origin/HEAD"], cwd=self.cache_dir),
            ],
        )

    @patch("gardener.conventions._origin_url", return_value=None)
    @patch("gardener.conventions.ConventionsSource.verify_complete")
    @patch("gardener.conventions._run_git")
    def test_unreadable_cache_origin_is_repointed_rather_than_failing(self, mock_run_git, mock_verify, mock_origin):
        (self.cache_dir / ".git").mkdir(parents=True)

        ensure_conventions(cache_dir=self.cache_dir, refresh=False, url=FAKE_URL)

        self.assertIn(
            unittest.mock.call(["remote", "set-url", "origin", FAKE_URL], cwd=self.cache_dir),
            mock_run_git.call_args_list,
        )

    @patch("gardener.conventions._run_git")
    def test_incomplete_checkout_raises_conventions_error(self, mock_run_git):
        # _run_git is mocked to a no-op, so the "clone" never actually
        # populates cache_dir — verify_complete (not mocked here) should
        # then genuinely catch the missing docs.
        with self.assertRaises(ConventionsError):
            ensure_conventions(cache_dir=self.cache_dir, url=FAKE_URL)

    @patch("gardener.conventions._run_git")
    def test_unconfigured_url_raises_before_any_git_call(self, mock_run_git):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ConventionsError):
                ensure_conventions(cache_dir=self.cache_dir)
        mock_run_git.assert_not_called()


class TestDefaultCacheDir(unittest.TestCase):
    def test_override_env_var_wins(self):
        with patch.dict("os.environ", {"GARDENER_CACHE_DIR": "/tmp/somewhere"}):
            self.assertEqual(default_cache_dir(), Path("/tmp/somewhere") / "conventions")

    def test_falls_back_to_home_cache_dir_when_unset(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                default_cache_dir(), Path.home() / ".cache" / "gardener" / "conventions"
            )


class TestConventionsSourcePaths(unittest.TestCase):
    def test_checklist_path_is_relative_to_source_path(self):
        source = ConventionsSource(path=Path("/tmp/conv"))
        self.assertEqual(source.checklist_path(), Path("/tmp/conv/ALIGNMENT_CHECKLIST.md"))

    def test_alignment_prompt_path_is_relative_to_source_path(self):
        source = ConventionsSource(path=Path("/tmp/conv"))
        self.assertEqual(source.alignment_prompt_path(), Path("/tmp/conv/ALIGNMENT_PROMPT.md"))


class TestOriginUrl(unittest.TestCase):
    """_origin_url is the one piece of ensure_conventions's repoint logic
    every TestEnsureConventions test above mocks out — these exercise its
    own subprocess-parsing behavior directly, at the subprocess.run
    boundary, the same way TestRunGit does for _run_git."""

    @patch("gardener.conventions.subprocess.run")
    def test_returns_stripped_stdout_on_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{FAKE_URL}\n", stderr=""
        )
        self.assertEqual(_origin_url(Path("/tmp/conv")), FAKE_URL)

    @patch("gardener.conventions.subprocess.run")
    def test_returns_none_on_nonzero_exit(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="fatal: not a git repository"
        )
        self.assertIsNone(_origin_url(Path("/tmp/conv")))

    @patch("gardener.conventions.subprocess.run")
    def test_returns_none_rather_than_raising_on_subprocess_error(self, mock_run):
        mock_run.side_effect = subprocess.SubprocessError("boom")
        self.assertIsNone(_origin_url(Path("/tmp/conv")))

    @patch("gardener.conventions.subprocess.run")
    def test_returns_none_rather_than_raising_on_os_error(self, mock_run):
        mock_run.side_effect = OSError("git binary not found")
        self.assertIsNone(_origin_url(Path("/tmp/conv")))


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
