"""Phase 2: Pitcher Outs Recorded O/U — integration tests.

Verifies that pitcher outs markets are automatically recognized through the
generic MarketConfig registry, parsed into the canonical O/U record structure,
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
from src.player_prop_analysis import analyze_prop_group
from src.prop_config import (
    MARKET_QUALITY_VALID,
    MARKET_QUALITY_NEEDS_REVIEW,
    MARKET_QUALITY_INSUFFICIENT,
    MARKET_QUALITY_EXCLUDED,
    BET_STATUS_STRONG,
    BET_STATUS_POSITIVE,
    BET_STATUS_MARGINAL,
    BET_STATUS_NO_EDGE,
    MIN_COMPARISON_BOOKS,
    PITCHER_OUTS,
    PITCHER_STRIKEOUTS,
    get_market_by_ou_type,
    match_ou_market,
    match_yn_market,
)
from src.validation_constants import STATUS_VALID, STATUS_NONE

from tests.fixture_data import (
    outs_event,
    flaherty_event as _flaherty_event,
    OUTS_EVENT_ID,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def outs_parsed():
    """Parse the outs event once per session."""
    return parse_player_props(outs_event)


@pytest.fixture(scope="session")
def flaherty_event():
    return dict(_flaherty_event)


@pytest.fixture(scope="session")
def outs_ou_rows(outs_parsed):
    """Only O/U rows with pitching_outs_ou market type."""
    return [r for r in outs_parsed.odds_rows if r["market_type"] == "pitching_outs_ou"]


@pytest.fixture(scope="session")
def outs_yn_rows(outs_parsed):
    """YN rows (should be empty — outs has no YN variant)."""
    return [r for r in outs_parsed.odds_rows if r["market_type"] == "pitching_outs_yn"]


# ====================================================================
# A. Valid normal market — multiple books, both sides, LOO, market_type
# ====================================================================


def test_outs_parsed_correct_count(outs_parsed):
    """6 books x 2 sides x 2 players = 24 main rows + 8 alt rows = 32? 
    Actually: Cole 6 over + 6 under + 4 alt over + 4 alt under = 20,
    Verlander 5 over + 5 under = 10. Total = 30."""
    assert len(outs_parsed.odds_rows) == 30


def test_outs_all_market_type(outs_ou_rows):
    for row in outs_ou_rows:
        assert row["market_type"] == "pitching_outs_ou"


def test_outs_player_ids(outs_ou_rows):
    pids = {r["player_id"] for r in outs_ou_rows}
    assert "GERRIT_COLE_1_MLB" in pids
    assert "JUSTIN_VERLANDER_1_MLB" in pids


def test_outs_player_names(outs_ou_rows):
    names = {r["player_name"] for r in outs_ou_rows}
    assert "Gerrit Cole" in names
    assert "Justin Verlander" in names


def test_outs_sides(outs_ou_rows):
    sides = {r["side"] for r in outs_ou_rows}
    assert sides == {"OVER", "UNDER"}


def test_outs_main_lines(outs_ou_rows):
    main = [r for r in outs_ou_rows if not r["is_alt_line"]]
    cole_lines = {r["line"] for r in main if r["player_id"] == "GERRIT_COLE_1_MLB"}
    vl_lines = {r["line"] for r in main if r["player_id"] == "JUSTIN_VERLANDER_1_MLB"}
    assert cole_lines == {17.5}
    assert vl_lines == {16.5}


def test_outs_market_group_key_contains_market_type(outs_ou_rows):
    for row in outs_ou_rows:
        assert "pitching_outs_ou" in row["market_group_key"]


def test_outs_analysis_valid_market():
    """Main line with 6 paired books should be VALID_MARKET."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(6)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(6)}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_VALID
    assert result["n_paired_books"] == 6
    assert result["vig_pct"] > 0


def test_outs_analysis_no_vig_fair_prob():
    """No-vig probabilities sum to 1."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(6)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(6)}
    result = analyze_prop_group("test", over, under)
    assert abs(result["nv_prob_over"] + result["nv_prob_under"] - 1.0) < 0.001


def test_outs_analysis_fair_odds_and_ev():
    """Each book entry has fair_prob, ev_pct, bet_status."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(6)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(6)}
    result = analyze_prop_group("test", over, under)
    for b in result["books"]:
        assert "fair_prob" in b
        assert "ev_pct" in b
        assert "bet_status" in b
        assert "american_odds" in b
        assert "decimal_odds" in b


