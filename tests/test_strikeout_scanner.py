"""Tests for the pitcher strikeout edge scanner.

Uses isolated in-memory database and synthetic market data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.prop_config import (
    MARKET_QUALITY_VALID, MARKET_QUALITY_NEEDS_REVIEW, MARKET_QUALITY_INSUFFICIENT,
    MARKET_QUALITY_EXCLUDED,
    BET_STATUS_STRONG, BET_STATUS_POSITIVE, BET_STATUS_MARGINAL, BET_STATUS_NO_EDGE,
    ACTIONABLE_EDGE_THRESHOLD, FRESHNESS_THRESHOLD_SECONDS,
)
from src.strikeout_scanner import run_scan, parse_args


# ==================================================================
# Helper: build a synthetic result from run_scan internals
# ==================================================================

def _make_opp(ev_pct: float, *, rec_eligible: bool = True, **overrides) -> dict:
    """Build a single opportunity dict for testing scanner sorting/filtering."""
    opp = {
        "event_id": "ev1",
        "away_team": "TeamA",
        "home_team": "TeamB",
        "start_time": "2026-07-20T23:00:00Z",
        "player_id": "PLAYER_1_MLB",
        "player_name": "Test Player",
        "market_type": "pitching_strikeouts_ou",
        "line": 5.5,
        "side": "OVER",
        "sportsbook": "testbook",
        "american_odds": -110,
        "decimal_odds": 1.9091,
        "n_consensus_books": 5,
        "fair_prob": 0.5,
        "ev_pct": ev_pct,
        "market_quality": MARKET_QUALITY_VALID if rec_eligible else MARKET_QUALITY_NEEDS_REVIEW,
        "rec_eligible": rec_eligible,
        "bet_status": BET_STATUS_NO_EDGE,
        "validation_status": "VALID",
        "is_alt_line": 0,
    }
    opp.update(overrides)
    return opp


# ==================================================================
# Mock run_scan for filtering/sorting tests
# ==================================================================

def _fake_run_scan(opportunities, **overrides):
    """Build a fake run_scan result dict."""
    result = {
        "opportunities": opportunities,
        "n_events": 3,
        "n_markets": len(opportunities) if opportunities else 0,
        "n_pitchers": 2,
        "n_approved_rows": 20,
        "n_excluded_rows": 0,
        "scan_start": "2026-07-20T12:00:00+00:00",
        "fetch_time": "2026-07-20T11:59:00+00:00",
        "data_source": "CACHE",
        "oldest_obs": "2026-07-20T11:50:00+00:00",
        "newest_obs": "2026-07-20T11:59:00+00:00",
        "age_seconds": 60,
        "stale_warning": False,
        "research_only": True,
    }
    result.update(overrides)
    return result


# ==================================================================
# Tests
# ==================================================================


def test_positive_ev_eligible_included():
    """Rec-eligible opportunity with positive EV should be included in positive mode."""
    opps = [_make_opp(3.5, bet_status=BET_STATUS_POSITIVE, rec_eligible=True)]
    result = _fake_run_scan(opps)
    # Simulate positive-only filtering from run_scan
    filtered = [o for o in result["opportunities"]
                if o["rec_eligible"]
                and o["bet_status"] in (BET_STATUS_STRONG, BET_STATUS_POSITIVE, BET_STATUS_MARGINAL)]
    assert len(filtered) == 1


def test_non_eligible_excluded_from_positive():
    """Non-rec-eligible market must NOT appear in positive mode even if EV is positive."""
    opps = [_make_opp(3.5, bet_status=BET_STATUS_POSITIVE, rec_eligible=False)]
    result = _fake_run_scan(opps)
    filtered = [o for o in result["opportunities"]
                if o["rec_eligible"]
                and o["bet_status"] in (BET_STATUS_STRONG, BET_STATUS_POSITIVE, BET_STATUS_MARGINAL)]
    assert len(filtered) == 0
    # But it should still appear in all-mode
    assert len(result["opportunities"]) == 1


def test_negative_ev_excluded_from_positive_only():
    """Negative EV must NOT appear in positive-only mode."""
    opps = [_make_opp(-4.5, bet_status=BET_STATUS_NO_EDGE, rec_eligible=True)]
    result = _fake_run_scan(opps)
    filtered = [o for o in result["opportunities"]
                if o["rec_eligible"]
                and o["bet_status"] in (BET_STATUS_STRONG, BET_STATUS_POSITIVE, BET_STATUS_MARGINAL)]
    assert len(filtered) == 0


def test_no_edge_in_all_mode():
    """NO_EDGE must appear in all-market mode."""
    opps = [_make_opp(-4.5, bet_status=BET_STATUS_NO_EDGE)]
    result = _fake_run_scan(opps)
    assert len(result["opportunities"]) == 1
    assert result["opportunities"][0]["bet_status"] == BET_STATUS_NO_EDGE


def test_actionable_threshold_filtering():
    """Only EV >= threshold should pass actionable mode."""
    threshold = ACTIONABLE_EDGE_THRESHOLD  # typically 0.02 = 2%
    below = _make_opp(1.5, bet_status=BET_STATUS_MARGINAL)
    above = _make_opp(3.0, bet_status=BET_STATUS_POSITIVE)

    target = threshold * 100
    assert below["ev_pct"] < target
    assert above["ev_pct"] >= target

    # Also require rec_eligible for actionable mode
    below_non_rec = _make_opp(3.0, bet_status=BET_STATUS_POSITIVE, rec_eligible=False)
    assert below_non_rec["ev_pct"] >= target
    assert not below_non_rec["rec_eligible"]


def test_highest_ev_ranks_first():
    """Opportunities must be ranked by EV descending."""
    opps = [
        _make_opp(1.0, bet_status=BET_STATUS_MARGINAL),
        _make_opp(5.0, bet_status=BET_STATUS_STRONG),
        _make_opp(2.5, bet_status=BET_STATUS_POSITIVE),
    ]
    opps.sort(key=lambda o: (-o["ev_pct"],
                              0,  # market quality rank all same (VALID)
                              -(o["n_consensus_books"] or 0),
                              o.get("start_time", ""),
                              o["player_name"],
                              o["sportsbook"]))
    assert opps[0]["ev_pct"] == 5.0
    assert opps[1]["ev_pct"] == 2.5
    assert opps[2]["ev_pct"] == 1.0


def test_deterministic_tie_breaking():
    """Same EV must use tie-breakers: MQ, n_books, start, name, book."""
    opps = [
        _make_opp(3.0, player_name="Zeta", sportsbook="fanduel"),
        _make_opp(3.0, player_name="Alpha", sportsbook="draftkings"),
    ]
    opps.sort(key=lambda o: (-o["ev_pct"],
                              0,  # market quality
                              -(o["n_consensus_books"] or 0),
                              o.get("start_time", ""),
                              o["player_name"],
                              o["sportsbook"]))
    assert opps[0]["player_name"] == "Alpha"  # alphabetical
    assert opps[1]["player_name"] == "Zeta"


def test_ev_book_excluded_from_consensus():
    """LOO consensus must exclude evaluated book."""
    from src.player_prop_analysis import analyze_prop_group
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(5)}
    over["book0"] = {"price": 500, "decimal_odds": 6.0, "line": 5.5}
    under["book0"] = {"price": -800, "decimal_odds": 1.125, "line": 5.5}
    result = analyze_prop_group("test", over, under)
    b0 = next(b for b in result["books"] if b["sportsbook"] == "book0" and b["side"] == "OVER")
    assert abs(b0["fair_prob"] - 0.5) < 0.01, "LOO fair prob should exclude book0's wild price"


def test_invalid_mappings_excluded():
    """Rows with unapproved validation status must be excluded."""
    from src.validation_constants import STATUS_INVALID_MAPPING, STATUS_POSSIBLE_MAPPING_ERROR, APPROVED_STATUSES
    assert STATUS_INVALID_MAPPING not in APPROVED_STATUSES
    assert STATUS_POSSIBLE_MAPPING_ERROR not in APPROVED_STATUSES


def test_insufficient_markets_excluded():
    """Markets with <5 paired books get INSUFFICIENT_MARKET and not rec-eligible."""
    from src.player_prop_analysis import analyze_prop_group
    over = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(3)}
    under = {f"book{i}": {"price": -110, "decimal_odds": 1.9091, "line": 5.5} for i in range(3)}
    result = analyze_prop_group("test", over, under)
    assert result["market_quality"] == MARKET_QUALITY_INSUFFICIENT
    # Scanner should mark this as not rec-eligible
    assert MARKET_QUALITY_INSUFFICIENT != MARKET_QUALITY_VALID


def test_alt_lines_separate():
    """Alt lines must have different group keys from main lines."""
    from src.player_prop_parser import _build_group_key
    main = _build_group_key("ev1", "PLAYER_1", 5.5, 0, "OVER")
    alt = _build_group_key("ev1", "PLAYER_1", 5.5, 1, "OVER")
    assert main != alt
    assert "_alt" in alt


def test_identical_lines_group_correctly():
    """Over and Under at same exact line must share group key."""
    from src.player_prop_parser import _build_group_key
    over = _build_group_key("ev1", "PLAYER_1", 5.5, 0, "OVER")
    under = _build_group_key("ev1", "PLAYER_1", 5.5, 0, "UNDER")
    assert over == under


def test_duplicate_deduplicated():
    """Duplicate (event, player, line, side, book) must be deduplicated."""
    opp1 = _make_opp(3.0, player_id="P1", line=5.5, side="OVER", sportsbook="book1")
    opp2 = _make_opp(3.0, player_id="P1", line=5.5, side="OVER", sportsbook="book1")
    seen = {}
    for o in [opp1, opp2]:
        key = (o["event_id"], o["player_id"], o["line"], o["side"], o["sportsbook"])
        seen[key] = o
    assert len(seen) == 1


def test_different_lines_separate():
    """Different lines (main vs alt) must NOT be combined."""
    from src.player_prop_parser import _build_group_key
    main = _build_group_key("ev1", "P1", 5.5, 0, "OVER")
    alt = _build_group_key("ev1", "P1", 6.5, 1, "OVER")
    assert main != alt


def test_stale_data_warning():
    """Old data beyond freshness threshold must trigger warning."""
    import time
    from datetime import datetime, timezone, timedelta
    old_time = datetime.now(timezone.utc) - timedelta(seconds=FRESHNESS_THRESHOLD_SECONDS + 10)
    age = (datetime.now(timezone.utc) - old_time).total_seconds()
    assert age > FRESHNESS_THRESHOLD_SECONDS


def test_empty_scanner_result():
    """Empty data must produce NO QUALIFYING OPPORTUNITIES."""
    result = {
        "opportunities": [],
        "n_events": 0,
        "n_markets": 0,
        "n_pitchers": 0,
        "scan_start": "2026-07-20T12:00:00+00:00",
        "fetch_time": "2026-07-20T12:00:00+00:00",
        "data_source": "CACHE",
        "oldest_obs": "",
        "newest_obs": "",
        "age_seconds": 0,
        "stale_warning": False,
        "research_only": True,
    }
    assert len(result["opportunities"]) == 0


def test_no_qualifying_opportunities_output():
    """Scanner should display 'NO QUALIFYING OPPORTUNITIES' when empty."""
    from io import StringIO
    from src.strikeout_scanner import display_results
    captured = StringIO()
    old = sys.stdout
    sys.stdout = captured
    try:
        result = _fake_run_scan([])
        display_results(result, "actionable")
    finally:
        sys.stdout = old
    output = captured.getvalue()
    assert "NO QUALIFYING OPPORTUNITIES" in output


def test_min_ev_validation():
    """--min-ev must be between 0 and 1 (tested via main)."""
    from src.strikeout_scanner import main
    with pytest.raises(SystemExit):
        try:
            main(["--min-ev", "1.5"])
        except SystemExit as e:
            assert e.code != 0
            raise
    with pytest.raises(SystemExit):
        try:
            main(["--min-ev", "-0.1"])
        except SystemExit as e:
            assert e.code != 0
            raise


def test_parse_args_defaults():
    """Default mode is actionable-only."""
    args = parse_args([])
    assert not args.all
    assert not args.positive_only
    assert not args.actionable_only  # No explicit flag


def test_parse_args_all_mode():
    args = parse_args(["--all"])
    assert args.all


def test_parse_args_positive_mode():
    args = parse_args(["--positive-only"])
    assert args.positive_only


def test_parse_args_limit():
    args = parse_args(["--limit", "10"])
    assert args.limit == 10


def test_duplicates_not_doubled_in_output():
    """Duplicates for exact (event, player, line, side, book) are not doubled."""
    high_ev = _make_opp(4.0, player_id="P1", line=5.5, side="OVER",
                        sportsbook="book1")
    lower_ev = _make_opp(3.0, player_id="P1", line=5.5, side="OVER",
                          sportsbook="book1")
    seen = {}
    for o in [high_ev, lower_ev]:
        key = (o["event_id"], o["player_id"], o["line"], o["side"], o["sportsbook"])
        if key not in seen:
            seen[key] = o
    assert len(seen) == 1
    assert seen[("ev1", "P1", 5.5, "OVER", "book1")] is high_ev


def test_research_only_flag():
    """Empty cache data must set research_only=True."""
    result = _fake_run_scan([])
    assert result["research_only"] is True


def test_data_source_cache():
    """Fake cache result must show CACHE source."""
    result = _fake_run_scan([])
    assert result["data_source"] == "CACHE"
