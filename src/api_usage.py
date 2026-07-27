"""API usage accounting and quota tracking.

Persists every API interaction with metrics: timestamp, endpoint,
job type, run ID, cache hit/live request, HTTP status, response time,
retry count, event count, market count, and estimated quota usage.

Provides summary queries for today, current month, by job, by endpoint,
cache-hit rate, retries, and failures.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ApiUsageRecord:
    """A single API usage record."""
    request_id: str = ""
    request_timestamp: str = ""
    endpoint: str = ""
    job_type: str = ""
    run_id: str = ""
    cache_hit: bool = False
    http_status: int = 0
    response_time_ms: float = 0.0
    retry_count: int = 0
    event_count: int = 0
    market_count: int = 0
    response_object_count: int = 0
    estimated_quota_usage: float = 0.0


@dataclass
class ApiUsageSummary:
    """Aggregated API usage summary."""
    period: str = ""
    total_requests: int = 0
    live_requests: int = 0
    cache_hits: int = 0
    cache_hit_rate: float = 0.0
    total_retries: int = 0
    total_failures: int = 0
    failure_rate: float = 0.0
    total_events: int = 0
    total_markets: int = 0
    total_quota_used: float = 0.0
    avg_response_time_ms: float = 0.0
    by_endpoint: dict[str, int] = field(default_factory=dict)
    by_job: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Schema ─────────────────────────────────────────────────────────

USAGE_TABLE = "api_usage"
QUOTA_WARN_DEFAULT = 80.0  # percent of daily quota


def init_usage_table(conn: Any) -> None:
    """Create the api_usage table if it doesn't exist."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {USAGE_TABLE} (
            request_id TEXT PRIMARY KEY,
            request_timestamp TEXT NOT NULL,
            endpoint TEXT NOT NULL DEFAULT '',
            job_type TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            cache_hit INTEGER NOT NULL DEFAULT 0,
            http_status INTEGER NOT NULL DEFAULT 0,
            response_time_ms REAL NOT NULL DEFAULT 0.0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            event_count INTEGER NOT NULL DEFAULT 0,
            market_count INTEGER NOT NULL DEFAULT 0,
            response_object_count INTEGER NOT NULL DEFAULT 0,
            estimated_quota_usage REAL NOT NULL DEFAULT 0.0
        )
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{USAGE_TABLE}_timestamp
        ON {USAGE_TABLE} (request_timestamp)
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{USAGE_TABLE}_job
        ON {USAGE_TABLE} (job_type)
    """)


def record_api_usage(conn: Any, record: ApiUsageRecord) -> None:
    """Persist an API usage record."""
    if not record.request_id:
        record.request_id = str(uuid.uuid4())
    if not record.request_timestamp:
        record.request_timestamp = datetime.now(timezone.utc).isoformat()

    conn.execute(f"""
        INSERT OR REPLACE INTO {USAGE_TABLE}
        (request_id, request_timestamp, endpoint, job_type, run_id,
         cache_hit, http_status, response_time_ms, retry_count,
         event_count, market_count, response_object_count, estimated_quota_usage)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        record.request_id, record.request_timestamp, record.endpoint,
        record.job_type, record.run_id, int(record.cache_hit),
        record.http_status, record.response_time_ms, record.retry_count,
        record.event_count, record.market_count, record.response_object_count,
        record.estimated_quota_usage,
    ))
    conn.commit()
    logger.debug("Recorded API usage: %s %s cache=%s status=%d",
                 record.endpoint, record.job_type, record.cache_hit, record.http_status)


