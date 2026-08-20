"""WNBA API credit tracking and budget control (The Odds API).

The Odds API's free tier is a hard 500-credits/month quota. Every real
(non-cached) response carries the ground truth —
``x-requests-used``/``x-requests-remaining``/``x-requests-last`` headers,
already captured by ``OddsAPIClient.last_quota`` (see
``src/odds_api_client.py``) but never persisted before this module. This
gives ``src/league_schedule.py`` a real budget to check against before
spending credits on the more expensive player-props calls, instead of
guessing usage or hoping the free tier holds up.
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DEFAULT_MONTHLY_BUDGET = 500  # The Odds API free tier

# Verified per-call costs — see src/odds_api_client.py and
# docs/MARKET_CAPABILITY.md for how these were confirmed live.
GAME_ODDS_COST = 3         # h2h+spreads+totals, regions=us, ONE call covers ALL games
EVENTS_COST = 0            # /events is free
PROPS_COST_PER_EVENT = 8   # 8 registered player-prop markets, regions=us, per event


def record_credit_usage(
    conn, *, endpoint: str, job_type: str = "",
    requests_used: int | None = None, requests_remaining: int | None = None,
    requests_last: int | None = None, cache_hit: bool = False,
) -> None:
    """Persist one API call's credit-header snapshot (ground truth from
    the provider, not estimated)."""
    conn.execute(
        """INSERT INTO odds_api_credits
           (recorded_at, endpoint, job_type, requests_used,
            requests_remaining, requests_last, cache_hit)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), endpoint, job_type,
         requests_used, requests_remaining, requests_last, int(cache_hit)),
    )
    conn.commit()


def record_client_quota(conn, client, *, endpoint: str, job_type: str = "",
                         cache_hit: bool = False) -> None:
    """Convenience: pull the last-seen quota headers off an OddsAPIClient
    instance (``client.last_quota``) and persist them."""
    quota = getattr(client, "last_quota", None) or {}

    def _int(key):
        val = quota.get(key)
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    record_credit_usage(
        conn, endpoint=endpoint, job_type=job_type,
        requests_used=_int("x-requests-used"),
        requests_remaining=_int("x-requests-remaining"),
        requests_last=_int("x-requests-last"),
        cache_hit=cache_hit,
    )


def get_latest_credit_status(conn) -> dict | None:
    """Most recent REAL (non-cache-hit) usage snapshot — ground truth for
    "how many credits are left right now", straight from the provider."""
    # Ordered by id, not just recorded_at — two calls close enough together
    # can share the same timestamp string, and id (autoincrement) is the
    # only field guaranteed to reflect true insertion order in that case.
    #
    # requests_remaining IS NOT NULL is required too: a call whose response
    # genuinely carried no quota headers (or a test/mocked client) still
    # gets a row via record_client_quota with everything NULL — without
    # this filter, that headerless row would become "the latest reading"
    # and mask a real prior one, silently defeating the budget check this
    # function exists to back.
    row = conn.execute(
        """SELECT recorded_at, requests_used, requests_remaining, requests_last
           FROM odds_api_credits WHERE cache_hit = 0 AND requests_remaining IS NOT NULL
           ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def get_usage_this_month(conn, *, as_of: datetime | None = None) -> dict:
    """Sum of ``requests_last`` (actual credits spent per call) for calls
    made so far this calendar month, plus a naive same-rate projection
    for the rest of the month.

    This is a fallback/cross-check for when no real call has happened yet
    this month — ``get_latest_credit_status``'s ``requests_remaining`` (the
    provider's own count) is the more accurate source whenever available;
    see ``credit_budget_check``, which prefers it.
    """
    as_of = as_of or datetime.now(timezone.utc)
    month_prefix = as_of.strftime("%Y-%m")
    rows = conn.execute(
        """SELECT requests_last FROM odds_api_credits
           WHERE cache_hit = 0 AND recorded_at LIKE ?
             AND requests_last IS NOT NULL""",
        (f"{month_prefix}%",),
    ).fetchall()
    used = sum(r["requests_last"] for r in rows)
    days_elapsed = as_of.day
    days_in_month = _days_in_month(as_of.year, as_of.month)
    daily_rate = used / days_elapsed if days_elapsed else 0.0
    projected = round(daily_rate * days_in_month, 1)
    return {
        "month": month_prefix,
        "credits_used_so_far": used,
        "calls_recorded": len(rows),
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "projected_month_total": projected,
    }


def estimate_monthly_cost(*, game_scans_per_day: float, prop_events_per_day: float) -> dict:
    """Pure analytical projection — no DB, no live calls. Answers "if we
    ran this cadence every day for a month, how many credits would it
    cost", using the verified per-call costs above. This is what
    ``src/league_schedule.py``'s cadence choices are sized against, and
    what the operator-facing credit report is built from.
    """
    daily_game_cost = game_scans_per_day * GAME_ODDS_COST
    daily_props_cost = prop_events_per_day * PROPS_COST_PER_EVENT
    daily_total = daily_game_cost + daily_props_cost
    return {
        "game_scans_per_day": game_scans_per_day,
        "prop_events_per_day": prop_events_per_day,
        "daily_game_credits": daily_game_cost,
        "daily_props_credits": daily_props_cost,
        "daily_total_credits": daily_total,
        "monthly_total_credits": round(daily_total * 30, 1),
        "fits_free_tier": (daily_total * 30) <= DEFAULT_MONTHLY_BUDGET,
    }


def credit_budget_check(
    conn, planned_cost: int, *,
    monthly_budget: int = DEFAULT_MONTHLY_BUDGET, reserve_pct: float = 0.10,
) -> tuple[bool, str]:
    """Decide whether spending *planned_cost* credits right now is safe.

    Uses the provider's own ``requests_remaining`` when we have a recent
    real reading (most accurate); falls back to the month-to-date
    estimate when we don't have one yet. A configurable reserve (default
    10%) is kept unspent as a safety margin against estimation error —
    this function is what stands between a scheduling bug and silently
    burning the whole month's quota in one bad loop.
    """
    reserve = monthly_budget * reserve_pct
    status = get_latest_credit_status(conn)
    if status and status.get("requests_remaining") is not None:
        remaining = status["requests_remaining"]
        if planned_cost > remaining - reserve:
            return False, (
                f"only {remaining} credits remaining (provider-reported), "
                f"{reserve:.0f} held in reserve; {planned_cost} requested"
            )
        return True, f"{remaining} credits remaining (provider-reported)"

    usage = get_usage_this_month(conn)
    remaining_estimate = monthly_budget - usage["credits_used_so_far"]
    if planned_cost > remaining_estimate - reserve:
        return False, (
            f"no recent provider reading; estimated {remaining_estimate} "
            f"credits remaining this month, {reserve:.0f} held in reserve"
        )
    return True, (
        f"estimated {remaining_estimate} credits remaining this month "
        f"(no provider reading yet)"
    )
