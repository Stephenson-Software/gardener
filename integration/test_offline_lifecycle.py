"""Offline tier: the rest of a dispatch's lifecycle through the real CLI.

- `ps`, `stop` and `kill` against a running session.
- `tend`, including its write-mode gate, the merge allow-list and the
  dev-loop skill bootstrap.
- `overnight`: concurrent batches, preflight, and Ctrl-C mid-batch.

The fake `gh` serves clones and refreshes from local bare repositories
(`Sandbox(local_github=True)`), so these also exercise the real
clone-and-refresh path. Every test asserts that no `gh` call could have
written anything. HOME points into the sandbox wherever `tend` would read
or write dev-loop skills, so nothing touches the operator's real
`~/.claude`.
"""
import json
import os
import signal
import subprocess
import time
import unittest

from integration.support import IntegrationCase, Sandbox, pid_alive, wait_dead

SECOND_REPO = "it-fixture/second"


class Sessions(IntegrationCase):
    """`gardener stop`/`kill` walk the session's process tree. The agent
    runs in its own session (0.4.1), so these check it is still reached."""

    def running_align(self):
        box = Sandbox(self)
        box.stub_harness("sleep")
        proc = box.start_align()
        children = box.sleeping_agents(1)
        self.assertEqual(len(children), 1, "the agent never started")
        return box, proc, children

    def session_id(self, box) -> str:
        ps = box.cli("ps", "-q")
        ids = ps.stdout.split()
        self.assertEqual(len(ids), 1, ps.stdout + ps.stderr)
        return ids[0]

    def test_ps_lists_the_running_align(self):
        box, proc, children = self.running_align()
        try:
            ps = box.cli("ps")
            self.assertIn("align", ps.stdout)
            self.assertIn(box.repo, ps.stdout)
        finally:
            # Not SIGKILL: no process can clean up after that one, so the
            # agent would be left running.
            box.cli("stop", "--all", "--time", "5")
            proc.wait(timeout=30)
            wait_dead(children)

    def test_stop_ends_the_session_and_its_agent(self):
        box, proc, children = self.running_align()
        sid = self.session_id(box)
        stop = box.cli("stop", sid, "--time", "5")
        self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)
        proc.wait(timeout=30)
        self.assertEqual(wait_dead(children), [], "the agent outlived `gardener stop`")
        self.assertEqual(box.cli("ps", "-q").stdout.strip(), "")

    def test_kill_ends_the_session_and_its_agent(self):
        box, proc, children = self.running_align()
        sid = self.session_id(box)
        kill = box.cli("kill", sid)
        self.assertEqual(kill.returncode, 0, kill.stdout + kill.stderr)
        proc.wait(timeout=30)
        self.assertEqual(wait_dead(children), [], "the agent outlived `gardener kill`")


class AlignThroughGitHub(IntegrationCase):
    def test_align_clones_then_refreshes_through_gh(self):
        box = Sandbox(self, local_github=True)
        box.stub_harness("ok-json")
        first = box.align()
        self.assertEqual(first.code, 0, first.describe())
        self.assertTrue(any(c.startswith("repo clone") for c in box.gh_calls()))
        # A second run refreshes the existing clone instead of cloning again.
        second = box.align()
        self.assertEqual(second.code, 0, second.describe())
        self.assertEqual(sum(c.startswith("repo clone") for c in box.gh_calls()), 1)
        self.assertSafeReport(second)
        self.assertEqual(box.write_gh_calls(), [])


