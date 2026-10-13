"""Track-record metrics computed ONLY from recorded predictions, signals and settled outcomes.

Nothing here is estimated or back-filled: if there are no resolved rows the functions return
``{"n": 0, ...}`` and the UI says so. All results are PAPER (simulated fills at recorded executable
prices); real trading results do not exist in this module.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean

from .calibration import brier, log_loss

MIN_SCORED = 30          # below this the UI labels metrics "too few samples to trust"


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def resolved_predictions(conn) -> list[dict]:
    """One prediction per (contract, lead day): the EARLIEST made at that lead, so the heavily
    overlapping repeats of the same forecast are not counted as independent samples."""
    rows = _rows(conn.execute(
        """SELECT p.prediction_id, p.platform, p.market_id, p.lead_days, p.p_yes, p.baseline_p_yes,
                  p.market_yes_mid, p.outcome_yes, p.created_at, c.station_icao, c.metric, c.target_date
           FROM weather_predictions p
           JOIN weather_contracts c ON c.platform = p.platform AND c.market_id = p.market_id
           WHERE p.outcome_yes IS NOT NULL
           ORDER BY p.created_at ASC"""
    ))
    seen, out = set(), []
    for r in rows:
        key = (r["platform"], r["market_id"], r["lead_days"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _score(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    y = [bool(r["outcome_yes"]) for r in rows]
    out = {
        "n": n,
        "brier_model": mean(brier(r["p_yes"], o) for r, o in zip(rows, y)),
        "logloss_model": mean(log_loss(r["p_yes"], o) for r, o in zip(rows, y)),
        "reliable": n >= MIN_SCORED,
    }
    with_mid = [(r, o) for r, o in zip(rows, y) if r["market_yes_mid"] is not None]
    if with_mid:
        out["n_vs_market"] = len(with_mid)
        out["brier_model_same_rows"] = mean(brier(r["p_yes"], o) for r, o in with_mid)
        out["brier_market"] = mean(brier(r["market_yes_mid"], o) for r, o in with_mid)
    with_base = [(r, o) for r, o in zip(rows, y) if r["baseline_p_yes"] is not None]
    if with_base:
        out["brier_baseline"] = mean(brier(r["baseline_p_yes"], o) for r, o in with_base)
    return out


def prediction_scores(conn) -> dict:
    rows = resolved_predictions(conn)
    overall = _score(rows)

    def grouped(keyfn):
        g = defaultdict(list)
        for r in rows:
            g[keyfn(r)].append(r)
        return {k: _score(v) for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}

    return {
        "overall": overall,
        "by_lead_days": grouped(lambda r: r["lead_days"]),
        "by_station": grouped(lambda r: r["station_icao"]),
        "by_platform": grouped(lambda r: r["platform"]),
        "by_metric": grouped(lambda r: r["metric"]),
        "reliability": reliability_table(rows),
    }


def reliability_table(rows: list[dict], bins: int = 10) -> list[dict]:
    """Predicted probability vs how often YES actually happened, per probability bucket."""
    buckets: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[min(int(r["p_yes"] * bins), bins - 1)].append(r)
    out = []
    for b in sorted(buckets):
        rs = buckets[b]
        out.append({
            "bucket": f"{b / bins:.1f}-{(b + 1) / bins:.1f}", "n": len(rs),
            "mean_predicted": mean(r["p_yes"] for r in rs),
            "observed_freq": mean(1.0 if r["outcome_yes"] else 0.0 for r in rs),
        })
    return out


def settled_trades(conn) -> list[dict]:
    return _rows(conn.execute(
        """SELECT t.trade_id, t.platform, t.market_id, t.side, t.qty, t.total_cost, t.p_win, t.won, t.pnl,
                  t.settled_at, s.net_ev, s.net_ev_pct, p.lead_days, c.station_icao, c.metric
           FROM weather_paper_trades t
           JOIN weather_signals s ON s.signal_id = t.signal_id
           JOIN weather_predictions p ON p.prediction_id = s.prediction_id
           JOIN weather_contracts c ON c.platform = t.platform AND c.market_id = t.market_id
           WHERE t.status = 'settled' AND t.is_paper = 1
           ORDER BY t.settled_at ASC, t.trade_id ASC"""
    ))


def _trade_stats(rows: list[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0}
    cost = sum(r["total_cost"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    cum, peak, dd = 0.0, 0.0, 0.0
    for r in rows:
        cum += r["pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {
        "n": n, "wins": sum(1 for r in rows if r["won"]), "win_rate": sum(1 for r in rows if r["won"]) / n,
        "total_cost": cost, "pnl": pnl, "roi_pct": (pnl / cost * 100) if cost else None,
        "avg_predicted_net_ev_per_contract": mean(r["net_ev"] for r in rows),
        "avg_realized_pnl_per_contract": pnl / sum(r["qty"] for r in rows),
        "max_drawdown": dd, "reliable": n >= MIN_SCORED,
    }


def paper_performance(conn) -> dict:
    rows = settled_trades(conn)

    def grouped(keyfn):
        g = defaultdict(list)
        for r in rows:
            g[keyfn(r)].append(r)
        return {k: _trade_stats(v) for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}

    open_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(total_cost), 0) AS cost FROM weather_paper_trades WHERE status = 'open'"
    ).fetchone()
    return {
        "label": "PAPER (simulated fills at recorded executable prices) — not real trading results",
        "overall": _trade_stats(rows),
        "open_positions": {"n": int(open_row["n"]), "cost": float(open_row["cost"])},
        "by_lead_days": grouped(lambda r: r["lead_days"]),
        "by_station": grouped(lambda r: r["station_icao"]),
        "by_platform": grouped(lambda r: r["platform"]),
        "by_metric": grouped(lambda r: r["metric"]),
    }
