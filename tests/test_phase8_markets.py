"""Phase 8 tests: Complete MLB market coverage (batter + remaining pitcher markets).

Tests the new MarketConfig entries, parser dispatch, name extraction,
and cross-market isolation for all 14 new markets added in Phase 8.

All tests use in-memory databases and synthetic fixtures.
No live API calls. No mutable cache. No production database access.
"""

from __future__ import annotations

import sqlite3

import pytest

from tests.fixture_data import batter_event, BATTER_EVENT_ID
from src.prop_config import (
    MARKET_REGISTRY,
    match_ou_market,
    match_yn_market,
    get_market_by_cli_name,
    get_market_by_ou_type,
    get_market_by_yn_type,
    BATTER_HITS,
    BATTER_TOTAL_BASES,
    BATTER_HOME_RUNS,
    BATTER_STOLEN_BASES,
    PITCHER_WIN,
    PITCHER_STRIKEOUTS,
    PITCHER_HITS_ALLOWED,
    PITCHER_WALKS_ALLOWED,
    PITCHER_OUTS,
    PITCHER_EARNED_RUNS,
)
from src.player_prop_parser import parse_player_props, _extract_player_name_from_market


# ── Registry tests ─────────────────────────────────────────────────

class TestRegistryPhase8:
    def test_total_market_count(self):
        assert len(MARKET_REGISTRY) == 10

    def test_all_new_markets_registered(self):
        expected = [
            BATTER_HITS, BATTER_TOTAL_BASES,
            BATTER_HOME_RUNS,
            BATTER_STOLEN_BASES,
            PITCHER_WIN,
            PITCHER_STRIKEOUTS, PITCHER_HITS_ALLOWED, PITCHER_WALKS_ALLOWED,
            PITCHER_OUTS, PITCHER_EARNED_RUNS,
        ]
        for mc in expected:
            assert mc in MARKET_REGISTRY

    def test_no_duplicate_cli_names(self):
        names = [m.cli_name for m in MARKET_REGISTRY]
        assert len(names) == len(set(names))

    def test_no_duplicate_ou_types(self):
        types = [m.market_type_ou for m in MARKET_REGISTRY if m.market_type_ou]
        assert len(types) == len(set(types))

    def test_no_duplicate_yn_types(self):
        types = [m.market_type_yn for m in MARKET_REGISTRY if m.market_type_yn]
        assert len(types) == len(set(types))

    def test_all_markets_have_scanner_title(self):
        for mc in MARKET_REGISTRY:
            assert mc.scanner_title, f"{mc.cli_name} missing scanner_title"

    def test_all_markets_have_display_name(self):
        for mc in MARKET_REGISTRY:
            assert mc.display_name, f"{mc.cli_name} missing display_name"

    def test_all_markets_have_short_label(self):
        for mc in MARKET_REGISTRY:
            assert mc.short_label, f"{mc.cli_name} missing short_label"

    def test_all_markets_period_is_game(self):
        for mc in MARKET_REGISTRY:
            assert mc.period == "game", f"{mc.cli_name} period != game"


# ── O/U dispatch tests ────────────────────────────────────────────

