"""Per-league production scheduling policy.

Pure decision functions — given "now" and what's actually been discovered
about today's/upcoming games (never assumed), decide whether a given job
should run right now. No I/O here; ``src/worker.py`` calls these and does
the actual API/DB work. Keeping this pure makes the scheduling POLICY
itself directly testable without mocking a live API or a persistent
worker loop.

Each league gets its own cadence because they're structurally different:

- MLB: games most days, at broadly similar times — the existing daily
  morning scan + wide pregame window in ``src/worker.py`` is left as-is
  (already tuned, already in production); nothing here touches it.
- NFL: games only some days (Thu/Sun/Mon, occasional Sat/international),
  at four or five very different kickoff windows. Nothing here assumes
  "Sunday only" — every decision is driven by discovered game start
  times, not a day-of-week table.
- WNBA: games most days, but the odds provider (The Odds API) has a hard
  monthly credit budget (see ``src/odds_api_credits.py``). Schedule
  discovery is free and can run often; game-market odds cost a flat fee
  per call regardless of game count; player props cost per event and are
  rationed hardest — see ``wnba_should_fetch_props``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ScheduleDecision:
    """Whether to run a job right now, and why — the "why" is what makes
    this debuggable from logs instead of a silent yes/no."""
    should_run: bool
    reason: str


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def extract_game_start_times(events: list[dict]) -> list[datetime]:
    """Pull real start times out of discovered events, whatever shape
    they're in (SportsGameOdds ``status.startsAt``, or the WNBA-normalized
    ``status.startsAt`` produced by ``src/sports/wnba.py`` — both already
    unified by the time events reach here). Unparseable or missing times
    are skipped, never guessed or defaulted to "now"."""
    times = []
    for ev in events:
        status = ev.get("status", {}) or {}
        raw = status.get("startsAt") or ev.get("commence_time") or ""
        dt = _parse_iso(raw)
        if dt:
            times.append(dt)
    return sorted(times)


def _same_local_date(dt: datetime, now: datetime) -> bool:
    tz = now.tzinfo
    dt_local = dt.astimezone(tz) if tz else dt
    return dt_local.date() == now.date()


# ── NFL ───────────────────────────────────────────────────────────

def nfl_has_games_today(now: datetime, game_times: list[datetime]) -> bool:
    """True if any discovered NFL game starts on *now*'s calendar date,
    in *now*'s own timezone — not UTC, since a Thursday-night or
    Monday-night kickoff can fall on a different UTC date than the local
    game day."""
    return any(_same_local_date(gt, now) for gt in game_times)


def nfl_should_run_daily_scan(
    now: datetime, game_times: list[datetime], already_ran_today: bool,
) -> ScheduleDecision:
    """One scan per NFL game day, in the morning — mirrors MLB's
    morning-run, but skipped entirely on the many days in a normal NFL
    week with no games at all (never runs "just in case")."""
    if already_ran_today:
        return ScheduleDecision(False, "already ran today")
    if not nfl_has_games_today(now, game_times):
        return ScheduleDecision(False, "no NFL games today")
    if now.hour < 8:
        return ScheduleDecision(False, "before the 8am scan window")
    return ScheduleDecision(True, "NFL game day, scan window reached")


def nfl_pregame_window(game_times: list[datetime]) -> tuple[datetime, datetime] | None:
    """(start, end) of the pregame-tracking window around today's
    earliest-to-latest NFL kickoffs: 4 hours before the first game
    through kickoff of the last. Naturally covers Thursday night, Sunday
    early/late/night, and Monday night without hardcoding any of those
    labels — it's derived entirely from the discovered kickoff times."""
    if not game_times:
        return None
    return (game_times[0] - timedelta(hours=4), game_times[-1])


def nfl_should_run_pregame_check(
    now: datetime, game_times: list[datetime],
) -> ScheduleDecision:
    window = nfl_pregame_window(game_times)
    if window is None:
        return ScheduleDecision(False, "no NFL games today")
    start, end = window
    if now < start:
        return ScheduleDecision(False, f"pregame window opens at {start.isoformat()}")
    if now > end:
        return ScheduleDecision(False, "all of today's kickoffs have passed")
    return ScheduleDecision(True, "inside NFL pregame tracking window")


# ── WNBA ──────────────────────────────────────────────────────────

def wnba_has_games_today(now: datetime, game_times: list[datetime]) -> bool:
    return any(_same_local_date(gt, now) for gt in game_times)


def wnba_should_check_schedule(
    now: datetime, last_check: datetime | None, min_interval_minutes: int = 60,
) -> ScheduleDecision:
    """Event discovery (``GET /events``) is free — 0 credits, confirmed
    live (see ``src/odds_api_client.py``) — so this is throttled only to
    avoid pointless request noise, not to save quota."""
    if last_check is None:
        return ScheduleDecision(True, "no prior schedule check")
    elapsed = (now - last_check).total_seconds() / 60
    if elapsed >= min_interval_minutes:
        return ScheduleDecision(True, f"{elapsed:.0f} min since last check")
    return ScheduleDecision(False, f"checked {elapsed:.0f} min ago (free endpoint, throttled for noise, not cost)")


def wnba_should_fetch_game_odds(
    now: datetime, game_times: list[datetime], last_fetch: datetime | None,
) -> ScheduleDecision:
    """Game-market odds cost a flat 3 credits per call REGARDLESS of how
    many games are on the slate — cheap enough to check several times a
    day, ramping up near tip-off, but pointless on a day with no games."""
    if not wnba_has_games_today(now, game_times):
        return ScheduleDecision(False, "no WNBA games today")
    if last_fetch is None:
        return ScheduleDecision(True, "no odds fetched yet today")
    upcoming = [gt for gt in game_times if gt > now]
    minutes_to_next = min((gt - now).total_seconds() / 60 for gt in upcoming) if upcoming else None
    interval = 30 if (minutes_to_next is not None and minutes_to_next <= 120) else 180
    elapsed = (now - last_fetch).total_seconds() / 60
    if elapsed >= interval:
        return ScheduleDecision(True, f"{elapsed:.0f} min since last odds fetch (interval={interval}min)")
    return ScheduleDecision(False, f"fetched {elapsed:.0f} min ago, next due at {interval}min")


def wnba_should_fetch_props(
    now: datetime, game_times: list[datetime], last_fetch: datetime | None,
    credits_remaining: int | None, reserve: int = 50,
) -> ScheduleDecision:
    """Player props are the expensive call (8 credits/event) — only
    worth spending close to tip-off, at most once an hour, and never if
    it would eat into the reserve. Stops once every game today has
    started (post-tip-off props aren't useful for pregame recommendations
    and there's no reason to keep spending on them)."""
    if not wnba_has_games_today(now, game_times):
        return ScheduleDecision(False, "no WNBA games today")
    upcoming = [gt for gt in game_times if gt > now]
    if not upcoming:
        return ScheduleDecision(False, "all of today's games have started")
    minutes_to_next = (min(upcoming) - now).total_seconds() / 60
    if minutes_to_next > 180:
        return ScheduleDecision(
            False, f"{minutes_to_next:.0f} min to next game — too early, wait until inside 3h",
        )
    if credits_remaining is not None and credits_remaining <= reserve:
        return ScheduleDecision(False, f"only {credits_remaining} credits remaining, reserve is {reserve}")
    if last_fetch is not None:
        elapsed = (now - last_fetch).total_seconds() / 60
        if elapsed < 60:
            return ScheduleDecision(False, f"props fetched {elapsed:.0f} min ago, at most once/hour")
    return ScheduleDecision(True, f"{minutes_to_next:.0f} min to next game, inside props window")
