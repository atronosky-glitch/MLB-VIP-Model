"""Tests for src/execution/paper/settlement.py. Pure -- operates only
on Market.raw from an already-fetched (mocked) provider.get_market()
call; no network, no changes to any Stage 1/2B provider file."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.execution.base import Market
from src.execution.paper.models import PaperPositionStatus
from src.execution.paper.settlement import (
    determine_position_outcome, resolve_kalshi_market, resolve_market, resolve_polymarket_market,
)


class TestResolveKalshiMarket:
    def test_finalized_yes_result_resolves(self):
        market = Market(id="M1", title="x", status="finalized", raw={"status": "finalized", "result": "yes"})
        result = resolve_kalshi_market(market)
        assert result.resolved is True
        assert result.winning_side == "YES"
        assert result.payout_per_contract == Decimal("1")

    def test_finalized_no_result_resolves(self):
        market = Market(id="M1", title="x", status="finalized", raw={"status": "finalized", "result": "no"})
        result = resolve_kalshi_market(market)
        assert result.winning_side == "NO"

    def test_settled_status_also_treated_as_finalized(self):
        market = Market(id="M1", title="x", status="settled", raw={"status": "settled", "result": "yes"})
        result = resolve_kalshi_market(market)
        assert result.resolved is True

    def test_open_market_is_not_resolved(self):
        market = Market(id="M1", title="x", status="open", raw={"status": "open", "result": ""})
        result = resolve_kalshi_market(market)
        assert result.resolved is False
        assert result.winning_side is None

    def test_missing_raw_fields_not_resolved_never_guessed(self):
        market = Market(id="M1", title="x", status="open", raw={})
        result = resolve_kalshi_market(market)
        assert result.resolved is False

    def test_voided_status_resolves_as_void(self):
        market = Market(id="M1", title="x", status="voided", raw={"status": "voided"})
        result = resolve_kalshi_market(market)
        assert result.resolved is True
        assert result.winning_side is None

    def test_finalized_but_result_field_unrecognized_stays_unresolved(self):
        market = Market(id="M1", title="x", status="finalized", raw={"status": "finalized", "result": "?"})
        result = resolve_kalshi_market(market)
        assert result.resolved is False


class TestResolvePolymarketMarket:
    def test_always_unresolved_regardless_of_payload(self):
        """No settled-market payload has ever been fetched from
        Polymarket US -- guessing field names here would violate the
        'never fabricate a result' rule, so this always returns
        unresolved no matter what raw looks like."""
        market = Market(id="M1", title="x", status="resolved", raw={"status": "resolved", "outcome": "yes"})
        result = resolve_polymarket_market(market)
        assert result.resolved is False
        assert result.winning_side is None

    def test_even_a_kalshi_shaped_payload_is_not_trusted(self):
        market = Market(id="M1", title="x", status="finalized", raw={"status": "finalized", "result": "yes"})
        result = resolve_polymarket_market(market)
        assert result.resolved is False


class TestResolveMarketDispatch:
    def test_dispatches_to_kalshi(self):
        market = Market(id="M1", title="x", status="finalized", raw={"status": "finalized", "result": "yes"})
        result = resolve_market("kalshi", market)
        assert result.resolved is True

    def test_dispatches_to_polymarket(self):
        market = Market(id="M1", title="x", status="finalized", raw={"status": "finalized", "result": "yes"})
        result = resolve_market("polymarket_us", market)
        assert result.resolved is False


class TestDeterminePositionOutcome:
    def test_yes_position_wins_when_yes_resolved(self):
        from src.execution.paper.models import PaperSettlementResult
        settlement = PaperSettlementResult(
            provider="kalshi", market_id="M1", resolved=True, winning_side="YES",
            payout_per_contract=Decimal("1"), source="test", resolved_at=datetime.now(timezone.utc),
        )
        status, value, pnl = determine_position_outcome(
            settlement, "YES", Decimal("14"), Decimal("9.73"), Decimal("0.23"),
        )
        assert status == PaperPositionStatus.WON
        assert value == Decimal("14")
        assert pnl == Decimal("14") - Decimal("9.73")

    def test_no_position_loses_when_yes_resolved(self):
        from src.execution.paper.models import PaperSettlementResult
        settlement = PaperSettlementResult(
            provider="kalshi", market_id="M1", resolved=True, winning_side="YES",
            payout_per_contract=Decimal("1"), source="test", resolved_at=datetime.now(timezone.utc),
        )
        status, value, pnl = determine_position_outcome(
            settlement, "NO", Decimal("20"), Decimal("6.00"), Decimal("0.10"),
        )
        assert status == PaperPositionStatus.LOST
        assert value == Decimal("0")
        assert pnl == Decimal("-6.00")

    def test_void_refunds_entry_cost_minus_fees(self):
        from src.execution.paper.models import PaperSettlementResult
        settlement = PaperSettlementResult(
            provider="kalshi", market_id="M1", resolved=True, winning_side=None,
            payout_per_contract=None, source="test", resolved_at=datetime.now(timezone.utc),
        )
        status, value, pnl = determine_position_outcome(
            settlement, "YES", Decimal("10"), Decimal("6.20"), Decimal("0.20"),
        )
        assert status == PaperPositionStatus.VOID
        assert value == Decimal("6.00")
        assert pnl == Decimal("-0.20")

    def test_raises_if_called_on_an_unresolved_settlement(self):
        from src.execution.paper.models import PaperSettlementResult
        settlement = PaperSettlementResult(
            provider="kalshi", market_id="M1", resolved=False, winning_side=None,
            payout_per_contract=None, source="unresolved", resolved_at=None,
        )
        with pytest.raises(ValueError):
            determine_position_outcome(settlement, "YES", Decimal("10"), Decimal("6"), Decimal("0.2"))
