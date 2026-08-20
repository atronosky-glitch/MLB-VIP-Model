"""Phase 11 tests: Live readiness, canary, delivery gate, dashboard, promotion, checklist.

All tests are deterministic, use mocks/fixtures, no live APIs.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# A single real (field-for-field verified against a cached SportsGameOdds v2
# response) event, used by the production-canary tests below instead of a
# fabricated schema.
REALISTIC_EVENT = {
    "eventID": "evt123",
    "leagueID": "MLB",
    "status": {"started": False, "ended": False},
    "teams": {
        "home": {"teamID": "NYY", "statEntityID": "home", "names": {"long": "New York Yankees"}},
        "away": {"teamID": "BOS", "statEntityID": "away", "names": {"long": "Boston Red Sox"}},
    },
    "odds": {
        "points-away-game-ml-away": {
            "oddID": "points-away-game-ml-away",
            "marketName": "Moneyline",
            "statEntityID": "away",
            "periodID": "game",
            "betTypeID": "ml",
            "sideID": "away",
            "byBookmaker": {
                "draftkings": {"odds": "+165", "lastUpdatedAt": "2026-08-01T19:14:04Z", "available": True},
                "fanduel": {"odds": "+190", "lastUpdatedAt": "2026-08-01T19:14:15Z", "available": True},
            },
        },
    },
}


# ───────────────────────────────────────────────────────────────────
# Live Readiness
# ───────────────────────────────────────────────────────────────────

class TestLiveReadiness:
    def test_readiness_report_finalize_ready(self):
        from src.live_readiness import ReadinessReport, ReadinessCheck
        report = ReadinessReport()
        report.add(ReadinessCheck(name="a", status="pass"))
        report.add(ReadinessCheck(name="b", status="pass"))
        report.finalize()
        assert report.overall_status == "ready"

    def test_readiness_report_finalize_with_warnings(self):
        from src.live_readiness import ReadinessReport, ReadinessCheck
        report = ReadinessReport()
        report.add(ReadinessCheck(name="a", status="pass"))
        report.add(ReadinessCheck(name="b", status="warn"))
        report.finalize()
        assert report.overall_status == "ready_with_warnings"

    def test_readiness_report_finalize_not_ready(self):
        from src.live_readiness import ReadinessReport, ReadinessCheck
        report = ReadinessReport()
        report.add(ReadinessCheck(name="a", status="pass"))
        report.add(ReadinessCheck(name="b", status="fail"))
        report.finalize()
        assert report.overall_status == "not_ready"

    def test_readiness_report_human_readable(self):
        from src.live_readiness import ReadinessReport, ReadinessCheck
        report = ReadinessReport()
        report.add(ReadinessCheck(name="test_check", status="pass", message="all good"))
        report.finalize()
        output = report.to_human_readable()
        assert "test_check" in output
        assert "all good" in output

    def test_readiness_report_to_dict(self):
        from src.live_readiness import ReadinessReport, ReadinessCheck
        report = ReadinessReport()
        report.add(ReadinessCheck(name="x", status="pass"))
        report.finalize()
        d = report.to_dict()
        assert d["overall_status"] == "ready"
        assert d["pass_count"] == 1

    def test_exit_codes(self):
        from src.live_readiness import (
            EXIT_READY, EXIT_READY_WITH_WARNINGS, EXIT_NOT_READY,
            EXIT_CONFIG_FAILURE, EXIT_NETWORK_FAILURE, EXIT_DB_FAILURE,
        )
        assert EXIT_READY == 0
        assert EXIT_NOT_READY == 2

    def test_check_api_credentials_missing(self):
        from src.live_readiness import _check_api_credentials
        from src.production_config import ProductionConfig
        config = ProductionConfig(api_key="")
        check = _check_api_credentials(config, skip_network=True)
        assert check.status == "fail"

    def test_check_api_credentials_short(self):
        from src.live_readiness import _check_api_credentials
        from src.production_config import ProductionConfig
        config = ProductionConfig(api_key="short")
        check = _check_api_credentials(config, skip_network=True)
        assert check.status == "warn"

    def test_check_api_credentials_ok(self):
        from src.live_readiness import _check_api_credentials
        from src.production_config import ProductionConfig
        config = ProductionConfig(api_key="valid-key-12345")
        check = _check_api_credentials(config, skip_network=True)
        assert check.status == "pass"

    def test_check_api_connectivity_skip(self):
        from src.live_readiness import _check_api_connectivity
        from src.production_config import ProductionConfig
        config = ProductionConfig()
        check = _check_api_connectivity(config, skip_network=True)
        assert check.status == "skip"

    def test_check_timezone_valid(self):
        from src.live_readiness import _check_timezone
        from src.production_config import ProductionConfig
        config = ProductionConfig(timezone="America/New_York")
        check = _check_timezone(config)
        assert check.status == "pass"

    def test_check_timezone_invalid(self):
        from src.live_readiness import _check_timezone
        from src.production_config import ProductionConfig
        config = ProductionConfig(timezone="Invalid/Zone")
        check = _check_timezone(config)
        assert check.status == "fail"

    def test_check_system_clock_ok(self):
        from src.live_readiness import _check_system_clock
        check = _check_system_clock()
        assert check.status == "pass"

    def test_check_shadow_mode_on(self):
        from src.live_readiness import _check_shadow_mode
        from src.production_config import ProductionConfig
        with patch("src.live_readiness.load_shadow_config") as mock_load:
            from src.shadow_mode import ShadowConfig
            mock_load.return_value = ShadowConfig(shadow_mode=True)
            check = _check_shadow_mode(ProductionConfig())
            assert check.status == "pass"

    def test_check_live_acknowledgement_missing(self):
        from src.live_readiness import _check_live_acknowledgement
        with patch("src.live_readiness.get_acknowledgement", return_value=None):
            check = _check_live_acknowledgement()
            assert check.status == "fail"

    def test_acknowledge_live_data(self, tmp_path):
        from src.live_readiness import acknowledge_live_data, get_acknowledgement, ACKNOWLEDGEMENT_FILE
        from src.production_config import ProductionConfig
        config = ProductionConfig(api_key="test-key-12345", database_path=str(tmp_path / "db.sqlite"))
        with patch("src.live_readiness.ACKNOWLEDGEMENT_FILE", str(tmp_path / ".ack.json")):
            ack = acknowledge_live_data(config)
            assert ack["acknowledged"] is True
            loaded = get_acknowledgement()
            # get_acknowledgement reads from original path, so patch it too
        assert ack["config_fingerprint"] != ""

    def test_check_discord_config_empty(self):
        from src.live_readiness import _check_discord_config
        from src.production_config import ProductionConfig
        config = ProductionConfig(discord_webhook_urls="  ,  ")
        check = _check_discord_config(config)
        assert check.status == "fail"

    def test_check_sheets_config_no_creds(self):
        from src.live_readiness import _check_sheets_config
        from src.production_config import ProductionConfig
        config = ProductionConfig(spreadsheet_id="abc", google_credentials_path="")
        check = _check_sheets_config(config)
        assert check.status == "fail"

    def test_check_backup_config_disabled(self):
        from src.live_readiness import _check_backup_config
        from src.production_config import ProductionConfig
        config = ProductionConfig(backup_retention_count=0)
        check = _check_backup_config(config)
        assert check.status == "warn"


# ───────────────────────────────────────────────────────────────────
# Production Canary
# ───────────────────────────────────────────────────────────────────

class TestProductionCanary:
    def test_canary_result_defaults(self):
        from src.production_canary import CanaryResult
        r = CanaryResult()
        assert r.status == "pending"
        assert r.errors == []
        assert r.warnings == []

    def test_canary_result_to_dict(self):
        from src.production_canary import CanaryResult
        r = CanaryResult(status="success", events_fetched=3)
        d = r.to_dict()
        assert d["status"] == "success"
        assert d["events_fetched"] == 3

    def test_format_canary_result(self):
        from src.production_canary import CanaryResult, format_canary_result
        r = CanaryResult(status="success", duration_seconds=1.5, events_fetched=3)
        output = format_canary_result(r)
        assert "SUCCESS" in output
        assert "1.5s" in output

    def test_validate_schema_missing_db(self, tmp_path):
        from src.production_canary import _validate_schema
        from src.production_config import ProductionConfig
        config = ProductionConfig(database_path=str(tmp_path / "nonexistent.db"))
        result = _validate_schema(config)
        assert result is not None
        assert "not found" in result

    def test_validate_api_schemas_empty(self):
        from src.production_canary import _validate_api_schemas
        issues = _validate_api_schemas([])
        assert len(issues) == 1

    def test_validate_api_schemas_valid(self):
        from src.production_canary import _validate_api_schemas
        events = [REALISTIC_EVENT]
        issues = _validate_api_schemas(events)
        assert len(issues) == 0

    def test_validate_api_schemas_missing_fields(self):
        from src.production_canary import _validate_api_schemas
        issues = _validate_api_schemas([{"eventID": "evt1"}])
        assert len(issues) == 1
        assert "missing fields" in issues[0]

    def test_extract_sportsbooks(self):
        from src.production_canary import _extract_sportsbooks
        books = _extract_sportsbooks([REALISTIC_EVENT])
        assert "draftkings" in books
        assert "fanduel" in books

    def test_extract_markets(self):
        from src.production_canary import _extract_markets
        markets = _extract_markets([REALISTIC_EVENT])
        assert len(markets) == 1
        assert markets[0]["odd_id"] == "points-away-game-ml-away"
        assert markets[0]["market_name"] == "Moneyline"
        assert "draftkings" in markets[0]["sportsbooks"]

    def test_validate_mappings_ok(self):
        from src.production_canary import _validate_mappings
        issues = _validate_mappings([REALISTIC_EVENT])
        assert issues == []

    def test_validate_mappings_missing_team_id(self):
        from src.production_canary import _validate_mappings
        event = {"teams": {
            "home": {"statEntityID": "home"},
            "away": {"teamID": "BOS", "statEntityID": "away"},
        }}
        issues = _validate_mappings([event])
        assert len(issues) == 1
        assert "missing team IDs" in issues[0]

    def test_validate_market_mappings_recognized_prop(self):
        from src.production_canary import _validate_market_mappings
        markets = [{
            "odd_id": "pitching_strikeouts-PLAYER123-game-ou-over",
            "stat_entity_id": "PLAYER123",
        }]
        issues = _validate_market_mappings(markets)
        assert issues == []

    def test_validate_market_mappings_game_level_skipped(self):
        from src.production_canary import _validate_market_mappings
        markets = [{"odd_id": "points-away-game-ml-away", "stat_entity_id": "away"}]
        issues = _validate_market_mappings(markets)
        assert issues == []

    def test_validate_market_mappings_stale_registry_warns(self):
        from src.production_canary import _validate_market_mappings
        markets = [{
            "odd_id": "unknown_stat-PLAYER123-game-ou-over",
            "stat_entity_id": "PLAYER123",
        }]
        issues = _validate_market_mappings(markets)
        assert len(issues) == 1
        assert "matched no registered pattern" in issues[0]

    def test_validate_market_mappings_respects_league_argument(self):
        """A real NFL oddID must match against NFL's registry, not MLB's."""
        from src.production_canary import _validate_market_mappings
        markets = [{
            "odd_id": "passing_yards-JARED_GOFF_1_NFL-game-ou-over",
            "stat_entity_id": "JARED_GOFF_1_NFL",
        }]
        assert _validate_market_mappings(markets, league="NFL") == []
        # The same real oddID does not exist in MLB's registry — proves the
        # league argument is actually being used, not silently ignored.
        mlb_issues = _validate_market_mappings(markets, league="MLB")
        assert len(mlb_issues) == 1

    def test_run_canary_rejects_unavailable_league(self, monkeypatch):
        """No real league is currently unavailable (WNBA became available
        2026-08-19), so this simulates one to prove the rejection path
        still works rather than deleting the test."""
        from src.production_canary import run_canary
        from src.production_config import ProductionConfig
        from src.sports import wnba as wnba_mod
        monkeypatch.setattr(wnba_mod, "AVAILABLE", False)
        monkeypatch.setattr(wnba_mod, "UNAVAILABLE_REASON", "simulated for test")
        result = run_canary(ProductionConfig(api_key="test-key-12345"), league="WNBA")
        assert result.status == "failed"
        assert any("not currently available" in e for e in result.errors)

    def test_run_canary_rejects_unknown_league(self):
        from src.production_canary import run_canary
        from src.production_config import ProductionConfig
        result = run_canary(ProductionConfig(api_key="test-key-12345"), league="NHL")
        assert result.status == "failed"
        assert any("Unknown league" in e for e in result.errors)

    def test_dry_analysis(self):
        from src.production_canary import _dry_analysis
        result = _dry_analysis([{"eventID": "evt1"}])
        assert result["events_analyzed"] == 1
        assert result["consensus_computed"] is False


# ───────────────────────────────────────────────────────────────────
# Delivery Gate
# ───────────────────────────────────────────────────────────────────

class TestDeliveryGate:
    def test_gate_blocked_by_default(self):
        from src.delivery_gate import DeliveryGateState
        state = DeliveryGateState()
        assert state.enabled is False

    def test_gate_state_save_load(self, tmp_path):
        from src.delivery_gate import DeliveryGateState, save_gate_state, load_gate_state, DELIVERY_GATE_FILE
        state = DeliveryGateState(enabled=True, acknowledged_at="2026-07-23")
        with patch("src.delivery_gate.DELIVERY_GATE_FILE", str(tmp_path / "gate.json")):
            save_gate_state(state)
            loaded = load_gate_state()
            assert loaded.enabled is True
            assert loaded.acknowledged_at == "2026-07-23"

    def test_load_gate_state_missing_file(self, tmp_path):
        from src.delivery_gate import load_gate_state
        with patch("src.delivery_gate.DELIVERY_GATE_FILE", str(tmp_path / "nonexistent.json")):
            state = load_gate_state()
            assert state.enabled is False

    def test_gate_checks_all_blocked(self):
        from src.delivery_gate import check_delivery_gate
        from src.production_config import ProductionConfig
        from src.shadow_mode import ShadowConfig
        config = ProductionConfig(discord_webhook_urls="")
        shadow = ShadowConfig(shadow_mode=True, live_delivery_acknowledged=False)
        with patch("src.delivery_gate._check_recent_readiness", return_value=False), \
             patch("src.delivery_gate._check_critical_health", return_value=True), \
             patch("src.delivery_gate._check_critical_data_quality", return_value=True):
            allowed, checks = check_delivery_gate(config, shadow)
            assert allowed is False
            assert any(c.name == "shadow_mode" and not c.passed for c in checks)

    def test_gate_all_passes(self):
        from src.delivery_gate import check_delivery_gate
        from src.production_config import ProductionConfig
        from src.shadow_mode import ShadowConfig
        config = ProductionConfig(discord_webhook_urls="https://hook.test")
        shadow = ShadowConfig(shadow_mode=False, live_delivery_acknowledged=True)
        with patch("src.delivery_gate._check_recent_readiness", return_value=True), \
             patch("src.delivery_gate._check_critical_health", return_value=True), \
             patch("src.delivery_gate._check_critical_data_quality", return_value=True):
            allowed, checks = check_delivery_gate(config, shadow)
            assert allowed is True

    def test_enable_live_delivery_wrong_phrase(self):
        from src.delivery_gate import enable_live_delivery
        from src.production_config import ProductionConfig
        config = ProductionConfig()
        result = enable_live_delivery(config, "WRONG PHRASE")
        assert result["success"] is False

    def test_format_gate_status(self):
        from src.delivery_gate import DeliveryGateCheck, format_gate_status
        checks = [
            DeliveryGateCheck(name="shadow_mode", passed=True, message="OK"),
            DeliveryGateCheck(name="readiness", passed=False, message="Not ready"),
        ]
        output = format_gate_status(False, checks)
        assert "BLOCKED" in output
        assert "[BLOCK]" in output


# ───────────────────────────────────────────────────────────────────
# Shadow Dashboard
# ───────────────────────────────────────────────────────────────────

class TestShadowDashboard:
    def test_dashboard_defaults(self):
        from src.shadow_dashboard import ShadowDashboard
        dash = ShadowDashboard()
        assert dash.shadow_mode is True
        assert dash.total_recommendations == 0

    def test_dashboard_to_dict(self):
        from src.shadow_dashboard import ShadowDashboard
        dash = ShadowDashboard(total_recommendations=5)
        d = dash.to_dict()
        assert d["total_recommendations"] == 5

    def test_format_dashboard(self):
        from src.shadow_dashboard import ShadowDashboard, format_dashboard
        dash = ShadowDashboard(total_recommendations=10, actionable_count=3)
        output = format_dashboard(dash)
        assert "SHADOW-MODE DASHBOARD" in output
        assert "10" in output

    def test_build_dashboard_no_db(self, tmp_path):
        from src.shadow_dashboard import build_dashboard
        from src.production_config import ProductionConfig
        from src.shadow_mode import ShadowConfig
        config = ProductionConfig(database_path=str(tmp_path / "nonexistent.db"))
        shadow = ShadowConfig(shadow_mode=True)
        with patch("src.shadow_dashboard.load_config", return_value=config), \
             patch("src.shadow_dashboard.load_shadow_config", return_value=shadow), \
             patch("src.live_readiness.run_readiness_checks") as mock_ready, \
             patch("src.promotion.check_promotion_criteria") as mock_promo:
            mock_ready.return_value = MagicMock(overall_status="ready")
            mock_promo.return_value = MagicMock(all_passed=False, days_shadow_active=0)
            dash = build_dashboard(config, shadow)
            assert dash.shadow_mode is True


# ───────────────────────────────────────────────────────────────────
# Promotion
# ───────────────────────────────────────────────────────────────────

class TestPromotion:
    def test_promotion_result_defaults(self):
        from src.promotion import PromotionResult
        result = PromotionResult()
        assert result.all_passed is False
        assert result.total_count == 0

    def test_promotion_result_to_dict(self):
        from src.promotion import PromotionResult, PromotionCriterion
        result = PromotionResult(
            all_passed=True,
            met_count=7,
            total_count=7,
            criteria=[PromotionCriterion(name="test", met=True)],
        )
        d = result.to_dict()
        assert d["all_passed"] is True

    def test_format_promotion_result(self):
        from src.promotion import PromotionResult, PromotionCriterion, format_promotion_result
        result = PromotionResult(
            all_passed=False,
            met_count=5,
            total_count=7,
            criteria=[
                PromotionCriterion(name="a", met=True, message="done"),
                PromotionCriterion(name="b", met=False, message="pending"),
            ],
        )
        output = format_promotion_result(result)
        assert "NOT MET" in output
        assert "[PASS]" in output
        assert "[FAIL]" in output

    def test_mark_and_get_shadow_start(self, tmp_path):
        from src.promotion import mark_shadow_start, get_shadow_start_date, SHADOW_START_FILE
        with patch("src.promotion.SHADOW_START_FILE", str(tmp_path / ".shadow_start")):
            mark_shadow_start()
            date_str = get_shadow_start_date()
            assert date_str is not None
            assert len(date_str) > 0

    def test_get_shadow_start_missing(self, tmp_path):
        from src.promotion import get_shadow_start_date
        with patch("src.promotion.SHADOW_START_FILE", str(tmp_path / ".shadow_start")):
            result = get_shadow_start_date()
            assert result is None

    def test_mark_and_check_yn_review(self, tmp_path):
        from src.promotion import mark_yn_review_complete, is_yn_review_complete, YN_REVIEW_FILE
        with patch("src.promotion.YN_REVIEW_FILE", str(tmp_path / ".yn_review")):
            assert is_yn_review_complete() is False
            mark_yn_review_complete()
            assert is_yn_review_complete() is True

    def test_check_job_success_rate_no_data(self):
        from src.promotion import _check_job_success_rate
        from src.production_config import ProductionConfig
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (0, 0)
        with patch("src.promotion.get_connection", return_value=mock_conn):
            result = _check_job_success_rate(ProductionConfig(database_path=":memory:"))
            assert result.met is False

    def test_check_shadow_active_days(self, tmp_path):
        from src.promotion import _check_shadow_active_days
        with patch("src.promotion.SHADOW_START_FILE", str(tmp_path / ".shadow_start")):
            # No file
            result = _check_shadow_active_days()
            assert result.met is False

            # Write a start date 20 days ago
            old_date = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
            Path(str(tmp_path / ".shadow_start")).write_text(old_date)
            result = _check_shadow_active_days()
            assert result.met is True

    def test_check_verified_backups_none(self, tmp_path):
        from src.promotion import _check_verified_backups
        from src.production_config import ProductionConfig
        config = ProductionConfig(output_dir=str(tmp_path))
        result = _check_verified_backups(config)
        assert result.met is False


# ───────────────────────────────────────────────────────────────────
# Manual Checklist
# ───────────────────────────────────────────────────────────────────

class TestManualChecklist:
    def test_load_checklist_creates_defaults(self, tmp_path):
        from src.manual_checklist import load_checklist, CHECKLIST_FILE
        with patch("src.manual_checklist.CHECKLIST_FILE", str(tmp_path / "checklist.json")):
            items = load_checklist()
            assert len(items) > 0
            assert items[0].id != ""

    def test_load_checklist_from_file(self, tmp_path):
        from src.manual_checklist import load_checklist, ChecklistItem, CHECKLIST_FILE
        items = [ChecklistItem(id="test-1", category="test", title="Test Item")]
        with patch("src.manual_checklist.CHECKLIST_FILE", str(tmp_path / "checklist.json")):
            from src.manual_checklist import save_checklist
            save_checklist(items)
            loaded = load_checklist()
            assert len(loaded) == 1
            assert loaded[0].id == "test-1"

    def test_complete_item(self, tmp_path):
        from src.manual_checklist import load_checklist, complete_item, CHECKLIST_FILE
        with patch("src.manual_checklist.CHECKLIST_FILE", str(tmp_path / "checklist.json")):
            items = load_checklist()
            first_id = items[0].id
            result = complete_item(first_id, notes="verified")
            assert result is True
            loaded = load_checklist()
            assert any(i.id == first_id and i.completed for i in loaded)

    def test_complete_nonexistent_item(self, tmp_path):
        from src.manual_checklist import load_checklist, complete_item, CHECKLIST_FILE
        with patch("src.manual_checklist.CHECKLIST_FILE", str(tmp_path / "checklist.json")):
            load_checklist()
            result = complete_item("nonexistent")
            assert result is False

    def test_uncomplete_item(self, tmp_path):
        from src.manual_checklist import load_checklist, complete_item, uncomplete_item, CHECKLIST_FILE
        with patch("src.manual_checklist.CHECKLIST_FILE", str(tmp_path / "checklist.json")):
            items = load_checklist()
            first_id = items[0].id
            complete_item(first_id)
            result = uncomplete_item(first_id)
            assert result is True
            loaded = load_checklist()
            assert any(i.id == first_id and not i.completed for i in loaded)

    def test_get_checklist_status(self, tmp_path):
        from src.manual_checklist import get_checklist_status, load_checklist, complete_item, CHECKLIST_FILE
        with patch("src.manual_checklist.CHECKLIST_FILE", str(tmp_path / "checklist.json")):
            load_checklist()
            status = get_checklist_status()
            assert "all_required_met" in status
            assert "required_total" in status
            assert status["required_total"] > 0

    def test_format_checklist(self, tmp_path):
        from src.manual_checklist import format_checklist, load_checklist, CHECKLIST_FILE
        with patch("src.manual_checklist.CHECKLIST_FILE", str(tmp_path / "checklist.json")):
            load_checklist()
            output = format_checklist()
            assert "Pre-Live Verification" in output

    def test_default_checklist_has_categories(self):
        from src.manual_checklist import DEFAULT_CHECKLIST
        categories = {item["category"] for item in DEFAULT_CHECKLIST}
        assert "delivery_safety" in categories
        assert "data_quality" in categories
        assert "yn_markets" in categories
        assert "system" in categories
        assert "monitoring" in categories

    def test_checklist_item_to_dict(self):
        from src.manual_checklist import ChecklistItem
        item = ChecklistItem(id="x", category="test", title="T", completed=True)
        d = item.to_dict()
        assert d["id"] == "x"
        assert d["completed"] is True
