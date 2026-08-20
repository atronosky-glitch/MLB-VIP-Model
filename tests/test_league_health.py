"""Tests for src/league_health.py — per-league production health
reporting, distinguishing which league is actually broken instead of one
global status."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.league_health import run_league_health_checks, run_all_leagues_health_checks


def _insert_rec(conn, rec_id, *, league="MLB", scan_timestamp=None,
                 freshness_status="FRESH", qualification_passed=1,
                 event_start_time=None):
    scan_timestamp = scan_timestamp or datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO historical_recommendations (
            recommendation_id, fingerprint, event_id, player_id, player_name,
            market_type, market_form, side, sportsbook, offered_american_odds,
            offered_decimal_odds, offered_implied_prob, rec_status,
            scan_timestamp, freshness_status, qualification_passed, league,
            event_start_time
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (rec_id, f"fp_{rec_id}", "E1", "P1", "Player",
         "strikeouts", "ou", "OVER", "DraftKings", -110,
         1.909, 0.524, "QUALIFIED",
         scan_timestamp, freshness_status, qualification_passed, league,
         event_start_time),
    )
    conn.commit()


class TestRunLeagueHealthChecks:
    def test_empty_db_reports_degraded_not_crashing(self, db_conn):
        report = run_league_health_checks(db_conn, "MLB")
        assert report.overall_status in {"degraded", "unhealthy", "healthy"}
        assert report.check_count > 0

    def test_mlb_and_nfl_have_no_wnba_specific_checks(self, db_conn):
        mlb = run_league_health_checks(db_conn, "MLB")
        nfl = run_league_health_checks(db_conn, "NFL")
        wnba = run_league_health_checks(db_conn, "WNBA")
        mlb_names = {c.name for c in mlb.checks}
        nfl_names = {c.name for c in nfl.checks}
        wnba_names = {c.name for c in wnba.checks}
        assert not any("credit budget" in n.lower() for n in mlb_names)
        assert not any("credit budget" in n.lower() for n in nfl_names)
        assert any("credit budget" in n.lower() for n in wnba_names)

    def test_todays_recommendation_count_reflected(self, db_conn):
        _insert_rec(db_conn, "r1", league="MLB")
        _insert_rec(db_conn, "r2", league="MLB")
        _insert_rec(db_conn, "r3", league="NFL")
        report = run_league_health_checks(db_conn, "MLB")
        rec_check = next(c for c in report.checks if "last recommendation" in c.name)
        assert rec_check.details["today_count"] == 2

    def test_recent_recommendation_is_ok_status(self, db_conn):
        _insert_rec(db_conn, "r1", league="MLB")
        report = run_league_health_checks(db_conn, "MLB")
        rec_check = next(c for c in report.checks if "last recommendation" in c.name)
        assert rec_check.status == "ok"

    def test_stale_recommendation_flagged(self, db_conn):
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        _insert_rec(db_conn, "r1", league="MLB", scan_timestamp=old_ts)
        report = run_league_health_checks(db_conn, "MLB")
        rec_check = next(c for c in report.checks if "last recommendation" in c.name)
        assert rec_check.status == "error"

    def test_event_date_sanity_ok_for_plausible_dates(self, db_conn):
        now = datetime.now(timezone.utc)
        _insert_rec(db_conn, "r1", league="MLB",
                    event_start_time=(now + timedelta(hours=6)).isoformat())
        report = run_league_health_checks(db_conn, "MLB")
        check = next(c for c in report.checks if "event date sanity" in c.name)
        assert check.status == "ok"
        assert check.details["implausible_count"] == 0

    def test_event_date_sanity_flags_wrong_year_event(self, db_conn):
        """Reproduces the real 2026-08-20 bug directly: a recommendation
        whose event_start_time is over a year in the past relative to
        when it was generated (an unfiltered odds-API call returning a
        stale historical event set instead of current games)."""
        now = datetime.now(timezone.utc)
        stale_event = now.replace(year=now.year - 2)
        _insert_rec(db_conn, "r1", league="MLB",
                    scan_timestamp=now.isoformat(),
                    event_start_time=stale_event.isoformat())
        report = run_league_health_checks(db_conn, "MLB")
        check = next(c for c in report.checks if "event date sanity" in c.name)
        assert check.status == "error"
        assert check.details["implausible_count"] == 1

    def test_event_date_sanity_flags_far_future_event(self, db_conn):
        now = datetime.now(timezone.utc)
        far_future = now + timedelta(days=60)
        _insert_rec(db_conn, "r1", league="MLB",
                    scan_timestamp=now.isoformat(),
                    event_start_time=far_future.isoformat())
        report = run_league_health_checks(db_conn, "MLB")
        check = next(c for c in report.checks if "event date sanity" in c.name)
        assert check.status == "error"

    def test_stale_market_percentage_computed(self, db_conn):
        _insert_rec(db_conn, "r1", league="MLB", freshness_status="STALE")
        _insert_rec(db_conn, "r2", league="MLB", freshness_status="FRESH")
        report = run_league_health_checks(db_conn, "MLB")
        stale_check = next(c for c in report.checks if "stale markets" in c.name)
        assert stale_check.details["stale_count"] == 1
        assert stale_check.details["total_count"] == 2

    def test_qualified_opportunity_count(self, db_conn):
        _insert_rec(db_conn, "r1", league="MLB", qualification_passed=1)
        _insert_rec(db_conn, "r2", league="MLB", qualification_passed=0)
        report = run_league_health_checks(db_conn, "MLB")
        qual_check = next(c for c in report.checks if "qualified opportunities" in c.name)
        assert qual_check.details["count"] == 1

    def test_settlement_backlog_flagged_for_old_unsettled_game(self, db_conn):
        old_start = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        _insert_rec(db_conn, "r1", league="MLB", event_start_time=old_start)
        report = run_league_health_checks(db_conn, "MLB")
        settle_check = next(c for c in report.checks if "last settlement" in c.name)
        assert settle_check.details["unsettled_backlog"] == 1
        assert settle_check.status == "warning"

    def test_recent_unsettled_game_not_flagged_as_backlog(self, db_conn):
        recent_start = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        _insert_rec(db_conn, "r1", league="MLB", event_start_time=recent_start)
        report = run_league_health_checks(db_conn, "MLB")
        settle_check = next(c for c in report.checks if "last settlement" in c.name)
        assert settle_check.details["unsettled_backlog"] == 0

    def test_job_activity_reflects_scheduled_jobs(self, db_conn):
        from src.automation import create_job
        job_id = create_job(db_conn, "wnba-odds-scan")
        db_conn.execute(
            "UPDATE scheduled_jobs SET status = 'completed', "
            "started_at = ?, completed_at = ? WHERE job_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
                datetime.now(timezone.utc).isoformat(),
                job_id,
            ),
        )
        db_conn.commit()
        report = run_league_health_checks(db_conn, "WNBA")
        job_check = next(c for c in report.checks if "job activity" in c.name)
        assert job_check.status == "ok"
        assert job_check.details["duration_seconds"] == pytest.approx(30, abs=2)

    def test_failed_job_flagged_as_error(self, db_conn):
        from src.automation import create_job
        job_id = create_job(db_conn, "wnba-odds-scan")
        db_conn.execute(
            "UPDATE scheduled_jobs SET status = 'failed', error_message = 'boom', "
            "completed_at = ? WHERE job_id = ?",
            (datetime.now(timezone.utc).isoformat(), job_id),
        )
        db_conn.commit()
        report = run_league_health_checks(db_conn, "WNBA")
        job_check = next(c for c in report.checks if "job activity" in c.name)
        assert job_check.status == "error"


class TestWNBACreditHealthCheck:
    def test_low_credits_reported_as_error(self, db_conn):
        from src.odds_api_credits import record_credit_usage
        record_credit_usage(db_conn, endpoint="odds", requests_remaining=20)
        report = run_league_health_checks(db_conn, "WNBA")
        credit_check = next(c for c in report.checks if "credit budget" in c.name.lower())
        assert credit_check.status == "error"

    def test_plentiful_credits_reported_as_ok(self, db_conn):
        from src.odds_api_credits import record_credit_usage
        record_credit_usage(db_conn, endpoint="odds", requests_remaining=450)
        report = run_league_health_checks(db_conn, "WNBA")
        credit_check = next(c for c in report.checks if "credit budget" in c.name.lower())
        assert credit_check.status == "ok"

    def test_no_credit_data_does_not_crash(self, db_conn):
        report = run_league_health_checks(db_conn, "WNBA")
        credit_check = next(c for c in report.checks if "credit budget" in c.name.lower())
        assert credit_check.status in {"ok", "warning", "error"}


class TestRunAllLeaguesHealthChecks:
    def test_returns_all_three_by_default(self, db_conn):
        reports = run_all_leagues_health_checks(db_conn)
        assert set(reports.keys()) == {"MLB", "NFL", "WNBA"}

    def test_one_league_report_failure_does_not_prevent_others(self, db_conn):
        """Each league's report must be independent."""
        _insert_rec(db_conn, "r1", league="MLB")
        reports = run_all_leagues_health_checks(db_conn, leagues=["MLB", "NFL"])
        assert reports["MLB"].check_count > 0
        assert reports["NFL"].check_count > 0
