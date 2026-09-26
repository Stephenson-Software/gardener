"""live.py's parsing and payload assembly. Like test_dashboard.py this
never opens a socket beyond driving `do_GET` directly, and never reads the
operator's real state: every test builds its own state dir, run log, and
synthetic transcript files in a tmp dir."""
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from gardener import dashboard, live, state, transcript

NOW = datetime(2026, 9, 26, 3, 0, 0, tzinfo=timezone.utc)


def _run(repo, at, outcome="tend", cost=1.0, summary="did a thing", duration_ms=60_000):
    return state.Run(
        repo=repo,
        mode="tend",
        outcome=outcome,
        timestamp=at.isoformat(timespec="seconds"),
        gap_summary=summary,
        duration_ms=duration_ms,
        cost_usd=cost,
    )


LIMIT_TEXT = "You've hit your session limit · resets 6am (America/Denver)"


class TestCurrentBatchLines(unittest.TestCase):
    def test_returns_the_newest_batch_and_the_lines_from_it_on(self):
        lines = [
            "gardener: overnight dispatching tend for o/a, o/b (1-2/4 candidates this run, concurrency=2)...",
            "gardener: o/a checked out at /c/o__a",
            "gardener: overnight dispatching tend for o/c, o/d (3-4/4 candidates this run, concurrency=2)...",
            "gardener: o/c checked out at /c/o__c",
        ]
        repos, scoped = live.current_batch_lines(lines)
        self.assertEqual(repos, ["o/c", "o/d"])
        self.assertEqual(scoped, lines[2:])

    def test_the_sequential_single_repo_form_is_a_batch_of_one(self):
        repos, _ = live.current_batch_lines(
            ["gardener: overnight dispatching tend for o/a (3/9 candidates this run)..."]
        )
        self.assertEqual(repos, ["o/a"])

    def test_no_batch_line_is_empty(self):
        self.assertEqual(live.current_batch_lines(["gardener: tending o/a (allow_merge=True)"]), ([], []))


class TestLogParsing(unittest.TestCase):
    def test_clone_paths_skip_the_conventions_clone(self):
        found = live.parse_clone_paths(
            [
                "gardener: conventions checked out at /c/conv",
                "gardener: o/a checked out at /root/.cache/gardener/repos/o__a",
            ]
        )
        self.assertEqual(found, {"o/a": "/root/.cache/gardener/repos/o__a"})

    def test_transcript_is_matched_by_clone_dir_not_by_position(self):
        # Concurrent dispatches interleave their lines, so the transcript
        # announced right after a repo's clone line may be a neighbour's.
        clone_a = "/root/.cache/gardener/repos/o__a"
        clone_b = "/root/.cache/gardener/repos/o__b"
        path_a = f"/p/{transcript.encode_cwd(clone_a)}/1.jsonl"
        path_b = f"/p/{transcript.encode_cwd(clone_b)}/2.jsonl"
        paths = live.parse_transcript_paths(
            [
                f"gardener: session transcript: {path_b} (tail -f it for live detail, or ...)",
                f"gardener: session transcript: {path_a} (tail -f it for live detail, or ...)",
            ]
        )
        self.assertEqual(live.transcript_for_clone(clone_a, paths), path_a)
        self.assertEqual(live.transcript_for_clone(clone_b, paths), path_b)
        self.assertIsNone(live.transcript_for_clone("/root/.cache/gardener/repos/o__c", paths))

    def test_the_later_transcript_in_a_clone_wins(self):
        # A bootstrap (create-dev-loop) then the tend itself, same cwd.
        clone = "/c/o__a"
        d = transcript.encode_cwd(clone)
        self.assertEqual(
            live.transcript_for_clone(clone, [f"/p/{d}/boot.jsonl", f"/p/{d}/tend.jsonl"]),
            f"/p/{d}/tend.jsonl",
        )

    def test_run_end_prefers_the_abort_over_the_done_summary_after_it(self):
        end = live.parse_run_end(
            [
                "gardener: overnight aborting — the usage/session limit is exhausted. Not advancing the resume cursor ...",
                "gardener: overnight done — 12 repo(s) tended, 3 error(s)",
            ]
        )
        self.assertEqual(end["kind"], "aborted")
        self.assertEqual(end["reason"], "the usage/session limit is exhausted")
        self.assertEqual(end["summary"], "12 repo(s) tended, 3 error(s)")

    def test_run_end_plain_done_and_still_running(self):
        self.assertEqual(
            live.parse_run_end(["gardener: overnight done — 4 repo(s) tended"])["kind"], "done"
        )
        self.assertIsNone(live.parse_run_end(["gardener: tending o/a (allow_merge=True)"]))


