"""dashboard.py's HTTP layer is a thin stdlib http.server wrapper around
build_status() — not covered here, mirroring how dispatch.py's actual
`claude` subprocess call is mocked rather than invoked in test_dispatch.py.
This file covers the pure log-parsing and status-assembly functions, which
never touch a socket."""
import io
import json
import socket
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from gardener import dashboard, dispatch, garden, merge_allowlist, overnight, state


class TestFindActiveLog(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self._tmpdir.name) / "logs"

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_missing_dir_returns_none(self):
        self.assertIsNone(dashboard.find_active_log(self.logs_dir))

    def test_empty_dir_returns_none(self):
        self.logs_dir.mkdir()
        self.assertIsNone(dashboard.find_active_log(self.logs_dir))

    def test_non_log_files_are_ignored(self):
        self.logs_dir.mkdir()
        (self.logs_dir / "notes.txt").write_text("hi")
        self.assertIsNone(dashboard.find_active_log(self.logs_dir))

    def test_picks_the_most_recently_modified_log(self):
        import os
        import time

        self.logs_dir.mkdir()
        older = self.logs_dir / "older.log"
        newer = self.logs_dir / "newer.log"
        older.write_text("old")
        time.sleep(0.01)
        newer.write_text("new")
        # Force a clear mtime gap regardless of filesystem timestamp
        # resolution rather than relying on the sleep alone.
        now = time.time()
        os.utime(older, (now - 100, now - 100))
        os.utime(newer, (now, now))
        self.assertEqual(dashboard.find_active_log(self.logs_dir), newer)


