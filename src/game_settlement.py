"""Shared, sport-agnostic game-level market settlement.

Grades moneyline, spread, and total recommendations from a verified
``event_results`` row (final scores). The settlement math itself doesn't
depend on which sport produced the score — only on the recommendation's
own stored ``side``/``line``/``raw_line`` values, which are never
reconstructed or guessed (see ``historical_recommendations.raw_line`` —
the signed spread value captured at recommendation time specifically so
settlement never has to infer favorite/underdog direction).

Used by every league (MLB run-line, NFL spread, WNBA spread — same
grading function, different sport).
"""

from __future__ import annotations

from src.grading import (
    SETTLEMENT_LOSS,
    SETTLEMENT_NEEDS_REVIEW,
    SETTLEMENT_PUSH,
    SETTLEMENT_UNRESOLVED,
    SETTLEMENT_VOID,
    SETTLEMENT_WIN,
    grade_ou,
)

GAME_MARKET_TYPES = frozenset({
    "game_moneyline", "game_spread_ou", "game_total_ou",
    # MLB's own naming for the same spread market NFL/WNBA call
    # "game_spread_ou" (see src/prop_config.py::GAME_RUN_LINE). Missing
    # here was a real bug, not a deliberate scope decision: this module's
    # own docstring already claimed "MLB run-line" support when it was
    # built, but the dispatch below only ever matched "game_spread_ou" —
    # every MLB run-line recommendation was silently unsettleable (caught
    # live 2026-08-23 while auditing why zero Official picks were being
    # produced).
    "game_runline_ou",
    # 2026-09-19 (CFB): a team total is an Over/Under against ONE team's
    # own score, not the combined away+home total — away and home are
    # kept as two distinct market_types (see src/sports/cfb.py's
    # GAME_TEAM_TOTAL_AWAY/GAME_TEAM_TOTAL_HOME) rather than one type
    # disambiguated by side, so they never collide with each other or
    # with game_total_ou's own O/U grouping. Only CFB registers these
    # MarketConfigs today; adding them here is purely additive for every
    # other league (they'll simply never appear in another league's
    # recommendations).
    "game_team_total_away_ou", "game_team_total_home_ou",
    # 2026-09-19 (CFB): 1st-quarter / 1st-half moneyline/spread/total --
    # graded against the score THROUGH that period (see
    # _score_through_period/database.db_manager.event_period_scores),
    # never the final score. Only CFB registers these MarketConfigs
    # today (src/sports/cfb.py); purely additive for every other league.
    "game_moneyline_1q", "game_spread_1q_ou", "game_total_1q_ou",
    "game_moneyline_1h", "game_spread_1h_ou", "game_total_1h_ou",
})

# market_type -> how many periods (quarters) to sum for that market's
# own score -- 1st quarter is period 1 alone; 1st half is periods 1+2.
# Deliberately a plain dict, not a guess-from-the-name parser: adding a
# market this doesn't know needs an explicit entry, not a silently wrong
# inferred value.
_PERIOD_MARKET_THROUGH_PERIOD = {
    "game_moneyline_1q": 1, "game_spread_1q_ou": 1, "game_total_1q_ou": 1,
    "game_moneyline_1h": 2, "game_spread_1h_ou": 2, "game_total_1h_ou": 2,
}

# Market types graded against a period score rather than the final score.
PERIOD_MARKET_TYPES = frozenset(_PERIOD_MARKET_THROUGH_PERIOD)

# Status strings recognized as "the game will never produce a final score."
# Verified field names: src/mlb_results.py reads MLB StatsAPI's
# gameData.status.abstractGameState/detailedState; src/nfl_results.py
# reads ESPN's status.type.name (STATUS_* constants) — both already
# persist the literal source status string when calling
# save_event_result(), rather than only ever writing "FINAL" or nothing.
VOID_STATUS_VALUES = frozenset({
    "POSTPONED", "CANCELLED", "CANCELED", "SUSPENDED",
    "STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED", "STATUS_SUSPENDED",
})
FINAL_STATUS_VALUES = frozenset({"FINAL", "STATUS_FINAL"})


