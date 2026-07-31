"""Tests for the Pinnacle-first sharp value model.

Covers the odds/probability helpers, Pinnacle book matching, the
Pinnacle reference branch of ``analyze_prop_group``, and the config
flags that control fallback/strict behaviour.

All tests are deterministic — synthetic prices, no API access.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src import prop_config as cfg
from src.player_prop_analysis import (
    analyze_prop_group,
    american_to_implied_prob,
    calculate_ev,
    calculate_no_vig_probs,
    is_pinnacle_book,
)
from src.market_analysis import american_to_decimal
from src.prop_config import (
    BET_STATUS_NO_EDGE,
    BET_STATUS_POSITIVE,
    BET_STATUS_STRONG,
    MARKET_QUALITY_VALID,
)


def _price(odds: int) -> dict:
    return {"price": odds, "decimal_odds": round(american_to_decimal(odds), 4), "line": 5.5}


def _books_prices(*pairs) -> tuple[dict, dict]:
    over = {}
    under = {}
    for i, (o, u) in enumerate(pairs):
        over[f"book{i}"] = _price(o)
        under[f"book{i}"] = _price(u)
    return over, under


def _pinny_group() -> tuple[dict, dict]:
    """Pinnacle -120/+100 with four recreational books at -110/-110."""
    over = {"pinnacle": _price(-120)}
    under = {"pinnacle": _price(100)}
    for i in range(4):
        over[f"book{i}"] = _price(-110)
        under[f"book{i}"] = _price(-110)
    return over, under


# ── Helper functions ───────────────────────────────────────────────

class TestOddsHelpers:
    def test_american_to_implied_prob_positive(self):
        assert american_to_implied_prob(100) == pytest.approx(0.5)
        assert american_to_implied_prob(200) == pytest.approx(1 / 3)

    def test_american_to_implied_prob_negative(self):
        assert american_to_implied_prob(-110) == pytest.approx(0.5238, abs=0.001)
        assert american_to_implied_prob(-200) == pytest.approx(2 / 3)

    def test_calculate_no_vig_probs_even_market(self):
        fair_over, fair_under = calculate_no_vig_probs(-110, -110)
        assert fair_over == pytest.approx(0.5, abs=0.001)
        assert fair_under == pytest.approx(0.5, abs=0.001)

    def test_calculate_no_vig_probs_known_pair(self):
        fair_over, fair_under = calculate_no_vig_probs(-120, 100)
        raw_over = 120 / 220
        raw_under = 100 / 200
        total = raw_over + raw_under
        assert fair_over == pytest.approx(raw_over / total, abs=0.001)
        assert fair_under == pytest.approx(raw_under / total, abs=0.001)
        assert fair_over + fair_under == pytest.approx(1.0, abs=0.001)

    def test_calculate_ev_positive(self):
        # true prob 0.52 at +110 (2.1) → EV = 0.092
        assert calculate_ev(0.52, 110) == pytest.approx(0.092, abs=0.0001)

    def test_calculate_ev_negative(self):
        # true prob 0.45 at -110 (1.9091) → EV = -0.1409
        assert calculate_ev(0.45, -110) == pytest.approx(-0.1409, abs=0.001)


class TestPinnacleBookMatching:
    def test_exact_lowercase(self):
        assert is_pinnacle_book("pinnacle")

    def test_case_insensitive(self):
        assert is_pinnacle_book("Pinnacle")
        assert is_pinnacle_book("PINNACLE")

    def test_variants(self):
        assert is_pinnacle_book("Pinnacle Sports")
        assert is_pinnacle_book("pinny")
        assert is_pinnacle_book("Pinnacle Sportsbook")

    def test_non_pinnacle_books(self):
        assert not is_pinnacle_book("draftkings")
        assert not is_pinnacle_book("fanduel")
        assert not is_pinnacle_book("")
        assert not is_pinnacle_book(None)


# ── Pinnacle reference branch ──────────────────────────────────────

class TestPinnacleReference:
    def test_pinnacle_is_used_as_reference(self):
        over, under = _pinny_group()
        over["fanduel"] = _price(110)  # +110 clear of Pinnacle no-vig 52.2%
        under["fanduel"] = _price(-110)
        result = analyze_prop_group("g1", over, under)
        assert result["market_quality"] == MARKET_QUALITY_VALID
        assert result["recommendation"] == "BET"
        # Pinnacle never appears as a target book
        assert all(b["sportsbook"] != "pinnacle" for b in result["books"])
        assert result["n_paired_books"] == 6

    def test_no_vig_fair_probabilities(self):
        over, under = _pinny_group()
        result = analyze_prop_group("g1", over, under)
        # -120 / +100 no-vig
        expected_over = (120 / 220) / ((120 / 220) + 0.5)
        assert result["nv_prob_over"] == pytest.approx(expected_over, abs=0.001)
        assert result["nv_prob_under"] == pytest.approx(1 - expected_over, abs=0.001)

    def test_pinnacle_approved_when_both_thresholds_pass(self):
        # One book offers OVER at +110 → EV and prob edge both clear thresholds
        over = {"pinnacle": _price(-120), "fanduel": _price(110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
        under = {"pinnacle": _price(100), "fanduel": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
        result = analyze_prop_group("g1", over, under)
        fanduel = next(b for b in result["books"] if b["sportsbook"] == "fanduel" and b["side"] == "OVER")
        assert fanduel["pinnacle_approved"] is True
        assert fanduel["pinnacle_ev"] is not None
        assert fanduel["pinnacle_prob_edge"] > 0
        assert fanduel["pinnacle_fair_prob"] == pytest.approx(result["nv_prob_over"], abs=0.001)

    def test_not_approved_when_thresholds_miss(self):
        over, under = _pinny_group()  # all recreational books at -110
        result = analyze_prop_group("g1", over, under)
        for b in result["books"]:
            assert b["pinnacle_approved"] is False
            assert b["pinnacle_ev"] < cfg.MIN_PINNACLE_EV * 100 or b["pinnacle_prob_edge"] < cfg.MIN_PINNACLE_PROB_EDGE * 100

    def test_pinnacle_variant_detected(self):
        over = {"Pinnacle Sports": _price(-120), "fanduel": _price(110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
        under = {"Pinnacle Sports": _price(100), "fanduel": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
        result = analyze_prop_group("g1", over, under)
        assert result["recommendation"] == "BET"
        assert all("pinnacle" not in b["sportsbook"].lower() for b in result["books"])

    def test_best_recreational_book_uses_highest_payout(self):
        over = {"pinnacle": _price(-120), "book1": _price(-140), "book2": _price(-120), "book3": _price(-110), "book4": _price(-110)}
        under = {"pinnacle": _price(100), "book1": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
        result = analyze_prop_group("g1", over, under)
        over_books = [b for b in result["books"] if b["side"] == "OVER"]
        best = max(over_books, key=lambda b: american_to_decimal(b["american_odds"]))
        assert best["american_odds"] == -110


# ── Fallback and config flags ──────────────────────────────────────

class TestFallbackBehaviour:
    def test_no_pinnacle_falls_back_to_loo(self):
        over, under = _books_prices((-110, -110), (-110, -110), (-110, -110), (-110, -110), (-110, -110))
        result = analyze_prop_group("g1", over, under)
        assert result["market_quality"] == MARKET_QUALITY_VALID
        for b in result["books"]:
            assert b["pinnacle_approved"] is None

    def test_req_pinnacle_blocks_fallback_official_but_displays(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        try:
            # book0 OVER +120 is well above the LOO median → STRONG if unrestricted
            over = {"book0": _price(120), "book1": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            under = {"book0": _price(-110), "book1": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            result = analyze_prop_group("g1", over, under)
            # Fallback opportunity is still displayed…
            assert result["best_ev"] is not None
            assert result["recommendation"] == "BET"
            # …but never official without Pinnacle approval
            assert result["best_ev"]["is_official"] is False
            assert result["official_count"] == 0
            assert all(b["is_official"] is False for b in result["books"])
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig

    def test_req_pinnacle_false_allows_fallback_official(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = False
        try:
            over = {"book0": _price(120), "book1": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            under = {"book0": _price(-110), "book1": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            result = analyze_prop_group("g1", over, under)
            assert result["recommendation"] == "BET"
            assert result["best_ev"] is not None
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig

    def test_model_disabled_uses_loo_even_with_pinnacle(self):
        orig = cfg.USE_PINNACLE_VALUE_MODEL
        cfg.USE_PINNACLE_VALUE_MODEL = False
        try:
            over, under = _pinny_group()
            result = analyze_prop_group("g1", over, under)
            # Pinnacle not skipped as a target when the model is disabled
            assert any(is_pinnacle_book(b["sportsbook"]) for b in result["books"])
            assert all(b["pinnacle_approved"] is None for b in result["books"])
        finally:
            cfg.USE_PINNACLE_VALUE_MODEL = orig

    def test_thresholds_are_configurable(self):
        orig_ev = cfg.MIN_PINNACLE_EV
        orig_edge = cfg.MIN_PINNACLE_PROB_EDGE
        cfg.MIN_PINNACLE_EV = 0.20
        cfg.MIN_PINNACLE_PROB_EDGE = 0.15
        try:
            over = {"pinnacle": _price(-120), "fanduel": _price(110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            under = {"pinnacle": _price(100), "fanduel": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            result = analyze_prop_group("g1", over, under)
            fanduel = next(b for b in result["books"] if b["sportsbook"] == "fanduel" and b["side"] == "OVER")
            # EV ~9.5% and edge ~4.5% no longer clear the raised thresholds
            assert fanduel["pinnacle_approved"] is False
        finally:
            cfg.MIN_PINNACLE_EV = orig_ev
            cfg.MIN_PINNACLE_PROB_EDGE = orig_edge

    def test_single_side_pinnacle_falls_back(self):
        over = {"pinnacle": _price(-120), "book1": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
        under = {"book1": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
        result = analyze_prop_group("g1", over, under)
        assert result["recommendation"] == "NO_BET"
        assert all(b["pinnacle_approved"] is None for b in result["books"])


# ── Phase 18B: Pinnacle required for official picks ────────────────

class TestPinnacleRequiredForOfficial:
    """Pinnacle approval gates official eligibility (REQUIRE flag explicit)."""

    def test_pinnacle_approved_book_is_official(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        try:
            over = {"pinnacle": _price(-120), "fanduel": _price(110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            under = {"pinnacle": _price(100), "fanduel": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            result = analyze_prop_group("g1", over, under)
            assert result["pinnacle_found"] is True
            assert result["pinnacle_reference_used"] is True
            fanduel = next(b for b in result["books"] if b["sportsbook"] == "fanduel" and b["side"] == "OVER")
            assert fanduel["pinnacle_approved"] is True
            assert fanduel["is_official"] is True
            assert result["official_count"] >= 1
            assert result["best_ev"]["is_official"] is True
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig

    def test_pinnacle_missing_blocks_official(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        try:
            over = {"book0": _price(120), "book1": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            under = {"book0": _price(-110), "book1": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            result = analyze_prop_group("g1", over, under)
            assert result["pinnacle_found"] is False
            assert result["pinnacle_reference_used"] is False
            assert result["best_ev"] is not None  # still displayed
            assert result["best_ev"]["is_official"] is False
            assert result["official_count"] == 0
            assert all(b["is_official"] is False for b in result["books"])
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig

    def test_pinnacle_threshold_fail_blocks_official(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        try:
            # OVER +100 beats Pinnacle no-vig prob (EV ~4.4%) but the probability
            # edge (2.2%) is below MIN_PINNACLE_PROB_EDGE → not approved.
            over = {"pinnacle": _price(-120), "fanduel": _price(100), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            under = {"pinnacle": _price(100), "fanduel": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            result = analyze_prop_group("g1", over, under)
            assert result["pinnacle_found"] is True
            assert result["pinnacle_reference_used"] is True
            fanduel = next(b for b in result["books"] if b["sportsbook"] == "fanduel" and b["side"] == "OVER")
            assert fanduel["pinnacle_approved"] is False
            assert fanduel["is_official"] is False
            assert result["best_ev"] is not None
            assert result["best_ev"]["is_official"] is False
            assert result["official_count"] == 0
            assert all(b["is_official"] is False for b in result["books"])
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig

    def test_fallback_still_displayed_but_not_official(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        try:
            over = {"book0": _price(120), "book1": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            under = {"book0": _price(-110), "book1": _price(-110), "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            result = analyze_prop_group("g1", over, under)
            # Opportunity still surfaces with a positive EV best entry…
            assert result["recommendation"] == "BET"
            assert result["best_ev"] is not None
            assert result["best_ev"]["ev_pct"] > 0
            # …but is never official.
            assert result["best_ev"]["is_official"] is False
            assert result["official_count"] == 0
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig

    def test_different_line_pinnacle_not_used_as_reference(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        try:
            over = {"book1": _price(-110),
                    "pinnacle": {"price": -120, "decimal_odds": round(american_to_decimal(-120), 4), "line": 6.5},
                    "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            under = {"book1": _price(-110),
                     "pinnacle": {"price": 100, "decimal_odds": round(american_to_decimal(100), 4), "line": 6.5},
                     "book2": _price(-110), "book3": _price(-110), "book4": _price(-110)}
            result = analyze_prop_group("g1", over, under)
            assert result["line"] == 5.5
            # Pinnacle at 6.5 must not be treated as the reference for 5.5
            assert result["pinnacle_found"] is False
            assert result["pinnacle_reference_used"] is False
            assert all(b["pinnacle_approved"] is None for b in result["books"])
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig


# ── Dashboard Score Calibration None-formatting regression ─────────

class TestScoreCalibrationDisplayGuard:
    def test_score_distribution_metrics_guard_against_none(self):
        """Score Calibration metrics must not f-string-format a None value."""
        src = (PROJECT_ROOT / "src" / "control_panel.py").read_text(encoding="utf-8")
        # The guards must exist for mean/median/stdev (these can be None when
        # there is no graded data yet).
        assert "_mean is not None else \"N/A\"" in src
        assert "_median is not None else \"N/A\"" in src
        assert "_stdev is not None else \"N/A\"" in src
        # And the calibration table must not format win rate / roi blindly.
        assert "if b.get(\"actual_win_rate\") is not None else \"N/A\"" in src
        assert "if b.get(\"roi\") is not None else \"N/A\"" in src

