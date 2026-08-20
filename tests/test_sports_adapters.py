"""Tests for the sport-agnostic sports/ adapter architecture.

Covers:
- src/sports/ package (registry, MLB/NFL/WNBA adapters)
- NFL market registry matching against real verified oddID shapes
- player_prop_parser.parse_player_props with an explicit (non-MLB) registry
- player_prop_scanner.run_scan end-to-end for NFL (mocked API client)
- src/nfl_results.py settlement extraction against synthetic ESPN fixtures

All fixtures are synthetic but shaped to match real API responses verified
live against SportsGameOdds v2 and ESPN's public NFL API on 2026-08-19 (see
src/sports/nfl.py and src/nfl_results.py docstrings for what was checked).
No test depends on a live network call.
"""

from __future__ import annotations

from unittest import mock

import pytest

from src.sports.base import MarketConfig, match_ou_market, match_yn_market, build_lookup_maps


# ───────────────────────────────────────────────────────────────────
# src/sports/ package
# ───────────────────────────────────────────────────────────────────

class TestSportsRegistry:
    def test_supported_leagues(self):
        from src.sports import supported_leagues
        assert supported_leagues() == ["MLB", "NFL", "WNBA"]

    def test_available_leagues_includes_all_three(self):
        """WNBA became available 2026-08-19 via The Odds API (game markets only)."""
        from src.sports import available_leagues
        assert set(available_leagues()) == {"MLB", "NFL", "WNBA"}

    def test_get_league_case_insensitive(self):
        from src.sports import get_league
        assert get_league("nfl") is get_league("NFL")

    def test_get_league_unknown_raises(self):
        from src.sports import get_league
        with pytest.raises(ValueError):
            get_league("NHL")

    def test_mlb_adapter_wraps_existing_registry(self):
        from src.sports import get_league
        from src.prop_config import MARKET_REGISTRY as mlb_registry
        mlb = get_league("MLB")
        assert mlb.AVAILABLE is True
        assert mlb.get_market_registry() is mlb_registry

    def test_nfl_adapter_available_with_markets(self):
        from src.sports import get_league
        nfl = get_league("NFL")
        assert nfl.AVAILABLE is True
        registry = nfl.get_market_registry()
        assert len(registry) >= 8
        assert all(isinstance(m, MarketConfig) for m in registry)

    def test_wnba_adapter_available_via_the_odds_api(self):
        """WNBA uses a different odds provider than MLB/NFL — see src/sports/wnba.py."""
        from src.sports import get_league
        wnba = get_league("WNBA")
        assert wnba.AVAILABLE is True
        assert wnba.UNAVAILABLE_REASON is None
        assert len(wnba.get_market_registry()) == 11  # 3 game markets + 8 player props
        assert wnba.ODDS_PROVIDER == "the_odds_api"
        assert hasattr(wnba.get_settlement_module(), "ingest_results_for_recommendations")

    def test_market_capability_report_structure(self):
        from src.sports import market_capability_report
        report = market_capability_report()
        assert set(report.keys()) == {"MLB", "NFL", "WNBA"}
        assert report["MLB"]["available"] is True
        assert report["NFL"]["available"] is True
        assert report["WNBA"]["available"] is True
        assert report["WNBA"]["n_markets"] == 11
        assert report["MLB"]["n_markets"] > 0
        assert report["NFL"]["n_markets"] > 0
        for entry in report["NFL"]["markets"]:
            assert {"cli_name", "display_name", "supports_ou", "supports_yn", "game_level"} <= entry.keys()

    def test_settlement_modules_have_common_interface(self):
        from src.sports import get_league
        for league in ("MLB", "NFL"):
            mod = get_league(league).get_settlement_module()
            assert hasattr(mod, "ingest_results_for_recommendations")


# ───────────────────────────────────────────────────────────────────
# src/sports/base.py — generic matching, registry-parameterized
# ───────────────────────────────────────────────────────────────────

class TestBaseMatching:
    def test_match_ou_market_against_arbitrary_registry(self):
        registry = [
            MarketConfig(
                cli_name="widgets", odd_id_stat_prefix="widget_count",
                market_type_ou="widget_count_ou", market_type_yn=None,
                display_name="Widgets", short_label="W", period="game",
            ),
        ]
        assert match_ou_market(registry, "widget_count-PLAYER1-game-ou-over") is not None
        assert match_ou_market(registry, "widget_count-PLAYER1-game-ou-sideways") is None
        assert match_yn_market(registry, "widget_count-PLAYER1-game-yn-yes") is None  # no YN variant

    def test_build_lookup_maps(self):
        registry = [
            MarketConfig(
                cli_name="a", odd_id_stat_prefix="stat_a", market_type_ou="a_ou",
                market_type_yn="a_yn", display_name="A", short_label="A", period="game",
            ),
        ]
        cli_map, ou_map, yn_map = build_lookup_maps(registry)
        assert cli_map["a"].cli_name == "a"
        assert "a_ou" in ou_map
        assert "a_yn" in yn_map