def grade_moneyline(side: str, side_score: int | None, opponent_score: int | None) -> str:
    """Grade a moneyline pick. PUSH only on an exact final-score tie
    (possible in some sports, e.g. an NFL regular-season tie)."""
    side = (side or "").upper()
    if side not in ("AWAY", "HOME") or side_score is None or opponent_score is None:
        return SETTLEMENT_UNRESOLVED
    if side_score > opponent_score:
        return SETTLEMENT_WIN
    if side_score < opponent_score:
        return SETTLEMENT_LOSS
    return SETTLEMENT_PUSH


def grade_spread(
    side: str, side_score: int | None, opponent_score: int | None, raw_line: float | None,
) -> str:
    """Grade a spread/run-line pick using the SIGNED line captured at
    recommendation time (favorite negative, underdog positive — standard
    convention). Never reconstructs the sign from context; a recommendation
    missing ``raw_line`` is flagged NEEDS_REVIEW rather than assumed.

    margin_for_side + raw_line > 0  => side covered (WIN)
                                 < 0  => side did not cover (LOSS)
                                == 0  => PUSH
    """
    side = (side or "").upper()
    if side not in ("AWAY", "HOME") or side_score is None or opponent_score is None:
        return SETTLEMENT_UNRESOLVED
    if raw_line is None:
        return SETTLEMENT_NEEDS_REVIEW
    margin = (side_score - opponent_score) + raw_line
    if margin > 0:
        return SETTLEMENT_WIN
    if margin < 0:
        return SETTLEMENT_LOSS
    return SETTLEMENT_PUSH


def grade_total(side: str, away_score: int | None, home_score: int | None, line: float | None) -> str:
    """Grade a game-total (Over/Under combined score) pick. Reuses the
    existing generic ``grade_ou`` — a game total is just an Over/Under
    comparison against ``away_score + home_score``, no different from any
    other O/U market."""
    if away_score is None or home_score is None:
        return SETTLEMENT_UNRESOLVED
    return grade_ou(float(away_score + home_score), line, side)


def grade_team_total(side: str, team_score: int | None, line: float | None) -> str:
    """Grade a team total (Over/Under ONE team's own score) pick. Same
    shape as grade_total, just against a single team's own final score
    instead of the combined away+home total."""
    if team_score is None:
        return SETTLEMENT_UNRESOLVED
    return grade_ou(float(team_score), line, side)


def _score_through_period(period_scores: dict, through_period: int) -> tuple[int | None, int | None]:
    """Sum away/home scores for periods 1..through_period (inclusive).
    Returns (None, None) if ANY period in that range is missing -- never
    partially sums (e.g. a 1st-half total computed from only period 1's
    score, with period 2 not yet recorded, would be a real, silently
    wrong number, not just an incomplete one)."""
    away_total = 0
    home_total = 0
    for period in range(1, through_period + 1):
        entry = period_scores.get(period)
        if not entry or entry.get("away_score") is None or entry.get("home_score") is None:
            return None, None
        away_total += entry["away_score"]
        home_total += entry["home_score"]
    return away_total, home_total


def classify_event_status(final_status: str | None) -> str:
    """Return "final", "void", or "pending" for a raw event_results.final_status.

    An unrecognized non-empty status is treated as "pending" (safe
    default — never silently voided or finalized on a status string this
    code doesn't specifically recognize) rather than guessed.
    """
    status = (final_status or "").upper()
    if status in FINAL_STATUS_VALUES:
        return "final"
    if status in VOID_STATUS_VALUES:
        return "void"
    return "pending"


