"""Live tier: `align --implement` and `align --file-issue`, for real.

These are the two `align` modes that write to GitHub, so they run only
against a repository named explicitly in `GARDENER_IT_WRITE_REPO`. It must
be a throwaway: each run opens a real pull request or issue there. They
use the real `claude` (paid, like the Claude Code tier) and are skipped
unless `GARDENER_IT_CLAUDE=1` is set too.

What each run must and must not do:
- `--implement` opens one pull request from a new branch. It never merges
  it, never moves the default branch, and opens no issue. The mode's
  `Bash(gh pr *)` allow-pattern would permit `gh pr merge`; only the
  prompt forbids it (dispatch.py's module docstring, point 4), so this
  checks the outcome.
- `--file-issue` opens one issue. It opens no pull request, never moves the
  default branch, and leaves the clone untouched, since the mode has no
  Edit or Write tool.
- Neither names the conventions repo, its URL or its local path in anything
  it posts: the conventions repo may be private while the target is
  public. An earlier prompt asked for the docs "by URL", and a private
  repo's name reached public pull requests and issues.

`tend` (no `--allow-merge`) runs that repository's dev-loop skill. The test
installs a small, deterministic one as
`~/.claude/commands/<slug>-dev-loop.md` (`claude` reads skills from the
real HOME, which holds its login), refuses to run if a skill of that name
already exists, and removes it afterwards. The run must open at most one
pull request, merge nothing, and leave the default branch alone.

What a run opened is closed afterwards (a pull request with its branch),
unless `GARDENER_IT_KEEP=1`.
"""
import json
import subprocess
import unittest
from pathlib import Path

from integration.support import IntegrationCase, Sandbox, env_setting, live_conventions_url

ENABLED = env_setting("GARDENER_IT_CLAUDE") in ("1", "true")
WRITE_REPO = env_setting("GARDENER_IT_WRITE_REPO")
KEEP = env_setting("GARDENER_IT_KEEP") in ("1", "true")


def gh_json(*args: str):
    out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120, check=True)
    return json.loads(out.stdout)


def default_branch_sha(repo: str) -> str:
    branch = gh_json("repo", "view", repo, "--json", "defaultBranchRef")["defaultBranchRef"]["name"]
    return gh_json("api", f"repos/{repo}/branches/{branch}")["commit"]["sha"]


def numbers(kind: str, repo: str) -> set[int]:
    items = gh_json(kind, "list", "--repo", repo, "--state", "all", "--limit", "200", "--json", "number")
    return {item["number"] for item in items}


@unittest.skipUnless(ENABLED and WRITE_REPO,
                     "set GARDENER_IT_CLAUDE=1 and GARDENER_IT_WRITE_REPO=<a throwaway owner/repo> "
                     "to run the (paid, GitHub-writing) write-mode tier")
