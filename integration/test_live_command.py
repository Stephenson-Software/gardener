"""Live tier: any `command` harness, through the real `gardener align`.

Skipped unless `GARDENER_IT_COMMAND` is set to the harness command (as it
would appear in GARDENER_HARNESS_COMMAND). Optional:

    GARDENER_IT_MODEL         passed as GARDENER_HARNESS_MODEL
    GARDENER_IT_EXPECT        deliver (default): the report must succeed;
                              any: a clean, well-formed failure also passes
    GARDENER_IT_PASSTHROUGH   GARDENER_-prefixed settings the harness itself
                              reads (e.g. GARDENER_ORKET_*), let through the
                              sandbox's scrub of ambient gardener settings
    GARDENER_IT_REPO / GARDENER_IT_CONVENTIONS_URL
                              a real target and conventions repo instead of
                              the bundled offline fixtures

Whatever the harness, the invariants are the same: a report run never
changes the clone or pushes, its envelope (if any) is well formed, and a
report-only harness asked for a write mode refuses instead of writing.
"""
import unittest

from integration.support import IntegrationCase, Sandbox, env_setting, live_conventions_url, live_repo

COMMAND = env_setting("GARDENER_IT_COMMAND")
MODEL = env_setting("GARDENER_IT_MODEL")
EXPECT = env_setting("GARDENER_IT_EXPECT", "deliver")
ENVELOPE_KEYS = {"result", "is_error"}


@unittest.skipUnless(COMMAND, "set GARDENER_IT_COMMAND to run the generic command-harness tier")
class LiveCommandHarness(IntegrationCase):
    def box(self) -> Sandbox:
        import shlex

        box = Sandbox(self, repo=live_repo())
        box.command_harness(shlex.split(COMMAND), model=MODEL)
        return box

    def test_report(self):
        box = self.box()
        r = box.align(conventions_url=live_conventions_url())
        self.assertSafeReport(r)
        if r.envelope is not None:
            self.assertTrue(ENVELOPE_KEYS <= set(r.envelope), r.envelope)
        if EXPECT == "deliver":
            self.assertEqual(r.code, 0, r.describe())
        else:
            self.assertIn(r.code, (0, 1), r.describe())
        if r.code == 0:
            self.assertTrue(r.stdout.strip(), "a successful run printed no report")
            self.assertEqual(r.run["outcome"], "report")
        else:
            self.assertEqual(r.run["outcome"], "error")

    @unittest.skipIf(live_repo(), "write-mode probe only runs against the offline fixture target")
    def test_report_only_harness_refuses_write_modes(self):
        """With gardener's gate opened, a report-only harness must refuse
        `--implement` itself. Offline target only: its origin is a GitHub
        URL that does not exist, so even a misbehaving harness cannot push."""
        box = self.box()
        box.settings["GARDENER_HARNESS_ALLOW_UNSCOPED"] = "implement"
        r = box.align("--implement", conventions_url=live_conventions_url())
        self.assertNotEqual(r.code, 0, r.describe())
        self.assertCloneUntouched(r)


if __name__ == "__main__":
    unittest.main()
