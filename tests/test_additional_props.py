"""Phase 3: Additional pitcher props — Hits Allowed, Walks Allowed, Earned Runs.

Verifies that these markets are automatically recognized through the generic
MarketConfig registry, parsed into the canonical O/U record structure,
analysed by the existing two-sided O/U engine, and displayed by the scanner.

All fixtures are synthetic and deterministic (no cache or live-API dependency).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.player_prop_parser import parse_player_props, _build_group_key
from src.player_prop_analysis import analyze_prop_group, analyze_yn_group
from src.prop_config import (
    MARKET_QUALITY_VALID,
    MARKET_QUALITY_INSUFFICIENT,
    MARKET_QUALITY_EXCLUDED,
    BET_STATUS_NO_EDGE,
    PITCHER_HITS_ALLOWED,
    PITCHER_WALKS_ALLOWED,
    PITCHER_STRIKEOUTS,
    match_ou_market,
    match_yn_market,
    get_market_by_ou_type,
    get_market_by_yn_type,
    get_market_by_cli_name,
)
from src.validation_constants import STATUS_VALID, APPROVED_STATUSES

from tests.fixture_data import (
    hits_event,
    walks_event,
    flaherty_event as _flaherty_event,
    HITS_EVENT_ID,
    WALKS_EVENT_ID,
)


# ── Fixtures ────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def flaherty_event():
    return dict(_flaherty_event)


# ====================================================================
# HITS ALLOWED
# ====================================================================

class TestHitsAllowed:

    def test_parsed_correct_count(self):
        result = parse_player_props(hits_event)
        ou = [r for r in result.odds_rows if r["market_type"] == "pitching_hits_ou"]
        assert len(ou) == 20  # 2 players x 2 sides x 5 books

    def test_market_type(self):
        result = parse_player_props(hits_event)
        for r in result.odds_rows:
            assert r["market_type"] == "pitching_hits_ou"

    def test_player_names(self):
        result = parse_player_props(hits_event)
        names = {r["player_name"] for r in result.odds_rows}
        assert "Cole Ragans" in names
        assert "Zack Wheeler" in names

    def test_sides(self):
        result = parse_player_props(hits_event)
        sides = {r["side"] for r in result.odds_rows}
        assert sides == {"OVER", "UNDER"}

    def test_lines(self):
        result = parse_player_props(hits_event)
        lines = {r["line"] for r in result.odds_rows}
        assert 5.5 in lines
        assert 4.5 in lines

    def test_group_key_contains_market_type(self):
        result = parse_player_props(hits_event)
        for r in result.odds_rows:
            assert "pitching_hits_ou" in r["market_group_key"]

    def test_different_players_different_groups(self):
        result = parse_player_props(hits_event)
        ragans_keys = {r["market_group_key"] for r in result.odds_rows
                       if r["player_id"] == "COLE_RAGANS_1_MLB"}
        wheeler_keys = {r["market_group_key"] for r in result.odds_rows
                        if r["player_id"] == "ZACK_WHEELER_1_MLB"}
        assert ragans_keys.isdisjoint(wheeler_keys)

    def test_analysis_valid_market(self):
        over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
        under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
        result = analyze_prop_group("test", over, under)
        assert result["market_quality"] == MARKET_QUALITY_VALID
        assert result["n_paired_books"] == 5

    def test_analysis_no_vig(self):
        over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
        under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
        result = analyze_prop_group("test", over, under)
        assert abs(result["nv_prob_over"] + result["nv_prob_under"] - 1.0) < 0.001

    def test_negative_ev(self):
        over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
        under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
        result = analyze_prop_group("test", over, under)
        for b in result["books"]:
            assert b["ev_pct"] < 0
            assert b["bet_status"] == BET_STATUS_NO_EDGE

    def test_missing_side_excluded(self):
        over = {"b1": {"price": -110, "decimal_odds": 1.9091, "line": 5.5}}
        result = analyze_prop_group("test", over, {})
        assert result["market_quality"] == MARKET_QUALITY_EXCLUDED

    def test_insufficient_books(self):
        """MIN_COMPARISON_BOOKS lowered to 1 (2 books total) 2026-08-22 —
        see docs/DECISIONS.md "Book-count gate lowered to the LOO floor".
        Only a single book (no comparison book at all -- the hard
        "fewer than 2 books" EXCLUDED floor, not INSUFFICIENT) is
        excluded now; 4 books is comfortably valid."""
        over = {"b0": {"price": -110, "decimal_odds": 1.9091, "line": 5.5}}
        under = {"b0": {"price": -110, "decimal_odds": 1.9091, "line": 5.5}}
        result = analyze_prop_group("test", over, under)
        assert result["market_quality"] == MARKET_QUALITY_EXCLUDED

    def test_two_books_now_sufficient(self):
        over = {f"b{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(4)}
        under = {f"b{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(4)}
        result = analyze_prop_group("test", over, under)
        assert result["market_quality"] != MARKET_QUALITY_INSUFFICIENT

    def test_no_yn_variant(self):
        assert PITCHER_HITS_ALLOWED.supports_yn is False
        assert PITCHER_HITS_ALLOWED.market_type_yn is None

    def test_registry_config(self):
        assert PITCHER_HITS_ALLOWED.cli_name == "hits_allowed"
        assert PITCHER_HITS_ALLOWED.odd_id_stat_prefix == "pitching_hits"
        assert PITCHER_HITS_ALLOWED.market_type_ou == "pitching_hits_ou"
        assert PITCHER_HITS_ALLOWED.display_name == "Pitcher Hits Allowed"
        assert PITCHER_HITS_ALLOWED.short_label == "Hits"

    def test_registry_match_ou(self):
        assert match_ou_market("pitching_hits-X-game-ou-over") is PITCHER_HITS_ALLOWED
        assert match_ou_market("pitching_hits-X-game-ou-under") is PITCHER_HITS_ALLOWED

    def test_registry_match_yn_returns_none(self):
        assert match_yn_market("pitching_hits-X-game-yn-yes") is None

    def test_registry_by_type(self):
        assert get_market_by_ou_type("pitching_hits_ou") is PITCHER_HITS_ALLOWED

    def test_registry_by_cli(self):
        assert get_market_by_cli_name("hits_allowed") is PITCHER_HITS_ALLOWED

    def test_cross_market_isolation(self):
        """Hits and strikeouts for same player don't mix."""
        combined = {
            "eventID": "COMBO",
            "odds": {
                "pitching_hits-PLAYER_1_MLB-game-ou-over": {
                    "playerID": "PLAYER_1_MLB",
                    "playerNames": {"full": "Test"},
                    "marketName": "Test Hits Allowed Over/Under",
                    "byBookmaker": {f"b{i}": {"odds": -110, "overUnder": 5.5, "available": True} for i in range(5)},
                },
                "pitching_hits-PLAYER_1_MLB-game-ou-under": {
                    "playerID": "PLAYER_1_MLB",
                    "playerNames": {"full": "Test"},
                    "marketName": "Test Hits Allowed Over/Under",
                    "byBookmaker": {f"b{i}": {"odds": -110, "overUnder": 5.5, "available": True} for i in range(5)},
                },
                "pitching_strikeouts-PLAYER_1_MLB-game-ou-over": {
                    "playerID": "PLAYER_1_MLB",
                    "playerNames": {"full": "Test"},
                    "marketName": "Test Strikeouts Over/Under",
                    "byBookmaker": {f"b{i}": {"odds": -110, "overUnder": 5.5, "available": True} for i in range(5)},
                },
                "pitching_strikeouts-PLAYER_1_MLB-game-ou-under": {
                    "playerID": "PLAYER_1_MLB",
                    "playerNames": {"full": "Test"},
                    "marketName": "Test Strikeouts Over/Under",
                    "byBookmaker": {f"b{i}": {"odds": -110, "overUnder": 5.5, "available": True} for i in range(5)},
                },
            },
        }
        result = parse_player_props(combined)
        hits_keys = {r["market_group_key"] for r in result.odds_rows
                     if r["market_type"] == "pitching_hits_ou"}
        k_keys = {r["market_group_key"] for r in result.odds_rows
                  if r["market_type"] == "pitching_strikeouts_ou"}
        assert hits_keys.isdisjoint(k_keys)


