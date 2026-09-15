"""Tests for src/discord_delivery.py's run_webhook_test()/`test-webhooks`
CLI command (2026-09-15) -- verifies each configured Discord webhook is
reachable without touching dedup state or sending real alert content."""

from unittest import mock

from src.discord_delivery import main
from src.discord_delivery import test_webhooks as run_webhook_test


class _FakeConfig:
    discord_webhook_urls = ""
    discord_webhook_urls_arb_middle = ""
    discord_webhook_urls_middle = ""


class TestTestWebhooks:
    def test_none_configured_reports_nothing_configured(self):
        result = run_webhook_test(config=_FakeConfig())
        assert result["any_configured"] is False
        for info in result["channels"].values():
            assert info["configured"] is False

    def test_sends_one_message_per_configured_url(self):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1,https://discord.com/api/webhooks/ev2"
        with mock.patch("src.discord_delivery.send_webhook_message", return_value=True) as mocked:
            result = run_webhook_test(config=config)
        assert mocked.call_count == 2
        assert result["channels"]["ev_picks"] == {
            "configured": True, "urls_tested": 2, "passed": 2, "failed": 0,
        }

    def test_failed_send_is_reported_per_url(self):
        config = _FakeConfig()
        config.discord_webhook_urls_middle = "https://discord.com/api/webhooks/mid"
        with mock.patch("src.discord_delivery.send_webhook_message", return_value=False):
            result = run_webhook_test(config=config)
        assert result["channels"]["middles"] == {
            "configured": True, "urls_tested": 1, "passed": 0, "failed": 1,
        }

    def test_never_touches_dedup_or_real_content(self):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        with mock.patch("src.discord_delivery.send_webhook_message", return_value=True) as mocked, \
             mock.patch("database.db_manager.mark_recommendations_alerted") as mocked_mark:
            run_webhook_test(config=config)
        mocked_mark.assert_not_called()
        sent_text = mocked.call_args[0][1]
        assert "test" in sent_text.lower()
        assert "STRONG_EDGE" not in sent_text  # never real recommendation content

    def test_independent_channels_report_independently(self):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        config.discord_webhook_urls_arb_middle = "https://discord.com/api/webhooks/arb1"
        with mock.patch("src.discord_delivery.send_webhook_message", return_value=True):
            result = run_webhook_test(config=config)
        assert result["channels"]["ev_picks"]["configured"] is True
        assert result["channels"]["arbitrage"]["configured"] is True
        assert result["channels"]["middles"]["configured"] is False


class TestCliDispatch:
    def test_no_webhooks_configured_exits_nonzero(self, capsys):
        with mock.patch("src.production_config.load_config", return_value=_FakeConfig()):
            exit_code = main(["test-webhooks"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "no discord webhooks configured" in out.lower()

    def test_all_pass_exits_zero(self, capsys):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        with mock.patch("src.production_config.load_config", return_value=config), \
             mock.patch("src.discord_delivery.send_webhook_message", return_value=True):
            exit_code = main(["test-webhooks"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "PASS" in out

    def test_a_failure_exits_nonzero(self, capsys):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        with mock.patch("src.production_config.load_config", return_value=config), \
             mock.patch("src.discord_delivery.send_webhook_message", return_value=False):
            exit_code = main(["test-webhooks"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "FAIL" in out
