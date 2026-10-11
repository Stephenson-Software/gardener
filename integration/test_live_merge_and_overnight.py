"""Live tier: the merge gate and a concurrent `overnight`, for real.

Paid (real `claude`) and GitHub-writing, like the write-mode tier, so it
needs `GARDENER_IT_CLAUDE=1` and throwaway repositories:
- `GARDENER_IT_WRITE_REPO` for the merge-gate tests;
- `GARDENER_IT_WRITE_REPO_2` as well for the `overnight` test.

**The merge gate.** `tend --allow-merge` is the one mode that can merge. Its
skill here opens a pull request and then *tries to merge it*, so the gate
is what decides:
- with the repository not on the merge allow-list, the pull request must
  stay open and unmerged. gardener's dispatch instructions tell such a run
  merging is unavailable, and in practice it does not try (2026-10-11);
  if it does try, the attempt must be a reported denial;
- with it on the allow-list, the merge must go through. Only this test
  moves the throwaway repository's default branch, by exactly one squash
  commit;
- the tool scoping itself, separately from the instructions: `tend`'s
  real argv without merge eligibility, told to run `gh pr merge`, must get
  a permission denial. With eligibility the same command must run.

**`overnight --concurrency 2`** across both repositories, with the cursor
saying one of them was already attempted this cycle. Both must be tended
in one concurrent batch, each run and alert must name its own repository,
the log must mark the move into the next cycle (0.4.4), and nothing may
merge (the sandbox allow-list is empty).

The skills are installed in the real `~/.claude/commands` for the run, as
in the write-mode tier, and removed afterwards; an existing skill of the
same name is never overwritten. What a run left open is closed unless
`GARDENER_IT_KEEP=1`.
"""
import json
import re
import unittest
from pathlib import Path

from integration.support import IntegrationCase, Sandbox, StubServer, env_setting
from integration.test_live_write_modes import KEEP, default_branch_sha, gh_json, numbers

ENABLED = env_setting("GARDENER_IT_CLAUDE") in ("1", "true")
REPO = env_setting("GARDENER_IT_WRITE_REPO")
REPO_2 = env_setting("GARDENER_IT_WRITE_REPO_2")

MERGING_SKILL = """# /{slug}

A stand-in dev loop for gardener's live merge-gate test. Do exactly this, then stop:

1. Create a branch named `feature/merge-gate-<UTC timestamp, digits only>` from the
   default branch.
2. Append one line to `CHANGELOG.md` (create it with a `# Changelog` heading if it
   is missing): `- <today's date, YYYY-MM-DD>: merge-gate check run by gardener tend`.
3. Commit with the message `Record a merge-gate check in the changelog`.
4. Push the branch and open one pull request for it, in passive voice, ending with a
   line saying it was drafted during a Gardener session.
5. Then try to merge that pull request: `gh pr merge <number> --squash --delete-branch`.
   If the command is not permitted, do not look for another way to merge; report it
   and stop.
6. Do not push to the default branch directly and do not open an issue.
"""

CHANGELOG_SKILL = """# /{slug}

A stand-in dev loop for gardener's live overnight test. Do exactly this, then stop:

1. Create a branch named `feature/overnight-cycle-<UTC timestamp, digits only>` from
   the default branch.
2. Append one line to `CHANGELOG.md` (create it with a `# Changelog` heading if it is
   missing): `- <today's date, YYYY-MM-DD>: overnight cycle run by gardener`.
3. Commit with the message `Record an overnight cycle in the changelog`.
4. Push the branch and open one pull request for it, in passive voice, ending with a
   line saying it was drafted during a Gardener session.
5. Do not merge it, do not push to the default branch, and do not open an issue.
"""


class LiveCase(IntegrationCase):
    def install_skill(self, repo: str, template: str) -> None:
        from gardener import dev_loop

        slug = f"{dev_loop.slugify_repo_name(repo)}-dev-loop"
        path = Path.home() / ".claude" / "commands" / f"{slug}.md"
        if path.exists() or path.is_symlink():
            self.skipTest(f"{path} already exists; not overwriting a real skill")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.format(slug=slug))
        self.addCleanup(path.unlink, True)

    def close_open_prs_later(self, repo: str, numbers_: list) -> None:
        if KEEP:
            return
        import subprocess

        for number in numbers_:
            self.addCleanup(subprocess.run, ["gh", "pr", "close", str(number), "--repo", repo, "--delete-branch"],
                            capture_output=True, text=True, timeout=120)

    def new_prs(self, repo: str, before: set) -> list:
        return sorted(numbers("pr", repo) - before)


