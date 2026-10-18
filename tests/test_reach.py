"""Tests for gardener/reach.py — real-usage signals for Parpt scoring.
Every fetch is an injected fake; nothing here touches the network."""
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from urllib.error import URLError

from gardener import reach

GARDEN = ["Dans-Plugins/Medieval-Factions", "Dans-Plugins/Wild-Pets",
          "Stephenson-Software/gardener", "owner-a/tools", "owner-b/tools"]


class TestGithubRepo(unittest.TestCase):
    def test_parses_plain_git_and_trailing_paths(self):
        self.assertEqual(reach.github_repo("https://github.com/O/R"), "O/R")
        self.assertEqual(reach.github_repo("https://github.com/O/R.git"), "O/R")
        self.assertEqual(reach.github_repo("https://github.com/O/R/wiki"), "O/R")
        self.assertIsNone(reach.github_repo("https://gitlab.com/O/R"))
        self.assertIsNone(reach.github_repo(None))


class TestParseTraceSummary(unittest.TestCase):
    def test_matches_program_names_ignoring_case_and_punctuation(self):
        facts = reach.parse_trace_summary(
            [{"application": "MedievalFactions", "count": 8705, "lastSeen": "2026-10-17T21:00:00Z"},
             {"application": "wildpets", "count": 3}], GARDEN)
        self.assertIn("8705 startups", facts["Dans-Plugins/Medieval-Factions"]["trace usage"])
        self.assertIn("last seen 2026-10-17T21:00:00Z", facts["Dans-Plugins/Medieval-Factions"]["trace usage"])
        self.assertIn("3 startups", facts["Dans-Plugins/Wild-Pets"]["trace usage"])

    def test_a_name_two_garden_repos_share_is_left_unmatched(self):
        self.assertEqual(reach.parse_trace_summary([{"application": "tools", "count": 9}], GARDEN), {})

    def test_unknown_programs_and_garbage_are_ignored(self):
        self.assertEqual(reach.parse_trace_summary(
            [{"application": "Elsewhere", "count": 1}, "junk", {"application": "WildPets"}], GARDEN), {})
        self.assertEqual(reach.parse_trace_summary({"not": "a list"}, GARDEN), {})


class TestParseSpiget(unittest.TestCase):
    def test_matches_by_source_code_link_case_insensitively(self):
        facts = reach.parse_spiget_resources(
            [{"id": 79941, "downloads": 63978, "sourceCodeLink": "https://github.com/dans-plugins/medieval-factions"},
             {"id": 1, "downloads": 5, "sourceCodeLink": "https://github.com/someone/else"},
             {"id": 2, "downloads": 5}], GARDEN)
        self.assertEqual(facts, {"Dans-Plugins/Medieval-Factions":
                                 {"SpigotMC": "listed (resource 79941), 63978 downloads"}})


class TestDpc(unittest.TestCase):
    def test_plugins_map_to_slugs_by_github_url(self):
        self.assertEqual(reach.parse_dpc_plugins(
            [{"slug": "wild-pets", "githubUrl": "https://github.com/Dans-Plugins/Wild-Pets"},
             {"slug": "x", "githubUrl": "https://github.com/someone/else"},
             {"githubUrl": "https://github.com/Dans-Plugins/Medieval-Factions"}], GARDEN),
            {"Dans-Plugins/Wild-Pets": "wild-pets"})

    def test_download_text(self):
        self.assertEqual(reach.describe_dpc_downloads({"total": 277, "latestTag": "v7.0.0", "latest": 22}),
                         "listed, 277 downloads through the site (22 of the latest release v7.0.0)")
        self.assertEqual(reach.describe_dpc_downloads(None), "listed")


class TestFetchUsageFacts(unittest.TestCase):
    ENV = {reach.ENV_TRACE_URL: "https://trace.example/", reach.ENV_SPIGET_AUTHOR: "42",
           reach.ENV_DPC_API_URL: "https://api.example"}

    def _fake(self, responses):
        calls = []

        def get_json(url):
            calls.append(url)
            for prefix, value in responses.items():
                if url.startswith(prefix):
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise URLError("no route")
        return get_json, calls

    def test_unconfigured_fetches_nothing(self):
        get_json, calls = self._fake({})
        missing = Path(tempfile.mkdtemp()) / "notify.env"  # never the operator's real one
        self.assertEqual(reach.fetch_usage_facts(GARDEN, env={}, config_path=missing, get_json=get_json), {})
        self.assertEqual(calls, [])

    def test_merges_every_source_per_repo(self):
        get_json, calls = self._fake({
            "https://trace.example/api/public/summary": [{"application": "MedievalFactions", "count": 5}],
            "https://api.spiget.org/v2/authors/42/resources": [
                {"id": 7, "downloads": 9, "sourceCodeLink": "https://github.com/Dans-Plugins/Medieval-Factions"}],
            "https://api.example/api/v1/plugins/medieval-factions/downloads": {"total": 3},
            "https://api.example/api/v1/plugins": [
                {"slug": "medieval-factions", "githubUrl": "https://github.com/Dans-Plugins/Medieval-Factions"}],
        })
        facts = reach.fetch_usage_facts(GARDEN, env=self.ENV, get_json=get_json)
        self.assertEqual(set(facts["Dans-Plugins/Medieval-Factions"]),
                         {"trace usage", "SpigotMC", "dansplugins.com"})
        self.assertIn("https://trace.example/api/public/summary", calls)

    def test_a_failing_source_is_skipped_and_the_rest_kept(self):
        get_json, _ = self._fake({
            "https://trace.example/": URLError("down"),
            "https://api.spiget.org/": ValueError("bad json"),
            "https://api.example/api/v1/plugins/wild-pets/downloads": URLError("down"),
            "https://api.example/api/v1/plugins": [
                {"slug": "wild-pets", "githubUrl": "https://github.com/Dans-Plugins/Wild-Pets"}],
        })
        with unittest.mock.patch("sys.stderr"):
            facts = reach.fetch_usage_facts(GARDEN, env=self.ENV, get_json=get_json)
        self.assertEqual(facts, {"Dans-Plugins/Wild-Pets": {"dansplugins.com": "listed"}})


if __name__ == "__main__":
    unittest.main()
