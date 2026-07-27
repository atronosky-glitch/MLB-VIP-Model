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