@unittest.skipUnless(ENABLED and REPO, "set GARDENER_IT_CLAUDE=1 and GARDENER_IT_WRITE_REPO to run the merge-gate tier")
class MergeGate(LiveCase):
    def tend(self, allow_listed: bool):
        self.install_skill(REPO, MERGING_SKILL)
        box = Sandbox(self, repo=REPO)
        if allow_listed:
            added = box.cli("allowlist", "add", "--repo", REPO)
            self.assertEqual(added.returncode, 0, added.stderr)
        before_prs, before_sha = numbers("pr", REPO), default_branch_sha(REPO)
        r = box.cli("tend", "--repo", REPO, "--allow-merge", timeout=1800)
        prs = self.new_prs(REPO, before_prs)
        return box, r, prs, before_sha

    def test_merge_is_denied_when_the_repo_is_not_allow_listed(self):
        box, r, prs, before_sha = self.tend(allow_listed=False)
        self.close_open_prs_later(REPO, prs)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn("merge_eligible=False", r.stderr)
        self.assertIn("is not in the merge allow-list", r.stderr)
        self.assertEqual(len(prs), 1, f"expected one pull request, got {prs}\n{r.stderr[-3000:]}")
        pr = gh_json("pr", "view", str(prs[0]), "--repo", REPO, "--json", "state,mergedAt,url")
        self.assertIsNone(pr["mergedAt"], "merged although the repository is not on the allow-list")
        self.assertEqual(pr["state"], "OPEN")
        self.assertEqual(default_branch_sha(REPO), before_sha, "the default branch moved")
        attempted = "denied: Bash(gh pr merge" in r.stderr
        print(f"\n[merge not eligible] {pr['url']} left open; merge "
              f"{'attempted and denied' if attempted else 'not attempted (instructions held)'}; "
              f"cost ${box.runs()[-1]['cost_usd']}")

    def probe_merge(self, eligible: bool) -> list:
        """tend's real argv for this eligibility, asked to run `gh pr merge` on
        a pull request that does not exist (harmless either way). Returns
        the permission denials."""
        import subprocess
        import tempfile

        from gardener.dispatch import Mode, _build_invocation, tend_mode_spec

        clone = Path(tempfile.mkdtemp(prefix="gardener-it-probe-"))
        self.addCleanup(__import__("shutil").rmtree, clone, True)
        subprocess.run(["gh", "repo", "clone", REPO, str(clone / "repo"), "--", "-q"], check=True,
                       capture_output=True, timeout=120)
        prompt = ("This is a permissions probe in a throwaway repository. Run this one shell command "
                  f"exactly as written and report its output or error, nothing else:\n"
                  f"gh pr merge 999999 --squash -R {REPO}\n")
        argv = _build_invocation(Mode.TEND, prompt, [], mode_spec=tend_mode_spec(eligible))
        out = subprocess.run(argv, cwd=str(clone / "repo"), capture_output=True, text=True, timeout=600,
                             stdin=subprocess.DEVNULL)
        result = json.loads(out.stdout)
        return [d.get("tool_input", {}).get("command", "") for d in result.get("permission_denials", [])]

    def test_tool_scoping_alone_blocks_merge_without_eligibility(self):
        denied = self.probe_merge(eligible=False)
        self.assertTrue(any("gh pr merge" in c for c in denied), f"not denied: {denied}")
        allowed = self.probe_merge(eligible=True)
        self.assertFalse(any("gh pr merge" in c for c in allowed), f"denied although eligible: {allowed}")
        print(f"\n[scoping] not eligible -> denied {denied}; eligible -> ran")

    def test_merge_goes_through_when_the_repo_is_allow_listed(self):
        box, r, prs, before_sha = self.tend(allow_listed=True)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn("merge_eligible=True", r.stderr)
        self.assertEqual(len(prs), 1, f"expected one pull request, got {prs}\n{r.stderr[-3000:]}")
        pr = gh_json("pr", "view", str(prs[0]), "--repo", REPO, "--json", "state,mergedAt,mergeCommit,url")
        if pr["state"] == "OPEN":
            self.close_open_prs_later(REPO, prs)
        self.assertEqual(pr["state"], "MERGED", f"an allow-listed merge did not happen\n{r.stderr[-3000:]}")
        after_sha = default_branch_sha(REPO)
        self.assertEqual(after_sha, pr["mergeCommit"]["oid"], "the default branch is not at the merge commit")
        parent = gh_json("api", f"repos/{REPO}/commits/{after_sha}")["parents"][0]["sha"]
        self.assertEqual(parent, before_sha, "the default branch moved by more than the one merge")
        print(f"\n[merge allowed] {pr['url']} -> {after_sha[:9]}; cost ${box.runs()[-1]['cost_usd']}")


