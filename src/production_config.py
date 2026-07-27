"""Production configuration and secrets management.

Centralizes all production settings with environment variable support,
optional local config file, safe defaults, secret redaction, and validation.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


# ── Secret field names (redacted in logs/display) ──────────────────

SECRET_FIELDS = frozenset({
    "api_key", "google_credentials_path", "discord_webhook_urls",
})

# ── Defaults ───────────────────────────────────────────────────────

DEFAULTS = {
    "api_key": "",
    "database_path": "database/mlb_model.db",
    "cache_path": "data/_api_cache",
    "output_dir": "output",
    "timezone": "America/New_York",
    "freshness_threshold_seconds": 3600,
    "scheduling_morning_hour": 9,
    "scheduling_pregame_interval_minutes": 30,
    "spreadsheet_id": "",
    "google_credentials_path": "",
    "discord_webhook_urls": "",
    "min_confidence_score": 40.0,
    "min_ev_pct": 2.0,
    "enabled_markets": "all",
    "enabled_delivery_channels": "none",
    "log_level": "INFO",
    "log_format": "human",
    "backup_retention_count": 7,
    "backup_compression": False,
    "backup_dir": "backups",
    "environment": "local",
    "scheduler_enabled": True,
    "shadow_mode": True,
}

# ── Environment variable mapping ───────────────────────────────────

ENV_MAP = {
    "SPORTSODDS_API_KEY": "api_key",
    "MLB_DB_PATH": "database_path",
    "MLB_CACHE_PATH": "cache_path",
    "MLB_OUTPUT_DIR": "output_dir",
    "MLB_TIMEZONE": "timezone",
    "MLB_FRESHNESS_THRESHOLD": "freshness_threshold_seconds",
    "MLB_SPREADSHEET_ID": "spreadsheet_id",
    "MLB_GOOGLE_CREDENTIALS": "google_credentials_path",
    "MLB_DISCORD_WEBHOOKS": "discord_webhook_urls",
    "MLB_MIN_CONFIDENCE": "min_confidence_score",
    "MLB_MIN_EV": "min_ev_pct",
    "MLB_ENABLED_MARKETS": "enabled_markets",
    "MLB_DELIVERY_CHANNELS": "enabled_delivery_channels",
    "MLB_LOG_LEVEL": "log_level",
    "MLB_LOG_FORMAT": "log_format",
    "MLB_BACKUP_RETENTION": "backup_retention_count",
    "MLB_BACKUP_COMPRESSION": "backup_compression",
    "MLB_BACKUP_DIR": "backup_dir",
    "MLB_ENVIRONMENT": "environment",
    "MLB_SCHEDULER_ENABLED": "scheduler_enabled",
    "MLB_SHADOW_MODE": "shadow_mode",
}


@dataclass
class ProductionConfig:
    """All production configuration fields with safe defaults."""
    api_key: str = ""
    database_path: str = "database/mlb_model.db"
    cache_path: str = "data/_api_cache"
    output_dir: str = "output"
    timezone: str = "America/New_York"
    freshness_threshold_seconds: int = 3600
    scheduling_morning_hour: int = 9
    scheduling_pregame_interval_minutes: int = 30
    spreadsheet_id: str = ""
    google_credentials_path: str = ""
    discord_webhook_urls: str = ""
    min_confidence_score: float = 40.0
    min_ev_pct: float = 2.0
    enabled_markets: str = "all"
    enabled_delivery_channels: str = "none"
    log_level: str = "INFO"
    log_format: str = "human"
    backup_retention_count: int = 7
    backup_compression: bool = False
    backup_dir: str = "backups"
    environment: str = "local"
    scheduler_enabled: bool = True
    shadow_mode: bool = True

    def redacted(self) -> dict[str, Any]:
        """Return config as dict with secret fields redacted."""
        d = asdict(self)
        for key in SECRET_FIELDS:
            if key in d and d[key]:
                d[key] = "***REDACTED***"
        return d

    def validate(self) -> list[str]:
        """Validate configuration. Returns list of error messages."""
        errors = []

        if not self.api_key:
            errors.append("api_key is required (set SPORTSODDS_API_KEY)")

        if self.freshness_threshold_seconds <= 0:
            errors.append("freshness_threshold_seconds must be > 0")

        if self.min_ev_pct < 0:
            errors.append("min_ev_pct must be >= 0")

        if self.min_confidence_score < 0 or self.min_confidence_score > 100:
            errors.append("min_confidence_score must be 0-100")

        if self.backup_retention_count < 0:
            errors.append("backup_retention_count must be >= 0")

        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            errors.append(f"invalid log_level: {self.log_level}")

        if self.log_format not in ("human", "json"):
            errors.append(f"invalid log_format: {self.log_format}")

        try:
            import zoneinfo
            zoneinfo.ZoneInfo(self.timezone)
        except (ValueError, zoneinfo.ZoneInfoNotFoundError):
            errors.append(f"invalid timezone: {self.timezone}")

        return errors


def load_config(config_path: str | Path | None = None) -> ProductionConfig:
    """Load configuration from file, environment variables, and defaults.

    Priority: environment variables > config file > defaults.
    """
    load_dotenv()
    values = dict(DEFAULTS)

    # Load from config file if provided
    if config_path:
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                file_values = json.load(f)
            values.update(file_values)

    # Override with environment variables
    for env_var, config_key in ENV_MAP.items():
        env_val = os.environ.get(env_var)
        if env_val is not None:
            # Type coercion
            default = DEFAULTS.get(config_key)
            if isinstance(default, bool):
                values[config_key] = env_val.lower() in ("true", "1", "yes")
            elif isinstance(default, float):
                values[config_key] = float(env_val)
            elif isinstance(default, int):
                values[config_key] = int(env_val)
            else:
                values[config_key] = env_val

    return ProductionConfig(**values)


def save_config(config: ProductionConfig, path: str | Path) -> None:
    """Save configuration to a JSON file. Secrets are excluded."""
    d = asdict(config)
    for key in SECRET_FIELDS:
        if key in d:
            d[key] = ""
    with open(path, "w") as f:
        json.dump(d, f, indent=2)


def create_env_example() -> str:
    """Generate .env.example content."""
    lines = [
        "# MLB Sportsbook Analysis — Environment Variables",
        "# Copy to .env and fill in values",
        "",
        "# Required",
        "SPORTSODDS_API_KEY=your_api_key_here",
        "",
        "# Optional — paths",
        "# MLB_DB_PATH=database/mlb_model.db",
        "# MLB_CACHE_PATH=data/_api_cache",
        "# MLB_OUTPUT_DIR=output",
        "",
        "# Optional — scheduling",
        "# MLB_TIMEZONE=America/New_York",
        "# MLB_FRESHNESS_THRESHOLD=3600",
        "",
        "# Optional — Google Sheets",
        "# MLB_SPREADSHEET_ID=your_spreadsheet_id",
        "# MLB_GOOGLE_CREDENTIALS=path/to/credentials.json",
        "",
        "# Optional — Discord",
        "# MLB_DISCORD_WEBHOOKS=https://discord.com/api/webhooks/...,https://...",
        "",
        "# Optional — filtering",
        "# MLB_MIN_CONFIDENCE=40.0",
        "# MLB_MIN_EV=2.0",
        "# MLB_ENABLED_MARKETS=all",
        "# MLB_DELIVERY_CHANNELS=none",
        "",
        "# Optional — logging",
        "# MLB_LOG_LEVEL=INFO",
        "# MLB_LOG_FORMAT=human",
        "",
        "# Optional — backup",
        "# MLB_BACKUP_RETENTION=7",
        "# MLB_BACKUP_COMPRESSION=false",
    ]
    return "\n".join(lines)
