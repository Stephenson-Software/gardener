"""Live tier: the default Claude Code harness, with a real `claude -p`.

This spends money (one report-mode dispatch, typically well under $1), so
it is skipped unless `GARDENER_IT_CLAUDE=1`. It uses the offline fixture
target unless `GARDENER_IT_REPO` names a real one, and the bundled
conventions unless `GARDENER_IT_CONVENTIONS_URL` is set. `claude` must be
on PATH (or `GARDENER_IT_CLAUDE_BIN` set) and already authenticated.

This is the automated form of the manual check CLAUDE.md requires for any
dispatch-layer change: exit 0, the clone unchanged, nothing pushed.
"""
import unittest

from integration.support import IntegrationCase, Sandbox, env_setting, live_conventions_url, live_repo

ENABLED = env_setting("GARDENER_IT_CLAUDE") in ("1", "true")


@unittest.skipUnless(ENABLED, "set GARDENER_IT_CLAUDE=1 to run the (paid) Claude Code tier")
class LiveClaudeCode(IntegrationCase):
    def test_report(self):
        box = Sandbox(self, repo=live_repo())
        if env_setting("GARDENER_IT_CLAUDE_BIN"):
            box.settings["GARDENER_CLAUDE_BIN"] = env_setting("GARDENER_IT_CLAUDE_BIN")
        r = box.align(conventions_url=live_conventions_url())
        self.assertEqual(r.code, 0, r.describe())
        self.assertSafeReport(r)
        self.assertIn("GARDENER_SUMMARY:", r.stdout)
        self.assertEqual(r.run["outcome"], "report")
        self.assertGreater(r.run["cost_usd"] or 0, 0)


if __name__ == "__main__":
    unittest.main()