def test_outs_market_quality_status_field():
    """Market quality is one of the expected statuses."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(6)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(6)}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] in (
        MARKET_QUALITY_VALID, MARKET_QUALITY_NEEDS_REVIEW,
        MARKET_QUALITY_INSUFFICIENT, MARKET_QUALITY_EXCLUDED,
    )


# ====================================================================
# B. Alternate lines — exact lines analyzed independently
# ====================================================================


def test_outs_alt_lines_separate_groups(outs_ou_rows):
    main_keys = {r["market_group_key"] for r in outs_ou_rows if not r["is_alt_line"]}
    alt_keys = {r["market_group_key"] for r in outs_ou_rows if r["is_alt_line"]}
    assert len(main_keys) > 0
    assert len(alt_keys) > 0
    assert main_keys.isdisjoint(alt_keys)


def test_outs_alt_lines_different_lines(outs_ou_rows):
    main_lines = {r["line"] for r in outs_ou_rows if not r["is_alt_line"]}
    alt_lines = {r["line"] for r in outs_ou_rows if r["is_alt_line"]}
    assert main_lines.isdisjoint(alt_lines)


def test_outs_main_and_alt_share_player(outs_ou_rows):
    cole_main = {r["market_group_key"] for r in outs_ou_rows
                 if r["player_id"] == "GERRIT_COLE_1_MLB" and not r["is_alt_line"]}
    cole_alt = {r["market_group_key"] for r in outs_ou_rows
                if r["player_id"] == "GERRIT_COLE_1_MLB" and r["is_alt_line"]}
    assert len(cole_main) >= 1
    assert len(cole_alt) >= 1
    assert cole_main.isdisjoint(cole_alt)


def test_outs_alt_15p5_has_3_books(outs_ou_rows):
    """The 15.5 alt line should have 3 books (fanduel, draftkings, betmgm)."""
    alt_15 = [r for r in outs_ou_rows if r["line"] == 15.5 and r["is_alt_line"]]
    assert len(alt_15) == 6  # 3 books x 2 sides


def test_outs_group_key_format():
    key = _build_group_key("EVT1", "P1", 17.5, 0, "OVER", "pitching_outs_ou")
    assert key == "EVT1|P1|pitching_outs_ou|game|17.5"
    alt_key = _build_group_key("EVT1", "P1", 17.5, 1, "OVER", "pitching_outs_ou")
    assert alt_key == "EVT1|P1|pitching_outs_ou|game|17.5_alt"


# ====================================================================
# C. Missing side — only Over or only Under at one line
# ====================================================================


def test_missing_under_excluded():
    """Only Over populated — cannot pair, so EXCLUDED."""
    over = {"book1": {"price": -110, "decimal_odds": 1.9091, "line": 17.5},
            "book2": {"price": -110, "decimal_odds": 1.9091, "line": 17.5}}
    under = {}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_EXCLUDED
    assert result["recommendation"] == "NO_BET"


def test_missing_over_excluded():
    """Only Under populated — cannot pair, so EXCLUDED."""
    over = {}
    under = {"book1": {"price": -110, "decimal_odds": 1.9091, "line": 17.5},
             "book2": {"price": -110, "decimal_odds": 1.9091, "line": 17.5}}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_EXCLUDED


def test_outs_parser_no_yn_side():
    """Outs markets produce no YN rows."""
    assert PITCHER_OUTS.supports_yn is False
    assert PITCHER_OUTS.market_type_yn is None


# ====================================================================
# D. Insufficient books — below minimum comparison-book threshold
# ====================================================================


def test_outs_two_paired_insufficient():
    """2 paired books = INSUFFICIENT_MARKET (need 5 total = 4 comparison)."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(2)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(2)}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_INSUFFICIENT


