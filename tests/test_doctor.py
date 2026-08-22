"""doctor.py is pure composition over injected callables — every git/gh
call goes through `run_fn`, PATH lookups through `which_fn`, and the
per-repo lock probe through `locked_fn` — so none of these tests invoke a
real `git`, `gh`, or network call, per gardener/CLAUDE.md's testing
conventions. The filesystem-walking checks (`check_state_dir`,
`check_cache_clones`) run against throwaway tmp dirs, the same way
`test_selfupdate.py` exercises `find_repo_root`.

`is_repo_locked` is tested in `test_repo_lock.py` alongside the lock it
probes; here it's always injected."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from gardener import doctor
from gardener.doctor import Severity


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class FakeRun:
    """Routes a call to a canned response by matching a prefix of `argv`.
    `git status --porcelain` -> key `("git", "status")`, `gh repo view` ->
    `("gh", "repo")` — enough to disambiguate every call doctor makes
    without a full fake git/gh implementation."""

    def __init__(self, responses: dict, calls: list | None = None):
        self.responses = responses
        self.calls = calls if calls is not None else []

    def __call__(self, argv, cwd, timeout):
        self.calls.append(argv)
        key = tuple(argv[0:2])
        if key not in self.responses:
            raise AssertionError(f"unexpected call: {argv}")
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(argv)
        return response


def _clone_dir(root: Path, name: str) -> Path:
    """A directory shaped enough like a clone for `check_cache_clones`'
    `.git` filter — its git *behavior* comes entirely from the fake."""
    path = root / name
    (path / ".git").mkdir(parents=True)
    return path


def _never_locked(repo: str) -> bool:
    return False


def _by_check(findings, check):
    return [f for f in findings if f.check == check]


class TestDirToRepoName(unittest.TestCase):
    def test_inverts_the_cache_dir_naming_scheme(self):
        self.assertEqual(doctor.dir_to_repo_name("Owner__repo"), "Owner/repo")

    def test_only_splits_the_first_separator(self):
        # A repo name may legitimately contain a double underscore; only the
        # owner/name boundary is a path separator.
        self.assertEqual(doctor.dir_to_repo_name("Owner__we__ird"), "Owner/we__ird")


class TestReport(unittest.TestCase):
    def test_worst_prefers_error_over_warn_over_skipped(self):
        report = doctor.Report([
            doctor.Finding("a", Severity.SKIPPED, ""),
            doctor.Finding("b", Severity.WARN, ""),
            doctor.Finding("c", Severity.ERROR, ""),
        ])
        self.assertIs(report.worst, Severity.ERROR)
        self.assertEqual(len(report.problems), 2)

    def test_skipped_is_not_a_problem(self):
        # A `doctor --offline` run is all SKIPPED and must not read as a
        # failure — this is what keeps the command's exit code meaningful
        # on a device with no signal.
        report = doctor.Report([
            doctor.Finding("a", Severity.SKIPPED, ""),
            doctor.Finding("b", Severity.OK, ""),
        ])
        self.assertEqual(report.problems, [])
        self.assertIs(report.worst, Severity.SKIPPED)

    def test_all_ok(self):
        report = doctor.Report([doctor.Finding("a", Severity.OK, "")])
        self.assertIs(report.worst, Severity.OK)


class TestRequiredClis(unittest.TestCase):
    def test_missing_cli_is_an_error_naming_what_breaks(self):
        findings = doctor.check_required_clis(which_fn=lambda name: None if name == "gh" else "/usr/bin/" + name)
        errors = [f for f in findings if f.severity is Severity.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn("gh", errors[0].message)
        self.assertIn(doctor.REQUIRED_CLIS["gh"], errors[0].message)

    def test_all_present(self):
        findings = doctor.check_required_clis(which_fn=lambda name: "/usr/bin/" + name)
        self.assertTrue(all(f.severity is Severity.OK for f in findings))


class TestGhAuth(unittest.TestCase):
    def test_offline_skips_without_calling_gh(self):
        run = FakeRun({})
        findings = doctor.check_gh_auth(run_fn=run, offline=True)
        self.assertIs(findings[0].severity, Severity.SKIPPED)
        self.assertEqual(run.calls, [])

    def test_unauthenticated_is_an_error(self):
        run = FakeRun({("gh", "auth"): _completed(returncode=1)})
        findings = doctor.check_gh_auth(run_fn=run)
        self.assertIs(findings[0].severity, Severity.ERROR)

    def test_subprocess_failure_degrades_to_skipped_not_error(self):
        run = FakeRun({("gh", "auth"): OSError("boom")})
        findings = doctor.check_gh_auth(run_fn=run)
        self.assertIs(findings[0].severity, Severity.SKIPPED)


class TestStateDir(unittest.TestCase):
    def test_missing_dir_is_fine(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings = doctor.check_state_dir(Path(tmp) / "nope")
            self.assertTrue(all(f.severity is Severity.OK for f in findings))

    def test_malformed_list_file_is_an_error(self):
        # garden.py/merge_allowlist.py both raise on malformed JSON, and a
        # missing file reads as empty — either way the operator needs to be
        # told, because "empty garden" is a silent no-op.
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "garden.json").write_text("{not json")
            findings = doctor.check_state_dir(state_dir)
            errors = [f for f in findings if f.severity is Severity.ERROR]
            self.assertEqual(len(errors), 1)
            self.assertIn("garden.json", errors[0].message)

    def test_valid_files_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            (state_dir / "garden.json").write_text(json.dumps(["a/b"]))
            (state_dir / "merge_allowlist.json").write_text(json.dumps([]))
            findings = doctor.check_state_dir(state_dir)
            self.assertTrue(all(f.severity is Severity.OK for f in findings))


class TestCacheClone(unittest.TestCase):
    def test_clean_clone_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _clone_dir(Path(tmp), "Owner__repo")
            run = FakeRun({
                ("git", "remote"): _completed(stdout="https://github.com/Owner/repo.git\n"),
                ("git", "status"): _completed(stdout=""),
            })
            findings = doctor.check_cache_clone(path, run_fn=run, locked_fn=_never_locked)
            self.assertEqual([f.severity for f in findings], [Severity.OK])

    def test_modified_tracked_files_are_an_error_with_a_stash_fix(self):
        # The exact failure that silently burned three repos' overnight
        # slots for a week: `git checkout -B` refuses to overwrite these.
        with tempfile.TemporaryDirectory() as tmp:
            path = _clone_dir(Path(tmp), "Owner__repo")
            run = FakeRun({
                ("git", "remote"): _completed(stdout="https://github.com/Owner/repo.git"),
                ("git", "status"): _completed(stdout=" M src/a.py\n M src/b.py\n"),
                ("git", "rev-parse"): _completed(stdout="fix/something"),
            })
            findings = doctor.check_cache_clone(path, run_fn=run, locked_fn=_never_locked)
            errors = [f for f in findings if f.severity is Severity.ERROR]
            self.assertEqual(len(errors), 1)
            self.assertIn("2 modified file(s)", errors[0].message)
            self.assertIn("fix/something", errors[0].message)
            self.assertIn("stash push", errors[0].fix)
            self.assertEqual(errors[0].repo, "Owner/repo")

    def test_untracked_only_is_a_warning_not_an_error(self):
        # The refresh runs `git clean -fdx` and is meant to remove these.
        with tempfile.TemporaryDirectory() as tmp:
            path = _clone_dir(Path(tmp), "Owner__repo")
            run = FakeRun({
                ("git", "remote"): _completed(stdout="https://github.com/Owner/repo.git"),
                ("git", "status"): _completed(stdout="?? scratch.md\n"),
            })
            findings = doctor.check_cache_clone(path, run_fn=run, locked_fn=_never_locked)
            self.assertEqual([f.severity for f in findings], [Severity.WARN])

    def test_origin_mismatch_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _clone_dir(Path(tmp), "Owner__repo")
            run = FakeRun({
                ("git", "remote"): _completed(stdout="https://github.com/Other/elsewhere.git"),
                ("git", "status"): _completed(stdout=""),
            })
            findings = doctor.check_cache_clone(path, run_fn=run, locked_fn=_never_locked)
            errors = [f for f in findings if f.severity is Severity.ERROR]
            self.assertEqual(len(errors), 1)
            self.assertIn("does not contain Owner/repo", errors[0].message)

    def test_locked_repo_is_skipped_entirely(self):
        # A tend in flight has legitimately dirtied its own clone. Reporting
        # that as a problem would make `doctor` useless during exactly the
        # unattended run it exists to protect — and it must not even shell
        # out to git for a locked repo.
        with tempfile.TemporaryDirectory() as tmp:
            path = _clone_dir(Path(tmp), "Owner__repo")
            run = FakeRun({})
            findings = doctor.check_cache_clone(path, run_fn=run, locked_fn=lambda repo: True)
            self.assertEqual([f.severity for f in findings], [Severity.SKIPPED])
            self.assertEqual(run.calls, [])

    def test_unreadable_origin_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _clone_dir(Path(tmp), "Owner__repo")
            run = FakeRun({("git", "remote"): _completed(returncode=128)})
            findings = doctor.check_cache_clone(path, run_fn=run, locked_fn=_never_locked)
            self.assertEqual([f.severity for f in findings], [Severity.ERROR])

    def test_a_timeout_on_one_clone_does_not_propagate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _clone_dir(Path(tmp), "Owner__repo")
            run = FakeRun({("git", "remote"): subprocess.TimeoutExpired(cmd="git", timeout=1)})
            findings = doctor.check_cache_clone(path, run_fn=run, locked_fn=_never_locked)
            self.assertEqual([f.severity for f in findings], [Severity.ERROR])


class TestCacheClones(unittest.TestCase):
    def test_skips_non_clone_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _clone_dir(root, "Owner__repo")
            (root / "stray-dir").mkdir()
            run = FakeRun({
                ("git", "remote"): _completed(stdout="https://github.com/Owner/repo.git"),
                ("git", "status"): _completed(stdout=""),
            })
            findings = doctor.check_cache_clones(root, run_fn=run, locked_fn=_never_locked)
            self.assertEqual(len(findings), 1)

    def test_missing_cache_dir_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            findings = doctor.check_cache_clones(Path(tmp) / "nope", run_fn=FakeRun({}))
            self.assertEqual([f.severity for f in findings], [Severity.OK])


class TestRepoNames(unittest.TestCase):
    def _gh(self, mapping):
        def respond(argv):
            repo = argv[3]
            if repo not in mapping:
                return _completed(returncode=1, stderr="not found")
            return _completed(stdout=json.dumps({"nameWithOwner": mapping[repo]}))

        return FakeRun({("gh", "repo"): respond})

    def test_renamed_repo_is_an_error_naming_the_new_name(self):
        run = self._gh({"old/name": "new/name"})
        findings = doctor.check_repo_names(["old/name"], "garden", run_fn=run)
        errors = [f for f in findings if f.severity is Severity.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn("new/name", errors[0].message)
        self.assertIn("gardener garden add --repo new/name", errors[0].fix)

    def test_the_fix_command_names_the_list_it_came_from(self):
        run = self._gh({"old/name": "new/name"})
        findings = doctor.check_repo_names(["old/name"], "allowlist", run_fn=run)
        self.assertIn("gardener allowlist remove", findings[0].fix)

    def test_unresolvable_repo_is_skipped_not_reported_as_renamed(self):
        # Offline, rate-limited, and deleted are the same answer from here,
        # and none of them mean "renamed".
        run = self._gh({})
        findings = doctor.check_repo_names(["gone/repo"], "garden", run_fn=run)
        self.assertTrue(all(f.severity is Severity.SKIPPED for f in findings))

    def test_matching_name_is_ok(self):
        run = self._gh({"same/name": "same/name"})
        findings = doctor.check_repo_names(["same/name"], "garden", run_fn=run)
        self.assertEqual([f.severity for f in findings], [Severity.OK])

    def test_offline_makes_no_calls(self):
        run = FakeRun({})
        findings = doctor.check_repo_names(["a/b"], "garden", run_fn=run, offline=True)
        self.assertEqual([f.severity for f in findings], [Severity.SKIPPED])
        self.assertEqual(run.calls, [])


class TestCanonicalRepoName(unittest.TestCase):
    def test_malformed_json_is_none_not_a_crash(self):
        run = FakeRun({("gh", "repo"): _completed(stdout="not json")})
        self.assertIsNone(doctor.canonical_repo_name("a/b", run_fn=run))

    def test_empty_name_is_none(self):
        run = FakeRun({("gh", "repo"): _completed(stdout=json.dumps({"nameWithOwner": ""}))})
        self.assertIsNone(doctor.canonical_repo_name("a/b", run_fn=run))


class TestRunChecks(unittest.TestCase):
    def test_composes_every_check_and_stays_offline_when_asked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "repos"
            cache.mkdir()
            _clone_dir(cache, "Owner__repo")
            run = FakeRun({
                ("git", "remote"): _completed(stdout="https://github.com/Owner/repo.git"),
                ("git", "status"): _completed(stdout=""),
            })
            report = doctor.run_checks(
                cache_dir=cache,
                state_dir=root / "state",
                garden_repos=["Owner/repo"],
                allowlist_repos=["Owner/repo"],
                run_fn=run,
                which_fn=lambda name: "/usr/bin/" + name,
                locked_fn=_never_locked,
                offline=True,
            )
            self.assertEqual(report.problems, [])
            self.assertTrue(_by_check(report.findings, "cache-clone"))
            self.assertTrue(_by_check(report.findings, "repo-name"))
            self.assertTrue(_by_check(report.findings, "state-dir"))
            # Offline means no `gh` process was ever spawned.
            self.assertTrue(all(argv[0] == "git" for argv in run.calls))


if __name__ == "__main__":
    unittest.main()