class TestFindActiveLogs(unittest.TestCase):
    """Two gardener processes at once is supported (see repo_lock.py), so
    the live panels read every log still being written to — before issue
    #50 a one-repo manual `tend` became the newest log and silently
    replaced the whole page's view of the overnight run beside it."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self._tmpdir.name) / "logs"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _log(self, name, age_seconds=0.0):
        import os
        import time

        self.logs_dir.mkdir(exist_ok=True)
        path = self.logs_dir / name
        path.write_text("x")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_missing_dir_is_empty(self):
        self.assertEqual(dashboard.find_active_logs(self.logs_dir), [])

    def test_empty_dir_is_empty(self):
        self.logs_dir.mkdir()
        self.assertEqual(dashboard.find_active_logs(self.logs_dir), [])

    def test_returns_every_fresh_log_most_recent_first(self):
        older = self._log("overnight-1.log", age_seconds=60)
        newer = self._log("tend-1.log", age_seconds=1)
        self.assertEqual(dashboard.find_active_logs(self.logs_dir), [newer, older])

    def test_excludes_logs_older_than_the_window(self):
        fresh = self._log("tend-1.log", age_seconds=1)
        self._log("overnight-old.log", age_seconds=dashboard.ACTIVE_LOG_WINDOW_SECONDS + 60)
        self.assertEqual(dashboard.find_active_logs(self.logs_dir), [fresh])

    def test_a_log_pruned_mid_scan_is_skipped_not_fatal(self):
        """`run_log.prune_old_logs` can delete a log between this function's
        `glob` and its `stat`. The dashboard polls every 4s while pruning
        runs on a separate process, so this race is routine — losing one log
        is correct, raising `OSError` at the caller is not."""
        doomed = self._log("overnight-doomed.log", age_seconds=1)
        survivor = self._log("tend-alive.log", age_seconds=1)

        real_stat = Path.stat

        def stat_but_the_doomed_one_vanished(self, *args, **kwargs):
            if self.name == doomed.name:
                raise OSError(2, "No such file or directory")
            return real_stat(self, *args, **kwargs)

        # `is_file()` is patched alongside `stat()` on purpose. It calls
        # `stat()` internally and swallows OSError by returning False, so
        # patching `stat()` alone makes this test pass via the `is_file()`
        # guard without ever reaching the `except OSError` branch it is
        # meant to exercise — a false negative confirmed by coverage
        # (lines stayed unhit). Forcing `is_file()` True reproduces the real
        # race: the file existed at the guard and was gone by the `stat()`.
        with patch.object(Path, "is_file", lambda self: True):
            with patch.object(Path, "stat", stat_but_the_doomed_one_vanished):
                self.assertEqual(dashboard.find_active_logs(self.logs_dir), [survivor])

    def test_falls_back_to_the_newest_log_when_nothing_is_fresh(self):
        # A finished run's narration is still the most relevant thing to
        # show until a newer run starts — the page must not go blank just
        # because the last line aged out.
        stale_older = self._log("overnight-1.log", age_seconds=100_000)
        stale_newer = self._log("overnight-2.log", age_seconds=90_000)
        self.assertEqual(dashboard.find_active_logs(self.logs_dir), [stale_newer])
        self.assertNotIn(stale_older, dashboard.find_active_logs(self.logs_dir))

    def test_non_log_files_are_ignored(self):
        self.logs_dir.mkdir()
        (self.logs_dir / "notes.txt").write_text("hi")
        self.assertEqual(dashboard.find_active_logs(self.logs_dir), [])

    def test_the_window_boundary_is_exact_with_an_injected_clock(self):
        # `now` is injectable so the boundary can be asserted without
        # depending on the filesystem's mtime resolution, in the same
        # spirit as transcript.py's injected time_fn/sleep_fn.
        import os

        self.logs_dir.mkdir()
        path = self.logs_dir / "tend-1.log"
        path.write_text("x")
        os.utime(path, (1_000_000, 1_000_000))

        inside = 1_000_000 + dashboard.ACTIVE_LOG_WINDOW_SECONDS
        self.assertEqual(dashboard.find_active_logs(self.logs_dir, now=inside), [path])
        # One second past the window it is no longer *fresh* — but it is
        # still the newest log, so the fallback keeps the page populated.
        self.assertEqual(dashboard.find_active_logs(self.logs_dir, now=inside + 1), [path])
        # With a fresher log present, the stale one is genuinely dropped.
        newer = self.logs_dir / "overnight-1.log"
        newer.write_text("x")
        os.utime(newer, (inside, inside))
        self.assertEqual(dashboard.find_active_logs(self.logs_dir, now=inside + 1), [newer])

    def test_window_is_long_enough_for_a_silent_tend_dispatch(self):
        # A tend can sit inside one `claude` subprocess for the entire
        # dispatch timeout without gardener printing anything, so a window
        # shorter than that would drop a genuinely-running batch.
        self.assertGreater(
            dashboard.ACTIVE_LOG_WINDOW_SECONDS, dispatch.TEND_DEFAULT_TIMEOUT_SECONDS
        )


class TestTailLines(unittest.TestCase):
    def test_missing_file_returns_empty_list(self):
        self.assertEqual(dashboard.tail_lines(Path("/nonexistent/x.log")), [])

    def test_returns_only_the_last_n_lines(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.log"
            path.write_text("\n".join(f"line{i}" for i in range(10)))
            self.assertEqual(dashboard.tail_lines(path, n=3), ["line7", "line8", "line9"])

    def test_short_file_returns_everything(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.log"
            path.write_text("a\nb")
            self.assertEqual(dashboard.tail_lines(path, n=10), ["a", "b"])

    def test_empty_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.log"
            path.write_bytes(b"")
            self.assertEqual(dashboard.tail_lines(path, n=5), [])

    def test_large_file_does_not_read_more_than_necessary(self):
        # Write a file that spans multiple 4096-byte chunks; verify we still
        # get back the correct last-N lines without needing to load it all.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "big.log"
            lines = [f"line{i:06d}" for i in range(2000)]
            path.write_text("\n".join(lines))
            result = dashboard.tail_lines(path, n=10)
            self.assertEqual(result, [f"line{i:06d}" for i in range(1990, 2000)])


class TestParseInProgress(unittest.TestCase):
    def test_no_lines_is_empty(self):
        self.assertEqual(dashboard.parse_in_progress([]), [])

    def test_started_repo_with_no_terminal_notify_is_in_progress(self):
        lines = ["gardener: tending owner/repo (allow_merge=True)"]
        self.assertEqual(dashboard.parse_in_progress(lines), ["owner/repo"])

    def test_finished_tending_marks_terminal_with_no_notify_line_at_all(self):
        # The issue #51 case: no webhook configured, so notify.py's
        # NullNotifier prints nothing — the repo must still clear.
        lines = [
            "gardener: tending owner/repo (allow_merge=True)",
            "gardener: finished tending owner/repo",
        ]
        self.assertEqual(dashboard.parse_in_progress(lines), [])

    def test_finished_tending_only_clears_the_repo_it_names(self):
        lines = [
            "gardener: tending owner/a (allow_merge=True)",
            "gardener: tending owner/b (allow_merge=True)",
            "gardener: finished tending owner/b",
        ]
        self.assertEqual(dashboard.parse_in_progress(lines), ["owner/a"])

    def test_tend_mode_notify_marks_terminal(self):
        # Retained as the fallback for logs written before the
        # `finished tending` marker existed — not the primary signal.
        lines = [
            "gardener: tending owner/repo (allow_merge=True)",
            "notify: sent to Discord: gardener tend: MUTATION — owner/repo",
        ]
        self.assertEqual(dashboard.parse_in_progress(lines), [])

    def test_tend_mode_failure_notify_also_marks_terminal(self):
        lines = [
            "gardener: tending owner/repo (allow_merge=True)",
            "notify: sent to Discord: gardener tend: FAILED — owner/repo",
        ]
        self.assertEqual(dashboard.parse_in_progress(lines), [])

    def test_successful_create_dev_loop_notify_is_not_terminal(self):
        # create-dev-loop succeeding just means the real tend dispatch is
        # about to start — the repo is still in progress until *that*
        # reports back.
        lines = [
            "gardener: tending owner/repo (allow_merge=True)",
            "notify: sent to Discord: gardener create-dev-loop: MUTATION — owner/repo",
        ]
        self.assertEqual(dashboard.parse_in_progress(lines), ["owner/repo"])

    def test_failed_create_dev_loop_notify_is_terminal(self):
        # A failed bootstrap means tend never dispatches for this repo
        # this cycle — nothing more is coming.
        lines = [
            "gardener: tending owner/repo (allow_merge=True)",
            "notify: sent to Discord: gardener create-dev-loop: FAILED — owner/repo",
        ]
        self.assertEqual(dashboard.parse_in_progress(lines), [])

    def test_preserves_dispatch_order_and_skips_finished_repos(self):
        lines = [
            "gardener: tending owner/a (allow_merge=True)",
            "gardener: tending owner/b (allow_merge=True)",
            "gardener: tending owner/c (allow_merge=True)",
            "notify: sent to Discord: gardener tend: MUTATION — owner/b",
        ]
        self.assertEqual(dashboard.parse_in_progress(lines), ["owner/a", "owner/c"])

    def test_repeated_tending_line_is_not_duplicated(self):
        lines = [
            "gardener: tending owner/repo (allow_merge=True)",
            "gardener: tending owner/repo (allow_merge=True)",
        ]
        self.assertEqual(dashboard.parse_in_progress(lines), ["owner/repo"])

    def test_a_repo_restarted_after_finishing_reads_as_in_flight_again(self):
        # Two invocations started in the same second append to the same
        # log file (see run_log.tee_stderr), so the last marker for a repo
        # has to win rather than any terminal line anywhere winning.
        lines = [
            "gardener: tending owner/repo (allow_merge=True)",
            "gardener: finished tending owner/repo",
            "gardener: tending owner/repo (allow_merge=True)",
        ]
        self.assertEqual(dashboard.parse_in_progress(lines), ["owner/repo"])

    def test_a_terminal_line_for_a_repo_never_started_in_this_tail_is_ignored(self):
        # The tail window can cut off a repo's `tending` line while keeping
        # its `finished` one — that repo simply isn't on the page, and must
        # not be resurrected into the list by its own terminal marker.
        self.assertEqual(
            dashboard.parse_in_progress(["gardener: finished tending owner/repo"]), []
        )


class TestParseBatchProgress(unittest.TestCase):
    def test_no_batch_line_returns_none(self):
        self.assertIsNone(dashboard.parse_batch_progress(["gardener: tending owner/repo"]))

    def test_extracts_the_range_and_total(self):
        lines = [
            "gardener: overnight dispatching tend for a, b, c, d "
            "(5-8/28 candidates this run, concurrency=4)...",
        ]
        self.assertEqual(dashboard.parse_batch_progress(lines), (5, 8, 28))

    def test_uses_the_most_recent_batch_line_when_several_present(self):
        lines = [
            "gardener: overnight dispatching tend for a (1-4/28 candidates this run, concurrency=4)...",
            "gardener: overnight dispatching tend for b (5-8/28 candidates this run, concurrency=4)...",
        ]
        self.assertEqual(dashboard.parse_batch_progress(lines), (5, 8, 28))

    def test_default_sequential_run_parses_as_a_batch_of_one(self):
        # The exact shape `cmd_overnight` emits at the default
        # --concurrency 1: a bare `N/T`, no range and no `concurrency=`.
        lines = [
            "gardener: overnight dispatching tend for owner/repo (3/5 candidates this run)...",
        ]
        self.assertEqual(dashboard.parse_batch_progress(lines), (3, 3, 5))

    def test_mixed_sequential_and_range_lines_use_the_most_recent(self):
        lines = [
            "gardener: overnight dispatching tend for a, b (1-2/5 candidates this run, concurrency=2)...",
            "gardener: overnight dispatching tend for c (3/5 candidates this run)...",
        ]
        self.assertEqual(dashboard.parse_batch_progress(lines), (3, 3, 5))


class TestFindFreePort(unittest.TestCase):
    def test_returns_preferred_port_when_free(self):
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        self.assertEqual(dashboard.find_free_port(preferred=free_port), free_port)

    def test_falls_back_to_a_different_port_when_preferred_is_taken(self):
        import socket

        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            taken_port = holder.getsockname()[1]
            got = dashboard.find_free_port(preferred=taken_port)
            self.assertNotEqual(got, taken_port)
        finally:
            holder.close()


class TestBuildStatus(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_empty_state_dir_produces_empty_but_valid_status(self):
        result = dashboard.build_status(state_dir=self.state_dir)
        self.assertEqual(result["runs"], [])
        self.assertEqual(result["garden"], [])
        self.assertEqual(result["merge_allowlist"], [])
        self.assertEqual(result["in_progress"], [])
        self.assertIsNone(result["active_log"])
        self.assertEqual(result["active_logs"], [])
        self.assertIsNone(result["batch_progress"])
        self.assertEqual(result["overnight_next_index"], 0)

    def test_reads_garden_and_allowlist_from_the_given_state_dir_not_the_real_default(self):
        # Regression guard: build_status's state_dir override must actually
        # reach garden.py/merge_allowlist.py/overnight.py, not just
        # state.py's own db path — each of those three otherwise defaults
        # straight to ~/.local/state/gardener/ regardless of what
        # build_status was told, silently leaking this machine's real
        # garden into what should be an isolated test.
        garden.add("owner/repo", path=self.state_dir / "garden.json")
        merge_allowlist.add("owner/repo", path=self.state_dir / "merge_allowlist.json")
        overnight.write_cursor(3, path=self.state_dir / "overnight_cursor.json")

        result = dashboard.build_status(state_dir=self.state_dir)
        self.assertEqual(result["garden"], ["owner/repo"])
        self.assertEqual(result["merge_allowlist"], ["owner/repo"])
        self.assertEqual(result["overnight_next_index"], 3)

    def test_includes_recorded_runs_and_session_cost(self):
        run = state.Run(
            repo="owner/repo", mode="tend", outcome="tend",
            timestamp="2026-07-19T00:00:00+00:00",
            gap_summary="did a thing", cost_usd=1.5,
        )
        state.record_run(run, db_path=self.state_dir / "gardener.sqlite3")

        result = dashboard.build_status(state_dir=self.state_dir)
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["runs"][0]["repo"], "owner/repo")
        self.assertEqual(result["stats"]["session_cost_usd"], 1.5)
        self.assertEqual(result["stats"]["session_run_count"], 1)
        self.assertEqual(result["stats"]["session_started_at"], "2026-07-19T00:00:00+00:00")
        self.assertEqual(result["stats"]["session_ended_at"], "2026-07-19T00:00:00+00:00")

    def test_stats_cover_the_session_not_the_run_limit_window(self):
        """The panel these feed names the window it shows, so the numbers
        have to be that window: a fixed row count reaches back into a
        previous night and reports its errors and spend as this night's
        (issue #105). The Recent runs table keeps the row window."""
        db_path = self.state_dir / "gardener.sqlite3"
        for timestamp, outcome, cost in [
            ("2026-08-08T02:00:00+00:00", "error", 4.0),
            ("2026-08-08T03:00:00+00:00", "tend", 4.0),
            ("2026-08-09T02:00:00+00:00", "tend", 1.0),
            ("2026-08-09T02:40:00+00:00", "error", 0.5),
        ]:
            state.record_run(
                state.Run(
                    repo="owner/repo", mode="tend", outcome=outcome,
                    timestamp=timestamp, cost_usd=cost,
                ),
                db_path=db_path,
            )

        result = dashboard.build_status(state_dir=self.state_dir)
        self.assertEqual(result["stats"]["session_run_count"], 2)
        self.assertEqual(result["stats"]["session_error_count"], 1)
        self.assertEqual(result["stats"]["session_cost_usd"], 1.5)
        self.assertEqual(result["stats"]["session_started_at"], "2026-08-09T02:00:00+00:00")
        # …while the runs table still carries the whole history it asked for.
        self.assertEqual(len(result["runs"]), 4)

    def test_empty_history_reports_a_zeroed_session(self):
        stats = dashboard.build_status(state_dir=self.state_dir)["stats"]
        self.assertEqual(stats["session_run_count"], 0)
        self.assertEqual(stats["session_error_count"], 0)
        self.assertEqual(stats["session_cost_usd"], 0.0)
        self.assertIsNone(stats["session_started_at"])

    def test_active_log_drives_in_progress_and_batch_progress(self):
        logs_dir = self.state_dir / "logs"
        logs_dir.mkdir()
        log_path = logs_dir / "overnight-test.log"
        log_path.write_text(
            "gardener: overnight dispatching tend for a, b "
            "(1-2/9 candidates this run, concurrency=2)...\n"
            "gardener: tending owner/a (allow_merge=True)\n"
            "gardener: tending owner/b (allow_merge=True)\n"
            "notify: sent to Discord: gardener tend: MUTATION — owner/a\n"
        )

        result = dashboard.build_status(state_dir=self.state_dir)
        self.assertEqual(result["active_log"], str(log_path))
        self.assertEqual(result["active_logs"], [str(log_path)])
        self.assertEqual(result["in_progress"], ["owner/b"])
        self.assertEqual(result["batch_progress"], {"start": 1, "end": 2, "total": 9})

    def test_a_newer_manual_tend_log_does_not_hide_the_overnight_run(self):
        # Issue #50, observed live on 2026-07-26: starting a manual
        # `gardener tend` mid-batch made its log the newest, and the whole
        # page switched to it — the overnight run's in-flight repos and
        # batch bar vanished with nothing saying a second run existed.
        import os
        import time

        logs_dir = self.state_dir / "logs"
        logs_dir.mkdir()
        overnight_log = logs_dir / "overnight-1.log"
        overnight_log.write_text(
            "gardener: overnight dispatching tend for owner/a, owner/b "
            "(1-2/9 candidates this run, concurrency=2)...\n"
            "gardener: tending owner/a (allow_merge=True)\n"
            "gardener: tending owner/b (allow_merge=True)\n"
            "gardener: finished tending owner/a\n"
        )
        tend_log = logs_dir / "tend-1.log"
        tend_log.write_text("gardener: tending owner/manual (allow_merge=False)\n")
        now = time.time()
        os.utime(overnight_log, (now - 60, now - 60))
        os.utime(tend_log, (now, now))

        result = dashboard.build_status(state_dir=self.state_dir)
        # The tail still shows one file — the newest — but both runs'
        # in-flight repos are on the page, and the overnight batch bar
        # survives even though the newest log has no batch line of its own.
        self.assertEqual(result["active_log"], str(tend_log))
        self.assertEqual(result["active_logs"], [str(tend_log), str(overnight_log)])
        self.assertEqual(result["in_progress"], ["owner/manual", "owner/b"])
        self.assertEqual(result["batch_progress"], {"start": 1, "end": 2, "total": 9})

    def test_a_repo_in_flight_in_two_logs_is_listed_once(self):
        import os
        import time

        logs_dir = self.state_dir / "logs"
        logs_dir.mkdir()
        for name in ("overnight-1.log", "tend-1.log"):
            path = logs_dir / name
            path.write_text("gardener: tending owner/a (allow_merge=True)\n")
            now = time.time()
            os.utime(path, (now, now))

        result = dashboard.build_status(state_dir=self.state_dir)
        self.assertEqual(result["in_progress"], ["owner/a"])

    def test_corrupt_garden_json_returns_empty_list_not_exception(self):
        # A mid-write or user-corrupted garden.json must not crash the dashboard.
        (self.state_dir / "garden.json").write_text("not valid json{{{")
        result = dashboard.build_status(state_dir=self.state_dir)
        self.assertEqual(result["garden"], [])

    def test_corrupt_merge_allowlist_json_returns_empty_list_not_exception(self):
        (self.state_dir / "merge_allowlist.json").write_text("[1, 2, 3]")  # not strings
        result = dashboard.build_status(state_dir=self.state_dir)
        self.assertEqual(result["merge_allowlist"], [])


class TestBuildGardenRows(unittest.TestCase):
    """The joined garden/allow-list/history view the dashboard renders as
    a table and as the plant plot. Pure — no db, no files."""

    def _stats(self, repo, **kw):
        base = dict(repo=repo, runs=0, successes=0, errors=0)
        base.update(kw)
        return state.RepoStats(**base)

    def test_joins_both_opt_in_lists_with_run_history(self):
        stats = {"owner/a": self._stats("owner/a", runs=5, successes=4, errors=1, cost_usd=1.234,
                                        last_run="2026-07-20T00:00:00+00:00",
                                        last_success="2026-07-19T00:00:00+00:00",
                                        last_outcome="error")}
        rows = dashboard.build_garden_rows(["owner/a"], ["owner/a"], stats, [])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["in_garden"])
        self.assertTrue(row["can_merge"])
        self.assertFalse(row["in_flight"])
        self.assertEqual((row["runs"], row["successes"], row["errors"]), (5, 4, 1))
        self.assertEqual(row["last_success"], "2026-07-19T00:00:00+00:00")
        self.assertEqual(row["last_outcome"], "error")
        self.assertEqual(row["cost_usd"], 1.23)

    def test_garden_repo_with_no_runs_yet_is_a_zeroed_row_not_a_missing_one(self):
        # A just-added repo must still appear (as an unplanted seed in the
        # plot) rather than vanishing until its first dispatch lands.
        rows = dashboard.build_garden_rows(["owner/new"], [], {}, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["runs"], 0)
        self.assertIsNone(rows[0]["last_run"])
        self.assertEqual(rows[0]["cost_usd"], 0.0)

    def test_allowlisted_repo_outside_the_garden_is_still_a_row(self):
        # This is the one fact the old separate allow-list panel carried
        # that the garden panel didn't; folding the panels together must
        # not silently drop it.
        rows = dashboard.build_garden_rows(["owner/a"], ["owner/b"], {}, [])
        self.assertEqual([r["repo"] for r in rows], ["owner/a", "owner/b"])
        self.assertEqual([r["in_garden"] for r in rows], [True, False])
        self.assertEqual([r["can_merge"] for r in rows], [False, True])

    def test_in_progress_repos_are_flagged(self):
        rows = dashboard.build_garden_rows(["owner/a", "owner/b"], [], {}, ["owner/b"])
        self.assertEqual({r["repo"]: r["in_flight"] for r in rows},
                         {"owner/a": False, "owner/b": True})

    def test_rows_are_sorted_by_repo_name(self):
        rows = dashboard.build_garden_rows(["o/c", "o/a"], ["o/b"], {}, [])
        self.assertEqual([r["repo"] for r in rows], ["o/a", "o/b", "o/c"])

    def test_stats_for_a_repo_in_neither_list_are_ignored(self):
        # A repo tended once by a direct `gardener tend --repo X` and never
        # opted in isn't part of the garden and must not appear in it.
        stats = {"owner/stranger": self._stats("owner/stranger", runs=1, successes=1)}
        self.assertEqual(dashboard.build_garden_rows([], [], stats, []), [])


