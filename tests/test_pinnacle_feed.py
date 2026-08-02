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
    build_pinnacle_lookup,
    inject_pinnacle_reference,
    match_pinnacle,
    normalize_name,
    normalize_team_name,
    parse_mlb_props,
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
    assert MARKET_OU_TO_UNIT["batting_RBI_ou"] == "RBIs"
    assert MARKET_OU_TO_UNIT["batting_runs_ou"] == "Runs"
    assert MARKET_OU_TO_UNIT["batting_basesOnBalls_ou"] == "Walks"
    assert MARKET_OU_TO_UNIT["pitching_pitchesThrown_ou"] == "PitchesThrown"
    assert MARKET_OU_TO_UNIT["batting_stolenBases_ou"] == "StolenBases"


def test_parse_accepts_new_units():
    payload = _sample_payload()
    payload["events"].append(
        _special(100, "Aaron Judge", "RBIs", 1.85, 1.95, 0.5)
    )
    payload["events"].append(
        _special(100, "Shota Imanaga", "PitchesThrown", 1.80, 2.00, 95.0)
    )
    props = parse_mlb_props(payload)
    units = {p.unit for p in props}
    assert "RBIs" in units
    assert "PitchesThrown" in units


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
    client = PinnacleFeedClient(
        api_key="test-key", cache_path=tmp_path / "cache.json", ttl_seconds=300
    )
    props = [PinnacleProp(
        home_name="Kansas City Royals", away_name="Detroit Tigers",
        player_name="Shota Imanaga", unit="Strikeouts", line=6.5,
        over_decimal=1.87, under_decimal=1.98,
        over_american=-115, under_american=-102,
    )]
    client._save_cache([p.__dict__ for p in props])

    def _explode(*args, **kwargs):
        raise AssertionError("fetch should not run when cache is fresh")

    monkeypatch.setattr(client, "_fetch_raw", _explode)
    got = client.get_mlb_props(allow_fetch=True)
    assert got is not None
    assert len(got) == 1
    assert got[0].player_name == "Shota Imanaga"


def test_client_returns_none_on_fetch_failure(tmp_path, monkeypatch):
    client = PinnacleFeedClient(
        api_key="test-key", cache_path=tmp_path / "cache.json", ttl_seconds=300
    )
    monkeypatch.setattr(client, "_fetch_raw", lambda: None)
    got = client.get_mlb_props(allow_fetch=True)
    assert got is None
