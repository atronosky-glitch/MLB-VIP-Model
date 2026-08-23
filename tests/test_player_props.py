"""Test pitcher strikeout Over/Under parsing, analysis, and pipeline.

Uses synthetic inline data (deterministic, never cache-dependent).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.player_prop_parser import (
    parse_player_props,
    _is_pitching_k_ou,
    _build_group_key,
    _extract_side,
    _resolve_line,
    _extract_player_name_from_market,
)
from src.player_prop_analysis import (
    analyze_prop_group,
)
from src.prop_config import (
    MARKET_QUALITY_VALID,
    MARKET_QUALITY_NEEDS_REVIEW,
    MARKET_QUALITY_INSUFFICIENT,
    MARKET_QUALITY_EXCLUDED,
    BET_STATUS_STRONG,
    BET_STATUS_POSITIVE,
    BET_STATUS_MARGINAL,
    BET_STATUS_NO_EDGE,
    BET_STATUS_EXCLUDED,
    STRONG_EDGE_THRESHOLD,
    POSITIVE_EDGE_THRESHOLD,
)
from src.validation_constants import (
    STATUS_VALID,
    STATUS_NONE,
)

from tests.fixture_data import (
    flaherty_event as _flaherty_event,
    all_synthetic_events,
)


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def api_data() -> dict:
    return {"data": all_synthetic_events}


@pytest.fixture(scope="session")
def flaherty_event() -> dict:
    return dict(_flaherty_event)


@pytest.fixture(scope="session")
def all_events(api_data) -> list[dict]:
    return api_data.get("data", api_data.get("events", []))


# ====================================================================
# Odd ID parsing
# ====================================================================


def test_is_pitching_k_ou_flaherty():
    assert _is_pitching_k_ou("pitching_strikeouts-JACK_FLAHERTY_1_MLB-game-ou-over") is True
    assert _is_pitching_k_ou("pitching_strikeouts-JACK_FLAHERTY_1_MLB-game-ou-under") is True


def test_is_pitching_k_ou_rejects_yn():
    assert _is_pitching_k_ou("pitching_strikeouts-JOE_RYAN_1_MLB-game-yn-yes") is False
    assert _is_pitching_k_ou("pitching_strikeouts-JOE_RYAN_1_MLB-game-yn-no") is False


def test_is_pitching_k_ou_rejects_other_stats():
    assert _is_pitching_k_ou("pitching_hits-JACK_FLAHERTY_1_MLB-game-ou-over") is False
    assert _is_pitching_k_ou("pitching_outs-JACK_FLAHERTY_1_MLB-game-ou-over") is False


def test_is_pitching_k_ou_rejects_non_props():
    assert _is_pitching_k_ou("points-away-game-ou-over") is False
    assert _is_pitching_k_ou("spreads-home-game-ou-over") is False


def test_is_pitching_k_ou_bad_format():
    assert _is_pitching_k_ou("") is False
    assert _is_pitching_k_ou("pitching_strikeouts") is False
    assert _is_pitching_k_ou("not-even-close") is False


# ====================================================================
# Helper functions
# ====================================================================


def test_extract_side():
    assert _extract_side("pitching_strikeouts-JOE_RYAN_1_MLB-game-ou-over") == "over"
    assert _extract_side("pitching_strikeouts-JOE_RYAN_1_MLB-game-ou-under") == "under"


def test_extract_side_invalid():
    assert _extract_side("nodash") is None


def test_resolve_line():
    assert _resolve_line({"overUnder": 5.5}) == 5.5
    assert _resolve_line({"overUnder": 4.5}) == 4.5
    assert _resolve_line({"odds": -110}) is None
    assert _resolve_line({}) is None


def test_extract_player_name_from_market():
    odd = {"marketName": "Jack Flaherty Strikeouts Over/Under"}
    assert _extract_player_name_from_market(odd) == "Jack Flaherty"
    odd2 = {"marketName": "Jacob deGrom Strikeouts Over/Under"}
    assert _extract_player_name_from_market(odd2) == "Jacob deGrom"
    odd3 = {"marketName": "Cristopher S\u00e1nchez Strikeouts Over/Under"}
    assert _extract_player_name_from_market(odd3) == "Cristopher S\u00e1nchez"


def test_extract_player_name_empty():
    assert _extract_player_name_from_market({}) == ""
    assert _extract_player_name_from_market({"marketName": ""}) == ""


# ====================================================================
# Market group key
# ====================================================================


def test_group_key_pairing():
    """Over and Under at same line must share group key."""
    over_key = _build_group_key("ev1", "PLAYER_1", 5.5, 0, "OVER")
    under_key = _build_group_key("ev1", "PLAYER_1", 5.5, 0, "UNDER")
    assert over_key == under_key


def test_group_key_diff_lines_dont_pair():
    """Different lines must have different group keys."""
    key_45 = _build_group_key("ev1", "PLAYER_1", 4.5, 0, "OVER")
    key_55 = _build_group_key("ev1", "PLAYER_1", 5.5, 0, "UNDER")
    assert key_45 != key_55


def test_group_key_diff_players():
    """Different players must have different group keys."""
    key_a = _build_group_key("ev1", "PLAYER_A", 5.5, 0, "OVER")
    key_b = _build_group_key("ev1", "PLAYER_B", 5.5, 0, "OVER")
    assert key_a != key_b


def test_group_key_alt_separate():
    """Alt lines must have different keys from main lines."""
    main_key = _build_group_key("ev1", "PLAYER_1", 5.5, 0, "OVER")
    alt_key = _build_group_key("ev1", "PLAYER_1", 5.5, 1, "OVER")
    assert main_key != alt_key
    assert "_alt" in alt_key
    assert "_alt" not in main_key


def test_group_key_empty():
    assert _build_group_key("", "PLAYER", 5.5, 0, "OVER") == ""
    assert _build_group_key("ev1", "", 5.5, 0, "OVER") == ""
    assert _build_group_key("ev1", "PLAYER", None, 0, "OVER") == ""


# ====================================================================
# Parsing pipeline
# ====================================================================


def test_parse_flaherty_ou_markets(flaherty_event):
    """Parse returns both approved and audit rows."""
    result = parse_player_props(flaherty_event)
    assert len(result.odds_rows) > 0
    assert len(result.audit_rows) > 0
    for row in result.odds_rows:
        assert row["validation_status"] == STATUS_VALID
    for row in result.audit_rows:
        assert "player_id" in row
        assert "player_name" in row
        assert "excluded" in row


def test_parse_flaherty_has_correct_player_ids(flaherty_event):
    result = parse_player_props(flaherty_event)
    player_ids = {row["player_id"] for row in result.odds_rows}
    assert "JACK_FLAHERTY_1_MLB" in player_ids
    assert "JAMESON_TAILLON_1_MLB" in player_ids


def test_parse_flaherty_market_type(flaherty_event):
    result = parse_player_props(flaherty_event)
    for row in result.odds_rows:
        assert row["market_type"] in ("pitching_strikeouts_ou", "pitching_strikeouts_yn")


def test_parse_flaherty_sides(flaherty_event):
    result = parse_player_props(flaherty_event)
    sides = {row["side"] for row in result.odds_rows}
    assert "OVER" in sides
    assert "UNDER" in sides


def test_parse_flaherty_line_55(flaherty_event):
    result = parse_player_props(flaherty_event)
    ou_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_ou"]
    main_rows = [r for r in ou_rows if not r["is_alt_line"]]
    flaherty_lines = {r["line"] for r in main_rows if r["player_id"] == "JACK_FLAHERTY_1_MLB"}
    taillon_lines = {r["line"] for r in main_rows if r["player_id"] == "JAMESON_TAILLON_1_MLB"}
    assert flaherty_lines == {5.5}
    assert 3.5 in taillon_lines
    assert 4.5 in taillon_lines


def test_parse_flaherty_has_alt_lines(flaherty_event):
    result = parse_player_props(flaherty_event)
    alt_rows = [r for r in result.odds_rows if r["is_alt_line"]]
    assert len(alt_rows) > 0


def test_parse_flaherty_alt_lines_diff_line(flaherty_event):
    result = parse_player_props(flaherty_event)
    alt_rows = [r for r in result.odds_rows if r["is_alt_line"]]
    alt_lines = {r["line"] for r in alt_rows}
    assert len(alt_lines) >= 2


def test_parse_flaherty_market_group_key_main(flaherty_event):
    """Each player's over/under at SAME line should share group key."""
    result = parse_player_props(flaherty_event)
    ou_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_ou"]
    main_rows = [r for r in ou_rows if not r["is_alt_line"]]
    for pid in {r["player_id"] for r in main_rows}:
        pid_rows = [r for r in main_rows if r["player_id"] == pid]
        for line in {r["line"] for r in pid_rows}:
            line_rows = [r for r in pid_rows if r["line"] == line]
            over_keys = {r["market_group_key"] for r in line_rows if r["side"] == "OVER"}
            under_keys = {r["market_group_key"] for r in line_rows if r["side"] == "UNDER"}
            assert len(over_keys) == 1, f"{pid} line {line}: over rows should share one key"
            assert over_keys == under_keys, f"{pid} line {line}: over/under keys should match"