def get_usage_summary(
    conn: Any,
    *,
    period: str = "today",
    job_filter: str = "",
) -> ApiUsageSummary:
    """Get API usage summary for a period.

    Parameters
    ----------
    period:
        'today', 'month', or ISO date prefix like '2026-07'
    job_filter:
        Optional job type filter.
    """
    if period == "today":
        date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    elif period == "month":
        date_prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    else:
        date_prefix = period

    where = f"WHERE request_timestamp LIKE '{date_prefix}%'"
    if job_filter:
        where += f" AND job_type = '{job_filter}'"

    # Totals
    row = conn.execute(f"""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN cache_hit = 0 THEN 1 ELSE 0 END) as live,
            SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) as cached,
            SUM(retry_count) as retries,
            SUM(CASE WHEN http_status >= 400 THEN 1 ELSE 0 END) as failures,
            SUM(event_count) as events,
            SUM(market_count) as markets,
            SUM(estimated_quota_usage) as quota,
            AVG(response_time_ms) as avg_rt
        FROM {USAGE_TABLE}
        {where}
    """).fetchone()

    total = row[0] or 0
    live = row[1] or 0
    cached = row[2] or 0

    # By endpoint
    ep_rows = conn.execute(f"""
        SELECT endpoint, COUNT(*) as cnt
        FROM {USAGE_TABLE} {where}
        GROUP BY endpoint ORDER BY cnt DESC
    """).fetchall()

    # By job
    job_rows = conn.execute(f"""
        SELECT job_type, COUNT(*) as cnt
        FROM {USAGE_TABLE} {where}
        GROUP BY job_type ORDER BY cnt DESC
    """).fetchall()

    return ApiUsageSummary(
        period=date_prefix,
        total_requests=total,
        live_requests=live,
        cache_hits=cached,
        cache_hit_rate=(cached / total * 100) if total > 0 else 0.0,
        total_retries=row[3] or 0,
        total_failures=row[4] or 0,
        failure_rate=(row[4] / total * 100) if total > 0 and row[4] else 0.0,
        total_events=row[5] or 0,
        total_markets=row[6] or 0,
        total_quota_used=row[7] or 0.0,
        avg_response_time_ms=round(row[8] or 0.0, 2),
        by_endpoint={r[0]: r[1] for r in ep_rows},
        by_job={r[0]: r[1] for r in job_rows},
    )


def check_quota_warning(
    conn: Any,
    *,
    daily_limit: float = 1000.0,
    warn_pct: float = QUOTA_WARN_DEFAULT,
) -> dict[str, Any] | None:
    """Check if current day's quota usage exceeds warning threshold."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(f"""
        SELECT SUM(estimated_quota_usage) as used
        FROM {USAGE_TABLE}
        WHERE request_timestamp LIKE '{today}%'
    """).fetchone()
    used = row[0] or 0.0
    pct = (used / daily_limit * 100) if daily_limit > 0 else 0.0

    if pct >= warn_pct:
        return {
            "used": used,
            "limit": daily_limit,
            "pct": round(pct, 1),
            "warning": f"API quota at {pct:.1f}% ({used:.0f}/{daily_limit:.0f})",
        }
    return None


def format_usage_report(summary: ApiUsageSummary) -> str:
    """Format a human-readable usage report."""
    lines = [
        f"API Usage Report — {summary.period}",
        f"  Total requests: {summary.total_requests}",
        f"  Live requests:  {summary.live_requests}",
        f"  Cache hits:     {summary.cache_hits} ({summary.cache_hit_rate:.1f}%)",
        f"  Retries:        {summary.total_retries}",
        f"  Failures:       {summary.total_failures} ({summary.failure_rate:.1f}%)",
        f"  Events:         {summary.total_events}",
        f"  Markets:        {summary.total_markets}",
        f"  Quota used:     {summary.total_quota_used:.0f}",
        f"  Avg resp time:  {summary.avg_response_time_ms:.0f}ms",
    ]
    if summary.by_endpoint:
        lines.append("  By endpoint:")
        for ep, cnt in summary.by_endpoint.items():
            lines.append(f"    {ep}: {cnt}")
    if summary.by_job:
        lines.append("  By job:")
        for job, cnt in summary.by_job.items():
            lines.append(f"    {job}: {cnt}")
    return "\n".join(lines)
