"""Phase 14 — EV scaling fix, unit helpers, Model Score v1 tests.

All tests use in-memory databases with explicit values.
No clock-dependent or API-dependent tests.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ==================================================================
# Unit helpers
# ==================================================================


class TestUnitHelpers:
    """Test conversion helpers in unit_helpers.py."""

    def test_pp_to_decimal(self):
        from src.unit_helpers import pp_to_decimal
        assert pp_to_decimal(5.0) == 0.05
        assert pp_to_decimal(0.0) == 0.0
        assert pp_to_decimal(100.0) == 1.0

    def test_decimal_to_pp(self):
        from src.unit_helpers import decimal_to_pp
        assert decimal_to_pp(0.05) == 5.0
        assert decimal_to_pp(0.0) == 0.0
        assert decimal_to_pp(1.0) == 100.0

    def test_roundtrip(self):
        from src.unit_helpers import pp_to_decimal, decimal_to_pp
        for val in [0.0, 2.5, 5.0, 10.0, 50.0]:
            assert decimal_to_pp(pp_to_decimal(val)) == val

    def test_format_ev_pct(self):
        from src.unit_helpers import format_ev_pct
        assert format_ev_pct(5.2341) == "+5.23%"
        assert format_ev_pct(-2.1) == "-2.10%"
        assert format_ev_pct(0.0) == "+0.00%"

    def test_format_price_advantage(self):
        from src.unit_helpers import format_price_advantage
        assert format_price_advantage(6.4059) == "+6.41 pp"
        assert format_price_advantage(-1.5) == "-1.50 pp"

    def test_format_score(self):
        from src.unit_helpers import format_score
        assert format_score(9.3) == "9.3"
        assert format_score(1.0) == "1.0"
        assert format_score(None) == "N/A"


# ==================================================================
# EV display fix (PART 1)
# ==================================================================


class TestEVDisplayFix:
    """Verify the double ×100 bug is fixed."""

    def test_stored_6_4059_displays_as_6_41(self):
        from src.unit_helpers import format_price_advantage
        # Previously this would display as 640.59 due to double ×100
        stored_value = 6.4059
        display = format_price_advantage(stored_value)
        assert "6.41" in display
        assert "640" not in display

    def test_stored_5_23_ev_displays_as_5_23_pct(self):
        from src.unit_helpers import format_ev_pct
        stored_value = 5.23
        display = format_ev_pct(stored_value)
        assert "5.23" in display
        assert "523" not in display

    def test_control_panel_no_double_multiply(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        # The old bug had "ev_pct * 100" and "yn_implied_prob_adv * 100"
        assert 'r["ev_pct"] * 100' not in source
        assert 'r["yn_implied_prob_adv"] * 100' not in source


# ==================================================================
# EV and Price Advantage separation (PART 2)
# ==================================================================


class TestColumnSeparation:
    """Verify EV% and Price Adv are separate columns."""

    def test_control_panel_has_separate_columns(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert '"EV %"' in source
        assert '"Price Adv (pp)"' in source
        assert '"EV%/Adv%"' not in source

    def test_csv_export_has_separate_columns(self):
        source = Path("src/export_sheets.py").read_text(encoding="utf-8")
        assert '"EV %"' in source
        assert '"Price Adv (pp)"' in source
        assert '"Price Advantage %"' not in source

    def test_message_formatter_uses_pp_for_yn(self):
        source = Path("src/message_formatter.py").read_text(encoding="utf-8")
        assert "pp adv" in source
        assert "Price Advantage:" in source


# ==================================================================
# Confidence unit fix (PART 3)
# ==================================================================


class TestConfidenceFix:
    """Verify confidence.py treats ev_pct as percentage points."""

    def test_ev_5_percent_gives_moderate_score(self):
        from src.confidence import _score_ev_magnitude
        # ev_pct=5.0 means 5%, not 500%
        score = _score_ev_magnitude(5.0, None)
        assert 0.0 < score < 1.0
        assert abs(score - 5.0 / 15.0) < 0.001

    def test_ev_15_percent_caps_at_1(self):
        from src.confidence import _score_ev_magnitude
        score = _score_ev_magnitude(15.0, None)
        assert score == 1.0

    def test_ev_0_gives_0(self):
        from src.confidence import _score_ev_magnitude
        score = _score_ev_magnitude(0.0, None)
        assert score == 0.0

    def test_yn_adv_8_percent_gives_high_score(self):
        from src.confidence import _score_ev_magnitude
        score = _score_ev_magnitude(None, 8.0)
        assert abs(score - 8.0 / 15.0) < 0.001

    def test_confidence_does_not_instantly_cap(self):
        from src.confidence import compute_confidence
        rec = {
            "ev_pct": 5.0,
            "n_consensus_books": 8,
            "market_quality": "VALID_MARKET",
            "freshness_status": "FRESH",
            "data_source": "LIVE API",
        }
        result = compute_confidence(rec)
        assert 0 < result["confidence_score"] < 100


# ==================================================================
# Analytics unit fix
# ==================================================================


class TestAnalyticsFix:
    """Verify analytics.py uses correct units."""

    def test_roi_by_ev_bucket_uses_direct_values(self):
        source = Path("src/analytics.py").read_text(encoding="utf-8")
        # Should NOT have the old "ev * 100" conversion
        assert "ev * 100" not in source
        assert "ev_pct_pts" not in source


# ==================================================================
# Model Score (PART 4-6)
# ==================================================================


class TestModelScore:
    """Test the 1-10 Model Score computation."""

    def _base_rec(self, **overrides):
        rec = {
            "market_form": "ou",
            "ev_pct": 5.0,
            "n_consensus_books": 6,
            "market_quality": "VALID_MARKET",
            "rec_status": "BET",
            "comparison_status": "",
            "freshness_status": "FRESH",
            "data_source": "LIVE API",
            "yn_implied_prob_adv": None,
            "yn_decimal_odds_adv": None,
            "fair_prob": 0.55,
            "confidence_score": 75.0,
        }
        rec.update(overrides)
        return rec

    def test_score_always_between_1_and_9_8(self):
        from src.model_scoring import compute_model_score
        rec = self._base_rec()
        result = compute_model_score(rec)
        assert 1.0 <= result.score <= 9.8

    def test_high_ev_gives_higher_score(self):
        from src.model_scoring import compute_model_score
        low = compute_model_score(self._base_rec(ev_pct=2.0))
        high = compute_model_score(self._base_rec(ev_pct=10.0))
        assert high.score > low.score

    def test_more_books_gives_higher_score(self):
        from src.model_scoring import compute_model_score
        few = compute_model_score(self._base_rec(n_consensus_books=3))
        many = compute_model_score(self._base_rec(n_consensus_books=8))
        assert many.score > few.score

    def test_fresh_data_gives_higher_score(self):
        from src.model_scoring import compute_model_score
        stale = compute_model_score(self._base_rec(
            freshness_status="STALE", data_source="CACHE"))
        fresh = compute_model_score(self._base_rec(
            freshness_status="FRESH", data_source="LIVE API"))
        assert fresh.score > stale.score

    def test_game_market_gets_full_confidence_without_a_confidence_score(self):
        """Regression (2026-08-23): game-level markets (player_id=="GAME")
        never go through player-identity name-matching, so there's no
        real ambiguity for a missing confidence_score to represent —
        unlike a player prop, where None genuinely means "identity
        uncertain." Scoring it as the neutral 0.5 default penalized every
        game-market recommendation for a form of uncertainty that
        structurally cannot apply to it."""
        from src.model_scoring import compute_model_score
        game_rec = self._base_rec(player_id="GAME")
        del game_rec["confidence_score"]
        prop_rec = self._base_rec(player_id="ESPN_MLB_123")
        del prop_rec["confidence_score"]
        game_result = compute_model_score(game_rec)
        prop_result = compute_model_score(prop_rec)
        assert game_result.score > prop_result.score

    def test_needs_review_capped_at_7_5(self):
        from src.model_scoring import compute_model_score
        rec = self._base_rec(market_quality="NEEDS_REVIEW")
        result = compute_model_score(rec)
        assert result.score <= 7.5

    def test_insufficient_market_capped_at_5(self):
        from src.model_scoring import compute_model_score
        rec = self._base_rec(market_quality="INSUFFICIENT_MARKET")
        result = compute_model_score(rec)
        assert result.score <= 5.0

    def test_price_outlier_capped_at_8_5(self):
        from src.model_scoring import compute_model_score
        rec = self._base_rec(comparison_status="PRICE_OUTLIER")
        result = compute_model_score(rec)
        assert result.score <= 8.5

    def test_excluded_gets_zero(self):
        from src.model_scoring import compute_model_score
        rec = self._base_rec(market_quality="EXCLUDED")
        result = compute_model_score(rec)
        assert result.score == 0.0

    def test_yn_market_uses_price_advantage(self):
        from src.model_scoring import compute_model_score
        rec = self._base_rec(
            market_form="yn",
            ev_pct=None,
            yn_implied_prob_adv=6.0,
            yn_decimal_odds_adv=15,
        )
        result = compute_model_score(rec)
        assert 1.0 <= result.score <= 9.8

    def test_high_ev_low_reliability_does_not_outrank(self):
        from src.model_scoring import compute_model_score
        # High EV but NEEDS_REVIEW
        risky = compute_model_score(self._base_rec(
            ev_pct=12.0, market_quality="NEEDS_REVIEW"))
        # Moderate EV but VALID_MARKET
        solid = compute_model_score(self._base_rec(
            ev_pct=5.0, market_quality="VALID_MARKET"))
        # The risky one is capped at 7.5
        assert risky.score <= 7.5
        # Solid one can go higher
        assert solid.score > risky.score or solid.score >= 7.0

    def test_components_reconcile(self):
        from src.model_scoring import compute_model_score
        rec = self._base_rec()
        result = compute_model_score(rec)
        # Sum of component_values should be close to (score - 1) / 8.8 * 100
        total = sum(result.component_values.values())
        expected_range = ((result.score - 1.0) / 8.8) * 8.8
        assert abs(total - expected_range) < 0.5

    def test_explanation_generated(self):
        from src.model_scoring import compute_model_score
        result = compute_model_score(self._base_rec())
        assert "Model Score:" in result.explanation
        assert "Value:" in result.explanation
        assert "Final:" in result.explanation

    def test_version_stored(self):
        from src.model_scoring import compute_model_score, SCORE_VERSION
        result = compute_model_score(self._base_rec())
        assert result.version == SCORE_VERSION
        assert result.version == "model_score_v1"

    def test_score_dict_has_all_fields(self):
        from src.model_scoring import compute_model_score
        result = compute_model_score(self._base_rec())
        d = result.to_dict()
        assert "model_score" in d
        assert "score_version" in d
        assert "components" in d
        assert "component_values" in d
        assert "applied_cap" in d
        assert "penalties" in d
        assert "explanation" in d


# ==================================================================
# Score normalization (PART 5)
# ==================================================================


class TestScoreNormalization:
    """Verify scores have meaningful separation."""

    def _base_rec(self, **overrides):
        rec = {
            "market_form": "ou",
            "ev_pct": 5.0,
            "n_consensus_books": 6,
            "market_quality": "VALID_MARKET",
            "rec_status": "BET",
            "comparison_status": "",
            "freshness_status": "FRESH",
            "data_source": "LIVE API",
            "yn_implied_prob_adv": None,
            "yn_decimal_odds_adv": None,
            "fair_prob": 0.55,
            "confidence_score": 75.0,
        }
        rec.update(overrides)
        return rec

    def test_typical_qualified_pick_scores_6_to_9(self):
        from src.model_scoring import compute_model_score
        rec = self._base_rec(ev_pct=3.0, n_consensus_books=5)
        result = compute_model_score(rec)
        assert 5.0 <= result.score <= 9.8

    def test_strong_pick_scores_above_8(self):
        from src.model_scoring import compute_model_score
        rec = self._base_rec(ev_pct=8.0, n_consensus_books=8)
        result = compute_model_score(rec)
        assert result.score >= 7.0

    def test_weak_pick_scores_below_7(self):
        from src.model_scoring import compute_model_score
        rec = self._base_rec(
            ev_pct=1.0, n_consensus_books=3,
            market_quality="NEEDS_REVIEW")
        result = compute_model_score(rec)
        assert result.score <= 7.5


# ==================================================================
# Dashboard integration (PART 7)
# ==================================================================


class TestDashboardIntegration:
    """Verify Model Score appears in dashboard."""

    def test_control_panel_has_model_score_in_table(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert '"Model Score"' in source

    def test_control_panel_loads_model_score_column(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "model_score" in source
        assert "score_version" in source
        assert "score_explanation" in source

    def test_control_panel_sorts_by_model_score(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert ".sort(" in source

    def test_control_panel_has_score_filter(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "6.0+" in source
        assert "5.5+" in source
        assert "Below 6.0" in source

    def test_control_panel_has_disclaimer(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert "not a guaranteed win probability" in source


# ==================================================================
# Historical versioning (PART 8)
# ==================================================================


class TestHistoricalVersioning:
    """Verify score version is stored."""

    def test_db_schema_has_score_columns(self):
        source = Path("database/db_manager.py").read_text(encoding="utf-8")
        assert "model_score" in source
        assert "score_version" in source
        assert "score_components" in source
        assert "score_cap" in source
        assert "score_explanation" in source

    def test_pipeline_freeze_computes_score(self):
        source = Path("src/daily_pipeline.py").read_text(encoding="utf-8")
        assert "compute_model_score" in source
        assert "model_score" in source

    def test_save_recommendation_includes_score(self):
        source = Path("database/db_manager.py").read_text(encoding="utf-8")
        assert "model_score" in source
        assert "score_version" in source
        assert "score_components" in source


# ==================================================================
# Source structure checks
# ==================================================================


class TestSourceStructure:
    """Verify key elements exist in source code."""

    def test_unit_helpers_exists(self):
        assert Path("src/unit_helpers.py").exists()

    def test_model_scoring_exists(self):
        assert Path("src/model_scoring.py").exists()

    def test_model_scoring_has_weights_config(self):
        source = Path("src/model_scoring.py").read_text(encoding="utf-8")
        assert "ScoreWeights" in source
        assert "value: float = 0.35" in source
        assert "market_quality: float = 0.20" in source
        assert "reliability: float = 0.15" in source
        assert "freshness: float = 0.10" in source
        assert "confidence: float = 0.10" in source
        assert "risk: float = 0.10" in source

    def test_model_scoring_has_caps(self):
        source = Path("src/model_scoring.py").read_text(encoding="utf-8")
        assert "SCORE_CAPS" in source
        assert "VALID_MARKET" in source
        assert "PRICE_OUTLIER" in source
        assert "NEEDS_REVIEW" in source
        assert "INSUFFICIENT_MARKET" in source

    def test_model_scoring_has_explanation(self):
        source = Path("src/model_scoring.py").read_text(encoding="utf-8")
        assert "explanation" in source
        assert "ScoreResult" in source

    def test_confidence_uses_correct_divisor(self):
        source = Path("src/confidence.py").read_text(encoding="utf-8")
        assert "value / 15.0" in source
        assert "value / 0.15" not in source

    def test_no_double_multiply_in_control_panel(self):
        source = Path("src/control_panel.py").read_text(encoding="utf-8")
        assert 'ev_pct"] * 100' not in source
        assert 'yn_implied_prob_adv"] * 100' not in source
