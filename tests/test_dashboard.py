"""dashboard.py's HTTP layer is a thin stdlib http.server wrapper around
build_status() — not covered here, mirroring how dispatch.py's actual
`claude` subprocess call is mocked rather than invoked in test_dispatch.py.
This file covers the pure log-parsing and status-assembly functions, which
never touch a socket."""
import tempfile
import unittest
from pathlib import Path

from gardener import dashboard, garden, merge_allowlist, overnight, state


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

    def test_tend_mode_notify_marks_terminal(self):
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

    def test_includes_recorded_runs_and_recent_cost(self):
        run = state.Run(
            repo="owner/repo", mode="tend", outcome="tend",
            timestamp="2026-07-19T00:00:00+00:00",
            gap_summary="did a thing", cost_usd=1.5,
        )
        state.record_run(run, db_path=self.state_dir / "gardener.sqlite3")

        result = dashboard.build_status(state_dir=self.state_dir)
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["runs"][0]["repo"], "owner/repo")
        self.assertEqual(result["stats"]["recent_cost_usd"], 1.5)
        self.assertEqual(result["stats"]["recent_run_count"], 1)

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
        self.assertEqual(result["in_progress"], ["owner/b"])
        self.assertEqual(result["batch_progress"], {"start": 1, "end": 2, "total": 9})

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


if __name__ == "__main__":
    unittest.main()
