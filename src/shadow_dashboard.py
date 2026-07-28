"""Shadow-run summary dashboard.

Aggregates all shadow-mode activity into a single summary:
- Shadow-mode status
- Recommendation volume (total, by sportsbook, by market, by confidence)
- Delivery attempts (blocked, would-deliver)
- Data-quality findings
- API usage
- Health status
- Readiness status
- Promotion progress

Generates a human-readable dashboard and optional JSON export.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from database.db_manager import get_connection
from src.production_config import load_config, ProductionConfig
from src.shadow_mode import load_shadow_config, ShadowConfig

logger = logging.getLogger(__name__)


@dataclass
class ShadowDashboard:
    """Aggregated shadow-run dashboard."""
    timestamp: str = ""
    shadow_mode: bool = True
    # Recommendations
    total_recommendations: int = 0
    actionable_count: int = 0
    avg_confidence: float = 0.0
    by_sportsbook: dict[str, int] = None  # type: ignore[assignment]
    by_market: dict[str, int] = None  # type: ignore[assignment]
    by_confidence_bucket: dict[str, int] = None  # type: ignore[assignment]
    # Delivery
    delivery_blocked_count: int = 0
    delivery_would_send: int = 0
    # Data quality
    critical_findings: int = 0
    warning_findings: int = 0
    info_findings: int = 0
    # API usage
    api_live_requests: int = 0
    api_cache_hits: int = 0
    # Health
    health_status: str = "unknown"
    # Readiness
    readiness_status: str = "unknown"
    # Promotion
    days_since_shadow_start: int = 0
    promotion_eligible: bool = False

    def __post_init__(self):
        if self.by_sportsbook is None:
            self.by_sportsbook = {}
        if self.by_market is None:
            self.by_market = {}
        if self.by_confidence_bucket is None:
            self.by_confidence_bucket = {}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_dashboard(
    config: ProductionConfig | None = None,
    shadow: ShadowConfig | None = None,
) -> ShadowDashboard:
    """Build a full shadow-run dashboard from current state."""
    if config is None:
        config = load_config()
    if shadow is None:
        shadow = load_shadow_config()

    dash = ShadowDashboard(
        timestamp=datetime.now(timezone.utc).isoformat(),
        shadow_mode=shadow.shadow_mode,
    )

    # Query database
    db_path = Path(config.database_path)
    if db_path.exists():
        conn = get_connection(str(db_path))
        try:
            _fill_recommendations(conn, dash)
            _fill_delivery(conn, dash)
            _fill_data_quality(conn, dash)
            _fill_api_usage(conn, dash)
            _fill_health(conn, dash)
        finally:
            conn.close()

    # Readiness from live_readiness
    try:
        from src.live_readiness import run_readiness_checks
        report = run_readiness_checks(config, skip_network=True)
        dash.readiness_status = report.overall_status
    except Exception:
        dash.readiness_status = "unknown"

    # Promotion eligibility
    try:
        from src.promotion import check_promotion_criteria
        result = check_promotion_criteria(config)
        dash.promotion_eligible = result.all_passed
        dash.days_since_shadow_start = result.days_shadow_active
    except Exception:
        pass

    return dash


def _fill_recommendations(conn: sqlite3.Connection, dash: ShadowDashboard) -> None:
    """Fill recommendation stats."""
    # Total recommendations (today)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN ev_pct > 0 THEN 1 ELSE 0 END) as actionable,
                   AVG(ev_pct) as avg_ev
            FROM historical_recommendations
            WHERE observation_timestamp LIKE ?
        """, (f"{today}%",)).fetchone()
        if row:
            dash.total_recommendations = row["total"] or 0
            dash.actionable_count = row["actionable"] or 0
            dash.avg_confidence = round(row["avg_ev"] or 0.0, 2)
    except Exception:
        pass

    # By sportsbook
    try:
        rows = conn.execute("""
            SELECT sportsbook, COUNT(*) as cnt
            FROM historical_recommendations
            WHERE observation_timestamp LIKE ?
            GROUP BY sportsbook ORDER BY cnt DESC
        """, (f"{today}%",)).fetchall()
        dash.by_sportsbook = {r["sportsbook"]: r["cnt"] for r in rows}
    except Exception:
        pass

    # By market
    try:
        rows = conn.execute("""
            SELECT market_type, COUNT(*) as cnt
            FROM historical_recommendations
            WHERE observation_timestamp LIKE ?
            GROUP BY market_type ORDER BY cnt DESC
        """, (f"{today}%",)).fetchall()
        dash.by_market = {r["market_type"]: r["cnt"] for r in rows}
    except Exception:
        pass

    # By confidence bucket
    buckets = {"low": (0, 30), "medium": (30, 50), "high": (50, 70), "very_high": (70, 100)}
    dash.by_confidence_bucket = {}
    for name, (lo, hi) in buckets.items():
        try:
            row = conn.execute("""
                SELECT COUNT(*) AS cnt FROM historical_recommendations
                WHERE observation_timestamp LIKE ?
                  AND ev_pct >= ? AND ev_pct < ?
            """, (f"{today}%", lo, hi)).fetchone()
            dash.by_confidence_bucket[name] = row["cnt"] if row else 0
        except Exception:
            pass


