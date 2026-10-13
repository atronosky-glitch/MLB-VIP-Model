"""Discord alerts for Weather Edge signals, reusing the existing webhook delivery code.

* Uses its OWN webhook (``WEATHER_EDGE_DISCORD_WEBHOOK_URL``) so sports alert channels are untouched.
* At-most-once per contract/side/local day: the alert key is recorded BEFORE sending and removed
  again only if the send fails, so overlapping jobs cannot double-post and a failed send retries.
* Honors the watchlist: when any watchlist entry exists, only matching stations/metrics alert.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from . import store
from .config import WeatherEdgeConfig

logger = logging.getLogger(__name__)


def _plain(text) -> str:
    """Provider-supplied text goes into a Discord message: neutralize mentions so it cannot ping anyone."""
    return str(text).replace("@", "(at)")


def format_alert(item: dict) -> str:
    item = {**item, "station_label": _plain(item["station_label"]), "bin_label": _plain(item["bin_label"])}
    return (
        f"**Weather Edge** (estimate, paper-only analysis)\n"
        f"{item['platform'].title()} | {item['station_label']} | {item['metric']} on {item['target_date']}\n"
        f"Buy **{item['side']}** on {item['bin_label']} at avg ${item['avg_price']:.3f} "
        f"(model {item['p_win'] * 100:.1f}% vs break-even {item['break_even_prob'] * 100:.1f}%)\n"
        f"Net EV after fees/slippage: {item['net_ev_pct']:+.1f}% of cost "
        f"(${item['net_ev']:+.3f}/contract) | confidence {item['confidence']}\n"
        f"Not financial advice. Model estimates can be wrong; verify the market rules before acting."
    )


def _eligible(item: dict, cfg: WeatherEdgeConfig, watch: list[dict]) -> bool:
    threshold = cfg.alert_min_net_ev_pct
    if watch:
        match = [w for w in watch if w["station_icao"] == item["station_icao"] and w["metric"] == item["metric"]]
        if not match:
            return False
        own = match[0].get("min_net_ev_pct")
        threshold = own if own is not None else threshold
    return item["net_ev_pct"] >= threshold


def deliver_signal_alerts(
    conn, cfg: WeatherEdgeConfig, items: list[dict], now: datetime,
    sender: Callable[[str, str], bool] | None = None,
) -> dict:
    """``items`` are SIGNAL rows enriched with contract fields. Returns counts; never raises."""
    result = {"sent": 0, "skipped_duplicate": 0, "skipped_filter": 0, "failed": 0, "disabled": False}
    if not cfg.alerts_enabled or not cfg.discord_webhook_url:
        result["disabled"] = True
        return result
    if sender is None:
        from src.discord_delivery import send_webhook_message

        def sender(url: str, text: str) -> bool:  # noqa: E306
            return send_webhook_message(url, text)

    watch = store.watchlist(conn)
    day = now.date().isoformat()
    for item in items:
        if not _eligible(item, cfg, watch):
            result["skipped_filter"] += 1
            continue
        key = f"{item['platform']}|{item['market_id']}|{item['side']}|{day}"
        if not store.mark_alert_sent(conn, key, item.get("signal_id"), "discord", now):
            result["skipped_duplicate"] += 1
            continue
        conn.commit()
        try:
            ok = sender(cfg.discord_webhook_url, format_alert(item))
        except Exception as exc:  # never let delivery break a scan
            logger.warning("Weather Edge alert send raised %s", type(exc).__name__)
            ok = False
        if ok:
            result["sent"] += 1
        else:
            store.unmark_alert_sent(conn, key)
            conn.commit()
            result["failed"] += 1
    return result
