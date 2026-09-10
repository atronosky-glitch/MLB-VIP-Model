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


class TestFormatEventStartLocal:
    """2026-09-09 (operator request): arbitrage/middle opportunity cards
    and tables show when the game starts, since both legs have to be
    placed before kickoff for the guaranteed math to hold."""

    def test_converts_utc_iso_to_configured_local_timezone(self, monkeypatch):
        from database.db_manager import format_event_start_local

        monkeypatch.setenv("MLB_SCHEDULER_TIMEZONE", "America/New_York")
        # 2026-09-10 00:10 UTC is 2026-09-09 20:10 EDT.
        assert format_event_start_local("2026-09-10T00:10:00Z") == "Sep 9, 8:10 PM EDT"

    def test_accepts_offset_form_not_just_z_suffix(self, monkeypatch):
        from database.db_manager import format_event_start_local

        monkeypatch.setenv("MLB_SCHEDULER_TIMEZONE", "America/New_York")
        assert format_event_start_local("2026-09-09T17:05:00+00:00") == "Sep 9, 1:05 PM EDT"

    def test_respects_mlb_timezone_env_override(self, monkeypatch):
        from database.db_manager import format_event_start_local

        monkeypatch.delenv("MLB_SCHEDULER_TIMEZONE", raising=False)
        monkeypatch.setenv("MLB_TIMEZONE", "UTC")
        assert format_event_start_local("2026-09-10T00:10:00Z") == "Sep 10, 12:10 AM UTC"

    def test_missing_timestamp_is_time_tbd_not_an_error(self):
        from database.db_manager import format_event_start_local

        assert format_event_start_local(None) == "Time TBD"
        assert format_event_start_local("") == "Time TBD"

    def test_unparseable_timestamp_is_time_tbd_not_an_error(self):
        from database.db_manager import format_event_start_local

        assert format_event_start_local("not-a-timestamp") == "Time TBD"

    def test_midnight_hour_displays_as_twelve_not_zero(self, monkeypatch):
        """Regression guard for the hour12 = local.hour % 12 or 12 logic --
        naive '% 12' alone would render midnight as '0:xx AM'."""
        from database.db_manager import format_event_start_local

        monkeypatch.setenv("MLB_SCHEDULER_TIMEZONE", "UTC")
        assert format_event_start_local("2026-09-10T00:00:00Z") == "Sep 10, 12:00 AM UTC"


class TestIsEventLive:
    """2026-09-10 (operator request): a Pregame/Live dropdown on the
    Arbitrage/Middling pages needs to classify each opportunity by
    whether its game has already started."""

    def test_future_start_time_is_not_live(self):
        from datetime import datetime, timedelta, timezone
        from database.db_manager import is_event_live

        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert is_event_live(future) is False

    def test_past_start_time_is_live(self):
        from datetime import datetime, timedelta, timezone
        from database.db_manager import is_event_live

        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        assert is_event_live(past) is True

    def test_accepts_z_suffix_and_offset_form(self):
        from datetime import datetime, timedelta, timezone
        from database.db_manager import is_event_live

        past_z = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        assert is_event_live(past_z) is True

    def test_missing_timestamp_is_not_live(self):
        """Unknown game time must default to "not live" (pregame bucket)
        so it stays visible in the default Pregame view instead of
        silently disappearing."""
        from database.db_manager import is_event_live

        assert is_event_live(None) is False
        assert is_event_live("") is False

    def test_unparseable_timestamp_is_not_live(self):
        from database.db_manager import is_event_live

        assert is_event_live("not-a-timestamp") is False

    def test_naive_datetime_is_treated_as_utc(self):
        """A timestamp with no timezone info must be treated as UTC, same
        convention format_event_start_local already uses -- comparing a
        naive datetime against an aware one raises TypeError otherwise."""
        from datetime import datetime, timedelta, timezone
        from database.db_manager import is_event_live

        naive_past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        assert is_event_live(naive_past) is True


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
