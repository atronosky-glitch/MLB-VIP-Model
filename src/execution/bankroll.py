"""Pure bankroll arithmetic for Stage 3 paper trading. No I/O -- the
DB-touching side (reading paper_accounts / open paper_positions and
constructing the inputs below) lives in src/execution/paper/portfolio.py.

Definitions (Decimal throughout):
    cash               -- dollars not currently tied up in an open
                           position; debited when a paper order fills,
                           credited when a position settles. This is
                           the running total persisted on paper_accounts,
                           never re-derived by summing full history on
                           every read.
    open_position_cost -- SUM(entry_cost) over currently OPEN positions
                           only (a small, bounded set -- capped by
                           MAX_OPEN_POSITIONS), computed on read.
    unrealized_pnl     -- mark-to-market gain/loss on open positions.
                           Re-pricing every open position against a
                           live book on every portfolio read is a real
                           network cost and not "safely calculable" in
                           general, so this stays 0 (documented, not
                           fabricated) unless a caller supplies a real
                           mark.
    equity             -- cash + open_position_cost + unrealized_pnl
                           (positions marked at cost by default).
    available_bankroll -- cash itself (already excludes money tied up
                           in open positions, since that was debited at
                           entry).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Bankroll:
    starting_bankroll: Decimal
    cash: Decimal
    open_position_cost: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    equity: Decimal
    available_bankroll: Decimal


def compute_bankroll(
    starting_bankroll: Decimal,
    cash: Decimal,
    open_position_cost: Decimal,
    realized_pnl: Decimal,
    unrealized_pnl: Decimal = Decimal("0"),
) -> Bankroll:
    equity = cash + open_position_cost + unrealized_pnl
    return Bankroll(
        starting_bankroll=starting_bankroll,
        cash=cash,
        open_position_cost=open_position_cost,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        equity=equity,
        available_bankroll=cash,
    )
