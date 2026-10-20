"""NFL Pinnacle prop unit map (offline only: no live API calls, no credits, no database).

PINNACLE_PROP_UNITS_BY_LEAGUE["NFL"] was empty, so src/odds_api_pinnacle_feed.py::_parse_props_response discarded
every NFL outcome The Odds API returned for Pinnacle. These tests pin the four-entry map and prove the fixture (an
abbreviated real response) now parses into the intended internal market types and matches on the exact line only.
"""

import json
from pathlib import Path

import src.prop_config as cfg
from src import odds_api_pinnacle_feed as feed
from src.odds_api_pinnacle_feed import _parse_props_response
from src.pinnacle_feed import build_pinnacle_lookup, match_pinnacle

FIXTURE = Path(__file__).parent / "fixtures" / "pinnacle_nfl_props_event.json"

EXPECTED_MAP = {
    "player_pass_yds": "passing_yards_ou",
    "player_rush_yds": "rushing_yards_ou",
    "player_receptions": "receiving_receptions_ou",
    "player_reception_yds": "receiving_yards_ou",
}


def nfl_event() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def nfl_map() -> dict:
    return feed._prop_market_type_map_for_league("NFL")


class TestNflPropUnitMap:
    def test_map_is_exactly_the_four_supported_markets(self):
        assert cfg.PINNACLE_PROP_UNITS_BY_LEAGUE["NFL"] == EXPECTED_MAP

    def test_map_is_pinned_to_the_sportsbook_side_parser(self):
        """Keys are The Odds API market keys and values our market types, exactly nfl_props_parser's map: the
        two sides of a comparison can never drift apart without this failing."""
        from src.nfl_props_parser import _PROP_MARKET_TYPE
        assert cfg.PINNACLE_PROP_UNITS_BY_LEAGUE["NFL"] == _PROP_MARKET_TYPE

    def test_unsupported_nfl_markets_stay_unmapped(self):
        for unsupported in ("player_pass_tds", "player_anytime_td", "player_pass_attempts",
                            "player_pass_completions", "player_rush_attempts", "player_defensive_interceptions"):
            assert unsupported not in cfg.PINNACLE_PROP_UNITS_BY_LEAGUE["NFL"]

    def test_labels_cannot_collide_with_pinnapi_unit_names(self):
        """The direct pinnapi.com feed shares this map. Its real unit names are CamelCase (e.g. "TotalBases");
        the NFL labels are snake_case Odds API keys, so the direct feed still recognises no NFL unit."""
        from src.pinnacle_feed import parse_player_props
        for unit in cfg.PINNACLE_PROP_UNITS_BY_LEAGUE["NFL"]:
            assert unit.startswith("player_") and unit == unit.lower()
        payload = {"events": [
            {"id": 1, "league_name": "NFL", "home": "A", "away": "B", "periods": {}},
            {"id": 2, "parent_id": 1, "special_category": "Player Props", "special_units": "PassYds",
             "special": "X Total PassYds", "special_markets": {}},
        ]}
        assert parse_player_props(payload, league="NFL") == []

    def test_mlb_and_wnba_maps_are_unchanged(self):
        assert cfg.PINNACLE_PROP_UNITS_BY_LEAGUE["MLB"] == {
            "Strikeouts": "pitching_strikeouts_ou", "HitsAllowed": "pitching_hits_ou",
            "EarnedRuns": "pitching_earnedRuns_ou", "PitchingOuts": "pitching_outs_ou",
            "TotalBases": "batting_totalBases_ou", "HomeRuns": "batting_homeRuns_ou"}
        assert cfg.PINNACLE_PROP_UNITS_BY_LEAGUE["WNBA"] == {
            "Points": "player_points_ou", "Rebounds": "player_rebounds_ou", "Assists": "player_assists_ou",
            "Threes Made": "player_threes_ou"}

    def test_nfl_props_do_not_become_auto_settleable_or_official_eligible(self):
        """The map only lets Pinnacle references attach to NFL recommendations; it must not widen settlement."""
        for market_type in EXPECTED_MAP.values():
            assert market_type not in cfg.AUTO_SETTLEABLE_MARKET_TYPES


class TestNflPropParsing:
    def test_all_four_keys_parse_into_the_intended_internal_types(self):
        props = _parse_props_response(nfl_event(), nfl_map(), "NFL")
        assert len(props) == 4
        unit_to_type = cfg.PINNACLE_PROP_UNITS_BY_LEAGUE["NFL"]
        assert {p.unit: unit_to_type[p.unit] for p in props} == EXPECTED_MAP
        assert {p.unit for p in props} == set(EXPECTED_MAP)

    def test_parsed_values_are_intact(self):
        props = _parse_props_response(nfl_event(), nfl_map(), "NFL")
        nix = next(p for p in props if p.unit == "player_pass_yds")
        assert (nix.player_name, nix.line, nix.over_american, nix.under_american) == ("Bo Nix", 200.5, -102, -123)
        assert nix.home_name == "Denver Broncos" and nix.away_name == "Seattle Seahawks" and nix.last_updated

    def test_every_supported_market_matches_by_its_internal_type_at_the_exact_line(self):
        lookup = build_pinnacle_lookup(_parse_props_response(nfl_event(), nfl_map(), "NFL"))
        cases = [
            ("passing_yards_ou", "Bo Nix", 200.5),
            ("rushing_yards_ou", "Bo Nix", 15.5),
            ("receiving_receptions_ou", "AJ Barner", 2.5),
            ("receiving_yards_ou", "AJ Barner", 23.5),
        ]
        for market_type, player, line in cases:
            kw = dict(home_name="Denver Broncos", away_name="Seattle Seahawks", player_name=player,
                      market_type=market_type, league="NFL")
            assert match_pinnacle(lookup, line=line, **kw) is not None, market_type
            assert match_pinnacle(lookup, line=line - 1.0, **kw) is None, market_type   # other line = other bet

    def test_a_player_and_stat_do_not_cross_match(self):
        lookup = build_pinnacle_lookup(_parse_props_response(nfl_event(), nfl_map(), "NFL"))
        kw = dict(home_name="Denver Broncos", away_name="Seattle Seahawks", league="NFL")
        assert match_pinnacle(lookup, player_name="Bo Nix", market_type="receiving_yards_ou", line=200.5, **kw) is None
        assert match_pinnacle(lookup, player_name="AJ Barner", market_type="passing_yards_ou", line=23.5, **kw) is None

    def test_a_market_outside_the_four_is_still_dropped(self):
        ev = nfl_event()
        ev["bookmakers"][0]["markets"].append({"key": "player_pass_tds", "outcomes": [
            {"name": "Over", "description": "Bo Nix", "price": 120, "point": 1.5},
            {"name": "Under", "description": "Bo Nix", "price": -150, "point": 1.5}]})
        props = _parse_props_response(ev, nfl_map(), "NFL")
        assert len(props) == 4 and all(p.unit != "player_pass_tds" for p in props)

    def test_unpaired_or_mismatched_sides_are_still_dropped(self):
        ev = nfl_event()
        ev["bookmakers"][0]["markets"] = [{"key": "player_pass_yds", "outcomes": [
            {"name": "Over", "description": "D", "price": -110, "point": 200.5},             # unpaired
            {"name": "Over", "description": "E", "price": -110, "point": 200.5},
            {"name": "Under", "description": "E", "price": -110, "point": 199.5},            # point mismatch
        ]}]
        assert _parse_props_response(ev, nfl_map(), "NFL") == []