# ───────────────────────────────────────────────────────────────────
# NFL market registry vs. real verified oddID shapes
# ───────────────────────────────────────────────────────────────────

# Real oddIDs, copied verbatim from a live SportsGameOdds v2 NFL response
# inspected 2026-08-19 (see src/sports/nfl.py docstring).
REAL_NFL_ODD_IDS = {
    "game_moneyline": "points-away-game-ml-away",
    "game_spread_ou": "points-away-game-sp-away",
    "game_total_ou": "points-all-game-ou-over",
    "passing_yards_ou": "passing_yards-FERNANDO_MENDOZA_1_NFL-game-ou-over",
    "field_goals_made_ou": "fieldGoals_made-KAIMI_FAIRBAIRN_1_NFL-game-ou-over",
}


class TestNFLMarketRegistry:
    @pytest.mark.parametrize("market_type,odd_id", list(REAL_NFL_ODD_IDS.items()))
    def test_real_odd_id_matches_registered_market(self, market_type, odd_id):
        from src.sports.nfl import MARKET_REGISTRY
        match = match_ou_market(MARKET_REGISTRY, odd_id)
        assert match is not None, f"{odd_id} did not match any NFL registry entry"
        assert match.market_type_ou == market_type

    def test_anytime_touchdown_yn_matches(self):
        from src.sports.nfl import MARKET_REGISTRY
        match = match_yn_market(MARKET_REGISTRY, "touchdowns-SOME_PLAYER_NFL-game-yn-yes")
        assert match is not None
        assert match.market_type_yn == "anytime_touchdown_yn"

    def test_passing_interceptions_supports_both_forms(self):
        from src.sports.nfl import PASSING_INTERCEPTIONS
        assert PASSING_INTERCEPTIONS.supports_ou is True
        assert PASSING_INTERCEPTIONS.supports_yn is True

    def test_registry_has_no_duplicate_cli_names(self):
        from src.sports.nfl import MARKET_REGISTRY
        names = [m.cli_name for m in MARKET_REGISTRY]
        assert len(names) == len(set(names))

    def test_game_markets_use_away_home_side_map(self):
        from src.sports.nfl import GAME_MONEYLINE, GAME_SPREAD
        for mc in (GAME_MONEYLINE, GAME_SPREAD):
            assert mc.game_level is True
            assert mc.internal_side_map == {"AWAY": "over", "HOME": "under"}


# ───────────────────────────────────────────────────────────────────
# Real event fixture (verified structure) for parser/scanner tests
# ───────────────────────────────────────────────────────────────────

def _nfl_event(passing_yards_line="245.5"):
    """A synthetic NFL event, shaped exactly like the real SportsGameOdds v2
    response verified live 2026-08-19 (eventID/teams/players/odds/status,
    oddID grammar, byBookmaker.overUnder field name)."""
    return {
        "eventID": "nfl-evt-1",
        "leagueID": "NFL",
        "status": {"started": False, "ended": False,
                    "startsAt": "2026-09-07T17:00:00.000Z"},
        "teams": {
            "home": {"teamID": "CIN", "statEntityID": "home",
                      "names": {"long": "Cincinnati Bengals"}},
            "away": {"teamID": "DET", "statEntityID": "away",
                      "names": {"long": "Detroit Lions"}},
        },
        "players": {
            "JARED_GOFF_1_NFL": {"playerID": "JARED_GOFF_1_NFL", "name": "Jared Goff"},
        },
        "odds": {
            "points-away-game-ml-away": {
                "oddID": "points-away-game-ml-away", "marketName": "Moneyline",
                "statEntityID": "away", "periodID": "game", "betTypeID": "ml", "sideID": "away",
                "byBookmaker": {
                    "draftkings": {"odds": "+150", "lastUpdatedAt": "2026-09-07T00:00:00Z", "available": True},
                    "fanduel": {"odds": "+145", "lastUpdatedAt": "2026-09-07T00:00:00Z", "available": True},
                },
            },
            "points-home-game-ml-home": {
                "oddID": "points-home-game-ml-home", "marketName": "Moneyline",
                "statEntityID": "home", "periodID": "game", "betTypeID": "ml", "sideID": "home",
                "byBookmaker": {
                    "draftkings": {"odds": "-170", "lastUpdatedAt": "2026-09-07T00:00:00Z", "available": True},
                    "fanduel": {"odds": "-165", "lastUpdatedAt": "2026-09-07T00:00:00Z", "available": True},
                },
            },
            "passing_yards-JARED_GOFF_1_NFL-game-ou-over": {
                "oddID": "passing_yards-JARED_GOFF_1_NFL-game-ou-over",
                "marketName": "Jared Goff Passing Yards Over/Under",
                "statEntityID": "JARED_GOFF_1_NFL", "periodID": "game", "betTypeID": "ou", "sideID": "over",
                "playerID": "JARED_GOFF_1_NFL",
                "byBookmaker": {
                    "draftkings": {"odds": "-110", "overUnder": passing_yards_line,
                                   "lastUpdatedAt": "2026-09-07T00:00:00Z", "available": True},
                    "fanduel": {"odds": "-115", "overUnder": passing_yards_line,
                                "lastUpdatedAt": "2026-09-07T00:00:00Z", "available": True},
                },
            },
            "passing_yards-JARED_GOFF_1_NFL-game-ou-under": {
                "oddID": "passing_yards-JARED_GOFF_1_NFL-game-ou-under",
                "marketName": "Jared Goff Passing Yards Over/Under",
                "statEntityID": "JARED_GOFF_1_NFL", "periodID": "game", "betTypeID": "ou", "sideID": "under",
                "playerID": "JARED_GOFF_1_NFL",
                "byBookmaker": {
                    "draftkings": {"odds": "-110", "overUnder": passing_yards_line,
                                   "lastUpdatedAt": "2026-09-07T00:00:00Z", "available": True},
                    "fanduel": {"odds": "-105", "overUnder": passing_yards_line,
                                "lastUpdatedAt": "2026-09-07T00:00:00Z", "available": True},
                },
            },
        },
    }


