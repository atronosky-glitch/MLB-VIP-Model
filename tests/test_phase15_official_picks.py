"""Phase 15 — Official Pick Qualification and Tier Classification tests."""

import sqlite3

import pytest

from src.official_picks import (
    OfficialPickConfig, QualificationResult, classify_recommendation,
    TIER_OFFICIAL, TIER_DISCOVERY, TIER_RESEARCH, RULES_VERSION, DEFAULT_CONFIG,
    compute_bet_slot_key, classify_pick_update,
    UPDATE_DUPLICATE, UPDATE_MATERIAL,
    PICK_STATUS_ACTIVE, PICK_STATUS_SUPERSEDED,
)
from src import prop_config as cfg


def _make_rec(**overrides):
    base = {
        "event_id": "E1",
        "player_id": "P1",
        "player_name": "Judge",
        "market_type": "strikeouts",
        "market_form": "ou",
        "side": "Over",
        "line": 6.5,
        "sportsbook": "DK",
        "offered_american_odds": -110,
        "offered_decimal_odds": 1.909,
        "offered_implied_prob": 0.524,
        "fair_prob": 0.55,
        "ev_pct": 5.0,
        "n_consensus_books": 6,
        "market_quality": "STRONG",
        "rec_status": "QUALIFIED",
        "freshness_status": "FRESH",
        "event_status": "scheduled",
        "model_score": 8.5,
        "model_version": "v1",
        "pinnacle_approved": True,
    }
    base.update(overrides)
    return base


class TestOfficialPickConfig:
    def test_default_config(self):
        c = OfficialPickConfig()
        assert c.official_min_model_score == 7.0
        assert c.official_min_ou_ev_pct == 3.0
        assert c.official_min_yn_price_adv_pp == 3.0
        assert c.official_min_books == 2
        assert c.official_allowed_statuses == (
            "QUALIFIED", "STRONG_EDGE", "POSITIVE_EDGE",
            "STRONG_PRICE_OUTLIER", "PRICE_OUTLIER", "MARGINAL_PRICE_OUTLIER",
        )

    def test_config_is_frozen(self):
        c = OfficialPickConfig()
        with pytest.raises(AttributeError):
            c.official_min_model_score = 9.0

    def test_custom_config(self):
        c = OfficialPickConfig(official_min_model_score=7.0, official_min_books=3)
        assert c.official_min_model_score == 7.0
        assert c.official_min_books == 3


class TestQualificationResult:
    def test_to_dict_official(self):
        q = QualificationResult(
            tier=TIER_OFFICIAL, passed=True,
            reasons=["Qualified"],
            disqualification_reasons=[],
            contributing_book_count=6,
            applicable_edge_metric="ev_pct",
            applicable_edge_threshold=3.0,
            model_score_threshold=7.0,
        )
        d = q.to_dict()
        assert d["recommendation_tier"] == TIER_OFFICIAL
        assert d["qualification_passed"] == 1
        assert d["contributing_book_count"] == 6
        assert d["qualification_rules_version"] == RULES_VERSION

    def test_to_dict_research(self):
        q = QualificationResult(tier=TIER_RESEARCH, passed=False)
        d = q.to_dict()
        assert d["recommendation_tier"] == TIER_RESEARCH
        assert d["qualification_passed"] == 0