class TestBuildStatusGardenRows(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_garden_rows_are_assembled_from_the_given_state_dir(self):
        garden.add("owner/a", path=self.state_dir / "garden.json")
        merge_allowlist.add("owner/a", path=self.state_dir / "merge_allowlist.json")
        state.record_run(
            state.Run(repo="owner/a", mode="tend", outcome="tend",
                      timestamp="2026-07-19T00:00:00+00:00", cost_usd=2.0),
            db_path=self.state_dir / "gardener.sqlite3",
        )
        rows = dashboard.build_status(state_dir=self.state_dir)["garden_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repo"], "owner/a")
        self.assertTrue(rows[0]["can_merge"])
        self.assertEqual(rows[0]["successes"], 1)
        self.assertEqual(rows[0]["cost_usd"], 2.0)

    def test_empty_state_dir_produces_empty_garden_rows(self):
        self.assertEqual(dashboard.build_status(state_dir=self.state_dir)["garden_rows"], [])


class TestPageHtmlInvariants(unittest.TestCase):
    """The page's in-page JavaScript has no test runner here (stdlib-only
    Python means no JS toolchain), so these assert the properties of it
    that are load-bearing and silently regressible, at the level the Python
    side can actually see: the emitted source text. They are deliberately
    narrow — they check that a specific mechanism is present, not that the
    JavaScript as a whole behaves. Anything about how the plot *looks* is
    still verified by rendering it, per CLAUDE.md."""

    def test_esc_escapes_quotes_because_it_builds_an_attribute_value(self):
        """`esc` output is interpolated into the plot's `title="..."`
        attribute, so &<> alone would let a repo name containing a quote
        close the attribute early. Both quote characters must be in the
        character class and in the replacement map."""
        self.assertIn(
            r'''.replace(/[&<>"']/g''',
            dashboard.PAGE_HTML,
        )
        self.assertIn(r'"\"":"&quot;"', dashboard.PAGE_HTML)
        self.assertIn(r'''"'":"&#39;"''', dashboard.PAGE_HTML)

    def test_the_tooltip_attribute_is_built_through_esc(self):
        """The reason the assertion above matters: this is the attribute
        context. If the tooltip ever stops going through `esc`, escaping
        quotes in `esc` no longer protects anything."""
        self.assertIn('title="${esc(title)}"', dashboard.PAGE_HTML)

    def test_the_poll_loop_is_gated_on_tab_visibility(self):
        """Every poll costs a sqlite aggregate plus a tail read of every
        live log, so a backgrounded tab must not keep polling. The interval
        must run the visibility-checking wrapper, never `refresh` directly."""
        self.assertIn("function tick() { if (!document.hidden) refresh(); }", dashboard.PAGE_HTML)
        self.assertIn("setInterval(tick, 4000)", dashboard.PAGE_HTML)
        self.assertNotIn("setInterval(refresh", dashboard.PAGE_HTML)

    def test_becoming_visible_refreshes_immediately(self):
        """Skipping polls while hidden is only safe because the payload is a
        full snapshot: a single refresh on becoming visible restores the
        page. Without this listener the tab shows stale data for up to 4 s
        at exactly the moment it is looked at."""
        self.assertIn('addEventListener("visibilitychange"', dashboard.PAGE_HTML)

    def test_a_plant_is_a_button_not_a_div(self):
        """The plot's per-repo facts are only reachable on a touch device
        because a plant is a real control: focusable, activatable with
        Enter/Space, and carrying an accessible name (the SVG inside it is
        aria-hidden, and the only visible text is the leaf name). A `div`
        with a click handler would satisfy none of that."""
        self.assertIn('<button type="button" class="${cls}" data-repo="${esc(r.repo)}"', dashboard.PAGE_HTML)
        self.assertIn('aria-label="${esc(title)}" aria-expanded="false" aria-controls="plant-detail"', dashboard.PAGE_HTML)

    def test_the_detail_card_renders_the_two_otherwise_unrendered_row_fields(self):
        """`build_garden_rows` emits `last_run` and `last_outcome`, and
        before the detail card no view rendered either — "the most recent
        attempt errored" was a question the payload could answer and the
        page could not."""
        self.assertIn("r.last_run", dashboard.PAGE_HTML)
        self.assertIn("r.last_outcome", dashboard.PAGE_HTML)

    def test_plot_and_detail_listeners_are_delegated(self):
        """Both the plot and the detail card are replaced wholesale by
        `innerHTML` on re-render, so a listener bound to an individual plant
        would be silently discarded. The listeners must be on the
        containers, which are never replaced."""
        self.assertIn('document.getElementById("plot").addEventListener("click"', dashboard.PAGE_HTML)
        self.assertIn('document.getElementById("plant-detail").addEventListener("click"', dashboard.PAGE_HTML)

    def test_the_detail_card_is_only_rewritten_when_it_changed(self):
        """`renderGarden` runs on every 4 s poll and the detail card is
        re-rendered with it, so an unconditional `innerHTML` would take
        focus off the card's own close button once a poll — the one control
        a keyboard user is most likely to be sitting on while it's open."""
        self.assertIn("if (html !== lastDetailHtml)", dashboard.PAGE_HTML)

    def test_a_focused_plant_survives_the_plot_being_rebuilt(self):
        """The plot's `innerHTML` is replaced wholesale whenever its
        signature changes, which during a run is often. Now that a plant is
        focusable, that would drop the keyboard to the document unless focus
        is captured before the rebuild and restored after it.

        With `preventScroll`, because the rebuild is driven by a poll
        rather than by the reader: without it, a poll would scroll the
        garden panel back into view under someone reading the log tail."""
        self.assertIn('focused.matches("#plot .plant[data-repo]")', dashboard.PAGE_HTML)
        self.assertIn("if (refocus) focusPlant(refocus, true);", dashboard.PAGE_HTML)
        self.assertIn("el.focus({preventScroll: !!preventScroll})", dashboard.PAGE_HTML)

    def test_the_tablist_wires_up_what_it_declares(self):
        """`role="tablist"` promises an association between each tab and
        its panel, and arrow-key navigation within the list. Declaring the
        roles without `aria-controls`/`role="tabpanel"`/`aria-labelledby`
        asserts a relationship to assistive tech that isn't there."""
        self.assertIn('aria-controls="plot-view"', dashboard.PAGE_HTML)
        self.assertIn('aria-controls="table-view"', dashboard.PAGE_HTML)
        self.assertIn('id="plot-view" role="tabpanel" aria-labelledby="tab-plot"', dashboard.PAGE_HTML)
        self.assertIn('id="table-view" role="tabpanel" aria-labelledby="tab-table"', dashboard.PAGE_HTML)
        for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
            self.assertIn(key, dashboard.PAGE_HTML)

    def test_only_the_selected_tab_is_in_the_tab_order(self):
        """The roving tabindex is the other half of the tab pattern: with
        both tabs at tabindex 0, Tab walks through the tablist and the
        arrow keys are redundant. `setTabState` must set it from the same
        selected flag it sets `aria-selected` from, so the two can't
        disagree."""
        self.assertIn("el.setAttribute(\"aria-selected\", String(selected));", dashboard.PAGE_HTML)
        self.assertIn("el.tabIndex = selected ? 0 : -1;", dashboard.PAGE_HTML)

    def test_run_summaries_are_linkified_from_the_raw_string_not_the_escaped_one(self):
        """`linkifyRefs` turns `#194` in a run summary into a link to that
        issue/PR. Escaping first and matching `#\\d+` over the *result*
        finds the digits inside numeric character references: `esc` renders
        an apostrophe as `&#39;`, so the real summary "You've hit your
        session limit" matched `#39` and rendered as `You&#39;ve` with an
        anchor through the middle of the entity. The raw string must be
        tokenized and each literal run escaped separately.

        There is no JS runner here (stdlib-only Python), so this pins the
        shape of the implementation that has to hold — the behaviour itself
        was verified by rendering the page against the real state db."""
        self.assertIn("const re = /#(\\d+)/g;", dashboard.PAGE_HTML)
        self.assertIn("while ((m = re.exec(s)) !== null)", dashboard.PAGE_HTML)
        self.assertIn("out += esc(s.slice(last, m.index))", dashboard.PAGE_HTML)
        self.assertIn("return out + esc(s.slice(last));", dashboard.PAGE_HTML)
        # The regression itself: escaping the whole summary up front and
        # replacing over that result is what produced the mangled entity.
        self.assertNotIn("const safe = esc(summary);", dashboard.PAGE_HTML)

    def test_every_sortable_header_is_a_real_button(self):
        """A `<th>` is not focusable and has no activation behaviour, so a
        click handler on one is a mouse-only control with no keyboard path
        at all (issue #106). Every sortable column must carry a real button,
        and the listener must be bound to that button rather than to the
        cell around it — binding it back to the `<th>` would restore the
        original bug while leaving the markup looking fixed."""
        keys = ("repo", "health", "successes", "errors", "last_success",
                "duration_ms", "cost_usd", "can_merge")
        for key in keys:
            self.assertIn(f'<th scope="col" data-sort="{key}" aria-sort="none"', dashboard.PAGE_HTML)
        self.assertEqual(
            dashboard.PAGE_HTML.count('<button type="button">'),
            len(keys),
            "one activation control per sortable column",
        )
        self.assertIn('const button = th.querySelector("button");', dashboard.PAGE_HTML)
        self.assertIn('button.addEventListener("click"', dashboard.PAGE_HTML)

    def test_the_sorted_column_and_direction_are_rendered_into_the_header(self):
        """`gardenSort` tracks both a key and a direction and used to render
        neither: the rows reordered with nothing saying which column caused
        it or which way it pointed, and `dir` toggles on a repeat click, so
        the exact inverse of the requested order looked identical. The
        header render must run from the same sort state the body does — it
        is called by `renderGardenTable`, so the two can't disagree."""
        self.assertIn("function renderGardenSortHeaders()", dashboard.PAGE_HTML)
        self.assertIn(
            'th.setAttribute("aria-sort", !active ? "none" : ascending ? "ascending" : "descending");',
            dashboard.PAGE_HTML,
        )
        self.assertIn("function renderGardenTable() {\n  renderGardenSortHeaders();", dashboard.PAGE_HTML)

    def test_the_session_panel_states_the_window_it_shows(self):
        """The panel was headed "Tonight" over whatever the last 40 rows
        happened to span (issue #105). The heading now carries the session's
        own start, so the stat tiles must be fed from the session-scoped
        payload keys rather than the row-window ones they replaced."""
        self.assertIn('<span class="sub" id="session-window">', dashboard.PAGE_HTML)
        self.assertIn("sessionWindow(st)", dashboard.PAGE_HTML)
        self.assertIn("st.session_run_count", dashboard.PAGE_HTML)
        for key in ("session_run_count", "session_cost_usd", "session_error_count"):
            self.assertIn("st." + key, dashboard.PAGE_HTML)
        self.assertNotIn("recent_run_count", dashboard.PAGE_HTML)
        self.assertNotIn(">Tonight<", dashboard.PAGE_HTML)

    def test_the_session_caption_shows_both_ends_and_degrades_to_neither(self):
        """A session that finished hours ago would read as one still running
        if only its start were shown — beside an "in flight" tile counting
        something else, that is two windows in one panel. And an unreadable
        timestamp is a case `state.session_stats` deliberately survives, so
        the caption must degrade to an empty string rather than to a
        dangling preposition."""
        self.assertIn("function sessionWindow(stats)", dashboard.PAGE_HTML)
        self.assertIn("stats.session_ended_at", dashboard.PAGE_HTML)
        self.assertIn("if (!start) return end;", dashboard.PAGE_HTML)
        self.assertIn("if (!end || end === start) return start;", dashboard.PAGE_HTML)

    def test_a_failed_poll_marks_the_whole_page_stale(self):
        """Every panel keeps rendering the last good snapshot after a failed
        poll, so changing only the header caption renders a dead server as a
        healthy dashboard. The failure path must set the body-level staleness
        class, and the success path must clear it."""
        self.assertIn('document.body.classList.add("stale")', dashboard.PAGE_HTML)
        self.assertIn('document.body.classList.remove("stale")', dashboard.PAGE_HTML)
        self.assertIn("body.stale main", dashboard.PAGE_HTML)

    def test_a_non_ok_response_is_detected_without_relying_on_a_parse_error(self):
        """A 500 only reached the failure branch by way of `res.json()`
        throwing on its error body, and a 2xx with an unparseable body was
        indistinguishable from a dead server. `res.ok` must be checked
        before the body is parsed at all."""
        self.assertIn("if (!res.ok)", dashboard.PAGE_HTML)
        self.assertIn("markStale(\"server returned \" + res.status)", dashboard.PAGE_HTML)

    def test_each_way_a_poll_can_fail_names_itself(self):
        """`docs/DASHBOARD.md` tabulates four distinct reasons. Collapsing
        any two of them back into one caption would put the doc and the page
        out of step, and would make an unparseable 2xx body read on screen
        as a dead server again."""
        for reason in ("fetch failed", "server returned ", "bad response body", "render failed"):
            self.assertIn(reason, dashboard.PAGE_HTML)

    def test_a_render_that_throws_still_marks_the_page_stale(self):
        """The page is marked fresh only after every panel has rendered, so
        an exception part-way through would otherwise leave *neither* mark
        set: the caption frozen at the last good time, no staleness class,
        and half the panels un-updated — the exact state this is for."""
        self.assertIn("renderStatus(data);", dashboard.PAGE_HTML)
        self.assertIn('markStale("render failed")', dashboard.PAGE_HTML)

    def test_polls_do_not_overlap(self):
        """Both `setInterval` and the `visibilitychange` listener start
        polls. Against a restarting server a slow, doomed request can
        resolve *after* a later successful one and re-mark a live page
        stale; the payload is a whole snapshot, so skipping a poll that
        starts while one is in flight loses nothing.

        A reader-driven refetch (the per-repo runs filter) is the one case
        that must not simply be dropped, so it supersedes instead: the
        running request is aborted and a generation counter discards its
        result. That keeps "never two live requests" true, which is the
        actual invariant — bypassing the guard would restore the race."""
        self.assertIn("if (!force) return;", dashboard.PAGE_HTML)
        self.assertIn("if (inFlightController) inFlightController.abort();", dashboard.PAGE_HTML)
        self.assertIn("const gen = ++pollGeneration;", dashboard.PAGE_HTML)
        self.assertIn("if (gen !== pollGeneration) return;", dashboard.PAGE_HTML)
        self.assertIn("pollInFlight = false;", dashboard.PAGE_HTML)

    def test_the_plot_signature_includes_the_age_string_it_renders(self):
        """The plot is re-rendered only when its signature changes, and the
        age is visible text on every plant as well as its button's
        accessible name. Keyed on `healthOf` alone (2/5/10-day buckets) a
        plot left open overnight keeps reading "just now" while the table
        view, re-rendered unconditionally, reads "3h ago" for the same
        repo."""
        self.assertIn("healthOf(r), fmtAge(daysSince(r.last_success))]", dashboard.PAGE_HTML)

    def test_the_stale_caption_reports_the_snapshot_age_not_a_static_string(self):
        """The point of the staleness treatment is answering "how old is
        what I'm looking at". `generated_at` is already in the payload, so
        the caption is built from it via the seconds-granularity formatter
        rather than `fmtAge`, whose smallest bucket ("just now") covers the
        first hour."""
        self.assertIn("fmtSince(lastGoodAt)", dashboard.PAGE_HTML)
        self.assertNotIn("fetch failed — retrying…", dashboard.PAGE_HTML)


