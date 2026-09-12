"""Tests for src/execution/cli.py. Providers are mocked entirely --
these tests confirm the CLI's own logic (skip/pass/fail/isolation/exit
code), not real provider behavior (covered in their own test files)."""

from datetime import datetime, timezone
from unittest import mock

from src.execution.base import HealthCheckResult
from src.execution.cli import main


class _FakeConfig:
    kalshi_enabled = False
    polymarket_us_enabled = False


def _healthy(provider_name):
    return HealthCheckResult(provider=provider_name, ok=True, detail="ok", checked_at=datetime.now(timezone.utc))


def _unhealthy(provider_name):
    return HealthCheckResult(provider=provider_name, ok=False, detail="bad creds", checked_at=datetime.now(timezone.utc))


class TestCheckConnectivity:
    def test_both_disabled_skips_both_and_exits_zero(self, capsys):
        config = _FakeConfig()
        with mock.patch("src.execution.cli.load_config", return_value=config):
            exit_code = main(["check-connectivity"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "kalshi: SKIPPED" in out
        assert "polymarket_us: SKIPPED" in out

    def test_one_enabled_and_healthy_passes_and_exits_zero(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.health_check.return_value = _healthy("kalshi")
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            exit_code = main(["check-connectivity"])
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "kalshi: PASS" in out

    def test_misconfigured_provider_fails_but_does_not_block_the_other(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        config.polymarket_us_enabled = True

        def fake_get_provider(name, cfg):
            if name == "kalshi":
                raise RuntimeError("bad private key path")
            provider = mock.Mock()
            provider.health_check.return_value = _healthy("polymarket_us")
            return provider

        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", side_effect=fake_get_provider):
            exit_code = main(["check-connectivity"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "kalshi: FAIL" in out
        assert "polymarket_us: PASS" in out

    def test_unhealthy_provider_fails_and_exits_nonzero(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.health_check.return_value = _unhealthy("kalshi")
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            exit_code = main(["check-connectivity"])
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "kalshi: FAIL" in out

    def test_never_calls_anything_but_health_check(self, capsys):
        config = _FakeConfig()
        config.kalshi_enabled = True
        fake_provider = mock.Mock()
        fake_provider.health_check.return_value = _healthy("kalshi")
        with mock.patch("src.execution.cli.load_config", return_value=config), \
             mock.patch("src.execution.cli.get_provider", return_value=fake_provider):
            main(["check-connectivity"])

        fake_provider.health_check.assert_called_once()
        fake_provider.get_balance.assert_not_called()
        fake_provider.place_order.assert_not_called()