class LiveWriteModes(IntegrationCase):
    def box(self) -> Sandbox:
        box = Sandbox(self, repo=WRITE_REPO)
        if env_setting("GARDENER_IT_CLAUDE_BIN"):
            box.settings["GARDENER_CLAUDE_BIN"] = env_setting("GARDENER_IT_CLAUDE_BIN")
        return box

    def run_align(self, *args: str):
        box = self.box()
        self.conventions_url = live_conventions_url() or box.conventions()
        return box.align(*args, conventions_url=self.conventions_url)

    def assertConventionsNotNamed(self, *texts: str) -> None:
        local_dir = self.conventions_url.rstrip("/").rsplit("/", 1)[-1]
        for text in texts:
            self.assertNotIn(self.conventions_url, text, "the conventions URL was posted")
            self.assertNotIn(local_dir, text, "the conventions repo's name or path was posted")

    def snapshot(self) -> dict:
        return {"sha": default_branch_sha(WRITE_REPO), "prs": numbers("pr", WRITE_REPO),
                "issues": numbers("issue", WRITE_REPO)}

    def close_later(self, kind: str, number: int) -> None:
        if KEEP:
            return
        cmd = ["gh", kind, "close", str(number), "--repo", WRITE_REPO,
               "--comment", "Closed by gardener's live write-mode integration test."]
        if kind == "pr":
            cmd.append("--delete-branch")
        self.addCleanup(subprocess.run, cmd, capture_output=True, text=True, timeout=120)

    def test_implement_opens_one_unmerged_pull_request(self):
        before = self.snapshot()
        r = self.run_align("--implement")
        after = self.snapshot()
        new_prs = sorted(after["prs"] - before["prs"])
        for number in new_prs:
            self.close_later("pr", number)
        self.assertEqual(r.code, 0, r.describe())
        self.assertEqual(r.run["mode"], "implement")
        self.assertEqual(len(new_prs), 1, f"expected one new pull request, got {new_prs}\n{r.describe()}")
        pr = gh_json("pr", "view", str(new_prs[0]), "--repo", WRITE_REPO,
                     "--json", "state,mergedAt,baseRefName,headRefName,files,url,title,body,commits")
        self.assertEqual(pr["state"], "OPEN", pr)
        self.assertIsNone(pr["mergedAt"], "an --implement run merged its own pull request")
        self.assertNotEqual(pr["headRefName"], pr["baseRefName"])
        self.assertTrue(pr["files"], "the pull request changes no file")
        self.assertEqual(after["sha"], before["sha"], "the default branch moved")
        self.assertEqual(after["issues"] - before["issues"], set(), "--implement opened an issue")
        commits = [c["messageHeadline"] + "\n" + c["messageBody"] for c in pr["commits"]]
        self.assertConventionsNotNamed(pr["title"], pr["body"], pr["headRefName"], *commits)
        print(f"\n[implement] {pr['url']} ({len(pr['files'])} file(s)); "
              f"cost ${r.run['cost_usd']}, {r.seconds:.0f} s")

    def test_file_issue_opens_one_issue_and_changes_nothing(self):
        before = self.snapshot()
        r = self.run_align("--file-issue")
        after = self.snapshot()
        new_issues = sorted(after["issues"] - before["issues"])
        new_prs = sorted(after["prs"] - before["prs"])
        for number in new_issues:
            self.close_later("issue", number)
        for number in new_prs:
            self.close_later("pr", number)
        self.assertEqual(r.code, 0, r.describe())
        self.assertEqual(r.run["mode"], "file-issue")
        self.assertEqual(len(new_issues), 1, f"expected one new issue, got {new_issues}\n{r.describe()}")
        self.assertEqual(new_prs, [], "--file-issue opened a pull request")
        self.assertEqual(after["sha"], before["sha"], "the default branch moved")
        self.assertCloneUntouched(r)
        issue = gh_json("issue", "view", str(new_issues[0]), "--repo", WRITE_REPO, "--json", "url,title,body")
        self.assertTrue(issue["body"].strip(), "the issue has no body")
        self.assertConventionsNotNamed(issue["title"], issue["body"])
        print(f"\n[file-issue] {issue['url']}: {issue['title']}; "
              f"cost ${r.run['cost_usd']}, {r.seconds:.0f} s")


TEND_SKILL = """# /{slug}

A stand-in dev loop for gardener's live `tend` test. Do exactly this, then stop:

1. Create a branch named `feature/dev-loop-cycle-<UTC timestamp, digits only>`
   from the default branch.
2. Append one line to `CHANGELOG.md` (create it with a `# Changelog` heading
   if it is missing): `- <today's date, YYYY-MM-DD>: dev-loop cycle run by gardener tend`.
3. Commit with the message `Record a dev-loop cycle in the changelog`.
4. Push the branch and open one pull request for it, in passive voice, ending
   with a line saying it was drafted during a Gardener session.
5. Do not merge it, do not push to the default branch, and do not open an issue.
"""


@unittest.skipUnless(ENABLED and WRITE_REPO,
                     "set GARDENER_IT_CLAUDE=1 and GARDENER_IT_WRITE_REPO=<a throwaway owner/repo> "
                     "to run the (paid, GitHub-writing) tend test")
class LiveTend(LiveWriteModes):
    def install_skill(self) -> None:
        from gardener import dev_loop

        slug = f"{dev_loop.slugify_repo_name(WRITE_REPO)}-dev-loop"
        path = Path.home() / ".claude" / "commands" / f"{slug}.md"
        if path.exists() or path.is_symlink():
            self.skipTest(f"{path} already exists; not overwriting a real skill")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEND_SKILL.format(slug=slug))
        self.addCleanup(path.unlink, True)

    def test_tend_without_allow_merge_opens_at_most_one_unmerged_pull_request(self):
        self.install_skill()
        box = self.box()
        before = self.snapshot()
        r = box.cli("tend", "--repo", WRITE_REPO, timeout=1800)
        after = self.snapshot()
        new_prs = sorted(after["prs"] - before["prs"])
        for number in new_prs:
            self.close_later("pr", number)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        run = box.runs()[-1]
        self.assertEqual(run["mode"], "tend")
        self.assertIn("merge_eligible=False", r.stderr)
        self.assertLessEqual(len(new_prs), 1, f"tend opened {new_prs}")
        for number in new_prs:
            pr = gh_json("pr", "view", str(number), "--repo", WRITE_REPO, "--json", "state,mergedAt,url")
            self.assertIsNone(pr["mergedAt"], "tend merged without --allow-merge")
            print(f"\n[tend] {pr['url']}; cost ${run['cost_usd']}")
        self.assertEqual(after["sha"], before["sha"], "the default branch moved")
        self.assertEqual(after["issues"] - before["issues"], set(), "tend opened an issue")

    # The write-mode tests are inherited only to share helpers.
    test_implement_opens_one_unmerged_pull_request = None
    test_file_issue_opens_one_issue_and_changes_nothing = None


if __name__ == "__main__":
    unittest.main()