def test_parse_alt_lines_separate_group_key(flaherty_event):
    result = parse_player_props(flaherty_event)
    main_keys = {r["market_group_key"] for r in result.odds_rows if not r["is_alt_line"]}
    alt_keys = {r["market_group_key"] for r in result.odds_rows if r["is_alt_line"]}
    assert main_keys.isdisjoint(alt_keys)


def test_parse_different_players_dont_mix(all_events):
    """Different players must have different group keys."""
    all_keys_per_player = {}
    for ev in all_events:
        result = parse_player_props(ev)
        for row in result.odds_rows:
            pid = row["player_id"]
            key = row["market_group_key"]
            all_keys_per_player.setdefault(pid, set()).add(key)

    for pid_a, keys_a in all_keys_per_player.items():
        for pid_b, keys_b in all_keys_per_player.items():
            if pid_a < pid_b:
                assert keys_a.isdisjoint(keys_b), \
                    f"Group keys overlap between {pid_a} and {pid_b}"


# ====================================================================
# Price validity
# ====================================================================


def test_parse_prices_are_integers(flaherty_event):
    result = parse_player_props(flaherty_event)
    for row in result.odds_rows:
        assert isinstance(row["price"], int)


def test_parse_decimal_odds_are_positive(flaherty_event):
    result = parse_player_props(flaherty_event)
    for row in result.odds_rows:
        assert row["decimal_odds"] is not None
        assert row["decimal_odds"] > 1.0


def test_parse_available_all_true_for_approved(flaherty_event):
    result = parse_player_props(flaherty_event)
    for row in result.odds_rows:
        assert row["available"] == 1


# ====================================================================
# Exclusion tests
# ====================================================================


