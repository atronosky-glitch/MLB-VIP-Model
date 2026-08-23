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
})

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


def grade_game_recommendation(rec: dict, event_result: dict | None) -> tuple[str, dict]:
    """Grade one game-level recommendation against its event_results row.

    Returns (settlement_status, detail) where detail carries the
    final score/side info worth persisting alongside the status (for
    settlement_reason / audit).

    *rec* must carry: market_type, side, line, raw_line, event_id.
    *event_result* is the row from database.db_manager (or None if the
    event isn't in event_results yet — game not final, still pending).
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

    away_score = event_result.get("away_score")
    home_score = event_result.get("home_score")
    side = (rec.get("side") or "").upper()

    if side in ("AWAY", "HOME"):
        side_score = away_score if side == "AWAY" else home_score
        opponent_score = home_score if side == "AWAY" else away_score
    else:
        side_score = opponent_score = None

    detail = {
        "away_score": away_score, "home_score": home_score,
        "reason": f"final score {away_score}-{home_score}",
    }

    if market_type == "game_moneyline":
        return grade_moneyline(side, side_score, opponent_score), detail
    if market_type in ("game_spread_ou", "game_runline_ou"):
        return grade_spread(side, side_score, opponent_score, rec.get("raw_line")), detail
    if market_type == "game_total_ou":
        return grade_total(side, away_score, home_score, rec.get("line")), detail

    return SETTLEMENT_UNRESOLVED, {"reason": "unhandled_market_type"}