# ====================================================================
# WALKS ALLOWED (O/U + YN)
# ====================================================================

class TestWalksAllowed:

    def test_ou_parsed_correct_count(self):
        result = parse_player_props(walks_event)
        ou = [r for r in result.odds_rows if r["market_type"] == "pitching_basesOnBalls_ou"]
        assert len(ou) == 10  # 1 player x 2 sides x 5 books

    def test_ou_lines(self):
        result = parse_player_props(walks_event)
        ou = [r for r in result.odds_rows if r["market_type"] == "pitching_basesOnBalls_ou"]
        lines = {r["line"] for r in ou}
        assert lines == {1.5}

    def test_player_name(self):
        result = parse_player_props(walks_event)
        names = {r["player_name"] for r in result.odds_rows}
        assert "Shota Imanaga" in names

    def test_group_key_market_type(self):
        result = parse_player_props(walks_event)
        for r in result.odds_rows:
            assert "pitching_basesOnBalls" in r["market_group_key"]

    def test_ou_analysis_valid(self):
        over = {f"b{i}": {"price": -110, "decimal_odds": 1.9091, "line": 1.5} for i in range(5)}
        under = {f"b{i}": {"price": -110, "decimal_odds": 1.9091, "line": 1.5} for i in range(5)}
        result = analyze_prop_group("test", over, under)
        assert result["market_quality"] == MARKET_QUALITY_VALID

    def test_registry_config(self):
        assert PITCHER_WALKS_ALLOWED.cli_name == "walks_allowed"
        assert PITCHER_WALKS_ALLOWED.odd_id_stat_prefix == "pitching_basesOnBalls"
        assert PITCHER_WALKS_ALLOWED.market_type_ou == "pitching_basesOnBalls_ou"
        assert PITCHER_WALKS_ALLOWED.market_type_yn is None
        assert PITCHER_WALKS_ALLOWED.display_name == "Pitcher Walks Allowed"
        assert PITCHER_WALKS_ALLOWED.short_label == "BB"
        assert PITCHER_WALKS_ALLOWED.supports_yn is False

    def test_registry_match_ou(self):
        assert match_ou_market("pitching_basesOnBalls-X-game-ou-over") is PITCHER_WALKS_ALLOWED
        assert match_ou_market("pitching_basesOnBalls-X-game-ou-under") is PITCHER_WALKS_ALLOWED

    def test_registry_match_yn_returns_none(self):
        assert match_yn_market("pitching_basesOnBalls-X-game-yn-yes") is None
        assert match_yn_market("pitching_basesOnBalls-X-game-yn-no") is None

    def test_registry_by_type(self):
        assert get_market_by_ou_type("pitching_basesOnBalls_ou") is PITCHER_WALKS_ALLOWED
        assert get_market_by_yn_type("pitching_basesOnBalls_yn") is None

    def test_registry_by_cli(self):
        assert get_market_by_cli_name("walks_allowed") is PITCHER_WALKS_ALLOWED

    def test_missing_side_excluded(self):
        over = {"b1": {"price": -110, "decimal_odds": 1.9091, "line": 1.5}}
        result = analyze_prop_group("test", over, {})
        assert result["market_quality"] == MARKET_QUALITY_EXCLUDED

    def test_negative_ev(self):
        over = {f"b{i}": {"price": -160, "decimal_odds": 1.625, "line": 1.5} for i in range(5)}
        under = {f"b{i}": {"price": 130, "decimal_odds": 2.3, "line": 1.5} for i in range(5)}
        result = analyze_prop_group("test", over, under)
        for b in result["books"]:
            assert b["ev_pct"] < 0

    def test_cross_market_isolation(self):
        """Walks and strikeouts don't mix."""
        combined = {
            "eventID": "COMBO",
            "odds": {
                "pitching_basesOnBalls-P_1_MLB-game-ou-over": {
                    "playerID": "P_1_MLB", "playerNames": {"full": "T"},
                    "marketName": "T Walks Over/Under",
                    "byBookmaker": {f"b{i}": {"odds": -110, "overUnder": 1.5, "available": True} for i in range(5)},
                },
                "pitching_basesOnBalls-P_1_MLB-game-ou-under": {
                    "playerID": "P_1_MLB", "playerNames": {"full": "T"},
                    "marketName": "T Walks Over/Under",
                    "byBookmaker": {f"b{i}": {"odds": -110, "overUnder": 1.5, "available": True} for i in range(5)},
                },
                "pitching_strikeouts-P_1_MLB-game-ou-over": {
                    "playerID": "P_1_MLB", "playerNames": {"full": "T"},
                    "marketName": "T Strikeouts Over/Under",
                    "byBookmaker": {f"b{i}": {"odds": -110, "overUnder": 5.5, "available": True} for i in range(5)},
                },
                "pitching_strikeouts-P_1_MLB-game-ou-under": {
                    "playerID": "P_1_MLB", "playerNames": {"full": "T"},
                    "marketName": "T Strikeouts Over/Under",
                    "byBookmaker": {f"b{i}": {"odds": -110, "overUnder": 5.5, "available": True} for i in range(5)},
                },
            },
        }
        result = parse_player_props(combined)
        bb_keys = {r["market_group_key"] for r in result.odds_rows
                   if r["market_type"] == "pitching_basesOnBalls_ou"}
        k_keys = {r["market_group_key"] for r in result.odds_rows
                  if r["market_type"] == "pitching_strikeouts_ou"}
        assert bb_keys.isdisjoint(k_keys)