def test_unavailable_excluded():
    event = {
        "eventID": "test_event",
        "odds": {
            "pitching_strikeouts-TEST_PLAYER_1_MLB-game-ou-over": {
                "playerID": "TEST_PLAYER_1_MLB",
                "marketName": "Test Player Strikeouts Over/Under",
                "byBookmaker": {
                    "testbook": {
                        "odds": -110,
                        "overUnder": 5.5,
                        "available": False,
                    }
                }
            }
        }
    }
    result = parse_player_props(event)
    assert len(result.odds_rows) == 0
    assert result.audit_rows[0]["excluded"] == 1


def test_missing_player_id_excluded():
    event = {
        "eventID": "test_event",
        "odds": {
            "pitching_strikeouts--game-ou-over": {
                "playerID": "",
                "marketName": "Test Strikeouts Over/Under",
                "byBookmaker": {
                    "testbook": {
                        "odds": -110,
                        "overUnder": 5.5,
                        "available": True,
                    }
                }
            }
        }
    }
    result = parse_player_props(event)
    assert len(result.odds_rows) == 0
    assert "Missing player ID" in result.audit_rows[0]["exclusion_reasons"]


def test_invalid_side_excluded():
    event = {
        "eventID": "test_event",
        "odds": {
            "pitching_strikeouts-TEST_1_MLB-game-ou-invalid": {
                "playerID": "TEST_1_MLB",
                "marketName": "Test Strikeouts Over/Under",
                "byBookmaker": {
                    "testbook": {
                        "odds": -110,
                        "overUnder": 5.5,
                        "available": True,
                    }
                }
            }
        }
    }
    result = parse_player_props(event)
    assert len(result.odds_rows) == 0


# ====================================================================
# Analysis — market quality
# ====================================================================


def test_analyze_empty_side_is_excluded():
    result = analyze_prop_group("test_key", {}, {})
    assert result["market_quality"] == MARKET_QUALITY_EXCLUDED
    assert result["recommendation"] == "NO_BET"


def test_analyze_one_book_is_excluded():
    """1 book = EXCLUDED (need minimum 2 for any pairing)."""
    over = {"book1": {"price": -110, "decimal_odds": 1.9091, "line": 5.5}}
    under = {"book1": {"price": -110, "decimal_odds": 1.9091, "line": 5.5}}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_EXCLUDED


def test_analyze_one_book_insufficient():
    """1 paired book = EXCLUDED (the hard "fewer than 2 books" floor, not
    the now-unreachable-at-1-book INSUFFICIENT branch) -- MIN_COMPARISON_BOOKS
    lowered from 4 to 1 2026-08-22, see docs/DECISIONS.md "Book-count gate
    lowered to the LOO floor". A single book has no comparison book at
    all -- there's no LOO consensus possible with zero peers."""
    over = {"book0": {"price": -110, "decimal_odds": 1.9091, "line": 5.5}}
    under = {"book0": {"price": -110, "decimal_odds": 1.9091, "line": 5.5}}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_EXCLUDED


def test_analyze_two_books_valid_market():
    """2 paired books (1+ comparison) = VALID_MARKET."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(2)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(2)}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_VALID
    assert result["n_paired_books"] == 2


def test_analyze_five_books_still_valid_market():
    """A market with more books than the new floor is still comfortably valid."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_VALID
    assert result["n_paired_books"] == 5


def test_all_negative_ev_still_valid_market():
    """A market with all negative EV is still VALID_MARKET but NO_EDGE/NO_BET."""
    over = {f"book{i}": {"price": -120, "decimal_odds": 1.8333, "line": 5.5} for i in range(5)}
    under = {f"book{i}": {"price": 100, "decimal_odds": 2.0, "line": 5.5} for i in range(5)}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_VALID
    assert result["recommendation"] == "NO_BET"
    # Every individual bet should be NO_EDGE
    for b in result["books"]:
        assert b["bet_status"] == BET_STATUS_NO_EDGE


def test_valid_market_does_not_force_recommendation():
    """A valid market must NOT automatically create a recommendation."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_VALID
    assert result["best_ev"] is None
    assert result["recommendation"] == "NO_BET"


# ====================================================================
# Analysis — bet status / edge thresholds
# ====================================================================


def test_strong_edge_threshold():
    """EV >= 5% should be STRONG_EDGE."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    # Over 5 at +300: fair prob from LOO ~50%, EV = 0.5*4 - 1 = 100%
    over["book0"] = {"price": 300, "decimal_odds": 4.0, "line": 5.5}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    under["book0"] = {"price": -400, "decimal_odds": 1.25, "line": 5.5}

    result = analyze_prop_group("test", over, under)
    # Find book0 OVER
    for b in result["books"]:
        if b["sportsbook"] == "book0" and b["side"] == "OVER":
            assert b["bet_status"] == BET_STATUS_STRONG, f"Expected STRONG, got {b['bet_status']}"
            assert b["ev_pct"] >= 5.0


