"""Tests for Phase 10 Part G: Production Configuration."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest

from src.production_config import (
    ProductionConfig,
    load_config,
    save_config,
    create_env_example,
    SECRET_FIELDS,
)


class TestProductionConfig:

    def test_defaults(self):
        cfg = ProductionConfig()
        assert cfg.api_key == ""
        assert cfg.log_level == "INFO"
        assert cfg.log_format == "human"
        assert cfg.backup_retention_count == 7
        assert cfg.freshness_threshold_seconds == 3600

    def test_redacted_masks_secrets(self):
        cfg = ProductionConfig(api_key="sk_test_12345678")
        redacted = cfg.redacted()
        assert redacted["api_key"] == "***REDACTED***"
        assert redacted["log_level"] == "INFO"

    def test_redacted_empty_key_not_masked(self):
        cfg = ProductionConfig()
        redacted = cfg.redacted()
        assert redacted["api_key"] == ""

    def test_validate_missing_api_key(self):
        cfg = ProductionConfig()
        errors = cfg.validate()
        assert any("api_key" in e for e in errors)

    def test_validate_valid_config(self):
        cfg = ProductionConfig(api_key="sk_test_12345678")
        errors = cfg.validate()
        assert errors == []

    def test_validate_negative_ev(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", min_ev_pct=-1.0)
        errors = cfg.validate()
        assert any("min_ev_pct" in e for e in errors)

    def test_validate_bad_log_level(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", log_level="BANANA")
        errors = cfg.validate()
        assert any("log_level" in e for e in errors)

    def test_validate_bad_timezone(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", timezone="Not/A/Zone")
        errors = cfg.validate()
        assert any("timezone" in e for e in errors)

    def test_validate_confidence_out_of_range(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", min_confidence_score=150)
        errors = cfg.validate()
        assert any("confidence" in e for e in errors)

    def test_validate_confidence_range_valid(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", min_confidence_score=50.0)
        errors = cfg.validate()
        assert errors == []

    def test_validate_negative_freshness(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", freshness_threshold_seconds=-1)
        errors = cfg.validate()
        assert any("freshness" in e for e in errors)

    def test_validate_bad_log_format(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", log_format="xml")
        errors = cfg.validate()
        assert any("log_format" in e for e in errors)

    def test_validate_negative_backup_retention(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", backup_retention_count=-1)
        errors = cfg.validate()
        assert any("backup_retention" in e for e in errors)

    def test_load_config_defaults(self, monkeypatch):
        monkeypatch.delenv("SPORTSODDS_API_KEY", raising=False)
        monkeypatch.setattr("src.production_config.load_dotenv", lambda: None)
        cfg = load_config()
        assert cfg.api_key == ""
        assert cfg.timezone == "America/New_York"

    def test_load_config_from_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SPORTSODDS_API_KEY", raising=False)
        monkeypatch.setattr("src.production_config.load_dotenv", lambda: None)
        config_file = tmp_path / "test_config.json"
        config_file.write_text(json.dumps({"api_key": "from_file", "log_level": "DEBUG"}))
        cfg = load_config(config_file)
        assert cfg.api_key == "from_file"
        assert cfg.log_level == "DEBUG"

    def test_load_config_env_override(self, tmp_path, monkeypatch):
        config_file = tmp_path / "test_config.json"
        config_file.write_text(json.dumps({"api_key": "from_file"}))
        monkeypatch.setenv("SPORTSODDS_API_KEY", "from_env")
        cfg = load_config(config_file)
        assert cfg.api_key == "from_env"

    def test_load_config_env_only(self, monkeypatch):
        monkeypatch.setenv("SPORTSODDS_API_KEY", "env_key")
        monkeypatch.setenv("MLB_LOG_LEVEL", "DEBUG")
        cfg = load_config()
        assert cfg.api_key == "env_key"
        assert cfg.log_level == "DEBUG"

    def test_load_config_env_type_coercion(self, monkeypatch):
        monkeypatch.setenv("MLB_MIN_CONFIDENCE", "55.5")
        monkeypatch.setenv("MLB_BACKUP_RETENTION", "14")
        monkeypatch.setenv("MLB_BACKUP_COMPRESSION", "true")
        cfg = load_config()
        assert cfg.min_confidence_score == 55.5
        assert cfg.backup_retention_count == 14
        assert cfg.backup_compression is True

    def test_save_config_excludes_secrets(self, tmp_path):
        config_file = tmp_path / "saved.json"
        save_config(ProductionConfig(api_key="super_secret"), config_file)
        saved = json.loads(config_file.read_text())
        assert saved["api_key"] == ""

    def test_env_example_content(self):
        content = create_env_example()
        assert "SPORTSODDS_API_KEY" in content
        assert "MLB_DISCORD_WEBHOOKS" in content

    def test_load_config_missing_file(self, monkeypatch):
        monkeypatch.delenv("SPORTSODDS_API_KEY", raising=False)
        monkeypatch.setattr("src.production_config.load_dotenv", lambda: None)
        cfg = load_config("/nonexistent/path/config.json")
        assert cfg.api_key == ""

    def test_secret_fields_is_frozen(self):
        assert isinstance(SECRET_FIELDS, frozenset)
        assert "api_key" in SECRET_FIELDS


class TestExecutionLayerConfig:
    """Stage 1 (2026-09-12): Kalshi/Polymarket US read-only provider
    config -- no order placement exists yet, both default disabled."""

    def test_defaults(self):
        cfg = ProductionConfig()
        assert cfg.kalshi_enabled is False
        assert cfg.kalshi_env == "demo"
        assert cfg.kalshi_api_key_id == ""
        assert cfg.kalshi_private_key_path == ""
        assert cfg.polymarket_us_enabled is False
        assert cfg.polymarket_us_api_key_id == ""
        assert cfg.polymarket_us_private_key_path == ""

    def test_secret_fields_include_credentials(self):
        for field_name in (
            "kalshi_api_key_id", "kalshi_private_key_path",
            "polymarket_us_api_key_id", "polymarket_us_private_key_path",
        ):
            assert field_name in SECRET_FIELDS

    def test_redacted_masks_credential_fields(self):
        cfg = ProductionConfig(
            kalshi_api_key_id="real-key-id", kalshi_private_key_path="/home/me/kalshi.pem",
            polymarket_us_api_key_id="real-pm-id", polymarket_us_private_key_path="/home/me/pm.txt",
        )
        redacted = cfg.redacted()
        assert redacted["kalshi_api_key_id"] == "***REDACTED***"
        assert redacted["kalshi_private_key_path"] == "***REDACTED***"
        assert redacted["polymarket_us_api_key_id"] == "***REDACTED***"
        assert redacted["polymarket_us_private_key_path"] == "***REDACTED***"

    def test_env_override_and_bool_coercion(self, monkeypatch):
        monkeypatch.setenv("KALSHI_ENABLED", "true")
        monkeypatch.setenv("KALSHI_ENV", "production")
        monkeypatch.setenv("KALSHI_API_KEY_ID", "abc123")
        monkeypatch.setenv("POLYMARKET_US_ENABLED", "true")
        cfg = load_config()
        assert cfg.kalshi_enabled is True
        assert cfg.kalshi_env == "production"
        assert cfg.kalshi_api_key_id == "abc123"
        assert cfg.polymarket_us_enabled is True

    def test_validate_rejects_bad_kalshi_env(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", kalshi_env="staging")
        errors = cfg.validate()
        assert any("kalshi_env" in e for e in errors)

    def test_validate_rejects_kalshi_enabled_without_credentials(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", kalshi_enabled=True)
        errors = cfg.validate()
        assert any("kalshi_enabled" in e for e in errors)

    def test_validate_passes_kalshi_enabled_with_credentials(self):
        cfg = ProductionConfig(
            api_key="sk_test_12345678", kalshi_enabled=True,
            kalshi_api_key_id="id", kalshi_private_key_path="/path/key.pem",
        )
        errors = cfg.validate()
        assert not any("kalshi" in e for e in errors)

    def test_validate_rejects_polymarket_us_enabled_without_credentials(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", polymarket_us_enabled=True)
        errors = cfg.validate()
        assert any("polymarket_us_enabled" in e for e in errors)

    def test_validate_passes_polymarket_us_enabled_with_credentials(self):
        cfg = ProductionConfig(
            api_key="sk_test_12345678", polymarket_us_enabled=True,
            polymarket_us_api_key_id="id", polymarket_us_private_key_path="/path/key.txt",
        )
        errors = cfg.validate()
        assert not any("polymarket_us" in e for e in errors)

    def test_env_example_mentions_both_providers(self):
        content = create_env_example()
        assert "KALSHI_ENABLED" in content
        assert "KALSHI_PRIVATE_KEY_PATH" in content
        assert "POLYMARKET_US_ENABLED" in content
        assert "POLYMARKET_US_PRIVATE_KEY_PATH" in content


class TestMarketMatchingConfig:
    """Stage 2 (2026-09-12): min_market_match_confidence gates
    src/execution/matching.py's find_best_match()."""

    def test_default(self):
        cfg = ProductionConfig()
        assert cfg.min_market_match_confidence == 0.98

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MLB_MIN_MARKET_MATCH_CONFIDENCE", "0.9")
        cfg = load_config()
        assert cfg.min_market_match_confidence == 0.9

    def test_validate_rejects_out_of_range_high(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", min_market_match_confidence=1.5)
        errors = cfg.validate()
        assert any("min_market_match_confidence" in e for e in errors)

    def test_validate_rejects_out_of_range_low(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", min_market_match_confidence=-0.1)
        errors = cfg.validate()
        assert any("min_market_match_confidence" in e for e in errors)

    def test_validate_accepts_boundary_values(self):
        for value in (0.0, 1.0, 0.98):
            cfg = ProductionConfig(api_key="sk_test_12345678", min_market_match_confidence=value)
            errors = cfg.validate()
            assert not any("min_market_match_confidence" in e for e in errors)

    def test_env_example_mentions_it(self):
        content = create_env_example()
        assert "MLB_MIN_MARKET_MATCH_CONFIDENCE" in content


class TestExecutionAnalysisConfig:
    """Stage 2B (2026-09-12): pricing/EV analysis config -- no order
    placement exists yet."""

    def test_defaults(self):
        cfg = ProductionConfig()
        assert cfg.execution_analysis_stake_usd == 10.0
        assert cfg.min_raw_ev_pct == 2.0
        assert cfg.min_net_ev_pct == 1.0
        assert cfg.max_spread_pct == 0.10
        assert cfg.max_slippage_pct == 0.05
        assert cfg.min_available_liquidity_usd == 50.0
        assert cfg.max_market_data_age_seconds == 30

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("MLB_EXECUTION_ANALYSIS_STAKE_USD", "25.0")
        monkeypatch.setenv("MLB_MIN_NET_EV_PCT", "3.5")
        monkeypatch.setenv("MLB_MAX_MARKET_DATA_AGE_SECONDS", "60")
        cfg = load_config()
        assert cfg.execution_analysis_stake_usd == 25.0
        assert cfg.min_net_ev_pct == 3.5
        assert cfg.max_market_data_age_seconds == 60

    def test_validate_rejects_non_positive_stake(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", execution_analysis_stake_usd=0)
        errors = cfg.validate()
        assert any("execution_analysis_stake_usd" in e for e in errors)

    def test_validate_rejects_negative_min_ev(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", min_raw_ev_pct=-1.0)
        errors = cfg.validate()
        assert any("min_raw_ev_pct" in e for e in errors)

    def test_validate_rejects_out_of_range_spread_pct(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", max_spread_pct=1.5)
        errors = cfg.validate()
        assert any("max_spread_pct" in e for e in errors)

    def test_validate_rejects_non_positive_max_age(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", max_market_data_age_seconds=0)
        errors = cfg.validate()
        assert any("max_market_data_age_seconds" in e for e in errors)

    def test_env_example_mentions_all_seven_fields(self):
        content = create_env_example()
        for var in (
            "MLB_EXECUTION_ANALYSIS_STAKE_USD", "MLB_MIN_RAW_EV_PCT", "MLB_MIN_NET_EV_PCT",
            "MLB_MAX_SPREAD_PCT", "MLB_MAX_SLIPPAGE_PCT", "MLB_MIN_AVAILABLE_LIQUIDITY_USD",
            "MLB_MAX_MARKET_DATA_AGE_SECONDS",
        ):
            assert var in content


class TestExecutionAllowedRecStatuses:
    """The single canonical source for which rec_status values the
    execution layer evaluates -- deliberately repeating
    src/discord_delivery.py's fix (STRONG_EDGE/POSITIVE_EDGE/
    STRONG_PRICE_OUTLIER/PRICE_OUTLIER) rather than its original bug
    ('BET'/'LEAN', which never existed in this schema)."""

    def test_default_matches_the_real_production_style_values(self):
        cfg = ProductionConfig()
        statuses = cfg.execution_allowed_rec_statuses_list()
        assert statuses == ("STRONG_EDGE", "POSITIVE_EDGE", "STRONG_PRICE_OUTLIER", "PRICE_OUTLIER")

    def test_does_not_include_bet_or_lean(self):
        cfg = ProductionConfig()
        statuses = cfg.execution_allowed_rec_statuses_list()
        assert "BET" not in statuses
        assert "LEAN" not in statuses

    def test_does_not_include_marginal_or_no_edge_tiers_by_default(self):
        cfg = ProductionConfig()
        statuses = cfg.execution_allowed_rec_statuses_list()
        assert "MARGINAL_EDGE" not in statuses
        assert "NO_EDGE" not in statuses
        assert "MARGINAL_PRICE_OUTLIER" not in statuses

    def test_env_override_can_expand_the_set(self, monkeypatch):
        monkeypatch.setenv("MLB_EXECUTION_ALLOWED_REC_STATUSES", "STRONG_EDGE,MARGINAL_EDGE")
        cfg = load_config()
        assert cfg.execution_allowed_rec_statuses_list() == ("STRONG_EDGE", "MARGINAL_EDGE")

    def test_validate_rejects_an_empty_list(self):
        cfg = ProductionConfig(api_key="sk_test_12345678", execution_allowed_rec_statuses="")
        errors = cfg.validate()
        assert any("execution_allowed_rec_statuses" in e for e in errors)

    def test_env_example_mentions_it(self):
        content = create_env_example()
        assert "MLB_EXECUTION_ALLOWED_REC_STATUSES" in content
