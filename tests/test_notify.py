"""DiscordNotifier and the NullNotifier/CompositeNotifier no-op paths.
`urllib.request.urlopen` is always mocked here — this suite must never make
a real network call."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from gardener.notify import (
    DISCORD_COLORS,
    UNKNOWN_DEVICE_NAME,
    CompositeNotifier,
    DiscordNotifier,
    Level,
    NullNotifier,
    default_notifier,
    load_device_name,
    load_webhook_url,
)


class TestNullNotifier(unittest.TestCase):
    def test_notify_is_a_no_op_and_returns_none(self):
        self.assertIsNone(NullNotifier().notify("title", "message", Level.ERROR))


class TestCompositeNotifier(unittest.TestCase):
    def test_fans_out_to_every_registered_notifier(self):
        a, b = MagicMock(), MagicMock()
        CompositeNotifier([a, b]).notify("t", "m", Level.WARNING)
        a.notify.assert_called_once_with("t", "m", Level.WARNING)
        b.notify.assert_called_once_with("t", "m", Level.WARNING)

    def test_one_notifier_raising_does_not_stop_the_others(self):
        broken = MagicMock()
        broken.notify.side_effect = RuntimeError("boom")
        healthy = MagicMock()
        with redirect_stderr(io.StringIO()):
            CompositeNotifier([broken, healthy]).notify("t", "m")
        healthy.notify.assert_called_once()

    def test_empty_list_is_a_safe_no_op(self):
        CompositeNotifier([]).notify("t", "m")  # must not raise


class TestLoadWebhookUrl(unittest.TestCase):
    def test_env_var_wins(self):
        with patch.dict("os.environ", {"GARDENER_DISCORD_WEBHOOK_URL": "https://example.com/hook"}):
            self.assertEqual(load_webhook_url(), "https://example.com/hook")

    def test_env_var_blank_falls_through_to_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "notify.env"
            cfg.write_text("DISCORD_WEBHOOK_URL=https://example.com/from-file\n")
            with patch.dict("os.environ", {"GARDENER_DISCORD_WEBHOOK_URL": ""}):
                self.assertEqual(load_webhook_url(cfg), "https://example.com/from-file")

    def test_reads_from_config_file_when_no_env_var(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "notify.env"
            cfg.write_text('# comment\nDISCORD_WEBHOOK_URL="https://example.com/quoted"\n')
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("GARDENER_DISCORD_WEBHOOK_URL", None)
                self.assertEqual(load_webhook_url(cfg), "https://example.com/quoted")

    def test_missing_file_and_no_env_var_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "does-not-exist.env"
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("GARDENER_DISCORD_WEBHOOK_URL", None)
                self.assertIsNone(load_webhook_url(cfg))

    def test_file_without_the_key_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "notify.env"
            cfg.write_text("SOME_OTHER_VAR=x\n")
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("GARDENER_DISCORD_WEBHOOK_URL", None)
                self.assertIsNone(load_webhook_url(cfg))


class TestDiscordNotifier(unittest.TestCase):
    """`DiscordNotifier` resolves its device name at construction, and that
    resolution falls through to reading `$GARDENER_STATE_DIR/notify.env`.
    Without the isolation below, every test in this class would read the
    operator's real notify.env on a machine where gardener is deployed —
    `TestNullNotifier`'s and `TestDefaultNotifier`'s siblings already scope
    `GARDENER_STATE_DIR` to a tmp dir for the same reason."""

    def setUp(self):
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        self.state_dir = Path(state_dir.name)

        env = patch.dict("os.environ", {"GARDENER_STATE_DIR": str(self.state_dir)}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        # Popped inside the patch.dict above, so it is restored on stop.
        os.environ.pop("GARDENER_DEVICE_NAME", None)

    @patch("gardener.notify.urllib.request.urlopen")
    def test_device_is_read_from_the_scoped_state_dir_not_the_real_one(self, mock_urlopen):
        """Guards the isolation in setUp above. Written so that it fails if
        `GARDENER_STATE_DIR` is not actually being redirected: the expected
        value exists only in this test's tmp dir, so a notifier that reads
        the real state dir cannot produce it."""
        (self.state_dir / "notify.env").write_text("GARDENER_DEVICE_NAME=scoped-tmp-device\n")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/x/y")
        with redirect_stderr(io.StringIO()):
            notifier.notify("t", "m")

        embed = json.loads(mock_urlopen.call_args[0][0].data)["embeds"][0]
        self.assertEqual(embed["footer"], {"text": "scoped-tmp-device"})

    def test_not_configured_is_a_silent_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"GARDENER_STATE_DIR": td}, clear=False):
                os.environ.pop("GARDENER_DISCORD_WEBHOOK_URL", None)
                notifier = DiscordNotifier(webhook_url=None)
                self.assertFalse(notifier.configured)
                with patch("gardener.notify.urllib.request.urlopen") as mock_urlopen:
                    with redirect_stderr(io.StringIO()) as err:
                        notifier.notify("title", "message")
                    mock_urlopen.assert_not_called()
                self.assertIn("skipping alert", err.getvalue())

    @patch("gardener.notify.urllib.request.urlopen")
    def test_configured_webhook_posts_successfully(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/x/y")
        self.assertTrue(notifier.configured)
        with redirect_stderr(io.StringIO()):
            notifier.notify("gardener report: a/b", "3 gaps found", Level.INFO)

        mock_urlopen.assert_called_once()
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://discord.com/api/webhooks/x/y")
        self.assertEqual(request.get_header("Content-type"), "application/json")

    @patch("gardener.notify.urllib.request.urlopen")
    def test_network_error_does_not_raise(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/x/y")
        with redirect_stderr(io.StringIO()) as err:
            notifier.notify("title", "message", Level.ERROR)  # must not raise
        self.assertIn("FAILED to send", err.getvalue())

    @patch("gardener.notify.urllib.request.urlopen")
    def test_http_error_does_not_raise(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://discord.com/api/webhooks/x/y", code=404, msg="Not Found", hdrs=None, fp=None
        )
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/x/y")
        with redirect_stderr(io.StringIO()):
            notifier.notify("title", "message")  # must not raise

    @patch("gardener.notify.urllib.request.urlopen")
    def test_payload_shape_and_color_per_level(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        for level in Level:
            with self.subTest(level=level):
                notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/x/y")
                with redirect_stderr(io.StringIO()):
                    notifier.notify("a title", "a message", level)
                request = mock_urlopen.call_args[0][0]
                body = json.loads(request.data)
                self.assertEqual(body["username"], "gardener")
                embed = body["embeds"][0]
                self.assertEqual(embed["title"], "a title")
                self.assertEqual(embed["description"], "a message")
                self.assertEqual(embed["color"], DISCORD_COLORS[level])

    def test_colors_are_distinct_per_level(self):
        self.assertEqual(len(set(DISCORD_COLORS.values())), len(DISCORD_COLORS))

    @patch("gardener.notify.urllib.request.urlopen")
    def test_every_level_carries_the_device_footer(self, mock_urlopen):
        """Provenance is applied at presentation time, so it must appear on
        every alert regardless of severity — including any future alert,
        which is the whole point of putting it here rather than at the call
        sites."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        for level in Level:
            with self.subTest(level=level):
                notifier = DiscordNotifier(
                    webhook_url="https://discord.com/api/webhooks/x/y",
                    device="test-box",
                )
                with redirect_stderr(io.StringIO()):
                    notifier.notify("a title", "a message", level)
                embed = json.loads(mock_urlopen.call_args[0][0].data)["embeds"][0]
                self.assertEqual(embed["footer"], {"text": "test-box"})
                # The footer must not have crowded out what was already there.
                self.assertEqual(embed["title"], "a title")
                self.assertEqual(embed["color"], DISCORD_COLORS[level])

    @patch("gardener.notify.urllib.request.urlopen")
    def test_device_is_resolved_once_not_per_alert(self, mock_urlopen):
        """`load_device_name` reads a file; a loop that alerts once per repo
        must not re-read it on every notification."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with patch("gardener.notify.load_device_name", return_value="box") as mock_load:
            notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/x/y")
            with redirect_stderr(io.StringIO()):
                notifier.notify("t", "m")
                notifier.notify("t", "m")
                notifier.notify("t", "m")
        mock_load.assert_called_once()


class TestLoadDeviceName(unittest.TestCase):
    """Mirrors TestLoadWebhookUrl's precedence cases — the two resolvers
    deliberately share the same env-var-then-notify.env shape."""

    def test_env_var_wins(self):
        with patch.dict("os.environ", {"GARDENER_DEVICE_NAME": "phone"}):
            self.assertEqual(load_device_name(), "phone")

    def test_env_var_is_stripped(self):
        with patch.dict("os.environ", {"GARDENER_DEVICE_NAME": "  phone  "}):
            self.assertEqual(load_device_name(), "phone")

    def test_blank_env_var_falls_through_to_file(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "notify.env"
            cfg.write_text("GARDENER_DEVICE_NAME=from-file\n")
            with patch.dict("os.environ", {"GARDENER_DEVICE_NAME": "   "}):
                self.assertEqual(load_device_name(cfg), "from-file")

    def test_reads_from_config_file_when_no_env_var(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "notify.env"
            cfg.write_text("DISCORD_WEBHOOK_URL=https://example.com/hook\nGARDENER_DEVICE_NAME=wsl-box\n")
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("GARDENER_DEVICE_NAME", None)
                self.assertEqual(load_device_name(cfg), "wsl-box")

    def test_falls_back_to_hostname_when_nothing_configured(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "notify.env"
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("GARDENER_DEVICE_NAME", None)
                with patch("gardener.notify.socket.gethostname", return_value="real-hostname"):
                    self.assertEqual(load_device_name(missing), "real-hostname")

    def test_blank_hostname_degrades_to_the_unknown_sentinel(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "notify.env"
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("GARDENER_DEVICE_NAME", None)
                with patch("gardener.notify.socket.gethostname", return_value="  "):
                    self.assertEqual(load_device_name(missing), UNKNOWN_DEVICE_NAME)

    def test_unresolvable_hostname_does_not_raise(self):
        """This runs on the alerting path, which must never break the run
        it is reporting on."""
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "notify.env"
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("GARDENER_DEVICE_NAME", None)
                with patch("gardener.notify.socket.gethostname", side_effect=OSError("no hostname")):
                    with redirect_stderr(io.StringIO()) as err:
                        self.assertEqual(load_device_name(missing), UNKNOWN_DEVICE_NAME)
                    self.assertIn("could not resolve hostname", err.getvalue())

    def test_unreadable_config_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "notify.env"
            cfg.write_text("GARDENER_DEVICE_NAME=unreadable\n")
            with patch.dict("os.environ", {}, clear=False):
                os.environ.pop("GARDENER_DEVICE_NAME", None)
                with patch("gardener.notify._parse_env_style_file", side_effect=OSError("denied")):
                    with patch("gardener.notify.socket.gethostname", return_value="fallback-host"):
                        with redirect_stderr(io.StringIO()) as err:
                            self.assertEqual(load_device_name(cfg), "fallback-host")
                        self.assertIn("could not read", err.getvalue())


class TestDefaultNotifier(unittest.TestCase):
    def test_returns_null_notifier_when_unconfigured(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.dict("os.environ", {"GARDENER_STATE_DIR": td}, clear=False):
                os.environ.pop("GARDENER_DISCORD_WEBHOOK_URL", None)
                self.assertIsInstance(default_notifier(), NullNotifier)

    def test_returns_discord_notifier_when_webhook_env_var_set(self):
        with patch.dict("os.environ", {"GARDENER_DISCORD_WEBHOOK_URL": "https://example.com/hook"}):
            self.assertIsInstance(default_notifier(), DiscordNotifier)


if __name__ == "__main__":
    unittest.main()
