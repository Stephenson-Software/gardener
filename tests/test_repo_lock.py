"""repo_lock's own mechanics: exclusivity across two independent lock
attempts (simulating two separate gardener processes), release-on-exit
(normal and exception), and the state-dir/path convention. Everything here
uses a real tmp directory and real fcntl.flock calls — no mocking of the
locking primitive itself, since the whole point is to prove the OS-level
exclusion actually holds."""
import os
import tempfile
import unittest
from pathlib import Path

from gardener.repo_lock import RepoLockedError, is_repo_locked, lock_file_path, repo_lock


class TestLockFilePath(unittest.TestCase):
    def test_slashes_become_double_underscores(self):
        with tempfile.TemporaryDirectory() as td:
            path = lock_file_path("owner/name", state_dir=Path(td))
        self.assertEqual(path.name, "owner__name.lock")

    def test_lives_under_a_locks_subdirectory(self):
        with tempfile.TemporaryDirectory() as td:
            path = lock_file_path("owner/name", state_dir=Path(td))
        self.assertEqual(path.parent.name, "locks")


class TestRepoLock(unittest.TestCase):
    def test_acquires_and_releases_around_a_normal_block(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            entered = False
            with repo_lock("owner/name", state_dir=state_dir):
                entered = True
            self.assertTrue(entered)
            # Lock file itself is left behind (unlocked) — normal resting state.
            self.assertTrue(lock_file_path("owner/name", state_dir=state_dir).is_file())

    def test_releases_even_if_the_wrapped_block_raises(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            with self.assertRaises(ValueError):
                with repo_lock("owner/name", state_dir=state_dir):
                    raise ValueError("boom")
            # A second acquisition must succeed — the first one's exception
            # must not have leaked the OS-level lock.
            with repo_lock("owner/name", state_dir=state_dir):
                pass

    def test_second_concurrent_attempt_on_the_same_repo_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            with repo_lock("owner/name", state_dir=state_dir):
                # Simulates a second, independent gardener process: a fresh
                # open() + flock() of the same path while the first is held.
                path = lock_file_path("owner/name", state_dir=state_dir)
                fd = os.open(path, os.O_CREAT | os.O_RDWR)
                try:
                    import fcntl

                    with self.assertRaises(OSError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)

    def test_repo_lock_context_manager_raises_repo_locked_error_when_already_held(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            with repo_lock("owner/name", state_dir=state_dir):
                with self.assertRaises(RepoLockedError) as ctx:
                    with repo_lock("owner/name", state_dir=state_dir):
                        pass
                self.assertEqual(ctx.exception.repo, "owner/name")

    def test_different_repos_do_not_contend(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            with repo_lock("owner/name-a", state_dir=state_dir):
                # A different repo's lock must be independently acquirable
                # while another repo's lock is held — this is exactly what
                # keeps `overnight --concurrency N` dispatching N distinct
                # repos in parallel from contending with itself.
                with repo_lock("owner/name-b", state_dir=state_dir):
                    pass

    def test_lock_is_released_after_the_with_block_so_it_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            with repo_lock("owner/name", state_dir=state_dir):
                pass
            # Must not raise — the first lock was fully released on exit.
            with repo_lock("owner/name", state_dir=state_dir):
                pass

    def test_error_message_names_the_repo(self):
        err = RepoLockedError("owner/name")
        self.assertIn("owner/name", str(err))


class TestIsRepoLocked(unittest.TestCase):
    """`is_repo_locked` is the read-only probe `doctor.py` uses to tell an
    in-flight dispatch's legitimately-dirty clone apart from one left dirty
    by a killed run. Same-process acquisition is a valid test of this:
    `flock` is per open-file-description, so a second `os.open` of the same
    lock file contends exactly the way a second process would."""

    def test_reports_a_held_lock(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            with repo_lock("owner/name", state_dir=state_dir):
                self.assertTrue(is_repo_locked("owner/name", state_dir=state_dir))

    def test_reports_a_released_lock_as_free(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            with repo_lock("owner/name", state_dir=state_dir):
                pass
            self.assertFalse(is_repo_locked("owner/name", state_dir=state_dir))

    def test_another_repos_lock_does_not_register(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            with repo_lock("owner/name-a", state_dir=state_dir):
                self.assertFalse(is_repo_locked("owner/name-b", state_dir=state_dir))

    def test_never_creates_the_lock_file(self):
        # A read-only command must stay read-only: probing a repo that has
        # never been dispatched against must not leave state behind.
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td)
            self.assertFalse(is_repo_locked("owner/never-touched", state_dir=state_dir))
            self.assertFalse(lock_file_path("owner/never-touched", state_dir).exists())


if __name__ == "__main__":
    unittest.main()
