"""OpportunityEvaluator: the only module in the execution layer that
calls provider network methods. Combines Stage 2A's market matching
with Stage 2B's order-book walking / fees / EV to answer "is this
recommendation, on this provider, actually worth executing right now" --
producing an ExecutionOpportunity or an explicit ExecutionRejection.
No order placement anywhere in this pipeline.

UNCONFIRMED simplifying assumption (flagged loudly, same posture as
Kalshi's guessed title format and Polymarket's guessed book structure):
neither provider's parsed game event currently indicates which team
corresponds to the market's YES side. This module assumes **YES = the
away team, NO = the home team** -- matching the away-first convention
already used throughout matching.py (matchup strings, Polymarket's
slug order). Verify via `inspect-markets --raw` against real data
before trusting this for anything beyond structural testing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from src.execution.base import PredictionMarketProvider
from src.execution.ev import compute_ev
from src.execution.matching import MatchResult, RecommendationEvent
from src.execution.orderbook_math import max_quantity_for_budget, walk_book
from src.execution.quotes import build_executable_quote

logger = logging.getLogger(__name__)


class RejectionReason(str, Enum):
    """Canonical, exhaustive set of reasons a recommendation/provider
    pair never becomes a qualified ExecutionOpportunity. A single
    source of truth so string literals never get scattered across
    cli.py/evaluator.py -- every rejection anywhere in this package
    uses one of these values, and every one is exercised by at least
    one test (some directly by OpportunityEvaluator, others by the
    scan-opportunities CLI loop for cases that happen before an
    ExecutionSignal even exists, like MODEL_PROBABILITY_UNAVAILABLE).

    Values marked "(reserved)" below are not yet triggered by any
    Stage 2B logic -- they exist now so persistence/serialization is
    already correct for a later stage that needs them (settlement-rule
    verification, live market status), rather than adding them
    piecemeal and risking a schema/serialization surprise then.
    """

    # Actively triggered in Stage 2B:
    UNSUPPORTED_MARKET_TYPE = "UNSUPPORTED_MARKET_TYPE"
    MODEL_PROBABILITY_UNAVAILABLE = "MODEL_PROBABILITY_UNAVAILABLE"
    NO_PROVIDER_MARKET = "NO_PROVIDER_MARKET"
    UNVERIFIED_PROVIDER_SIDE_SEMANTICS = "UNVERIFIED_PROVIDER_SIDE_SEMANTICS"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"
    RAW_EV_TOO_LOW = "RAW_EV_TOO_LOW"
    NET_EV_TOO_LOW = "NET_EV_TOO_LOW"
    PARTIAL_FILL_ONLY = "PARTIAL_FILL_ONLY"
    API_ERROR = "API_ERROR"
    INVALID_ORDERBOOK = "INVALID_ORDERBOOK"
    INVALID_PRICE = "INVALID_PRICE"
    UNKNOWN = "UNKNOWN"

    # Reserved for a later stage (settlement-rule verification, live
    # market status/timing checks -- none of this stage's code path
    # currently produces these):
    MARKET_MATCH_LOW_CONFIDENCE = "MARKET_MATCH_LOW_CONFIDENCE"
    MARKET_RULE_MISMATCH = "MARKET_RULE_MISMATCH"
    UNVERIFIED_SETTLEMENT_RULES = "UNVERIFIED_SETTLEMENT_RULES"
    MARKET_CLOSED = "MARKET_CLOSED"
    EVENT_ALREADY_STARTED = "EVENT_ALREADY_STARTED"
    UNPARSED_PROVIDER_MARKET = "UNPARSED_PROVIDER_MARKET"


class EvaluationStatus(str, Enum):
    """Top-level outcome of one evaluation -- what opportunity_store.py
    persists to execution_opportunities.status."""
    QUALIFIED = "qualified"
    REJECTED = "rejected"


# Only moneyline is real right now -- see src/execution/matching.py's
# module docstring for why spread/total are scaffolded but unexercised.
_SUPPORTED_MARKET_TYPES = frozenset({"moneyline"})


@dataclass(frozen=True)
class ExecutionSignal:
    recommendation_id: str
    league: str
    market_type: str            # LOGICAL value ("moneyline"/"spread"/"total"), not the raw DB string
    home_team: str
    away_team: str
    side: str
    line: float | None
    event_start_time: datetime | None
    model_probability: Decimal
    sportsbook_ev_pct: float | None
    rec_status: str
    signal_timestamp: datetime


@dataclass(frozen=True)
class ExecutionOpportunity:
    recommendation_id: str
    league: str
    event: str
    market: str
    side: str
    model_probability: Decimal
    provider: str
    provider_market_id: str
    match_confidence: float
    best_bid: Decimal | None
    best_ask: Decimal | None
    spread: Decimal | None
    analysis_stake_usd: Decimal
    quantity_analyzed: Decimal
    expected_fill_price: Decimal | None
    estimated_fees: Decimal
    expected_slippage: Decimal | None
    available_liquidity: Decimal
    raw_ev_pct: Decimal
    net_ev_pct: Decimal
    max_acceptable_price: Decimal
    market_data_timestamp: datetime
    signal_timestamp: datetime
    generated_at: datetime
    expiration_time: datetime


@dataclass(frozen=True)
class ExecutionRejection:
    recommendation_id: str
    provider: str
    league: str
    event: str
    market: str
    side: str
    reason: RejectionReason
    detail: str
    generated_at: datetime


@dataclass(frozen=True)
class VenueComparison:
    """Result of OpportunityEvaluator.compare(): one signal evaluated
    against every enabled, matched provider. best is None when nothing
    qualified anywhere -- never a provider that failed a quality filter,
    even if its raw price looked nominally better."""
    signal: ExecutionSignal
    best: ExecutionOpportunity | None
    qualified_alternatives: list[ExecutionOpportunity]
    provider_rejections: list[ExecutionRejection]


_LOGICAL_MARKET_TYPE = {
    "game_moneyline": "moneyline",
    "game_spread_ou": "spread",
    "game_runline_ou": "spread",
    "game_total_ou": "total",
}


def build_execution_signal(row: dict[str, Any], rec_event: RecommendationEvent) -> ExecutionSignal | None:
    """Builds on Stage 2A's already-parsed RecommendationEvent rather
    than re-deriving team/date parsing. Returns None only when
    fair_prob is missing -- market-type support is OpportunityEvaluator's
    concern (produces an explicit UNSUPPORTED_MARKET_TYPE rejection),
    not something silently filtered out here."""
    fair_prob = row.get("fair_prob")
    if fair_prob is None:
        return None

    return ExecutionSignal(
        recommendation_id=rec_event.recommendation_id,
        league=rec_event.league,
        market_type=_LOGICAL_MARKET_TYPE.get(rec_event.market_type, rec_event.market_type),
        home_team=rec_event.home_team,
        away_team=rec_event.away_team,
        side=rec_event.side,
        line=rec_event.line,
        event_start_time=rec_event.event_start_time,
        model_probability=Decimal(str(fair_prob)),
        sportsbook_ev_pct=row.get("ev_pct"),
        rec_status=row.get("rec_status", ""),
        signal_timestamp=datetime.now(timezone.utc),
    )


def _provider_side_for_signal(signal: ExecutionSignal) -> str:
    """UNCONFIRMED assumption -- see module docstring. AWAY -> YES,
    HOME -> NO."""
    return "YES" if signal.side == "AWAY" else "NO"


def reject_without_signal(
    rec_event: RecommendationEvent, provider: str, reason: RejectionReason, detail: str,
) -> ExecutionRejection:
    """A rejection for a case that happens BEFORE a valid ExecutionSignal
    can even be built (currently only MODEL_PROBABILITY_UNAVAILABLE --
    build_execution_signal returns None when fair_prob is missing, and
    the execution layer must never guess a substitute probability from
    displayed EV or sportsbook implied probability). Built from Stage
    2A's RecommendationEvent, which has everything needed except the
    model probability itself."""
    return ExecutionRejection(
        recommendation_id=rec_event.recommendation_id,
        provider=provider,
        league=rec_event.league,
        event=f"{rec_event.away_team} @ {rec_event.home_team}",
        market=_LOGICAL_MARKET_TYPE.get(rec_event.market_type, rec_event.market_type),
        side=rec_event.side,
        reason=reason,
        detail=detail,
        generated_at=datetime.now(timezone.utc),
    )


class OpportunityEvaluator:
    def __init__(self, config: Any) -> None:
        self.config = config

    def evaluate(
        self, signal: ExecutionSignal, match: MatchResult, provider: PredictionMarketProvider,
        desired_contracts: Decimal | None = None,
    ) -> ExecutionOpportunity | ExecutionRejection:
        """*desired_contracts*, if given, is used directly instead of
        deriving a quantity from config.execution_analysis_stake_usd --
        needed because a stake-derived quantity is, by construction,
        always exactly fillable from the same book it was derived
        from, so PARTIAL_FILL_ONLY could otherwise never actually
        trigger through the stake-based path."""
        event_label = f"{signal.away_team} @ {signal.home_team}"

        if signal.market_type not in _SUPPORTED_MARKET_TYPES:
            return self._reject(
                signal, match.provider, event_label,
                RejectionReason.UNSUPPORTED_MARKET_TYPE,
                f"market_type={signal.market_type!r} is not yet supported for execution analysis",
            )

        side = _provider_side_for_signal(signal)

        # UNCONFIRMED whether Polymarket US models NO as genuinely
        # separate, independently-tradeable liquidity or as a single
        # binary book (NO = 1-YES) -- see PolymarketUSProvider.
        # normalize_orderbook's docstring. Rather than price a NO-side
        # opportunity against a synthesized book that might not
        # reflect real tradeable liquidity, reject it outright until
        # verified: better to reject a valid opportunity than price
        # one incorrectly. YES-side is unaffected -- Polymarket's raw
        # book already IS a normal bidirectional YES book, no
        # synthesis involved there.
        if match.provider == "polymarket_us" and side == "NO":
            return self._reject(
                signal, match.provider, event_label, RejectionReason.UNVERIFIED_PROVIDER_SIDE_SEMANTICS,
                "Polymarket US's NO-side book is a synthesized (1-P) assumption, not confirmed "
                "against real independently-tradeable liquidity -- rejected until verified via "
                "inspect-markets --raw",
            )

        try:
            raw_book = provider.get_orderbook(match.provider_market_id)
            book = provider.normalize_orderbook(raw_book)
        except Exception as exc:
            return self._reject(
                signal, match.provider, event_label, RejectionReason.API_ERROR,
                f"could not fetch/normalize orderbook: {type(exc).__name__}: {exc}",
            )

        if book.timestamp is None:
            return self._reject(
                signal, match.provider, event_label, RejectionReason.MARKET_DATA_STALE,
                "orderbook has no timestamp -- freshness cannot be established, treated conservatively as stale",
            )

        age_seconds = (datetime.now(timezone.utc) - book.timestamp).total_seconds()
        if age_seconds > self.config.max_market_data_age_seconds:
            return self._reject(
                signal, match.provider, event_label, RejectionReason.MARKET_DATA_STALE,
                f"orderbook age {age_seconds:.1f}s exceeds max_market_data_age_seconds="
                f"{self.config.max_market_data_age_seconds}",
            )

        asks = book.yes_asks if side == "YES" else book.no_asks

        stake = Decimal(str(self.config.execution_analysis_stake_usd))
        if desired_contracts is not None:
            desired_quantity = desired_contracts
        else:
            desired_quantity = max_quantity_for_budget(asks, stake)
        if desired_quantity <= 0:
            return self._reject(
                signal, match.provider, event_label, RejectionReason.INSUFFICIENT_LIQUIDITY,
                "no liquidity available at any ask price for the configured analysis stake",
            )

        preliminary_fill = walk_book(asks, desired_quantity)
        if preliminary_fill.quantity_filled <= 0:
            return self._reject(
                signal, match.provider, event_label, RejectionReason.INSUFFICIENT_LIQUIDITY,
                "book walk filled zero contracts",
            )

        fee_estimate = provider.estimate_fees(
            side, preliminary_fill.vwap, preliminary_fill.quantity_filled,
        )
        quote = build_executable_quote(
            match.provider, match.provider_market_id, book, side, desired_quantity, fee_estimate,
        )

        if quote.unfilled_quantity > 0:
            return self._reject(
                signal, match.provider, event_label, RejectionReason.PARTIAL_FILL_ONLY,
                f"only {quote.expected_fill_quantity} of {quote.requested_quantity} "
                "requested contracts could be filled",
            )

        min_liquidity = Decimal(str(self.config.min_available_liquidity_usd))
        if quote.liquidity_available < min_liquidity:
            return self._reject(
                signal, match.provider, event_label, RejectionReason.INSUFFICIENT_LIQUIDITY,
                f"available liquidity {quote.liquidity_available} < minimum {min_liquidity}",
            )

        max_spread_pct = Decimal(str(self.config.max_spread_pct)) * 100
        if quote.spread_pct is not None and quote.spread_pct * 100 > max_spread_pct:
            return self._reject(
                signal, match.provider, event_label, RejectionReason.SPREAD_TOO_WIDE,
                f"spread {quote.spread_pct * 100:.2f}% > max {max_spread_pct:.2f}%",
            )

        max_slippage_pct = Decimal(str(self.config.max_slippage_pct)) * 100
        if quote.slippage_pct is not None and quote.slippage_pct * 100 > max_slippage_pct:
            return self._reject(
                signal, match.provider, event_label, RejectionReason.SLIPPAGE_TOO_HIGH,
                f"slippage {quote.slippage_pct * 100:.2f}% > max {max_slippage_pct:.2f}%",
            )

        min_net_ev_pct = Decimal(str(self.config.min_net_ev_pct))
        ev = compute_ev(
            signal.model_probability, side, quote, min_net_ev_pct,
            provider.estimate_fees,
        )

        min_raw_ev_pct = Decimal(str(self.config.min_raw_ev_pct))
        if ev.raw_ev_pct < min_raw_ev_pct:
            return self._reject(
                signal, match.provider, event_label, RejectionReason.RAW_EV_TOO_LOW,
                f"raw_ev_pct {ev.raw_ev_pct:.2f}% < minimum {min_raw_ev_pct}%",
            )

        if ev.net_ev_pct < min_net_ev_pct:
            return self._reject(
                signal, match.provider, event_label, RejectionReason.NET_EV_TOO_LOW,
                f"net_ev_pct {ev.net_ev_pct:.2f}% < minimum {min_net_ev_pct}%",
            )

        now = datetime.now(timezone.utc)
        return ExecutionOpportunity(
            recommendation_id=signal.recommendation_id,
            league=signal.league,
            event=event_label,
            market=signal.market_type,
            side=side,
            model_probability=signal.model_probability,
            provider=match.provider,
            provider_market_id=match.provider_market_id,
            match_confidence=match.confidence,
            best_bid=(book.yes_bids[0].price if side == "YES" and book.yes_bids
                      else (book.no_bids[0].price if book.no_bids else None)),
            best_ask=quote.best_price,
            spread=quote.spread,
            analysis_stake_usd=stake,
            quantity_analyzed=quote.expected_fill_quantity,
            expected_fill_price=quote.expected_fill_price,
            estimated_fees=quote.estimated_fees,
            expected_slippage=quote.slippage_absolute,
            available_liquidity=quote.liquidity_available,
            raw_ev_pct=ev.raw_ev_pct,
            net_ev_pct=ev.net_ev_pct,
            max_acceptable_price=ev.max_acceptable_entry_price,
            market_data_timestamp=book.timestamp,
            signal_timestamp=signal.signal_timestamp,
            generated_at=now,
            expiration_time=now + timedelta(seconds=self.config.max_market_data_age_seconds),
        )

    def _reject(
        self, signal: ExecutionSignal, provider_name: str, event_label: str,
        reason: RejectionReason, detail: str,
    ) -> ExecutionRejection:
        return ExecutionRejection(
            recommendation_id=signal.recommendation_id,
            provider=provider_name,
            league=signal.league,
            event=event_label,
            market=signal.market_type,
            side=signal.side,
            reason=reason,
            detail=detail,
            generated_at=datetime.now(timezone.utc),
        )

    def compare(
        self,
        signal: ExecutionSignal,
        matches: dict[str, MatchResult],
        providers: dict[str, PredictionMarketProvider],
    ) -> VenueComparison:
        """Evaluate *signal* independently against every provider in
        *matches*, and pick the best QUALIFYING one by highest
        net_ev_pct. Never picks a provider that failed any quality
        filter, even if its raw price looked better."""
        results: list[ExecutionOpportunity | ExecutionRejection] = []
        for provider_name, match in matches.items():
            provider = providers.get(provider_name)
            if provider is None:
                continue
            results.append(self.evaluate(signal, match, provider))

        qualified = [r for r in results if isinstance(r, ExecutionOpportunity)]
        rejections = [r for r in results if isinstance(r, ExecutionRejection)]
        best = max(qualified, key=lambda o: o.net_ev_pct) if qualified else None
        alternatives = [q for q in qualified if q is not best]

        return VenueComparison(
            signal=signal, best=best,
            qualified_alternatives=alternatives, provider_rejections=rejections,
        )
