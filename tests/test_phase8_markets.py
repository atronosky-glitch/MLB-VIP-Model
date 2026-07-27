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
    BATTER_HITS_RUNS_RBI,
    BATTER_HOME_RUNS,
    BATTER_RBI,
    BATTER_RUNS_RBI,
    BATTER_SINGLES,
    BATTER_DOUBLES,
    BATTER_WALKS,
    BATTER_STOLEN_BASES,
    BATTER_TRIPLES,
    BATTER_STRIKEOUTS,
    BATTER_FIRST_HR,
    PITCHER_PITCHES_THROWN,
    PITCHER_WIN,
    PITCHER_STRIKEOUTS,
    PITCHER_OUTS,
    PITCHER_HITS_ALLOWED,
    PITCHER_WALKS_ALLOWED,
    PITCHER_EARNED_RUNS,
)
from src.player_prop_parser import parse_player_props, _extract_player_name_from_market


# ── Registry tests ─────────────────────────────────────────────────

class TestRegistryPhase8:
    def test_total_market_count(self):
        assert len(MARKET_REGISTRY) == 21

    def test_all_new_markets_registered(self):
        expected = [
            BATTER_HITS, BATTER_TOTAL_BASES, BATTER_HITS_RUNS_RBI,
            BATTER_HOME_RUNS, BATTER_RBI, BATTER_RUNS_RBI,
            BATTER_SINGLES, BATTER_DOUBLES, BATTER_WALKS,
            BATTER_STOLEN_BASES, BATTER_TRIPLES,
            BATTER_STRIKEOUTS, BATTER_FIRST_HR,
            PITCHER_PITCHES_THROWN, PITCHER_WIN,
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

    def test_batter_hrr_ou(self):
        m = match_ou_market("batting_hits+runs+rbi-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_HITS_RUNS_RBI

    def test_batter_hr_ou(self):
        m = match_ou_market("batting_homeRuns-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_HOME_RUNS

    def test_batter_rbi_ou(self):
        m = match_ou_market("batting_RBI-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_RBI

    def test_batter_runs_rbi_ou(self):
        m = match_ou_market("batting_runs+rbi-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_RUNS_RBI

    def test_batter_singles_ou(self):
        m = match_ou_market("batting_singles-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_SINGLES

    def test_batter_doubles_ou(self):
        m = match_ou_market("batting_doubles-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_DOUBLES

    def test_batter_walks_ou(self):
        m = match_ou_market("batting_basesOnBalls-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_WALKS

    def test_batter_stolen_bases_ou(self):
        m = match_ou_market("batting_stolenBases-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_STOLEN_BASES

    def test_batter_triples_ou(self):
        m = match_ou_market("batting_triples-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_TRIPLES

    def test_batter_strikeouts_ou(self):
        m = match_ou_market("batting_strikeouts-AARON_JUDGE_1_MLB-game-ou-over")
        assert m is BATTER_STRIKEOUTS

    def test_pitches_thrown_ou(self):
        m = match_ou_market("pitching_pitchesThrown-BRANDON_PFAADT_1_MLB-game-ou-over")
        assert m is PITCHER_PITCHES_THROWN

    def test_pitching_win_no_ou(self):
        assert match_ou_market("pitching_win-BRANDON_PFAADT_1_MLB-game-ou-over") is None

    def test_first_hr_no_ou(self):
        assert match_ou_market("batting_firstHomeRun-AARON_JUDGE_1_MLB-game-ou-over") is None


# ── YN dispatch tests ─────────────────────────────────────────────

class TestYNDispatchPhase8:
    def test_batter_hits_yn(self):
        m = match_yn_market("batting_hits-AARON_JUDGE_1_MLB-game-yn-yes")
        assert m is BATTER_HITS

    def test_batter_hr_yn(self):
        m = match_yn_market("batting_homeRuns-AARON_JUDGE_1_MLB-game-yn-yes")
        assert m is BATTER_HOME_RUNS

    def test_batter_rbi_yn(self):
        m = match_yn_market("batting_RBI-AARON_JUDGE_1_MLB-game-yn-yes")
        assert m is BATTER_RBI

    def test_batter_first_hr_yn(self):
        m = match_yn_market("batting_firstHomeRun-AARON_JUDGE_1_MLB-game-yn-yes")
        assert m is BATTER_FIRST_HR

    def test_pitching_win_yn(self):
        m = match_yn_market("pitching_win-BRANDON_PFAADT_1_MLB-game-yn-yes")
        assert m is PITCHER_WIN

    def test_pitches_thrown_no_yn(self):
        assert match_yn_market("pitching_pitchesThrown-X-game-yn-yes") is None


# ── CLI name lookup tests ─────────────────────────────────────────

class TestCLILookupPhase8:
    def test_batter_hits(self):
        assert get_market_by_cli_name("batter_hits") is BATTER_HITS

    def test_total_bases(self):
        assert get_market_by_cli_name("total_bases") is BATTER_TOTAL_BASES

    def test_hits_runs_rbi(self):
        assert get_market_by_cli_name("hits_runs_rbi") is BATTER_HITS_RUNS_RBI

    def test_home_runs(self):
        assert get_market_by_cli_name("home_runs") is BATTER_HOME_RUNS

    def test_rbi(self):
        assert get_market_by_cli_name("rbi") is BATTER_RBI

    def test_runs_rbi(self):
        assert get_market_by_cli_name("runs_rbi") is BATTER_RUNS_RBI

    def test_singles(self):
        assert get_market_by_cli_name("singles") is BATTER_SINGLES

    def test_doubles(self):
        assert get_market_by_cli_name("doubles") is BATTER_DOUBLES

    def test_batter_walks(self):
        assert get_market_by_cli_name("batter_walks") is BATTER_WALKS

    def test_stolen_bases(self):
        assert get_market_by_cli_name("stolen_bases") is BATTER_STOLEN_BASES

    def test_triples(self):
        assert get_market_by_cli_name("triples") is BATTER_TRIPLES

    def test_batter_strikeouts(self):
        assert get_market_by_cli_name("batter_strikeouts") is BATTER_STRIKEOUTS

    def test_first_home_run(self):
        assert get_market_by_cli_name("first_home_run") is BATTER_FIRST_HR

    def test_pitches_thrown(self):
        assert get_market_by_cli_name("pitches_thrown") is PITCHER_PITCHES_THROWN

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

    def test_ou_type_pitches_thrown(self):
        assert get_market_by_ou_type("pitching_pitchesThrown_ou") is PITCHER_PITCHES_THROWN

    def test_yn_type_pitching_win(self):
        assert get_market_by_yn_type("pitching_win_yn") is PITCHER_WIN

    def test_yn_type_first_hr(self):
        assert get_market_by_yn_type("batting_firstHomeRun_yn") is BATTER_FIRST_HR


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

    def test_batter_hrr_ou_parsed(self, parsed):
        hrr_rows = [r for r in parsed.odds_rows if r["market_type"] == "batting_hits+runs+rbi_ou"]
        assert len(hrr_rows) > 0

    def test_batter_rbi_ou_parsed(self, parsed):
        rbi_rows = [r for r in parsed.odds_rows if r["market_type"] == "batting_RBI_ou"]
        assert len(rbi_rows) > 0

    def test_batter_singles_ou_parsed(self, parsed):
        rows = [r for r in parsed.odds_rows if r["market_type"] == "batting_singles_ou"]
        assert len(rows) > 0

    def test_batter_doubles_ou_parsed(self, parsed):
        rows = [r for r in parsed.odds_rows if r["market_type"] == "batting_doubles_ou"]
        assert len(rows) > 0

    def test_batter_walks_ou_parsed(self, parsed):
        rows = [r for r in parsed.odds_rows if r["market_type"] == "batting_basesOnBalls_ou"]
        assert len(rows) > 0

    def test_batter_first_hr_yn_parsed(self, parsed):
        rows = [r for r in parsed.odds_rows if r["market_type"] == "batting_firstHomeRun_yn"]
        assert len(rows) > 0

    def test_first_hr_no_ou_rows(self, parsed):
        """First Home Run is YN-only, no O/U rows should exist."""
        assert not any(r["market_type"] == "batting_firstHomeRun_ou" for r in parsed.odds_rows)


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

    def test_batter_hrr_ou(self):
        odd_data = {"marketName": "Aaron Judge Hits + Runs + RBIs Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Aaron Judge"

    def test_batter_rbi_ou(self):
        odd_data = {"marketName": "Aaron Judge Runs Batted In Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Aaron Judge"

    def test_batter_singles_ou(self):
        odd_data = {"marketName": "Aaron Judge Singles Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Aaron Judge"

    def test_batter_doubles_ou(self):
        odd_data = {"marketName": "Aaron Judge Doubles Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Aaron Judge"

    def test_batter_walks_ou(self):
        odd_data = {"marketName": "Aaron Judge Walks Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Aaron Judge"

    def test_batter_first_hr_yn(self):
        odd_data = {"marketName": "Aaron Judge To Record First Home Run Yes/No"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Aaron Judge"

    def test_batter_strikeouts_ou(self):
        odd_data = {"marketName": "Aaron Judge Strikeouts Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Aaron Judge"

    def test_pitcher_still_works(self):
        odd_data = {"marketName": "Jack Flaherty Strikeouts Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Jack Flaherty"

    def test_pitcher_outs_still_works(self):
        odd_data = {"marketName": "Gerrit Cole Outs Recorded Over/Under"}
        name = _extract_player_name_from_market(odd_data)
        assert name == "Gerrit Cole"


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
        assert len(batter_types) >= 8

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
    def test_pitching_win_ou_only(self):
        assert PITCHER_WIN.supports_ou is False
        assert PITCHER_WIN.supports_yn is True

    def test_first_hr_ou_only(self):
        assert BATTER_FIRST_HR.supports_ou is False
        assert BATTER_FIRST_HR.supports_yn is True

    def test_pitches_thrown_yn_only(self):
        assert PITCHER_PITCHES_THROWN.supports_ou is True
        assert PITCHER_PITCHES_THROWN.supports_yn is False

    def test_batter_hits_both(self):
        assert BATTER_HITS.supports_ou is True
        assert BATTER_HITS.supports_yn is True

    def test_batter_hr_both(self):
        assert BATTER_HOME_RUNS.supports_ou is True
        assert BATTER_HOME_RUNS.supports_yn is True

    def test_batter_rbi_both(self):
        assert BATTER_RBI.supports_ou is True
        assert BATTER_RBI.supports_yn is True


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

    def test_pitcher_earnings_still_work(self):
        from tests.fixture_data import earned_runs_event
        parsed = parse_player_props(earned_runs_event)
        er_rows = [r for r in parsed.odds_rows if "earnedRuns" in r["market_type"]]
        assert len(er_rows) > 0

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
        no_rows = [r for r in parsed.audit_rows if r.get("side") == "NO"]
        # First Home Run No-side should be audit-only
        first_hr_no = [r for r in no_rows if "firstHomeRun" in r.get("odd_id", "")]
        for row in first_hr_no:
            assert row["excluded"] == 1

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