def _fill_delivery(conn: sqlite3.Connection, dash: ShadowDashboard) -> None:
    """Fill delivery stats."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT status, COUNT(*) as cnt
            FROM delivery_log
            WHERE timestamp LIKE ?
            GROUP BY status
        """, (f"{today}%",)).fetchall()
        for r in rows:
            status = r["status"]
            cnt = r["cnt"]
            if status == "blocked":
                dash.delivery_blocked_count += cnt
            elif status in ("sent", "delivered"):
                dash.delivery_would_send += cnt
    except Exception:
        pass


def _fill_data_quality(conn: sqlite3.Connection, dash: ShadowDashboard) -> None:
    """Fill data-quality stats."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT severity, COUNT(*) as cnt
            FROM data_quality_findings
            WHERE timestamp LIKE ?
            GROUP BY severity
        """, (f"{today}%",)).fetchall()
        for r in rows:
            sev = r["severity"]
            cnt = r["cnt"]
            if sev == "CRITICAL":
                dash.critical_findings = cnt
            elif sev == "WARNING":
                dash.warning_findings = cnt
            elif sev == "INFO":
                dash.info_findings = cnt
    except Exception:
        pass


def _fill_api_usage(conn: sqlite3.Connection, dash: ShadowDashboard) -> None:
    """Fill API usage stats."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = conn.execute("""
            SELECT SUM(CASE WHEN cache_hit = 0 THEN 1 ELSE 0 END) as live,
                   SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) as cached
            FROM api_usage
            WHERE request_timestamp LIKE ?
        """, (f"{today}%",)).fetchone()
        if row:
            dash.api_live_requests = row["live"] or 0
            dash.api_cache_hits = row["cached"] or 0
    except Exception:
        pass


def _fill_health(conn: sqlite3.Connection, dash: ShadowDashboard) -> None:
    """Fill health status."""
    try:
        from src.health_check import run_health_checks
        config = load_config()
        report = run_health_checks(
            db_path=config.database_path,
            api_key=config.api_key,
            output_dir=config.output_dir,
            freshness_threshold=config.freshness_threshold_seconds,
        )
        dash.health_status = report.overall_status
    except Exception:
        dash.health_status = "unknown"


def format_dashboard(dash: ShadowDashboard) -> str:
    """Format dashboard for human-readable display."""
    lines = [
        "=== SHADOW-MODE DASHBOARD ===",
        f"  Timestamp: {dash.timestamp}",
        f"  Shadow mode: {'ON' if dash.shadow_mode else 'OFF'}",
        "",
        "--- Recommendations ---",
        f"  Total: {dash.total_recommendations}",
        f"  Actionable (EV>0): {dash.actionable_count}",
        f"  Avg confidence: {dash.avg_confidence:.1f}",
    ]

    if dash.by_sportsbook:
        lines.append("  By sportsbook:")
        for book, cnt in dash.by_sportsbook.items():
            lines.append(f"    {book}: {cnt}")

    if dash.by_market:
        lines.append("  By market:")
        for mkt, cnt in dash.by_market.items():
            lines.append(f"    {mkt}: {cnt}")

    if dash.by_confidence_bucket:
        lines.append("  By confidence:")
        for bucket, cnt in dash.by_confidence_bucket.items():
            lines.append(f"    {bucket}: {cnt}")

    lines.extend([
        "",
        "--- Delivery ---",
        f"  Blocked: {dash.delivery_blocked_count}",
        f"  Would send: {dash.delivery_would_send}",
        "",
        "--- Data Quality ---",
        f"  Critical: {dash.critical_findings}",
        f"  Warnings: {dash.warning_findings}",
        f"  Info: {dash.info_findings}",
        "",
        "--- API Usage ---",
        f"  Live requests: {dash.api_live_requests}",
        f"  Cache hits: {dash.api_cache_hits}",
        "",
        "--- Health ---",
        f"  Status: {dash.health_status}",
        "",
        "--- Readiness ---",
        f"  Status: {dash.readiness_status}",
        "",
        "--- Promotion ---",
        f"  Days since shadow start: {dash.days_since_shadow_start}",
        f"  Promotion eligible: {'YES' if dash.promotion_eligible else 'NO'}",
    ])

    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="shadow_dashboard", description="Shadow-run dashboard")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--output", help="Write dashboard to file")
    args = parser.parse_args(argv)

    from src.structured_logging import setup_logging
    setup_logging(level="WARNING")

    dash = build_dashboard()

    if args.json:
        output = json.dumps(dash.to_dict(), indent=2)
    else:
        output = format_dashboard(dash)

    if args.output:
        Path(args.output).write_text(output)
        print(f"Dashboard written to {args.output}")
    else:
        print(output)

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
