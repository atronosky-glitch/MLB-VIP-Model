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
    "discord_webhook_urls_arb_middle", "discord_webhook_urls_middle",
    "results_webhook_url",
    "kalshi_api_key_id", "kalshi_private_key_path",
    "polymarket_us_api_key_id", "polymarket_us_private_key_path",
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
    "discord_webhook_urls_arb_middle": "",
    "discord_webhook_urls_middle": "",
    "results_webhook_url": "",
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
    "kalshi_enabled": False,
    "kalshi_env": "demo",
    "kalshi_api_key_id": "",
    "kalshi_private_key_path": "",
    "polymarket_us_enabled": False,
    "polymarket_us_api_key_id": "",
    "polymarket_us_private_key_path": "",
    "min_market_match_confidence": 0.98,
    "execution_analysis_stake_usd": 10.0,
    "min_raw_ev_pct": 2.0,
    "min_net_ev_pct": 1.0,
    "max_spread_pct": 0.10,
    "max_slippage_pct": 0.05,
    "min_available_liquidity_usd": 50.0,
    "max_market_data_age_seconds": 30,
    "execution_allowed_rec_statuses": "STRONG_EDGE,POSITIVE_EDGE,STRONG_PRICE_OUTLIER,PRICE_OUTLIER",
    # Stage 3: paper trading. Everything below is simulated -- no real
    # money ever moves, and no config value here can enable order
    # placement (that path is hard-blocked in the provider classes
    # themselves, not gated by config).
    "paper_trading_enabled": True,
    "paper_starting_bankroll_usd": 1000.0,
    "unit_size_usd": 10.0,
    "bet_sizing_mode": "FLAT",
    "default_units": 1.0,
    "max_units_per_bet": 2.0,
    "kelly_multiplier": 0.25,
    "ev_tiered_sizing_table_json": (
        '[{"min_net_ev_pct": 3, "units": 0.5}, {"min_net_ev_pct": 5, "units": 1.0}, '
        '{"min_net_ev_pct": 8, "units": 1.5}, {"min_net_ev_pct": 12, "units": 2.0}]'
    ),
    "max_bet_usd": 25.0,
    "max_bet_pct_bankroll": 0.025,
    "max_event_exposure_usd": 50.0,
    "max_provider_exposure_usd": 250.0,
    "max_sport_exposure_usd": 300.0,
    "max_open_exposure_usd": 500.0,
    "max_daily_wagered_usd": 250.0,
    "max_daily_loss_usd": 100.0,
    "max_open_positions": 20,
    "max_trades_per_hour": 20,
    "min_paper_trade_usd": 1.0,
    "allow_risk_size_reduction": True,
    "allow_partial_paper_fills": False,
    "allow_position_addons": False,
    "allow_retrade_settled_recommendation": False,
    "max_opportunity_age_seconds": 30,
    "stop_after_daily_profit_target": False,
    "daily_profit_target_usd": 0.0,
    # Stage 4: human-approved live (real-money) execution. Every value
    # below defaults OFF/conservative. A real trade is only reachable if
    # live_trading_enabled AND require_human_approval AND the specific
    # provider's live flag are ALL true, AND a valid unexpired approval
    # exists, AND RiskEngine approves the fresh re-check -- see
    # src/execution/live/service.py::_can_attempt_live_trade(). No
    # single toggle here can enable real order placement by itself.
    "live_trading_enabled": False,
    "require_human_approval": True,
    "kalshi_live_enabled": False,
    "polymarket_us_live_enabled": False,
    # Per-customer Auto-Bet (src/execution/customer_autobet.py) hard
    # server-side ceilings, section 12 of the Kalshi+Polymarket Auto-Bet
    # spec: a customer's own auto_approve_max_usd is necessary but never
    # sufficient for unattended execution -- the intended stake must ALSO
    # be at/under this operator-controlled cap, which no customer setting
    # can override. $100 is a conservative starting default (well above
    # the customer-facing DEFAULT_RISK_SETTINGS auto_approve_max_usd of
    # $10 so it doesn't interfere with typical accounts, but still a real
    # ceiling against a misconfigured or compromised customer setting) --
    # review and adjust for your own risk tolerance before relying on it.
    "kalshi_autobet_server_max_order_usd": 100.0,
    "polymarket_us_autobet_server_max_order_usd": 100.0,
    "approval_ttl_seconds": 30,
    "live_order_mode": "IOC_LIMIT",
    "live_max_price_move_pct": 0.02,
    "live_max_ev_degradation_pct": 0.5,
    "live_require_fresh_orderbook": True,
    "live_max_orderbook_age_seconds": 10,
    "live_approval_ui_mode": "LOCAL_ONLY",
    "streamlit_live_approval_enabled": False,
    "live_allow_post_approval_size_reduction": False,
    "max_financial_post_attempts_per_approval": 1,
    "live_provider_error_threshold": 3,
    "live_provider_error_window_minutes": 15,
    "kill_switch_cancel_open_orders": False,
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
    "MLB_DISCORD_WEBHOOKS_ARB_MIDDLE": "discord_webhook_urls_arb_middle",
    "MLB_DISCORD_WEBHOOKS_MIDDLE": "discord_webhook_urls_middle",
    # 2026-09-21: the operator created these two Render env vars with
    # their own names directly in the dashboard ("Arbitrage_Finder" for
    # the arbitrage channel, "Result_Webhook" for the new daily-results
    # channel) rather than the MLB_DISCORD_WEBHOOKS_* convention above --
    # mapped here with their exact names (case-sensitive, os.environ.get
    # is exact-match) instead of asking for a rename.
    "Arbitrage_Finder": "discord_webhook_urls_arb_middle",
    "Result_Webhook": "results_webhook_url",
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
    "KALSHI_ENABLED": "kalshi_enabled",
    "KALSHI_ENV": "kalshi_env",
    "KALSHI_API_KEY_ID": "kalshi_api_key_id",
    "KALSHI_PRIVATE_KEY_PATH": "kalshi_private_key_path",
    "POLYMARKET_US_ENABLED": "polymarket_us_enabled",
    "POLYMARKET_US_API_KEY_ID": "polymarket_us_api_key_id",
    "POLYMARKET_US_PRIVATE_KEY_PATH": "polymarket_us_private_key_path",
    "MLB_MIN_MARKET_MATCH_CONFIDENCE": "min_market_match_confidence",
    "MLB_EXECUTION_ANALYSIS_STAKE_USD": "execution_analysis_stake_usd",
    "MLB_MIN_RAW_EV_PCT": "min_raw_ev_pct",
    "MLB_MIN_NET_EV_PCT": "min_net_ev_pct",
    "MLB_MAX_SPREAD_PCT": "max_spread_pct",
    "MLB_MAX_SLIPPAGE_PCT": "max_slippage_pct",
    "MLB_MIN_AVAILABLE_LIQUIDITY_USD": "min_available_liquidity_usd",
    "MLB_MAX_MARKET_DATA_AGE_SECONDS": "max_market_data_age_seconds",
    "MLB_EXECUTION_ALLOWED_REC_STATUSES": "execution_allowed_rec_statuses",
    "PAPER_TRADING_ENABLED": "paper_trading_enabled",
    "PAPER_STARTING_BANKROLL_USD": "paper_starting_bankroll_usd",
    "UNIT_SIZE_USD": "unit_size_usd",
    "BET_SIZING_MODE": "bet_sizing_mode",
    "DEFAULT_UNITS": "default_units",
    "MAX_UNITS_PER_BET": "max_units_per_bet",
    "KELLY_MULTIPLIER": "kelly_multiplier",
    "EV_TIERED_SIZING_TABLE_JSON": "ev_tiered_sizing_table_json",
    "MAX_BET_USD": "max_bet_usd",
    "MAX_BET_PCT_BANKROLL": "max_bet_pct_bankroll",
    "MAX_EVENT_EXPOSURE_USD": "max_event_exposure_usd",
    "MAX_PROVIDER_EXPOSURE_USD": "max_provider_exposure_usd",
    "MAX_SPORT_EXPOSURE_USD": "max_sport_exposure_usd",
    "MAX_OPEN_EXPOSURE_USD": "max_open_exposure_usd",
    "MAX_DAILY_WAGERED_USD": "max_daily_wagered_usd",
    "MAX_DAILY_LOSS_USD": "max_daily_loss_usd",
    "MAX_OPEN_POSITIONS": "max_open_positions",
    "MAX_TRADES_PER_HOUR": "max_trades_per_hour",
    "MIN_PAPER_TRADE_USD": "min_paper_trade_usd",
    "ALLOW_RISK_SIZE_REDUCTION": "allow_risk_size_reduction",
    "ALLOW_PARTIAL_PAPER_FILLS": "allow_partial_paper_fills",
    "ALLOW_POSITION_ADDONS": "allow_position_addons",
    "ALLOW_RETRADE_SETTLED_RECOMMENDATION": "allow_retrade_settled_recommendation",
    "MAX_OPPORTUNITY_AGE_SECONDS": "max_opportunity_age_seconds",
    "STOP_AFTER_DAILY_PROFIT_TARGET": "stop_after_daily_profit_target",
    "DAILY_PROFIT_TARGET_USD": "daily_profit_target_usd",
    "LIVE_TRADING_ENABLED": "live_trading_enabled",
    "REQUIRE_HUMAN_APPROVAL": "require_human_approval",
    "KALSHI_LIVE_ENABLED": "kalshi_live_enabled",
    "POLYMARKET_US_LIVE_ENABLED": "polymarket_us_live_enabled",
    "KALSHI_AUTOBET_SERVER_MAX_ORDER_USD": "kalshi_autobet_server_max_order_usd",
    "POLYMARKET_US_AUTOBET_SERVER_MAX_ORDER_USD": "polymarket_us_autobet_server_max_order_usd",
    "APPROVAL_TTL_SECONDS": "approval_ttl_seconds",
    "LIVE_ORDER_MODE": "live_order_mode",
    "LIVE_MAX_PRICE_MOVE_PCT": "live_max_price_move_pct",
    "LIVE_MAX_EV_DEGRADATION_PCT": "live_max_ev_degradation_pct",
    "LIVE_REQUIRE_FRESH_ORDERBOOK": "live_require_fresh_orderbook",
    "LIVE_MAX_ORDERBOOK_AGE_SECONDS": "live_max_orderbook_age_seconds",
    "LIVE_APPROVAL_UI_MODE": "live_approval_ui_mode",
    "STREAMLIT_LIVE_APPROVAL_ENABLED": "streamlit_live_approval_enabled",
    "LIVE_ALLOW_POST_APPROVAL_SIZE_REDUCTION": "live_allow_post_approval_size_reduction",
    "MAX_FINANCIAL_POST_ATTEMPTS_PER_APPROVAL": "max_financial_post_attempts_per_approval",
    "LIVE_PROVIDER_ERROR_THRESHOLD": "live_provider_error_threshold",
    "LIVE_PROVIDER_ERROR_WINDOW_MINUTES": "live_provider_error_window_minutes",
    "KILL_SWITCH_CANCEL_OPEN_ORDERS": "kill_switch_cancel_open_orders",
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
    discord_webhook_urls_arb_middle: str = ""
    discord_webhook_urls_middle: str = ""
    results_webhook_url: str = ""
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
    kalshi_enabled: bool = False
    kalshi_env: str = "demo"
    kalshi_api_key_id: str = ""
    kalshi_private_key_path: str = ""
    polymarket_us_enabled: bool = False
    polymarket_us_api_key_id: str = ""
    polymarket_us_private_key_path: str = ""
    min_market_match_confidence: float = 0.98
    execution_analysis_stake_usd: float = 10.0
    min_raw_ev_pct: float = 2.0
    min_net_ev_pct: float = 1.0
    max_spread_pct: float = 0.10
    max_slippage_pct: float = 0.05
    min_available_liquidity_usd: float = 50.0
    max_market_data_age_seconds: int = 30
    # Which historical_recommendations.rec_status values the execution
    # layer will evaluate. A single canonical source (this field), not
    # hardcoded separately in cli.py/evaluator.py -- deliberately
    # repeating src/discord_delivery.py's fix rather than its original
    # bug: 'BET'/'LEAN' never existed in this schema; the real values
    # src/prop_config.py's classification writes are STRONG_EDGE/
    # POSITIVE_EDGE/MARGINAL_EDGE (O/U) and STRONG_PRICE_OUTLIER/
    # PRICE_OUTLIER/MARGINAL_PRICE_OUTLIER (YN). Only the four
    # "actionable" tiers are included by default -- MARGINAL_*/NO_EDGE
    # are excluded because they represent a weaker edge the model
    # itself doesn't currently classify as worth acting on (see
    # src/discord_delivery.py's identical default for the same reason).
    execution_allowed_rec_statuses: str = "STRONG_EDGE,POSITIVE_EDGE,STRONG_PRICE_OUTLIER,PRICE_OUTLIER"

    # Stage 3: paper trading (simulated orders/fills/positions/P&L only --
    # no config value here can enable real order placement; that path is
    # hard-blocked in KalshiProvider/PolymarketUSProvider themselves).
    paper_trading_enabled: bool = True
    paper_starting_bankroll_usd: float = 1000.0
    unit_size_usd: float = 10.0
    bet_sizing_mode: str = "FLAT"
    default_units: float = 1.0
    max_units_per_bet: float = 2.0
    kelly_multiplier: float = 0.25
    # JSON list of {"min_net_ev_pct": <float>, "units": <float>} tiers for
    # BET_SIZING_MODE=EV_TIERED -- an example table, not a fixed rule; see
    # ev_tiered_sizing_tiers() for the one place this is parsed.
    ev_tiered_sizing_table_json: str = (
        '[{"min_net_ev_pct": 3, "units": 0.5}, {"min_net_ev_pct": 5, "units": 1.0}, '
        '{"min_net_ev_pct": 8, "units": 1.5}, {"min_net_ev_pct": 12, "units": 2.0}]'
    )
    max_bet_usd: float = 25.0
    max_bet_pct_bankroll: float = 0.025
    max_event_exposure_usd: float = 50.0
    max_provider_exposure_usd: float = 250.0
    max_sport_exposure_usd: float = 300.0
    max_open_exposure_usd: float = 500.0
    max_daily_wagered_usd: float = 250.0
    max_daily_loss_usd: float = 100.0
    max_open_positions: int = 20
    max_trades_per_hour: int = 20
    min_paper_trade_usd: float = 1.0
    allow_risk_size_reduction: bool = True
    allow_partial_paper_fills: bool = False
    allow_position_addons: bool = False
    # Implements section 12's "...unless explicitly configured otherwise"
    # clause for ALREADY_TRADED_RECOMMENDATION -- there is no other knob
    # for this, so it gets its own flag rather than silently always
    # blocking re-trades of a settled recommendation forever.
    allow_retrade_settled_recommendation: bool = False
    max_opportunity_age_seconds: int = 30
    stop_after_daily_profit_target: bool = False
    daily_profit_target_usd: float = 0.0

    # Stage 4: human-approved live (real-money) execution -- all default
    # OFF/conservative; see DEFAULTS above for the full explanation of
    # the multi-gate requirement.
    live_trading_enabled: bool = False
    require_human_approval: bool = True
    kalshi_live_enabled: bool = False
    polymarket_us_live_enabled: bool = False
    kalshi_autobet_server_max_order_usd: float = 100.0
    polymarket_us_autobet_server_max_order_usd: float = 100.0
    approval_ttl_seconds: int = 30
    live_order_mode: str = "IOC_LIMIT"
    live_max_price_move_pct: float = 0.02
    live_max_ev_degradation_pct: float = 0.5
    live_require_fresh_orderbook: bool = True
    live_max_orderbook_age_seconds: int = 10
    live_approval_ui_mode: str = "LOCAL_ONLY"
    streamlit_live_approval_enabled: bool = False
    live_allow_post_approval_size_reduction: bool = False
    max_financial_post_attempts_per_approval: int = 1
    live_provider_error_threshold: int = 3
    live_provider_error_window_minutes: int = 15
    kill_switch_cancel_open_orders: bool = False

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

        if self.kalshi_env not in ("demo", "production"):
            errors.append(f"invalid kalshi_env: {self.kalshi_env} (must be 'demo' or 'production')")

        if self.kalshi_enabled and not (self.kalshi_api_key_id and self.kalshi_private_key_path):
            errors.append("kalshi_enabled requires both kalshi_api_key_id and kalshi_private_key_path")

        if self.polymarket_us_enabled and not (
            self.polymarket_us_api_key_id and self.polymarket_us_private_key_path
        ):
            errors.append(
                "polymarket_us_enabled requires both polymarket_us_api_key_id "
                "and polymarket_us_private_key_path"
            )

        if self.kalshi_autobet_server_max_order_usd <= 0:
            errors.append("kalshi_autobet_server_max_order_usd must be > 0")
        if self.polymarket_us_autobet_server_max_order_usd <= 0:
            errors.append("polymarket_us_autobet_server_max_order_usd must be > 0")

        if not 0.0 <= self.min_market_match_confidence <= 1.0:
            errors.append("min_market_match_confidence must be between 0 and 1")

        if self.execution_analysis_stake_usd <= 0:
            errors.append("execution_analysis_stake_usd must be > 0")

        if self.min_raw_ev_pct < 0:
            errors.append("min_raw_ev_pct must be >= 0")

        if self.min_net_ev_pct < 0:
            errors.append("min_net_ev_pct must be >= 0")

        if not 0.0 <= self.max_spread_pct <= 1.0:
            errors.append("max_spread_pct must be between 0 and 1")

        if not 0.0 <= self.max_slippage_pct <= 1.0:
            errors.append("max_slippage_pct must be between 0 and 1")

        if self.min_available_liquidity_usd < 0:
            errors.append("min_available_liquidity_usd must be >= 0")

        if self.max_market_data_age_seconds <= 0:
            errors.append("max_market_data_age_seconds must be > 0")

        allowed_statuses = [s.strip() for s in self.execution_allowed_rec_statuses.split(",") if s.strip()]
        if not allowed_statuses:
            errors.append("execution_allowed_rec_statuses must list at least one status")

        if self.paper_starting_bankroll_usd <= 0:
            errors.append("paper_starting_bankroll_usd must be > 0")

        if self.unit_size_usd <= 0:
            errors.append("unit_size_usd must be > 0")

        if self.bet_sizing_mode not in ("FLAT", "EV_TIERED", "FRACTIONAL_KELLY"):
            errors.append(f"invalid bet_sizing_mode: {self.bet_sizing_mode}")

        if self.default_units <= 0:
            errors.append("default_units must be > 0")

        if self.max_units_per_bet <= 0:
            errors.append("max_units_per_bet must be > 0")

        if not 0.0 < self.kelly_multiplier <= 1.0:
            errors.append("kelly_multiplier must be between 0 (exclusive) and 1")

        try:
            tiers = self.ev_tiered_sizing_tiers()
            if not tiers:
                errors.append("ev_tiered_sizing_table_json must list at least one tier")
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"invalid ev_tiered_sizing_table_json: {exc}")

        for field_name in (
            "max_bet_usd", "max_event_exposure_usd", "max_provider_exposure_usd",
            "max_sport_exposure_usd", "max_open_exposure_usd", "max_daily_wagered_usd",
            "max_daily_loss_usd", "min_paper_trade_usd", "daily_profit_target_usd",
        ):
            if getattr(self, field_name) < 0:
                errors.append(f"{field_name} must be >= 0")

        if not 0.0 <= self.max_bet_pct_bankroll <= 1.0:
            errors.append("max_bet_pct_bankroll must be between 0 and 1")

        if self.max_open_positions <= 0:
            errors.append("max_open_positions must be > 0")

        if self.max_trades_per_hour <= 0:
            errors.append("max_trades_per_hour must be > 0")

        if self.max_opportunity_age_seconds <= 0:
            errors.append("max_opportunity_age_seconds must be > 0")

        if self.approval_ttl_seconds <= 0:
            errors.append("approval_ttl_seconds must be > 0")

        if self.live_order_mode not in ("IOC_LIMIT", "FOK_LIMIT"):
            errors.append(f"invalid live_order_mode: {self.live_order_mode} (unrestricted market orders are not supported)")

        if not 0.0 <= self.live_max_price_move_pct <= 1.0:
            errors.append("live_max_price_move_pct must be between 0 and 1")

        if not 0.0 <= self.live_max_ev_degradation_pct <= 1.0:
            errors.append("live_max_ev_degradation_pct must be between 0 and 1")

        if self.live_max_orderbook_age_seconds <= 0:
            errors.append("live_max_orderbook_age_seconds must be > 0")

        if self.live_approval_ui_mode != "LOCAL_ONLY":
            errors.append(
                f"invalid live_approval_ui_mode: {self.live_approval_ui_mode} "
                "(only LOCAL_ONLY is supported -- there is no authentication layer for remote approval yet)"
            )

        if self.max_financial_post_attempts_per_approval != 1:
            errors.append("max_financial_post_attempts_per_approval must be 1 (no provider supports safe automatic retries yet)")

        if self.live_provider_error_threshold <= 0:
            errors.append("live_provider_error_threshold must be > 0")

        if self.live_provider_error_window_minutes <= 0:
            errors.append("live_provider_error_window_minutes must be > 0")

        return errors

    def execution_allowed_rec_statuses_list(self) -> tuple[str, ...]:
        """execution_allowed_rec_statuses, parsed -- the single
        canonical way any execution-layer code should get this list
        (never re-split the raw string in more than one place)."""
        return tuple(s.strip() for s in self.execution_allowed_rec_statuses.split(",") if s.strip())

    def ev_tiered_sizing_tiers(self) -> tuple[tuple[float, float], ...]:
        """ev_tiered_sizing_table_json, parsed once into (min_net_ev_pct,
        units) pairs sorted ascending by threshold -- the single
        canonical way sizing.py reads this table."""
        raw = json.loads(self.ev_tiered_sizing_table_json)
        tiers = tuple(
            (float(entry["min_net_ev_pct"]), float(entry["units"]))
            for entry in raw
        )
        return tuple(sorted(tiers, key=lambda t: t[0]))


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
        "# MLB_DISCORD_WEBHOOKS_ARB_MIDDLE=https://discord.com/api/webhooks/... (arbitrage alerts; MLB_DISCORD_WEBHOOKS above is EV picks only)",
        "# MLB_DISCORD_WEBHOOKS_MIDDLE=https://discord.com/api/webhooks/... (middle alerts, its own channel)",
        "# Arbitrage_Finder=https://discord.com/api/webhooks/... (alternate name accepted for the arbitrage channel above)",
        "# Result_Webhook=https://discord.com/api/webhooks/... (end-of-day results summary, its own channel)",
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
        "",
        "# Optional — Kalshi / Polymarket US execution layer (Stage 1: read-only",
        "# connectivity; Stage 2: game-level market matching only). No order",
        "# placement exists yet. PAPER_TRADING/AUTO_TRADING_ENABLED-style flags",
        "# are introduced in a later stage.",
        "# KALSHI_ENABLED=false",
        "# KALSHI_ENV=demo",
        "# KALSHI_API_KEY_ID=your_kalshi_key_id",
        "# KALSHI_PRIVATE_KEY_PATH=/path/to/kalshi_private_key.pem",
        "# POLYMARKET_US_ENABLED=false",
        "# POLYMARKET_US_API_KEY_ID=your_polymarket_us_key_id",
        "# POLYMARKET_US_PRIVATE_KEY_PATH=/path/to/polymarket_us_secret_key.txt",
        "# MLB_MIN_MARKET_MATCH_CONFIDENCE=0.98",
        "",
        "# Optional — execution-layer analysis (Stage 2B: pricing/EV analysis",
        "# only, no order placement exists yet)",
        "# MLB_EXECUTION_ANALYSIS_STAKE_USD=10.00",
        "# MLB_MIN_RAW_EV_PCT=2.0",
        "# MLB_MIN_NET_EV_PCT=1.0",
        "# MLB_MAX_SPREAD_PCT=0.10",
        "# MLB_MAX_SLIPPAGE_PCT=0.05",
        "# MLB_MIN_AVAILABLE_LIQUIDITY_USD=50.00",
        "# MLB_MAX_MARKET_DATA_AGE_SECONDS=30",
        "# MLB_EXECUTION_ALLOWED_REC_STATUSES=STRONG_EDGE,POSITIVE_EDGE,STRONG_PRICE_OUTLIER,PRICE_OUTLIER",
        "",
        "# Optional — Stage 3 paper trading (simulated only -- no real money,",
        "# no real order ever placed; place_order/cancel_order remain",
        "# NotImplemented on every provider regardless of these values)",
        "# PAPER_TRADING_ENABLED=true",
        "# PAPER_STARTING_BANKROLL_USD=1000.00",
        "# UNIT_SIZE_USD=10.00",
        "# BET_SIZING_MODE=FLAT",
        "# DEFAULT_UNITS=1",
        "# MAX_UNITS_PER_BET=2",
        "# KELLY_MULTIPLIER=0.25",
        '# EV_TIERED_SIZING_TABLE_JSON=[{"min_net_ev_pct": 3, "units": 0.5}, {"min_net_ev_pct": 5, "units": 1.0}]',
        "# MAX_BET_USD=25.00",
        "# MAX_BET_PCT_BANKROLL=0.025",
        "# MAX_EVENT_EXPOSURE_USD=50.00",
        "# MAX_PROVIDER_EXPOSURE_USD=250.00",
        "# MAX_SPORT_EXPOSURE_USD=300.00",
        "# MAX_OPEN_EXPOSURE_USD=500.00",
        "# MAX_DAILY_WAGERED_USD=250.00",
        "# MAX_DAILY_LOSS_USD=100.00",
        "# MAX_OPEN_POSITIONS=20",
        "# MAX_TRADES_PER_HOUR=20",
        "# MIN_PAPER_TRADE_USD=1.00",
        "# ALLOW_RISK_SIZE_REDUCTION=true",
        "# ALLOW_PARTIAL_PAPER_FILLS=false",
        "# ALLOW_POSITION_ADDONS=false",
        "# ALLOW_RETRADE_SETTLED_RECOMMENDATION=false",
        "# MAX_OPPORTUNITY_AGE_SECONDS=30",
        "# STOP_AFTER_DAILY_PROFIT_TARGET=false",
        "# DAILY_PROFIT_TARGET_USD=0.00",
        "",
        "# Optional — Stage 4 human-approved LIVE (real-money) execution.",
        "# EVERY value below must default to the safe/OFF value shown. A real",
        "# order can only ever be attempted if live_trading_enabled AND",
        "# require_human_approval AND the specific provider's live flag are ALL",
        "# true, AND a valid unexpired human approval exists, AND RiskEngine",
        "# approves a FRESH re-check immediately before submission. Kalshi live",
        "# submission additionally fails closed unconditionally",
        "# (KALSHI_LIVE_SCHEMA_VERIFIED=False, hard-coded in kalshi.py, not an",
        "# env var) until its exact live-order schema is independently verified.",
        "# DO NOT enable any of these in production without understanding every",
        "# gate above.",
        "# LIVE_TRADING_ENABLED=false",
        "# REQUIRE_HUMAN_APPROVAL=true",
        "# KALSHI_LIVE_ENABLED=false",
        "# POLYMARKET_US_LIVE_ENABLED=false",
        "# Per-customer Auto-Bet hard server-side order-size ceilings -- no",
        "# customer setting can exceed these regardless of their own",
        "# auto_approve_max_usd. Review before launch.",
        "# KALSHI_AUTOBET_SERVER_MAX_ORDER_USD=100.0",
        "# POLYMARKET_US_AUTOBET_SERVER_MAX_ORDER_USD=100.0",
        "# APPROVAL_TTL_SECONDS=30",
        "# LIVE_ORDER_MODE=IOC_LIMIT",
        "# LIVE_MAX_PRICE_MOVE_PCT=0.02",
        "# LIVE_MAX_EV_DEGRADATION_PCT=0.5",
        "# LIVE_REQUIRE_FRESH_ORDERBOOK=true",
        "# LIVE_MAX_ORDERBOOK_AGE_SECONDS=10",
        "# LIVE_APPROVAL_UI_MODE=LOCAL_ONLY",
        "# STREAMLIT_LIVE_APPROVAL_ENABLED=false",
        "# LIVE_ALLOW_POST_APPROVAL_SIZE_REDUCTION=false",
        "# MAX_FINANCIAL_POST_ATTEMPTS_PER_APPROVAL=1",
        "# LIVE_PROVIDER_ERROR_THRESHOLD=3",
        "# LIVE_PROVIDER_ERROR_WINDOW_MINUTES=15",
        "# KILL_SWITCH_CANCEL_OPEN_ORDERS=false",
    ]
    return "\n".join(lines)
