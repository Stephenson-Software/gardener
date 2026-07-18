"""transcript.py's job is entirely deterministic (a path-encoding transform,
filesystem-polling logic, and JSONL-line parsing) — never invoke a real
`claude` process here, and never sleep for a real wall-clock second: every
timing-dependent test injects a fake `time_fn`/`sleep_fn` instead."""
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gardener import transcript


class TestEncodeCwd(unittest.TestCase):
    """Both examples here are real, observed transcript directory names —
    see transcript.py's module docstring for exactly how each was produced
    and confirmed, not invented after the fact."""

    def test_matches_the_real_throwaway_probe_session(self):
        cwd = (
            "/tmp/claude-2000/-home-userland/2c0a53d4-2197-4dd7-88c9-2e2382e7c79b"
            "/scratchpad/enc_probe.fresh/sub-dir_name.example"
        )
        expected = (
            "-tmp-claude-2000--home-userland-2c0a53d4-2197-4dd7-88c9-2e2382e7c79b"
            "-scratchpad-enc-probe-fresh-sub-dir-name-example"
        )
        self.assertEqual(transcript.encode_cwd(cwd), expected)

    def test_matches_the_real_gateway_tend_dispatch(self):
        cwd = "/home/userland/.cache/gardener/repos/Stephenson-Software__gateway"
        expected = "-home-userland--cache-gardener-repos-Stephenson-Software--gateway"
        self.assertEqual(transcript.encode_cwd(cwd), expected)

    def test_accepts_a_path_object_not_just_a_string(self):
        self.assertEqual(transcript.encode_cwd(Path("/a/b")), "-a-b")

    def test_every_non_alnum_character_becomes_a_literal_dash_one_for_one(self):
        # A synthetic case exercising every special character used across
        # the two real examples above, in one string, to lock in that runs
        # of special characters are never collapsed.
        self.assertEqual(transcript.encode_cwd("/a.b_c-d__e..f"), "-a-b-c-d--e--f")