class TestClassifyOU:
    def test_official_when_all_gates_pass(self):
        rec = _make_rec(
            model_score=9.0, ev_pct=6.0, n_consensus_books=7,
            rec_status="QUALIFIED", freshness_status="FRESH",
            event_status="scheduled", market_quality="STRONG",
        )
        q = classify_recommendation(rec)
        assert q.tier == TIER_OFFICIAL
        assert q.passed is True
        assert len(q.disqualification_reasons) == 0

    def test_research_when_model_score_low(self):
        rec = _make_rec(model_score=5.0, ev_pct=6.0, n_consensus_books=7)
        q = classify_recommendation(rec)
        assert q.tier == TIER_RESEARCH
        assert q.passed is False
        assert any("5.0" in r for r in q.disqualification_reasons)

    def test_research_when_ev_low(self):
        """EV 1.5% with 7 books → DISCOVERY (below official 3.0% but above discovery threshold)."""
        rec = _make_rec(model_score=9.0, ev_pct=1.5, n_consensus_books=7)
        q = classify_recommendation(rec)
        assert q.tier == TIER_DISCOVERY
        assert q.passed is False

    def test_research_when_books_low(self):
        rec = _make_rec(model_score=9.0, ev_pct=6.0, n_consensus_books=1)
        q = classify_recommendation(rec)
        assert q.tier == TIER_RESEARCH
        assert any("Only 1" in r for r in q.disqualification_reasons)

    def test_research_when_wrong_status(self):
        rec = _make_rec(model_score=9.0, ev_pct=6.0, n_consensus_books=7, rec_status="OPPORTUNITY")
        q = classify_recommendation(rec)
        assert q.tier == TIER_RESEARCH
        assert any("OPPORTUNITY" in r for r in q.disqualification_reasons)

    def test_research_when_stale(self):
        rec = _make_rec(model_score=9.0, ev_pct=6.0, n_consensus_books=7, freshness_status="STALE")
        q = classify_recommendation(rec)
        assert q.tier == TIER_RESEARCH
        assert any("stale" in r.lower() for r in q.disqualification_reasons)

    def test_research_when_live_game(self):
        rec = _make_rec(model_score=9.0, ev_pct=6.0, n_consensus_books=7, event_status="live")
        q = classify_recommendation(rec)
        assert q.tier == TIER_RESEARCH

    def test_research_when_price_outlier(self):
        rec = _make_rec(model_score=9.0, ev_pct=6.0, n_consensus_books=7, market_quality="PRICE_OUTLIER")
        q = classify_recommendation(rec)
        assert q.tier == TIER_DISCOVERY

    def test_multiple_failures_all_listed(self):
        rec = _make_rec(model_score=2.0, ev_pct=0.1, n_consensus_books=1)
        q = classify_recommendation(rec)
        assert q.tier == TIER_RESEARCH
        assert len(q.disqualification_reasons) >= 3


