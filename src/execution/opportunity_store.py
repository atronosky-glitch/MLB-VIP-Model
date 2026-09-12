"""Persistence for src/execution/evaluator.py's results, against the
execution_opportunities table. Mirrors market_match_store.py's split
from matching.py -- evaluator.py stays testable with fake providers
and zero DB fixture setup.

Identity/dedup scheme (item 15): persist_opportunity/persist_rejection
always INSERT a new row -- there is no upsert keyed on
(recommendation_id, provider). This is intentional, not an oversight:
Stage 2B's whole point for persistence is capturing every provider
evaluation over time for later performance analysis (item 14), not
just the latest one, so re-scanning the same recommendation/provider
pair across multiple CLI runs is expected to accumulate history rather
than overwrite it. get_existing_opportunity resolves "the current
view" by picking the most recent row (ORDER BY opportunity_id DESC),
so callers that only care about "what's true right now" still get a
single, well-defined answer. There is no order-level dedup concern at
this stage since no order table exists yet -- that's a later stage's
problem once real order submission exists.
"""

from __future__ import annotations

from typing import Any

from src.execution.evaluator import ExecutionOpportunity, ExecutionRejection


def persist_opportunity(conn: Any, opportunity: ExecutionOpportunity) -> None:
    conn.execute(
        """INSERT INTO execution_opportunities (
               recommendation_id, provider, provider_market_id, model_probability,
               best_bid, best_ask, expected_fill_price, quantity_analyzed, analysis_stake,
               estimated_fees, slippage, raw_ev_pct, net_ev_pct, max_acceptable_price,
               liquidity, status, market_data_timestamp
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            opportunity.recommendation_id, opportunity.provider, opportunity.provider_market_id,
            float(opportunity.model_probability),
            float(opportunity.best_bid) if opportunity.best_bid is not None else None,
            float(opportunity.best_ask) if opportunity.best_ask is not None else None,
            float(opportunity.expected_fill_price) if opportunity.expected_fill_price is not None else None,
            float(opportunity.quantity_analyzed), float(opportunity.analysis_stake_usd),
            float(opportunity.estimated_fees),
            float(opportunity.expected_slippage) if opportunity.expected_slippage is not None else None,
            float(opportunity.raw_ev_pct), float(opportunity.net_ev_pct),
            float(opportunity.max_acceptable_price), float(opportunity.available_liquidity),
            "qualified", opportunity.market_data_timestamp.isoformat(),
        ),
    )
    conn.commit()


def persist_rejection(conn: Any, rejection: ExecutionRejection) -> None:
    conn.execute(
        """INSERT INTO execution_opportunities (
               recommendation_id, provider, status, rejection_reason
           ) VALUES (?, ?, ?, ?)""",
        (rejection.recommendation_id, rejection.provider, "rejected", rejection.reason.value),
    )
    conn.commit()


def get_existing_opportunity(conn: Any, recommendation_id: str, provider: str) -> dict | None:
    """The most recently persisted evaluation for this (recommendation,
    provider) pair, or None if never evaluated."""
    row = conn.execute(
        """SELECT * FROM execution_opportunities
           WHERE recommendation_id = ? AND provider = ?
           ORDER BY opportunity_id DESC LIMIT 1""",
        (recommendation_id, provider),
    ).fetchone()
    return dict(row) if row is not None else None