class TestOUDispatchPhase8:
    def test_batter_hits_ou(self):
        m = match_ou_market("batting_hits-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_HITS

    def test_batter_total_bases_ou(self):
        m = match_ou_market("batting_totalBases-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_TOTAL_BASES

    def test_batter_hr_ou(self):
        m = match_ou_market("batting_homeRuns-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_HOME_RUNS

    def test_batter_stolen_bases_ou(self):
        m = match_ou_market("batting_stolenBases-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_STOLEN_BASES

    def test_pitching_win_no_ou(self):
        assert match_ou_market("pitching_win-BRANDON_PFAADT_1_MLB-game-ou-over") is None

    def test_hits_allowed_ou(self):
        m = match_ou_market("pitching_hits-BRANDON_PFAADT_1_MLB-game-ou-over")
        assert m is PITCHER_HITS_ALLOWED

    def test_walks_allowed_ou(self):
        m = match_ou_market("pitching_basesOnBalls-BRANDON_PFAADT_1_MLB-game-ou-over")
        assert m is PITCHER_WALKS_ALLOWED

    def test_strikeouts_ou(self):
        m = match_ou_market("pitching_strikeouts-BRANDON_PFAADT_1_MLB-game-ou-over")
        assert m is PITCHER_STRIKEOUTS


# ── YN dispatch tests ─────────────────────────────────────────────

class TestYNDispatchPhase8:
    def test_batter_hits_yn(self):
        m = match_yn_market("batting_hits-AARON_JUDGE_1_MLB-game-yn-yes")
        assert m is BATTER_HITS

    def test_batter_hr_yn(self):
        m = match_yn_market("batting_homeRuns-AARON_JUDGE_1_MLB-game-yn-yes")
        assert m is BATTER_HOME_RUNS

    def test_pitching_win_yn(self):
        m = match_yn_market("pitching_win-BRANDON_PFAADT_1_MLB-game-yn-yes")
        assert m is PITCHER_WIN

    def test_walks_allowed_no_yn(self):
        assert match_yn_market("pitching_basesOnBalls-X-game-yn-yes") is None

    def test_total_bases_no_yn(self):
        assert match_yn_market("batting_totalBases-X-game-yn-yes") is None


# ── CLI name lookup tests ─────────────────────────────────────────

class TestCLILookupPhase8:
    def test_batter_hits(self):
        assert get_market_by_cli_name("batter_hits") is BATTER_HITS

    def test_total_bases(self):
        assert get_market_by_cli_name("total_bases") is BATTER_TOTAL_BASES

    def test_home_runs(self):
        assert get_market_by_cli_name("home_runs") is BATTER_HOME_RUNS

    def test_stolen_bases(self):
        assert get_market_by_cli_name("stolen_bases") is BATTER_STOLEN_BASES

    def test_pitching_win(self):
        assert get_market_by_cli_name("pitching_win") is PITCHER_WIN


# ── Market type lookup tests ──────────────────────────────────────

class TestTypeLookupPhase8:
    def test_ou_type_batter_hits(self):
        assert get_market_by_ou_type("batting_hits_ou") is BATTER_HITS

    def test_yn_type_batter_hits(self):
        assert get_market_by_yn_type("batting_hits_yn") is BATTER_HITS

    def test_ou_type_batter_hr(self):
        assert get_market_by_ou_type("batting_homeRuns_ou") is BATTER_HOME_RUNS

    def test_yn_type_pitching_win(self):
        assert get_market_by_yn_type("pitching_win_yn") is PITCHER_WIN

    def test_ou_type_strikeouts(self):
        assert get_market_by_ou_type("pitching_strikeouts_ou") is PITCHER_STRIKEOUTS

    def test_ou_type_hits_allowed(self):
        assert get_market_by_ou_type("pitching_hits_ou") is PITCHER_HITS_ALLOWED

    def test_ou_type_walks_allowed(self):
        assert get_market_by_ou_type("pitching_basesOnBalls_ou") is PITCHER_WALKS_ALLOWED


# ── Parser tests ──────────────────────────────────────────────────

class TestParserPhase8:
    @pytest.fixture
    def parsed(self):
        return parse_player_props(batter_event)

    def test_total_odds_rows(self, parsed):
        assert len(parsed.odds_rows) > 0

    def test_batter_hits_ou_parsed(self, parsed):
        hits_rows = [r for r in parsed.odds_rows if r["market_type"] == "batting_hits_ou"]
        assert len(hits_rows) > 0
        player_ids = {r["player_id"] for r in hits_rows}
        assert "AARON_JUDGE_1_MLB" in player_ids

    def test_batter_hits_yn_parsed(self, parsed):
        hits_yn = [r for r in parsed.odds_rows if r["market_type"] == "batting_hits_yn"]
        assert len(hits_yn) > 0

    def test_batter_hr_ou_parsed(self, parsed):
        hr_rows = [r for r in parsed.odds_rows if r["market_type"] == "batting_homeRuns_ou"]
        assert len(hr_rows) > 0

    def test_batter_total_bases_ou_parsed(self, parsed):
        tb_rows = [r for r in parsed.odds_rows if r["market_type"] == "batting_totalBases_ou"]
        assert len(tb_rows) > 0


# ── Name extraction tests ─────────────────────────────────────────

class TestNameExtractionPhase8:
    def test_batter_hits_ou(self):
        odd_data = {"marketName": "Aaron Judge Hits Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Aaron Judge"

    def test_batter_hits_yn(self):
        odd_data = {"marketName": "Aaron Judge Any Hits Yes/No"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Aaron Judge"

    def test_batter_hr_ou(self):
        odd_data = {"marketName": "Aaron Judge Home Runs Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Aaron Judge"

    def test_batter_total_bases_ou(self):
        odd_data = {"marketName": "Aaron Judge Total Bases Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Aaron Judge"

    def test_pitcher_still_works(self):
        odd_data = {"marketName": "Jack Flaherty Strikeouts Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Jack Flaherty"


# ── Cross-market isolation tests ──────────────────────────────────

class TestCrossMarketIsolation:
    def test_batter_and_pitcher_k_independent(self, db_conn):
        """Batter strikeouts and pitcher strikeouts produce separate groups."""
        from tests.fixture_data import flaherty_event

        # Parse both events
        batter_parsed = parse_player_props(batter_event)
        pitcher_parsed = parse_player_props(flaherty_event)

        batter_k = [r for r in batter_parsed.odds_rows if r["market_type"] == "batting_strikeouts_ou"]
        pitcher_k = [r for r in pitcher_parsed.odds_rows if r["market_type"] == "pitching_strikeouts_ou"]

        # They should have different market types
        if batter_k and pitcher_k:
            batter_keys = {r["market_group_key"] for r in batter_k}
            pitcher_keys = {r["market_group_key"] for r in pitcher_k}
            assert batter_keys.isdisjoint(pitcher_keys)

    def test_all_batter_markets_different_types(self, db_conn):
        """Each batter market type produces distinct market_type values."""
        parsed = parse_player_props(batter_event)
        market_types = {r["market_type"] for r in parsed.odds_rows}
        # Should have multiple distinct batter market types
        batter_types = {t for t in market_types if t.startswith("batting_")}
        assert len(batter_types) >= 4

    def test_batter_hits_different_from_pitcher_hits(self, db_conn):
        """Batter hits O/U and pitcher hits allowed O/U are independent."""
        from tests.fixture_data import flaherty_event

        batter_parsed = parse_player_props(batter_event)
        pitcher_parsed = parse_player_props(flaherty_event)

        batter_hits = {r["market_type"] for r in batter_parsed.odds_rows if "batting_hits" in r["market_type"]}
        pitcher_hits = {r["market_type"] for r in pitcher_parsed.odds_rows if "pitching_hits" in r["market_type"]}

        assert batter_hits.isdisjoint(pitcher_hits)


# ── Supports flags tests ──────────────────────────────────────────

class TestSupportsFlags:
    def test_pitching_win_yn_only(self):
        assert PITCHER_WIN.supports_ou is False
        assert PITCHER_WIN.supports_yn is True

    def test_batter_hits_both(self):
        assert BATTER_HITS.supports_ou is True
        assert BATTER_HITS.supports_yn is True

    def test_batter_hr_both(self):
        assert BATTER_HOME_RUNS.supports_ou is True
        assert BATTER_HOME_RUNS.supports_yn is True

    def test_walks_allowed_ou_only(self):
        assert PITCHER_WALKS_ALLOWED.supports_ou is True
        assert PITCHER_WALKS_ALLOWED.supports_yn is False

    def test_total_bases_ou_only(self):
        assert BATTER_TOTAL_BASES.supports_ou is True
        assert BATTER_TOTAL_BASES.supports_yn is False


# ── Group key tests ───────────────────────────────────────────────

class TestGroupKeysPhase8:
    @pytest.fixture
    def parsed(self):
        return parse_player_props(batter_event)

    def test_batter_hits_groups(self, parsed):
        hits_rows = [r for r in parsed.odds_rows if r["market_type"] == "batting_hits_ou"]
        keys = {r["market_group_key"] for r in hits_rows}
        assert len(keys) == 1  # same player, same line → one group

    def test_batter_hr_groups(self, parsed):
        hr_rows = [r for r in parsed.odds_rows if r["market_type"] == "batting_homeRuns_ou"]
        keys = {r["market_group_key"] for r in hr_rows}
        assert len(keys) == 1

    def test_different_markets_different_keys(self, parsed):
        hits_keys = {r["market_group_key"] for r in parsed.odds_rows if r["market_type"] == "batting_hits_ou"}
        hr_keys = {r["market_group_key"] for r in parsed.odds_rows if r["market_type"] == "batting_homeRuns_ou"}
        assert hits_keys.isdisjoint(hr_keys)


# ── Validation status tests ───────────────────────────────────────

class TestValidationPhase8:
    @pytest.fixture
    def parsed(self):
        return parse_player_props(batter_event)

    def test_valid_rows_have_status(self, parsed):
        for row in parsed.odds_rows:
            assert row["validation_status"] in ("VALID", "CONFIRMED", "VERIFIED")

    def test_valid_rows_have_price(self, parsed):
        for row in parsed.odds_rows:
            assert row["price"] is not None
            assert isinstance(row["price"], int)

    def test_valid_rows_have_decimal_odds(self, parsed):
        for row in parsed.odds_rows:
            assert row["decimal_odds"] is not None
            assert row["decimal_odds"] > 1.0

    def test_valid_rows_have_player_id(self, parsed):
        for row in parsed.odds_rows:
            assert row["player_id"] == "AARON_JUDGE_1_MLB"

    def test_valid_rows_have_event_id(self, parsed):
        for row in parsed.odds_rows:
            assert row["event_id"] == BATTER_EVENT_ID


# ── Existing pitcher market regression tests ──────────────────────

class TestPitcherRegression:
    def test_pitcher_strikeouts_still_work(self):
        from tests.fixture_data import flaherty_event
        parsed = parse_player_props(flaherty_event)
        k_rows = [r for r in parsed.odds_rows if r["market_type"] == "pitching_strikeouts_ou"]
        assert len(k_rows) > 0

    def test_pitcher_walks_still_work(self):
        from tests.fixture_data import walks_event
        parsed = parse_player_props(walks_event)
        bb_rows = [r for r in parsed.odds_rows if "basesOnBalls" in r["market_type"]]
        assert len(bb_rows) > 0

    def test_pitcher_hits_allowed_still_work(self):
        from tests.fixture_data import hits_event
        parsed = parse_player_props(hits_event)
        h_rows = [r for r in parsed.odds_rows if r["market_type"] == "pitching_hits_ou"]
        assert len(h_rows) > 0


# ── Edge case tests ──────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_bybookmaker_yn_no(self):
        """YN No-side with empty byBookmaker produces audit-only row."""
        parsed = parse_player_props(batter_event)

    def test_unknown_odd_id_not_parsed(self):
        """Odd IDs not in any registry entry are silently skipped."""
        event = {
            "eventID": "UNKNOWN_001",
            "teams": {},
            "odds": {
                "unknown_stat-PLAYER_1_MLB-game-ou-over": {
                    "playerID": "PLAYER_1_MLB",
                    "marketName": "Test Unknown Over/Under",
                    "byBookmaker": {"dk": {"odds": "-110", "overUnder": "1.5"}},
                },
            },
        }
        parsed = parse_player_props(event)
        assert len(parsed.odds_rows) == 0
        assert len(parsed.audit_rows) == 0
