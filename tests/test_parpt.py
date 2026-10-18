"""Tests for gardener/parpt.py — pure scoring, storage and ordering logic.
Nothing here invokes `claude` or `gh`."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gardener import parpt


def _reply(**overrides) -> str:
    answers = {dim: [3, 3, 3, 3] for dim in parpt.DIMENSIONS}
    answers.update(overrides)
    lines = [f"{dim}: {' '.join(str(v) for v in vals)}" for dim, vals in answers.items() if vals is not None]
    lines += [f"{dim} reason: {dim} reason" for dim in parpt.DIMENSIONS]
    return "\n".join(lines)


def _record(reach=3, impact=3, confidence=3, ease=3, effort=3, scored_at=None):
    return {
        "scores": {"reach": reach, "impact": impact, "confidence": confidence,
                   "ease": ease, "effort": effort},
        "scored_at": scored_at or datetime.now(timezone.utc).isoformat(),
    }


class TestFormulas(unittest.TestCase):
    def test_rice_matches_parpt(self):
        self.assertEqual(parpt.rice({"reach": 4, "impact": 5, "confidence": 3, "effort": 2}), 30.0)

    def test_ice_matches_parpt(self):
        self.assertEqual(parpt.ice({"impact": 2, "confidence": 3, "ease": 4}), 24.0)

    def test_rice_without_positive_effort_is_none(self):
        self.assertIsNone(parpt.rice({"reach": 1, "impact": 1, "confidence": 1, "effort": 0}))
        self.assertIsNone(parpt.rice({}))

    def test_rounding_is_half_up_like_java_math_round(self):
        # mean 2.5: Python's round() gives 2, Parpt's Math.round gives 3
        self.assertEqual(parpt.parpt_round([2, 3, 2, 3]), 3)
        self.assertEqual(parpt.parpt_round([1, 1, 1, 2]), 1)


class TestQuestionnaire(unittest.TestCase):
    def test_four_questions_per_dimension_like_parpt(self):
        self.assertEqual(parpt.DIMENSIONS, ("impact", "confidence", "ease", "reach", "effort"))
        for questions in parpt.QUESTIONS.values():
            self.assertEqual(len(questions), 4)

    def test_prompt_contains_every_question_and_the_facts(self):
        prompt = parpt.build_score_prompt("owner/repo", {"stars": 12})
        for questions in parpt.QUESTIONS.values():
            for q in questions:
                self.assertIn(q, prompt)
        self.assertIn("owner/repo", prompt)
        self.assertIn("- stars: 12", prompt)

    def test_prompt_carries_the_owner_priorities_when_given(self):
        prompt = parpt.build_score_prompt("owner/repo", {}, "Game X is my flagship.")
        self.assertIn("owner's priorities", prompt)
        self.assertIn("Game X is my flagship.", prompt)
        self.assertNotIn("owner's priorities", parpt.build_score_prompt("owner/repo", {}))

    def test_prompt_says_when_facts_are_unavailable(self):
        self.assertIn("(unavailable)", parpt.build_score_prompt("owner/repo", {}))


class TestParseScoreReply(unittest.TestCase):
    def test_parses_and_rounds_each_dimension(self):
        parsed = parpt.parse_score_reply(_reply(impact=[5, 5, 4, 4], effort=[1, 1, 1, 2]))
        self.assertEqual(parsed["scores"]["impact"], 5)
        self.assertEqual(parsed["scores"]["effort"], 1)
        self.assertEqual(parsed["answers"]["impact"], [5, 5, 4, 4])
        self.assertEqual(parsed["reasons"]["reach"], "reach reason")

    def test_tolerates_prose_markdown_and_list_punctuation(self):
        reply = "Here you go:\n**impact**: [4, 4, 3, 3]\n" + _reply(impact=None) + "\nDone."
        parsed = parpt.parse_score_reply(reply)
        self.assertEqual(parsed["answers"]["impact"], [4, 4, 3, 3])
        self.assertEqual(parsed["scores"]["ease"], 3)

    def test_reason_with_braces_and_quotes_is_fine(self):
        reply = _reply() + '\nimpact reason: uses {"json": "like"} config, 0 stars.'
        self.assertIn('{"json"', parpt.parse_score_reply(reply)["reasons"]["impact"])

    def test_missing_reasons_are_empty_not_an_error(self):
        reply = "\n".join(l for l in _reply().splitlines() if "reason" not in l)
        self.assertEqual(parpt.parse_score_reply(reply)["reasons"]["impact"], "")

    def test_rejects_malformed_replies(self):
        for bad in (
            "no scores here",
            _reply(impact=[3, 3, 3]),        # too few answers
            _reply(reach=[3, 3, 3, 6]),      # out of range
            _reply(ease=[0, 3, 3, 3]),       # out of range
            _reply(effort=[3, 3, 3, 3, 3]),  # too many answers
            _reply() + "\nimpact: 1 1 1 1",   # answered twice
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(parpt.ScoreParseError):
                    parpt.parse_score_reply(bad)

    def test_rejects_a_missing_dimension(self):
        with self.assertRaises(parpt.ScoreParseError):
            parpt.parse_score_reply(_reply(confidence=None))


class TestStorage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "parpt_scores.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_or_corrupt_file_loads_empty(self):
        self.assertEqual(parpt.load_scores(self.path), {})
        self.path.write_text("not json")
        self.assertEqual(parpt.load_scores(self.path), {})

    def test_save_round_trips_and_keeps_other_repos(self):
        parpt.save_score("a/one", _record(reach=5), path=self.path)
        parpt.save_score("a/two", _record(reach=1), path=self.path)
        records = parpt.load_scores(self.path)
        self.assertEqual(records["a/one"]["scores"]["reach"], 5)
        self.assertEqual(records["a/two"]["scores"]["reach"], 1)

    def test_save_writes_a_parpt_projects_file_with_only_project_fields(self):
        record = _record(reach=4)
        record["reasons"] = {"reach": "many users"}
        parpt.save_score("a/one", record, path=self.path)
        projects = json.loads(parpt.default_parpt_projects_path(self.path).read_text())
        self.assertEqual(len(projects), 1)
        # Parpt's ObjectMapper rejects unknown properties: exactly its seven fields
        self.assertEqual(
            set(projects[0]),
            {"name", "description", "impact", "confidence", "ease", "reach", "effort"},
        )
        self.assertEqual(projects[0]["name"], "a/one")
        self.assertEqual(projects[0]["reach"], 4)
        self.assertIn("many users", projects[0]["description"])


class TestPriorities(unittest.TestCase):
    def test_missing_file_is_empty_and_has_no_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(parpt.load_priorities(Path(tmp) / "nope.md"), "")
        self.assertEqual(parpt.priorities_digest(""), "")

    def test_file_is_read_stripped_and_digest_tracks_its_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.md"
            path.write_text("\nRoam first.\n")
            self.assertEqual(parpt.load_priorities(path), "Roam first.")
        self.assertEqual(parpt.priorities_digest("a"), parpt.priorities_digest("a"))
        self.assertNotEqual(parpt.priorities_digest("a"), parpt.priorities_digest("b"))


class TestReposNeedingScore(unittest.TestCase):
    NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)

    def _at(self, days_ago):
        return (self.NOW - timedelta(days=days_ago)).isoformat()

    def test_unscored_first_then_stale_oldest_first_fresh_never(self):
        records = {
            "a/fresh": _record(scored_at=self._at(1)),
            "a/stale": _record(scored_at=self._at(40)),
            "a/staler": _record(scored_at=self._at(90)),
        }
        todo = parpt.repos_needing_score(
            ["a/stale", "a/new", "a/fresh", "a/staler"], records, limit=10, now=self.NOW
        )
        self.assertEqual(todo, ["a/new", "a/staler", "a/stale"])

    def test_limit_caps_the_list(self):
        todo = parpt.repos_needing_score(["a", "b", "c"], {}, limit=2, now=self.NOW)
        self.assertEqual(todo, ["a", "b"])
        self.assertEqual(parpt.repos_needing_score(["a"], {}, limit=0, now=self.NOW), [])

    def test_scores_under_other_priorities_are_stale_however_recent(self):
        old = _record(scored_at=self._at(1))
        old["priorities_digest"] = "aaaa"
        current = _record(scored_at=self._at(1))
        current["priorities_digest"] = "bbbb"
        legacy = _record(scored_at=self._at(2))  # scored before priorities existed
        records = {"a/old": old, "a/current": current, "a/legacy": legacy}
        garden = ["a/old", "a/current", "a/legacy"]
        self.assertEqual(
            parpt.repos_needing_score(garden, records, 10, now=self.NOW, digest="bbbb"),
            ["a/legacy", "a/old"],
        )
        # priorities file removed: records made under any text are stale,
        # legacy records (never had one) stay fresh
        self.assertEqual(
            parpt.repos_needing_score(garden, records, 10, now=self.NOW, digest=""),
            ["a/current", "a/old"],
        )
        # no digest passed at all: only age counts
        self.assertEqual(parpt.repos_needing_score(garden, records, 10, now=self.NOW), [])

    def test_unreadable_record_counts_as_unscored(self):
        records = {"a/bad": {"scores": {}, "scored_at": "yesterday"}}
        self.assertEqual(parpt.repos_needing_score(["a/bad"], records, 5, now=self.NOW), ["a/bad"])


class TestOrderByParptScore(unittest.TestCase):
    def test_sorts_descending_by_rice_unscored_last(self):
        records = {
            "a/low": _record(reach=1, impact=1, confidence=1, effort=5),
            "a/high": _record(reach=5, impact=5, confidence=5, effort=1),
        }
        order = parpt.order_by_parpt_score(["a/low", "a/none", "a/high"], records)
        self.assertEqual(order, ["a/high", "a/low", "a/none"])

    def test_ice_then_name_break_ties(self):
        records = {
            "a/b": _record(ease=2),
            "a/a": _record(ease=2),
            "a/c": _record(ease=5),
        }
        self.assertEqual(parpt.order_by_parpt_score(["a/a", "a/b", "a/c"], records),
                         ["a/c", "a/a", "a/b"])

    def test_every_repo_appears_exactly_once(self):
        garden = ["a", "b", "c"]
        self.assertEqual(sorted(parpt.order_by_parpt_score(garden, {"b": _record()})), garden)


if __name__ == "__main__":
    unittest.main()
