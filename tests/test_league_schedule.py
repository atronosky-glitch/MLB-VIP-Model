"""Tests for src/league_schedule.py — pure per-league scheduling policy.

No live API calls, no worker loop, no database — every decision function
takes plain datetimes and lists, which is exactly what makes the
scheduling POLICY testable independent of the I/O around it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.league_schedule import (
    extract_game_start_times,
    nfl_has_games_today,
    nfl_should_run_daily_scan,
    nfl_pregame_window,
    nfl_should_run_pregame_check,
    wnba_has_games_today,
    wnba_should_check_schedule,
    wnba_should_fetch_game_odds,
    wnba_should_fetch_props,
)

UTC = timezone.utc


class TestExtractGameStartTimes:
    def test_sportsgameodds_shape(self):
        events = [{"status": {"startsAt": "2026-09-11T00:20:00Z"}}]
        times = extract_game_start_times(events)
        assert len(times) == 1
        assert times[0].year == 2026

    def test_wnba_normalized_shape(self):
        events = [{"status": {"startsAt": "2026-08-19T23:30:00Z"}}]
        times = extract_game_start_times(events)
        assert len(times) == 1

    def test_missing_time_skipped_not_guessed(self):
        events = [{"status": {}}, {"status": {"startsAt": "2026-08-19T23:30:00Z"}}]
        times = extract_game_start_times(events)
        assert len(times) == 1

    def test_malformed_time_skipped(self):
        events = [{"status": {"startsAt": "not-a-date"}}]
        assert extract_game_start_times(events) == []

    def test_sorted_ascending(self):
        events = [
            {"status": {"startsAt": "2026-08-19T23:30:00Z"}},
            {"status": {"startsAt": "2026-08-19T19:00:00Z"}},
        ]
        times = extract_game_start_times(events)
        assert times[0] < times[1]

    def test_empty_events(self):
        assert extract_game_start_times([]) == []


class TestNFLGameDayDiscovery:
    def test_no_games_no_scan(self):
        now = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)  # a Wednesday, bye week
        assert nfl_has_games_today(now, []) is False

    def test_thursday_night_game_detected(self):
        now = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)  # Thursday morning
        kickoff = datetime(2026, 9, 10, 0, 20, tzinfo=UTC)  # same UTC date
        assert nfl_has_games_today(now, [kickoff]) is True

    def test_game_on_different_date_not_counted(self):
        now = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
        other_day = datetime(2026, 9, 14, 17, 0, tzinfo=UTC)
        assert nfl_has_games_today(now, [other_day]) is False

    def test_monday_night_and_sunday_slate_both_seen(self):
        now = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)  # Sunday
        sunday_early = datetime(2026, 9, 14, 17, 0, tzinfo=UTC)
        sunday_late = datetime(2026, 9, 14, 20, 5, tzinfo=UTC)
        assert nfl_has_games_today(now, [sunday_early, sunday_late]) is True


class TestNFLDailyScan:
    def test_no_games_never_runs(self):
        now = datetime(2026, 9, 9, 9, 0, tzinfo=UTC)
        d = nfl_should_run_daily_scan(now, [], already_ran_today=False)
        assert d.should_run is False
        assert "no NFL games" in d.reason

    def test_runs_once_on_game_day_after_8am(self):
        now = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
        kickoff = datetime(2026, 9, 10, 0, 20, tzinfo=UTC)
        d = nfl_should_run_daily_scan(now, [kickoff], already_ran_today=False)
        assert d.should_run is True

    def test_does_not_run_before_8am(self):
        now = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)
        kickoff = datetime(2026, 9, 10, 0, 20, tzinfo=UTC)
        d = nfl_should_run_daily_scan(now, [kickoff], already_ran_today=False)
        assert d.should_run is False

    def test_does_not_run_twice(self):
        now = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
        kickoff = datetime(2026, 9, 10, 0, 20, tzinfo=UTC)
        d = nfl_should_run_daily_scan(now, [kickoff], already_ran_today=True)
        assert d.should_run is False
        assert "already ran" in d.reason


class TestNFLPregameWindow:
    def test_no_games_no_window(self):
        assert nfl_pregame_window([]) is None

    def test_window_spans_first_minus_4h_to_last_kickoff(self):
        sunday_early = datetime(2026, 9, 14, 17, 0, tzinfo=UTC)
        sunday_late = datetime(2026, 9, 14, 20, 5, tzinfo=UTC)
        snf = datetime(2026, 9, 15, 0, 20, tzinfo=UTC)
        start, end = nfl_pregame_window([sunday_early, sunday_late, snf])
        assert start == sunday_early - timedelta(hours=4)
        assert end == snf

    def test_should_run_inside_window(self):
        kickoff = datetime(2026, 9, 14, 17, 0, tzinfo=UTC)
        now = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)  # 3h before kickoff
        d = nfl_should_run_pregame_check(now, [kickoff])
        assert d.should_run is True

    def test_should_not_run_before_window(self):
        kickoff = datetime(2026, 9, 14, 17, 0, tzinfo=UTC)
        now = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)  # 9h before
        d = nfl_should_run_pregame_check(now, [kickoff])
        assert d.should_run is False

    def test_should_not_run_after_last_kickoff(self):
        kickoff = datetime(2026, 9, 14, 17, 0, tzinfo=UTC)
        now = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)  # after kickoff
        d = nfl_should_run_pregame_check(now, [kickoff])
        assert d.should_run is False

    def test_no_games_today_never_runs(self):
        now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
        d = nfl_should_run_pregame_check(now, [])
        assert d.should_run is False


class TestWNBAScheduleDiscovery:
    def test_first_check_always_allowed(self):
        now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
        d = wnba_should_check_schedule(now, last_check=None)
        assert d.should_run is True

    def test_throttled_within_interval(self):
        now = datetime(2026, 8, 19, 12, 30, tzinfo=UTC)
        last = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
        d = wnba_should_check_schedule(now, last_check=last, min_interval_minutes=60)
        assert d.should_run is False

    def test_allowed_after_interval_elapses(self):
        now = datetime(2026, 8, 19, 13, 1, tzinfo=UTC)
        last = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
        d = wnba_should_check_schedule(now, last_check=last, min_interval_minutes=60)
        assert d.should_run is True


class TestWNBAGameOdds:
    def test_no_games_no_fetch(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        d = wnba_should_fetch_game_odds(now, [], last_fetch=None)
        assert d.should_run is False
        assert "no WNBA games" in d.reason

    def test_first_fetch_of_the_day_allowed(self):
        now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
        game = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)
        d = wnba_should_fetch_game_odds(now, [game], last_fetch=None)
        assert d.should_run is True

    def test_far_from_tipoff_uses_wide_interval(self):
        now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
        game = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)  # 11.5h away
        last = now - timedelta(minutes=90)
        d = wnba_should_fetch_game_odds(now, [game], last_fetch=last)
        assert d.should_run is False  # 90 min < 180 min interval far from tip-off

    def test_close_to_tipoff_ramps_up_frequency(self):
        now = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)
        game = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)  # 90 min away
        last = now - timedelta(minutes=45)
        d = wnba_should_fetch_game_odds(now, [game], last_fetch=last)
        assert d.should_run is True  # 45 min >= 30 min interval near tip-off


class TestWNBAProps:
    def test_no_games_no_fetch(self):
        now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        d = wnba_should_fetch_props(now, [], last_fetch=None, credits_remaining=5000)
        assert d.should_run is False

    def test_too_early_in_the_day_skipped(self):
        now = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
        game = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)  # 13.5h away
        d = wnba_should_fetch_props(now, [game], last_fetch=None, credits_remaining=5000)
        assert d.should_run is False
        assert "too early" in d.reason

    def test_inside_window_and_budget_allows_fetch(self):
        now = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)
        game = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)  # 90 min away
        d = wnba_should_fetch_props(now, [game], last_fetch=None, credits_remaining=5000)
        assert d.should_run is True

    def test_widened_window_allows_4h_out(self):
        """Window was widened from 3h to 6h 2026-08-22 (real 20,000/mo
        budget makes fresher pregame lines affordable, not just close to
        tip-off) — a game 4h away used to be "too early", now isn't."""
        now = datetime(2026, 8, 19, 18, 0, tzinfo=UTC)
        game = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)  # 4h away
        d = wnba_should_fetch_props(now, [game], last_fetch=None, credits_remaining=5000)
        assert d.should_run is True

    def test_still_too_early_beyond_6h(self):
        now = datetime(2026, 8, 19, 15, 0, tzinfo=UTC)
        game = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)  # 7h away
        d = wnba_should_fetch_props(now, [game], last_fetch=None, credits_remaining=5000)
        assert d.should_run is False
        assert "too early" in d.reason

    def test_low_credits_blocks_props(self):
        now = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)
        game = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)
        d = wnba_should_fetch_props(now, [game], last_fetch=None, credits_remaining=40, reserve=50)
        assert d.should_run is False
        assert "reserve" in d.reason

    def test_default_reserve_scales_with_monthly_budget(self):
        """No explicit reserve passed -> defaults to 10% of the real
        current DEFAULT_MONTHLY_BUDGET (20,000 as of 2026-08-22), not a
        number sized for the old 500/mo free tier."""
        from src.odds_api_credits import DEFAULT_MONTHLY_BUDGET
        expected_reserve = int(DEFAULT_MONTHLY_BUDGET * 0.10)
        now = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)
        game = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)
        just_below = wnba_should_fetch_props(
            now, [game], last_fetch=None, credits_remaining=expected_reserve,
        )
        assert just_below.should_run is False
        just_above = wnba_should_fetch_props(
            now, [game], last_fetch=None, credits_remaining=expected_reserve + 1,
        )
        assert just_above.should_run is True

    def test_recently_fetched_throttled_to_once_per_30_minutes(self):
        now = datetime(2026, 8, 19, 22, 30, tzinfo=UTC)
        game = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)
        last = now - timedelta(minutes=20)
        d = wnba_should_fetch_props(now, [game], last_fetch=last, credits_remaining=5000)
        assert d.should_run is False

    def test_tightened_throttle_allows_35_minutes(self):
        """Throttle was tightened from once/hour to once/30min 2026-08-22
        — 35 minutes elapsed used to be blocked, now isn't."""
        now = datetime(2026, 8, 19, 22, 30, tzinfo=UTC)
        game = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)
        last = now - timedelta(minutes=35)
        d = wnba_should_fetch_props(now, [game], last_fetch=last, credits_remaining=5000)
        assert d.should_run is True

    def test_stops_after_all_games_started(self):
        now = datetime(2026, 8, 19, 23, 45, tzinfo=UTC)
        game = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)  # started 15 min ago, same day
        d = wnba_should_fetch_props(now, [game], last_fetch=None, credits_remaining=5000)
        assert d.should_run is False
        assert "started" in d.reason.lower()

    def test_none_credits_remaining_does_not_block(self):
        """No provider reading yet (e.g. first call ever) must not
        silently prevent every future fetch."""
        now = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)
        game = datetime(2026, 8, 19, 23, 30, tzinfo=UTC)
        d = wnba_should_fetch_props(now, [game], last_fetch=None, credits_remaining=None)
        assert d.should_run is True
