"""Verify Stage 3: market analysis engine."""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.market_analysis import (
    american_to_probability,
    american_to_decimal,
    probability_to_american,
    decimal_to_american,
    remove_vig,
    vig_percentage,
    consensus_price,
    expected_value,
    better_price,
    best_price,
    worst_price,
    analyze_two_way_market,
    analyze_side,
    find_slow_books,
    compute_clv,
    _filter_approved,
)
from src.validation_constants import (
    STATUS_VALID,
    STATUS_CONFIRMED,
    STATUS_POSSIBLE_MAPPING_ERROR,
    APPROVED_STATUSES,
)


# ── American odds conversion ────────────────────────────────────


def test_american_to_decimal():
    """Decimal odds conversion."""
    assert abs(american_to_decimal(-110) - 1.9091) < 0.01
    assert american_to_decimal(150) == 2.5
    assert american_to_decimal(100) == 2.0
    assert american_to_decimal(-100) == 2.0


def test_american_to_probability_favorite():
    """-110 -> ~0.524"""
    prob = american_to_probability(-110)
    assert abs(prob - 0.5238) < 0.01


def test_american_to_probability_underdog():
    """+150 -> 0.4"""
    prob = american_to_probability(150)
    assert prob == 0.4


def test_american_to_probability_even():
    """+100 -> 0.5"""
    prob = american_to_probability(100)
    assert prob == 0.5
    prob = american_to_probability(-100)
    assert prob == 0.5


def test_roundtrip():
    """Converting back and forth should preserve value."""
    for odds in [-500, -300, -110, 100, 150, 300, 500]:
        prob = american_to_probability(odds)
        back = probability_to_american(prob)
        assert abs(abs(back) - abs(odds)) <= 1, f"Failed roundtrip for {odds}"

    for dec in [1.1, 1.5, 2.0, 3.0, 5.0]:
        odds = decimal_to_american(dec)
        back = american_to_decimal(odds)
        assert abs(back - dec) < 0.01, f"Failed roundtrip for decimal {dec}"


# ── Price comparison ───────────────────────────────────────────


def test_better_price():
    """A higher payout is always better."""
    assert better_price(125, -169)
    assert not better_price(-169, 125)
    assert better_price(-110, -125)
    assert better_price(150, 130)


def test_best_price_positive():
    """Among underdog odds, highest positive is best."""
    assert best_price([130, 140, 143, 135]) == 143


def test_best_price_negative():
    """Among favorite odds, least negative is best."""
    assert best_price([-110, -115, -125, -120]) == -110


def test_best_price_mixed():
    """Always pick highest decimal."""
    assert best_price([-169, 136, 143, 130]) == 143


def test_worst_price():
    """Among all odds, lowest decimal is worst."""
    assert worst_price([-169, 136, 143, 130]) == -169
    assert worst_price([-110, -115, -125]) == -125


# ── No-vig / vig removal ────────────────────────────────────────


def test_remove_vig_even():
    """Even odds -> 50/50 with no vig adjustment."""
    p_a, p_b = remove_vig(100, 100)
    assert abs(p_a - 0.5) < 0.001
    assert abs(p_b - 0.5) < 0.001


def test_remove_vig_standard():
    """-110 / -110 (standard vig)."""
    p_a, p_b = remove_vig(-110, -110)
    assert abs(p_a + p_b - 1.0) < 0.001
    assert abs(p_a - 0.5) < 0.001


def test_remove_vig_asymmetric():
    """-150 / +130 should sum to 1.0 after vig removal."""
    p_a, p_b = remove_vig(-150, 130)
    assert abs(p_a + p_b - 1.0) < 0.001


def test_vig_percentage():
    """-110 / -110 has ~4.76% vig."""
    vig = vig_percentage(-110, -110)
    assert abs(vig - 4.76) < 0.1


def test_vig_asymmetric():
    vig = vig_percentage(-150, 130)
    assert vig > 0
    assert vig < 10


# ── Expected value ──────────────────────────────────────────────


def test_expected_value_positive():
    """A 42.5% fair probability at +150 should have positive EV."""
    ev = expected_value(0.425, 150)
    expected = 0.425 * american_to_decimal(150) - 1
    assert abs(ev - expected) < 0.001
    assert ev > 0.05  # about 6.25%


def test_expected_value_negative():
    """A 42.5% fair probability at -169 should have strongly negative EV."""
    ev = expected_value(0.425, -169)
    assert ev < -0.30, f"Expected EV < -30%, got {ev:.4%}"