class TestPinnacleOfficialGate:
    """REQUIRE_PINNACLE_FOR_OFFICIAL gates the official tier for O/U."""

    def _strong_rec(self, **overrides):
        rec = _make_rec(
            model_score=9.0, ev_pct=6.0, n_consensus_books=7,
            rec_status="QUALIFIED", freshness_status="FRESH",
            event_status="scheduled", market_quality="STRONG",
        )
        rec.update(overrides)
        return rec

    def test_approved_is_official(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        try:
            q = classify_recommendation(self._strong_rec(pinnacle_approved=True))
            assert q.tier == TIER_OFFICIAL
            assert q.passed is True
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig

    def test_missing_pinnacle_blocks_official(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        try:
            # QUALIFIED + STRONG + high score: everything passes except the gate.
            q = classify_recommendation(self._strong_rec(pinnacle_approved=None))
            assert q.tier == TIER_DISCOVERY
            assert q.passed is False
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig

    def test_threshold_fail_blocks_official(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        try:
            q = classify_recommendation(self._strong_rec(pinnacle_approved=False))
            assert q.tier == TIER_DISCOVERY
            assert q.passed is False
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig

    def test_no_pinnacle_uses_explicit_loo_fallback(self):
        old_require = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        old_fallback = cfg.PINNACLE_FALLBACK_TO_MARKET_MEDIAN
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        cfg.PINNACLE_FALLBACK_TO_MARKET_MEDIAN = True
        try:
            q = classify_recommendation(self._strong_rec(
                pinnacle_found=False, pinnacle_approved=None,
            ))
            assert q.tier == TIER_OFFICIAL
            assert q.passed is True
            assert any("LOO fallback" in reason for reason in q.reasons)
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = old_require
            cfg.PINNACLE_FALLBACK_TO_MARKET_MEDIAN = old_fallback

    def test_one_sided_or_mismatched_pinnacle_still_blocks(self):
        old_require = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        old_fallback = cfg.PINNACLE_FALLBACK_TO_MARKET_MEDIAN
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        cfg.PINNACLE_FALLBACK_TO_MARKET_MEDIAN = True
        try:
            q = classify_recommendation(self._strong_rec(
                pinnacle_found=True, pinnacle_reference_used=False,
                pinnacle_approved=None,
            ))
            assert q.tier == TIER_DISCOVERY
            assert q.passed is False
            assert any("Pinnacle approval required" in reason for reason in q.disqualification_reasons)
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = old_require
            cfg.PINNACLE_FALLBACK_TO_MARKET_MEDIAN = old_fallback

    def test_research_reports_pinnacle_reason(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        try:
            # MARGINAL_EDGE is not discovery-allowed → falls through to RESEARCH,
            # where the official disqualifications are preserved for display.
            q = classify_recommendation(self._strong_rec(
                pinnacle_approved=None, rec_status="MARGINAL_EDGE",
            ))
            assert q.tier == TIER_RESEARCH
            assert any("Pinnacle approval required" in r
                       for r in q.disqualification_reasons)
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig

    def test_legacy_behavior_when_flag_disabled(self):
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = False
        try:
            # Without the requirement, no pinnacle approval needed for official.
            q = classify_recommendation(self._strong_rec(pinnacle_approved=None))
            assert q.tier == TIER_OFFICIAL
            assert q.passed is True
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig

    def test_yn_unaffected_by_pinnacle_gate(self):
        """YN single-sided markets have no Pinnacle approval — never gated."""
        orig = cfg.REQUIRE_PINNACLE_FOR_OFFICIAL
        cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = True
        try:
            rec = {
                "event_id": "E2", "player_id": "P2", "player_name": "Ohtani",
                "market_type": "pitching_win", "market_form": "yn", "side": "Yes",
                "line": None, "sportsbook": "FD", "offered_american_odds": 150,
                "offered_decimal_odds": 2.5, "offered_implied_prob": 0.4,
                "yn_implied_prob_adv": 6.0, "yn_reference_prob": 0.55,
                "yn_reference_odds": -110,
                "n_consensus_books": 7, "market_quality": "STRONG",
                "rec_status": "QUALIFIED", "freshness_status": "FRESH",
                "event_status": "scheduled", "model_score": 9.0,
            }
            q = classify_recommendation(rec)
            assert q.tier == TIER_OFFICIAL
            assert q.passed is True
        finally:
            cfg.REQUIRE_PINNACLE_FOR_OFFICIAL = orig


class TestClassifyYN:
    def _make_yn_rec(self, **overrides):
        base = {
            "event_id": "E2", "player_id": "P2", "player_name": "Ohtani",
            "market_type": "pitching_win", "market_form": "yn", "side": "Yes",
            "line": None, "sportsbook": "FD", "offered_american_odds": 150,
            "offered_decimal_odds": 2.5, "offered_implied_prob": 0.4,
            "yn_implied_prob_adv": 5.0, "yn_reference_prob": 0.55,
            "yn_reference_odds": -110,
            "n_consensus_books": 6, "market_quality": "STRONG",
            "rec_status": "QUALIFIED", "freshness_status": "FRESH",
            "event_status": "scheduled", "model_score": 8.5,
        }
        base.update(overrides)
        return base

    def test_official_yn(self):
        rec = self._make_yn_rec(model_score=9.0, yn_implied_prob_adv=6.0, n_consensus_books=7)
        q = classify_recommendation(rec)
        assert q.tier == TIER_OFFICIAL
        assert q.passed is True

    def test_research_yn_low_advantage(self):
        rec = self._make_yn_rec(model_score=9.0, yn_implied_prob_adv=1.0, n_consensus_books=7)
        q = classify_recommendation(rec)
        assert q.tier == TIER_DISCOVERY


class TestEdgeMetricTracking:
    def test_ou_ev_metric(self):
        rec = _make_rec(model_score=8.0, ev_pct=5.0, n_consensus_books=6)
        q = classify_recommendation(rec)
        assert q.applicable_edge_metric == "ev_pct"
        assert q.applicable_edge_threshold == 3.0

    def test_yn_advantage_metric(self):
        rec = {"market_form": "yn", "yn_implied_prob_adv": 5.0, "n_consensus_books": 6,
               "model_score": 8.0, "rec_status": "QUALIFIED", "market_quality": "STRONG",
               "freshness_status": "FRESH", "event_status": "scheduled",
               "event_id": "E1", "player_id": "P1", "side": "Yes",
               "sportsbook": "FD", "yn_reference_odds": -110}
        q = classify_recommendation(rec)
        assert q.applicable_edge_metric == "yn_implied_prob_adv"
        assert q.applicable_edge_threshold == 3.0


class TestOfficialSelectionTierSafety:
    def test_discovery_rows_are_not_selected_as_official(self):
        from src.official_picks import rank_and_select_official_picks

        rows = [
            _make_rec(
                recommendation_id="official",
                recommendation_tier=TIER_OFFICIAL,
                qualification_passed=1,
            ),
            _make_rec(
                recommendation_id="discovery",
                recommendation_tier=TIER_DISCOVERY,
                qualification_passed=0,
                model_score=10.0,
            ),
        ]
        selected = rank_and_select_official_picks(rows)
        assert [row["recommendation_id"] for row in selected] == ["official"]


class TestCustomConfig:
    def test_stricter_thresholds(self):
        c = OfficialPickConfig(official_min_model_score=9.5, official_min_ou_ev_pct=5.0, official_min_books=5)
        rec = _make_rec(model_score=9.0, ev_pct=4.0, n_consensus_books=4)
        q = classify_recommendation(rec, config=c)
        assert q.tier == TIER_DISCOVERY
        assert q.passed is False

    def test_stricter_thresholds_below_discovery(self):
        c = OfficialPickConfig(official_min_model_score=9.5, official_min_ou_ev_pct=5.0, official_min_books=5)
        rec = _make_rec(model_score=5.0, ev_pct=1.0, n_consensus_books=2)
        q = classify_recommendation(rec, config=c)
        assert q.tier == TIER_RESEARCH
        assert len(q.disqualification_reasons) >= 1

    def test_looser_thresholds(self):
        c = OfficialPickConfig(official_min_model_score=5.0, official_min_ou_ev_pct=1.0, official_min_books=2)
        rec = _make_rec(model_score=5.5, ev_pct=1.5, n_consensus_books=3)
        q = classify_recommendation(rec, config=c)
        assert q.tier == TIER_OFFICIAL


class TestPipelineIntegration:
    def test_classify_called_in_freeze(self, tmp_path):
        """Verify _stage_freeze calls classify_recommendation and stores tier."""
        import database.db_manager as dbm
        import src.daily_pipeline as dp

        _orig_db_path = dbm.DB_PATH
        _orig_get_conn = dbm.get_connection

        db_path = str(tmp_path / "test.db")
        dbm.DB_PATH = db_path

        def _file_conn():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            return conn

        dbm.get_connection = _file_conn
        dbm.init_db()

        conn = _file_conn()
        conn.execute("INSERT INTO games VALUES ('E1', 'MLB', 'BOS', 'NYY', '2099-01-01T20:00:00Z', 'scheduled', NULL, NULL, datetime('now'), datetime('now'))")
        conn.commit()

        _orig = dp.get_connection
        dp.get_connection = _file_conn
        try:
            state = dp.PipelineState()
            state.scan_run_id = "test-run"
            state.ingestion_run_id = "test-ingest"
            state.data_source = "test"
            state.version = "1.0.0"

            opp = {
                "event_id": "E1", "player_id": "P1", "player_name": "Judge",
                "market_type": "strikeouts", "line": 6.5, "side": "Over",
                "sportsbook": "DK", "american_odds": -110, "decimal_odds": 1.909,
                "ev_pct": 6.0, "comparison_status": "CONSENSUS",
                "bet_status": "QUALIFIED", "start_time": "2099-01-01T20:00:00Z",
                "market_reference_probability": 0.52, "market_reference_odds": -110,
                "price_advantage_pct": 3.0, "decimal_odds_advantage": 0.05,
                "n_consensus_books": 7, "market_quality": "STRONG",
                "rec_eligible": True, "fair_prob": 0.55,
            }
            state.scan_result = {"opportunities": [opp], "yn_opportunities": []}

            config = dp.PipelineConfig(dry_run=False)
            dp._stage_freeze(config, state)

            assert state.n_recommendations_saved == 1
            row = conn.execute(
                "SELECT recommendation_tier, qualification_passed FROM historical_recommendations LIMIT 1"
            ).fetchone()
            assert row is not None
        finally:
            dp.get_connection = _orig
            dbm.get_connection = _orig_get_conn
            dbm.DB_PATH = _orig_db_path
            conn.close()


class TestTierConstants:
    def test_tier_values(self):
        assert TIER_OFFICIAL == "OFFICIAL_TRACKED"
        assert TIER_RESEARCH == "RESEARCH_ONLY"

    def test_rules_version(self):
        assert RULES_VERSION == "official_pick_rules_v2"

    def test_default_config_singleton(self):
        assert DEFAULT_CONFIG.official_min_model_score == 7.0


class TestImmutability:
    def test_official_tier_unchanged_after_settle(self):
        """Once a rec is saved as OFFICIAL, its tier never changes."""
        rec = _make_rec(model_score=9.0, ev_pct=6.0, n_consensus_books=7)
        q = classify_recommendation(rec)
        assert q.tier == TIER_OFFICIAL
        rec.update(q.to_dict())
        assert rec["recommendation_tier"] == TIER_OFFICIAL
        rec["event_status"] = "final"
        q2 = classify_recommendation(rec)
        assert q2.tier == TIER_RESEARCH
        assert rec["recommendation_tier"] == TIER_OFFICIAL


class TestBetSlotKey:
    def test_same_event_player_market_side_same_key(self):
        a = _make_rec(line=5.5, offered_american_odds=-110)
        b = _make_rec(line=4.5, offered_american_odds=+125, sportsbook="FanDuel")
        assert compute_bet_slot_key(a) == compute_bet_slot_key(b)

    def test_different_side_different_key(self):
        a = _make_rec(side="Over")
        b = _make_rec(side="Under")
        assert compute_bet_slot_key(a) != compute_bet_slot_key(b)

    def test_different_player_different_key(self):
        a = _make_rec(player_id="P1")
        b = _make_rec(player_id="P2")
        assert compute_bet_slot_key(a) != compute_bet_slot_key(b)

    def test_different_market_different_key(self):
        a = _make_rec(market_type="strikeouts")
        b = _make_rec(market_type="hits")
        assert compute_bet_slot_key(a) != compute_bet_slot_key(b)


class TestClassifyPickUpdate:
    def test_small_price_wiggle_is_duplicate(self):
        # +110 -> +108: ~0.46pp implied-probability move, below threshold.
        existing = {"line": 5.5, "offered_american_odds": 110}
        candidate = {"line": 5.5, "offered_american_odds": 108}
        assert classify_pick_update(existing, candidate) == UPDATE_DUPLICATE

    def test_large_favorable_price_move_is_material(self):
        # +110 -> +125: ~3.2pp implied-probability move, above threshold.
        existing = {"line": 5.5, "offered_american_odds": 110}
        candidate = {"line": 5.5, "offered_american_odds": 125}
        assert classify_pick_update(existing, candidate) == UPDATE_MATERIAL

    def test_large_unfavorable_price_move_is_material(self):
        existing = {"line": 5.5, "offered_american_odds": -110}
        candidate = {"line": 5.5, "offered_american_odds": -150}
        assert classify_pick_update(existing, candidate) == UPDATE_MATERIAL

    def test_any_line_change_is_material_even_with_identical_price(self):
        existing = {"line": 5.5, "offered_american_odds": -110}
        candidate = {"line": 4.5, "offered_american_odds": -110}
        assert classify_pick_update(existing, candidate) == UPDATE_MATERIAL

    def test_identical_price_and_line_is_duplicate(self):
        existing = {"line": 5.5, "offered_american_odds": -110}
        candidate = {"line": 5.5, "offered_american_odds": -110}
        assert classify_pick_update(existing, candidate) == UPDATE_DUPLICATE

    def test_missing_price_data_defaults_to_material(self):
        """Cannot prove it's the same pick without a comparable price —
        never silently suppress on missing data."""
        existing = {"line": 5.5, "offered_american_odds": None}
        candidate = {"line": 5.5, "offered_american_odds": -110}
        assert classify_pick_update(existing, candidate) == UPDATE_MATERIAL


_rec_counter = [0]


def _save_rec(conn, **overrides):
    """Insert a full historical_recommendations row via the real save path."""
    from database.db_manager import save_recommendation

    _rec_counter[0] += 1
    rec = {
        "event_id": "E1", "player_id": "P1", "player_name": "Judge",
        "market_type": "strikeouts", "market_form": "ou", "period": "game",
        "line": 5.5, "side": "OVER", "sportsbook": "DraftKings",
        "offered_american_odds": -110, "offered_decimal_odds": 1.909,
        "offered_implied_prob": 0.524, "fair_prob": 0.55, "ev_pct": 5.0,
        "n_consensus_books": 6, "market_quality": "VALID_MARKET",
        "rec_status": "QUALIFIED", "rec_eligible": 1,
        "scan_timestamp": f"2026-08-21T{9 + _rec_counter[0]:02d}:00:00+00:00",
        "recommendation_tier": "OFFICIAL_TRACKED", "qualification_passed": 1,
        "league": "MLB", "sport": "baseball",
    }
    rec.update(overrides)
    rec["recommendation_id"] = save_recommendation(conn, rec)
    assert rec["recommendation_id"] is not None
    return rec


class TestFreezeOrUpdateOfficialPick:
    """Integration tests for database.db_manager.freeze_or_update_official_pick
    — the mechanism preventing a rescan's price wiggle from inflating the
    official-picks historical record with duplicated signal."""

    def test_first_pick_for_a_slot_is_frozen(self, db_conn):
        from database.db_manager import freeze_or_update_official_pick
        rec = _save_rec(db_conn)
        result = freeze_or_update_official_pick(db_conn, rec, official_rank=1)
        assert result["action"] == "frozen"
        row = db_conn.execute(
            "SELECT pick_status, bet_slot_key, first_recommended_at, best_american_odds "
            "FROM official_picks WHERE recommendation_id = ?",
            (rec["recommendation_id"],),
        ).fetchone()
        assert row["pick_status"] == PICK_STATUS_ACTIVE
        assert row["bet_slot_key"] == compute_bet_slot_key(rec)
        assert row["best_american_odds"] == -110

    def test_price_wiggle_does_not_create_second_pick(self, db_conn):
        from database.db_manager import freeze_or_update_official_pick
        rec1 = _save_rec(db_conn, offered_american_odds=-110)
        freeze_or_update_official_pick(db_conn, rec1, official_rank=1)

        rec2 = _save_rec(db_conn, offered_american_odds=-108)
        result = freeze_or_update_official_pick(db_conn, rec2, official_rank=1)

        assert result["action"] == "duplicate"
        assert result["recommendation_id"] == rec1["recommendation_id"]
        count = db_conn.execute(
            "SELECT COUNT(*) AS c FROM official_picks WHERE bet_slot_key = ?",
            (compute_bet_slot_key(rec1),),
        ).fetchone()["c"]
        assert count == 1, "a small price move must not create a second official pick"

    def test_duplicate_updates_latest_and_best_price_on_existing_row(self, db_conn):
        from database.db_manager import freeze_or_update_official_pick
        rec1 = _save_rec(db_conn, offered_american_odds=-110)
        freeze_or_update_official_pick(db_conn, rec1, official_rank=1)

        rec2 = _save_rec(db_conn, offered_american_odds=-105)  # better price, still a dup
        freeze_or_update_official_pick(db_conn, rec2, official_rank=1)

        row = db_conn.execute(
            "SELECT best_american_odds, latest_american_odds, update_count "
            "FROM official_picks WHERE recommendation_id = ?",
            (rec1["recommendation_id"],),
        ).fetchone()
        assert row["best_american_odds"] == -105  # -105 is a better price than -110
        assert row["latest_american_odds"] == -105
        assert row["update_count"] == 1

    def test_line_change_supersedes_and_freezes_new_pick(self, db_conn):
        from database.db_manager import freeze_or_update_official_pick
        rec1 = _save_rec(db_conn, line=5.5, offered_american_odds=-110)
        freeze_or_update_official_pick(db_conn, rec1, official_rank=1)

        rec2 = _save_rec(db_conn, line=4.5, offered_american_odds=-110)
        result = freeze_or_update_official_pick(db_conn, rec2, official_rank=1)

        assert result["action"] == "superseded"
        assert result["recommendation_id"] == rec2["recommendation_id"]

        old_row = db_conn.execute(
            "SELECT pick_status, superseded_by FROM official_picks WHERE recommendation_id = ?",
            (rec1["recommendation_id"],),
        ).fetchone()
        assert old_row["pick_status"] == PICK_STATUS_SUPERSEDED
        assert old_row["superseded_by"] == rec2["recommendation_id"]

        new_row = db_conn.execute(
            "SELECT pick_status FROM official_picks WHERE recommendation_id = ?",
            (rec2["recommendation_id"],),
        ).fetchone()
        assert new_row["pick_status"] == PICK_STATUS_ACTIVE

    def test_large_price_move_supersedes(self, db_conn):
        from database.db_manager import freeze_or_update_official_pick
        rec1 = _save_rec(db_conn, offered_american_odds=110)
        freeze_or_update_official_pick(db_conn, rec1, official_rank=1)

        rec2 = _save_rec(db_conn, offered_american_odds=125)
        result = freeze_or_update_official_pick(db_conn, rec2, official_rank=1)
        assert result["action"] == "superseded"

    def test_only_one_active_pick_survives_a_chain_of_updates(self, db_conn):
        """Simulates several rescans of the same bet slot: two harmless
        wiggles, then a real line move, then another wiggle. Exactly one
        ACTIVE row must remain for the slot at the end."""
        from database.db_manager import freeze_or_update_official_pick

        r1 = _save_rec(db_conn, line=5.5, offered_american_odds=-110)
        freeze_or_update_official_pick(db_conn, r1, official_rank=1)
        r2 = _save_rec(db_conn, line=5.5, offered_american_odds=-108)  # wiggle
        freeze_or_update_official_pick(db_conn, r2, official_rank=1)
        r3 = _save_rec(db_conn, line=4.5, offered_american_odds=-110)  # line move
        freeze_or_update_official_pick(db_conn, r3, official_rank=1)
        r4 = _save_rec(db_conn, line=4.5, offered_american_odds=-112)  # wiggle
        result4 = freeze_or_update_official_pick(db_conn, r4, official_rank=1)

        assert result4["action"] == "duplicate"
        assert result4["recommendation_id"] == r3["recommendation_id"]

        active_rows = db_conn.execute(
            "SELECT recommendation_id FROM official_picks "
            "WHERE bet_slot_key = ? AND pick_status = 'ACTIVE'",
            (compute_bet_slot_key(r1),),
        ).fetchall()
        assert len(active_rows) == 1
        assert active_rows[0]["recommendation_id"] == r3["recommendation_id"]

        all_rows = db_conn.execute(
            "SELECT COUNT(*) AS c FROM official_picks WHERE bet_slot_key = ?",
            (compute_bet_slot_key(r1),),
        ).fetchone()["c"]
        assert all_rows == 2, "one ACTIVE + one SUPERSEDED, not four separate picks"

    def test_preserves_first_recommended_at_across_supersession(self, db_conn):
        from database.db_manager import freeze_or_update_official_pick
        rec1 = _save_rec(db_conn, line=5.5, offered_american_odds=-110)
        freeze_or_update_official_pick(db_conn, rec1, official_rank=1)
        original_first = db_conn.execute(
            "SELECT first_recommended_at FROM official_picks WHERE recommendation_id = ?",
            (rec1["recommendation_id"],),
        ).fetchone()["first_recommended_at"]

        rec2 = _save_rec(db_conn, line=4.5, offered_american_odds=-110)
        freeze_or_update_official_pick(db_conn, rec2, official_rank=1)

        new_first = db_conn.execute(
            "SELECT first_recommended_at FROM official_picks WHERE recommendation_id = ?",
            (rec2["recommendation_id"],),
        ).fetchone()["first_recommended_at"]
        assert new_first == original_first

    def test_get_official_picks_today_excludes_superseded(self, db_conn):
        from database.db_manager import freeze_or_update_official_pick, get_official_picks_today
        rec1 = _save_rec(db_conn, line=5.5, offered_american_odds=-110)
        freeze_or_update_official_pick(db_conn, rec1, official_rank=1)
        rec2 = _save_rec(db_conn, line=4.5, offered_american_odds=-110)
        freeze_or_update_official_pick(db_conn, rec2, official_rank=1)

        today = get_official_picks_today(db_conn)
        ids = {r["recommendation_id"] for r in today}
        assert rec2["recommendation_id"] in ids
        assert rec1["recommendation_id"] not in ids