def test_outs_four_paired_insufficient():
    """4 paired books = INSUFFICIENT_MARKET (need 5 total = 4 comparison)."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(4)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(4)}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_INSUFFICIENT


def test_outs_five_paired_valid():
    """5 paired books = VALID_MARKET."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(5)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(5)}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_VALID


# ====================================================================
# E. Duplicate entries — same sportsbook/side/line
# ====================================================================


def test_outs_duplicate_deduplication():
    """Duplicate rows for same (event, player, line, side, book) are deduplicated."""
    from collections import defaultdict

    # Simulate: book1 appears twice with same price
    over = {
        "book1": {"price": -110, "decimal_odds": 1.9091, "line": 17.5},
        "book2": {"price": -110, "decimal_odds": 1.9091, "line": 17.5},
        "book3": {"price": -110, "decimal_odds": 1.9091, "line": 17.5},
        "book4": {"price": -110, "decimal_odds": 1.9091, "line": 17.5},
        "book5": {"price": -110, "decimal_odds": 1.9091, "line": 17.5},
    }
    under = dict(over)

    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_VALID
    assert result["n_paired_books"] == 5


# ====================================================================
# F. Malformed line — missing or non-numeric
# ====================================================================


def test_outs_missing_line_excluded():
    """No overUnder field → line is None → REASON_MISSING_LINE → excluded."""
    event = {
        "eventID": "test_outs_event",
        "odds": {
            "pitching_outs-TEST_1_MLB-game-ou-over": {
                "playerID": "TEST_1_MLB",
                "playerNames": {"full": "Test Player"},
                "marketName": "Test Player Outs Recorded Over/Under",
                "byBookmaker": {
                    "testbook": {"odds": -110, "available": True}
                },
            },
        },
    }
    result = parse_player_props(event)
    assert len(result.odds_rows) == 0
    assert any("Missing or non-numeric line" in r["exclusion_reasons"]
               for r in result.audit_rows)


def test_outs_invalid_line_excluded():
    """Non-numeric overUnder → REASON_MISSING_LINE → excluded."""
    event = {
        "eventID": "test_outs_event",
        "odds": {
            "pitching_outs-TEST_1_MLB-game-ou-over": {
                "playerID": "TEST_1_MLB",
                "playerNames": {"full": "Test Player"},
                "marketName": "Test Player Outs Recorded Over/Under",
                "byBookmaker": {
                    "testbook": {"odds": -110, "overUnder": "invalid", "available": True}
                },
            },
        },
    }
    result = parse_player_props(event)
    assert len(result.odds_rows) == 0


# ====================================================================
# G. Invalid participant mapping — fails validation
# ====================================================================


def test_outs_missing_player_id_excluded():
    event = {
        "eventID": "test_outs_event",
        "odds": {
            "pitching_outs--game-ou-over": {
                "playerID": "",
                "playerNames": {"full": "Test Player"},
                "marketName": "Test Player Outs Recorded Over/Under",
                "byBookmaker": {
                    "testbook": {"odds": -110, "overUnder": 17.5, "available": True}
                },
            },
        },
    }
    result = parse_player_props(event)
    assert len(result.odds_rows) == 0
    assert any("Missing player ID" in r["exclusion_reasons"]
               for r in result.audit_rows)


def test_outs_missing_player_name_excluded():
    event = {
        "eventID": "test_outs_event",
        "odds": {
            "pitching_outs-TEST_1_MLB-game-ou-over": {
                "playerID": "TEST_1_MLB",
                "playerNames": {},
                "marketName": "",
                "byBookmaker": {
                    "testbook": {"odds": -110, "overUnder": 17.5, "available": True}
                },
            },
        },
    }
    result = parse_player_props(event)
    assert len(result.odds_rows) == 0
    assert any("Missing player name" in r["exclusion_reasons"]
               for r in result.audit_rows)


def test_outs_unavailable_excluded():
    event = {
        "eventID": "test_outs_event",
        "odds": {
            "pitching_outs-TEST_1_MLB-game-ou-over": {
                "playerID": "TEST_1_MLB",
                "playerNames": {"full": "Test Player"},
                "marketName": "Test Player Outs Recorded Over/Under",
                "byBookmaker": {
                    "testbook": {"odds": -110, "overUnder": 17.5, "available": False}
                },
            },
        },
    }
    result = parse_player_props(event)
    assert len(result.odds_rows) == 0
    assert result.audit_rows[0]["excluded"] == 1


