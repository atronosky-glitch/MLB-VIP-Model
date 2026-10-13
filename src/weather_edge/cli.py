"""Command line for Weather Edge: ``python -m src.weather_edge.cli <command>``.

Commands: scan | settle | backfill-calibration | backtest | status

``scan`` and ``settle`` write only to the application database and (for alerts) the optional weather
Discord webhook. No command places an order or needs any trading credential.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from .config import WeatherEdgeConfig

logger = logging.getLogger("weather_edge")


def _open():
    from database.db_manager import get_connection, init_db
    init_db()
    return get_connection()


def cmd_scan(args, cfg) -> int:
    from .scanner import run_scan
    conn = _open()
    try:
        s = run_scan(conn, cfg)
    finally:
        conn.close()
    print(f"scan {s.scan_id}: {s.status} | contracts {s.contracts_seen} (modeled {s.contracts_modeled}) | "
          f"predictions {s.predictions} | candidates {s.candidates} | signals {s.signals} | paper trades {s.paper_trades}")
    if s.reject_reasons:
        print("rejections:", json.dumps(s.reject_reasons, sort_keys=True))
    if s.skipped_events:
        print(f"skipped events: {len(s.skipped_events)}")
    for e in s.errors[:10]:
        print("error:", e)
    return 0 if s.status in ("ok", "partial") else 1


def cmd_settle(args, cfg) -> int:
    from .settlement import run_settlement
    conn = _open()
    try:
        out = run_settlement(conn, datetime.now(timezone.utc))
    finally:
        conn.close()
    print(json.dumps(out))
    return 0


def cmd_backfill(args, cfg) -> int:
    from .calibration import backfill
    from .kalshi_source import KalshiWeatherClient
    from .registry import STATIONS
    conn = _open()
    try:
        report = backfill(conn, KalshiWeatherClient(), STATIONS, lookback_days=args.days)
    finally:
        conn.close()
    print(json.dumps(report, indent=2, default=str))
    return 0


def cmd_backtest(args, cfg) -> int:
    from .backtest import run_backtest
    from .kalshi_source import KalshiWeatherClient
    from .registry import STATIONS
    conn = _open()
    try:
        out = run_backtest(conn, KalshiWeatherClient(), STATIONS, cfg, lookback_days=args.days)
    finally:
        conn.close()
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_status(args, cfg) -> int:
    from . import store
    conn = _open()
    try:
        last = store.latest_scan(conn, only_finished=False)
        cal = store.all_calibration(conn)
    finally:
        conn.close()
    print(f"enabled={cfg.enabled} interval={cfg.scan_interval_minutes}m min_net_ev={cfg.min_net_ev_pct}% "
          f"stake=${cfg.paper_stake_usd}")
    print("last scan:", dict(last) if last else "never")
    print(f"calibration buckets: {len(cal)} ({sum(1 for c in cal if c['n'] >= 30)} with >=30 samples)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="weather_edge", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    sub.add_parser("settle")
    sub.add_parser("status")
    for name in ("backfill-calibration", "backtest"):
        p = sub.add_parser(name)
        p.add_argument("--days", type=int, default=90)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = WeatherEdgeConfig.from_env()
    handlers = {"scan": cmd_scan, "settle": cmd_settle, "backfill-calibration": cmd_backfill,
                "backtest": cmd_backtest, "status": cmd_status}
    return handlers[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