def test_positive_edge_threshold():
    """EV >= 2% and < 5% should be POSITIVE_EDGE."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    # Give the last book slightly favourable Over price: -105 -> dec=1.9524, fair~0.5, EV ~ -0.0238... too low
    # Need better: Over at -108: dec = 1.9259, EV = 0.5*1.9259 - 1 = -0.037... still negative
    # Let's use Over at -102: dec = 1.9804, EV = 0.5*1.9804 - 1 = -0.0098... still negative
    # The issue: consensus Over from -110 is prob 0.5238, consensus Under from -110 also 0.5238
    # nv_prob = 0.5 both sides. Any -110 is EV = 0.5*1.9091-1 = -0.0455 (negative, NO_EDGE)
    # We need to manipulate the under side to be very cheap so Over has positive EV
    # Over at -102: fair prob ~0.5 via LOO (other 4 are -110/-110), EV = 0.5*1.9804-1 = -0.0098
    # Still negative. Let me make 4 books have Over -110 / Under 100 (sharp Under)
    # Then book0 has Over 105 / Under -130
    # Actually let's think differently. Make all books but one have Over at -130 and Under at +110.
    # nv for the 4: prob = 1/1.7692=0.5652 for Over, 1/2.1=0.4762 for Under
    # total = 1.0414, nv_over = 0.5652/1.0414 = 0.5427
    # Then book5 has Over at -110: dec = 1.9091, EV = 0.5427*1.9091 - 1 = 0.0359 -> 3.59%

    over["book5"] = {"price": -110, "decimal_odds": 1.9091, "line": 5.5}
    under["book5"] = {"price": -110, "decimal_odds": 1.9091, "line": 5.5}

    # Make 4 books have Over at -130, Under at +110 (sharp Under)
    for i in range(4):
        over[f"book{i}"] = {"price": -130, "decimal_odds": 1.7692, "line": 5.5}
        under[f"book{i}"] = {"price": 110, "decimal_odds": 2.1, "line": 5.5}

    # book4 stays as -110/-110 (neutral)
    over["book4"] = {"price": -110, "decimal_odds": 1.9091, "line": 5.5}
    under["book4"] = {"price": -110, "decimal_odds": 1.9091, "line": 5.5}

    result = analyze_prop_group("test", over, under)
    # Check book5 (the one with cheapest Over): LOO excludes book5, uses 4 sharp + 1 neutral
    for b in result["books"]:
        if b["sportsbook"] == "book5" and b["side"] == "OVER":
            assert b["ev_pct"] >= 2.0, f"Expected >= 2%, got {b['ev_pct']}%"
            assert b["bet_status"] == BET_STATUS_POSITIVE, \
                f"Expected POSITIVE, got {b['bet_status']} for {b['ev_pct']}%"


def test_marginal_edge():
    """0% < EV < 2% should be MARGINAL_EDGE.

    Scenaro: 4 books have Over at -120 / Under at +100 (sharp Under).
    LOO for book5: those 4 + book4 (-110/-110) gives fair Over ~52.18%.
    Book5 Over at -105: EV = 0.5218 * 1.9524 - 1 = ~1.87%.
    """
    over = {f"book{i}": {"price": -120, "decimal_odds": 1.8333, "line": 5.5} for i in range(4)}
    under = {f"book{i}": {"price": 100, "decimal_odds": 2.0, "line": 5.5} for i in range(4)}
    over["book4"] = {"price": -110, "decimal_odds": 1.9091, "line": 5.5}
    under["book4"] = {"price": -110, "decimal_odds": 1.9091, "line": 5.5}
    over["book5"] = {"price": -105, "decimal_odds": 1.9524, "line": 5.5}
    under["book5"] = {"price": -115, "decimal_odds": 1.8696, "line": 5.5}

    result = analyze_prop_group("test", over, under)
    for b in result["books"]:
        if b["sportsbook"] == "book5" and b["side"] == "OVER":
            assert 0 < b["ev_pct"] < 2.0, f"EV should be marginal: {b['ev_pct']}%"
            assert b["bet_status"] == BET_STATUS_MARGINAL


def test_no_edge_for_zero_ev():
    """EV <= 0 should be NO_EDGE."""
    # With all books at -110/-110, every EV should be negative (vig present)
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    result = analyze_prop_group("test", over, under)
    for b in result["books"]:
        assert b["bet_status"] == BET_STATUS_NO_EDGE, \
            f"{b['sportsbook']} {b['side']}: EV {b['ev_pct']}% should be NO_EDGE"


# ====================================================================
# Analysis — LOO and EV correctness
# ====================================================================


def test_loo_excludes_evaluated_book():
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    over["book0"] = {"price": 500, "decimal_odds": 6.0, "line": 5.5}
    under["book0"] = {"price": -800, "decimal_odds": 1.125, "line": 5.5}

    result = analyze_prop_group("test", over, under)

    book0 = next(b for b in result["books"] if b["sportsbook"] == "book0" and b["side"] == "OVER")
    book1 = next(b for b in result["books"] if b["sportsbook"] == "book1" and b["side"] == "OVER")

    assert abs(book0["fair_prob"] - 0.5) < 0.01, \
        "LOO fair prob should be ~50% when other 4 books are -110/-110"
    assert book0["fair_prob"] != book1["fair_prob"], \
        "LOO should give different fair probs for different evaluated books"


def test_ev_calculation():
    """EV = fair_prob * decimal - 1."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}

    result = analyze_prop_group("test", over, under)

    for book_entry in result["books"]:
        fair = book_entry["fair_prob"]
        dec = book_entry["decimal_odds"]
        expected_ev_pct = (fair * dec - 1.0) * 100
        assert abs(book_entry["ev_pct"] - expected_ev_pct) < 0.01


def test_extreme_outlier_needs_review():
    """An extreme outlier (EV magnitude >10%) should be NEEDS_REVIEW."""
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    over["book0"] = {"price": 500, "decimal_odds": 6.0, "line": 5.5}
    under["book0"] = {"price": -800, "decimal_odds": 1.125, "line": 5.5}

    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_NEEDS_REVIEW


def test_analyze_vig_positive():
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    result = analyze_prop_group("test", over, under)
    assert result["vig_pct"] > 0


def test_analyze_probs_sum_to_one():
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    result = analyze_prop_group("test", over, under)
    assert abs(result["nv_prob_over"] + result["nv_prob_under"] - 1.0) < 0.001