def test_expected_value_break_even():
    """Fair probability exactly equal to implied probability = 0 EV."""
    dec = american_to_decimal(-110)
    imp_prob = 1.0 / dec
    ev = expected_value(imp_prob, -110)
    assert abs(ev) < 0.001


# ── Consensus ────────────────────────────────────────────────────


def test_consensus_single():
    assert consensus_price([-110]) == -110


def test_consensus_uniform():
    assert consensus_price([-110, -110, -110]) == -110


def test_consensus_mixed():
    """Average of -110 and +110 should be close to fair (even money)."""
    cons = consensus_price([-110, 110])
    assert abs(abs(cons) - 100) <= 25


def test_consensus_empty():
    assert consensus_price([]) == 0


# ── analyze_two_way_market ──────────────────────────────────────


def test_two_way_basic():
    """Basic sanity check."""
    away = {"fanduel": 120, "draftkings": 115, "caesars": 125}
    home = {"fanduel": -140, "draftkings": -135, "caesars": -145}
    result = analyze_two_way_market(away, home)
    assert result["side_a"]["n_books"] == 3
    assert result["side_b"]["n_books"] == 3
    assert abs(result["nv_prob_a"] + result["nv_prob_b"] - 1.0) < 0.001
    assert result["vig_pct"] > 0
    assert len(result["books"]) == 6
    assert result["best_ev"] is not None or all(b["ev"] <= 0 for b in result["books"])


def test_two_way_best_ev_correct_sign():
    """Best EV should be positive if any book offers value."""
    away = {"fanduel": -120, "draftkings": -115, "caesars": -125}
    home = {"fanduel": 100, "draftkings": -105, "caesars": 105}
    result = analyze_two_way_market(away, home)
    if result["best_ev"]:
        assert result["best_ev"]["ev"] > 0


# ── Side analysis ────────────────────────────────────────────────


def test_analyze_side_basic():
    """For favorite odds, best is least negative."""
    prices = {"fanduel": -120, "draftkings": -115, "caesars": -125}
    result = analyze_side(prices)
    assert result["n_books"] == 3
    assert result["best_price"] == -115
    assert result["best_book"] == "draftkings"
    assert result["worst_book"] == "caesars"


def test_analyze_empty_side():
    result = analyze_side({})
    assert result["n_books"] == 0
    assert result["consensus_price"] == 0


def test_analyze_single_side():
    result = analyze_side({"fanduel": -110})
    assert result["n_books"] == 1
    assert result["best_price"] == -110
    assert result["worst_price"] == -110


# ── Slow book detection ──────────────────────────────────────────


def test_find_slow_books():
    """Books that move less than the market are 'slow'."""
    morning = {"a": -110, "b": -115, "c": -120}
    pregame = {"a": -120, "b": -118, "c": -130}
    slow = find_slow_books(morning, pregame)
    assert len(slow) > 0
    assert slow[0]["sportsbook"] == "b"


def test_slow_books_empty():
    assert find_slow_books({}, {"a": -110}) == []
    assert find_slow_books({"a": -110}, {}) == []


# ── CLV ──────────────────────────────────────────────────────────


def test_clv_negative():
    """Got -110, closed at -120 -> closing pays less -> CLV negative."""
    clv = compute_clv(-110, -120)
    assert clv < 0


def test_clv_positive():
    """Got -120, closed at -110 -> closing pays more -> CLV positive."""
    clv = compute_clv(-120, -110)
    assert clv > 0


# ── Validation-aware filtering tests ──────────────────────────────


def test_filter_approved_pass_all():
    """Without a validation_map, all records pass."""
    prices = {"fanduel": 120, "draftkings": 115}
    result = _filter_approved(prices, None)
    assert result == prices


def test_filter_approved_excludes_possible_mapping_error():
    """POSSIBLE_MAPPING_ERROR records must be excluded."""
    prices = {"fanduel": 120, "draftkings": 115, "betmgm": -169}
    vmap = {"fanduel": STATUS_VALID, "draftkings": STATUS_CONFIRMED, "betmgm": STATUS_POSSIBLE_MAPPING_ERROR}
    result = _filter_approved(prices, vmap)
    assert "betmgm" not in result
    assert len(result) == 2


