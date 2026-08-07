"""Regression tests for quote-time freshness rather than scan-time freshness."""

from datetime import datetime, timedelta, timezone

from src.daily_pipeline import _freshness_for_observation


def test_recent_quote_is_fresh():
    now = datetime.now(timezone.utc)
    observed = (now - timedelta(minutes=5)).isoformat()
    assert _freshness_for_observation(observed, now=now) == "FRESH"


def test_old_quote_is_stale_even_when_scan_is_current():
    now = datetime.now(timezone.utc)
    observed = (now - timedelta(hours=2)).isoformat()
    assert _freshness_for_observation(observed, now=now) == "STALE"


def test_missing_or_invalid_quote_time_is_stale():
    assert _freshness_for_observation(None) == "STALE"
    assert _freshness_for_observation("not-a-timestamp") == "STALE"
