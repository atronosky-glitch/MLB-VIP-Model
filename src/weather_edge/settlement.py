"""Settlement of predictions and paper trades from the platforms' own final results.

* Kalshi: a market is final when ``result`` is yes/no; ``expiration_value`` is the official reading.
  The result is cross-checked against the contract's outcome range: if the official value and the
  yes/no result DISAGREE (e.g. settled on a different source than our rules parsing assumed) the
  contract is flagged ``disputed`` and NO paper trade or prediction is settled from it.
* Polymarket: final when the event is closed and UMA ``resolved`` with a clean 1/0 price pair.

Unfinished or ambiguous markets are left open; nothing is ever inferred.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from . import store
from .ev import paper_pnl
from .kalshi_source import KalshiWeatherClient, settlement_result as kalshi_result
from .models import Bin
from .polymarket_source import PolymarketWeatherClient, settlement_result as poly_result

logger = logging.getLogger(__name__)


def value_consistent(bin_lo, bin_hi, value: float | None, result: str) -> bool:
    """Does the official reading agree with the platform's yes/no for this outcome range?"""
    if value is None:
        return True            # cannot cross-check (Polymarket gives no value); trust the platform
    b = Bin(bin_lo, bin_hi)
    inside = b.contains(int(round(value)))
    return inside == (result == "yes")


def apply_result(conn, platform: str, market_id: str, result: str, value: float | None, now: datetime) -> dict:
    row = store.get_contract(conn, platform, market_id)
    if row is None:
        return {"status": "unknown_contract"}
    if not value_consistent(row["bin_lo"], row["bin_hi"], value, result):
        store.record_settlement(conn, platform, market_id, "disputed", value, now)
        logger.warning("Weather Edge settlement mismatch for %s %s", platform, market_id)
        return {"status": "disputed"}
    store.record_settlement(conn, platform, market_id, result, value, now)
    store.settle_predictions(conn, platform, market_id, result == "yes", now)
    settled = 0
    for t in store.open_paper_trades(conn, platform, market_id):
        won = (t["side"] == "YES") == (result == "yes")
        pnl = paper_pnl(filled_qty=Decimal(str(t["qty"])), total_cost=Decimal(str(t["total_cost"])), won=won)
        store.settle_paper_trade(conn, t["trade_id"], won, pnl, now)
        settled += 1
    return {"status": "settled", "paper_trades_settled": settled}


def run_settlement(conn, now: datetime, kalshi: KalshiWeatherClient | None = None,
                   polymarket: PolymarketWeatherClient | None = None) -> dict:
    kalshi = kalshi or KalshiWeatherClient()
    polymarket = polymarket or PolymarketWeatherClient()
    out = {"checked": 0, "settled": 0, "disputed": 0, "pending": 0, "errors": []}
    pending = store.unsettled_contracts_past(conn, now.date().isoformat())
    poly_cache: dict[str, dict | None] = {}
    for row in pending:
        out["checked"] += 1
        try:
            if row["platform"] == "kalshi":
                result, value = kalshi_result(kalshi.get_market(row["market_id"]))
            else:
                slug = row["event_key"]
                if slug not in poly_cache:
                    poly_cache[slug] = polymarket.get_event(slug)
                ev = poly_cache[slug] or {}
                mkt = next((m for m in ev.get("markets", [])
                            if str(m.get("conditionId") or m.get("id")) == row["market_id"]), None)
                result, value = (poly_result(mkt) if mkt else None), None
        except Exception as exc:
            out["errors"].append(f"{row['platform']}:{row['market_id']}: {type(exc).__name__}")
            continue
        if result is None:
            out["pending"] += 1
            continue
        res = apply_result(conn, row["platform"], row["market_id"], result, value, now)
        if res["status"] == "settled":
            out["settled"] += 1
        elif res["status"] == "disputed":
            out["disputed"] += 1
    conn.commit()
    return out
