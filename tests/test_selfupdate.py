"""selfupdate.py's git-calling logic is exercised entirely through the
injectable `run_fn` (never a real `subprocess.run`, `git`, or network
call) — mirrors overnight.py's injectable time_fn/sleep_fn pattern, per
gardener/CLAUDE.md's testing conventions. `find_repo_root` is exercised
against a real (but throwaway, tmp-dir) directory tree instead, since its
whole job is walking real filesystem parents looking for `.git`."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gardener import selfupdate
from gardener.selfupdate import UpdateStatus


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeGit:
    """Routes each `argv` to a canned CompletedProcess (or raises) by
    matching the first couple of args — real `self_update` calls always
    start with one of `status`/`rev-parse`/`fetch`/`merge-base`/`merge`, so
    that prefix alone disambiguates every call site without needing a full
    fake git plumbing implementation."""

    def __init__(self, responses: dict, calls: list | None = None):
        self.responses = responses
        self.calls = calls if calls is not None else []

    def __call__(self, argv, cwd, timeout):
        self.calls.append(argv)
        key = tuple(argv[1:3])
        if key not in self.responses:
            raise AssertionError(f"unexpected git call: {argv}")
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return response


CLEAN_STATUS = ("status", "--porcelain")
BRANCH = ("rev-parse", "--abbrev-ref")
LOCAL_SHA = ("rev-parse", "HEAD")
UPSTREAM = ("rev-parse", "--verify")
FETCH = ("fetch", "--quiet")
ANCESTOR = ("merge-base", "--is-ancestor")
MERGE = ("merge", "--ff-only")


def _base_responses(local_sha="aaa111", remote_sha="bbb222", branch="main"):
    return {
        CLEAN_STATUS: _completed(stdout=""),
        BRANCH: _completed(stdout=f"{branch}\n"),
        LOCAL_SHA: _completed(stdout=f"{local_sha}\n"),
        UPSTREAM: _completed(stdout=f"{local_sha}\n"),
        FETCH: _completed(),
        ("rev-parse", f"origin/{branch}"): _completed(stdout=f"{remote_sha}\n"),
        ANCESTOR: _completed(returncode=0),
        MERGE: _completed(),
    }


class TestFindRepoRoot(unittest.TestCase):
    def test_finds_git_dir_in_an_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            nested = repo / "gardener" / "sub"
            nested.mkdir(parents=True)
            (repo / ".git").mkdir()
            self.assertEqual(selfupdate.find_repo_root(start=nested), repo)

    def test_returns_none_when_no_git_dir_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "a" / "b"
            nested.mkdir(parents=True)
            # Force every `.git` existence check to report false so the walk
            # is guaranteed to reach the filesystem root and return None,
            # regardless of where the machine's real TMPDIR happens to live
            # (a real ancestor being a git checkout would otherwise make
            # this test's outcome depend on the environment, not the code).
            with patch.object(Path, "exists", return_value=False):
                self.assertIsNone(selfupdate.find_repo_root(start=nested))


class TestSelfUpdate(unittest.TestCase):
    def test_no_git_checkout_is_skipped_not_an_error(self):
        with patch.object(selfupdate, "find_repo_root", return_value=None):
            result = selfupdate.self_update()
        self.assertEqual(result.status, UpdateStatus.SKIPPED_NO_GIT)

    def test_dirty_tree_with_tracked_changes_is_skipped(self):
        responses = _base_responses()
        responses[CLEAN_STATUS] = _completed(stdout=" M gardener/cli.py\n")
        fake = FakeGit(responses)
        result = selfupdate.self_update(repo_root=Path("/repo"), run_fn=fake)
        self.assertEqual(result.status, UpdateStatus.SKIPPED_DIRTY)
        # Only the status check should have run before bailing out.
        self.assertEqual(len(fake.calls), 1)

    def test_untracked_only_files_are_not_dirty(self):
        responses = _base_responses(local_sha="aaa111", remote_sha="aaa111")
        responses[CLEAN_STATUS] = _completed(stdout="?? scratch/notes.txt\n")
        fake = FakeGit(responses)
        result = selfupdate.self_update(repo_root=Path("/repo"), run_fn=fake)
        self.assertEqual(result.status, UpdateStatus.UP_TO_DATE)

    def test_detached_head_is_skipped(self):
        responses = _base_responses()
        responses[BRANCH] = _completed(stdout="HEAD\n")
        fake = FakeGit(responses)
        result = selfupdate.self_update(repo_root=Path("/repo"), run_fn=fake)
        self.assertEqual(result.status, UpdateStatus.SKIPPED_DETACHED)

    def test_branch_with_no_upstream_is_skipped(self):
        responses = _base_responses()
        responses[UPSTREAM] = _completed(returncode=1)
        fake = FakeGit(responses)
        result = selfupdate.self_update(repo_root=Path("/repo"), run_fn=fake)
        self.assertEqual(result.status, UpdateStatus.SKIPPED_NO_UPSTREAM)
        # Never reaches fetch once we already know there's no upstream.
        self.assertNotIn(list(FETCH), [c[1:3] for c in fake.calls])

    def test_already_up_to_date(self):
        fake = FakeGit(_base_responses(local_sha="aaa111", remote_sha="aaa111"))
        result = selfupdate.self_update(repo_root=Path("/repo"), run_fn=fake)
        self.assertEqual(result.status, UpdateStatus.UP_TO_DATE)
        self.assertEqual(result.old_sha, "aaa111")
        self.assertEqual(result.new_sha, "aaa111")
        # Never reaches merge-base or merge once shas already match.
        self.assertNotIn(list(ANCESTOR), [c[1:3] for c in fake.calls])

    def test_diverged_branch_is_not_fast_forwarded(self):
        responses = _base_responses()
        responses[ANCESTOR] = _completed(returncode=1)
        fake = FakeGit(responses)
        result = selfupdate.self_update(repo_root=Path("/repo"), run_fn=fake)
        self.assertEqual(result.status, UpdateStatus.SKIPPED_NOT_FAST_FORWARD)
        self.assertNotIn(list(MERGE), [c[1:3] for c in fake.calls])

    def test_check_only_reports_update_available_without_merging(self):
        fake = FakeGit(_base_responses())
        result = selfupdate.self_update(repo_root=Path("/repo"), check_only=True, run_fn=fake)
        self.assertEqual(result.status, UpdateStatus.UPDATE_AVAILABLE)
        self.assertNotIn(list(MERGE), [c[1:3] for c in fake.calls])

    def test_successful_fast_forward_updates(self):
        fake = FakeGit(_base_responses(local_sha="aaa111", remote_sha="bbb222"))
        result = selfupdate.self_update(repo_root=Path("/repo"), run_fn=fake)
        self.assertEqual(result.status, UpdateStatus.UPDATED)
        self.assertEqual(result.old_sha, "aaa111")
        self.assertEqual(result.new_sha, "bbb222")
        self.assertIn(list(MERGE), [c[1:3] for c in fake.calls])

    def test_fetch_failure_is_an_error_not_a_crash(self):
        responses = _base_responses()
        responses[FETCH] = _completed(returncode=1, stderr="could not resolve host")
        fake = FakeGit(responses)
        result = selfupdate.self_update(repo_root=Path("/repo"), run_fn=fake)
        self.assertEqual(result.status, UpdateStatus.ERROR)
        self.assertIn("could not resolve host", result.message)

    def test_merge_failure_is_an_error_not_a_crash(self):
        responses = _base_responses()
        responses[MERGE] = _completed(returncode=1, stderr="not possible because you have unmerged files")
        fake = FakeGit(responses)
        result = selfupdate.self_update(repo_root=Path("/repo"), run_fn=fake)
        self.assertEqual(result.status, UpdateStatus.ERROR)

    def test_timeout_is_an_error_not_a_crash(self):
        responses = _base_responses()
        responses[FETCH] = subprocess.TimeoutExpired(cmd="git fetch", timeout=30)
        fake = FakeGit(responses)
        result = selfupdate.self_update(repo_root=Path("/repo"), run_fn=fake, timeout=30)
        self.assertEqual(result.status, UpdateStatus.ERROR)
        self.assertIn("timed out", result.message)

    def test_os_error_is_an_error_not_a_crash(self):
        responses = _base_responses()
        responses[CLEAN_STATUS] = OSError("git not found")
        fake = FakeGit(responses)
        result = selfupdate.self_update(repo_root=Path("/repo"), run_fn=fake)
        self.assertEqual(result.status, UpdateStatus.ERROR)


if __name__ == "__main__":
    unittest.main()
