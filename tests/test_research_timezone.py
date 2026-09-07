"""Regression tests for the 2026-09-06 fix to the customer-facing
"Today's Research" timezone bug.

Before this fix, ``database.db_manager.get_research_picks_today`` and
``src.customer_view.py``'s own research query both filtered with
``date(scan_timestamp) = date('now')`` — a UTC calendar day. Since MLB
games run into the evening, any viewer between roughly 8:00 PM and
midnight Eastern would see the list computed against tomorrow's
(mostly empty) UTC date instead of the rest of today's real research
picks. Official Picks' own "today" (the daily cap in
``src/daily_pipeline.py``, freshness elsewhere) is deliberately left on
the UTC-day boundary and is not touched by this fix.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest


class TestGetTodayInConfiguredTimezone:
    def test_returns_eastern_calendar_day_across_the_utc_midnight_boundary(self):
        """2026-09-06 02:30 UTC is still 2026-09-05 22:30 in America/New_York
        — exactly the window where the old date('now') (UTC) comparison
        would have shown "tomorrow" while it was still evening "today" on
        the US east coast."""
        from database.db_manager import get_today_in_configured_timezone

        fixed_utc = datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc)

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc.astimezone(tz) if tz else fixed_utc

        with patch("database.db_manager.datetime", _FixedDatetime):
            result = get_today_in_configured_timezone()

        assert result == "2026-09-05"

    def test_matches_utc_day_outside_the_boundary_window(self):
        """Sanity check: mid-afternoon UTC is the same calendar day in both
        UTC and Eastern, so the fix shouldn't shift "today" every day —
        only across the specific evening boundary window."""
        from database.db_manager import get_today_in_configured_timezone

        fixed_utc = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc.astimezone(tz) if tz else fixed_utc

        with patch("database.db_manager.datetime", _FixedDatetime):
            result = get_today_in_configured_timezone()

        assert result == "2026-09-06"

    def test_respects_mlb_timezone_env_override(self, monkeypatch):
        from database.db_manager import get_today_in_configured_timezone

        monkeypatch.delenv("MLB_SCHEDULER_TIMEZONE", raising=False)
        monkeypatch.setenv("MLB_TIMEZONE", "UTC")

        fixed_utc = datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc)

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc.astimezone(tz) if tz else fixed_utc

        with patch("database.db_manager.datetime", _FixedDatetime):
            result = get_today_in_configured_timezone()

        # With the timezone forced to UTC, "today" is the UTC day again.
        assert result == "2026-09-06"

    def test_scheduler_timezone_env_takes_priority_over_mlb_timezone(self, monkeypatch):
        from database.db_manager import get_today_in_configured_timezone

        monkeypatch.setenv("MLB_TIMEZONE", "UTC")
        monkeypatch.setenv("MLB_SCHEDULER_TIMEZONE", "America/New_York")

        fixed_utc = datetime(2026, 9, 6, 2, 30, tzinfo=timezone.utc)

        class _FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_utc.astimezone(tz) if tz else fixed_utc

        with patch("database.db_manager.datetime", _FixedDatetime):
            result = get_today_in_configured_timezone()

        assert result == "2026-09-05"


class TestGetResearchPicksTodayUsesConfiguredTimezone:
    def test_filters_by_the_configured_timezone_day_not_a_hardcoded_utc_day(self, db_conn):
        """Integration check: get_research_picks_today must actually use
        get_today_in_configured_timezone's value in its WHERE clause, not
        some other hardcoded date."""
        import database.db_manager as dbm
        from database.db_manager import save_recommendation, get_research_picks_today

        rec_today = {
            "event_id": "E1", "player_id": "P1", "player_name": "Judge",
            "market_type": "strikeouts", "market_form": "ou", "period": "game",
            "line": 6.5, "side": "OVER", "sportsbook": "DraftKings",
            "offered_american_odds": -110, "offered_decimal_odds": 1.909,
            "offered_implied_prob": 0.524, "fair_prob": 0.55, "ev_pct": 5.0,
            "n_consensus_books": 6, "market_quality": "VALID_MARKET",
            "rec_status": "QUALIFIED", "rec_eligible": 1,
            "scan_timestamp": "2026-09-05T22:30:00+00:00",
            "recommendation_tier": "RESEARCH_ONLY", "qualification_passed": 0,
            "league": "MLB", "sport": "baseball",
        }
        rec_other_day = dict(rec_today, event_id="E2", player_id="P2",
                              scan_timestamp="2026-09-06T15:00:00+00:00")
        save_recommendation(db_conn, rec_today)
        save_recommendation(db_conn, rec_other_day)

        with patch.object(dbm, "get_today_in_configured_timezone", return_value="2026-09-05"):
            rows = get_research_picks_today(db_conn)

        event_ids = {r["event_id"] for r in rows}
        assert "E1" in event_ids
        assert "E2" not in event_ids
