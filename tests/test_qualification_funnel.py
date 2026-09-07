"""Tests for src/qualification_funnel.py — read-only funnel reporting
built from data the pipeline already persists. Never touches
qualification logic; verifies the report is a correct, honest summary
of exactly what's in the database (including reporting "not available"
rather than a misleading 0/0 when a data source genuinely has nothing).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.qualification_funnel import (
    build_funnel_report,
    get_ingestion_funnel,
    get_recommendation_level_funnel,
    get_scan_level_funnel,
)


def _init(tmp_path, name="funnel_test.db"):
    from database.db_manager import init_db, get_connection
    db_path = tmp_path / name
    init_db(str(db_path))
    return get_connection(str(db_path))


_rec_counter = [0]


def _save_rec(conn, **overrides):
    """Each call gets a unique fingerprint by default (varying event_id
    and observation_timestamp, both FINGERPRINT_FIELDS) — save_recommendation
    correctly dedupes identical fingerprints, so distinct test rows must
    differ on at least one fingerprinted field, not just an override
    field like model_score that isn't part of the fingerprint at all."""
    from database.db_manager import save_recommendation
    _rec_counter[0] += 1
    n = _rec_counter[0]
    rec = {
        "event_id": f"evt-{n}", "player_id": "GAME", "player_name": "Total",
        "market_type": "game_total_ou", "market_form": "ou", "side": "OVER",
        "sportsbook": "draftkings", "offered_american_odds": -110,
        "offered_decimal_odds": 1.909, "offered_implied_prob": 0.524,
        "rec_status": "BET", "scan_timestamp": "2026-08-23T12:00:00Z",
        "observation_timestamp": f"2026-08-23T12:00:{n:02d}Z",
        "league": "MLB", "sport": "baseball", "scan_run_id": "run-1",
    }
    rec.update(overrides)
    return save_recommendation(conn, rec)


def test_get_scan_level_funnel_returns_none_when_no_metadata(tmp_path):
    from database.db_manager import create_run
    conn = _init(tmp_path)
    run_id = create_run(conn, run_type="scan")  # no metadata at all
    assert get_scan_level_funnel(conn, run_id) is None


def test_get_scan_level_funnel_returns_persisted_pinnacle_funnel(tmp_path):
    from database.db_manager import create_run, finish_run
    conn = _init(tmp_path)
    run_id = create_run(conn, run_type="scan")
    finish_run(conn, run_id, metadata={
        "pinnacle_funnel": {"total_groups": 40, "pinnacle_missing": 7, "official_approved": 0},
    })
    result = get_scan_level_funnel(conn, run_id)
    assert result == {"total_groups": 40, "pinnacle_missing": 7, "official_approved": 0}


def test_get_ingestion_funnel_none_when_no_ingestion_run_id(tmp_path):
    """WNBA and the Odds-API fallback path never set ingestion_run_id —
    must report None (unavailable), never a misleading 0/0."""
    conn = _init(tmp_path)
    _save_rec(conn, ingestion_run_id=None)
    assert get_ingestion_funnel(conn, "run-1") is None


def test_get_ingestion_funnel_aggregates_real_rows(tmp_path):
    from database.db_manager import create_run, log_ingestion
    conn = _init(tmp_path)
    ingestion_run_id = create_run(conn, run_type="ingest")
    _save_rec(conn, ingestion_run_id=ingestion_run_id)
    log_ingestion(conn, ingestion_run_id, "evt-a", odds_rows=8, audit_rows=12)
    log_ingestion(conn, ingestion_run_id, "evt-b", odds_rows=5, audit_rows=9)
    result = get_ingestion_funnel(conn, "run-1")
    assert result == {"raw_odds_rows": 21, "normalized_approved_rows": 13}


def test_ev_buckets_are_cumulative_ge_thresholds(tmp_path):
    conn = _init(tmp_path)
    _save_rec(conn, market_type="game_total_ou", ev_pct=0.5)   # positive, no bucket
    _save_rec(conn, market_type="game_total_ou", ev_pct=1.5)   # >=1%
    _save_rec(conn, market_type="game_total_ou", ev_pct=4.0)   # >=1,2,3
    _save_rec(conn, market_type="game_total_ou", ev_pct=6.0)   # >=1,2,3,5
    _save_rec(conn, market_type="game_total_ou", ev_pct=-1.0)  # negative
    [mf] = get_recommendation_level_funnel(conn, "run-1")
    assert mf.n_recommendations == 5
    assert mf.n_positive_ev == 4
    assert mf.n_ev_ge_1pct == 3
    assert mf.n_ev_ge_2pct == 2
    assert mf.n_ev_ge_3pct == 2
    assert mf.n_ev_ge_5pct == 1