# ====================================================================
# Configuration / threshold tests
# ====================================================================


def test_thresholds_can_be_changed_via_config():
    """Overriding prop_config values must change behavior at runtime."""
    import src.prop_config as cfg

    saved_s = cfg.STRONG_EDGE_THRESHOLD
    saved_p = cfg.POSITIVE_EDGE_THRESHOLD
    try:
        cfg.STRONG_EDGE_THRESHOLD = 0.005  # 0.5%
        cfg.POSITIVE_EDGE_THRESHOLD = 0.0025  # 0.25%

        over = {f"book{i}": {"price": -120, "decimal_odds": 1.8333, "line": 5.5} for i in range(4)}
        under = {f"book{i}": {"price": 100, "decimal_odds": 2.0, "line": 5.5} for i in range(4)}
        over["book4"] = {"price": -110, "decimal_odds": 1.9091, "line": 5.5}
        under["book4"] = {"price": -110, "decimal_odds": 1.9091, "line": 5.5}
        over["book5"] = {"price": -105, "decimal_odds": 1.9524, "line": 5.5}
        under["book5"] = {"price": -115, "decimal_odds": 1.8696, "line": 5.5}

        result = analyze_prop_group("test", over, under)
        for b in result["books"]:
            if b["sportsbook"] == "book5" and b["side"] == "OVER":
                assert b["ev_pct"] > 0.25, f"EV should be > 0.25%: {b['ev_pct']}%"
                assert b["bet_status"] == "STRONG_EDGE", \
                    f"With changed thresholds ({b['ev_pct']}%), should be STRONG"
    finally:
        cfg.STRONG_EDGE_THRESHOLD = saved_s
        cfg.POSITIVE_EDGE_THRESHOLD = saved_p


def test_config_changes_do_not_leak():
    """A config change in one test must not affect the next."""
    import src.prop_config as cfg
    # Verify values are the defaults
    assert cfg.STRONG_EDGE_THRESHOLD == 0.05
    assert cfg.POSITIVE_EDGE_THRESHOLD == 0.02


# ====================================================================
# Full pipeline
# ====================================================================


def test_pipeline_flaherty_main_line(flaherty_event):
    """Parse, group by market_group_key, and analyze the main line."""
    result = parse_player_props(flaherty_event)
    ou_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_ou"]
    main_rows = [r for r in ou_rows if not r["is_alt_line"]]

    assert len(main_rows) > 0

    groups = {}
    for row in main_rows:
        key = row["market_group_key"]
        if key not in groups:
            groups[key] = {"over": {}, "under": {}}
        side = row["side"]
        groups[key][side.lower()][row["sportsbook"]] = {
            "price": row["price"],
            "decimal_odds": row["decimal_odds"],
            "line": row["line"],
            "validation_status": row["validation_status"],
        }

    for gkey, gdata in groups.items():
        analysis = analyze_prop_group(gkey, gdata["over"], gdata["under"])
        assert analysis["n_paired_books"] > 0
        assert analysis["line"] is not None
        assert analysis["market_quality"] in (
            MARKET_QUALITY_VALID, MARKET_QUALITY_NEEDS_REVIEW,
            MARKET_QUALITY_INSUFFICIENT, MARKET_QUALITY_EXCLUDED,
        )
        for book_entry in analysis["books"]:
            assert -100.0 < book_entry["ev_pct"] < 500.0, \
                f"EV should be within reasonable range: {book_entry['ev_pct']}%"


def test_pipeline_alt_lines_separate(flaherty_event):
    result = parse_player_props(flaherty_event)
    main_keys = {r["market_group_key"] for r in result.odds_rows if not r["is_alt_line"]}
    alt_keys = {r["market_group_key"] for r in result.odds_rows if r["is_alt_line"]}
    assert main_keys.isdisjoint(alt_keys)


def test_pipeline_multiple_players_different_groups(all_events):
    all_keys = set()
    for ev in all_events:
        result = parse_player_props(ev)
        keys = {r["market_group_key"] for r in result.odds_rows}
        duplicates = keys & all_keys
        assert len(duplicates) == 0, f"Duplicate group keys across events: {duplicates}"
        all_keys.update(keys)


# ====================================================================
# Field completeness
# ====================================================================


def test_all_required_fields_present(flaherty_event):
    required = {
        "event_id", "odd_id", "sportsbook", "player_id", "player_name",
        "market_type", "market_group_key", "side", "line", "price",
        "decimal_odds", "is_alt_line", "available", "validation_status",
        "mapping_confidence", "mapping_method", "validation_reason",
        "captured_at",
    }
    result = parse_player_props(flaherty_event)
    for row in result.odds_rows:
        missing = required - set(row.keys())
        assert not missing, f"Row missing fields: {missing}"


def test_audit_rows_have_exclusion_fields(flaherty_event):
    result = parse_player_props(flaherty_event)
    for row in result.audit_rows:
        assert "excluded" in row
        assert "exclusion_reasons" in row


def test_all_events_parse_successfully(all_events):
    for ev in all_events:
        result = parse_player_props(ev)
        assert result is not None


