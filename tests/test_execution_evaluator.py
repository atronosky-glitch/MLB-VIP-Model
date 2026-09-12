"""Tests for src/execution/evaluator.py. Providers are fake stubs
implementing only what OpportunityEvaluator needs -- no HTTP, no real
credentials."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

import pytest

from src.execution.base import (
    Balance, BestBidAsk, FeeEstimate, HealthCheckResult, Market,
    NormalizedOrderBook, Orderbook, OrderLevel, PredictionMarketProvider,
)
from src.execution.evaluator import (
    ExecutionOpportunity, ExecutionRejection, ExecutionSignal,
    OpportunityEvaluator, RejectionReason, build_execution_signal,
)
from src.execution.matching import MatchResult


def _levels(*pairs):
    return [OrderLevel(Decimal(str(p)), Decimal(str(q))) for p, q in pairs]


class _FakeProvider(PredictionMarketProvider):
    name = "fake"

    def __init__(self, book: NormalizedOrderBook, fee=Decimal("0.05")):
        self._book = book
        self._fee = fee

    def get_balance(self):
        raise NotImplementedError

    def get_markets(self, **filters):
        return []

    def get_market(self, market_id):
        return Market(id=market_id, title="", status="open")

    def get_orderbook(self, market_id):
        return Orderbook(market_id=market_id, bids=[], asks=[])

    def get_best_bid_ask(self, market_id):
        raise NotImplementedError

    def health_check(self):
        raise NotImplementedError

    def normalize_orderbook(self, raw):
        return self._book

    def estimate_fees(self, side, price, quantity):
        return FeeEstimate(fee=self._fee, fee_estimate=True, detail="fake")


def _book(yes_bids=(), yes_asks=(), no_bids=(), no_asks=(), age_seconds=0):
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return NormalizedOrderBook(
        market_id="M1",
        yes_bids=_levels(*yes_bids), yes_asks=_levels(*yes_asks),
        no_bids=_levels(*no_bids), no_asks=_levels(*no_asks),
        timestamp=ts,
    )


def _signal(market_type="moneyline", side="AWAY", model_probability="0.70") -> ExecutionSignal:
    return ExecutionSignal(
        recommendation_id="rec-1", league="MLB", market_type=market_type,
        home_team="Toronto Blue Jays", away_team="Athletics", side=side, line=None,
        event_start_time=datetime.now(timezone.utc),
        model_probability=Decimal(model_probability), sportsbook_ev_pct=5.0,
        rec_status="STRONG_EDGE", signal_timestamp=datetime.now(timezone.utc),
    )


def _match(provider="fake") -> MatchResult:
    return MatchResult(
        recommendation_id="rec-1", provider=provider, provider_market_id="M1",
        confidence=0.99, team_score=1.0, market_type_score=1.0,
        line_score=1.0, date_score=1.0, provider_title="Athletics @ Toronto Blue Jays",
    )


class _FakeConfig:
    execution_analysis_stake_usd = 10.0
    min_raw_ev_pct = 2.0
    min_net_ev_pct = 1.0
    max_spread_pct = 0.10
    max_slippage_pct = 0.05
    min_available_liquidity_usd = 5.0
    max_market_data_age_seconds = 30


class TestBuildExecutionSignal:
    def test_builds_from_a_real_shaped_row(self):
        from src.execution.matching import build_recommendation_event
        row = {
            "recommendation_id": "rec-1", "league": "MLB", "market_type": "game_moneyline",
            "matchup": "Athletics @ Toronto Blue Jays", "side": "AWAY", "line": None,
            "event_start_time": "2026-09-12T23:00:00+00:00", "fair_prob": 0.70,
            "ev_pct": 5.0, "rec_status": "STRONG_EDGE",
        }
        rec_event = build_recommendation_event(row)
        signal = build_execution_signal(row, rec_event)
        assert signal is not None
        assert signal.market_type == "moneyline"
        assert signal.model_probability == Decimal("0.7")

    def test_none_when_fair_prob_missing(self):
        from src.execution.matching import build_recommendation_event
        row = {
            "recommendation_id": "rec-1", "league": "MLB", "market_type": "game_moneyline",
            "matchup": "Athletics @ Toronto Blue Jays", "side": "AWAY",
        }
        rec_event = build_recommendation_event(row)
        assert build_execution_signal(row, rec_event) is None


class TestUnsupportedMarketType:
    def test_short_circuits_before_any_provider_call(self):
        provider = mock.MagicMock(spec=PredictionMarketProvider)
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(market_type="spread"), _match(), provider)
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.UNSUPPORTED_MARKET_TYPE
        provider.get_orderbook.assert_not_called()


class TestMarketDataStale:
    def test_rejects_when_book_is_older_than_max_age(self):
        provider = _FakeProvider(_book(yes_asks=[(0.62, 1000)], yes_bids=[(0.60, 1000)], age_seconds=999))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(), _match(), provider)
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.MARKET_DATA_STALE

    def test_fresh_book_is_not_stale(self):
        provider = _FakeProvider(_book(yes_asks=[(0.62, 1000)], yes_bids=[(0.60, 1000)], age_seconds=0))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(model_probability="0.70"), _match(), provider)
        assert isinstance(result, ExecutionOpportunity)

    def test_exactly_at_the_threshold_still_qualifies(self):
        config = _FakeConfig()
        provider = _FakeProvider(_book(
            yes_asks=[(0.62, 1000)], yes_bids=[(0.60, 1000)],
            age_seconds=config.max_market_data_age_seconds,
        ))
        evaluator = OpportunityEvaluator(config)
        result = evaluator.evaluate(_signal(model_probability="0.70"), _match(), provider)
        assert isinstance(result, ExecutionOpportunity)

    def test_one_second_over_the_threshold_rejects(self):
        config = _FakeConfig()
        provider = _FakeProvider(_book(
            yes_asks=[(0.62, 1000)], yes_bids=[(0.60, 1000)],
            age_seconds=config.max_market_data_age_seconds + 1,
        ))
        evaluator = OpportunityEvaluator(config)
        result = evaluator.evaluate(_signal(model_probability="0.70"), _match(), provider)
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.MARKET_DATA_STALE

    def test_missing_timestamp_is_treated_conservatively_as_stale(self):
        book = _book(yes_asks=[(0.62, 1000)], yes_bids=[(0.60, 1000)])
        book = book.__class__(**{**book.__dict__, "timestamp": None})
        provider = _FakeProvider(book)
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(), _match(), provider)
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.MARKET_DATA_STALE


class TestApiError:
    def test_rejects_when_orderbook_fetch_raises(self):
        provider = mock.MagicMock(spec=PredictionMarketProvider)
        provider.get_orderbook.side_effect = RuntimeError("network error")
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(), _match(), provider)
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.API_ERROR


class TestInsufficientLiquidity:
    def test_rejects_on_an_empty_book(self):
        provider = _FakeProvider(_book(yes_asks=[], yes_bids=[(0.60, 100)]))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(), _match(), provider)
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.INSUFFICIENT_LIQUIDITY


class TestPartialFillOnly:
    def test_rejects_when_explicit_desired_contracts_exceeds_book_depth(self):
        provider = _FakeProvider(_book(yes_asks=[(0.62, 5)], yes_bids=[(0.60, 100)]))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(), _match(), provider, desired_contracts=Decimal("50"))
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.PARTIAL_FILL_ONLY


class TestSpreadTooWide:
    def test_rejects_when_spread_exceeds_the_configured_max(self):
        provider = _FakeProvider(_book(yes_asks=[(0.62, 1000)], yes_bids=[(0.40, 1000)]))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(), _match(), provider)
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.SPREAD_TOO_WIDE


class TestSlippageTooHigh:
    def test_rejects_when_walking_the_book_moves_vwap_too_far(self):
        provider = _FakeProvider(_book(
            yes_asks=[(0.61, 2), (0.90, 1000)], yes_bids=[(0.60, 1000)],
        ))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(), _match(), provider)
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.SLIPPAGE_TOO_HIGH


class TestRawEvTooLow:
    def test_rejects_when_price_already_reflects_the_models_edge(self):
        # model probability 0.70, ask price 0.70 -- no raw edge at all
        provider = _FakeProvider(_book(
            yes_asks=[(0.70, 1000)], yes_bids=[(0.69, 1000)],
        ), fee=Decimal("0"))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(model_probability="0.70"), _match(), provider)
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.RAW_EV_TOO_LOW


class TestNetEvTooLow:
    def test_rejects_when_fees_erase_a_thin_raw_edge(self):
        # small raw edge that a large fee wipes out below the net threshold
        provider = _FakeProvider(_book(
            yes_asks=[(0.67, 1000)], yes_bids=[(0.66, 1000)],
        ), fee=Decimal("5.00"))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(model_probability="0.70"), _match(), provider)
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.NET_EV_TOO_LOW


class TestPolymarketNoSideRejectedUntilVerified:
    """Polymarket US's NO-side book is a synthesized (1-P) assumption,
    not confirmed against real independently-tradeable liquidity --
    reject rather than price it incorrectly."""

    def test_no_side_is_rejected_before_any_provider_call(self):
        provider = mock.MagicMock(spec=PredictionMarketProvider)
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(side="HOME"), _match("polymarket_us"), provider)
        assert isinstance(result, ExecutionRejection)
        assert result.reason == RejectionReason.UNVERIFIED_PROVIDER_SIDE_SEMANTICS
        provider.get_orderbook.assert_not_called()

    def test_yes_side_is_unaffected_on_polymarket(self):
        provider = _FakeProvider(_book(yes_asks=[(0.62, 1000)], yes_bids=[(0.60, 1000)]), fee=Decimal("0.05"))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(side="AWAY", model_probability="0.70"), _match("polymarket_us"), provider)
        assert isinstance(result, ExecutionOpportunity)

    def test_no_side_is_unaffected_on_kalshi(self):
        """The restriction is Polymarket-specific -- Kalshi's NO side
        rests on a confirmed mechanism fact (see KalshiProvider.
        normalize_orderbook's docstring), not a guess. model_probability
        0.70 (YES) -> NO win probability 0.30; a 0.25 NO ask is below
        that, a real edge."""
        provider = _FakeProvider(_book(no_asks=[(0.25, 1000)], no_bids=[(0.23, 1000)]), fee=Decimal("0.05"))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(side="HOME", model_probability="0.70"), _match("kalshi"), provider)
        assert isinstance(result, ExecutionOpportunity)


class TestQualifiedOpportunity:
    def test_a_clean_favorable_book_qualifies(self):
        provider = _FakeProvider(_book(
            yes_asks=[(0.62, 1000)], yes_bids=[(0.60, 1000)],
        ), fee=Decimal("0.05"))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(model_probability="0.70"), _match(), provider)
        assert isinstance(result, ExecutionOpportunity)
        assert result.net_ev_pct > 0
        assert result.provider == "fake"
        assert result.recommendation_id == "rec-1"

    def test_qualified_opportunity_never_persists_a_rejection_reason_field(self):
        provider = _FakeProvider(_book(
            yes_asks=[(0.62, 1000)], yes_bids=[(0.60, 1000)],
        ), fee=Decimal("0.05"))
        evaluator = OpportunityEvaluator(_FakeConfig())
        result = evaluator.evaluate(_signal(model_probability="0.70"), _match(), provider)
        assert not hasattr(result, "reason")


class TestCrossVenueComparison:
    def test_picks_the_qualifying_provider_when_only_one_qualifies(self):
        good = _FakeProvider(_book(yes_asks=[(0.62, 1000)], yes_bids=[(0.60, 1000)]), fee=Decimal("0.05"))
        bad = _FakeProvider(_book(yes_asks=[], yes_bids=[]))
        evaluator = OpportunityEvaluator(_FakeConfig())
        signal = _signal(model_probability="0.70")
        matches = {"good": _match("good"), "bad": _match("bad")}
        providers = {"good": good, "bad": bad}
        comparison = evaluator.compare(signal, matches, providers)
        assert comparison.best is not None
        assert comparison.best.provider == "good"
        assert len(comparison.provider_rejections) == 1

    def test_both_providers_rejected_returns_none_best(self):
        bad1 = _FakeProvider(_book(yes_asks=[], yes_bids=[]))
        bad2 = _FakeProvider(_book(yes_asks=[], yes_bids=[]))
        evaluator = OpportunityEvaluator(_FakeConfig())
        signal = _signal(model_probability="0.70")
        matches = {"bad1": _match("bad1"), "bad2": _match("bad2")}
        providers = {"bad1": bad1, "bad2": bad2}
        comparison = evaluator.compare(signal, matches, providers)
        assert comparison.best is None
        assert len(comparison.provider_rejections) == 2
        assert comparison.qualified_alternatives == []

    def test_both_qualify_picks_the_higher_net_ev(self):
        cheap = _FakeProvider(_book(yes_asks=[(0.56, 1000)], yes_bids=[(0.55, 1000)]), fee=Decimal("0.05"))
        pricier = _FakeProvider(_book(yes_asks=[(0.62, 1000)], yes_bids=[(0.60, 1000)]), fee=Decimal("0.05"))
        evaluator = OpportunityEvaluator(_FakeConfig())
        signal = _signal(model_probability="0.70")
        matches = {"cheap": _match("cheap"), "pricier": _match("pricier")}
        providers = {"cheap": cheap, "pricier": pricier}
        comparison = evaluator.compare(signal, matches, providers)
        assert comparison.best is not None
        assert comparison.best.provider == "cheap"  # lower entry price -> higher EV
        assert len(comparison.qualified_alternatives) == 1
        assert comparison.qualified_alternatives[0].provider == "pricier"

    def test_never_picks_a_failing_provider_even_with_a_nominally_better_price(self):
        """A provider whose raw price looks great but fails a quality
        filter (here: empty book) must never be chosen over one that
        actually qualifies."""
        looks_great_but_empty = _FakeProvider(_book(yes_asks=[], yes_bids=[]))
        actually_qualifies = _FakeProvider(_book(yes_asks=[(0.62, 1000)], yes_bids=[(0.60, 1000)]), fee=Decimal("0.05"))
        evaluator = OpportunityEvaluator(_FakeConfig())
        signal = _signal(model_probability="0.70")
        matches = {"empty": _match("empty"), "real": _match("real")}
        providers = {"empty": looks_great_but_empty, "real": actually_qualifies}
        comparison = evaluator.compare(signal, matches, providers)
        best = comparison.best
        assert best is not None
        assert best.provider == "real"