class TestGardenSortOnNarrowViewports(unittest.TestCase):
    """The garden table's header cells carry its sort control as well as
    its labels, and the phone card layout hides the whole `<thead>` — so
    hiding it removed a feature rather than a repetition, on the layout the
    page is explicitly written for (issue #120). These assert the second
    control exists, is offered exactly where the header row isn't, and is
    driven from the same state — at the level the Python side can see, per
    `TestPageHtmlInvariants`' note about there being no JS runner here."""

    PHONE_BLOCK_START = "@media (max-width: 720px) {"

    def phone_block(self) -> str:
        """The stylesheet's narrow-viewport block, which is where the
        `<thead>` is hidden and therefore where the substitute control has
        to be turned on."""
        start = dashboard.PAGE_HTML.index(self.PHONE_BLOCK_START)
        return dashboard.PAGE_HTML[start:dashboard.PAGE_HTML.index("</style>", start)]

    def test_the_substitute_control_is_shown_exactly_where_the_header_row_is_hidden(self):
        """Both rules must live in the same media block. Split across two
        breakpoints, a range of widths would show either both controls or
        neither — and "neither" is the bug this fixes, silently restored."""
        block = self.phone_block()
        self.assertIn("table thead { display: none; }", block)
        self.assertIn(".table-sort { display: flex; }", block)
        # ...and it is off by default, so the wide layout keeps the header
        # cells as its only sort control rather than growing a second one.
        default_rules = dashboard.PAGE_HTML[:dashboard.PAGE_HTML.index(self.PHONE_BLOCK_START)]
        self.assertIn(".table-sort {\n    display: none;", default_rules)

    def test_the_control_sits_inside_the_table_view_not_the_shared_toolbar(self):
        """Sorting reorders the table only — the plot orders itself by
        attention — so in the panel's shared toolbar this control would sit
        beside the view tabs doing nothing whenever the plot is on screen."""
        table_view = dashboard.PAGE_HTML.index('id="table-view"')
        table = dashboard.PAGE_HTML.index('<table class="garden-table"', table_view)
        self.assertIn('<div class="table-sort">', dashboard.PAGE_HTML[table_view:table])

    def test_both_controls_change_the_sort_through_one_entry_point(self):
        """Two controls over one `gardenSort` is the whole risk here: a
        second writer that sets the state directly would reorder the rows
        while the header cells went on describing the previous order.
        `setGardenSort` must be the only assignment to it after the
        initial declaration."""
        self.assertIn("function setGardenSort(key, dir) {\n  gardenSort = {key, dir};", dashboard.PAGE_HTML)
        self.assertIn("setGardenSort(key, gardenSort.key === key ? -gardenSort.dir : 1);", dashboard.PAGE_HTML)
        self.assertIn("setGardenSort(ev.target.value, gardenSort.dir);", dashboard.PAGE_HTML)
        self.assertIn("setGardenSort(gardenSort.key, -gardenSort.dir);", dashboard.PAGE_HTML)
        self.assertEqual(
            dashboard.PAGE_HTML.count("gardenSort = {"),
            2,
            "only the initial declaration and setGardenSort may assign gardenSort",
        )

    def test_the_rendered_sort_state_reaches_both_controls_from_one_read(self):
        """`renderGardenSortHeaders` already runs from `renderGardenTable`,
        i.e. from the same call that renders the rows. Syncing the select
        from there rather than from its own path is what keeps the caret,
        `aria-sort` and the select from disagreeing."""
        self.assertIn("  }\n  syncGardenSortControl();\n}", dashboard.PAGE_HTML)
        self.assertIn("if (select && select.value !== gardenSort.key) select.value = gardenSort.key;",
                      dashboard.PAGE_HTML)

    def test_the_options_are_derived_from_the_header_cells(self):
        """A hand-written second list of columns is what would drift: a
        column added to the `<thead>` must appear in the select or in
        neither control, never in only one."""
        self.assertIn('for (const th of document.querySelectorAll(".garden-table th[data-sort]")) {\n'
                      "    const option = document.createElement(\"option\");",
                      dashboard.PAGE_HTML)
        self.assertIn("option.value = th.dataset.sort;", dashboard.PAGE_HTML)
        self.assertNotIn("<option value=", dashboard.PAGE_HTML)
        # The caret span sits inside the same button as the label, so
        # `textContent` on the button would put "▲" in every option.
        self.assertIn("(first && first.textContent) || th.dataset.sort", dashboard.PAGE_HTML)

    def test_the_direction_button_names_its_direction_in_words(self):
        """The caret is `aria-hidden` (issue #130), so without an explicit
        accessible name the only control for sort direction on a phone
        would announce as an unnamed button."""
        self.assertIn('<span aria-hidden="true" id="garden-sort-dir-glyph">', dashboard.PAGE_HTML)
        self.assertIn('button.setAttribute("aria-label", ascending', dashboard.PAGE_HTML)
        self.assertIn('"Sorted ascending. Activate to sort descending."', dashboard.PAGE_HTML)
        self.assertIn('"Sorted descending. Activate to sort ascending."', dashboard.PAGE_HTML)
        # The visible label is a word, not the glyph, so the accessible
        # name contains it (WCAG's label-in-name).
        self.assertIn('ascending" : "descending"', dashboard.PAGE_HTML)

    def test_the_select_is_labelled_and_wired_before_the_first_render(self):
        """`syncGardenSortControl` sets `select.value`, which silently
        resolves to "" against an empty option list — so the options have
        to be built before the initial header render, not on the first
        poll."""
        self.assertIn('<label for="garden-sort">', dashboard.PAGE_HTML)
        self.assertIn('<select id="garden-sort"></select>', dashboard.PAGE_HTML)
        self.assertLess(
            dashboard.PAGE_HTML.index("\nbuildGardenSortOptions();"),
            dashboard.PAGE_HTML.index("\nrenderGardenSortHeaders();"),
        )


