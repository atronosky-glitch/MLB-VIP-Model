"""Phase 11 tests: Shadow mode, API usage, data quality, audit trail.

All tests are deterministic, use mocks/fixtures, no live APIs.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest


# ───────────────────────────────────────────────────────────────────
# Shadow Mode
# ───────────────────────────────────────────────────────────────────

class TestShadowMode:
    def test_default_shadow_config_is_blocked(self):
        from src.shadow_mode import ShadowConfig
        sc = ShadowConfig()
        assert sc.is_delivery_blocked() is True
        assert sc.shadow_mode is True

    def test_shadow_config_can_enable_delivery(self):
        from src.shadow_mode import ShadowConfig
        sc = ShadowConfig(shadow_mode=False, live_delivery_acknowledged=True)
        assert sc.can_enable_delivery() is True
        assert sc.is_delivery_blocked() is False

    def test_shadow_config_block_reasons(self):
        from src.shadow_mode import ShadowConfig
        sc = ShadowConfig()
        reasons = sc.block_reasons()
        assert len(reasons) == 2
        assert any("SHADOW_MODE=true" in r for r in reasons)

    def test_shadow_config_single_block_reason(self):
        from src.shadow_mode import ShadowConfig
        sc = ShadowConfig(shadow_mode=True, live_delivery_acknowledged=True)
        reasons = sc.block_reasons()
        assert len(reasons) == 1

    def test_load_shadow_config_defaults(self):
        from src.shadow_mode import load_shadow_config
        with patch.dict(os.environ, {}, clear=True):
            sc = load_shadow_config()
            assert sc.shadow_mode is True
            assert sc.live_delivery_acknowledged is False

    def test_load_shadow_config_env_override(self):
        from src.shadow_mode import load_shadow_config
        with patch.dict(os.environ, {"MLB_SHADOW_MODE": "false", "MLB_LIVE_DELIVERY_ACKNOWLEDGED": "true"}):
            sc = load_shadow_config()
            assert sc.shadow_mode is False
            assert sc.live_delivery_acknowledged is True

    def test_load_shadow_config_file(self, tmp_path):
        from src.shadow_mode import load_shadow_config
        cfg = tmp_path / "shadow.json"
        cfg.write_text(json.dumps({"shadow_mode": False}))
        sc = load_shadow_config(str(cfg))
        assert sc.shadow_mode is False

    def test_save_and_load_shadow_config(self, tmp_path):
        from src.shadow_mode import ShadowConfig, save_shadow_config, load_shadow_config
        sc = ShadowConfig(shadow_mode=False, live_delivery_acknowledged=True)
        path = tmp_path / "shadow.json"
        save_shadow_config(sc, str(path))
        loaded = load_shadow_config(str(path))
        assert loaded.shadow_mode is False
        assert loaded.live_delivery_acknowledged is True

    def test_env_takes_priority_over_file(self, tmp_path):
        from src.shadow_mode import load_shadow_config
        cfg = tmp_path / "shadow.json"
        cfg.write_text(json.dumps({"shadow_mode": True}))
        with patch.dict(os.environ, {"MLB_SHADOW_MODE": "false"}):
            sc = load_shadow_config(str(cfg))
            assert sc.shadow_mode is False


# ───────────────────────────────────────────────────────────────────
# API Usage
# ───────────────────────────────────────────────────────────────────

class TestApiUsage:
    def _init_db(self, conn):
        from src.api_usage import init_usage_table
        init_usage_table(conn)

    def test_init_usage_table(self):
        conn = sqlite3.connect(":memory:")
        self._init_db(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "api_usage" in tables
        conn.close()

    def test_record_api_usage(self):
        from src.api_usage import ApiUsageRecord, record_api_usage, init_usage_table
        conn = sqlite3.connect(":memory:")
        init_usage_table(conn)
        rec = ApiUsageRecord(
            endpoint="/EventOdds",
            job_type="morning-run",
            run_id="test-run",
            cache_hit=False,
            http_status=200,
            response_time_ms=150.0,
            event_count=5,
            market_count=20,
        )
        record_api_usage(conn, rec)
        row = conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()
        assert row[0] == 1
        conn.close()

    def test_get_usage_summary_empty(self):
        from src.api_usage import get_usage_summary, init_usage_table
        conn = sqlite3.connect(":memory:")
        init_usage_table(conn)
        summary = get_usage_summary(conn)
        assert summary.total_requests == 0
        conn.close()

    def test_get_usage_summary_with_data(self):
        from src.api_usage import ApiUsageRecord, record_api_usage, get_usage_summary, init_usage_table
        conn = sqlite3.connect(":memory:")
        init_usage_table(conn)
        for i in range(5):
            record_api_usage(conn, ApiUsageRecord(
                endpoint="/EventOdds",
                job_type="morning-run",
                cache_hit=i % 2 == 0,
                http_status=200,
                event_count=10,
                market_count=30,
                estimated_quota_usage=1.0,
            ))
        summary = get_usage_summary(conn)
        assert summary.total_requests == 5
        assert summary.live_requests == 2
        assert summary.cache_hits == 3
        assert summary.total_events == 50
        conn.close()

    def test_check_quota_warning_below(self):
        from src.api_usage import check_quota_warning, ApiUsageRecord, record_api_usage, init_usage_table
        conn = sqlite3.connect(":memory:")
        init_usage_table(conn)
        record_api_usage(conn, ApiUsageRecord(estimated_quota_usage=10.0))
        result = check_quota_warning(conn, daily_limit=1000.0)
        assert result is None
        conn.close()

    def test_check_quota_warning_above(self):
        from src.api_usage import check_quota_warning, ApiUsageRecord, record_api_usage, init_usage_table
        conn = sqlite3.connect(":memory:")
        init_usage_table(conn)
        record_api_usage(conn, ApiUsageRecord(estimated_quota_usage=900.0))
        result = check_quota_warning(conn, daily_limit=1000.0)
        assert result is not None
        assert result["pct"] == 90.0
        conn.close()

    def test_format_usage_report(self):
        from src.api_usage import ApiUsageSummary, format_usage_report
        summary = ApiUsageSummary(
            period="2026-07-23",
            total_requests=10,
            live_requests=5,
            cache_hits=5,
            cache_hit_rate=50.0,
        )
        report = format_usage_report(summary)
        assert "2026-07-23" in report
        assert "10" in report

    def test_usage_by_job_filter(self):
        from src.api_usage import ApiUsageRecord, record_api_usage, get_usage_summary, init_usage_table
        conn = sqlite3.connect(":memory:")
        init_usage_table(conn)
        record_api_usage(conn, ApiUsageRecord(job_type="morning-run"))
        record_api_usage(conn, ApiUsageRecord(job_type="pregame-run"))
        summary = get_usage_summary(conn, job_filter="morning-run")
        assert summary.total_requests == 1
        conn.close()


# ───────────────────────────────────────────────────────────────────
# Data Quality
# ───────────────────────────────────────────────────────────────────

class TestDataQuality:
    def test_finding_default_timestamp(self):
        from src.data_quality import DataQualityFinding
        f = DataQualityFinding(check_name="test", message="ok")
        assert f.timestamp != ""
        assert f.finding_id != ""

    def test_report_add_counts(self):
        from src.data_quality import DataQualityReport, DataQualityFinding, SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL
        report = DataQualityReport()
        report.add(DataQualityFinding(severity=SEVERITY_INFO))
        report.add(DataQualityFinding(severity=SEVERITY_WARNING))
        report.add(DataQualityFinding(severity=SEVERITY_CRITICAL))
        assert report.total_checks == 3
        assert report.info_count == 1
        assert report.warning_count == 1
        assert report.critical_count == 1
        assert report.has_critical is True

    def test_sportsbook_count_drop_warning(self):
        from src.data_quality import check_sportsbook_count
        f = check_sportsbook_count(7, 10, drop_threshold_pct=30.0)
        assert f is not None
        assert f.severity == "WARNING"

    def test_sportsbook_count_drop_critical(self):
        from src.data_quality import check_sportsbook_count
        f = check_sportsbook_count(3, 10, drop_threshold_pct=30.0)
        assert f is not None
        assert f.severity == "CRITICAL"

    def test_sportsbook_count_no_drop(self):
        from src.data_quality import check_sportsbook_count
        f = check_sportsbook_count(9, 10, drop_threshold_pct=30.0)
        assert f is None

    def test_market_count_drop(self):
        from src.data_quality import check_market_count
        f = check_market_count(5, 10, drop_threshold_pct=30.0)
        assert f is not None

    def test_missing_major_sportsbooks(self):
        from src.data_quality import check_missing_major_sportsbooks
        f = check_missing_major_sportsbooks({"DraftKings", "FanDuel"})
        assert f is not None
        assert "BetMGM" in f.message

    def test_no_missing_sportsbooks(self):
        from src.data_quality import check_missing_major_sportsbooks
        f = check_missing_major_sportsbooks({"DraftKings", "FanDuel", "BetMGM", "Caesars"})
        assert f is None

    def test_unsupported_market(self):
        from src.data_quality import check_unsupported_market
        f = check_unsupported_market({"new_market", "test"}, {"existing"})
        assert f is not None

    def test_inverted_odds(self):
        from src.data_quality import check_inverted_odds
        f = check_inverted_odds([{"over_price": -110, "under_price": -150}])
        assert f is not None

    def test_no_inverted_odds(self):
        from src.data_quality import check_inverted_odds
        f = check_inverted_odds([{"over_price": -150, "under_price": -110}])
        assert f is None

    def test_impossible_prices(self):
        from src.data_quality import check_impossible_prices
        f = check_impossible_prices([{"american_odds": 50}])
        assert f is not None

    def test_one_sided_market(self):
        from src.data_quality import check_one_sided_market
        f = check_one_sided_market([{"over_price": -110, "under_price": None}])
        assert f is not None

    def test_volume_spike(self):
        from src.data_quality import check_volume_spike
        f = check_volume_spike(100, 20, spike_threshold_pct=200.0)
        assert f is not None

    def test_volume_collapse(self):
        from src.data_quality import check_volume_collapse
        f = check_volume_collapse(2, 100, collapse_threshold_pct=70.0)
        assert f is not None

    def test_extreme_consensus_disagreement(self):
        from src.data_quality import check_extreme_consensus_disagreement
        f = check_extreme_consensus_disagreement(0.20)
        assert f is not None

    def test_duplicate_api_objects(self):
        from src.data_quality import check_duplicate_api_objects
        records = [
            {"odd_id": "1", "sportsbook": "DK"},
            {"odd_id": "1", "sportsbook": "DK"},
        ]
        f = check_duplicate_api_objects(records)
        assert f is not None

    def test_stale_observations(self):
        from src.data_quality import check_stale_observations
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        f = check_stale_observations([{"observation_timestamp": old_ts}], stale_threshold_seconds=3600)
        assert f is not None

    def test_init_findings_table(self):
        from src.data_quality import init_findings_table
        conn = sqlite3.connect(":memory:")
        init_findings_table(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "data_quality_findings" in tables
        conn.close()

    def test_persist_and_get_critical_findings(self):
        from src.data_quality import (
            init_findings_table, persist_finding, get_critical_findings,
            DataQualityFinding, SEVERITY_CRITICAL,
        )
        conn = sqlite3.connect(":memory:")
        init_findings_table(conn)
        f = DataQualityFinding(check_name="test", severity=SEVERITY_CRITICAL, message="critical issue")
        persist_finding(conn, f)
        findings = get_critical_findings(conn, since_hours=1)
        assert len(findings) == 1
        assert findings[0]["severity"] == "CRITICAL"
        conn.close()


# ───────────────────────────────────────────────────────────────────
# Audit Trail
# ───────────────────────────────────────────────────────────────────

class TestAuditTrail:
    def test_trace_step_default(self):
        from src.audit_trail import TraceStep
        step = TraceStep(recommendation_id="rec-1", step_name="api_request", status="ok")
        assert step.step_id != ""
        assert step.timestamp != ""

    def test_recommendation_trace_human_readable(self):
        from src.audit_trail import RecommendationTrace, TraceStep
        trace = RecommendationTrace(
            recommendation_id="rec-123",
            player_name="Test Player",
            market_type="strikeouts",
            sportsbook="DraftKings",
            steps=[
                TraceStep(step_name="api_request", status="ok"),
                TraceStep(step_name="scan", status="ok"),
            ],
        )
        output = trace.to_human_readable()
        assert "rec-123" in output
        assert "Test Player" in output
        assert "(OK)" in output

    def test_init_trace_table(self):
        from src.audit_trail import init_trace_table
        conn = sqlite3.connect(":memory:")
        init_trace_table(conn)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "recommendation_traces" in tables
        conn.close()

    def test_record_and_get_trace(self):
        from src.audit_trail import (
            init_trace_table, record_trace_step, get_recommendation_trace,
            TraceStep,
        )
        conn = sqlite3.connect(":memory:")
        init_trace_table(conn)

        # Insert a fake recommendation
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recommendations (
                recommendation_id TEXT PRIMARY KEY,
                player_name TEXT,
                market_type TEXT,
                sportsbook TEXT,
                observation_timestamp TEXT
            )
        """)
        conn.execute(
            "INSERT INTO recommendations VALUES (?, ?, ?, ?, ?)",
            ("rec-1", "Player", "strikeouts", "DK", "2026-07-23T10:00:00Z"),
        )
        conn.commit()

        step = TraceStep(recommendation_id="rec-1", step_name="api_request", status="ok")
        record_trace_step(conn, step)

        trace = get_recommendation_trace(conn, "rec-1")
        assert trace is not None
        assert trace.recommendation_id == "rec-1"
        assert len(trace.steps) == 1
        conn.close()

    def test_get_trace_nonexistent(self):
        from src.audit_trail import init_trace_table, get_recommendation_trace
        conn = sqlite3.connect(":memory:")
        init_trace_table(conn)
        result = get_recommendation_trace(conn, "nonexistent")
        assert result is None
        conn.close()

    def test_redact_secrets(self):
        from src.audit_trail import RecommendationTrace, TraceStep, redact_secrets
        trace = RecommendationTrace(
            recommendation_id="rec-1",
            steps=[
                TraceStep(step_name="api_request", details={"api_key": "sk-1234567890"}),
            ],
        )
        redacted = redact_secrets(trace)
        assert redacted.steps[0].details["api_key"] != "sk-1234567890"
        assert "***" in redacted.steps[0].details["api_key"]

    def test_lifecycle_recorders(self):
        from src.audit_trail import (
            init_trace_table, record_api_request, record_ingestion,
            record_scan, record_recommendation, record_confidence,
            record_delivery_decision, record_delivery_attempt,
            record_closing_price, record_settlement,
        )
        conn = sqlite3.connect(":memory:")
        init_trace_table(conn)

        s1 = record_api_request(conn, "r1", "/EventOdds")
        s2 = record_ingestion(conn, "r1")
        s3 = record_scan(conn, "r1")
        s4 = record_recommendation(conn, "r1")
        s5 = record_confidence(conn, "r1", 75.0)
        s6 = record_delivery_decision(conn, "r1", "deliver")
        s7 = record_delivery_attempt(conn, "r1", "discord")
        s8 = record_closing_price(conn, "r1")
        s9 = record_settlement(conn, "r1", "win")

        rows = conn.execute("SELECT COUNT(*) FROM recommendation_traces").fetchone()
        assert rows[0] == 9
        conn.close()

    def test_record_trace_steps_batch(self):
        from src.audit_trail import init_trace_table, record_trace_steps, TraceStep
        conn = sqlite3.connect(":memory:")
        init_trace_table(conn)
        steps = [
            TraceStep(recommendation_id="r1", step_name="a", status="ok"),
            TraceStep(recommendation_id="r1", step_name="b", status="ok"),
        ]
        record_trace_steps(conn, steps)
        rows = conn.execute("SELECT COUNT(*) FROM recommendation_traces").fetchone()
        assert rows[0] == 2
        conn.close()
