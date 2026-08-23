"""Tests for the pinnapi.com Pinnacle feed integration (Phase 18)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.pinnacle_feed import (  # noqa: E402
    PinnacleFeedClient,
    PinnacleProp,
    PinnacleGameOdds,
    build_pinnacle_lookup,
    inject_pinnacle_reference,
    match_pinnacle,
    normalize_name,
    normalize_team_name,
    parse_mlb_props,
    parse_player_props,
    parse_game_odds,
    build_pinnacle_game_lookup,
    match_pinnacle_game,
    inject_pinnacle_game_reference,
)


# ==================================================================
# Fixtures
# ==================================================================

def _total_market(over: float, under: float, points: float) -> dict:
    return {
        "num_0": [
            {
                "type": "total",
                "prices": [
                    {"name": "Over", "participant_id": 1, "points": points, "price": over},
                    {"name": "Under", "participant_id": 2, "points": points, "price": under},
                ],
            }
        ]
    }


def _special(parent_id: int, name: str, unit: str, over: float, under: float,
             points: float) -> dict:
    return {
        "event_id": parent_id * 10,
        "parent_id": parent_id,
        "special_category": "Player Props",
        "special": f"{name} ({unit})(must start)",
        "special_units": unit,
        "special_markets": _total_market(over, under, points),
        "league_id": 246,
        "league_name": "MLB",
    }


def _sample_payload() -> dict:
    mains = [
        {
            "event_id": 100,
            "parent_id": None,
            "event_type": "prematch",
            "sport_id": 6,
            "league_id": 246,
            "league_name": "MLB",
            "home": "Kansas City Royals",
            "away": "Detroit Tigers",
            "starts": "2026-08-01T19:10:00Z",
        },
        {
            "event_id": 101,
            "parent_id": None,
            "event_type": "prematch",
            "sport_id": 6,
            "league_id": 300,
            "league_name": "Nippon Professional Baseball",
            "home": "Tokyo Yakult Swallows",
            "away": "Hanshin Tigers",
            "starts": "2026-08-02T09:00:00Z",
        },
    ]
    specials = [
        _special(100, "Shota Imanaga", "Strikeouts", 1.87, 1.98, 6.5),
        _special(100, "Juan Soto", "HomeRuns", 1.55, 2.40, 0.5),
        _special(100, "Vinnie Pasquantino", "TotalBases", 1.61, 2.28, 1.5),
        # NPB prop must be filtered out with its league
        _special(101, "Munetaka Murakami", "TotalBases", 1.68, 2.09, 0.5),
        # non prop-market specials are skipped
        {
            "event_id": 222,
            "parent_id": 100,
            "special_category": "Special",
            "special": "Team Total",
            "special_units": "Runs",
            "special_markets": _total_market(1.9, 1.9, 8.5),
            "league_id": 246,
            "league_name": "MLB",
        },
    ]
    return {"sport_id": 6, "sport_name": "Baseball", "events": mains + specials}


# ==================================================================
# Parsing
# ==================================================================

def test_parse_mlb_props_basic():
    props = parse_mlb_props(_sample_payload())
    assert len(props) == 3
    assert all(p.home_name == "Kansas City Royals" for p in props)
    assert all(p.away_name == "Detroit Tigers" for p in props)


def test_parse_filters_non_mlb_and_non_props():
    props = parse_mlb_props(_sample_payload())
    units = [p.unit for p in props]
    assert "TotalBases" in units
    assert len([p for p in props if p.player_name == "Munetaka Murakami"]) == 0


def test_parse_american_conversion():
    props = parse_mlb_props(_sample_payload())
    k = next(p for p in props if p.unit == "Strikeouts")
    # 1.87 -> -115, 1.98 -> -102
    assert k.over_american == -115
    assert k.under_american == -102
    assert k.line == 6.5


def test_parse_player_name_strips_suffix():
    props = parse_mlb_props(_sample_payload())
    names = {p.player_name for p in props}
    assert "Shota Imanaga" in names
    assert all("(" not in n for n in names)


def test_parse_player_name_strips_market_name_suffix():
    payload = _sample_payload()
    payload["events"].append(
        _special(100, "Walker Buehler Total Strikeouts", "Strikeouts", 1.90, 1.90, 3.5)
    )
    props = parse_mlb_props(payload)
    assert any(p.player_name == "Walker Buehler" for p in props)


def test_parse_skips_unsupported_units():
    payload = _sample_payload()
    payload["events"].append(
        _special(100, "Riley Greene", "Steals", 1.9, 1.9, 0.5)
    )
    props = parse_mlb_props(payload)
    assert len(props) == 3
    assert all(p.unit != "Steals" for p in props)


# ==================================================================
# Normalization
# ==================================================================

def test_normalize_name_strips_accents():
    assert normalize_name("José Ramírez") == "jose ramirez"
    assert normalize_name("Díaz") == "diaz"
    assert normalize_name("  Shota   Imanaga ") == "shota imanaga"


def test_normalize_team_name():
    assert normalize_team_name("Kansas City Royals") == "kansas city royals"


# ==================================================================
# Matching
# ==================================================================

def test_match_pinnacle_exact():
    props = parse_mlb_props(_sample_payload())
    lookup = build_pinnacle_lookup(props)
    pin = match_pinnacle(
        lookup,
        home_name="Kansas City Royals",
        away_name="Detroit Tigers",
        player_name="Shota Imanaga",
        market_type="pitching_strikeouts_ou",
        line=6.5,
    )
    assert pin is not None
    assert pin.unit == "Strikeouts"


def test_match_short_team_names():
    props = parse_mlb_props(_sample_payload())
    lookup = build_pinnacle_lookup(props)
    pin = match_pinnacle(
        lookup,
        home_name="Royals",
        away_name="Tigers",
        player_name="Shota Imanaga",
        market_type="pitching_strikeouts_ou",
        line=6.5,
    )
    assert pin is not None


def test_match_requires_same_line():
    props = parse_mlb_props(_sample_payload())
    lookup = build_pinnacle_lookup(props)
    pin = match_pinnacle(
        lookup,
        home_name="Kansas City Royals",
        away_name="Detroit Tigers",
        player_name="Shota Imanaga",
        market_type="pitching_strikeouts_ou",
        line=7.5,
    )
    assert pin is None


def test_match_unknown_market_returns_none():
    props = parse_mlb_props(_sample_payload())
    lookup = build_pinnacle_lookup(props)
    pin = match_pinnacle(
        lookup,
        home_name="Kansas City Royals",
        away_name="Detroit Tigers",
        player_name="Shota Imanaga",
        market_type="pitching_walks_ou",
        line=6.5,
    )
    assert pin is None


def test_unit_mapping_covers_new_markets():
    from src.pinnacle_feed import MARKET_OU_TO_UNIT
    assert MARKET_OU_TO_UNIT["pitching_strikeouts_ou"] == "Strikeouts"
    assert MARKET_OU_TO_UNIT["pitching_hits_ou"] == "HitsAllowed"
    assert MARKET_OU_TO_UNIT["pitching_earnedRuns_ou"] == "EarnedRuns"
    assert MARKET_OU_TO_UNIT["pitching_outs_ou"] == "PitchingOuts"
    assert MARKET_OU_TO_UNIT["batting_totalBases_ou"] == "TotalBases"
    assert MARKET_OU_TO_UNIT["batting_homeRuns_ou"] == "HomeRuns"


def test_unit_mapping_excludes_unverified_units():
    from src.pinnacle_feed import MARKET_OU_TO_UNIT
    assert "batting_RBI_ou" not in MARKET_OU_TO_UNIT
    assert "batting_runs_ou" not in MARKET_OU_TO_UNIT
    assert "batting_basesOnBalls_ou" not in MARKET_OU_TO_UNIT
    assert "pitching_pitchesThrown_ou" not in MARKET_OU_TO_UNIT
    assert "batting_stolenBases_ou" not in MARKET_OU_TO_UNIT


def test_parse_filters_unverified_units():
    payload = _sample_payload()
    payload["events"].append(
        _special(100, "Aaron Judge", "RBIs", 1.85, 1.95, 0.5)
    )
    payload["events"].append(
        _special(100, "Shota Imanaga", "PitchesThrown", 1.80, 2.00, 95.0)
    )
    props = parse_mlb_props(payload)
    units = {p.unit for p in props}
    assert len(props) == 3
    assert "RBIs" not in units
    assert "PitchesThrown" not in units


# ==================================================================
# Injection
# ==================================================================

def _make_group(line: float = 6.5, player: str = "Shota Imanaga",
                market_type: str = "pitching_strikeouts_ou",
                event_id: str = "ev100") -> dict:
    return {
        "over": {"draftkings": {"price": -110, "decimal_odds": 1.9091,
                                "line": line, "validation_status": "VALID"}},
        "under": {"draftkings": {"price": -110, "decimal_odds": 1.9091,
                                 "line": line, "validation_status": "VALID"}},
        "line": line,
        "player_id": "PLAYER_1_MLB",
        "player_name": player,
        "event_id": event_id,
        "market_type": market_type,
    }


def _make_event_map() -> dict:
    return {
        "ev100": {"home_name": "Kansas City Royals",
                  "away_name": "Detroit Tigers", "start_time": "2026-08-01T19:10:00Z"},
    }


def test_inject_pinnacle_reference():
    groups = {"k1": _make_group()}
    lookup = build_pinnacle_lookup(parse_mlb_props(_sample_payload()))
    n = inject_pinnacle_reference(groups, _make_event_map(), lookup)
    assert n == 1
    pinn_over = groups["k1"]["over"]["pinnacle"]
    pinn_under = groups["k1"]["under"]["pinnacle"]
    assert pinn_over["price"] == -115
    assert pinn_over["decimal_odds"] == 1.87
    assert pinn_over["line"] == 6.5
    assert pinn_under["price"] == -102
    assert pinn_under["validation_status"] == "VALID"


def test_inject_skips_line_mismatch():
    groups = {"k1": _make_group(line=7.5)}
    lookup = build_pinnacle_lookup(parse_mlb_props(_sample_payload()))
    n = inject_pinnacle_reference(groups, _make_event_map(), lookup)
    assert n == 0
    assert "pinnacle" not in groups["k1"]["over"]


def test_inject_does_not_overwrite_existing_pinnacle():
    groups = {"k1": _make_group()}
    groups["k1"]["over"]["pinnacle"] = {"price": -999, "decimal_odds": 1.0,
                                        "line": 6.5, "validation_status": "VALID"}
    lookup = build_pinnacle_lookup(parse_mlb_props(_sample_payload()))
    inject_pinnacle_reference(groups, _make_event_map(), lookup)
    assert groups["k1"]["over"]["pinnacle"]["price"] == -999


def test_inject_uncovered_market_untouched():
    groups = {"k1": _make_group(market_type="pitching_walks_ou")}
    lookup = build_pinnacle_lookup(parse_mlb_props(_sample_payload()))
    n = inject_pinnacle_reference(groups, _make_event_map(), lookup)
    assert n == 0
    assert "pinnacle" not in groups["k1"]["over"]


# ==================================================================
# Client cache behaviour
# ==================================================================

def test_client_reuses_fresh_cache(tmp_path, monkeypatch):
    """Real behavior change 2026-08-23: the cache now stores the RAW
    fixtures payload (re-parsed on every read), not pre-parsed props —
    fixing a real bug where fetching props and game-odds as two separate
    live calls would hit the same rate limiter back-to-back and the
    second would almost always be silently blocked. One raw payload per
    league now covers both. See PinnacleFeedClient._get_raw_payload."""
    client = PinnacleFeedClient(
        api_key="test-key", cache_path=tmp_path / "cache.json", ttl_seconds=300
    )
    client._save_raw_cache(tmp_path / "cache.json", _sample_payload())

    def _explode(*args, **kwargs):
        raise AssertionError("fetch should not run when cache is fresh")

    monkeypatch.setattr(client, "_fetch_raw", _explode)
    got = client.get_mlb_props(allow_fetch=True)
    assert got is not None
    assert len(got) == 3
    assert any(p.player_name == "Shota Imanaga" for p in got)


def test_client_returns_none_on_fetch_failure(tmp_path, monkeypatch):
    client = PinnacleFeedClient(
        api_key="test-key", cache_path=tmp_path / "cache.json", ttl_seconds=300
    )
    monkeypatch.setattr(client, "_fetch_raw", lambda sport_id: None)
    got = client.get_mlb_props(allow_fetch=True)
    assert got is None


def test_props_and_game_odds_share_one_raw_fetch(tmp_path, monkeypatch):
    """The actual bug fix, tested directly: get_player_props() followed
    immediately by get_game_odds() for the same league must only call
    _fetch_raw() once, not twice — otherwise the second call would hit
    the shared rate limiter and silently return nothing in real usage."""
    client = PinnacleFeedClient(
        api_key="test-key", cache_path=tmp_path / "shared_cache.json", ttl_seconds=300,
        min_interval_seconds=0.0,  # avoid bleeding into the shared module-level rate limit
    )
    call_count = {"n": 0}

    def _fake_fetch_raw(sport_id):
        call_count["n"] += 1
        return _sample_payload()

    monkeypatch.setattr(client, "_fetch_raw", _fake_fetch_raw)
    props = client.get_player_props(league="MLB", allow_fetch=True)
    games = client.get_game_odds(league="MLB", allow_fetch=True)
    assert props is not None
    assert games is not None
    assert call_count["n"] == 1


# ==================================================================
# Multi-league (added 2026-08-23) — WNBA player props
# ==================================================================

def _wnba_special(parent_id: int, name: str, unit: str, over: float, under: float,
                   points: float) -> dict:
    return {
        "event_id": parent_id * 10,
        "parent_id": parent_id,
        "special_category": "Player Props",
        "special": f"{name} Total {unit}",
        "special_units": unit,
        "special_markets": _total_market(over, under, points),
        "league_id": 999,
        "league_name": "WNBA",
    }


def _wnba_payload() -> dict:
    mains = [
        {
            "event_id": 200, "parent_id": None, "event_type": "prematch",
            "sport_id": 3, "league_id": 999, "league_name": "WNBA",
            "home": "Dallas Wings", "away": "Seattle Storm",
            "starts": "2026-08-23T23:00:00Z",
        },
        # A different real basketball league sharing sport_id 3 must be
        # filtered out, same as MLB filters NPB out of sport_id 6.
        {
            "event_id": 201, "parent_id": None, "event_type": "prematch",
            "sport_id": 3, "league_id": 500, "league_name": "Mexico - Liga Nacional de Baloncesto Profesional",
            "home": "Fuerza Regia", "away": "Halcones",
            "starts": "2026-08-23T23:00:00Z",
        },
    ]
    specials = [
        _wnba_special(200, "Alanna Smith", "Points", 1.87, 1.98, 14.5),
        _wnba_special(200, "Alanna Smith", "Threes Made", 1.90, 1.90, 1.5),
        _wnba_special(201, "Someone Else", "Points", 1.90, 1.90, 10.5),  # non-WNBA, filtered
    ]
    return {"sport_id": 3, "sport_name": "Basketball", "events": mains + specials}


def test_wnba_props_parsed_with_correct_units():
    props = parse_player_props(_wnba_payload(), league="WNBA")
    assert len(props) == 2
    units = {p.unit for p in props}
    assert units == {"Points", "Threes Made"}
    assert all(p.home_name == "Dallas Wings" for p in props)


def test_wnba_props_filters_out_other_basketball_leagues():
    props = parse_player_props(_wnba_payload(), league="WNBA")
    assert all(p.player_name != "Someone Else" for p in props)


def test_wnba_suffix_stripped_correctly():
    """WNBA's real suffix format is "Total Points"/"Total Threes Made" —
    a real, live-verified difference from MLB's mixed "Total X"/"X Y"
    forms (see cfg.PINNACLE_PROP_SUFFIXES_BY_LEAGUE)."""
    props = parse_player_props(_wnba_payload(), league="WNBA")
    assert all(p.player_name == "Alanna Smith" for p in props)


def test_mlb_parse_unaffected_by_league_param_default():
    """parse_player_props(payload) with no league arg still defaults to MLB."""
    props = parse_player_props(_sample_payload())
    assert len(props) == 3


def test_match_pinnacle_requires_matching_league_market_map():
    """A WNBA market_type must not match against an MLB-shaped lookup
    (or vice versa) — different leagues have entirely different
    market_type<->unit maps now."""
    props = parse_player_props(_wnba_payload(), league="WNBA")
    lookup = build_pinnacle_lookup(props)
    # Using league="MLB" (default) against a WNBA lookup: player_points_ou
    # isn't in MLB's map at all, so this must return None.
    pin = match_pinnacle(
        lookup, home_name="Dallas Wings", away_name="Seattle Storm",
        player_name="Alanna Smith", market_type="player_points_ou", line=14.5,
    )
    assert pin is None
    # With league="WNBA", the same lookup/inputs must match.
    pin_wnba = match_pinnacle(
        lookup, home_name="Dallas Wings", away_name="Seattle Storm",
        player_name="Alanna Smith", market_type="player_points_ou", line=14.5,
        league="WNBA",
    )
    assert pin_wnba is not None
    assert pin_wnba.unit == "Points"


# ==================================================================
# Multi-league (added 2026-08-23) — game markets (moneyline/spread/total)
# ==================================================================

def _game_period_payload(league_name: str, sport_id: int, home: str, away: str) -> dict:
    return {
        "sport_id": sport_id, "sport_name": "x",
        "events": [{
            "event_id": 300, "parent_id": None, "event_type": "prematch",
            "sport_id": sport_id, "league_id": 1, "league_name": league_name,
            "home": home, "away": away, "starts": "2026-08-23T20:00:00Z",
            "periods": {
                "num_0": {
                    "description": "Game",
                    "money_line": {"home": 1.6536, "away": 2.41, "draw": None},
                    "spreads": {
                        "1.5": {"hdp": 1.5, "home": 1.869, "away": 1.943, "max": 2000},
                        "-1.5": {"hdp": -1.5, "home": 2.02, "away": 1.775, "max": 2000},
                    },
                    "totals": {
                        "8.5": {"points": 8.5, "over": 1.909, "under": 1.909, "max": 2000},
                    },
                },
                "num_1": {"description": "Half 1", "money_line": None, "spreads": {}, "totals": {}},
            },
        }],
    }


def test_parse_game_odds_extracts_moneyline_spread_total():
    payload = _game_period_payload("MLB", 6, "Miami Marlins", "Washington Nationals")
    games = parse_game_odds(payload, league="MLB")
    market_types = {g.market_type for g in games}
    assert market_types == {"game_moneyline", "game_runline_ou", "game_total_ou"}


def test_parse_game_odds_moneyline_has_no_line():
    payload = _game_period_payload("MLB", 6, "Miami Marlins", "Washington Nationals")
    games = parse_game_odds(payload, league="MLB")
    ml = next(g for g in games if g.market_type == "game_moneyline")
    assert ml.line is None
    assert ml.home_decimal == 1.6536
    assert ml.away_decimal == 2.41


def test_parse_game_odds_spread_line_is_abs_valued():
    payload = _game_period_payload("MLB", 6, "Miami Marlins", "Washington Nationals")
    games = parse_game_odds(payload, league="MLB")
    spreads = [g for g in games if g.market_type == "game_runline_ou"]
    assert {g.line for g in spreads} == {1.5}  # both +1.5 and -1.5 hdp collapse to abs 1.5


def test_parse_game_odds_ignores_half_period():
    """Only the "Game" (num_0) period should ever be extracted — a half
    with money_line=None and empty spreads/totals must not produce
    entries."""
    payload = _game_period_payload("MLB", 6, "Miami Marlins", "Washington Nationals")
    games = parse_game_odds(payload, league="MLB")
    # 1 moneyline + 2 spread entries (hdp 1.5 and -1.5, each its own
    # entry) + 1 total = 4. None come from the "Half 1" period.
    assert len(games) == 4


def test_parse_game_odds_uses_correct_market_naming_per_league():
    """MLB uses run-line naming (game_runline_ou); NFL/WNBA use the
    generic game_spread_ou — same distinction the Odds-API game-odds
    fallback already makes."""
    nfl_payload = _game_period_payload("NFL", 5, "Tennessee Titans", "New York Jets")
    nfl_games = parse_game_odds(nfl_payload, league="NFL")
    assert any(g.market_type == "game_spread_ou" for g in nfl_games)
    assert not any(g.market_type == "game_runline_ou" for g in nfl_games)

    wnba_payload = _game_period_payload("WNBA", 3, "Dallas Wings", "Seattle Storm")
    wnba_games = parse_game_odds(wnba_payload, league="WNBA")
    assert any(g.market_type == "game_spread_ou" for g in wnba_games)


def test_parse_game_odds_empty_for_league_with_no_game_market_config():
    """A league with no PINNACLE_GAME_MARKET_TYPES_BY_LEAGUE entry
    returns an empty list rather than erroring."""
    payload = _game_period_payload("MLB", 6, "Miami Marlins", "Washington Nationals")
    games = parse_game_odds(payload, league="NOT_A_REAL_LEAGUE")
    assert games == []


def _make_game_group(market_type: str = "game_moneyline", line=None,
                      event_id: str = "ev300") -> dict:
    return {
        "over": {"draftkings": {"price": 120, "decimal_odds": 2.2, "line": line,
                                 "validation_status": "VALID"}},
        "under": {"draftkings": {"price": -140, "decimal_odds": 1.71, "line": line,
                                  "validation_status": "VALID"}},
        "line": line,
        "player_id": "GAME",
        "player_name": "Moneyline",
        "event_id": event_id,
        "market_type": market_type,
    }


def _make_game_event_map() -> dict:
    return {"ev300": {"home_name": "Miami Marlins", "away_name": "Washington Nationals",
                       "start_time": "2026-08-23T20:00:00Z"}}


def test_inject_pinnacle_game_reference_moneyline():
    payload = _game_period_payload("MLB", 6, "Miami Marlins", "Washington Nationals")
    lookup = build_pinnacle_game_lookup(parse_game_odds(payload, league="MLB"))
    groups = {"k1": _make_game_group(market_type="game_moneyline", line=None)}
    n = inject_pinnacle_game_reference(groups, _make_game_event_map(), lookup)
    assert n == 1
    # AWAY=over/HOME=under convention: over gets away_decimal, under gets home_decimal.
    assert groups["k1"]["over"]["pinnacle"]["decimal_odds"] == 2.41
    assert groups["k1"]["under"]["pinnacle"]["decimal_odds"] == 1.6536


def test_inject_pinnacle_game_reference_total():
    payload = _game_period_payload("MLB", 6, "Miami Marlins", "Washington Nationals")
    lookup = build_pinnacle_game_lookup(parse_game_odds(payload, league="MLB"))
    groups = {"k1": _make_game_group(market_type="game_total_ou", line=8.5)}
    n = inject_pinnacle_game_reference(groups, _make_game_event_map(), lookup)
    assert n == 1
    assert groups["k1"]["over"]["pinnacle"]["decimal_odds"] == 1.909
    assert groups["k1"]["under"]["pinnacle"]["decimal_odds"] == 1.909


def test_inject_pinnacle_game_reference_skips_player_props():
    """A real player-prop group (player_id != "GAME") must never be
    touched by the game-odds injector, even if it happens to share an
    event_id with a game market."""
    payload = _game_period_payload("MLB", 6, "Miami Marlins", "Washington Nationals")
    lookup = build_pinnacle_game_lookup(parse_game_odds(payload, league="MLB"))
    groups = {"k1": _make_group(event_id="ev300")}  # a real player-prop group, player_id="PLAYER_1_MLB"
    n = inject_pinnacle_game_reference(groups, _make_game_event_map(), lookup)
    assert n == 0
    assert "pinnacle" not in groups["k1"]["over"]


def test_inject_pinnacle_game_reference_no_match_leaves_group_untouched():
    payload = _game_period_payload("MLB", 6, "Miami Marlins", "Washington Nationals")
    lookup = build_pinnacle_game_lookup(parse_game_odds(payload, league="MLB"))
    groups = {"k1": _make_game_group(market_type="game_total_ou", line=99.5)}  # no such line
    n = inject_pinnacle_game_reference(groups, _make_game_event_map(), lookup)
    assert n == 0
