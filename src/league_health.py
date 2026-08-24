"""Per-league production health reporting.

Complements src/health_check.py (infrastructure: database, disk, worker
heartbeat) with data-PIPELINE health, broken out by league — the thing
that actually tells an operator "MLB is fine but WNBA silently stopped
ingesting odds three days ago" instead of a single global status that
can't say which league is the problem.

Reuses the HealthCheck/HealthReport dataclasses from src/health_check.py
for a consistent PASS/WARN/FAIL vocabulary across both.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from src.health_check import HealthCheck, HealthReport

logger = logging.getLogger(__name__)

# How stale is too stale before a WARN/FAIL, per check. Chosen to be
# generous enough that a normal off-day (no games) doesn't false-alarm,
# while still catching a genuinely broken pipeline within a day or two.
_STALE_WARN_HOURS = 30
_STALE_FAIL_HOURS = 72

_LEAGUE_JOB_TYPES = {
    "MLB": {"scan": "morning-run", "pregame": "pregame-check"},
    "NFL": {"scan": "morning-run-nfl", "pregame": "pregame-check-nfl"},
    "WNBA": {"scan": "wnba-odds-scan", "pregame": "wnba-props-scan"},
}


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _age_status(dt: datetime | None, now: datetime, *, allow_none_ok: bool = False) -> tuple[str, str]:
    """Common "how stale is this timestamp" -> (status, message) helper."""
    if dt is None:
        return ("ok" if allow_none_ok else "warning"), "none recorded yet"
    age_hours = (now - dt).total_seconds() / 3600
    if age_hours >= _STALE_FAIL_HOURS:
        return "error", f"{age_hours:.0f}h ago (>{_STALE_FAIL_HOURS}h)"
    if age_hours >= _STALE_WARN_HOURS:
        return "warning", f"{age_hours:.0f}h ago (>{_STALE_WARN_HOURS}h)"
    return "ok", f"{age_hours:.1f}h ago"


def _check_last_recommendation(conn, league: str, now: datetime) -> HealthCheck:
    row = conn.execute(
        "SELECT MAX(scan_timestamp) AS ts, COUNT(*) AS n FROM historical_recommendations "
        "WHERE league = ? AND date(scan_timestamp) = date('now')",
        (league,),
    ).fetchone()
    latest = conn.execute(
        "SELECT MAX(scan_timestamp) AS ts FROM historical_recommendations WHERE league = ?",
        (league,),
    ).fetchone()
    status, msg = _age_status(_parse_ts(latest["ts"] if latest else None), now)
    today_count = row["n"] if row else 0
    return HealthCheck(
        name=f"{league}: last recommendation", status=status,
        message=f"{msg}; {today_count} today",
        details={"today_count": today_count, "last_at": latest["ts"] if latest else None},
    )


def _check_last_settlement(conn, league: str, now: datetime) -> HealthCheck:
    row = conn.execute(
        """SELECT MAX(ms.settled_at) AS ts, COUNT(*) AS n
           FROM market_settlements ms
           JOIN historical_recommendations hr ON hr.recommendation_id = ms.recommendation_id
           WHERE hr.league = ? AND ms.settled_at IS NOT NULL""",
        (league,),
    ).fetchone()
    pending_rows = conn.execute(
        """SELECT hr.market_type AS market_type FROM historical_recommendations hr
           LEFT JOIN market_settlements ms ON ms.recommendation_id = hr.recommendation_id
           WHERE hr.league = ? AND (ms.settlement_status IS NULL OR ms.settlement_status = 'UNRESOLVED')
             AND hr.event_start_time < ?""",
        (league, (now - timedelta(hours=6)).isoformat()),
    ).fetchall()
    # "None ever settled" is OK for a league with no games yet today, not
    # necessarily broken — only escalate once there's a real backlog of
    # recs whose games clearly already finished (6h past start) still
    # sitting unsettled. Filtered to markets that actually have a verified
    # settlement contract (is_auto_settleable_market) — a market with no
    # contract yet (e.g. first_home_run) will sit UNRESOLVED forever by
    # design, not because anything is stuck, and counting it here made
    # this look like a growing operational backlog when it wasn't one.
    from src.prop_config import is_auto_settleable_market
    unsettled_backlog = sum(
        1 for r in pending_rows if is_auto_settleable_market(r["market_type"])
    )
    status, msg = _age_status(_parse_ts(row["ts"] if row else None), now, allow_none_ok=True)
    if unsettled_backlog > 0 and status == "ok":
        status = "warning"
    if unsettled_backlog > 0:
        msg = f"{msg}; {unsettled_backlog} unresolved recs from games that started >6h ago"
    return HealthCheck(
        name=f"{league}: last settlement", status=status, message=msg,
        details={"unsettled_backlog": unsettled_backlog},
    )


def _check_unresolved_identities(conn, league: str) -> HealthCheck:
    """WNBA-specific (player_identity_mappings only exists for the
    identity-resolved provider) — for other leagues this is N/A, not a
    failure, since they use provider-stable IDs with no resolution step.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM player_identity_mappings "
            "WHERE league = ? AND mapping_confidence IN ('LOW', 'UNRESOLVED')",
            (league,),
        ).fetchone()
        low_count = row["n"] if row else 0
    except Exception:
        return HealthCheck(name=f"{league}: unresolved identities", status="ok",
                            message="not applicable (no identity-mapping table)")
    status = "warning" if low_count > 0 else "ok"
    return HealthCheck(
        name=f"{league}: unresolved identities", status=status,
        message=f"{low_count} LOW/UNRESOLVED player-identity mappings cached (excluded from recs, not a bug)",
        details={"low_or_unresolved": low_count},
    )


