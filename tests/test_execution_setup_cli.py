"""Tests for src/execution/setup_cli.py (setup-polymarket,
polymarket-setup-check). Every test uses a temp directory standing in
for the key-storage directory and a temp .env file -- never touches the
real ~/.prediction-market-keys or repo .env. Credential VALUES are
never asserted to be printed anywhere; only presence/absence markers
are checked.
"""

import base64
from unittest import mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.execution import setup_cli


class _FakeConfig:
    polymarket_us_api_key_id = ""
    polymarket_us_private_key_path = ""
    polymarket_us_enabled = False
    polymarket_us_live_enabled = False
    live_trading_enabled = False


def _valid_ed25519_key_text() -> str:
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.b64encode(raw).decode()


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    key_dir = tmp_path / "keys"
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING_VAR=untouched\n")
    monkeypatch.setattr(setup_cli, "_KEY_DIR", key_dir)
    monkeypatch.setattr(setup_cli, "_ENV_PATH", env_path)
    return key_dir, env_path


class TestSetupPolymarketCredentialDetection:
    def test_reports_missing_key_id_and_path(self, capsys, monkeypatch):
        monkeypatch.setattr(setup_cli, "load_config", lambda: _FakeConfig())
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        exit_code = setup_cli.setup_polymarket()
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "API key ID configured........ NO" in out
        assert "USER ACTION REQUIRED" in out

    def test_creates_key_storage_directory(self, monkeypatch, _isolated_paths):
        key_dir, _ = _isolated_paths
        monkeypatch.setattr(setup_cli, "load_config", lambda: _FakeConfig())
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        setup_cli.setup_polymarket()
        assert key_dir.is_dir()

    def test_prefills_expected_key_path_into_env_when_blank(self, monkeypatch, _isolated_paths):
        key_dir, env_path = _isolated_paths
        monkeypatch.setattr(setup_cli, "load_config", lambda: _FakeConfig())
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        setup_cli.setup_polymarket()
        content = env_path.read_text()
        assert "POLYMARKET_US_PRIVATE_KEY_PATH=" in content
        assert str(key_dir) in content
        assert "EXISTING_VAR=untouched" in content  # other lines preserved

    def test_never_prompts_or_hangs_when_not_interactive(self, capsys, monkeypatch):
        monkeypatch.setattr(setup_cli, "load_config", lambda: _FakeConfig())
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        exit_code = setup_cli.setup_polymarket()  # must not block on input()
        assert exit_code == 0

    def test_interactive_key_id_entry_is_saved_and_never_echoed(self, capsys, monkeypatch):
        monkeypatch.setattr(setup_cli, "load_config", lambda: _FakeConfig())
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt: "pk_live_abc123secretlooking")
        setup_cli.setup_polymarket()
        out = capsys.readouterr().out
        assert "pk_live_abc123secretlooking" not in out
        assert "Saved to .env as POLYMARKET_US_API_KEY_ID" in out

    def test_interactive_key_id_entry_writes_to_env(self, monkeypatch, _isolated_paths):
        _, env_path = _isolated_paths
        monkeypatch.setattr(setup_cli, "load_config", lambda: _FakeConfig())
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt: "my-key-id")
        setup_cli.setup_polymarket()
        assert "POLYMARKET_US_API_KEY_ID='my-key-id'" in env_path.read_text() \
            or "POLYMARKET_US_API_KEY_ID=my-key-id" in env_path.read_text()

    def test_eof_on_input_does_not_crash(self, capsys, monkeypatch):
        monkeypatch.setattr(setup_cli, "load_config", lambda: _FakeConfig())
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def _raise_eof(prompt):
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise_eof)
        exit_code = setup_cli.setup_polymarket()
        assert exit_code == 0


class TestSetupPolymarketKeyFileValidation:
    def test_valid_key_file_reports_found_and_valid(self, capsys, monkeypatch, _isolated_paths):
        key_dir, _ = _isolated_paths
        key_dir.mkdir(parents=True)
        key_file = key_dir / "polymarket_us.txt"
        key_file.write_text(_valid_ed25519_key_text())
        config = _FakeConfig()
        config.polymarket_us_private_key_path = str(key_file)
        monkeypatch.setattr(setup_cli, "load_config", lambda: config)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        setup_cli.setup_polymarket()
        out = capsys.readouterr().out
        assert "FOUND, readable, and correctly formatted" in out

    def test_invalid_key_file_reports_invalid_without_printing_contents(self, capsys, monkeypatch, _isolated_paths):
        key_dir, _ = _isolated_paths
        key_dir.mkdir(parents=True)
        key_file = key_dir / "polymarket_us.txt"
        key_file.write_text("not-a-valid-base64-key!!!")
        config = _FakeConfig()
        config.polymarket_us_private_key_path = str(key_file)
        monkeypatch.setattr(setup_cli, "load_config", lambda: config)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        setup_cli.setup_polymarket()
        out = capsys.readouterr().out
        assert "FOUND but INVALID" in out
        assert "not-a-valid-base64-key" not in out

    def test_missing_key_file_reports_not_found_with_exact_path(self, capsys, monkeypatch, _isolated_paths):
        key_dir, _ = _isolated_paths
        config = _FakeConfig()
        config.polymarket_us_private_key_path = str(key_dir / "polymarket_us.txt")
        monkeypatch.setattr(setup_cli, "load_config", lambda: config)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        setup_cli.setup_polymarket()
        out = capsys.readouterr().out
        assert "NOT FOUND" in out
        assert str(key_dir / "polymarket_us.txt") in out


