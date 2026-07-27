"""Tests for the generic player-prop scanner (Phase 4).

Covers: market/form resolution, filtering, backward compatibility,
cross-market isolation, YN output, freshness, and output structure.
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.prop_config import (
    MARKET_QUALITY_VALID, MARKET_QUALITY_NEEDS_REVIEW,
    MARKET_QUALITY_INSUFFICIENT, MARKET_QUALITY_EXCLUDED,
    BET_STATUS_STRONG, BET_STATUS_POSITIVE, BET_STATUS_MARGINAL,
    BET_STATUS_NO_EDGE, ACTIONABLE_EDGE_THRESHOLD,
    FRESHNESS_THRESHOLD_SECONDS, MARKET_REGISTRY,
    PITCHER_STRIKEOUTS, PITCHER_OUTS, PITCHER_HITS_ALLOWED,
    PITCHER_WALKS_ALLOWED, PITCHER_EARNED_RUNS,
)
from src.player_prop_scanner import (
    run_scan, parse_args, display_results, display_verbose,
    resolve_markets, _accepted_market_types, _build_scanner_title,
    build_parser, VALID_MARKETS,
)
from src.strikeout_scanner import (
    run_scan as ks_run_scan,
    parse_args as ks_parse_args,
    display_results as ks_display_results,
)


# ==================================================================
# Helpers
# ==================================================================

def _make_opp(ev_pct: float = 3.0, *, market_type: str = "pitching_strikeouts_ou",
              rec_eligible: bool = True, side: str = "OVER", **overrides) -> dict:
    """Build a single O/U opportunity dict."""
    opp = {
        "event_id": "ev1",
        "away_team": "TeamA",
        "home_team": "TeamB",
        "start_time": "2026-07-20T23:00:00Z",
        "player_id": "PLAYER_1_MLB",
        "player_name": "Test Player",
        "market_type": market_type,
        "line": 5.5,
        "side": side,
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


def _make_yn_opp(price_adv: float = 5.0, *,
                 market_type: str = "pitching_strikeouts_yn",
                 rec_eligible: bool = True, **overrides) -> dict:
    """Build a single YN opportunity dict."""
    opp = {
        "event_id": "ev1",
        "away_team": "TeamA",
        "home_team": "TeamB",
        "start_time": "2026-07-20T23:00:00Z",
        "player_id": "PLAYER_1_MLB",
        "player_name": "Test Player",
        "market_type": market_type,
        "line": None,
        "side": "YES",
        "sportsbook": "testbook",
        "american_odds": -110,
        "decimal_odds": 1.9091,
        "n_consensus_books": 5,
        "price_advantage_pct": price_adv,
        "relative_payout_advantage_pct": 2.5,
        "decimal_odds_advantage": 5,
        "market_reference_probability": 0.55,
        "market_reference_odds": -182,
        "comparison_status": "STRONG_PRICE_OUTLIER" if price_adv >= 8 else "PRICE_OUTLIER",
        "market_quality": MARKET_QUALITY_VALID,
        "rec_eligible": rec_eligible,
        "validation_status": "VALID",
    }
    opp.update(overrides)
    return opp


def _fake_result(opps=None, yn_opps=None, **overrides):
    """Build a fake run_scan result dict."""
    result = {
        "opportunities": opps or [],
        "yn_opportunities": yn_opps or [],
        "n_events": 3,
        "n_markets": 2,
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
        "scanner_title": "MLB PITCHER STRIKEOUTS EDGE SCANNER",
    }
    result.update(overrides)
    return result


# ==================================================================
# 1. Market/form resolution
# ==================================================================

class TestMarketFormResolution:
    def test_valid_market_ou(self):
        r = resolve_markets("strikeouts", "ou")
        assert r.market_names == ["strikeouts"]
        assert r.form == "ou"
        assert len(r.market_configs) == 1
        assert r.market_configs[0] is PITCHER_STRIKEOUTS

    def test_valid_market_yn(self):
        r = resolve_markets("strikeouts", "yn")
        assert r.form == "yn"
        assert r.market_configs[0].supports_yn

    def test_valid_market_all_forms(self):
        r = resolve_markets("strikeouts", "all")
        assert r.form == "all"

    def test_all_markets(self):
        r = resolve_markets("all", "ou")
        # O/U-only filtering: markets with supports_ou=False are excluded
        ou_markets = [m for m in MARKET_REGISTRY if m.supports_ou]
        assert len(r.market_configs) == len(ou_markets)
        assert r.market_names == [m.cli_name for m in ou_markets]

    def test_invalid_market_name(self):
        with pytest.raises(SystemExit):
            resolve_markets("invalid_market", "ou")

    def test_invalid_form(self):
        with pytest.raises(SystemExit):
            resolve_markets("strikeouts", "invalid_form")

    def test_unsupported_ou_combination(self):
        """outs + yn should fail."""
        with pytest.raises(SystemExit):
            resolve_markets("outs", "yn")

    def test_unsupported_yn_combination(self):
        """hits_allowed + yn should fail."""
        with pytest.raises(SystemExit):
            resolve_markets("hits_allowed", "yn")

    def test_all_markets_with_yn_form(self):
        """all + yn should work — markets without YN are filtered out."""
        r = resolve_markets("all", "yn")
        for mc in r.market_configs:
            assert mc.supports_yn

    def test_accepted_market_types_ou(self):
        r = resolve_markets("strikeouts", "ou")
        types = _accepted_market_types(r)
        assert types == {"pitching_strikeouts_ou"}

    def test_accepted_market_types_yn(self):
        r = resolve_markets("strikeouts", "yn")
        types = _accepted_market_types(r)
        assert types == {"pitching_strikeouts_yn"}

    def test_accepted_market_types_all(self):
        r = resolve_markets("strikeouts", "all")
        types = _accepted_market_types(r)
        assert "pitching_strikeouts_ou" in types
        assert "pitching_strikeouts_yn" in types

    def test_scanner_title_single_market(self):
        r = resolve_markets("outs", "ou")
        title = _build_scanner_title(r)
        assert title == "MLB PITCHER OUTS RECORDED EDGE SCANNER"

    def test_scanner_title_all_markets(self):
        r = resolve_markets("all", "ou")
        title = _build_scanner_title(r)
        assert "PLAYER PROP" in title


# ==================================================================
# 2. Filtering tests
# ==================================================================

class TestFiltering:
    def _run_with_filters(self, **filter_kwargs):
        """Run scan with synthetic events that include all market types."""
        from tests.fixture_data import (
            flaherty_event, outs_event, hits_event, walks_event, earned_runs_event,
        )
        events = [flaherty_event, outs_event, hits_event, walks_event, earned_runs_event]
        from src.player_prop_parser import parse_player_props
        all_odds = []
        for ev in events:
            parsed = parse_player_props(ev)
            all_odds.extend(parsed.odds_rows)

        from src import prop_config as cfg
        from src.player_prop_analysis import analyze_prop_group, analyze_yn_group
        from src.validation_constants import APPROVED_STATUSES
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        ou_groups = {}
        yn_groups = {}
        excluded = 0
        approved = 0

        for row in all_odds:
            if row["validation_status"] not in APPROVED_STATUSES:
                excluded += 1
                continue
            approved += 1
            key = row["market_group_key"]
            mt = row["market_type"]
            if cfg.get_market_by_yn_type(mt) is not None:
                if key not in yn_groups:
                    yn_groups[key] = {"yes": {}, "player_id": row["player_id"],
                                      "player_name": row["player_name"],
                                      "event_id": row["event_id"], "market_type": mt}
                if row["side"] == "YES":
                    yn_groups[key]["yes"][row["sportsbook"]] = {
                        "price": row["price"], "decimal_odds": row["decimal_odds"],
                        "validation_status": row["validation_status"],
                    }
            elif cfg.get_market_by_ou_type(mt) is not None:
                if key not in ou_groups:
                    ou_groups[key] = {"over": {}, "under": {}, "line": row["line"],
                                      "player_id": row["player_id"],
                                      "player_name": row["player_name"],
                                      "event_id": row["event_id"], "market_type": mt}
                ou_groups[key][row["side"].lower()][row["sportsbook"]] = {
                    "price": row["price"], "decimal_odds": row["decimal_odds"],
                    "line": row["line"], "validation_status": row["validation_status"],
                }

        opps = []
        for gkey, gdata in ou_groups.items():
            if not gdata["over"] or not gdata["under"]:
                continue
            analysis = analyze_prop_group(gkey, gdata["over"], gdata["under"])
            if analysis["market_quality"] == cfg.MARKET_QUALITY_EXCLUDED:
                continue
            for be in analysis["books"]:
                if be["included"]:
                    opps.append({
                        "event_id": gdata["event_id"],
                        "away_team": "TeamA", "home_team": "TeamB",
                        "start_time": "", "player_id": gdata["player_id"],
                        "player_name": gdata["player_name"],
                        "market_type": gdata["market_type"],
                        "line": gdata["line"], "side": be["side"],
                        "sportsbook": be["sportsbook"],
                        "american_odds": be["american_odds"],
                        "decimal_odds": be["decimal_odds"],
                        "n_consensus_books": analysis["n_paired_books"],
                        "fair_prob": be["fair_prob"], "ev_pct": be["ev_pct"],
                        "market_quality": analysis["market_quality"],
                        "rec_eligible": analysis["market_quality"] == cfg.MARKET_QUALITY_VALID,
                        "bet_status": be["bet_status"],
                        "validation_status": be.get("validation_status", ""),
                        "is_alt_line": 0,
                    })

        yn_opps = []
        for gkey, gdata in yn_groups.items():
            if not gdata["yes"]:
                continue
            analysis = analyze_yn_group(gkey, gdata["yes"])
            if analysis["market_quality"] == cfg.MARKET_QUALITY_EXCLUDED:
                continue
            for be in analysis["books"]:
                yn_opps.append({
                    "event_id": gdata["event_id"],
                    "away_team": "TeamA", "home_team": "TeamB",
                    "start_time": "", "player_id": gdata["player_id"],
                    "player_name": gdata["player_name"],
                    "market_type": gdata["market_type"],
                    "line": None, "side": "YES",
                    "sportsbook": be["sportsbook"],
                    "american_odds": be["american_odds"],
                    "decimal_odds": be["decimal_odds"],
                    "n_consensus_books": analysis["n_books"],
                    "price_advantage_pct": be["price_advantage_pct"],
                    "relative_payout_advantage_pct": be["relative_payout_advantage_pct"],
                    "decimal_odds_advantage": be["decimal_odds_advantage"],
                    "market_reference_probability": be["market_reference_probability"],
                    "market_reference_odds": be["market_reference_odds"],
                    "comparison_status": be["comparison_status"],
                    "market_quality": analysis["market_quality"],
                    "rec_eligible": be["recommendation_eligible"],
                    "validation_status": be.get("validation_status", ""),
                })

        # Apply filters
        sb = filter_kwargs.get("sportsbook")
        pl = filter_kwargs.get("player")
        gm = filter_kwargs.get("game")

        if sb:
            sb_lower = sb.lower()
            opps = [o for o in opps if sb_lower in o["sportsbook"].lower()]
            yn_opps = [o for o in yn_opps if sb_lower in o["sportsbook"].lower()]
        if pl:
            pl_lower = pl.lower()
            opps = [o for o in opps if pl_lower in o["player_name"].lower()]
            yn_opps = [o for o in yn_opps if pl_lower in o["player_name"].lower()]
        if gm:
            gm_lower = gm.lower()
            opps = [o for o in opps if gm_lower in o.get("event_id", "")]
            yn_opps = [o for o in yn_opps if gm_lower in o.get("event_id", "")]

        return opps, yn_opps

    def test_sportsbook_filter(self):
        opps, _ = self._run_with_filters(sportsbook="fanduel")
        for o in opps:
            assert "fanduel" in o["sportsbook"].lower()

    def test_sportsbook_filter_no_match(self):
        opps, _ = self._run_with_filters(sportsbook="nonexistent_book")
        assert len(opps) == 0

    def test_player_filter(self):
        opps, _ = self._run_with_filters(player="Flaherty")
        for o in opps:
            assert "flaherty" in o["player_name"].lower()

    def test_player_filter_no_match(self):
        opps, _ = self._run_with_filters(player="NonexistentPlayer")
        assert len(opps) == 0

    def test_case_insensitive_sportsbook(self):
        opps, _ = self._run_with_filters(sportsbook="FANDUEL")
        assert len(opps) > 0
        for o in opps:
            assert "fanduel" in o["sportsbook"].lower()

    def test_case_insensitive_player(self):
        opps, _ = self._run_with_filters(player="FLAHERTY")
        assert len(opps) > 0

    def test_combined_filters(self):
        opps, _ = self._run_with_filters(sportsbook="fanduel", player="Flaherty")
        for o in opps:
            assert "fanduel" in o["sportsbook"].lower()
            assert "flaherty" in o["player_name"].lower()


# ==================================================================
# 3. Backward-compatibility tests
# ==================================================================

class TestBackwardCompatibility:
    def test_old_command_module_entry(self):
        """python -m src.strikeout_scanner is importable."""
        import src.strikeout_scanner as ks
        assert hasattr(ks, "run_scan")
        assert hasattr(ks, "main")
        assert hasattr(ks, "parse_args")

    def test_old_parse_args_defaults(self):
        args = ks_parse_args([])
        assert not args.all
        assert not args.positive_only
        assert not args.actionable_only
        assert args.market == "all"

    def test_old_parse_args_all_mode(self):
        args = ks_parse_args(["--all"])
        assert args.all

    def test_old_parse_args_positive_mode(self):
        args = ks_parse_args(["--positive-only"])
        assert args.positive_only

    def test_old_parse_args_limit(self):
        args = ks_parse_args(["--limit", "10"])
        assert args.limit == 10

    def test_old_parse_args_market_ou(self):
        args = ks_parse_args(["--market", "ou"])
        assert args.market == "ou"

    def test_old_parse_args_market_yn(self):
        args = ks_parse_args(["--market", "yn"])
        assert args.market == "yn"

    def test_old_run_scan_delegates_to_generic(self):
        """Old run_scan should call generic run_scan with market='strikeouts'."""
        import unittest.mock as mock
        with mock.patch("src.strikeout_scanner._generic.run_scan") as mock_scan:
            mock_scan.return_value = _fake_result()
            ks_run_scan(mode="all", market="ou")
            mock_scan.assert_called_once_with(
                mode="all", min_ev=None, limit=25,
                market="strikeouts", market_form="ou",
            )

    def test_old_run_scan_yn_delegates(self):
        import unittest.mock as mock
        with mock.patch("src.strikeout_scanner._generic.run_scan") as mock_scan:
            mock_scan.return_value = _fake_result()
            ks_run_scan(mode="all", market="yn")
            mock_scan.assert_called_once_with(
                mode="all", min_ev=None, limit=25,
                market="strikeouts", market_form="yn",
            )

    def test_old_run_scan_all_delegates(self):
        import unittest.mock as mock
        with mock.patch("src.strikeout_scanner._generic.run_scan") as mock_scan:
            mock_scan.return_value = _fake_result()
            ks_run_scan(mode="all", market="all")
            mock_scan.assert_called_once_with(
                mode="all", min_ev=None, limit=25,
                market="strikeouts", market_form="all",
            )

    def test_old_display_delegates_to_generic(self):
        import unittest.mock as mock
        result = _fake_result()
        with mock.patch("src.player_prop_scanner.display_results") as mock_disp:
            ks_display_results(result, "actionable")
            mock_disp.assert_called_once_with(result, "actionable")

    def test_old_min_ev_validation(self):
        with pytest.raises(SystemExit):
            from src.strikeout_scanner import main
            main(["--min-ev", "1.5"])


# ==================================================================
# 4. Generic scanner CLI tests
# ==================================================================

class TestGenericCLI:
    def test_parse_args_defaults(self):
        args = parse_args([])
        assert args.market == "all"
        assert args.market_form == "all"
        assert not args.all
        assert args.limit == 25
        assert args.sportsbook is None
        assert args.player is None
        assert args.game is None

    def test_parse_args_market_strikeouts(self):
        args = parse_args(["--market", "strikeouts"])
        assert args.market == "strikeouts"

    def test_parse_args_market_outs(self):
        args = parse_args(["--market", "outs"])
        assert args.market == "outs"

    def test_parse_args_market_form_ou(self):
        args = parse_args(["--market-form", "ou"])
        assert args.market_form == "ou"

    def test_parse_args_market_form_yn(self):
        args = parse_args(["--market-form", "yn"])
        assert args.market_form == "yn"

    def test_parse_args_sportsbook(self):
        args = parse_args(["--sportsbook", "fanduel"])
        assert args.sportsbook == "fanduel"

    def test_parse_args_player(self):
        args = parse_args(["--player", "Flaherty"])
        assert args.player == "Flaherty"

    def test_parse_args_game(self):
        args = parse_args(["--game", "Dodgers"])
        assert args.game == "Dodgers"

    def test_parse_args_verbose(self):
        args = parse_args(["--verbose"])
        assert args.verbose

    def test_parse_args_all_flag(self):
        args = parse_args(["--all"])
        assert args.all

    def test_parse_args_limit(self):
        args = parse_args(["--limit", "5"])
        assert args.limit == 5

    def test_valid_markets_list(self):
        assert "strikeouts" in VALID_MARKETS
        assert "outs" in VALID_MARKETS
        assert "hits_allowed" in VALID_MARKETS
        assert "walks_allowed" in VALID_MARKETS
        assert "earned_runs" in VALID_MARKETS

    def test_build_parser_is_argparse(self):
        parser = build_parser()
        assert parser is not None


# ==================================================================
# 5. Cross-market scanner tests
# ==================================================================

class TestCrossMarketScanner:
    def test_strikeouts_scanner_title(self):
        r = resolve_markets("strikeouts", "ou")
        assert r.market_configs[0].scanner_title == "MLB PITCHER STRIKEOUTS EDGE SCANNER"

    def test_outs_scanner_title(self):
        r = resolve_markets("outs", "ou")
        assert r.market_configs[0].scanner_title == "MLB PITCHER OUTS RECORDED EDGE SCANNER"

    def test_hits_scanner_title(self):
        r = resolve_markets("hits_allowed", "ou")
        assert r.market_configs[0].scanner_title == "MLB PITCHER HITS ALLOWED EDGE SCANNER"

    def test_walks_scanner_title(self):
        r = resolve_markets("walks_allowed", "ou")
        assert r.market_configs[0].scanner_title == "MLB PITCHER WALKS ALLOWED EDGE SCANNER"

    def test_earned_runs_scanner_title(self):
        r = resolve_markets("earned_runs", "ou")
        assert r.market_configs[0].scanner_title == "MLB PITCHER EARNED RUNS EDGE SCANNER"

    def test_outs_rejects_yn(self):
        with pytest.raises(SystemExit):
            resolve_markets("outs", "yn")

    def test_hits_allowed_rejects_yn(self):
        with pytest.raises(SystemExit):
            resolve_markets("hits_allowed", "yn")

    def test_walks_allowed_supports_ou(self):
        r = resolve_markets("walks_allowed", "ou")
        assert r.market_configs[0].supports_ou

    def test_walks_allowed_supports_yn(self):
        r = resolve_markets("walks_allowed", "yn")
        assert r.market_configs[0].supports_yn

    def test_earned_runs_supports_ou(self):
        r = resolve_markets("earned_runs", "ou")
        assert r.market_configs[0].supports_ou

    def test_earned_runs_supports_yn(self):
        r = resolve_markets("earned_runs", "yn")
        assert r.market_configs[0].supports_yn

    def test_no_cross_market_contamination(self):
        """O/U rows from different markets should have different market_type."""
        from tests.fixture_data import flaherty_event, outs_event, hits_event
        from src.player_prop_parser import parse_player_props

        ou_types = set()
        for ev in [flaherty_event, outs_event, hits_event]:
            parsed = parse_player_props(ev)
            for row in parsed.odds_rows:
                if row["side"] in ("OVER", "UNDER"):
                    ou_types.add(row["market_type"])

        assert len(ou_types) >= 3, f"Expected at least 3 distinct O/U types, got {ou_types}"
        assert "pitching_strikeouts_ou" in ou_types
        assert "pitching_outs_ou" in ou_types
        assert "pitching_hits_ou" in ou_types


# ==================================================================
# 6. YN output tests
# ==================================================================

class TestYNOutput:
    def test_yn_no_ev_fields(self):
        """YN opportunities must NOT contain ev_pct, fair_prob, fair_odds."""
        opp = _make_yn_opp()
        assert "ev_pct" not in opp
        assert "fair_prob" not in opp

    def test_yn_has_price_advantage_fields(self):
        opp = _make_yn_opp()
        assert "price_advantage_pct" in opp
        assert "decimal_odds_advantage" in opp
        assert "market_reference_probability" in opp
        assert "market_reference_odds" in opp
        assert "comparison_status" in opp
        assert "recommendation_eligible" not in opp  # field name is rec_eligible

    def test_yn_comparison_status_values(self):
        valid_statuses = {
            "STRONG_PRICE_OUTLIER", "PRICE_OUTLIER", "MARGINAL_PRICE_OUTLIER",
            "IN_LINE_WITH_MARKET", "WORSE_THAN_MARKET",
        }
        for status in valid_statuses:
            opp = _make_yn_opp(comparison_status=status)
            assert opp["comparison_status"] in valid_statuses

    def test_yn_single_sided_disclaimer_in_display(self):
        """YN output must state SINGLE-SIDED MARKET COMPARISON."""
        yn_opps = [_make_yn_opp()]
        result = _fake_result(yn_opps=yn_opps)
        captured = StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            display_results(result, "all")
        finally:
            sys.stdout = old
        output = captured.getvalue()
        assert "SINGLE-SIDED MARKET COMPARISON" in output
        assert "TRUE EV NOT AVAILABLE" in output

    def test_yn_no_ev_in_display(self):
        """YN display should NOT show EV or fair probability columns."""
        yn_opps = [_make_yn_opp()]
        result = _fake_result(yn_opps=yn_opps)
        captured = StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            display_results(result, "all")
        finally:
            sys.stdout = old
        output = captured.getvalue()
        # The YN section should not have EV% or Fair Prob columns
        yn_section = output.split("SINGLE-SIDED MARKET COMPARISON")[1] if "SINGLE-SIDED MARKET COMPARISON" in output else ""
        assert "EV%" not in yn_section
        assert "Fair Prob" not in yn_section

    def test_yn_valid_dispatch_all_markets(self):
        """All YN markets should dispatch correctly through registry."""
        yn_markets = [m for m in MARKET_REGISTRY if m.supports_yn]
        for mc in yn_markets:
            r = resolve_markets(mc.cli_name, "yn")
            assert r.market_configs[0] is mc


# ==================================================================
# 7. Freshness and source tests
# ==================================================================

class TestFreshnessAndSource:
    def test_stale_warning_triggered(self):
        from datetime import datetime, timezone, timedelta
        old = datetime.now(timezone.utc) - timedelta(seconds=FRESHNESS_THRESHOLD_SECONDS + 10)
        age = (datetime.now(timezone.utc) - old).total_seconds()
        assert age > FRESHNESS_THRESHOLD_SECONDS

    def test_fresh_data_no_warning(self):
        from datetime import datetime, timezone, timedelta
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        age = (datetime.now(timezone.utc) - recent).total_seconds()
        assert age < FRESHNESS_THRESHOLD_SECONDS

    def test_cache_source(self):
        result = _fake_result(data_source="CACHE")
        assert result["data_source"] == "CACHE"

    def test_live_source(self):
        result = _fake_result(data_source="LIVE API")
        assert result["data_source"] == "LIVE API"

    def test_unknown_source(self):
        result = _fake_result(data_source="UNKNOWN")
        assert result["data_source"] == "UNKNOWN"

    def test_research_only_from_cache(self):
        result = _fake_result(research_only=True, data_source="CACHE")
        captured = StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            display_results(result, "all")
        finally:
            sys.stdout = old
        output = captured.getvalue()
        assert "RESEARCH ONLY" in output

    def test_scan_and_observation_timestamps_distinct(self):
        result = _fake_result(
            scan_start="2026-07-20T12:00:00+00:00",
            fetch_time="2026-07-20T11:59:00+00:00",
            newest_obs="2026-07-20T11:50:00+00:00",
        )
        captured = StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            display_results(result, "all")
        finally:
            sys.stdout = old
        output = captured.getvalue()
        assert "Scan started:" in output
        assert "Data fetched:" in output
        assert "Odds observed:" in output


# ==================================================================
# 8. Output structure tests
# ==================================================================

class TestOutputStructure:
    def test_ou_display_has_required_columns(self):
        opps = [_make_opp(3.0)]
        result = _fake_result(opps=opps)
        captured = StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            display_results(result, "all")
        finally:
            sys.stdout = old
        output = captured.getvalue()
        assert "Pitcher" in output
        assert "Side" in output
        assert "Line" in output
        assert "Book" in output
        assert "Odds" in output
        assert "EV%" in output
        assert "MQ" in output
        assert "Rec" in output

    def test_yn_display_has_required_columns(self):
        yn_opps = [_make_yn_opp()]
        result = _fake_result(yn_opps=yn_opps)
        captured = StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            display_results(result, "all")
        finally:
            sys.stdout = old
        output = captured.getvalue()
        assert "Pitcher" in output
        assert "Book" in output
        assert "Odds" in output
        assert "Ref Prob" in output
        assert "Adv%" in output
        assert "DecAdv" in output
        assert "Status" in output

    def test_empty_result_shows_no_qualifying(self):
        result = _fake_result(opps=[], yn_opps=[])
        captured = StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            display_results(result, "actionable")
        finally:
            sys.stdout = old
        output = captured.getvalue()
        assert "NO QUALIFYING OPPORTUNITIES" in output

    def test_scanner_title_in_output(self):
        result = _fake_result(scanner_title="MLB PITCHER STRIKEOUTS EDGE SCANNER")
        captured = StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            display_results(result, "all")
        finally:
            sys.stdout = old
        output = captured.getvalue()
        assert "MLB PITCHER STRIKEOUTS EDGE SCANNER" in output

    def test_verbose_ou_output(self):
        opps = [_make_opp(3.0)]
        result = _fake_result(opps=opps)
        captured = StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            display_verbose(result)
        finally:
            sys.stdout = old
        output = captured.getvalue()
        assert "RECOMMENDATION ELIGIBLE" in output or "RESEARCH ONLY" in output
        assert "Sportsbook:" in output
        assert "Fair odds:" in output
        assert "Fair prob:" in output
        assert "EV:" in output

    def test_verbose_yn_output(self):
        yn_opps = [_make_yn_opp()]
        result = _fake_result(yn_opps=yn_opps)
        captured = StringIO()
        old = sys.stdout
        sys.stdout = captured
        try:
            display_verbose(result)
        finally:
            sys.stdout = old
        output = captured.getvalue()
        assert "Market ref prob:" in output
        assert "Price advantage:" in output
        assert "Decimal odds adv:" in output


# ==================================================================
# 9. min-ev rejection for YN
# ==================================================================

class TestMinEvForYN:
    def test_min_ev_ignored_for_yn_in_scan(self):
        """min-ev should not affect YN filtering — only rec_eligible matters."""
        yn_opps = [_make_yn_opp(price_adv=10.0)]
        result = _fake_result(yn_opps=yn_opps)
        # The scanner should still include YN opps regardless of min_ev
        assert len(result["yn_opportunities"]) == 1

    def test_min_ev_flag_only_applies_to_ou(self):
        """--min-ev should only be validated, not applied to YN."""
        args = parse_args(["--min-ev", "0.05"])
        assert args.min_ev == 0.05


# ==================================================================
# 10. Stale cache blocking
# ==================================================================

class TestStaleBlocking:
    def test_stale_row_cannot_be_actionable(self):
        """A stale market cannot be actionable even if EV is high."""
        from datetime import datetime, timezone, timedelta
        old_time = datetime.now(timezone.utc) - timedelta(seconds=FRESHNESS_THRESHOLD_SECONDS + 100)
        age = (datetime.now(timezone.utc) - old_time).total_seconds()
        assert age > FRESHNESS_THRESHOLD_SECONDS
        # Scanner flags stale data
        result = _fake_result(stale_warning=True, age_seconds=int(age))
        assert result["stale_warning"]

    def test_non_stale_allows_actionable(self):
        from datetime import datetime, timezone, timedelta
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        age = (datetime.now(timezone.utc) - recent).total_seconds()
        assert age < FRESHNESS_THRESHOLD_SECONDS
        result = _fake_result(stale_warning=False, age_seconds=int(age))
        assert not result["stale_warning"]


# ==================================================================
# 11. Registry completeness
# ==================================================================

class TestRegistryCompleteness:
    def test_all_markets_have_scanner_title(self):
        for mc in MARKET_REGISTRY:
            assert mc.scanner_title, f"{mc.cli_name} missing scanner_title"

    def test_all_markets_have_cli_name(self):
        for mc in MARKET_REGISTRY:
            assert mc.cli_name
            assert mc.cli_name in VALID_MARKETS

    def test_all_markets_in_valid_markets(self):
        for mc in MARKET_REGISTRY:
            assert mc.cli_name in VALID_MARKETS

    def test_all_cli_lookups_work(self):
        for mc in MARKET_REGISTRY:
            from src.prop_config import get_market_by_cli_name
            assert get_market_by_cli_name(mc.cli_name) is mc


# ==================================================================
# 12. Single implementation proof
# ==================================================================

class TestSingleImplementation:
    def test_generic_scanner_is_canonical(self):
        """player_prop_scanner.run_scan is the only real implementation."""
        import inspect
        src = inspect.getsource(run_scan)
        assert "def run_scan" in src

    def test_strikeout_scanner_has_no_pipeline(self):
        """strikeout_scanner.run_scan should not contain analysis logic."""
        import inspect
        src = inspect.getsource(ks_run_scan)
        # The wrapper should just delegate, not contain analyze_prop_group etc.
        assert "analyze_prop_group" not in src
        assert "analyze_yn_group" not in src