def test_model_score_pass_uses_official_threshold(tmp_path):
    conn = _init(tmp_path)
    _save_rec(conn, model_score=6.9)
    _save_rec(conn, model_score=7.0)
    _save_rec(conn, model_score=8.5)
    [mf] = get_recommendation_level_funnel(conn, "run-1", official_min_model_score=7.0)
    assert mf.n_model_score_pass == 2


def test_pinnacle_or_loo_valid_counts_approved_and_genuinely_missing(tmp_path):
    """The real Gate 9 LOO-fallback eligibility condition: approved OR
    pinnacle_found is explicitly False. A present-but-blocked Pinnacle
    match (pinnacle_found=True, pinnacle_approved=False/None) must NOT
    count — that's the case Gate 9 still blocks."""
    conn = _init(tmp_path)
    _save_rec(conn, pinnacle_approved=1, pinnacle_found=1)   # approved
    _save_rec(conn, pinnacle_approved=0, pinnacle_found=0)   # genuinely missing -> LOO-eligible
    _save_rec(conn, pinnacle_approved=0, pinnacle_found=1)   # present but blocked -> NOT counted
    [mf] = get_recommendation_level_funnel(conn, "run-1")
    assert mf.n_pinnacle_or_loo_valid == 2


def test_tier_counts(tmp_path):
    conn = _init(tmp_path)
    _save_rec(conn, recommendation_tier="OFFICIAL_TRACKED")
    _save_rec(conn, recommendation_tier="DISCOVERY_TRACKED")
    _save_rec(conn, recommendation_tier="RESEARCH_ONLY")
    _save_rec(conn, recommendation_tier="RESEARCH_ONLY")
    [mf] = get_recommendation_level_funnel(conn, "run-1")
    assert mf.n_official == 1
    assert mf.n_discovery == 1
    assert mf.n_research == 2


def test_gate_rejections_classified_correctly(tmp_path):
    conn = _init(tmp_path)
    _save_rec(conn, disqualification_reasons=(
        "Model Score 6.2 < 7.0; O/U EV 1.50% < 3.0%; "
        "Pinnacle approval required for official status"
    ))
    [mf] = get_recommendation_level_funnel(conn, "run-1")
    assert mf.gate_rejections["4_model_score"] == 1
    assert mf.gate_rejections["8_edge_threshold"] == 1
    assert mf.gate_rejections["9_pinnacle_gate"] == 1


def test_gate_rejections_unknown_clause_bucketed_as_other(tmp_path):
    conn = _init(tmp_path)
    _save_rec(conn, disqualification_reasons="Some entirely new future gate message")
    [mf] = get_recommendation_level_funnel(conn, "run-1")
    assert any(k.startswith("other:") for k in mf.gate_rejections)


def test_by_market_splits_correctly(tmp_path):
    conn = _init(tmp_path)
    _save_rec(conn, market_type="game_total_ou")
    _save_rec(conn, market_type="game_total_ou")
    _save_rec(conn, market_type="game_moneyline")
    funnels = get_recommendation_level_funnel(conn, "run-1")
    by_market = {mf.market_type: mf.n_recommendations for mf in funnels}
    assert by_market == {"game_total_ou": 2, "game_moneyline": 1}


def test_build_funnel_report_scopes_to_one_league(tmp_path):
    from database.db_manager import create_run
    conn = _init(tmp_path)
    create_run(conn, run_type="scan")  # unrelated empty run, ignored
    _save_rec(conn, league="MLB", scan_run_id="run-mlb")
    _save_rec(conn, league="WNBA", scan_run_id="run-wnba")
    report = build_funnel_report(conn, league="MLB", limit_runs=5)
    assert "run-mlb" in report
    assert "run-wnba" not in report


def test_build_funnel_report_empty_db_returns_empty(tmp_path):
    conn = _init(tmp_path)
    assert build_funnel_report(conn) == {}


# ── Provider-coverage vs. qualification distinction (2026-09-06) ────

from datetime import datetime, timezone

from src.qualification_funnel import (
    NO_DATA_FROM_PROVIDER,
    DATA_RECEIVED_NO_BET_QUALIFIED,
    PRODUCING_RECOMMENDATIONS,
    classify_market_data_status,
    build_market_coverage_report,
)


class _FakeMarketConfig:
    def __init__(self, cli_name, display_name, market_type_ou=None, market_type_yn=None):
        self.cli_name = cli_name
        self.display_name = display_name
        self.market_type_ou = market_type_ou
        self.market_type_yn = market_type_yn