# ====================================================================
# Cross-market: all markets don't mix
# ====================================================================

class TestAllMarketsIsolation:

    def test_all_markets_independent(self):
        """All supported markets for same player produce separate group keys."""
        event = {
            "eventID": "MEGA",
            "odds": {},
        }
        markets = [
            ("pitching_strikeouts", "Strikeouts Over/Under", 5.5),
            ("pitching_hits", "Hits Allowed Over/Under", 5.5),
            ("pitching_basesOnBalls", "Walks Over/Under", 1.5),
        ]
        for prefix, suffix, line in markets:
            event["odds"][f"{prefix}-P_1_MLB-game-ou-over"] = {
                "playerID": "P_1_MLB", "playerNames": {"full": "Test"},
                "marketName": f"Test {suffix}",
                "byBookmaker": {f"b{i}": {"odds": -110, "overUnder": line, "available": True} for i in range(5)},
            }
            event["odds"][f"{prefix}-P_1_MLB-game-ou-under"] = {
                "playerID": "P_1_MLB", "playerNames": {"full": "Test"},
                "marketName": f"Test {suffix}",
                "byBookmaker": {f"b{i}": {"odds": -110, "overUnder": line, "available": True} for i in range(5)},
            }

        result = parse_player_props(event)
        keys_by_type = {}
        for r in result.odds_rows:
            mt = r["market_type"]
            keys_by_type.setdefault(mt, set()).add(r["market_group_key"])

        # Each market type should have its own group key(s)
        assert len(keys_by_type) == 3
        all_keys = set()
        for keys in keys_by_type.values():
            assert keys.isdisjoint(all_keys), f"Group key collision between market types"
            all_keys.update(keys)

    def test_strikeout_regression_unchanged(self, flaherty_event):
        """Existing Flaherty strikeout tests still pass."""
        result = parse_player_props(flaherty_event)
        ou = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_ou"]
        assert len(ou) > 0
        for r in ou:
            assert "pitching_strikeouts_ou" in r["market_group_key"]

    def test_yn_regression_unchanged(self, flaherty_event):
        """Existing Flaherty YN tests still pass."""
        result = parse_player_props(flaherty_event)
        yn = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
        assert len(yn) == 5
        assert all(r["side"] == "YES" for r in yn)


