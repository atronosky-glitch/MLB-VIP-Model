"""Tests for src/execution/matching.py. Pure -- no DB, no network."""

from datetime import datetime, timedelta, timezone

import pytest

from src.execution.base import Market, RawGameEvent
from src.execution.matching import (
    ProviderEvent, RecommendationEvent,
    build_recommendation_event, find_best_match, provider_event_from_market,
    score_match, MIN_MARKET_MATCH_CONFIDENCE_DEFAULT,
)


def _rec(
    market_type="game_moneyline", home="Toronto Blue Jays", away="Athletics",
    side="HOME", line=None, event_start_time="2026-09-12T23:00:00+00:00",
) -> RecommendationEvent:
    return RecommendationEvent(
        recommendation_id="rec-1", league="MLB", market_type=market_type,
        home_team=home, away_team=away, side=side, line=line,
        event_start_time=datetime.fromisoformat(event_start_time) if event_start_time else None,
    )


def _prov(
    provider="kalshi", market_id="M1", home="Toronto Blue Jays", away="Athletics",
    market_type="moneyline", side=None, line=None, event_start_time="2026-09-12T23:00:00+00:00",
) -> ProviderEvent:
    return ProviderEvent(
        provider=provider, market_id=market_id, home_team=home, away_team=away,
        market_type=market_type, side=side, line=line,
        event_start_time=datetime.fromisoformat(event_start_time) if event_start_time else None,
        raw_market=Market(id=market_id, title=f"{away} @ {home}", status="open"),
    )


class TestBuildRecommendationEvent:
    def test_builds_from_a_real_shaped_row(self):
        row = {
            "recommendation_id": "rec-1", "league": "MLB", "market_type": "game_moneyline",
            "matchup": "Toronto Blue Jays @ Athletics", "side": "HOME", "line": None,
            "event_start_time": "2026-09-12T23:00:00+00:00",
        }
        event = build_recommendation_event(row)
        assert event.home_team == "Athletics"
        assert event.away_team == "Toronto Blue Jays"
        assert event.event_start_time is not None

    def test_returns_none_for_player_prop_market_types(self):
        row = {
            "recommendation_id": "rec-1", "league": "MLB", "market_type": "batting_homeRuns_ou",
            "matchup": "Toronto Blue Jays @ Athletics", "side": "OVER", "line": 0.5,
        }
        assert build_recommendation_event(row) is None

    def test_returns_none_when_matchup_has_no_separator(self):
        row = {
            "recommendation_id": "rec-1", "league": "MLB", "market_type": "game_moneyline",
            "matchup": "garbage", "side": "HOME",
        }
        assert build_recommendation_event(row) is None


class TestProviderEventFromMarket:
    def test_none_in_none_out(self):
        market = Market(id="M1", title="x", status="open")
        assert provider_event_from_market("kalshi", market, None) is None

    def test_combines_parsed_fields_with_market_identity(self):
        market = Market(id="M1", title="Athletics @ Toronto Blue Jays", status="open")
        parsed = RawGameEvent(
            home_team="Toronto Blue Jays", away_team="Athletics",
            market_type="moneyline", side=None, line=None, event_start_time=None,
        )
        event = provider_event_from_market("kalshi", market, parsed)
        assert event.provider == "kalshi"
        assert event.market_id == "M1"
        assert event.raw_market is market