class TestClassifyMarketDataStatus:
    def test_zero_raw_rows_is_no_data_from_provider(self):
        assert classify_market_data_status(raw_rows=0, rec_rows=0) == NO_DATA_FROM_PROVIDER

    def test_raw_rows_but_zero_recs_is_data_received_no_bet_qualified(self):
        assert (
            classify_market_data_status(raw_rows=500, rec_rows=0)
            == DATA_RECEIVED_NO_BET_QUALIFIED
        )

    def test_raw_rows_and_recs_is_producing_recommendations(self):
        assert (
            classify_market_data_status(raw_rows=500, rec_rows=3)
            == PRODUCING_RECOMMENDATIONS
        )

    def test_never_reports_data_received_when_raw_rows_is_zero(self):
        """Even if a caller somehow has rec_rows > 0 with raw_rows == 0
        (shouldn't happen — a rec can't exist without raw data — but the
        classification must never call that "no bet qualified", which
        would misleadingly imply the provider sent data this cycle)."""
        assert classify_market_data_status(raw_rows=0, rec_rows=5) == NO_DATA_FROM_PROVIDER


def _insert_player_prop_odds(conn, *, market_type, n=1, validation_status="VALID"):
    from database.db_manager import save_player_prop_batch

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for i in range(n):
        rows.append({
            "event_id": f"E-{market_type}-{i}", "odd_id": f"odd-{market_type}-{i}",
            "sportsbook": "draftkings", "player_id": f"P-{market_type}-{i}",
            "player_name": "Test Player", "team_id": None, "team_name": None,
            "market_type": market_type, "market_group_key": f"grp-{market_type}-{i}",
            "side": "OVER", "line": 1.5, "price": -110, "decimal_odds": 1.909,
            "is_alt_line": 0, "available": 1, "validation_status": validation_status,
            "mapping_confidence": "HIGH", "mapping_method": "exact",
            "validation_reason": "", "captured_at": now,
        })
    save_player_prop_batch(conn, rows)


class TestBuildMarketCoverageReport:
    def test_market_with_no_raw_rows_is_no_data_from_provider(self, tmp_path):
        conn = _init(tmp_path)
        registry = [_FakeMarketConfig("hits", "Player Hits", market_type_ou="batting_hits_ou")]
        report = build_market_coverage_report(conn, "MLB", registry)
        assert len(report) == 1
        assert report[0]["status"] == NO_DATA_FROM_PROVIDER
        assert report[0]["raw_rows"] == 0
        assert report[0]["rec_rows"] == 0

    def test_market_with_raw_rows_but_no_recs_is_data_received_no_bet_qualified(self, tmp_path):
        conn = _init(tmp_path)
        _insert_player_prop_odds(conn, market_type="pitching_strikeouts_ou", n=10)
        registry = [_FakeMarketConfig(
            "strikeouts", "Pitcher Strikeouts", market_type_ou="pitching_strikeouts_ou",
        )]
        report = build_market_coverage_report(conn, "MLB", registry)
        assert report[0]["status"] == DATA_RECEIVED_NO_BET_QUALIFIED
        assert report[0]["raw_rows"] == 10
        assert report[0]["rec_rows"] == 0

    def test_market_with_raw_rows_and_recs_is_producing_recommendations(self, tmp_path):
        conn = _init(tmp_path)
        _insert_player_prop_odds(conn, market_type="batting_homeRuns_ou", n=10)
        _save_rec(
            conn, market_type="batting_homeRuns_ou", league="MLB",
            scan_timestamp=datetime.now(timezone.utc).isoformat(),
        )
        registry = [_FakeMarketConfig(
            "home_runs", "Home Runs", market_type_ou="batting_homeRuns_ou",
        )]
        report = build_market_coverage_report(conn, "MLB", registry)
        assert report[0]["status"] == PRODUCING_RECOMMENDATIONS
        assert report[0]["raw_rows"] == 10
        assert report[0]["rec_rows"] == 1

    def test_scopes_recommendation_counts_to_the_requested_league(self, tmp_path):
        """save_player_prop_batch always tags raw rows 'MLB' (its schema
        column default — there's no per-row league param), so the
        league-scoping to actually exercise here is on the recommendation
        side: a same-market_type recommendation logged under a different
        league must not count toward this league's status."""
        conn = _init(tmp_path)
        _insert_player_prop_odds(conn, market_type="batting_homeRuns_ou", n=5)
        _save_rec(
            conn, market_type="batting_homeRuns_ou", league="WNBA",
            scan_timestamp=datetime.now(timezone.utc).isoformat(),
        )
        registry = [_FakeMarketConfig(
            "home_runs", "Home Runs", market_type_ou="batting_homeRuns_ou",
        )]
        report = build_market_coverage_report(conn, "MLB", registry)
        # Raw MLB coverage exists, but the only recommendation is WNBA's —
        # an MLB-scoped report must not count it as an MLB recommendation.
        assert report[0]["raw_rows"] == 5
        assert report[0]["rec_rows"] == 0
        assert report[0]["status"] == DATA_RECEIVED_NO_BET_QUALIFIED

    def test_skips_registry_entries_with_no_market_type_at_all(self, tmp_path):
        conn = _init(tmp_path)
        registry = [_FakeMarketConfig("broken", "Broken Entry")]  # no ou/yn type
        report = build_market_coverage_report(conn, "MLB", registry)
        assert report == []
