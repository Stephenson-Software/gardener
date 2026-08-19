"""sessions.py's own mechanics: the registry round trip, lock-based
liveness (real `fcntl.flock` calls against a real tmp directory, like
`test_repo_lock.py` — the whole point is that the OS-level signal is what
decides "running", so mocking it would test nothing), id/target
resolution, `/proc` descendant walking against a synthetic proc tree, and
`stop`'s signal/escalate sequence with `os.kill`, liveness, and the clock
all injected. Nothing here signals a real process or invokes `claude`,
`git`, or `gh`."""
import contextlib
import fcntl
import io
import os
import signal
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gardener import sessions


def _write_proc(root: Path, pid: int, ppid: int) -> None:
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status").write_text(f"Name:\tfake\nPid:\t{pid}\nPPid:\t{ppid}\n")


class TestSessionsDir(unittest.TestCase):
    def test_lives_under_a_sessions_subdirectory(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                sessions.default_sessions_dir(state_dir=Path(td)),
                Path(td) / "sessions",
            )

    def test_state_dir_env_override_is_honoured(self):
        with tempfile.TemporaryDirectory() as td:
            original = os.environ.get("GARDENER_STATE_DIR")
            os.environ["GARDENER_STATE_DIR"] = td
            try:
                self.assertEqual(sessions.default_sessions_dir(), Path(td) / "sessions")
            finally:
                if original is None:
                    del os.environ["GARDENER_STATE_DIR"]
                else:
                    os.environ["GARDENER_STATE_DIR"] = original


