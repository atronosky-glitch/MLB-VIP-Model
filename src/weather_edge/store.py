"""Persistence for Weather Edge: schema (SQLite + PostgreSQL compatible), idempotent writers, readers.

Design rules that make the track record trustworthy:

* Forecast snapshots, market-price snapshots and predictions are written WHEN THEY ARE MADE and are
  never updated afterwards; outcomes are attached later in separate columns/rows. A prediction row
  therefore always proves what the model believed before the result was known.
* Every writer is idempotent via a natural unique key, so a retried or overlapping job cannot create
  duplicate predictions, signals, paper trades or alerts.
* ``is_paper`` / ``source`` columns keep simulated and real results permanently separable. Real
  order placement does not exist in this module (analysis, alerts and paper trading only).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS weather_scan_runs (
        scan_id          TEXT PRIMARY KEY,
        started_at       TEXT NOT NULL,
        finished_at      TEXT,
        status           TEXT NOT NULL DEFAULT 'running',
        contracts_seen   INTEGER NOT NULL DEFAULT 0,
        contracts_modeled INTEGER NOT NULL DEFAULT 0,
        signals_created  INTEGER NOT NULL DEFAULT 0,
        alerts_sent      INTEGER NOT NULL DEFAULT 0,
        census_json      TEXT,
        errors_json      TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS weather_contracts (
        platform         TEXT NOT NULL,
        market_id        TEXT NOT NULL,
        event_key        TEXT NOT NULL,
        title            TEXT,
        metric           TEXT NOT NULL,
        target_date      TEXT,
        station_icao     TEXT,
        station_label    TEXT,
        settlement_source TEXT,
        bin_lo           INTEGER,
        bin_hi           INTEGER,
        unit             TEXT NOT NULL DEFAULT 'F',
        tz               TEXT,
        status           TEXT NOT NULL DEFAULT 'open',
        close_time       TEXT,
        fee_model        TEXT,
        fee_param        REAL,
        min_order        REAL,
        modeled          INTEGER NOT NULL DEFAULT 0,
        unmodeled_reason TEXT,
        rules_text       TEXT,
        first_seen_at    TEXT NOT NULL,
        last_seen_at     TEXT NOT NULL,
        settled_value    REAL,
        settled_result   TEXT,
        settled_at       TEXT,
        PRIMARY KEY (platform, market_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_weather_contracts_event ON weather_contracts(platform, event_key)",
    "CREATE INDEX IF NOT EXISTS idx_weather_contracts_date ON weather_contracts(target_date, station_icao)",
    """CREATE TABLE IF NOT EXISTS weather_forecast_snapshots (
        snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        station_icao     TEXT NOT NULL,
        metric           TEXT NOT NULL,
        target_date      TEXT NOT NULL,
        fetched_at       TEXT NOT NULL,
        lead_days        INTEGER NOT NULL,
        n_members        INTEGER NOT NULL,
        ensemble_mean    REAL,
        ensemble_std     REAL,
        members_json     TEXT NOT NULL,
        nws_point        REAL,
        observed_so_far  REAL,
        same_day         INTEGER NOT NULL DEFAULT 0,
        source_note      TEXT,
        UNIQUE (station_icao, metric, target_date, fetched_at)
    )""",
    """CREATE TABLE IF NOT EXISTS weather_market_snapshots (
        snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
        platform         TEXT NOT NULL,
        market_id        TEXT NOT NULL,
        captured_at      TEXT NOT NULL,
        yes_bid          REAL,
        yes_ask          REAL,
        no_ask           REAL,
        yes_depth_json   TEXT,
        no_depth_json    TEXT,
        depth_known      INTEGER NOT NULL DEFAULT 0,
        liquidity_usd    REAL,
        volume           REAL,
        status           TEXT,
        UNIQUE (platform, market_id, captured_at)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_weather_market_snapshots_mkt ON weather_market_snapshots(platform, market_id, captured_at)",
    """CREATE TABLE IF NOT EXISTS weather_predictions (
        prediction_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_key   TEXT NOT NULL UNIQUE,
        scan_id          TEXT,
        platform         TEXT NOT NULL,
        market_id        TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        model_version    TEXT NOT NULL,
        lead_days        INTEGER NOT NULL,
        p_yes            REAL NOT NULL,
        p_yes_low        REAL,
        p_yes_high       REAL,
        baseline_p_yes   REAL,
        market_yes_mid   REAL,
        bias_f           REAL,
        residual_std_f   REAL,
        calibrated       INTEGER NOT NULL DEFAULT 0,
        forecast_snapshot_id INTEGER,
        market_snapshot_id   INTEGER,
        outcome_yes      INTEGER,
        resolved_at      TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_weather_predictions_mkt ON weather_predictions(platform, market_id)",
    """CREATE TABLE IF NOT EXISTS weather_signals (
        signal_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_key       TEXT NOT NULL UNIQUE,
        prediction_id    INTEGER NOT NULL,
        scan_id          TEXT,
        platform         TEXT NOT NULL,
        market_id        TEXT NOT NULL,
        created_at       TEXT NOT NULL,
        side             TEXT NOT NULL,
        decision         TEXT NOT NULL,
        reasons          TEXT,
        qty              REAL,
        best_price       REAL,
        avg_price        REAL,
        fee              REAL,
        total_cost       REAL,
        p_win            REAL,
        gross_ev         REAL,
        net_ev           REAL,
        net_ev_pct       REAL,
        conservative_net_ev REAL,
        break_even_prob  REAL,
        spread           REAL,
        liquidity_usd    REAL,
        confidence       TEXT,
        stale            INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_weather_signals_mkt ON weather_signals(platform, market_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_weather_signals_decision ON weather_signals(decision, created_at)",
    """CREATE TABLE IF NOT EXISTS weather_paper_trades (
        trade_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id        INTEGER NOT NULL UNIQUE,
        platform         TEXT NOT NULL,
        market_id        TEXT NOT NULL,
        side             TEXT NOT NULL,
        qty              REAL NOT NULL,
        total_cost       REAL NOT NULL,
        p_win            REAL NOT NULL,
        opened_at        TEXT NOT NULL,
        is_paper         INTEGER NOT NULL DEFAULT 1,
        status           TEXT NOT NULL DEFAULT 'open',
        won              INTEGER,
        pnl              REAL,
        settled_at       TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_weather_paper_trades_status ON weather_paper_trades(status)",
    """CREATE TABLE IF NOT EXISTS weather_calibration (
        platform         TEXT NOT NULL,
        station_icao     TEXT NOT NULL,
        metric           TEXT NOT NULL,
        lead_bucket      TEXT NOT NULL,
        n                INTEGER NOT NULL,
        bias_f           REAL NOT NULL,
        residual_std_f   REAL NOT NULL,
        bias_se_f        REAL NOT NULL,
        fitted_at        TEXT NOT NULL,
        source           TEXT NOT NULL,
        PRIMARY KEY (platform, station_icao, metric, lead_bucket)
    )""",
    """CREATE TABLE IF NOT EXISTS weather_alert_log (
        alert_key        TEXT PRIMARY KEY,
        signal_id        INTEGER,
        channel          TEXT NOT NULL,
        sent_at          TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS weather_watchlist (
        station_icao     TEXT NOT NULL,
        metric           TEXT NOT NULL,
        min_net_ev_pct   REAL,
        created_at       TEXT NOT NULL,
        PRIMARY KEY (station_icao, metric)
    )""",
    """CREATE TABLE IF NOT EXISTS weather_backtests (
        run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at       TEXT NOT NULL,
        label            TEXT NOT NULL,
        params_json      TEXT NOT NULL,
        summary_json     TEXT NOT NULL
    )""",
)

WEATHER_SCHEMA_SQL = ";\n".join(SCHEMA_STATEMENTS) + ";"


def create_schema(conn: Any) -> None:
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _f(x) -> float | None:
    return None if x is None else float(x)


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _one(cur) -> dict | None:
    r = cur.fetchone()
    return dict(r) if r is not None else None


# ---------------------------------------------------------------------------------------- scan runs
def start_scan(conn: Any, scan_id: str, now: datetime) -> None:
    conn.execute(
        "INSERT INTO weather_scan_runs (scan_id, started_at) VALUES (?, ?) ON CONFLICT (scan_id) DO NOTHING",
        (scan_id, _iso(now)),
    )
    conn.commit()


def finish_scan(conn: Any, scan_id: str, now: datetime, status: str, *, seen: int, modeled: int,
                signals: int, alerts: int, census: dict, errors: list[str]) -> None:
    conn.execute(
        """UPDATE weather_scan_runs SET finished_at = ?, status = ?, contracts_seen = ?, contracts_modeled = ?,
               signals_created = ?, alerts_sent = ?, census_json = ?, errors_json = ? WHERE scan_id = ?""",
        (_iso(now), status, seen, modeled, signals, alerts, json.dumps(census), json.dumps(errors[:50]), scan_id),
    )
    conn.commit()


def latest_scan(conn: Any, only_finished: bool = True) -> dict | None:
    where = "WHERE finished_at IS NOT NULL" if only_finished else ""
    return _one(conn.execute(f"SELECT * FROM weather_scan_runs {where} ORDER BY started_at DESC LIMIT 1"))


# ----------------------------------------------------------------------------------------- contracts
def upsert_contract(conn: Any, c, now: datetime) -> None:
    conn.execute(
        """INSERT INTO weather_contracts (
               platform, market_id, event_key, title, metric, target_date, station_icao, station_label,
               settlement_source, bin_lo, bin_hi, unit, tz, status, close_time, fee_model, fee_param,
               min_order, modeled, unmodeled_reason, rules_text, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (platform, market_id) DO UPDATE SET
               title = excluded.title, status = excluded.status, close_time = excluded.close_time,
               modeled = excluded.modeled, unmodeled_reason = excluded.unmodeled_reason,
               station_label = excluded.station_label, settlement_source = excluded.settlement_source,
               fee_param = excluded.fee_param, rules_text = excluded.rules_text,
               last_seen_at = excluded.last_seen_at""",
        (
            c.platform, c.market_id, c.event_key, c.title, c.metric,
            c.target_date.isoformat() if c.target_date else None, c.station_icao, c.station_label,
            c.settlement_source, c.bin.lo if c.bin else None, c.bin.hi if c.bin else None, c.unit, c.tz,
            c.status, _iso(c.close_time), c.fee_model, _f(c.fee_param), _f(c.min_order), 1 if c.modeled else 0,
            c.unmodeled_reason, (c.rules_text or "")[:4000], _iso(now), _iso(now),
        ),
    )


def get_contract(conn: Any, platform: str, market_id: str) -> dict | None:
    return _one(conn.execute("SELECT * FROM weather_contracts WHERE platform = ? AND market_id = ?", (platform, market_id)))


def record_settlement(conn: Any, platform: str, market_id: str, result: str, value: float | None, now: datetime) -> None:
    conn.execute(
        """UPDATE weather_contracts SET status = 'settled', settled_result = ?, settled_value = ?, settled_at = ?
           WHERE platform = ? AND market_id = ?""",
        (result, value, _iso(now), platform, market_id),
    )


def unsettled_contracts_past(conn: Any, today_iso: str) -> list[dict]:
    """Contracts we predicted on whose target date has passed but have no recorded result yet."""
    return _rows(conn.execute(
        """SELECT * FROM weather_contracts WHERE settled_result IS NULL AND target_date < ?
           AND (platform, market_id) IN (SELECT platform, market_id FROM weather_predictions)""",
        (today_iso,),
    ))


# ------------------------------------------------------------------------------------ snapshots
def insert_forecast_snapshot(conn: Any, bundle, lead_days: int) -> int:
    vals = bundle.all_values
    mean = sum(vals) / len(vals) if vals else None
    std = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5 if vals else None
    conn.execute(
        """INSERT INTO weather_forecast_snapshots (
               station_icao, metric, target_date, fetched_at, lead_days, n_members, ensemble_mean, ensemble_std,
               members_json, nws_point, observed_so_far, same_day, source_note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (station_icao, metric, target_date, fetched_at) DO NOTHING""",
        (bundle.station_icao, bundle.metric, bundle.target_date.isoformat(), _iso(bundle.fetched_at), lead_days,
         len(vals), mean, std, json.dumps({m.family: list(m.values) for m in bundle.members}), bundle.nws_point,
         bundle.observed_so_far, 1 if bundle.same_day else 0, bundle.source_note),
    )
    row = _one(conn.execute(
        """SELECT snapshot_id FROM weather_forecast_snapshots
           WHERE station_icao = ? AND metric = ? AND target_date = ? AND fetched_at = ?""",
        (bundle.station_icao, bundle.metric, bundle.target_date.isoformat(), _iso(bundle.fetched_at)),
    ))
    return int(row["snapshot_id"])


def insert_market_snapshot(conn: Any, c, quote, now: datetime) -> int:
    def depth(levels):
        return json.dumps([[str(lv.price), str(lv.quantity)] for lv in levels])
    captured = _iso(quote.fetched_at or now)
    conn.execute(
        """INSERT INTO weather_market_snapshots (
               platform, market_id, captured_at, yes_bid, yes_ask, no_ask, yes_depth_json, no_depth_json,
               depth_known, liquidity_usd, volume, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (platform, market_id, captured_at) DO NOTHING""",
        (c.platform, c.market_id, captured, _f(quote.yes_bid), _f(quote.yes_ask), _f(quote.no_ask),
         depth(quote.yes_asks), depth(quote.no_asks), 1 if quote.depth_known else 0, _f(quote.liquidity_usd),
         _f(quote.volume), c.status),
    )
    row = _one(conn.execute(
        "SELECT snapshot_id FROM weather_market_snapshots WHERE platform = ? AND market_id = ? AND captured_at = ?",
        (c.platform, c.market_id, captured),
    ))
    return int(row["snapshot_id"])


def price_history(conn: Any, platform: str, market_id: str, limit: int = 500) -> list[dict]:
    return _rows(conn.execute(
        """SELECT captured_at, yes_bid, yes_ask, no_ask FROM weather_market_snapshots
           WHERE platform = ? AND market_id = ? ORDER BY captured_at DESC LIMIT ?""",
        (platform, market_id, limit),
    ))[::-1]


# ------------------------------------------------------------------------- predictions / signals
def insert_prediction(conn: Any, p: dict) -> tuple[int, bool]:
    """(prediction_id, created). A repeat of the same key (same contract, same decision hour) is a no-op."""
    before = _one(conn.execute("SELECT prediction_id FROM weather_predictions WHERE prediction_key = ?", (p["prediction_key"],)))
    if before:
        return int(before["prediction_id"]), False
    conn.execute(
        """INSERT INTO weather_predictions (
               prediction_key, scan_id, platform, market_id, created_at, model_version, lead_days, p_yes,
               p_yes_low, p_yes_high, baseline_p_yes, market_yes_mid, bias_f, residual_std_f, calibrated,
               forecast_snapshot_id, market_snapshot_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (prediction_key) DO NOTHING""",
        (p["prediction_key"], p.get("scan_id"), p["platform"], p["market_id"], p["created_at"], p["model_version"],
         p["lead_days"], p["p_yes"], p.get("p_yes_low"), p.get("p_yes_high"), p.get("baseline_p_yes"),
         p.get("market_yes_mid"), p.get("bias_f"), p.get("residual_std_f"), 1 if p.get("calibrated") else 0,
         p.get("forecast_snapshot_id"), p.get("market_snapshot_id")),
    )
    row = _one(conn.execute("SELECT prediction_id FROM weather_predictions WHERE prediction_key = ?", (p["prediction_key"],)))
    return int(row["prediction_id"]), True


def insert_signal(conn: Any, s: dict) -> tuple[int, bool]:
    before = _one(conn.execute("SELECT signal_id FROM weather_signals WHERE signal_key = ?", (s["signal_key"],)))
    if before:
        return int(before["signal_id"]), False
    conn.execute(
        """INSERT INTO weather_signals (
               signal_key, prediction_id, scan_id, platform, market_id, created_at, side, decision, reasons, qty,
               best_price, avg_price, fee, total_cost, p_win, gross_ev, net_ev, net_ev_pct, conservative_net_ev,
               break_even_prob, spread, liquidity_usd, confidence, stale)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (signal_key) DO NOTHING""",
        (s["signal_key"], s["prediction_id"], s.get("scan_id"), s["platform"], s["market_id"], s["created_at"],
         s["side"], s["decision"], s.get("reasons"), s.get("qty"), s.get("best_price"), s.get("avg_price"),
         s.get("fee"), s.get("total_cost"), s.get("p_win"), s.get("gross_ev"), s.get("net_ev"), s.get("net_ev_pct"),
         s.get("conservative_net_ev"), s.get("break_even_prob"), s.get("spread"), s.get("liquidity_usd"),
         s.get("confidence"), 1 if s.get("stale") else 0),
    )
    row = _one(conn.execute("SELECT signal_id FROM weather_signals WHERE signal_key = ?", (s["signal_key"],)))
    return int(row["signal_id"]), True


def insert_paper_trade(conn: Any, t: dict) -> tuple[int, bool]:
    before = _one(conn.execute("SELECT trade_id FROM weather_paper_trades WHERE signal_id = ?", (t["signal_id"],)))
    if before:
        return int(before["trade_id"]), False
    conn.execute(
        """INSERT INTO weather_paper_trades (signal_id, platform, market_id, side, qty, total_cost, p_win, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (signal_id) DO NOTHING""",
        (t["signal_id"], t["platform"], t["market_id"], t["side"], t["qty"], t["total_cost"], t["p_win"], t["opened_at"]),
    )
    row = _one(conn.execute("SELECT trade_id FROM weather_paper_trades WHERE signal_id = ?", (t["signal_id"],)))
    return int(row["trade_id"]), True


def open_paper_exposure(conn: Any, platform: str, event_key: str) -> float:
    """Dollars currently at risk in open paper trades on one event (correlated outcomes of one forecast)."""
    row = _one(conn.execute(
        """SELECT COALESCE(SUM(t.total_cost), 0) AS exposure FROM weather_paper_trades t
           JOIN weather_contracts c ON c.platform = t.platform AND c.market_id = t.market_id
           WHERE t.status = 'open' AND t.platform = ? AND c.event_key = ?""",
        (platform, event_key),
    ))
    return float(row["exposure"]) if row else 0.0


def settle_predictions(conn: Any, platform: str, market_id: str, outcome_yes: bool, now: datetime) -> None:
    conn.execute(
        """UPDATE weather_predictions SET outcome_yes = ?, resolved_at = ?
           WHERE platform = ? AND market_id = ? AND outcome_yes IS NULL""",
        (1 if outcome_yes else 0, _iso(now), platform, market_id),
    )


def open_paper_trades(conn: Any, platform: str, market_id: str) -> list[dict]:
    return _rows(conn.execute(
        "SELECT * FROM weather_paper_trades WHERE status = 'open' AND platform = ? AND market_id = ?",
        (platform, market_id),
    ))


def settle_paper_trade(conn: Any, trade_id: int, won: bool, pnl: Decimal | float, now: datetime) -> None:
    conn.execute(
        """UPDATE weather_paper_trades SET status = 'settled', won = ?, pnl = ?, settled_at = ?
           WHERE trade_id = ? AND status = 'open'""",
        (1 if won else 0, float(pnl), _iso(now), trade_id),
    )


# ---------------------------------------------------------------------------------- calibration
def upsert_calibration(conn: Any, row: dict) -> None:
    conn.execute(
        """INSERT INTO weather_calibration (platform, station_icao, metric, lead_bucket, n, bias_f, residual_std_f,
               bias_se_f, fitted_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (platform, station_icao, metric, lead_bucket) DO UPDATE SET n = excluded.n, bias_f = excluded.bias_f,
               residual_std_f = excluded.residual_std_f, bias_se_f = excluded.bias_se_f,
               fitted_at = excluded.fitted_at, source = excluded.source""",
        (row["platform"], row["station_icao"], row["metric"], row["lead_bucket"], row["n"], row["bias_f"], row["residual_std_f"],
         row["bias_se_f"], row["fitted_at"], row["source"]),
    )


def get_calibration(conn: Any, platform: str, station_icao: str, metric: str, lead_bucket: str) -> dict | None:
    return _one(conn.execute(
        "SELECT * FROM weather_calibration WHERE platform = ? AND station_icao = ? AND metric = ? AND lead_bucket = ?",
        (platform, station_icao, metric, lead_bucket),
    ))


def all_calibration(conn: Any) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM weather_calibration ORDER BY platform, station_icao, metric, lead_bucket"))


# ---------------------------------------------------------------------------- alerts / watchlist
def alert_sent(conn: Any, alert_key: str) -> bool:
    return _one(conn.execute("SELECT alert_key FROM weather_alert_log WHERE alert_key = ?", (alert_key,))) is not None


def mark_alert_sent(conn: Any, alert_key: str, signal_id: int | None, channel: str, now: datetime) -> bool:
    """True when this call recorded the alert (False = another run already did: do not send again)."""
    if alert_sent(conn, alert_key):
        return False
    conn.execute(
        "INSERT INTO weather_alert_log (alert_key, signal_id, channel, sent_at) VALUES (?, ?, ?, ?) ON CONFLICT (alert_key) DO NOTHING",
        (alert_key, signal_id, channel, _iso(now)),
    )
    return True


def unmark_alert_sent(conn: Any, alert_key: str) -> None:
    conn.execute("DELETE FROM weather_alert_log WHERE alert_key = ?", (alert_key,))


def watchlist(conn: Any) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM weather_watchlist ORDER BY station_icao, metric"))


def watch(conn: Any, station_icao: str, metric: str, min_net_ev_pct: float | None, now: datetime) -> None:
    conn.execute(
        """INSERT INTO weather_watchlist (station_icao, metric, min_net_ev_pct, created_at) VALUES (?, ?, ?, ?)
           ON CONFLICT (station_icao, metric) DO UPDATE SET min_net_ev_pct = excluded.min_net_ev_pct""",
        (station_icao, metric, min_net_ev_pct, _iso(now)),
    )
    conn.commit()


def unwatch(conn: Any, station_icao: str, metric: str) -> None:
    conn.execute("DELETE FROM weather_watchlist WHERE station_icao = ? AND metric = ?", (station_icao, metric))
    conn.commit()


# --------------------------------------------------------------------------------------- backtests
def save_backtest(conn: Any, label: str, params: dict, summary: dict, now: datetime) -> None:
    conn.execute(
        "INSERT INTO weather_backtests (created_at, label, params_json, summary_json) VALUES (?, ?, ?, ?)",
        (_iso(now), label, json.dumps(params, default=str), json.dumps(summary, default=str)),
    )
    conn.commit()


def latest_backtests(conn: Any, limit: int = 5) -> list[dict]:
    return _rows(conn.execute("SELECT * FROM weather_backtests ORDER BY run_id DESC LIMIT ?", (limit,)))