class TestRunServerLoopbackEnforcement(unittest.TestCase):
    def test_non_loopback_host_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            dashboard.run_server(host="0.0.0.0", port=19999)
        self.assertIn("loopback", str(ctx.exception))

    def test_loopback_host_does_not_raise_before_binding(self):
        # run_server blocks forever once it binds; we only want to confirm
        # the loopback validation passes for 127.0.0.1 — interrupt
        # immediately after the check by mocking serve_forever.
        import unittest.mock as mock
        with mock.patch(
            "gardener.dashboard.ThreadingHTTPServer",
            side_effect=KeyboardInterrupt,
        ):
            # Should not raise ValueError (loopback check passes).
            try:
                dashboard.run_server(host="127.0.0.1", port=19999)
            except KeyboardInterrupt:
                pass

    def test_unresolvable_host_raises_value_error(self):
        """The guard's other rejection path. A name that doesn't resolve must
        fail closed the same way a non-loopback one does — `getaddrinfo`
        raising must not escape as a bare `socket.gaierror`, which a caller
        checking for `ValueError` would not catch."""
        with patch("gardener.dashboard.socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            with self.assertRaises(ValueError) as ctx:
                dashboard.run_server(host="not-a-real-host.invalid", port=19999)
        self.assertIn("could not be resolved", str(ctx.exception))

    def test_no_socket_is_bound_when_the_host_is_rejected(self):
        """The point of validating before binding: a rejected host must not
        reach `ThreadingHTTPServer` at all."""
        with patch("gardener.dashboard.ThreadingHTTPServer") as mock_server:
            with self.assertRaises(ValueError):
                dashboard.run_server(host="0.0.0.0", port=19999)
        mock_server.assert_not_called()


class TestRunServerLifecycle(unittest.TestCase):
    """`run_server`'s serve/shutdown path. The existing loopback tests stop
    at the validation boundary (they make `ThreadingHTTPServer` itself
    raise), so nothing previously exercised what happens once a server
    object exists."""

    def setUp(self):
        # run_server sets this class attribute; restore it so ordering
        # between tests can't matter.
        original = dashboard._DashboardHandler.state_dir
        self.addCleanup(setattr, dashboard._DashboardHandler, "state_dir", original)

    def test_keyboard_interrupt_is_swallowed_and_the_server_is_closed(self):
        """Ctrl+C is the documented way to stop the dashboard, so it must
        exit cleanly rather than surfacing a traceback — and must still
        release the socket on the way out."""
        httpd = MagicMock()
        httpd.serve_forever.side_effect = KeyboardInterrupt
        with patch("gardener.dashboard.ThreadingHTTPServer", return_value=httpd):
            with redirect_stderr(io.StringIO()):
                dashboard.run_server(host="127.0.0.1", port=19999)  # must not raise
        httpd.serve_forever.assert_called_once()
        httpd.server_close.assert_called_once()

    def test_server_is_closed_even_if_serve_forever_fails(self):
        """The `finally` is the load-bearing part — an unexpected error must
        not leak the bound port."""
        httpd = MagicMock()
        httpd.serve_forever.side_effect = RuntimeError("boom")
        with patch("gardener.dashboard.ThreadingHTTPServer", return_value=httpd):
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    dashboard.run_server(host="127.0.0.1", port=19999)
        httpd.server_close.assert_called_once()

    def test_state_dir_reaches_the_handler_class_before_serving(self):
        """`http.server` constructs a handler per request, so per-server
        config can only reach it as a class attribute. Assert the wiring
        rather than trusting the comment that explains it."""
        seen = {}
        httpd = MagicMock()
        httpd.serve_forever.side_effect = lambda: seen.setdefault(
            "state_dir", dashboard._DashboardHandler.state_dir
        )
        with patch("gardener.dashboard.ThreadingHTTPServer", return_value=httpd):
            with redirect_stderr(io.StringIO()):
                dashboard.run_server(host="127.0.0.1", port=19999, state_dir=Path("/tmp/some-state"))
        self.assertEqual(seen["state_dir"], Path("/tmp/some-state"))


if __name__ == "__main__":
    unittest.main()


class TestStatusQuery(unittest.TestCase):
    """`/api/status`'s query string. Total by design: this runs inside the
    poll path, so a malformed hand-typed URL must degrade to the default
    page rather than to a 500."""

    def test_no_query_is_no_overrides(self):
        self.assertEqual(dashboard._status_query(""), {})

    def test_limit_and_repo_are_parsed(self):
        self.assertEqual(
            dashboard._status_query("limit=5&repo=owner/name"),
            {"run_limit": 5, "repo": "owner/name"},
        )

    def test_unparseable_limit_falls_back_to_the_default(self):
        self.assertEqual(dashboard._status_query("limit=lots"), {})

    def test_limit_is_clamped_at_both_ends(self):
        self.assertEqual(dashboard._status_query("limit=0")["run_limit"], 1)
        self.assertEqual(dashboard._status_query("limit=-3")["run_limit"], 1)
        self.assertEqual(
            dashboard._status_query("limit=99999")["run_limit"], dashboard.MAX_RUN_LIMIT
        )

    def test_an_empty_repo_is_not_a_filter(self):
        self.assertEqual(dashboard._status_query("repo="), {})

    def test_unknown_parameters_are_ignored(self):
        self.assertEqual(dashboard._status_query("nope=1&limit=7"), {"run_limit": 7})


class TestStatusEndpointErrorHandling(unittest.TestCase):
    """`BaseHTTPRequestHandler.handle_one_request` does not catch exceptions
    from the dispatched method, so an unguarded raise closed the socket
    having written zero bytes — which the page reports as `fetch failed`,
    i.e. "the server is gone", for what is usually a transient read
    error (issue #121)."""

    def _handler(self, build_status):
        # Exercised without a socket: do_GET's two write paths are driven
        # through _send, which is what gets recorded here.
        handler = dashboard._DashboardHandler.__new__(dashboard._DashboardHandler)
        handler.state_dir = None
        handler.path = "/api/status"
        sent = {}

        def _send(code, content_type, body):
            sent.update(code=code, content_type=content_type, body=body)

        handler._send = _send
        with patch.object(dashboard, "build_status", build_status):
            handler.do_GET()
        return sent

    def test_a_raising_build_status_returns_a_real_500_with_a_json_body(self):
        def boom(**kwargs):
            raise OSError("log pruned mid-poll")

        sent = self._handler(boom)
        self.assertEqual(sent["code"], 500)
        self.assertIn("application/json", sent["content_type"])
        payload = json.loads(sent["body"].decode("utf-8"))
        self.assertEqual(payload["error"], "OSError")
        self.assertIn("log pruned mid-poll", payload["detail"])

    def test_a_working_build_status_still_returns_200(self):
        sent = self._handler(lambda **kwargs: {"ok": True})
        self.assertEqual(sent["code"], 200)
        self.assertEqual(json.loads(sent["body"].decode("utf-8")), {"ok": True})

    def test_query_parameters_reach_build_status(self):
        seen = {}

        def capture(**kwargs):
            seen.update(kwargs)
            return {}

        handler = dashboard._DashboardHandler.__new__(dashboard._DashboardHandler)
        handler.state_dir = None
        handler.path = "/api/status?repo=o/r&limit=3"
        handler._send = lambda *a: None
        with patch.object(dashboard, "build_status", capture):
            handler.do_GET()
        self.assertEqual(seen["repo"], "o/r")
        self.assertEqual(seen["run_limit"], 3)


class TestFindActiveLogSurvivesPruning(unittest.TestCase):
    """`run_log.prune_old_logs` runs at the start of every dispatching run
    and can delete a log between the glob and the stat. `find_active_logs`
    guarded that from the beginning; `find_active_log` did not, and it is
    the path every poll takes once no log is fresh enough to be active."""

    def test_a_log_vanishing_between_glob_and_stat_is_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            (logs / "old.log").write_text("x")
            (logs / "new.log").write_text("y")
            real_stat = Path.stat

            def flaky_stat(self, *args, **kwargs):
                if self.name == "old.log":
                    raise OSError("pruned between the glob and the stat")
                return real_stat(self, *args, **kwargs)

            # `is_file()` is patched alongside `stat()` for the reason
            # `TestFindActiveLogs` spells out: it calls `stat()` internally
            # and swallows `OSError` by returning False, so patching
            # `stat()` alone passes via the guard without ever reaching the
            # `except OSError` branch this exercises.
            with patch.object(Path, "is_file", lambda self: True):
                with patch.object(Path, "stat", flaky_stat):
                    found = dashboard.find_active_log(logs)
            self.assertEqual(found.name, "new.log")

    def test_every_log_vanishing_yields_none_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td)
            (logs / "a.log").write_text("x")

            real_stat = Path.stat

            def gone_stat(self, *args, **kwargs):
                # Scoped to the logs themselves: the directory's own
                # `exists()` check stats too, and failing that would be
                # testing a different thing entirely.
                if self.suffix == ".log":
                    raise OSError("pruned")
                return real_stat(self, *args, **kwargs)

            with patch.object(Path, "is_file", lambda self: True):
                with patch.object(Path, "stat", gone_stat):
                    self.assertIsNone(dashboard.find_active_log(logs))