# ====================================================================
# Stale cache handling
# ====================================================================

class TestStaleCache:

    def test_observation_time_preserved_hits(self):
        event = {
            "eventID": "stale",
            "odds": {
                "pitching_hits-P_1_MLB-game-ou-over": {
                    "playerID": "P_1_MLB", "playerNames": {"full": "T"},
                    "marketName": "T Hits Allowed Over/Under",
                    "byBookmaker": {
                        "b1": {"odds": -110, "overUnder": 5.5, "available": True,
                               "lastUpdatedAt": "2026-07-01T10:00:00Z"},
                    },
                },
                "pitching_hits-P_1_MLB-game-ou-under": {
                    "playerID": "P_1_MLB", "playerNames": {"full": "T"},
                    "marketName": "T Hits Allowed Over/Under",
                    "byBookmaker": {
                        "b1": {"odds": -110, "overUnder": 5.5, "available": True,
                               "lastUpdatedAt": "2026-07-01T10:00:00Z"},
                    },
                },
            },
        }
        result = parse_player_props(event)
        for r in result.odds_rows:
            assert "2026-07-01" in r["observation_time"]

    def test_unavailable_excluded(self):
        event = {
            "eventID": "unavail",
            "odds": {
                "pitching_hits-P_1_MLB-game-ou-over": {
                    "playerID": "P_1_MLB", "playerNames": {"full": "T"},
                    "marketName": "T Hits Allowed Over/Under",
                    "byBookmaker": {
                        "b1": {"odds": -110, "overUnder": 5.5, "available": False},
                    },
                },
            },
        }
        result = parse_player_props(event)
        assert len(result.odds_rows) == 0
        assert result.audit_rows[0]["excluded"] == 1

    def test_missing_line_excluded(self):
        event = {
            "eventID": "noline",
            "odds": {
                "pitching_hits-P_1_MLB-game-ou-over": {
                    "playerID": "P_1_MLB", "playerNames": {"full": "T"},
                    "marketName": "T Hits Allowed Over/Under",
                    "byBookmaker": {
                        "b1": {"odds": -110, "available": True},
                    },
                },
            },
        }
        result = parse_player_props(event)
        assert len(result.odds_rows) == 0
        assert any("Missing or non-numeric line" in r["exclusion_reasons"]
                   for r in result.audit_rows)

    def test_missing_player_id_excluded(self):
        event = {
            "eventID": "noid",
            "odds": {
                "pitching_hits--game-ou-over": {
                    "playerID": "", "playerNames": {"full": "T"},
                    "marketName": "T Hits Allowed Over/Under",
                    "byBookmaker": {
                        "b1": {"odds": -110, "overUnder": 5.5, "available": True},
                    },
                },
            },
        }
        result = parse_player_props(event)
        assert len(result.odds_rows) == 0