class TestClaudeProjectsDir(unittest.TestCase):
    def test_defaults_to_home_dot_claude_projects(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("gardener.transcript.Path.home", return_value=Path("/home/x")):
                self.assertEqual(transcript.claude_projects_dir(), Path("/home/x/.claude/projects"))

    def test_honors_claude_config_dir_override(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/custom/cfg"}):
            self.assertEqual(transcript.claude_projects_dir(), Path("/custom/cfg/projects"))

    def test_project_transcript_dir_combines_both(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/custom/cfg"}):
            self.assertEqual(
                transcript.project_transcript_dir("/a/b"),
                Path("/custom/cfg/projects/-a-b"),
            )


class TestFindNewTranscript(unittest.TestCase):
    def test_missing_directory_returns_none(self):
        with TemporaryDirectory() as d:
            self.assertIsNone(transcript.find_new_transcript(Path(d) / "nope", after=0))

    def test_ignores_files_older_than_after(self):
        with TemporaryDirectory() as d:
            project_dir = Path(d)
            old_file = project_dir / "old.jsonl"
            old_file.write_text("{}")
            os.utime(old_file, (1000, 1000))
            self.assertIsNone(transcript.find_new_transcript(project_dir, after=2000))

    def test_finds_a_file_newer_than_after(self):
        with TemporaryDirectory() as d:
            project_dir = Path(d)
            new_file = project_dir / "new.jsonl"
            new_file.write_text("{}")
            os.utime(new_file, (5000, 5000))
            found = transcript.find_new_transcript(project_dir, after=2000)
            self.assertEqual(found, new_file)

    def test_picks_the_most_recently_modified_when_multiple_qualify(self):
        with TemporaryDirectory() as d:
            project_dir = Path(d)
            earlier = project_dir / "earlier.jsonl"
            later = project_dir / "later.jsonl"
            earlier.write_text("{}")
            later.write_text("{}")
            os.utime(earlier, (3000, 3000))
            os.utime(later, (4000, 4000))
            found = transcript.find_new_transcript(project_dir, after=2000)
            self.assertEqual(found, later)

    def test_non_jsonl_files_are_ignored(self):
        with TemporaryDirectory() as d:
            project_dir = Path(d)
            (project_dir / "notes.txt").write_text("hi")
            self.assertIsNone(transcript.find_new_transcript(project_dir, after=0))


class TestPollForNewTranscript(unittest.TestCase):
    """Every test here injects time_fn/sleep_fn so the loop under test never
    sleeps for a real second — sleep_fn just advances a fake clock."""

    def test_returns_immediately_if_file_already_present(self):
        with TemporaryDirectory() as d:
            project_dir = Path(d) / "projects" / transcript.encode_cwd("/some/cwd")
            project_dir.mkdir(parents=True)
            f = project_dir / "s.jsonl"
            f.write_text("{}")
            os.utime(f, (5000, 5000))
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
                sleep_calls = []
                result = transcript.poll_for_new_transcript(
                    "/some/cwd",
                    after=2000,
                    timeout=5.0,
                    time_fn=lambda: 0.0,
                    sleep_fn=lambda s: sleep_calls.append(s),
                )
            self.assertEqual(result, f)
            self.assertEqual(sleep_calls, [])

    def test_finds_a_file_that_appears_after_a_few_polls(self):
        with TemporaryDirectory() as d:
            project_dir = Path(d) / "projects" / transcript.encode_cwd("/some/cwd")
            project_dir.mkdir(parents=True)
            fake_clock = {"t": 0.0}

            def time_fn():
                return fake_clock["t"]

            def sleep_fn(interval):
                fake_clock["t"] += interval
                if fake_clock["t"] >= 0.6:
                    f = project_dir / "s.jsonl"
                    f.write_text("{}")
                    os.utime(f, (5000, 5000))

            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
                result = transcript.poll_for_new_transcript(
                    "/some/cwd", after=2000, timeout=5.0, interval=0.2,
                    time_fn=time_fn, sleep_fn=sleep_fn,
                )
            self.assertIsNotNone(result)
            self.assertEqual(result.name, "s.jsonl")

    def test_gives_up_after_timeout_without_ever_finding_a_file(self):
        with TemporaryDirectory() as d:
            fake_clock = {"t": 0.0}

            def time_fn():
                return fake_clock["t"]

            def sleep_fn(interval):
                fake_clock["t"] += interval

            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
                result = transcript.poll_for_new_transcript(
                    "/never/shows/up", after=2000, timeout=1.0, interval=0.2,
                    time_fn=time_fn, sleep_fn=sleep_fn,
                )
            self.assertIsNone(result)
            # Bounded: must not have polled forever.
            self.assertLessEqual(fake_clock["t"], 1.2)


class TestLogTranscriptWhenFound(unittest.TestCase):
    def test_prints_the_path_when_found(self):
        with TemporaryDirectory() as d:
            project_dir = Path(d) / "projects" / transcript.encode_cwd("/some/cwd")
            project_dir.mkdir(parents=True)
            f = project_dir / "s.jsonl"
            f.write_text("{}")
            os.utime(f, (5000, 5000))
            buf = io.StringIO()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
                transcript.log_transcript_when_found(
                    "/some/cwd", after=2000, stream=buf,
                    time_fn=lambda: 0.0, sleep_fn=lambda s: None,
                )
            self.assertIn("gardener: session transcript:", buf.getvalue())
            self.assertIn(str(f), buf.getvalue())

    def test_silent_no_op_when_nothing_found_in_time(self):
        with TemporaryDirectory() as d:
            fake_clock = {"t": 0.0}
            buf = io.StringIO()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
                transcript.log_transcript_when_found(
                    "/nope", after=2000, stream=buf, timeout=1.0, interval=0.2,
                    time_fn=lambda: fake_clock.update(t=fake_clock["t"] + 1) or fake_clock["t"],
                    sleep_fn=lambda s: None,
                )
            self.assertEqual(buf.getvalue(), "")

    def test_never_raises_even_if_polling_itself_errors(self):
        buf = io.StringIO()
        with patch("gardener.transcript.poll_for_new_transcript", side_effect=RuntimeError("boom")):
            try:
                transcript.log_transcript_when_found("/x", after=0, stream=buf)
            except Exception as e:  # pragma: no cover - the point of the test is that this doesn't happen
                self.fail(f"log_transcript_when_found raised: {e}")
        self.assertEqual(buf.getvalue(), "")


class TestStartTranscriptWatcher(unittest.TestCase):
    def test_starts_a_daemon_thread_and_returns_it(self):
        with patch("gardener.transcript.log_transcript_when_found") as mock_target:
            thread = transcript.start_transcript_watcher("/some/cwd", after=123.0)
            thread.join(timeout=2)
            self.assertTrue(mock_target.called)
            args, kwargs = mock_target.call_args
            self.assertEqual(args[0], "/some/cwd")
            self.assertEqual(args[1], 123.0)

    def test_defaults_after_to_current_wall_clock_time(self):
        with patch("gardener.transcript.log_transcript_when_found") as mock_target, \
                patch("gardener.transcript.time.time", return_value=999.0):
            thread = transcript.start_transcript_watcher("/some/cwd")
            thread.join(timeout=2)
            args, _kwargs = mock_target.call_args
            self.assertEqual(args[1], 999.0)


class TestFormatTranscriptLine(unittest.TestCase):
    def test_blank_line_is_none(self):
        self.assertIsNone(transcript.format_transcript_line(""))
        self.assertIsNone(transcript.format_transcript_line("   \n"))

    def test_malformed_json_is_none(self):
        self.assertIsNone(transcript.format_transcript_line("{not json"))

    def test_non_dict_json_is_none(self):
        self.assertIsNone(transcript.format_transcript_line("[1, 2, 3]"))

    def test_bookkeeping_line_with_no_message_is_none(self):
        line = json.dumps({"type": "queue-operation", "operation": "enqueue"})
        self.assertIsNone(transcript.format_transcript_line(line))

    def test_tool_use_block(self):
        line = json.dumps({
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}],
            }
        })
        formatted = transcript.format_transcript_line(line)
        self.assertIn("tool_use", formatted)
        self.assertIn("Bash", formatted)
        self.assertIn("git status", formatted)

    def test_text_block(self):
        line = json.dumps({
            "message": {"role": "assistant", "content": [{"type": "text", "text": "hello there"}]}
        })
        formatted = transcript.format_transcript_line(line)
        self.assertIn("text:", formatted)
        self.assertIn("hello there", formatted)

    def test_empty_text_block_is_skipped(self):
        line = json.dumps({
            "message": {"role": "assistant", "content": [{"type": "text", "text": "   "}]}
        })
        self.assertIsNone(transcript.format_transcript_line(line))

    def test_tool_result_ok(self):
        line = json.dumps({
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "did the thing", "is_error": False}],
            }
        })
        formatted = transcript.format_transcript_line(line)
        self.assertIn("tool_result (ok)", formatted)
        self.assertIn("did the thing", formatted)

    def test_tool_result_error_is_flagged(self):
        line = json.dumps({
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": "boom", "is_error": True}],
            }
        })
        formatted = transcript.format_transcript_line(line)
        self.assertIn("tool_result (ERROR)", formatted)
        self.assertIn("boom", formatted)

    def test_tool_result_with_list_content_extracts_text(self):
        line = json.dumps({
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "content": [{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}],
                    "is_error": False,
                }],
            }
        })
        formatted = transcript.format_transcript_line(line)
        self.assertIn("part one", formatted)
        self.assertIn("part two", formatted)

    def test_long_text_is_truncated(self):
        long_text = "x" * 500
        line = json.dumps({
            "message": {"role": "assistant", "content": [{"type": "text", "text": long_text}]}
        })
        formatted = transcript.format_transcript_line(line)
        self.assertLess(len(formatted), len(long_text))
        self.assertIn("…", formatted)

    def test_plain_string_content_is_treated_as_text(self):
        line = json.dumps({"message": {"role": "user", "content": "a plain string prompt"}})
        formatted = transcript.format_transcript_line(line)
        self.assertIn("a plain string prompt", formatted)

    def test_multiple_blocks_in_one_message_all_appear(self):
        line = json.dumps({
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "thinking..."},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/a"}},
                ],
            }
        })
        formatted = transcript.format_transcript_line(line)
        self.assertIn("thinking...", formatted)
        self.assertIn("Read", formatted)