class TestTranscriptLineRoundTrip(unittest.TestCase):
    def test_the_line_transcript_py_prints_is_matched_back_to_its_clone(self):
        # Producer/consumer contract, same shape as test_cli.py's batch-line
        # round trip: the real `log_transcript_when_found` output, never a
        # hand-copied fixture, read back by the real parser.
        with tempfile.TemporaryDirectory() as tmp:
            clone = "/root/.cache/gardener/repos/Owner__Some-Repo"
            with patch.dict(os.environ, {transcript.CLAUDE_CONFIG_DIR_ENV: tmp}):
                d = transcript.project_transcript_dir(clone)
                d.mkdir(parents=True)
                (d / "abc.jsonl").write_text("")
                out = io.StringIO()
                transcript.log_transcript_when_found(clone, after=0, stream=out, timeout=0, sleep_fn=lambda s: None)
            paths = live.parse_transcript_paths(out.getvalue().splitlines())
            self.assertEqual(live.transcript_for_clone(clone, paths), str(d / "abc.jsonl"))


class TestToolDetail(unittest.TestCase):
    def test_prefers_description_and_strips_the_clone_prefix(self):
        self.assertEqual(
            live.tool_detail({"command": "ls", "description": "List files"}, "/c/o__a"), "List files"
        )
        self.assertEqual(live.tool_detail({"file_path": "/c/o__a/src/x.py"}, "/c/o__a"), "src/x.py")

    def test_home_paths_are_shortened(self):
        home = str(Path.home())
        self.assertEqual(
            live.tool_detail({"file_path": f"{home}/.claude/skills/x/SKILL.md"}), "~/.claude/skills/x/SKILL.md"
        )

    def test_non_dict_input_is_empty(self):
        self.assertEqual(live.tool_detail(None), "")


def _assistant(ts, msg_id, blocks, out_tokens=10):
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {
                "id": msg_id,
                "model": "claude-opus-5-5",
                "content": blocks,
                "usage": {"output_tokens": out_tokens},
            },
        }
    )


class TestTranscriptCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "t.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def _append(self, text):
        with self.path.open("a", encoding="utf-8") as f:
            f.write(text)

    def test_counts_tools_once_and_tokens_once_per_message(self):
        # One message is written as one line per content block, each
        # repeating the message's usage.
        self._append(
            json.dumps({"type": "queue-operation", "timestamp": "2026-09-26T02:00:00Z"}) + "\n"
            + _assistant("2026-09-26T02:00:05Z", "m1", [{"type": "text", "text": "Reading the backlog."}], 40) + "\n"
            + _assistant("2026-09-26T02:00:06Z", "m1",
                         [{"type": "tool_use", "id": "t1", "name": "Bash",
                           "input": {"command": "gh issue list", "description": "List issues"}}], 40) + "\n"
        )
        s = live.TranscriptCache().summary(str(self.path))
        self.assertEqual(s["started_at"], "2026-09-26T02:00:00Z")
        self.assertEqual(s["last_event_at"], "2026-09-26T02:00:06Z")
        self.assertEqual(s["tool_calls"], 1)
        self.assertEqual(s["output_tokens"], 40)
        self.assertEqual(s["last_tool"], {"name": "Bash", "detail": "List issues", "at": "2026-09-26T02:00:06Z"})
        self.assertEqual(s["last_text"]["text"], "Reading the backlog.")
        self.assertEqual(s["model"], "claude-opus-5-5")

    def test_reads_incrementally_and_leaves_a_partial_line_for_later(self):
        cache = live.TranscriptCache()
        line = _assistant("2026-09-26T02:00:05Z", "m1",
                          [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/x"}}])
        self._append(line + "\n" + line[:20])
        self.assertEqual(cache.summary(str(self.path))["tool_calls"], 1)
        second = _assistant("2026-09-26T02:00:09Z", "m2",
                            [{"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "/y"}}])
        # Complete the dangling fragment as garbage (it was never valid) and
        # add a real line: the fragment must not have been half-parsed.
        self._append("\n" + second + "\n")
        s = cache.summary(str(self.path))
        self.assertEqual(s["tool_calls"], 2)
        self.assertEqual(s["last_tool"]["name"], "Edit")

    def test_a_shrunk_file_is_reread_from_the_start(self):
        cache = live.TranscriptCache()
        tool = lambda i: _assistant("2026-09-26T02:00:05Z", f"m{i}",
                                    [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}])
        self._append(tool(1) + "\n" + tool(2) + "\n")
        self.assertEqual(cache.summary(str(self.path))["tool_calls"], 2)
        self.path.write_text(tool(3) + "\n")
        self.assertEqual(cache.summary(str(self.path))["tool_calls"], 1)

    def test_rate_limit_error_is_recorded(self):
        # The shape Claude Code writes when the usage window is exhausted,
        # copied from a real transcript.
        self._append(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-09-25T09:41:20Z",
                    "isApiErrorMessage": True,
                    "error": "rate_limit",
                    "message": {"id": "e", "content": [{"type": "text", "text": LIMIT_TEXT}]},
                }
            )
            + "\n"
        )
        s = live.TranscriptCache().summary(str(self.path))
        self.assertEqual(s["rate_limit"], {"message": LIMIT_TEXT, "at": "2026-09-25T09:41:20Z"})
        self.assertIsNone(s["api_error"])

    def test_missing_file_and_retain(self):
        cache = live.TranscriptCache()
        self.assertIsNone(cache.summary(str(self.path)))
        self._append(_assistant("2026-09-26T02:00:05Z", "m1", []) + "\n")
        cache.summary(str(self.path))
        cache.retain([])
        self.assertEqual(cache._states, {})


class TestLimit(unittest.TestCase):
    def test_hits_cluster_and_carry_the_trailing_spend_before_them(self):
        hit_at = NOW - timedelta(days=1)
        runs = [
            _run("o/a", hit_at - timedelta(hours=6), cost=500.0),  # outside the 5 h window
            _run("o/b", hit_at - timedelta(hours=4), cost=30.0),
            _run("o/c", hit_at - timedelta(hours=1), cost=50.0),
            _run("o/d", hit_at, outcome="error", cost=0.5, summary=LIMIT_TEXT),
            _run("o/e", hit_at + timedelta(seconds=3), outcome="error", cost=0.5, summary=LIMIT_TEXT),
        ]
        hits = live.limit_hits(runs)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["repos"], 2)
        self.assertEqual(hits[0]["trailing_cost_usd"], 80.0)
        self.assertIn("resets 6am", hits[0]["message"])

    def test_a_successful_run_discussing_rate_limits_is_not_a_hit(self):
        runs = [_run("o/a", NOW, summary="added a usage limit check to the API")]
        self.assertEqual(live.limit_hits(runs), [])

    def test_levels(self):
        hit_at = NOW - timedelta(days=1)
        history = [
            _run("o/b", hit_at - timedelta(hours=2), cost=60.0),
            _run("o/d", hit_at, outcome="error", summary=LIMIT_TEXT, cost=0.0),
        ]
        quiet = live.build_limit(history + [_run("o/x", NOW - timedelta(hours=1), cost=10.0)], NOW, None)
        self.assertEqual(quiet["level"], "ok")
        self.assertEqual(quiet["current_cost_usd"], 10.0)
        self.assertEqual(quiet["band"], {"min": 60.0, "median": 60.0, "max": 60.0})
        near = live.build_limit(history + [_run("o/x", NOW - timedelta(hours=1), cost=61.0)], NOW, None)
        self.assertEqual(near["level"], "near")
        hit = live.build_limit(history, NOW, {"repo": "o/z", "message": LIMIT_TEXT, "at": None})
        self.assertEqual(hit["level"], "hit")

    def test_no_history_has_no_band(self):
        limit = live.build_limit([], NOW, None)
        self.assertIsNone(limit["band"])
        self.assertEqual(limit["level"], "ok")


class TestRunsSince(unittest.TestCase):
    def test_returns_the_window_oldest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "g.sqlite3"
            for i, hours in enumerate((10, 3, 1)):
                state.record_run(_run(f"o/{i}", NOW - timedelta(hours=hours)), db_path=db)
            got = state.runs_since(NOW - timedelta(hours=5), db_path=db)
            self.assertEqual([r.repo for r in got], ["o/1", "o/2"])
            self.assertEqual(state.runs_since(NOW, db_path=Path(tmp) / "missing.sqlite3"), [])


class TestBuildLive(unittest.TestCase):
    """End to end over a synthetic state dir: a log shaped exactly like
    `cmd_overnight`'s narration, two transcripts, and a run history."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "state"
        self.logs = self.base / "logs"
        self.logs.mkdir(parents=True)
        self.projects = Path(self._tmp.name) / "projects"
        self.db = self.base / "gardener.sqlite3"
        self.started = datetime(2026, 9, 26, 1, 0, 0)  # naive local, like the log stamp
        self.log = self.logs / f"overnight-{self.started:%Y%m%d-%H%M%S}.log"
        self.now = datetime.fromtimestamp(self.started.timestamp(), tz=timezone.utc) + timedelta(hours=1)

    def tearDown(self):
        self._tmp.cleanup()

    def _transcript(self, clone, lines):
        d = self.projects / transcript.encode_cwd(clone)
        d.mkdir(parents=True, exist_ok=True)
        p = d / "s.jsonl"
        p.write_text("".join(line + "\n" for line in lines))
        return p

    def _write_log(self, extra=()):
        clone_a = "/cache/o__a"
        clone_b = "/cache/o__b"
        recent = (self.now - timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
        stale = (self.now - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
        ta = self._transcript(clone_a, [_assistant(recent, "m1", [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"description": "Run tests"}}])])
        tb = self._transcript(clone_b, [_assistant(stale, "m1", [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"description": "Build"}}])])
        lines = [
            "gardener: overnight starting — 10 repo(s) in garden, strategy=random, budget=8.0h, 0 repo(s) already attempted this cycle",
            "gardener: overnight dispatching tend for o/z, o/y (1-2/10 candidates this run, concurrency=2)...",
            "gardener: tending o/z (allow_merge=True)",
            "gardener: tending o/y (allow_merge=True)",
            "gardener: finished tending o/z",
            "gardener: finished tending o/y",
            "gardener: overnight dispatching tend for o/a, o/b, o/c (3-5/10 candidates this run, concurrency=3)...",
            "gardener: tending o/a (allow_merge=True)",
            "gardener: tending o/b (allow_merge=True)",
            "gardener: tending o/c (allow_merge=True)",
            f"gardener: o/b checked out at {clone_b}",
            f"gardener: o/a checked out at {clone_a}",
            f"gardener: session transcript: {tb} (tail -f it for live detail)",
            f"gardener: session transcript: {ta} (tail -f it for live detail)",
            "gardener: o/c checked out at /cache/o__c",
            *extra,
        ]
        self.log.write_text("\n".join(lines) + "\n")
        # The log's mtime is what "still being written" is judged by, so it
        # has to be on the test's clock rather than the wall clock.
        written = (self.now - timedelta(seconds=10)).timestamp()
        os.utime(self.log, (written, written))

    def _build(self):
        return live.build_live(state_dir=self.base, now=self.now, cache=live.TranscriptCache())

    def test_slots_phases_and_quiet_flag(self):
        self._write_log()
        state.record_run(_run("o/z", self.now - timedelta(minutes=30), cost=2.0), db_path=self.db)
        state.record_run(_run("o/y", self.now - timedelta(minutes=29), outcome="error", cost=1.0,
                              summary="tests failed"), db_path=self.db)
        d = self._build()
        self.assertEqual(d["schema"], live.LIVE_SCHEMA)
        slots = {s["repo"]: s for s in d["slots"]}
        self.assertEqual(list(slots), ["o/a", "o/b", "o/c"])
        self.assertEqual(slots["o/a"]["phase"], "running")
        self.assertEqual(slots["o/a"]["activity"]["last_tool"]["detail"], "Run tests")
        self.assertFalse(slots["o/a"]["stalled"])
        self.assertTrue(slots["o/b"]["stalled"])
        self.assertEqual(slots["o/c"]["phase"], "preparing")
        run = d["run"]
        # No sessions dir in this state dir: falls back to "no end line and
        # the log is fresh" — the file was just written.
        self.assertEqual(run["budget_hours"], 8.0)
        self.assertEqual(run["concurrency"], 3)
        self.assertEqual(run["batch"], {"start": 3, "end": 5, "total": 10})
        self.assertEqual(run["pace"]["finished"], 2)
        self.assertEqual(run["pace"]["errors"], 1)
        self.assertEqual(run["pace"]["cost_usd"], 3.0)
        self.assertEqual([f["repo"] for f in d["finished"]], ["o/y", "o/z"])
        self.assertTrue(any("o/b" in a["text"] and a["level"] == "warn" for a in d["alerts"]))

    def test_finished_slot_shows_its_recorded_result(self):
        self._write_log(extra=["gardener: finished tending o/a"])
        state.record_run(_run("o/a", self.now - timedelta(minutes=1), cost=1.25), db_path=self.db)
        slots = {s["repo"]: s for s in self._build()["slots"]}
        self.assertEqual(slots["o/a"]["phase"], "finished")
        self.assertEqual(slots["o/a"]["result"]["cost_usd"], 1.25)

    def test_a_limit_failure_this_run_raises_the_hit(self):
        self._write_log()
        state.record_run(_run("o/z", self.now - timedelta(minutes=5), outcome="error", cost=0.0,
                              summary=LIMIT_TEXT), db_path=self.db)
        d = self._build()
        self.assertEqual(d["limit"]["level"], "hit")
        self.assertEqual(d["limit"]["live_hit"]["repo"], "o/z")
        self.assertEqual(d["alerts"][0]["level"], "error")

    def test_a_log_without_a_live_session_reads_as_stopped(self):
        self._write_log()
        (self.base / "sessions").mkdir()  # registry exists, nothing running
        d = self._build()
        self.assertFalse(d["run"]["alive"])
        self.assertIsNone(d["run"]["end"])
        self.assertTrue(all(s["phase"] in ("stopped", "finished") for s in d["slots"]))
        self.assertTrue(any("stopped or" in a["text"] for a in d["alerts"]))

    def test_empty_state_dir(self):
        d = live.build_live(state_dir=self.base, now=self.now, cache=live.TranscriptCache())
        self.assertIsNone(d["run"])
        self.assertEqual(d["slots"], [])
        self.assertEqual(d["limit"]["level"], "ok")


class TestLiveEndpoints(unittest.TestCase):
    def _get(self, path, build_live=None):
        handler = dashboard._DashboardHandler.__new__(dashboard._DashboardHandler)
        handler.state_dir = None
        handler.path = path
        sent = {}
        handler._send = lambda code, content_type, body: sent.update(code=code, content_type=content_type, body=body)
        if build_live is None:
            handler.do_GET()
        else:
            with patch.object(live, "build_live", build_live):
                handler.do_GET()
        return sent

    def test_page_is_served_with_its_schema_baked_in(self):
        sent = self._get("/live")
        self.assertEqual(sent["code"], 200)
        body = sent["body"].decode("utf-8")
        self.assertIn(f"const SCHEMA = {live.LIVE_SCHEMA};", body)
        self.assertNotIn("%%", body)

    def test_a_raising_build_live_is_a_real_500(self):
        def boom(**kwargs):
            raise OSError("transcript vanished")

        with redirect_stderr_quiet():
            sent = self._get("/api/live", boom)
        self.assertEqual(sent["code"], 500)
        self.assertEqual(json.loads(sent["body"])["error"], "OSError")

    def test_api_returns_the_payload(self):
        sent = self._get("/api/live", lambda **kwargs: {"schema": live.LIVE_SCHEMA})
        self.assertEqual(sent["code"], 200)
        self.assertEqual(json.loads(sent["body"]), {"schema": live.LIVE_SCHEMA})


def redirect_stderr_quiet():
    from contextlib import redirect_stderr

    return redirect_stderr(io.StringIO())


if __name__ == "__main__":
    unittest.main()
