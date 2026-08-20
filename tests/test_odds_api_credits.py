"""Tests for src/odds_api_credits.py — WNBA (The Odds API) credit
tracking and budget control. No live API calls; all usage rows are
inserted directly, mirroring what record_client_quota persists from a
real response's headers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.odds_api_credits import (
    record_credit_usage,
    record_client_quota,
    get_latest_credit_status,
    get_usage_this_month,
    estimate_monthly_cost,
    credit_budget_check,
    DEFAULT_MONTHLY_BUDGET,
    GAME_ODDS_COST,
    PROPS_COST_PER_EVENT,
)


class TestRecordCreditUsage:
    def test_persists_a_row(self, db_conn):
        record_credit_usage(
            db_conn, endpoint="odds", job_type="game_odds",
            requests_used=10, requests_remaining=490, requests_last=3,
        )
        row = db_conn.execute("SELECT * FROM odds_api_credits").fetchone()
        assert row["endpoint"] == "odds"
        assert row["requests_remaining"] == 490
        assert row["cache_hit"] == 0

    def test_cache_hit_flag_stored(self, db_conn):
        record_credit_usage(db_conn, endpoint="odds", cache_hit=True)
        row = db_conn.execute("SELECT cache_hit FROM odds_api_credits").fetchone()
        assert row["cache_hit"] == 1


class TestRecordClientQuota:
    def test_pulls_headers_off_client_last_quota(self, db_conn):
        class FakeClient:
            last_quota = {
                "x-requests-used": "15",
                "x-requests-remaining": "485",
                "x-requests-last": "3",
            }
        record_client_quota(db_conn, FakeClient(), endpoint="odds", job_type="game_odds")
        row = db_conn.execute("SELECT * FROM odds_api_credits").fetchone()
        assert row["requests_used"] == 15
        assert row["requests_remaining"] == 485
        assert row["requests_last"] == 3

    def test_missing_or_malformed_headers_do_not_crash(self, db_conn):
        class FakeClient:
            last_quota = {}
        record_client_quota(db_conn, FakeClient(), endpoint="events")
        row = db_conn.execute("SELECT * FROM odds_api_credits").fetchone()
        assert row["requests_used"] is None
        assert row["requests_remaining"] is None

    def test_client_with_no_last_quota_attribute(self, db_conn):
        class BareClient:
            pass
        record_client_quota(db_conn, BareClient(), endpoint="events")
        row = db_conn.execute("SELECT * FROM odds_api_credits").fetchone()
        assert row is not None


class TestGetLatestCreditStatus:
    def test_none_when_no_rows(self, db_conn):
        assert get_latest_credit_status(db_conn) is None

    def test_returns_most_recent_real_call(self, db_conn):
        record_credit_usage(db_conn, endpoint="odds", requests_remaining=490)
        record_credit_usage(db_conn, endpoint="odds", requests_remaining=487)
        status = get_latest_credit_status(db_conn)
        assert status["requests_remaining"] == 487

    def test_ignores_cache_hits(self, db_conn):
        record_credit_usage(db_conn, endpoint="odds", requests_remaining=487, cache_hit=False)
        record_credit_usage(db_conn, endpoint="odds", requests_remaining=999, cache_hit=True)
        status = get_latest_credit_status(db_conn)
        assert status["requests_remaining"] == 487


class TestGetUsageThisMonth:
    def test_sums_real_calls_only(self, db_conn):
        now = datetime.now(timezone.utc)
        record_credit_usage(db_conn, endpoint="odds", requests_last=3, cache_hit=False)
        record_credit_usage(db_conn, endpoint="odds", requests_last=8, cache_hit=False)
        record_credit_usage(db_conn, endpoint="odds", requests_last=999, cache_hit=True)
        usage = get_usage_this_month(db_conn, as_of=now)
        assert usage["credits_used_so_far"] == 11
        assert usage["calls_recorded"] == 2

    def test_projection_scales_by_days_remaining(self, db_conn):
        as_of = datetime(2026, 8, 10, tzinfo=timezone.utc)  # day 10 of 31-day month
        record_credit_usage(db_conn, endpoint="odds", requests_last=10, cache_hit=False)
        # Manually backdate isn't possible via record_credit_usage (it stamps "now"),
        # so directly insert a historical row for this month.
        db_conn.execute(
            "UPDATE odds_api_credits SET recorded_at = ?",
            (as_of.isoformat(),),
        )
        usage = get_usage_this_month(db_conn, as_of=as_of)
        assert usage["days_elapsed"] == 10
        assert usage["days_in_month"] == 31
        # 10 credits over 10 days = 1/day * 31 days = 31 projected
        assert usage["projected_month_total"] == pytest.approx(31.0)


class TestEstimateMonthlyCost:
    def test_game_only_cadence(self):
        result = estimate_monthly_cost(game_scans_per_day=2, prop_events_per_day=0)
        assert result["daily_game_credits"] == 2 * GAME_ODDS_COST
        assert result["daily_props_credits"] == 0
        assert result["fits_free_tier"] is True

    def test_props_dominate_cost(self):
        result = estimate_monthly_cost(game_scans_per_day=1, prop_events_per_day=5)
        assert result["daily_props_credits"] == 5 * PROPS_COST_PER_EVENT
        assert result["monthly_total_credits"] > result["daily_total_credits"]

    def test_daily_props_scan_exceeds_free_tier(self):
        # 5 games/day, props once per game, every day of the month.
        result = estimate_monthly_cost(game_scans_per_day=1, prop_events_per_day=5)
        assert result["fits_free_tier"] is False
        assert result["monthly_total_credits"] > DEFAULT_MONTHLY_BUDGET


class TestCreditBudgetCheck:
    def test_allows_when_plenty_remaining(self, db_conn):
        record_credit_usage(db_conn, endpoint="odds", requests_remaining=400)
        allowed, reason = credit_budget_check(db_conn, 8)
        assert allowed is True

    def test_blocks_when_reserve_would_be_breached(self, db_conn):
        # 500 budget, 10% reserve = 50. Only 40 remaining reported.
        record_credit_usage(db_conn, endpoint="odds", requests_remaining=40)
        allowed, reason = credit_budget_check(db_conn, 8)
        assert allowed is False
        assert "reserve" in reason

    def test_exactly_at_reserve_boundary_is_blocked(self, db_conn):
        record_credit_usage(db_conn, endpoint="odds", requests_remaining=58)
        # 58 - 50 reserve = 8 available; requesting exactly 8 must NOT
        # exceed and should be allowed (boundary is > not >=).
        allowed, _ = credit_budget_check(db_conn, 8)
        assert allowed is True
        allowed2, _ = credit_budget_check(db_conn, 9)
        assert allowed2 is False

    def test_falls_back_to_monthly_estimate_when_no_provider_reading(self, db_conn):
        allowed, reason = credit_budget_check(db_conn, 8)
        assert allowed is True  # no usage yet, full budget available
        assert "no recent provider reading" in reason.lower() or "no provider reading" in reason.lower()

    def test_falls_back_estimate_blocks_when_month_usage_high(self, db_conn):
        now = datetime.now(timezone.utc)
        record_credit_usage(db_conn, endpoint="odds", requests_last=470, cache_hit=False)
        allowed, reason = credit_budget_check(db_conn, 40)
        assert allowed is False