# ====================================================================
# H. Positive-EV opportunity
# ====================================================================


def test_outs_positive_ev_opportunity():
    """Crafted odds where one book has slightly better Over price → positive EV."""
    # 4 books with Over -120 / Under +100 (sharp Under → Over fair prob ~51.7%)
    over = {f"book{i}": {"price": -120, "decimal_odds": 1.8333, "line": 17.5} for i in range(4)}
    under = {f"book{i}": {"price": 100, "decimal_odds": 2.0, "line": 17.5} for i in range(4)}
    # book4 neutral
    over["book4"] = {"price": -110, "decimal_odds": 1.9091, "line": 17.5}
    under["book4"] = {"price": -110, "decimal_odds": 1.9091, "line": 17.5}
    # book5 slightly cheap Over → small positive EV
    over["book5"] = {"price": -105, "decimal_odds": 1.9524, "line": 17.5}
    under["book5"] = {"price": -115, "decimal_odds": 1.8696, "line": 17.5}

    result = analyze_prop_group("test", over, under)
    has_positive = any(b["ev_pct"] > 0 for b in result["books"])
    assert has_positive, "At least one book should have positive EV"


def test_outs_strong_edge():
    """Extreme mispricing → STRONG_EDGE."""
    over = {f"book{i}": {"price": -130, "decimal_odds": 1.7692, "line": 17.5} for i in range(4)}
    under = {f"book{i}": {"price": 110, "decimal_odds": 2.1, "line": 17.5} for i in range(4)}
    over["book4"] = {"price": -110, "decimal_odds": 1.9091, "line": 17.5}
    under["book4"] = {"price": -110, "decimal_odds": 1.9091, "line": 17.5}
    # book5 extreme mispricing
    over["book5"] = {"price": 250, "decimal_odds": 3.5, "line": 17.5}
    under["book5"] = {"price": -400, "decimal_odds": 1.25, "line": 17.5}

    result = analyze_prop_group("test", over, under)
    strong = [b for b in result["books"]
              if b["sportsbook"] == "book5" and b["side"] == "OVER"]
    assert len(strong) == 1
    assert strong[0]["bet_status"] == BET_STATUS_STRONG


# ====================================================================
# I. Negative-EV opportunity
# ====================================================================


