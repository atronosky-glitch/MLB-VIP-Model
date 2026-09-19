"""Tests for src/discord_delivery.py's run_webhook_test()/`test-webhooks`
CLI command (2026-09-15) -- verifies each configured Discord webhook is
reachable without touching dedup state or sending real alert content."""

from unittest import mock

from src.discord_delivery import main
from src.discord_delivery import test_mlb_discord_connection as run_mlb_discord_test
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


class TestMlbDiscordConnection:
    """Tests for src.discord_delivery.test_mlb_discord_connection() -- the 2026-09-19 operator
    request for a connectivity check against MLB_DISCORD_WEBHOOKS
    specifically, separate from the general test_webhooks() above."""

    def test_not_configured_reports_zero_urls(self):
        result = run_mlb_discord_test(config=_FakeConfig())
        assert result == {
            "configured": False, "urls_tested": 0, "passed": 0, "failed": 0, "response_statuses": [],
        }

    def test_single_url_success(self):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        with mock.patch("src.discord_delivery.send_webhook_message", return_value=True) as mocked:
            result = run_mlb_discord_test(config=config)
        assert result == {"configured": True, "urls_tested": 1, "passed": 1, "failed": 0}
        mocked.assert_called_once_with("https://discord.com/api/webhooks/ev1", "MLB Discord connection test")

    def test_multiple_urls_mixed_results(self):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1,https://discord.com/api/webhooks/ev2"
        with mock.patch("src.discord_delivery.send_webhook_message", side_effect=[True, False]):
            result = run_mlb_discord_test(config=config)
        assert result == {"configured": True, "urls_tested": 2, "passed": 1, "failed": 1}

    def test_never_returns_or_logs_the_webhook_url(self, caplog):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1/secrettoken"
        with mock.patch("src.discord_delivery.send_webhook_message", return_value=True):
            result = run_mlb_discord_test(config=config)
        assert "secrettoken" not in str(result)
        for record in caplog.records:
            assert "secrettoken" not in record.getMessage()
            assert "https://discord.com/api/webhooks" not in record.getMessage()

    def test_ignores_other_channels_arb_and_middle(self):
        """Only MLB_DISCORD_WEBHOOKS (discord_webhook_urls) matters here --
        this is the whole point of a test that answers one narrow
        question, not a general all-channels check like test_webhooks()."""
        config = _FakeConfig()
        config.discord_webhook_urls_arb_middle = "https://discord.com/api/webhooks/arb1"
        config.discord_webhook_urls_middle = "https://discord.com/api/webhooks/mid1"
        result = run_mlb_discord_test(config=config)
        assert result["configured"] is False

    def test_loads_real_production_config_when_none_given(self):
        with mock.patch("src.production_config.load_config", return_value=_FakeConfig()) as mocked_load:
            result = run_mlb_discord_test()
        mocked_load.assert_called_once()
        assert result["configured"] is False


class TestMlbDiscordConnectionCliDispatch:
    def test_not_configured_prints_clear_message_and_exits_nonzero(self, capsys):
        with mock.patch("src.production_config.load_config", return_value=_FakeConfig()):
            exit_code = main(["test-mlb"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "MLB_DISCORD_WEBHOOKS is not set (empty)" in out

    def test_all_pass_prints_pass_and_exits_zero(self, capsys):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        with mock.patch("src.production_config.load_config", return_value=config), \
             mock.patch("src.discord_delivery.send_webhook_message", return_value=True):
            exit_code = main(["test-mlb"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "PASS" in out
        assert "1/1" in out

    def test_a_failure_prints_fail_and_exits_nonzero(self, capsys):
        config = _FakeConfig()
        config.discord_webhook_urls = "https://discord.com/api/webhooks/ev1"
        with mock.patch("src.production_config.load_config", return_value=config), \
             mock.patch("src.discord_delivery.send_webhook_message", return_value=False):
            exit_code = main(["test-mlb"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "FAIL" in out