def test_player_prop_analysis_imports():
    """All key constants and functions must be importable."""
    from src.prop_config import (
        MARKET_QUALITY_VALID, MARKET_QUALITY_NEEDS_REVIEW,
        MARKET_QUALITY_INSUFFICIENT, MARKET_QUALITY_EXCLUDED,
        BET_STATUS_STRONG, BET_STATUS_POSITIVE,
        BET_STATUS_MARGINAL, BET_STATUS_NO_EDGE,
        STRONG_EDGE_THRESHOLD, POSITIVE_EDGE_THRESHOLD,
    )
    assert MARKET_QUALITY_VALID == "VALID_MARKET"
    assert BET_STATUS_NO_EDGE == "NO_EDGE"
    assert STRONG_EDGE_THRESHOLD == 0.05
    assert POSITIVE_EDGE_THRESHOLD == 0.02


# ====================================================================
# Yes/No (YN) parser tests
# ====================================================================


def test_is_pitching_k_yn_yes():
    from src.player_prop_parser import _is_pitching_k_yn
    assert _is_pitching_k_yn("pitching_strikeouts-JACK_FLAHERTY_1_MLB-game-yn-yes")


def test_is_pitching_k_yn_no():
    from src.player_prop_parser import _is_pitching_k_yn
    assert _is_pitching_k_yn("pitching_strikeouts-JACK_FLAHERTY_1_MLB-game-yn-no")


def test_is_pitching_k_yn_rejects_ou():
    from src.player_prop_parser import _is_pitching_k_yn
    assert not _is_pitching_k_yn("pitching_strikeouts-JACK_FLAHERTY_1_MLB-game-ou-over")


def test_yn_yes_side_parsed(flaherty_event):
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    assert len(yn_rows) == 5
    assert all(r["side"] == "YES" for r in yn_rows)


def test_yn_no_side_not_in_odds_rows(flaherty_event):
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    assert all(r["side"] != "NO" for r in yn_rows)


def test_yn_no_side_in_audit(flaherty_event):
    result = parse_player_props(flaherty_event)
    yn_audit = [r for r in result.audit_rows
                if r["market_type"] == "pitching_strikeouts_yn" and r["side"] == "NO"]
    assert len(yn_audit) == 1
    assert yn_audit[0]["excluded"] == 1


def test_yn_line_is_none(flaherty_event):
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    assert all(r["line"] is None for r in yn_rows)


def test_yn_group_key_format(flaherty_event):
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    keys = {r["market_group_key"] for r in yn_rows}
    assert len(keys) == 1
    key = keys.pop()
    assert "pitching_strikeouts_yn" in key
    assert "JACK_FLAHERTY_1_MLB" in key


def test_yn_prices_are_integers(flaherty_event):
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    for r in yn_rows:
        assert isinstance(r["price"], int)


def test_yn_market_group_key_differs_from_ou(flaherty_event):
    result = parse_player_props(flaherty_event)
    ou_keys = {r["market_group_key"] for r in result.odds_rows
               if r["market_type"] == "pitching_strikeouts_ou"}
    yn_keys = {r["market_group_key"] for r in result.odds_rows
               if r["market_type"] == "pitching_strikeouts_yn"}
    assert ou_keys.isdisjoint(yn_keys)


# ====================================================================
# Yes/No analysis tests
# ====================================================================


def test_yn_analysis_basic(flaherty_event):
    from src.player_prop_analysis import analyze_yn_group
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    yes_prices = {}
    for r in yn_rows:
        yes_prices[r["sportsbook"]] = {
            "price": r["price"],
            "decimal_odds": r["decimal_odds"],
            "validation_status": r["validation_status"],
        }
    analysis = analyze_yn_group("test_key", yes_prices)
    assert analysis["market_quality"] == MARKET_QUALITY_VALID
    assert analysis["n_books"] == 5
    assert len(analysis["books"]) == 5


def test_yn_no_true_ev_fields(flaherty_event):
    from src.player_prop_analysis import analyze_yn_group
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    yes_prices = {}
    for r in yn_rows:
        yes_prices[r["sportsbook"]] = {
            "price": r["price"],
            "decimal_odds": r["decimal_odds"],
            "validation_status": r["validation_status"],
        }
    analysis = analyze_yn_group("test_key", yes_prices)
    for book in analysis["books"]:
        assert "ev_pct" not in book
        assert "fair_prob" not in book
        assert "fair_odds" not in book
        assert "expected_value" not in book


def test_yn_has_price_advantage_metrics(flaherty_event):
    from src.player_prop_analysis import analyze_yn_group
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    yes_prices = {}
    for r in yn_rows:
        yes_prices[r["sportsbook"]] = {
            "price": r["price"],
            "decimal_odds": r["decimal_odds"],
            "validation_status": r["validation_status"],
        }
    analysis = analyze_yn_group("test_key", yes_prices)
    for book in analysis["books"]:
        assert "price_advantage_pct" in book
        assert "relative_payout_advantage_pct" in book
        assert "decimal_odds_advantage" in book
        assert "comparison_status" in book
        assert "market_reference_probability" in book
        assert "market_reference_odds" in book


def test_yn_reference_method_is_loo_median(flaherty_event):
    from src.player_prop_analysis import analyze_yn_group
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    yes_prices = {}
    for r in yn_rows:
        yes_prices[r["sportsbook"]] = {
            "price": r["price"],
            "decimal_odds": r["decimal_odds"],
            "validation_status": r["validation_status"],
        }
    analysis = analyze_yn_group("test_key", yes_prices)
    assert analysis["reference_method"] == "LOO median implied probability"
    assert analysis["reference_book_count"] == 4


