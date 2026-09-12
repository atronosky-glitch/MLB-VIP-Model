"""Tests for src/execution/opportunity_store.py against the db_conn
fixture (full schema via tests/conftest.py, including
execution_opportunities). Mirrors tests/test_market_match_store.py."""

from datetime import datetime, timezone
from decimal import Decimal

from src.execution.evaluator import ExecutionOpportunity, ExecutionRejection, RejectionReason
from src.execution.opportunity_store import (
    get_existing_opportunity, persist_opportunity, persist_rejection,
)


def _opportunity(provider="kalshi", net_ev_pct="12.34") -> ExecutionOpportunity:
    now = datetime.now(timezone.utc)
    return ExecutionOpportunity(
        recommendation_id="rec-1", league="MLB", event="Athletics @ Toronto Blue Jays",
        market="moneyline", side="YES", model_probability=Decimal("0.70"),
        provider=provider, provider_market_id="M1", match_confidence=0.99,
        best_bid=Decimal("0.60"), best_ask=Decimal("0.62"), spread=Decimal("0.02"),
        analysis_stake_usd=Decimal("10"), quantity_analyzed=Decimal("16"),
        expected_fill_price=Decimal("0.62"), estimated_fees=Decimal("0.05"),
        expected_slippage=Decimal("0"), available_liquidity=Decimal("620"),
        raw_ev_pct=Decimal("14.0"), net_ev_pct=Decimal(net_ev_pct),
        max_acceptable_price=Decimal("0.65"),
        market_data_timestamp=now, signal_timestamp=now, generated_at=now,
        expiration_time=now,
    )


def _rejection(provider="kalshi", reason=RejectionReason.NET_EV_TOO_LOW) -> ExecutionRejection:
    return ExecutionRejection(
        recommendation_id="rec-1", provider=provider, league="MLB",
        event="Athletics @ Toronto Blue Jays", market="moneyline", side="YES",
        reason=reason, detail="test", generated_at=datetime.now(timezone.utc),
    )


class TestPersistOpportunity:
    def test_no_existing_opportunity_returns_none(self, db_conn):
        assert get_existing_opportunity(db_conn, "rec-1", "kalshi") is None

    def test_persisted_opportunity_is_retrievable(self, db_conn):
        persist_opportunity(db_conn, _opportunity())
        stored = get_existing_opportunity(db_conn, "rec-1", "kalshi")
        assert stored is not None
        assert stored["status"] == "qualified"
        assert stored["net_ev_pct"] == 12.34

    def test_scoped_by_provider(self, db_conn):
        persist_opportunity(db_conn, _opportunity(provider="kalshi"))
        assert get_existing_opportunity(db_conn, "rec-1", "polymarket_us") is None


class TestPersistRejection:
    def test_persisted_rejection_is_retrievable(self, db_conn):
        persist_rejection(db_conn, _rejection())
        stored = get_existing_opportunity(db_conn, "rec-1", "kalshi")
        assert stored is not None
        assert stored["status"] == "rejected"
        assert stored["rejection_reason"] == "NET_EV_TOO_LOW"

    def test_rejection_reason_is_the_enum_string_value(self, db_conn):
        persist_rejection(db_conn, _rejection(reason=RejectionReason.SPREAD_TOO_WIDE))
        stored = get_existing_opportunity(db_conn, "rec-1", "kalshi")
        assert stored["rejection_reason"] == "SPREAD_TOO_WIDE"


class TestGetExistingOpportunityReturnsMostRecent:
    def test_returns_the_latest_of_multiple_evaluations(self, db_conn):
        persist_rejection(db_conn, _rejection())
        persist_opportunity(db_conn, _opportunity())
        stored = get_existing_opportunity(db_conn, "rec-1", "kalshi")
        assert stored["status"] == "qualified"


class TestRepeatedScansAreIntentionallyNotDeduplicated:
    """Item 15: a scan re-evaluating the same (recommendation, provider)
    pair always inserts a new row rather than upserting one -- documented
    here as the intentional identity scheme for Stage 2B, since every
    evaluation (not just the latest) is kept for later provider-
    performance analysis. There is no order-dedup concern yet because no
    order table exists at this stage."""

    def test_re_scanning_the_same_pair_inserts_a_second_row_not_an_error(self, db_conn):
        persist_opportunity(db_conn, _opportunity(net_ev_pct="5.0"))
        persist_opportunity(db_conn, _opportunity(net_ev_pct="7.0"))
        rows = db_conn.execute(
            "SELECT net_ev_pct FROM execution_opportunities WHERE recommendation_id = ? AND provider = ?",
            ("rec-1", "kalshi"),
        ).fetchall()
        assert len(rows) == 2


class TestRejectionReasonEnumSerialization:
    """Item 4: the full canonical RejectionReason set must round-trip
    through persistence without corruption -- not just the two values
    already exercised above."""

    def test_every_rejection_reason_value_round_trips_through_the_store(self, db_conn):
        for reason in RejectionReason:
            persist_rejection(db_conn, _rejection(provider=f"kalshi-{reason.value}", reason=reason))
            stored = get_existing_opportunity(db_conn, "rec-1", f"kalshi-{reason.value}")
            assert stored["rejection_reason"] == reason.value
