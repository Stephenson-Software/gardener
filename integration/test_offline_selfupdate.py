"""Offline tier: self-update with real git, through the real CLI.

Each test builds a copy of this gardener's own package as an "older"
checkout (commit A) tracking a local bare origin, then pushes a "newer"
commit B to that origin. It then runs `gardener` *from that checkout* as an
editable install would: `PYTHONPATH` points at it, so `selfupdate`
finds the checkout through its own module file. The tests cover:

- `gardener update --check` and `gardener update`: a real fast-forward that
  the next process imports;
- every skip: tracked local changes, a diverged branch, a detached HEAD, a
  branch with no upstream. Untracked files alone don't block;
- an unreachable origin is an error (exit 1), not a skip;
- `overnight`'s self-update step: logged, silent on success, alerted on a
  skip, and the run carries on either way.

Nothing leaves the machine: the origin is a local bare repository and the
webhook a loopback stub.
"""
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from integration.support import ROOT, IntegrationCase, Sandbox, StubServer, git

NEW_VERSION = "9.9.9+it"


class SelfUpdateCase(IntegrationCase):
    def setUp(self):
        self.box = Sandbox(self)
        root = self.box.root
        self.checkout = root / "gardener-checkout"
        self.origin = root / "gardener-origin.git"
        self._build_checkout()
        self.old = self.head()
        self.new = self._push_newer_commit()
        self.box.extra_env["PYTHONPATH"] = str(self.checkout)

    def _build_checkout(self) -> None:
        self.checkout.mkdir()
        shutil.copytree(ROOT / "gardener", self.checkout / "gardener",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy(ROOT / "pyproject.toml", self.checkout / "pyproject.toml")
        (self.checkout / ".gitignore").write_text("__pycache__/\n")
        git(self.checkout, "init", "-q")
        git(self.checkout, "symbolic-ref", "HEAD", "refs/heads/main")  # git 2.25: no `init -b`
        git(self.checkout, "add", "-A")
        git(self.checkout, "commit", "-q", "-m", "older gardener")
        git(self.box.root, "init", "-q", "--bare", str(self.origin))
        git(self.checkout, "remote", "add", "origin", str(self.origin))
        git(self.checkout, "push", "-q", "-u", "origin", "main")

    def _push_newer_commit(self) -> str:
        other = self.box.root / "upstream-work"
        git(self.box.root, "clone", "-q", "-b", "main", str(self.origin), str(other))
        init = other / "gardener" / "__init__.py"
        text = init.read_text()
        start = text.index('__version__ = "')
        end = text.index('"', start + len('__version__ = "'))
        init.write_text(text[:start] + f'__version__ = "{NEW_VERSION}' + text[end:])
        git(other, "commit", "-q", "-am", "newer gardener")
        git(other, "push", "-q", "origin", "main")
        return git(other, "rev-parse", "HEAD").stdout.strip()

    def head(self) -> str:
        return git(self.checkout, "rev-parse", "HEAD").stdout.strip()

    def imported_version(self) -> str:
        """What a fresh process gets on `import gardener` from the checkout."""
        out = subprocess.run([sys.executable, "-c", "import gardener; print(gardener.__version__)"],
                             cwd=str(self.box.root), env=self.box.env(), capture_output=True,
                             text=True, timeout=60)
        return out.stdout.strip()


class Update(SelfUpdateCase):
    def test_check_reports_the_update_without_applying_it(self):
        r = self.box.cli("update", "--check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"update available: {self.old[:9]} -> {self.new[:9]}", r.stdout)
        self.assertEqual(self.head(), self.old)

    def test_update_fast_forwards_and_the_next_process_runs_the_new_code(self):
        self.assertNotEqual(self.imported_version(), NEW_VERSION)
        r = self.box.cli("update")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"updated {self.old[:9]} -> {self.new[:9]}", r.stdout)
        self.assertEqual(self.head(), self.new)
        self.assertEqual(git(self.checkout, "status", "--porcelain").stdout, "")
        self.assertEqual(self.imported_version(), NEW_VERSION)
        again = self.box.cli("update")
        self.assertIn(f"already up to date ({self.new[:9]})", again.stdout)

    def test_untracked_files_do_not_block_the_update(self):
        (self.checkout / "scratch-notes.md").write_text("local only\n")
        r = self.box.cli("update")
        self.assertIn("updated", r.stdout, r.stderr)
        self.assertEqual(self.head(), self.new)
        self.assertTrue((self.checkout / "scratch-notes.md").exists())


class Skips(SelfUpdateCase):
    def assertSkipped(self, needle: str) -> None:
        before = self.head()
        r = self.box.cli("update")
        self.assertEqual(r.returncode, 0, "a skip is not a CLI failure: " + r.stderr)
        self.assertIn(needle, r.stdout)
        self.assertEqual(self.head(), before, "a skip moved HEAD")

    def test_tracked_local_changes_are_never_overwritten(self):
        target = self.checkout / "gardener" / "garden.py"
        target.write_text(target.read_text() + "\n# local edit\n")
        self.assertSkipped("has uncommitted local changes")
        self.assertIn("# local edit", target.read_text())

    def test_a_diverged_branch_is_not_merged(self):
        (self.checkout / "LOCAL.md").write_text("an unpushed local commit\n")
        git(self.checkout, "add", "LOCAL.md")
        git(self.checkout, "commit", "-q", "-m", "local work")
        self.assertSkipped("not a fast-forward")
        self.assertTrue((self.checkout / "LOCAL.md").exists())

    def test_a_detached_head_is_left_alone(self):
        git(self.checkout, "checkout", "-q", "--detach")
        self.assertSkipped("detached HEAD")

    def test_a_branch_without_an_upstream_is_left_alone(self):
        git(self.checkout, "checkout", "-q", "-b", "operator-testing")
        self.assertSkipped("has no upstream on origin")

    def test_an_unreachable_origin_is_an_error(self):
        self.origin.rename(self.origin.with_name("moved.git"))
        r = self.box.cli("update")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("git fetch failed", r.stdout)
        self.assertEqual(self.head(), self.old)


class Overnight(SelfUpdateCase):
    """`overnight` self-updates before reading the garden. It must log the
    outcome, alert only when the run is left on code it couldn't update,
    and carry on either way. The garden is empty, so nothing is tended."""

    def run_overnight(self, *extra: str):
        hook = StubServer()
        self.addCleanup(hook.close)
        self.box.stub_harness("ok-json")
        self.box.settings["GARDENER_HARNESS_ALLOW_UNSCOPED"] = "tend"
        self.box.settings["GARDENER_DISCORD_WEBHOOK_URL"] = hook.url
        r = self.box.cli("overnight", "--hours", "0.05", *extra)
        return r, hook

    def test_overnight_updates_silently_and_carries_on(self):
        r, hook = self.run_overnight()
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn(f"gardener: self-update: updated {self.old[:9]} -> {self.new[:9]}", r.stderr)
        self.assertIn("garden is empty", r.stderr)
        self.assertEqual(self.head(), self.new)
        self.assertEqual(hook.requests, [], "a routine update sent an alert")

    def test_a_skipped_update_alerts_and_the_run_carries_on(self):
        target = self.checkout / "gardener" / "garden.py"
        target.write_text(target.read_text() + "\n# local edit\n")
        r, hook = self.run_overnight()
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertIn("has uncommitted local changes", r.stderr)
        self.assertIn("garden is empty", r.stderr, "the run stopped after the skip")
        self.assertEqual(len(hook.requests), 1, [x["body"] for x in hook.requests])
        embed = hook.json_bodies()[0]["embeds"][0]
        self.assertIn("self-update SKIPPED", embed["title"])
        self.assertEqual(self.head(), self.old)

    def test_no_self_update_leaves_the_checkout_alone(self):
        r, hook = self.run_overnight("--no-self-update")
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertNotIn("self-update", r.stderr)
        self.assertEqual(self.head(), self.old)
        self.assertEqual(hook.requests, [])


if __name__ == "__main__":
    unittest.main()