class TestPolymarketSetupCheck:
    def test_no_credentials_reports_not_ready(self, capsys, monkeypatch):
        monkeypatch.setattr(setup_cli, "load_config", lambda: _FakeConfig())
        exit_code = setup_cli.polymarket_setup_check()
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "OVERALL: NOT READY" in out
        assert "setup-polymarket" in out

    def test_credentials_configured_but_provider_disabled(self, capsys, monkeypatch, _isolated_paths):
        key_dir, _ = _isolated_paths
        key_dir.mkdir(parents=True)
        key_file = key_dir / "polymarket_us.txt"
        key_file.write_text(_valid_ed25519_key_text())
        config = _FakeConfig()
        config.polymarket_us_api_key_id = "key-id"
        config.polymarket_us_private_key_path = str(key_file)
        config.polymarket_us_enabled = False
        monkeypatch.setattr(setup_cli, "load_config", lambda: config)
        exit_code = setup_cli.polymarket_setup_check()
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "POLYMARKET_US_ENABLED=false" in out

    def test_full_pass_reports_ready(self, capsys, monkeypatch, _isolated_paths):
        key_dir, _ = _isolated_paths
        key_dir.mkdir(parents=True)
        key_file = key_dir / "polymarket_us.txt"
        key_file.write_text(_valid_ed25519_key_text())
        config = _FakeConfig()
        config.polymarket_us_api_key_id = "key-id"
        config.polymarket_us_private_key_path = str(key_file)
        config.polymarket_us_enabled = True
        monkeypatch.setattr(setup_cli, "load_config", lambda: config)

        provider = mock.Mock()
        provider.health_check.return_value = mock.Mock(ok=True, detail="ok")
        provider.get_balance.return_value = mock.Mock()
        market = mock.Mock(id="m1")
        provider.get_markets.return_value = [market]
        provider.get_orderbook.return_value = mock.Mock()
        provider.normalize_orderbook.return_value = mock.Mock()
        provider.parse_game_event.return_value = mock.Mock()
        provider.get_recent_orders.return_value = []
        monkeypatch.setattr(setup_cli, "get_provider", lambda name, cfg: provider)

        exit_code = setup_cli.polymarket_setup_check()
        out = capsys.readouterr().out
        assert exit_code == 0
        assert "OVERALL: READY" in out
        provider.place_order.assert_not_called()

    def test_auth_failure_reports_not_ready(self, capsys, monkeypatch, _isolated_paths):
        key_dir, _ = _isolated_paths
        key_dir.mkdir(parents=True)
        key_file = key_dir / "polymarket_us.txt"
        key_file.write_text(_valid_ed25519_key_text())
        config = _FakeConfig()
        config.polymarket_us_api_key_id = "key-id"
        config.polymarket_us_private_key_path = str(key_file)
        config.polymarket_us_enabled = True
        monkeypatch.setattr(setup_cli, "load_config", lambda: config)

        provider = mock.Mock()
        provider.health_check.return_value = mock.Mock(ok=False, detail="401")
        provider.get_balance.side_effect = Exception("unauthorized")
        provider.get_markets.side_effect = Exception("unauthorized")
        monkeypatch.setattr(setup_cli, "get_provider", lambda name, cfg: provider)

        exit_code = setup_cli.polymarket_setup_check()
        out = capsys.readouterr().out
        assert exit_code == 1
        assert "OVERALL: NOT READY" in out
        assert "USER ACTION REQUIRED" in out

    def test_never_calls_place_or_cancel_order(self, monkeypatch, _isolated_paths):
        key_dir, _ = _isolated_paths
        key_dir.mkdir(parents=True)
        key_file = key_dir / "polymarket_us.txt"
        key_file.write_text(_valid_ed25519_key_text())
        config = _FakeConfig()
        config.polymarket_us_api_key_id = "key-id"
        config.polymarket_us_private_key_path = str(key_file)
        config.polymarket_us_enabled = True
        monkeypatch.setattr(setup_cli, "load_config", lambda: config)

        provider = mock.Mock(spec=[
            "health_check", "get_balance", "get_markets", "get_orderbook",
            "normalize_orderbook", "parse_game_event", "get_recent_orders",
        ])
        provider.health_check.return_value = mock.Mock(ok=True, detail="ok")
        provider.get_markets.return_value = []
        monkeypatch.setattr(setup_cli, "get_provider", lambda name, cfg: provider)

        setup_cli.polymarket_setup_check()
        assert not hasattr(provider, "place_order") or not provider.place_order.called


class TestCliDispatch:
    def test_setup_polymarket_registered_in_main(self, monkeypatch):
        from src.execution.cli import main
        called = {}

        def _fake():
            called["ran"] = True
            return 0

        monkeypatch.setattr("src.execution.setup_cli.setup_polymarket", _fake)
        exit_code = main(["setup-polymarket"])
        assert exit_code == 0
        assert called.get("ran") is True

    def test_polymarket_setup_check_registered_in_main(self, monkeypatch):
        from src.execution.cli import main
        called = {}

        def _fake():
            called["ran"] = True
            return 0

        monkeypatch.setattr("src.execution.setup_cli.polymarket_setup_check", _fake)
        exit_code = main(["polymarket-setup-check"])
        assert exit_code == 0
        assert called.get("ran") is True