class Tend(IntegrationCase):
    def box(self, behaviour="ok-json", skill=True, unscoped="tend,create-dev-loop"):
        box = Sandbox(self, local_github=True, fake_home=True)
        box.stub_harness(behaviour)
        if unscoped:
            box.settings["GARDENER_HARNESS_ALLOW_UNSCOPED"] = unscoped
        box.add_remote()
        if skill:
            box.install_dev_loop_skill()
        return box

    def spec(self, record) -> dict:
        return json.loads(record["env"]["GARDENER_HARNESS_SPEC"])

    def test_tend_is_refused_without_the_unscoped_opt_in(self):
        box = self.box(unscoped="")
        r = box.cli("tend", "--repo", box.repo)
        self.assertNotEqual(r.returncode, 0, r.stderr)
        self.assertIn("GARDENER_HARNESS_ALLOW_UNSCOPED", r.stderr)
        self.assertEqual(box._records(), [])

    def test_tend_dispatches_with_merge_off_unless_allow_listed(self):
        box = self.box()
        r = box.cli("tend", "--repo", box.repo, "--allow-merge")
        self.assertEqual(r.returncode, 0, r.stderr)
        records = box._records()
        self.assertEqual([rec["env"]["GARDENER_HARNESS_MODE"] for rec in records], ["tend"])
        self.assertFalse(self.spec(records[0])["merge_allowed"], "merge allowed without the allow-list")
        self.assertNotEqual(self.spec(records[0])["permission_mode"], "bypassPermissions")
        self.assertEqual(box.runs()[-1]["mode"], "tend")
        self.assertEqual(box.write_gh_calls(), [])

        added = box.cli("allowlist", "add", "--repo", box.repo)
        self.assertEqual(added.returncode, 0, added.stderr)
        r = box.cli("tend", "--repo", box.repo, "--allow-merge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.spec(box._records()[-1])["merge_allowed"])

        r = box.cli("tend", "--repo", box.repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.spec(box._records()[-1])["merge_allowed"], "merge allowed without --allow-merge")
        self.assertEqual(box.write_gh_calls(), [])

    def test_missing_skill_is_bootstrapped_inside_the_sandbox_home(self):
        box = self.box(skill=False)
        r = box.cli("tend", "--repo", box.repo)
        records = box._records()
        self.assertTrue(records, r.stderr)
        self.assertEqual(records[0]["env"]["GARDENER_HARNESS_MODE"], "create-dev-loop")
        for d in self.spec(records[0])["add_dirs"]:
            self.assertTrue(d.startswith(str(box.home)), f"create-dev-loop was pointed outside the sandbox: {d}")
        self.assertEqual(box.write_gh_calls(), [])


class Overnight(IntegrationCase):
    def garden(self, behaviour="ok-json", unscoped="tend,create-dev-loop"):
        box = Sandbox(self, local_github=True, fake_home=True)
        box.stub_harness(behaviour)
        if unscoped:
            box.settings["GARDENER_HARNESS_ALLOW_UNSCOPED"] = unscoped
        for repo in (box.repo, SECOND_REPO):
            box.add_remote(repo)
            box.install_dev_loop_skill(repo)
            added = box.cli("garden", "add", "--repo", repo)
            self.assertEqual(added.returncode, 0, added.stderr)
        return box

    ARGS = ("overnight", "--hours", "0.2", "--timeout", "120", "--concurrency", "2",
            "--strategy", "round-robin", "--no-self-update")

    def test_every_garden_repo_is_tended_concurrently(self):
        box = self.garden()
        r = box.cli(*self.ARGS)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        tended = sorted(run["repo"] for run in box.runs() if run["mode"] == "tend")
        self.assertEqual(tended, sorted([box.repo, SECOND_REPO]))
        self.assertEqual(len(box._records()), 2)
        self.assertIn("overnight done — 2 repo(s) attempted", r.stderr)
        # Concurrent dispatches print at the same time; every log line must
        # still be whole (0.4.3: lines used to be glued together mid-line).
        log = next(box.state.glob("logs/overnight-*.log")).read_text()
        glued = [line for line in log.splitlines() if line.count("gardener: ") > 1]
        self.assertEqual(glued, [], "log lines were glued together")
        self.assertEqual(box.write_gh_calls(), [])

    def test_an_unready_harness_aborts_before_any_dispatch(self):
        box = self.garden(unscoped="")
        r = box.cli(*self.ARGS)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(box._records(), [], "a repo was dispatched despite the unready harness")

    def test_a_run_that_finishes_a_cycle_continues_into_the_next(self):
        """0.4.4: with one repo left in the cycle, a random-order run used
        to tend that one repo and stop, leaving most of its budget unused
        (2026-10-11: one repo in 7 minutes of an 8-hour budget)."""
        box = self.garden()
        cursor = box.state / "overnight_cursor.json"
        cursor.write_text(json.dumps({"attempted": [box.repo]}) + "\n")
        r = box.cli("overnight", "--hours", "0.2", "--timeout", "120", "--concurrency", "1",
                    "--strategy", "random", "--no-self-update")
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        tended = [run["repo"] for run in box.runs() if run["mode"] == "tend"]
        self.assertEqual(tended, [SECOND_REPO, box.repo], "the leftover first, then the next cycle")
        self.assertIn("1 left before the next cycle begins", r.stderr)
        self.assertIn("continuing into the next cycle", r.stderr)
        # The finished cycle's history is dropped; the next cycle has begun
        # with the one repo this run reached past the boundary.
        self.assertEqual(json.loads(cursor.read_text())["attempted"], [box.repo])
        self.assertEqual(box.write_gh_calls(), [])

    def test_ctrl_c_mid_batch_kills_every_agent(self):
        """The 0.4.1 regression: agents run on worker threads in their own
        sessions, so the terminal's Ctrl-C reached neither them nor their
        threads. Fixed in 0.4.2."""
        box = self.garden(behaviour="sleep")
        proc = box.start_cli(*self.ARGS)
        children = box.sleeping_agents(2)
        self.assertEqual(len(children), 2, "both agents never started")
        self.assertTrue(all(pid_alive(p) for p in children))
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=60)
            hung = False
        except subprocess.TimeoutExpired:
            hung = True  # waiting on worker threads whose agents never died
        survivors = wait_dead(children)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        for pid in survivors:
            os.kill(pid, signal.SIGKILL)
        self.assertEqual(survivors, [], "an agent outlived Ctrl-C during overnight")
        self.assertFalse(hung, "overnight did not exit after Ctrl-C")


if __name__ == "__main__":
    unittest.main()
