"""Regression coverage for a real bug caught live 2026-08-23: game-level
spread/run-line markets were grouped by abs(line) alone, which lets two
sportsbooks that DISAGREE on which team is favored (a genuinely different
real bet at the same point magnitude — e.g. one book has the away team
-1.5, another has the away team +1.5) collide into the same market group.
Comparing across them produced a nonsensical ~45% blended "EV" in a real
live pipeline run.

This is the primary SportsGameOdds-facing path
(``src.player_prop_parser._process_entry``); the parallel fix for the
Odds-API fallback/WNBA path is covered in
``tests/test_mlb_odds_parser.py::TestRunLineDirectionDisagreement``.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.player_prop_parser import _process_entry
from src.prop_config import GAME_RUN_LINE, GAME_TOTAL, GAME_MONEYLINE


def _teams():
    return {"home_name": "Milwaukee Brewers", "away_name": "Atlanta Braves",
            "team_id": "GAME", "team_name": "Atlanta Braves"}


def _run_entry(book_name, spread, side_raw="away"):
    odds_rows, audit_rows = [], []
    _process_entry(
        event_id="ev1", odd_id="points-game-sp-away-game-ou-1.5", odd_data={},
        book_name=book_name, book_data={"spread": spread, "odds": -110, "available": True},
        player_id="GAME", player_name="Run Line", side="OVER", side_raw=side_raw,
        teams=_teams(), is_alt_line=0, market_type="game_runline_ou",
        odds_rows=odds_rows, audit_rows=audit_rows, game_mc=GAME_RUN_LINE,
    )
    return odds_rows[0] if odds_rows else None


def test_books_disagreeing_on_favorite_land_in_different_groups():
    away_favored = _run_entry("draftkings", spread=-1.5, side_raw="away")
    away_underdog = _run_entry("fanduel", spread=1.5, side_raw="away")
    assert away_favored["raw_line"] == -1.5
    assert away_underdog["raw_line"] == 1.5
    # Both abs-value to the same "line" for display/pairing purposes...
    assert away_favored["line"] == away_underdog["line"] == 1.5
    # ...but must NOT share a group_key, since they represent opposite bets.
    assert away_favored["market_group_key"] != away_underdog["market_group_key"]


def test_home_side_of_same_real_bet_shares_the_away_side_group():
    """The away/home rows of the SAME real line (mirror-signed) must
    still pair into one group — the fix must not break normal pairing."""
    odds_rows, audit_rows = [], []
    for book_name, side_raw, spread in (("draftkings", "away", -1.5), ("draftkings", "home", 1.5)):
        _process_entry(
            event_id="ev1", odd_id="points-game-sp-away-game-ou-1.5", odd_data={},
            book_name=book_name, book_data={"spread": spread, "odds": -110, "available": True},
            player_id="GAME", player_name="Run Line", side="OVER" if side_raw == "away" else "UNDER",
            side_raw=side_raw, teams=_teams(), is_alt_line=0, market_type="game_runline_ou",
            odds_rows=odds_rows, audit_rows=audit_rows, game_mc=GAME_RUN_LINE,
        )
    assert len(odds_rows) == 2
    assert odds_rows[0]["market_group_key"] == odds_rows[1]["market_group_key"]


def test_agreeing_books_still_share_one_group():
    a = _run_entry("draftkings", spread=-1.5, side_raw="away")
    b = _run_entry("betmgm", spread=-1.5, side_raw="away")
    assert a["market_group_key"] == b["market_group_key"]


def test_totals_unaffected_by_the_sign_fix():
    """Totals have no favorite/underdog direction — the fix must be a
    no-op there, still grouping purely by the numeric line."""
    odds_rows, audit_rows = [], []
    for book_name, side_raw in (("draftkings", "over"), ("betmgm", "over")):
        _process_entry(
            event_id="ev1", odd_id="points-game-ou-total-game-ou-8.5", odd_data={},
            book_name=book_name, book_data={"overUnder": 8.5, "odds": -110, "available": True},
            player_id="GAME", player_name="Total", side="OVER", side_raw=side_raw,
            teams=_teams(), is_alt_line=0, market_type="game_total_ou",
            odds_rows=odds_rows, audit_rows=audit_rows, game_mc=GAME_TOTAL,
        )
    assert odds_rows[0]["market_group_key"] == odds_rows[1]["market_group_key"]


def test_moneyline_unaffected_by_the_sign_fix():
    odds_rows, audit_rows = [], []
    _process_entry(
        event_id="ev1", odd_id="points-game-ml-away-game-ou", odd_data={},
        book_name="draftkings", book_data={"odds": 130, "available": True},
        player_id="GAME", player_name="Moneyline", side="OVER", side_raw="away",
        teams=_teams(), is_alt_line=0, market_type="game_moneyline",
        odds_rows=odds_rows, audit_rows=audit_rows, game_mc=GAME_MONEYLINE,
    )
    assert odds_rows[0]["market_group_key"] == "ev1|GAME|game_moneyline|game|ML"
