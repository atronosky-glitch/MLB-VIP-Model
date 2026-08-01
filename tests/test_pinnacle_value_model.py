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


# ── Phase 18C: Pinnacle + alt-line diagnostics ─────────────────────

class TestPinnacleDiagnostics:
    """Per-group diagnostics metadata + rejection reasons (logging only)."""

    def test_diagnostics_key_present_and_populated(self):
        over, under = _pinny_group()
        result = analyze_prop_group("e|p1|total_bases|game|5.5", over, under)
        d = result["diagnostics"]
        assert d["player"] == "p1"
        assert d["market"] == "total_bases"
        assert d["line"] == 5.5
        assert d["total_books"] == 5
        assert d["n_comparison_books"] == 4
        assert d["pinnacle_present"] is True
        assert d["pinnacle_both_sides"] is True
        assert d["pinnacle_reference_used"] is True
        assert d["fallback_used"] is False
        assert d["pinnacle_over_price"] == -120
        assert d["pinnacle_under_price"] == 100
        assert d["pinnacle_fair_over"] == pytest.approx(result["nv_prob_over"], abs=1e-6)
        assert d["pinnacle_fair_under"] == pytest.approx(result["nv_prob_under"], abs=1e-6)
        assert d["pinnacle_books"] == ["pinnacle"]
        assert d["official_approved"] is False  # all -110 → no approval
        assert d["rejection_reason"] == "no_positive_edge"

    def test_rejection_reason_approved(self):
        over = {"pinnacle": _price(-120), "fanduel": _price(110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        under = {"pinnacle": _price(100), "fanduel": _price(-110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        r = analyze_prop_group("e|p1|total_bases|game|5.5", over, under)
        d = r["diagnostics"]
        assert d["rejection_reason"] == "approved"
        assert d["official_approved"] is True
        assert d["best_side"] == "OVER"
        assert d["best_sportsbook"] == "fanduel"
        assert d["best_ev_pct"] > 0
        assert d["best_pinnacle_ev"] is not None
        assert d["best_pinnacle_prob_edge"] is not None

    def test_rejection_reason_ev_threshold_failed(self):
        # OVER +95 → EV ~1.7% < MIN_PINNACLE_EV (4%)
        over = {"pinnacle": _price(-120), "fanduel": _price(95), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        under = {"pinnacle": _price(100), "fanduel": _price(-110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        r = analyze_prop_group("e|p1|total_bases|game|5.5", over, under)
        assert r["diagnostics"]["rejection_reason"] == "ev_threshold_failed"

    def test_rejection_reason_prob_edge_threshold_failed(self):
        # OVER +100 → EV ~4.3% passes, but prob edge ~2.2% < MIN (2.5%)
        over = {"pinnacle": _price(-120), "fanduel": _price(100), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        under = {"pinnacle": _price(100), "fanduel": _price(-110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        r = analyze_prop_group("e|p1|total_bases|game|5.5", over, under)
        assert r["diagnostics"]["rejection_reason"] == "prob_edge_threshold_failed"

    def test_rejection_reasons_missing_and_insufficient(self):
        # missing entirely → fallback displayed, never official
        over = {"b0": _price(120), "b1": _price(-110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        under = {"b0": _price(-110), "b1": _price(-110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        r = analyze_prop_group("e|p1|total_bases|game|5.5", over, under)
        d = r["diagnostics"]
        assert d["pinnacle_present"] is False
        assert d["fallback_used"] is True
        assert d["rejection_reason"] == "missing_pinnacle"

        # too few books → never reaches full analysis
        r = analyze_prop_group("e|p1|total_bases|game|5.5", {"b1": _price(-110)}, {"b1": _price(-110)})
        assert r["diagnostics"]["rejection_reason"] == "insufficient_comparison_books"
        assert r["diagnostics"]["total_books"] == 1

    def test_rejection_reason_one_side_and_line_mismatch(self):
        # only Over side on Pinnacle
        over = {"pinnacle": _price(-120), "b1": _price(-110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        under = {"b1": _price(-110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        r = analyze_prop_group("e|p1|total_bases|game|5.5", over, under)
        assert r["diagnostics"]["rejection_reason"] == "pinnacle_missing_opposite_side"

        # Pinnacle present but only on a different line (6.5 vs 5.5)
        over = {"b1": _price(-110),
                "pinnacle": {"price": -120, "decimal_odds": round(american_to_decimal(-120), 4), "line": 6.5},
                "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        under = {"b1": _price(-110),
                 "pinnacle": {"price": 100, "decimal_odds": round(american_to_decimal(100), 4), "line": 6.5},
                 "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        r = analyze_prop_group("e|p1|total_bases|game|5.5", over, under)
        d = r["diagnostics"]
        assert d["pinnacle_present"] is False
        assert d["pinnacle_books"] == ["pinnacle"]
        assert d["rejection_reason"] == "pinnacle_line_mismatch"

    def test_rejection_reason_model_disabled(self):
        orig = cfg.USE_PINNACLE_VALUE_MODEL
        cfg.USE_PINNACLE_VALUE_MODEL = False
        try:
            over, under = _pinny_group()
            r = analyze_prop_group("e|p1|total_bases|game|5.5", over, under)
            d = r["diagnostics"]
            assert d["rejection_reason"] == "pinnacle_model_disabled"
            assert d["fallback_used"] is True
            assert d["pinnacle_both_sides"] is True
        finally:
            cfg.USE_PINNACLE_VALUE_MODEL = orig

    def test_empty_result_has_diagnostics(self):
        r = analyze_prop_group("e|p1|total_bases|game|5.5", {}, {})
        assert r["diagnostics"]["rejection_reason"] == "insufficient_comparison_books"
        assert r["diagnostics"]["total_books"] == 0


class TestScannerPinnacleDiagnostics:
    """Scanner-side summary counters, fragmentation log, and result key."""

    def test_accumulate_summary(self):
        from src.player_prop_scanner import (
            _new_pinnacle_summary, _accumulate_pinnacle_summary,
        )
        over = {"pinnacle": _price(-120), "fanduel": _price(110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        under = {"pinnacle": _price(100), "fanduel": _price(-110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        r1 = analyze_prop_group("e|p1|total_bases|game|5.5", over, under)
        over2 = {"b0": _price(120), "b1": _price(-110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        under2 = {"b0": _price(-110), "b1": _price(-110), "b2": _price(-110), "b3": _price(-110), "b4": _price(-110)}
        r2 = analyze_prop_group("e|p1|total_bases|game|5.5", over2, under2)

        s = _new_pinnacle_summary()
        _accumulate_pinnacle_summary(s, r1)
        _accumulate_pinnacle_summary(s, r2)
        assert s["total_groups"] == 2
        assert s["pinnacle_exact_match"] == 1
        assert s["pinnacle_reference_used"] == 1
        assert s["official_approved"] == 1
        assert s["pinnacle_missing"] == 1
        assert s["fallback_lean"] == 1

    def test_accumulate_summary_fallback_lean_excludes_insufficient_books(self):
        from src.player_prop_scanner import (
            _new_pinnacle_summary, _accumulate_pinnacle_summary,
        )
        over = {"b0": _price(120), "b1": _price(-110), "b2": _price(-110), "b3": _price(-110)}
        under = {"b0": _price(-110), "b1": _price(-110), "b2": _price(-110), "b3": _price(-110)}
        r = analyze_prop_group("e|p1|total_bases|game|5.5", over, under)
        assert r["diagnostics"]["fallback_used"] is True
        assert r["diagnostics"]["rejection_reason"] == "insufficient_comparison_books"

        s = _new_pinnacle_summary()
        _accumulate_pinnacle_summary(s, r)
        assert s["total_groups"] == 1
        assert s["insufficient_comparison_books"] == 1
        assert s["fallback_lean"] == 0

    def test_accumulate_summary_handles_no_diagnostics_key(self):
        from src.player_prop_scanner import (
            _new_pinnacle_summary, _accumulate_pinnacle_summary,
        )
        s = _new_pinnacle_summary()
        _accumulate_pinnacle_summary(s, {"market_quality": "VALID_MARKET"})
        assert s["total_groups"] == 1
        assert s["pinnacle_missing"] == 0

    def test_line_fragmentation_log(self, caplog):
        from src.player_prop_scanner import _log_line_fragmentation
        groups = {
            "k1": {"over": {"pinnacle": _price(-120), "b1": _price(-110)},
                   "under": {"pinnacle": _price(100), "b1": _price(-110)},
                   "line": 5.5, "player_id": "p1", "market_type": "total_bases"},
            "k2": {"over": {"fd": _price(110), "b2": _price(-110)},
                   "under": {"fd": _price(-110), "b2": _price(-110)},
                   "line": 6.5, "player_id": "p1", "market_type": "total_bases"},
        }
        with caplog.at_level("DEBUG", logger="src.player_prop_scanner"):
            _log_line_fragmentation(groups)
        frag = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("LINE_FRAGMENTATION")]
        assert len(frag) == 2
        assert "line=5.5" in frag[0] and "pinnacle_on_line=True" in frag[0]
        assert "line=6.5" in frag[1] and "pinnacle_on_line=False" in frag[1]
        assert "player=p1" in frag[0] and "market=total_bases" in frag[0]

    def test_pinnacle_summary_log(self, caplog):
        from src.player_prop_scanner import (
            _new_pinnacle_summary, _log_pinnacle_summary,
        )
        with caplog.at_level("INFO", logger="src.player_prop_scanner"):
            _log_pinnacle_summary(_new_pinnacle_summary())
        msgs = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("PINNACLE_SUMMARY")]
        assert len(msgs) == 1
        assert "total_groups=0" in msgs[0]
        assert "official_approved=0" in msgs[0]

    def test_run_scan_result_includes_pinnacle_diagnostics(self):
        from unittest import mock
        from src.player_prop_scanner import run_scan
        from src.player_prop_parser import ParsedPlayerPropResult

        event = {
            "eventID": "E1",
            "teams": {"home": {"name": "Home"}, "away": {"name": "Away"}},
            "status": {"startsAt": "2099-01-01T00:00:00Z"},
            "odds": {},
        }
        key = "E1|p1|pitching_strikeouts_ou|game|5.5"
        odds_rows = []
        # Pinnacle -120/+100 + fanduel OVER +110 (approved) + 3 neutral books
        book_prices = {"pinnacle": (-120, 100), "fanduel": (110, -110),
                       "b2": (-110, -110), "b3": (-110, -110), "b4": (-110, -110)}
        for book, (o, u) in book_prices.items():
            odds_rows.append({
                "event_id": "E1", "odd_id": f"o-{book}", "sportsbook": book,
                "player_id": "p1", "player_name": "Player One",
                "team_id": "", "team_name": "",
                "market_type": "pitching_strikeouts_ou",
                "market_group_key": key, "side": "OVER", "line": 5.5,
                "price": o, "decimal_odds": round(american_to_decimal(o), 4),
                "is_alt_line": 0, "available": 1, "validation_status": "VALID",
                "mapping_confidence": "HIGH", "mapping_method": "test",
                "validation_reason": "OK", "captured_at": "", "observation_time": "",
            })
            odds_rows.append({
                "event_id": "E1", "odd_id": f"u-{book}", "sportsbook": book,
                "player_id": "p1", "player_name": "Player One",
                "team_id": "", "team_name": "",
                "market_type": "pitching_strikeouts_ou",
                "market_group_key": key, "side": "UNDER", "line": 5.5,
                "price": u, "decimal_odds": round(american_to_decimal(u), 4),
                "is_alt_line": 0, "available": 1, "validation_status": "VALID",
                "mapping_confidence": "HIGH", "mapping_method": "test",
                "validation_reason": "OK", "captured_at": "", "observation_time": "",
            })

        with mock.patch("src.player_prop_scanner.get_connection") as mock_gc, \
             mock.patch("src.player_prop_scanner.create_run",
                        return_value="test-run-id"), \
             mock.patch("src.player_prop_scanner.finish_run"), \
             mock.patch("src.player_prop_scanner.SportsGameOddsClient") as mock_cls, \
             mock.patch("src.player_prop_scanner.parse_player_props") as mock_parse:

            mock_gc.return_value = mock.MagicMock()
            mock_cls.return_value.get_events.return_value = ({"data": [event]}, False)
            mock_parse.return_value = ParsedPlayerPropResult(
                odds_rows=odds_rows, audit_rows=[],
            )
            result = run_scan(mode="all", market="all", market_form="all", limit=25)

        diag = result["pinnacle_diagnostics"]
        assert diag["total_groups"] == 1
        assert diag["pinnacle_exact_match"] == 1
        assert diag["pinnacle_reference_used"] == 1
        assert diag["official_approved"] == 1
        assert diag["pinnacle_missing"] == 0


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

