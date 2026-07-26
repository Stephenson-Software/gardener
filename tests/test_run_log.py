"""run_log.py writes the file dashboard.py reads. The two halves are in
different modules and resolve the logs directory independently (each
module defines its own `_default_state_dir`, matching this repo's existing
convention), so the tests that matter most here are the drift guards: the
paths agree, the filename matches the glob the dashboard searches with, and
a real gardener progress line survives the round trip from `print(...,
file=sys.stderr)` through the log file and back out of the dashboard's
parser."""
import io
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from gardener import dashboard, run_log


class TestLogPaths(unittest.TestCase):
    def test_agrees_with_the_dashboard_on_where_logs_live(self):
        state_dir = Path("/tmp/gardener-test-state")
        self.assertEqual(
            run_log.default_logs_dir(state_dir),
            dashboard.default_logs_dir(state_dir),
        )

    def test_honors_the_state_dir_env_override(self):
        import os

        original = os.environ.get("GARDENER_STATE_DIR")
        os.environ["GARDENER_STATE_DIR"] = "/tmp/overridden"
        try:
            self.assertEqual(run_log.default_logs_dir(), Path("/tmp/overridden/logs"))
        finally:
            if original is None:
                del os.environ["GARDENER_STATE_DIR"]
            else:
                os.environ["GARDENER_STATE_DIR"] = original

    def test_file_name_shape(self):
        name = run_log.log_file_name("overnight", datetime(2026, 7, 26, 18, 5, 4))
        self.assertEqual(name, "overnight-20260726-180504.log")

    def test_file_name_matches_the_glob_the_dashboard_searches_with(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            path = logs_dir / run_log.log_file_name("tend", datetime(2026, 7, 26, 18, 5, 4))
            path.write_text("x")
            self.assertEqual(dashboard.find_active_log(logs_dir), path)


class TestTeeStderr(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self._tmpdir.name) / "logs"
        self._real_stderr = sys.stderr

    def tearDown(self):
        sys.stderr = self._real_stderr
        self._tmpdir.cleanup()

    def test_writes_to_both_the_file_and_the_original_stderr(self):
        captured = io.StringIO()
        sys.stderr = captured
        with run_log.tee_stderr("tend", logs_dir=self.logs_dir) as path:
            print("gardener: tending owner/repo (allow_merge=True)", file=sys.stderr)
        self.assertIn("tending owner/repo", captured.getvalue())
        self.assertIn("tending owner/repo", path.read_text())

    def test_restores_stderr_on_a_normal_exit(self):
        sentinel = io.StringIO()
        sys.stderr = sentinel
        with run_log.tee_stderr("tend", logs_dir=self.logs_dir):
            self.assertIsNot(sys.stderr, sentinel)
        self.assertIs(sys.stderr, sentinel)

    def test_restores_stderr_on_a_base_exception(self):
        # cmd_overnight is explicitly designed to survive being killed
        # mid-run, so the KeyboardInterrupt path is a real one here.
        sentinel = io.StringIO()
        sys.stderr = sentinel
        with self.assertRaises(KeyboardInterrupt):
            with run_log.tee_stderr("overnight", logs_dir=self.logs_dir):
                raise KeyboardInterrupt
        self.assertIs(sys.stderr, sentinel)

    def test_content_is_flushed_while_the_block_is_still_open(self):
        # The dashboard polls every 4s during a run that can sit in one
        # dispatch for minutes — buffering until exit would defeat it.
        with run_log.tee_stderr("tend", logs_dir=self.logs_dir) as path:
            print("gardener: dispatching /some-dev-loop", file=sys.stderr)
            self.assertIn("dispatching", path.read_text())

    def test_two_runs_in_the_same_second_do_not_erase_each_other(self):
        stamp = datetime(2026, 7, 26, 18, 5, 4)
        with run_log.tee_stderr("tend", logs_dir=self.logs_dir, now=stamp):
            print("first run", file=sys.stderr)
        with run_log.tee_stderr("tend", logs_dir=self.logs_dir, now=stamp) as path:
            print("second run", file=sys.stderr)
        text = path.read_text()
        self.assertIn("first run", text)
        self.assertIn("second run", text)

    def test_unopenable_log_yields_none_and_leaves_stderr_alone(self):
        # A regular file where the logs dir should be: mkdir fails, and the
        # run must continue anyway.
        self.logs_dir.parent.mkdir(parents=True, exist_ok=True)
        self.logs_dir.write_text("not a directory")
        sentinel = io.StringIO()
        sys.stderr = sentinel
        with run_log.tee_stderr("tend", logs_dir=self.logs_dir) as path:
            self.assertIsNone(path)
            self.assertIs(sys.stderr, sentinel)
        self.assertIn("could not open a run log", sentinel.getvalue())

    def test_fileno_passes_through_to_the_real_stderr(self):
        # Anything handing sys.stderr to a subprocess needs a usable fd.
        with tempfile.TemporaryFile(mode="w") as fh:
            sys.stderr = fh
            with run_log.tee_stderr("tend", logs_dir=self.logs_dir):
                self.assertEqual(sys.stderr.fileno(), fh.fileno())


class TestPruneOldLogs(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write(self, name: str, mtime: float) -> Path:
        import os

        path = self.logs_dir / name
        path.write_text("x")
        os.utime(path, (mtime, mtime))
        return path

    def test_keeps_only_the_newest_n(self):
        old = self._write("tend-1.log", 1000)
        mid = self._write("tend-2.log", 2000)
        new = self._write("tend-3.log", 3000)
        removed = run_log.prune_old_logs(self.logs_dir, keep=2)
        self.assertEqual(removed, [old])
        self.assertFalse(old.exists())
        self.assertTrue(mid.exists())
        self.assertTrue(new.exists())

    def test_ignores_non_log_files(self):
        keeper = self._write("notes.txt", 1000)
        self._write("tend-1.log", 2000)
        run_log.prune_old_logs(self.logs_dir, keep=0)
        self.assertTrue(keeper.exists())

    def test_missing_dir_is_not_an_error(self):
        self.assertEqual(run_log.prune_old_logs(self.logs_dir / "nope"), [])

    def test_a_new_run_prunes_but_never_its_own_log(self):
        # `keep` counts the current run's log too, so keep=1 means the new
        # run's file is the only survivor.
        for i in range(3):
            self._write(f"tend-{i}.log", 1000 + i)
        with run_log.tee_stderr("tend", logs_dir=self.logs_dir, keep=1) as path:
            print("current run", file=sys.stderr)
            self.assertTrue(path.exists())
        self.assertEqual(list(self.logs_dir.glob("*.log")), [path])
        self.assertIn("current run", path.read_text())

    def test_keep_zero_still_cannot_delete_the_live_log(self):
        with run_log.tee_stderr("tend", logs_dir=self.logs_dir, keep=0) as path:
            print("current run", file=sys.stderr)
        self.assertTrue(path.exists())


class TestDashboardReadsWhatGardenerWrites(unittest.TestCase):
    """The end-to-end drift guard: the exact progress lines cli.py prints,
    written through the real tee, must come back out of the dashboard's own
    parsers. If either side's wording or regex changes, this fails rather
    than the live panels silently going blank again."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self._tmpdir.name) / "logs"
        self._real_stderr = sys.stderr
        sys.stderr = io.StringIO()

    def tearDown(self):
        sys.stderr = self._real_stderr
        self._tmpdir.cleanup()

    def test_in_progress_and_batch_progress_survive_the_round_trip(self):
        with run_log.tee_stderr("overnight", logs_dir=self.logs_dir) as path:
            print(
                "gardener: overnight dispatching tend for owner/a, owner/b "
                "(3-4/20 candidates this run, concurrency=2)...",
                file=sys.stderr,
            )
            print("gardener: tending owner/a (allow_merge=True)", file=sys.stderr)
            print("gardener: tending owner/b (allow_merge=True)", file=sys.stderr)
            print("notify: sent to Discord: gardener tend: MUTATION — owner/a", file=sys.stderr)

        found = dashboard.find_active_log(self.logs_dir)
        self.assertEqual(found, path)
        lines = dashboard.tail_lines(found)
        self.assertEqual(dashboard.parse_in_progress(lines), ["owner/b"])
        self.assertEqual(dashboard.parse_batch_progress(lines), (3, 4, 20))


if __name__ == "__main__":
    unittest.main()