def test_filter_approved_excludes_invalid_mapping():
    prices = {"book_a": -110, "book_b": -120}
    vmap = {"book_a": STATUS_VALID, "book_b": "INVALID_MAPPING"}
    result = _filter_approved(prices, vmap)
    assert "book_b" not in result


def test_filter_approved_keeps_valid():
    """VALID, CONFIRMED, VERIFIED all pass."""
    prices = {"a": -110, "b": -120, "c": -130}
    vmap = {"a": STATUS_VALID, "b": STATUS_CONFIRMED, "c": "VERIFIED"}
    result = _filter_approved(prices, vmap)
    assert result == prices


def test_analyze_side_with_validation_map():
    """analyze_side must exclude unapproved books when validation_map is given."""
    prices = {"fanduel": 120, "draftkings": 115, "betmgm": -169}
    vmap = {"fanduel": STATUS_VALID, "draftkings": STATUS_VALID, "betmgm": STATUS_POSSIBLE_MAPPING_ERROR}
    result = analyze_side(prices, validation_map=vmap)
    assert result["n_books"] == 2
    assert result["best_book"] != "betmgm"
    assert result["worst_book"] != "betmgm"


def test_analyze_side_without_validation_map_includes_all():
    """Without validation_map, all records (including flagged) are analysed."""
    prices = {"fanduel": 120, "draftkings": 115, "betmgm": -169}
    result = analyze_side(prices)
    assert result["n_books"] == 3


def test_analyze_two_way_excludes_flagged_from_consensus():
    """Flagged records must not affect consensus in analyze_two_way_market."""
    # TB @ TOR scenario: BetMGM has away=-169, home=+140
    # All other books have away=+positive, home=-negative
    away = {"fanduel": 140, "draftkings": 136, "caesars": 143,
            "pointsbet": 138, "williamhill": 130, "bovada": 145,
            "unibet": 135, "espnbet": 132, "betmgm": -169}
    home = {"fanduel": -165, "draftkings": -160, "caesars": -170,
            "pointsbet": -162, "williamhill": -150, "bovada": -175,
            "unibet": -158, "espnbet": -155, "betmgm": 140}

    vmap_away = {b: STATUS_VALID for b in away}
    vmap_away["betmgm"] = STATUS_POSSIBLE_MAPPING_ERROR
    vmap_home = {b: STATUS_VALID for b in home}
    vmap_home["betmgm"] = STATUS_POSSIBLE_MAPPING_ERROR

    result_with_filter = analyze_two_way_market(
        away, home,
        validation_map_a=vmap_away, validation_map_b=vmap_home,
    )
    result_no_filter = analyze_two_way_market(away, home)

    # With filter: away consensus should be positive (underdog)
    with_filter_consensus = result_with_filter["side_a"]["consensus_price"]
    no_filter_consensus = result_no_filter["side_a"]["consensus_price"]

    # The filtered consensus should be higher (more positive / less negative)
    assert with_filter_consensus > no_filter_consensus, \
        f"Filtering BetMGM should raise away consensus: filtered={with_filter_consensus}, unfiltered={no_filter_consensus}"

    # BetMGM must not appear in filtered books
    betmgm_in_books = [
        b for b in result_with_filter["books"]
        if b["sportsbook"] == "betmgm"
    ]
    assert len(betmgm_in_books) == 0, \
        "BetMGM should not appear in filtered result books"

    # No best_ev should be from betmgm
    if result_with_filter["best_ev"]:
        assert result_with_filter["best_ev"]["sportsbook"] != "betmgm", \
            "BetMGM cannot be best EV when filtered"

    # With filter: n_books should be 8 (the 9 minus BetMGM)
    assert result_with_filter["side_a"]["n_books"] == 8
    assert result_with_filter["side_b"]["n_books"] == 8


def test_unverified_excluded():
    """UNVERIFIED status must be excluded."""
    prices = {"fanduel": 120, "unknown_book": 130}
    vmap = {"fanduel": STATUS_VALID, "unknown_book": "UNVERIFIED"}
    result = _filter_approved(prices, vmap)
    assert "unknown_book" not in result
    assert len(result) == 1


def test_approved_statuses_contains_valid_confirmed_verified():
    assert STATUS_VALID in APPROVED_STATUSES
    assert STATUS_CONFIRMED in APPROVED_STATUSES
    assert "VERIFIED" in APPROVED_STATUSES
    assert STATUS_POSSIBLE_MAPPING_ERROR not in APPROVED_STATUSES