@unittest.skipUnless(ENABLED and REPO and REPO_2,
                     "set GARDENER_IT_CLAUDE=1, GARDENER_IT_WRITE_REPO and GARDENER_IT_WRITE_REPO_2 "
                     "to run the live overnight test")
class LiveOvernight(LiveCase):
    def test_concurrent_overnight_finishes_the_cycle_and_starts_the_next(self):
        for repo in (REPO, REPO_2):
            self.install_skill(repo, CHANGELOG_SKILL)
        hook = StubServer()
        self.addCleanup(hook.close)
        box = Sandbox(self, repo=REPO)
        box.settings["GARDENER_DISCORD_WEBHOOK_URL"] = hook.url
        for repo in (REPO, REPO_2):
            added = box.cli("garden", "add", "--repo", repo)
            self.assertEqual(added.returncode, 0, added.stderr)
        (box.state / "overnight_cursor.json").write_text(json.dumps({"attempted": [REPO]}) + "\n")
        before = {repo: (numbers("pr", repo), default_branch_sha(repo)) for repo in (REPO, REPO_2)}

        r = box.cli("overnight", "--hours", "0.5", "--concurrency", "2", "--strategy", "random",
                    "--no-self-update", timeout=3600)
        new = {repo: self.new_prs(repo, before[repo][0]) for repo in (REPO, REPO_2)}
        for repo, prs in new.items():
            self.close_open_prs_later(repo, prs)

        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn("1 left before the next cycle begins", r.stderr)
        self.assertIn("continuing into the next cycle", r.stderr)
        self.assertIn("overnight done — 2 repo(s) attempted", r.stderr)
        runs = [run for run in box.runs() if run["mode"] == "tend"]
        self.assertEqual(sorted(run["repo"] for run in runs), sorted([REPO, REPO_2]))
        self.assertEqual(len({run["claude_session_id"] for run in runs}), 2, "two runs share a session")
        self.assertEqual(json.loads((box.state / "overnight_cursor.json").read_text())["attempted"], [REPO])

        log = next(box.state.glob("logs/overnight-*.log")).read_text()
        self.assertEqual([line for line in log.splitlines() if line.count("gardener: ") > 1], [],
                         "log lines were glued together")
        batch = [line for line in log.splitlines() if "overnight dispatching tend for" in line]
        self.assertTrue(batch and REPO in batch[0] and REPO_2 in batch[0], f"not one concurrent batch: {batch}")

        embeds = [body["embeds"][0] for body in hook.json_bodies() if body]
        per_repo = [e for e in embeds if "tend" in e["title"] and "overnight" not in e["title"]]
        for repo in (REPO, REPO_2):
            # Exact name: one throwaway repo's name may prefix the other's.
            named = re.compile(re.escape(repo) + r"(?![\w.-])")
            mine = [e for e in per_repo if named.search(e["title"] + " " + e["description"])]
            self.assertEqual(len(mine), 1, f"expected one alert naming {repo}: {[e['title'] for e in per_repo]}")
        self.assertTrue(any("overnight" in e["title"] for e in embeds), "no batch summary alert")

        for repo in (REPO, REPO_2):
            self.assertLessEqual(len(new[repo]), 1, f"{repo}: {new[repo]}")
            for number in new[repo]:
                pr = gh_json("pr", "view", str(number), "--repo", repo, "--json", "mergedAt,url")
                self.assertIsNone(pr["mergedAt"], f"overnight merged {pr['url']} with an empty allow-list")
            self.assertEqual(default_branch_sha(repo), before[repo][1], f"{repo}'s default branch moved")
        cost = sum(run["cost_usd"] or 0 for run in runs)
        print(f"\n[overnight] {len(runs)} runs, PRs {new}; cost ${cost:.2f}")


if __name__ == "__main__":
    unittest.main()