class TestScoreMatch:
    def test_exact_match_scores_at_or_above_the_gate(self):
        result = score_match(_rec(), _prov())
        assert result.confidence >= MIN_MARKET_MATCH_CONFIDENCE_DEFAULT
        assert result.team_score == 1.0

    def test_same_teams_wrong_date_scores_below_the_gate(self):
        rec = _rec(event_start_time="2026-09-12T23:00:00+00:00")
        prov = _prov(event_start_time="2026-09-15T23:00:00+00:00")
        result = score_match(rec, prov)
        assert result.confidence < MIN_MARKET_MATCH_CONFIDENCE_DEFAULT
        assert result.date_score == 0.0

    def test_fuzzy_team_name_still_clears_the_gate(self):
        rec = _rec(home="Kansas City Athletics", away="Toronto Blue Jays")
        prov = _prov(home="Athletics", away="Toronto Blue Jays")
        result = score_match(rec, prov)
        assert result.confidence >= MIN_MARKET_MATCH_CONFIDENCE_DEFAULT
        assert 0.0 < result.team_score < 1.0

    def test_wrong_line_spread_scores_below_the_gate(self):
        rec = _rec(market_type="game_spread_ou", line=-1.5)
        prov = _prov(market_type="spread", line=-2.5)
        result = score_match(rec, prov)
        assert result.line_score == 0.0
        assert result.confidence < MIN_MARKET_MATCH_CONFIDENCE_DEFAULT

    def test_wrong_line_total_scores_below_the_gate(self):
        rec = _rec(market_type="game_total_ou", line=8.5)
        prov = _prov(market_type="total", line=7.5)
        result = score_match(rec, prov)
        assert result.line_score == 0.0
        assert result.confidence < MIN_MARKET_MATCH_CONFIDENCE_DEFAULT

    def test_exact_line_spread_matches(self):
        rec = _rec(market_type="game_spread_ou", line=-1.5)
        prov = _prov(market_type="spread", line=-1.5)
        result = score_match(rec, prov)
        assert result.line_score == 1.0

    def test_moneyline_line_dimension_is_not_applicable_and_not_penalized(self):
        rec = _rec(market_type="game_moneyline", line=None)
        prov = _prov(market_type="moneyline", line=None)
        result = score_match(rec, prov)
        assert result.line_score == 1.0

    def test_market_type_mismatch_scores_zero_on_that_dimension(self):
        rec = _rec(market_type="game_moneyline")
        prov = _prov(market_type="total")
        result = score_match(rec, prov)
        assert result.market_type_score == 0.0
        assert result.confidence < MIN_MARKET_MATCH_CONFIDENCE_DEFAULT

    def test_missing_provider_team_data_scores_low_never_crashes(self):
        rec = _rec()
        prov = _prov(home=None, away=None)
        result = score_match(rec, prov)
        assert result.team_score == 0.0

    def test_date_proximity_decay_is_monotonic(self):
        base = datetime(2026, 9, 12, 23, 0, tzinfo=timezone.utc)
        rec = RecommendationEvent(
            recommendation_id="rec-1", league="MLB", market_type="game_moneyline",
            home_team="A", away_team="B", side="HOME", line=None, event_start_time=base,
        )

        def score_at(delta_hours):
            prov = ProviderEvent(
                provider="kalshi", market_id="M1", home_team="A", away_team="B",
                market_type="moneyline", side=None, line=None,
                event_start_time=base + timedelta(hours=delta_hours),
                raw_market=Market(id="M1", title="B @ A", status="open"),
            )
            return score_match(rec, prov).date_score

        scores = [score_at(h) for h in (0, 1, 12, 25)]
        assert scores[0] == 1.0
        assert scores[-1] == 0.0
        assert scores[0] >= scores[1] >= scores[2] >= scores[3]

    def test_no_date_evidence_from_provider_is_not_penalized(self):
        rec = _rec()
        prov = _prov(event_start_time=None)
        result = score_match(rec, prov)
        assert result.date_score == 1.0

    def test_swapped_home_away_orientation_still_matches(self):
        """Provider lists the pair in the opposite home/away order --
        still the same two teams playing each other."""
        rec = _rec(home="Toronto Blue Jays", away="Athletics")
        prov = _prov(home="Athletics", away="Toronto Blue Jays")
        result = score_match(rec, prov)
        assert result.team_score == 1.0


class TestFindBestMatch:
    def test_no_candidates_returns_none(self):
        assert find_best_match(_rec(), []) is None

    def test_nothing_below_the_gate_is_ever_returned(self):
        rec = _rec()
        candidates = [
            _prov(market_id="low1", event_start_time="2026-09-20T23:00:00+00:00"),
            _prov(market_id="low2", home="Somewhere Else", away="Another Team"),
        ]
        assert find_best_match(rec, candidates) is None

    def test_picks_the_highest_scoring_candidate_above_the_gate(self):
        rec = _rec()
        worse = _prov(market_id="worse", event_start_time="2026-09-13T05:00:00+00:00")
        better = _prov(market_id="better")
        result = find_best_match(rec, [worse, better])
        assert result is not None
        assert result.provider_market_id == "better"

    def test_respects_a_custom_min_confidence(self):
        rec = _rec()
        prov = _prov(event_start_time="2026-09-15T23:00:00+00:00")  # scores ~0.90
        assert find_best_match(rec, [prov], min_confidence=0.98) is None
        assert find_best_match(rec, [prov], min_confidence=0.5) is not None