def test_yn_comparison_status_valid_values(flaherty_event):
    from src.player_prop_analysis import analyze_yn_group
    from src.prop_config import (
        YN_STATUS_STRONG_OUTLIER, YN_STATUS_OUTLIER,
        YN_STATUS_MARGINAL_OUTLIER, YN_STATUS_IN_LINE, YN_STATUS_WORSE,
    )
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    yes_prices = {}
    for r in yn_rows:
        yes_prices[r["sportsbook"]] = {
            "price": r["price"],
            "decimal_odds": r["decimal_odds"],
            "validation_status": r["validation_status"],
        }
    analysis = analyze_yn_group("test_key", yes_prices)
    valid_statuses = {
        YN_STATUS_STRONG_OUTLIER, YN_STATUS_OUTLIER,
        YN_STATUS_MARGINAL_OUTLIER, YN_STATUS_IN_LINE, YN_STATUS_WORSE,
    }
    for book in analysis["books"]:
        assert book["comparison_status"] in valid_statuses


def test_yn_recommendation_eligible_only_outliers(flaherty_event):
    from src.player_prop_analysis import analyze_yn_group
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    yes_prices = {}
    for r in yn_rows:
        yes_prices[r["sportsbook"]] = {
            "price": r["price"],
            "decimal_odds": r["decimal_odds"],
            "validation_status": r["validation_status"],
        }
    analysis = analyze_yn_group("test_key", yes_prices)
    for book in analysis["books"]:
        if book["recommendation_eligible"]:
            assert book["comparison_status"] in ("STRONG_PRICE_OUTLIER", "PRICE_OUTLIER")


def test_yn_insufficient_books():
    """YN_MIN_COMPARISON_BOOKS lowered from 3 to 1 2026-08-22, see
    docs/DECISIONS.md "Book-count gate lowered to the LOO floor" -- only a
    single book (no comparison book at all) is insufficient now."""
    from src.player_prop_analysis import analyze_yn_group
    yes_prices = {
        "book_a": {"price": -500, "decimal_odds": 1.2, "validation_status": "VALID"},
    }
    analysis = analyze_yn_group("test_key", yes_prices)
    assert analysis["market_quality"] == MARKET_QUALITY_INSUFFICIENT
    assert analysis["n_books"] == 1
    assert not analysis["recommendation_eligible"]


def test_yn_two_books_now_sufficient():
    from src.player_prop_analysis import analyze_yn_group
    yes_prices = {
        "book_a": {"price": -500, "decimal_odds": 1.2, "validation_status": "VALID"},
        "book_b": {"price": -550, "decimal_odds": 1.1818, "validation_status": "VALID"},
    }
    analysis = analyze_yn_group("test_key", yes_prices)
    assert analysis["market_quality"] != MARKET_QUALITY_INSUFFICIENT


def test_yn_empty_prices():
    from src.player_prop_analysis import analyze_yn_group
    analysis = analyze_yn_group("test_key", {})
    assert analysis["market_quality"] == MARKET_QUALITY_EXCLUDED
    assert analysis["n_books"] == 0
    assert not analysis["recommendation_eligible"]


def test_yn_group_key_builder():
    from src.player_prop_parser import _build_yn_group_key
    key = _build_yn_group_key("EVT001", "PLAYER_1")
    assert key == "EVT001|PLAYER_1|pitching_strikeouts_yn|game"


def test_yn_group_key_empty():
    from src.player_prop_parser import _build_yn_group_key
    assert _build_yn_group_key("", "PLAYER_1") == ""
    assert _build_yn_group_key("EVT001", "") == ""


# ── decimal_odds_advantage unit tests ──────────────────────────────


def test_decimal_odds_advantage_neg_vs_neg():
    from src.player_prop_analysis import _compute_decimal_odds_advantage
    # -110 (dec 1.9091) vs -120 (dec 1.8333) → offered better by 8
    assert _compute_decimal_odds_advantage(-110, -120) == 8


def test_decimal_odds_advantage_pos_vs_pos():
    from src.player_prop_analysis import _compute_decimal_odds_advantage
    # +150 (dec 2.5) vs +120 (dec 2.2) → offered better by 30
    assert _compute_decimal_odds_advantage(150, 120) == 30


def test_decimal_odds_advantage_neg_vs_pos_crossing():
    from src.player_prop_analysis import _compute_decimal_odds_advantage
    # -110 (dec 1.9091) vs +110 (dec 2.1) → offered worse by 19
    assert _compute_decimal_odds_advantage(-110, 110) == -19


def test_decimal_odds_advantage_pos_vs_neg_crossing():
    from src.player_prop_analysis import _compute_decimal_odds_advantage
    # +150 (dec 2.5) vs -150 (dec 1.6667) → offered better by 83
    assert _compute_decimal_odds_advantage(150, -150) == 83


def test_decimal_odds_advantage_equal():
    from src.player_prop_analysis import _compute_decimal_odds_advantage
    assert _compute_decimal_odds_advantage(-110, -110) == 0


# ── Market registry regression tests ──────────────────────────────


def test_registry_strikeouts_ou_detected():
    from src.prop_config import match_ou_market, PITCHER_STRIKEOUTS
    assert match_ou_market("pitching_strikeouts-X-game-ou-over") is PITCHER_STRIKEOUTS
    assert match_ou_market("pitching_strikeouts-X-game-ou-under") is PITCHER_STRIKEOUTS


def test_registry_strikeouts_yn_detected():
    from src.prop_config import match_yn_market, PITCHER_STRIKEOUTS
    assert match_yn_market("pitching_strikeouts-X-game-yn-yes") is PITCHER_STRIKEOUTS
    assert match_yn_market("pitching_strikeouts-X-game-yn-no") is PITCHER_STRIKEOUTS