class TestNFLParsing:
    def test_parse_player_props_with_nfl_registry(self):
        from src.player_prop_parser import parse_player_props
        from src.sports.nfl import MARKET_REGISTRY

        result = parse_player_props(_nfl_event(), registry=MARKET_REGISTRY)
        market_types = {r["market_type"] for r in result.odds_rows}
        assert "game_moneyline" in market_types
        assert "passing_yards_ou" in market_types

        prop_rows = [r for r in result.odds_rows if r["market_type"] == "passing_yards_ou"]
        assert len(prop_rows) == 4  # 2 sides x 2 books
        assert all(r["player_name"] == "Jared Goff" for r in prop_rows)
        assert all(r["line"] == 245.5 for r in prop_rows)

    def test_parse_player_props_resolves_name_from_players_map(self):
        """event.players[playerID].name must be used even when
        odd_data.playerNames is absent (verified real-world NFL case)."""
        from src.player_prop_parser import parse_player_props
        from src.sports.nfl import MARKET_REGISTRY

        event = _nfl_event()
        assert "playerNames" not in event["odds"]["passing_yards-JARED_GOFF_1_NFL-game-ou-over"]
        result = parse_player_props(event, registry=MARKET_REGISTRY)
        names = {r["player_name"] for r in result.odds_rows if r["market_type"] == "passing_yards_ou"}
        assert names == {"Jared Goff"}

    def test_parse_player_props_default_registry_is_mlb(self):
        """Omitting registry must not change any existing MLB call site's behavior."""
        from src.player_prop_parser import parse_player_props
        result_default = parse_player_props(_nfl_event())
        # NFL oddIDs (passing_yards-...) don't match MLB's registry at all,
        # so with the default (MLB) registry only unmatched/no-op rows occur —
        # proves the default truly is MLB's registry, not "any registry".
        market_types = {r["market_type"] for r in result_default.odds_rows}
        assert "passing_yards_ou" not in market_types


class TestNFLScanEnd2End:
    def test_run_scan_produces_nfl_opportunities(self):
        """Full run_scan(league='NFL', ...) pipeline, API client mocked."""
        from src import player_prop_scanner as scanner

        event = _nfl_event()
        with mock.patch.object(scanner, "get_connection", return_value=mock.MagicMock()), \
             mock.patch.object(scanner, "create_run", return_value="run-1"), \
             mock.patch.object(scanner, "save_player_prop_batch"), \
             mock.patch("src.api_client.SportsGameOddsClient.get_events",
                         return_value=({"data": [event]}, False)):
            result = scanner.run_scan(
                mode="all", market="all", market_form="all", league="NFL",
            )

        assert result["n_events"] == 1
        opp_market_types = {o["market_type"] for o in result["opportunities"]}
        assert "passing_yards_ou" in opp_market_types

    def test_run_scan_rejects_unavailable_league_via_cli(self, monkeypatch):
        """No real league is currently unavailable (WNBA became available
        2026-08-19), so this simulates one to prove the rejection path
        still works rather than deleting the test."""
        from src.player_prop_scanner import main
        from src.sports import wnba as wnba_mod
        monkeypatch.setattr(wnba_mod, "AVAILABLE", False)
        monkeypatch.setattr(wnba_mod, "UNAVAILABLE_REASON", "simulated for test")
        with pytest.raises(SystemExit) as exc:
            main(["--league", "WNBA"])
        assert exc.value.code == 1

    def test_run_scan_rejects_unknown_league_via_cli(self):
        from src.player_prop_scanner import main
        with pytest.raises(SystemExit) as exc:
            main(["--league", "NHL"])
        assert exc.value.code == 1