def _check_stale_markets(conn, league: str) -> HealthCheck:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM historical_recommendations "
        "WHERE league = ? AND date(scan_timestamp) = date('now') AND freshness_status = 'STALE'",
        (league,),
    ).fetchone()
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM historical_recommendations "
        "WHERE league = ? AND date(scan_timestamp) = date('now')",
        (league,),
    ).fetchone()
    stale_n = row["n"] if row else 0
    total_n = total["n"] if total else 0
    pct = (stale_n / total_n * 100) if total_n else 0.0
    status = "error" if pct >= 50 else "warning" if pct > 0 else "ok"
    return HealthCheck(
        name=f"{league}: stale markets", status=status,
        message=f"{stale_n}/{total_n} today's rows marked STALE ({pct:.0f}%)",
        details={"stale_count": stale_n, "total_count": total_n},
    )


def _check_event_date_sanity(conn, league: str, now: datetime) -> HealthCheck:
    """Flag recommendations whose event_start_time is implausibly far
    from when they were generated.

    Added 2026-08-20 after a real production-adjacent bug: an odds-API
    call missing its date-range filter could return events from a
    completely different year (verified live — a stale demo/historical
    event set was returned instead of current games). Nothing else in
    this health report would have caught that specific failure mode —
    "did a scan run recently" and "is this quote's price stale" are both
    satisfied even when the scan is confidently analyzing the wrong
    year's games. This checks the actual event dates being recommended
    on, not just that a scan happened.
    """
    # Bounds computed in Python and compared as plain ISO-8601 text rather
    # than SQL date arithmetic: julianday() is SQLite-only and has no
    # PostgreSQL equivalent, which broke this check in production (real
    # error: "function julianday(text) does not exist"). Both dialects
    # store these columns as ISO-8601 text, which sorts/compares correctly
    # lexicographically, so plain </> works identically on both.
    lower_bound = (now - timedelta(days=3)).isoformat()
    upper_bound = (now + timedelta(days=14)).isoformat()
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM historical_recommendations
           WHERE league = ? AND date(scan_timestamp) = date('now')
             AND event_start_time IS NOT NULL
             AND (event_start_time < ? OR event_start_time > ?)""",
        (league, lower_bound, upper_bound),
    ).fetchone()
    implausible_n = row["n"] if row else 0
    status = "error" if implausible_n > 0 else "ok"
    return HealthCheck(
        name=f"{league}: event date sanity", status=status,
        message=(
            f"{implausible_n} today's recommendation(s) reference an event "
            f"more than 3 days in the past or 14 days in the future"
            if implausible_n else "all event dates look plausible"
        ),
        details={"implausible_count": implausible_n},
    )


def _check_qualified_opportunities(conn, league: str) -> HealthCheck:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM historical_recommendations "
        "WHERE league = ? AND date(scan_timestamp) = date('now') AND qualification_passed = 1",
        (league,),
    ).fetchone()
    n = row["n"] if row else 0
    return HealthCheck(
        name=f"{league}: qualified opportunities today", status="ok",
        message=f"{n} qualified", details={"count": n},
    )


def _check_job_activity(conn, league: str, now: datetime) -> HealthCheck:
    job_types = _LEAGUE_JOB_TYPES.get(league, {})
    scan_type = job_types.get("scan")
    if not scan_type:
        return HealthCheck(name=f"{league}: job activity", status="ok", message="not applicable")

    row = conn.execute(
        "SELECT status, started_at, completed_at, error_message FROM scheduled_jobs "
        "WHERE job_type = ? ORDER BY created_at DESC LIMIT 1",
        (scan_type,),
    ).fetchone()
    if not row:
        return HealthCheck(
            name=f"{league}: job activity", status="warning",
            message=f"no {scan_type} job has ever run",
        )
    duration = None
    if row["started_at"] and row["completed_at"]:
        s, c = _parse_ts(row["started_at"]), _parse_ts(row["completed_at"])
        if s and c:
            duration = (c - s).total_seconds()
    status = "error" if row["status"] == "failed" else "ok"
    msg = f"last {scan_type}: {row['status']}"
    if duration is not None:
        msg += f", {duration:.0f}s"
    if row["status"] == "failed" and row["error_message"]:
        msg += f" — {row['error_message'][:120]}"
    return HealthCheck(
        name=f"{league}: job activity", status=status, message=msg,
        details={"last_status": row["status"], "duration_seconds": duration},
    )


def _check_wnba_credits(conn) -> HealthCheck:
    from src.odds_api_credits import get_latest_credit_status, get_usage_this_month, DEFAULT_MONTHLY_BUDGET
    status_row = get_latest_credit_status(conn)
    usage = get_usage_this_month(conn)
    if status_row and status_row.get("requests_remaining") is not None:
        remaining = status_row["requests_remaining"]
        pct_remaining = remaining / DEFAULT_MONTHLY_BUDGET * 100
        level = "error" if pct_remaining < 10 else "warning" if pct_remaining < 25 else "ok"
        return HealthCheck(
            name="WNBA: API credit budget", status=level,
            message=f"{remaining}/{DEFAULT_MONTHLY_BUDGET} credits remaining (provider-reported)",
            details={"requests_remaining": remaining, **usage},
        )
    level = "warning" if usage["credits_used_so_far"] > DEFAULT_MONTHLY_BUDGET * 0.75 else "ok"
    return HealthCheck(
        name="WNBA: API credit budget", status=level,
        message=f"~{usage['credits_used_so_far']} credits used this month "
                f"(no provider reading yet; projected {usage['projected_month_total']})",
        details=usage,
    )


# Fetch-status values that represent a genuine problem with the Pinnacle
# props feed itself, as opposed to Pinnacle legitimately having nothing
# posted right now (PINNACLE_STATUS_OK / PINNACLE_STATUS_NO_PROPS_POSTED
# are both healthy — see src/pinnacle_feed.py's PINNACLE_STATUS_* constants).
_PINNACLE_PROPS_FAILURE_STATUSES = frozenset({
    "no_api_key", "auth_failure", "http_error", "network_error",
    "parse_error", "league_not_configured",
})


def _check_pinnacle_props_health(conn, league: str) -> HealthCheck:
    """Warn if the Pinnacle player-props fetch itself has been failing
    (auth/network/parse/rate-limit) across recent scans — added
    2026-08-23 per operator directive. Deliberately does NOT warn on
    "no_props_currently_posted" — that's Pinnacle legitimately having
    nothing posted yet (real and expected, especially for NFL this far
    pre-season, or any league between slates), not a health problem.
    """
    import json
    rows = conn.execute(
        "SELECT metadata_json FROM scan_runs "
        "WHERE run_type = 'scan' AND metadata_json IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 30"
    ).fetchall()

    statuses: list[str] = []
    for row in rows:
        try:
            meta = json.loads(row["metadata_json"])
        except (TypeError, ValueError):
            continue
        funnel = meta.get("pinnacle_funnel") or {}
        if funnel.get("league") != league:
            continue
        status = funnel.get("pinnacle_props_status")
        if status is not None:
            statuses.append(status)
        if len(statuses) >= 5:
            break

    if not statuses:
        return HealthCheck(
            name=f"{league}: Pinnacle props fetch", status="ok",
            message="no recent scan data yet (feature added 2026-08-23, or league not yet scanned)",
        )

    n_failures = sum(1 for s in statuses if s in _PINNACLE_PROPS_FAILURE_STATUSES)
    latest = statuses[0]
    if n_failures == len(statuses) and n_failures >= 3:
        return HealthCheck(
            name=f"{league}: Pinnacle props fetch", status="error",
            message=f"last {len(statuses)} scans all failed to fetch Pinnacle props "
                    f"(latest reason: {latest}) — feed may be down, key may be invalid, "
                    f"or rate limits may be persistently exhausted",
            details={"recent_statuses": statuses},
        )
    if latest in _PINNACLE_PROPS_FAILURE_STATUSES:
        return HealthCheck(
            name=f"{league}: Pinnacle props fetch", status="warning",
            message=f"most recent scan failed to fetch Pinnacle props (reason: {latest})",
            details={"recent_statuses": statuses},
        )
    return HealthCheck(
        name=f"{league}: Pinnacle props fetch", status="ok",
        message=f"latest status: {latest}",
        details={"recent_statuses": statuses},
    )


def run_league_health_checks(conn, league: str) -> HealthReport:
    """Full data-pipeline health report for one league.

    Covers: last recommendation, last settlement, unresolved identities,
    stale markets, event-date sanity, qualified-opportunity count, job
    activity/duration, Pinnacle player-props fetch health, and (WNBA
    only) API credit budget state.
    """
    now = datetime.now(timezone.utc)
    report = HealthReport(overall_status="healthy", timestamp=now.isoformat())

    report.add(_check_last_recommendation(conn, league, now))
    report.add(_check_last_settlement(conn, league, now))
    report.add(_check_stale_markets(conn, league))
    report.add(_check_event_date_sanity(conn, league, now))
    report.add(_check_qualified_opportunities(conn, league))
    report.add(_check_job_activity(conn, league, now))
    try:
        report.add(_check_pinnacle_props_health(conn, league))
    except Exception:
        logger.exception("[%s] Pinnacle props health check failed", league)
        report.add(HealthCheck(name=f"{league}: Pinnacle props fetch", status="warning",
                                message="could not evaluate Pinnacle props health"))
    if league == "WNBA":
        report.add(_check_unresolved_identities(conn, league))
        try:
            report.add(_check_wnba_credits(conn))
        except Exception:
            logger.exception("[WNBA] Credit health check failed")
            report.add(HealthCheck(name="WNBA: API credit budget", status="warning",
                                    message="could not read credit status"))

    return report


def run_all_leagues_health_checks(conn, leagues: list[str] | None = None) -> dict[str, HealthReport]:
    """Convenience: run run_league_health_checks for every league at once."""
    leagues = leagues or ["MLB", "NFL", "WNBA"]
    return {league: run_league_health_checks(conn, league) for league in leagues}