class TestRegister(unittest.TestCase):
    def test_records_a_listable_running_session_inside_the_block(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            with sessions.register(
                "tend", "owner/name", log_path=Path("/tmp/x.log"),
                sessions_dir=d, id_fn=lambda: "abc123",
            ) as session:
                self.assertIsNotNone(session)
                found = sessions.list_sessions(sessions_dir=d)
                self.assertEqual([s.id for s in found], ["abc123"])
                self.assertTrue(found[0].running)
                self.assertEqual(found[0].command, "tend")
                self.assertEqual(found[0].target, "owner/name")
                self.assertEqual(found[0].pid, os.getpid())
                self.assertEqual(found[0].log_path, Path("/tmp/x.log"))

    def test_session_reads_as_exited_once_the_block_leaves(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            with sessions.register("tend", "owner/name", sessions_dir=d, id_fn=lambda: "abc123"):
                pass
            self.assertEqual(sessions.list_sessions(sessions_dir=d), [])
            all_sessions = sessions.list_sessions(sessions_dir=d, include_exited=True)
            self.assertEqual([s.id for s in all_sessions], ["abc123"])
            self.assertFalse(all_sessions[0].running)

    def test_lock_released_even_when_the_block_raises(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            with self.assertRaises(RuntimeError):
                with sessions.register("tend", "owner/name", sessions_dir=d, id_fn=lambda: "abc"):
                    raise RuntimeError("boom")
            self.assertEqual(sessions.list_sessions(sessions_dir=d), [])

    def test_unwritable_directory_degrades_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as td:
            # A regular file where the sessions dir should be: mkdir fails.
            blocker = Path(td) / "sessions"
            blocker.write_text("not a directory")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                with sessions.register("tend", "owner/name", sessions_dir=blocker) as session:
                    self.assertIsNone(session)
            # The degraded path must say what was lost, not just that
            # something failed.
            self.assertIn("gardener ps", err.getvalue())

    def test_ordered_newest_first(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            base = datetime(2026, 8, 19, 3, 0, tzinfo=timezone.utc)
            for i, name in enumerate(["old", "new"]):
                with sessions.register(
                    "tend", f"owner/{name}", sessions_dir=d,
                    id_fn=lambda name=name: name, now=base + timedelta(hours=i),
                ):
                    pass
            found = sessions.list_sessions(sessions_dir=d, include_exited=True)
            self.assertEqual([s.id for s in found], ["new", "old"])


class TestLiveness(unittest.TestCase):
    def test_a_held_lock_reads_as_running(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "held.json"
            path.write_text('{"id": "held", "pid": 1, "command": "tend", "target": "owner/name"}')
            fd = os.open(path, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                found = sessions.list_sessions(sessions_dir=Path(td))
                self.assertEqual([s.id for s in found], ["held"])
            finally:
                os.close(fd)

    def test_an_unlocked_file_reads_as_exited_however_recent(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "stale.json"
            # A pid that is certainly alive (this test process) — liveness
            # must still come from the lock, never from the pid, or a
            # recycled pid would make a dead session stoppable.
            path.write_text(
                f'{{"id": "stale", "pid": {os.getpid()}, "command": "tend", "target": "owner/name"}}'
            )
            self.assertEqual(sessions.list_sessions(sessions_dir=Path(td)), [])

    def test_unparseable_file_is_skipped_not_raised_over(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "half-written.json").write_text('{"id": "x", ')
            self.assertEqual(
                sessions.list_sessions(sessions_dir=Path(td), include_exited=True), []
            )

    def test_missing_sessions_dir_lists_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(sessions.list_sessions(sessions_dir=Path(td) / "nope"), [])


class TestPruneExited(unittest.TestCase):
    def _write(self, d: Path, session_id: str, mtime: float) -> Path:
        path = d / f"{session_id}.json"
        path.write_text(
            f'{{"id": "{session_id}", "pid": 1, "command": "tend", "target": "owner/name",'
            ' "started_at": "2026-08-19T03:00:00+00:00"}'
        )
        os.utime(path, (mtime, mtime))
        return path

    def test_keeps_only_the_most_recent_exited_files(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            for i in range(5):
                self._write(d, f"s{i}", mtime=1000 + i)
            removed = sessions.prune_exited(d, keep=2)
            left = sorted(p.stem for p in d.glob("*.json"))
            self.assertEqual(left, ["s3", "s4"])
            self.assertEqual(len(removed), 3)

    def test_never_prunes_a_running_session(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            live = self._write(d, "live", mtime=1)
            for i in range(3):
                self._write(d, f"s{i}", mtime=1000 + i)
            fd = os.open(live, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                sessions.prune_exited(d, keep=0)
                self.assertTrue(live.exists())
            finally:
                os.close(fd)


class TestResolve(unittest.TestCase):
    def _session(self, session_id: str, target: str = "owner/name") -> sessions.Session:
        return sessions.Session(
            id=session_id, pid=1, command="tend", target=target,
            started_at="2026-08-19T03:00:00+00:00", running=True, path=Path("/x"),
        )

    def test_exact_id(self):
        pool = [self._session("abcdef01"), self._session("abcdef02")]
        self.assertIs(sessions.resolve("abcdef01", pool), pool[0])

    def test_unique_prefix(self):
        pool = [self._session("abcdef01"), self._session("ffff0000")]
        self.assertIs(sessions.resolve("abc", pool), pool[0])

    def test_target_repo(self):
        pool = [self._session("aaaa", "owner/one"), self._session("bbbb", "owner/two")]
        self.assertIs(sessions.resolve("owner/two", pool), pool[1])

    def test_ambiguous_prefix_names_the_candidates(self):
        pool = [self._session("abcdef01", "owner/one"), self._session("abcdef02", "owner/two")]
        with self.assertRaises(sessions.SessionLookupError) as ctx:
            sessions.resolve("abc", pool)
        message = str(ctx.exception)
        self.assertIn("matches 2 sessions", message)
        self.assertIn("owner/one", message)
        self.assertIn("owner/two", message)

    def test_no_match_points_at_ps(self):
        with self.assertRaises(sessions.SessionLookupError) as ctx:
            sessions.resolve("zzz", [self._session("abcdef01")])
        self.assertIn("gardener ps", str(ctx.exception))


class TestDescendants(unittest.TestCase):
    def test_walks_the_whole_tree_deepest_first(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_proc(root, 100, 1)     # the session
            _write_proc(root, 200, 100)   # claude
            _write_proc(root, 300, 200)   # a build shell
            _write_proc(root, 400, 300)   # the build itself
            _write_proc(root, 999, 1)     # unrelated
            found = sessions.descendants(100, proc_root=root)
            self.assertEqual(found, [400, 300, 200])

    def test_unrelated_processes_are_never_included(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_proc(root, 100, 1)
            _write_proc(root, 999, 1)
            self.assertEqual(sessions.descendants(100, proc_root=root), [])

    def test_missing_proc_returns_empty_rather_than_raising(self):
        self.assertEqual(sessions.descendants(1, proc_root=Path("/nonexistent-proc")), [])


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class TestStop(unittest.TestCase):
    def _session(self, pid: int = 100) -> sessions.Session:
        return sessions.Session(
            id="abcdef01", pid=pid, command="tend", target="owner/name",
            started_at="2026-08-19T03:00:00+00:00", running=True, path=Path("/x"),
        )

    def _proc_tree(self, td: str) -> Path:
        root = Path(td)
        _write_proc(root, 100, 1)
        _write_proc(root, 200, 100)
        return root

    def test_signals_the_whole_tree_not_just_the_session(self):
        sent = []
        with tempfile.TemporaryDirectory() as td:
            result = sessions.stop(
                self._session(), proc_root=self._proc_tree(td),
                kill_fn=lambda pid, sig: sent.append((pid, sig)),
                alive_fn=lambda pid: False,
            )
        self.assertEqual(sent, [(200, signal.SIGTERM), (100, signal.SIGTERM)])
        self.assertTrue(result.stopped)
        self.assertFalse(result.escalated)

    def test_escalates_to_sigkill_when_the_grace_period_expires(self):
        sent = []
        clock = _Clock()
        with tempfile.TemporaryDirectory() as td:
            result = sessions.stop(
                self._session(), timeout=1.0, proc_root=self._proc_tree(td),
                kill_fn=lambda pid, sig: sent.append((pid, sig)),
                alive_fn=lambda pid: True,
                sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
            )
        self.assertIn((100, signal.SIGKILL), sent)
        self.assertIn((200, signal.SIGKILL), sent)
        self.assertTrue(result.escalated)
        self.assertFalse(result.stopped)

    def test_sweeps_children_that_outlived_a_stopped_session(self):
        sent = []
        with tempfile.TemporaryDirectory() as td:
            sessions.stop(
                self._session(), proc_root=self._proc_tree(td),
                kill_fn=lambda pid, sig: sent.append((pid, sig)),
                # The session itself is gone; its `claude` child is not.
                alive_fn=lambda pid: pid == 200,
            )
        self.assertIn((200, signal.SIGKILL), sent)

    def test_already_gone_pids_are_skipped_not_raised_over(self):
        def kill_fn(pid, sig):
            raise ProcessLookupError()

        with tempfile.TemporaryDirectory() as td:
            result = sessions.stop(
                self._session(), proc_root=self._proc_tree(td),
                kill_fn=kill_fn, alive_fn=lambda pid: False,
            )
        self.assertEqual(result.signalled, [])
        self.assertTrue(result.stopped)

    def test_kill_mode_sends_one_signal_without_waiting(self):
        sent = []
        clock = _Clock()
        with tempfile.TemporaryDirectory() as td:
            result = sessions.stop(
                self._session(), sig=signal.SIGINT, escalate=False,
                proc_root=self._proc_tree(td),
                kill_fn=lambda pid, sig: sent.append((pid, sig)),
                alive_fn=lambda pid: False,
                sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
            )
        self.assertEqual(sent, [(200, signal.SIGINT), (100, signal.SIGINT)])
        self.assertEqual(clock.now, 0.0)
        self.assertFalse(result.escalated)


class TestFormatAge(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(sessions.format_age(None), "-")
        self.assertEqual(sessions.format_age(9), "9s")
        self.assertEqual(sessions.format_age(65), "01:05")
        self.assertEqual(sessions.format_age(3 * 3600 + 14 * 60), "03:14")
        self.assertEqual(sessions.format_age(86400 + 2 * 3600), "1d02h")

    def test_age_seconds_measures_from_started_at(self):
        session = sessions.Session(
            id="a", pid=1, command="tend", target="owner/name",
            started_at="2026-08-19T03:00:00+00:00", running=True, path=Path("/x"),
        )
        now = datetime(2026, 8, 19, 3, 5, tzinfo=timezone.utc)
        self.assertEqual(session.age_seconds(now=now), 300.0)

    def test_unparseable_timestamp_is_unknown_not_an_error(self):
        session = sessions.Session(
            id="a", pid=1, command="tend", target="owner/name",
            started_at="", running=True, path=Path("/x"),
        )
        self.assertIsNone(session.age_seconds())


if __name__ == "__main__":
    unittest.main()
