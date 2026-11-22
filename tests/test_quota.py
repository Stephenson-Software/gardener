"""Tests for `gardener/quota.py`: reset-time parsing, the wait rule, and the
pre-flight probe's classification. The probe's runner is always injected,
so nothing here invokes `claude`."""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gardener import quota
from gardener.dispatch import DispatchResult, HarnessConfig, Mode

UTC = timezone.utc
MST = timezone(timedelta(hours=-7))

# The real wordings this is calibrated against (see dispatch.py's markers).
SESSION_LIMIT = "You've hit your session limit · resets 12am (UTC)"
BILLING = (
    "Claude Code can't use your Claude subscription, usually because a payment is past due · "
    "Pay or renew at https://claude.ai/settings/billing (if you just paid, try again shortly), "
    "or use an Anthropic API key instead"
)


def _result(ok, text="", stderr="", blocked=False):
    return DispatchResult(
        ok=ok, result_text=text, raw_stdout="{}", stderr=stderr, exit_code=0 if ok else 1,
        duration_ms=1, cost_usd=0, session_id="s", permission_denials=[], is_error=not ok,
        blocked=blocked,
    )


class TestParseResetTime(unittest.TestCase):
    def test_bare_hour_with_zone_is_next_occurrence(self):
        now = datetime(2026, 11, 22, 22, 30, tzinfo=UTC)
        self.assertEqual(
            quota.parse_reset_time(SESSION_LIMIT, now), datetime(2026, 11, 23, 0, 0, tzinfo=UTC)
        )

    def test_later_today(self):
        now = datetime(2026, 11, 22, 13, 0, tzinfo=UTC)
        self.assertEqual(
            quota.parse_reset_time("5-hour limit reached ∙ resets 3:30pm (UTC)", now),
            datetime(2026, 11, 22, 15, 30, tzinfo=UTC),
        )

    def test_no_zone_uses_nows_zone(self):
        now = datetime(2026, 11, 22, 19, 0, tzinfo=MST)
        reset = quota.parse_reset_time("limit reached ∙ resets 9pm", now)
        self.assertEqual(reset, datetime(2026, 11, 22, 21, 0, tzinfo=MST))

    def test_named_zone(self):
        now = datetime(2026, 11, 23, 1, 0, tzinfo=UTC)  # 18:00 in Denver
        reset = quota.parse_reset_time("resets 11pm (America/Denver)", now)
        self.assertEqual(reset.astimezone(UTC), datetime(2026, 11, 23, 6, 0, tzinfo=UTC))

    def test_dated(self):
        now = datetime(2026, 11, 22, 19, 0, tzinfo=UTC)
        reset = quota.parse_reset_time("You've hit your weekly limit · resets Nov 25, 5am (UTC)", now)
        self.assertEqual(reset, datetime(2026, 11, 25, 5, 0, tzinfo=UTC))

    def test_dated_across_new_year(self):
        now = datetime(2026, 12, 31, 20, 0, tzinfo=UTC)
        reset = quota.parse_reset_time("resets Jan 2 at 1am (UTC)", now)
        self.assertEqual(reset, datetime(2027, 1, 2, 1, 0, tzinfo=UTC))

    def test_epoch_form(self):
        now = datetime(2026, 11, 22, tzinfo=UTC)
        reset = quota.parse_reset_time("Claude AI usage limit reached|1795400000", now)
        self.assertEqual(reset, datetime.fromtimestamp(1795400000, tz=UTC))

    def test_unreadable_is_none(self):
        now = datetime(2026, 11, 22, tzinfo=UTC)
        for text in ("", "usage limit reached", "resets soon", "resets 13pm", BILLING):
            self.assertIsNone(quota.parse_reset_time(text, now), text)

    def test_unknown_zone_falls_back(self):
        now = datetime(2026, 11, 22, 20, 0, tzinfo=UTC)
        reset = quota.parse_reset_time("session limit · resets 11pm (Nowhere/Fake)", now)
        self.assertEqual(reset, datetime(2026, 11, 22, 23, 0, tzinfo=UTC))


class TestSecondsToWait(unittest.TestCase):
    NOW = datetime(2026, 11, 22, 22, 0, tzinfo=UTC)

    def test_waits_until_reset_plus_margin(self):
        wait = quota.seconds_to_wait(SESSION_LIMIT, self.NOW, 10 * 3600, 3600, margin_seconds=120)
        self.assertEqual(wait, 2 * 3600 + 120)

    def test_none_when_reset_leaves_no_room_for_a_tend(self):
        # 2 h to the reset + 1 h tend > 2.5 h left.
        self.assertIsNone(quota.seconds_to_wait(SESSION_LIMIT, self.NOW, 2.5 * 3600, 3600))

    def test_none_for_billing(self):
        self.assertIsNone(quota.seconds_to_wait(BILLING, self.NOW, 10 * 3600, 3600))

    def test_none_for_limit_without_reset(self):
        self.assertIsNone(quota.seconds_to_wait("usage limit reached", self.NOW, 10 * 3600, 3600))


class TestProbe(unittest.TestCase):
    def _probe(self, result=None, exc=None):
        calls = []

        def runner(mode, prompt, cwd, **kwargs):
            calls.append((mode, prompt, cwd, kwargs))
            if exc:
                raise exc
            return result

        return quota.probe(HarnessConfig(), model="m", runner=runner), calls

    def test_ok(self):
        found, calls = self._probe(_result(True, "OK"))
        self.assertTrue(found.ok)
        mode, _prompt, cwd, kwargs = calls[0]
        self.assertIs(mode, Mode.REPORT)
        self.assertEqual(kwargs["model"], "m")
        self.assertNotIn("repo", kwargs)  # never routed
        self.assertFalse(Path(cwd).exists())  # temp dir cleaned up

    def test_billing_is_blocked(self):
        found, _ = self._probe(_result(False, BILLING))
        self.assertFalse(found.ok)
        self.assertTrue(found.blocked)
        self.assertIn("past due", found.text)

    def test_session_limit_is_blocked(self):
        found, _ = self._probe(_result(False, SESSION_LIMIT))
        self.assertTrue(found.blocked)

    def test_other_failure_is_not_blocked(self):
        found, _ = self._probe(_result(False, "", stderr="timed out after 180s"))
        self.assertFalse(found.ok)
        self.assertFalse(found.blocked)

    def test_runner_exception_is_not_blocked(self):
        found, _ = self._probe(exc=RuntimeError("boom"))
        self.assertFalse(found.ok)
        self.assertFalse(found.blocked)
        self.assertIn("boom", found.text)


if __name__ == "__main__":
    unittest.main()