def grade_game_recommendation(
    rec: dict, event_result: dict | None, period_scores: dict | None = None,
) -> tuple[str, dict]:
    """Grade one game-level recommendation against its event_results row.

    Returns (settlement_status, detail) where detail carries the
    final score/side info worth persisting alongside the status (for
    settlement_reason / audit).

    *rec* must carry: market_type, side, line, raw_line, event_id.
    *event_result* is the row from database.db_manager (or None if the
    event isn't in event_results yet — game not final, still pending).
    *period_scores* is database.db_manager.get_event_period_scores'
    return value ({period: {"away_score", "home_score"}}) — only needed
    for 1st-quarter/1st-half market types (see
    _PERIOD_MARKET_THROUGH_PERIOD); every other market type ignores it.
    A 1Q/1H recommendation settles at the same time the WHOLE game goes
    final (this module gates on event_result's final_status the same as
    every other game market, not on the period alone finishing) — CFB's
    result ingestion only captures period scores once a game is
    confirmed final in the first place (see src/cfb_results.py), so this
    is the real, current behavior, not an arbitrary added delay.
    """
    market_type = rec.get("market_type", "")
    if market_type not in GAME_MARKET_TYPES:
        return SETTLEMENT_UNRESOLVED, {"reason": "not_a_game_market"}

    if event_result is None:
        return SETTLEMENT_UNRESOLVED, {"reason": "event_not_final_yet"}

    status_class = classify_event_status(event_result.get("final_status"))
    if status_class == "void":
        return SETTLEMENT_VOID, {"reason": f"game status: {event_result.get('final_status')}"}
    if status_class == "pending":
        return SETTLEMENT_UNRESOLVED, {"reason": f"game status not yet final: {event_result.get('final_status')!r}"}

    through_period = _PERIOD_MARKET_THROUGH_PERIOD.get(market_type)
    if through_period is not None:
        away_score, home_score = _score_through_period(period_scores or {}, through_period)
        if away_score is None or home_score is None:
            return SETTLEMENT_UNRESOLVED, {"reason": f"period scores through period {through_period} not yet available"}
        score_label = f"score through period {through_period}"
    else:
        away_score = event_result.get("away_score")
        home_score = event_result.get("home_score")
        score_label = "final score"

    side = (rec.get("side") or "").upper()

    if side in ("AWAY", "HOME"):
        side_score = away_score if side == "AWAY" else home_score
        opponent_score = home_score if side == "AWAY" else away_score
    else:
        side_score = opponent_score = None

    detail = {
        "away_score": away_score, "home_score": home_score,
        "reason": f"{score_label} {away_score}-{home_score}",
    }

    if market_type in ("game_moneyline_1q", "game_moneyline_1h"):
        # A tied quarter/half is common (7-7 after one quarter), and whether a two-way
        # period moneyline is refunded, lost or settled as a draw differs by sportsbook.
        # Unprovable here, so a tie is flagged for manual review, never guessed as a PUSH.
        if side_score is not None and side_score == opponent_score:
            return SETTLEMENT_NEEDS_REVIEW, {
                **detail, "reason": f"{score_label} tied {away_score}-{home_score}: two-way period moneyline rules vary by book",
            }
        return grade_moneyline(side, side_score, opponent_score), detail
    if market_type == "game_moneyline":
        return grade_moneyline(side, side_score, opponent_score), detail
    if market_type in ("game_spread_ou", "game_runline_ou", "game_spread_1q_ou", "game_spread_1h_ou"):
        return grade_spread(side, side_score, opponent_score, rec.get("raw_line")), detail
    if market_type in ("game_total_ou", "game_total_1q_ou", "game_total_1h_ou"):
        return grade_total(side, away_score, home_score, rec.get("line")), detail
    if market_type == "game_team_total_away_ou":
        return grade_team_total(side, away_score, rec.get("line")), detail
    if market_type == "game_team_total_home_ou":
        return grade_team_total(side, home_score, rec.get("line")), detail

    return SETTLEMENT_UNRESOLVED, {"reason": "unhandled_market_type"}
