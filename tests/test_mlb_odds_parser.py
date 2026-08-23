"""Tests for the MLB-via-The-Odds-API game-odds parser (the SportsGameOdds
quota-exhaustion fallback path, added 2026-08-22).

The row-building mechanics are already covered thoroughly by
tests/test_wnba_odds.py (both parsers share the same core via
src/odds_api_game_parser.py) — these tests focus on what's actually
different for MLB: the market-type naming (game_runline_ou instead of
WNBA's game_spread_ou) and the sign/magnitude handling for a real run-line
value, using a fixture shaped exactly like a real live response (see
src/mlb_odds_parser.py's docstring for the real event this was verified
against).
"""

from __future__ import annotations

from src.mlb_odds_parser import parse_mlb_game_odds


def _game(
    game_id="37d5ada29cdca43791f56ead0c6f958a",
    home_team="New York Yankees",
    away_team="Toronto Blue Jays",
    run_line_point=1.5,
):
    """Shaped like one entry in GET /v4/sports/baseball_mlb/odds — verified
    field names/nesting against a real live response 2026-08-22."""
    return {
        "id": game_id,
        "sport_key": "baseball_mlb",
        "sport_title": "MLB",
        "commence_time": "2026-08-22T17:36:00Z",
        "home_team": home_team,
        "away_team": away_team,
        "bookmakers": [
            {
                "key": "fanduel",
                "title": "FanDuel",
                "last_update": "2026-08-22T17:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-08-22T17:00:00Z",
                        "outcomes": [
                            {"name": away_team, "price": 130},
                            {"name": home_team, "price": -150},
                        ],
                    },
                    {
                        "key": "spreads",
                        "last_update": "2026-08-22T17:00:00Z",
                        "outcomes": [
                            {"name": away_team, "price": 110, "point": run_line_point},
                            {"name": home_team, "price": -130, "point": -run_line_point},
                        ],
                    },
                    {
                        "key": "totals",
                        "last_update": "2026-08-22T17:00:00Z",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 8.5},
                            {"name": "Under", "price": -110, "point": 8.5},
                        ],
                    },
                ],
            },
            {
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": "2026-08-22T16:58:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-08-22T16:58:00Z",
                        "outcomes": [
                            {"name": away_team, "price": 125},
                            {"name": home_team, "price": -145},
                        ],
                    },
                ],
            },
        ],
    }


class TestParseMLBGameOdds:
    def test_uses_run_line_naming_not_generic_spread(self):
        """The one real difference from WNBA's parser: MLB's registered
        market type is game_runline_ou (src/prop_config.py::GAME_RUN_LINE),
        not game_spread_ou — same 'spreads' market key on The Odds API's
        side, different internal naming, matching this platform's own
        existing MLB market registry (not a provider distinction)."""
        result = parse_mlb_game_odds([_game()])
        market_types = {r["market_type"] for r in result.odds_rows}
        assert market_types == {"game_moneyline", "game_runline_ou", "game_total_ou"}
        assert "game_spread_ou" not in market_types

    def test_parses_all_books_and_markets(self):
        result = parse_mlb_game_odds([_game()])
        # fanduel: h2h(2) + spreads(2) + totals(2) = 6; draftkings: h2h(2) = 2
        assert len(result.odds_rows) == 8
        assert result.audit_rows and all(not r["excluded"] for r in result.audit_rows)

    def test_run_line_sign_and_magnitude(self):
        result = parse_mlb_game_odds([_game(run_line_point=1.5)])
        run_line_rows = [r for r in result.odds_rows if r["market_type"] == "game_runline_ou"]
        assert len(run_line_rows) == 2
        # line is the abs-valued magnitude (for O/U-style pairing); raw_line
        # keeps the real signed favorite/underdog value for settlement.
        for row in run_line_rows:
            assert row["line"] == 1.5
        raw_lines = {row["raw_line"] for row in run_line_rows}
        assert raw_lines == {1.5, -1.5}

    def test_moneyline_sides_resolve_to_real_teams(self):
        result = parse_mlb_game_odds([_game()])
        ml_rows = [r for r in result.odds_rows if r["market_type"] == "game_moneyline"]
        team_names = {r["team_name"] for r in ml_rows}
        assert team_names == {"New York Yankees", "Toronto Blue Jays"}

    def test_unrecognized_team_name_excludes_row(self):
        game = _game()
        game["bookmakers"][0]["markets"][0]["outcomes"][0]["name"] = "Some Other Team"
        result = parse_mlb_game_odds([game])
        # The unresolvable outcome is excluded (audited), never guessed.
        excluded = [r for r in result.audit_rows if r["excluded"]]
        assert any("Could not resolve side" in r["validation_reason"] for r in excluded)

    def test_empty_games_list_returns_empty_result(self):
        result = parse_mlb_game_odds([])
        assert result.odds_rows == []
        assert result.audit_rows == []


class TestRunLineDirectionDisagreement:
    """Regression coverage for a real bug caught live 2026-08-23: a book
    that disagrees with the consensus on WHICH TEAM IS FAVORED for the
    run line (e.g. draftkings has away -1.5, fanduel has away +1.5 — a
    genuinely different real bet at the same magnitude) must not be
    grouped with the majority. Grouping purely by abs(line) let one book's
    row silently overwrite/blend with the opposite-direction consensus,
    producing a nonsensical ~45% blended "EV" in a real live run.
    """

    def _disagreeing_game(self):
        away, home = "Atlanta Braves", "Milwaukee Brewers"
        game = _game(home_team=home, away_team=away, run_line_point=1.5)
        # Majority (draftkings): away favored, laying -1.5.
        game["bookmakers"][1]["markets"] = [{
            "key": "spreads",
            "last_update": "2026-08-22T16:58:00Z",
            "outcomes": [
                {"name": away, "price": 160, "point": -1.5},
                {"name": home, "price": -190, "point": 1.5},
            ],
        }]
        # fanduel (index 0, from _game()) already has away +1.5/home -1.5 —
        # the OPPOSITE direction: away is the underdog here, not the favorite.
        return game

    def test_disagreeing_books_land_in_different_groups(self):
        result = parse_mlb_game_odds([self._disagreeing_game()])
        run_line_rows = [r for r in result.odds_rows if r["market_type"] == "game_runline_ou"]
        assert len(run_line_rows) == 4
        fanduel_away = next(r for r in run_line_rows
                             if r["sportsbook"] == "fanduel" and r["side"] == "AWAY")
        draftkings_away = next(r for r in run_line_rows
                                if r["sportsbook"] == "draftkings" and r["side"] == "AWAY")
        assert fanduel_away["raw_line"] == 1.5
        assert draftkings_away["raw_line"] == -1.5
        assert fanduel_away["market_group_key"] != draftkings_away["market_group_key"]

    def test_agreeing_books_still_share_one_group(self):
        """Sanity check: when books agree on direction, they must still
        pair into the same group — the fix must not over-correct into
        never grouping anything."""
        game = _game(run_line_point=1.5)  # fanduel + draftkings both default-shaped, same direction
        game["bookmakers"][1]["markets"] = [{
            "key": "spreads",
            "last_update": "2026-08-22T16:58:00Z",
            "outcomes": [
                {"name": game["away_team"], "price": 105, "point": 1.5},
                {"name": game["home_team"], "price": -125, "point": -1.5},
            ],
        }]
        result = parse_mlb_game_odds([game])
        run_line_rows = [r for r in result.odds_rows if r["market_type"] == "game_runline_ou"]
        group_keys = {r["market_group_key"] for r in run_line_rows}
        assert len(group_keys) == 1
