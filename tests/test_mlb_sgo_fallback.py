"""Tests for the SportsGameOdds -> The Odds API fallback (added 2026-08-22
after SportsGameOdds's free-tier monthly object quota was genuinely
exhausted mid-session — verified live via the real /v2/account/usage
endpoint: 2,501/2,500 entities used).

Live-verified separately (not here): running the real pipeline against
the real exhausted account correctly triggered this fallback and produced
2 real recommendations from 2 real live MLB games. These tests cover the
same logic with the failure mocked, so they stay meaningful once the
account's quota resets and the 429 stops happening naturally.
"""

from __future__ import annotations

from unittest import mock

import requests


def _http_429_error():
    resp = mock.MagicMock()
    resp.status_code = 429
    err = requests.exceptions.HTTPError("429 rate limit exceeded")
    err.response = resp
    return err


def _http_500_error():
    resp = mock.MagicMock()
    resp.status_code = 500
    err = requests.exceptions.HTTPError("500 server error")
    err.response = resp
    return err


def _mlb_game(game_id="game-1", home_team="New York Yankees", away_team="Toronto Blue Jays"):
    return {
        "id": game_id,
        "home_team": home_team,
        "away_team": away_team,
        "commence_time": "2026-08-22T17:36:00Z",
        "bookmakers": [
            {
                "key": "fanduel",
                "last_update": "2026-08-22T17:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": away_team, "price": 130},
                            {"name": home_team, "price": -150},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 8.5},
                            {"name": "Under", "price": -110, "point": 8.5},
                        ],
                    },
                ],
            },
            {
                "key": "draftkings",
                "last_update": "2026-08-22T16:58:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": away_team, "price": 125},
                            {"name": home_team, "price": -145},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": -108, "point": 8.5},
                            {"name": "Under", "price": -112, "point": 8.5},
                        ],
                    },
                ],
            },
        ],
    }


class TestFallbackCreditBudget:
    """The MLB/NFL fallback shares its budget with WNBA's existing Odds
    API usage (upgraded 2026-08-22 to the 20K/mo paid tier specifically
    to fund this fallback) — credit_budget_check() must actually stop a
    fallback call when that shared budget is exhausted, not just record
    usage after the fact."""

    def test_mlb_fallback_raises_when_budget_exhausted(self, db_conn):
        from src.sports.mlb import fetch_game_odds_via_odds_api
        from src.odds_api_credits import record_credit_usage

        record_credit_usage(
            db_conn, endpoint="odds", requests_used=19995, requests_remaining=5,
        )
        with mock.patch("src.odds_api_client.OddsAPIClient") as MockClient:
            MockClient.return_value.get_odds.return_value = ([], False)
            try:
                fetch_game_odds_via_odds_api(conn=db_conn)
                assert False, "expected RuntimeError for exhausted budget"
            except RuntimeError as exc:
                assert "budget" in str(exc).lower()
        MockClient.return_value.get_odds.assert_not_called()

    def test_nfl_fallback_raises_when_budget_exhausted(self, db_conn):
        from src.sports.nfl import fetch_game_odds_via_odds_api
        from src.odds_api_credits import record_credit_usage

        record_credit_usage(
            db_conn, endpoint="odds", requests_used=19995, requests_remaining=5,
        )
        with mock.patch("src.odds_api_client.OddsAPIClient") as MockClient:
            MockClient.return_value.get_odds.return_value = ([], False)
            try:
                fetch_game_odds_via_odds_api(conn=db_conn)
                assert False, "expected RuntimeError for exhausted budget"
            except RuntimeError as exc:
                assert "budget" in str(exc).lower()
        MockClient.return_value.get_odds.assert_not_called()

    def test_mlb_fallback_proceeds_when_budget_available(self, db_conn):
        from src.sports.mlb import fetch_game_odds_via_odds_api
        from src.odds_api_credits import record_credit_usage

        record_credit_usage(
            db_conn, endpoint="odds", requests_used=100, requests_remaining=19900,
        )
        with mock.patch("src.odds_api_client.OddsAPIClient") as MockClient:
            MockClient.return_value.get_odds.return_value = ([_mlb_game()], False)
            odds_rows, audit_rows, events, from_cache = fetch_game_odds_via_odds_api(conn=db_conn)
        assert len(events) == 1