class TestIterPrettyLines(unittest.TestCase):
    def test_dump_mode_reads_existing_lines_and_stops(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            path.write_text(
                json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "one"}]}}) + "\n"
                + json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "two"}]}}) + "\n"
            )
            lines = list(transcript.iter_pretty_lines(path, follow=False))
            self.assertEqual(len(lines), 2)
            self.assertIn("one", lines[0])
            self.assertIn("two", lines[1])

    def test_dump_mode_skips_malformed_and_blank_lines(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            path.write_text(
                "\n"
                + "not json\n"
                + json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "real"}]}}) + "\n"
            )
            lines = list(transcript.iter_pretty_lines(path, follow=False))
            self.assertEqual(len(lines), 1)
            self.assertIn("real", lines[0])

    def test_follow_mode_picks_up_an_appended_line_without_real_sleep(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            path.write_text(
                json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "first"}]}}) + "\n"
            )
            appended = json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "second"}]}}) + "\n"
            calls = []

            def fake_sleep(interval):
                calls.append(interval)
                if len(calls) == 1:
                    with path.open("a") as f:
                        f.write(appended)
                elif len(calls) > 5:
                    raise RuntimeError("test loop guard: sleep called too many times")

            gen = transcript.iter_pretty_lines(path, follow=True, sleep_fn=fake_sleep)
            first = next(gen)
            second = next(gen)
            self.assertIn("first", first)
            self.assertIn("second", second)
            self.assertEqual(calls, [0.5])


class TestPrintTranscript(unittest.TestCase):
    def test_missing_file_returns_1_and_reports_error(self):
        with TemporaryDirectory() as d:
            missing = Path(d) / "nope.jsonl"
            code = transcript.print_transcript(missing, stream=io.StringIO())
            self.assertEqual(code, 1)

    def test_existing_file_dumps_and_returns_0(self):
        with TemporaryDirectory() as d:
            path = Path(d) / "t.jsonl"
            path.write_text(
                json.dumps({"message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}) + "\n"
            )
            buf = io.StringIO()
            code = transcript.print_transcript(path, stream=buf)
            self.assertEqual(code, 0)
            self.assertIn("hi", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