def test_registry_unknown_returns_none():
    from src.prop_config import match_ou_market, match_yn_market
    assert match_ou_market("batting_unknown_stat-X-game-ou-over") is None
    assert match_yn_market("batting_unknown_stat-X-game-yn-yes") is None
    assert match_ou_market("pitching_unknown-X-game-ou-over") is None


def test_registry_market_type_lookup():
    from src.prop_config import get_market_by_ou_type, get_market_by_yn_type, PITCHER_STRIKEOUTS, PITCHER_HITS_ALLOWED, PITCHER_WALKS_ALLOWED
    assert get_market_by_ou_type("pitching_strikeouts_ou") is PITCHER_STRIKEOUTS
    assert get_market_by_yn_type("pitching_strikeouts_yn") is PITCHER_STRIKEOUTS
    assert get_market_by_ou_type("pitching_hits_ou") is PITCHER_HITS_ALLOWED
    assert get_market_by_ou_type("pitching_basesOnBalls_ou") is PITCHER_WALKS_ALLOWED


def test_registry_cli_name_lookup():
    from src.prop_config import get_market_by_cli_name, PITCHER_STRIKEOUTS
    assert get_market_by_cli_name("strikeouts") is PITCHER_STRIKEOUTS
    assert get_market_by_cli_name("unknown") is None


def test_regression_flaherty_ou_identical(flaherty_event):
    """After registry refactor, Flaherty O/U parsing must produce identical rows."""
    result = parse_player_props(flaherty_event)
    ou_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_ou"]
    assert len(ou_rows) > 0
    for row in ou_rows:
        assert row["market_type"] == "pitching_strikeouts_ou"
        assert "pitching_strikeouts_ou" in row["market_group_key"]
        assert row["side"] in ("OVER", "UNDER")
        assert row["line"] is not None


def test_regression_flaherty_yn_identical(flaherty_event):
    """After registry refactor, Flaherty YN parsing must produce identical rows."""
    result = parse_player_props(flaherty_event)
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]
    assert len(yn_rows) > 0
    for row in yn_rows:
        assert row["market_type"] == "pitching_strikeouts_yn"
        assert "pitching_strikeouts_yn" in row["market_group_key"]
        assert row["side"] in ("YES", "NO")
        assert row["line"] is None


def test_regression_group_key_format():
    """Group key format is unchanged after refactor."""
    from src.player_prop_parser import _build_group_key, _build_yn_group_key
    assert _build_group_key("EVT1", "P1", 5.5, 0, "OVER") == "EVT1|P1|pitching_strikeouts_ou|game|5.5"
    assert _build_group_key("EVT1", "P1", 5.5, 1, "OVER") == "EVT1|P1|pitching_strikeouts_ou|game|5.5_alt"
    assert _build_yn_group_key("EVT1", "P1") == "EVT1|P1|pitching_strikeouts_yn|game"


def test_regression_backward_compat_imports():
    """All original imports still work after refactor."""
    from src.player_prop_parser import (
        STAT_ID, PERIOD, BET_TYPE_OU, BET_TYPE_YN,
        SIDE_OVER, SIDE_UNDER, SIDE_YES, SIDE_NO,
        _SIDE_MAP, parse_player_props,
        _is_pitching_k_ou, _is_pitching_k_yn,
        _build_group_key, _build_yn_group_key,
        _extract_side, _resolve_line, _extract_player_name_from_market,
    )
    assert STAT_ID == "pitching_strikeouts"
    assert PERIOD == "game"
    assert BET_TYPE_OU == "ou"
    assert BET_TYPE_YN == "yn"
    assert SIDE_OVER == "OVER"
    assert SIDE_UNDER == "UNDER"
    assert SIDE_YES == "YES"
    assert SIDE_NO == "NO"
    assert _is_pitching_k_ou("pitching_strikeouts-X-game-ou-over") is True
    assert _is_pitching_k_yn("pitching_strikeouts-X-game-yn-yes") is True


def test_regression_analysis_unaffected(flaherty_event):
    """Analysis results are identical after parser refactor."""
    from src.player_prop_analysis import analyze_prop_group, analyze_yn_group
    result = parse_player_props(flaherty_event)
    ou_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_ou"]
    yn_rows = [r for r in result.odds_rows if r["market_type"] == "pitching_strikeouts_yn"]

    # Build O/U groups the same way scanner does
    ou_groups = {}
    for row in ou_rows:
        key = row["market_group_key"]
        if key not in ou_groups:
            ou_groups[key] = {"over": {}, "under": {}, "line": row["line"]}
        ou_groups[key][row["side"].lower()][row["sportsbook"]] = {
            "price": row["price"], "decimal_odds": row["decimal_odds"],
            "line": row["line"], "validation_status": row["validation_status"],
        }

    # Analyze each group
    for gkey, gdata in ou_groups.items():
        if gdata["over"] and gdata["under"]:
            analysis = analyze_prop_group(gkey, gdata["over"], gdata["under"])
            assert "books" in analysis
            assert "nv_prob_over" in analysis
            assert "vig_pct" in analysis

    # YN groups
    yn_groups = {}
    for row in yn_rows:
        key = row["market_group_key"]
        if key not in yn_groups:
            yn_groups[key] = {"yes": {}}
        if row["side"] == "YES":
            yn_groups[key]["yes"][row["sportsbook"]] = {
                "price": row["price"], "decimal_odds": row["decimal_odds"],
                "validation_status": row["validation_status"],
            }

    for gkey, gdata in yn_groups.items():
        if gdata["yes"]:
            analysis = analyze_yn_group(gkey, gdata["yes"])
            assert "books" in analysis
            assert "decimal_odds_advantage" in analysis["books"][0]
