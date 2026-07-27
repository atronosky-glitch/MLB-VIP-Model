"""Tests for Phase 10 Part J: Structured Logging, Part E: Message Formatting."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest


class TestStructuredLogging:

    def test_json_formatter(self):
        from src.structured_logging import JSONFormatter
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["message"] == "test message"
        assert "ts" in data

    def test_json_formatter_with_exception(self):
        from src.structured_logging import JSONFormatter
        formatter = JSONFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg="error occurred", args=(), exc_info=exc_info,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert "exception" in data
        assert "ValueError" in data["exception"]

    def test_json_formatter_with_job_id(self):
        from src.structured_logging import JSONFormatter
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="job log", args=(), exc_info=None,
        )
        record.job_id = "abc-123"
        output = formatter.format(record)
        data = json.loads(output)
        assert data["job_id"] == "abc-123"

    def test_human_formatter(self):
        from src.structured_logging import HumanFormatter
        formatter = HumanFormatter()
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="warn msg", args=(), exc_info=None,
        )
        output = formatter.format(record)
        assert "WARNING" in output
        assert "warn msg" in output

    def test_job_context_filter_no_id(self):
        from src.structured_logging import JobContextFilter
        f = JobContextFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        result = f.filter(record)
        assert result is True
        assert not hasattr(record, "job_id")

    def test_job_context_filter_with_id(self):
        from src.structured_logging import JobContextFilter
        f = JobContextFilter()
        f.job_id = "job-xyz"
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="test", args=(), exc_info=None,
        )
        f.filter(record)
        assert record.job_id == "job-xyz"

    def test_set_job_context(self):
        from src.structured_logging import set_job_context, _job_filter
        set_job_context("test-job-1")
        assert _job_filter.job_id == "test-job-1"
        set_job_context(None)
        assert _job_filter.job_id is None

    def test_setup_logging_human(self):
        from src.structured_logging import setup_logging
        setup_logging(level="DEBUG", fmt="human")
        root = logging.getLogger()
        assert len(root.handlers) >= 1
        for h in root.handlers[:]:
            root.removeHandler(h)

    def test_setup_logging_json(self):
        from src.structured_logging import setup_logging
        setup_logging(level="WARNING", fmt="json")
        root = logging.getLogger()
        assert len(root.handlers) >= 1
        for h in root.handlers[:]:
            root.removeHandler(h)


class TestMessageFormatting:

    def test_format_recommendation_ou(self):
        from src.message_formatter import format_recommendation
        rec = {
            "player_name": "Aaron Judge",
            "event_name": "NYY vs BOS",
            "market_type": "strikeouts",
            "sportsbook": "DraftKings",
            "offered_american_odds": -110,
            "line": "Over 0.5",
            "side": "Over",
            "period": "game",
            "ev_pct": 3.5,
            "confidence_score": 72,
            "rec_status": "BET",
            "recommendation_fingerprint": "abcdef1234567890",
        }
        msg = format_recommendation(rec)
        assert "Aaron Judge" in msg
        assert "DraftKings" in msg
        assert "-110" in msg
        assert "3.5" in msg
        assert "72" in msg
        assert "BET" in msg

    def test_format_recommendation_yn(self):
        from src.message_formatter import format_recommendation
        rec = {
            "player_name": "Shohei Ohtani",
            "event_name": "LAD vs SF",
            "market_type": "batting_homeRuns",
            "sportsbook": "FanDuel",
            "offered_american_odds": 350,
            "price_advantage_pct": 6.2,
            "rec_status": "LEAN",
        }
        msg = format_recommendation(rec)
        assert "Ohtani" in msg
        assert "350" in msg
        assert "6.2" in msg

    def test_format_recommendation_minimal(self):
        from src.message_formatter import format_recommendation
        rec = {"player_name": "Test Player"}
        msg = format_recommendation(rec)
        assert "Test Player" in msg

    def test_format_daily_summary_empty(self):
        from src.message_formatter import format_daily_summary
        msg = format_daily_summary([], date_label="2025-07-23")
        assert "Daily Summary" in msg
        assert "No actionable" in msg

    def test_format_daily_summary_with_stats(self):
        from src.message_formatter import format_daily_summary
        stats = {"total_scanned": 100, "total_recommended": 5, "strong_edges": 2}
        msg = format_daily_summary([], stats=stats, date_label="2025-07-23")
        assert "100" in msg
        assert "5" in msg

    def test_format_daily_summary_groups_by_status(self):
        from src.message_formatter import format_daily_summary
        recs = [
            {"player_name": "P1", "rec_status": "BET", "market_type": "strikeouts",
             "sportsbook": "DK", "offered_american_odds": -110},
            {"player_name": "P2", "rec_status": "LEAN", "market_type": "hits",
             "sportsbook": "FD", "offered_american_odds": 150},
            {"player_name": "P3", "rec_status": "MONITOR", "market_type": "rbi",
             "sportsbook": "B365", "offered_american_odds": -105},
        ]
        msg = format_daily_summary(recs)
        assert "BET (1)" in msg
        assert "LEAN (1)" in msg
        assert "MONITOR (1)" in msg

    def test_format_daily_summary_truncates_monitor(self):
        from src.message_formatter import format_daily_summary
        recs = [
            {"player_name": f"P{i}", "rec_status": "MONITOR", "market_type": "hits",
             "sportsbook": "DK", "offered_american_odds": -110}
            for i in range(10)
        ]
        msg = format_daily_summary(recs)
        assert "...and 5 more" in msg

    def test_chunk_message_single(self):
        from src.message_formatter import chunk_message
        chunks = chunk_message("short message")
        assert len(chunks) == 1
        assert chunks[0] == "short message"

    def test_chunk_message_multiple(self):
        from src.message_formatter import chunk_message
        long_msg = "line\n" * 500
        chunks = chunk_message(long_msg, max_length=1000)
        assert len(chunks) > 1

    def test_chunk_message_continuation_markers(self):
        from src.message_formatter import chunk_message
        long_msg = "A" * 200 + "\n" + "B" * 200
        chunks = chunk_message(long_msg, max_length=150)
        if len(chunks) > 1:
            assert "Part 1/" in chunks[0]
            assert "Part 2/" in chunks[1]

    def test_format_for_discord(self):
        from src.message_formatter import format_for_discord
        recs = [{"player_name": "Test", "rec_status": "BET", "market_type": "strikeouts",
                 "sportsbook": "DK", "offered_american_odds": -110}]
        msg = format_for_discord(recs)
        assert msg.channel == "discord"
        assert msg.chunk_count >= 1
        assert msg.total_chars > 0

    def test_format_for_slack(self):
        from src.message_formatter import format_for_slack
        recs = [{"player_name": "Test", "rec_status": "BET", "market_type": "strikeouts",
                 "sportsbook": "DK", "offered_american_odds": -110}]
        msg = format_for_slack(recs)
        assert msg.channel == "slack"
        assert msg.chunk_count == 1

    def test_compact_line_with_ev(self):
        from src.message_formatter import _compact_line
        rec = {"player_name": "Judge", "market_type": "strikeouts", "sportsbook": "DK",
               "offered_american_odds": -110, "ev_pct": 5.2}
        line = _compact_line(rec)
        assert "Judge" in line
        assert "5.2" in line

    def test_compact_line_with_price_advantage(self):
        from src.message_formatter import _compact_line
        rec = {"player_name": "Ohtani", "market_type": "home_runs", "sportsbook": "FD",
               "offered_american_odds": 350, "price_advantage_pct": 4.1}
        line = _compact_line(rec)
        assert "Ohtani" in line
        assert "4.1" in line

    def test_compact_line_positive_odds(self):
        from src.message_formatter import _compact_line
        rec = {"player_name": "P1", "market_type": "hits", "sportsbook": "DK",
               "offered_american_odds": 150}
        line = _compact_line(rec)
        assert "+150" in line

    def test_confidence_labels(self):
        from src.message_formatter import _confidence_label
        assert _confidence_label(90) == "Very High"
        assert _confidence_label(70) == "High"
        assert _confidence_label(50) == "Medium"
        assert _confidence_label(30) == "Low"
        assert _confidence_label(10) == "Very Low"