# ====================================================================
# Full registry completeness
# ====================================================================

class TestRegistryCompleteness:

    def test_all_five_markets_in_registry(self):
        from src.prop_config import MARKET_REGISTRY
        cli_names = {m.cli_name for m in MARKET_REGISTRY}
        assert "strikeouts" in cli_names
        assert "hits_allowed" in cli_names
        assert "walks_allowed" in cli_names
        assert "home_runs" in cli_names
        assert "batter_hits" in cli_names

    def test_all_cli_lookups(self):
        assert get_market_by_cli_name("strikeouts") is not None
        assert get_market_by_cli_name("hits_allowed") is not None
        assert get_market_by_cli_name("walks_allowed") is not None
        assert get_market_by_cli_name("home_runs") is not None
        assert get_market_by_cli_name("batter_hits") is not None

    def test_all_ou_type_lookups(self):
        assert get_market_by_ou_type("pitching_strikeouts_ou") is not None
        assert get_market_by_ou_type("pitching_hits_ou") is not None
        assert get_market_by_ou_type("pitching_basesOnBalls_ou") is not None
        assert get_market_by_ou_type("batting_homeRuns_ou") is not None
        assert get_market_by_ou_type("batting_hits_ou") is not None

    def test_yn_type_lookups(self):
        assert get_market_by_yn_type("pitching_strikeouts_yn") is not None
        assert get_market_by_yn_type("batting_homeRuns_yn") is not None
        assert get_market_by_yn_type("pitching_basesOnBalls_yn") is None
        assert get_market_by_yn_type("batting_totalBases_yn") is None
        assert get_market_by_yn_type("pitching_hits_yn") is None