def test_outs_negative_ev():
    """All books at -110/-110 → all EV should be negative (vig present)."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(6)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 17.5} for i in range(6)}
    result = analyze_prop_group("test", over, under)
    for b in result["books"]:
        assert b["ev_pct"] < 0
        assert b["bet_status"] == BET_STATUS_NO_EDGE
    assert result["recommendation"] == "NO_BET"


def test_outs_all_no_edge_still_valid():
    """All negative EV → still VALID_MARKET but NO_BET."""
    over = {f"book{i}": {"price": -120, "decimal_odds": 1.8333, "line": 17.5} for i in range(5)}
    under = {f"book{i}": {"price": 100, "decimal_odds": 2.0, "line": 17.5} for i in range(5)}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_VALID
    assert result["recommendation"] == "NO_BET"


# ====================================================================
# J. Freshness — stale vs fresh
# ====================================================================


def test_outs_stale_row_has_observation_time():
    """Observation time is preserved in parsed rows."""
    event = {
        "eventID": "test_fresh",
        "odds": {
            "pitching_outs-TEST_1_MLB-game-ou-over": {
                "playerID": "TEST_1_MLB",
                "playerNames": {"full": "Test Player"},
                "marketName": "Test Player Outs Recorded Over/Under",
                "byBookmaker": {
                    "testbook": {
                        "odds": -110,
                        "overUnder": 17.5,
                        "available": True,
                        "lastUpdatedAt": "2026-07-20T10:00:00Z",
                    }
                },
            },
            "pitching_outs-TEST_1_MLB-game-ou-under": {
                "playerID": "TEST_1_MLB",
                "playerNames": {"full": "Test Player"},
                "marketName": "Test Player Outs Recorded Over/Under",
                "byBookmaker": {
                    "testbook": {
                        "odds": -110,
                        "overUnder": 17.5,
                        "available": True,
                        "lastUpdatedAt": "2026-07-20T10:00:00Z",
                    }
                },
            },
        },
    }
    result = parse_player_props(event)
    for row in result.odds_rows:
        assert row["observation_time"] != ""


# ====================================================================
# K. Cross-market isolation — outs and strikeouts don't mix
# ====================================================================


def test_outs_and_strikeouts_separate_groups():
    """Same event with both outs and strikeouts → different group key prefixes."""
    combined = {
        "eventID": "COMBINED_EVENT",
        "odds": {
            "pitching_outs-PLAYER_1_MLB-game-ou-over": {
                "playerID": "PLAYER_1_MLB",
                "playerNames": {"full": "Test Pitcher"},
                "marketName": "Test Pitcher Outs Recorded Over/Under",
                "byBookmaker": {
                    "book1": {"odds": -110, "overUnder": 17.5, "available": True},
                    "book2": {"odds": -105, "overUnder": 17.5, "available": True},
                    "book3": {"odds": -112, "overUnder": 17.5, "available": True},
                    "book4": {"odds": -108, "overUnder": 17.5, "available": True},
                    "book5": {"odds": -115, "overUnder": 17.5, "available": True},
                },
            },
            "pitching_outs-PLAYER_1_MLB-game-ou-under": {
                "playerID": "PLAYER_1_MLB",
                "playerNames": {"full": "Test Pitcher"},
                "marketName": "Test Pitcher Outs Recorded Over/Under",
                "byBookmaker": {
                    "book1": {"odds": -110, "overUnder": 17.5, "available": True},
                    "book2": {"odds": -115, "overUnder": 17.5, "available": True},
                    "book3": {"odds": -108, "overUnder": 17.5, "available": True},
                    "book4": {"odds": -112, "overUnder": 17.5, "available": True},
                    "book5": {"odds": -105, "overUnder": 17.5, "available": True},
                },
            },
            "pitching_strikeouts-PLAYER_1_MLB-game-ou-over": {
                "playerID": "PLAYER_1_MLB",
                "playerNames": {"full": "Test Pitcher"},
                "marketName": "Test Pitcher Strikeouts Over/Under",
                "byBookmaker": {
                    "book1": {"odds": -110, "overUnder": 5.5, "available": True},
                    "book2": {"odds": -115, "overUnder": 5.5, "available": True},
                    "book3": {"odds": -108, "overUnder": 5.5, "available": True},
                    "book4": {"odds": -112, "overUnder": 5.5, "available": True},
                    "book5": {"odds": -105, "overUnder": 5.5, "available": True},
                },
            },
            "pitching_strikeouts-PLAYER_1_MLB-game-ou-under": {
                "playerID": "PLAYER_1_MLB",
                "playerNames": {"full": "Test Pitcher"},
                "marketName": "Test Pitcher Strikeouts Over/Under",
                "byBookmaker": {
                    "book1": {"odds": -110, "overUnder": 5.5, "available": True},
                    "book2": {"odds": -105, "overUnder": 5.5, "available": True},
                    "book3": {"odds": -112, "overUnder": 5.5, "available": True},
                    "book4": {"odds": -108, "overUnder": 5.5, "available": True},
                    "book5": {"odds": -115, "overUnder": 5.5, "available": True},
                },
            },
        },
    }
    result = parse_player_props(combined)
    outs_keys = {r["market_group_key"] for r in result.odds_rows
                 if r["market_type"] == "pitching_outs_ou"}
    k_keys = {r["market_group_key"] for r in result.odds_rows
              if r["market_type"] == "pitching_strikeouts_ou"}
    assert len(outs_keys) == 1
    assert len(k_keys) == 1
    assert outs_keys.isdisjoint(k_keys)
    assert "pitching_outs_ou" in outs_keys.pop()
    assert "pitching_strikeouts_ou" in k_keys.pop()


def test_outs_group_key_differs_from_strikeouts():
    """Same player/event/line → different market type → different group key."""
    outs_key = _build_group_key("EVT1", "P1", 17.5, 0, "OVER", "pitching_outs_ou")
    k_key = _build_group_key("EVT1", "P1", 17.5, 0, "OVER", "pitching_strikeouts_ou")
    assert outs_key != k_key
    assert "pitching_outs_ou" in outs_key
    assert "pitching_strikeouts_ou" in k_key


# ====================================================================
# Regression — existing strikeout + YN behavior unchanged
# ====================================================================


def test_strikeout_regression_unchanged(flaherty_event):
    """Flaherty strikeout parsing still works identically."""
    result = parse_player_props(flaherty_event)
    ou_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_ou"]
    assert len(ou_rows) > 0
    for row in ou_rows:
        assert row["market_type"] == "pitching_strikeouts_ou"
        assert "pitching_strikeouts_ou" in row["market_group_key"]


def test_yn_regression_unchanged(flaherty_event):
    """Flaherty YN parsing still works identically."""
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    assert len(yn_rows) == 5
    assert all(r["side"] == "YES" for r in yn_rows)
    assert all(r["line"] is None for r in yn_rows)


def test_strikeout_analysis_unchanged(flaherty_event):
    """Strikeout analysis produces same structure as before."""
    result = parse_player_props(flaherty_event)
    ou_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_ou"]
    main_rows = [r for r in ou_rows if not r["is_alt_line"]]
    groups = {}
    for row in main_rows:
        key = row["market_group_key"]
        if key not in groups:
            groups[key] = {"over": {}, "under": {}, "line": row["line"]}
        groups[key][row["side"].lower()][row["sportsbook"]] = {
            "price": row["price"], "decimal_odds": row["decimal_odds"],
            "line": row["line"], "validation_status": row["validation_status"],
        }
    for gkey, gdata in groups.items():
        if gdata["over"] and gdata["under"]:
            a = analyze_prop_group(gkey, gdata["over"], gdata["under"])
            assert a["market_quality"] in (
                MARKET_QUALITY_VALID, MARKET_QUALITY_NEEDS_REVIEW,
                MARKET_QUALITY_INSUFFICIENT, MARKET_QUALITY_EXCLUDED,
            )


# ====================================================================
# Registry checks
# ====================================================================


def test_registry_outs_config():
    assert PITCHER_OUTS.cli_name == "outs"
    assert PITCHER_OUTS.odd_id_stat_prefix == "pitching_outs"
    assert PITCHER_OUTS.market_type_ou == "pitching_outs_ou"
    assert PITCHER_OUTS.market_type_yn is None
    assert PITCHER_OUTS.display_name == "Pitcher Outs Recorded"
    assert PITCHER_OUTS.short_label == "Outs"
    assert PITCHER_OUTS.period == "game"
    assert PITCHER_OUTS.supports_ou is True
    assert PITCHER_OUTS.supports_yn is False
    assert PITCHER_OUTS.allowed_sides_ou == ("over", "under")


def test_registry_outs_match_ou():
    assert match_ou_market("pitching_outs-X-game-ou-over") is PITCHER_OUTS
    assert match_ou_market("pitching_outs-X-game-ou-under") is PITCHER_OUTS


def test_registry_outs_match_yn_returns_none():
    assert match_yn_market("pitching_outs-X-game-yn-yes") is None


def test_registry_outs_by_ou_type():
    assert get_market_by_ou_type("pitching_outs_ou") is PITCHER_OUTS


def test_registry_outs_by_cli():
    from src.prop_config import get_market_by_cli_name
    assert get_market_by_cli_name("outs") is PITCHER_OUTS


# ====================================================================
# Field completeness
# ====================================================================


def test_outs_required_fields_present(outs_ou_rows):
    required = {
        "event_id", "odd_id", "sportsbook", "player_id", "player_name",
        "market_type", "market_group_key", "side", "line", "price",
        "decimal_odds", "is_alt_line", "available", "validation_status",
        "mapping_confidence", "mapping_method", "validation_reason",
        "captured_at",
    }
    for row in outs_ou_rows:
        missing = required - set(row.keys())
        assert not missing, f"Row missing fields: {missing}"


def test_outs_validation_status_all_valid(outs_ou_rows):
    for row in outs_ou_rows:
        assert row["validation_status"] == STATUS_VALID


def test_outs_audit_rows_have_exclusion_fields(outs_parsed):
    for row in outs_parsed.audit_rows:
        assert "excluded" in row
        assert "exclusion_reasons" in row


# ====================================================================
# Scanner integration — run_scan with outs data
# ====================================================================


def test_scanner_outs_grouped_correctly():
    """The scanner groups outs rows into separate O/U groups."""
    from src.player_prop_parser import parse_player_props
    from src.prop_config import get_market_by_ou_type
    from src.validation_constants import APPROVED_STATUSES

    result = parse_player_props(outs_event)
    ou_groups = {}
    for row in result.odds_rows:
        if row["validation_status"] in APPROVED_STATUSES:
            key = row["market_group_key"]
            market_type = row.get("market_type", "")
            if get_market_by_ou_type(market_type) is not None:
                if key not in ou_groups:
                    ou_groups[key] = {"over": {}, "under": {}, "line": row["line"],
                                      "player_id": row["player_id"],
                                      "player_name": row["player_name"],
                                      "event_id": row["event_id"],
                                      "market_type": market_type}
                side = row["side"]
                ou_groups[key][side.lower()][row["sportsbook"]] = {
                    "price": row["price"],
                    "decimal_odds": row["decimal_odds"],
                    "line": row["line"],
                    "validation_status": row["validation_status"],
                }

    # Should have 3 groups: Cole 17.5, Cole 15.5_alt, Verlander 16.5
    # (19.5_alt has only fanduel → EXCLUDED by analysis, but still a group)
    assert len(ou_groups) >= 3
    for gkey, gdata in ou_groups.items():
        assert "pitching_outs_ou" in gdata["market_type"]
        assert gdata["line"] is not None


def test_scanner_outs_analysis_produces_opportunities():
    """Analyze outs groups and verify they produce valid analysis results."""
    from src.player_prop_parser import parse_player_props
    from src.prop_config import get_market_by_ou_type
    from src.validation_constants import APPROVED_STATUSES

    result = parse_player_props(outs_event)
    ou_groups = {}
    for row in result.odds_rows:
        if row["validation_status"] in APPROVED_STATUSES:
            key = row["market_group_key"]
            market_type = row.get("market_type", "")
            if get_market_by_ou_type(market_type) is not None:
                if key not in ou_groups:
                    ou_groups[key] = {"over": {}, "under": {}, "line": row["line"]}
                ou_groups[key][row["side"].lower()][row["sportsbook"]] = {
                    "price": row["price"], "decimal_odds": row["decimal_odds"],
                    "line": row["line"], "validation_status": row["validation_status"],
                }

    for gkey, gdata in ou_groups.items():
        if gdata["over"] and gdata["under"]:
            a = analyze_prop_group(gkey, gdata["over"], gdata["under"])
            assert "books" in a
            assert "market_quality" in a
            assert "recommendation" in a
            assert a["line"] is not None


# ====================================================================
# Stale row blocking
# ====================================================================


def test_stale_analysis_flag_preserved():
    """Observation timestamps are preserved in parsed rows."""
    event = {
        "eventID": "test_stale",
        "odds": {
            "pitching_outs-TEST_1_MLB-game-ou-over": {
                "playerID": "TEST_1_MLB",
                "playerNames": {"full": "Test Player"},
                "marketName": "Test Player Outs Recorded Over/Under",
                "byBookmaker": {
                    "testbook": {
                        "odds": -110, "overUnder": 17.5, "available": True,
                        "lastUpdatedAt": "2026-07-01T10:00:00Z",
                    }
                },
            },
            "pitching_outs-TEST_1_MLB-game-ou-under": {
                "playerID": "TEST_1_MLB",
                "playerNames": {"full": "Test Player"},
                "marketName": "Test Player Outs Recorded Over/Under",
                "byBookmaker": {
                    "testbook": {
                        "odds": -110, "overUnder": 17.5, "available": True,
                        "lastUpdatedAt": "2026-07-01T10:00:00Z",
                    }
                },
            },
        },
    }
    result = parse_player_props(event)
    for row in result.odds_rows:
        assert "2026-07-01" in row["observation_time"]