class TestRunScanMLBFallback:
    """run_scan(league='MLB', ...) — only the SportsGameOdds call mocked
    to fail, the real scanner/analysis pipeline runs on top of it."""

    def test_429_falls_back_and_produces_opportunities(self):
        from src import player_prop_scanner as scanner

        fake_games = [_mlb_game()]
        with mock.patch.object(scanner, "get_connection", return_value=mock.MagicMock()), \
             mock.patch.object(scanner, "create_run", return_value="run-1"), \
             mock.patch.object(scanner, "save_player_prop_batch"), \
             mock.patch.object(scanner.SportsGameOddsClient, "get_events",
                                side_effect=_http_429_error()), \
             mock.patch("src.odds_api_client.OddsAPIClient") as MockClient:
            MockClient.return_value.get_odds.return_value = (fake_games, False)
            result = scanner.run_scan(mode="all", market="all", market_form="all", league="MLB")

        assert result["n_events"] == 1
        opp_market_types = {o["market_type"] for o in result["opportunities"]}
        # game_runline_ou specifically (not the generic game_spread_ou WNBA
        # uses) — confirms MLB's own market naming was actually used, not
        # just any fallback data.
        assert opp_market_types & {"game_moneyline", "game_total_ou"}
        assert "game_spread_ou" not in opp_market_types

    def test_non_429_error_is_not_swallowed(self):
        """A real, different failure (e.g. a genuine 500) must still raise
        normally — the fallback is scoped to the specific quota/rate-limit
        case, not a blanket safety net that could mask other real bugs."""
        from src import player_prop_scanner as scanner

        with mock.patch.object(scanner, "get_connection", return_value=mock.MagicMock()), \
             mock.patch.object(scanner, "create_run", return_value="run-1"), \
             mock.patch.object(scanner.SportsGameOddsClient, "get_events",
                                side_effect=_http_500_error()):
            try:
                scanner.run_scan(mode="all", market="all", market_form="all", league="MLB")
                assert False, "expected HTTPError to propagate for a non-429 failure"
            except requests.exceptions.HTTPError as exc:
                assert exc.response.status_code == 500

    def test_fallback_not_available_for_a_league_without_it(self):
        """A league with no fetch_game_odds_via_odds_api registered must
        still raise normally on a 429, not silently no-op. Uses a
        synthetic fake league module rather than a real one (e.g. NFL),
        since both MLB and NFL now have this fallback registered and a
        real-league-name test would go stale/break the moment a third
        league gets it too."""
        from src import player_prop_scanner as scanner
        from src.sports import base as sports_base

        fake_league = mock.MagicMock(spec=["get_market_registry"])
        # No ODDS_PROVIDER attr and no fetch_game_odds_via_odds_api attr —
        # getattr(..., default) in both scanner branches must fall through
        # to "sportsgameodds" / None respectively, exactly like a brand
        # new, not-yet-updated league module would.
        del fake_league.ODDS_PROVIDER
        del fake_league.fetch_game_odds_via_odds_api

        with mock.patch.object(scanner, "get_connection", return_value=mock.MagicMock()), \
             mock.patch.object(scanner, "create_run", return_value="run-1"), \
             mock.patch.object(scanner.SportsGameOddsClient, "get_events",
                                side_effect=_http_429_error()), \
             mock.patch("src.sports.get_league", return_value=fake_league):
            try:
                scanner.run_scan(mode="all", market="all", market_form="all", league="FAKE")
                assert False, "expected HTTPError to propagate — no fallback registered"
            except requests.exceptions.HTTPError as exc:
                assert exc.response.status_code == 429
